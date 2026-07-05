"""FastAPI app — board API + static UI + the /s/<id>/* proxy (DESIGN §3/§10).

``create_app`` wires Store + Router + Orchestrator + KeepAliveManager. The
orchestrator/keepalive collaborators are injectable so the API is testable
without spawning agent-cli.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent_board import instances, models_registry, sessions
from agent_board.config import Config
from agent_board.live_events import LiveEvents
from agent_board.keepalive import (
    KeepAliveManager,
    default_port_for,
    make_sse_connect,
)
from agent_board.orchestrator import Orchestrator, RealBackend
from agent_board.router import BoardProxyRouter, CaddyRouter, Router
from agent_board.store import Store

_STATIC = Path(__file__).parent / "static"

# default board port: 0xCAFE (51966). agent-cli web defaults to 0xC0DE (49374),
# so the two don't collide out of the box; both are below the instance port
# range (50000-60000) and clear of omlx-server's 8000.
DEFAULT_PORT = 0xCAFE


def pick_board_port(host: str, preferred: int) -> int:
    """``preferred`` if bindable, else an OS-assigned free port — so a second
    board (or a leftover on the port) starts on a fresh port instead of dying
    with "address already in use". Mirrors agent-cli's web ``pick_port``."""
    import socket

    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    for candidate in (preferred, 0):
        if candidate:  # a LIVE listener already answers → skip to fallback
            with socket.socket() as c:
                c.settimeout(0.2)
                if c.connect_ex((probe_host, candidate)) == 0:
                    continue
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, candidate))
            except OSError:
                continue
            return s.getsockname()[1]
    return preferred  # let uvicorn surface the error


def gateway_banner(config: Config) -> str:
    """One-line description of the active routing data plane, for the startup
    log — so an operator can see AT A GLANCE whether the board itself is
    proxying (default) or Caddy is (and where its admin API is). The gateway
    was previously silent, which made it easy to assume Caddy while actually
    running the in-process proxy."""
    if config.gateway == "caddy":
        return f"caddy (admin {config.caddy_admin})"
    return "board-proxy (in-process reverse proxy — default)"


def acquire_singleton_lock(data_dir: Path) -> int | None:
    """Single-instance guard: hold an exclusive ``flock`` on
    ``<data_dir>/board.lock`` for the process lifetime. Returns the open fd on
    success (the caller MUST keep it — closing releases the lock), or ``None``
    if another board already holds it.

    Two boards on the same ``data_dir`` would race on the shared ``board.db``
    and, lacking cross-process spawn coordination (the per-post lock is a
    process-local ``asyncio.Lock``), could double-spawn the same post's instance
    into one workspace. The kernel drops the lock automatically when the holder
    dies, so a crash leaves no stale lock to clean up (unlike a bare pidfile)."""
    import fcntl
    import os

    data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(data_dir / "board.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())  # pidfile content: who holds it
    return fd


def build_log_config(log_file: str | Path) -> dict:
    """uvicorn logging config: access logs (the /api/posts polling) → a rotating
    file so the console stays clean; startup + errors still print to stderr."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(message)s",
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(asctime)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            },
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            },
            "access_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "access",
                "filename": str(log_file),
                "maxBytes": 5_000_000,
                "backupCount": 3,
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["access_file"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }


class NewPost(BaseModel):
    topic: str
    model_id: str | None = None


class ForceActive(BaseModel):
    enabled: bool


class SetModel(BaseModel):
    model_id: str | None = None


def _post_view(config: Config, store: Store, post) -> dict:
    """A post + its derived (live) fields for the list."""
    ws = config.workspace_for(post.post_id)
    lq = sessions.last_query_record(ws, post.session_id)
    state = sessions.live_state(ws, post.session_id)
    status = state["status"]
    # human viewers = live subscribers minus the force-active keep-alive's own one
    viewers = max(0, state.get("viewers", 0) - (1 if post.force_active else 0))
    # model is changeable only when nobody is watching: down, or up-and-idle with
    # 0 human viewers (mirrors orchestrator.change_model's gate).
    model_changeable = status == "idle" or (status == "running" and viewers == 0)
    return {
        "post_id": post.post_id,
        "topic": post.topic,
        "model_id": post.model_id,
        "force_active": post.force_active,
        "created_at": post.created_at,
        "last_query": lq["text"] if lq else None,
        "last_query_at": lq.get("ts") if lq else None,
        "status": status,
        "awaiting_input": state["awaiting_input"],
        "viewers": viewers,
        "model_changeable": model_changeable,
    }


async def restore_state(config: Config, store: Store, router, keepalive) -> None:
    """After a board restart the in-memory route map + keepalive tasks are gone,
    but detached instances may still be alive (start_new_session). Re-register a
    route for each live instance and restore force-active keepalives so an
    already-open browser keeps working without a manual re-open."""
    loop = asyncio.get_event_loop()
    posts = await loop.run_in_executor(None, store.list_posts)
    for post in posts:
        if post.session_id:
            ws = config.workspace_for(post.post_id)
            info = await loop.run_in_executor(
                None, instances.read_web_json, ws, post.session_id
            )
            if info and await loop.run_in_executor(None, instances.alive, info):
                router.ensure_route(post.post_id, info["port"])
        if post.force_active:
            await keepalive.enable(post.post_id)


def create_app(
    config: Config,
    *,
    store: Store | None = None,
    router: Router | None = None,
    orchestrator=None,
    keepalive=None,
) -> FastAPI:
    store = store or Store(config.db_path)
    if router is None:
        # gateway=caddy → Caddy proxies /s/<id> (board out of the data path);
        # board-proxy (default) → the board reverse-proxies it in-process.
        if config.gateway == "caddy":
            router = CaddyRouter(config.caddy_admin, basic_auth=config.caddy_basic_auth)
        else:
            router = BoardProxyRouter()
    if orchestrator is None:
        orchestrator = Orchestrator(config, store, backend=RealBackend(config, router))
    if keepalive is None:
        keepalive = KeepAliveManager(
            connect=make_sse_connect(default_port_for(config, store))
        )
    # revive a self-reaped instance when its old /s/<id> URL is hit: board-proxy
    # catches the dead-port ConnectError; Caddy falls through to the board's
    # revive handler (its route was dropped on the death edge). Both reopen.
    if hasattr(router, "set_reopen"):
        router.set_reopen(orchestrator.open)

    # Live push: an mtime scanner broadcasts changed rows to EventSource clients
    # so the browser doesn't poll /api/posts (Phase 2). view_fn injected to keep
    # live_events out of the app layer. on_death → drop the dead instance's
    # gateway route so a stale /s/<id> hit revives (Caddy falls through to the
    # board revive handler; board-proxy re-revives on next access).
    live = LiveEvents(
        config,
        store,
        lambda p: _post_view(config, store, p),
        on_death=router.remove_route,
    )

    @asynccontextmanager
    async def lifespan(_app):
        await restore_state(config, store, router, keepalive)
        scanner = asyncio.create_task(live.run())
        try:
            yield
        finally:
            scanner.cancel()
            if hasattr(router, "aclose"):
                await router.aclose()  # release the router's httpx client

    app = FastAPI(title="agent-board", lifespan=lifespan)
    router.mount(app)  # /s/<post_id>/* reverse proxy

    @app.get("/")
    async def index():
        return FileResponse(_STATIC / "index.html")

    if _STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC), name="static")

    @app.get("/api/models")
    async def list_models():
        # selectable models from agent-cli's registry (admin-managed)
        return models_registry.list_models(config.models_json)

    @app.get("/api/posts")
    async def list_posts():
        loop = asyncio.get_event_loop()
        posts = await loop.run_in_executor(None, store.list_posts)
        return [_post_view(config, store, p) for p in posts]

    @app.get("/api/events")
    async def events():
        """SSE stream of live row changes (``post_update`` / ``post_removed``).
        A ``ping`` every 15s (idle) is a real message event so the frontend's
        watchdog can detect a half-open connection and reconnect. The browser
        does a full ``load()`` on (re)connect, so nothing missed during a gap."""

        async def gen():
            q = live.subscribe()
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield 'data: {"type": "ping"}\n\n'  # heartbeat
                        continue
                    yield f"data: {json.dumps(msg)}\n\n"
            finally:
                live.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/api/posts")
    async def create_post(body: NewPost):
        post = store.create_post(topic=body.topic, model_id=body.model_id)
        ws = config.workspace_for(post.post_id)
        try:
            ws.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            store.delete(post.post_id)  # roll back the row (no orphan)
            raise HTTPException(status_code=500, detail=f"workspace: {e}") from e
        return _post_view(config, store, post)

    @app.delete("/api/posts/{post_id}")
    async def delete_post(post_id: str):
        post = store.get(post_id)
        if post is None:
            raise HTTPException(status_code=404, detail="no such post")
        await keepalive.disable(post_id)
        # deregister the gateway route (Caddy) / in-memory map (board-proxy) —
        # else a deleted post leaves a dangling /s/<id> route behind.
        router.remove_route(post_id)
        # kill the running instance BEFORE removing its workspace, else it is
        # orphaned with a deleted cwd (fails to save its session on exit).
        instances.stop_instance(config.workspace_for(post_id), post.session_id)
        ws = config.workspace_for(post_id).resolve()
        # safety: only ever remove a board-owned dir under the workspaces root
        if config.workspaces_root in ws.parents and ws.is_dir():
            shutil.rmtree(ws, ignore_errors=True)
        store.delete(post_id)
        return JSONResponse({"deleted": post_id})

    @app.post("/api/posts/{post_id}/open")
    async def open_post(post_id: str):
        if store.get(post_id) is None:
            raise HTTPException(status_code=404, detail="no such post")
        try:
            url = await orchestrator.open(post_id)
        except KeyError as e:  # belt-and-suspenders (race: deleted mid-open)
            raise HTTPException(status_code=404, detail="no such post") from e
        return JSONResponse({"url": url})

    @app.post("/api/posts/{post_id}/restart")
    async def restart_post(post_id: str):
        # Force-restart the instance (stop + respawn) so a freshly installed
        # agent-cli is picked up. Always allowed (no busy/viewer gate); the same
        # token is reused so open viewers reconnect without re-opening.
        if store.get(post_id) is None:
            raise HTTPException(status_code=404, detail="no such post")
        try:
            url = await orchestrator.restart(post_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="no such post") from e
        return JSONResponse({"url": url})

    @app.post("/api/posts/{post_id}/model")
    async def change_model(post_id: str, body: SetModel):
        if store.get(post_id) is None:
            raise HTTPException(status_code=404, detail="no such post")
        try:
            result = await orchestrator.change_model(post_id, body.model_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="no such post") from e
        if not result["ok"]:
            # blocked by the gate (busy / someone watching) — 409 Conflict so the
            # frontend can revert the dropdown and explain why.
            raise HTTPException(status_code=409, detail=result["reason"])
        return JSONResponse(result)

    @app.post("/api/posts/{post_id}/force_active")
    async def set_force_active(post_id: str, body: ForceActive):
        if store.get(post_id) is None:
            raise HTTPException(status_code=404, detail="no such post")
        store.set_force_active(post_id, body.enabled)
        if body.enabled:
            await keepalive.enable(post_id)
        else:
            await keepalive.disable(post_id)
        return JSONResponse({"force_active": body.enabled})

    return app


def main() -> None:  # pragma: no cover
    import os
    import sys

    import uvicorn

    config = Config.from_env()
    # Single-instance guard: refuse to start a second board on the same data_dir
    # (would race on board.db + double-spawn instances). Held for the process
    # lifetime — assigned so the fd isn't dropped.
    lock_fd = acquire_singleton_lock(config.data_dir)
    if lock_fd is None:
        try:
            holder = (config.data_dir / "board.lock").read_text().strip()
        except OSError:
            holder = ""
        print(
            f"이미 이 data_dir 에서 agent-board 가 실행 중입니다: {config.data_dir}"
            + (f" (pid {holder})" if holder else ""),
            file=sys.stderr,
        )
        raise SystemExit(1)
    host = os.environ.get("AGENT_BOARD_HOST", "0.0.0.0")
    # AGENT_BOARD_PORT set → bind it exactly (fail loudly on conflict). Omitted →
    # prefer 0xCAFE but dynamically fall back to a free port if it's taken.
    explicit = os.environ.get("AGENT_BOARD_PORT")
    port = int(explicit) if explicit else pick_board_port(host, DEFAULT_PORT)
    config.data_dir.mkdir(parents=True, exist_ok=True)  # so the log file can open
    print(
        f"agent-board → http://localhost:{port}  (workspaces: {config.workspaces_root})"
    )
    print(f"  gateway    → {gateway_banner(config)}")
    print(f"  access log → {config.log_file}")
    # caddy mode + non-loopback bind = footgun: Caddy is meant to front the
    # board, but 0.0.0.0/external also exposes the board's own port directly.
    # Hitting THAT bypasses Caddy — /s/<id> lands on the revive fall-through,
    # which redirects to the same origin and loops to a 503. Behind Caddy the
    # board should bind loopback (deploy/agent-board.service uses 127.0.0.1).
    if config.gateway == "caddy" and host not in ("127.0.0.1", "::1", "localhost"):
        print(
            f"  ⚠️  gateway=caddy 인데 {host} 로 바인드됨 — 보드 포트({port})에 직접 접속하면 "
            "Caddy 를 우회해 /s/<id> 가 503 루프가 됩니다. 브라우저는 Caddy 주소로 접속하고, "
            "보드는 AGENT_BOARD_HOST=127.0.0.1 로 바인드하세요.",
            file=sys.stderr,
        )
    uvicorn.run(
        create_app(config),
        host=host,
        port=port,
        log_config=build_log_config(config.log_file),
    )


if __name__ == "__main__":  # python -m agent_board.app
    main()

"""FastAPI app — board API + static UI + the /s/<id>/* proxy (DESIGN §3/§10).

``create_app`` wires Store + Router + Orchestrator + KeepAliveManager. The
orchestrator/keepalive collaborators are injectable so the API is testable
without spawning agent-cli.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent_board import instances, sessions
from agent_board.config import Config
from agent_board.keepalive import (
    KeepAliveManager,
    default_port_for,
    make_sse_connect,
)
from agent_board.orchestrator import Orchestrator, RealBackend
from agent_board.router import BoardProxyRouter, CaddyRouter
from agent_board.store import Store

_STATIC = Path(__file__).parent / "static"


class NewPost(BaseModel):
    topic: str
    directive: str | None = None


class ForceActive(BaseModel):
    enabled: bool


def _post_view(config: Config, store: Store, post) -> dict:
    """A post + its derived (live) fields for the list."""
    ws = config.workspace_for(post.post_id)
    return {
        "post_id": post.post_id,
        "topic": post.topic,
        "force_active": post.force_active,
        "created_at": post.created_at,
        "last_query": sessions.last_query(ws, post.session_id),
        "status": sessions.status(ws, post.session_id),
    }


def create_app(
    config: Config,
    *,
    store: Store | None = None,
    router: BoardProxyRouter | None = None,
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

    app = FastAPI(title="agent-board")
    router.mount(app)  # /s/<post_id>/* reverse proxy

    @app.get("/")
    async def index():
        return FileResponse(_STATIC / "index.html")

    if _STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC), name="static")

    @app.get("/api/posts")
    async def list_posts():
        loop = asyncio.get_event_loop()
        posts = await loop.run_in_executor(None, store.list_posts)
        return [_post_view(config, store, p) for p in posts]

    @app.post("/api/posts")
    async def create_post(body: NewPost):
        post = store.create_post(topic=body.topic, directive=body.directive)
        ws = config.workspace_for(post.post_id)
        try:
            ws.mkdir(parents=True, exist_ok=True)
            if body.directive:
                d = ws / ".agent-cli"
                d.mkdir(parents=True, exist_ok=True)
                (d / "DIRECTIVE.md").write_text(body.directive, encoding="utf-8")
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

    import uvicorn

    config = Config.from_env()
    host = os.environ.get("AGENT_BOARD_HOST", "0.0.0.0")
    # default 8001, not 8000 — omlx-server (agent-cli's LLM backend) commonly
    # holds 8000.
    port = int(os.environ.get("AGENT_BOARD_PORT", "8001"))
    print(
        f"agent-board → http://localhost:{port}  (workspaces: {config.workspaces_root})"
    )
    uvicorn.run(create_app(config), host=host, port=port)


if __name__ == "__main__":  # python -m agent_board.app
    main()

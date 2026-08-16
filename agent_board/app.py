"""FastAPI app — board API + static UI + the /s/<id>/* proxy (DESIGN §3/§10).

``create_app`` wires Store + Router + Orchestrator + KeepAliveManager. The
orchestrator/keepalive collaborators are injectable so the API is testable
without spawning agent-cli.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent_board import (
    __version__,
    admin,
    cron,
    instances,
    models_registry,
    sched_contract,
    sessions,
)
from agent_board import clone as clone_mod
from agent_board.auth import AuthMiddleware
from agent_board.config import Config
from agent_board.keepalive import (
    KeepAliveManager,
    default_port_for,
    make_sse_connect,
)
from agent_board.live_events import LiveEvents
from agent_board.models import DEFAULT_SCHEDULE_NICKNAME
from agent_board.orchestrator import Orchestrator, RealBackend
from agent_board.router import BoardProxyRouter, CaddyRouter, Router
from agent_board.scheduler import Scheduler
from agent_board.store import Store


class _NoCacheStaticFiles(StaticFiles):
    """모든 정적 응답에 no-cache 스탬프 (agent-cli 동형 — 재기동만으로 UI 반영)."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


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
    return preferred  # let the ASGI server surface the bind error


def _new_session_id() -> str:
    """clone 대상 새 세션 id — agent-cli create_session 과 동형
    (``str(int(time.time()))``). 모듈 함수라 테스트가 monkeypatch 가능."""
    import time

    return str(int(time.time()))


def _h2_active(config: Config) -> bool:
    """HTTP/2 in effect for browser clients: caddy (h2 at the edge) or the board
    serving TLS itself (Hypercorn negotiates h2 via ALPN). Drives the frontend
    tab-guard release (/api/gateway) — it's a TRANSPORT fact, not a gateway name."""
    return config.gateway == "caddy" or config.tls_enabled


def gateway_banner(config: Config) -> str:
    """One-line description of the active routing data plane + transport, for the
    startup log — so an operator sees AT A GLANCE whether the board itself is
    proxying (default) or Caddy is, and whether TLS/h2 is on."""
    if config.gateway == "caddy":
        return f"caddy (admin {config.caddy_admin})"
    transport = "TLS, h2" if config.tls_enabled else "plaintext, h1"
    return f"board-proxy (in-process reverse proxy — default; {transport})"


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _load_or_create_auth_token(data_dir: Path) -> str:
    """Persisted auto-generated board token, ``0600`` in the data dir. Persisting
    (vs a fresh token per start) keeps open viewers' ``abt`` cookies valid across
    a board restart."""
    p = data_dir / "auth-token"
    try:
        existing = p.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # owner-only
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    return token


def resolve_auth_token(
    config: Config, host: str, *, allow_unauth_lan: bool = False
) -> tuple[str | None, str]:
    """Decide the board control-plane token — returns ``(token_or_None, source)``.
    ``None`` = auth off (``AuthMiddleware`` not installed).

    - explicit ``AGENT_BOARD_AUTH_TOKEN`` wins (stable; recommended for exposure)
    - ``caddy`` gateway → None (Caddy's ``basic_auth`` guards the control plane)
    - ``allow_unauth_lan`` → None (operator explicitly accepted unauth exposure)
    - non-loopback bind → auto-generate + persist a token, so exposing the board
      is safe-by-default (this bind was previously REFUSED with SystemExit)
    - loopback (default) → None (zero-config local use, unchanged)"""
    if config.auth_token:
        return config.auth_token, "env"
    if config.gateway == "caddy":
        return None, "caddy"
    if allow_unauth_lan:
        return None, "unauth-lan"
    if host not in _LOOPBACK_HOSTS:
        return _load_or_create_auth_token(config.data_dir), "auto"
    return None, "loopback"


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
    """Hypercorn ``logconfig_dict``: access logs (the /api/posts polling) → a
    rotating file so the console stays clean; startup + errors still print to
    stderr. Hypercorn interpolates the access atoms into the log MESSAGE itself
    (``%(h)s ... "%(r)s" %(s)s`` via ``access_log_format``), so the access
    formatter just prefixes a timestamp around ``%(message)s``. Standard
    ``logging`` formatters — no server-specific formatter dependency."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": "%(levelname)s %(message)s"},
            "access": {"format": "%(asctime)s %(message)s"},
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
            "hypercorn.error": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
            "hypercorn.access": {
                "handlers": ["access_file"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }


def build_hypercorn_config(config: Config, host: str, port: int):
    """Assemble the Hypercorn ``Config`` from the board config — kept separate so
    the cert/key/bind/log mapping is unit-testable without ``serve()``.

    TLS on (cert&key present) → Hypercorn negotiates HTTP/2 via ALPN (default
    ``['h2','http/1.1']``); absent → plaintext HTTP/1.1 (unchanged). Access logs
    go to the rotating file: ``accesslog='-'`` makes the access logger non-None
    (else Hypercorn skips access logging entirely), then ``logconfig_dict`` runs
    ``dictConfig`` which REPLACES that placeholder handler with our rotating file
    handler on the same ``hypercorn.access`` logger — so no console duplication."""
    from hypercorn.config import Config as HConfig

    hcfg = HConfig()
    hcfg.bind = [f"{host}:{port}"]
    if config.tls_enabled:
        hcfg.certfile = config.tls_cert
        hcfg.keyfile = config.tls_key
    hcfg.accesslog = "-"  # enable access logger; dictConfig re-homes its handler
    hcfg.errorlog = "-"
    hcfg.logconfig_dict = build_log_config(config.log_file)
    return hcfg


class NewPost(BaseModel):
    topic: str
    model_id: str | None = None
    # 대화방 clone (v1.20.0): 원본 post + 그 워크스페이스에서 복사할
    # 상대경로들. clone_from 만 주고 paths 를 비우면 아무것도 안 옮김
    # (fresh). `.agent-cli/sessions/<sid>` 가 포함되면 대화까지 이어받음.
    clone_from: str | None = None
    clone_paths: list[str] = []


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
        # 상주 에이전트 요약 (없으면 None — 프런트가 칩/상태 숨김)
        "agents": state.get("agents"),
        # ⏰ 예약 요약 — 카드 배지(개수)와 놓친 발화 배너용. 전체 목록은
        # 패널이 열릴 때 /api/posts/{id}/schedules 로 가져온다.
        "schedules": _schedules_summary(store, post.post_id),
    }


def _schedules_summary(store: Store, post_id: str) -> dict:
    scheds = store.list_schedules(post_id)
    return {
        "count": len(scheds),
        "missed": [
            {
                "schedule_id": s.schedule_id,
                "label": s.label or cron.describe(s.cron),
                "missed_at": s.missed_at,
            }
            for s in scheds
            if s.missed_at
        ],
    }


def _schedule_view(s) -> dict:
    """A schedule row for the API/UI — cron + human label + next fire."""
    next_fire = None
    human = s.cron
    try:
        spec = cron.parse(s.cron)
        human = cron.describe(s.cron)
        if s.enabled:
            next_fire = cron.next_fire(spec, datetime.now()).isoformat()
    except ValueError:
        pass  # corrupt cron → 원문 표기, next 없음
    return {
        "schedule_id": s.schedule_id,
        "post_id": s.post_id,
        "source": s.source,
        "cron": s.cron,
        "human": human,
        "prompt": s.prompt,
        "label": s.label,
        "nickname": s.nickname,
        # 발화 시 실제 쓰일 이름(미지정이면 기본값) — UI 표시·확인용
        "effective_nickname": s.nickname or DEFAULT_SCHEDULE_NICKNAME,
        "enabled": s.enabled,
        "created_at": s.created_at,
        "last_fired_at": s.last_fired_at,
        "missed_at": s.missed_at,
        "next_fire": next_fire,
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
    scheduler=None,
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
    # ⏰ 에이전트 파일 계약 (§7): 요청파일 변경을 live 스캐너가 감지하면 반영
    # 후 스케줄러를 깨움 (rearm 은 thread-safe — executor 스레드에서 호출됨).
    def _on_sched_requests(post) -> None:
        changed = sched_contract.apply_requests(
            store, post.post_id, config.workspace_for(post.post_id)
        )
        if changed:
            scheduler.rearm()

    live = LiveEvents(
        config,
        store,
        lambda p: _post_view(config, store, p),
        on_death=router.remove_route,
        on_sched_requests=_on_sched_requests,
    )

    # ⏰ 스케줄러 (docs/schedule-design.md) — sleep-until-next + rearm. 발화 =
    # spawn-or-attach(orchestrator.open) 후 인스턴스 /api/input 에 주입.
    def _sched_inject(post, prompt: str, nickname: str) -> None:
        if post is None or not post.session_id:
            raise RuntimeError("no session to inject into")
        instances.inject_prompt(
            config.workspace_for(post.post_id),
            post.session_id,
            prompt,
            nickname=nickname,
        )

    def _sched_changed(post_id: str) -> None:
        # 스케줄 상태 변화(발화·missed·UI 뮤테이션)를 그 행의 post_update 로
        # 즉시 push — 프런트 배지가 새로고침 없이 갱신된다. 에이전트 쪽 state
        # 파일도 함께 갱신해(§7 refresh) agent 의 다음 list 가 진실을 본다.
        try:
            sched_contract.refresh_state(store, post_id, config.workspace_for(post_id))
        except OSError:
            pass  # state 갱신 실패가 API/발화를 막으면 안 됨
        post = store.get(post_id)
        if post is not None:
            live._broadcast(
                {"type": "post_update", "post": _post_view(config, store, post)}
            )

    if scheduler is None:
        scheduler = Scheduler(
            store, orchestrator, inject_fn=_sched_inject, on_change=_sched_changed
        )

    @asynccontextmanager
    async def lifespan(_app):
        await restore_state(config, store, router, keepalive)
        scanner = asyncio.create_task(live.run())
        sched_task = asyncio.create_task(scheduler.run())
        try:
            yield
        finally:
            scanner.cancel()
            sched_task.cancel()
            if hasattr(router, "aclose"):
                await router.aclose()  # release the router's httpx client

    app = FastAPI(title="agent-board", lifespan=lifespan)
    # 컨트롤플레인 default-deny (auth.py): 토큰이 설정된 경우에만 설치 —
    # 미설정(로컬 기본)이면 미들웨어 자체가 없어 표면·성능 무변화. 설치되면
    # /api/* 가 fail-closed 로 보호되고 /s/<id> 방은 인스턴스 토큰이 지킨다.
    if config.auth_token:
        app.add_middleware(AuthMiddleware, token=config.auth_token)
    # 테스트 표면 (v1.18.1): death-edge→라우트 제거 배선(on_death=
    # router.remove_route)을 합동 검증할 수 있게 노출 — 배선 누락은
    # 양쪽 반쪽 유닛만으로는 안 잡힌다.
    app.state.live_events = live
    app.state.scheduler = scheduler
    router.mount(app)  # /s/<post_id>/* reverse proxy

    # no-cache — plain StaticFiles 는 Cache-Control 미설정이라 브라우저가
    # HTML/JS 를 휴리스틱 캐시 → 코드 교체 후에도 옛 UI 가 보임 (v1.11.0
    # admin 링크가 안 보이던 실사례; agent-cli _NoCacheStaticFiles 교훈).
    # no-store 가 아닌 no-cache 라 미변경 파일은 304 fast path 유지.
    _NO_CACHE = "no-cache, must-revalidate"

    @app.get("/")
    async def index():
        return FileResponse(
            _STATIC / "index.html", headers={"Cache-Control": _NO_CACHE}
        )

    if _STATIC.is_dir():
        app.mount("/static", _NoCacheStaticFiles(directory=_STATIC), name="static")

    @app.get("/api/models")
    async def list_models():
        # selectable models from agent-cli's registry (admin-managed)
        return models_registry.list_models(config.models_json)

    # ── Admin (⚙): agent-cli config.json / models.json 편집 ──────────
    # 도메인 로직은 admin.py (전송 계층 분리). 블로킹 I/O·프로브·탐지는
    # 전부 executor 오프로드 — SSE 이벤트루프 보호 (agent-cli C3 교훈).

    @app.get("/admin")
    async def admin_page():
        return FileResponse(
            _STATIC / "admin.html", headers={"Cache-Control": _NO_CACHE}
        )

    def _admin_call(fn, *args):
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(None, fn, *args)

    @app.get("/api/admin/config")
    async def admin_get_config():
        try:
            return await _admin_call(admin.get_config, config.agent_cli_config_json)
        except admin.AdminError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.put("/api/admin/config")
    async def admin_put_config(body: dict):
        try:
            return await _admin_call(
                admin.update_config, body, config.agent_cli_config_json
            )
        except admin.AdminError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/api/admin/models")
    async def admin_list_models():
        try:
            view = await _admin_call(
                admin.list_models_with_status,
                config.models_json,
                config.agent_cli_config_json,
            )
        except admin.AdminError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # wire format 바인딩 드롭다운 옵션 (예외 없음 — 미설치면 빈 목록)
        view["wire_formats"] = await _admin_call(admin.list_wire_format_names)
        return view

    @app.post("/api/admin/models/detect")
    async def admin_detect_model(body: dict):
        model_id = (body or {}).get("model", "")
        if not model_id:
            raise HTTPException(status_code=400, detail="model 필드가 필요합니다")
        try:
            entry = await _admin_call(
                admin.detect_model_entry, model_id, config.agent_cli_config_json
            )
        except admin.AdminError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"model": model_id, "entry": entry}

    @app.put("/api/admin/models/{model_id}")
    async def admin_put_model(model_id: str, body: dict):
        try:
            await _admin_call(
                admin.save_model_entry, model_id, body, config.models_json
            )
        except admin.AdminError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    @app.delete("/api/admin/models/{model_id}")
    async def admin_delete_model(model_id: str):
        removed = await _admin_call(
            admin.delete_model_entry, model_id, config.models_json
        )
        if not removed:
            raise HTTPException(status_code=404, detail="registry 에 없는 모델")
        return {"ok": True}

    @app.get("/api/posts")
    async def list_posts():
        loop = asyncio.get_event_loop()
        posts = await loop.run_in_executor(None, store.list_posts)
        return [_post_view(config, store, p) for p in posts]

    @app.get("/api/gateway")
    async def gateway_info():
        """프런트 탭 가드의 조건 스위치. 브라우저의 origin 당 6연결
        (HTTP/1.1) 풀 고갈은 board-proxy(모든 방=이 origin, 방/대시보드
        탭마다 SSE 1개 점유)에서만 위험 — h2(연결 1개 멀티플렉스)면 가드가
        스스로 물러난다. h2 는 caddy 게이트웨이 또는 board 자체 TLS(Hypercorn
        ALPN)로 활성 — 프런트는 gateway 이름이 아니라 이 h2 플래그를 본다."""
        return {"gateway": config.gateway, "h2": _h2_active(config)}

    @app.get("/api/version")
    async def version_info():
        """헤더에 표시할 버전 — board 자신 + board 가 방을 spawn 하는
        agent-cli 바이너리(config.agent_cli_bin). cli 는 부재/미인식이면
        None(프런트가 board 만 표시). cli_version 은 lru_cache 라 반복
        호출해도 프로세스당 1회만 실행."""
        return {
            "board": __version__,
            "cli": instances.cli_version(config.agent_cli_bin),
        }

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

    @app.get("/api/posts/{post_id}/tree")
    async def post_tree(post_id: str, path: str = ""):
        """원본 post 워크스페이스 한 레벨 목록 — clone 트리 피커용 (board
        가 fs 직접 읽음, 인스턴스 미기동이어도 동작). `.agent-cli` 포함."""
        if store.get(post_id) is None:
            raise HTTPException(status_code=404, detail="no such post")
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, clone_mod.list_tree, config.workspace_for(post_id), path
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/posts")
    async def create_post(body: NewPost):
        # clone 원본 검증 (선택). paths 있는데 원본 없으면 거절.
        if body.clone_paths and not body.clone_from:
            raise HTTPException(
                status_code=400, detail="clone_paths without clone_from"
            )
        if body.clone_from and store.get(body.clone_from) is None:
            raise HTTPException(status_code=404, detail="clone source not found")

        post = store.create_post(topic=body.topic, model_id=body.model_id)
        ws = config.workspace_for(post.post_id)
        try:
            ws.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            store.delete(post.post_id)  # roll back the row (no orphan)
            raise HTTPException(status_code=500, detail=f"workspace: {e}") from e

        if body.clone_from and body.clone_paths:
            src_ws = config.workspace_for(body.clone_from)
            # 새 session_id = post 생성 시각 기반(agent-cli 규칙과 동형:
            # str(int(time))). clone 은 첫 open 전에 배정돼 --resume 된다.
            new_sid = _new_session_id()
            loop = asyncio.get_event_loop()
            try:
                sid = await loop.run_in_executor(
                    None,
                    lambda: clone_mod.clone_paths(
                        src_ws, ws, body.clone_paths, new_session_id=new_sid
                    ),
                )
            except ValueError as e:
                # traversal 등 — 방·워크스페이스 롤백
                shutil.rmtree(ws, ignore_errors=True)
                store.delete(post.post_id)
                raise HTTPException(status_code=400, detail=str(e)) from e
            if sid is not None:
                # 세션까지 복제 → 첫 open 이 --resume <sid> 로 대화 이어받음.
                try:
                    store.set_session_id(post.post_id, sid)
                    post = store.get(post.post_id)
                except Exception:
                    pass  # sid 충돌 등 — fresh 로 강등(파일은 이미 복사됨)

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
        # ``wait_s``: 종료를 실제로 확인하고 나서 rmtree — signal-만-보내던
        # 종전엔 죽어가는 인스턴스의 마지막 status 발행이 지워진 경로를
        # 재생성해 고아 워크스페이스를 남겼다 (v1.24.0 레이스 봉합).
        await asyncio.to_thread(
            instances.stop_instance,
            config.workspace_for(post_id),
            post.session_id,
            wait_s=8.0,
        )
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

    # ── ⏰ 예약 API (docs/schedule-design.md §6) ─────────────

    @app.get("/api/posts/{post_id}/schedules")
    async def list_schedules_api(post_id: str):
        if store.get(post_id) is None:
            raise HTTPException(status_code=404, detail="no such post")
        return [_schedule_view(s) for s in store.list_schedules(post_id)]

    @app.post("/api/posts/{post_id}/schedules")
    async def add_schedule_api(post_id: str, body: dict):
        if store.get(post_id) is None:
            raise HTTPException(status_code=404, detail="no such post")
        expr = (body.get("cron") or "").strip()
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        try:
            cron.parse(expr)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"invalid cron: {e}") from e
        s = store.add_schedule(
            post_id=post_id,
            source="user",
            cron=expr,
            prompt=prompt,
            label=(body.get("label") or "").strip(),
            nickname=(body.get("nickname") or "").strip(),
        )
        scheduler.rearm()
        _sched_changed(post_id)
        return _schedule_view(s)

    def _get_sched_or_404(schedule_id: str):
        s = store.get_schedule(schedule_id)
        if s is None:
            raise HTTPException(status_code=404, detail="no such schedule")
        return s

    @app.delete("/api/schedules/{schedule_id}")
    async def delete_schedule_api(schedule_id: str):
        s = _get_sched_or_404(schedule_id)
        store.delete_schedule(schedule_id)
        scheduler.rearm()
        _sched_changed(s.post_id)
        return JSONResponse({"deleted": schedule_id})

    @app.post("/api/schedules/{schedule_id}/toggle")
    async def toggle_schedule_api(schedule_id: str, body: dict):
        s = _get_sched_or_404(schedule_id)
        store.set_schedule_enabled(schedule_id, bool(body.get("enabled")))
        scheduler.rearm()
        _sched_changed(s.post_id)
        return _schedule_view(store.get_schedule(schedule_id))

    @app.post("/api/schedules/{schedule_id}/run-now")
    async def run_now_api(schedule_id: str):
        """즉시 1회 발화 — 놓친 발화의 [지금 실행] 과 수동 ▶ 버튼 공용.
        성공 시 mark_fired 가 missed 도 함께 해소한다."""
        s = _get_sched_or_404(schedule_id)
        ok = await scheduler.fire(s)
        return JSONResponse({"ok": ok})

    @app.post("/api/schedules/{schedule_id}/dismiss-missed")
    async def dismiss_missed_api(schedule_id: str):
        s = _get_sched_or_404(schedule_id)
        store.clear_missed(schedule_id)
        _sched_changed(s.post_id)
        return JSONResponse({"ok": True})

    return app


def main() -> None:  # pragma: no cover
    import asyncio as _asyncio

    from hypercorn.asyncio import serve

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
    host = os.environ.get("AGENT_BOARD_HOST", "127.0.0.1")
    # Resolve the control-plane token BEFORE binding. Non-loopback bind now
    # auto-enables auth (persisted token) instead of being refused — the board is
    # safe to expose over its own TLS. caddy / ALLOW_UNAUTH_LAN keep auth off
    # (Caddy guards it / explicit risk). Loopback default → no auth (unchanged).
    allow_unauth_lan = os.environ.get("AGENT_BOARD_ALLOW_UNAUTH_LAN") == "1"
    token, src = resolve_auth_token(config, host, allow_unauth_lan=allow_unauth_lan)
    config.auth_token = token or ""
    # AGENT_BOARD_PORT set → bind it exactly (fail loudly on conflict). Omitted →
    # prefer 0xCAFE but dynamically fall back to a free port if it's taken.
    explicit = os.environ.get("AGENT_BOARD_PORT")
    port = int(explicit) if explicit else pick_board_port(host, DEFAULT_PORT)
    config.data_dir.mkdir(parents=True, exist_ok=True)  # so the log file can open
    scheme = "https" if config.tls_enabled else "http"
    print(
        f"agent-board → {scheme}://localhost:{port}  "
        f"(workspaces: {config.workspaces_root})"
    )
    print(f"  gateway    → {gateway_banner(config)}")
    print(f"  access log → {config.log_file}")
    # Auth banner: default-deny on when a token is active. Print the one-time
    # bootstrap URL so the operator (or an auto-generated token) can get in;
    # the ?token= installs the abt cookie and drops out of the URL after one hop.
    if config.auth_token:
        origin = f"{scheme}://{host}:{port}"
        note = {
            "env": "AGENT_BOARD_AUTH_TOKEN",
            "auto": f"자동생성·영속화({config.data_dir / 'auth-token'})",
        }.get(src, src)
        print(f"  auth       → ON (컨트롤플레인 default-deny; source={note})")
        print(f"  bootstrap  → {origin}/?token={config.auth_token}")
    elif host not in _LOOPBACK_HOSTS and config.gateway != "caddy":
        # allow_unauth_lan path — exposed WITHOUT auth by explicit operator opt-in.
        print(
            f"  ⚠️  {host} 로 바인드되었으나 인증이 꺼져 있습니다 "
            "(AGENT_BOARD_ALLOW_UNAUTH_LAN=1) — 컨트롤플레인이 무인증 노출됩니다.",
            file=sys.stderr,
        )
    if not config.tls_enabled and host not in _LOOPBACK_HOSTS:
        print(
            "  ⚠️  TLS 미설정으로 평문(h1)입니다 — 토큰·트래픽이 평문 전송됩니다. "
            "AGENT_BOARD_TLS_CERT/KEY 로 HTTPS(h2)를 켜세요.",
            file=sys.stderr,
        )
    # caddy mode + non-loopback bind = footgun: Caddy is meant to front the
    # board, but 0.0.0.0/external also exposes the board's own port directly.
    # Hitting THAT bypasses Caddy — /s/<id> lands on the revive fall-through,
    # which redirects to the same origin and loops to a 503. Behind Caddy the
    # board should bind loopback (deploy/agent-board.service uses 127.0.0.1).
    if config.gateway == "caddy" and host not in _LOOPBACK_HOSTS:
        print(
            f"  ⚠️  gateway=caddy 인데 {host} 로 바인드됨 — 보드 포트({port})에 직접 접속하면 "
            "Caddy 를 우회해 /s/<id> 가 503 루프가 됩니다. 브라우저는 Caddy 주소로 접속하고, "
            "보드는 AGENT_BOARD_HOST=127.0.0.1 로 바인드하세요.",
            file=sys.stderr,
        )
    _asyncio.run(serve(create_app(config), build_hypercorn_config(config, host, port)))


if __name__ == "__main__":  # python -m agent_board.app
    main()

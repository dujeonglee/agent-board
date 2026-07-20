"""Routing data plane (DESIGN §9).

``Router`` interface = ``ensure_route(post_id, port)`` / ``remove_route(post_id)``.

v1 uses ``BoardProxyRouter`` — the board itself reverse-proxies ``/s/<post_id>/*``
to ``127.0.0.1:<port>``, streaming both directions so SSE (``/api/stream``) passes
through chunk-by-chunk with NO buffering. (Production swaps in a ``CaddyRouter``
that registers routes in a separate gateway; the rest of the board is identical.)
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask


class Router(ABC):
    """The routing data-plane contract: how the board publishes and revives an
    instance's ``/s/<post_id>`` route. Two implementations that must stay at
    parity — ``BoardProxyRouter`` (in-process reverse proxy) and ``CaddyRouter``
    (external Caddy via admin API). They share NO mechanism, so this is a PURE
    interface (no shared implementation → no coupling); the emergent BEHAVIOUR
    that an interface can't express (e.g. revive-on-stale-hit, which silently
    diverged once) is pinned by ``tests/test_router_parity.py``.

    ★ When either side gains a routing capability, add it here as an
    ``@abstractmethod`` — the other router then fails to instantiate until it
    implements it (structural parity). For any observable *behaviour*, add a
    matching case to the parity test (structure can't enforce behaviour)."""

    @abstractmethod
    def ensure_route(self, post_id: str, port: int) -> None:
        """Publish ``/s/<post_id>/*`` → ``127.0.0.1:<port>`` (idempotent replace)."""

    @abstractmethod
    def remove_route(self, post_id: str) -> None:
        """Withdraw the route (idempotent — no error if absent)."""

    @abstractmethod
    def set_reopen(self, reopen) -> None:
        """Wire the ``async reopen(post_id)`` spawn-or-attach hook used to revive
        a self-reaped instance when its stale ``/s/<id>`` URL is hit."""

    @abstractmethod
    def mount(self, app: FastAPI) -> None:
        """Install the board-side handlers (full proxy / revive fall-through)."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release the router's resources on board shutdown (e.g. its httpx
        client). Idempotent — safe to call more than once."""


# response headers the proxy must NOT copy verbatim — they describe the upstream
# framing, which StreamingResponse re-derives for the chunked relay.
_DROP_RESP_HEADERS = {
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "content-type",  # passed via media_type instead
}


async def _relay_body(upstream: httpx.Response):
    """Relay the upstream response body, ending CLEANLY if the instance is
    stopped mid-stream (force-restart / idle-reap / model change).

    When the peer (agent-cli instance) is killed while an SSE stream
    (``/api/stream``) is open, it closes the socket without a clean chunked EOF,
    so httpx raises ``RemoteProtocolError`` ("peer closed connection without
    sending complete message body") — a ``TransportError`` — on the next read.
    Letting it bubble out of ``StreamingResponse`` surfaces as an unhandled ASGI
    500 in the board log. Swallowing it and ending the generator closes the
    browser's stream gracefully instead; its ``EventSource`` then reconnects
    through the board and lands on the restarted/revived instance."""
    try:
        async for chunk in upstream.aiter_raw():
            yield chunk
    except httpx.TransportError:
        return  # upstream vanished mid-stream — end the relay cleanly


class BoardProxyRouter(Router):
    def __init__(self):
        self._routes: dict[str, int] = {}  # post_id → upstream port
        # one shared client (connection pooling); no timeout so SSE can hang
        # open. trust_env=False: loopback proxying must bypass any corporate
        # HTTP proxy (which "Access Denied"s 127.0.0.1).
        self._client = httpx.AsyncClient(timeout=None, trust_env=False)
        # async reopen(post_id) — re-spawn/attach a stopped instance so hitting
        # the old /s/<id> URL after idle-reap revives it (wired to the
        # orchestrator's open). Must register the route before returning.
        self._reopen = None

    # ── Router interface ────────────────────────────────────
    def ensure_route(self, post_id: str, port: int) -> None:
        self._routes[post_id] = port

    def remove_route(self, post_id: str) -> None:
        self._routes.pop(post_id, None)

    def set_reopen(self, reopen) -> None:
        self._reopen = reopen

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _revive(self, post_id: str) -> int | None:
        """Re-open a stopped instance and return its fresh port (None if there's
        no reopen hook, the post is unknown, or the spawn failed)."""
        if self._reopen is None:
            return None
        try:
            await self._reopen(post_id)  # spawn-or-attach + ensure_route
        except Exception:
            return None
        return self._routes.get(post_id)

    # ── mount the catch-all proxy on the board app ──────────
    def mount(self, app: FastAPI) -> None:
        @app.api_route(
            "/s/{post_id}/{path:path}",
            methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
        )
        async def _proxy(post_id: str, path: str, request: Request):
            return await self._handle(request, post_id, path)

        @app.get("/s/{post_id}")
        async def _proxy_root(post_id: str):
            # canonicalise to the trailing-slash form so the instance's <base
            # href="/s/<id>/"> + relative URLs resolve correctly.
            return RedirectResponse(f"/s/{post_id}/")

    async def _handle(self, request: Request, post_id: str, path: str):
        # No route (board restarted / never opened here) → try to revive.
        port = self._routes.get(post_id) or await self._revive(post_id)
        if port is None:
            raise HTTPException(
                status_code=503, detail="instance stopped — reopen from the board"
            )
        try:
            return await self._proxy(request, post_id, path, port, body_stream=True)
        except httpx.TransportError:
            # upstream 이 사라진 두 경로가 모두 여기로 온다:
            #  ① idle-reap 된 dead 포트로 신규 연결 → ConnectError,
            #  ② kill/재시작으로 죽은 인스턴스의 stale keep-alive 풀을 초기
            #     send() 에서 재사용 → ReadError/RemoteProtocolError.
            # 좁게 (ConnectError, ConnectTimeout) 만 잡던 때는 ②가 _proxy 밖으로
            # 새어 uvicorn run_asgi 500 크래시("가끔 안 열림")가 됐다 — 부하 시
            # 잦음(실측 재현: kill 직후 반열림 풀 재사용 = ReadError). TransportError
            # (전송 실패 상위 — timeout=None 이라 타임아웃은 미발생)로 넓혀 ②도
            # 우아하게 처리(_relay_body 의 미스트림 catch 와 파리티).
            # Revive 는 안전 메서드(GET/HEAD)만 — 바디 재스트림 불가한 POST 는
            # 그대로 502(리로드가 재열기).
            if request.method in ("GET", "HEAD"):
                port = await self._revive(post_id)
                if port is not None:
                    return await self._proxy(
                        request, post_id, path, port, body_stream=False
                    )
            raise HTTPException(
                status_code=502, detail="instance stopped — reopen from the board"
            ) from None

    async def _proxy(
        self, request: Request, post_id: str, path: str, port: int, *, body_stream: bool
    ):
        url = httpx.URL(f"http://127.0.0.1:{port}/{path}")
        if request.url.query:
            url = url.copy_with(query=request.url.query.encode("utf-8"))
        fwd_headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

        # stream the request body UP (uploads) and the response DOWN (SSE). On a
        # retry (GET/HEAD) the body is already gone, so skip it.
        content = request.stream() if body_stream else None
        upstream_req = self._client.build_request(
            request.method, url, headers=fwd_headers, content=content
        )
        upstream = await self._client.send(upstream_req, stream=True)

        resp_headers = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower() not in _DROP_RESP_HEADERS
        }

        # Close the upstream via a BackgroundTask, NOT a generator ``finally``:
        # on client disconnect Starlette cancels the body iterator, and an
        # ``await aclose()`` inside the cancelled ``finally`` can itself be
        # interrupted before it completes — leaking the upstream SSE so agent-cli
        # keeps counting a viewer that has gone (roster grows on every browser
        # reconnect). The background task runs after the response ends (incl.
        # disconnect), outside the cancelled scope, so it always closes.
        return StreamingResponse(
            _relay_body(upstream),
            status_code=upstream.status_code,
            headers=resp_headers,
            media_type=upstream.headers.get("content-type"),
            background=BackgroundTask(upstream.aclose),
        )


class CaddyRouter(Router):
    """Production router (DESIGN §9): registers ``/s/<post_id>/*`` routes in a
    Caddy gateway via its admin API, so the board stays OUT of the data path
    (TLS, single port, restart-resilient streaming all handled by Caddy).

    ★ Auth-safety: each dynamic route EMBEDS the ``authentication`` handler
    before ``reverse_proxy`` when ``basic_auth`` is configured, so a proxied
    instance is never reachable unauthenticated — independent of where the
    route lands in Caddy's route list (no bypass via insertion order).
    """

    def __init__(
        self,
        admin_url: str = "http://127.0.0.1:2019",
        *,
        server: str = "srv0",
        basic_auth: str = "",
        client: httpx.Client | None = None,
    ):
        self._admin = admin_url.rstrip("/")
        self._server = server
        self._basic_auth = basic_auth  # "username:bcrypt-hash" or ""
        # trust_env=False: Caddy admin API is on loopback — bypass corporate proxy.
        self._client = client or httpx.Client(timeout=5.0, trust_env=False)
        # async reopen(post_id) — spawn-or-attach a self-reaped instance and
        # re-register its Caddy route. Wired to the orchestrator's open (below).
        self._reopen = None

    def set_reopen(self, reopen) -> None:
        self._reopen = reopen

    def mount(self, app: FastAPI) -> None:
        # Caddy proxies /s/<id> straight to a LIVE instance, so normally the
        # board is OUT of the data path. But when an instance self-reaps
        # (--idle-timeout), the board removes its dynamic route on the death edge
        # → Caddy's catch-all (Caddyfile: everything → board) now falls through
        # to THESE handlers. We spawn-or-attach (which re-registers the Caddy
        # route) and 302 back, so the retry lands on the revived instance —
        # parity with board-proxy's lazy revive. GET/HEAD only: a POST body
        # can't be replayed, so it 503s (reload from the board first).
        @app.api_route(
            "/s/{post_id}/{path:path}",
            methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"],
        )
        async def _revive(post_id: str, path: str, request: Request):
            return await self._revive_and_redirect(post_id, request)

        @app.api_route("/s/{post_id}", methods=["GET", "HEAD"])
        async def _revive_root(post_id: str, request: Request):
            return await self._revive_and_redirect(post_id, request)

    async def _revive_and_redirect(self, post_id: str, request: Request):
        if request.method not in ("GET", "HEAD"):
            # can't replay a body on the redirect retry — reload from the board.
            raise HTTPException(503, "instance stopped — reopen from the board")
        # Already redirected once but STILL fell through → the spawn failed or
        # the Caddy route hasn't applied yet. Don't loop: ask the browser to
        # retry shortly instead of reviving again.
        if "__revive" in request.query_params:
            raise HTTPException(
                503, "instance is starting — retry", headers={"Retry-After": "2"}
            )
        if self._reopen is None:
            raise HTTPException(503, "instance stopped — reopen from the board")
        try:
            await self._reopen(post_id)  # spawn-or-attach + ensure_route (Caddy PUT)
        except Exception:
            raise HTTPException(
                502, "instance stopped — reopen from the board"
            ) from None
        # route now registered in Caddy → bounce back; the retry hits the live
        # route (the __revive marker guards against a loop if it didn't).
        #
        # RELATIVE Location (path+query only), never the absolute request.url:
        # behind Caddy the board sees its OWN bind host (e.g. 0.0.0.0:51966) in
        # request.url, so an absolute redirect would bounce the browser OFF the
        # Caddy origin and straight onto the board — bypassing Caddy, landing
        # back in this handler, and looping to the __revive 503. A relative ref
        # keeps the browser on its current origin (the Caddy gateway).
        target = request.url.include_query_params(__revive="1")
        location = target.path + (f"?{target.query}" if target.query else "")
        return RedirectResponse(location, status_code=302)

    def _route_id(self, post_id: str) -> str:
        return f"agentboard-{post_id}"

    def _auth_handler(self) -> dict | None:
        if not self._basic_auth or ":" not in self._basic_auth:
            return None
        username, _, password_hash = self._basic_auth.partition(":")
        return {
            "handler": "authentication",
            "providers": {
                "http_basic": {
                    "accounts": [{"username": username, "password": password_hash}]
                }
            },
        }

    def ensure_route(self, post_id: str, port: int) -> None:
        rid = self._route_id(post_id)
        # Idempotent replace: drop any stale route for this post first (re-open
        # gets a fresh port), then insert at index 0.
        self._client.delete(f"{self._admin}/id/{rid}")
        handle: list[dict] = []
        auth = self._auth_handler()
        if auth:
            handle.append(auth)
        handle.append({"handler": "rewrite", "strip_path_prefix": f"/s/{post_id}"})
        handle.append(
            {"handler": "reverse_proxy", "upstreams": [{"dial": f"127.0.0.1:{port}"}]}
        )
        route = {
            "@id": rid,
            "match": [{"path": [f"/s/{post_id}", f"/s/{post_id}/*"]}],
            "handle": handle,
        }
        self._client.put(
            f"{self._admin}/config/apps/http/servers/{self._server}/routes/0",
            json=route,
        )

    def remove_route(self, post_id: str) -> None:
        self._client.delete(f"{self._admin}/id/{self._route_id(post_id)}")

    async def aclose(self) -> None:
        # sync admin client — close() is idempotent (httpx no-ops if already shut)
        self._client.close()

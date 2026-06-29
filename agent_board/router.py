"""Routing data plane (DESIGN §9).

``Router`` interface = ``ensure_route(post_id, port)`` / ``remove_route(post_id)``.

v1 uses ``BoardProxyRouter`` — the board itself reverse-proxies ``/s/<post_id>/*``
to ``127.0.0.1:<port>``, streaming both directions so SSE (``/api/stream``) passes
through chunk-by-chunk with NO buffering. (Production swaps in a ``CaddyRouter``
that registers routes in a separate gateway; the rest of the board is identical.)
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

# response headers the proxy must NOT copy verbatim — they describe the upstream
# framing, which StreamingResponse re-derives for the chunked relay.
_DROP_RESP_HEADERS = {
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "content-type",  # passed via media_type instead
}


class BoardProxyRouter:
    def __init__(self):
        self._routes: dict[str, int] = {}  # post_id → upstream port
        # one shared client (connection pooling); no timeout so SSE can hang
        # open. trust_env=False: loopback proxying must bypass any corporate
        # HTTP proxy (which "Access Denied"s 127.0.0.1).
        self._client = httpx.AsyncClient(timeout=None, trust_env=False)

    # ── Router interface ────────────────────────────────────
    def ensure_route(self, post_id: str, port: int) -> None:
        self._routes[post_id] = port

    def remove_route(self, post_id: str) -> None:
        self._routes.pop(post_id, None)

    async def aclose(self) -> None:
        await self._client.aclose()

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
        port = self._routes.get(post_id)
        if port is None:
            raise HTTPException(status_code=404, detail="post not running")

        url = httpx.URL(f"http://127.0.0.1:{port}/{path}")
        if request.url.query:
            url = url.copy_with(query=request.url.query.encode("utf-8"))
        fwd_headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

        # stream the request body UP (uploads) and the response DOWN (SSE).
        upstream_req = self._client.build_request(
            request.method, url, headers=fwd_headers, content=request.stream()
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
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=resp_headers,
            media_type=upstream.headers.get("content-type"),
            background=BackgroundTask(upstream.aclose),
        )


class CaddyRouter:
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

    def mount(self, app: FastAPI) -> None:
        # Caddy serves /s/<id>; the board does not. Nothing to mount.
        pass

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

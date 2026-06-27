"""CaddyRouter — registers /s/<id> routes in Caddy via its admin API (DESIGN §9).

The board stays OUT of the data path: Caddy proxies /s/<id>/* straight to the
instance. Each dynamic route EMBEDS the basic_auth handler before reverse_proxy,
so a proxied instance can never be reached unauthenticated regardless of where
the route sits in Caddy's route list (no auth-bypass).

Admin calls are captured with an httpx MockTransport — no real Caddy needed.
"""

from __future__ import annotations

import json

import httpx

from agent_board.router import CaddyRouter


def _router_with_recorder(**kw):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append({"method": request.method, "url": str(request.url), "json": body})
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return CaddyRouter(admin_url="http://127.0.0.1:2019", client=client, **kw), calls


class TestCaddyRouter:
    def test_mount_is_noop(self):
        # Caddy routes /s/<id>, not the board — mount must add nothing.
        from fastapi import FastAPI

        app = FastAPI()
        before = len(app.routes)
        router, _ = _router_with_recorder()
        router.mount(app)
        assert len(app.routes) == before

    def test_ensure_route_puts_reverse_proxy(self):
        router, calls = _router_with_recorder()
        router.ensure_route("p1", 50010)
        put = [c for c in calls if c["method"] == "PUT"]
        assert len(put) == 1
        # inserted at index 0 of srv0's routes
        assert put[0]["url"].endswith("/config/apps/http/servers/srv0/routes/0")
        route = put[0]["json"]
        assert route["@id"] == "agentboard-p1"
        assert route["match"][0]["path"] == ["/s/p1", "/s/p1/*"]
        handlers = [h["handler"] for h in route["handle"]]
        assert handlers == ["rewrite", "reverse_proxy"]  # no auth configured
        # strip prefix + correct upstream
        rw = next(h for h in route["handle"] if h["handler"] == "rewrite")
        assert rw["strip_path_prefix"] == "/s/p1"
        rp = next(h for h in route["handle"] if h["handler"] == "reverse_proxy")
        assert rp["upstreams"][0]["dial"] == "127.0.0.1:50010"

    def test_ensure_route_idempotent_deletes_first(self):
        router, calls = _router_with_recorder()
        router.ensure_route("p1", 50010)
        # a DELETE (drop stale) precedes the PUT so re-open replaces cleanly
        assert calls[0]["method"] == "DELETE"
        assert calls[0]["url"].endswith("/id/agentboard-p1")
        assert calls[1]["method"] == "PUT"

    def test_ensure_route_embeds_basic_auth(self):
        # ★ auth must be enforced ON the dynamic route itself (no bypass).
        router, calls = _router_with_recorder(basic_auth="alice:$2a$14$abc")
        router.ensure_route("p1", 50010)
        route = [c for c in calls if c["method"] == "PUT"][0]["json"]
        handlers = [h["handler"] for h in route["handle"]]
        assert handlers == ["authentication", "rewrite", "reverse_proxy"]
        auth = route["handle"][0]
        accts = auth["providers"]["http_basic"]["accounts"]
        assert accts[0] == {"username": "alice", "password": "$2a$14$abc"}

    def test_remove_route_deletes_by_id(self):
        router, calls = _router_with_recorder()
        router.remove_route("p1")
        assert calls[-1]["method"] == "DELETE"
        assert calls[-1]["url"].endswith("/id/agentboard-p1")

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
    def test_mount_registers_revive_routes(self):
        # Caddy proxies /s/<id> to a live instance, but the board mounts a
        # GET/HEAD revive handler for the fall-through case (route dropped on the
        # instance's death edge → Caddyfile catch-all lands here → reopen).
        from fastapi import FastAPI

        app = FastAPI()
        router, _ = _router_with_recorder()
        router.mount(app)
        paths = {getattr(r, "path", "") for r in app.routes}
        assert "/s/{post_id}/{path:path}" in paths
        assert "/s/{post_id}" in paths

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


def _revive_client(reopen=None):
    """A minimal FastAPI app with ONLY the CaddyRouter revive handlers mounted,
    plus an optional async ``reopen`` hook. redirects are NOT auto-followed so
    the 302 is inspectable."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    router, calls = _router_with_recorder()
    if reopen is not None:
        router.set_reopen(reopen)
    app = FastAPI()
    router.mount(app)
    return TestClient(app, follow_redirects=False), calls


class TestCaddyRevive:
    def test_get_fallthrough_reopens_then_redirects(self):
        # Caddy fell through (route dropped after self-reap) → the board revives
        # and 302s back so the retry hits the freshly-registered live route.
        seen = []

        async def reopen(post_id):
            seen.append(post_id)
            return f"/s/{post_id}/"

        client, _ = _revive_client(reopen)
        r = client.get("/s/p1/api/health?token=T", follow_redirects=False)
        assert r.status_code == 302
        assert seen == ["p1"]  # reopen called exactly once
        # redirect back to the same path, marked so a re-fall-through won't loop
        loc = r.headers["location"]
        assert "/s/p1/api/health" in loc and "__revive=1" in loc
        # MUST be RELATIVE (path+query, no scheme/host) — an absolute URL would
        # carry the board's bind host and bounce the browser off the Caddy
        # origin, bypassing Caddy and looping back to the __revive 503.
        assert loc.startswith("/s/p1/") and "://" not in loc
        assert "token=T" in loc  # existing query preserved

    def test_revive_marker_breaks_the_loop(self):
        # Already redirected once but STILL fell through (route not applied yet /
        # spawn failed) → 503 Retry-After, and reopen is NOT called again.
        seen = []

        async def reopen(post_id):
            seen.append(post_id)

        client, _ = _revive_client(reopen)
        r = client.get("/s/p1/api/health?__revive=1")
        assert r.status_code == 503
        assert r.headers.get("retry-after") == "2"
        assert seen == []  # no re-revive → no infinite loop

    def test_root_path_revives(self):
        seen = []

        async def reopen(post_id):
            seen.append(post_id)

        client, _ = _revive_client(reopen)
        r = client.get("/s/p1")
        assert r.status_code == 302
        assert seen == ["p1"]

    def test_non_get_does_not_revive(self):
        # a POST body can't be replayed on the redirect retry → 503, no reopen.
        seen = []

        async def reopen(post_id):
            seen.append(post_id)

        client, _ = _revive_client(reopen)
        r = client.post("/s/p1/api/query", json={"q": "hi"})
        assert r.status_code == 503
        assert seen == []

    def test_reopen_failure_is_502(self):
        async def reopen(post_id):
            raise RuntimeError("spawn failed")

        client, _ = _revive_client(reopen)
        r = client.get("/s/p1/api/health")
        assert r.status_code == 502

    def test_no_reopen_hook_is_503(self):
        # mount without set_reopen (defensive) → 503, never crashes.
        client, _ = _revive_client(reopen=None)
        r = client.get("/s/p1/api/health")
        assert r.status_code == 503

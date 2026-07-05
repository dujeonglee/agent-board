"""Cross-router BEHAVIOUR parity (DESIGN §9).

``BoardProxyRouter`` and ``CaddyRouter`` share the ``Router`` contract but NO
implementation, so behaviour can silently diverge — it did: revive-on-stale-hit
existed only on board-proxy (Caddy 502'd) and nothing caught it. The ``Router``
ABC pins the METHOD surface structurally (a missing method → can't instantiate);
these tests pin the shared BEHAVIOUR that an interface can't express.

★ When either router gains an observable behaviour the other must match, add a
parametrized case here so the gap fails loudly instead of shipping silently.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_board.router import BoardProxyRouter, CaddyRouter, Router

ROUTERS = ["board-proxy", "caddy"]


def _make(kind: str):
    """Build a router of ``kind`` with a recording reopen hook + a TestClient
    over an app with only its handlers mounted. Returns (router, client, calls)."""
    calls: list[str] = []

    async def reopen(post_id: str):
        calls.append(post_id)

    if kind == "board-proxy":
        router: Router = BoardProxyRouter()
    else:
        # MockTransport → no real Caddy; admin calls just succeed.
        client = httpx.Client(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={}))
        )
        router = CaddyRouter(client=client)
    router.set_reopen(reopen)
    app = FastAPI()
    router.mount(app)
    return router, TestClient(app, follow_redirects=False), calls


@pytest.mark.parametrize("kind", ROUTERS)
class TestRouterParity:
    def test_satisfies_router_contract(self, kind):
        router, _, _ = _make(kind)
        assert isinstance(router, Router)

    def test_remove_route_is_idempotent(self, kind):
        router, _, _ = _make(kind)
        # withdrawing a route that was never registered must not raise
        router.remove_route("never-registered")
        router.remove_route("never-registered")

    def test_down_instance_get_triggers_reopen(self, kind):
        # THE parity invariant that once diverged: a GET to /s/<id> for a
        # self-reaped instance (no live route) must trigger the revive hook —
        # board-proxy on the dead-port ConnectError, Caddy on the fall-through.
        _router, client, calls = _make(kind)
        client.get("/s/p1/api/health")
        assert calls == ["p1"]

    @pytest.mark.asyncio
    async def test_aclose_is_idempotent(self, kind):
        router, _, _ = _make(kind)
        await router.aclose()
        await router.aclose()  # second call must not raise

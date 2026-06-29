"""BoardProxyRouter — in-process reverse proxy for /s/<post_id>/* (DESIGN §9).

The board mounts a catch-all that strips the /s/<post_id> prefix and streams the
request to 127.0.0.1:<port>. The critical property is **SSE pass-through with no
buffering** — a long-lived text/event-stream must arrive at the browser chunk by
chunk, not all at once after the upstream closes.

A tiny FastAPI "upstream" (standing in for an agent-cli instance) is mounted on a
real loopback port; the board proxies to it and we assert the bytes + streaming.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import httpx
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from agent_board.router import BoardProxyRouter


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _make_upstream() -> FastAPI:
    up = FastAPI()

    @up.get("/api/health")
    async def health():
        return {"ok": True}

    @up.get("/api/echo")
    async def echo(q: str = ""):
        return {"path": "/api/echo", "q": q}

    @up.post("/api/upload")
    async def upload(request: Request):  # raw body echo (size)
        body = await request.body()
        return {"size": len(body)}

    @up.get("/api/stream")
    async def stream():
        async def gen():
            for i in range(3):
                yield f"data: tick{i}\n\n"
                await asyncio.sleep(0.05)

        return StreamingResponse(gen(), media_type="text/event-stream")

    return up


@pytest_asyncio.fixture
async def upstream():
    """Run the fake agent-cli upstream on a real loopback port."""
    port = _free_port()
    config = uvicorn.Config(
        _make_upstream(), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    # wait until it's up
    async with httpx.AsyncClient() as c:
        for _ in range(100):
            try:
                if (
                    await c.get(f"http://127.0.0.1:{port}/api/health")
                ).status_code == 200:
                    break
            except httpx.HTTPError:
                await asyncio.sleep(0.02)
    yield port
    server.should_exit = True
    # bounded teardown — never let a lingering stream hang the whole suite
    try:
        await asyncio.wait_for(task, timeout=5)
    except asyncio.TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _board_app(router) -> FastAPI:
    app = FastAPI()
    router.mount(app)
    return app


class TestBoardProxyRouter:
    @pytest.mark.asyncio
    async def test_proxies_get_and_strips_prefix(self, upstream):
        router = BoardProxyRouter()
        router.ensure_route("p1", upstream)
        app = _board_app(router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/s/p1/api/echo?q=hi")
            assert r.status_code == 200
            # prefix /s/p1 stripped → upstream saw /api/echo with the query
            assert r.json() == {"path": "/api/echo", "q": "hi"}

    @pytest.mark.asyncio
    async def test_proxies_post_body(self, upstream):
        router = BoardProxyRouter()
        router.ensure_route("p1", upstream)
        app = _board_app(router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/s/p1/api/upload", content=b"x" * 1234)
            assert r.json()["size"] == 1234

    @pytest.mark.asyncio
    async def test_sse_streams_without_buffering(self, upstream):
        router = BoardProxyRouter()
        router.ensure_route("p1", upstream)
        app = _board_app(router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            async with c.stream("GET", "/s/p1/api/stream") as r:
                assert r.status_code == 200
                assert "text/event-stream" in r.headers["content-type"]
                ticks = []
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        ticks.append(line)
                assert ticks == ["data: tick0", "data: tick1", "data: tick2"]

    @pytest.mark.asyncio
    async def test_no_route_no_reopen_is_503(self, upstream):
        # no route + no reopen hook → clean 503 (not a raw exception)
        router = BoardProxyRouter()
        app = _board_app(router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            assert (await c.get("/s/nope/api/echo")).status_code == 503

    @pytest.mark.asyncio
    async def test_remove_route(self, upstream):
        router = BoardProxyRouter()
        router.ensure_route("p1", upstream)
        router.remove_route("p1")
        app = _board_app(router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            assert (await c.get("/s/p1/api/echo")).status_code == 503

    @pytest.mark.asyncio
    async def test_revives_when_route_missing(self, upstream):
        # hitting /s/<id> with no route → reopen revives it → proxies
        router = BoardProxyRouter()
        reopened = []

        async def reopen(post_id):
            reopened.append(post_id)
            router.ensure_route(post_id, upstream)  # spawn-or-attach result

        router.set_reopen(reopen)
        app = _board_app(router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/s/p1/api/echo?q=hi")
            assert r.status_code == 200 and r.json()["q"] == "hi"
            assert reopened == ["p1"]

    @pytest.mark.asyncio
    async def test_revives_on_dead_route(self, upstream):
        # stale route → dead port (idle-reaped). GET → ConnectError → revive → retry
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = s.getsockname()[1]  # nothing listens here
        router = BoardProxyRouter()
        router.ensure_route("p1", dead)

        async def reopen(post_id):
            router.ensure_route(post_id, upstream)  # revive to the live port

        router.set_reopen(reopen)
        app = _board_app(router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/s/p1/api/health")
            assert r.status_code == 200  # retried on the revived instance

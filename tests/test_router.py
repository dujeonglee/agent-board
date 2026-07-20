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

    @up.get("/api/broken-stream")
    async def broken_stream():
        # Emit one chunk, then die mid-stream — stands in for an instance killed
        # by a force-restart while its SSE stream is open. uvicorn closes the
        # connection without a clean chunked EOF, so the proxy's httpx read
        # raises RemoteProtocolError (the bug this reproduces).
        async def gen():
            yield b"data: partial\n\n"
            raise RuntimeError("upstream killed mid-stream")

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
    async def test_upstream_disconnect_midstream_ends_cleanly(self, upstream):
        # Force-restart kills the instance mid-SSE → the proxy must end the
        # stream cleanly, NOT let RemoteProtocolError bubble as an ASGI 500.
        router = BoardProxyRouter()
        router.ensure_route("p1", upstream)
        app = _board_app(router)
        transport = httpx.ASGITransport(app=app)  # raises app exceptions by default
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            async with c.stream("GET", "/s/p1/api/broken-stream") as r:
                assert r.status_code == 200
                chunks = []
                # must complete without raising (buggy version raised here)
                async for chunk in r.aiter_raw():
                    chunks.append(chunk)
        assert b"partial" in b"".join(chunks)  # partial data relayed before EOF

    @pytest.mark.asyncio
    async def test_pooled_reset_on_send_returns_502_not_crash(self, monkeypatch):
        """kill/재시작으로 죽은 인스턴스의 stale keep-alive 풀을 초기 send()
        에서 재사용하면 ConnectError 가 아닌 ReadError(=TransportError)가 난다
        — 좁은 (ConnectError, ConnectTimeout) catch 를 빠져나가 run_asgi 500
        크래시가 됐던 회귀(실측: 부하 시 재열기 크래시, kill 직후 반열림 풀
        재사용 = ReadError). 이제 우아한 502(GET → revive 시도 후)여야 한다."""
        router = BoardProxyRouter()
        router.ensure_route("p1", 59999)  # 포트 무의미 — send 를 boom 으로 가로챔

        async def boom(*a, **k):
            raise httpx.ReadError("Server disconnected without sending a response.")

        monkeypatch.setattr(router._client, "send", boom)
        app = _board_app(router)
        transport = httpx.ASGITransport(app=app)  # 앱 예외를 raise = 크래시 감지
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/s/p1/api/echo")
        # 좁은 catch 였으면 ReadError 가 새어 위에서 raise(테스트 실패=red).
        # 넓힌 뒤엔 revive(미배선 → None) → 502.
        assert r.status_code == 502
        await router.aclose()

    def test_proxy_client_bounds_connect_but_not_stream(self):
        """프록시 client 는 connect 만 상한(응답 없는/반쯤 죽은 upstream 에
        무한 대기하다 요청이 영영 안 끝나는 것 방지), read 는 무제한 —
        SSE 스트림(/api/stream)이 무한히 열려 있어야 하므로."""
        router = BoardProxyRouter()
        t = router._client.timeout
        assert t.connect is not None  # 연결은 상한
        assert t.read is None  # 스트림 읽기는 무제한

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

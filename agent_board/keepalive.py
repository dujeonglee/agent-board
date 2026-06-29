"""Force-active keep-alive (DESIGN §8).

When a post is force-active, the control plane holds ONE SSE connection to its
instance (`/api/stream`) so agent-cli counts ≥1 viewer and never idle-reaps.
One asyncio task per active post keeps that connection open (reconnecting if it
drops); disabling cancels it.

The connection coroutine is injected so the on/off bookkeeping is unit-tested
without a live agent-cli; the production ``connect`` opens an SSE stream over
loopback (trust-local → no token).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from agent_board import instances

ConnectFn = Callable[[str], Awaitable[None]]


class KeepAliveManager:
    def __init__(self, *, connect: ConnectFn, retry_delay: float = 1.0):
        self._connect = connect
        self._retry_delay = retry_delay
        self._tasks: dict[str, asyncio.Task] = {}

    def is_active(self, post_id: str) -> bool:
        t = self._tasks.get(post_id)
        return t is not None and not t.done()

    async def enable(self, post_id: str) -> None:
        if self.is_active(post_id):
            return  # idempotent — one connection per post
        self._tasks[post_id] = asyncio.create_task(self._hold(post_id))

    async def disable(self, post_id: str) -> None:
        task = self._tasks.pop(post_id, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def shutdown(self) -> None:
        for post_id in list(self._tasks):
            await self.disable(post_id)

    async def _hold(self, post_id: str) -> None:
        """Keep a connection open; if it returns (instance dropped/crashed),
        wait briefly and reconnect, until cancelled."""
        while True:
            try:
                await self._connect(post_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self._retry_delay)


def make_sse_connect(
    port_for: Callable[[str], int | None],
) -> ConnectFn:
    """Production ``connect``: open and hold an SSE stream to the instance for
    ``post_id``. ``port_for(post_id)`` resolves the live upstream port (None if
    the instance isn't up — then this returns and the manager retries)."""

    async def connect(post_id: str) -> None:
        port = port_for(post_id)
        if port is None:
            return
        url = f"http://127.0.0.1:{port}/api/stream"
        # trust_env=False: loopback SSE must bypass any corporate HTTP proxy.
        async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
            async with client.stream("GET", url) as resp:
                async for _ in resp.aiter_raw():
                    pass  # drain + hold the connection open (we are a viewer)

    return connect


def default_port_for(config, store):
    """Resolve a post's live instance port (or None) — for ``make_sse_connect``."""

    def port_for(post_id: str) -> int | None:
        post = store.get(post_id)
        if post is None or not post.session_id:
            return None
        info = instances.read_web_json(config.workspace_for(post_id), post.session_id)
        if info and instances.alive(info):
            return info.get("port")
        return None

    return port_for

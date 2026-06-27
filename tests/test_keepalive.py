"""Force-active keep-alive (DESIGN §8).

force-active keeps an instance from idle-reaping by holding ONE SSE connection
(`/api/stream`) to it — agent-cli then counts ≥1 viewer and never self-exits.
``KeepAliveManager`` runs one asyncio task per active post: it connects to the
instance and stays connected; toggle off / stop cancels it.

The actual SSE connection is injected (a ``connect`` coroutine) so the manager's
on/off bookkeeping is tested without a live agent-cli.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_board.keepalive import KeepAliveManager


class FakeConn:
    """Records open/close; blocks until cancelled (like a held SSE stream)."""

    def __init__(self):
        self.opened = 0
        self.closed = 0

    async def __call__(self, post_id):
        self.opened += 1
        try:
            await asyncio.Event().wait()  # hold open forever
        finally:
            self.closed += 1


@pytest.mark.asyncio
async def test_enable_holds_a_connection():
    conn = FakeConn()
    mgr = KeepAliveManager(connect=conn)
    await mgr.enable("p1")
    await asyncio.sleep(0.02)
    assert conn.opened == 1
    assert mgr.is_active("p1")
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_disable_closes_connection():
    conn = FakeConn()
    mgr = KeepAliveManager(connect=conn)
    await mgr.enable("p1")
    await asyncio.sleep(0.02)
    await mgr.disable("p1")
    await asyncio.sleep(0.02)
    assert conn.closed == 1
    assert not mgr.is_active("p1")


@pytest.mark.asyncio
async def test_enable_is_idempotent():
    conn = FakeConn()
    mgr = KeepAliveManager(connect=conn)
    await mgr.enable("p1")
    await mgr.enable("p1")  # second enable should not open a 2nd connection
    await asyncio.sleep(0.02)
    assert conn.opened == 1
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_disable_unknown_is_noop():
    mgr = KeepAliveManager(connect=FakeConn())
    await mgr.disable("nope")  # no error
    assert not mgr.is_active("nope")


@pytest.mark.asyncio
async def test_reconnects_when_connection_drops():
    # a connection that returns quickly (instance crashed) should be retried
    attempts = {"n": 0}

    async def flaky(post_id):
        attempts["n"] += 1
        await asyncio.sleep(0.01)  # returns → manager should reconnect

    mgr = KeepAliveManager(connect=flaky, retry_delay=0.01)
    await mgr.enable("p1")
    await asyncio.sleep(0.08)
    await mgr.shutdown()
    assert attempts["n"] >= 2  # reconnected at least once


@pytest.mark.asyncio
async def test_shutdown_cancels_all():
    conn = FakeConn()
    mgr = KeepAliveManager(connect=conn)
    await mgr.enable("p1")
    await mgr.enable("p2")
    await asyncio.sleep(0.02)
    await mgr.shutdown()
    assert not mgr.is_active("p1") and not mgr.is_active("p2")
    assert conn.closed == 2

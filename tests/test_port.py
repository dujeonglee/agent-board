"""pick_board_port — prefer the default port, else a free one (collision avoid)."""

from __future__ import annotations

import socket

from agent_board.app import DEFAULT_PORT, pick_board_port


def test_default_port_is_cafe():
    assert DEFAULT_PORT == 0xCAFE == 51966


def test_free_preferred_is_used():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert pick_board_port("127.0.0.1", free) == free


def test_busy_preferred_falls_back():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    busy = srv.getsockname()[1]
    try:
        got = pick_board_port("127.0.0.1", busy)
        assert got != busy and got > 0  # OS-assigned fallback
    finally:
        srv.close()

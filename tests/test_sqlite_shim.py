"""agent_board._sqlite shim — stdlib sqlite3, or pysqlite3 fallback.

Guards the board against ``No module named '_sqlite3'`` on Python builds without
the stdlib sqlite3 extension (locked-down servers).
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

_PYSQLITE3 = importlib.util.find_spec("pysqlite3") is not None


def test_shim_exports_working_sqlite():
    from agent_board._sqlite import sqlite3

    assert hasattr(sqlite3, "Connection")
    assert hasattr(sqlite3, "Row")
    assert hasattr(sqlite3, "IntegrityError")
    conn = sqlite3.connect(":memory:")
    try:
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()


def test_store_uses_shim_not_plain_import():
    # a plain ``import sqlite3`` in store.py would crash on sqlite-less Python
    src = importlib.util.find_spec("agent_board.store").origin
    text = open(src, encoding="utf-8").read()
    assert "from agent_board._sqlite import sqlite3" in text
    assert "\nimport sqlite3" not in text


@pytest.mark.skipif(not _PYSQLITE3, reason="pysqlite3 wheel not on this platform")
def test_falls_back_to_pysqlite3_when_stdlib_missing(monkeypatch):
    sys.modules.pop("agent_board._sqlite", None)
    sys.modules.pop("sqlite3", None)

    class _Block:
        def find_spec(self, name, path=None, target=None):
            if name == "sqlite3":
                raise ImportError("simulated: stdlib sqlite3 unavailable")
            return None

    monkeypatch.setattr(sys, "meta_path", [_Block(), *sys.meta_path])
    try:
        shim = importlib.import_module("agent_board._sqlite")
    finally:
        sys.modules.pop("agent_board._sqlite", None)
    conn = shim.sqlite3.connect(":memory:")
    try:
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()

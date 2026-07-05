"""Live push scanner + SSE endpoint (DESIGN §7 Phase 2).

The board no longer polls ``/api/posts`` every 5s; a background scanner samples
each post's cheap on-disk *signature* (status.json mtime + history.jsonl mtime +
pid-liveness) and pushes only the changed rows over SSE. These tests exercise
the diffing (``_scan``) and the signature (``_sig``) directly with deterministic
mtimes (``os.utime``), plus a smoke test of the ``/api/events`` stream.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from agent_board import instances
from agent_board.config import Config
from agent_board.live_events import LiveEvents
from agent_board.store import Store


def _session_dir(cfg: Config, post_id: str, sid: str) -> Path:
    d = cfg.workspace_for(post_id) / ".agent-cli" / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(d: Path, name: str, body: dict, *, mtime: float) -> None:
    p = d / name
    p.write_text(json.dumps(body), encoding="utf-8")
    os.utime(p, (mtime, mtime))


def _live(tmp_path, view_fn=None):
    cfg = Config(data_dir=tmp_path / "data", workspaces_root=tmp_path / "ws")
    store = Store(cfg.db_path)
    live = LiveEvents(cfg, store, view_fn or (lambda p: {"post_id": p.post_id}))
    return cfg, store, live


def _seed_session(cfg, store, *, sid="S1", pid=4242, mtime=1000.0):
    """A post with a live session: web.json (pid), status.json, history.jsonl."""
    post = store.create_post(topic="t")
    store.set_session_id(post.post_id, sid)
    d = _session_dir(cfg, post.post_id, sid)
    _write(d, "web.json", {"session_id": sid, "pid": pid, "port": 9000}, mtime=mtime)
    _write(
        d,
        "status.json",
        {"busy": False, "awaiting_input": False, "viewers": 0},
        mtime=mtime,
    )
    _write(d, "history.jsonl", {}, mtime=mtime)
    return store.get(post.post_id)


class TestSignature:
    def test_no_session_id_is_stable_sentinel(self, tmp_path):
        _cfg, store, live = _live(tmp_path)
        post = store.create_post(topic="t")  # never opened → no session_id
        assert live._sig(post) == (None, None, False)

    def test_reflects_pid_liveness(self, tmp_path, monkeypatch):
        cfg, store, live = _live(tmp_path)
        post = _seed_session(cfg, store)
        monkeypatch.setattr(instances, "pid_alive", lambda pid: True)
        assert live._sig(post)[2] is True
        monkeypatch.setattr(instances, "pid_alive", lambda pid: False)
        assert live._sig(post)[2] is False

    def test_missing_files_are_none_not_error(self, tmp_path, monkeypatch):
        cfg, store, live = _live(tmp_path)
        post = store.create_post(topic="t")
        store.set_session_id(post.post_id, "S1")  # session_id but no files on disk
        monkeypatch.setattr(instances, "pid_alive", lambda pid: True)
        assert live._sig(store.get(post.post_id)) == (None, None, False)


class TestScan:
    def test_no_change_after_prime_is_empty(self, tmp_path, monkeypatch):
        cfg, store, live = _live(tmp_path)
        _seed_session(cfg, store)
        monkeypatch.setattr(instances, "pid_alive", lambda pid: True)
        live._prime()
        assert live._scan() == []

    def test_status_mtime_bump_emits_update(self, tmp_path, monkeypatch):
        cfg, store, live = _live(tmp_path)
        post = _seed_session(cfg, store)
        monkeypatch.setattr(instances, "pid_alive", lambda pid: True)
        live._prime()
        d = cfg.workspace_for(post.post_id) / ".agent-cli" / "sessions" / "S1"
        _write(
            d,
            "status.json",
            {"busy": True, "awaiting_input": False, "viewers": 2},
            mtime=2000.0,
        )
        events = live._scan()
        assert len(events) == 1
        assert events[0]["type"] == "post_update"
        assert events[0]["post"]["post_id"] == post.post_id
        assert live._scan() == []  # no re-emit once absorbed

    def test_history_mtime_bump_emits_update(self, tmp_path, monkeypatch):
        cfg, store, live = _live(tmp_path)
        post = _seed_session(cfg, store)
        monkeypatch.setattr(instances, "pid_alive", lambda pid: True)
        live._prime()
        d = cfg.workspace_for(post.post_id) / ".agent-cli" / "sessions" / "S1"
        _write(d, "history.jsonl", {}, mtime=3000.0)
        assert [e["type"] for e in live._scan()] == ["post_update"]

    def test_pid_death_emits_update_without_file_change(self, tmp_path, monkeypatch):
        cfg, store, live = _live(tmp_path)
        _seed_session(cfg, store)
        alive = {"v": True}
        monkeypatch.setattr(instances, "pid_alive", lambda pid: alive["v"])
        live._prime()
        alive["v"] = False  # SIGKILL: files untouched, only liveness flips
        assert [e["type"] for e in live._scan()] == ["post_update"]

    def test_new_post_emits_update(self, tmp_path, monkeypatch):
        cfg, store, live = _live(tmp_path)
        monkeypatch.setattr(instances, "pid_alive", lambda pid: True)
        live._prime()  # empty baseline
        post = _seed_session(cfg, store)
        events = live._scan()
        assert len(events) == 1
        assert events[0]["post"]["post_id"] == post.post_id

    def test_deleted_post_emits_removed(self, tmp_path, monkeypatch):
        cfg, store, live = _live(tmp_path)
        post = _seed_session(cfg, store)
        monkeypatch.setattr(instances, "pid_alive", lambda pid: True)
        live._prime()
        store.delete(post.post_id)
        events = live._scan()
        assert events == [{"type": "post_removed", "post_id": post.post_id}]
        assert live._scan() == []  # gone from baseline → no repeat

    def test_scan_uses_view_fn(self, tmp_path, monkeypatch):
        cfg, store, live = _live(
            tmp_path, view_fn=lambda p: {"post_id": p.post_id, "topic": p.topic}
        )
        post = _seed_session(cfg, store)
        monkeypatch.setattr(instances, "pid_alive", lambda pid: True)
        live._prime()
        d = cfg.workspace_for(post.post_id) / ".agent-cli" / "sessions" / "S1"
        _write(d, "status.json", {"busy": True}, mtime=5000.0)
        assert live._scan()[0]["post"]["topic"] == "t"


class TestSubscription:
    def test_broadcast_reaches_subscribers(self, tmp_path):
        _cfg, _store, live = _live(tmp_path)
        q1 = live.subscribe()
        q2 = live.subscribe()
        assert live.subscriber_count == 2
        live._broadcast({"type": "ping"})
        assert q1.get_nowait() == {"type": "ping"}
        assert q2.get_nowait() == {"type": "ping"}

    def test_unsubscribe_stops_delivery(self, tmp_path):
        _cfg, _store, live = _live(tmp_path)
        q = live.subscribe()
        live.unsubscribe(q)
        assert live.subscriber_count == 0
        live._broadcast({"type": "ping"})
        assert q.empty()


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_run_broadcasts_delta_to_subscriber(self, tmp_path, monkeypatch):
        cfg, store, live = _live(tmp_path)
        post = _seed_session(cfg, store)
        monkeypatch.setattr(instances, "pid_alive", lambda pid: True)
        live._interval = 0.01
        q = live.subscribe()
        task = asyncio.create_task(live.run())
        try:
            await asyncio.sleep(0.05)  # let it prime (no emit for existing state)
            assert q.empty()
            d = cfg.workspace_for(post.post_id) / ".agent-cli" / "sessions" / "S1"
            _write(d, "status.json", {"busy": True}, mtime=9000.0)
            msg = await asyncio.wait_for(q.get(), timeout=1.0)
            assert msg["type"] == "post_update"
            assert msg["post"]["post_id"] == post.post_id
        finally:
            task.cancel()

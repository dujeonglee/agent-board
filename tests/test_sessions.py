"""Derived views over an agent-cli session's on-disk files (DESIGN §7).

INTEGRATION CONTRACT with agent-cli's on-disk format:
- ``<ws>/.agent-cli/sessions/<sid>/history.jsonl`` — round-trip records; a user
  query is ``{role:"user", kind:"query", text:..., content:...}``. We read the
  LAST such record's ``text`` (clean; falls back to ``content``).
- ``<ws>/.agent-cli/sessions/<sid>/web.json`` — presence + pid + health = running.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_board import sessions


def _history(ws: Path, sid: str, records: list[dict]):
    d = ws / ".agent-cli" / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    with (d / "history.jsonl").open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _q(text):
    return {"role": "user", "kind": "query", "text": text, "content": f"[me]: {text}"}


class TestLastQuery:
    def test_returns_last_query_text(self, tmp_path):
        ws = tmp_path / "ws"
        _history(
            ws,
            "S1",
            [
                _q("첫 질문"),
                {"role": "assistant", "kind": "action", "text": "..."},
                _q("마지막 질문"),
                {"role": "user", "kind": "observation", "text": "Observation: ok"},
            ],
        )
        assert sessions.last_query(ws, "S1") == "마지막 질문"

    def test_prefers_text_over_content(self, tmp_path):
        ws = tmp_path / "ws"
        _history(
            ws,
            "S1",
            [
                {
                    "role": "user",
                    "kind": "query",
                    "text": "clean",
                    "content": "[me]: clean",
                }
            ],
        )
        assert sessions.last_query(ws, "S1") == "clean"

    def test_none_when_no_session(self, tmp_path):
        assert sessions.last_query(tmp_path / "ws", None) is None
        assert sessions.last_query(tmp_path / "ws", "missing") is None

    def test_none_when_no_query_records(self, tmp_path):
        ws = tmp_path / "ws"
        _history(ws, "S1", [{"role": "assistant", "kind": "final", "text": "done"}])
        assert sessions.last_query(ws, "S1") is None

    def test_record_returns_text_and_ts(self, tmp_path):
        ws = tmp_path / "ws"
        _history(
            ws,
            "S1",
            [
                {
                    "role": "user",
                    "kind": "query",
                    "text": "처음",
                    "ts": "2026-06-01T00:00:00+00:00",
                },
                {
                    "role": "user",
                    "kind": "query",
                    "text": "마지막",
                    "ts": "2026-06-02T09:30:00+00:00",
                },
            ],
        )
        rec = sessions.last_query_record(ws, "S1")
        assert rec == {"text": "마지막", "ts": "2026-06-02T09:30:00+00:00"}

    def test_record_none_when_no_session(self, tmp_path):
        assert sessions.last_query_record(tmp_path / "ws", None) is None


class TestStatus:
    """3-state: working (LLM 응답 중) / running (대기) / idle (꺼짐)."""

    def _web_json(self, tmp_path, **fields):
        ws = tmp_path / "ws"
        d = ws / ".agent-cli" / "sessions" / "S1"
        d.mkdir(parents=True)
        info = {"pid": 1, "port": 50001}
        info.update(fields)
        (d / "web.json").write_text(json.dumps(info))
        return ws

    def test_idle_when_never_opened(self, tmp_path):
        assert sessions.status(tmp_path / "ws", None) == "idle"

    def test_idle_when_no_web_json(self, tmp_path):
        ws = tmp_path / "ws"
        _history(ws, "S1", [_q("x")])  # session exists but not running
        assert sessions.status(ws, "S1") == "idle"

    def test_idle_when_pid_dead(self, tmp_path, monkeypatch):
        ws = self._web_json(tmp_path)
        monkeypatch.setattr(sessions.instances, "pid_alive", lambda pid: False)
        assert sessions.status(ws, "S1") == "idle"

    def test_idle_when_health_unreachable(self, tmp_path, monkeypatch):
        ws = self._web_json(tmp_path)
        monkeypatch.setattr(sessions.instances, "pid_alive", lambda pid: True)
        monkeypatch.setattr(sessions.instances, "health_info", lambda port: None)
        assert sessions.status(ws, "S1") == "idle"

    def test_running_when_alive_and_idle_worker(self, tmp_path, monkeypatch):
        ws = self._web_json(tmp_path)
        monkeypatch.setattr(sessions.instances, "pid_alive", lambda pid: True)
        monkeypatch.setattr(
            sessions.instances, "health_info", lambda port: {"busy": False}
        )
        assert sessions.status(ws, "S1") == "running"

    def test_working_when_busy(self, tmp_path, monkeypatch):
        ws = self._web_json(tmp_path)
        monkeypatch.setattr(sessions.instances, "pid_alive", lambda pid: True)
        monkeypatch.setattr(
            sessions.instances, "health_info", lambda port: {"busy": True}
        )
        assert sessions.status(ws, "S1") == "working"

    def test_live_state_awaiting_input(self, tmp_path, monkeypatch):
        ws = self._web_json(tmp_path)
        monkeypatch.setattr(sessions.instances, "pid_alive", lambda pid: True)
        monkeypatch.setattr(
            sessions.instances,
            "health_info",
            lambda port: {"busy": True, "awaiting_input": True},
        )
        state = sessions.live_state(ws, "S1")
        assert state["awaiting_input"] is True and state["status"] == "working"

    def test_live_state_idle_not_awaiting(self, tmp_path):
        assert sessions.live_state(tmp_path / "ws", None) == {
            "status": "idle",
            "awaiting_input": False,
        }

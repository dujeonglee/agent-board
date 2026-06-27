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


class TestStatus:
    def test_idle_when_never_opened(self, tmp_path):
        assert sessions.status(tmp_path / "ws", None) == "idle"

    def test_idle_when_no_web_json(self, tmp_path):
        ws = tmp_path / "ws"
        _history(ws, "S1", [_q("x")])  # session exists but not running
        assert sessions.status(ws, "S1") == "idle"

    def test_running_when_alive(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        d = ws / ".agent-cli" / "sessions" / "S1"
        d.mkdir(parents=True)
        (d / "web.json").write_text(json.dumps({"pid": 1, "port": 50001}))
        monkeypatch.setattr(sessions.instances, "alive", lambda info: True)
        assert sessions.status(ws, "S1") == "running"

    def test_idle_when_web_json_but_dead(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        d = ws / ".agent-cli" / "sessions" / "S1"
        d.mkdir(parents=True)
        (d / "web.json").write_text(json.dumps({"pid": 1, "port": 50001}))
        monkeypatch.setattr(sessions.instances, "alive", lambda info: False)
        assert sessions.status(ws, "S1") == "idle"

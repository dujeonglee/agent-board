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
            "viewers": 0,
        }

    def test_live_state_reports_viewers(self, tmp_path, monkeypatch):
        ws = self._web_json(tmp_path)
        monkeypatch.setattr(sessions.instances, "pid_alive", lambda pid: True)
        monkeypatch.setattr(
            sessions.instances,
            "health_info",
            lambda port: {"busy": False, "awaiting_input": False, "viewers": 3},
        )
        assert sessions.live_state(ws, "S1")["viewers"] == 3

    def _status_json(self, ws, **fields):
        d = ws / ".agent-cli" / "sessions" / "S1"
        d.mkdir(parents=True, exist_ok=True)
        info = {"busy": False, "awaiting_input": False, "viewers": 0}
        info.update(fields)
        (d / "status.json").write_text(json.dumps(info))

    def test_live_state_passes_agents_through_from_status_file(
        self, tmp_path, monkeypatch
    ):
        """v1.17.0 agents 추출 자체 검증 (v1.17.1 보강 — 종전엔 live_state
        를 몽키패치해 이 코드가 무검증이었다). status.json 의 agents dict
        는 그대로 통과, 비-dict 는 무시, 부재 시 키 없음."""
        import json

        ws = self._web_json(tmp_path)
        monkeypatch.setattr(sessions.instances, "pid_alive", lambda pid: True)
        sdir = ws / ".agent-cli" / "sessions" / "S1"
        agents = {
            "alive": 2,
            "working": 1,
            "list": [{"key": "a", "profile": "coder", "name": "", "state": "busy"}],
        }
        (sdir / "status.json").write_text(
            json.dumps(
                {"busy": False, "awaiting_input": False, "viewers": 0, "agents": agents}
            )
        )
        state = sessions.live_state(ws, "S1")
        assert state["agents"] == agents

        # 비-dict agents(손상/구버전 이상치) → 키 자체를 안 만든다
        (sdir / "status.json").write_text(
            json.dumps(
                {
                    "busy": False,
                    "awaiting_input": False,
                    "viewers": 0,
                    "agents": "garbage",
                }
            )
        )
        assert "agents" not in sessions.live_state(ws, "S1")

        # 필드 부재(구버전 agent-cli) → 키 없음
        (sdir / "status.json").write_text(
            json.dumps({"busy": False, "awaiting_input": False, "viewers": 0})
        )
        assert "agents" not in sessions.live_state(ws, "S1")

    def test_live_state_prefers_status_file_over_health(self, tmp_path, monkeypatch):
        # status.json present (agent-cli >= 4.27.0) → read the file, NEVER HTTP
        ws = self._web_json(tmp_path)
        self._status_json(ws, busy=True, awaiting_input=True, viewers=2)
        monkeypatch.setattr(sessions.instances, "pid_alive", lambda pid: True)

        def _boom(port):
            raise AssertionError("health_info must not be called when file exists")

        monkeypatch.setattr(sessions.instances, "health_info", _boom)
        assert sessions.live_state(ws, "S1") == {
            "status": "working",
            "awaiting_input": True,
            "viewers": 2,
        }

    def test_live_state_falls_back_to_health_without_status_file(
        self, tmp_path, monkeypatch
    ):
        # older instance (no status.json) → fall back to the HTTP health poll
        ws = self._web_json(tmp_path)
        monkeypatch.setattr(sessions.instances, "pid_alive", lambda pid: True)
        monkeypatch.setattr(
            sessions.instances, "health_info", lambda port: {"busy": True, "viewers": 1}
        )
        state = sessions.live_state(ws, "S1")
        assert state["status"] == "working" and state["viewers"] == 1

    def test_live_state_corrupt_status_file_falls_back(self, tmp_path, monkeypatch):
        ws = self._web_json(tmp_path)
        (ws / ".agent-cli" / "sessions" / "S1" / "status.json").write_text("{bad")
        monkeypatch.setattr(sessions.instances, "pid_alive", lambda pid: True)
        monkeypatch.setattr(
            sessions.instances, "health_info", lambda port: {"busy": False}
        )
        assert sessions.live_state(ws, "S1")["status"] == "running"


class TestStatusFileCrossRepoContract:
    """★크로스-레포 실계약 (2026-07-18 감사): board 테스트가 status.json
    을 손으로 쓰면 agent-cli 쪽 writer/필드가 드리프트해도 양쪽 유닛이
    다 통과한다("working" 어휘 버그와 같은 부류). dev/배포 환경은
    agent-cli co-install 전제(admin 테스트 동형)이므로 **agent-cli 의
    실제 writer 로 생산한 파일**을 board 가 읽는 계약을 고정한다."""

    def _ws(self, tmp_path):
        import json as _json

        ws = tmp_path / "ws"
        sdir = ws / ".agent-cli" / "sessions" / "S1"
        sdir.mkdir(parents=True)
        (sdir / "web.json").write_text(
            _json.dumps(
                {"session_id": "S1", "host": "h", "port": 1, "token": "t", "pid": 1}
            )
        )
        return ws, sdir

    def test_agent_cli_written_status_parses(self, tmp_path, monkeypatch):
        from agent_cli.web.instance_file import write_status_file

        ws, sdir = self._ws(tmp_path)
        monkeypatch.setattr(sessions.instances, "pid_alive", lambda pid: True)
        agents = {
            "alive": 2,
            "working": 1,
            "list": [
                {"key": "agt-1", "profile": "coder", "name": "ui", "state": "busy"},
                {"key": "agt-2", "profile": "rev", "name": "", "state": "idle"},
            ],
        }
        write_status_file(
            sdir, busy=False, awaiting_input=False, viewers=2, agents=agents
        )
        state = sessions.live_state(ws, "S1")
        assert state["status"] == "running"
        assert state["viewers"] == 2
        assert state["agents"] == agents
        # 프런트 계약 필드 — app.js agentsChip/AGENTS_BUSY 가 읽는 키들
        assert {"alive", "working", "list"} <= set(state["agents"])
        assert {"key", "profile", "name", "state"} <= set(state["agents"]["list"][0])

    def test_agent_cli_summary_builder_end_to_end(self, tmp_path, monkeypatch):
        """한 단계 더 실물로: agent-cli 렌더러의 요약 빌더 출력(실어휘
        busy 포함)이 board 파서·프런트 계약을 그대로 만족."""
        from agent_cli.render.web import WebRenderer
        from agent_cli.web.instance_file import write_status_file

        ws, sdir = self._ws(tmp_path)
        monkeypatch.setattr(sessions.instances, "pid_alive", lambda pid: True)
        summary = WebRenderer._agents_summary_from(
            [
                {"key": "a", "profile": "coder", "name": "", "state": "busy"},
                {"key": "b", "profile": "rev", "name": "", "state": "waiting_ask"},
                {"key": "c", "profile": "x", "name": "", "state": "dead"},
            ]
        )
        write_status_file(
            sdir, busy=False, awaiting_input=False, viewers=0, agents=summary
        )
        agents = sessions.live_state(ws, "S1")["agents"]
        assert agents["alive"] == 2
        assert agents["working"] == 2  # busy + waiting_ask 둘 다 작업 중

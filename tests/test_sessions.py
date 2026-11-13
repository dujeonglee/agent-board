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

import pytest

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

    def test_live_state_passes_knobs_through_from_status_file(
        self, tmp_path, monkeypatch
    ):
        """v1.36.0: agent-cli ≥10.46.0 의 ``knobs`` (노브 현재 값) — 보드 거울의
        소스. 구버전(키 없음)은 키 생략, dict 가 아니면 무시."""
        ws = self._web_json(tmp_path)
        monkeypatch.setattr(sessions.instances, "pid_alive", lambda pid: True)
        self._status_json(ws, knobs={"model": "m", "thinking": "on", "stall": 600})
        assert sessions.live_state(ws, "S1")["knobs"] == {
            "model": "m",
            "thinking": "on",
            "stall": 600,
        }
        self._status_json(ws)
        assert "knobs" not in sessions.live_state(ws, "S1")
        self._status_json(ws, knobs="bogus")
        assert "knobs" not in sessions.live_state(ws, "S1")

    def test_live_state_passes_open_items_through_from_status_file(
        self, tmp_path, monkeypatch
    ):
        """v1.44.0: agent-cli ≥10.119.0 의 ``open_items`` (열린 항목 장부 요약) —
        카드 📋 칩의 소스. 모양이 맞으면 그대로(나머지 키 포함), 없으면 키 없음,
        깨진 값(dict 아님·count 비정수·ids 비문자열 목록)은 생략."""
        ws = self._web_json(tmp_path)
        monkeypatch.setattr(sessions.instances, "pid_alive", lambda pid: True)
        oi = {
            "count": 3,
            "ids": ["u-2", "n-1", "r-1"],
            "oldest_turns": 7,
            "budget_left": 6,
        }
        self._status_json(ws, open_items=oi)
        assert sessions.live_state(ws, "S1")["open_items"] == oi
        # oldest_turns 는 모르면 null — 그대로 통과
        oi_null = {"count": 0, "ids": [], "oldest_turns": None, "budget_left": 8}
        self._status_json(ws, open_items=oi_null)
        assert sessions.live_state(ws, "S1")["open_items"] == oi_null
        # 구버전(키 없음)
        self._status_json(ws)
        assert "open_items" not in sessions.live_state(ws, "S1")
        # 깨진 값
        for bad in (
            "bogus",
            [1, 2],
            {"ids": ["u-1"]},
            {"count": "3", "ids": ["u-1"]},
            {"count": True, "ids": ["u-1"]},
            {"count": 1, "ids": "u-1"},
            {"count": 1, "ids": [1]},
        ):
            self._status_json(ws, open_items=bad)
            assert "open_items" not in sessions.live_state(ws, "S1"), bad

    def test_live_state_passes_stuck_through_from_status_file(
        self, tmp_path, monkeypatch
    ):
        """v1.45.0: agent-cli ≥10.120.0 의 ``stuck`` (main 런이 새 결과 없이 N 턴째)
        — 카드 🔁 배지의 소스. 모양이 맞으면 그대로(나머지 키 포함), 없으면 키 없음,
        깨진 값(dict 아님·turns 비정수·bool)은 생략."""
        ws = self._web_json(tmp_path)
        monkeypatch.setattr(sessions.instances, "pid_alive", lambda pid: True)
        st = {"turns": 4, "since_turn": 31, "asking": False}
        self._status_json(ws, stuck=st)
        assert sessions.live_state(ws, "S1")["stuck"] == st
        st_ask = {"turns": 6, "since_turn": 31, "asking": True}
        self._status_json(ws, stuck=st_ask, awaiting_input=True)
        assert sessions.live_state(ws, "S1")["stuck"] == st_ask
        # 구버전(키 없음)·null(제자리 아님)
        self._status_json(ws)
        assert "stuck" not in sessions.live_state(ws, "S1")
        self._status_json(ws, stuck=None)
        assert "stuck" not in sessions.live_state(ws, "S1")
        # 깨진 값
        for bad in (
            "bogus",
            [4],
            4,
            {"since_turn": 31},
            {"turns": "4"},
            {"turns": True},
            {"turns": 4.0},
        ):
            self._status_json(ws, stuck=bad)
            assert "stuck" not in sessions.live_state(ws, "S1"), bad

    def test_live_state_passes_jobs_through_from_status_file(
        self, tmp_path, monkeypatch
    ):
        """v1.46.0: agent-cli ≥10.121.0 의 ``jobs`` (돌고 있는 백그라운드 작업) — 카드
        🧵 칩의 소스. 모양이 맞으면 그대로, 없으면 키 없음, 깨진 값은 생략."""
        ws = self._web_json(tmp_path)
        monkeypatch.setattr(sessions.instances, "pid_alive", lambda pid: True)
        jb = {
            "count": 1,
            "list": [
                {
                    "id": 3,
                    "pid": 4242,
                    "owner": "main",
                    "command": "python3 -m http.server",
                    "started_at": 1794285259.5,
                }
            ],
        }
        self._status_json(ws, jobs=jb)
        assert sessions.live_state(ws, "S1")["jobs"] == jb
        self._status_json(ws)  # 구버전·작업 없음(키 생략)
        assert "jobs" not in sessions.live_state(ws, "S1")
        self._status_json(ws, jobs=None)
        assert "jobs" not in sessions.live_state(ws, "S1")
        for bad in (
            "bogus",
            [jb],
            2,
            {"list": []},
            {"count": "1", "list": []},
            {"count": True, "list": []},
            {"count": 1},
            {"count": 1, "list": "x"},
            {"count": 1, "list": [3]},
        ):
            self._status_json(ws, jobs=bad)
            assert "jobs" not in sessions.live_state(ws, "S1"), bad

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

    @pytest.fixture(autouse=True)
    def _require_agent_cli(self):
        # Real cross-repo contract needs agent-cli co-installed (dev/CI). Skip
        # (not fail) where it is absent so the board suite stays green solo.
        pytest.importorskip("agent_cli")

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
        from agent_cli.instance_file import write_status_file

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
        from agent_cli.instance_file import write_status_file
        from agent_cli.render.web import WebRenderer

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

"""에이전트 파일 계약 (docs/schedule-design.md §7) — 요청 반영·오프셋·캡·격리."""

from __future__ import annotations

import json

import pytest

from agent_board import sched_contract as sc
from agent_board.store import Store


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "board.db")
    post = store.create_post(topic="t")
    ws = tmp_path / "ws"
    (ws / ".agent-cli").mkdir(parents=True)
    yield store, post, ws
    store.close()


def _write_reqs(ws, *reqs):
    p = sc.requests_path(ws)
    with p.open("a", encoding="utf-8") as f:
        for r in reqs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _state(ws):
    return json.loads(sc.state_path(ws).read_text(encoding="utf-8"))


class TestApply:
    def test_add_applies_and_acks(self, env):
        store, post, ws = env
        _write_reqs(
            ws, {"op": "add", "cron": "0 9 * * 1", "prompt": "보고", "req_id": "r1"}
        )
        assert sc.apply_requests(store, post.post_id, ws) is True
        scheds = store.list_schedules(post.post_id)
        assert len(scheds) == 1 and scheds[0].source == "agent"
        st = _state(ws)
        assert st["results"]["r1"]["ok"] is True
        assert st["schedules"][0]["human"] == "매주 월 09:00"
        assert st["consumed"] == 1

    def test_offset_prevents_reprocessing(self, env):
        # ★핵심 회귀: 같은 라인을 두 번 적용하면 스케줄이 중복 등록됨
        store, post, ws = env
        _write_reqs(
            ws, {"op": "add", "cron": "* * * * *", "prompt": "x", "req_id": "r1"}
        )
        sc.apply_requests(store, post.post_id, ws)
        assert sc.apply_requests(store, post.post_id, ws) is False  # no new lines
        assert len(store.list_schedules(post.post_id)) == 1
        _write_reqs(
            ws, {"op": "add", "cron": "* * * * *", "prompt": "y", "req_id": "r2"}
        )
        sc.apply_requests(store, post.post_id, ws)
        assert len(store.list_schedules(post.post_id)) == 2  # 새 라인만 처리

    def test_delete_own_schedule(self, env):
        store, post, ws = env
        s = store.add_schedule(
            post_id=post.post_id, source="agent", cron="* * * * *", prompt="x"
        )
        _write_reqs(ws, {"op": "delete", "schedule_id": s.schedule_id, "req_id": "r1"})
        assert sc.apply_requests(store, post.post_id, ws) is True
        assert store.list_schedules(post.post_id) == []

    def test_delete_foreign_schedule_rejected(self, env):
        # 다른 post 의 스케줄은 삭제 불가 (존재 여부도 미노출)
        store, post, ws = env
        other = store.create_post(topic="other")
        s = store.add_schedule(
            post_id=other.post_id, source="user", cron="* * * * *", prompt="x"
        )
        _write_reqs(ws, {"op": "delete", "schedule_id": s.schedule_id, "req_id": "r1"})
        assert sc.apply_requests(store, post.post_id, ws) is False
        assert store.get_schedule(s.schedule_id) is not None
        assert "no such schedule" in _state(ws)["results"]["r1"]["error"]

    def test_agent_cap_enforced(self, env):
        store, post, ws = env
        reqs = [
            {"op": "add", "cron": "* * * * *", "prompt": f"p{i}", "req_id": f"r{i}"}
            for i in range(7)
        ]
        _write_reqs(ws, *reqs)
        sc.apply_requests(store, post.post_id, ws, agent_cap=5)
        assert store.count_agent_schedules(post.post_id) == 5
        st = _state(ws)
        assert "cap" in st["results"]["r5"]["error"]
        assert "cap" in st["results"]["r6"]["error"]

    def test_invalid_lines_reported_not_fatal(self, env):
        store, post, ws = env
        p = sc.requests_path(ws)
        p.write_text(
            'not json\n{"op":"weird","req_id":"r1"}\n'
            '{"op":"add","cron":"bad","prompt":"x","req_id":"r2"}\n'
            '{"op":"add","cron":"* * * * *","prompt":"ok","req_id":"r3"}\n',
            encoding="utf-8",
        )
        assert sc.apply_requests(store, post.post_id, ws) is True
        st = _state(ws)
        assert "invalid JSON" in st["results"]["line1"]["error"]
        assert "unknown op" in st["results"]["r1"]["error"]
        assert "invalid cron" in st["results"]["r2"]["error"]
        assert st["results"]["r3"]["ok"] is True

    def test_list_op_refreshes_state(self, env):
        store, post, ws = env
        store.add_schedule(
            post_id=post.post_id, source="user", cron="0 9 * * *", prompt="x"
        )
        _write_reqs(ws, {"op": "list", "req_id": "r1"})
        assert sc.apply_requests(store, post.post_id, ws) is False  # 변화 없음
        st = _state(ws)
        assert st["results"]["r1"]["ok"] is True
        assert len(st["schedules"]) == 1  # user 분 포함 전체 노출

    def test_truncated_request_file_resets_offset(self, env):
        # 계약은 append-only 라 truncation 은 비정상(수동 정리) — 라인 수가
        # 줄어든 경우만 감지해 offset 을 리셋한다(동일 길이 교체는 감지 불가).
        store, post, ws = env
        _write_reqs(
            ws,
            {"op": "list", "req_id": "r1"},
            {"op": "list", "req_id": "r2"},
        )
        sc.apply_requests(store, post.post_id, ws)  # consumed=2
        sc.requests_path(ws).write_text(
            '{"op":"list","req_id":"n1"}\n', encoding="utf-8"
        )
        sc.apply_requests(store, post.post_id, ws)
        assert _state(ws)["results"]["n1"]["ok"] is True

    def test_no_request_file_noop(self, env):
        store, post, ws = env
        assert sc.apply_requests(store, post.post_id, ws) is False
        assert not sc.state_path(ws).exists()


class TestRefreshState:
    def test_refresh_after_board_mutation(self, env):
        store, post, ws = env
        _write_reqs(ws, {"op": "list", "req_id": "r1"})
        sc.apply_requests(store, post.post_id, ws)
        s = store.add_schedule(
            post_id=post.post_id, source="user", cron="* * * * *", prompt="x"
        )
        sc.refresh_state(store, post.post_id, ws)
        st = _state(ws)
        assert [x["schedule_id"] for x in st["schedules"]] == [s.schedule_id]
        assert st["consumed"] == 1  # 오프셋 보존 (요청 미소비)

    def test_refresh_skips_untouched_workspace(self, env):
        store, post, ws = env
        sc.refresh_state(store, post.post_id, ws)
        assert not sc.state_path(ws).exists()  # 계약 미사용 워크스페이스 오염 금지


class TestNickname:
    def test_agent_add_with_nickname(self, env):
        store, post, ws = env
        _write_reqs(
            ws,
            {
                "op": "add",
                "cron": "* * * * *",
                "prompt": "x",
                "nickname": "봇",
                "req_id": "r1",
            },
        )
        sc.apply_requests(store, post.post_id, ws)
        s = store.list_schedules(post.post_id)[0]
        assert s.nickname == "봇"
        st = _state(ws)
        assert st["schedules"][0]["nickname"] == "봇"
        assert st["schedules"][0]["effective_nickname"] == "봇"

    def test_agent_add_without_nickname_effective_default(self, env):
        store, post, ws = env
        _write_reqs(
            ws, {"op": "add", "cron": "* * * * *", "prompt": "x", "req_id": "r1"}
        )
        sc.apply_requests(store, post.post_id, ws)
        st = _state(ws)
        assert st["schedules"][0]["nickname"] == ""
        assert st["schedules"][0]["effective_nickname"] == "⏰ Scheduler"

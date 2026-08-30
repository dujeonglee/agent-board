"""Post registry (SQLite) — CRUD + the invariants from DESIGN §2.

Persistent fields only (no port/token/status/last_query). post_id is the PK and
the workspace is derived from it (not stored). session_id is UNIQUE + nullable.
"""

from __future__ import annotations

import pytest

from agent_board.models import Post
from agent_board.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "board.db")
    yield s
    s.close()


class TestPostIdAllocation:
    """``post_id`` is the PK AND the workspace directory name, so allocation has
    to be unique against both namespaces (v1.30.0 — short random ids)."""

    def test_ids_are_short_and_from_the_id_alphabet(self, store):
        from agent_board.ids import ALPHABET, ID_LENGTH

        for i in range(20):
            p = store.create_post(topic=f"t{i}")
            assert len(p.post_id) == ID_LENGTH
            assert set(p.post_id) <= set(ALPHABET)

    def test_ids_are_unique(self, store):
        ids = {store.create_post(topic="t").post_id for _ in range(100)}
        assert len(ids) == 100

    def test_skips_ids_whose_workspace_directory_already_exists(self, store):
        """An orphaned directory (half-finished delete) must never be handed to
        a new post — it would inherit someone else's files."""
        seen = []

        def dir_taken(pid):
            seen.append(pid)
            return len(seen) <= 3  # first three draws are "taken"

        p = store.create_post(topic="t", dir_taken=dir_taken)
        assert len(seen) == 4
        assert p.post_id == seen[-1]
        assert store.get(p.post_id) is not None

    def test_retries_on_a_primary_key_collision(self, store, monkeypatch):
        taken = store.create_post(topic="first").post_id
        seq = iter([taken, taken, "zzzzzz"])
        monkeypatch.setattr("agent_board.store.new_post_id", lambda: next(seq))
        p = store.create_post(topic="second")
        assert p.post_id == "zzzzzz"
        assert store.get(taken).topic == "first"  # untouched

    def test_gives_up_loudly_rather_than_looping(self, store, monkeypatch):
        monkeypatch.setattr("agent_board.store.new_post_id", lambda: "fixed1")
        store.create_post(topic="first")
        with pytest.raises(RuntimeError, match="ID_LENGTH"):
            store.create_post(topic="second")

    def test_no_partial_row_after_giving_up(self, store, monkeypatch):
        monkeypatch.setattr("agent_board.store.new_post_id", lambda: "fixed2")
        store.create_post(topic="first")
        with pytest.raises(RuntimeError):
            store.create_post(topic="second")
        assert [p.topic for p in store.list_posts()] == ["first"]


class TestStore:
    def test_create_returns_post_with_generated_id(self, store):
        p = store.create_post(topic="DOOM 만들기")
        assert isinstance(p, Post)
        assert p.post_id  # non-empty generated id
        assert p.topic == "DOOM 만들기"
        assert p.session_id is None
        assert p.force_active is False
        assert p.created_at  # stamped

    def test_get_round_trips(self, store):
        p = store.create_post(topic="t")
        got = store.get(p.post_id)
        assert got is not None
        assert got.post_id == p.post_id and got.topic == "t"

    def test_get_missing_returns_none(self, store):
        assert store.get("nope") is None

    def test_post_ids_are_unique(self, store):
        ids = {store.create_post(topic=f"t{i}").post_id for i in range(20)}
        assert len(ids) == 20

    def test_list_is_recent_first(self, store):
        a = store.create_post(topic="a")
        b = store.create_post(topic="b")
        store.touch_opened(b.post_id)  # b opened most recently
        ids = [p.post_id for p in store.list_posts()]
        assert ids[0] == b.post_id and a.post_id in ids

    def test_set_session_id(self, store):
        p = store.create_post(topic="t")
        store.set_session_id(p.post_id, "1782999")
        assert store.get(p.post_id).session_id == "1782999"

    def test_session_id_is_unique(self, store):
        a = store.create_post(topic="a")
        b = store.create_post(topic="b")
        store.set_session_id(a.post_id, "S1")
        with pytest.raises(Exception):  # noqa: B017 — UNIQUE(session_id) 위반이 어떤 예외든 거부되면 충분
            store.set_session_id(b.post_id, "S1")  # one session = one post

    def test_set_force_active(self, store):
        p = store.create_post(topic="t")
        assert store.get(p.post_id).force_active is False
        store.set_force_active(p.post_id, True)
        assert store.get(p.post_id).force_active is True
        store.set_force_active(p.post_id, False)
        assert store.get(p.post_id).force_active is False

    def test_delete(self, store):
        p = store.create_post(topic="t")
        store.delete(p.post_id)
        assert store.get(p.post_id) is None

    def test_force_active_posts(self, store):
        a = store.create_post(topic="a")
        store.create_post(topic="b")
        store.set_force_active(a.post_id, True)
        ids = [p.post_id for p in store.force_active_posts()]
        assert ids == [a.post_id]  # only the force-active one (restart recovery)

    def test_persists_across_reopen(self, tmp_path):
        path = tmp_path / "board.db"
        s1 = Store(path)
        pid = s1.create_post(topic="persisted").post_id
        s1.close()
        s2 = Store(path)
        assert s2.get(pid).topic == "persisted"
        s2.close()


class TestSchedules:
    """schedules 테이블 (docs/schedule-design.md §2) — CRUD·cascade·missed."""

    def _post(self, store):
        return store.create_post(topic="t")

    def test_add_and_get(self, store):
        p = self._post(store)
        s = store.add_schedule(
            post_id=p.post_id,
            source="user",
            cron="0 9 * * 1",
            prompt="주간 보고를 작성해줘",
            label="주간 보고",
        )
        got = store.get_schedule(s.schedule_id)
        assert got.post_id == p.post_id
        assert got.source == "user"
        assert got.cron == "0 9 * * 1"
        assert got.enabled is True
        assert got.last_fired_at is None and got.missed_at is None

    def test_list_by_post(self, store):
        a, b = self._post(store), self._post(store)
        store.add_schedule(
            post_id=a.post_id, source="user", cron="* * * * *", prompt="x"
        )
        store.add_schedule(
            post_id=b.post_id, source="agent", cron="* * * * *", prompt="y"
        )
        assert len(store.list_schedules(a.post_id)) == 1
        assert len(store.list_schedules()) == 2

    def test_agent_count_for_cap(self, store):
        p = self._post(store)
        store.add_schedule(
            post_id=p.post_id, source="agent", cron="* * * * *", prompt="x"
        )
        store.add_schedule(
            post_id=p.post_id, source="user", cron="* * * * *", prompt="y"
        )
        assert store.count_agent_schedules(p.post_id) == 1  # user 분은 캡 미산입

    def test_delete_schedule(self, store):
        p = self._post(store)
        s = store.add_schedule(
            post_id=p.post_id, source="user", cron="* * * * *", prompt="x"
        )
        store.delete_schedule(s.schedule_id)
        assert store.get_schedule(s.schedule_id) is None

    def test_post_delete_cascades(self, store):
        # 글 삭제 후 스케줄이 남으면 유령 발화 — 핵심 회귀
        p = self._post(store)
        store.add_schedule(
            post_id=p.post_id, source="user", cron="* * * * *", prompt="x"
        )
        store.delete(p.post_id)
        assert store.list_schedules(p.post_id) == []

    def test_toggle(self, store):
        p = self._post(store)
        s = store.add_schedule(
            post_id=p.post_id, source="user", cron="* * * * *", prompt="x"
        )
        store.set_schedule_enabled(s.schedule_id, False)
        assert store.get_schedule(s.schedule_id).enabled is False

    def test_mark_fired_clears_missed(self, store):
        p = self._post(store)
        s = store.add_schedule(
            post_id=p.post_id, source="user", cron="* * * * *", prompt="x"
        )
        store.mark_missed(s.schedule_id, "2026-08-13T09:00:00")
        assert store.get_schedule(s.schedule_id).missed_at is not None
        store.mark_fired(s.schedule_id, "2026-08-13T10:00:00")
        got = store.get_schedule(s.schedule_id)
        assert got.last_fired_at == "2026-08-13T10:00:00"
        assert got.missed_at is None  # run-now 가 missed 해소를 겸함

    def test_mark_missed_collapses_to_latest(self, store):
        # 여러 주기 놓쳐도 질문 1건 (최신으로 덮어씀)
        p = self._post(store)
        s = store.add_schedule(
            post_id=p.post_id, source="user", cron="* * * * *", prompt="x"
        )
        store.mark_missed(s.schedule_id, "2026-08-06T09:00:00")
        store.mark_missed(s.schedule_id, "2026-08-13T09:00:00")
        assert store.get_schedule(s.schedule_id).missed_at == "2026-08-13T09:00:00"

    def test_clear_missed(self, store):
        p = self._post(store)
        s = store.add_schedule(
            post_id=p.post_id, source="user", cron="* * * * *", prompt="x"
        )
        store.mark_missed(s.schedule_id, "2026-08-13T09:00:00")
        store.clear_missed(s.schedule_id)
        assert store.get_schedule(s.schedule_id).missed_at is None

    def test_old_db_gains_table_on_reopen(self, tmp_path):
        # 구버전 DB(스케줄 테이블 없음) 재열기 → IF NOT EXISTS 로 추가, 기존 행 무손상
        db = tmp_path / "board.db"
        s1 = Store(db)
        s1.create_post(topic="old")
        s1._conn.execute("DROP TABLE schedules")
        s1._conn.commit()
        s1.close()
        s2 = Store(db)
        assert len(s2.list_posts()) == 1
        assert s2.list_schedules() == []
        s2.close()

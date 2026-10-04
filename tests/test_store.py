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

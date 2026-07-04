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
        with pytest.raises(Exception):
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

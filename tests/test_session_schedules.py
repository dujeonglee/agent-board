"""예약은 agent-cli 세션의 것이다 (v1.34.0) — 보드는 세션 폴더의 파일을 읽어
배지를 그리고, 켜진 예약이 있는 방이 죽으면 다시 띄울 뿐이다.

v1.33 까지는 보드가 스케줄러였다(DB 테이블 + asyncio 루프 + 워크스페이스
요청·회신 파일 계약). 그 데이터는 기동 때 한 번 세션 폴더로 옮긴다.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent_board import app as appmod
from agent_board import clone, instances, session_schedules
from agent_board.config import Config
from agent_board.live_events import LiveEvents
from agent_board.models import Post
from agent_board.store import Store


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=tmp_path / "d", workspaces_root=tmp_path / "w")


@pytest.fixture
def store(cfg):
    s = Store(cfg.db_path)
    yield s
    s.close()


def _post(store, sid="S1"):
    p = store.create_post(topic="t")
    if sid:
        store.set_session_id(p.post_id, sid)
    return store.get(p.post_id)


def _row(**kw):
    base = {
        "id": "a1",
        "source": "agent",
        "cron": "0 9 * * 1",
        "prompt": "weekly report",
        "label": "주간 보고",
        "nickname": "",
        "enabled": True,
        "created_at": "2026-10-04T10:00:00",
        "settled_at": "2026-10-04T10:00:00",
        "last_fired_at": None,
        "missed_at": None,
    }
    return {**base, **kw}


def _write(cfg, post, rows):
    path = session_schedules.path_for(cfg.workspace_for(post.post_id), post.session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"v": 1, "schedules": rows}))
    return path


class TestRead:
    def test_no_session_no_file_or_corrupt_is_empty(self, cfg, store):
        post = _post(store)
        ws = cfg.workspace_for(post.post_id)
        assert session_schedules.read(ws, None) == []
        assert session_schedules.read(ws, post.session_id) == []
        _write(cfg, post, []).write_text("{not json")
        assert session_schedules.read(ws, post.session_id) == []

    def test_summary_counts_and_lists_missed(self, cfg, store):
        post = _post(store)
        _write(
            cfg,
            post,
            [
                _row(),
                _row(
                    id="b2", label="", cron="0 * * * *", missed_at="2026-10-05T09:00:00"
                ),
            ],
        )
        s = session_schedules.summary(cfg.workspace_for(post.post_id), post.session_id)
        assert s["count"] == 2
        assert s["missed"] == [
            {"id": "b2", "label": "0 * * * *", "missed_at": "2026-10-05T09:00:00"}
        ]

    def test_has_enabled(self, cfg, store):
        post = _post(store)
        ws = cfg.workspace_for(post.post_id)
        assert session_schedules.has_enabled(ws, post.session_id) is False
        _write(cfg, post, [_row(enabled=False)])
        assert session_schedules.has_enabled(ws, post.session_id) is False
        _write(cfg, post, [_row(enabled=False), _row(id="b2")])
        assert session_schedules.has_enabled(ws, post.session_id) is True


class TestPostView:
    def test_card_summary_comes_from_the_session_file(self, cfg, store):
        post = _post(store)
        _write(cfg, post, [_row(missed_at="2026-10-05T09:00:00")])
        view = appmod._post_view(cfg, store, post)
        assert view["schedules"]["count"] == 1
        assert view["schedules"]["missed"][0]["label"] == "주간 보고"


class TestLiveSignature:
    def test_schedule_file_change_flips_the_row_signature(self, cfg, store):
        """에이전트가 방 안에서 예약을 걸면 보드 카드의 배지가 따라온다."""
        post = _post(store)
        live = LiveEvents(cfg, store, lambda p: {"post_id": p.post_id})
        live._prime()
        assert live._scan() == []
        _write(cfg, post, [_row()])
        (ev,) = live._scan()
        assert ev["type"] == "post_update"


def _legacy_table(cfg, rows):
    conn = sqlite3.connect(cfg.db_path)
    conn.execute(
        "CREATE TABLE schedules (schedule_id TEXT PRIMARY KEY, post_id TEXT NOT NULL, "
        "source TEXT NOT NULL, cron TEXT NOT NULL, prompt TEXT NOT NULL, "
        "label TEXT NOT NULL DEFAULT '', nickname TEXT NOT NULL DEFAULT '', "
        "enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, "
        "last_fired_at TEXT, missed_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


class TestMigrateLegacy:
    def test_new_db_has_no_table_and_migration_is_a_noop(self, cfg, store):
        assert store.drain_legacy_schedules() == []
        assert session_schedules.migrate_legacy(store, cfg) == 0

    def test_rows_move_into_the_session_folder_and_the_table_is_dropped(
        self, cfg, store
    ):
        post = _post(store)
        _legacy_table(
            cfg,
            [
                ("s1", post.post_id, "user", "0 9 * * 1", "weekly", "주간", "봇", 1,
                 "2026-08-13T00:00:00+00:00", "2026-09-28T09:00:01", None),
                ("s2", post.post_id, "agent", "0 * * * *", "hourly", "", "", 0,
                 "2026-08-14T00:00:00+00:00", None, "2026-09-30T09:00:00"),
            ],
        )  # fmt: skip
        assert session_schedules.migrate_legacy(store, cfg) == 2

        a, b = session_schedules.read(cfg.workspace_for(post.post_id), post.session_id)
        assert (a["id"], a["source"], a["cron"], a["prompt"]) == (
            "s1", "user", "0 9 * * 1", "weekly",
        )  # fmt: skip
        assert (a["label"], a["nickname"], a["enabled"]) == ("주간", "봇", True)
        # 보드가 마지막으로 내린 판정까지를 "정리됨" 으로 넘긴다 — 옮긴 직후
        # agent-cli 가 지난 발화를 다시 놓친 것으로 잡지 않는다.
        assert a["last_fired_at"] == a["settled_at"] == "2026-09-28T09:00:01"
        assert "+" not in a["created_at"]  # 서버 로컬 naive
        # 답을 기다리던 놓친 예약은 질문으로 유지된다.
        assert b["enabled"] is False
        assert b["missed_at"] == b["settled_at"] == "2026-09-30T09:00:00"

        conn = sqlite3.connect(cfg.db_path)
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='schedules'"
        ).fetchone()
        conn.close()
        assert session_schedules.migrate_legacy(store, cfg) == 0  # 두 번째는 no-op

    def test_keeps_rows_the_session_already_has(self, cfg, store):
        post = _post(store)
        _write(cfg, post, [_row(id="mine")])
        _legacy_table(
            cfg,
            [("s1", post.post_id, "user", "0 9 * * *", "x", "", "", 1, "2026-08-13T00:00:00", None, None)],
        )  # fmt: skip
        session_schedules.migrate_legacy(store, cfg)
        rows = session_schedules.read(cfg.workspace_for(post.post_id), post.session_id)
        assert [r["id"] for r in rows] == ["mine", "s1"]

    def test_never_opened_post_has_nowhere_to_put_them(self, cfg, store, caplog):
        post = _post(store, sid=None)
        _legacy_table(
            cfg,
            [("s1", post.post_id, "user", "0 9 * * *", "x", "", "", 1, "2026-08-13T00:00:00", None, None)],
        )  # fmt: skip
        with caplog.at_level("WARNING"):
            assert session_schedules.migrate_legacy(store, cfg) == 0
        assert "no session to own them" in caplog.text

    def test_running_instance_is_stopped_before_its_file_is_written(self, cfg, store):
        """예약 파일은 그 세션의 프로세스가 기동 때 읽어 메모리에 쥔다 — 떠 있는
        프로세스 밑에서 쓰면 반영되지 않고 다음 저장에 덮인다."""
        post = _post(store)
        _legacy_table(
            cfg,
            [("s1", post.post_id, "user", "0 9 * * *", "x", "", "", 1, "2026-08-13T00:00:00", None, None)],
        )  # fmt: skip
        order = []
        path = session_schedules.path_for(
            cfg.workspace_for(post.post_id), post.session_id
        )

        def stop(p):
            order.append(("stop", p.post_id, path.exists()))

        session_schedules.migrate_legacy(store, cfg, stop_instance=stop)
        assert order == [("stop", post.post_id, False)]
        assert path.exists()


class TestReviver:
    def _reviver(self, cfg, store, now):
        opened = []

        async def open_fn(post_id):
            opened.append(post_id)

        r = appmod.ScheduleReviver(
            cfg, store, open_fn, min_interval=60.0, clock=lambda: now[0]
        )
        return r, opened

    def test_due_only_with_an_enabled_schedule(self, cfg, store):
        now = [100.0]
        r, _ = self._reviver(cfg, store, now)
        post = _post(store)
        assert r.due(post.post_id) is False  # 예약 없음
        _write(cfg, post, [_row(enabled=False)])
        assert r.due(post.post_id) is False  # 꺼진 예약뿐
        _write(cfg, post, [_row()])
        assert r.due(post.post_id) is True
        assert r.due("gone") is False

    def test_a_crash_looping_instance_is_not_revived_every_scan(self, cfg, store):
        """기동하자마자 죽는 인스턴스를 1초 스캔마다 되살리면 스폰 루프가 된다."""
        now = [100.0]
        r, _ = self._reviver(cfg, store, now)
        post = _post(store)
        _write(cfg, post, [_row()])
        assert r.due(post.post_id) is True
        now[0] += 5
        assert r.due(post.post_id) is False
        now[0] += 60
        assert r.due(post.post_id) is True

    @pytest.mark.asyncio
    async def test_revive_opens_the_post(self, cfg, store):
        import asyncio

        now = [100.0]
        r, opened = self._reviver(cfg, store, now)
        post = _post(store)
        _write(cfg, post, [_row()])
        r.revive(post.post_id)
        await asyncio.sleep(0)
        await asyncio.gather(*r._tasks)
        assert opened == [post.post_id]

    @pytest.mark.asyncio
    async def test_restore_revives_a_dead_room_that_has_schedules(
        self, cfg, store, monkeypatch
    ):
        """재부팅 뒤: 켜진 예약이 있는 방은 다시 띄운다. 예약 없는 방은 종전처럼
        다음 열기까지 꺼 둔다."""

        class _Router:
            def ensure_route(self, *a):
                raise AssertionError("no live instance in this test")

        class _KA:
            async def enable(self, post_id):
                pass

        with_sched = _post(store, "S1")
        without = _post(store, "S2")
        _write(cfg, with_sched, [_row()])
        monkeypatch.setattr(instances, "alive", lambda info: False)
        revived = []
        await appmod.restore_state(cfg, store, _Router(), _KA(), revived.append)
        assert revived == [with_sched.post_id]
        assert without.post_id not in revived


class TestSpawnAndClone:
    def test_spawn_no_longer_sets_the_scheduler_env(self, cfg, monkeypatch):
        """`AGENT_CLI_SCHEDULER` 는 보드가 스케줄러이던 때의 게이트다."""
        seen = {}

        class _P:
            pass

        def popen(cmd, **kw):
            seen.update(kw)
            return _P()

        monkeypatch.setattr(instances.subprocess, "Popen", popen)
        instances.spawn(cfg, Post("p1", "t", model_id="m"), port=50001, token="x")
        assert "AGENT_CLI_SCHEDULER" not in (seen.get("env") or {})

    def test_clone_does_not_inherit_the_sources_schedules(self):
        """복제본이 예약 파일을 물려받으면 두 방이 같은 예약을 각자 발화한다."""
        assert {"schedules.json", "schedule-log.jsonl", "session.lock"} <= (
            clone._SIDECAR_EXCLUDE
        )

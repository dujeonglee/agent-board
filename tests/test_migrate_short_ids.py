"""The one-off uuid-hex → short-id migration (``scripts/migrate_short_post_ids``).

Run as a subprocess so the test exercises the real entry point (argparse,
``Config.from_env``, exit code) rather than a re-implementation of it.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent_board.ids import ID_LENGTH
from agent_board.store import Store

SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "migrate_short_post_ids.py"
)

OLD_ID = "cf2dad8ec6894e6583a72b0ba02fe98e"


@pytest.fixture
def board_home(tmp_path):
    """A board home holding one OLD-style post with a populated workspace."""
    home = tmp_path / "home"
    ws_root = home / "ws"
    ws_root.mkdir(parents=True)
    store = Store(home / "board.db")
    store.create_post(topic="placeholder")  # a new-style row, must be left alone
    store.close()
    conn = sqlite3.connect(home / "board.db")
    conn.execute(
        "INSERT INTO posts (post_id, topic, model_id, force_active, created_at) "
        "VALUES (?, 'rwar', NULL, 0, '2026-08-19T13:17:25Z')",
        (OLD_ID,),
    )
    conn.execute(
        "INSERT INTO schedules (schedule_id, post_id, source, cron, prompt, "
        "created_at) VALUES ('s1', ?, 'user', '0 9 * * *', 'ping', 'now')",
        (OLD_ID,),
    )
    conn.commit()
    conn.close()
    (ws_root / OLD_ID).mkdir()
    (ws_root / OLD_ID / "worm_game.html").write_text("<html>keep me</html>")
    return home


def _run(home: Path, *args: str):
    env = {**os.environ, "AGENT_BOARD_HOME": str(home)}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _rows(home: Path):
    conn = sqlite3.connect(home / "board.db")
    conn.row_factory = sqlite3.Row
    posts = {r["post_id"]: r["topic"] for r in conn.execute("SELECT * FROM posts")}
    scheds = [r["post_id"] for r in conn.execute("SELECT post_id FROM schedules")]
    conn.close()
    return posts, scheds


class TestDryRun:
    def test_reports_without_changing_anything(self, board_home):
        r = _run(board_home)
        assert r.returncode == 0, r.stderr
        assert OLD_ID in r.stdout
        assert "dry run" in r.stdout
        posts, _ = _rows(board_home)
        assert OLD_ID in posts
        assert (board_home / "ws" / OLD_ID).is_dir()


class TestApply:
    def test_rekeys_row_directory_and_schedule(self, board_home):
        r = _run(board_home, "--apply")
        assert r.returncode == 0, r.stderr
        posts, scheds = _rows(board_home)
        assert OLD_ID not in posts
        new = next(pid for pid, topic in posts.items() if topic == "rwar")
        assert len(new) == ID_LENGTH
        # directory moved, contents intact
        assert not (board_home / "ws" / OLD_ID).exists()
        assert (board_home / "ws" / new / "worm_game.html").read_text() == (
            "<html>keep me</html>"
        )
        # the foreign key followed
        assert scheds == [new]

    def test_leaves_already_short_posts_alone(self, board_home):
        before, _ = _rows(board_home)
        short = [pid for pid in before if len(pid) == ID_LENGTH]
        assert short, "fixture should contain one new-style post"
        _run(board_home, "--apply")
        after, _ = _rows(board_home)
        for pid in short:
            assert pid in after

    def test_is_idempotent(self, board_home):
        _run(board_home, "--apply")
        posts_once, _ = _rows(board_home)
        r = _run(board_home, "--apply")
        assert r.returncode == 0
        assert "0 to migrate" in r.stdout
        posts_twice, _ = _rows(board_home)
        assert posts_once == posts_twice

    def test_handles_a_post_with_no_workspace_directory(self, board_home):
        """A post that was never opened has no directory — must not crash."""
        import shutil

        shutil.rmtree(board_home / "ws" / OLD_ID)
        r = _run(board_home, "--apply")
        assert r.returncode == 0, r.stderr
        posts, _ = _rows(board_home)
        assert OLD_ID not in posts


class TestMissingDb:
    def test_exits_nonzero_with_a_message(self, tmp_path):
        r = _run(tmp_path / "nope")
        assert r.returncode == 1
        assert "no board.db" in r.stdout


class TestLegacyRootMove:
    """v1.30.0 also shortened the default root name. Without moving it, a
    migrated board looks in an empty ``ws/`` and every post appears to have
    lost its files."""

    @pytest.fixture
    def legacy_home(self, tmp_path):
        home = tmp_path / "home"
        legacy = home / "workspaces"  # the OLD default name
        legacy.mkdir(parents=True)
        store = Store(home / "board.db")
        store.close()
        conn = sqlite3.connect(home / "board.db")
        conn.execute(
            "INSERT INTO posts (post_id, topic, model_id, force_active, "
            "created_at) VALUES (?, 'rwar', NULL, 0, 'now')",
            (OLD_ID,),
        )
        conn.commit()
        conn.close()
        (legacy / OLD_ID).mkdir()
        (legacy / OLD_ID / "keep.txt").write_text("payload")
        return home

    def test_dry_run_finds_files_under_the_legacy_root(self, legacy_home):
        r = _run(legacy_home)
        assert r.returncode == 0, r.stderr
        assert "root:" in r.stdout
        # must NOT claim the post has no workspace — it does, under the old root
        assert "no workspace dir" not in r.stdout
        assert (legacy_home / "workspaces" / OLD_ID).is_dir()  # untouched

    def test_apply_moves_the_root_then_the_post(self, legacy_home):
        r = _run(legacy_home, "--apply")
        assert r.returncode == 0, r.stderr
        assert not (legacy_home / "workspaces").exists()
        posts, _ = _rows(legacy_home)
        new = next(iter(posts))
        assert (legacy_home / "ws" / new / "keep.txt").read_text() == "payload"

    def test_does_not_touch_an_explicitly_configured_root(self, legacy_home):
        """An operator who set AGENT_BOARD_WORKSPACES chose that path."""
        custom = legacy_home / "custom"
        custom.mkdir()
        env = {
            **os.environ,
            "AGENT_BOARD_HOME": str(legacy_home),
            "AGENT_BOARD_WORKSPACES": str(custom),
        }
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--apply"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert r.returncode == 0, r.stderr
        assert "root:" not in r.stdout
        assert (legacy_home / "workspaces").is_dir()  # left alone

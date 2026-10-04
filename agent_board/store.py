"""SQLite post registry (DESIGN §2).

Stores ONLY persistent post metadata — never ephemeral state (port/token/status/
last_query), which is read live from the instance's ``web.json`` + session
files. ``post_id`` is the PK (workspace derives from it); ``session_id`` is a
nullable UNIQUE (one session = one post).

The sqlite3 calls are synchronous; the FastAPI layer wraps them in
``run_in_executor`` so they never block the event loop (no extra dependency).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from agent_board._sqlite import sqlite3  # stdlib sqlite3, or pysqlite3 fallback
from agent_board.ids import new_post_id
from agent_board.models import Post

# Draws before giving up on finding a free post id (see create_post).
_ID_ALLOC_ATTEMPTS = 16

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  post_id        TEXT PRIMARY KEY,
  topic          TEXT NOT NULL,
  session_id     TEXT UNIQUE,
  model_id       TEXT,
  force_active   INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL,
  last_opened_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_recent
  ON posts(last_opened_at DESC, created_at DESC);
"""

# additive migrations for DBs created before a column existed — old rows get the
# default (no behaviour change), so resuming an old DB never breaks. Keyed by
# table, then column → the ALTER that adds it. (The dropped ``directive`` column
# is simply left unqueried on old DBs — no migration needed.)
_MIGRATIONS = {
    "posts": {"model_id": "ALTER TABLE posts ADD COLUMN model_id TEXT"},
}

_COLS = "post_id, topic, session_id, model_id, force_active, created_at, last_opened_at"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_post(row: sqlite3.Row) -> Post:
    return Post(
        post_id=row["post_id"],
        topic=row["topic"],
        session_id=row["session_id"],
        model_id=row["model_id"],
        force_active=bool(row["force_active"]),
        created_at=row["created_at"],
        last_opened_at=row["last_opened_at"],
    )


class Store:
    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        for table, cols_ddl in _MIGRATIONS.items():
            existing = {
                r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")
            }
            for col, ddl in cols_ddl.items():
                if col not in existing:
                    self._conn.execute(ddl)

    def close(self) -> None:
        self._conn.close()

    # ── writes ──────────────────────────────────────────────
    def create_post(
        self,
        *,
        topic: str,
        model_id: str | None = None,
        dir_taken: Callable[[str], bool] | None = None,
    ) -> Post:
        """Allocate a post with a fresh short id (:mod:`agent_board.ids`).

        The id must be unique against TWO namespaces, because it is both the
        primary key and the workspace directory name: the ``posts`` table (the
        PK enforces it — a duplicate raises ``IntegrityError``) and the
        workspaces directory. ``dir_taken`` is how the caller supplies the
        second check (``lambda pid: config.workspace_for(pid).exists()``); the
        store must not import Config, and an orphaned directory left by a
        half-finished delete must never be handed to a new post.

        With a 6-character id a collision is ~1 in 10^9 per draw, so the retry
        loop realistically never runs twice — it exists so that a shortened
        ``ID_LENGTH``, or a board with a very large number of posts, degrades
        into a retry instead of a 500."""
        for _ in range(_ID_ALLOC_ATTEMPTS):
            post = Post(
                post_id=new_post_id(),
                topic=topic,
                model_id=model_id,
                created_at=_now(),
            )
            if dir_taken is not None and dir_taken(post.post_id):
                continue
            try:
                self._conn.execute(
                    "INSERT INTO posts (post_id, topic, model_id, force_active, "
                    "created_at) VALUES (?, ?, ?, 0, ?)",
                    (post.post_id, post.topic, post.model_id, post.created_at),
                )
            except sqlite3.IntegrityError:
                continue  # PK collision — draw again
            self._conn.commit()
            return post
        raise RuntimeError(
            f"could not allocate a free post id in {_ID_ALLOC_ATTEMPTS} attempts "
            "— raise agent_board.ids.ID_LENGTH"
        )

    def set_session_id(self, post_id: str, session_id: str) -> None:
        # session_id UNIQUE → raises sqlite3.IntegrityError if already claimed
        self._conn.execute(
            "UPDATE posts SET session_id = ? WHERE post_id = ?",
            (session_id, post_id),
        )
        self._conn.commit()

    def set_model(self, post_id: str, model_id: str | None) -> None:
        self._conn.execute(
            "UPDATE posts SET model_id = ? WHERE post_id = ?",
            (model_id, post_id),
        )
        self._conn.commit()

    def set_force_active(self, post_id: str, enabled: bool) -> None:
        self._conn.execute(
            "UPDATE posts SET force_active = ? WHERE post_id = ?",
            (1 if enabled else 0, post_id),
        )
        self._conn.commit()

    def touch_opened(self, post_id: str) -> None:
        self._conn.execute(
            "UPDATE posts SET last_opened_at = ? WHERE post_id = ?",
            (_now(), post_id),
        )
        self._conn.commit()

    def delete(self, post_id: str) -> None:
        self._conn.execute("DELETE FROM posts WHERE post_id = ?", (post_id,))
        self._conn.commit()

    # ── reads ───────────────────────────────────────────────
    def get(self, post_id: str) -> Post | None:
        row = self._conn.execute(
            f"SELECT {_COLS} FROM posts WHERE post_id = ?", (post_id,)
        ).fetchone()
        return _row_to_post(row) if row else None

    def list_posts(self) -> list[Post]:
        rows = self._conn.execute(
            f"SELECT {_COLS} FROM posts ORDER BY last_opened_at DESC, created_at DESC"
        ).fetchall()
        return [_row_to_post(r) for r in rows]

    def force_active_posts(self) -> list[Post]:
        rows = self._conn.execute(
            f"SELECT {_COLS} FROM posts WHERE force_active = 1"
        ).fetchall()
        return [_row_to_post(r) for r in rows]

    # ── v1.33 예약 테이블의 일회성 이전 ─────────────────────
    def drain_legacy_schedules(self) -> list[dict]:
        """v1.33 까지의 ``schedules`` 테이블 행을 전부 돌려주고 테이블을 지운다.

        v1.34.0 부터 예약은 agent-cli 세션의 것이다 — 보드는 저장하지 않는다.
        테이블이 없으면(새 DB, 또는 이미 옮김) 빈 목록.
        """
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schedules'"
        ).fetchone()
        if not exists:
            return []
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(schedules)")}
        rows = [dict(r) for r in self._conn.execute("SELECT * FROM schedules")]
        for r in rows:
            if "nickname" not in cols:  # 1.26.0 이전 DB
                r["nickname"] = ""
        self._conn.execute("DROP TABLE schedules")
        self._conn.commit()
        return rows

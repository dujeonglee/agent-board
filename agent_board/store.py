"""SQLite post registry (DESIGN §2).

Stores ONLY persistent post metadata — never ephemeral state (port/token/status/
last_query), which is read live from the instance's ``web.json`` + session
files. ``post_id`` is the PK (workspace derives from it); ``session_id`` is a
nullable UNIQUE (one session = one post).

The sqlite3 calls are synchronous; the FastAPI layer wraps them in
``run_in_executor`` so they never block the event loop (no extra dependency).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent_board._sqlite import sqlite3  # stdlib sqlite3, or pysqlite3 fallback
from agent_board.models import Post

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  post_id        TEXT PRIMARY KEY,
  topic          TEXT NOT NULL,
  session_id     TEXT UNIQUE,
  directive      TEXT,
  model_id       TEXT,
  force_active   INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL,
  last_opened_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_recent
  ON posts(last_opened_at DESC, created_at DESC);
"""

# additive, nullable migrations for DBs created before a column existed —
# old rows get NULL (no behaviour change), so resuming an old DB never breaks.
_MIGRATIONS = {"model_id": "ALTER TABLE posts ADD COLUMN model_id TEXT"}

_COLS = (
    "post_id, topic, session_id, directive, model_id, force_active, "
    "created_at, last_opened_at"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_post(row: sqlite3.Row) -> Post:
    return Post(
        post_id=row["post_id"],
        topic=row["topic"],
        session_id=row["session_id"],
        directive=row["directive"],
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
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(posts)")}
        for col, ddl in _MIGRATIONS.items():
            if col not in cols:
                self._conn.execute(ddl)

    def close(self) -> None:
        self._conn.close()

    # ── writes ──────────────────────────────────────────────
    def create_post(
        self,
        *,
        topic: str,
        directive: str | None = None,
        model_id: str | None = None,
    ) -> Post:
        post = Post(
            post_id=uuid.uuid4().hex,
            topic=topic,
            directive=directive,
            model_id=model_id,
            created_at=_now(),
        )
        self._conn.execute(
            "INSERT INTO posts (post_id, topic, directive, model_id, force_active, "
            "created_at) VALUES (?, ?, ?, ?, 0, ?)",
            (post.post_id, post.topic, post.directive, post.model_id, post.created_at),
        )
        self._conn.commit()
        return post

    def set_session_id(self, post_id: str, session_id: str) -> None:
        # session_id UNIQUE → raises sqlite3.IntegrityError if already claimed
        self._conn.execute(
            "UPDATE posts SET session_id = ? WHERE post_id = ?",
            (session_id, post_id),
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

#!/usr/bin/env python3
"""One-off: re-key existing posts from uuid4 hex to short ids (v1.30.0).

The board itself carries NO backward compatibility for the old 32-character
ids — ``post_id`` is opaque everywhere, so old rows keep working as-is; they
just keep their long directory names and long paths forever. This script exists
so an existing board can adopt the new shape instead of living with a mix.

Two things move. First the workspaces ROOT, if it is still on the old default
name (``<home>/workspaces`` -> ``<home>/ws``) — v1.30.0 shortened that too, and
without this an existing board looks in an empty directory and every post seems
to have lost its files. Then each post's own directory is renamed and its row
rewritten (plus the ``schedules.post_id`` foreign key), one transaction per
post, renaming the directory FIRST so a crash leaves the DB pointing at a
directory that still exists rather than the other way round.

What it does NOT do: rewrite absolute paths already recorded inside a session's
history (the agent's own transcript). Those become stale references to a
directory that no longer exists. That is why this is opt-in and why the default
is a dry run.

    python scripts/migrate_short_post_ids.py            # dry run
    python scripts/migrate_short_post_ids.py --apply    # do it

STOP THE BOARD (and every instance it spawned) FIRST. A running instance has the
old workspace as its cwd and will fail to save its session if the directory
moves underneath it.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_board.config import Config
from agent_board.ids import ID_LENGTH, new_post_id


def _free_id(conn: sqlite3.Connection, root: Path) -> str:
    for _ in range(64):
        pid = new_post_id()
        taken = conn.execute("SELECT 1 FROM posts WHERE post_id = ?", (pid,)).fetchone()
        if taken is None and not (root / pid).exists():
            return pid
    raise RuntimeError("could not find a free id — raise agent_board.ids.ID_LENGTH")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually migrate")
    args = ap.parse_args()

    cfg = Config.from_env()
    if not cfg.db_path.exists():
        print(f"no board.db at {cfg.db_path}")
        return 1
    print(f"db:         {cfg.db_path}")
    print(f"workspaces: {cfg.workspaces_root}")

    # v1.30.0 also shortened the default root ("workspaces" -> "ws"). An
    # existing board's directories still sit under the old name, so the board
    # would look in an empty "ws" and every post would appear to have lost its
    # files. Move the whole root first — but ONLY when the configured root is
    # the default one; an operator who set AGENT_BOARD_WORKSPACES chose that
    # path deliberately and we must not second-guess it.
    legacy_root = cfg.workspaces_root.parent / "workspaces"
    root_move = (
        cfg.workspaces_root.name == "ws"
        and not cfg.workspaces_root.exists()
        and legacy_root.is_dir()
    )
    if root_move:
        print(f"root:       {legacy_root}  ->  {cfg.workspaces_root}")
        if args.apply:
            legacy_root.rename(cfg.workspaces_root)
    # Where the directories are RIGHT NOW. On a dry run the root has not moved,
    # so probing the new root would report every post as having no workspace —
    # a dry run that misdescribes what will happen is worse than none.
    probe_root = legacy_root if (root_move and not args.apply) else cfg.workspaces_root
    print()

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT post_id, topic FROM posts").fetchall()
    todo = [r for r in rows if len(r["post_id"]) > ID_LENGTH]
    print(f"posts: {len(rows)} total, {len(todo)} to migrate\n")
    if not todo:
        return 0

    for r in todo:
        old = r["post_id"]
        new = _free_id(conn, cfg.workspaces_root)
        old_ws, new_ws = probe_root / old, cfg.workspace_for(new)
        mark = "" if old_ws.is_dir() else "   (no workspace dir)"
        print(f"  {old}  ->  {new}   {r['topic'][:40]}{mark}")
        if not args.apply:
            continue
        # directory first: a crash then leaves the DB pointing at a directory
        # that still exists (recoverable) rather than at a missing one.
        if old_ws.is_dir():
            new_ws.parent.mkdir(parents=True, exist_ok=True)
            old_ws.rename(new_ws)
        try:
            with conn:
                conn.execute(
                    "UPDATE posts SET post_id = ? WHERE post_id = ?", (new, old)
                )
                conn.execute(
                    "UPDATE schedules SET post_id = ? WHERE post_id = ?", (new, old)
                )
        except Exception:
            if new_ws.is_dir():  # roll the rename back so state stays consistent
                new_ws.rename(old_ws)
            raise

    if not args.apply:
        print("\n(dry run — re-run with --apply)")
    else:
        print(f"\nmigrated {len(todo)} post(s)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

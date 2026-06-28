"""SQLite import shim (mirrors agent-cli's ``code_index._sqlite``).

Some Python distributions (locked-down servers, minimal/Alpine builds, custom
``--without-sqlite`` rebuilds) ship CPython WITHOUT the ``sqlite3`` extension
module — a plain ``import sqlite3`` then raises ``No module named '_sqlite3'``
and the board fails to start.

This shim prefers stdlib ``sqlite3`` (zero overhead on normal systems) and
falls back to the ``pysqlite3-binary`` wheel (bundles its own SQLite C library)
only when the stdlib import fails. Both expose the same DB-API 2.0 surface the
store uses (``Row`` factory, ``connect``, ``IntegrityError``), so callers see no
difference.

Use ``from agent_board._sqlite import sqlite3`` — NOT a plain ``import sqlite3``.
"""

from __future__ import annotations

try:
    import sqlite3
except ImportError:  # pragma: no cover — exercised where stdlib sqlite3 is absent
    # pysqlite3-binary is a base dep on x86_64 Linux (see pyproject.toml). On
    # other platforms a sqlite-less Python must be replaced with one built WITH
    # sqlite (conda / system Python ship it); let the ImportError propagate.
    from pysqlite3 import dbapi2 as sqlite3

__all__ = ["sqlite3"]

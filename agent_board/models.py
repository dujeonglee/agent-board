"""Core data types.

A ``Post`` is the persistent record of a board post. Its workspace is NOT
stored — it is derived from ``post_id`` under the configured workspaces root
(see ``config.Config.workspace_for``), so there is no user-supplied path and no
shared-workspace collisions. Ephemeral state (port/token/status/last_query) is
never stored here; it is read live from the instance's ``web.json`` + session
files.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Post:
    post_id: str
    topic: str
    session_id: str | None = None  # filled on first open (agent-cli session)
    model_id: str | None = None  # agent-cli model id → `--model` (None = default)
    force_active: bool = False
    created_at: str = ""
    last_opened_at: str | None = None

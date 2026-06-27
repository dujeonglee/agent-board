"""Derived views over an agent-cli session's on-disk files (DESIGN §7).

agent-cli is not modified, so the board reads its session files directly — this
is the INTEGRATION CONTRACT (couples to agent-cli's on-disk format; bump
carefully across agent-cli versions):

- ``last_query``  ← ``history.jsonl`` last ``{role:user, kind:query}`` record.
- ``status``      ← ``web.json`` presence + pid alive + /api/health.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_board import instances


def _session_dir(workspace: Path, session_id: str) -> Path:
    return Path(workspace) / ".agent-cli" / "sessions" / session_id


def last_query(workspace: Path, session_id: str | None) -> str | None:
    """The most recent user query text for this session, or None.

    Reads history.jsonl from the end so the last query is found without caring
    about everything before it."""
    if not session_id:
        return None
    path = _session_dir(workspace, session_id) / "history.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return None
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("role") == "user" and rec.get("kind") == "query":
            return rec.get("text") or rec.get("content")
    return None


def status(workspace: Path, session_id: str | None) -> str:
    """Three states derived from the live instance:

    - ``"working"`` — up and the agent is processing a turn (LLM responding),
    - ``"running"`` — up and idle (response done, waiting for input),
    - ``"idle"``    — not running (incl. a never-opened post).

    One /api/health call yields both liveness and the ``busy`` bit (agent-cli
    >= 4.17.2; a missing ``busy`` field degrades to ``"running"``)."""
    if not session_id:
        return "idle"
    info = instances.read_web_json(workspace, session_id)
    if not info:
        return "idle"
    pid, port = info.get("pid"), info.get("port")
    if not (pid and instances.pid_alive(pid) and port):
        return "idle"
    health = instances.health_info(port)
    if health is None:
        return "idle"
    return "working" if health.get("busy") else "running"

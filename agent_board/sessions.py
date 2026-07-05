"""Derived views over an agent-cli session's on-disk files (DESIGN §7).

agent-cli is not modified, so the board reads its session files directly — this
is the INTEGRATION CONTRACT (couples to agent-cli's on-disk format; bump
carefully across agent-cli versions):

- ``last_query``  ← ``history.jsonl`` last ``{role:user, kind:query}`` record.
- ``status``      ← ``web.json`` presence + pid alive + ``status.json`` sidecar
                    (agent-cli >= 4.27.0), falling back to ``/api/health``.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_board import instances


def _session_dir(workspace: Path, session_id: str) -> Path:
    return Path(workspace) / ".agent-cli" / "sessions" / session_id


def last_query_record(workspace: Path, session_id: str | None) -> dict | None:
    """The most recent user query as ``{"text", "ts"}`` (ISO timestamp), or None.

    Reads history.jsonl from the end so the last query is found without caring
    about everything before it. One read serves both text and timestamp."""
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
            return {"text": rec.get("text") or rec.get("content"), "ts": rec.get("ts")}
    return None


def last_query(workspace: Path, session_id: str | None) -> str | None:
    """The most recent user query text for this session, or None."""
    rec = last_query_record(workspace, session_id)
    return rec["text"] if rec else None


_IDLE = {"status": "idle", "awaiting_input": False, "viewers": 0}


def live_state(workspace: Path, session_id: str | None) -> dict:
    """``{"status", "awaiting_input", "viewers"}`` — read from the instance's
    ``status.json`` sidecar (a local file), falling back to ``GET /api/health``:

    - status: ``working`` (LLM responding) / ``running`` (up, idle) / ``idle`` (down),
    - awaiting_input: an ask/confirm prompt is waiting for a reply,
    - viewers: live browser subscribers on the instance (0 when down).

    agent-cli >= 4.27.0 writes ``status.json`` on every change, so the common
    path is a file read (no HTTP). Older instances have no file → fall back to
    ``/api/health`` (same ``{busy, awaiting_input, viewers}`` shape). Missing
    fields degrade gracefully."""
    if not session_id:
        return dict(_IDLE)
    info = instances.read_web_json(workspace, session_id)
    if not info:
        return dict(_IDLE)
    pid, port = info.get("pid"), info.get("port")
    if not (pid and instances.pid_alive(pid) and port):
        return dict(_IDLE)
    # Prefer the local status.json sidecar; fall back to an HTTP health poll for
    # pre-4.27.0 instances that don't write it.
    live = instances.read_status_json(workspace, session_id)
    if live is None:
        live = instances.health_info(port)
    if live is None:
        return dict(_IDLE)
    return {
        "status": "working" if live.get("busy") else "running",
        "awaiting_input": bool(live.get("awaiting_input")),
        "viewers": int(live.get("viewers") or 0),
    }


def status(workspace: Path, session_id: str | None) -> str:
    """The 3-state status string (see :func:`live_state`)."""
    return live_state(workspace, session_id)["status"]

"""Agent-side schedule file contract (docs/schedule-design.md §7).

The agent inside a workspace knows nothing about the board. To register or
delete schedules it appends JSONL requests to
``<workspace>/.agent-cli/schedule-requests.jsonl``; the board's live scanner
notices the mtime change, applies the new lines here, and atomically rewrites
``<workspace>/.agent-cli/schedule-state.json`` with the current schedule list +
per-``req_id`` results — which the agent's ``schedule`` tool reads back for its
ack. Processed lines are tracked by a line-count offset stored in the state
file, so a line is never applied twice (append-only requests, no truncation).

Request lines::

    {"op":"add","cron":"0 9 * * 1","prompt":"...","label":"주간 보고","req_id":"r1"}
    {"op":"delete","schedule_id":"...","req_id":"r2"}
    {"op":"list","req_id":"r3"}

Safety: agent-sourced schedules are capped per post; a delete may only target a
schedule of THIS post (the file lives in its workspace — natural isolation).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from agent_board import cron
from agent_board.models import DEFAULT_SCHEDULE_NICKNAME

REQUESTS_REL = Path(".agent-cli") / "schedule-requests.jsonl"
STATE_REL = Path(".agent-cli") / "schedule-state.json"

AGENT_CAP_PER_POST = 5  # 에이전트 등록분 상한 (post 당)


def requests_path(workspace: Path) -> Path:
    return Path(workspace) / REQUESTS_REL


def state_path(workspace: Path) -> Path:
    return Path(workspace) / STATE_REL


def _read_state(workspace: Path) -> dict:
    try:
        return json.loads(state_path(workspace).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_state_atomic(workspace: Path, state: dict) -> None:
    """Unique-tmp + os.replace — the agent may read concurrently (never a torn
    file), and unique tmp names avoid the fixed-tmp replace race (agent-cli
    v4.27.1 lesson)."""
    target = state_path(workspace)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".sched-state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _schedule_state_view(s) -> dict:
    view = {
        "schedule_id": s.schedule_id,
        "source": s.source,
        "cron": s.cron,
        "human": cron.describe(s.cron),
        "label": s.label,
        "nickname": s.nickname,
        "effective_nickname": s.nickname or DEFAULT_SCHEDULE_NICKNAME,
        "prompt": s.prompt,
        "enabled": s.enabled,
        "last_fired_at": s.last_fired_at,
        "missed_at": s.missed_at,
    }
    try:
        if s.enabled:
            view["next_fire"] = cron.next_fire(
                cron.parse(s.cron), datetime.now()
            ).isoformat()
    except ValueError:
        pass
    return view


def _apply_one(store, post_id: str, req: dict, *, agent_cap: int) -> dict:
    """One request line → its result dict (never raises)."""
    op = req.get("op")
    if op == "add":
        expr = (req.get("cron") or "").strip()
        prompt = (req.get("prompt") or "").strip()
        if not prompt:
            return {"error": "prompt is required"}
        try:
            cron.parse(expr)
        except ValueError as e:
            return {"error": f"invalid cron: {e}"}
        if store.count_agent_schedules(post_id) >= agent_cap:
            return {"error": f"agent schedule cap reached ({agent_cap} per post)"}
        s = store.add_schedule(
            post_id=post_id,
            source="agent",
            cron=expr,
            prompt=prompt,
            label=(req.get("label") or "").strip(),
            nickname=(req.get("nickname") or "").strip(),
        )
        return {"ok": True, "schedule_id": s.schedule_id}
    if op == "delete":
        sid = req.get("schedule_id") or ""
        s = store.get_schedule(sid)
        if s is None or s.post_id != post_id:
            # 남의 post 스케줄은 존재 여부조차 노출하지 않음 (자연 격리)
            return {"error": "no such schedule in this workspace"}
        store.delete_schedule(sid)
        return {"ok": True}
    if op == "list":
        return {"ok": True}  # state 자체가 목록을 실음
    return {"error": f"unknown op: {op!r}"}


def apply_requests(
    store, post_id: str, workspace: Path, *, agent_cap: int = AGENT_CAP_PER_POST
) -> bool:
    """Apply NEW request lines (past the consumed offset) and refresh the state
    file. Returns True when the schedule set changed (caller rearms the
    scheduler + pushes the post row)."""
    try:
        raw = requests_path(workspace).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False
    lines = raw.splitlines()
    state = _read_state(workspace)
    consumed = int(state.get("consumed") or 0)
    if consumed > len(lines):
        consumed = 0  # 요청 파일이 교체/축소됨 — 처음부터 (offset 신뢰 불가)
    new_lines = lines[consumed:]
    if not new_lines:
        return False

    results: dict[str, dict] = {}
    changed = False
    for i, line in enumerate(new_lines):
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            results[f"line{consumed + i + 1}"] = {"error": "invalid JSON"}
            continue
        res = _apply_one(store, post_id, req, agent_cap=agent_cap)
        if res.get("ok") and req.get("op") in ("add", "delete"):
            changed = True
        results[str(req.get("req_id") or f"line{consumed + i + 1}")] = res

    _write_state_atomic(
        workspace,
        {
            "consumed": len(lines),
            "results": results,
            "schedules": [
                _schedule_state_view(s) for s in store.list_schedules(post_id)
            ],
        },
    )
    return changed


def refresh_state(store, post_id: str, workspace: Path) -> None:
    """Rewrite the state file from the DB WITHOUT consuming requests — called
    after board-side mutations (UI add/delete/toggle) so the agent's next read
    sees the truth. Only writes when a state file (or request file) already
    exists, to avoid littering schedule-less workspaces."""
    if not state_path(workspace).exists() and not requests_path(workspace).exists():
        return
    state = _read_state(workspace)
    state["schedules"] = [
        _schedule_state_view(s) for s in store.list_schedules(post_id)
    ]
    state.setdefault("consumed", 0)
    state.setdefault("results", {})
    _write_state_atomic(workspace, state)

"""세션의 예약 파일을 **읽기만** 한다 (v1.34.0).

예약은 agent-cli 의 기능이다 — 세션 폴더의 ``schedules.json`` 을 그 세션을 연
agent-cli 프로세스가 소유하고 발화한다(agent-cli ≥ 10.12.0,
``docs/schedule/DESIGN.md``). 보드는 그리기만 한다: 목록 카드의 ⏰ 배지와 놓친
예약 표시, 그리고 켜진 예약이 있는 방을 다시 띄울지의 판단.

v1.33 까지는 보드가 스케줄러였다(DB ``schedules`` 테이블 + asyncio 루프 +
워크스페이스 요청·회신 파일 계약). 그 데이터는 기동 시 한 번
:func:`migrate_legacy` 가 세션 폴더로 옮긴다.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("agent_board.session_schedules")

_NAME = "schedules.json"


def path_for(workspace: Path, session_id: str) -> Path:
    return Path(workspace) / ".agent-cli" / "sessions" / session_id / _NAME


def read(workspace: Path, session_id: str | None) -> list[dict]:
    """The session's schedule rows, or ``[]`` (no session / no file / corrupt)."""
    if not session_id:
        return []
    try:
        raw = json.loads(path_for(workspace, session_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    rows = raw.get("schedules") if isinstance(raw, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def summary(workspace: Path, session_id: str | None) -> dict:
    """카드용 요약 — 예약 수와 답을 기다리는 놓친 예약."""
    rows = read(workspace, session_id)
    return {
        "count": len(rows),
        "missed": [
            {
                "id": r.get("id"),
                "label": r.get("label") or r.get("cron") or "",
                "missed_at": r.get("missed_at"),
            }
            for r in rows
            if r.get("missed_at")
        ],
    }


def has_enabled(workspace: Path, session_id: str | None) -> bool:
    """켜진 예약이 있는가 — 있으면 그 방의 프로세스는 떠 있어야 한다."""
    return any(r.get("enabled") for r in read(workspace, session_id))


# ── v1.33 → v1.34 일회성 이전 ─────────────────────────────────


def _local_naive(ts: str | None) -> str | None:
    """보드 DB 의 시각(UTC-aware 또는 naive) → agent-cli 가 쓰는 서버 로컬
    naive ISO (cron 의 의미가 로컬 벽시계다)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


def _to_session_row(row: dict) -> dict:
    created = _local_naive(row["created_at"]) or _local_naive(
        datetime.now(timezone.utc).isoformat()
    )
    fired = _local_naive(row["last_fired_at"])
    missed = _local_naive(row["missed_at"])
    # settled_at: "이 시각까지의 발화는 정리됐다" — 보드가 마지막으로 내린 판정
    # (발화 또는 놓침)까지를 그대로 넘긴다. 답을 기다리던 질문은 유지된다.
    settled = max(t for t in (created, fired, missed) if t)
    return {
        "id": row["schedule_id"],
        "source": row["source"],
        "cron": row["cron"],
        "prompt": row["prompt"],
        "label": row["label"] or "",
        "nickname": row["nickname"] or "",
        "enabled": bool(row["enabled"]),
        "created_at": created,
        "settled_at": settled,
        "last_fired_at": fired,
        "missed_at": missed,
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".schedules-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "schedules": rows}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def migrate_legacy(store, config, *, stop_instance=None) -> int:
    """보드 DB 의 예약을 각 방의 세션 폴더로 옮기고 테이블을 지운다. 옮긴 수 반환.

    테이블이 없으면(새 DB, 또는 이미 옮김) 아무것도 하지 않는다. 세션이 아직
    없는 방(한 번도 연 적 없음)의 예약은 옮길 곳이 없어 로그에 남기고 버린다.

    ``stop_instance(post)``: 그 방의 인스턴스가 떠 있으면 먼저 멈춘다 — 예약
    파일은 그 세션을 연 프로세스가 기동 때 읽어 메모리에 쥐므로, 떠 있는
    프로세스 밑에서 파일을 쓰면 반영되지 않고 다음 저장에 덮인다. 멈춘 방은
    켜진 예약이 있으면 보드가 곧 다시 띄운다.
    """
    legacy = store.drain_legacy_schedules()
    if not legacy:
        return 0
    by_post: dict[str, list[dict]] = {}
    for row in legacy:
        by_post.setdefault(row["post_id"], []).append(row)
    moved = 0
    for post_id, rows in by_post.items():
        post = store.get(post_id)
        if post is None or not post.session_id:
            log.warning(
                "dropping %d schedule(s) of post %s — it has no session to own them",
                len(rows),
                post_id,
            )
            continue
        ws = config.workspace_for(post_id)
        if stop_instance is not None:
            stop_instance(post)
        existing = read(ws, post.session_id)
        have = {r.get("id") for r in existing}
        merged = existing + [
            _to_session_row(r) for r in rows if r["schedule_id"] not in have
        ]
        _write(path_for(ws, post.session_id), merged)
        moved += len(merged) - len(existing)
    log.info("moved %d schedule(s) from the board DB into session folders", moved)
    return moved

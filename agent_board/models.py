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

# 예약 발화 시 주입 메시지의 귀속 닉네임 기본값 (스케줄에 미지정 시). 발화
# 시점에 `sched.nickname or DEFAULT_SCHEDULE_NICKNAME` 로 해석하므로 기본을
# 바꾸면 빈 항목에 소급 적용된다. ⏰ 접두 = 트랜스크립트/팀뷰에서 사람 닉네임과
# 시각적으로 구분.
DEFAULT_SCHEDULE_NICKNAME = "⏰ Scheduler"


@dataclass
class Post:
    post_id: str
    topic: str
    session_id: str | None = None  # filled on first open (agent-cli session)
    model_id: str | None = None  # agent-cli model id → `--model` (None = default)
    force_active: bool = False
    created_at: str = ""
    last_opened_at: str | None = None


@dataclass
class Schedule:
    """A per-post recurring prompt injection (docs/schedule-design.md §2).

    ``source`` records WHO registered it ('user' via the board UI, 'agent' via
    the workspace file contract) — shown as a badge, both deletable by the user.
    ``missed_at`` non-None means a fire was missed while the board (or machine)
    was down and is awaiting the user's run-now / dismiss decision — missed
    fires are NEVER auto-run (user decision)."""

    schedule_id: str
    post_id: str
    source: str  # 'user' | 'agent'
    cron: str
    prompt: str
    label: str = ""
    # 주입 메시지 귀속 닉네임 — 사용자/에이전트가 지정 가능, 빈 값이면 발화 시
    # DEFAULT_SCHEDULE_NICKNAME 으로 해석.
    nickname: str = ""
    enabled: bool = True
    created_at: str = ""
    last_fired_at: str | None = None
    missed_at: str | None = None

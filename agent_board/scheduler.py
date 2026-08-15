"""Per-post schedule engine (docs/schedule-design.md §4–§5).

Sleep-until-next + rearm — NOT a polling tick. The loop computes the earliest
next fire over enabled schedules and sleeps exactly until then, capped at
``max_sleep`` (300s) as the system-sleep/clock-jump safety net: ``asyncio``
timers freeze while the machine sleeps, so without the cap a wake-up after a
long nap would fire arbitrarily late. Mutations (API or the agent file
contract) call :meth:`rearm` to wake the loop immediately.

Missed fires are NEVER auto-run (user decision): anything older than
``miss_threshold`` when we settle is stamped ``missed_at`` and surfaces in the
UI as a run-now/dismiss question; several missed periods collapse into one.

Times are naive server-local datetimes end-to-end (cron semantics are local);
``last_fired_at``/``missed_at`` are written as local-naive ISO strings and only
this module interprets them.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from agent_board import cron
from agent_board.models import Schedule

log = logging.getLogger("agent_board.scheduler")

MISS_THRESHOLD_S = 120.0  # 이보다 오래 지난 발화 = 놓침 → 질문 (자동실행 금지)
MAX_SLEEP_S = 300.0  # 시스템 sleep/시계점프 안전망 (≤5분 내 재평가)
AGENT_CAP_PER_POST = 5  # 에이전트 등록분 캡 (파일 계약에서 검사)

SCHEDULE_NICKNAME = "⏰ schedule"  # 주입 귀속 (cli ≥8.9.0; 구 cli 는 '?' 로 표시)


def _parse_local(ts: str | None) -> datetime | None:
    """Stored timestamp → naive local datetime. UTC-aware strings (e.g. a
    post's ``created_at``) are converted; naive strings pass through."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


class Scheduler:
    """Owns the rearm event + the sleep loop. ``inject_fn(post, prompt)`` is a
    SYNC callable (run in an executor) that delivers the prompt to the post's
    running instance; ``on_change(post_id)`` notifies the SSE layer so the UI
    updates without a refresh. Both injected for tests."""

    def __init__(
        self,
        store,
        orchestrator,
        *,
        inject_fn,
        on_change=None,
        clock=None,
        miss_threshold: float = MISS_THRESHOLD_S,
        max_sleep: float = MAX_SLEEP_S,
    ):
        self._store = store
        self._orch = orchestrator
        self._inject = inject_fn
        self._on_change = on_change
        self._clock = clock or datetime.now
        self._miss_threshold = miss_threshold
        self._max_sleep = max_sleep
        self._rearm = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None  # run() 이 캡처

    # ── external surface ────────────────────────────────────
    def rearm(self) -> None:
        """Wake the sleep loop now — call after ANY schedule mutation (API
        add/delete/toggle, agent file contract application). Thread-safe: the
        live scanner applies the file contract on an executor thread, and
        ``asyncio.Event.set`` is NOT thread-safe — marshal via the loop."""
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                if loop != asyncio.get_running_loop():
                    loop.call_soon_threadsafe(self._rearm.set)
                    return
            except RuntimeError:  # no running loop in THIS thread → foreign
                loop.call_soon_threadsafe(self._rearm.set)
                return
        self._rearm.set()

    def _notify(self, post_id: str) -> None:
        if self._on_change:
            try:
                self._on_change(post_id)
            except Exception:  # UI 알림 실패가 스케줄링을 죽이면 안 됨
                log.exception("schedule on_change failed for %s", post_id)

    # ── core decisions (pure-ish, unit-tested) ──────────────
    def _anchor(self, s: Schedule) -> datetime | None:
        """The exactly-once baseline: last fire if any, else CREATION time —
        a schedule never owes fires from before it existed."""
        return _parse_local(s.last_fired_at) or _parse_local(s.created_at)

    def _due(self, s: Schedule, now: datetime):
        """None (nothing owed) | ('fire', prev) | ('missed', prev)."""
        if not s.enabled:
            return None
        try:
            spec = cron.parse(s.cron)
        except ValueError:
            return None  # corrupt row must not kill the loop
        prev = cron.prev_fire(spec, now)
        if prev is None:
            return None
        anchor = self._anchor(s)
        if anchor is not None and anchor >= prev:
            return None  # already fired (or predates creation) — exactly-once
        if (now - prev).total_seconds() > self._miss_threshold:
            return ("missed", prev)
        return ("fire", prev)

    def next_wake(self, now: datetime) -> datetime | None:
        """Earliest upcoming fire across enabled schedules (None = idle)."""
        best = None
        for s in self._store.list_schedules():
            if not s.enabled:
                continue
            try:
                nf = cron.next_fire(cron.parse(s.cron), now)
            except ValueError:
                continue
            if best is None or nf < best:
                best = nf
        return best

    # ── firing ──────────────────────────────────────────────
    async def fire(self, sched: Schedule) -> bool:
        """Ensure the instance is up (spawn-or-attach, ``--resume``) and inject
        the prompt. Success stamps ``last_fired_at`` (clearing missed); failure
        demotes to a missed question instead of retry-looping."""
        now_iso = self._clock().isoformat()
        try:
            await self._orch.open(sched.post_id)  # spawn-or-attach
            post = self._store.get(sched.post_id)  # re-read: first open set sid
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._inject, post, sched.prompt)
        except Exception:
            log.exception(
                "schedule fire failed (post=%s label=%r) — demoting to missed",
                sched.post_id,
                sched.label,
            )
            self._store.mark_missed(sched.schedule_id, now_iso)
            self._notify(sched.post_id)
            return False
        self._store.mark_fired(sched.schedule_id, now_iso)
        self._notify(sched.post_id)
        return True

    async def settle(self) -> None:
        """Resolve everything owed as of now: fire fresh dues, stamp stale ones
        as missed questions."""
        now = self._clock()
        for s in self._store.list_schedules():
            verdict = self._due(s, now)
            if verdict is None:
                continue
            kind, prev = verdict
            if kind == "missed":
                self._store.mark_missed(s.schedule_id, prev.isoformat())
                self._notify(s.post_id)
            else:
                await self.fire(s)

    # ── loop ────────────────────────────────────────────────
    async def run(self) -> None:
        """The sleep loop. Cancelled on app shutdown."""
        self._loop = asyncio.get_running_loop()  # rearm() thread-marshal 용
        while True:
            try:
                await self.settle()
            except Exception:
                log.exception("scheduler settle crashed — loop continues")
            now = self._clock()
            nxt = self.next_wake(now)
            timeout = self._max_sleep
            if nxt is not None:
                timeout = max(0.05, min((nxt - now).total_seconds(), self._max_sleep))
            try:
                await asyncio.wait_for(self._rearm.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            self._rearm.clear()

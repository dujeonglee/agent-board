"""Scheduler (docs/schedule-design.md §4–§5) — sleep-until-next + rearm.

핵심 회귀:
- exactly-once: 같은 발화가 두 번 실행되면 안 됨 (anchor 가드)
- 놓친 발화(> MISS_THRESHOLD)는 **자동실행 금지** — missed 질문으로만
- 새 스케줄은 생성 이전 발화를 빚지지 않음 (anchor=created_at)
- fire 실패는 missed 로 강등 (재시도 루프 금지)
- rearm 이 sleep 을 즉시 깨움
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from agent_board.scheduler import Scheduler
from agent_board.store import Store


class FakeOrch:
    def __init__(self, fail=False):
        self.opened: list[str] = []
        self.fail = fail

    async def open(self, post_id):
        if self.fail:
            raise RuntimeError("spawn failed")
        self.opened.append(post_id)
        return f"/s/{post_id}/"


class Clock:
    def __init__(self, now: datetime):
        self.t = now

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "board.db")
    yield s
    s.close()


def _mk(store, cron_expr, *, created: datetime, enabled=True, source="user"):
    """스케줄 생성 + created_at 을 테스트 제어 시각(local naive)으로 고정."""
    p = store.create_post(topic="t")
    s = store.add_schedule(
        post_id=p.post_id, source=source, cron=cron_expr, prompt="do it", label="L"
    )
    store._conn.execute(
        "UPDATE schedules SET created_at = ? WHERE schedule_id = ?",
        (created.isoformat(), s.schedule_id),
    )
    store._conn.commit()
    if not enabled:
        store.set_schedule_enabled(s.schedule_id, False)
    return store.get_schedule(s.schedule_id)


def _sched(store, clock, *, orch=None, inject=None, notes=None):
    injected: list[tuple] = []

    def _inject(post, prompt, nickname):
        injected.append((post.post_id, prompt, nickname))

    sch = Scheduler(
        store,
        orch or FakeOrch(),
        inject_fn=inject or _inject,
        on_change=(notes.append if notes is not None else None),
        clock=clock,
    )
    return sch, injected


BASE = datetime(2026, 8, 17, 8, 59)  # 월요일 08:59


class TestSettle:
    @pytest.mark.asyncio
    async def test_on_time_fire(self, store):
        clock = Clock(BASE)
        s = _mk(store, "0 9 * * 1", created=BASE)  # 매주 월 9시
        sch, injected = _sched(store, clock)
        clock.t = datetime(2026, 8, 17, 9, 0, 30)  # 발화 30초 뒤 기상
        await sch.settle()
        # 닉네임 미지정 → 기본값으로 귀속
        assert injected == [(s.post_id, "do it", "⏰ Scheduler")]
        got = store.get_schedule(s.schedule_id)
        assert got.last_fired_at is not None and got.missed_at is None

    @pytest.mark.asyncio
    async def test_custom_nickname_used(self, store):
        clock = Clock(BASE)
        p = store.create_post(topic="t")
        s = store.add_schedule(
            post_id=p.post_id,
            source="user",
            cron="0 9 * * 1",
            prompt="do it",
            nickname="주간봇",
        )
        store._conn.execute(
            "UPDATE schedules SET created_at = ? WHERE schedule_id = ?",
            (BASE.isoformat(), s.schedule_id),
        )
        store._conn.commit()
        sch, injected = _sched(store, clock)
        clock.t = datetime(2026, 8, 17, 9, 0, 30)
        await sch.settle()
        assert injected == [(p.post_id, "do it", "주간봇")]

    @pytest.mark.asyncio
    async def test_exactly_once(self, store):
        # ★뮤테이션 대상: _due 의 anchor >= prev 가드 제거 시 두 번째 settle 이 재발화
        clock = Clock(BASE)
        _mk(store, "0 9 * * 1", created=BASE)
        sch, injected = _sched(store, clock)
        clock.t = datetime(2026, 8, 17, 9, 0, 30)
        await sch.settle()
        await sch.settle()  # 같은 시각 재정산 — 재발화 금지
        clock.t = datetime(2026, 8, 17, 9, 1, 30)
        await sch.settle()  # 1분 뒤에도 (다음 주 전까진) 재발화 금지
        assert len(injected) == 1

    @pytest.mark.asyncio
    async def test_stale_due_becomes_missed_not_autorun(self, store):
        # ★핵심 회귀 (사용자 결정): 오래 지난 발화는 자동실행 없이 질문
        clock = Clock(BASE)
        s = _mk(store, "0 9 * * 1", created=BASE)
        sch, injected = _sched(store, clock)
        clock.t = datetime(2026, 8, 17, 11, 0)  # 2시간 지남 (> 120s)
        await sch.settle()
        assert injected == []  # 자동실행 안 함
        got = store.get_schedule(s.schedule_id)
        assert got.missed_at == "2026-08-17T09:00:00"
        assert got.last_fired_at is None

    @pytest.mark.asyncio
    async def test_new_schedule_owes_nothing_from_before_creation(self, store):
        # 화요일에 만든 "매주 월 9시" — 지난 월요일 발화를 빚지지 않음
        created = datetime(2026, 8, 18, 10, 0)  # 화
        clock = Clock(created)
        s = _mk(store, "0 9 * * 1", created=created)
        sch, injected = _sched(store, clock)
        await sch.settle()
        assert injected == []
        assert store.get_schedule(s.schedule_id).missed_at is None

    @pytest.mark.asyncio
    async def test_disabled_ignored(self, store):
        clock = Clock(BASE)
        _mk(store, "0 9 * * 1", created=BASE, enabled=False)
        sch, injected = _sched(store, clock)
        clock.t = datetime(2026, 8, 17, 9, 0, 30)
        await sch.settle()
        assert injected == []

    @pytest.mark.asyncio
    async def test_corrupt_cron_does_not_crash(self, store):
        clock = Clock(BASE)
        s = _mk(store, "0 9 * * 1", created=BASE)
        store._conn.execute(
            "UPDATE schedules SET cron = 'garbage' WHERE schedule_id = ?",
            (s.schedule_id,),
        )
        store._conn.commit()
        sch, injected = _sched(store, clock)
        clock.t = datetime(2026, 8, 17, 9, 0, 30)
        await sch.settle()  # no raise
        assert injected == []

    @pytest.mark.asyncio
    async def test_fire_failure_demotes_to_missed(self, store):
        clock = Clock(BASE)
        s = _mk(store, "0 9 * * 1", created=BASE)
        sch, _injected = _sched(store, clock, orch=FakeOrch(fail=True))
        clock.t = datetime(2026, 8, 17, 9, 0, 30)
        await sch.settle()
        got = store.get_schedule(s.schedule_id)
        assert got.missed_at is not None and got.last_fired_at is None

    @pytest.mark.asyncio
    async def test_on_change_notified(self, store):
        clock = Clock(BASE)
        s = _mk(store, "0 9 * * 1", created=BASE)
        notes: list[str] = []
        sch, _ = _sched(store, clock, notes=notes)
        clock.t = datetime(2026, 8, 17, 9, 0, 30)
        await sch.settle()
        assert notes == [s.post_id]


class TestNextWake:
    def test_min_across_enabled(self, store):
        clock = Clock(BASE)
        _mk(store, "0 12 * * *", created=BASE)  # 오늘 12시
        _mk(store, "30 9 * * *", created=BASE)  # 오늘 9시30분 ← 더 이름
        _mk(store, "0 9 * * *", created=BASE, enabled=False)  # off → 제외
        sch, _ = _sched(store, clock)
        assert sch.next_wake(BASE) == datetime(2026, 8, 17, 9, 30)

    def test_none_when_no_schedules(self, store):
        sch, _ = _sched(store, Clock(BASE))
        assert sch.next_wake(BASE) is None


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_rearm_wakes_sleep_immediately(self, store):
        # max_sleep 를 크게 두고 rearm 만으로 깨어나 새 스케줄을 잡는지
        clock = Clock(datetime(2026, 8, 17, 9, 0, 30))
        injected: list[tuple] = []

        def _inject(post, prompt, nickname):
            injected.append((post.post_id, prompt))

        sch = Scheduler(
            store, FakeOrch(), inject_fn=_inject, clock=clock, max_sleep=60.0
        )
        task = asyncio.create_task(sch.run())
        try:
            await asyncio.sleep(0.05)  # 루프가 빈 스케줄로 잠들 때까지
            _mk(store, "0 9 * * 1", created=datetime(2026, 8, 17, 8, 59))
            sch.rearm()  # ← API 변이가 하는 일
            for _ in range(100):
                if injected:
                    break
                await asyncio.sleep(0.02)
            assert injected, "rearm 이 sleep 을 깨워 발화해야 함"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_settle_crash_does_not_kill_loop(self, store):
        clock = Clock(BASE)
        sch = Scheduler(
            store,
            FakeOrch(),
            inject_fn=lambda p, x, n: None,
            clock=clock,
            max_sleep=0.02,
        )
        calls = {"n": 0}

        async def boom():
            calls["n"] += 1
            raise RuntimeError("boom")

        sch.settle = boom  # type: ignore[method-assign]
        task = asyncio.create_task(sch.run())
        try:
            await asyncio.sleep(0.15)
            assert calls["n"] >= 2  # 크래시 후에도 루프 지속
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

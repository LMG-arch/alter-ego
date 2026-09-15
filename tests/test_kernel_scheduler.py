"""`kernel/scheduler.py` 的单元测试。

调度器最难测的部分是「时间」——所以这里几乎全部用 `FrozenClock` 手动推进，
一次真实 `sleep` 都没有。用例之间不共享状态：每个用例自己造时钟和调度器。
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, time, timedelta, tzinfo
from typing import Any

import pytest

from alterego.kernel.clock import FrozenClock, VirtualClock, resolve_timezone
from alterego.kernel.scheduler import JobHandle, Scheduler


TZ: tzinfo = resolve_timezone("Asia/Shanghai")
T0: datetime = datetime(2026, 9, 15, 9, 0, tzinfo=TZ)
LOGGER = logging.getLogger("test.scheduler")


class Recorder:
    """记录每次被调用的次数（和时钟读数），可选地抛异常。"""

    def __init__(
        self,
        clock: FrozenClock | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.calls = 0
        self.times: list[datetime] = []
        self._clock = clock
        self._error = error

    async def __call__(self) -> None:
        self.calls += 1
        if self._clock is not None:
            self.times.append(self._clock.virtual_now())
        if self._error is not None:
            raise self._error


def make_scheduler(
    start: datetime = T0, *, seed: int = 42, **kwargs: Any
) -> tuple[Scheduler, FrozenClock]:
    clock = FrozenClock(start)
    scheduler = Scheduler(clock, rng=random.Random(seed), logger=LOGGER, **kwargs)
    return scheduler, clock


async def step_to(
    scheduler: Scheduler,
    clock: FrozenClock,
    end: datetime,
    *,
    by: timedelta = timedelta(minutes=1),
) -> None:
    """以固定步长推进时钟并每次跑一遍调度器。"""
    while clock.virtual_now() < end:
        clock.advance(by)
        await scheduler.run_due()


# ── every ──────────────────────────────────────────────────


async def test_every_first_run_is_one_interval_later() -> None:
    scheduler, clock = make_scheduler()
    job = Recorder()
    scheduler.every(timedelta(minutes=5), job, name="tick")
    # 注册后不应立刻执行——「注册即触发」会让启动瞬间跑一堆任务
    await scheduler.run_due()
    assert job.calls == 0
    clock.advance(timedelta(minutes=5))
    await scheduler.run_due()
    assert job.calls == 1


async def test_every_repeats_on_its_own() -> None:
    scheduler, clock = make_scheduler()
    job = Recorder()
    scheduler.every(timedelta(minutes=5), job, name="tick")
    await step_to(scheduler, clock, T0 + timedelta(minutes=25))
    assert job.calls == 5


async def test_every_rejects_non_positive_interval() -> None:
    scheduler, _ = make_scheduler()
    for bad in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError, match="interval"):
            scheduler.every(bad, Recorder(), name="bad")


async def test_every_rejects_negative_jitter() -> None:
    scheduler, _ = make_scheduler()
    with pytest.raises(ValueError, match="jitter"):
        scheduler.every(timedelta(minutes=1), Recorder(), name="bad", jitter=timedelta(seconds=-1))


async def test_missed_intervals_skip_forward_instead_of_bursting() -> None:
    """进程睡了一夜醒来，不该连发 288 个 tick。"""
    scheduler, clock = make_scheduler()
    job = Recorder()
    scheduler.every(timedelta(minutes=5), job, name="tick")
    clock.advance(timedelta(hours=1))
    await scheduler.run_due()
    assert job.calls == 1
    due = scheduler.next_due()
    assert due is not None
    assert due > clock.virtual_now()


# ── 抖动 ────────────────────────────────────────────────────


async def test_jitter_stays_within_its_bound() -> None:
    scheduler, _ = make_scheduler()
    scheduler.every(timedelta(minutes=10), Recorder(), name="jittery", jitter=timedelta(minutes=2))
    nominal = T0 + timedelta(minutes=10)
    due = scheduler.next_due()
    assert due is not None
    assert nominal <= due <= nominal + timedelta(minutes=2)


async def test_jitter_does_not_accumulate() -> None:
    """抖动围绕名义节奏上下浮动，不是往后滚的雪球。"""
    interval = timedelta(minutes=10)
    jitter = timedelta(minutes=2)
    scheduler, clock = make_scheduler()
    job = Recorder(clock)
    scheduler.every(interval, job, name="jittery", jitter=jitter)
    # 跑到 55 分钟：第 5 次最晚在 52 分钟触发，第 6 次最早也要到 60 分钟
    await step_to(scheduler, clock, T0 + timedelta(minutes=55))

    assert job.calls == 5
    for index, fired_at in enumerate(job.times, start=1):
        nominal = T0 + interval * index
        # 步长 1 分钟，所以上限还要加一格
        assert nominal <= fired_at <= nominal + jitter + timedelta(minutes=1)


async def test_jitter_is_reproducible_with_the_same_seed() -> None:
    first, _ = make_scheduler(seed=7)
    second, _ = make_scheduler(seed=7)
    for scheduler in (first, second):
        scheduler.every(timedelta(minutes=10), Recorder(), name="j", jitter=timedelta(minutes=3))
    assert first.next_due() == second.next_due()


async def test_zero_jitter_is_exactly_on_time() -> None:
    scheduler, _ = make_scheduler()
    scheduler.every(timedelta(minutes=10), Recorder(), name="punctual")
    assert scheduler.next_due() == T0 + timedelta(minutes=10)


# ── at ──────────────────────────────────────────────────────


async def test_at_runs_once_then_disappears() -> None:
    scheduler, clock = make_scheduler()
    job = Recorder()
    scheduler.at(T0 + timedelta(hours=2), job, name="once")
    await step_to(scheduler, clock, T0 + timedelta(hours=3))
    assert job.calls == 1
    assert scheduler.job_names() == ()
    assert scheduler.next_due() is None


async def test_at_rejects_naive_datetime() -> None:
    scheduler, _ = make_scheduler()
    with pytest.raises(ValueError, match="时区"):
        scheduler.at(datetime(2026, 9, 16, 12, 0), Recorder(), name="bad")


async def test_at_rejects_a_moment_in_the_past() -> None:
    scheduler, _ = make_scheduler()
    with pytest.raises(ValueError, match="过去"):
        scheduler.at(T0 - timedelta(seconds=1), Recorder(), name="bad")


# ── at_time_of_day ──────────────────────────────────────────


async def test_at_time_of_day_starts_tomorrow_when_already_passed() -> None:
    scheduler, _ = make_scheduler()  # 09:00
    scheduler.at_time_of_day(time(8, 0), Recorder(), name="morning")
    assert scheduler.next_due() == datetime(2026, 9, 16, 8, 0, tzinfo=TZ)


async def test_at_time_of_day_starts_today_when_still_ahead() -> None:
    scheduler, _ = make_scheduler()  # 09:00
    scheduler.at_time_of_day(time(22, 30), Recorder(), name="night")
    assert scheduler.next_due() == datetime(2026, 9, 15, 22, 30, tzinfo=TZ)


async def test_at_time_of_day_honours_the_timezone() -> None:
    scheduler, _ = make_scheduler()  # 09:00 +08:00 == 01:00 UTC
    scheduler.at_time_of_day(time(8, 0), Recorder(), name="utc_morning", tz="UTC")
    assert scheduler.next_due() == datetime(2026, 9, 15, 8, 0, tzinfo=resolve_timezone("UTC"))


async def test_at_time_of_day_repeats_daily() -> None:
    scheduler, clock = make_scheduler()
    job = Recorder()
    scheduler.at_time_of_day(time(10, 0), job, name="daily")
    await step_to(scheduler, clock, T0 + timedelta(days=3))
    assert job.calls == 3


# ── 取消与查询 ──────────────────────────────────────────────


async def test_cancel_stops_future_runs_and_is_idempotent() -> None:
    scheduler, clock = make_scheduler()
    job = Recorder()
    handle = scheduler.every(timedelta(minutes=5), job, name="tick")
    scheduler.cancel(handle)
    scheduler.cancel(handle)
    await step_to(scheduler, clock, T0 + timedelta(minutes=30))
    assert job.calls == 0
    assert scheduler.job_names() == ()


async def test_cancel_rejects_unknown_handle_quietly() -> None:
    scheduler, _ = make_scheduler()
    scheduler.cancel(JobHandle(job_id="nope", name="ghost"))


async def test_empty_job_name_is_rejected() -> None:
    scheduler, _ = make_scheduler()
    with pytest.raises(ValueError, match="任务名"):
        scheduler.every(timedelta(minutes=1), Recorder(), name="")


async def test_run_due_without_jobs_returns_none() -> None:
    scheduler, _ = make_scheduler()
    assert await scheduler.run_due() is None


def test_clock_is_exposed() -> None:
    scheduler, clock = make_scheduler()
    assert scheduler.clock is clock
    assert scheduler.stopped is False


def test_invalid_limits_are_rejected() -> None:
    _, clock = make_scheduler()
    with pytest.raises(ValueError, match="max_sleep_seconds"):
        Scheduler(clock, max_sleep_seconds=0)
    with pytest.raises(ValueError, match="max_failures"):
        Scheduler(clock, max_failures=0)


# ── 失败隔离 ────────────────────────────────────────────────


async def test_a_failing_job_does_not_stop_the_others() -> None:
    scheduler, clock = make_scheduler()
    good = Recorder()
    bad = scheduler.every(timedelta(minutes=5), Recorder(error=RuntimeError("boom")), name="bad")
    scheduler.every(timedelta(minutes=5), good, name="good")
    clock.advance(timedelta(minutes=5))
    await scheduler.run_due()
    assert good.calls == 1
    assert scheduler.failures(bad) == 1


async def test_a_failing_job_stays_scheduled() -> None:
    """失败一次就再也不跑，比失败本身更糟——那叫静默死亡。"""
    scheduler, clock = make_scheduler()
    handle = scheduler.every(timedelta(minutes=5), Recorder(error=RuntimeError("boom")), name="bad")
    clock.advance(timedelta(minutes=5))
    await scheduler.run_due()
    assert scheduler.next_due() is not None
    clock.advance(timedelta(minutes=5))
    await scheduler.run_due()
    assert scheduler.failures(handle) == 2


async def test_a_job_is_dropped_after_max_failures() -> None:
    scheduler, clock = make_scheduler(max_failures=2)
    handle = scheduler.every(timedelta(minutes=5), Recorder(error=RuntimeError("boom")), name="bad")
    for _ in range(3):
        clock.advance(timedelta(minutes=5))
        await scheduler.run_due()
    assert scheduler.job_names() == ()
    assert scheduler.dropped_jobs() == ("bad",)
    assert scheduler.failures(handle) == 2


async def test_a_sync_job_is_reported_as_a_failure() -> None:
    """很容易犯的错：写了个普通函数而不是 async def。"""

    def not_async() -> None:
        pass

    scheduler, clock = make_scheduler()
    handle = scheduler.every(timedelta(minutes=5), not_async, name="oops")  # type: ignore[arg-type]
    clock.advance(timedelta(minutes=5))
    await scheduler.run_due()
    assert scheduler.failures(handle) == 1


async def test_cancelled_error_is_not_swallowed() -> None:
    """关停时不吞 CancelledError，否则进程关不掉。"""
    scheduler, clock = make_scheduler()
    scheduler.every(timedelta(minutes=5), Recorder(error=asyncio.CancelledError()), name="cancel")
    clock.advance(timedelta(minutes=5))
    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_due()


# ── run_forever ─────────────────────────────────────────────


async def test_run_forever_returns_immediately_when_already_stopped() -> None:
    scheduler, _ = make_scheduler()
    scheduler.stop()
    await asyncio.wait_for(scheduler.run_forever(), timeout=1.0)


async def test_run_forever_fires_jobs_on_a_virtual_clock() -> None:
    """真实循环：虚拟时钟 3600 倍速，30 虚拟分钟 ≈ 0.5 真实秒。"""
    clock = VirtualClock(T0, speed=3600.0)
    scheduler = Scheduler(clock, rng=random.Random(1), logger=LOGGER, max_sleep_seconds=0.05)
    calls = 0

    async def job() -> None:
        nonlocal calls
        calls += 1
        if calls >= 3:
            scheduler.stop()

    scheduler.every(timedelta(minutes=30), job, name="tick")
    await asyncio.wait_for(scheduler.run_forever(), timeout=10.0)
    assert calls == 3
    assert scheduler.stopped is True

"""调度器。

内核需要「让它按节奏做某事」的能力，但**不需要**知道这些事是什么。
因此 :class:`Scheduler` 只认识三件事：间隔、时刻、每天几点。

两条刻意的约束：

1. **时间一律来自 :class:`~alterego.kernel.clock.Clock`**，不用
   ``asyncio.sleep(5)`` 这种真实时间。虚拟时钟推进 3 小时时，
   ``every(timedelta(minutes=5))`` 的任务必须真的被触发 36 次——
   否则 ``fast`` / ``turbo`` 模式下整个推演就是假的。
2. **抖动会用 ``rng`` 而不是全局 ``random``**。固定种子 = 可复现推演（P6）。
   人不是准点机器，所以抖动是特性而不是缺陷：``every(5min, jitter=90s)``
   会让任务落在 5:00–6:30 之间的任意位置。

依据: docs/design/01-architecture.md § 2.6
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo

from alterego.kernel.clock import Clock, resolve_timezone


__all__ = ["Job", "JobHandle", "Scheduler"]


#: 单次 ``asyncio.sleep`` 的真实时间上限。
#:
#: 没有它，``every(6h)`` 会让 :meth:`Scheduler.stop` 最多等 6 小时才生效——
#: 关不掉的进程就是僵尸进程。分段睡不增加 CPU 负担（每秒醒一次）。
_DEFAULT_MAX_SLEEP_SECONDS: float = 1.0

#: 单个任务连续失败多少次后不再重试。
#:
#: 一个每分钟都抛异常的任务会在日志里刷屏并吃掉事件循环，
#: 与其让它烂在那儿，不如停掉并明确告诉用户。
_DEFAULT_MAX_FAILURES: int = 5

#: 时钟连续多少轮不前进就判定「这个时钟驱动不了 ``run_forever``」。
#:
#: ``FrozenClock`` 的 ``sleep_until`` 立即返回而不推进时间，于是循环会
#: 变成每秒几十万次的空转——进程看起来卡死，CPU 却是满的。
_MAX_IDLE_ITERATIONS: int = 200

Job = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class JobHandle:
    """任务的唯一句柄，交给 :meth:`Scheduler.cancel`。"""

    job_id: str
    name: str

    def __str__(self) -> str:
        return f"{self.name}#{self.job_id}"


@dataclass(slots=True)
class _Entry:
    handle: JobHandle
    job: Job
    #: 名义节奏点。``every`` 用它累加，因此抖动不会累积偏移。
    base_due: datetime
    #: 实际触发点 = ``base_due`` + 抖动。``at`` 任务两者相同。
    due: datetime
    interval: timedelta | None = None
    at_time: time | None = None
    tz: tzinfo | None = None
    jitter_seconds: float = 0.0
    failures: int = 0
    runs: int = 0
    dropped: bool = False


class Scheduler:
    """基于 :class:`Clock` 的任务调度器。

    所有方法都必须在事件循环所在的线程里调用（非线程安全）。
    """

    def __init__(
        self,
        clock: Clock,
        /,
        *,
        rng: random.Random | None = None,
        logger: logging.Logger | None = None,
        max_sleep_seconds: float = _DEFAULT_MAX_SLEEP_SECONDS,
        max_failures: int = _DEFAULT_MAX_FAILURES,
    ) -> None:
        self._clock = clock
        #: 不传就自己造一个。生产代码应该传 ``random.Random(config.core.random_seed)``，
        #: 否则每次启动的抖动都不一样，黄金测试无法复现。
        self._rng = rng if rng is not None else random.Random()
        self._logger = logger or logging.getLogger("alterego.kernel.scheduler")
        if max_sleep_seconds <= 0:
            raise ValueError("max_sleep_seconds 必须为正数")
        if max_failures <= 0:
            raise ValueError("max_failures 必须为正数")
        self._max_sleep_seconds = max_sleep_seconds
        self._max_failures = max_failures
        self._entries: dict[str, _Entry] = {}
        #: 连续失败被停用的任务。留着只为了让 ``failures()`` 能回答
        #: 「它为什么不见了」——直接删掉的话用户只能看到任务凭空消失。
        self._dropped: dict[str, _Entry] = {}
        self._stopped = False

    # ── 注册 ────────────────────────────────────────────────

    def every(
        self,
        interval: timedelta,
        job: Job,
        *,
        name: str,
        jitter: timedelta = timedelta(0),
    ) -> JobHandle:
        """每 ``interval`` 跑一次。**首次触发在 ``interval`` 之后**，不是立刻。

        Args:
            interval: 周期间隔，必须为正。
            job: 无参异步可调用对象。
            name: 任务名，只用于日志。
            jitter: 抖动上界。每次触发点 = 名义时刻 + ``uniform(0, jitter)``。
        """
        if interval <= timedelta(0):
            raise ValueError("interval 必须为正")
        if jitter < timedelta(0):
            raise ValueError("jitter 不能为负")
        now = self._clock.virtual_now()
        return self._add(
            _Entry(
                handle=self._new_handle(name),
                job=job,
                base_due=now + interval,
                due=now + interval,
                interval=interval,
                jitter_seconds=jitter.total_seconds(),
            )
        )

    def at(self, when: datetime, job: Job, *, name: str) -> JobHandle:
        """在指定的绝对时刻跑一次。时刻必须带时区，且不能是过去。"""
        if when.tzinfo is None:
            raise ValueError("when 必须带时区（不要用 naive datetime）")
        if when <= self._clock.virtual_now():
            raise ValueError(
                f"when 已经是过去（{when.isoformat()}）；"
                "若想立刻执行请直接 await 协程，不要注册任务"
            )
        return self._add(_Entry(handle=self._new_handle(name), job=job, base_due=when, due=when))

    def at_time_of_day(
        self,
        t: time,
        job: Job,
        *,
        name: str,
        tz: str = "Asia/Shanghai",
    ) -> JobHandle:
        """每天 ``tz`` 时区的 ``t`` 跑一次（今天已过则从明天开始）。"""
        zone = resolve_timezone(tz)
        when = _next_occurrence(self._clock.virtual_now(), t, zone)
        return self._add(
            _Entry(
                handle=self._new_handle(name),
                job=job,
                base_due=when,
                due=when,
                at_time=t,
                tz=zone,
            )
        )

    def cancel(self, handle: JobHandle) -> None:
        """取消任务。对不存在或已取消的句柄是幂等的。"""
        self._entries.pop(handle.job_id, None)

    # ── 运行 ────────────────────────────────────────────────

    async def run_due(self) -> datetime | None:
        """跑掉所有到点任务，然后返回下一个触发时刻。

        这个方法把「什么时候该跑」和「等待」拆开了——于是测试可以用
        :class:`~alterego.kernel.clock.FrozenClock` 完全确定性地驱动调度器，
        不必依赖真实时间。:meth:`run_forever` 只是它加一层等待。

        任务**逐个串行**执行：一次 tick 不该和下一次的 tick 重叠，
        否则两个 tick 会同时改同一份状态。
        """
        now = self._clock.virtual_now()
        for entry in self._due_sorted(now):
            await self._fire(entry)
        return self.next_due()

    def next_due(self) -> datetime | None:
        """最近的触发时刻，没有任务时为 ``None``。"""
        if not self._entries:
            return None
        return min(entry.due for entry in self._entries.values())

    async def run_forever(self) -> None:
        """跑到 :meth:`stop` 被调用为止。

        要求时钟**真的会走**：``RealClock`` 和 ``VirtualClock`` 都可以；
        ``FrozenClock`` 不行——它的 ``sleep_until`` 立即返回而不推进时间，
        循环会变成忙等。真遇上这种时钟时会记一条 error 后退出，
        而不是把 CPU 烧到 100%（见 ``_MAX_IDLE_ITERATIONS``）。
        测试请用 :meth:`run_due` 手动驱动，或像 ``test_kernel_scheduler.py``
        那样用高倍速 ``VirtualClock``。
        """
        last = self._clock.virtual_now()
        idle = 0
        while not self._stopped:
            due = await self.run_due()
            now = self._clock.virtual_now()
            if now == last:
                idle += 1
                if idle >= _MAX_IDLE_ITERATIONS:
                    self._logger.error(
                        "时钟连续 %d 轮没有前进，停止调度循环——%s 不能驱动 run_forever",
                        idle,
                        type(self._clock).__name__,
                    )
                    return
            else:
                idle = 0
                last = now
            if due is None:
                await self._sleep_seconds(self._max_sleep_seconds)
            else:
                await self._sleep_until(due)

    def stop(self) -> None:
        """请求 :meth:`run_forever` 退出。最多等 ``max_sleep_seconds`` 秒生效。"""
        self._stopped = True

    # ── 查询 ────────────────────────────────────────────────

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def stopped(self) -> bool:
        return self._stopped

    def job_names(self) -> tuple[str, ...]:
        return tuple(entry.handle.name for entry in self._entries.values())

    def failures(self, handle: JobHandle) -> int:
        entry = self._entries.get(handle.job_id) or self._dropped.get(handle.job_id)
        return entry.failures if entry is not None else 0

    def dropped_jobs(self) -> tuple[str, ...]:
        """因为连续失败而被停用的任务名。"""
        return tuple(entry.handle.name for entry in self._dropped.values())

    # ── 内部 ────────────────────────────────────────────────

    def _new_handle(self, name: str) -> JobHandle:
        if not name:
            raise ValueError("任务名不能为空——日志里全是匿名任务等于没有日志")
        return JobHandle(job_id=uuid.uuid4().hex[:12], name=name)

    def _add(self, entry: _Entry) -> JobHandle:
        self._entries[entry.handle.job_id] = entry
        self._logger.debug("注册任务 %s，下次触发 %s", entry.handle, entry.due.isoformat())
        return entry.handle

    def _due_sorted(self, now: datetime) -> list[_Entry]:
        due = [entry for entry in self._entries.values() if entry.due <= now]
        # 同一时刻到点的任务按注册顺序跑，保证可复现
        due.sort(key=lambda entry: entry.due)
        return due

    async def _fire(self, entry: _Entry) -> None:
        entry.runs += 1
        try:
            result = entry.job()
            if not isinstance(result, Awaitable):
                raise TypeError("任务必须是 async def（返回 awaitable）")
            await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 异常隔离：一个任务炸了不影响同一批里的其他任务，更不该杀掉调度循环
            self._record_failure(entry, exc)
        self._reschedule(entry)

    def _record_failure(self, entry: _Entry, exc: BaseException) -> None:
        entry.failures += 1
        self._logger.warning(
            "任务 %s 第 %d 次失败：%s: %s",
            entry.handle,
            entry.failures,
            type(exc).__name__,
            exc,
        )
        if entry.failures >= self._max_failures:
            entry.dropped = True
            self._entries.pop(entry.handle.job_id, None)
            self._dropped[entry.handle.job_id] = entry
            self._logger.error(
                "任务 %s 连续失败 %d 次，已停用（阈值见 [plugins] circuit_breaker_threshold）",
                entry.handle,
                entry.failures,
            )

    def _reschedule(self, entry: _Entry) -> None:
        if entry.dropped:
            return
        now = self._clock.virtual_now()
        if entry.interval is None:
            if entry.at_time is not None and entry.tz is not None:
                entry.base_due = _next_occurrence(now, entry.at_time, entry.tz)
                entry.due = entry.base_due
                return
            # 一次性任务
            self._entries.pop(entry.handle.job_id, None)
            return
        # 落后太多时直接跳到下一个未来的点，而不是把欠下的次数一次性补跑——
        # 进程睡了一夜醒来后连发 288 个 tick 不是任何人想要的行为。
        base = entry.base_due + entry.interval
        while base <= now:
            base += entry.interval
        entry.base_due = base
        entry.due = base + self._jitter(entry)

    def _jitter(self, entry: _Entry) -> timedelta:
        if entry.jitter_seconds <= 0:
            return timedelta(0)
        return timedelta(seconds=self._rng.uniform(0.0, entry.jitter_seconds))

    async def _sleep_until(self, when: datetime) -> None:
        while not self._stopped:
            if self._clock.virtual_now() >= when:
                return
            try:
                # 分段等待：既尊重虚拟时钟的倍速，又保证 stop() 能及时生效
                await asyncio.wait_for(
                    self._clock.sleep_until(when), timeout=self._max_sleep_seconds
                )
            except TimeoutError:
                continue
            else:
                return

    async def _sleep_seconds(self, seconds: float) -> None:
        end = self._clock.virtual_now() + timedelta(seconds=seconds)
        await self._sleep_until(end)


def _next_occurrence(now: datetime, t: time, tz: tzinfo) -> datetime:
    """``tz`` 时区里今天（或明天）的 ``t``。

    夏令时切换的那一刻不做特殊处理：这个项目里所有时段都是「人起床了没」
    这种粗糙判断，差一小时不影响正确性，但多写 30 行 DST 补丁会。
    """
    local_date = now.astimezone(tz).date()
    candidate = datetime.combine(local_date, t, tzinfo=tz)
    if candidate <= now:
        candidate = datetime.combine(local_date + timedelta(days=1), t, tzinfo=tz)
    return candidate

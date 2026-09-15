"""可复现性黄金测试。

设计原则 P6（可复现）说的是：**同一个种子 + 同一份配置 = 一模一样的行为**。
没有这条，用户在 issue 里贴一段日志、我们却在本地复现不出来，
所有「它为什么突然不理我了」就永远查不下去。

这里测的不是某个函数的返回值，而是**一致性**：把同一件事跑两遍
（包括在独立的解释器进程里各跑一遍），输出必须逐字节相同。
任何一个从 `set` 里迭代出来的顺序、任何一个没播种的 `random`、
任何一个绕过配置直接读环境变量的隐藏分支，都会在这里现形。

为什么单独放一个目录：这些测试依赖「当前代码的默认值」，比普通单元测试更脆弱。
一旦失败，先别改断言——先想清楚是行为真的变了，还是新增了非确定性。

依据: docs/DESIGN.md § 7、docs/design/06-roadmap.md § 4、CONTRIBUTING.md § 测试要求
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
import sys
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from alterego.kernel.clock import FrozenClock, resolve_timezone
from alterego.kernel.config import Config
from alterego.kernel.scheduler import Scheduler


pytestmark = pytest.mark.golden

SRC = Path(__file__).resolve().parent.parent.parent / "src"
TZ = resolve_timezone("Asia/Shanghai")
T0 = datetime(2026, 9, 15, 9, 0, tzinfo=TZ)

_CONFIG_DUMP = """
import json
from alterego.kernel.config import Config

config = Config.load(path=None, env={}, overrides={"core": {"data_dir": "data"}})
print(json.dumps(config.to_dict(redact=False), sort_keys=True, ensure_ascii=False, default=str))
"""


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def in_a_fresh_interpreter(script: str) -> str:
    """在**全新的解释器进程**里跑一段脚本，返回它的 stdout。

    必须另起进程：同一个进程里跑两遍会共享 `sys.modules`、模块级缓存
    和字符串哈希随机化上下文，测出来的「一致」是假的。
    跨进程一致才说明真的没有隐藏状态。
    """
    env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONIOENCODING="utf-8")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=True,
    )
    return completed.stdout


class Recorder:
    """按触发顺序记下「谁、在虚拟时间的哪一刻跑的」。

    让任务自己写记录，而不是去读调度器的内部结构：黄金测试要锁住的是**契约**，
    内部字段改名时这些测试不该跟着改。
    """

    def __init__(self, clock: FrozenClock) -> None:
        self.clock = clock
        self.fires: list[tuple[str, float]] = []

    def job(self, name: str) -> Callable[[], Awaitable[None]]:
        async def record() -> None:
            offset = (self.clock.virtual_now() - T0).total_seconds()
            self.fires.append((name, offset))

        return record


async def collect_fires(
    seed: int,
    *,
    jobs: int = 5,
    interval: timedelta = timedelta(minutes=5),
    jitter: timedelta = timedelta(seconds=45),
    horizon: timedelta = timedelta(hours=3),
    step: timedelta = timedelta(seconds=15),
) -> list[tuple[str, float]]:
    """用给定种子调度 N 个带抖动的任务，返回 ``[(任务名, 触发偏移秒), ...]``。"""
    clock = FrozenClock(T0)
    recorder = Recorder(clock)
    scheduler = Scheduler(clock, rng=random.Random(seed))
    for index in range(jobs):
        scheduler.every(interval, recorder.job(f"job-{index}"), name=f"job-{index}", jitter=jitter)

    elapsed = timedelta(0)
    while elapsed < horizon:
        clock.advance(step)
        elapsed += step
        await scheduler.run_due()
    return recorder.fires


class TestConfigIsReproducible:
    def test_two_processes_produce_the_same_dump(self) -> None:
        first = in_a_fresh_interpreter(_CONFIG_DUMP)
        second = in_a_fresh_interpreter(_CONFIG_DUMP)

        assert first == second
        assert first.strip(), "配置导出不该是空的"

    def test_the_dump_is_stable_within_one_process(self) -> None:
        """`to_dict()` 每次调用都该给出同样的键序。

        这条曾经真的会挂：dict 从 set 里构造出来的话，键序会随
        字符串哈希随机化而变，同一台机器上两次都可能不一样。
        """
        config = Config.load(path=None, env={}, overrides={"core": {"data_dir": "data"}})

        dumps = {
            json.dumps(config.to_dict(redact=False), sort_keys=True, default=str) for _ in range(3)
        }

        assert len(dumps) == 1

    def test_loading_twice_gives_equal_configs(self) -> None:
        overrides = {"core": {"data_dir": "data"}}

        first = Config.load(path=None, env={}, overrides=overrides)
        second = Config.load(path=None, env={}, overrides=overrides)

        assert first.to_dict(redact=False) == second.to_dict(redact=False)


class TestSchedulerIsReproducible:
    def test_the_same_seed_gives_the_same_fires(self) -> None:
        first = run(collect_fires(1234))
        second = run(collect_fires(1234))

        assert first == second
        assert first, "三小时里一个任务都没跑，说明场景没搭起来"

    def test_different_seeds_shift_the_fire_times(self) -> None:
        """不同种子必须给出不同的触发时刻。

        这也是反向保险：一条「恒等返回」的实现能通过上面那条，但过不了这一条。
        """
        assert run(collect_fires(1)) != run(collect_fires(2))

    def test_running_twice_in_the_same_process_agrees(self) -> None:
        """同一进程内两次运行也要一致——排除掉模块级缓存带来的漂移。"""
        assert run(collect_fires(7, jobs=4, interval=timedelta(minutes=10))) == run(
            collect_fires(7, jobs=4, interval=timedelta(minutes=10))
        )

    def test_jitter_never_fires_early(self) -> None:
        """抖动只允许**推后**，不能提前。

        提前会让「每 90 分钟最多一条消息」这条约束失效，
        而克制正是这个项目最核心的产品特性（S7：能看到 Agent 的克制）。
        """
        interval = timedelta(minutes=30)
        fires = run(collect_fires(5, jobs=2, interval=interval, jitter=timedelta(seconds=45)))

        assert fires
        for _name, offset in fires:
            position_in_cycle = offset % interval.total_seconds()
            assert position_in_cycle <= 45, f"提前触发了：{offset}s"

    def test_a_task_actually_repeats(self) -> None:
        """三小时 / 每 5 分钟 → 每个任务至少跑 30 次。"""
        fires = run(collect_fires(3, jobs=1, interval=timedelta(minutes=5)))

        assert len(fires) >= 30


class TestFreshSchedulerHasNothingDue:
    def test_nothing_fires_before_it_is_due(self) -> None:
        """不推进时间就什么都不该跑，且 `run_due()` 报出下一个时刻。"""
        clock = FrozenClock(T0)
        recorder = Recorder(clock)
        scheduler = Scheduler(clock, rng=random.Random(5))
        scheduler.every(timedelta(minutes=30), recorder.job("x"), name="x")

        next_due = run(scheduler.run_due())

        assert recorder.fires == []
        assert next_due is not None
        assert next_due >= T0 + timedelta(minutes=30)

    def test_an_empty_scheduler_has_no_next_due(self) -> None:
        scheduler = Scheduler(FrozenClock(T0), rng=random.Random(0))

        assert scheduler.next_due() is None
        assert run(scheduler.run_due()) is None

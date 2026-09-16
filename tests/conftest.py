"""全局测试夹具。

两个约定贯穿全部测试：

1. **时间必须是冻结的**。``FrozenClock`` 而不是 ``RealClock``——
   任何依赖真实时间的断言都会变成偶发失败。
2. **时区用内核的解析器**。Windows 上没有 ``tzdata``，直接
   ``ZoneInfo("Asia/Shanghai")`` 会抛异常，导致测试不可移植。

还有一条**全局状态**约定，见 :func:`_restore_logging`。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, tzinfo

import pytest

from alterego.kernel.bus import EventBus
from alterego.kernel.clock import FrozenClock, resolve_timezone
from alterego.kernel.logging import ROOT_LOGGER_NAME
from alterego.kernel.registry import ServiceRegistry


#: 与 config/alterego.toml 的默认时区保持一致。
TZ: tzinfo = resolve_timezone("Asia/Shanghai")

#: 所有测试共用的虚拟起点。选一个工作日的上午——
#: 「上午 9 点」在作息判定里是明确的清醒时段，不会踩到静默期的边界。
T0: datetime = datetime(2026, 9, 15, 9, 0, tzinfo=TZ)


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """跑完一个测试就把 ``alterego`` 这个 logger 恢复原样。

    ``alterego serve`` 装配前会调 :func:`alterego.kernel.logging.setup_logging`，
    而它做两件**跨测试留得住**的事：把 ``alterego`` 的 ``propagate`` 关掉，
    再挂上自己的 handler。关掉之后 ``caplog``（它挂在 root 上）就再也收不到
    这个包的日志，于是出现最难查的一种失败：

    ``pytest tests/test_holidays.py`` → 绿
    ``pytest tests/test_cli_serve.py tests/test_holidays.py`` → 红

    这不是被测试代码的错——CLI 入口本来就该配置日志。错的是测试之间共用了
    ``logging`` 的全局状态而没人收回来，所以收在这里，而不是在某个具体
    测试文件里打补丁。
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    handlers_before = list(logger.handlers)
    level_before = logger.level
    propagate_before = logger.propagate
    yield
    for handler in list(logger.handlers):
        if handler not in handlers_before:
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(level_before)
    logger.propagate = propagate_before


@pytest.fixture
def clock() -> FrozenClock:
    """冻结在 2026-09-15 09:00 (Asia/Shanghai) 的时钟。"""
    return FrozenClock(T0)


@pytest.fixture
def hour() -> timedelta:
    """常用时长，避免测试里到处写 ``timedelta(hours=1)``。"""
    return timedelta(hours=1)


@pytest.fixture
def bus(clock: FrozenClock) -> EventBus:
    """事件总线。"""
    return EventBus(clock, logger=logging.getLogger("test.bus"))


@pytest.fixture
def registry() -> ServiceRegistry:
    """服务注册表。"""
    return ServiceRegistry(logger=logging.getLogger("test.registry"))

"""全局测试夹具。

两个约定贯穿全部测试：

1. **时间必须是冻结的**。``FrozenClock`` 而不是 ``RealClock``——
   任何依赖真实时间的断言都会变成偶发失败。
2. **时区用内核的解析器**。Windows 上没有 ``tzdata``，直接
   ``ZoneInfo("Asia/Shanghai")`` 会抛异常，导致测试不可移植。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, tzinfo

import pytest

from alterego.kernel.bus import EventBus
from alterego.kernel.clock import FrozenClock, resolve_timezone
from alterego.kernel.registry import ServiceRegistry


#: 与 config/alterego.toml 的默认时区保持一致。
TZ: tzinfo = resolve_timezone("Asia/Shanghai")

#: 所有测试共用的虚拟起点。选一个工作日的上午——
#: 「上午 9 点」在作息判定里是明确的清醒时段，不会踩到静默期的边界。
T0: datetime = datetime(2026, 9, 15, 9, 0, tzinfo=TZ)


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

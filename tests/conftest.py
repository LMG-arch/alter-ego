"""全局测试夹具。

两个约定贯穿全部测试：

1. **时间必须是冻结的**。``FrozenClock`` 而不是 ``RealClock``——
   任何依赖真实时间的断言都会变成偶发失败。
2. **时区用内核的解析器**。Windows 上没有 ``tzdata``，直接
   ``ZoneInfo("Asia/Shanghai")`` 会抛异常，导致测试不可移植。

还有两条**全局状态**约定，见 :func:`_restore_logging` 与 :func:`_no_config_file_from_this_machine`。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, tzinfo
from pathlib import Path

import pytest

from alterego.kernel import config as kernel_config
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


@pytest.fixture(scope="session")
def no_config_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """一间**一定不存在** ``config/alterego.toml`` 的目录，整个会话共用。

    只建一次，而不是每个测试建一间：这个夹具要的只是一个「没有配置文件的地方」，
    而这个性质与「哪个测试在跑」无关。挂在 ``tmp_path`` 上会让每个测试都多一次
    建目录，三千多个测试下来是实打实的分钟级开销。

    它是 session 级的，所以**只读**——需要写东西的测试请用自己的 ``tmp_path``。
    """
    return tmp_path_factory.mktemp("no-config", numbered=False)


@pytest.fixture(autouse=True)
def _no_config_file_from_this_machine(no_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把「默认配置文件」的位置挪出这台机器，让测试看不见它。

    ``Config.load(path=None)`` 会按 :data:`alterego.kernel.config.DEFAULT_CONFIG_PATHS`
    在**当前工作目录**里找 ``config/alterego.toml``——这是产品该有的行为。但在测试里
    它带来一个隐形依赖：用例的成败取决于**跑测试的这台机器上恰好有没有那份文件**。
    照 Quickstart 建过配置文件的人，会看到一批与本次改动毫不相关的红，而且报错信息
    指向断言本身，看不出是环境造成的（例：``assert 'deepseek' == 'openai_compatible'``）。

    收在 conftest 而不是逐个用例打补丁，理由和 :func:`_restore_logging` 一样：
    漏出去的是一份跨用例的全局状态，收回来就该收在一处。

    指到一个空目录而不是清空成空元组，是为了让「自己写一份配置文件、再让它被发现」
    的写法（``tmp_path / "alterego.toml"``）仍然成立——那正是多数用例在做的事。
    显式传 ``path=`` 的用例完全不受影响。
    """
    monkeypatch.setattr(
        kernel_config,
        "DEFAULT_CONFIG_PATHS",
        (no_config_dir / "config" / "alterego.toml", no_config_dir / "alterego.toml"),
    )


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

"""时钟。

**两个时间概念必须分开**（``docs/design/01-architecture.md`` § 2.5）：

===============  ==========================================================
``now()``        真实世界时间。只用于日志、UI、报告、重试退避、文件命名
``virtual_now()`` Agent 感知的时间。作息判定、时段计算、记忆衰减全部基于它
===============  ==========================================================

推演层（``sim/``）禁止直接调用 ``datetime.now()``，由
``scripts/check_architecture.sh`` 第 3 组强制。原因很实际：

- ``fast`` / ``turbo`` 倍速模式要求虚拟时间能被推进，真实时钟做不到
- 黄金测试要求时间冻结，否则每一次运行的结果都不一样
- 「它有自己的一天」这件事本身依赖时间可以被快进

实现方式：用**重定基点**（rebase）而不是累加两个时间来源。
每次 ``advance()`` / ``set_speed()`` / ``jump_to()`` 都把当前虚拟时间固化为
新起点，然后重新开始计时——否则「真实流逝的部分」会被重复计入。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Final, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from alterego.kernel.errors import ConfigError, SimulationError


__all__ = [
    "Clock",
    "FrozenClock",
    "RealClock",
    "VirtualClock",
    "make_clock",
    "resolve_timezone",
]

#: 各推演模式的默认倍速。
#: 数值取自 ``docs/design/04-simulation-loop.md`` § 1.2。
_DEFAULT_SPEEDS: Final[dict[str, float]] = {"fast": 60.0, "turbo": 300.0}

#: 只收录「不实行夏令时」的时区。
#:
#: 固定偏移对它们来说是**精确**的近似；对伦敦、纽约之类实行夏令时的时区，
#: 固定偏移一年里会错一小时——那比直接报错更糟，所以宁可不支持。
_FIXED_OFFSET_HOURS: Final[dict[str, float]] = {
    "UTC": 0.0,
    "Asia/Bangkok": 7.0,
    "Asia/Dubai": 4.0,
    "Asia/Hong_Kong": 8.0,
    "Asia/Kolkata": 5.5,
    "Asia/Seoul": 9.0,
    "Asia/Shanghai": 8.0,
    "Asia/Singapore": 8.0,
    "Asia/Taipei": 8.0,
    "Asia/Tokyo": 9.0,
}

_TZDATA_HINT: Final[str] = (
    "Windows 系统不自带 IANA 时区数据库。"
    "执行 `pip install tzdata` 可获得完整的时区支持；"
    "否则只能使用不实行夏令时的时区（已内置 UTC / Asia/Shanghai 等 10 个）。"
)


def resolve_timezone(name: str) -> tzinfo:
    """把配置里的时区名解析为 :class:`datetime.tzinfo`。

    优先使用标准库 :mod:`zoneinfo`（需要系统时区库或 ``tzdata`` 包）。
    Windows 上没有 ``tzdata`` 时会退回到**内置的固定偏移表**，
    该表只包含不实行夏令时的时区——因为对实行夏令时的时区给出
    固定偏移会静默地算错一小时。

    Args:
        name: IANA 时区名（如 ``"Asia/Shanghai"``），或 ``"local"`` 表示本机时区。

    Returns:
        可用于构造 aware datetime 的 tzinfo。

    Raises:
        ConfigError: 时区名无法解析，或需要 tzdata 但未安装。
    """
    if name in {"local", ""}:
        return _local_timezone()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        pass
    hours = _FIXED_OFFSET_HOURS.get(name)
    if hours is not None:
        return timezone(timedelta(hours=hours), name)
    raise ConfigError("无法解析时区", timezone=name, hint=_TZDATA_HINT)


def _local_timezone() -> tzinfo:
    """取本机时区。

    注意：这里返回的是**当前时刻**的固定偏移，不是带夏令时规则的时区对象。
    对「本地单人运行」的场景足够，且避免了额外依赖。
    """
    offset: tzinfo | None = datetime.fromtimestamp(time.time(), tz=UTC).astimezone().tzinfo
    return offset if offset is not None else UTC


def _utcnow() -> datetime:
    """真实 UTC 时间。集中一处，方便将来替换为可注入的时间源。"""
    return datetime.now(UTC)


def _require_aware(value: datetime, *, field: str = "时间") -> datetime:
    """拒绝 naive datetime。

    naive 时间会在跨时区、跨夏令时时静默算错。与其等到线上出现
    「凌晨三点它给你发消息」，不如在入口处直接拒绝。
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ConfigError(
            f"{field}必须是带时区信息的（aware）。请传 tzinfo，"
            "例如 datetime(2026, 9, 15, 9, 0, tzinfo=resolve_timezone('Asia/Shanghai'))",
            value=str(value),
        )
    return value


@runtime_checkable
class Clock(Protocol):
    """时钟接口。

    推演层只依赖这个协议，因此换时钟不需要改任何业务代码。
    """

    def now(self) -> datetime:
        """真实时间。用于日志、UI、报告。"""
        ...

    def virtual_now(self) -> datetime:
        """Agent 感知的时间。所有作息与衰减判定都用它。"""
        ...

    def advance(self, delta: timedelta) -> None:
        """推进虚拟时间。``realtime`` 模式下不支持。"""
        ...

    async def sleep_until(self, when: datetime) -> None:
        """等到给定的**虚拟**时间。

        在倍速模式下，等待的真实时长会按倍速缩短。
        """
        ...


class RealClock:
    """真实时钟：``virtual_now() == now()``。

    用于 ``realtime`` 模式——Agent 与用户处在同一条时间轴上，
    「它有自己的一天」就是你的这一天。
    """

    def __init__(self, tz: tzinfo | None = None) -> None:
        """初始化。

        Args:
            tz: 时区。``None`` 时使用 ``Asia/Shanghai``（与配置默认值一致）。
        """
        self._tz: tzinfo = tz if tz is not None else resolve_timezone("Asia/Shanghai")

    def now(self) -> datetime:
        return datetime.now(self._tz)

    def virtual_now(self) -> datetime:
        return self.now()

    def advance(self, delta: timedelta) -> None:
        """真实时钟无法推进时间。

        这里**抛异常而不是静默忽略**：真实模式下调用 ``advance()``
        说明调用方搞错了运行模式，静默忽略会让时间悄悄对不上，
        而错误很难定位。
        """
        raise SimulationError(
            "真实时钟不能推进时间（realtime 模式下时间由现实决定）",
            clock="RealClock",
            delta=str(delta),
        )

    async def sleep_until(self, when: datetime) -> None:
        delay = (when - self.now()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)


class VirtualClock:
    """虚拟时钟：支持倍速与跳转。

    ``virtual_now() = 起点 + 真实经过时间 × 倍速 + 显式推进量``

    用于 ``fast``（60 倍）与 ``turbo``（300 倍）模式。
    """

    def __init__(self, start: datetime, speed: float = 1.0) -> None:
        """初始化。

        Args:
            start: 虚拟起点，必须是 aware datetime。
            speed: 倍速，必须大于 0。``60.0`` 表示真实 1 秒 = 虚拟 1 分钟。

        Raises:
            ConfigError: ``start`` 是 naive 时间，或 ``speed <= 0``。
        """
        if speed <= 0:
            raise ConfigError("时钟倍速必须大于 0", speed=speed)
        self._origin: datetime = _require_aware(start, field="虚拟起点")
        self._speed: float = float(speed)
        self._origin_real: datetime = _utcnow()

    @property
    def speed(self) -> float:
        """当前倍速。"""
        return self._speed

    def now(self) -> datetime:
        """真实时间。UI 想知道「现实中现在几点」时用它。"""
        return datetime.now(self._origin.tzinfo)

    def virtual_now(self) -> datetime:
        elapsed = _utcnow() - self._origin_real
        return self._origin + elapsed * self._speed

    def advance(self, delta: timedelta) -> None:
        """直接推进虚拟时间（推演引擎调用）。"""
        self._rebase(self.virtual_now() + delta)

    def set_speed(self, speed: float) -> None:
        """调整倍速。调整不会造成时间跳变——以当前虚拟时间为新起点。"""
        if speed <= 0:
            raise ConfigError("时钟倍速必须大于 0", speed=speed)
        self._rebase(self.virtual_now())
        self._speed = float(speed)

    def jump_to(self, when: datetime) -> None:
        """跳转到指定虚拟时间（用于「睡到早上 8 点」这类大跨度推进）。"""
        self._rebase(_require_aware(when, field="跳转目标"))

    def _rebase(self, when: datetime) -> None:
        """把 ``when`` 固化为新起点，并从这里重新开始计时。"""
        self._origin = when
        self._origin_real = _utcnow()

    async def sleep_until(self, when: datetime) -> None:
        remaining = (when - self.virtual_now()).total_seconds()
        if remaining > 0:
            await asyncio.sleep(remaining / self._speed)


class FrozenClock:
    """测试专用时钟：虚拟时间**只**由 :meth:`advance` 控制。

    这是可复现性的基础（设计原则 P6）。三个刻意的选择：

    1. ``now()`` 也返回冻结时间——测试里出现真实时间，断言就会变得不确定
    2. ``sleep_until()`` 立即返回且**不推进**时间——如果偷偷推进，
       测试会自己走时间，失败时无从定位
    3. 需要时间前进时**显式**调用 ``advance()``，让时间变化出现在测试代码里
    """

    def __init__(self, start: datetime) -> None:
        """初始化。

        Args:
            start: 冻结的起点时间，必须是 aware datetime。
        """
        self._now: datetime = _require_aware(start)

    def now(self) -> datetime:
        return self._now

    def virtual_now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta

    def set(self, when: datetime) -> None:
        """直接跳到某个时间点（便于「第二天早上」这类场景）。"""
        self._now = _require_aware(when, field="设置时间")

    async def sleep_until(self, when: datetime) -> None:
        """测试里不真的等待，也不自动推进时间。

        参数刻意保留不校验：测试需要能验证「有人传了错的时间也不炸」——
        时间推进必须由测试显式调用 :meth:`advance`，否则用例就不再是确定性的。
        """
        del when


def make_clock(
    mode: str,
    *,
    tz: tzinfo,
    start: datetime | None = None,
    speed: float | None = None,
) -> Clock:
    """按推演模式创建时钟。

    集中在这里是为了让「模式 → 时钟类型」的映射只有一处，
    避免在引擎里散落 ``if mode == "fast"``。

    Args:
        mode: ``"realtime"`` / ``"fast"`` / ``"turbo"``。
        tz: 时区。
        start: 虚拟起点，仅虚拟模式使用。``None`` 表示用 ``tz`` 下的当前时间。
        speed: 倍速，仅虚拟模式使用。``None`` 时按模式取默认值
            （``fast`` → 60，``turbo`` → 300）。

    Returns:
        RealClock 或 VirtualClock。

    Raises:
        ConfigError: 模式名无法识别。
    """
    if mode == "realtime":
        return RealClock(tz)
    if mode not in _DEFAULT_SPEEDS:
        raise ConfigError(
            "未知的推演模式",
            mode=mode,
            supported=["realtime", *_DEFAULT_SPEEDS],
        )
    origin = start if start is not None else datetime.now(tz)
    chosen = _DEFAULT_SPEEDS[mode] if speed is None else speed
    return VirtualClock(_require_aware(origin, field="虚拟起点"), chosen)

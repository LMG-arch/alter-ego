"""``alterego.kernel.clock`` 的行为测试。

时钟是整个项目的可复现性基石（设计原则 P6），因此这里测得比其他模块细：
时间必须**只**按照代码里写明的方向流动。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alterego.kernel.clock import (
    Clock,
    FrozenClock,
    RealClock,
    VirtualClock,
    make_clock,
    resolve_timezone,
)
from alterego.kernel.errors import ConfigError, SimulationError


# 刻意在本文件内重复定义而不是从 conftest import：
# 测试文件之间不互相依赖，任何一处改动的影响范围才是可预测的。
TZ = resolve_timezone("Asia/Shanghai")
T0 = datetime(2026, 9, 15, 9, 0, tzinfo=TZ)


# ── resolve_timezone ────────────────────────────────────────


def test_resolve_timezone_gives_asia_shanghai_plus_eight() -> None:
    """Asia/Shanghai 没有夏令时，因此固定偏移 +8 永远是精确的。"""
    tz = resolve_timezone("Asia/Shanghai")
    offset = datetime(2026, 9, 15, 9, 0, tzinfo=tz).utcoffset()
    assert offset == timedelta(hours=8)


def test_resolve_timezone_supports_utc() -> None:
    tz = resolve_timezone("UTC")
    assert datetime(2026, 9, 15, tzinfo=tz).utcoffset() == timedelta(0)


def test_resolve_timezone_rejects_dst_zone_without_tzdata() -> None:
    """实行夏令时的时区不能用固定偏移糊弄过去，必须明确报错。"""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo("America/New_York")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        pass
    else:
        pytest.skip("本机有 tzdata，无法验证「未安装 tzdata」的分支")

    with pytest.raises(ConfigError) as info:
        resolve_timezone("America/New_York")
    assert "tzdata" in str(info.value)


def test_resolve_timezone_local() -> None:
    assert resolve_timezone("local").utcoffset(datetime(2026, 9, 15)) is not None


# ── 边界：naive datetime 一律拒绝 ───────────────────────────


def test_virtual_clock_rejects_naive_start() -> None:
    with pytest.raises(ConfigError) as info:
        VirtualClock(datetime(2026, 9, 15, 9, 0))
    assert "时区" in str(info.value)


def test_frozen_clock_rejects_naive_start() -> None:
    with pytest.raises(ConfigError):
        FrozenClock(datetime(2026, 9, 15, 9, 0))


def test_virtual_clock_rejects_non_positive_speed() -> None:
    with pytest.raises(ConfigError):
        VirtualClock(T0, speed=0)
    with pytest.raises(ConfigError):
        VirtualClock(T0, speed=-1)


# ── RealClock ───────────────────────────────────────────────


def test_real_clock_virtual_now_equals_now() -> None:
    """realtime 模式下 Agent 与用户在同一条时间轴上。"""
    clock = RealClock(TZ)
    # 两次调用之间允许微小流逝，但不允许出现量级上的差别
    assert abs(clock.now() - clock.virtual_now()) < timedelta(seconds=1)
    assert clock.virtual_now().tzinfo is not None


def test_real_clock_refuses_to_advance() -> None:
    """静默忽略会让时间悄悄对不上，因此这里必须显式失败。"""
    with pytest.raises(SimulationError) as info:
        RealClock(TZ).advance(timedelta(hours=1))
    assert "realtime" in str(info.value)


async def test_real_clock_sleep_until_returns_immediately_for_past() -> None:
    clock = RealClock(TZ)
    await clock.sleep_until(clock.now() - timedelta(hours=1))  # 不抛异常即通过


# ── VirtualClock ────────────────────────────────────────────


def test_virtual_clock_starts_at_given_point() -> None:
    clock = VirtualClock(T0, speed=1.0)
    assert clock.virtual_now() >= T0
    assert clock.virtual_now() - T0 < timedelta(seconds=2)  # 只允许真实流逝的零头


def test_virtual_clock_advance_moves_forward_exactly_once() -> None:
    """重定基点必须只计一次，重复计入是这里最容易犯的错。"""
    clock = VirtualClock(T0, speed=1.0)
    clock.advance(timedelta(hours=6))
    delta = clock.virtual_now() - T0
    assert timedelta(hours=6) <= delta < timedelta(hours=6, seconds=2)


def test_virtual_clock_accumulates_advances() -> None:
    clock = VirtualClock(T0, speed=1.0)
    clock.advance(timedelta(hours=1))
    clock.advance(timedelta(minutes=30))
    delta = clock.virtual_now() - T0
    assert timedelta(hours=1, minutes=30) <= delta < timedelta(hours=1, minutes=30, seconds=2)


def test_virtual_clock_speed_change_does_not_jump_time() -> None:
    clock = VirtualClock(T0, speed=1.0)
    before = clock.virtual_now()
    clock.set_speed(60.0)
    assert clock.speed == 60.0
    # 切换倍速的瞬间不应该出现时间跳变
    assert abs(clock.virtual_now() - before) < timedelta(seconds=2)


def test_virtual_clock_fast_mode_runs_faster_in_real_time() -> None:
    """60 倍速下，虚拟时间的前进量应约为真实流逝量的 60 倍。

    这里用 ``advance()`` 而非真实等待来验证倍速：既确定又快。
    """
    clock = VirtualClock(T0, speed=60.0)
    clock.advance(timedelta(minutes=10))
    assert clock.virtual_now() - T0 >= timedelta(minutes=10)


def test_virtual_clock_jump_to() -> None:
    clock = VirtualClock(T0, speed=1.0)
    target = T0 + timedelta(days=1, hours=3)
    clock.jump_to(target)
    assert clock.virtual_now() >= target
    assert clock.virtual_now() - target < timedelta(seconds=2)


def test_virtual_clock_jump_to_rejects_naive() -> None:
    with pytest.raises(ConfigError):
        VirtualClock(T0).jump_to(datetime(2026, 9, 16, 9, 0))


def test_virtual_clock_now_is_real_time() -> None:
    """``now()`` 是给 UI 用的真实时间，不能被虚拟推进带走。

    这里的断言只能拿**虚拟时间自己前后的差**来比。曾经的写法是
    ``clock.virtual_now() - after > timedelta(days=29)``——把
    ``T0`` 推出来的合成时间直接减真实时间，而那个差每天都会变小，
    于是每天 09:00 一到就必挂。合成时间只和它自己的起点比才有意义。
    """
    clock = VirtualClock(T0, speed=1.0)
    before, virtual_before = clock.now(), clock.virtual_now()
    clock.advance(timedelta(days=30))
    after = clock.now()
    assert after - before < timedelta(seconds=5)
    assert clock.virtual_now() - virtual_before > timedelta(days=29)


async def test_virtual_clock_sleep_until_past_returns_immediately() -> None:
    clock = VirtualClock(T0, speed=1.0)
    await clock.sleep_until(clock.virtual_now() - timedelta(hours=1))


# ── FrozenClock ─────────────────────────────────────────────


def test_frozen_clock_does_not_move_on_its_own() -> None:
    clock = FrozenClock(T0)
    first = clock.virtual_now()
    second = clock.virtual_now()
    assert first == second == T0
    assert clock.now() == T0


def test_frozen_clock_advances_only_when_told() -> None:
    clock = FrozenClock(T0)
    clock.advance(timedelta(hours=8))
    assert clock.virtual_now() == T0 + timedelta(hours=8)


def test_frozen_clock_set() -> None:
    clock = FrozenClock(T0)
    clock.set(T0 + timedelta(days=1))
    assert clock.virtual_now() == T0 + timedelta(days=1)


async def test_frozen_clock_sleep_until_does_not_advance_time() -> None:
    """如果这里偷偷推进时间，测试就会「自己走时间」，失败时无从定位。"""
    clock = FrozenClock(T0)
    await clock.sleep_until(T0 + timedelta(hours=5))
    assert clock.virtual_now() == T0


# ── 协议一致性 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "clock_obj",
    [RealClock(TZ), VirtualClock(T0), FrozenClock(T0)],
    ids=["RealClock", "VirtualClock", "FrozenClock"],
)
def test_all_clocks_satisfy_protocol(clock_obj: object) -> None:
    assert isinstance(clock_obj, Clock)


# ── make_clock（模式 → 时钟类型 的唯一映射点） ──────────────


def test_make_clock_realtime() -> None:
    assert isinstance(make_clock("realtime", tz=TZ), RealClock)


def test_make_clock_fast_uses_60x() -> None:
    clock = make_clock("fast", tz=TZ, start=T0)
    assert isinstance(clock, VirtualClock)
    assert clock.speed == 60.0


def test_make_clock_turbo_uses_300x() -> None:
    clock = make_clock("turbo", tz=TZ, start=T0)
    assert isinstance(clock, VirtualClock)
    assert clock.speed == 300.0


def test_make_clock_accepts_explicit_speed() -> None:
    clock = make_clock("fast", tz=TZ, start=T0, speed=17.5)
    assert isinstance(clock, VirtualClock)
    assert clock.speed == 17.5


def test_make_clock_rejects_unknown_mode() -> None:
    with pytest.raises(ConfigError) as info:
        make_clock("warp", tz=TZ)
    assert info.value.context["mode"] == "warp"


def test_make_clock_defaults_to_current_time_in_given_zone() -> None:
    clock = make_clock("fast", tz=resolve_timezone("Asia/Shanghai"))
    assert clock.virtual_now().utcoffset() == timedelta(hours=8)
    assert clock.virtual_now().tzinfo is not UTC

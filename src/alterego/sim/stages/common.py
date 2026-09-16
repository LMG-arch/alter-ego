"""六个内置阶段共用的小工具。

放在这里而不是各自复制一份，是因为「距上次说话多久」这类换算一旦出现两份，
它们迟早在取整方式上分叉——而两个只在差一分钟时才不同的答案，
比一个明显错的答案更难查。

**这里面全是纯函数。** 没有 IO、没有全局状态、不取当前时间（时间一律由调用方
从 ``ctx.virtual_now`` 传进来）。一个阶段的时间来源只能有一个。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any


__all__ = [
    "clock_text",
    "day_key",
    "describe_block",
    "elapsed_since",
    "last_started_at",
    "minutes_between",
    "weekday_text",
]

#: 中文星期。索引与 ``datetime.weekday()`` 对齐（0 = 周一）。
_WEEKDAYS: tuple[str, ...] = ("一", "二", "三", "四", "五", "六", "日")


def minutes_between(earlier: datetime | None, later: datetime) -> float | None:
    """``earlier`` 到 ``later`` 隔了多少分钟。

    ``earlier`` 为 ``None`` 时返回 ``None`` 而不是一个很大的数——
    「从来没说过话」和「很久没说话」是两件不同的事，而把它们混成一个
    大数字，会让新装好的它在第一天就摆出被冷落的样子。
    """
    if earlier is None:
        return None
    delta = later - earlier
    return max(0.0, delta.total_seconds() / 60.0)


def last_started_at(activities: Iterable[Any]) -> datetime | None:
    """从行为记录里取最后一次的开始时间。

    用 ``max`` 而不是取最后一个元素：``today_activity`` 来自仓储，
    它的顺序是「按时间升序」，但那是实现细节而不是契约——依赖它会让
    某天有人改一句 ``ORDER BY`` 就悄悄改变推演结果。
    """
    times = [
        moment
        for moment in (getattr(item, "started_at", None) for item in activities)
        if isinstance(moment, datetime)
    ]
    return max(times) if times else None


def describe_block(block: Any) -> str:
    """把日程块说成一句话。没有日程块时说「没有安排」。"""
    activity = getattr(block, "activity", "") if block is not None else ""
    return str(activity) if activity else "没有安排"


def clock_text(moment: datetime) -> str:
    """给提示词看的虚拟时间。不含秒——秒对「现在是下午还是半夜」毫无帮助。"""
    return f"{moment:%Y-%m-%d %H:%M}"


def weekday_text(moment: datetime) -> str:
    """「周三」这种写法。模型对中文星期的理解比 ``weekday=2`` 可靠得多。"""
    return f"周{_WEEKDAYS[moment.weekday()]}"


def day_key(moment: datetime) -> str:
    """``budget_usage.day`` 的键。用当地日期而不是 UTC 日期。"""
    return moment.date().isoformat()


def elapsed_since(earlier: datetime | None, later: datetime, fallback: timedelta) -> timedelta:
    """距上次更新过了多久。

    ``earlier`` 为 ``None``（情绪还没有过任何记录）或时间倒流（换了时钟、
    导入了旧库）时返回 ``fallback``。**不允许返回负数**：``update_emotion``
    会用这个值算指数衰减，负数会让情绪往反方向飞。
    """
    if earlier is None:
        return fallback
    delta = later - earlier
    return delta if delta > timedelta(0) else fallback

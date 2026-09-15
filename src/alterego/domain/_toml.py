"""TOML 里的值 → 领域层认的值。

`holidays/<年份>.toml` 和 `data/birthdays.toml` 是两份**手写**的数据文件，
读法必须一模一样：`verified = "true"` 是错的，`lead_days = 3.0` 也是错的。

不能靠 Python 的真值判断放过去——`"false"` 是个非空字符串，
`bool("false")` 是 `True`，于是「没核实」被静悄悄读成「已核实」。
这些函数存在的唯一理由就是不给上面那种事留缝。

放在这一个模块里而不是各自的文件里，是因为**两份数据文件必须守同一条规矩**。
复制一份出来两边迟早会分叉，而分叉的表现是「其中一份文件突然能写不合法的值了」，
那种 bug 只有在有人真的写错的时候才炸，平时一点症状都没有。

依据: docs/design/12-calendar-and-conversation.md § 3.3
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


__all__ = ["as_bool", "as_date", "as_dates", "as_int", "as_str", "as_strs"]


def as_str(value: Any, *, where: str) -> str:
    """`where` 必须是字符串。"""
    if not isinstance(value, str):
        raise ValueError(f"{where} 必须是字符串，收到 {value!r}")
    return value


def as_bool(value: Any, *, where: str) -> bool:
    """`where` 必须是布尔值。

    这条卡得比看起来重要：TOML 里 `verified = "false"` 是**合法 TOML**，
    只有这一行能拦住它。
    """
    if not isinstance(value, bool):
        raise ValueError(f"{where} 必须是布尔值，收到 {value!r}")
    return value


def as_int(value: Any, *, where: str) -> int:
    """`where` 必须是整数。

    顺带排掉 `bool`：`True` 在 Python 里是 `int` 的子类，
    不排的话 `lead_days = true` 会变成 `lead_days = 1`。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where} 必须是整数，收到 {value!r}")
    return value


def as_date(value: Any, *, where: str) -> date:
    """`where` 必须是 TOML 日期。

    TOML 的日期是裸写的（`day = 2026-02-17`），tomllib 直接给 `date` 对象。
    注意 `datetime` 是 `date` 的子类，要排掉——带时间的写法说明作者想错了。
    """
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError(f"{where} 必须是 TOML 日期，写成 `{where.split()[-1]} = 2026-02-17` 这样")
    return value


def as_dates(value: Any, *, where: str) -> tuple[date, ...]:
    """`where` 必须是日期数组。"""
    if not isinstance(value, list):
        raise ValueError(f"{where} 必须是日期数组，收到 {value!r}")
    return tuple(as_date(item, where=f"{where}[{index}]") for index, item in enumerate(value))


def as_strs(value: Any, *, where: str) -> tuple[str, ...]:
    """`where` 必须是字符串数组。"""
    if not isinstance(value, list):
        raise ValueError(f"{where} 必须是字符串数组，收到 {value!r}")
    return tuple(as_str(item, where=f"{where}[{index}]") for index, item in enumerate(value))

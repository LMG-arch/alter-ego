"""命令行入口。

完整命令树见 ``docs/design/05-channels.md`` § 8。当前实现到「能启动、能报版本、
能查节日日历」——后续每加一个功能就多一个子命令，而不是一次性写完再调。

退出码约定（``docs/design/01-architecture.md`` § 7）::

    0  正常
    1  通用错误
    2  配置非法
    3  插件依赖环
    4  数据库迁移失败

关于输出：这一层用 ``sys.stdout.write`` 而不是 ``print``。
``scripts/check_architecture.sh`` 第 5 组对 ``src/alterego`` **整个目录**禁止
``print(``（本意是「生产代码别留调试打印」，而 CLI 正好也在那个目录里）。
不去改那条红线——它是给生产代码用的；CLI 用显式的 ``sys.stdout.write``
表达「这行是给用户看的输出」，反而更清楚。
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from collections.abc import Callable
from datetime import date, datetime

from alterego import __version__
from alterego.domain.calendar import (
    DAY_KIND_LABELS,
    HOLIDAY_KIND_LABELS,
    HOLIDAY_PHASE_LABELS,
    PREP_THRESHOLD,
    Holiday,
    HolidayCalendar,
    HolidayContext,
)
from alterego.holidays import available_years, load_calendar
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.config import Config
from alterego.kernel.errors import AlterEgoError


__all__ = ["build_parser", "main"]

_WEEKDAY_NAMES = "一二三四五六日"
_RULE = "─" * 58
_BAR_WIDTH = 10


# ────────────────────────────────────────────────────────────
# 输出助手
# ────────────────────────────────────────────────────────────


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def _weekday(day: date) -> str:
    """`周一` 这样的中文星期。领域层不提供——那是界面的事。"""
    return f"周{_WEEKDAY_NAMES[day.weekday()]}"


def _today() -> date:
    """按**配置的时区**取今天。

    不用 `date.today()`：它按进程本地时区算，装的可能是 UTC。
    东八区早上 8 点之前，UTC 还停在昨天——而节日问的就是「哪一天」，
    差一天就是差一个节日。`DTZ011` 也正是拦这个。
    """
    zone = resolve_timezone(Config.load().core.timezone)
    return datetime.now(zone).date()


def _bar(value: float, width: int = _BAR_WIDTH) -> str:
    """把 0~1 的强度画成条形：`0.35` 看不出趋势，`███░░░░░░░` 看得出。"""
    filled = max(0, min(width, round(value * width)))
    return "█" * filled + "░" * (width - filled)


def _width(text: str) -> int:
    """显示宽度。CJK 与全角标点占两列。"""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _pad(text: str, width: int) -> str:
    """按**显示宽度**补齐。

    用 `f"{text:<8}"` 不行：它数的是字符个数，而中文一个字占两列，
    于是表格里所有含中文的列都会错位（`节后 0  天` 旁边跟着 `节后 1 天`）。
    """
    return text + " " * max(0, width - _width(text))


def _span(holiday: Holiday) -> str:
    """放假区间。

    注意先看 `days_off`：`span` 在没有 `days_off` 时会退化成「锚点那一天」，
    那是为了 `covers()` 能工作，不代表真放假——情人节、圣诞都不放假，
    只看 `length_days` 会把它们写成「放假 02-14」。
    """
    if not holiday.days_off:
        return "不放假"
    if len(holiday.days_off) == 1:
        return f"放假 {holiday.start:%m-%d}"
    return f"放假 {len(holiday.days_off)} 天（{holiday.start:%m-%d} ~ {holiday.end:%m-%d}）"


def _prepare_notice(context: HolidayContext) -> str:
    """这一天的准备状态。

    区分「知道」与「开始安排」是这套曲线的重点：
    跨过 `PREP_THRESHOLD` 才会真的往日程里塞准备活动，在那之前只是知道而已。
    """
    if context.phase != "anticipating":
        return ""
    if context.is_preparing:
        return "  → 已经开始安排了"
    return f"  → 还只是知道（跨过 {PREP_THRESHOLD:.2f} 才排进日程）"


# ────────────────────────────────────────────────────────────
# 子命令实现
# ────────────────────────────────────────────────────────────


def _resolve_calendar(year: int) -> HolidayCalendar | None:
    """取回答 `year` 需要的那份日历；**答不了那一年就返回 `None`**。

    `load_calendar` 会顺带读前后各一年（年末得看得见元旦），但它合并出来的东西
    未必包含 `year` 本身：磁盘上只有 2026 时，`load_calendar(2027)` 照样返回一份
    「不覆盖 2027」的日历。不在这里拦住，`list --year 2027` 就会拿 2026 的节日列表
    顶着 2027 的标题输出，退出码还是 0——一个没人会发现的错答案。
    宁可退回 `None`，让调用方说「没有那一年的数据」。
    """
    calendar = load_calendar(year)
    if calendar is None or not calendar.covers_year(year):
        return None
    return calendar


def _missing_year(year: int) -> int:
    """三年都没有时统一的出口。已有的年份要列出来，不然用户不知道怎么补。"""
    years = available_years()
    listed = "、".join(str(item) for item in years) if years else "（无）"
    _err(f"没有 {year} 年的节日数据。已有的年份：{listed}")
    return 2


def _print_status(calendar: HolidayCalendar) -> None:
    """把「这份数据可信到什么程度」摆在最前面。

    `confirmed = false` 是出厂状态（调休要等国务院公告），用户有权先知道这件事，
    而不是某天才发现它一直按错的日期过日子。
    """
    years = "、".join(str(item) for item in sorted(calendar.years))
    if calendar.confirmed:
        _out(f"数据已核对 · 覆盖 {years}")
        return
    _out(f"数据未核对 · 覆盖 {years}")
    if calendar.note:
        _out(f"  {calendar.note}")
    pending = calendar.unverified()
    if pending:
        names = "、".join(holiday.name for holiday in pending)
        _out(f"  待按权威日历核对 {len(pending)} 个：{names}")


def _cmd_list(args: argparse.Namespace) -> int:
    year: int = args.year or _today().year
    calendar = _resolve_calendar(year)
    if calendar is None:
        return _missing_year(year)

    holidays = sorted(
        (holiday for holiday in calendar.holidays if holiday.day.year == year),
        key=lambda item: item.day,
    )
    _out(f"{year} 年节日 · 共 {len(holidays)} 个")
    _print_status(calendar)
    _out(_RULE)
    for holiday in holidays:
        kind = HOLIDAY_KIND_LABELS[holiday.kind]
        _out(
            f"{holiday.day:%m-%d}  {_weekday(holiday.day)}  {_pad(holiday.name, 8)}"
            f"{_pad(kind, 6)}{_pad(_span(holiday), 28)}"
            f"提前 {holiday.lead_days} 天 · 节后 {holiday.aftermath_days} 天"
        )
    return 0


def _cmd_today(args: argparse.Namespace) -> int:
    day: date = args.date or _today()
    calendar = _resolve_calendar(day.year)
    if calendar is None:
        return _missing_year(day.year)

    _out(f"{day:%Y-%m-%d}  {_weekday(day)}  {DAY_KIND_LABELS[calendar.day_kind(day)]}")
    _out(_RULE)

    context = calendar.context(day)
    if context.is_active and context.holiday is not None:
        phase = HOLIDAY_PHASE_LABELS[context.phase]
        signed = context.days_until or 0
        when = f"{abs(signed)} 天后" if signed > 0 else f"{abs(signed)} 天前"
        _out(
            f"{phase} {context.holiday.name}（{when}）"
            f"  强度 {_bar(context.intensity)} {context.intensity:.2f}"
        )
        if context.activities:
            _out(f"  这段时间大概会：{' · '.join(context.activities)}")
        notice = _prepare_notice(context)
        if notice:
            _out(notice)
    else:
        _out("今天没有节日安排")

    upcoming = calendar.upcoming(day, within_days=args.days)
    _out("")
    _out(f"未来 {args.days} 天")
    if not upcoming:
        # 空着也要把标题和这句话说出来：整段消失会让人以为窗口没生效。
        _out("  这几天没有节日")
    for holiday in upcoming:
        gap = (holiday.start - day).days
        suffix = "就是今天" if gap == 0 else f"{gap} 天后 · {_span(holiday)}"
        _out(f"  {holiday.day:%m-%d}  {_weekday(holiday.day)}  {holiday.name}  {suffix}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    year: int = args.year or _today().year
    calendar = _resolve_calendar(year)
    if calendar is None:
        return _missing_year(year)

    _out(f"节日数据核对 · {year}")
    _out(_RULE)
    mine = [holiday for holiday in calendar.holidays if holiday.day.year == year]
    _out(f"节日总数   {len(mine)}")
    _out(f"核对状态   {'已核对' if calendar.confirmed else '未核对'}")
    if calendar.note:
        _out(f"说明       {calendar.note}")

    pending = sorted(
        (holiday for holiday in calendar.unverified() if holiday.day.year == year),
        key=lambda item: item.day,
    )
    _out(f"待核对     {len(pending)} 个")
    for holiday in pending:
        _out(f"  {_pad(holiday.name, 8)}{holiday.day}   {holiday.note}")

    _out("")
    _out(f"已有数据   {'、'.join(str(item) for item in available_years()) or '（无）'}")
    return 0


# ────────────────────────────────────────────────────────────
# 参数解析
# ────────────────────────────────────────────────────────────


def _add_year(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="看哪一年（默认今年）",
    )


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。

    用标准库 ``argparse`` 而不是 click / typer——见设计原则 P5（标准库优先）
    与 ``docs/adr/0002-use-python-as-implementation-language.md``。
    """
    parser = argparse.ArgumentParser(
        prog="alterego",
        description="AlterEgo · 拟我 —— 一个会自己生活、自己思考、按自己的节奏找你的数字存在。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"alterego {__version__}",
    )
    commands = parser.add_subparsers(dest="command", metavar="{calendar}")

    calendar_parser = commands.add_parser("calendar", help="节日日历：它知道过几天要过节")
    calendar_commands = calendar_parser.add_subparsers(
        dest="subcommand",
        metavar="{list,today,check}",
    )

    list_parser = calendar_commands.add_parser("list", help="一年的节日一览")
    _add_year(list_parser)
    list_parser.set_defaults(handler=_cmd_list)

    today_parser = calendar_commands.add_parser("today", help="今天处在哪个节日的哪一段")
    today_parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="看哪一天（YYYY-MM-DD，默认今天）",
    )
    today_parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="往后看几天（默认 14）",
    )
    today_parser.set_defaults(handler=_cmd_today)

    check_parser = calendar_commands.add_parser("check", help="数据可信度：哪些日期还没核对")
    _add_year(check_parser)
    check_parser.set_defaults(handler=_cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 参数列表。``None`` 时取 ``sys.argv[1:]``（便于测试直接传列表）。

    Returns:
        进程退出码。
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    handler: Callable[[argparse.Namespace], int] | None = getattr(args, "handler", None)
    if handler is None:
        # 还没指定具体子命令（例如只敲了 `alterego`）——打印帮助是当前最合理的行为。
        parser.print_help()
        return 0

    try:
        return handler(args)
    except (AlterEgoError, ValueError) as exc:
        # 数据文件写坏了、年份越界、配置文件非法。都是「配置非法」，不是崩溃。
        _err(f"错误：{exc}")
        return 2

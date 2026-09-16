"""命令行入口。

完整命令树见 ``docs/design/05-channels.md`` § 8。当前实现到「能启动、能报版本、
能查节日日历、能记生日、能维护数据库、能梳理记忆」——后续每加一个功能就多一个
子命令，而不是一次性写完再调。

退出码约定（``docs/design/01-architecture.md`` § 6.3）::

    0  正常
    1  通用错误
    2  配置非法
    3  插件依赖环
    4  数据库迁移失败

关于输出：统一走 ``alterego.cli_io`` 里的 ``_out`` / ``_err``。为什么不用
``print``、为什么把它们单独放一个模块，那里写着。

关于分层：**组装根**（composition root）有三个文件——

- 本文件：参数树，以及不认识任何具体实现的命令组；
- ``cli_db.py``：``alterego db`` 那几个维护命令；
- ``cli_memory.py``：``alterego memory`` 的两条梳理命令。

后两个都要挑一个具体的存储实现（``cli_memory.py`` 还要挑一个具体的模型供应商）
装上。它们从这里拆出去，一半是因为本文件撞上 `AGENTS.md` § 5 的 900 行上限，
另一半是因为「谁认识 SQLite」这件事越窄越好查。整个程序里只有这三个文件知道
「存储用的是 SQLite」，其余代码一律只认 ``StorageBackend`` Protocol。
``scripts/check_architecture.sh`` 第 3 组红线把这件事钉住了。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from alterego import __version__
from alterego.birthdays import default_path, load_book, save_book
from alterego.cli_chat import add_chat_parser
from alterego.cli_config import add_config_parser
from alterego.cli_dataset import add_dataset_parser
from alterego.cli_db import (
    cmd_db_backup,
    cmd_db_migrate,
    cmd_db_restore,
    cmd_db_status,
)
from alterego.cli_io import _RULE, _err, _out, _pad
from alterego.cli_memory import add_memory_parser
from alterego.cli_plugins import add_plugins_parser
from alterego.cli_study import add_study_parser
from alterego.cli_vault import add_vault_parser
from alterego.domain.birthday import (
    DEFAULT_AFTERMATH_DAYS,
    DEFAULT_LEAD_DAYS,
    DEFAULT_NAME,
    PERSONAL_PHASE_LABELS,
    SUBJECT_LABELS,
    SUBJECTS,
    Birthday,
    BirthdayBook,
    BirthdaySubject,
    check_month_day,
)
from alterego.domain.calendar import (
    DAY_KIND_LABELS,
    HOLIDAY_KIND_LABELS,
    HOLIDAY_PHASE_LABELS,
    PREP_THRESHOLD,
    Holiday,
    HolidayCalendar,
    HolidayContext,
    HolidayPhase,
)
from alterego.holidays import available_years, load_calendar
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.config import Config
from alterego.kernel.errors import AlterEgoError, MigrationError


__all__ = ["build_parser", "main"]

_WEEKDAY_NAMES = "一二三四五六日"
_BAR_WIDTH = 10


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


def _span(holiday: Holiday) -> str:
    """放假区间。

    注意先看 `days_off`：`span` 在没有 `days_off` 时会退化成「锚点那一天」，
    那是为了让 `covers()` 能工作，不代表真放假——生日、情人节、圣诞都不放假，
    只看 `length_days` 会把它们写成「放假 02-14」。
    """
    if not holiday.days_off:
        return "不放假"
    if len(holiday.days_off) == 1:
        return f"放假 {holiday.start:%m-%d}"
    return f"放假 {len(holiday.days_off)} 天（{holiday.start:%m-%d} ~ {holiday.end:%m-%d}）"


def _phase_label(holiday: Holiday, phase: HolidayPhase) -> str:
    """阶段名。生日用自己的那套。

    「节前 3 天是你的生日」读起来就是不对劲——生日不是节。
    两套名字都覆盖全部四个 `HolidayPhase`，少一个的表现是界面上直接印出一个 `None`。
    """
    labels = PERSONAL_PHASE_LABELS if holiday.kind == "personal" else HOLIDAY_PHASE_LABELS
    return labels[phase]


def _upcoming_suffix(holiday: Holiday, gap: int) -> str:
    """`calendar today` 那列「未来 N 天」的后半段。

    生日没有放假区间可报，写成「不放假」又答非所问——
    这里要说的其实是「每年都是这天」，那才是「提前几天就知道」的依据。
    """
    if gap == 0:
        return "就是今天"
    if holiday.kind == "personal":
        return f"{gap} 天后 · 每年这天"
    return f"{gap} 天后 · {_span(holiday)}"


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


def _load_holidays(year: int) -> HolidayCalendar | None:
    """取回答 `year` 需要的那份**节日**数据（不含生日）；答不了那一年就返回 `None`。

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


def _birthday_path() -> Path:
    """生日记录的位置。

    单独一个函数，是为了给测试一个能换掉的接口：不然 `tests/test_cli.py`
    会去读开发机上真实的 `data/birthdays.toml`——
    在别人机器上绿、在自己机器上红的测试比没有测试还糟。
    """
    return default_path(Config.load().data_dir)


def _load_birthdays() -> BirthdayBook:
    """读生日记录。文件不存在就当没记过（那是默认状态，不是错误）。"""
    return load_book(_birthday_path())


def _view(year: int, book: BirthdayBook) -> HolidayCalendar | None:
    """节日数据 + 生日记录，合成一份能回答那一年每一天的日历。

    **生日能独立撑起一天**：随包只带一两年节日数据，而生日是用户自己填的。
    只填了生日、那年又没有节日数据时，这里给的是一份只有生日的日历而不是
    `None`——否则「填了生日却查不到」就变成一个说不清的问题。

    但「缺节日数据」这件事必须说出来（往 stderr 打一行）：
    不然 `calendar list` 会拿一份只有生日的列表顶着「节日一览」的标题输出，
    读的人以为今年就这几个节。**一个「空」被换成一个看起来正常的答案**，
    是这套日历里反复踩到的那个形状（见 `_load_holidays` 的说明）。

    生日那份给自己 `confirmed=True`：生日不存在「整份数据待核对」这种状态，
    每一条自己带 `verified`。合并用 `all()`，所以节日那份是 false 时结果仍是 false。
    """
    holidays = _load_holidays(year)
    personal = book.as_holidays(year)
    if not personal:
        return holidays
    own = HolidayCalendar(holidays=personal, confirmed=True)
    if holidays is None:
        _err(f"注意：没有 {year} 年的节日数据，下面只有生日记录。")
        return own
    return HolidayCalendar.merge(holidays, own)


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
    # 生日和节日的「待核对」不是同一件事：节日的调休要等国务院公告，
    # 生日的日期只是还没从本人嘴里问实。混在一行里会让人不知道该去核哪个。
    pending = [item for item in calendar.unverified() if item.kind != "personal"]
    if pending:
        names = "、".join(holiday.name for holiday in pending)
        _out(f"  待按权威日历核对 {len(pending)} 个：{names}")
    personal = [item for item in calendar.unverified() if item.kind == "personal"]
    if personal:
        names = "、".join(holiday.name for holiday in personal)
        _out(f"  生日日期待确认 {len(personal)} 个：{names}（见 alterego birthday list）")


def _cmd_list(args: argparse.Namespace) -> int:
    year: int = args.year or _today().year
    calendar = _view(year, _load_birthdays())
    if calendar is None:
        return _missing_year(year)

    holidays = sorted(
        (holiday for holiday in calendar.holidays if holiday.day.year == year),
        key=lambda item: item.day,
    )
    personal = sum(1 for holiday in holidays if holiday.kind == "personal")
    extra = f"（含 {personal} 个生日）" if personal else ""
    _out(f"{year} 年节日 · 共 {len(holidays)} 个{extra}")
    _print_status(calendar)
    _out(_RULE)
    for holiday in holidays:
        kind = HOLIDAY_KIND_LABELS[holiday.kind]
        tail = "提前 {lead} 天 · 节后 {after} 天"
        if holiday.kind == "personal":
            tail = "提前 {lead} 天 · 过完还惦记 {after} 天"
        _out(
            f"{holiday.day:%m-%d}  {_weekday(holiday.day)}  {_pad(holiday.name, 8)}"
            f"{_pad(kind, 6)}{_pad(_span(holiday), 28)}"
            + tail.format(lead=holiday.lead_days, after=holiday.aftermath_days)
        )
    return 0


def _cmd_today(args: argparse.Namespace) -> int:
    day: date = args.date or _today()
    calendar = _view(day.year, _load_birthdays())
    if calendar is None:
        return _missing_year(day.year)

    _out(f"{day:%Y-%m-%d}  {_weekday(day)}  {DAY_KIND_LABELS[calendar.day_kind(day)]}")
    _out(_RULE)

    context = calendar.context(day)
    if context.is_active and context.holiday is not None:
        phase = _phase_label(context.holiday, context.phase)
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
        _out(
            f"  {holiday.day:%m-%d}  {_weekday(holiday.day)}  {holiday.name}  "
            f"{_upcoming_suffix(holiday, (holiday.start - day).days)}"
        )
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    year: int = args.year or _today().year
    calendar = _load_holidays(year)
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

    # 这里只核对**节日**数据。生日的可信度是另一回事（逐条带 verified，
    # 来源可能是「本人说的」也可能是「听说的」），整在 `birthday list` 里。
    count = len(_load_birthdays())
    hint = "alterego birthday list" if count else "alterego birthday add --who user --on 03-15"
    _out(f"生日记录   {count} 条（{hint}）")
    return 0


def _cmd_birthday_list(args: argparse.Namespace) -> int:
    book = _load_birthdays()
    today = _today()
    _out(f"生日记录 · {len(book)} 条 · {_birthday_path()}")
    _out(_RULE)
    if not len(book):
        # 「一条都没有」必须说出来。只印一行空表头，看起来像命令没生效。
        #
        # 空记录**不提前 return**：提醒逻辑在下面，而空记录恰恰是最需要提醒的那种情况
        # （「自己」和「用户」两条都缺）。提前 return 会让最该看到提醒的人看不到。
        _out("一条都还没有。")
    else:
        for birthday, when in book.sorted_by_next(today, within_days=args.days):
            gap = (when - today).days
            left = "就是今天" if gap == 0 else f"{gap} 天后"
            flag = "" if birthday.verified else "  ⚠ 日期未确认"
            # 星期几用 `sorted_by_next` 给出的**那一天**，不再自己算一次当年日期：
            # 「闰日在平年落到 02-28」只应该有一处实现。
            _out(
                f"  {birthday.month_day}  {_weekday(when)}  "
                f"{_pad(SUBJECT_LABELS[birthday.subject], 6)}{_pad(birthday.name, 10)}"
                f"下次 {left} · 提前 {birthday.lead_days} 天{flag}"
            )
    # 记了别人的生日不等于它自己会过生日。「自己」和「用户」这两条缺了要说出来——
    # 这不是错误，只是没记；但不说的话，用户看到一列生日会以为齐了，
    # 而它自己的生日恰恰是「还要会过生日」这句话最直接的意思。
    absent: list[BirthdaySubject] = [key for key in ("self", "user") if book.find(key) is None]
    if absent:
        who = "、".join(DEFAULT_NAME[key] for key in absent)
        _out("")
        _out(f"还没记 {who} 的生日。补上：alterego birthday add --who {absent[0]} --on MM-DD")
    return 0


def _write_birthday(args: argparse.Namespace, *, creating: bool) -> int:
    """`add` 与 `set` 的全部区别就是中间那两个分支：允不允许盖掉已有的那一条。

    默认**不盖**。盖掉一条生日记录不会有任何提示，而写错一个月份的下场是
    「那天什么也没发生，当事人也不会说」——这种错误没有任何反馈回路，
    所以宁可在这里多问一句，把决定权交回给用户。
    """
    book = _load_birthdays()
    month, day = args.on
    subject: BirthdaySubject = args.who
    npc_id: str = args.npc_id or ""
    # 先按 key 找旧记录：`set` 只改日期时，称呼、提前量、消退期都该留着。
    # 不这么做的话 `birthday set --on 06-04` 会顺手把「妈妈」改回「对方」、
    # 把调过的提前量改回默认值——一次「只改一天」的操作改了四样东西。
    existing = book.find(npc_id if subject == "npc" else subject)
    name = args.name or (existing.name if existing is not None else DEFAULT_NAME[subject])
    lead = args.lead_days
    if lead is None:
        lead = existing.lead_days if existing is not None else DEFAULT_LEAD_DAYS[subject]
    after = args.aftermath_days
    if after is None:
        after = existing.aftermath_days if existing is not None else DEFAULT_AFTERMATH_DAYS[subject]
    birthday = Birthday(
        subject=subject,
        name=name,
        month=month,
        day=day,
        lead_days=lead,
        aftermath_days=after,
        npc_id=npc_id,
        verified=not args.unverified,
    )
    if creating and existing is not None:
        _err(f"「{name}」已经记过 {existing.month_day} 了。要改就用 set，别用 add。")
        return 2
    if not creating and existing is None:
        _err(f"没有记过「{name}」的生日（{birthday.month_day}），没什么可改的。用 add 加一条。")
        return 2
    save_book(_birthday_path(), book.with_record(birthday))
    verb = "已改" if existing is not None else "已记下"
    extra = "" if birthday.verified else "（日期还没跟本人确认过）"
    _out(
        f"{verb}「{name}」的生日：{birthday.month_day}，提前 {birthday.lead_days} 天开始准备{extra}。"
    )
    return 0


def _cmd_birthday_add(args: argparse.Namespace) -> int:
    """新增一条。已经记过的会被挡住，而不是静默盖掉。"""
    return _write_birthday(args, creating=True)


def _cmd_birthday_set(args: argparse.Namespace) -> int:
    """改写已有的那条。没记过的会被挡住，免得「以为改了」其实是新加了一条。"""
    return _write_birthday(args, creating=False)


# ────────────────────────────────────────────────────────────
# 参数解析
# ────────────────────────────────────────────────────────────


def _month_day(text: str) -> tuple[int, int]:
    """argparse 的 ``type=``：把 ``MM-DD`` 解析成 ``(月, 日)``。

    在这里就调用 `check_month_day`，所以 `--on 02-30` 是**用法错误**
    （argparse 报错、退出码 2），而不是写进文件之后才在下次读取时炸。
    写进文件的坏日期没人会再看第二眼：它要到那天才发作，而那天正好是
    「什么也没发生」——没有比这更难查的失败了。
    """
    parts = text.split("-")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise argparse.ArgumentTypeError(f"日期要写成 MM-DD（例如 06-03），而不是 {text!r}")
    month, day = (int(part) for part in parts)
    try:
        check_month_day(month, day)
    except ValueError as exc:
        # 把用户原样输入的 `text` 也带回来：领域层的报错只知道数字，
        # 于是 `--on 02-30` 会被回显成「2-30 不是一个真实存在的日期」——
        # 用户按的是 02-30，看到 2-30 只会怀疑自己按错了哪一位。
        raise argparse.ArgumentTypeError(f"--on {text} 用不了：{exc}") from exc
    return (month, day)


def _add_year(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="看哪一年（默认今年）",
    )


def _add_birthday_arguments(parser: argparse.ArgumentParser) -> None:
    """`add` 与 `set` 接受完全相同的参数——区别只在语义，不在参数表。"""
    parser.add_argument("--who", choices=SUBJECTS, required=True, help="这是谁的生日")
    parser.add_argument(
        "--on",
        type=_month_day,
        required=True,
        metavar="MM-DD",
        help="几月几日，不含年份（生日每年都过）",
    )
    parser.add_argument("--name", default=None, help="怎么称呼（默认「自己 / 你 / 对方」）")
    parser.add_argument("--npc-id", default=None, help="NPC 的 id（--who npc 时必填）")
    parser.add_argument(
        "--lead-days",
        type=int,
        default=None,
        metavar="N",
        help="提前几天开始准备（默认按主体：自己 7 / 用户 14 / NPC 3）",
    )
    parser.add_argument(
        "--aftermath-days",
        type=int,
        default=None,
        metavar="N",
        help="过完还惦记几天（默认按主体：自己 3 / 用户 3 / NPC 1）",
    )
    parser.add_argument(
        "--unverified",
        action="store_true",
        help="只是听说过、没跟本人确认过（列表里会标出来）",
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
    commands = parser.add_subparsers(
        dest="command",
        metavar="{birthday,calendar,chat,config,dataset,db,memory,plugins,study,vault}",
    )

    calendar_parser = commands.add_parser("calendar", help="节日日历：它知道过几天要过节")
    calendar_commands = calendar_parser.add_subparsers(
        dest="subcommand",
        metavar="{list,today,check}",
    )
    # 记住自己，好让「只敲 `alterego calendar`」打出**这一层**的帮助。
    # 不记的话会回落到顶层帮助，看着像「calendar 后面没东西可以敲」。
    calendar_parser.set_defaults(subparser=calendar_parser)

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

    birthday_parser = commands.add_parser(
        "birthday",
        help="生日：谁的、哪一天、提前多久开始准备",
    )
    birthday_commands = birthday_parser.add_subparsers(
        dest="subcommand",
        metavar="{list,add,set}",
    )
    birthday_parser.set_defaults(subparser=birthday_parser)

    birthday_list = birthday_commands.add_parser("list", help="记过谁的生日，下次还有几天")
    birthday_list.add_argument(
        "--days",
        type=int,
        default=None,
        help="只看这么多天内过生日的人（默认全部）",
    )
    birthday_list.set_defaults(handler=_cmd_birthday_list)

    birthday_add = birthday_commands.add_parser("add", help="记一个新的生日")
    _add_birthday_arguments(birthday_add)
    birthday_add.set_defaults(handler=_cmd_birthday_add)

    birthday_set = birthday_commands.add_parser("set", help="改掉已经记过的生日")
    _add_birthday_arguments(birthday_set)
    birthday_set.set_defaults(handler=_cmd_birthday_set)

    db_parser = commands.add_parser("db", help="数据库：建库、看状态、备份、恢复")
    db_commands = db_parser.add_subparsers(
        dest="subcommand",
        metavar="{status,migrate,backup,restore}",
    )
    db_parser.set_defaults(subparser=db_parser)

    db_status = db_commands.add_parser("status", help="库在哪、版本多少、还差几个迁移")
    db_status.set_defaults(handler=cmd_db_status)

    db_migrate = db_commands.add_parser("migrate", help="建库，或把库升到当前版本")
    db_migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示将要执行什么，不改数据库，也不建库",
    )
    db_migrate.set_defaults(handler=cmd_db_migrate)

    db_backup = db_commands.add_parser("backup", help="整份备份（唯一的「回滚」手段）")
    db_backup.add_argument(
        "--dest",
        default=None,
        metavar="PATH",
        help="备份到哪（默认 data/backups/alterego-<时间戳>.db）",
    )
    db_backup.set_defaults(handler=cmd_db_backup)

    db_restore = db_commands.add_parser("restore", help="从备份恢复（会覆盖现在的库）")
    db_restore.add_argument("file", metavar="FILE", help="要恢复的备份文件")
    db_restore.set_defaults(handler=cmd_db_restore)

    add_memory_parser(commands)
    add_vault_parser(commands)
    add_dataset_parser(commands)
    add_study_parser(commands)
    add_chat_parser(commands)
    add_plugins_parser(commands)
    add_config_parser(commands)

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
        # 只敲了 `alterego`，或只敲到某一层组名（如 `alterego db`）。
        # 后者要打**那一层**的帮助：敲 `db` 却看到顶层帮助，会让人以为
        # `db` 后面没东西可以敲——而这恰好是最需要指路的时候。
        group: argparse.ArgumentParser | None = getattr(args, "subparser", None)
        (group or parser).print_help()
        return 0

    try:
        return handler(args)
    except MigrationError as exc:
        # 迁移失败单独一个退出码。它是唯一一条「数据库可能停在半路」的失败，
        # 脚本最需要把它和「配置写错了」区分开；文档也承诺了 4。
        _err(f"错误：{exc}")
        return 4
    except (AlterEgoError, ValueError) as exc:
        # 数据文件写坏了、年份越界、配置文件非法。都是「配置非法」，不是崩溃。
        _err(f"错误：{exc}")
        return 2

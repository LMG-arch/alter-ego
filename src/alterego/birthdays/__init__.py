"""生日记录文件（默认 `data/birthdays.toml`）——和 `holidays/` 对称，但数据不随包。

为什么不塞进 `holidays/`：那里面是**程序的数据**（法条 + 天文 + 行政公告，谁装都一样），
这里是**你的数据**（谁过生日、哪一天、提前多久开始准备）。
混在一起会让「升级会不会覆盖我的文件」变成一个真问题——包内文件可以随便覆盖，
用户文件一个字都不能动。所以一个住在包内、一个住在 `data/`，读的代码也各放一处，
各自和 `domain/` 划清 IO 边界（红线 2：`domain/` 不开文件）。

⚠️ 不知道某人的生日时**不要编一个日期出来**。没有记录就是没有记录，
那一天只是普通的一天；`alterego birthday list` 会把“还没记的人和没核实的日期”列成一句提醒。

依据: docs/design/12-calendar-and-conversation.md § 17
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from alterego.domain.birthday import (
    DEFAULT_AFTERMATH_DAYS,
    DEFAULT_LEAD_DAYS,
    Birthday,
    BirthdayBook,
)


__all__ = ["FILE_NAME", "default_path", "load_book", "render", "save_book"]


FILE_NAME: Final[str] = "birthdays.toml"
"""记录文件的固定名字。可配的只有「数据目录在哪」，文件名跟着走。"""

logger = logging.getLogger(__name__)


def default_path(data_dir: Path) -> Path:
    """生日记录的位置：`<data_dir>/birthdays.toml`。

    和 `backups/`、`outbox/` 一样是个**派生路径**而不是配置项——
    多一个可以配置的文件名，就多一处「配置指向 A，程序读 B」的失配可能。
    """
    return data_dir / FILE_NAME


def load_book(path: Path) -> BirthdayBook:
    """读生日记录。**文件不存在返回空书，不报错。**

    要求比 `holidays.load_year()` 还弱一档：那一年没有节日数据是「今年还没写」，
    而这里没有文件是**默认状态**——刚装好的实例本来就没有任何生日记录。

    但文件存在却读不出来照样要炸：那是有人把手改坏了（月份写成 13、少一个引号、
    `verified = "true"`）。静默跳过的后果是「某个人的生日凭空消失」，
    而 `calendar today` 那天看起来完全正常——这种问题比启动失败难查得多。
    """
    if not path.is_file():
        logger.debug("还没有生日记录（%s 不存在）", path)
        return BirthdayBook()
    return BirthdayBook.from_toml(path.read_text(encoding="utf-8"), source=path.name)


def save_book(path: Path, book: BirthdayBook) -> None:
    """写回生日记录，目录不存在就建。

    整个文件重写，不追加：`birthday add` 要能**改**而不只是加，
    而「改」在一次重写里比在一次追加里容易说清楚。

    `newline="\\n"` 不是可有可无的：不加它 `write_text` 会按 `os.linesep`
    把每个 `\\n` 翻成 `\\r\\n`，于是在 Windows 上写出来的是一份 CRLF 文件，
    而仓库（`.gitattributes` / `core.autocrlf false`）用的全是 LF——
    这份文件一旦提交，之后每次改动都会显示成整个文件被重写。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(book), encoding="utf-8", newline="\n")


def render(book: BirthdayBook) -> str:
    """把整本书渲染成这个文件的文本。

    省略等于默认值的字段（`lead_days`、`aftermath_days`、`verified = true`、
    空 `note` 与空活动表）：写全会让这份文件看起来像一份配置文件，
    而它其实是几行事实。省略是安全的，因为**省略的条件恰好就是
    「再读回来还是这个值」**——`tests/test_birthdays.py` 里那条
    「读回来跟写出去一样」的测试就是这句话的证据。

    为什么渲染在这一侧、解析在领域层：解析只认字段，那是规则；
    渲染还要写这份文件的抬头注释，而抬头得说清「文件在哪、怎么改」——
    领域层不该知道 `data/birthdays.toml` 这个路径的存在。
    """
    parts: list[str] = [_HEADER]
    if book.note:
        parts.append(f"note = {_quote(book.note)}\n")
    parts.extend(_render_one(birthday) for birthday in book.birthdays)
    return "\n".join(parts)


_HEADER: Final[str] = """\
# 生日记录 —— 谁在哪天过生日。
#
# 这份文件是**你的**数据，不在包里；升级不会碰它。
# 改它用命令行，不用手写：
#     alterego birthday add --who user --on 03-15 --name 你
#     alterego birthday list
#     alterego birthday list
#
# 日期只写月和日，不写年份：生日每年重复，一条记录管所有年份。
#   2 月 29 日在平年算 2 月 28 日（早说一天比晚说一天好）。
#
# subject 只能是 self / user / npc；npc 必须另给 npc_id（用来认「同一个人」）。
# 省略 lead_days / aftermath_days 就用该 subject 的默认值。
# 生日不放假：那天原本是工作日就还是工作日。
"""


def _render_one(birthday: Birthday) -> str:
    lines = [
        "[[birthday]]",
        f"subject = {_quote(birthday.subject)}",
        f"name = {_quote(birthday.name)}",
    ]
    if birthday.subject == "npc":
        lines.append(f"npc_id = {_quote(birthday.npc_id)}")
    lines.append(f"month = {birthday.month}")
    lines.append(f"day = {birthday.day}")
    if birthday.lead_days != DEFAULT_LEAD_DAYS[birthday.subject]:
        lines.append(f"lead_days = {birthday.lead_days}")
    if birthday.aftermath_days != DEFAULT_AFTERMATH_DAYS[birthday.subject]:
        lines.append(f"aftermath_days = {birthday.aftermath_days}")
    if not birthday.verified:
        lines.append("verified = false")
    if birthday.note:
        lines.append(f"note = {_quote(birthday.note)}")
    for field, activities in (
        ("prep_activities", birthday.prep_activities),
        ("during_activities", birthday.during_activities),
        ("aftermath_activities", birthday.aftermath_activities),
    ):
        if activities:
            lines.append(f"{field} = [{', '.join(_quote(item) for item in activities)}]")
    return "\n".join(lines) + "\n"


#: TOML 基本字符串里必须转义的字符。不转义的话会写出一个**读不回来**的文件，
#: 而错误会在下一次读取时才出现——那时已经没人记得是谁写的了。
_ESCAPES: Final[dict[str, str]] = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _quote(text: str) -> str:
    """把任意文本写成一个合法的 TOML 基本字符串。"""
    out: list[str] = []
    for char in text:
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif char < " " or char == "\x7f":
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'

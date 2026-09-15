"""生日——同一套节日机制，日期却来自记录而不是查表。

`HolidayKind` 里的 `personal`（「只属于这个角色自己的日子」）本来就是为生日留的。
把生日做成 `Holiday`，节前曲线（`_ramp_up`）、支配规则、节后余温、活动解锁
全部直接复用，真正多出来的只有三件事：

1. **每年重复**——一条记录管所有年份，而节日是一年一份数据文件
2. **日期来自人**——谁过生日、哪一天，写在 `data/birthdays.toml` 里，不随包
3. **不放假**——`days_off` 恒为空，所以那天照常在过工作日或周末

以及三条真实世界的边界，没有一条是可以四舍五入掉的细节：

- **2 月 29 日**在平年不存在（见 `date_in_year`），而它每四年才出现一次，
  所以除非专门挑一个平年去测，这个崩溃会一直藏着
- **同一天有两个人过生日**（见 `_merge_day`）：那本来就是同一件事
- **生日撞上节日**（国庆、元旦、春节里生日的人不少）：
  强度打平的时候个人日子优先，规则在 `calendar._personal_rank`

依据: docs/design/12-calendar-and-conversation.md § 17
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Literal, get_args

from alterego.domain._toml import as_bool, as_int, as_str, as_strs
from alterego.domain.calendar import (
    MAX_AFTERMATH_DAYS,
    MAX_LEAD_DAYS,
    MAX_YEAR,
    MIN_LEAD_DAYS,
    MIN_YEAR,
    Holiday,
    HolidayPhase,
)


__all__ = [
    "AFTERMATH_ACTIVITIES",
    "DEFAULT_AFTERMATH_DAYS",
    "DEFAULT_LEAD_DAYS",
    "DEFAULT_NAME",
    "DURING_ACTIVITIES",
    "PERSONAL_PHASE_LABELS",
    "PREP_ACTIVITIES",
    "SUBJECTS",
    "SUBJECT_LABELS",
    "Birthday",
    "BirthdayBook",
    "BirthdaySubject",
    "check_month_day",
    "date_in_year",
]


BirthdaySubject = Literal["self", "user", "npc"]
"""谁过生日。

- `self` 这个角色自己。**它也有生日**——不知道的话它就不是一个人，而是一个服务
- `user` 用户。这一个最容易忘，忘了的后果也最重
- `npc` 它认识的人（家人、朋友、同事），靠 `npc_id` 区分

⚠️ 三种之外还有一个真实存在的情况：**不知道某人的生日**。
那不是一个 `subject`，而是**没有这条记录**——不要为了凑满而编一个日期出来。
`BirthdayBook` 允许为空，`alterego birthday list` 会把缺的人列出来。
"""

SUBJECTS: Final[tuple[str, ...]] = get_args(BirthdaySubject)
"""三种主体，同时也是展示顺序（自己 → 用户 → NPC）。"""

SUBJECT_LABELS: Final[dict[BirthdaySubject, str]] = {
    "self": "自己",
    "user": "用户",
    "npc": "NPC",
}

DEFAULT_NAME: Final[dict[BirthdaySubject, str]] = {
    "self": "自己",
    "user": "你",
    "npc": "对方",
}
"""没给 `--name` 时怎么称呼。

写进文件的是**解析后的名字**，不是「按 subject 取默认」这种约定——
文件里少一个字段就跑一次隐式回退，是这类数据文件最容易长出来的暗坑。
"""

DEFAULT_LEAD_DAYS: Final[dict[BirthdaySubject, int]] = {
    "self": 7,
    "user": 14,
    "npc": 3,
}
"""默认提前几天开始准备。

比节日的默认值（3 天）更长，理由很具体：**生日要准备礼物**，
而礼物不是当天想买就能买到的。三档的差别也是真的：

- `user` 最长（14 天）：忘了谁的生日都比忘了这一个好受
- `self` 居中（7 天）：自己的生日往往是「提前一周想起来」
- `npc` 最短（3 天）：一句生日快乐 + 一份小礼物，三天够了
"""

DEFAULT_AFTERMATH_DAYS: Final[dict[BirthdaySubject, int]] = {
    "self": 3,
    "user": 3,
    "npc": 1,
}
"""默认过了之后还惦记几天。

比节日的默认值（1 天）长：节日的余温是「假期结束了」，
生日的余温是「他昨天是不是没太高兴」——后者在脑子里留得久一点。
"""

PERSONAL_PHASE_LABELS: Final[dict[HolidayPhase, str]] = {
    "none": "平常",
    "anticipating": "生日前",
    "during": "生日当天",
    "aftermath": "生日后",
}
"""个人日子的阶段名，替换节日那套「节前 / 过节 / 节后」。

「节前 3 天是你的生日」读起来就是不对劲——生日不是节。
两套名字必须都覆盖全部四个 `HolidayPhase`，
少一个的表现是界面上直接印出一个 `None`。
"""

PREP_ACTIVITIES: Final[dict[BirthdaySubject, tuple[str, ...]]] = {
    "self": (
        "看一眼那天是星期几",
        "想想要不要跟谁提一句",
        "翻翻去年的照片",
        "猜今年谁会记得",
    ),
    "user": (
        "挑生日礼物",
        "想想那天怎么开口",
        "提前订个蛋糕或者餐厅",
        "记着那天别提让人不高兴的事",
    ),
    "npc": ("想送点什么合适", "看看那天有没有空", "记着那天发条消息"),
}
"""按强度逐条解锁的「准备期活动」，顺序即解锁顺序（见 `calendar._take`）。

⚠️ 这些句子会进提示词，而提示词里的「你」是**这个角色**。
所以写「今天是你的生日」会被读成角色自己的生日——提到用户一律说「用户」。
"""

DURING_ACTIVITIES: Final[dict[BirthdaySubject, tuple[str, ...]]] = {
    "self": ("今天是自己生日", "在意别人有没有想起来"),
    "user": ("今天是用户的生日", "今天多留一点时间给用户"),
    "npc": ("今天是对方的生日", "该说一句生日快乐"),
}

AFTERMATH_ACTIVITIES: Final[dict[BirthdaySubject, tuple[str, ...]]] = {
    "self": ("生日过完了，有点说不上来的感觉", "回味那天谁说了什么"),
    "user": ("昨天是用户的生日，还惦记着", "想着礼物有没有送到心坎上"),
    "npc": ("昨天是对方的生日",),
}


_PROBE_YEAR: Final[int] = 2024
"""用来判断「(月, 日) 是不是一个真实存在的日期」的探针年份。

挑闰年是有意的：2024 年有 2 月 29 日、2025 年没有，
所以 `02-29` 合法而 `02-30` 非法——一条 `try` 就把
「合法但四年才有一次」和「根本不存在」分开。
"""


def check_month_day(month: int, day: int) -> None:
    """校验 `month`/`day` 是一个真实存在的日期；不合法就抛 `ValueError`。

    公开出来是因为**界面层也需要它**：`alterego birthday add --on 02-30`
    应该在参数解析阶段就报错，而不是等构造 `Birthday` 时才炸。

    两处各写一份校验必然分叉，而分叉的表现是「命令行收下了，写文件时报错」——
    所以规则只有这一份，`Birthday.__post_init__` 和 CLI 都调它。
    """
    if not 1 <= month <= 12:
        raise ValueError(f"月份必须在 1~12 之间，收到 {month}")
    if not 1 <= day <= 31:
        raise ValueError(f"日期必须在 1~31 之间，收到 {day}")
    try:
        date(_PROBE_YEAR, month, day)
    except ValueError as exc:
        raise ValueError(f"{month}-{day} 不是一个真实存在的日期") from exc


def date_in_year(year: int, month: int, day: int) -> date:
    """`month`/`day` 在 `year` 年对应哪一天。

    **2 月 29 日的闰日**：平年没有这一天，这里算作 **2 月 28 日**。

    也有人过 3 月 1 日，两种都真实存在，没有哪个是「对的」。选早不选晚的理由是
    **迟到比早到糟糕得多**：提前一天说生日快乐，对方只会高兴；
    晚一天说，那句话就变成了「哦对，昨天是你生日啊」。

    不处理它会直接崩在 `date(year, 2, 29)` 上，而崩的那一年**每四年才来一次**，
    所以测试里必须显式挑一个平年（见 `tests/test_domain_birthday.py`）。
    """
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise ValueError(f"年份必须在 {MIN_YEAR}~{MAX_YEAR} 之间，收到 {year}")
    try:
        return date(year, month, day)
    except ValueError as exc:
        if (month, day) == (2, 29):
            return date(year, 2, 28)
        raise ValueError(f"{month}-{day} 不是一个真实存在的日期") from exc


@dataclass(frozen=True, slots=True)
class Birthday:
    """一个人的生日。

    `subject` 是身份，`npc_id` 只在 `subject == "npc"` 时才有意义。
    没有单独的 `subject_id` 字段：那会多出一个能和 `subject` 矛盾的字段，
    而矛盾的数据比少一个字段难查得多。
    """

    subject: BirthdaySubject
    name: str
    month: int
    day: int
    lead_days: int
    aftermath_days: int
    npc_id: str = ""
    verified: bool = True
    note: str = ""
    prep_activities: tuple[str, ...] = ()
    during_activities: tuple[str, ...] = ()
    aftermath_activities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.subject not in SUBJECTS:
            raise ValueError(f"未知的主体：{self.subject}，可选 {list(SUBJECTS)}")
        if not self.name.strip():
            raise ValueError(f"{self.subject} 的生日必须有一个称呼（name）")
        if self.subject == "npc" and not self.npc_id.strip():
            raise ValueError("NPC 的生日必须给 npc_id——没有稳定标识就认不出「同一个人」")
        if self.subject != "npc" and self.npc_id:
            raise ValueError(
                f"{self.subject} 的生日不该有 npc_id（收到 {self.npc_id!r}）——"
                "自己与用户各只有一个，身份就是 subject 本身"
            )
        check_month_day(self.month, self.day)
        if not MIN_LEAD_DAYS <= self.lead_days <= MAX_LEAD_DAYS:
            raise ValueError(
                f"「{self.name}」的提前量必须在 {MIN_LEAD_DAYS}~{MAX_LEAD_DAYS} 天之间，"
                f"收到 {self.lead_days}"
            )
        if not 0 <= self.aftermath_days <= MAX_AFTERMATH_DAYS:
            raise ValueError(
                f"「{self.name}」的消退期必须在 0~{MAX_AFTERMATH_DAYS} 天之间，"
                f"收到 {self.aftermath_days}"
            )

    @property
    def key(self) -> str:
        """这条记录的唯一标识。改生日是「换掉同 key 的那条」，所以它必须稳定。"""
        return self.npc_id if self.subject == "npc" else self.subject

    @property
    def tag(self) -> str:
        """`self` / `user` / `npc:张三` —— 用来拼 `Holiday.id`。"""
        return f"npc:{self.npc_id}" if self.subject == "npc" else self.subject

    @property
    def month_day(self) -> str:
        """`06-03` 这样的展示形式。"""
        return f"{self.month:02d}-{self.day:02d}"

    @property
    def label(self) -> str:
        return f"{self.name}的生日"

    def in_year(self, year: int) -> date:
        """这个人在 `year` 年的生日（闰日见 `date_in_year`）。"""
        return date_in_year(year, self.month, self.day)

    def next_after(self, day: date) -> date:
        """`day` 当天或之后最近的一次生日。

        跨年要往后垫一年：12 月 20 日问一个 1 月 5 日的生日，
        答案在明年——只看今年的话「下一个生日」会答成「今年已经过了」，
        而这个功能存在的理由恰恰是**提前知道**。
        """
        this_year = self.in_year(day.year)
        return this_year if this_year >= day else self.in_year(day.year + 1)

    def days_until(self, day: date) -> int:
        """离下一次生日还有几天（当天为 0）。"""
        return (self.next_after(day) - day).days

    def activities_for(self, phase: HolidayPhase) -> tuple[str, ...]:
        """这条记录为某个阶段准备的活动；自己没写就用该主体的默认值。

        「空元组表示用默认值」是**这里唯一的一处隐式约定**，
        而且只跨了这一个函数边界——所以它不构成暗坑。
        换成 `None` 会让字段类型多一种状态，而那种状态在渲染时又要翻译回来。
        """
        fallback: dict[HolidayPhase, dict[BirthdaySubject, tuple[str, ...]]] = {
            "anticipating": PREP_ACTIVITIES,
            "during": DURING_ACTIVITIES,
            "aftermath": AFTERMATH_ACTIVITIES,
        }
        if phase not in fallback:
            return ()
        table = fallback[phase]
        chosen = {
            "anticipating": self.prep_activities,
            "during": self.during_activities,
            "aftermath": self.aftermath_activities,
        }[phase]
        return chosen or table[self.subject]


@dataclass(frozen=True, slots=True)
class BirthdayBook:
    """一批生日记录。

    和 `HolidayCalendar` 有一条关键的不对称：**空是正常的**。
    `HolidayCalendar` 一个节日都没有说明数据文件写坏了，所以它报错；
    而一本什么都没记的生日簿只是「还没填」，是一个完全合法的状态——
    刚装好的实例第一次跑 `alterego calendar today` 就在这个状态里。

    也正因为如此，`alterego birthday list` 要把「自己和用户的生日都还没记」
    当成一句**要输出的提醒**，而不是让 `calendar today` 看起来一切正常。
    """

    birthdays: tuple[Birthday, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for birthday in self.birthdays:
            if birthday.key in seen:
                raise ValueError(f"生日记录重复：{birthday.key}")
            seen.add(birthday.key)

    def __len__(self) -> int:
        return len(self.birthdays)

    def find(self, key: str) -> Birthday | None:
        """按 `key`（`self` / `user` / npc_id）找一条记录。"""
        return next((item for item in self.birthdays if item.key == key), None)

    def with_record(self, birthday: Birthday) -> BirthdayBook:
        """换掉同 `key` 的那条（没有就新增），顺序按主体与月日排好。

        顺序在这里**不携带含义**，所以排序只做一处：写文件的时候。
        换成在 `BirthdayBook.__post_init__` 里强制有序，每个构造点都得记得先排——
        而那正是「忘了排就静默出错」的形状。
        """
        rest = tuple(item for item in self.birthdays if item.key != birthday.key)
        merged = (*rest, birthday)
        return BirthdayBook(
            birthdays=tuple(sorted(merged, key=_file_order)),
            note=self.note,
        )

    def sorted_by_next(
        self, day: date, *, within_days: int | None = None
    ) -> tuple[tuple[Birthday, date], ...]:
        """按「下一次生日离 `day` 还有多久」排序，返回 `(记录, 那一天)`。

        和 `HolidayCalendar.upcoming()` 答的不是同一个问题：那个只看一份日历
        装着的年份、而且有窗口；这里要回答「最近的那个生日是谁」——
        它可能在明年 1 月，而窗口只有 14 天。
        """
        if within_days is not None and within_days < 0:
            raise ValueError("within_days 不能为负")
        pairs = [(item, item.next_after(day)) for item in self.birthdays]
        if within_days is not None:
            pairs = [pair for pair in pairs if (pair[1] - day).days <= within_days]
        pairs.sort(key=lambda pair: ((pair[1] - day).days, _file_order(pair[0])))
        return tuple(pairs)

    def as_holidays(self, year: int) -> tuple[Holiday, ...]:
        """这本书里的人在 `year` 对应的节日条目，按日期排好。

        **同一天的多个人合并成一条**。这不只是为了绕开
        `HolidayCalendar` 的 `(id, 年份)` 唯一约束——更重要的是：
        同一天过生日本来就是同一件事，「爸爸和小明的生日」比两条并列的记录
        更接近真实的那一天。合并规则全部由内容决定（见 `_merge_day`），
        所以同一份记录永远得到同一个结果（P6）。
        """
        grouped: dict[date, list[Birthday]] = {}
        for birthday in self.birthdays:
            grouped.setdefault(birthday.in_year(year), []).append(birthday)
        return tuple(_merge_day(day, tuple(grouped[day])) for day in sorted(grouped))

    @classmethod
    def from_toml(cls, text: str, *, source: str = "<toml>") -> BirthdayBook:
        """从 TOML 文本建一本书。**只解析文本，不读写文件**（红线 2）。

        读盘在 `alterego.birthdays` 里，和 `HolidayCalendar.from_toml` 同一个分工。
        字段名写错会被挡下来：这是给人（也给它自己）改的文件。
        """
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"生日记录 {source} 不是合法的 TOML：{exc}") from exc

        unknown = sorted(set(raw) - _TOP_LEVEL_KEYS)
        if unknown:
            raise ValueError(
                f"生日记录 {source} 有无法识别的顶层字段：{unknown}；"
                f"可用字段 {sorted(_TOP_LEVEL_KEYS)}"
            )

        entries = raw.get("birthday", [])
        if not isinstance(entries, list):
            raise ValueError(f"生日记录 {source} 的 [[birthday]] 必须是表数组")
        birthdays = tuple(
            _parse_birthday(entry, where=f"生日记录 {source} 的第 {index} 条 [[birthday]]")
            for index, entry in enumerate(entries, start=1)
        )
        return cls(
            birthdays=birthdays,
            note=as_str(raw.get("note", ""), where=f"生日记录 {source} 的 note"),
        )


# ────────────────────────────────────────────────────────────
# 合并与排序
# ────────────────────────────────────────────────────────────


def _file_order(birthday: Birthday) -> tuple[int, int, int, str]:
    """写进文件时的顺序：主体 → 月 → 日 → key。

    纯看内容，不看输入顺序。这样 `alterego birthday add` 跑两次、或者手工把
    文件里的两条对调，得到的文件都一模一样——`git diff` 才有意义。
    """
    return (SUBJECTS.index(birthday.subject), birthday.month, birthday.day, birthday.tag)


def _union(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """按**首次出现**的顺序去重合并。

    保持首次出现的顺序而不是排序：`_take()` 是按顺序截前几件的，
    排序会把「先挑礼物、再想怎么开口」变成「先想怎么开口」——
    准备期的活动顺序是有意安排的，不能为了让集合好看就丢掉。
    """
    out: list[str] = []
    for group in groups:
        for item in group:
            if item not in out:
                out.append(item)
    return tuple(out)


def _merge_day(day: date, group: tuple[Birthday, ...]) -> Holiday:
    """同一天的若干生日 → 一条 `personal` 的 `Holiday`。

    取最大值的两处（`lead_days`、`aftermath_days`）：谁最上心就按谁的来。
    早一点想起来不会冒犯谁，晚一点会——所以合并取大不取小。

    活动表取**并集**而不是最大值那一方：同一天过生日的两个人，
    该做的两件事都要做，没有理由丢掉一件。
    """
    ordered = sorted(group, key=_file_order)
    names = "、".join(item.name for item in ordered)
    return Holiday(
        id="birthday:" + "+".join(sorted(item.tag for item in ordered)),
        name=f"{names}的生日",
        kind="personal",
        day=day,
        # 生日不放假——这一行就是「今天要上班」的来源，不要顺手给 days_off 填值。
        days_off=(),
        lead_days=max(item.lead_days for item in ordered),
        aftermath_days=max(item.aftermath_days for item in ordered),
        verified=all(item.verified for item in ordered),
        note="；".join(item.note for item in ordered if item.note),
        prep_activities=_union(*(item.activities_for("anticipating") for item in ordered)),
        during_activities=_union(*(item.activities_for("during") for item in ordered)),
        aftermath_activities=_union(*(item.activities_for("aftermath") for item in ordered)),
    )


# ────────────────────────────────────────────────────────────
# TOML 解析
# ────────────────────────────────────────────────────────────

_TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset({"note", "birthday"})

_BIRTHDAY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "subject",
        "name",
        "month",
        "day",
        "npc_id",
        "lead_days",
        "aftermath_days",
        "verified",
        "note",
        "prep_activities",
        "during_activities",
        "aftermath_activities",
    }
)


def _parse_birthday(raw: Any, *, where: str) -> Birthday:
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 必须是一张表（TOML 里写作 [[birthday]]）")

    unknown = sorted(set(raw) - _BIRTHDAY_KEYS)
    if unknown:
        raise ValueError(f"{where} 有无法识别的字段：{unknown}；可用字段 {sorted(_BIRTHDAY_KEYS)}")
    for required in ("subject", "name", "month", "day"):
        if required not in raw:
            raise ValueError(f"{where} 缺少必填字段 {required}")

    subject = raw["subject"]
    if subject not in SUBJECTS:
        raise ValueError(f"{where} 的 subject 必须是 {list(SUBJECTS)} 之一，收到 {subject!r}")

    return Birthday(
        subject=subject,
        name=as_str(raw["name"], where=f"{where} 的 name"),
        month=as_int(raw["month"], where=f"{where} 的 month"),
        day=as_int(raw["day"], where=f"{where} 的 day"),
        # 省略提前量就按主体的默认值补——补出来的是**具体数字**，
        # 对象里不留「按约定回退」这种状态。
        lead_days=as_int(
            raw.get("lead_days", DEFAULT_LEAD_DAYS[subject]), where=f"{where} 的 lead_days"
        ),
        aftermath_days=as_int(
            raw.get("aftermath_days", DEFAULT_AFTERMATH_DAYS[subject]),
            where=f"{where} 的 aftermath_days",
        ),
        npc_id=as_str(raw.get("npc_id", ""), where=f"{where} 的 npc_id"),
        verified=as_bool(raw.get("verified", True), where=f"{where} 的 verified"),
        note=as_str(raw.get("note", ""), where=f"{where} 的 note"),
        prep_activities=as_strs(
            raw.get("prep_activities", []), where=f"{where} 的 prep_activities"
        ),
        during_activities=as_strs(
            raw.get("during_activities", []), where=f"{where} 的 during_activities"
        ),
        aftermath_activities=as_strs(
            raw.get("aftermath_activities", []), where=f"{where} 的 aftermath_activities"
        ),
    )

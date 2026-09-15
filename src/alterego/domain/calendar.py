"""节日日历与「节前准备期」——纯函数。

**为什么节日必须查表而不是算出来**：春节 / 元宵 / 端午 / 中秋是农历，
清明是节气。标准库没有农历，为了七个日期引一个农历库不划算（P5）。

**为什么放假安排必须是数据而不能是算法**：三件事性质完全不同——
放假**天数**是《全国年节及纪念日放假办法》的条文（法条），
节日**日期**是农历/节气推算（天文事实），
而「哪一天补班」是国务院办公厅每年一发的行政公告（行政决定）。
行政决定不是天文事实，任何算法都推不出来，
所以只能像 `defaults.toml` 一样当作随包数据维护，并且**缺数据时显式降级**。

**平滑过渡靠 `intensity` 这一个连续量**：节前 `lead_days` 天里它从接近 0 爬到接近 1，
节后 `aftermath_days` 天里衰减回 0。日程生成器用它决定「今天往计划里掺几件准备活动」，
于是 D-3 和 D-2 的日程只差一点，而不是到节日当天突然换成另一份模板。
这就是「不要突然过渡到节日」的机制——它是一条曲线，不是一句提示词（P3）。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, Literal, get_args


__all__ = [
    "DAY_KINDS",
    "DAY_KIND_LABELS",
    "HOLIDAY_KINDS",
    "HOLIDAY_KIND_LABELS",
    "HOLIDAY_PHASES",
    "HOLIDAY_PHASE_LABELS",
    "MAX_AFTERMATH_DAYS",
    "MAX_LEAD_DAYS",
    "MIN_LEAD_DAYS",
    "PREP_THRESHOLD",
    "DayKind",
    "Holiday",
    "HolidayCalendar",
    "HolidayContext",
    "HolidayKind",
    "HolidayPhase",
]

# ────────────────────────────────────────────────────────────
# 枚举
# ────────────────────────────────────────────────────────────

DayKind = Literal["workday", "weekend", "holiday", "makeup_workday"]
"""这一天是什么日子。

- `workday` 普通工作日
- `weekend` 普通周末
- `holiday` 法定放假日
- `makeup_workday` **调休补班日**——通常是周六周日但要上班。
  这是朴素实现最容易漏掉的一格：只看 `weekday()` 会把它当成休息日。
"""

HolidayKind = Literal["statutory", "traditional", "western", "personal"]
"""节日的来源。

- `statutory` 法定放假（元旦 / 春节 / 清明 / 劳动节 / 端午 / 中秋 / 国庆）
- `traditional` 传统节日但不放假（元宵）
- `western` 洋节（情人节 / 圣诞）
- `personal` 只属于这个角色自己的日子（生日 / 纪念日），存在 `persona` / `world` 里
"""

HolidayPhase = Literal["none", "anticipating", "during", "aftermath"]
"""现在处在节日的哪个阶段。

`anticipating` 就是「提前几天就知道过几天是节日」的那个窗口。
"""

DAY_KINDS: Final[tuple[str, ...]] = get_args(DayKind)
HOLIDAY_KINDS: Final[tuple[str, ...]] = get_args(HolidayKind)
HOLIDAY_PHASES: Final[tuple[str, ...]] = get_args(HolidayPhase)

DAY_KIND_LABELS: Final[dict[DayKind, str]] = {
    "workday": "工作日",
    "weekend": "周末",
    "holiday": "放假",
    "makeup_workday": "调休上班",
}
"""给人看的日型名称。

放在领域层而不是 CLI，是因为 Web 与 CLI 会各写一份、然后慢慢不一致
（`EMOTION_LABELS` 同理）。表里每个成员都必须有标签，
`test_domain_calendar.py` 会逐个检查——漏一个就是界面上一个 `None`。
"""

HOLIDAY_PHASE_LABELS: Final[dict[HolidayPhase, str]] = {
    "none": "平常",
    "anticipating": "节前",
    "during": "过节",
    "aftermath": "节后",
}

HOLIDAY_KIND_LABELS: Final[dict[HolidayKind, str]] = {
    "statutory": "法定",
    "traditional": "传统",
    "western": "西方",
    "personal": "个人",
}

MIN_LEAD_DAYS: Final[int] = 1
"""提前量下限。至少要提前一天，否则「提前安排」无从谈起。"""

MAX_LEAD_DAYS: Final[int] = 30
"""提前量上限。超过一个月就不叫「过几天是节日」了，只会让日程永远是节前。"""

MAX_AFTERMATH_DAYS: Final[int] = 14

PREP_THRESHOLD: Final[float] = 0.35
"""强度跨过它才把准备活动排进日程。

于是「知道有这个节日」和「开始为它做事」是两个时点——
`lead_days` 的外沿只是知道，往里走几天才开始安排。
"""

MIN_YEAR: Final[int] = 1900
MAX_YEAR: Final[int] = 2100


# ────────────────────────────────────────────────────────────
# 强度曲线
# ────────────────────────────────────────────────────────────


def _smoothstep(t: float) -> float:
    """平滑阶跃：0→0，1→1，两端一阶导为 0，所以起步和收尾都不突兀。"""
    return t * t * (3.0 - 2.0 * t)


def _ramp_up(days_until: int, lead_days: int) -> float:
    """节前强度。

    `days_until == lead_days` 那天起步值就**大于 0**（否则「知道了」等于没知道），
    而 `days_until == 1` 那天仍然**小于 1**（这样节前永远不会混同于节日当天）。
    """
    t = (lead_days - days_until + 1) / (lead_days + 1)
    return _smoothstep(t)


def _ramp_down(days_since: int, aftermath_days: int) -> float:
    """节后强度：刚过完最接近 1，之后衰减到 0。"""
    t = 1.0 - days_since / (aftermath_days + 1)
    return _smoothstep(t)


def _take(activities: tuple[str, ...], intensity: float) -> tuple[str, ...]:
    """按强度决定今天该掺几件事；一旦跨过准备阈值，至少给一件。"""
    if not activities:
        return ()
    count = int(intensity * len(activities) + 0.5)
    if count == 0 and intensity >= PREP_THRESHOLD:
        count = 1
    return activities[: min(count, len(activities))]


# ────────────────────────────────────────────────────────────
# 节日
# ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Holiday:
    """一个节日。

    `day` 是**锚点**（节日当天，通常是放假第一天或传统上过节那天），
    `days_off` 是实际放假的日期。两者分开是因为锚点是事实，放假区间是安排：
    公告没出来时 `days_off` 留空，日历会退化成「只放锚点那天」，而不是猜一个区间。
    """

    id: str
    name: str
    kind: HolidayKind
    day: date
    days_off: tuple[date, ...] = ()
    makeup_days: tuple[date, ...] = ()
    lead_days: int = 3
    aftermath_days: int = 1
    verified: bool = False
    """这条数据是否已人工核对。

    公历固定日期（元旦 / 劳动节 / 国庆）和「不放假」的节日可以为 true；
    农历与节气推算的日期应当先核对再改 true。
    """
    note: str = ""
    prep_activities: tuple[str, ...] = ()
    during_activities: tuple[str, ...] = ()
    aftermath_activities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("节日的 id 不能为空")
        if not self.name.strip():
            raise ValueError("节日的 name 不能为空")
        if self.kind not in HOLIDAY_KINDS:
            raise ValueError(f"未知的节日类别：{self.kind}")
        if not MIN_YEAR <= self.day.year <= MAX_YEAR:
            raise ValueError(f"节日「{self.name}」的日期 {self.day} 超出 {MIN_YEAR}~{MAX_YEAR} 年")
        if not MIN_LEAD_DAYS <= self.lead_days <= MAX_LEAD_DAYS:
            raise ValueError(
                f"节日「{self.name}」的提前量必须在 {MIN_LEAD_DAYS}~{MAX_LEAD_DAYS} 天之间，"
                f"收到 {self.lead_days}"
            )
        if not 0 <= self.aftermath_days <= MAX_AFTERMATH_DAYS:
            raise ValueError(
                f"节日「{self.name}」的消退期必须在 0~{MAX_AFTERMATH_DAYS} 天之间，"
                f"收到 {self.aftermath_days}"
            )
        if len(set(self.days_off)) != len(self.days_off):
            raise ValueError(f"节日「{self.name}」的 days_off 有重复日期")
        if len(set(self.makeup_days)) != len(self.makeup_days):
            raise ValueError(f"节日「{self.name}」的 makeup_days 有重复日期")
        both = sorted(set(self.days_off) & set(self.makeup_days))
        if both:
            raise ValueError(f"节日「{self.name}」同一天既放假又补班：{both}")
        if self.days_off and self.day not in self.days_off:
            raise ValueError(
                f"节日「{self.name}」的锚点 {self.day} 不在放假区间 {list(self.days_off)} 里——"
                "要么把它补进 days_off，要么把 day 改成区间内的日子"
            )

    @property
    def span(self) -> tuple[date, ...]:
        """实际放假的日期。未核实放假区间时退化为「只有锚点那天」。"""
        return self.days_off or (self.day,)

    @property
    def start(self) -> date:
        return min(self.span)

    @property
    def end(self) -> date:
        return max(self.span)

    @property
    def length_days(self) -> int:
        return (self.end - self.start).days + 1

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end


# ────────────────────────────────────────────────────────────
# 上下文
# ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HolidayContext:
    """「此刻离某个节日有多近」——感知层直接用它填 `Percepts`。

    `days_until` 一律以**假期开始**为基准：节前为正，假期中为 0，节后为负。
    """

    holiday: Holiday | None = None
    phase: HolidayPhase = "none"
    days_until: int | None = None
    intensity: float = 0.0
    activities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.phase not in HOLIDAY_PHASES:
            raise ValueError(f"未知的节日阶段：{self.phase}")
        if not 0.0 <= self.intensity <= 1.0:
            raise ValueError(f"intensity 必须在 0~1 之间，收到 {self.intensity}")
        if (self.holiday is None) != (self.phase == "none"):
            raise ValueError("holiday 为空时 phase 必须是 none；phase 不是 none 时必须给出 holiday")
        if self.phase == "none":
            if self.days_until is not None:
                raise ValueError("不在任何节日附近时不应该有 days_until")
            if self.intensity != 0.0 or self.activities:
                raise ValueError("不在任何节日附近时 intensity 必须是 0、activities 必须为空")
        elif self.days_until is None:
            raise ValueError(f"{self.phase} 阶段必须给出 days_until")

    @property
    def is_active(self) -> bool:
        return self.holiday is not None

    @property
    def is_preparing(self) -> bool:
        """是否已经到了「该往日程里安排准备活动」的程度。"""
        return self.phase == "anticipating" and self.intensity >= PREP_THRESHOLD


# ────────────────────────────────────────────────────────────
# 日历
# ────────────────────────────────────────────────────────────

_TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset({"confirmed", "note", "holiday"})

_HOLIDAY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "name",
        "kind",
        "day",
        "days_off",
        "makeup_days",
        "lead_days",
        "aftermath_days",
        "verified",
        "note",
        "prep_activities",
        "during_activities",
        "aftermath_activities",
    }
)


@dataclass(frozen=True, slots=True)
class HolidayCalendar:
    """一份（或多份合并后的）节日日历。

    日历**只回答它装着的年份**：拿别的年份问 `day_kind()` 会直接报错，
    而不是默默按「没有节日」处理。跨年时请把两份日历 `merge()` 起来——
    「12 月 29 日知道 1 月 1 日放假」正是需要跨年视野的场景。
    """

    holidays: tuple[Holiday, ...]
    confirmed: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if not self.holidays:
            raise ValueError("日历里一个节日都没有——这份数据多半是空的")

        seen: set[tuple[str, int]] = set()
        used: dict[date, str] = {}
        for holiday in self.holidays:
            key = (holiday.id, holiday.day.year)
            if key in seen:
                raise ValueError(f"节日的 (id, 年份) 重复：{key}")
            seen.add(key)
            for moment in holiday.span:
                if moment in used:
                    raise ValueError(
                        f"{moment} 同时被「{used[moment]}」和「{holiday.name}」放假——"
                        "同一天不能属于两个节日，请合并或调整日期"
                    )
                used[moment] = holiday.name

        for holiday in self.holidays:
            for moment in holiday.makeup_days:
                if moment in used:
                    raise ValueError(
                        f"{moment} 已经算「{used[moment]}」了，不能再算「{holiday.name}」的补班日"
                    )
                used[moment] = f"{holiday.name}（补班）"

    # ── 年份 ────────────────────────────────────────────────

    @property
    def years(self) -> frozenset[int]:
        """这份日历覆盖了哪些年份。"""
        out: set[int] = set()
        for holiday in self.holidays:
            for moment in (*holiday.span, *holiday.makeup_days):
                out.add(moment.year)
        return frozenset(out)

    def covers_year(self, year: int) -> bool:
        return year in self.years

    def _require_year(self, day: date) -> None:
        if not self.covers_year(day.year):
            raise ValueError(
                f"这份日历只有 {sorted(self.years)} 年的数据，回答不了 {day}——"
                "请补一份 holidays/<年份>.toml，不要拿相邻年份的调休安排硬套"
            )

    # ── 查询 ────────────────────────────────────────────────

    def day_kind(self, day: date) -> DayKind:
        """这一天是工作日、周末、假日还是**补班日**。

        优先级：补班 > 放假 > 周末。
        补班日通常落在周六周日，所以必须先判——否则会把它当成休息日，
        而「调休上班」恰恰是朴素实现最容易忽略的一格。
        """
        self._require_year(day)
        if any(day in holiday.makeup_days for holiday in self.holidays):
            return "makeup_workday"
        if any(holiday.covers(day) for holiday in self.holidays):
            return "holiday"
        return "weekend" if day.weekday() >= 5 else "workday"

    def find(self, day: date) -> Holiday | None:
        """这一天正在过哪个节日（`Holiday.span` 之内）。"""
        self._require_year(day)
        return next((holiday for holiday in self.holidays if holiday.covers(day)), None)

    def context(self, day: date) -> HolidayContext:
        """`day` 处在节日的哪个阶段、强度多少、今天该做什么。

        选择规则：**强度最大者主导这一天**，而不是「节前永远压过节后」。

        为什么不能按阶段排序：9 月 26 日是中秋次日（余温约 0.5），同日又是国庆的
        第 5 天前（刚起步约 0.3）。若让节前无条件胜出，中秋的「月饼还剩一堆」会被
        五天外的国庆顶掉——「刚过完」被「还早着」盖住，正是要避免的突然过渡。

        强度是按**离今天多近**算出来的，所以「按强度选」等于「按远近选」：
        节前爬升到压过余温时自然交接，两头都不会断。这也让「不要突然过渡」
        在时间轴的两个方向上是同一条规则，而不是两条特例。

        `during` 的强度恒为 1.0，必然胜出，无需特判。
        平手时先看阶段序（过节 > 节前 > 节后），再按离今天更近，
        保证同一天同一份日历永远给同一个答案（P6）。
        """
        self._require_year(day)

        # (强度, 阶段序, 距离, 结果)——四元组的前三段都确定，所以 max 的结果唯一。
        candidates: list[tuple[float, int, int, HolidayContext]] = []
        for holiday in self.holidays:
            if holiday.covers(day):
                candidates.append(
                    (
                        1.0,
                        0,
                        0,
                        HolidayContext(
                            holiday=holiday,
                            phase="during",
                            days_until=0,
                            intensity=1.0,
                            activities=holiday.during_activities,
                        ),
                    )
                )
                continue

            gap = (holiday.start - day).days
            if 1 <= gap <= holiday.lead_days:
                intensity = _ramp_up(gap, holiday.lead_days)
                candidates.append(
                    (
                        intensity,
                        1,
                        gap,
                        HolidayContext(
                            holiday=holiday,
                            phase="anticipating",
                            days_until=gap,
                            intensity=intensity,
                            activities=_take(holiday.prep_activities, intensity),
                        ),
                    )
                )
                continue

            since = (day - holiday.end).days
            if 1 <= since <= holiday.aftermath_days:
                intensity = _ramp_down(since, holiday.aftermath_days)
                candidates.append(
                    (
                        intensity,
                        2,
                        since,
                        HolidayContext(
                            holiday=holiday,
                            phase="aftermath",
                            days_until=-since,
                            intensity=intensity,
                            activities=_take(holiday.aftermath_activities, intensity),
                        ),
                    )
                )

        if not candidates:
            return HolidayContext()
        return max(candidates, key=lambda item: (item[0], -item[1], -item[2]))[3]

    def upcoming(self, day: date, *, within_days: int = 14) -> tuple[Holiday, ...]:
        """`day` 起 `within_days` 天内（含当天）会过的节日，按先后排序。

        这是「提前几天就知道过几天是节日」的原始形态：它不看 `lead_days`，
        只回答「有没有」；排不排准备活动是 `context()` 的事。
        """
        self._require_year(day)
        if within_days < 0:
            raise ValueError("within_days 不能为负")
        ahead = [
            ((holiday.start - day).days, holiday)
            for holiday in self.holidays
            if 0 <= (holiday.start - day).days <= within_days
        ]
        ahead.sort(key=lambda pair: pair[0])
        return tuple(holiday for _, holiday in ahead)

    def unverified(self) -> tuple[Holiday, ...]:
        """还没人工核对过的节日。CLI 用它提醒用户去改数据文件，而不是猜日期。"""
        return tuple(holiday for holiday in self.holidays if not holiday.verified)

    # ── 组装 ────────────────────────────────────────────────

    @classmethod
    def merge(cls, *calendars: HolidayCalendar) -> HolidayCalendar:
        """合并多份日历，供跨年查询使用。

        `confirmed` 取与集：只要有一年没核对，合并结果就不能算核对过。
        """
        if not calendars:
            raise ValueError("至少要给一份日历才能合并")
        holidays: list[Holiday] = []
        for calendar in calendars:
            holidays.extend(calendar.holidays)
        return cls(
            holidays=tuple(holidays),
            confirmed=all(calendar.confirmed for calendar in calendars),
            note="；".join(calendar.note for calendar in calendars if calendar.note),
        )

    @classmethod
    def from_toml(cls, text: str, *, source: str = "<toml>") -> HolidayCalendar:
        """从 TOML 文本解析。**只解析文本，不读文件**——IO 由调用方负责。

        数据文件长什么样见 `src/alterego/holidays/2026.toml`。
        字段名写错会被挡下来：这是给人手改的文件，静默忽略拼写错误代价太大。
        """
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"节日数据 {source} 不是合法的 TOML：{exc}") from exc

        unknown = sorted(set(raw) - _TOP_LEVEL_KEYS)
        if unknown:
            raise ValueError(
                f"节日数据 {source} 有无法识别的顶层字段：{unknown}；可用字段 {sorted(_TOP_LEVEL_KEYS)}"
            )

        entries = raw.get("holiday")
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"节日数据 {source} 至少要有一个 [[holiday]] 表数组")
        holidays = tuple(
            _parse_holiday(entry, where=f"节日数据 {source} 的第 {index} 条 [[holiday]]")
            for index, entry in enumerate(entries, start=1)
        )

        confirmed = raw.get("confirmed", False)
        if not isinstance(confirmed, bool):
            raise ValueError(f"节日数据 {source} 的 confirmed 必须是布尔值")
        note = raw.get("note", "")
        if not isinstance(note, str):
            raise ValueError(f"节日数据 {source} 的 note 必须是字符串")

        return cls(holidays=holidays, confirmed=confirmed, note=note)


# ────────────────────────────────────────────────────────────
# TOML 解析
# ────────────────────────────────────────────────────────────


def _parse_holiday(raw: Any, *, where: str) -> Holiday:
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 必须是一张表（TOML 里写作 [[holiday]]）")

    unknown = sorted(set(raw) - _HOLIDAY_KEYS)
    if unknown:
        raise ValueError(f"{where} 有无法识别的字段：{unknown}；可用字段 {sorted(_HOLIDAY_KEYS)}")
    for required in ("id", "name", "kind", "day"):
        if required not in raw:
            raise ValueError(f"{where} 缺少必填字段 {required}")

    kind = raw["kind"]
    if kind not in HOLIDAY_KINDS:
        raise ValueError(f"{where} 的 kind 必须是 {list(HOLIDAY_KINDS)} 之一，收到 {kind!r}")

    return Holiday(
        id=_as_str(raw["id"], where=f"{where} 的 id"),
        name=_as_str(raw["name"], where=f"{where} 的 name"),
        kind=kind,
        day=_as_date(raw["day"], where=f"{where} 的 day"),
        days_off=_as_dates(raw.get("days_off", []), where=f"{where} 的 days_off"),
        makeup_days=_as_dates(raw.get("makeup_days", []), where=f"{where} 的 makeup_days"),
        lead_days=_as_int(raw.get("lead_days", 3), where=f"{where} 的 lead_days"),
        aftermath_days=_as_int(raw.get("aftermath_days", 1), where=f"{where} 的 aftermath_days"),
        verified=_as_bool(raw.get("verified", False), where=f"{where} 的 verified"),
        note=_as_str(raw.get("note", ""), where=f"{where} 的 note"),
        prep_activities=_as_strs(
            raw.get("prep_activities", []), where=f"{where} 的 prep_activities"
        ),
        during_activities=_as_strs(
            raw.get("during_activities", []), where=f"{where} 的 during_activities"
        ),
        aftermath_activities=_as_strs(
            raw.get("aftermath_activities", []), where=f"{where} 的 aftermath_activities"
        ),
    )


def _as_str(value: Any, *, where: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{where} 必须是字符串，收到 {value!r}")
    return value


def _as_bool(value: Any, *, where: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{where} 必须是布尔值，收到 {value!r}")
    return value


def _as_int(value: Any, *, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where} 必须是整数，收到 {value!r}")
    return value


def _as_date(value: Any, *, where: str) -> date:
    # TOML 的日期是裸写的（`day = 2026-02-17`），tomllib 直接给 date 对象。
    # 注意 datetime 是 date 的子类，要排掉——带时间的写法说明作者想错了。
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError(f"{where} 必须是 TOML 日期，写成 `{where.split()[-1]} = 2026-02-17` 这样")
    return value


def _as_dates(value: Any, *, where: str) -> tuple[date, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where} 必须是日期数组，收到 {value!r}")
    return tuple(_as_date(item, where=f"{where}[{index}]") for index, item in enumerate(value))


def _as_strs(value: Any, *, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where} 必须是字符串数组，收到 {value!r}")
    return tuple(_as_str(item, where=f"{where}[{index}]") for index, item in enumerate(value))

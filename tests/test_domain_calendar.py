"""`alterego.domain.calendar` 的单元测试。

这个文件里**故意不用** `src/alterego/holidays/2026.toml`：
那份数据会年年改，而规则不该跟着改。这里用自己搭的日历，
`tests/test_holiday_data.py` 单独校验随包数据本身。

重点钉住三件事：

1. **`makeup_workday` 不会被当成周末**。这是调休最容易被朴素实现漏掉的一格。
2. **强度是一条连续曲线**：节前外沿就有信号、节前永远不等于节日当天。
   这就是「不要突然过渡到节日」本身——这条性质一破，过渡就退化成切换。
   而两个节日的窗口交叠时，**支配今天的是强度最大的那个**，不是阶段序——
   否则刚过完的节会在第二天被节日顶掉，又变回切换。
3. **拿错年份要报错**，不能默默按「没有节日」处理。

依据: docs/design/12-calendar-and-conversation.md
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from alterego.domain.calendar import (
    DAY_KIND_LABELS,
    DAY_KINDS,
    HOLIDAY_KIND_LABELS,
    HOLIDAY_KINDS,
    HOLIDAY_PHASE_LABELS,
    HOLIDAY_PHASES,
    MAX_AFTERMATH_DAYS,
    MAX_LEAD_DAYS,
    PREP_THRESHOLD,
    Holiday,
    HolidayCalendar,
    HolidayContext,
)


# ────────────────────────────────────────────────────────────
# 自建的样本日历
# ────────────────────────────────────────────────────────────

SPRING_FESTIVAL = Holiday(
    id="spring_festival",
    name="春节",
    kind="statutory",
    day=date(2026, 2, 17),
    days_off=(date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19)),
    lead_days=4,
    aftermath_days=2,
    verified=True,
    prep_activities=("抢回家的票", "买年货", "收行李", "跟同事说一声"),
    during_activities=("回老家", "走亲戚"),
    aftermath_activities=("返程", "缓一缓"),
)

MID_AUTUMN = Holiday(
    id="mid_autumn",
    name="中秋节",
    kind="statutory",
    day=date(2026, 9, 25),
    days_off=(date(2026, 9, 25),),
    lead_days=6,
    aftermath_days=1,
    verified=True,
    prep_activities=("挑月饼", "看回家的票", "跟朋友约"),
    during_activities=("吃月饼", "赏月"),
    aftermath_activities=("月饼还剩一堆",),
)

CALENDAR = HolidayCalendar(holidays=(SPRING_FESTIVAL, MID_AUTUMN), confirmed=True)

#: 春节的节前四天：gap 分别是 4、3、2、1
BEFORE_SPRING = (date(2026, 2, 12), date(2026, 2, 13), date(2026, 2, 14), date(2026, 2, 15))


def _minimal(**overrides: object) -> dict[str, object]:
    """只保证必填字段的最小合法构造参数。"""
    base: dict[str, object] = {
        "id": "x",
        "name": "测试节",
        "kind": "statutory",
        "day": date(2026, 5, 1),
    }
    return {**base, **overrides}


def test_the_sample_calendar_is_itself_valid() -> None:
    """先确认样本没问题，后面挂掉才一定是被测代码的问题。"""
    assert CALENDAR.years == frozenset({2026})
    assert CALENDAR.confirmed is True


# ────────────────────────────────────────────────────────────
# 标签表 · 界面上的字
# ────────────────────────────────────────────────────────────


def test_every_day_kind_has_a_label() -> None:
    assert set(DAY_KIND_LABELS) == set(DAY_KINDS)


def test_every_phase_has_a_label() -> None:
    assert set(HOLIDAY_PHASE_LABELS) == set(HOLIDAY_PHASES)


def test_every_holiday_kind_has_a_label() -> None:
    assert set(HOLIDAY_KIND_LABELS) == set(HOLIDAY_KINDS)


def test_no_label_is_left_empty() -> None:
    """漏一个标签就是界面上一个 `None`；漏一个空字符串是界面上一个洞。"""
    for table in (DAY_KIND_LABELS, HOLIDAY_PHASE_LABELS, HOLIDAY_KIND_LABELS):
        assert all(isinstance(value, str) and value for value in table.values())


# ────────────────────────────────────────────────────────────
# Holiday · 构造与校验
# ────────────────────────────────────────────────────────────


def test_span_is_inferred_from_the_days_off() -> None:
    assert SPRING_FESTIVAL.start == date(2026, 2, 16)
    assert SPRING_FESTIVAL.end == date(2026, 2, 19)
    assert SPRING_FESTIVAL.length_days == 4


def test_without_days_off_the_span_is_just_the_anchor() -> None:
    """公告没出来就不能猜一个区间，否则猜出来的日子会被当成真的。"""
    lantern = Holiday(id="lantern", name="元宵节", kind="traditional", day=date(2026, 3, 3))
    assert lantern.span == (date(2026, 3, 3),)
    assert lantern.length_days == 1
    assert lantern.days_off == ()


def test_covers_includes_both_ends() -> None:
    assert SPRING_FESTIVAL.covers(date(2026, 2, 16))
    assert SPRING_FESTIVAL.covers(date(2026, 2, 19))
    assert not SPRING_FESTIVAL.covers(date(2026, 2, 15))
    assert not SPRING_FESTIVAL.covers(date(2026, 2, 20))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "   "),
        ("name", "   "),
        ("kind", "moon_festival"),
        ("lead_days", 0),
        ("lead_days", MAX_LEAD_DAYS + 1),
        ("aftermath_days", -1),
        ("aftermath_days", MAX_AFTERMATH_DAYS + 1),
    ],
)
def test_invalid_fields_are_rejected_on_the_spot(field: str, value: object) -> None:
    """手改 TOML 写错字段必须立刻挂，不能等三个月后表现成「节日没生效」。"""
    with pytest.raises(ValueError):
        Holiday(**_minimal(**{field: value}))


def test_a_day_cannot_be_off_and_makeup_at_once() -> None:
    with pytest.raises(ValueError, match="既放假又补班"):
        Holiday(**_minimal(days_off=(date(2026, 5, 1),), makeup_days=(date(2026, 5, 1),)))


def test_the_anchor_must_fall_inside_the_days_off() -> None:
    """锚点是「节日当天」，不在放假区间里说明数据抄错了。"""
    with pytest.raises(ValueError, match="不在放假区间"):
        Holiday(**_minimal(day=date(2026, 5, 1), days_off=(date(2026, 5, 2),)))


@pytest.mark.parametrize("field", ["days_off", "makeup_days"])
def test_duplicate_dates_are_rejected(field: str) -> None:
    with pytest.raises(ValueError, match="重复日期"):
        Holiday(**_minimal(**{field: (date(2026, 4, 1), date(2026, 4, 1))}))


# ────────────────────────────────────────────────────────────
# HolidayCalendar · 构造与校验
# ────────────────────────────────────────────────────────────


def test_an_empty_calendar_is_rejected() -> None:
    with pytest.raises(ValueError, match="一个节日都没有"):
        HolidayCalendar(holidays=())


def test_ids_are_unique_within_a_year() -> None:
    twin = Holiday(id="spring_festival", name="另一个春节", kind="statutory", day=date(2026, 6, 1))
    with pytest.raises(ValueError, match="重复"):
        HolidayCalendar(holidays=(SPRING_FESTIVAL, twin))


def test_two_holidays_cannot_share_a_day_off() -> None:
    clash = Holiday(
        id="clash",
        name="撞车的节",
        kind="traditional",
        day=date(2026, 2, 18),
        days_off=(date(2026, 2, 18),),
    )
    with pytest.raises(ValueError, match="同时被"):
        HolidayCalendar(holidays=(SPRING_FESTIVAL, clash))


def test_a_day_without_days_off_does_not_claim_the_date() -> None:
    """不放假的节日可以和别的节撞在同一天。

    这条不是漏洞而是规则：它让生日（`kind="personal"`，`days_off` 恒为空）
    能直接叠加进一份节日日历，而不用先说服日历允许共存。
    如果哪天有人把「占不占这一天」改回 `span`，这个测试会挂。
    """
    clash = Holiday(id="clash", name="情人节", kind="western", day=date(2026, 2, 18))
    calendar = HolidayCalendar(holidays=(SPRING_FESTIVAL, clash))

    assert any(item.name == "情人节" for item in calendar.holidays)
    # 情人节没让春节少放一天假，也没让自己变成假日。
    assert calendar.day_kind(date(2026, 2, 18)) == "holiday"
    assert calendar.day_kind(date(2026, 2, 20)) == "workday"


def test_a_birthday_does_not_make_the_day_a_holiday() -> None:
    """生日不放假：那天原本是工作日就还是工作日。"""
    birthday = Holiday(
        id="birthday:user",
        name="你的生日",
        kind="personal",
        day=date(2026, 6, 3),
        lead_days=7,
    )
    calendar = HolidayCalendar(holidays=(birthday,))

    assert calendar.day_kind(date(2026, 6, 3)) == "workday"  # 2026-06-03 是周三
    assert calendar.day_kind(date(2026, 6, 6)) == "weekend"
    assert calendar.context(date(2026, 6, 3)).phase == "during"


def test_a_makeup_day_cannot_land_on_a_day_off() -> None:
    clash = Holiday(
        id="clash",
        name="撞车的节",
        kind="traditional",
        day=date(2026, 3, 1),
        makeup_days=(date(2026, 2, 18),),
    )
    with pytest.raises(ValueError, match="已经算"):
        HolidayCalendar(holidays=(SPRING_FESTIVAL, clash))


# ────────────────────────────────────────────────────────────
# day_kind · 调休是独立的一格
# ────────────────────────────────────────────────────────────


def test_plain_workdays_and_weekends() -> None:
    assert CALENDAR.day_kind(date(2026, 3, 4)) == "workday"  # 周三
    assert CALENDAR.day_kind(date(2026, 3, 7)) == "weekend"  # 周六
    assert CALENDAR.day_kind(date(2026, 3, 8)) == "weekend"  # 周日


def test_days_off_win_over_the_weekend() -> None:
    assert date(2026, 2, 16).weekday() == 0
    assert CALENDAR.day_kind(date(2026, 2, 16)) == "holiday"


def test_makeup_days_must_win_over_the_weekend() -> None:
    """调休上班。只看 `weekday()` 会把它当成周六——朴素实现最容易错在这一格。"""
    plain = HolidayCalendar(holidays=(SPRING_FESTIVAL,), confirmed=True)
    adjusted = HolidayCalendar(
        holidays=(replace(SPRING_FESTIVAL, makeup_days=(date(2026, 2, 21),)),),
        confirmed=True,
    )

    assert date(2026, 2, 21).weekday() == 5
    assert plain.day_kind(date(2026, 2, 21)) == "weekend"
    assert adjusted.day_kind(date(2026, 2, 21)) == "makeup_workday"


def test_the_wrong_year_raises_instead_of_reporting_no_holiday() -> None:
    with pytest.raises(ValueError, match="2026"):
        CALENDAR.day_kind(date(2027, 1, 1))


# ────────────────────────────────────────────────────────────
# context · 强度是一条连续曲线
# ────────────────────────────────────────────────────────────


def test_during_the_holiday() -> None:
    ctx = CALENDAR.context(date(2026, 2, 17))
    assert ctx.phase == "during"
    assert ctx.holiday is SPRING_FESTIVAL
    assert ctx.days_until == 0
    assert ctx.intensity == 1.0
    assert ctx.activities == SPRING_FESTIVAL.during_activities
    assert ctx.is_active
    assert not ctx.is_preparing


@pytest.mark.parametrize(("day", "gap"), list(zip(BEFORE_SPRING, (4, 3, 2, 1), strict=True)))
def test_every_lead_day_knows_which_holiday(day: date, gap: int) -> None:
    ctx = CALENDAR.context(day)
    assert ctx.phase == "anticipating"
    assert ctx.holiday is SPRING_FESTIVAL
    assert ctx.days_until == gap


def test_the_outer_edge_of_the_lead_window_already_has_a_signal() -> None:
    """「提前几天就知道」：外沿那天强度必须大于 0，否则「知道了」等于没知道。"""
    ctx = CALENDAR.context(BEFORE_SPRING[0])
    assert ctx.intensity > 0.0
    assert ctx.intensity < PREP_THRESHOLD
    assert not ctx.is_preparing


def test_outside_the_calendar_nothing_is_said() -> None:
    assert CALENDAR.context(date(2026, 3, 20)) == HolidayContext()


def test_anticipation_intensity_rises_monotonically_and_differs_every_day() -> None:
    intensities = [CALENDAR.context(day).intensity for day in BEFORE_SPRING]
    assert intensities == sorted(intensities)
    assert len(set(intensities)) == len(intensities), "每天强度都该不一样，否则曲线变成了台阶"


def test_anticipation_intensity_never_reaches_the_day_itself() -> None:
    """否则 D-1 和 D0 就没区别，「平滑过渡」也就无从谈起。"""
    eve = CALENDAR.context(BEFORE_SPRING[-1])
    assert eve.days_until == 1
    assert eve.intensity < 1.0


def test_prep_activities_appear_one_by_one() -> None:
    counts = [len(CALENDAR.context(day).activities) for day in BEFORE_SPRING]
    assert counts[0] == 0, "外沿只该知道，还不该动手"
    assert counts == sorted(counts), "越靠近节日，排上的准备活动只能更多"
    assert 0 < counts[1] < counts[-1]
    assert counts[-1] == len(SPRING_FESTIVAL.prep_activities), "到最后一天该全排上"


def test_is_preparing_only_after_crossing_the_threshold() -> None:
    assert not CALENDAR.context(BEFORE_SPRING[0]).is_preparing
    assert CALENDAR.context(BEFORE_SPRING[-1]).is_preparing


def test_aftermath_fades_out_instead_of_dropping_to_zero() -> None:
    first = CALENDAR.context(date(2026, 2, 20))
    second = CALENDAR.context(date(2026, 2, 21))
    assert first.phase == "aftermath"
    assert first.days_until == -1
    assert second.phase == "aftermath"
    assert second.days_until == -2
    assert 0.0 < second.intensity < first.intensity < 1.0


def test_after_the_aftermath_it_goes_quiet() -> None:
    assert CALENDAR.context(date(2026, 2, 22)) == HolidayContext()


def _two_holidays(*, approaching_day: date, lead_days: int) -> HolidayCalendar:
    """一个刚过完（01-01，余温 5 天）加一个快到了的，用来量「谁主导今天」。"""
    just_past = Holiday(
        id="past",
        name="刚过完的节",
        kind="traditional",
        day=date(2026, 1, 1),
        aftermath_days=5,
        verified=True,
    )
    approaching = Holiday(
        id="soon",
        name="快到了的节",
        kind="traditional",
        day=approaching_day,
        lead_days=lead_days,
        verified=True,
    )
    return HolidayCalendar(holidays=(just_past, approaching), confirmed=True)


def test_the_fading_holiday_can_outweigh_a_distant_one() -> None:
    """01-03：余温 0.74 压过五天外的 0.32。

    按阶段排序会让 5 天外的节「抢走」今天，于是刚过完的那个凭空消失——
    「月饼还剩一堆」被「国庆还早着」顶掉，就是要避免的突然过渡。
    """
    ctx = _two_holidays(approaching_day=date(2026, 1, 8), lead_days=7).context(date(2026, 1, 3))
    assert ctx.phase == "aftermath"
    assert ctx.holiday is not None
    assert ctx.holiday.id == "past"
    assert ctx.days_until == -2


def test_a_close_holiday_outweighs_a_faint_leftover() -> None:
    """换成 01-05 之后，节前爬到 0.84 反超余温 0.74——交接是**算出来的**，不是特例。"""
    ctx = _two_holidays(approaching_day=date(2026, 1, 5), lead_days=7).context(date(2026, 1, 3))
    assert ctx.phase == "anticipating"
    assert ctx.holiday is not None
    assert ctx.holiday.id == "soon"
    assert ctx.days_until == 2


# ────────────────────────────────────────────────────────────
# upcoming · 「过几天是节日」
# ────────────────────────────────────────────────────────────


def test_upcoming_looks_ahead() -> None:
    assert CALENDAR.upcoming(date(2026, 2, 13), within_days=3) == (SPRING_FESTIVAL,)


def test_today_counts_towards_the_window() -> None:
    assert CALENDAR.upcoming(date(2026, 2, 16), within_days=0) == (SPRING_FESTIVAL,)


def test_a_distant_window_sees_nothing() -> None:
    assert CALENDAR.upcoming(date(2026, 4, 1), within_days=7) == ()


def test_the_window_cannot_be_negative() -> None:
    with pytest.raises(ValueError, match="不能为负"):
        CALENDAR.upcoming(date(2026, 4, 1), within_days=-1)


# ────────────────────────────────────────────────────────────
# 跨年
# ────────────────────────────────────────────────────────────


def test_merging_sees_next_years_new_year() -> None:
    """12 月 30 日就该知道 1 月 1 日放假——这正是需要跨年视野的场景。"""
    next_new_year = Holiday(
        id="new_year",
        name="元旦",
        kind="statutory",
        day=date(2027, 1, 1),
        days_off=(date(2027, 1, 1),),
        lead_days=3,
        verified=True,
    )
    this_year = HolidayCalendar(holidays=(MID_AUTUMN,), confirmed=True)
    next_year = HolidayCalendar(holidays=(next_new_year,), confirmed=False)

    merged = HolidayCalendar.merge(this_year, next_year)

    assert merged.years == frozenset({2026, 2027})
    assert merged.confirmed is False, "只要有一年没核对，合并结果就不能算核对过"
    assert merged.upcoming(date(2026, 12, 29), within_days=5) == (next_new_year,)
    assert merged.context(date(2026, 12, 30)).holiday is next_new_year


def test_merging_needs_at_least_one_calendar() -> None:
    with pytest.raises(ValueError, match="至少"):
        HolidayCalendar.merge()


def test_merging_the_same_calendar_twice_is_blocked() -> None:
    with pytest.raises(ValueError, match="重复"):
        HolidayCalendar.merge(CALENDAR, CALENDAR)


# ────────────────────────────────────────────────────────────
# 未核对
# ────────────────────────────────────────────────────────────


def test_unverified_holidays_can_be_named() -> None:
    guessed = Holiday(id="guess", name="猜的节", kind="traditional", day=date(2026, 7, 7))
    calendar = HolidayCalendar(holidays=(SPRING_FESTIVAL, guessed), confirmed=False)
    assert calendar.unverified() == (guessed,)


# ────────────────────────────────────────────────────────────
# from_toml
# ────────────────────────────────────────────────────────────

MINIMAL_TOML = """
confirmed = false
note = "测试用"

[[holiday]]
id = "new_year"
name = "元旦"
kind = "statutory"
day = 2026-01-01
days_off = [2026-01-01]
lead_days = 2
aftermath_days = 1
verified = true
prep_activities = ["翻一下去年的记录"]
during_activities = ["睡到自然醒"]
"""


def test_a_minimal_data_file() -> None:
    calendar = HolidayCalendar.from_toml(MINIMAL_TOML, source="test.toml")

    assert len(calendar.holidays) == 1
    holiday = calendar.holidays[0]
    assert holiday.id == "new_year"
    assert holiday.name == "元旦"
    assert holiday.day == date(2026, 1, 1)
    assert holiday.days_off == (date(2026, 1, 1),)
    assert holiday.makeup_days == ()
    assert holiday.verified is True
    assert holiday.prep_activities == ("翻一下去年的记录",)
    assert calendar.note == "测试用"
    assert calendar.years == frozenset({2026})


def test_omitted_fields_fall_back_to_defaults() -> None:
    text = """
[[holiday]]
id = "x"
name = "测试节"
kind = "traditional"
day = 2026-07-07
"""
    holiday = HolidayCalendar.from_toml(text).holidays[0]
    assert holiday.days_off == ()
    assert holiday.makeup_days == ()
    assert holiday.lead_days == 3
    assert holiday.aftermath_days == 1
    assert holiday.verified is False


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("confirmed = false\n", "至少要有一个"),
        ("[[holiday]]\nid='x'\nname='y'\n", "缺少必填字段"),
        ("[[holiday]]\nid='x'\nname='y'\nkind='nope'\nday=2026-01-01\n", "kind 必须是"),
        ("[[holiday]]\nid='x'\nname='y'\nkind='statutory'\nday='2026-01-01'\n", "必须是 TOML 日期"),
        (
            "[[holiday]]\nid='x'\nname='y'\nkind='statutory'\nday=2026-01-01\nlead_days='3'\n",
            "必须是整数",
        ),
        (
            "[[holiday]]\nid='x'\nname='y'\nkind='statutory'\nday=2026-01-01\nverified=1\n",
            "必须是布尔值",
        ),
        (
            "[[holiday]]\nid='x'\nname='y'\nkind='statutory'\nday=2026-01-01\nnote=1\n",
            "必须是字符串",
        ),
        (
            "[[holiday]]\nid='x'\nname='y'\nkind='statutory'\nday=2026-01-01\nprep_activities='买'\n",
            "必须是字符串数组",
        ),
        (
            "[[holiday]]\nid='x'\nname='y'\nkind='statutory'\nday=2026-01-01\ndays_off=1\n",
            "必须是日期数组",
        ),
        (
            "[[holiday]]\nid='x'\nname='y'\nkind='statutory'\nday=2026-01-01\nlead_dayz=1\n",
            "无法识别",
        ),
        (
            "confirmed = true\nnope = 1\n"
            "[[holiday]]\nid='x'\nname='y'\nkind='statutory'\nday=2026-01-01\n",
            "顶层字段",
        ),
        (
            "confirmed = 'yes'\n[[holiday]]\nid='x'\nname='y'\nkind='statutory'\nday=2026-01-01\n",
            "必须是布尔值",
        ),
        (
            "note = 1\n[[holiday]]\nid='x'\nname='y'\nkind='statutory'\nday=2026-01-01\n",
            "必须是字符串",
        ),
    ],
)
def test_bad_data_files_raise_and_say_where(text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        HolidayCalendar.from_toml(text, source="坏文件.toml")


def test_invalid_toml_names_its_source() -> None:
    with pytest.raises(ValueError, match="坏文件"):
        HolidayCalendar.from_toml("这不是 = = toml", source="坏文件.toml")


def test_duplicate_ids_in_one_file_are_blocked() -> None:
    text = """
[[holiday]]
id = "x"
name = "甲"
kind = "statutory"
day = 2026-01-01

[[holiday]]
id = "x"
name = "乙"
kind = "statutory"
day = 2026-02-01
"""
    with pytest.raises(ValueError, match="重复"):
        HolidayCalendar.from_toml(text)


# ────────────────────────────────────────────────────────────
# HolidayContext 自己的不变量
# ────────────────────────────────────────────────────────────


def test_an_empty_context_must_be_completely_clean() -> None:
    with pytest.raises(ValueError, match="days_until"):
        HolidayContext(days_until=3)
    with pytest.raises(ValueError, match="intensity"):
        HolidayContext(intensity=0.5)
    with pytest.raises(ValueError, match="activities"):
        HolidayContext(activities=("干活",))


def test_a_holiday_requires_a_phase_and_a_day_count() -> None:
    with pytest.raises(ValueError, match="必须给出 holiday"):
        HolidayContext(phase="during", days_until=0)
    with pytest.raises(ValueError, match="days_until"):
        HolidayContext(holiday=SPRING_FESTIVAL, phase="during", intensity=1.0)


def test_intensity_stays_within_the_unit_interval() -> None:
    with pytest.raises(ValueError, match="0~1"):
        HolidayContext(holiday=SPRING_FESTIVAL, phase="during", days_until=0, intensity=9.0)


def test_an_unknown_phase_is_rejected() -> None:
    with pytest.raises(ValueError, match="未知的节日阶段"):
        HolidayContext(holiday=SPRING_FESTIVAL, phase="before", days_until=1, intensity=0.5)

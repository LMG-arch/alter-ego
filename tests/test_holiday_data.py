"""校验随包的节日数据文件本身。

`tests/test_domain_calendar.py` 测的是**规则**，这里测的是**数据**。
两者必须分开：规则不随年份变，数据年年要改。

这份文件的存在意义只有一个——**让「抄错一个日期」在 CI 里挂掉**，
而不是三个月后表现成「它怎么突然说过两天是中秋」。

依据: docs/design/12-calendar-and-conversation.md
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from alterego.domain.calendar import (
    HOLIDAY_KINDS,
    HolidayCalendar,
    HolidayKind,
)


HOLIDAYS_DIR = Path(__file__).resolve().parent.parent / "src" / "alterego" / "holidays"
DATA_PATH = HOLIDAYS_DIR / "2026.toml"
YEAR = 2026

#: 《全国年节及纪念日放假办法》（2024-11 修订）规定的七个法定节日
STATUTORY_NAMES = frozenset({"元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节"})

#: 法条规定的全年法定节假日总天数。改了这里的一定是抄错了日期，或者法条真的动了。
STATUTORY_DAYS_OFF = 13


@pytest.fixture(scope="module")
def raw_text() -> str:
    return DATA_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def calendar(raw_text: str) -> HolidayCalendar:
    return HolidayCalendar.from_toml(raw_text, source=DATA_PATH.name)


def test_the_data_file_lives_in_the_package() -> None:
    """放在 `holidays/` 而不是 `domain/` —— 领域层不许读文件（红线 2）。"""
    package_root = Path(__file__).resolve().parent.parent / "src" / "alterego"
    assert DATA_PATH.is_file()
    assert DATA_PATH.parent == package_root / "holidays"


def test_the_data_file_parses(calendar: HolidayCalendar) -> None:
    assert len(calendar.holidays) >= len(STATUTORY_NAMES)


def test_the_filename_and_the_content_agree_on_the_year(calendar: HolidayCalendar) -> None:
    """锚点年份必须等于文件名。

    锚点是节日的身份，跨年的是**放假区间**（比如 12-31 就开始放假），
    两者不同——所以只钉锚点。
    """
    assert {holiday.day.year for holiday in calendar.holidays} == {YEAR}
    assert calendar.covers_year(YEAR)


def test_every_holiday_declares_its_kind(calendar: HolidayCalendar) -> None:
    for holiday in calendar.holidays:
        assert holiday.kind in HOLIDAY_KINDS, holiday.id


def test_no_statutory_holiday_is_missing(calendar: HolidayCalendar) -> None:
    """漏一个的后果是那一年它完全不过这个节，而且不会有任何报错。"""
    names = {holiday.name for holiday in calendar.holidays}
    assert names >= STATUTORY_NAMES


def test_the_total_days_off_match_the_statute(calendar: HolidayCalendar) -> None:
    """13 天是《放假办法》规定的。数字对不上就是某处 `days_off` 抄错了。"""
    total = sum(len(holiday.days_off) for holiday in calendar.holidays)
    assert total == STATUTORY_DAYS_OFF


def test_every_holiday_has_something_to_do(calendar: HolidayCalendar) -> None:
    """没有 `during_activities` 的节日，到了当天它也只是「知道放假了」而已。"""
    for holiday in calendar.holidays:
        assert holiday.during_activities, holiday.id


def test_lead_and_aftermath_windows_match_their_activities(calendar: HolidayCalendar) -> None:
    """两处不对称都说明数据漏填：

    - 写了 `lead_days` 却没有 `prep_activities` → 提前知道了但没事可做
    - 写了 `aftermath_days` 却没有 `aftermath_activities` → 节后一直空转
    """
    for holiday in calendar.holidays:
        assert bool(holiday.prep_activities) == (holiday.lead_days > 0), holiday.id
        assert bool(holiday.aftermath_activities) == (holiday.aftermath_days > 0), holiday.id


def test_spring_festival_has_the_longest_lead(calendar: HolidayCalendar) -> None:
    """整件事的重点就在春节：抢票是个硬 deadline，晚了就没了。

    如果哪天别的节日排到了春节前面，说明有人把某个 `lead_days` 抄成了 20。
    """
    longest = max(calendar.holidays, key=lambda holiday: holiday.lead_days)
    assert longest.name == "春节"
    assert longest.lead_days >= 7


def test_at_least_one_holiday_has_a_long_lead(calendar: HolidayCalendar) -> None:
    assert any(holiday.lead_days >= 5 for holiday in calendar.holidays)


def test_ids_are_stable_lowercase_ascii(calendar: HolidayCalendar) -> None:
    """`id` 会进数据库、进日志、进命令行参数，不能带中文或空格。"""
    for holiday in calendar.holidays:
        assert holiday.id.isascii(), holiday.id
        assert holiday.id.islower(), holiday.id
        assert holiday.id.replace("_", "").isalnum(), holiday.id


def test_every_holiday_carries_a_note(calendar: HolidayCalendar) -> None:
    """备注是这份数据唯一的出处说明，删掉就没人知道那个日期哪来的了。"""
    for holiday in calendar.holidays:
        assert holiday.note.strip(), holiday.id


def test_unverified_dates_must_be_flagged(calendar: HolidayCalendar) -> None:
    """`verified = false` 表示「算出来的，请对着权威日历核一遍」。

    农历、节气都属于这一类；全部标成 true 等于把这个信号丢掉。
    """
    assert calendar.unverified(), "一个待核对的都没有，说明 verified 全被写成 true 了"
    for holiday in calendar.unverified():
        assert holiday.note.strip(), holiday.id


def test_fixed_gregorian_dates_are_marked_verified(calendar: HolidayCalendar) -> None:
    """公历固定日期（元旦 / 劳动节 / 国庆）不该被标成待核对——那是白丢的可信度。"""
    fixed_days = {date(YEAR, 1, 1), date(YEAR, 5, 1), date(YEAR, 10, 1)}
    for holiday in calendar.holidays:
        if holiday.day in fixed_days:
            assert holiday.verified is True, holiday.id


def test_makeup_days_require_a_confirmed_schedule(calendar: HolidayCalendar) -> None:
    """`makeup_days` 是行政公告，推不出来。

    没核对公告却填了调休，等于把猜的日期当成规定用——那比留空危险得多。
    """
    if not calendar.confirmed:
        for holiday in calendar.holidays:
            assert holiday.makeup_days == (), holiday.id


def test_an_unconfirmed_calendar_must_say_what_is_pending(calendar: HolidayCalendar) -> None:
    """`confirmed = false` 而 `note` 为空，用户根本不知道要不要信这份数据。"""
    if not calendar.confirmed:
        assert calendar.note.strip()


def test_the_header_comment_matches_the_trust_flags(calendar: HolidayCalendar) -> None:
    """注释里点名「待核对」的节日，数据里也必须没被标成已核对。

    两边不一致时，人只读注释会以为可以放心，而代码只信数据。
    """
    unverified_names = {holiday.name for holiday in calendar.unverified()}
    assert {"春节", "清明节", "端午节", "中秋节", "元宵节"} <= unverified_names


def test_one_file_per_year() -> None:
    """`2026.toml` 和 `2026-x.toml` 同时存在时，加载顺序决定用哪份——不要留这种可能。"""
    stems = sorted(path.stem for path in HOLIDAYS_DIR.glob("*.toml"))
    assert stems, "一份节日数据都没有"
    assert all(stem.isdigit() for stem in stems), stems


def test_every_kind_has_a_real_sample(calendar: HolidayCalendar) -> None:
    """`personal`（生日这类）留给用户自己加，所以这里只要覆盖剩下三种。"""
    kinds = {holiday.kind for holiday in calendar.holidays}
    expected: set[HolidayKind] = {"statutory", "traditional", "western"}
    assert kinds >= expected

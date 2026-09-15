"""``alterego.domain.birthday`` 的测试。

生日复用的就是节日那套机制（提前量曲线、活动解锁、消退期、平手裁定），
所以这里**不重测**那套机制——它已经在 `test_domain_calendar.py` 里测过了。
这里只测生日特有的四件事：

1. **闰日**：平年没有 2 月 29 日。这是整个文件里最值得写的一条测试，
   因为不处理它只在**每四年**里坏一次（见 `date_in_year` 的 docstring）。
2. **两个人在同一天**：合并成一条，谁最上心就按谁的提前量。
3. **生日撞上节日**：强度打平时生日优先，而且那天**照常上班**。
4. **文件往返**：`from_toml` 的默认值补齐，和 `adapters` 侧渲染出来的东西
   能读回来（那条在 `tests/test_birthdays.py`）。

不读 `data/birthdays.toml`：那份文件是用户自己的，随时可能是空的或不存在。
"""

from __future__ import annotations

from datetime import date

import pytest

from alterego.domain.birthday import (
    DEFAULT_AFTERMATH_DAYS,
    DEFAULT_LEAD_DAYS,
    DEFAULT_NAME,
    PERSONAL_PHASE_LABELS,
    SUBJECT_LABELS,
    SUBJECTS,
    Birthday,
    BirthdayBook,
    check_month_day,
    date_in_year,
)
from alterego.domain.calendar import Holiday, HolidayCalendar, HolidayPhase


#: 一个**平年**。闰日的坑只在平年出现，所以这个常量在下面被反复用到。
COMMON_YEAR = 2026

#: 一个闰年。
LEAP_YEAR = 2024


def _birthday(**overrides: object) -> Birthday:
    """一条最小可用的记录。默认是用户的生日 06-03。"""
    fields: dict[str, object] = {
        "subject": "user",
        "name": "小明",
        "month": 6,
        "day": 3,
        "lead_days": DEFAULT_LEAD_DAYS["user"],
        "aftermath_days": DEFAULT_AFTERMATH_DAYS["user"],
    }
    fields.update(overrides)
    return Birthday(**fields)  # type: ignore[arg-type]


def _festival(**overrides: object) -> Holiday:
    """一个放假一天的节日，用来和生日撞车。"""
    fields: dict[str, object] = {
        "id": "national_day",
        "name": "国庆节",
        "kind": "statutory",
        "day": date(COMMON_YEAR, 10, 1),
        "days_off": (date(COMMON_YEAR, 10, 1),),
        "lead_days": 7,
        "aftermath_days": 3,
    }
    fields.update(overrides)
    return Holiday(**fields)  # type: ignore[arg-type]


class TestCheckMonthDay:
    def test_a_real_date_passes(self) -> None:
        check_month_day(6, 3)
        check_month_day(12, 31)

    def test_the_leap_day_is_a_real_date(self) -> None:
        # 探针年份是闰年，所以 02-29 合法——它只是四年才有一次，不是不存在。
        check_month_day(2, 29)

    def test_the_day_after_the_leap_day_is_not(self) -> None:
        with pytest.raises(ValueError, match="真实存在"):
            check_month_day(2, 30)

    def test_a_thirteenth_month_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="月份必须在"):
            check_month_day(13, 1)

    def test_a_zero_day_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="日期必须在"):
            check_month_day(1, 0)

    def test_april_thirty_first_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="真实存在"):
            check_month_day(4, 31)


class TestDateInYear:
    def test_a_normal_day_is_itself(self) -> None:
        assert date_in_year(COMMON_YEAR, 6, 3) == date(2026, 6, 3)

    def test_the_leap_day_in_a_leap_year_is_itself(self) -> None:
        assert date_in_year(LEAP_YEAR, 2, 29) == date(2024, 2, 29)

    def test_the_leap_day_falls_back_in_a_common_year(self) -> None:
        """**这条是整个文件里最重要的测试。**

        2026 年没有 2 月 29 日。不处理的话 `date(2026, 2, 29)` 直接抛
        `ValueError`——而 2026 年跑得好好的代码要到 2027→2028 才第一次坏，
        中间隔了整整三年，那时候已经没人记得这里有个假设了。
        """
        assert date_in_year(COMMON_YEAR, 2, 29) == date(2026, 2, 28)

    def test_the_fallback_is_earlier_not_later(self) -> None:
        # 提前一天说生日快乐，对方只会高兴；晚一天说就变成「哦对，昨天是你生日」。
        fallback = date_in_year(COMMON_YEAR, 2, 29)
        assert fallback < date(2026, 3, 1)

    def test_a_year_out_of_range_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="年份必须在"):
            date_in_year(1800, 6, 3)

    def test_a_nonexistent_date_other_than_the_leap_day_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="真实存在"):
            date_in_year(COMMON_YEAR, 2, 30)


class TestBirthdayValidation:
    def test_a_minimal_record_is_valid(self) -> None:
        assert _birthday().month_day == "06-03"

    def test_an_unknown_subject_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="未知的主体"):
            _birthday(subject="boss")

    def test_an_empty_name_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="称呼"):
            _birthday(name="   ")

    def test_an_npc_without_an_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="npc_id"):
            _birthday(subject="npc", name="张三")

    def test_a_self_birthday_with_an_npc_id_is_rejected(self) -> None:
        # 自己只有一个，身份就是 subject 本身；多给一个 id 只会让「哪个才算数」变成问题。
        with pytest.raises(ValueError, match="不该有 npc_id"):
            _birthday(subject="self", npc_id="张三")

    def test_the_leap_day_is_accepted(self) -> None:
        assert _birthday(month=2, day=29).month_day == "02-29"

    def test_the_thirty_first_of_february_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="真实存在"):
            _birthday(month=2, day=30)

    def test_a_lead_of_zero_is_rejected(self) -> None:
        # 提前量为 0 等于「当天才知道」，那正是这个功能要避免的事。
        with pytest.raises(ValueError, match="提前量"):
            _birthday(lead_days=0)

    def test_a_huge_lead_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="提前量"):
            _birthday(lead_days=99)

    def test_a_negative_aftermath_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="消退期"):
            _birthday(aftermath_days=-1)

    def test_a_zero_aftermath_is_allowed(self) -> None:
        # 普通同事的生日：当天说一句就够了，第二天不该还惦记着。
        assert _birthday(aftermath_days=0).aftermath_days == 0


class TestBirthdayIdentity:
    def test_a_non_npc_key_is_the_subject(self) -> None:
        assert _birthday().key == "user"
        assert _birthday(subject="self").key == "self"

    def test_an_npc_key_is_the_id(self) -> None:
        # 两个 NPC 都叫「张三」是常事，所以认 id 不认名字。
        assert _birthday(subject="npc", name="张三", npc_id="npc-7").key == "npc-7"

    def test_the_tag_prefixes_npcs_only(self) -> None:
        assert _birthday().tag == "user"
        assert _birthday(subject="npc", name="张三", npc_id="npc-7").tag == "npc:npc-7"

    def test_month_day_is_zero_padded(self) -> None:
        assert _birthday(month=1, day=5).month_day == "01-05"

    def test_the_label_says_whose_birthday_it_is(self) -> None:
        assert _birthday().label == "小明的生日"

    def test_every_subject_has_a_label_and_a_default_name(self) -> None:
        # 缺一个的表现是界面上印出 `None`，而不是报错。
        for subject in SUBJECTS:
            assert subject in SUBJECT_LABELS
            assert subject in DEFAULT_NAME


class TestBirthdayDates:
    def test_in_year_uses_the_current_year(self) -> None:
        assert _birthday().in_year(COMMON_YEAR) == date(2026, 6, 3)

    def test_days_until_is_zero_on_the_day(self) -> None:
        assert _birthday().days_until(date(2026, 6, 3)) == 0

    def test_days_until_counts_forward(self) -> None:
        assert _birthday().days_until(date(2026, 5, 31)) == 3

    def test_next_after_rolls_into_next_year(self) -> None:
        # 12 月 20 日问一个 1 月 5 日的生日：答案在明年。
        # 只看今年的话「下一个生日」会答成「今年已经过了」，
        # 而这个功能存在的理由恰恰是提前知道。
        record = _birthday(month=1, day=5)
        assert record.next_after(date(2026, 12, 20)) == date(2027, 1, 5)

    def test_next_after_stays_in_this_year_when_today_is_the_day(self) -> None:
        assert _birthday().next_after(date(2026, 6, 3)) == date(2026, 6, 3)

    def test_next_after_uses_the_leap_fallback(self) -> None:
        record = _birthday(month=2, day=29)
        assert record.next_after(date(2026, 1, 1)) == date(2026, 2, 28)


class TestActivities:
    def test_a_record_without_its_own_list_uses_the_subject_default(self) -> None:
        assert _birthday().activities_for("during") == (
            "今天是用户的生日",
            "今天多留一点时间给用户",
        )

    def test_a_record_with_its_own_list_uses_that(self) -> None:
        record = _birthday(during_activities=("只做这一件",))
        assert record.activities_for("during") == ("只做这一件",)

    def test_the_quiet_phase_has_no_activities(self) -> None:
        assert _birthday().activities_for("none") == ()

    def test_every_subject_covers_every_active_phase(self) -> None:
        phases: tuple[HolidayPhase, ...] = ("anticipating", "during", "aftermath")
        for subject in SUBJECTS:
            record = _birthday(
                subject=subject,
                name="某个称呼",
                npc_id="npc-1" if subject == "npc" else "",
            )
            for phase in phases:
                assert record.activities_for(phase)

    def test_personal_phase_labels_cover_every_phase(self) -> None:
        # 少一个键的表现是界面上直接印出一个 `None`。
        for phase in ("none", "anticipating", "during", "aftermath"):
            assert PERSONAL_PHASE_LABELS[phase]  # type: ignore[index]


def _other() -> Birthday:
    """同一天生日的另一个人，用来测合并。"""
    return _birthday(subject="npc", name="爸爸", npc_id="npc-1")


class TestBirthdayBook:
    def test_an_empty_book_is_valid(self) -> None:
        # 和 `HolidayCalendar` 不同：空是这里的**默认状态**，不是缺失。
        # 刚装好的实例本来就不知道任何人的生日。
        book = BirthdayBook()
        assert len(book) == 0
        assert book.as_holidays(COMMON_YEAR) == ()

    def test_a_duplicate_key_is_rejected(self) -> None:
        # 重复的 key 会让「改生日」变得没有定义：改的是哪一条？
        with pytest.raises(ValueError, match="重复"):
            BirthdayBook(birthdays=(_birthday(), _birthday(name="另一个称呼")))

    def test_two_npcs_may_share_a_name(self) -> None:
        book = BirthdayBook(
            birthdays=(
                _birthday(subject="npc", name="张三", npc_id="npc-1"),
                _birthday(subject="npc", name="张三", npc_id="npc-2"),
            )
        )
        assert len(book) == 2

    def test_find_returns_none_for_an_unknown_key(self) -> None:
        assert BirthdayBook().find("user") is None

    def test_with_record_adds_a_new_one(self) -> None:
        book = BirthdayBook().with_record(_birthday())
        assert book.find("user") is not None
        assert len(book) == 1

    def test_with_record_replaces_by_key(self) -> None:
        first = BirthdayBook().with_record(_birthday())
        second = first.with_record(_birthday(month=7, day=1))
        assert len(second) == 1
        assert second.find("user") is not None
        assert second.find("user").month_day == "07-01"  # type: ignore[union-attr]

    def test_the_file_order_ignores_the_input_order(self) -> None:
        # 顺序只看内容，不看谁先加进来。同一天的两个人在输入里换个顺序，
        # 结果必须一模一样（P6），否则 `git diff` 会被无意义的重排淹没。
        forward = BirthdayBook(birthdays=(_birthday(name="小明"), _other()))
        backward = BirthdayBook(birthdays=(_other(), _birthday(name="小明")))
        assert forward.as_holidays(COMMON_YEAR) == backward.as_holidays(COMMON_YEAR)
        assert forward.with_record(_birthday(name="小明")).birthdays == (
            backward.with_record(_birthday(name="小明")).birthdays
        )

    def test_a_load_keeps_the_file_order(self) -> None:
        # 手工整理过的文件（按家人分组之类）不该在一次读入之后就散架。
        # 写文件时才排——所以「读进来什么顺序，就是什么顺序」。
        text = (
            '[[birthday]]\nsubject = "user"\nname = "小明"\nmonth = 6\nday = 3\n'
            '[[birthday]]\nsubject = "self"\nname = "自己"\nmonth = 1\nday = 1\n'
        )
        assert [item.tag for item in BirthdayBook.from_toml(text).birthdays] == ["user", "self"]

    def test_a_write_normalises_the_order(self) -> None:
        # 写出去的一定是规范顺序（主体 → 月 → 日），哪怕读进来的是乱的。
        book = BirthdayBook(
            birthdays=(_birthday(name="小明"), _other(), _birthday(subject="self", name="自己"))
        )
        written = book.with_record(_birthday(subject="self", name="自己"))
        assert [item.tag for item in written.birthdays] == ["self", "user", "npc:npc-1"]

    def test_the_book_does_not_mutate_on_with_record(self) -> None:
        book = BirthdayBook()
        book.with_record(_birthday())
        assert len(book) == 0


class TestSortedByNext:
    def test_the_nearest_birthday_comes_first(self) -> None:
        book = BirthdayBook(
            birthdays=(
                _birthday(month=12, day=1),
                _birthday(subject="self", name="自己", month=6, day=10),
            )
        )
        tags = [item.tag for item, _ in book.sorted_by_next(date(2026, 6, 1))]
        assert tags == ["self", "user"]

    def test_it_crosses_the_year_boundary(self) -> None:
        # 12 月 31 日问：最近的生日在明年 1 月，而不是「今年已经过了」。
        book = BirthdayBook(birthdays=(_birthday(month=1, day=2),))
        pairs = book.sorted_by_next(date(2026, 12, 31))
        assert pairs[0][1] == date(2027, 1, 2)

    def test_within_days_filters(self) -> None:
        book = BirthdayBook(
            birthdays=(
                _birthday(month=6, day=5),
                _birthday(subject="self", name="自己", month=9, day=1),
            )
        )
        pairs = book.sorted_by_next(date(2026, 6, 1), within_days=10)
        assert [item.tag for item, _ in pairs] == ["user"]

    def test_a_negative_window_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="不能为负"):
            BirthdayBook().sorted_by_next(date(2026, 6, 1), within_days=-1)


class TestAsHolidays:
    def test_one_record_becomes_one_personal_holiday(self) -> None:
        holidays = BirthdayBook(birthdays=(_birthday(),)).as_holidays(COMMON_YEAR)
        assert len(holidays) == 1
        only = holidays[0]
        assert only.kind == "personal"
        assert only.day == date(2026, 6, 3)
        assert only.id == "birthday:user"
        assert only.name == "小明的生日"

    def test_a_birthday_never_takes_a_day_off(self) -> None:
        """生日不放假——**这一条决定了「那天照常上班」。**

        给它填 `days_off` 会让 `HolidayCalendar.day_kind()` 把 6 月 3 日
        报成「假日」，于是角色那天排休、不去上班。
        """
        holidays = BirthdayBook(birthdays=(_birthday(),)).as_holidays(COMMON_YEAR)
        assert holidays[0].days_off == ()

    def test_the_lead_days_come_along(self) -> None:
        holidays = BirthdayBook(birthdays=(_birthday(),)).as_holidays(COMMON_YEAR)
        assert holidays[0].lead_days == DEFAULT_LEAD_DAYS["user"]

    def test_two_on_the_same_day_become_one(self) -> None:
        # 同一天过生日本来就是同一件事，「小明、爸爸的生日」比两条并列更接近真实。
        # 名字顺序按主体排（自己 → 用户 → NPC），所以用户排在 NPC 前面。
        holidays = BirthdayBook(birthdays=(_birthday(name="小明"), _other())).as_holidays(
            COMMON_YEAR
        )
        assert len(holidays) == 1
        assert holidays[0].name == "小明、爸爸的生日"

    def test_a_merge_takes_the_larger_lead_days(self) -> None:
        # 谁最上心就按谁的来。早一点想起来不会冒犯谁，晚一点会。
        book = BirthdayBook(birthdays=(_birthday(lead_days=2), _other()))
        other = _birthday(subject="npc", name="爸爸", npc_id="npc-1", lead_days=20)
        assert (
            BirthdayBook(birthdays=(_birthday(lead_days=2), other))
            .as_holidays(COMMON_YEAR)[0]
            .lead_days
            == 20
        )
        # 反过来也取大，而不是「后加进来的那个说了算」。
        assert (
            BirthdayBook(birthdays=(other, _birthday(lead_days=2)))
            .as_holidays(COMMON_YEAR)[0]
            .lead_days
            == 20
        )
        # 两个都用默认值时，取的是两者中大的那个（用户 14 > NPC 3）。
        assert book.as_holidays(COMMON_YEAR)[0].lead_days == DEFAULT_LEAD_DAYS["user"]

    def test_a_merge_unions_the_activities_in_first_seen_order(self) -> None:
        # 顺序不能丢：`_take()` 是按顺序截前几件的，
        # 「先挑礼物、再想怎么开口」和反过来是两回事。
        book = BirthdayBook(
            birthdays=(
                _birthday(prep_activities=("甲先做", "都做的")),
                _birthday(
                    subject="npc",
                    name="爸爸",
                    npc_id="npc-1",
                    prep_activities=("都做的", "乙后做"),
                ),
            )
        )
        merged = book.as_holidays(COMMON_YEAR)[0]
        assert merged.prep_activities == ("甲先做", "都做的", "乙后做")

    def test_the_id_does_not_depend_on_the_year(self) -> None:
        book = BirthdayBook(birthdays=(_birthday(),))
        assert book.as_holidays(2026)[0].id == book.as_holidays(2027)[0].id

    def test_the_merge_is_deterministic(self) -> None:
        # 同一天的两个人在输入里换个顺序，结果必须一样（P6）。
        forward = BirthdayBook(birthdays=(_birthday(name="小明"), _other()))
        backward = BirthdayBook(birthdays=(_other(), _birthday(name="小明")))
        assert forward.as_holidays(COMMON_YEAR) == backward.as_holidays(COMMON_YEAR)

    def test_an_unverified_record_makes_the_merged_one_unverified(self) -> None:
        unsure = _birthday(subject="npc", name="爸爸", npc_id="npc-1", verified=False)
        book = BirthdayBook(birthdays=(_birthday(verified=True), unsure))
        assert book.as_holidays(COMMON_YEAR)[0].verified is False

    def test_records_land_in_the_right_year(self) -> None:
        # 同一个人每年都要出现一次，而不是只有文件里那一年。
        book = BirthdayBook(birthdays=(_birthday(month=1, day=5),))
        assert book.as_holidays(2027)[0].day == date(2027, 1, 5)


class TestWithAFestivalCalendar:
    """生日与真正节日放在一起时的行为。

    这一组是**接缝测试**：`as_holidays()` 吐出来的东西真的能并进
    `HolidayCalendar`，而且既有的保证（占位、`day_kind`、平手裁定）都还成立。
    """

    def _merged(self, **overrides: object) -> HolidayCalendar:
        festival = HolidayCalendar(holidays=(_festival(),), confirmed=False)
        book = BirthdayBook(birthdays=(_birthday(**overrides),))
        return HolidayCalendar.merge(festival, HolidayCalendar(holidays=book.as_holidays(2026)))

    def _tie(self) -> HolidayCalendar:
        """生日和国庆节撞在同一天。"""
        return self._merged(month=10, day=1)

    def test_the_same_day_can_hold_both(self) -> None:
        # 生日不放假，所以它不占这一天的名额——没有人需要二选一。
        assert len(self._tie().holidays) == 2

    def test_a_birthday_does_not_turn_the_day_into_a_holiday(self) -> None:
        own = HolidayCalendar(holidays=BirthdayBook(birthdays=(_birthday(),)).as_holidays(2026))
        assert own.day_kind(date(2026, 6, 3)) == "workday"
        assert own.day_kind(date(2026, 6, 6)) == "weekend"

    def test_a_merged_calendar_stays_unconfirmed(self) -> None:
        # 生日那份给自己 confirmed=True，但节日那份是 false——
        # 合并必须取与集，否则「数据未核对」这句提醒会凭空消失。
        assert self._tie().confirmed is False

    def test_a_birthday_wins_a_tie_against_a_festival(self) -> None:
        """强度完全一样时，生日优先。

        节日是所有人共享的那一天，生日是这个人独有的那一天。
        「国庆快乐」明天说后天说都还对，「生日快乐」过了那天就再也补不回来。

        ⚠ 这条盯的是 `context()` 里那个负号。少个负号不会报错、也不会变空，
        只会安静地选出「另一条节日」——一个说得通但不对的答案。
        """
        context = self._tie().context(date(2026, 10, 1))
        assert context.holiday is not None
        assert context.holiday.kind == "personal"

    def test_the_birthday_is_still_findable_on_its_own(self) -> None:
        found = self._tie().find(date(2026, 10, 1))
        assert found is not None
        assert found.name == "小明的生日"

    def test_the_festival_is_still_listed(self) -> None:
        assert "国庆节" in [item.name for item in self._tie().holidays]

    def test_upcoming_puts_the_birthday_first_on_a_tie(self) -> None:
        upcoming = self._tie().upcoming(date(2026, 10, 1), within_days=1)
        assert [item.kind for item in upcoming] == ["personal", "statutory"]

    def test_the_birthday_is_visible_in_the_window(self) -> None:
        # 提前几天就知道——靠的就是它出现在 `upcoming` 里。
        upcoming = self._merged().upcoming(date(2026, 6, 1), within_days=14)
        assert [item.name for item in upcoming] == ["小明的生日"]

    def test_a_past_birthday_is_not_in_the_window(self) -> None:
        # 6 月 3 日已经过去了，10 月 1 日不该再看见它——
        # 「生日后还惦记几天」由 `aftermath_days` 管，不由这个窗口管。
        assert self._merged().upcoming(date(2026, 10, 1), within_days=14) == (_festival(),)


class TestFromToml:
    def test_a_minimal_entry(self) -> None:
        book = BirthdayBook.from_toml(
            '[[birthday]]\nsubject = "user"\nname = "小明"\nmonth = 6\nday = 3\n'
        )
        assert len(book) == 1
        assert book.birthdays[0].month_day == "06-03"

    def test_the_subject_defaults_are_filled_in(self) -> None:
        # 省略不等于「没有」：补出来的是具体数字，对象里不留「按约定回退」这种状态。
        book = BirthdayBook.from_toml(
            '[[birthday]]\nsubject = "self"\nname = "自己"\nmonth = 1\nday = 1\n'
        )
        assert book.birthdays[0].lead_days == DEFAULT_LEAD_DAYS["self"]
        assert book.birthdays[0].aftermath_days == DEFAULT_AFTERMATH_DAYS["self"]

    def test_an_explicit_value_wins_over_the_default(self) -> None:
        book = BirthdayBook.from_toml(
            '[[birthday]]\nsubject = "user"\nname = "小明"\nmonth = 6\nday = 3\nlead_days = 20\n'
        )
        assert book.birthdays[0].lead_days == 20

    def test_an_npc_entry_keeps_its_id(self) -> None:
        book = BirthdayBook.from_toml(
            '[[birthday]]\nsubject = "npc"\nname = "张三"\nnpc_id = "npc-7"\nmonth = 6\nday = 3\n'
        )
        assert book.birthdays[0].tag == "npc:npc-7"
        assert book.birthdays[0].key == "npc-7"

    def test_the_top_level_note_is_read(self) -> None:
        book = BirthdayBook.from_toml('note = "随手记"\n')
        assert book.note == "随手记"

    def test_a_header_only_file_is_an_empty_book(self) -> None:
        # 写出去再读回来必须成立，而空书的渲染结果正好只有注释。
        assert len(BirthdayBook.from_toml("# 只有注释\n")) == 0

    def test_activities_are_read(self) -> None:
        book = BirthdayBook.from_toml(
            "[[birthday]]\n"
            'subject = "user"\n'
            'name = "小明"\n'
            "month = 6\nday = 3\n"
            'prep_activities = ["挑礼物", "订蛋糕"]\n'
        )
        assert book.birthdays[0].prep_activities == ("挑礼物", "订蛋糕")

    def test_an_unknown_top_level_field_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="顶层字段"):
            BirthdayBook.from_toml("nonsense = 1\n")

    def test_an_unknown_entry_field_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="无法识别的字段"):
            BirthdayBook.from_toml(
                '[[birthday]]\nsubject = "user"\nname = "小明"\nmonth = 6\nday = 3\nbirthday = 1\n'
            )

    def test_a_missing_required_field_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="缺少必填字段 day"):
            BirthdayBook.from_toml('[[birthday]]\nsubject = "user"\nname = "小明"\nmonth = 6\n')

    def test_a_non_list_entry_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="表数组"):
            BirthdayBook.from_toml('birthday = "小明"\n')

    def test_broken_toml_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="不是合法的 TOML"):
            BirthdayBook.from_toml("[[birthday]\n")

    def test_the_source_name_appears_in_the_error(self) -> None:
        # 配置目录下可能有不止一份生日文件（例如手工留了备份），
        # 报错里必须说清是哪一份，否则得靠猜。
        with pytest.raises(ValueError, match=r"birthdays\.toml"):
            BirthdayBook.from_toml("nonsense = 1\n", source="birthdays.toml")

    def test_a_string_is_not_a_boolean(self) -> None:
        # TOML 里 `verified = "false"` 是合法的，而 `bool("false") is True`——
        # 不挡住的话「没核实」会静默读成「已核实」。
        with pytest.raises(ValueError, match="布尔值"):
            BirthdayBook.from_toml(
                '[[birthday]]\nsubject = "user"\nname = "小明"\nmonth = 6\nday = 3\n'
                'verified = "false"\n'
            )

    def test_a_boolean_is_not_a_number(self) -> None:
        # `True` 是 `int` 的子类，不挡住的话 lead_days 会变成 1。
        with pytest.raises(ValueError, match="整数"):
            BirthdayBook.from_toml(
                '[[birthday]]\nsubject = "user"\nname = "小明"\nmonth = 6\nday = 3\n'
                "lead_days = true\n"
            )

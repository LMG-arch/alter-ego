"""``alterego.holidays`` —— 节日数据的读取器。

领域层不许做 IO（红线 2），所以「读文件」这件事必须有人负责，
而它的行为（缺文件怎么办、坏文件怎么办）恰恰是最容易出错的边界，
必须逐个钉住。
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest

from alterego.domain.calendar import MAX_YEAR, MIN_YEAR
from alterego.holidays import (
    DATA_DIR,
    available_years,
    load_calendar,
    load_year,
    path_for_year,
)


SHIPPED_YEAR = 2026

#: 一份最小可用的数据：一个公历固定日期的节日，不用核对任何权威日历。
MINIMAL_TOML = """\
confirmed = true
note = "测试用，仅含公历固定日期"

[[holiday]]
id = "new_year"
name = "元旦"
kind = "statutory"
day = 2027-01-01
days_off = [2027-01-01]
lead_days = 2
aftermath_days = 1
verified = true
note = "公历固定"
prep_activities = ["想想要不要出去玩"]
during_activities = ["睡到自然醒"]
aftermath_activities = ["不太想上班"]
"""


def _write_year(directory: Path, year: int) -> None:
    """写一份只有元旦的数据文件，锚点改到指定的那一年。"""
    text = MINIMAL_TOML.replace("2027-01-01", f"{year}-01-01")
    (directory / f"{year}.toml").write_text(text, encoding="utf-8")


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把数据目录指到临时目录，这样测试不去碰随包的真实数据。"""
    monkeypatch.setattr("alterego.holidays.DATA_DIR", tmp_path)
    return tmp_path


class TestPathForYear:
    def test_the_shipped_data_dir_is_inside_the_package(self) -> None:
        """跟包走才可能被 wheel 带出去。"""
        assert DATA_DIR.name == "holidays"
        assert DATA_DIR.parent.name == "alterego"

    def test_the_name_is_just_the_year(self) -> None:
        assert path_for_year(2026).name == "2026.toml"
        assert path_for_year(2026).parent == DATA_DIR

    @pytest.mark.parametrize("year", [MIN_YEAR - 1, 0, -1, MAX_YEAR + 1])
    def test_out_of_range_years_are_rejected(self, year: int) -> None:
        """比「找不到文件」更早、更明确地报错——年份写错和今年没写数据是两回事。"""
        with pytest.raises(ValueError, match="年份必须在"):
            path_for_year(year)


class TestLoadYear:
    def test_the_shipped_year_loads(self) -> None:
        calendar = load_year(SHIPPED_YEAR)
        assert calendar is not None
        assert calendar.covers_year(SHIPPED_YEAR)

    def test_a_missing_year_is_not_an_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """「那一年不知道有节」是合法状态，不该让程序起不来。

        只记 debug：`load_calendar` 每次都要探前后三年，警告级别的结果是
        每一条 CLI 命令开头都先吐两行「没有 2025/2027 年的数据」。
        """
        with caplog.at_level(logging.DEBUG):
            assert load_year(SHIPPED_YEAR + 1) is None
        assert "节日数据" in caplog.text

    def test_a_broken_file_must_blow_up(self, sandbox: Path) -> None:
        """文件存在却读不出来是「有人写坏了」。

        静默跳过的后果是这个节日从这一年里凭空消失，
        而且没有任何报错——比启动失败难查得多。
        """
        (sandbox / "2027.toml").write_text("confirmed = yes\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"2027\.toml"):
            load_year(2027)

    def test_a_valid_sandbox_file_loads(self, sandbox: Path) -> None:
        _write_year(sandbox, 2027)
        calendar = load_year(2027)
        assert calendar is not None
        assert calendar.confirmed is True
        assert [holiday.id for holiday in calendar.holidays] == ["new_year"]


class TestAvailableYears:
    def test_the_shipped_data_has_at_least_one_year(self) -> None:
        assert SHIPPED_YEAR in available_years()

    def test_the_result_is_sorted_and_has_no_duplicates(self) -> None:
        years = available_years()
        assert list(years) == sorted(set(years))

    def test_junk_files_are_ignored(self, sandbox: Path) -> None:
        """`README.toml` 或者编辑器留下的 `.tmp` 不该被当成一份年份数据。"""
        _write_year(sandbox, 2027)
        (sandbox / "README.toml").write_text("# 说明\n", encoding="utf-8")
        (sandbox / "2026.toml.bak").write_text("", encoding="utf-8")
        assert available_years() == (2027,)


class TestLoadCalendar:
    def test_a_single_file_needs_no_merging(self, sandbox: Path) -> None:
        _write_year(sandbox, 2027)
        calendar = load_calendar(2027)
        assert calendar is not None
        assert calendar.confirmed is True
        assert calendar.years == frozenset({2027})

    def test_nothing_anywhere_is_none(self, sandbox: Path) -> None:
        assert load_calendar(2027) is None

    def test_the_neighbouring_year_is_pulled_in(self, sandbox: Path) -> None:
        """这就是跨年视野：12 月 29 日得看得见 1 月 1 日。

        只读「需要的那一年」的实现会在这里返回空，而且是无声的——
        用户只会在年末发现「它怎么不知道元旦要放假」。
        """
        _write_year(sandbox, 2026)
        _write_year(sandbox, 2027)
        calendar = load_calendar(2026)
        assert calendar is not None
        assert {2026, 2027} <= calendar.years

        upcoming = calendar.upcoming(date(2026, 12, 29), within_days=5)
        assert [holiday.name for holiday in upcoming] == ["元旦"]

    def test_partial_coverage_still_answers(self, sandbox: Path) -> None:
        """只有一份文件时，另一年答不了就如实报错，而不是硬套相邻年份的调休。"""
        _write_year(sandbox, 2027)
        calendar = load_calendar(2027)
        assert calendar is not None
        with pytest.raises(ValueError, match="2026"):
            calendar.day_kind(date(2026, 5, 1))

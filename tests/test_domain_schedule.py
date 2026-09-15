"""`alterego.domain.schedule` 的单元测试。

`current_block` / `is_interruptible` 是打扰预算与疲劳恢复的共同输入，
边界（哪一刻算在哪一块里）错一格，主动消息就会在工作时间发出去。

依据: docs/design/01-architecture.md § 3
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from alterego.domain.schedule import (
    ScheduleBlock,
    current_block,
    is_interruptible,
)


def dt(hour: int = 10, minute: int = 0, day: int = 15) -> datetime:
    """2026 年 9 月某天的某个时刻（默认 15 日 10:00）。"""
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def block(
    *,
    identifier: str = "b1",
    start: datetime | None = None,
    end: datetime | None = None,
    category: str = "work",
    interruptible: bool = False,
    **extra: object,
) -> ScheduleBlock:
    return ScheduleBlock(
        id=identifier,
        persona_id="p1",
        day=date(2026, 9, 15),
        start_at=start or dt(9),
        end_at=end or dt(18),
        activity="写代码",
        category=category,  # type: ignore[arg-type]
        interruptible=interruptible,
        **extra,  # type: ignore[arg-type]
    )


class TestScheduleBlock:
    def test_holds_every_column_of_the_table(self) -> None:
        item = block(
            location="公司",
            source="llm",
            actual_start_at=dt(9, 10),
            actual_end_at=dt(18, 30),
            deviation_note="起晚了",
            created_at=dt(8),
        )

        assert item.location == "公司"
        assert item.source == "llm"
        assert item.actual_start_at == dt(9, 10)
        assert item.actual_end_at == dt(18, 30)
        assert item.deviation_note == "起晚了"
        assert item.created_at == dt(8)

    def test_defaults_are_planned_and_interruptible_or_not(self) -> None:
        item = block()

        assert item.source == "template"
        assert item.location is None
        assert item.actual_start_at is None
        assert item.actual_end_at is None
        assert item.deviation_note is None
        assert item.created_at is None

    def test_rejects_unknown_category(self) -> None:
        with pytest.raises(ValueError, match="未知的日程分类"):
            block(category="gaming")

    def test_rejects_unknown_source(self) -> None:
        with pytest.raises(ValueError, match="未知的日程来源"):
            block(source="guess")

    def test_rejects_non_positive_duration(self) -> None:
        with pytest.raises(ValueError, match="结束时间必须晚于开始时间"):
            block(start=dt(10), end=dt(10))

    def test_rejects_reversed_actual_times(self) -> None:
        with pytest.raises(ValueError, match="实际结束时间不能早于实际开始时间"):
            block(actual_start_at=dt(18), actual_end_at=dt(9))

    def test_actual_end_without_actual_start_is_allowed(self) -> None:
        assert block(actual_end_at=dt(18)).actual_end_at == dt(18)

    def test_is_sleep(self) -> None:
        assert block(category="sleep").is_sleep is True
        assert block(category="work").is_sleep is False

    def test_duration_minutes(self) -> None:
        assert block(start=dt(9), end=dt(12, 30)).duration_minutes == 210

    def test_contains_is_left_closed_right_open(self) -> None:
        item = block(start=dt(9), end=dt(12))

        assert item.contains(dt(9)) is True
        assert item.contains(dt(11, 59)) is True
        assert item.contains(dt(12)) is False
        assert item.contains(dt(8, 59)) is False


class TestCurrentBlock:
    def test_returns_none_when_the_schedule_has_a_gap(self) -> None:
        blocks = [block(start=dt(9), end=dt(12)), block(start=dt(13), end=dt(18))]

        assert current_block(blocks, dt(12, 30)) is None

    def test_returns_the_single_matching_block(self) -> None:
        blocks = [block(identifier="a", start=dt(9), end=dt(12)), block(start=dt(13), end=dt(18))]

        found = current_block(blocks, dt(10))

        assert found is not None
        assert found.id == "a"

    def test_overlaps_resolve_to_the_latest_start(self) -> None:
        blocks = [
            block(identifier="原计划", start=dt(9), end=dt(18)),
            block(identifier="改后", start=dt(14), end=dt(18)),
        ]

        found = current_block(blocks, dt(15))

        assert found is not None
        assert found.id == "改后"

    def test_empty_schedule(self) -> None:
        assert current_block([], dt(10)) is None


class TestIsInterruptible:
    def test_true_when_not_in_any_block(self) -> None:
        assert is_interruptible([], dt(10)) is True

    def test_false_during_a_protected_block(self) -> None:
        blocks = [block(start=dt(9), end=dt(18), interruptible=False)]

        assert is_interruptible(blocks, dt(10)) is False

    def test_true_during_a_loose_block(self) -> None:
        blocks = [block(start=dt(9), end=dt(18), interruptible=True)]

        assert is_interruptible(blocks, dt(10)) is True

    def test_a_later_block_can_win_back_the_decision(self) -> None:
        blocks = [
            block(identifier="工作", start=dt(9), end=dt(18), interruptible=False),
            block(identifier="咖啡时间", start=dt(15), end=dt(15, 30), interruptible=True),
        ]

        assert is_interruptible(blocks, dt(15, 10)) is True
        assert is_interruptible(blocks, dt(16)) is False


class TestNightWindow:
    def test_sleep_block_is_recognised_across_a_night(self) -> None:
        night = ScheduleBlock(
            id="sleep",
            persona_id="p1",
            day=date(2026, 9, 14),
            start_at=dt(23, 0, 14),
            end_at=dt(8),
            activity="睡觉",
            category="sleep",
            interruptible=False,
        )

        assert night.contains(dt(23, 0, 14)) is True
        assert night.contains(dt(7, 59)) is True
        assert night.contains(dt(8)) is False
        assert current_block([night], dt(3)) is night

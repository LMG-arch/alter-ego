"""`alterego.domain.conversation` 的单元测试。

「像人」是这一层的**验收目标**，所以断言得具体：不是「返回了一个决策」，
而是「睡觉时不回」「连着秒回会被拉长」「同一句话不会连着说三遍」。

依据: docs/design/12-calendar-and-conversation.md
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from alterego.domain.conversation import (
    DELAYED_HIGH_S,
    DELAYED_LOW_S,
    FAST_REPLY_LIMIT,
    INSTANT_HIGH_S,
    INSTANT_LOW_S,
    NORMAL_HIGH_S,
    NORMAL_LOW_S,
    PASSIVE_TURNS_BEFORE_TOPIC,
    REPETITION_THRESHOLD,
    SHORT_MESSAGE_CHARS,
    TOPIC_COOLDOWN_MINUTES,
    ReplyDecision,
    bigrams,
    decide_reply,
    find_repetition,
    should_open_topic,
    similarity,
)
from alterego.domain.emotion import Emotion
from alterego.domain.schedule import ScheduleBlock


def dt(hour: int = 10, minute: int = 0) -> datetime:
    """2026 年 9 月 15 日的某个时刻（默认 10:00）。"""
    return datetime(2026, 9, 15, hour, minute, tzinfo=UTC)


def emotion(
    *,
    valence: float = 0.2,
    arousal: float = 0.5,
    fatigue: float = 0.1,
    label: str = "平静",
) -> Emotion:
    return Emotion(valence=valence, arousal=arousal, fatigue=fatigue, label=label, updated_at=dt())


def block(
    *,
    activity: str = "写代码",
    start: datetime | None = None,
    end: datetime | None = None,
    interruptible: bool = True,
    category: str = "work",
) -> ScheduleBlock:
    return ScheduleBlock(
        id="b1",
        persona_id="p1",
        day=date(2026, 9, 15),
        start_at=start or dt(9),
        end_at=end or dt(18),
        activity=activity,
        category=category,  # type: ignore[arg-type]
        interruptible=interruptible,
    )


def between(low: float, high: float) -> tuple[timedelta, timedelta]:
    """把区间按实现里的取整方式转成 `timedelta`，免得断言里再写一遍 magic number。"""
    return timedelta(seconds=round(low)), timedelta(seconds=round(high))


def reply_args(**overrides: object) -> dict[str, object]:
    """`decide_reply` 的一组合法参数，按需覆盖。"""
    base: dict[str, object] = {
        "now": dt(10),
        "block": None,
        "emotion": emotion(),
        "text_length": 100,
        "consecutive_instant": 0,
        "roll": 0.0,
    }
    return {**base, **overrides}


# ────────────────────────────────────────────────────────────
# ReplyDecision 自己的不变量
# ────────────────────────────────────────────────────────────


class TestReplyDecision:
    def test_an_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="未知的回复模式"):
            ReplyDecision(mode="maybe", delay=timedelta(), reason="")

    def test_the_delay_cannot_be_negative(self) -> None:
        with pytest.raises(ValueError, match="不能为负"):
            ReplyDecision(mode="normal", delay=timedelta(seconds=-1), reason="")

    def test_silent_means_no_delay(self) -> None:
        with pytest.raises(ValueError, match="silent"):
            ReplyDecision(mode="silent", delay=timedelta(seconds=60), reason="")

    def test_silent_really_does_not_respond(self) -> None:
        assert ReplyDecision(mode="silent", delay=timedelta(), reason="累").responds is False
        assert ReplyDecision(mode="instant", delay=timedelta(seconds=2), reason="").responds is True


# ────────────────────────────────────────────────────────────
# decide_reply · 日程优先
# ────────────────────────────────────────────────────────────


class TestScheduleDecides:
    def test_sleeping_waits_until_waking_up(self) -> None:
        """「凌晨三点发消息」就是靠这一条挡住的，不需要再写别的规则。"""
        sleep = block(
            activity="睡觉",
            start=dt(23),
            end=datetime(2026, 9, 16, 7, 30, tzinfo=UTC),
            interruptible=False,
            category="sleep",
        )

        decision = decide_reply(**reply_args(now=dt(23, 40), block=sleep, roll=0.5))  # type: ignore[arg-type]

        assert decision.mode == "much_later"
        assert decision.delay >= timedelta(hours=7)
        assert "睡觉" in decision.reason

    def test_meetings_also_wait(self) -> None:
        meeting = block(activity="开会", start=dt(9), end=dt(18), interruptible=False)

        decision = decide_reply(**reply_args(block=meeting))  # type: ignore[arg-type]

        assert decision.mode == "much_later"
        assert decision.delay >= timedelta(hours=8)

    def test_an_elapsed_block_does_not_stall_the_reply(self) -> None:
        """`end_at` 早于 `now` 时只能按 0 处理，不能算出负延迟。"""
        stale = block(start=dt(8), end=dt(9), interruptible=False)

        decision = decide_reply(**reply_args(block=stale))  # type: ignore[arg-type]

        assert decision.mode == "much_later"
        assert decision.delay >= timedelta(0)
        assert decision.delay < timedelta(hours=1), "只剩「忙完缓一下」那一小段"

    def test_an_interruptible_block_does_not_block(self) -> None:
        decision = decide_reply(**reply_args(block=block(interruptible=True)))  # type: ignore[arg-type]

        assert decision.mode == "normal"

    def test_deciding_works_without_a_block(self) -> None:
        """新角色、模板缺失时 `block` 是 None，不能因此崩掉。"""
        decision = decide_reply(**reply_args(block=None))  # type: ignore[arg-type]

        assert decision.mode == "normal"


# ────────────────────────────────────────────────────────────
# decide_reply · 情绪与节奏
# ────────────────────────────────────────────────────────────


class TestMoodAndPace:
    def test_exhaustion_skips_this_turn(self) -> None:
        decision = decide_reply(**reply_args(now=dt(21), emotion=emotion(fatigue=0.97)))  # type: ignore[arg-type]

        assert decision.mode == "silent"
        assert decision.delay == timedelta(0)
        assert decision.responds is False

    def test_being_tired_slows_the_reply(self) -> None:
        decision = decide_reply(**reply_args(now=dt(21), emotion=emotion(fatigue=0.9)))  # type: ignore[arg-type]

        assert decision.mode == "delayed"
        low, high = between(DELAYED_LOW_S, DELAYED_HIGH_S)
        assert low <= decision.delay <= high

    def test_a_low_mood_slows_the_reply(self) -> None:
        decision = decide_reply(**reply_args(now=dt(21), emotion=emotion(valence=-0.8)))  # type: ignore[arg-type]

        assert decision.mode == "delayed"

    def test_a_streak_of_instant_replies_gets_capped(self) -> None:
        """一直秒回比人还像客服。这条规则存在的唯一目的就是打破它。"""
        decision = decide_reply(**reply_args(text_length=2, consecutive_instant=FAST_REPLY_LIMIT))  # type: ignore[arg-type]

        assert decision.mode == "normal", "短消息本来该秒回，但连着秒回太多就必须放一次"
        assert str(FAST_REPLY_LIMIT) in decision.reason

    def test_short_messages_get_instant_replies(self) -> None:
        decision = decide_reply(**reply_args(text_length=SHORT_MESSAGE_CHARS))  # type: ignore[arg-type]

        assert decision.mode == "instant"
        low, high = between(INSTANT_LOW_S, INSTANT_HIGH_S)
        assert low <= decision.delay <= high

    def test_longer_messages_get_a_normal_reply(self) -> None:
        decision = decide_reply(**reply_args(text_length=SHORT_MESSAGE_CHARS + 1))  # type: ignore[arg-type]

        assert decision.mode == "normal"
        low, high = between(NORMAL_LOW_S, NORMAL_HIGH_S)
        assert low <= decision.delay <= high

    def test_mood_outranks_the_instant_streak(self) -> None:
        """累的时候不会因为「刚才秒回太多」反而变成正常速度。"""
        decision = decide_reply(  # type: ignore[arg-type]
            **reply_args(
                now=dt(21), emotion=emotion(fatigue=0.99), text_length=2, consecutive_instant=99
            )
        )

        assert decision.mode == "silent"


# ────────────────────────────────────────────────────────────
# decide_reply · 可复现
# ────────────────────────────────────────────────────────────


class TestReproducible:
    def test_the_same_roll_yields_the_same_decision(self) -> None:
        args = reply_args(roll=0.42)

        assert decide_reply(**args) == decide_reply(**args)  # type: ignore[arg-type]

    @pytest.mark.parametrize("roll", [0.0, 0.25, 0.5, 0.75, 0.999])
    def test_a_valid_roll_keeps_the_delay_in_range(self, roll: float) -> None:
        decision = decide_reply(**reply_args(roll=roll))  # type: ignore[arg-type]

        low, high = between(NORMAL_LOW_S, NORMAL_HIGH_S)
        assert low <= decision.delay <= high

    def test_a_larger_roll_means_a_longer_delay(self) -> None:
        delays = [decide_reply(**reply_args(roll=roll)).delay for roll in (0.0, 0.5, 0.99)]  # type: ignore[arg-type]

        assert delays == sorted(delays)
        assert len(set(delays)) == 3

    @pytest.mark.parametrize("roll", [-0.1, 1.0, 2.5])
    def test_an_out_of_range_roll_is_rejected(self, roll: float) -> None:
        with pytest.raises(ValueError, match=r"\[0, 1\)"):
            decide_reply(**reply_args(roll=roll))  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("field", "value"),
        [("text_length", -1), ("consecutive_instant", -1)],
    )
    def test_negative_arguments_are_rejected(self, field: str, value: int) -> None:
        with pytest.raises(ValueError, match="不能为负"):
            decide_reply(**reply_args(**{field: value}))  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────
# should_open_topic · 不能只会被问一句答一句
# ────────────────────────────────────────────────────────────


def topic_args(**overrides: object) -> dict[str, object]:
    """`should_open_topic` 的一组合法参数，按需覆盖。"""
    base: dict[str, object] = {
        "block": None,
        "emotion": emotion(),
        "consecutive_passive_turns": PASSIVE_TURNS_BEFORE_TOPIC,
        "minutes_since_last_topic": None,
        "roll": 0.0,
    }
    return {**base, **overrides}


class TestOpeningTopic:
    def test_too_few_passive_turns_stays_reactive(self) -> None:
        decision = should_open_topic(  # type: ignore[arg-type]
            **topic_args(consecutive_passive_turns=PASSIVE_TURNS_BEFORE_TOPIC - 1)
        )

        assert decision.open_topic is False
        assert "先顺着" in decision.reason

    def test_enough_turns_and_a_lucky_roll_opens_a_topic(self) -> None:
        decision = should_open_topic(**topic_args())  # type: ignore[arg-type]

        assert decision.open_topic is True
        assert str(PASSIVE_TURNS_BEFORE_TOPIC) in decision.reason

    def test_an_unlucky_roll_does_not_open_a_topic(self) -> None:
        decision = should_open_topic(**topic_args(roll=0.99))  # type: ignore[arg-type]

        assert decision.open_topic is False

    def test_a_busy_block_suppresses_new_topics(self) -> None:
        decision = should_open_topic(  # type: ignore[arg-type]
            **topic_args(
                block=block(activity="开会", interruptible=False),
                consecutive_passive_turns=99,
            )
        )

        assert decision.open_topic is False
        assert "开会" in decision.reason

    def test_exhaustion_suppresses_new_topics(self) -> None:
        decision = should_open_topic(  # type: ignore[arg-type]
            **topic_args(emotion=emotion(fatigue=0.97), consecutive_passive_turns=99)
        )

        assert decision.open_topic is False

    def test_the_cooldown_prevents_topic_hopping(self) -> None:
        decision = should_open_topic(  # type: ignore[arg-type]
            **topic_args(
                consecutive_passive_turns=99,
                minutes_since_last_topic=TOPIC_COOLDOWN_MINUTES - 1,
            )
        )

        assert decision.open_topic is False
        assert "别一直换话题" in decision.reason

    def test_the_topic_can_open_again_after_the_cooldown(self) -> None:
        decision = should_open_topic(  # type: ignore[arg-type]
            **topic_args(minutes_since_last_topic=TOPIC_COOLDOWN_MINUTES)
        )

        assert decision.open_topic is True

    def test_longer_passivity_raises_the_chance(self) -> None:
        chances = [
            should_open_topic(**topic_args(consecutive_passive_turns=turns, roll=0.3)).open_topic  # type: ignore[arg-type]
            for turns in (PASSIVE_TURNS_BEFORE_TOPIC, PASSIVE_TURNS_BEFORE_TOPIC + 3)
        ]

        assert chances == [False, True]

    @pytest.mark.parametrize(
        ("field", "value"),
        [("consecutive_passive_turns", -1), ("minutes_since_last_topic", -1)],
    )
    def test_negative_arguments_are_rejected(self, field: str, value: int) -> None:
        with pytest.raises(ValueError, match="不能为负"):
            should_open_topic(**topic_args(**{field: value}))  # type: ignore[arg-type]

    def test_an_out_of_range_roll_is_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\[0, 1\)"):
            should_open_topic(**topic_args(roll=1.0))  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────
# 不复读自己
# ────────────────────────────────────────────────────────────


class TestNoSelfRepetition:
    def test_bigrams_ignore_whitespace_and_punctuation(self) -> None:
        assert bigrams("今天 真冷") == bigrams("今天真冷")
        assert bigrams("好嘞~") == bigrams("好嘞！")
        assert bigrams("好嘞~") == frozenset({"好嘞"})

    def test_single_characters_and_blank_text_degrade_cleanly(self) -> None:
        assert bigrams("嗯") == frozenset({"嗯"})
        assert bigrams("   ") == frozenset()
        assert bigrams("！！！") == frozenset(), "纯标点里没有可比较的词"

    def test_identical_text_is_perfectly_similar(self) -> None:
        assert similarity("我又忘了带伞", "我又忘了带伞") == 1.0

    def test_unrelated_text_scores_zero(self) -> None:
        assert similarity("我又忘了带伞", "明天开会改到十点") == 0.0

    def test_two_empty_strings_are_perfectly_similar(self) -> None:
        assert similarity("", "") == 1.0

    def test_one_empty_one_not_is_completely_dissimilar(self) -> None:
        assert similarity("", "有内容") == 0.0

    def test_a_couple_of_changed_characters_still_counts_as_repetition(self) -> None:
        """正是这种「换了标点、加了语气词又发一遍」要被抓住。"""
        assert similarity("我又忘了带伞啊啊", "我又忘了带伞……") >= REPETITION_THRESHOLD

    def test_different_meanings_are_not_repetition(self) -> None:
        assert similarity("我又忘了带伞", "今天中午吃什么") < REPETITION_THRESHOLD

    def test_catching_a_repeat_returns_the_original(self) -> None:
        recent = ["今天中午吃什么", "我又忘了带伞"]
        assert find_repetition("我又忘了带伞啊啊", recent) == "我又忘了带伞"

    def test_the_most_similar_one_is_returned(self) -> None:
        recent = ["我又忘了带伞……", "我又忘了带伞啊啊啊啊"]
        assert find_repetition("我又忘了带伞啊啊啊", recent) == "我又忘了带伞啊啊啊啊"

    def test_no_repetition_returns_none(self) -> None:
        assert find_repetition("今天中午吃什么", ["我又忘了带伞"]) is None

    def test_nothing_recent_returns_none(self) -> None:
        assert find_repetition("随便什么", []) is None

    def test_a_lower_threshold_catches_more(self) -> None:
        candidate = "我又忘了带伞"
        old = "我又忘了带伞啊啊"
        assert find_repetition(candidate, [old], threshold=1.0) is None
        assert find_repetition(candidate, [old], threshold=0.5) == old

    @pytest.mark.parametrize("threshold", [-0.1, 1.5])
    def test_an_out_of_range_threshold_is_rejected(self, threshold: float) -> None:
        with pytest.raises(ValueError, match="0~1"):
            find_repetition("x", ["y"], threshold=threshold)

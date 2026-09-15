"""`alterego.domain.emotion` 的单元测试。

四条规则各有独立的边界用例；`update_emotion` 额外断言**顺序**——
「先回归 → 再冲击 → 最后疲劳」换一下，同样的输入会给出不同的情绪，
而 `emotion_log.reason` 是用户唯一能看到的解释。

依据: docs/design/04-simulation-loop.md § 6
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from alterego.domain.emotion import (
    EMOTION_LABELS,
    MAX_SENSITIVITY,
    MIN_SENSITIVITY,
    SIGNIFICANT_DELTA,
    Emotion,
    EmotionalEvent,
    apply_event,
    apply_with_inertia,
    clamp,
    clamp_sensitivity,
    decay_toward,
    infer_label,
    update_emotion,
    update_fatigue,
)
from alterego.domain.schedule import ScheduleBlock


def dt(hour: int = 10, minute: int = 0) -> datetime:
    return datetime(2026, 9, 15, hour, minute, tzinfo=UTC)


def emotion(
    *,
    valence: float = 0.0,
    arousal: float = 0.4,
    fatigue: float = 0.2,
    label: str = "一般",
    updated_at: datetime | None = None,
) -> Emotion:
    return Emotion(
        valence=valence,
        arousal=arousal,
        fatigue=fatigue,
        label=label,  # type: ignore[arg-type]
        updated_at=updated_at or dt(),
    )


def sleep_block() -> ScheduleBlock:
    return ScheduleBlock(
        id="sleep",
        persona_id="p1",
        day=date(2026, 9, 15),
        start_at=dt(0),
        end_at=dt(8),
        activity="睡觉",
        category="sleep",
        interruptible=False,
    )


class TestClamp:
    def test_passes_through_in_range(self) -> None:
        assert clamp(0.5, 0.0, 1.0) == 0.5

    def test_pins_both_ends(self) -> None:
        assert clamp(-3.0, -1.0, 1.0) == -1.0
        assert clamp(3.0, -1.0, 1.0) == 1.0


class TestClampSensitivity:
    def test_keeps_values_inside_the_range(self) -> None:
        assert clamp_sensitivity(1.2) == 1.2

    def test_pins_out_of_range_values(self) -> None:
        assert clamp_sensitivity(0.1) == MIN_SENSITIVITY
        assert clamp_sensitivity(9.0) == MAX_SENSITIVITY

    def test_matches_the_documented_bounds(self) -> None:
        assert (MIN_SENSITIVITY, MAX_SENSITIVITY) == (0.5, 1.5)


class TestInferLabel:
    @pytest.mark.parametrize(
        ("valence", "arousal", "expected"),
        [
            (0.9, 0.9, "兴奋"),
            (0.5, 0.2, "愉快"),
            (-0.5, 0.8, "焦虑"),
            (-0.5, 0.4, "低落"),
            (0.0, 0.8, "警觉"),
            (0.15, 0.28, "平静"),
            (0.2, 0.45, "一般"),
            (0.0, 0.2, "疲惫"),
        ],
    )
    def test_every_label_has_a_region(self, valence: float, arousal: float, expected: str) -> None:
        assert infer_label(valence, arousal) == expected

    def test_tiredness_wins_over_the_neutral_quadrant(self) -> None:
        # arousal=0.1 / valence=0.05 既满足「低唤醒」也满足「中性」，
        # 但规则表把疲惫判在低唤醒之前，所以它说的是「累」。
        assert infer_label(0.05, 0.1) == "疲惫"

    def test_low_arousal_claims_everything_below_neutral(self) -> None:
        # 这条规则的实际代价：只要 arousal < 0.25 且 valence < 0.1 就判为疲惫，
        # 所以「低落」至少要 arousal >= 0.25，「平静」只在 0.25 ~ 0.3 这个窄带里出现。
        # 记在这里是为了它变成回归时能被看见。
        assert infer_label(-0.5, 0.2) == "疲惫"
        assert infer_label(0.0, 0.2) == "疲惫"

    def test_tiredness_does_not_swallow_a_good_mood(self) -> None:
        assert infer_label(0.5, 0.1) == "愉快"


class TestEmotion:
    def test_rejects_out_of_range_values(self) -> None:
        with pytest.raises(ValueError, match="valence"):
            emotion(valence=1.5)
        with pytest.raises(ValueError, match="arousal"):
            emotion(arousal=-0.1)
        with pytest.raises(ValueError, match="fatigue"):
            emotion(fatigue=1.2)

    def test_rejects_an_empty_label(self) -> None:
        with pytest.raises(ValueError, match="label 不能为空"):
            emotion(label="")

    def test_the_label_vocabulary_is_open_for_the_llm_path(self) -> None:
        # 「感动」「委屈」「恼火」在 01-architecture.md § 3.2 里是合法标签，
        # 它们不会由 infer_label 产出，而是 LLM 路径给出的。
        assert emotion(label="感动").label == "感动"

    def test_infer_label_only_produces_the_degraded_vocabulary(self) -> None:
        produced = {infer_label(v / 20, a / 20) for v in range(-20, 21, 2) for a in range(0, 21, 2)}

        assert produced <= set(EMOTION_LABELS)

    def test_fatigue_thresholds(self) -> None:
        assert emotion(fatigue=0.71).is_tired is True
        assert emotion(fatigue=0.7).is_tired is False
        assert emotion(fatigue=0.86).is_exhausted is True
        assert emotion(fatigue=0.85).is_exhausted is False


class TestDecayToward:
    def test_a_half_life_halves_the_distance_to_the_baseline(self) -> None:
        result = decay_toward(
            current=1.0, baseline=0.0, elapsed=timedelta(hours=4), half_life_hours=4.0
        )

        assert result == pytest.approx(0.5)

    def test_a_full_series_of_half_lives(self) -> None:
        assert decay_toward(1.0, 0.0, timedelta(hours=8), 4.0) == pytest.approx(0.25)
        assert decay_toward(1.0, 0.0, timedelta(hours=12), 4.0) == pytest.approx(0.125)

    def test_distance_is_halved_not_the_value(self) -> None:
        # 基线 +0.2、当前 -0.6：一个半衰期后是 -0.2，而不是 -0.3。
        assert decay_toward(-0.6, 0.2, timedelta(hours=4), 4.0) == pytest.approx(-0.2)

    def test_no_time_means_no_change(self) -> None:
        assert decay_toward(0.7, 0.0, timedelta(0), 4.0) == pytest.approx(0.7)

    def test_already_at_the_baseline_stays_there(self) -> None:
        assert decay_toward(0.0, 0.0, timedelta(hours=100), 4.0) == pytest.approx(0.0)

    def test_rejects_a_non_positive_half_life(self) -> None:
        with pytest.raises(ValueError, match="half_life_hours"):
            decay_toward(1.0, 0.0, timedelta(hours=1), 0.0)


class TestEmotionalEvent:
    def test_defaults_are_a_pure_description(self) -> None:
        event = EmotionalEvent(description="窗外有只鸟")

        assert (event.valence_delta, event.arousal_delta, event.fatigue_delta) == (0.0, 0.0, 0.0)

    def test_requires_a_description(self) -> None:
        with pytest.raises(ValueError, match="description"):
            EmotionalEvent(description="")


class TestApplyEvent:
    def test_adds_all_three_dimensions(self) -> None:
        result = apply_event(
            emotion(valence=0.1, arousal=0.4, fatigue=0.2),
            EmotionalEvent("好消息", valence_delta=0.3, arousal_delta=0.2, fatigue_delta=-0.1),
        )

        assert result.valence == pytest.approx(0.4)
        assert result.arousal == pytest.approx(0.6)
        assert result.fatigue == pytest.approx(0.1)

    def test_clamps_at_every_bound(self) -> None:
        result = apply_event(
            emotion(valence=0.9, arousal=0.1, fatigue=0.95),
            EmotionalEvent("极端", valence_delta=0.5, arousal_delta=-0.5, fatigue_delta=0.5),
        )

        assert result.valence == 1.0
        assert result.arousal == 0.0
        assert result.fatigue == 1.0


class TestApplyWithInertia:
    def test_same_direction_is_amplified_up_to_1_4x(self) -> None:
        # |valence| = 1.0 → inertia = 1 + 0.4 = 1.4
        assert apply_with_inertia(emotion(valence=1.0), 0.2, 1.0) == pytest.approx(1.0)

    def test_amplification_is_visible_below_the_ceiling(self) -> None:
        # 0.5 + 0.2 * (1 + 0.4*0.5) = 0.5 + 0.24
        assert apply_with_inertia(emotion(valence=0.5), 0.2, 1.0) == pytest.approx(0.74)

    def test_opposite_direction_is_weakened_to_0_6x(self) -> None:
        # 0.5 + (-0.2) * (1 - 0.4*0.5) = 0.5 - 0.16
        assert apply_with_inertia(emotion(valence=0.5), -0.2, 1.0) == pytest.approx(0.34)

    def test_a_neutral_mood_has_no_inertia(self) -> None:
        assert apply_with_inertia(emotion(valence=0.0), 0.2, 1.0) == pytest.approx(0.2)

    def test_sensitivity_scales_the_effect(self) -> None:
        dull = apply_with_inertia(emotion(valence=0.0), 0.4, MIN_SENSITIVITY)
        sharp = apply_with_inertia(emotion(valence=0.0), 0.4, MAX_SENSITIVITY)

        assert dull == pytest.approx(0.2)
        assert sharp == pytest.approx(0.6)

    def test_result_is_clamped(self) -> None:
        assert apply_with_inertia(emotion(valence=0.95), 0.5, 1.5) == 1.0
        assert apply_with_inertia(emotion(valence=-0.95), -0.5, 1.5) == -1.0

    def test_rejects_sensitivity_outside_the_persona_range(self) -> None:
        with pytest.raises(ValueError, match="sensitivity"):
            apply_with_inertia(emotion(), 0.1, 0.49)
        with pytest.raises(ValueError, match="sensitivity"):
            apply_with_inertia(emotion(), 0.1, 1.51)


class TestUpdateFatigue:
    def test_awake_hours_add_fatigue(self) -> None:
        assert update_fatigue(0.2, timedelta(hours=4), None) == pytest.approx(0.4)

    def test_twenty_awake_hours_saturate(self) -> None:
        assert update_fatigue(0.0, timedelta(hours=20), None) == pytest.approx(1.0)

    def test_awake_accumulation_adds_a_quarter_per_five_hours(self) -> None:
        assert update_fatigue(0.2, timedelta(hours=5), None) == pytest.approx(0.45)

    def test_sleep_recovers(self) -> None:
        assert update_fatigue(0.9, timedelta(hours=1), sleep_block()) == pytest.approx(0.1)

    def test_sleep_cannot_recover_past_zero(self) -> None:
        assert update_fatigue(0.2, timedelta(hours=8), sleep_block()) == 0.0

    def test_a_non_sleep_block_counts_as_awake(self) -> None:
        work = replace(sleep_block(), category="work")

        assert update_fatigue(0.2, timedelta(hours=4), work) == pytest.approx(0.4)


class TestUpdateEmotion:
    def test_nothing_happens_when_nothing_changes(self) -> None:
        current = emotion()
        baseline = current

        updated, reason = update_emotion(current, [], baseline, 1.0, timedelta(minutes=1))

        assert updated.valence == pytest.approx(0.0)
        assert reason == "无显著变化"

    def test_natural_decay_is_reported_when_it_matters(self) -> None:
        current = emotion(valence=0.8, arousal=0.4, label="兴奋")
        baseline = emotion(valence=0.0, arousal=0.0, label="平静")

        updated, reason = update_emotion(current, [], baseline, 1.0, timedelta(hours=4))

        assert updated.valence == pytest.approx(0.4)
        assert "情绪自然回落 +0.80→+0.40" in reason

    def test_small_decay_is_not_reported(self) -> None:
        current = emotion(valence=0.8)
        baseline = emotion(valence=0.0)

        updated, reason = update_emotion(current, [], baseline, 1.0, timedelta(minutes=5))

        distance = abs(updated.valence - current.valence)
        assert distance < SIGNIFICANT_DELTA
        assert reason == "无显著变化"

    def test_events_are_described_in_the_reason(self) -> None:
        current = emotion()
        baseline = emotion()

        _, reason = update_emotion(
            current,
            [EmotionalEvent("用户夸了它", valence_delta=0.3)],
            baseline,
            1.0,
            timedelta(minutes=1),
        )

        assert "用户夸了它 效价" in reason

    def test_decay_runs_before_the_shock(self) -> None:
        # 若先冲击再回归：0.0 + 0.6 = 0.6，回归 4 小时后 → 0.3。
        # 正确顺序：0.0 先回归仍是 0.0，再冲击 → 0.6。
        current = emotion(valence=0.0)
        baseline = emotion(valence=0.0)

        updated, _ = update_emotion(
            current,
            [EmotionalEvent("好事", valence_delta=0.6)],
            baseline,
            1.0,
            timedelta(hours=4),
        )

        assert updated.valence == pytest.approx(0.6)

    def test_inertia_uses_the_decayed_valence(self) -> None:
        # 当前 +1.0，回归 4 小时后到 +0.5，惯性系数 1.2，
        # 于是 +0.5 + 0.1*1.2 = +0.62。若用未衰减的 +1.0，结果是 +0.64。
        current = emotion(valence=1.0, label="兴奋")
        baseline = emotion(valence=0.0, label="平静")

        updated, _ = update_emotion(
            current, [EmotionalEvent("好事", valence_delta=0.1)], baseline, 1.0, timedelta(hours=4)
        )

        assert updated.valence == pytest.approx(0.62)

    def test_arousal_is_scaled_by_sensitivity(self) -> None:
        current = emotion(valence=0.0, arousal=0.0)

        updated, _ = update_emotion(
            current,
            [EmotionalEvent("惊吓", arousal_delta=0.4)],
            current,
            1.5,
            timedelta(0),
        )

        assert updated.arousal == pytest.approx(0.6)

    def test_event_fatigue_delta_is_applied(self) -> None:
        current = emotion(fatigue=0.2)

        updated, _ = update_emotion(
            current,
            [EmotionalEvent("跑了一趟", fatigue_delta=0.3)],
            current,
            1.0,
            timedelta(0),
        )

        assert updated.fatigue == pytest.approx(0.5)

    def test_sleeping_during_the_interval_lowers_fatigue(self) -> None:
        current = emotion(fatigue=0.9)

        updated, _ = update_emotion(current, [], current, 1.0, timedelta(hours=1), sleep_block())

        assert updated.fatigue == pytest.approx(0.1)

    def test_without_a_block_fatigue_only_accumulates(self) -> None:
        current = emotion(fatigue=0.5)

        updated, _ = update_emotion(current, [], current, 1.0, timedelta(hours=1))

        assert updated.fatigue == pytest.approx(0.55)

    def test_label_is_recomputed_from_the_new_coordinates(self) -> None:
        current = emotion(valence=0.0, arousal=0.4, label="一般")

        updated, _ = update_emotion(
            current,
            [EmotionalEvent("激动", valence_delta=0.9, arousal_delta=0.5)],
            current,
            1.0,
            timedelta(0),
        )

        assert updated.label == "兴奋"

    def test_zero_elapsed_time_does_not_move_the_clock(self) -> None:
        current = emotion(updated_at=dt(10))

        updated, _ = update_emotion(current, [], current, 1.0, timedelta(0))

        assert updated.updated_at == dt(10)

    def test_the_clock_advances_by_exactly_the_elapsed_time(self) -> None:
        current = emotion(updated_at=dt(10))

        updated, _ = update_emotion(current, [], current, 1.0, timedelta(minutes=90))

        assert updated.updated_at == dt(11, 30)

    def test_values_are_rounded_to_three_decimals(self) -> None:
        current = emotion(valence=0.0, arousal=0.123456789)

        updated, _ = update_emotion(current, [], current, 1.0, timedelta(0))

        assert updated.arousal == pytest.approx(0.123)

    def test_multiple_events_are_applied_in_order(self) -> None:
        current = emotion(valence=0.0)

        updated, reason = update_emotion(
            current,
            [
                EmotionalEvent("第一件", valence_delta=0.2),
                EmotionalEvent("第二件", valence_delta=0.2),
            ],
            current,
            1.0,
            timedelta(0),
        )

        # 0.0 → 0.2（中性，无惯性）→ 0.2 + 0.2*(1+0.4*0.2) = 0.416
        assert updated.valence == pytest.approx(0.416)
        assert reason.index("第一件") < reason.index("第二件")

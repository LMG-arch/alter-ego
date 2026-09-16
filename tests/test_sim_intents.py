"""``sim/intents.py`` 的测试：它能想到哪些事，以及此刻更想做哪一件。

这是设计原则 P6「可复现」最吃紧的一块。权重如果交给模型判断，
同一天重放两遍会得到两条不同的行为线，``alterego why`` 也就没法回答
「它为什么突然想找你」。所以这里的每一条断言都在钉两件事：

1. **权重是算出来的**：给同一个 :class:`IntentContext`，永远是同一组数。
2. **权重背后的理由说得出口**：``Candidate.reason`` 是要给人看的那句话，
   不是调试用的系数。

另外两条容易写错的机制：``choose`` 是**加权抽签**而不是取第一名
（只取第一名会让它变成一台机器），以及 ``reach_out`` 的动机要跟着情绪走。
"""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from alterego.interfaces.simulation import IntentType
from alterego.sim.intents import (
    BUILTIN_INTENTS,
    LONELY_DISCOUNT,
    MAX_CANDIDATES,
    QUIET_HOURS_BEFORE_DISCOUNT,
    REACH_OUT_MOTIVATIONS,
    Candidate,
    IntentCatalog,
    IntentContext,
    build_candidates,
    choose,
    choose_reach_out_motivation,
)


CATALOG = IntentCatalog()

#: 一个「正常的下午」：有活干、情绪平、不累、没人等着回话。
NEUTRAL = IntentContext(hour=14)


def weight_of(name: str, context: IntentContext = NEUTRAL) -> float:
    """某条意图在某个世界里的权重。不存在的意图由测试断言暴露。"""
    for candidate in build_candidates(CATALOG, context):
        if candidate.intent.name == name:
            return candidate.weight
    return 0.0


def reason_of(name: str, context: IntentContext = NEUTRAL) -> str:
    for candidate in build_candidates(CATALOG, context):
        if candidate.intent.name == name:
            return candidate.reason
    return ""


def fixed(value: float) -> random.Random:
    """恒定 ``random()`` 的随机源。加权抽签的落点因此可预测。"""

    class _Fixed(random.Random):
        def __init__(self) -> None:
            super().__init__(0)

        def random(self) -> float:
            return value

    return _Fixed()


# ── 目录 ────────────────────────────────────────────────────


class TestCatalog:
    def test_the_builtin_catalog_has_ten_intents(self) -> None:
        """10 条是文档 § 4.1 的数字。多一条少一条都意味着设计改了。"""
        assert len(IntentCatalog()) == 10

    def test_every_builtin_name_is_unique(self) -> None:
        names = [intent.name for intent in BUILTIN_INTENTS]
        assert len(names) == len(set(names))

    def test_names_are_sorted_for_stable_output(self) -> None:
        assert list(CATALOG.names()) == sorted(CATALOG.names())

    def test_lookup_by_name(self) -> None:
        found = CATALOG.get("reach_out")
        assert found is not None
        assert found.outbound is True

    def test_a_missing_name_is_none_not_an_error(self) -> None:
        """插件问「有没有这条意图」是正常操作，不该抛异常。"""
        assert CATALOG.get("research") is None

    def test_membership_testing(self) -> None:
        assert "reply" in CATALOG
        assert "research" not in CATALOG

    def test_iteration_yields_intent_types(self) -> None:
        assert all(isinstance(intent, IntentType) for intent in CATALOG)

    def test_a_duplicate_name_is_refused(self) -> None:
        """静默顶掉 ``reach_out`` 之后，用户会看到「它突然会做一件不存在的事」。"""
        with pytest.raises(ValueError, match="意图名重复：work"):
            IntentCatalog(intents=(CATALOG.get("work"), CATALOG.get("work")))  # type: ignore[arg-type]

    def test_adding_after_construction_works(self) -> None:
        catalog = IntentCatalog(intents=())
        catalog.add(CATALOG.get("work"))  # type: ignore[arg-type]
        assert len(catalog) == 1

    def test_a_plugin_intent_is_equal_to_a_builtin_one(self) -> None:
        """没有「插件意图权重减半」这种说法——那会让插件作者永远在猜。"""
        plugin_style = IntentType(
            name="water_plants",
            description="给窗台的植物浇水",
            category="internal",
            default_weight=0.2,
        )
        catalog = IntentCatalog(intents=(plugin_style,))
        candidates = build_candidates(catalog, NEUTRAL)
        assert candidates[0].weight == 0.2
        assert candidates[0].reason == "按它自己声明的权重"

    def test_research_is_deliberately_absent(self) -> None:
        """``research`` 标着 v0.3.0+。一个永远选不中的意图只会多一个假答案。"""
        assert CATALOG.get("research") is None


# ── 候选与排序 ──────────────────────────────────────────────


class TestBuildCandidates:
    def test_zero_weight_intents_are_dropped(self) -> None:
        """留着它们只会让「候选 5 条」这个上限被废动作占满。"""
        night = IntentContext(hour=2)
        names = {candidate.intent.name for candidate in build_candidates(CATALOG, night)}
        assert "work" not in names
        assert "commute" not in names

    def test_no_unread_means_no_reply_candidate(self) -> None:
        names = {candidate.intent.name for candidate in build_candidates(CATALOG, NEUTRAL)}
        assert "reply" not in names

    def test_an_unread_message_makes_reply_the_top_candidate(self) -> None:
        context = replace(NEUTRAL, unread_messages=2)
        assert build_candidates(CATALOG, context)[0].intent.name == "reply"

    def test_the_reply_reason_counts_the_messages(self) -> None:
        context = replace(NEUTRAL, unread_messages=3)
        assert reason_of("reply", context) == "有 3 条没回"

    def test_candidates_are_sorted_by_weight_descending(self) -> None:
        weights = [candidate.weight for candidate in build_candidates(CATALOG, NEUTRAL)]
        assert weights == sorted(weights, reverse=True)

    def test_equal_weights_break_the_tie_by_name(self) -> None:
        """权重相同时的先后必须确定，否则同一颗种子重放会得到不同结果（P6）。"""
        equal = tuple(
            IntentType(name=name, description="一样重", category="internal", default_weight=0.1)
            for name in ("zeta", "alpha", "mid")
        )
        names = [c.intent.name for c in build_candidates(IntentCatalog(intents=equal), NEUTRAL)]
        assert names == ["alpha", "mid", "zeta"]

    def test_a_candidate_carries_its_intent_object(self) -> None:
        candidate = build_candidates(CATALOG, NEUTRAL)[0]
        assert isinstance(candidate, Candidate)
        assert candidate.intent is CATALOG.get(candidate.intent.name)

    def test_the_same_context_always_gives_the_same_weights(self) -> None:
        """P6：同一个世界跑两遍，权重必须一字不差。"""
        first = build_candidates(CATALOG, NEUTRAL)
        second = build_candidates(CATALOG, NEUTRAL)
        assert [(c.intent.name, c.weight, c.reason) for c in first] == [
            (c.intent.name, c.weight, c.reason) for c in second
        ]

    def test_an_empty_catalog_gives_no_candidates(self) -> None:
        assert build_candidates(IntentCatalog(intents=()), NEUTRAL) == []


# ── 逐条权重规则 ────────────────────────────────────────────


class TestReplyWeight:
    def test_one_unread_is_enough_to_want_to_answer(self) -> None:
        assert weight_of("reply", replace(NEUTRAL, unread_messages=1)) == 1.0

    def test_more_unread_does_not_make_it_even_more_urgent(self) -> None:
        """1.0 已经是最高的那一档；再往上加会让别的意图永远没机会。"""
        assert weight_of("reply", replace(NEUTRAL, unread_messages=9)) == 1.0


class TestReachOutWeight:
    def test_the_default_is_declared_by_the_intent(self) -> None:
        declared = CATALOG.get("reach_out")
        assert declared is not None
        assert weight_of("reach_out") == declared.default_weight

    def test_a_busy_block_does_not_change_the_weight(self) -> None:
        """不能打断不代表不能想——只是这一 tick 不该发出去。"""
        busy = replace(NEUTRAL, interruptible=False)
        assert weight_of("reach_out", busy) == weight_of("reach_out")

    def test_a_busy_block_changes_only_the_reason(self) -> None:
        assert reason_of("reach_out", replace(NEUTRAL, interruptible=False)) == "想找你（但正在忙）"

    def test_being_ignored_for_a_day_only_discounts_it(self) -> None:
        """想找你的冲动不该被一次冷落磨平，所以是降权而不是禁止。"""
        ignored = replace(NEUTRAL, hours_since_last_user_reply=30.0)
        assert weight_of("reach_out", ignored) == pytest.approx(
            CATALOG.get("reach_out").default_weight * LONELY_DISCOUNT  # type: ignore[union-attr]
        )

    def test_the_discount_needs_more_than_a_full_day(self) -> None:
        boundary = replace(NEUTRAL, hours_since_last_user_reply=QUIET_HOURS_BEFORE_DISCOUNT)
        assert weight_of("reach_out", boundary) == weight_of("reach_out")

    def test_the_reason_says_how_long_it_has_been(self) -> None:
        ignored = replace(NEUTRAL, hours_since_last_user_reply=30.4)
        assert "30 小时没理它了" in reason_of("reach_out", ignored)

    def test_never_having_talked_is_not_being_ignored(self) -> None:
        """新装好的机器不该在第一天就摆出被冷落的样子。"""
        assert weight_of("reach_out", replace(NEUTRAL, hours_since_last_user_reply=None)) == (
            weight_of("reach_out")
        )


class TestWorkWeight:
    def test_it_disappears_late_at_night(self) -> None:
        assert weight_of("work", IntentContext(hour=23)) == 0.0

    def test_it_disappears_before_eight(self) -> None:
        assert weight_of("work", IntentContext(hour=7)) == 0.0

    def test_eight_in_the_morning_is_already_awake(self) -> None:
        assert weight_of("work", IntentContext(hour=8)) > 0

    def test_office_hours_rank_it_highest(self) -> None:
        assert weight_of("work", IntentContext(hour=14)) > weight_of("work", IntentContext(hour=20))

    def test_the_office_hours_reason_says_why(self) -> None:
        assert reason_of("work", IntentContext(hour=10)) == "工作时段，手头的活最要紧"

    def test_exhaustion_takes_the_work_out_of_it(self) -> None:
        """累到睁不开眼还想着「有活在手上」，会让它看起来不像个人。"""
        tired = IntentContext(hour=14, fatigue=0.9)
        assert weight_of("work", tired) < weight_of("work")
        assert reason_of("work", tired) == "累到没什么心思干活"

    def test_fatigue_below_the_threshold_changes_nothing(self) -> None:
        assert weight_of("work", IntentContext(hour=14, fatigue=0.5)) == weight_of("work")


class TestRestWeight:
    def test_exhaustion_doubles_the_appeal_of_resting(self) -> None:
        tired = IntentContext(fatigue=0.7)
        assert weight_of("rest", tired) == pytest.approx(weight_of("rest") * 2.0)

    def test_the_tired_reason_says_it_is_tired(self) -> None:
        assert reason_of("rest", IntentContext(fatigue=0.7)) == "累了，该歇了"

    def test_sitting_still_too_long_also_nudges_it(self) -> None:
        idle = IntentContext(idle_minutes=180.0)
        assert weight_of("rest", idle) == pytest.approx(weight_of("rest") * 1.3)

    def test_the_idle_reason_says_it_has_been_sitting(self) -> None:
        assert reason_of("rest", IntentContext(idle_minutes=180.0)) == "坐太久了，想动一动"

    def test_both_reasons_apply_at_once(self) -> None:
        both = IntentContext(fatigue=0.7, idle_minutes=180.0)
        assert weight_of("rest", both) == pytest.approx(weight_of("rest") * 2.0 * 1.3)

    def test_the_last_reason_wins_when_both_apply(self) -> None:
        """两个条件都成立时只说后面那句。连着说两句会读成两个人。"""
        both = IntentContext(fatigue=0.7, idle_minutes=180.0)
        assert reason_of("rest", both) == "坐太久了，想动一动"


class TestEatWeight:
    def test_the_declared_weight_is_the_baseline_not_the_midday_one(self) -> None:
        """比较基准要用意图自己声明的权重：饭点那一下是 ``×3`` 乘上去的。"""
        declared = CATALOG.get("eat")
        assert declared is not None
        assert declared.default_weight == pytest.approx(0.08)

    @pytest.mark.parametrize("hour", [11, 12, 17, 18, 19])
    def test_meal_times_make_it_three_times_more_likely(self, hour: int) -> None:
        declared = CATALOG.get("eat")
        assert weight_of("eat", IntentContext(hour=hour)) == pytest.approx(
            declared.default_weight * 3.0  # type: ignore[union-attr]
        )

    def test_the_meal_reason_says_it_is_meal_time(self) -> None:
        assert reason_of("eat", IntentContext(hour=12)) == "到饭点了"

    def test_between_meals_it_is_still_possible(self) -> None:
        """饿是会随时发生的，只是可能性小得多。"""
        assert weight_of("eat", IntentContext(hour=15)) > 0

    def test_between_meals_it_is_five_times_less_likely(self) -> None:
        declared = CATALOG.get("eat")
        assert weight_of("eat", IntentContext(hour=15)) == pytest.approx(
            declared.default_weight * 0.2  # type: ignore[union-attr]
        )

    def test_between_meals_the_reason_is_just_hunger(self) -> None:
        assert reason_of("eat", IntentContext(hour=15)) == "有点饿"


class TestCommuteWeight:
    @pytest.mark.parametrize("hour", [7, 8, 9, 17, 18, 19])
    def test_commuting_hours(self, hour: int) -> None:
        assert weight_of("commute", IntentContext(hour=hour)) > 0

    @pytest.mark.parametrize("hour", [0, 6, 10, 13, 16, 20, 23])
    def test_no_commuting_at_other_hours(self, hour: int) -> None:
        assert weight_of("commute", IntentContext(hour=hour)) == 0.0

    def test_the_reason_says_it_is_time_to_leave(self) -> None:
        assert reason_of("commute", IntentContext(hour=8)) == "该出门了"


class TestEntertainWeight:
    def test_evenings_make_it_more_appealing(self) -> None:
        assert weight_of("entertain", IntentContext(hour=21)) == pytest.approx(
            weight_of("entertain") * 2.0
        )

    def test_after_midnight_is_still_evening_as_far_as_it_cares(self) -> None:
        assert weight_of("entertain", IntentContext(hour=0)) == pytest.approx(
            weight_of("entertain") * 2.0
        )

    def test_a_low_mood_also_pushes_it_towards_something_easy(self) -> None:
        sad = IntentContext(hour=14, valence=-0.4)
        assert weight_of("entertain", sad) == pytest.approx(weight_of("entertain") * 1.5)

    def test_the_low_mood_reason_says_why(self) -> None:
        assert reason_of("entertain", IntentContext(valence=-0.4)) == "心情不好，想看点轻松的"

    def test_a_good_evening_multiplies_both(self) -> None:
        both = IntentContext(hour=21, valence=-0.4)
        assert weight_of("entertain", both) == pytest.approx(weight_of("entertain") * 2.0 * 1.5)


class TestReflectInternalWeight:
    def test_a_low_mood_makes_it_want_to_sit_with_it(self) -> None:
        sad = IntentContext(valence=-0.3)
        assert weight_of("reflect_internal", sad) == pytest.approx(
            weight_of("reflect_internal") * 1.8
        )

    def test_the_low_mood_reason_says_why(self) -> None:
        assert (
            reason_of("reflect_internal", IntentContext(valence=-0.3)) == "情绪不太对，想自己待会儿"
        )

    def test_exhaustion_also_pushes_it_inwards(self) -> None:
        tired = IntentContext(fatigue=0.8)
        assert weight_of("reflect_internal", tired) == pytest.approx(
            weight_of("reflect_internal") * 1.5
        )

    def test_a_mild_mood_dip_is_not_enough(self) -> None:
        assert weight_of("reflect_internal", IntentContext(valence=-0.1)) == weight_of(
            "reflect_internal"
        )


class TestSocializeWeight:
    def test_a_good_mood_makes_it_want_company(self) -> None:
        happy = IntentContext(valence=0.4)
        assert weight_of("socialize", happy) == pytest.approx(weight_of("socialize") * 1.6)

    def test_the_good_mood_reason_says_why(self) -> None:
        assert reason_of("socialize", IntentContext(valence=0.4)) == "心情好，想跟人说说话"

    def test_exhaustion_takes_the_company_out_of_it(self) -> None:
        tired = IntentContext(valence=0.4, fatigue=0.8)
        assert reason_of("socialize", tired) == "累得不想见人"
        assert weight_of("socialize", tired) < weight_of("socialize")

    def test_both_multipliers_apply(self) -> None:
        both = IntentContext(valence=0.4, fatigue=0.8)
        assert weight_of("socialize", both) == pytest.approx(weight_of("socialize") * 1.6 * 0.3)


class TestPostMomentWeight:
    def test_a_good_mood_doubles_the_urge_to_post(self) -> None:
        happy = IntentContext(valence=0.5)
        assert weight_of("post_moment", happy) == pytest.approx(weight_of("post_moment") * 2.0)

    def test_the_good_mood_reason_says_why(self) -> None:
        assert reason_of("post_moment", IntentContext(valence=0.5)) == "心情不错，想发点什么"

    def test_a_low_mood_makes_it_less_willing_to_be_seen(self) -> None:
        sad = IntentContext(valence=-0.5)
        assert weight_of("post_moment", sad) == pytest.approx(weight_of("post_moment") * 0.5)

    def test_the_low_mood_reason_says_why(self) -> None:
        assert reason_of("post_moment", IntentContext(valence=-0.5)) == "心情不好，不太想让人看见"

    def test_a_flat_mood_leaves_the_declared_weight(self) -> None:
        assert reason_of("post_moment") == "发生了点想记下来的事"


# ── 抽签 ────────────────────────────────────────────────────


class TestChoose:
    def test_no_candidates_means_no_choice(self) -> None:
        """没选中意图是合法结果——那就是在发呆。"""
        assert choose([], rng=fixed(0.5)) is None

    def test_the_top_candidate_is_reachable(self) -> None:
        candidates = build_candidates(CATALOG, replace(NEUTRAL, unread_messages=1))
        assert choose(candidates, rng=fixed(0.0)).intent.name == "reply"  # type: ignore[union-attr]

    def test_the_last_of_the_pool_is_reachable(self) -> None:
        candidates = build_candidates(CATALOG, NEUTRAL)
        assert choose(candidates, rng=fixed(0.999999)) is candidates[MAX_CANDIDATES - 1]

    def test_a_roll_beyond_the_total_still_returns_something(self) -> None:
        """浮点累加可能差一点，最后的兜底不能是 ``None``。"""
        candidates = [
            Candidate(intent=BUILTIN_INTENTS[0], weight=0.1, reason=""),
            Candidate(intent=BUILTIN_INTENTS[1], weight=0.1, reason=""),
        ]
        assert choose(candidates, rng=fixed(1.0)) is candidates[-1]

    def test_a_bigger_weight_wins_a_bigger_slice(self) -> None:
        """抽签不是取第一名，但权重大确实该更容易被抽到。"""
        candidates = build_candidates(CATALOG, replace(NEUTRAL, unread_messages=1))
        top = candidates[0]
        assert top.weight > candidates[1].weight
        assert choose(candidates, rng=fixed(0.1)) is top

    def test_only_the_top_few_are_in_the_pool(self) -> None:
        """``MAX_CANDIDATES`` 是 5：权重落在五名之外的意图永远抽不到。"""
        assert MAX_CANDIDATES == 5
        many = [
            Candidate(intent=BUILTIN_INTENTS[index], weight=1.0 - index * 0.01, reason="")
            for index in range(len(BUILTIN_INTENTS))
        ]
        assert choose(many, rng=fixed(0.999999)) is many[MAX_CANDIDATES - 1]

    def test_the_same_seed_gives_the_same_choice(self) -> None:
        """P6：同一颗种子重放同一天，它做的每个选择都一模一样。"""
        candidates = build_candidates(CATALOG, NEUTRAL)
        first = choose(candidates, rng=random.Random(7))
        second = choose(candidates, rng=random.Random(7))
        assert first is not None
        assert second is not None
        assert first.intent.name == second.intent.name

    def test_a_zero_total_falls_back_to_the_first(self) -> None:
        """防御性分支：``build_candidates`` 已经滤掉 0，但直接构造的调用方可能没有。"""
        candidates = [
            Candidate(intent=BUILTIN_INTENTS[0], weight=0.0, reason=""),
            Candidate(intent=BUILTIN_INTENTS[1], weight=0.0, reason=""),
        ]
        assert choose(candidates, rng=fixed(0.5)) is candidates[0]


# ── 动机 ────────────────────────────────────────────────────


class TestReachOutMotivation:
    def test_every_answer_is_a_known_motivation(self) -> None:
        for step in range(20):
            name = choose_reach_out_motivation(random.Random(step), context=NEUTRAL)
            assert name in REACH_OUT_MOTIVATIONS

    def test_a_low_mood_opens_up_need_comfort(self) -> None:
        sad = IntentContext(valence=-0.5)
        assert "need_comfort" in {
            choose_reach_out_motivation(random.Random(step), context=sad) for step in range(40)
        }

    def test_a_good_mood_never_produces_need_comfort(self) -> None:
        for step in range(40):
            assert (
                choose_reach_out_motivation(random.Random(step), context=NEUTRAL) != "need_comfort"
            )

    def test_never_talking_makes_it_miss_you(self) -> None:
        stranger = IntentContext(hours_since_last_user_reply=None)
        assert "miss_you" in {
            choose_reach_out_motivation(random.Random(step), context=stranger) for step in range(40)
        }

    def test_a_long_silence_also_makes_it_miss_you(self) -> None:
        long_ago = IntentContext(hours_since_last_user_reply=30.0)
        assert "miss_you" in {
            choose_reach_out_motivation(random.Random(step), context=long_ago) for step in range(40)
        }

    def test_a_recent_conversation_opens_up_follow_up(self) -> None:
        recent = IntentContext(hours_since_last_user_reply=1.0)
        assert "follow_up" in {
            choose_reach_out_motivation(random.Random(step), context=recent) for step in range(40)
        }

    def test_follow_up_needs_a_recent_conversation(self) -> None:
        for step in range(40):
            name = choose_reach_out_motivation(random.Random(step), context=NEUTRAL)
            assert name != "follow_up"

    def test_the_same_seed_gives_the_same_motivation(self) -> None:
        first = choose_reach_out_motivation(random.Random(3), context=NEUTRAL)
        assert first == choose_reach_out_motivation(random.Random(3), context=NEUTRAL)

    def test_a_motivation_is_never_an_empty_string(self) -> None:
        """空串会让 ``message.motivation`` 变成一个没人知道含义的列。"""
        assert choose_reach_out_motivation(random.Random(1), context=IntentContext()) != ""

    def test_a_fallback_roll_still_returns_the_least_intrusive_one(self) -> None:
        """权重全为 0 的极端回落到 ``just_bored``。"""
        assert choose_reach_out_motivation(fixed(1.0), context=NEUTRAL) == "just_bored"

    def test_the_motivations_have_chinese_labels(self) -> None:
        """这一列最终会显示给人看，所以值必须是中文而不是 ``miss_you``。"""
        assert all(label for label in REACH_OUT_MOTIVATIONS.values())
        assert REACH_OUT_MOTIVATIONS["miss_you"] == "有点想你"

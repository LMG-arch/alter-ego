"""`alterego.domain.memory` 的单元测试。

最硬的一条是 `TestStrengthAt`：`04-simulation-loop.md § 7.2` 给了一张
验证表（0 天→0.70、7 天→0.35、21 天→0.09、28 天→0.04），
它把「公式有没有写对」变成一个可以断言的事实，而不是一句注释。

依据: docs/design/04-simulation-loop.md § 7、docs/design/03-data-model.md § 6
"""

from __future__ import annotations

import math
import sys
import types
from datetime import UTC, datetime, timedelta

import pytest

from alterego.domain.memory import (
    DEFAULT_TOP_K,
    FADE_THRESHOLD,
    HALF_LIFE_DAYS,
    Memory,
    MemoryCandidate,
    MemorySearchHit,
    MemoryStats,
    RetrievalWeights,
    apply_consolidation,
    decay_rate,
    is_faded,
    mood_alignment,
    preprocess_for_fts,
    rank_memories,
    recency_at,
    resurrect,
    should_resurrect,
    strength_at,
)


NOW = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)


def memory(
    *,
    identifier: str = "m1",
    kind: str = "episodic",
    content: str = "和小王吃火锅",
    summary: str = "和小王吃火锅",
    importance: float = 0.7,
    valence: float = 0.0,
    occurred_at: datetime | None = None,
    last_recalled_at: datetime | None = None,
    recall_count: int = 0,
    forgotten: bool = False,
    **extra: object,
) -> Memory:
    return Memory(
        persona_id="p1",
        kind=kind,  # type: ignore[arg-type]
        content=content,
        summary=summary,
        importance=importance,
        valence=valence,
        occurred_at=occurred_at or NOW,
        id=identifier,
        last_recalled_at=last_recalled_at,
        recall_count=recall_count,
        forgotten=forgotten,
        **extra,  # type: ignore[arg-type]
    )


def days_ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


class TestMemoryModel:
    def test_defaults_describe_an_unsaved_memory(self) -> None:
        item = Memory(persona_id="p1", kind="episodic", content="a", summary="a", occurred_at=NOW)

        assert item.id == ""
        assert item.created_at is None
        assert item.strength == 1.0
        assert item.entities == ()
        assert item.tags == ()
        assert item.source == "tick"
        assert item.source_ref is None
        assert item.forgotten is False

    def test_rejects_unknown_kind(self) -> None:
        with pytest.raises(ValueError, match="未知的记忆类型"):
            memory(kind="dream")

    def test_rejects_unknown_source(self) -> None:
        with pytest.raises(ValueError, match="未知的记忆来源"):
            memory(source="guess")

    def test_rejects_empty_content(self) -> None:
        with pytest.raises(ValueError, match="content 不能为空"):
            memory(content="   ")

    def test_rejects_empty_summary(self) -> None:
        with pytest.raises(ValueError, match="summary 不能为空"):
            memory(summary="")

    def test_rejects_out_of_range_numbers(self) -> None:
        with pytest.raises(ValueError, match="importance"):
            memory(importance=1.2)
        with pytest.raises(ValueError, match="strength"):
            memory(strength=-0.1)
        with pytest.raises(ValueError, match="valence"):
            memory(valence=2.0)
        with pytest.raises(ValueError, match="recall_count"):
            memory(recall_count=-1)

    def test_reference_time_prefers_the_last_recall(self) -> None:
        assert memory(occurred_at=days_ago(30)).reference_time == days_ago(30)
        assert memory(
            occurred_at=days_ago(30), last_recalled_at=days_ago(1)
        ).reference_time == days_ago(1)


class TestDecayRate:
    def test_matches_the_documented_half_lives(self) -> None:
        assert HALF_LIFE_DAYS == {"episodic": 7.0, "semantic": 180.0, "emotional": 365.0}

    def test_lambda_is_ln2_over_half_life(self) -> None:
        assert decay_rate("episodic") == pytest.approx(0.099, abs=5e-4)
        assert decay_rate("semantic") == pytest.approx(0.0039, abs=5e-5)
        assert decay_rate("emotional") == pytest.approx(0.0019, abs=5e-5)

    def test_rejects_unknown_kind(self) -> None:
        with pytest.raises(ValueError, match="未知的记忆类型"):
            decay_rate("dream")


class TestStrengthAt:
    @pytest.mark.parametrize(
        ("days", "expected"),
        [(0.0, 0.70), (7.0, 0.35), (21.0, 0.0875), (28.0, 0.04375)],
    )
    def test_reproduces_the_validation_table(self, days: float, expected: float) -> None:
        item = memory(importance=0.7, occurred_at=days_ago(days))

        assert strength_at(item, NOW) == pytest.approx(expected, rel=1e-6)

    @pytest.mark.parametrize(
        ("days", "documented"),
        [(0.0, 0.70), (7.0, 0.35), (21.0, 0.09), (28.0, 0.04)],
    )
    def test_table_values_are_two_decimal_roundings(self, days: float, documented: float) -> None:
        item = memory(importance=0.7, occurred_at=days_ago(days))

        assert round(strength_at(item, NOW), 2) == documented

    def test_decay_is_counted_from_the_last_recall_not_the_event(self) -> None:
        # 事情发生在 30 天前，但 1 分钟前刚想起来过 → 仍然清晰。
        item = memory(occurred_at=days_ago(30), last_recalled_at=NOW - timedelta(minutes=1))

        assert strength_at(item, NOW) == pytest.approx(0.7, abs=1e-4)

    def test_a_full_half_life_halves_the_strength(self) -> None:
        assert strength_at(memory(occurred_at=days_ago(7)), NOW) == pytest.approx(0.35)

    def test_each_recall_multiplies_by_1_35(self) -> None:
        assert strength_at(memory(recall_count=1), NOW) == pytest.approx(0.7 * 1.35)
        assert strength_at(memory(recall_count=2), NOW) == pytest.approx(0.7 * 1.7)

    def test_a_clock_that_runs_backwards_does_not_strengthen_the_memory(self) -> None:
        item = memory(occurred_at=days_ago(-10))

        assert strength_at(item, NOW) == pytest.approx(0.7)

    def test_semantic_memories_decay_much_slower_than_episodic(self) -> None:
        episodic = memory(kind="episodic", occurred_at=days_ago(30))
        semantic = memory(kind="semantic", occurred_at=days_ago(30))

        assert strength_at(semantic, NOW) > strength_at(episodic, NOW) * 5

    def test_zero_importance_never_holds(self) -> None:
        assert strength_at(memory(importance=0.0), NOW) == 0.0


class TestIsFaded:
    def test_episodic_memory_is_faded_after_28_days(self) -> None:
        item = memory(importance=0.7, occurred_at=days_ago(28))

        assert strength_at(item, NOW) < FADE_THRESHOLD
        assert is_faded(item, NOW) is True

    def test_fresh_memory_is_not_faded(self) -> None:
        assert is_faded(memory(), NOW) is False

    def test_threshold_is_overridable(self) -> None:
        item = memory(occurred_at=days_ago(7))

        assert is_faded(item, NOW, threshold=0.5) is True


class TestRecencyAt:
    def test_a_fresh_memory_has_recency_one(self) -> None:
        assert recency_at(memory(), NOW) == pytest.approx(1.0)

    def test_decays_with_lambda_0_03(self) -> None:
        assert recency_at(memory(occurred_at=days_ago(10)), NOW) == pytest.approx(math.exp(-0.3))

    def test_recency_uses_the_event_time_not_the_recall_time(self) -> None:
        # 复习效应已经进了 strength_at，这里再用一次等于算两遍。
        item = memory(occurred_at=days_ago(10), last_recalled_at=NOW)

        assert recency_at(item, NOW) == pytest.approx(math.exp(-0.3))

    def test_future_events_are_clamped(self) -> None:
        assert recency_at(memory(occurred_at=days_ago(-5)), NOW) == pytest.approx(1.0)


class TestMoodAlignment:
    def test_a_matching_mood_is_perfectly_aligned(self) -> None:
        assert mood_alignment(memory(valence=0.8), 0.8) == pytest.approx(1.0)

    def test_opposite_moods_are_not_aligned_at_all(self) -> None:
        assert mood_alignment(memory(valence=-1.0), 1.0) == pytest.approx(0.0)

    def test_a_neutral_memory_sits_in_the_middle(self) -> None:
        assert mood_alignment(memory(valence=0.0), 1.0) == pytest.approx(0.5)


class TestRetrievalWeights:
    def test_defaults_come_from_the_design_doc(self) -> None:
        weights = RetrievalWeights()

        assert (weights.relevance, weights.importance, weights.recency, weights.mood) == (
            0.40,
            0.25,
            0.20,
            0.15,
        )

    @pytest.mark.parametrize("field", ["relevance", "importance", "recency", "mood"])
    def test_rejects_negative_weights(self, field: str) -> None:
        with pytest.raises(ValueError, match=f"权重 {field} 不能为负"):
            RetrievalWeights(**{field: -0.1})  # type: ignore[arg-type]


class TestMemoryCandidate:
    def test_defaults_to_zero_relevance(self) -> None:
        assert MemoryCandidate(memory=memory()).relevance == 0.0

    def test_rejects_relevance_outside_zero_to_one(self) -> None:
        with pytest.raises(ValueError, match="relevance"):
            MemoryCandidate(memory=memory(), relevance=1.5)


class TestRankMemories:
    def test_the_score_decomposes_into_the_four_terms(self) -> None:
        item = memory(kind="semantic", importance=0.5, occurred_at=days_ago(10))
        candidate = MemoryCandidate(memory=item, relevance=0.5)

        [hit] = rank_memories([candidate], NOW, query_valence=0.0)

        expected_strength = 0.5 * math.exp(-math.log(2) / 180 * 10)
        assert hit.strength == pytest.approx(expected_strength)
        assert hit.relevance == pytest.approx(0.5)
        assert hit.importance == pytest.approx(0.5 * expected_strength)
        assert hit.recency == pytest.approx(math.exp(-0.3))
        assert hit.mood == pytest.approx(1.0)
        assert hit.score == pytest.approx(
            0.40 * hit.relevance + 0.25 * hit.importance + 0.20 * hit.recency + 0.15 * hit.mood
        )

    def test_the_importance_term_already_includes_strength(self) -> None:
        # 同样重要度，一条刚想起来过、一条从没想起来过。
        fresh = MemoryCandidate(
            memory=memory(identifier="a", occurred_at=days_ago(7), last_recalled_at=NOW)
        )
        stale = MemoryCandidate(memory=memory(identifier="b", occurred_at=days_ago(7)))

        hits = {hit.memory.id: hit for hit in rank_memories([fresh, stale], NOW)}

        assert hits["a"].importance > hits["b"].importance

    def test_weights_can_isolate_a_single_term(self) -> None:
        candidate = MemoryCandidate(memory=memory(), relevance=0.6)
        only_relevance = RetrievalWeights(relevance=1.0, importance=0.0, recency=0.0, mood=0.0)

        [hit] = rank_memories([candidate], NOW, weights=only_relevance)

        assert hit.score == pytest.approx(0.6)

    def test_mood_alignment_reshuffles_the_order(self) -> None:
        happy = MemoryCandidate(memory=memory(identifier="happy", valence=0.9))
        sad = MemoryCandidate(memory=memory(identifier="sad", valence=-0.9))

        first = rank_memories([sad, happy], NOW, query_valence=0.9)[0]

        assert first.memory.id == "happy"

    def test_without_a_mood_context_every_memory_is_equally_aligned(self) -> None:
        happy = MemoryCandidate(memory=memory(identifier="happy", valence=0.9))
        sad = MemoryCandidate(memory=memory(identifier="sad", valence=-0.9))

        hits = rank_memories([happy, sad], NOW, query_valence=None)

        assert all(hit.mood == pytest.approx(1.0) for hit in hits)
        assert hits[0].score == pytest.approx(hits[1].score)

    def test_relevance_orders_the_result(self) -> None:
        low = MemoryCandidate(memory=memory(identifier="low"), relevance=0.1)
        high = MemoryCandidate(memory=memory(identifier="high"), relevance=0.9)

        hits = rank_memories([low, high], NOW)

        assert [hit.memory.id for hit in hits] == ["high", "low"]

    def test_candidates_below_the_strength_threshold_are_dropped(self) -> None:
        faded = MemoryCandidate(memory=memory(identifier="faded", importance=0.04))
        alive = MemoryCandidate(memory=memory(identifier="alive", importance=0.7))

        hits = rank_memories([faded, alive], NOW)

        assert [hit.memory.id for hit in hits] == ["alive"]

    def test_min_strength_is_overridable(self) -> None:
        faint = MemoryCandidate(memory=memory(identifier="faint", importance=0.04))

        assert rank_memories([faint], NOW) == []
        assert len(rank_memories([faint], NOW, min_strength=0.0)) == 1

    def test_default_limit_is_top_k(self) -> None:
        candidates = [
            MemoryCandidate(memory=memory(identifier=f"m{i}"), relevance=i / 100)
            for i in range(DEFAULT_TOP_K + 5)
        ]

        assert len(rank_memories(candidates, NOW)) == DEFAULT_TOP_K

    def test_limit_none_returns_everything(self) -> None:
        candidates = [
            MemoryCandidate(memory=memory(identifier=f"m{i}")) for i in range(DEFAULT_TOP_K + 5)
        ]

        assert len(rank_memories(candidates, NOW, limit=None)) == DEFAULT_TOP_K + 5

    def test_ties_are_broken_by_id_so_the_order_is_reproducible(self) -> None:
        first = MemoryCandidate(memory=memory(identifier="b"))
        second = MemoryCandidate(memory=memory(identifier="a"))

        hits = rank_memories([first, second], NOW)

        assert [hit.memory.id for hit in hits] == ["a", "b"]

    def test_empty_input(self) -> None:
        assert rank_memories([], NOW) == []

    def test_hits_carry_the_memory_object_itself(self) -> None:
        item = memory()
        [hit] = rank_memories([MemoryCandidate(memory=item)], NOW)

        assert hit.memory is item


class TestResurrection:
    def test_only_important_forgotten_memories_qualify(self) -> None:
        assert should_resurrect(memory(forgotten=True, importance=0.7)) is True
        assert should_resurrect(memory(forgotten=True, importance=0.3)) is False
        assert should_resurrect(memory(forgotten=False, importance=0.7)) is False

    def test_the_threshold_is_overridable(self) -> None:
        assert should_resurrect(memory(forgotten=True, importance=0.3), min_importance=0.2) is True

    def test_resurrect_restores_but_not_fully(self) -> None:
        item = memory(forgotten=True, importance=0.8, recall_count=2)

        revived = resurrect(item, now=NOW)

        assert revived.forgotten is False
        assert revived.strength == pytest.approx(0.32)
        assert revived.last_recalled_at == NOW
        assert revived.recall_count == 3

    def test_resurrect_refuses_memories_that_were_never_forgotten(self) -> None:
        with pytest.raises(ValueError, match="只有已遗忘的记忆"):
            resurrect(memory(forgotten=False), now=NOW)

    def test_a_revived_memory_is_weaker_than_it_was_before_forgetting(self) -> None:
        revived = resurrect(memory(forgotten=True, importance=0.8), now=NOW)

        assert revived.strength < strength_at(memory(importance=0.8), NOW)


class TestConsolidation:
    def test_importance_is_discounted(self) -> None:
        [kept] = apply_consolidation([memory(importance=0.8)])

        assert kept.importance == pytest.approx(0.48)

    def test_strength_drops_with_it(self) -> None:
        original = memory(importance=0.8)
        [kept] = apply_consolidation([original])

        assert strength_at(kept, NOW) < strength_at(original, NOW)

    def test_every_input_memory_is_returned(self) -> None:
        items = [memory(identifier="a"), memory(identifier="b")]

        assert len(apply_consolidation(items)) == 2

    def test_the_original_is_not_mutated(self) -> None:
        original = memory(importance=0.8)

        apply_consolidation([original])

        assert original.importance == 0.8

    @pytest.mark.parametrize("factor", [0.0, 1.0, 1.5, -0.2])
    def test_rejects_a_factor_that_is_not_a_discount(self, factor: float) -> None:
        with pytest.raises(ValueError, match="factor"):
            apply_consolidation([memory()], factor=factor)


class TestPreprocessForFts:
    def test_falls_back_to_the_raw_text_without_jieba(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "jieba", None)

        assert preprocess_for_fts("和小王吃火锅") == "和小王吃火锅"

    def test_segments_the_text_when_jieba_is_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = types.ModuleType("jieba")
        fake.cut_for_search = lambda text: [text[i : i + 2] for i in range(0, len(text), 2)]  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "jieba", fake)

        assert preprocess_for_fts("火锅好吃") == "火锅 好吃"

    def test_write_and_query_paths_must_agree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "jieba", None)

        assert preprocess_for_fts("爬山") == preprocess_for_fts("爬山")


class TestMemoryStats:
    def test_defaults_describe_an_empty_library(self) -> None:
        stats = MemoryStats()

        assert stats.total == 0
        assert stats.by_kind == {}
        assert stats.oldest_at is None

    def test_carries_the_full_picture(self) -> None:
        stats = MemoryStats(
            total=10,
            active=8,
            forgotten=2,
            by_kind={"episodic": 6, "semantic": 4},
            avg_importance=0.5,
            avg_strength=0.4,
            oldest_at=days_ago(90),
            newest_at=NOW,
        )

        assert stats.by_kind["episodic"] == 6

    def test_rejects_a_total_that_does_not_add_up(self) -> None:
        with pytest.raises(ValueError, match=r"active \+ forgotten 必须等于 total"):
            MemoryStats(total=10, active=8, forgotten=1)

    def test_rejects_negative_counts(self) -> None:
        with pytest.raises(ValueError, match="total 不能为负"):
            MemoryStats(total=-1, active=0, forgotten=-1)


class TestMemorySearchHit:
    def test_is_immutable(self) -> None:
        hit = MemorySearchHit(
            memory=memory(),
            score=0.5,
            relevance=0.4,
            importance=0.3,
            recency=0.2,
            mood=0.1,
            strength=0.6,
        )

        with pytest.raises(AttributeError):
            hit.score = 0.9  # type: ignore[misc]

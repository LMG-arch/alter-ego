"""``alterego.domain.consolidation`` 的测试。

纯函数，没有 IO，所以这里全部是同步用例。重点不在「能解析」（那是最低要求），
而在**边界上它是否还讲道理**：

- 包装层宽容（围栏、前后寒暄、单个字符串），语义层一点也不宽容；
- 整批丢弃，而不是逐条跳过；
- 空数组是合法答案，超限是非法答案。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from alterego.domain.consolidation import (
    MAX_ITEMS,
    SUMMARY_LIMIT,
    MemoryDraft,
    extract_json_array,
    parse_memory_drafts,
    summarize,
)
from alterego.kernel.clock import resolve_timezone


TZ = resolve_timezone("Asia/Shanghai")
T0 = datetime(2026, 9, 15, 9, 0, tzinfo=TZ)


# ────────────────────────────────────────────────────────────
# summarize · 摘要不能是「切 60 个字」
# ────────────────────────────────────────────────────────────


class TestSummarize:
    def test_it_takes_the_first_sentence(self) -> None:
        assert summarize("今天很累。明天再说。") == "今天很累。"

    def test_it_keeps_a_sentence_that_fits(self) -> None:
        """没有句末标点、但本身就够短——原样返回，不要画蛇添足加省略号。"""
        assert summarize("今天很累没有别的") == "今天很累没有别的"

    def test_it_truncates_a_sentence_that_is_too_long(self) -> None:
        content = "甲" * (SUMMARY_LIMIT + 20)
        result = summarize(content)
        assert result.endswith("…")
        assert len(result) == SUMMARY_LIMIT + 1

    def test_a_later_sentence_does_not_save_a_long_first_one(self) -> None:
        """第一句就超长时不能跳到第二句——那是摘要，不是摘抄。"""
        content = "甲" * (SUMMARY_LIMIT + 5) + "。第二句。"
        assert summarize(content).endswith("…")

    def test_it_normalises_whitespace(self) -> None:
        """换行与连续空格归一成一个空格：这一行会被塞进提示词。"""
        assert summarize("今天  很累\n明天再说") == "今天 很累 明天再说"

    def test_blank_content_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="不能为空白"):
            summarize("   \n  ")

    def test_the_limit_is_respected(self) -> None:
        content = "甲" * 10 + "。" + "乙" * 10
        assert summarize(content, limit=5) == "甲" * 5 + "…"


# ────────────────────────────────────────────────────────────
# extract_json_array · 只对包装宽容
# ────────────────────────────────────────────────────────────


class TestExtractJsonArray:
    def test_a_bare_array(self) -> None:
        assert extract_json_array('[{"content": "甲"}]') == [{"content": "甲"}]

    def test_it_survives_a_markdown_fence(self) -> None:
        """要求「只输出 JSON」的模型里总有一批会加上围栏。"""
        text = '好的，这是结果：\n```json\n[{"content": "甲"}]\n```\n希望有帮助。'
        assert extract_json_array(text) == [{"content": "甲"}]

    def test_it_survives_an_empty_array_with_prose(self) -> None:
        assert extract_json_array("今天没什么值得记的：[]") == []

    def test_empty_reply_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="空的"):
            extract_json_array("   ")

    def test_a_json_object_is_not_an_array(self) -> None:
        with pytest.raises(ValueError, match="期望一个 JSON 数组"):
            extract_json_array('{"content": "甲"}')

    def test_broken_json_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="解析失败"):
            extract_json_array('[{"content": "甲",}]')


# ────────────────────────────────────────────────────────────
# parse_memory_drafts · 整批丢弃
# ────────────────────────────────────────────────────────────


class TestParseMemoryDrafts:
    def test_an_empty_array_is_a_valid_answer(self) -> None:
        """「今天没什么值得记的」是一个结论，不是一次失败。"""
        assert parse_memory_drafts("[]") == []

    def test_it_fills_defaults(self) -> None:
        drafts = parse_memory_drafts('[{"content": "今天很累"}]')
        assert len(drafts) == 1
        assert drafts[0].kind == "episodic"
        assert drafts[0].importance == 0.5
        assert drafts[0].valence == 0.0
        assert drafts[0].entities == ()

    def test_it_reads_every_field(self) -> None:
        text = (
            '[{"content": "和阿哲聊到搬家", "kind": "semantic", "importance": 0.8,'
            ' "valence": -0.4, "entities": ["阿哲", "搬家"]}]'
        )
        draft = parse_memory_drafts(text)[0]
        assert draft.kind == "semantic"
        assert draft.importance == 0.8
        assert draft.valence == -0.4
        assert draft.entities == ("阿哲", "搬家")

    def test_an_emotional_flag_becomes_its_own_kind(self) -> None:
        """模板里用布尔开关表达「带情绪的瞬间」，这里合流到 kind。"""
        drafts = parse_memory_drafts('[{"content": "很久没这么开心过", "emotional": true}]')
        assert drafts[0].kind == "emotional"

    def test_emotional_false_does_not_change_the_kind(self) -> None:
        drafts = parse_memory_drafts('[{"content": "买了瓶水", "emotional": false}]')
        assert drafts[0].kind == "episodic"

    def test_a_bare_string_entities_is_accepted(self) -> None:
        """常见偏差，语义没有歧义——为方括号重试一次不划算。"""
        drafts = parse_memory_drafts('[{"content": "甲", "entities": "阿哲"}]')
        assert drafts[0].entities == ("阿哲",)

    def test_it_strips_blank_entries(self) -> None:
        drafts = parse_memory_drafts('[{"content": "甲", "entities": ["阿哲", "  ", ""]}]')
        assert drafts[0].entities == ("阿哲",)

    def test_duplicates_within_one_batch_are_dropped(self) -> None:
        """只差空格的两条算重复。

        注意归一只发生在**空白**上：中文不靠空格分词，所以
        「今天很累」与「今天 很累」仍是两条。这是刻意的——按字符集合
        去重要么得引入分词器，要么会把「他不来了」和「他来了」判成同一条。
        """
        text = '[{"content": "今天 很累"}, {"content": "今天   很累"}]'
        assert len(parse_memory_drafts(text)) == 1

    def test_content_must_be_a_non_blank_string(self) -> None:
        with pytest.raises(ValueError, match="缺少 content"):
            parse_memory_drafts('[{"importance": 0.8}]')

    def test_a_boolean_importance_is_rejected(self) -> None:
        """``True`` 在 Python 里就是 ``1``——会静静地变成「这辈子最重要的事」。"""
        with pytest.raises(ValueError, match="不是数字"):
            parse_memory_drafts('[{"content": "甲", "importance": true}]')

    def test_a_textual_importance_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="不是数字"):
            parse_memory_drafts('[{"content": "甲", "importance": "很高"}]')

    def test_an_unknown_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="不是合法记忆类型"):
            parse_memory_drafts('[{"content": "甲", "kind": "dream"}]')

    def test_non_string_entities_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="非字符串项"):
            parse_memory_drafts('[{"content": "甲", "entities": [{"who": "阿哲"}]}]')

    def test_a_non_object_item_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="不是对象"):
            parse_memory_drafts('["今天很累"]')

    def test_one_bad_item_discards_the_whole_batch(self) -> None:
        """留一半比全丢更危险：那半会出现在下一次的 {existing_memories} 里。"""
        text = '[{"content": "好的"}, {"content": "", "importance": 0.9}]'
        with pytest.raises(ValueError):
            parse_memory_drafts(text)

    def test_over_the_limit_the_batch_is_rejected(self) -> None:
        text = "[" + ",".join(f'{{"content": "第{i}条"}}' for i in range(MAX_ITEMS + 1)) + "]"
        with pytest.raises(ValueError, match="超过上限"):
            parse_memory_drafts(text)

    def test_exactly_the_limit_is_fine(self) -> None:
        text = "[" + ",".join(f'{{"content": "第{i}条"}}' for i in range(MAX_ITEMS)) + "]"
        assert len(parse_memory_drafts(text)) == MAX_ITEMS

    def test_the_limit_is_a_parameter(self) -> None:
        text = '[{"content": "甲"}, {"content": "乙"}]'
        with pytest.raises(ValueError, match="超过上限"):
            parse_memory_drafts(text, max_items=1)


# ────────────────────────────────────────────────────────────
# MemoryDraft · 草稿自己不合法，就不该有落库的机会
# ────────────────────────────────────────────────────────────


class TestMemoryDraft:
    def test_it_rejects_blank_content(self) -> None:
        with pytest.raises(ValueError, match="不能为空白"):
            MemoryDraft(content="  ")

    def test_it_rejects_importance_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="重要度"):
            MemoryDraft(content="甲", importance=1.5)

    def test_it_rejects_valence_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="情绪效价"):
            MemoryDraft(content="甲", valence=-2.0)

    def test_summary_comes_from_content(self) -> None:
        assert MemoryDraft(content="今天很累。明天再说。").summary == "今天很累。"

    def test_to_memory_marks_it_as_derived(self) -> None:
        """``source`` 必须是 ``consolidation``：这是归纳出来的，不是亲历的。"""
        memory = MemoryDraft(content="我好像不太想说话。").to_memory(
            persona_id="p1",
            memory_id="m1",
            occurred_at=T0,
            created_at=T0,
            source_ref="act-1,act-2",
        )
        assert memory.source == "consolidation"
        assert memory.source_ref == "act-1,act-2"
        assert memory.summary == "我好像不太想说话。"
        assert memory.id == "m1"
        assert memory.persona_id == "p1"
        assert memory.occurred_at == T0

    def test_to_memory_carries_tags(self) -> None:
        memory = MemoryDraft(content="甲").to_memory(
            persona_id="p1",
            memory_id="m1",
            occurred_at=T0,
            created_at=T0,
            tags=("梳理",),
        )
        assert memory.tags == ("梳理",)

    def test_occurred_at_is_not_invented_by_the_model(self) -> None:
        """时间由调用方给。模型编出来的时间是幻觉的一部分。"""
        memory = MemoryDraft(content="甲").to_memory(
            persona_id="p1",
            memory_id="m1",
            occurred_at=T0,
            created_at=T0,
        )
        assert memory.occurred_at == T0
        assert memory.created_at == T0

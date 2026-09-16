"""``sim/persona_view.py`` 的测试：写提示词时真正会读到的那几个字段。

这个模块存在的原因是「``persona_json`` 的形状还没有定论」。形状没定论意味着
它会变，而一个读不到语气的人是能说话的（只是没个性），一个因为少了 ``tone``
键就抛异常的人是彻底说不了话的。

所以这里几乎每一条测试都在问同一个问题：**文档里缺了东西会怎样**。
另外两条同样重要的约定是「兜底不能是空串」（模板里 ``{persona}`` 空掉，
模型会自己编一个人格，而且每轮编得都不一样）和「视图是只读的」。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from alterego.interfaces.repository import PersonaRecord
from alterego.sim.persona_view import EXPRESSION_KEYS, SUMMARY_KEYS, PersonaView


RECORD = PersonaRecord(
    id="p1",
    name="林晚",
    age=27,
    gender="女",
    city="杭州",
    occupation="算法工程师",
)


def build(**overrides: object) -> PersonaView:
    values: dict[str, object] = {"record": RECORD}
    values.update(overrides)
    return PersonaView.build(**values)  # type: ignore[arg-type]


# ── 从表里那几列拼出来 ──────────────────────────────────────


class TestFromTheRecord:
    def test_the_fixed_columns_come_across(self) -> None:
        view = build()
        assert view.id == "p1"
        assert view.name == "林晚"
        assert view.age == 27
        assert view.gender == "女"
        assert view.city == "杭州"
        assert view.occupation == "算法工程师"

    def test_the_user_name_is_passed_in_separately(self) -> None:
        """它不在这张表里——「用户叫什么」是用户的事，不是它的事。"""
        assert build(user_name="小陈").user_name == "小陈"

    def test_no_user_name_is_an_empty_string_not_none(self) -> None:
        assert build().user_name == ""

    def test_a_record_without_a_job_or_city_still_builds(self) -> None:
        bare = PersonaRecord(id="p2", name="无名")
        view = PersonaView.build(bare)
        assert view.age is None
        assert view.occupation == ""

    def test_an_empty_document_is_normal(self) -> None:
        """``alterego persona`` 没跑过时文档是空的，它仍然应该能说话。"""
        assert build(document={}).expression == {}
        assert build(document=None).document == {}

    def test_the_document_is_kept_whole(self) -> None:
        """``--show-prompt`` 要能回答「这句话是根据什么生成的」。"""
        raw = {"summary": "我是林晚。", "favourite_food": "小面"}
        assert build(document=raw).document == raw


# ── 自我介绍 ────────────────────────────────────────────────


class TestSummary:
    def test_the_document_wins_over_the_fallback(self) -> None:
        assert build(document={"summary": "我是林晚，写代码的。"}).summary == "我是林晚，写代码的。"

    def test_whitespace_is_stripped(self) -> None:
        assert build(document={"summary": "  我是林晚  "}).summary == "我是林晚"

    def test_a_blank_string_is_not_a_summary(self) -> None:
        assert build(document={"summary": "   "}).summary.startswith("我是林晚")

    @pytest.mark.parametrize("key", SUMMARY_KEYS)
    def test_every_candidate_key_is_honoured(self, key: str) -> None:
        """这是一份**猜测清单**：提示词每改一次就可能换一个键名。"""
        assert build(document={key: "自定义介绍"}).summary == "自定义介绍"

    def test_the_keys_are_tried_in_order(self) -> None:
        document = {"description": "第二选择", "summary": "第一选择"}
        assert build(document=document).summary == "第一选择"

    def test_a_later_key_fills_in_when_the_first_is_missing(self) -> None:
        assert build(document={"bio": "备选介绍"}).summary == "备选介绍"

    def test_a_list_is_not_a_summary(self) -> None:
        """``['a','b']`` 字符串化塞进提示词会给它一段带引号的垃圾。"""
        assert "[" not in build(document={"summary": ["我是林晚", "写代码的"]}).summary

    def test_a_number_is_not_a_summary(self) -> None:
        assert build(document={"summary": 42}).summary.startswith("我是林晚")

    def test_the_fallback_uses_the_job_and_city(self) -> None:
        assert build(document={}).summary == "我是林晚，算法工程师，杭州。"

    def test_the_fallback_survives_a_missing_job(self) -> None:
        bare = PersonaRecord(id="p2", name="无名", city="上海")
        assert PersonaView.build(bare).summary == "我是无名，上海。"

    def test_the_fallback_is_never_empty(self) -> None:
        """一句只有名字的话也比空串好——空串会让模型自己编一个人格。"""
        bare = PersonaRecord(id="p3", name="无名")
        assert PersonaView.build(bare).summary == "我是无名。"

    def test_the_fallback_says_nothing_about_personality(self) -> None:
        """事实来自数据库，性格来自文档。编一句性格进来会让「语气变了」查不出原因。"""
        summary = build(document={}).summary
        assert "温柔" not in summary
        assert "开朗" not in summary


# ── 表达风格 ────────────────────────────────────────────────


class TestExpression:
    def test_the_document_provides_it(self) -> None:
        style = {"tone": "很冷淡", "verbosity": "一句话"}
        assert build(document={"expression": style}).expression == style

    @pytest.mark.parametrize("key", EXPRESSION_KEYS)
    def test_every_candidate_key_is_honoured(self, key: str) -> None:
        assert build(document={key: {"tone": "x"}}).expression == {"tone": "x"}

    def test_the_keys_are_tried_in_order(self) -> None:
        document = {"voice": {"tone": "第二选择"}, "expression": {"tone": "第一选择"}}
        assert build(document=document).expression == {"tone": "第一选择"}

    def test_an_empty_mapping_is_skipped_in_favour_of_a_later_key(self) -> None:
        document = {"expression": {}, "style": {"tone": "有内容"}}
        assert build(document=document).expression == {"tone": "有内容"}

    def test_a_string_is_not_a_style(self) -> None:
        assert build(document={"expression": "很冷淡"}).expression == {}

    def test_a_list_is_not_a_style(self) -> None:
        assert build(document={"expression": ["很冷淡"]}).expression == {}

    def test_an_unknown_key_is_ignored(self) -> None:
        assert build(document={"mood_board": {"tone": "x"}}).expression == {}

    def test_keys_are_coerced_to_strings(self) -> None:
        """JSON 的键本来就是字符串，但手工拼的字典可能不是。"""
        assert build(document={"expression": {1: "x"}}).expression == {"1": "x"}

    def test_the_values_are_left_alone(self) -> None:
        """值可能是列表或嵌套字典——那一层的形状由提示词决定，这里不猜。"""
        style = {"catchphrases": ["哎", "嗯"], "emoji_habit": {"level": 0}}
        assert build(document={"expression": style}).expression == style


# ── 拼成提示词 ──────────────────────────────────────────────


class TestText:
    def test_it_includes_the_summary(self) -> None:
        assert "我是林晚" in build().text()

    def test_it_includes_the_facts_it_has(self) -> None:
        text = build().text()
        assert "27 岁" in text
        assert "女" in text
        assert "在杭州" in text
        assert "职业是算法工程师" in text

    def test_missing_facts_do_not_leave_empty_pieces(self) -> None:
        """「27 岁，，职业是」这种断句会让模型读出一段坏掉的人设。"""
        bare = PersonaRecord(id="p2", name="无名")
        text = PersonaView.build(bare).text()
        assert "，，" not in text
        assert not text.endswith("，")

    def test_a_record_with_nothing_but_a_name_still_says_something(self) -> None:
        assert PersonaView.build(PersonaRecord(id="p3", name="无名")).text() == "我是无名。"

    def test_it_is_never_empty(self) -> None:
        """``{persona}`` 空掉时模型会自己编一个人格，而编出来的每轮都不一样。"""
        assert PersonaView.build(PersonaRecord(id="p4", name="")).text()

    def test_it_is_a_single_line(self) -> None:
        """模板里 ``{persona}`` 占一行，换行会把后面的字段挤出提示词。"""
        assert "\n" not in build().text()

    def test_it_uses_a_chinese_comma(self) -> None:
        assert build().text().count("，") >= 4

    def test_the_summary_is_replaced_but_the_facts_stay(self) -> None:
        text = build(document={"summary": "我是林晚，做算法的。"}).text()
        assert text.startswith("我是林晚，做算法的。")
        assert "27 岁" in text


# ── 只读 ────────────────────────────────────────────────────


class TestReadOnly:
    def test_the_view_cannot_be_modified(self) -> None:
        """它是 ``StateSnapshot`` 的一部分，一次 tick 里的人设不该被中途改掉。"""
        view = build()
        with pytest.raises(FrozenInstanceError):
            view.name = "别人"  # type: ignore[misc]

    def test_it_compares_by_value(self) -> None:
        assert build() == build()

    def test_two_views_with_different_documents_differ(self) -> None:
        assert build(document={"summary": "甲"}) != build(document={"summary": "乙"})


class TestKeyLists:
    def test_the_summary_keys_start_with_the_most_trusted_one(self) -> None:
        assert SUMMARY_KEYS[0] == "summary"

    def test_the_expression_keys_start_with_the_most_trusted_one(self) -> None:
        assert EXPRESSION_KEYS[0] == "expression"

    def test_the_two_lists_do_not_overlap(self) -> None:
        """一份文档里 ``summary`` 是「我是谁」，``expression`` 是「我怎么说话」。"""
        assert not set(SUMMARY_KEYS) & set(EXPRESSION_KEYS)

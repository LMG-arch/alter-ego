"""``domain/study.py`` 的规矩：学什么、学到哪、什么时候该翻出来用。

这个文件里最要紧的两条断言是：

1. **认不出方向就返回 `None`，不编一个出来。** 让角色深耕一门我们瞎猜的
   专业，比让它说「我不知道我该学什么」糟得多。
2. **召回是纯函数：同样的输入永远同样的输出。** 「它怎么突然说起这个」
   必须能答，所以排序里不能有任何依赖输入顺序的东西。

依据: docs/plans/2026-09-16-specialized-study.md § 2–4
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from alterego.domain.knowledge import Note
from alterego.domain.study import (
    ASPECTS,
    DEFAULT_MIN_SCORE,
    MISSING_FIELD_HINT,
    STATE_FILENAME,
    Field,
    Progress,
    Topic,
    advance,
    curriculum,
    field_hint,
    field_key,
    next_topics,
    parse_progress,
    parse_study_note,
    render_context,
    render_note_body,
    resolve_field,
    score_note,
    select,
    terms,
    to_payload,
)


TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 16, 21, 40, tzinfo=TZ)

DATA = Field(key="data", label="数据与算法", source="config")


def note(
    title: str = "数据与算法 · 是什么",
    *,
    path: str = "60-专业/数据与算法 · 是什么.md",
    tags: tuple[str, ...] = (),
    body: str = "",
) -> Note:
    return Note(path=path, title=title, type="专业", created=T0, tags=tags, body=body)


# ────────────────────────────────────────────────────────────
# 学什么
# ────────────────────────────────────────────────────────────


class TestFieldKey:
    def test_chinese_survives(self) -> None:
        """进度文件是 JSON，中文键读起来就是那门学问的名字。"""
        assert field_key("计算机视觉") == "计算机视觉"

    def test_case_is_folded(self) -> None:
        assert field_key("Computer Vision") == "computer-vision"

    def test_whitespace_becomes_a_dash(self) -> None:
        assert field_key("Data  Science") == "data-science"

    def test_edges_are_trimmed(self) -> None:
        assert field_key("  --数据--  ") == "数据"

    @pytest.mark.parametrize("raw", ["", "   ", "!!!", "---"])
    def test_nothing_left_is_unnamed(self, raw: str) -> None:
        """空键会让所有方向共用一份进度，比难看严重得多。"""
        assert field_key(raw) == "unnamed"

    def test_it_is_idempotent(self) -> None:
        """同一个方向跑两遍必须得到同一个键，否则进度会凭空重置。"""
        once = field_key("数据 / 分析")
        assert field_key(once) == once


class TestResolveField:
    def test_config_wins(self) -> None:
        """用户说了算：填了就照填的来，不再去猜 occupation。"""
        field = resolve_field("机器学习", occupation="医生")
        assert field is not None
        assert field.key == "机器学习"
        assert field.label == "机器学习"
        assert field.source == "config"
        assert field.evidence == ""

    def test_blank_config_falls_back_to_occupation(self) -> None:
        field = resolve_field("   ", occupation="程序员")
        assert field is not None
        assert field.label == "计算机软件"
        assert field.source == "occupation"
        assert field.evidence == "程序员"

    @pytest.mark.parametrize(
        ("occupation", "label"),
        [
            ("算法工程师", "数据与算法"),
            ("数据分析师", "数据与算法"),
            ("前端工程师", "计算机软件"),
            ("产品经理", "产品与设计"),
            ("新媒体运营", "运营与增长"),
            ("会计", "财务与会计"),
            ("律师", "法律"),
            ("医生", "医学与健康"),
            ("中学老师", "教育与科研"),
            ("土木工程师", "建筑与土木"),
            ("嵌入式开发", "制造与硬件"),
            ("记者", "传媒与内容"),
            ("HRBP", "人力资源"),
            ("大客户销售", "销售与客户"),
            ("摄影师", "艺术与表演"),
        ],
    )
    def test_it_recognises_every_builtin_field(self, occupation: str, label: str) -> None:
        field = resolve_field("", occupation=occupation)
        assert field is not None
        assert field.label == label

    def test_the_table_order_decides_ties(self) -> None:
        """「人力资源数据分析」同时像两个方向，表里靠前的那个赢。"""
        field = resolve_field("", occupation="人力资源数据分析")
        assert field is not None
        assert field.label == "数据与算法"

    @pytest.mark.parametrize("occupation", ["研究生", "在读硕士", "大学生", "实习生"])
    def test_a_student_is_not_a_field(self, occupation: str) -> None:
        """「研究生」看得出在读书，看不出读的是什么——那是当事人知道的。"""
        assert resolve_field("", occupation=occupation) is None

    def test_nothing_at_all_is_none(self) -> None:
        assert resolve_field("", occupation="") is None
        assert resolve_field("   ", occupation="   ") is None

    def test_an_unknown_occupation_is_none(self) -> None:
        assert resolve_field("", occupation="养蜂人") is None


class TestFieldHint:
    def test_it_says_there_is_no_occupation_at_all(self) -> None:
        hint = field_hint("")
        assert MISSING_FIELD_HINT in hint
        assert "occupation" in hint

    def test_it_asks_a_student_for_a_major(self) -> None:
        hint = field_hint("研究生")
        assert "研究生" in hint
        assert "[study] field" in hint

    def test_it_tells_other_people_the_name_need_not_be_a_job_title(self) -> None:
        hint = field_hint("养蜂人")
        assert "养蜂人" in hint
        assert "对不上任何内置领域" in hint
        assert "[study] field" in hint


# ────────────────────────────────────────────────────────────
# 学什么，按顺序
# ────────────────────────────────────────────────────────────


class TestCurriculum:
    def test_the_first_round_is_the_five_aspects(self) -> None:
        topics = curriculum(DATA, rounds=1)
        assert tuple(topic.aspect for topic in topics) == ASPECTS
        assert tuple(topic.round for topic in topics) == (1,) * len(ASPECTS)

    def test_the_first_round_has_no_suffix(self) -> None:
        assert curriculum(DATA, rounds=1)[0].title == "数据与算法 · 是什么"

    def test_later_rounds_say_which_round(self) -> None:
        topics = curriculum(DATA, rounds=2)
        assert topics[len(ASPECTS)].title == "数据与算法 · 是什么（第 2 轮）"

    def test_the_key_carries_round_and_aspect(self) -> None:
        assert curriculum(DATA, rounds=2)[0].key == "data/1/是什么"
        assert curriculum(DATA, rounds=2)[-1].key == "data/2/我还不服的"

    @pytest.mark.parametrize("rounds", [0, -1, -100])
    def test_no_rounds_no_topics(self, rounds: int) -> None:
        assert curriculum(DATA, rounds=rounds) == ()

    def test_rounds_multiply(self) -> None:
        assert len(curriculum(DATA, rounds=3)) == 3 * len(ASPECTS)

    def test_it_is_deterministic(self) -> None:
        """顺序即进度：换一次顺序，之前记的进度就全对不上了。"""
        assert curriculum(DATA, rounds=4) == curriculum(DATA, rounds=4)

    def test_the_aspect_keeps_the_plain_name(self) -> None:
        """题面带轮次，但 aspect 不带——正文里那句「第 N 轮」用它。"""
        assert curriculum(DATA, rounds=2)[len(ASPECTS)].aspect == ASPECTS[0]


class TestNextTopics:
    def test_an_empty_progress_starts_at_the_beginning(self) -> None:
        topics = next_topics(DATA, progress=Progress(field_key="data"), rounds=2)
        assert tuple(topic.key for topic in topics) == ("data/1/是什么", "data/1/怎么做")

    def test_it_skips_what_is_already_done(self) -> None:
        done = ("data/1/是什么", "data/1/怎么做")
        progress = Progress(field_key="data", done=done)
        topics = next_topics(DATA, progress=progress, rounds=1)
        assert topics[0].key == "data/1/容易踩的坑"

    def test_it_still_finds_topics_after_several_rounds(self) -> None:
        """**这是修过的 bug**：只展开 ``rounds`` 轮的话，进度走到第 3 轮时
        表里一个都不剩，于是「学完了」——而它其实才刚起步。"""
        full = Progress(
            field_key="data",
            done=tuple(topic.key for topic in curriculum(DATA, rounds=3)),
        )
        topics = next_topics(DATA, progress=full, rounds=1)
        assert topics[0].round == 4

    @pytest.mark.parametrize("rounds", [0, -3])
    def test_no_rounds_no_topics(self, rounds: int) -> None:
        assert next_topics(DATA, progress=Progress(field_key="data"), rounds=rounds) == ()

    def test_it_never_returns_a_done_topic(self) -> None:
        done = tuple(topic.key for topic in curriculum(DATA, rounds=2))
        found = next_topics(DATA, progress=Progress(field_key="data", done=done), rounds=9)
        assert set(done).isdisjoint({topic.key for topic in found})

    def test_it_returns_exactly_what_was_asked_for(self) -> None:
        topics = next_topics(DATA, progress=Progress(field_key="data"), rounds=3)
        assert len(topics) == 3


# ────────────────────────────────────────────────────────────
# 学到哪了
# ────────────────────────────────────────────────────────────


class TestProgress:
    def test_an_empty_progress_is_zero_rounds(self) -> None:
        assert Progress(field_key="data").rounds_done == 0

    def test_full_rounds_are_counted(self) -> None:
        done = tuple(topic.key for topic in curriculum(DATA, rounds=2))
        assert Progress(field_key="data", done=done).rounds_done == 2

    def test_a_started_next_round_counts_too(self) -> None:
        done = (*tuple(topic.key for topic in curriculum(DATA, rounds=1)), "data/2/是什么")
        assert Progress(field_key="data", done=done).rounds_done == 1

    def test_a_nearly_finished_round_is_not_a_round(self) -> None:
        done = (*tuple(topic.key for topic in curriculum(DATA, rounds=1))[:3], "data/2/是什么")
        assert Progress(field_key="data", done=done).rounds_done == 0


class TestParseProgress:
    def test_it_round_trips(self) -> None:
        original = Progress(field_key="data", done=("data/1/是什么",), last_at="2026-09-16")
        assert parse_progress(to_payload(original), field=DATA) == original

    def test_a_payload_for_another_field_is_empty(self) -> None:
        """两门专业的主题混在一个进度里，「学到第几个了」就没意义了。"""
        payload = {"field_key": "software", "done": ["software/1/是什么"], "last_at": "x"}
        parsed = parse_progress(payload, field=DATA)
        assert parsed.done == ()
        assert parsed.field_key == "data"

    def test_an_empty_payload_is_empty(self) -> None:
        assert parse_progress({}, field=DATA).done == ()

    def test_a_missing_field_key_is_empty(self) -> None:
        assert parse_progress({"done": ["data/1/是什么"]}, field=DATA).done == ()

    def test_junk_in_done_is_dropped(self) -> None:
        payload = {"field_key": "data", "done": ["data/1/是什么", "", 7, None]}
        assert parse_progress(payload, field=DATA).done == ("data/1/是什么",)

    def test_a_bare_string_in_done_counts_as_one(self) -> None:
        payload = {"field_key": "data", "done": "data/1/是什么"}
        assert parse_progress(payload, field=DATA).done == ("data/1/是什么",)

    def test_done_that_is_not_a_list_is_empty(self) -> None:
        assert parse_progress({"field_key": "data", "done": 42}, field=DATA).done == ()

    def test_a_non_string_timestamp_is_empty(self) -> None:
        payload = {"field_key": "data", "done": [], "last_at": 123}
        assert parse_progress(payload, field=DATA).last_at == ""

    def test_the_timestamp_survives(self) -> None:
        payload = {"field_key": "data", "done": [], "last_at": "2026-09-16T21:40:00"}
        assert parse_progress(payload, field=DATA).last_at == "2026-09-16T21:40:00"


class TestAdvance:
    def test_it_records_the_topics(self) -> None:
        topics = curriculum(DATA, rounds=1)[:2]
        progress = advance(Progress(field_key="data"), field=DATA, topics=topics)
        assert progress.done == tuple(topic.key for topic in topics)

    def test_it_is_idempotent(self) -> None:
        """同一个主题记两次还是一条——重复学习不该让进度翻倍。"""
        topics = curriculum(DATA, rounds=1)[:2]
        once = advance(Progress(field_key="data"), field=DATA, topics=topics)
        assert advance(once, field=DATA, topics=topics) == once

    def test_it_keeps_what_was_already_there(self) -> None:
        first = advance(
            Progress(field_key="data"), field=DATA, topics=curriculum(DATA, rounds=1)[:1]
        )
        second = advance(first, field=DATA, topics=curriculum(DATA, rounds=1)[1:2])
        assert len(second.done) == 2

    def test_it_resets_when_the_field_changed(self) -> None:
        """换了方向就是换了门学问，旧进度留着只会让它「学在第 3 格」。"""
        stale = Progress(field_key="software", done=("software/1/是什么",))
        fresh = advance(stale, field=DATA, topics=curriculum(DATA, rounds=1)[:1])
        assert fresh.field_key == "data"
        assert "software/1/是什么" not in fresh.done

    def test_it_stamps_the_time(self) -> None:
        moved = advance(
            Progress(field_key="data"),
            field=DATA,
            topics=curriculum(DATA, rounds=1)[:1],
            at="2026-09-16T21:40:00",
        )
        assert moved.last_at == "2026-09-16T21:40:00"

    def test_it_keeps_the_old_stamp_when_no_time_is_given(self) -> None:
        old = Progress(field_key="data", done=(), last_at="2026-09-01T00:00:00")
        moved = advance(old, field=DATA, topics=curriculum(DATA, rounds=1)[:1])
        assert moved.last_at == "2026-09-01T00:00:00"

    def test_no_topics_is_a_no_op(self) -> None:
        empty = Progress(field_key="data", done=("data/1/是什么",))
        assert advance(empty, field=DATA, topics=()) == empty


class TestToPayload:
    def test_it_can_be_written_to_json(self) -> None:
        import json

        payload = to_payload(Progress(field_key="data", done=("data/1/是什么",), last_at="t"))
        assert json.loads(json.dumps(payload))["done"] == ["data/1/是什么"]

    def test_done_is_a_list_not_a_tuple(self) -> None:
        """元组进 JSON 会变成数组，但读回来再读出去必须稳定。"""
        payload = to_payload(Progress(field_key="data", done=("a", "b")))
        assert isinstance(payload["done"], list)

    def test_the_keys_are_stable(self) -> None:
        assert set(to_payload(Progress(field_key="data"))) == {"done", "field_key", "last_at"}


# ────────────────────────────────────────────────────────────
# 学到了什么
# ────────────────────────────────────────────────────────────


class TestParseStudyNote:
    def test_a_complete_reply_is_accepted(self) -> None:
        draft = parse_study_note('{"summary": "一句话", "points": ["一", "二"]}')
        assert draft is not None
        assert draft.summary == "一句话"
        assert draft.points == ("一", "二")
        assert draft.unsure == ()

    def test_it_finds_the_json_inside_prose(self) -> None:
        reply = '好的，我记一下：\n```json\n{"summary": "一句话", "points": ["一"]}\n```\n'
        draft = parse_study_note(reply)
        assert draft is not None
        assert draft.points == ("一",)

    def test_the_summary_is_stripped(self) -> None:
        draft = parse_study_note('{"summary": "  一句话  ", "points": ["一"]}')
        assert draft is not None
        assert draft.summary == "一句话"

    def test_unsure_is_kept(self) -> None:
        draft = parse_study_note('{"summary": "s", "points": ["一"], "unsure": ["不确定"]}')
        assert draft is not None
        assert draft.unsure == ("不确定",)

    @pytest.mark.parametrize(
        "reply",
        [
            "",
            "我看了一下，觉得挺好。",
            "{ 不是 json }",
            '["summary"]',
            '{"points": ["一"]}',
            '{"summary": "", "points": ["一"]}',
            '{"summary": "   ", "points": ["一"]}',
            '{"summary": "一句话"}',
            '{"summary": "一句话", "points": []}',
        ],
    )
    def test_an_incomplete_reply_is_rejected(self, reply: str) -> None:
        """宁可不写，也不写下一篇只有标题的笔记——它会变成一份它没有的学历。"""
        assert parse_study_note(reply) is None

    def test_a_bare_string_point_counts_as_one(self) -> None:
        draft = parse_study_note('{"summary": "s", "points": "只有一条"}')
        assert draft is not None
        assert draft.points == ("只有一条",)

    def test_the_first_object_wins(self) -> None:
        """文字里夹了第二个对象就整段读不出来——宁可不成，也不猜哪段是笔记。"""
        reply = '{"summary": "a", "points": ["x"]} 后面还有 {"summary": "b"}'
        assert parse_study_note(reply) is None


class TestRenderNoteBody:
    def test_it_always_has_the_three_sections(self) -> None:
        draft = parse_study_note('{"summary": "一句话", "points": ["一"]}')
        assert draft is not None
        body = render_note_body(draft, field=DATA, topic=curriculum(DATA, rounds=1)[0])
        assert "## 一句话" in body
        assert "## 记下来的" in body
        assert "## 出处" in body

    def test_the_unsure_section_appears_only_when_used(self) -> None:
        """骨架是机制不是格式：不确定的那一节不许被默认省略掉。"""
        topic = curriculum(DATA, rounds=1)[0]
        quiet = parse_study_note('{"summary": "s", "points": ["一"]}')
        loud = parse_study_note('{"summary": "s", "points": ["一"], "unsure": ["不知道"]}')
        assert quiet is not None and loud is not None
        assert "## 我还不确定的" not in render_note_body(quiet, field=DATA, topic=topic)
        assert "## 我还不确定的" in render_note_body(loud, field=DATA, topic=topic)

    def test_it_says_where_it_came_from(self) -> None:
        topic = curriculum(DATA, rounds=2)[len(ASPECTS)]
        draft = parse_study_note('{"summary": "s", "points": ["一"]}')
        assert draft is not None
        body = render_note_body(draft, field=DATA, topic=topic)
        assert "- 领域：数据与算法" in body
        assert f"- 这一轮：第 {topic.round} 轮 · {topic.aspect}" in body
        assert "这是我自己学的" in body

    def test_every_point_becomes_a_bullet(self) -> None:
        draft = parse_study_note('{"summary": "s", "points": ["一", "二", "三"]}')
        assert draft is not None
        body = render_note_body(draft, field=DATA, topic=curriculum(DATA, rounds=1)[0])
        assert body.count("- ") >= 3


# ────────────────────────────────────────────────────────────
# 什么时候该翻出来用
# ────────────────────────────────────────────────────────────


class TestTerms:
    def test_chinese_becomes_bigrams(self) -> None:
        assert terms("卷积神经网络") == ("卷积", "积神", "神经", "经网", "网络")

    def test_latin_words_and_chinese_together(self) -> None:
        assert terms("卷积神经网络 CNN") == ("cnn", "卷积", "积神", "神经", "经网", "网络")

    def test_a_single_chinese_character_survives(self) -> None:
        assert "好" in terms("好")

    def test_duplicates_keep_the_first_position(self) -> None:
        assert terms("网络 网络") == ("网络",)

    def test_case_is_folded(self) -> None:
        assert terms("CNN cnn") == ("cnn",)

    def test_a_one_letter_word_is_dropped(self) -> None:
        assert terms("a cc") == ("cc",)

    def test_it_survives_punctuation_only(self) -> None:
        assert terms("！！！ …… ") == ()

    def test_it_is_deterministic(self) -> None:
        assert terms("降维 与 卷积") == terms("降维 与 卷积")

    def test_cpp_keeps_the_letters(self) -> None:
        """``_KEY_CLEAN`` 会吃掉加号，切词这边不必——搜得到就行。"""
        assert "c++" in terms("C++ 宏")

    @pytest.mark.parametrize("word", ["什么", "怎么", "今天", "可以", "我们", "应该"])
    def test_a_function_word_is_dropped(self, word: str) -> None:
        """虚词不能当命中。

        第 1 轮的题面全都是 ``X · 是什么``，所以漏掉 ``什么`` 就等于
        「任何一句带疑问词的话都翻出全部第一轮」——真实使用里最刺眼的
        一种噪声，而且是那种不会报错的噪声。
        """
        assert word not in terms(f"你好{word}啊")

    def test_the_real_content_survives_the_filter(self) -> None:
        """挡噪声不等于挡信号：丢掉「是什么」之后，题面里还得留下东西。"""
        assert "是什" in terms("降维是什么")
        assert "么做" in terms("降维怎么做")


class TestExcerpt:
    def test_it_skips_headings_and_takes_the_first_sentence(self) -> None:
        from alterego.domain.study import _excerpt

        body = "## 一句话\n\n这一层把高维压到低维。\n\n## 记下来的\n\n- 要点一"
        assert _excerpt(body) == "这一层把高维压到低维。"

    def test_it_skips_bullet_markers(self) -> None:
        from alterego.domain.study import _excerpt

        assert _excerpt("- 要点一") == "要点一"

    def test_it_truncates_a_long_line(self) -> None:
        from alterego.domain.study import _excerpt

        long_line = "啊" * 200
        result = _excerpt(long_line, limit=10)
        assert result == "啊" * 10 + "…"

    def test_a_body_with_nothing_to_say_gives_an_empty_string(self) -> None:
        from alterego.domain.study import _excerpt

        assert _excerpt("") == ""
        assert _excerpt("## 只有标题\n") == ""


class TestScoreNote:
    def test_a_title_hit_beats_a_tag_hit(self) -> None:
        title = score_note(note(title="卷积是什么"), query_terms=("卷积",))[0]
        tag = score_note(note(title="别的", tags=("卷积",)), query_terms=("卷积",))[0]
        assert title > tag

    def test_a_tag_hit_beats_a_body_hit(self) -> None:
        tag = score_note(note(title="别的", tags=("卷积",)), query_terms=("卷积",))[0]
        body = score_note(note(title="别的", body="卷积"), query_terms=("卷积",))[0]
        assert tag > body

    def test_no_hit_scores_zero(self) -> None:
        assert score_note(note(title="卷积"), query_terms=("神经",)) == (0.0, ())

    def test_the_hits_come_back_in_query_order(self) -> None:
        """同样的输入必须得到同样的顺序，否则日志读起来每次都不同。"""
        _, hit = score_note(note(title="卷积网络"), query_terms=("网络", "卷积"))
        assert hit == ("网络", "卷积")

    def test_a_longer_body_is_damped(self) -> None:
        short = score_note(note(body="卷积"), query_terms=("卷积",))[0]
        long_body = "卷积" + "。".join(f"第{index}个不相干的词" for index in range(300))
        long = score_note(note(body=long_body), query_terms=("卷积",))[0]
        assert long < short

    def test_damping_never_makes_a_hit_negative(self) -> None:
        assert score_note(note(body="卷积"), query_terms=("卷积",))[0] > 0


class TestSelect:
    def test_a_matching_note_is_found(self) -> None:
        found = select([note(title="卷积网络是什么")], query="卷积怎么降维")
        assert found.hit
        assert found.matches[0].title == "卷积网络是什么"

    def test_the_excerpt_comes_along(self) -> None:
        body = "## 一句话\n\n把高维压到低维。"
        found = select([note(title="卷积", body=body)], query="卷积")
        assert found.matches[0].excerpt == "把高维压到低维。"

    def test_a_weak_hit_is_below_the_threshold(self) -> None:
        """正文里偶发共用一个字，不该赢过一次正常的对话。"""
        found = select([note(title="别的", body="网")], query="网")
        assert not found.hit

    def test_the_threshold_can_be_lowered(self) -> None:
        found = select([note(title="别的", body="网")], query="网", min_score=0.0)
        assert found.hit

    def test_limit_caps_the_result(self) -> None:
        notes = [note(title=f"卷积 第{index}篇", path=f"60-专业/{index}.md") for index in range(5)]
        assert len(select(notes, query="卷积", limit=2).matches) == 2

    @pytest.mark.parametrize("limit", [0, -1])
    def test_a_non_positive_limit_finds_nothing(self, limit: int) -> None:
        notes = [note(title="卷积")]
        found = select(notes, query="卷积", limit=limit)
        assert found.matches == ()
        assert found.considered == 1

    def test_an_empty_query_finds_nothing(self) -> None:
        found = select([note(title="卷积")], query="")
        assert not found.hit
        assert found.considered == 0

    def test_the_result_does_not_depend_on_input_order(self) -> None:
        """文件扫描顺序一变就换一批笔记，是最难查的一类「它今天怎么怪怪的」。"""
        first = note(title="卷积 甲", path="60-专业/a.md")
        second = note(title="卷积 乙", path="60-专业/b.md")
        forward = select([first, second], query="卷积")
        backward = select([second, first], query="卷积")
        assert forward.matches == backward.matches

    def test_equal_scores_are_broken_by_path(self) -> None:
        later = note(title="卷积", path="60-专业/z.md")
        earlier = note(title="卷积", path="60-专业/a.md")
        found = select([later, earlier], query="卷积")
        assert tuple(match.path for match in found.matches) == ("60-专业/a.md", "60-专业/z.md")

    def test_considered_counts_what_was_scored(self) -> None:
        notes = [note(title=f"别的 {index}", path=f"60-专业/{index}.md") for index in range(3)]
        assert select(notes, query="卷积").considered == 3

    def test_the_threshold_is_reported(self) -> None:
        assert select([], query="卷积", min_score=7.5).threshold == 7.5

    def test_the_default_threshold_is_above_zero(self) -> None:
        found = select([note(title="别的")], query="卷积")
        assert found.threshold == DEFAULT_MIN_SCORE > 0


class TestRenderContext:
    def test_nothing_found_renders_nothing(self) -> None:
        assert render_context(select([], query="卷积")) == ""

    def test_it_names_the_note_and_its_path(self) -> None:
        found = select([note(title="卷积网络")], query="卷积")
        context = render_context(found)
        assert "卷积网络" in context
        assert "60-专业/数据与算法 · 是什么.md" in context

    def test_it_says_these_are_my_own_notes(self) -> None:
        """不写这一句，模型会把这些当成对方刚说的话接着答——比不调用更糟。"""
        context = render_context(select([note(title="卷积")], query="卷积"))
        assert "不是这次对话里对方说的话" in context

    def test_the_excerpt_is_indented_under_the_title(self) -> None:
        body = "## 一句话\n\n把高维压到低维。"
        context = render_context(select([note(title="卷积", body=body)], query="卷积"))
        assert "  把高维压到低维。" in context

    def test_a_tight_budget_drops_the_whole_block(self) -> None:
        """半篇笔记会变成一句像结论的错话，所以丢就整篇丢。"""
        found = select([note(title="卷积网络")], query="卷积")
        context = render_context(found, budget=1)
        assert "卷积网络" not in context
        assert "不是这次对话里对方说的话" in context

    def test_a_roomy_budget_keeps_everything(self) -> None:
        notes = [note(title=f"卷积 第{index}篇", path=f"60-专业/{index}.md") for index in range(3)]
        context = render_context(select(notes, query="卷积"), budget=10000)
        assert context.count("- 卷积 第") == 3


# ────────────────────────────────────────────────────────────
# 常量
# ────────────────────────────────────────────────────────────


def test_the_state_file_is_hidden_from_obsidian() -> None:
    """以点开头 Obsidian 就不显示它——它是机器状态，不是笔记。"""
    assert STATE_FILENAME.startswith(".")


def test_there_are_five_aspects() -> None:
    """前两个是讲清楚，后三个是知道边界。少一个就不是同一门课了。"""
    assert len(ASPECTS) == 5


def test_a_topic_is_a_frozen_value() -> None:
    """题面是不可变的：改一个字段就等于换了一道题，进度会跟着错位。"""
    topic = Topic(key="k", title="t", aspect="是什么", round=1)
    with pytest.raises(FrozenInstanceError):
        topic.round = 2  # type: ignore[misc]

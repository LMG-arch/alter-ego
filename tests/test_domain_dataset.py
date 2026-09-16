"""``domain/dataset.py`` 的规矩：样本从哪来、什么该丢、渲染成什么形状。

三条最要紧的不变量：

1. **准入条件真的在拦人。** 只有 ``user`` 的会话、``status != 'ok'`` 的 tick
   都必须被丢掉——否则训练框架会在读到它们时直接报错，
   而错误信息不会告诉你「是哪一类样本导错了」。
2. **排序是确定的。** 同一批输入跑两次必须得到逐字节相同的文件，
   否则 ``manifest.json`` 里的 sha256 毫无意义（P6）。
3. **JSONL 是**真的** JSONL。** 每行单独 ``json.loads`` 都能过，
   末尾有换行。这是和训练框架之间唯一的硬契约。
"""

from __future__ import annotations

import json
import re

import pytest

from alterego.domain.dataset import (
    DATASET_SPECS,
    FORMATS,
    MESSAGE_PLACEHOLDER,
    REASONING_OPEN,
    ActivityRow,
    ConversationTurn,
    ExportedFile,
    MessageRow,
    TickRow,
    TrainingSample,
    build_conversation_samples,
    build_reasoning_samples,
    build_tooluse_samples,
    merge_adjacent,
    render_manifest,
    render_readme,
    render_sample,
    spec_for,
    to_jsonl,
)


def _message(
    identifier: str,
    *,
    conversation: str = "c1",
    direction: str = "inbound",
    content: str = "在吗",
    content_type: str = "text",
    created_at: str = "2026-01-01T09:00:00+08:00",
) -> MessageRow:
    """造一行 message，省得每个用例写全字段。"""
    return MessageRow(
        id=identifier,
        conversation_id=conversation,
        direction=direction,
        content=content,
        content_type=content_type,
        created_at=created_at,
    )


# ────────────────────────────────────────────────────────────
# 合并相邻同向
# ────────────────────────────────────────────────────────────
def test_merge_adjacent_joins_consecutive_same_direction() -> None:
    """连发三条要并成一轮——拆开会训出吞吞吐吐的模型。"""
    rows = [
        _message("m1", content="在吗"),
        _message("m2", content="在的"),
        _message("m3", content="那个事"),
        _message("m4", direction="outbound", content="说吧"),
    ]
    assert merge_adjacent(rows) == (
        ConversationTurn(role="user", content="在吗\n在的\n那个事"),
        ConversationTurn(role="assistant", content="说吧"),
    )


def test_merge_adjacent_replaces_non_text_with_placeholder() -> None:
    """图片消息留占位，不留空——留空会让对话看起来断了一轮。"""
    rows = [
        _message("m1", content="[base64...]", content_type="image"),
        _message("m2", direction="outbound", content="这张好看"),
    ]
    assert merge_adjacent(rows) == (
        ConversationTurn(role="user", content=MESSAGE_PLACEHOLDER),
        ConversationTurn(role="assistant", content="这张好看"),
    )


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_merge_adjacent_drops_blank(blank: str) -> None:
    """空消息不占位，合并出的正文里也不能多出空行。"""
    rows = [
        _message("m1", content="在吗"),
        _message("m2", content=blank),
        _message("m3", content="在的"),
    ]
    assert merge_adjacent(rows) == (ConversationTurn(role="user", content="在吗\n在的"),)


def test_merge_adjacent_on_empty_input() -> None:
    """空输入返回空，不炸。"""
    assert merge_adjacent([]) == ()


# ────────────────────────────────────────────────────────────
# 对话训练集
# ────────────────────────────────────────────────────────────
def test_conversation_needs_both_roles() -> None:
    """只发了还没回的会话导不出东西。"""
    only_inbound = [
        _message("m1", content="在吗"),
        _message("m2", content="在吗？"),
    ]
    assert build_conversation_samples(only_inbound) == []


def test_conversation_keeps_proactive_opener() -> None:
    """以它主动开口的会话必须留下——那是这类数据里最有价值的一批。"""
    rows = [
        _message("m1", direction="outbound", content="今天路过你上次说的那家店"),
        _message("m2", content="真的吗"),
        _message("m3", direction="outbound", content="真的"),
    ]
    samples = build_conversation_samples(rows)
    assert len(samples) == 1
    assert samples[0].messages[0].role == "assistant"


def test_conversation_groups_by_conversation_id() -> None:
    """两条会话归成两个样本，不串在一起。"""
    rows = [
        _message("m1", conversation="a", content="甲"),
        _message("m2", conversation="a", direction="outbound", content="A"),
        _message("m3", conversation="b", content="乙"),
        _message("m4", conversation="b", direction="outbound", content="B"),
    ]
    samples = build_conversation_samples(rows)
    assert [sample.source for sample in samples] == ["a", "b"]
    assert samples[0].messages[1].content == "A"
    assert samples[1].messages[1].content == "B"


def test_conversation_orders_by_created_at_then_id() -> None:
    """同一秒内的消息靠 id 定序——只看时间会退化成插入顺序。"""
    rows = [
        _message("m2", content="第二句", created_at="2026-01-01T09:00:00+08:00"),
        _message("m1", content="第一句", created_at="2026-01-01T09:00:00+08:00"),
        _message("m9", direction="outbound", content="回", created_at="2026-01-01T09:01:00+08:00"),
    ]
    turns = build_conversation_samples(rows)[0].messages
    assert turns[0].content == "第一句\n第二句"


def test_conversation_is_byte_stable_across_runs() -> None:
    """同一批输入跑两次，JSONL 逐字节相同。"""
    rows = [
        _message("m1", conversation="b", content="乙"),
        _message("m2", conversation="b", direction="outbound", content="B"),
        _message("m3", conversation="a", content="甲"),
        _message("m4", conversation="a", direction="outbound", content="A"),
    ]
    first = to_jsonl(build_conversation_samples(rows), "chat")
    second = to_jsonl(build_conversation_samples(list(reversed(rows))), "chat")
    assert first == second


# ────────────────────────────────────────────────────────────
# 思考推理训练集
# ────────────────────────────────────────────────────────────
def _tick(identifier: str, *, status: str = "ok", **overrides: str) -> TickRow:
    """造一行 tick_log。默认是一个内容完整的、成功的 tick。"""
    fields: dict[str, str] = {
        "state_snapshot_json": '{"mood": "平静", "energy": 0.7}',
        "percepts_json": '["用户发来一条消息"]',
        "candidates_json": '[{"intent": "chat"}, {"intent": "post"}]',
        "chosen_intent": "chat",
        "motivation": "想回她",
        "trigger_note": "她先开口的",
        "suppressed_json": '[{"intent": "分享今天的事", "reason": "刚刚才聊过"}]',
        "memories_json": '["上周一起看过海"]',
        "notes_json": '{"心情": "不错"}',
    }
    fields.update(overrides)
    return TickRow(id=identifier, virtual_time="2026-01-01T09:00:00+08:00", status=status, **fields)


@pytest.mark.parametrize("status", ["partial", "failed", "interrupted", "skipped"])
def test_reasoning_skips_unsuccessful_ticks(status: str) -> None:
    """失败/中断的轨迹不是「它怎么想的」——喂给模型会教它犯错。"""
    assert build_reasoning_samples([_tick("t1", status=status)]) == []


def test_reasoning_keeps_ok_tick() -> None:
    """成功的 tick 变成一条样本，三段齐全。"""
    sample = build_reasoning_samples([_tick("t1")])[0]
    assert sample.kind == "reasoning"
    assert sample.source == "t1"
    assert "感知" in sample.instruction
    assert "用户发来一条消息" in sample.instruction
    assert "考虑过的选项" in sample.reasoning
    assert "压下的念头" in sample.reasoning
    assert "上周一起看过海" in sample.reasoning
    assert "决定" in sample.output
    assert "想回她" in sample.output


def test_reasoning_requires_output() -> None:
    """什么都没决定的 tick 导不出样本。"""
    empty = _tick("t1", chosen_intent="", motivation="", trigger_note="")
    assert build_reasoning_samples([empty]) == []


def test_reasoning_survives_malformed_json() -> None:
    """库里一条脏 JSON 不该让整次导出失败。"""
    broken = _tick("t1", candidates_json="{不是 JSON", percepts_json="")
    samples = build_reasoning_samples([broken])
    assert len(samples) == 1
    assert "{不是 JSON" in samples[0].reasoning


def test_reasoning_orders_by_virtual_time_then_id() -> None:
    """按虚拟时间排序，同一时刻靠 id 定序。"""
    same = "2026-01-01T09:00:00+08:00"
    rows = [
        TickRow(id="t2", virtual_time=same, status="ok", percepts_json='["b"]', chosen_intent="b"),
        TickRow(id="t1", virtual_time=same, status="ok", percepts_json='["a"]', chosen_intent="a"),
    ]
    assert [sample.source for sample in build_reasoning_samples(rows)] == ["t1", "t2"]


# ────────────────────────────────────────────────────────────
# 工具调用训练集
# ────────────────────────────────────────────────────────────
def _activity(identifier: str, **overrides: str | int) -> ActivityRow:
    fields: dict[str, str | int] = {
        "intent": "post_moment",
        "category": "social",
        "description": "发了一条动态",
        "detail_json": '{"text": "今天的云很好看"}',
        "location": "家",
        "inner_voice": "想让她们看到",
        "duration_minutes": 3,
        "tick_id": "t1",
        "started_at": "2026-01-01T09:00:00+08:00",
    }
    fields.update(overrides)
    return ActivityRow(id=identifier, **fields)  # type: ignore[arg-type]


def test_tooluse_names_the_tool_as_category_slash_intent() -> None:
    """工具名沿用能力契约里的 ``形态.动作`` 命名，不另造一套。"""
    sample = build_tooluse_samples([_activity("a1")])[0]
    assert sample.tools == ("social/post_moment",)
    assert "可用工具" in sample.instruction


def test_tooluse_prefers_the_tick_intent_as_request() -> None:
    """有 tick 上下文时，「请求」用它自己定的意图，不是规范化后的意图。"""
    sample = build_tooluse_samples([_activity("a1")], intents_by_tick={"t1": "分享今天看到的云"})[0]
    assert "分享今天看到的云" in sample.instruction


def test_tooluse_falls_back_to_activity_intent() -> None:
    """没有 tick 上下文时退回行为日志里那个意图，不空着。"""
    sample = build_tooluse_samples([_activity("a1", tick_id="")])[0]
    assert "请求" in sample.instruction
    assert "post_moment" in sample.instruction


def test_tooluse_requires_a_result() -> None:
    """没有描述、没有内心的行为行导不出东西。"""
    blank = _activity("a1", description="", inner_voice="", location="")
    assert build_tooluse_samples([blank]) == []


def test_tooluse_is_empty_today_and_that_is_honest() -> None:
    """上游没实现之前，这个数据集必须是 0 条，不能有假数据。"""
    spec = next(item for item in DATASET_SPECS if item.name == "tooluse")
    assert spec.upstream_ready is False
    assert build_tooluse_samples([]) == []


def test_specs_cover_exactly_the_three_datasets() -> None:
    """三类数据集不多不少，名字与训练格式里的约定一致。"""
    assert [spec.name for spec in DATASET_SPECS] == ["conversation", "reasoning", "tooluse"]
    assert all(
        spec.label.strip() and spec.source.strip() and spec.teaches.strip()
        for spec in DATASET_SPECS
    )


# ────────────────────────────────────────────────────────────
# 渲染
# ────────────────────────────────────────────────────────────
def test_render_chat_puts_reasoning_in_a_marked_block() -> None:
    """``chat`` 形状里思考必须被标记出来，否则模型会把思考当正文吐出来。"""
    sample = TrainingSample(
        kind="reasoning",
        source="t1",
        instruction="看到下雨",
        reasoning="要带伞吗",
        output="记得带伞",
    )
    messages = render_sample(sample, "chat")["messages"]
    assert messages[0] == {"role": "user", "content": "看到下雨"}
    assert messages[1]["content"].startswith("<thinking>")
    assert "</thinking>" in messages[1]["content"]
    assert messages[1]["content"].rstrip().endswith("记得带伞")


def test_render_chat_without_reasoning_has_no_marker() -> None:
    """没有思考就别放空标记。"""
    sample = TrainingSample(kind="reasoning", source="t1", instruction="a", output="b")
    assert render_sample(sample, "chat")["messages"][1]["content"] == "b"


def test_render_chat_puts_system_first_when_present() -> None:
    """系统提示永远排第一条。"""
    sample = TrainingSample(
        kind="conversation",
        source="c1",
        system="你是林墨",
        messages=(ConversationTurn(role="user", content="在吗"),),
    )
    messages = render_sample(sample, "chat")["messages"]
    assert messages[0] == {"role": "system", "content": "你是林墨"}
    assert messages[1]["role"] == "user"


def test_render_chat_omits_blank_system() -> None:
    """系统提示是空白就不写这一条。"""
    sample = TrainingSample(
        kind="conversation", source="c1", messages=(ConversationTurn(role="user", content="在吗"),)
    )
    assert [item["role"] for item in render_sample(sample, "chat")["messages"]] == ["user"]


def test_render_sharegpt_renames_roles() -> None:
    """``sharegpt`` 用 human / gpt 这套名字。"""
    sample = TrainingSample(
        kind="conversation",
        source="c1",
        system="你是林墨",
        messages=(
            ConversationTurn(role="user", content="在吗"),
            ConversationTurn(role="assistant", content="在"),
        ),
    )
    conversations = render_sample(sample, "sharegpt")["conversations"]
    assert [item["from"] for item in conversations] == ["system", "human", "gpt"]
    assert conversations[2]["value"] == "在"


def test_render_alpaca_flattens_multi_turn() -> None:
    """``alpaca`` 只能表达一问一答，多轮被摊平——最后一句助手话当 output。"""
    sample = TrainingSample(
        kind="conversation",
        source="c1",
        messages=(
            ConversationTurn(role="user", content="第一问"),
            ConversationTurn(role="assistant", content="第一答"),
            ConversationTurn(role="user", content="第二问"),
            ConversationTurn(role="assistant", content="第二答"),
        ),
    )
    record = render_sample(sample, "alpaca")
    assert record["instruction"] == "第二问"
    assert "第一答" in record["input"]
    assert record["output"] == "第二答"


def test_render_alpaca_uses_system_as_instruction_when_present() -> None:
    """有系统提示时，``alpaca`` 的 instruction 用系统提示，正文进 input。"""
    sample = TrainingSample(
        kind="reasoning",
        source="t1",
        system="你是林墨",
        instruction="看到下雨",
        output="记得带伞",
    )
    record = render_sample(sample, "alpaca")
    assert record["instruction"] == "你是林墨"
    assert record["input"] == "看到下雨"


def test_render_alpaca_never_drops_the_question_when_system_is_present() -> None:
    """系统提示占位之后，真正的题目不能被丢掉。

    丢掉的失败最隐蔽：文件照样能解析、条数照样对，只是每条样本都没了题目。
    """
    sample = TrainingSample(
        kind="conversation",
        source="c1",
        system="你是林墨",
        messages=(
            ConversationTurn(role="user", content="第一问"),
            ConversationTurn(role="assistant", content="第一答"),
            ConversationTurn(role="user", content="第二问"),
            ConversationTurn(role="assistant", content="第二答"),
        ),
    )
    record = render_sample(sample, "alpaca")
    assert record["instruction"] == "你是林墨"
    assert "第二问" in record["input"]
    assert "第一答" in record["input"]
    assert record["output"] == "第二答"


def test_render_rejects_unknown_format() -> None:
    """认不出的形状要报错并列出可选项，不要静默产出空文件。"""
    sample = TrainingSample(kind="reasoning", source="t1", instruction="a", output="b")
    with pytest.raises(ValueError, match="chat"):
        render_sample(sample, "yaml")  # type: ignore[arg-type]


@pytest.mark.parametrize("fmt", FORMATS)
def test_every_format_is_valid_jsonl(fmt: str) -> None:
    """三种形状都必须逐行可解析——这是和训练框架之间唯一的硬契约。"""
    samples = [
        TrainingSample(
            kind="conversation",
            source="c1",
            system="你是林墨",
            messages=(ConversationTurn(role="user", content="在吗"),),
        ),
        TrainingSample(
            kind="reasoning",
            source="t1",
            instruction="看到下雨",
            reasoning="要带伞吗",
            output="记得带伞",
        ),
    ]
    text = to_jsonl(samples, fmt)  # type: ignore[arg-type]
    assert text.endswith("\n")
    lines = text.splitlines()
    assert len(lines) == 2
    assert all(isinstance(json.loads(line), dict) for line in lines)


def test_to_jsonl_keeps_chinese_readable() -> None:
    """中文原样写出：转义成 ``\\u4f60`` 会让人没法一眼看出脱敏干不干净。"""
    sample = TrainingSample(
        kind="reasoning", source="t1", instruction="看到下雨", output="记得带伞"
    )
    assert "看到下雨" in to_jsonl([sample], "chat")
    assert "\\u" not in to_jsonl([sample], "chat")


def test_to_jsonl_on_empty_input_is_empty() -> None:
    """没有样本就产出空串，不是只有一个换行的空文件。"""
    assert to_jsonl([], "chat") == ""


# ────────────────────────────────────────────────────────────
# README / manifest —— 「数据页面」就是这两页
# ────────────────────────────────────────────────────────────
_EXAMPLE = re.compile(r"```json\n(?P<body>.+?)\n```", re.DOTALL)


def _sample(kind: str) -> TrainingSample:
    """一条随便什么样本，只为比出「示例 JSON 用了哪些键」。"""
    return TrainingSample(
        kind=kind,  # type: ignore[arg-type]
        source="c1",
        messages=(ConversationTurn(role="user", content="在吗"),),
    )


def _readme(fmts: tuple[str, ...] = ("chat",), *, skipped=()) -> str:
    """造一份有两个文件的说明页，省得每个用例抄一遍。"""
    return render_readme(
        persona_name="林晚",
        generated_at="2026-09-16T08:00:00+08:00",
        formats=fmts,  # type: ignore[arg-type]
        redact_digest="abc123def456",
        redact_terms=(),
        files=(
            ExportedFile(
                kind="conversation",
                filename=f"conversation.{fmts[0]}.jsonl",
                samples=2,
                byte_count=356,
                sha256="0" * 64,
            ),
        ),
        redactions=(("phone_cn", 1), ("win_path", 1)),
        skipped=skipped,
    )


def test_readme_example_has_the_shape_of_the_format_it_claims() -> None:
    """说明页里那段示例 JSON，包裹键必须**真的**是这个形状用的那个。

    这是最会骗人的一种文档错误：示例照样是合法 JSON，看着也对，
    但用户按它写解析器，跑真数据时才炸——而且那时他只会怀疑自己的代码。
    """
    chat = json.loads(_EXAMPLE.search(_readme(("chat",))).group("body"))
    assert set(chat) == set(render_sample(_sample("conversation"), "chat"))

    sharegpt = json.loads(_EXAMPLE.search(_readme(("sharegpt",))).group("body"))
    assert set(sharegpt) == set(render_sample(_sample("conversation"), "sharegpt"))

    alpaca = json.loads(_EXAMPLE.search(_readme(("alpaca",))).group("body"))
    assert set(alpaca) == set(render_sample(_sample("conversation"), "alpaca"))


def test_readme_example_never_uses_a_role_name_from_another_format() -> None:
    """``sharegpt`` 的示例里不该出现 ``role``/``content``，反过来也一样。"""
    sharegpt = _readme(("sharegpt",))
    assert '"from"' in sharegpt
    assert '"role"' not in sharegpt

    chat = _readme(("chat",))
    assert '"role"' in chat
    assert '"from"' not in chat


def test_readme_marks_reasoning_with_a_balanced_tag() -> None:
    """开标记与闭标记必须是同一个词。

    标记不成对，微调出来的模型会把内心独白当正文一起说出来——
    而且它认的是文档里那个形状，所以整份说明页都在教错的东西。
    """
    text = _readme()
    assert f"<{REASONING_OPEN}>" in text
    assert f"</{REASONING_OPEN}>" in text
    assert "</response>" not in text


def test_readme_tells_you_not_to_read_every_jsonl() -> None:
    """必须写明「按形状挑文件」，不能让人一股脑读目录。"""
    assert "*.jsonl" in _readme()


def test_readme_lists_every_file_with_its_size() -> None:
    text = _readme()
    assert "`conversation.chat.jsonl`" in text
    assert "356 B" in text
    assert "| 2 |" in text


def test_readme_reports_empty_datasets_instead_of_hiding_them() -> None:
    """空的那一类必须留下一行说明，不能从表里消失。"""
    text = _readme(skipped=(("reasoning", "库里这个时间段还没有对应的记录"),))
    assert "思考推理训练集这一批是空的" in text
    assert "库里这个时间段还没有对应的记录" in text


def test_readme_says_this_project_does_not_train() -> None:
    """说清边界：这里只产数据。不说清就会有人以为跑一次就得到模型了。"""
    assert "不做训练" in _readme()
    assert "0011-training-datasets-are-derived-and-redacted" in _readme()


def test_readme_says_the_directory_is_derived() -> None:
    """手改会没——这句话必须在最显眼的地方，不然改了再重跑会以为丢数据了。"""
    assert "派生产物" in _readme()
    assert "redact_terms" in _readme()


def test_manifest_records_every_format_not_just_the_last_one() -> None:
    """一次 build 可以同时产出两种形状，记录只留最后一个会让另一份凭空消失。"""
    manifest = render_manifest(
        persona_name="林晚",
        generated_at="2026-09-16T08:00:00+08:00",
        formats=("chat", "sharegpt"),
        redact_digest="abc123def456",
        redact_terms=("张伟",),
        files=(),
        redactions=(),
    )
    assert manifest["formats"] == ["chat", "sharegpt"]
    assert manifest["redact_terms"] == ["张伟"]


def test_manifest_skipped_entries_round_trip_through_the_reader() -> None:
    """写进去的 ``skipped`` 要能被 ``scan`` 读回来，否则空集原因就丢了。"""
    manifest = render_manifest(
        persona_name="林晚",
        generated_at="2026-09-16T08:00:00+08:00",
        formats=("chat",),
        redact_digest="abc123def456",
        redact_terms=(),
        files=(),
        redactions=(),
        skipped=(("reasoning", "上游还没落地"),),
    )
    assert manifest["skipped"] == [{"dataset": "reasoning", "reason": "上游还没落地"}]


def test_manifest_totals_add_up_across_files() -> None:
    """合计是算出来的，不是抄第一个文件的。"""
    manifest = render_manifest(
        persona_name="林晚",
        generated_at="2026-09-16T08:00:00+08:00",
        formats=("chat",),
        redact_digest="abc123def456",
        redact_terms=(),
        files=(
            ExportedFile(kind="conversation", filename="a", samples=2, byte_count=100, sha256="0"),
            ExportedFile(kind="tooluse", filename="b", samples=3, byte_count=50, sha256="1"),
        ),
        redactions=(("phone_cn", 2), ("email", 1)),
    )
    assert manifest["totals"] == {"samples": 5, "bytes": 150, "redactions": 3}


def test_manifest_file_rows_name_the_dataset_by_its_chinese_label() -> None:
    """机器读 ``dataset``，人读 ``label``——两样都要有。"""
    manifest = render_manifest(
        persona_name="林晚",
        generated_at="2026-09-16T08:00:00+08:00",
        formats=("chat",),
        redact_digest="abc123def456",
        redact_terms=(),
        files=(
            ExportedFile(
                kind="tooluse",
                filename="tooluse.chat.jsonl",
                samples=1,
                byte_count=10,
                sha256="0",
            ),
        ),
        redactions=(),
    )
    row = manifest["files"][0]
    assert row["dataset"] == "tooluse"
    assert row["label"] == spec_for("tooluse").label
    assert row["source"] == spec_for("tooluse").source


def test_tooluse_does_not_double_the_category_prefix() -> None:
    """``intent`` 自己已经带了形态前缀时不再叠一层。

    ``social/reply`` 配上 ``category='social'`` 会拼出 ``social/social/reply``，
    而工具名是模型调用时唯一能写的东西——写错就是一次都调不动。
    """
    sample = build_tooluse_samples([_activity("a1", intent="social/reply")])[0]
    assert sample.tools == ("social/reply",)


def test_tooluse_still_prefixes_a_bare_intent() -> None:
    """没带前缀的 ``intent`` 照旧补上形态。"""
    sample = build_tooluse_samples([_activity("a1", intent="reply", category="social")])[0]
    assert sample.tools == ("social/reply",)


def test_tooluse_without_category_uses_the_bare_intent() -> None:
    """没有形态就不补，不要拼出一个开头是斜杠的名字。"""
    sample = build_tooluse_samples([_activity("a1", intent="reply", category="")])[0]
    assert sample.tools == ("reply",)

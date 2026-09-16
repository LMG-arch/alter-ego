"""训练数据集：从库里**已经发生的事实**归纳出 (输入, 思考, 输出) 三元组。

## 数据集是派生产物

真源永远是库里的表。本模块只做一件事：把表里的行读成样本。
没有回写、没有水位、没有去重——``build`` 全量重跑是幂等的。

这不是洁癖。假设三个月后有人发现「手机号没脱干净」：

- 实时追加写的数据集 → 旧泄漏永远留在旧文件里，只能写迁移脚本重扫
- 派生的数据集 → 改一条正则，重跑一次，**全部历史重新脱一遍**

见 ``docs/adr/0011-training-datasets-are-derived-and-redacted.md``。

## 三类数据集，三个源

=================  =========================  ==========================================
数据集              源                          它教模型什么
=================  =========================  ==========================================
``conversation``   ``message``                怎么和你说话
``reasoning``      ``tick_log``               怎么想事情（候选、被压制、调用过的记忆）
``tooluse``        ``activity_log``           想干什么 → 调用了什么 → 得到什么
=================  =========================  ==========================================

⚠️ 后两个源的**写入者还没实现**（``sim/`` 主体与 tick 引擎尚未落地），
所以今天它们必然是 0 条。这是真实状态，不生成假数据来填。

## 纯函数

本模块只依赖 ``json`` / ``re`` / ``dataclasses``。没有 IO、没有时间、
没有随机数、没有配置读取——``domain/`` 的红线本来就禁止这些（P6）。
排序一律显式给出（``created_at`` 后跟 ``id``），不依赖字典序的偶然稳定。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from alterego.domain.redact import REDACTION_RULES, RedactionRule, apply_rules


__all__ = [
    "DATASET_SPECS",
    "FORMATS",
    "MESSAGE_PLACEHOLDER",
    "REASONING_OPEN",
    "ActivityRow",
    "ConversationTurn",
    "DatasetName",
    "DatasetSpec",
    "ExportedFile",
    "FormatName",
    "MessageRow",
    "RedactionSummary",
    "TickRow",
    "TrainingSample",
    "build_conversation_samples",
    "build_reasoning_samples",
    "build_tooluse_samples",
    "merge_adjacent",
    "reasoning_block",
    "redact_sample",
    "redact_samples",
    "render_manifest",
    "render_readme",
    "render_sample",
    "spec_for",
    "to_jsonl",
]


DatasetName = Literal["conversation", "reasoning", "tooluse"]
FormatName = Literal["chat", "sharegpt", "alpaca"]


#: 思考/输出在样本正文里的分界标记。
#:
#: 这两行不是装饰。训练集里如果不标出「哪一段是它想的过程」，
#: 微调出来的模型会把推理过程当成正文一起吐给你。
#: 这个形状与主流推理模型的约定一致，训练框架能直接识别。
REASONING_OPEN: Final[str] = "thinking"

#: 各格式「一行样本长什么样」的示例，只用于生成 README。
#:
#: 写成现成字符串而不是现场 ``json.dumps``：形状与包裹键都不同，
#: 用字典拼就分不清哪些花括号是格式、哪些是数据。
_EXAMPLES: Final[dict[str, str]] = {
    "chat": '{"messages": [{"role": "user", "content": "……"}, '
    '{"role": "assistant", "content": "……"}]}',
    "sharegpt": '{"conversations": [{"from": "human", "value": "……"}, '
    '{"from": "gpt", "value": "……"}]}',
    "alpaca": '{"instruction": "……", "input": "……", "output": "……"}',
}

#: 非文本消息（图片）在样本里的占位。
#:
#: 不能直接跳过：跳过会让对话看起来「它凭空没接上一轮」，
#: 训出来的模型会忽略用户发图这个动作。留占位它至少知道这里有事发生。
MESSAGE_PLACEHOLDER: Final[str] = "[图片]"

#: 角色名 → 各格式里的名字。
_ROLE_BY_FORMAT: Final[dict[str, dict[str, str]]] = {
    "sharegpt": {"system": "system", "user": "human", "assistant": "gpt"},
}

#: 系统提示里那段「你怎么回应」的固定说明。三类数据集共用。
_UNSET: Final[str] = ""


# ────────────────────────────────────────────────────────────
# 数据集的说明（CLI 与导出的 README 都读它，单一真源）
# ────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """一类数据集的元信息。

    Attributes:
        name: 标识，也是文件名前缀。
        label: 中文名，出现在 CLI 输出里。
        source: 源表，出现在 README 与 ``dataset list`` 里。
        teaches: 一句话说清它教模型什么。
        upstream_ready: 源的写入者是否已经实现。
            ``False`` 时 ``build`` 会如实报 0 条并解释原因，**不生成假数据**。
    """

    name: DatasetName
    label: str
    source: str
    teaches: str
    upstream_ready: bool


DATASET_SPECS: Final[tuple[DatasetSpec, ...]] = (
    DatasetSpec(
        name="conversation",
        label="对话训练集",
        source="message + conversation",
        teaches="怎么和你说话：什么时候短、什么时候长、什么时候主动开口",
        upstream_ready=True,
    ),
    DatasetSpec(
        name="reasoning",
        label="思考推理训练集",
        source="tick_log",
        teaches="怎么想事情：考虑过哪些选项、压下了什么、用上了哪段记忆",
        upstream_ready=False,
    ),
    DatasetSpec(
        name="tooluse",
        label="工具调用训练集",
        source="activity_log + tick_log",
        teaches="想干什么 → 调用了什么 → 得到什么",
        upstream_ready=False,
    ),
)

#: 支持的输出形状。取值是**形状名**不是厂商名——``{"messages": [...]}``
#: 早就是跨厂商的通用形状（ChatML 家族），拿厂商名命名会把用户误导成私有格式。
#: 另外 ``kernel/`` 的红线也不许出现厂商名，而这个常量在配置里有一份镜像。
FORMATS: Final[tuple[FormatName, ...]] = ("chat", "sharegpt", "alpaca")


# ────────────────────────────────────────────────────────────
# 输入行（与库里的列一一对应）
# ────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class MessageRow:
    """``message`` 表的一行。字段名与列名一致，方便逐字段搬。"""

    id: str
    conversation_id: str
    direction: str
    content: str
    content_type: str = "text"
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class TickRow:
    """``tick_log`` 表的一行。

    ``*_json`` 字段保持**原样字符串**，不在这一层解析——
    库里存的就是 JSON 文本，解析失败时本模块要能原样降级而不是崩掉。
    """

    id: str
    virtual_time: str
    status: str
    state_snapshot_json: str = "{}"
    percepts_json: str = "{}"
    candidates_json: str = "[]"
    chosen_intent: str = _UNSET
    motivation: str = _UNSET
    trigger_note: str = _UNSET
    suppressed_json: str = "[]"
    memories_json: str = "[]"
    notes_json: str = "[]"


@dataclass(frozen=True, slots=True)
class ActivityRow:
    """``activity_log`` 表的一行。"""

    id: str
    intent: str
    category: str
    description: str
    detail_json: str = "{}"
    location: str = _UNSET
    inner_voice: str = _UNSET
    duration_minutes: int = 0
    tick_id: str = _UNSET
    started_at: str = _UNSET
    suppressed_intent: str = _UNSET
    suppress_reason: str = _UNSET


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """样本里的一轮。``role`` 只有两个取值——这是训练格式要求的，不是简化。"""

    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class TrainingSample:
    """一条样本。三类数据集共用一个形状，多余字段留空。

    Attributes:
        kind: 属于哪一类数据集。
        source: 溯源标识（``conversation_id`` / ``tick_id`` / ``activity_id``）。
            **这个字段不进 JSONL**，只在 ``dataset show`` 里显示，
            方便从一条样本倒查到库里那一行。
        created_at: 源记录的创建时刻，用于样本排序（P6：显式排序）。
        system: 系统提示，会作为第一条 ``system`` 消息写进样本。
        messages: 对话类样本的多轮内容。
        instruction: 非对话类样本的输入（感知 / 意图）。
        reasoning: 思考过程。
        output: 最终输出的正文。
        tools: 这条样本涉及的工具名。今天只可能是空的，见 :data:`DATASET_SPECS`。
    """

    kind: DatasetName
    source: str
    created_at: str = _UNSET
    system: str = _UNSET
    messages: tuple[ConversationTurn, ...] = ()
    instruction: str = _UNSET
    reasoning: str = _UNSET
    output: str = _UNSET
    tools: tuple[str, ...] = field(default_factory=tuple)


# ────────────────────────────────────────────────────────────
# 对话训练集
# ────────────────────────────────────────────────────────────
def merge_adjacent(rows: Sequence[MessageRow]) -> tuple[ConversationTurn, ...]:
    """把连续同向的消息合并成一轮。

    真人会连发三条。拆成三个 ``user`` 轮训出来的模型会以为
    「一次只说一句话」——它会把「在吗」「在的」「那个事」当成三次独立对话，
    于是回复也变得吞吞吐吐。
    """
    turns: list[ConversationTurn] = []
    for row in rows:
        role: Literal["user", "assistant"] = "assistant" if row.direction == "outbound" else "user"
        text = row.content if row.content_type == "text" else MESSAGE_PLACEHOLDER
        if not text.strip():
            continue
        if turns and turns[-1].role == role:
            turns[-1] = ConversationTurn(role=role, content=f"{turns[-1].content}\n{text}")
        else:
            turns.append(ConversationTurn(role=role, content=text))
    return tuple(turns)


def _order_key(row: MessageRow) -> tuple[str, str]:
    """显式排序键。

    只按 ``created_at`` 排会在同一秒内退化成插入顺序，
    而插入顺序不是承诺——加上 ``id`` 才真正确定（P6）。
    """
    return (row.created_at, row.id)


def build_conversation_samples(
    rows: Sequence[MessageRow],
    *,
    system: str = _UNSET,
) -> list[TrainingSample]:
    """把 ``message`` 的行按会话归成对话样本。

    两条准入条件，缺一不可：

    1. **至少一轮用户 + 至少一轮助手**。只发了还没回的会话导不出东西，
       硬导出来是一条只有 ``user`` 的样本，训练框架会直接报错。
    2. 合并之后非空。全是图片占位以外的空串时丢掉。

    一个样本**允许以助手开头**：它会主动找你。那正是这类数据里最有价值的一批，
    丢掉等于把「主动性」从训练集里删掉。
    """
    grouped: dict[str, list[MessageRow]] = {}
    for row in rows:
        grouped.setdefault(row.conversation_id, []).append(row)

    samples: list[TrainingSample] = []
    for conversation_id in sorted(grouped):
        ordered = sorted(grouped[conversation_id], key=_order_key)
        turns = merge_adjacent(ordered)
        if not turns:
            continue
        roles = {turn.role for turn in turns}
        if roles != {"user", "assistant"}:
            continue
        samples.append(
            TrainingSample(
                kind="conversation",
                source=conversation_id,
                created_at=ordered[-1].created_at,
                system=system,
                messages=turns,
            )
        )
    return samples


# ────────────────────────────────────────────────────────────
# 思考推理训练集
# ────────────────────────────────────────────────────────────
def _load_json(raw: str) -> Any:
    """宽松解析库里的 JSON 文本。

    解析不了就返回原字符串。**不抛异常**是对的：一条脏数据不该让
    一整次导出失败，而降级成原文至少还看得见它长什么样。
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _describe(value: Any, *, indent: str = "  ") -> str:
    """把任意 JSON 值说成人话，按行铺开。

    刻意不用 ``json.dumps(ensure_ascii=False)`` 直接塞进样本：
    训练数据里出现 ``{"a": 1, "b": 2}`` 会让模型学会输出 JSON，
    而我们要的是它学会**描述**自己看到了什么。
    """
    if value is None or value == [] or value == {}:
        return ""
    if isinstance(value, Mapping):
        lines = [
            f"{indent}- {key}：{_flatten(item)}"
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if item not in (None, "", [], {})
        ]
        return "\n".join(lines)
    if isinstance(value, list):
        return "\n".join(f"{indent}- {_flatten(item)}" for item in value)
    return f"{indent}- {_flatten(value)}"


def _flatten(value: Any) -> str:
    """把一个叶子值压成一行。嵌套结构退化成 JSON，反正只在少见情况下发生。"""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, bool | int | float) or value is None:
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _join_blocks(blocks: Iterable[tuple[str, str]]) -> str:
    """把 (小标题, 正文) 拼成一段，空块自动省略。"""
    parts = [f"【{title}】\n{body}" for title, body in blocks if body.strip()]
    return "\n\n".join(parts)


def build_reasoning_samples(
    rows: Sequence[TickRow],
    *,
    system: str = _UNSET,
) -> list[TrainingSample]:
    """把 ``tick_log`` 的行读成「它怎么想的」。

    **只取 ``status == 'ok'`` 的 tick。** ``partial`` / ``failed`` /
    ``interrupted`` 的轨迹不是「它怎么想的」，是「它怎么没想成的」——
    喂给模型会教它犯错，而且是最难排查的那种错（模型学会了失败的推理路径）。

    输出里带上 ``suppressed_json``：**被预算拦下的那些念头是金子**。
    它们记录着「它想说但没说」，这正是拟人感最难以从提示词里获得的部分。
    """
    samples: list[TrainingSample] = []
    for row in sorted(rows, key=lambda item: (item.virtual_time, item.id)):
        if row.status != "ok":
            continue
        instruction = _join_blocks(
            (
                ("感知", _describe(_load_json(row.percepts_json))),
                ("状态", _describe(_load_json(row.state_snapshot_json))),
            )
        )
        reasoning = _join_blocks(
            (
                ("考虑过的选项", _describe(_load_json(row.candidates_json))),
                ("压下的念头", _describe(_load_json(row.suppressed_json))),
                ("想起的事", _describe(_load_json(row.memories_json))),
                ("顺手记的", _describe(_load_json(row.notes_json))),
            )
        )
        output = _join_blocks(
            (
                ("决定", row.chosen_intent),
                ("为什么", row.motivation),
                ("起因", row.trigger_note),
            )
        )
        if not instruction or not output:
            continue
        samples.append(
            TrainingSample(
                kind="reasoning",
                source=row.id,
                created_at=row.virtual_time,
                system=system,
                instruction=instruction,
                reasoning=reasoning,
                output=output,
            )
        )
    return samples


# ────────────────────────────────────────────────────────────
# 工具调用训练集
# ────────────────────────────────────────────────────────────
def build_tooluse_samples(
    rows: Sequence[ActivityRow],
    *,
    system: str = _UNSET,
    intents_by_tick: Mapping[str, str] | None = None,
) -> list[TrainingSample]:
    """把 ``activity_log`` 的行读成「想干什么 → 调用了什么 → 得到什么」。

    Args:
        rows: 行为日志。
        system: 系统提示。
        intents_by_tick: ``tick_id → chosen_intent``。
            有它的时候，「请求」用 tick 里那个**它自己定的意图**，
            而不是行为日志里那个已经被规范化过的 ``intent``。
            两者的差别正是「它想做」与「它实际做的」。

    ⚠️ **今天这个函数必然返回空列表**：``activity_log`` 还没有写入者
    （``sim/`` 的 Act / Persist 阶段尚未实现）。定义先立在这里，
    等上游落地自然有数据。见 ``DATASET_SPECS`` 的 ``upstream_ready``。

    工具名取 ``{category}/{intent}``：项目的 ``Capability`` 契约里
    ``intent_types`` 就是一组 ``形态.动作`` 的标识，这里沿用同一个命名，
    而不是另造一套。``intent`` 自己已经带了形态前缀（``social/reply``
    这种）时不再叠一层——``social/social/reply`` 会让模型学到一个谁也不认的
    工具名，而工具名是它调用时唯一能写的东西。
    """
    lookup = intents_by_tick or {}
    samples: list[TrainingSample] = []
    for row in sorted(rows, key=lambda item: (item.started_at, item.id)):
        prefixed = bool(row.category) and not row.intent.startswith(f"{row.category}/")
        tool = f"{row.category}/{row.intent}" if prefixed else row.intent
        request = lookup.get(row.tick_id) or row.intent
        arguments = _describe(_load_json(row.detail_json))
        output = _join_blocks(
            (
                ("结果", row.description),
                ("内心", row.inner_voice),
                ("被拦下的", row.suppressed_intent),
                ("拦截原因", row.suppress_reason),
                ("地点", row.location),
            )
        )
        if not output:
            continue
        instruction = _join_blocks(
            (
                ("请求", request),
                ("参数", arguments),
                ("可用工具", tool),
            )
        )
        samples.append(
            TrainingSample(
                kind="tooluse",
                source=row.id,
                created_at=row.started_at,
                system=system,
                instruction=instruction,
                reasoning=arguments,
                output=output,
                tools=(tool,),
            )
        )
    return samples


# ────────────────────────────────────────────────────────────
# 渲染
# ────────────────────────────────────────────────────────────
def reasoning_block(reasoning: str, output: str) -> str:
    """把思考与正文拼成一段，带分界标记。没有思考时原样返回正文。"""
    body = output.strip()
    thought = reasoning.strip()
    if not thought:
        return body
    return f"<{REASONING_OPEN}>\n{thought}\n</{REASONING_OPEN}>\n\n{body}"


def _chat_messages(sample: TrainingSample) -> list[dict[str, str]]:
    """``chat`` 形状的消息列表。"""
    messages: list[dict[str, str]] = []
    if sample.system.strip():
        messages.append({"role": "system", "content": sample.system.strip()})
    if sample.messages:
        messages.extend({"role": turn.role, "content": turn.content} for turn in sample.messages)
    else:
        messages.append({"role": "user", "content": sample.instruction.strip()})
        messages.append(
            {"role": "assistant", "content": reasoning_block(sample.reasoning, sample.output)}
        )
    return messages


def _sharegpt_messages(sample: TrainingSample) -> list[dict[str, str]]:
    """``sharegpt`` 形状：``from`` / ``value``，角色名换成 human / gpt。"""
    alias = _ROLE_BY_FORMAT["sharegpt"]
    return [
        {"from": alias[item["role"]], "value": item["content"]} for item in _chat_messages(sample)
    ]


def _alpaca_record(sample: TrainingSample) -> dict[str, str]:
    """``alpaca`` 形状：单轮。多轮被摊平。

    ``alpaca`` 天生只能表达一问一答，所以多轮对话要摊：
    **最后一句用户话当 instruction，之前的全部并进 input，最后一句助手话当 output。**
    这不是理想形态，但它是这套格式的能力上限——写清楚，别假装无损。

    有系统提示时，系统提示占 ``instruction`` 位，**原本要写在那儿的输入会被
    并进 ``input`` 而不是丢掉**。丢掉是最容易犯又最难发现的错：
    文件照样能解析、条数照样对，只是每一条样本的题目都不见了。
    """
    if sample.messages:
        turns = list(sample.messages)
        last_assistant = max(
            (index for index, turn in enumerate(turns) if turn.role == "assistant"),
            default=len(turns) - 1,
        )
        output = turns[last_assistant].content
        head = turns[:last_assistant]
        last_user = max(
            (index for index, turn in enumerate(head) if turn.role == "user"),
            default=0,
        )
        instruction = head[last_user].content if head else ""
        context = "\n".join(
            f"{'我' if turn.role == 'user' else '它'}：{turn.content}"
            for index, turn in enumerate(head)
            if index != last_user
        )
    else:
        instruction = sample.instruction.strip()
        context = ""
        output = reasoning_block(sample.reasoning, sample.output)

    system = sample.system.strip()
    if system:
        return {
            "instruction": system,
            "input": "\n".join(part for part in (instruction, context) if part),
            "output": output,
        }
    return {"instruction": instruction, "input": context, "output": output}


def render_sample(sample: TrainingSample, fmt: FormatName) -> dict[str, Any]:
    """把一条样本渲染成指定形状的 JSON 对象。

    Raises:
        ValueError: ``fmt`` 不在 :data:`FORMATS` 里。
    """
    if fmt == "chat":
        return {"messages": _chat_messages(sample)}
    if fmt == "sharegpt":
        return {"conversations": _sharegpt_messages(sample)}
    if fmt == "alpaca":
        return _alpaca_record(sample)
    raise ValueError(f"不认识的输出形状：{fmt!r}（可选：{'、'.join(FORMATS)}）")


def to_jsonl(samples: Sequence[TrainingSample], fmt: FormatName) -> str:
    """渲染成 JSONL 文本，一行一条。

    ``ensure_ascii=False``：中文原样写出。训练框架读的是 UTF-8，
    转义成 ``\\u4f60`` 只会让文件大一圈、还让人没法用肉眼看有没有脱干净。

    末尾**带**换行。少了它，``cat a.jsonl b.jsonl`` 会把两条样本粘在一行上。
    """
    if not samples:
        return ""
    lines = [
        json.dumps(render_sample(sample, fmt), ensure_ascii=False, sort_keys=False)
        for sample in samples
    ]
    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────────
# 脱敏
# ────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RedactionSummary:
    """脱敏之后的全部样本，以及每类规则总共命中多少次。"""

    samples: tuple[TrainingSample, ...] = ()
    hits: tuple[tuple[str, int], ...] = ()

    @property
    def total(self) -> int:
        """总共替换了多少处。"""
        return sum(count for _, count in self.hits)


def redact_sample(
    sample: TrainingSample,
    *,
    rules: Sequence[RedactionRule] = REDACTION_RULES,
) -> tuple[TrainingSample, tuple[tuple[str, int], ...]]:
    """给一条样本的每一段正文过一遍脱敏。

    两个字段**刻意不脱**：

    - ``source``：它是 ``conversation_id`` / ``tick_id``，随机 id，
      不含身份信息；脱了它就再也倒查不回库里那一行。
    - ``tools``：那是代码里的能力标识（``social/post_moment``），不是人说的话。

    ``rules`` 做成参数而不是在这里现取 ``user_name``：一次导出要过几万个字段，
    规则表应该由调用方建一次再传进来。
    """
    counts: dict[str, int] = {}

    def _clean(text: str) -> str:
        report = apply_rules(text, rules)
        for name, count in report.hits:
            counts[name] = counts.get(name, 0) + count
        return report.text

    updated = TrainingSample(
        kind=sample.kind,
        source=sample.source,
        created_at=sample.created_at,
        system=_clean(sample.system),
        messages=tuple(
            ConversationTurn(role=turn.role, content=_clean(turn.content))
            for turn in sample.messages
        ),
        instruction=_clean(sample.instruction),
        reasoning=_clean(sample.reasoning),
        output=_clean(sample.output),
        tools=sample.tools,
    )
    return updated, tuple(counts.items())


def redact_samples(
    samples: Sequence[TrainingSample],
    *,
    rules: Sequence[RedactionRule] = REDACTION_RULES,
) -> RedactionSummary:
    """批量脱敏，命中次数按规则名汇总。

    ``hits`` 按**命中次数降序、规则名升序**排。降序是因为用户看一眼最想知道
    「哪条规则在疯狂命中」（通常是路径，因为样例里有绝对路径）；
    升序保证同一批输入得到同一个顺序，不依赖字典序的偶然稳定（P6）。
    """
    cleaned: list[TrainingSample] = []
    totals: dict[str, int] = {}
    for sample in samples:
        updated, hits = redact_sample(sample, rules=rules)
        cleaned.append(updated)
        for name, count in hits:
            totals[name] = totals.get(name, 0) + count
    ordered = tuple(sorted(totals.items(), key=lambda pair: (-pair[1], pair[0])))
    return RedactionSummary(samples=tuple(cleaned), hits=ordered)


# ────────────────────────────────────────────────────────────
# 给「数据页面」用的渲染
# ────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class ExportedFile:
    """一个已经落盘的 ``.jsonl`` 的统计。

    由 ``sim/`` 量出来（大小、摘要），本模块只负责把它摆进 manifest 与 README。
    """

    kind: DatasetName
    filename: str
    samples: int
    byte_count: int
    sha256: str


def spec_for(kind: DatasetName) -> DatasetSpec:
    """按名字取数据集说明。

    Raises:
        ValueError: 名字不在 :data:`DATASET_SPECS` 里。
    """
    for spec in DATASET_SPECS:
        if spec.name == kind:
            return spec
    raise ValueError(f"没有这个数据集：{kind!r}")


def _format_bytes(size: int) -> str:
    """字节数说成人话。README 里 ``1234567`` 不如 ``1.2 MB`` 好读。"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def render_manifest(
    *,
    persona_name: str,
    generated_at: str,
    formats: Sequence[FormatName],
    redact_digest: str,
    redact_terms: Sequence[str],
    files: Sequence[ExportedFile],
    redactions: Sequence[tuple[str, int]],
    skipped: Sequence[tuple[DatasetName, str]] = (),
) -> dict[str, Any]:
    """``manifest.json`` 的内容。

    这个文件唯一的机器可读用途是 ``redact_digest``：它记着**磁盘上的数据集
    是用哪一版脱敏规则脱的**。脱敏规则改了之后 ``dataset list`` 一比对就知道
    该重跑——这是「脱敏必须可回溯重做」那条决策的兑现机制（ADR-0011）。

    ``formats`` 是**这个目录里出现过的全部形状**，不是一个：一次 ``build``
    可以同时产出 ``chat`` 与 ``sharegpt``，而说明文件只有一份，
    只记最后一个形状会让另一份文件在文档里凭空消失。
    """
    return {
        "persona": persona_name,
        "generated_at": generated_at,
        "formats": list(formats),
        "redact_digest": redact_digest,
        "redact_terms": list(redact_terms),
        "totals": {
            "samples": sum(item.samples for item in files),
            "bytes": sum(item.byte_count for item in files),
            "redactions": sum(count for _, count in redactions),
        },
        "files": [
            {
                "dataset": item.kind,
                "label": spec_for(item.kind).label,
                "file": item.filename,
                "samples": item.samples,
                "bytes": item.byte_count,
                "sha256": item.sha256,
                "source": spec_for(item.kind).source,
            }
            for item in files
        ],
        "redactions": [{"rule": name, "count": count} for name, count in redactions],
        "skipped": [{"dataset": kind, "reason": reason} for kind, reason in skipped],
    }


def render_readme(
    *,
    persona_name: str,
    generated_at: str,
    formats: Sequence[FormatName],
    redact_digest: str,
    redact_terms: Sequence[str],
    files: Sequence[ExportedFile],
    redactions: Sequence[tuple[str, int]],
    skipped: Sequence[tuple[DatasetName, str]] = (),
) -> str:
    """``README.md`` 的内容——这就是「数据页面」当前的形态。

    用行列表拼而不是一个大 f-string：正文里有 JSON 示例，
    里面全是花括号，塞进 f-string 只会让人分不清哪些括号是格式、哪些是数据。
    """
    primary = formats[0] if formats else "chat"

    lines: list[str] = [
        f"# {persona_name} 的训练数据集",
        "",
        f"> 由 `alterego dataset build` 生成于 {generated_at}。",
        ">",
        "> **整个目录都是派生产物。** 手改的内容会在下次 `build` 时被覆盖——",
        "> 要改脱敏规则，改配置里的 `[dataset] redact_terms` 再重跑。",
        "",
        "## 怎么用",
        "",
        f"每个 `.jsonl` 一行一条样本，UTF-8，一行一个 JSON 对象。形状看文件名"
        f"（本次：{'、'.join(f'`{item}`' for item in formats)}）：",
        "",
        "```json",
        _EXAMPLES[primary],
        "```",
        "",
        f"上面是 `{primary}` 的形状。文件名里写着哪一种，就读哪一种——",
        "**不要按 `*.jsonl` 整体读**，配方换过之后目录里可能不止一种形状。",
        "",
        "直接交给任何支持这套形状的训练框架即可。**本项目只产出数据，不做训练**",
        "（见 `docs/adr/0011-training-datasets-are-derived-and-redacted.md`）。",
        "",
        "## 有什么",
        "",
        "| 文件 | 数据集 | 条数 | 大小 | 来自 |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    if files:
        lines.extend(
            f"| `{item.filename}` | {spec_for(item.kind).label} | {item.samples} | "
            f"{_format_bytes(item.byte_count)} | {spec_for(item.kind).source} |"
            for item in files
        )
    else:
        lines.append("| — | — | 0 | — | — |")

    for kind, reason in skipped:
        spec = spec_for(kind)
        lines.extend(["", f"> ⏸ **{spec.label}这一批是空的**：{reason}"])

    lines.extend(
        [
            "",
            "### 思考写在哪儿",
            "",
            f"推理与工具调用样本的助手正文里，思考被包在 `<{REASONING_OPEN}>` 与 "
            f"`</{REASONING_OPEN}>` 之间：",
            "",
            "```",
            f"<{REASONING_OPEN}>",
            "……它考虑过的选项、压下的念头、想起的事……",
            f"</{REASONING_OPEN}>",
            "",
            "……它最后说的话……",
            "```",
            "",
            "去掉这两个标记，微调出来的模型会把内心独白当成正文一起说出来。",
            "",
            "## 脱了什么",
            "",
            f"规则表指纹：`{redact_digest}`。条目与替换结果：",
            "",
            "| 规则 | 替换为 |",
            "| --- | --- |",
        ]
    )
    lines.extend(f"| `{rule.name}` | `[{rule.label}]` |" for rule in REDACTION_RULES)
    if redact_terms:
        joined = "、".join(f"`{term}`" for term in redact_terms)
        lines.append(f"| `extra`（来自 `[dataset] redact_terms`） | `[自定义]`（{joined}） |")
    lines.extend(
        [
            "",
            f"这一批总共替换了 **{sum(count for _, count in redactions)}** 处。",
            "",
            "**虚构角色的名字没有脱。** 这是有意的：训练集的目标是「训出一个会以",
            "这个名字自称、会称呼你的模型」，把所有人名统一换掉会让它失去称呼能力。",
            "脱敏摘的是**可识别的真实身份**，不是专有名词。",
            "",
            "## 已知局限",
            "",
            "- **正则挡不住自然语言里的身份信息。** 「我住在某某小区」这类句子，",
            "  规则表看不见。这一层不假装完备。",
            "- **脱敏规则改过之后要重跑 `build`。** `alterego dataset list` 会比对",
            "  指纹并提示，但它不会自动重跑——重跑要花时间，该由你决定什么时候。",
            "- **`tooluse` 与 `reasoning` 依赖上游。** 推演引擎还没落地之前，",
            "  这两类必然是 0 条。此时**不会写出空文件**（一个 0 字节的 `.jsonl`",
            "  看起来像坏了），上面那张表会说清是哪一种空。",
            "- **不带系统提示。** 样本里现在只有对话本身。正式的人设提示词还没接进来",
            "  （它要由 `persona_json` 渲染出来），先编一份塞进训练集，只会让模型",
            "  学会一套运行时根本不会发给它的前言——比不带更糟。位置已经留好了。",
        ]
    )
    return "\n".join(lines) + "\n"

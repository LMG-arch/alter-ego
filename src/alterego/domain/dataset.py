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

⚠️ 三个源的**写入者都已实现**（``sim/conversation.py`` 与 ``sim/engine.py`` 写
``message`` / ``conversation`` / ``tick_log`` / ``activity_log``），所以再报 0 条
只能是「时间范围里真没记录」或「取到行但拼不出样本」，**不是上游没落地**。
分辨这三种原因的逻辑在 ``sim/dataset.py`` 的 ``_skip_reason``——它们是用户
唯一能据此决定「接着等」还是「改配置」的东西。

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
            ``False`` 时 ``build`` 会如实报 0 条，并把原因说成「上游还没落地」
            而不是「你时间范围开小了」（见 ``sim/dataset.py`` 的 ``_skip_reason``），
            **也不生成假数据**。
            ⚠️ 它**不等于**「今天导得出样本」——后者是另外两种解释的事。
            2026-09-16 三类全为 ``True``：``message`` / ``conversation`` 的写入者是
            ``sim/conversation.py`` 与 ``sim/engine.py``，``tick_log`` 与
            ``activity_log`` 的是 ``sim/engine.py`` 的 Persist 一环。
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
        # ``tick_log`` 的写入者是 ``sim/engine.py`` 末尾那一句 ``tick_logs.append``。
        upstream_ready=True,
    ),
    DatasetSpec(
        name="tooluse",
        label="工具调用训练集",
        source="activity_log + tick_log",
        teaches="想干什么 → 调用了什么 → 得到什么",
        # ``activity_log`` 的写入者是 ``sim/engine.py`` 的 ``activities.append``。
        upstream_ready=True,
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

    ⚠️ **空列表不等于「上游还没落地」**：``activity_log`` 的写入者是
    ``sim/engine.py`` 的 Persist 一环，早就有了。空结果只会来自两种原因——
    「这个时间范围里没有行为」或「取到了行但拼不出样本」，两者都归
    ``sim/dataset.py`` 的 ``_skip_reason`` 区分并写给用户看。

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

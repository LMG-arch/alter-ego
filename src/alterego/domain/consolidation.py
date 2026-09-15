"""记忆梳理：把模型吐出来的东西变成可以落库的记忆。

四件事，全是纯函数：

1. :func:`extract_json_array` —— 从一段可能带围栏的回复里抠出 JSON 数组；
2. :func:`parse_memory_drafts` —— 逐条校验，转成 :class:`MemoryDraft`；
3. :func:`summarize` —— 从内容里取一句话，填 ``Memory.summary``；
4. :meth:`MemoryDraft.to_memory` —— 补上主键与时间，变成领域对象。

**整批丢弃，不是逐条跳过。** 一段语义完整的输出里夹着一条
``"importance": "很高"``，说明的不是「格式抖动」，而是模型没听懂任务。
留一半比全丢更危险：留下那半会出现在下一次的 ``{existing_memories}`` 里，
对着它说「你已经记住了」，而它记的是模型编的。

宽容只发生在**包装层**（``` 围栏、前后那句「好的，这是结果」、
``entities`` 写成单个字符串），一旦进到语义层就一点也不宽容。

依据: docs/design/03-data-model.md § 6.5、prompts/memory_consolidate.md
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, cast

from alterego.domain.memory import MEMORY_KINDS, Memory, MemoryKind


__all__ = [
    "MAX_ITEMS",
    "MIN_SOURCE_ITEMS",
    "SUMMARY_LIMIT",
    "WINDOW_HOURS",
    "MemoryDraft",
    "extract_json_array",
    "parse_memory_drafts",
    "summarize",
]


WINDOW_HOURS: Final[float] = 6.0
"""梳理时往回看多久（**虚拟**小时）。"""

MIN_SOURCE_ITEMS: Final[int] = 3
"""少于这么多条原始素材就不值得调用一次模型。

三条流水账归纳不出认知，只够把「上午开会」写成「上午开了个会」。
"""

MAX_ITEMS: Final[int] = 10
"""一次最多产出几条记忆。"""

SUMMARY_LIMIT: Final[int] = 60
"""摘要长度上限（字符）。"""

_DEFAULT_KIND: Final[str] = "episodic"
_DEFAULT_IMPORTANCE: Final[float] = 0.5
_DEFAULT_VALENCE: Final[float] = 0.0

_SENTENCE_ENDS: Final[frozenset[str]] = frozenset("。！？!?；;")


# ── 摘要 ────────────────────────────────────────────────────


def _first_sentence_end(text: str, limit: int) -> int | None:
    """第一个句末标点的**后一位**，只在 ``limit`` 以内找。"""
    for index, char in enumerate(text[:limit]):
        if char in _SENTENCE_ENDS:
            return index + 1
    return None


def summarize(content: str, *, limit: int = SUMMARY_LIMIT) -> str:
    """从一整段里取一句话当摘要。

    ``summary`` 是**唯一会被注入提示词的部分**（``Memory.__post_init__``
    为此专门校验它非空），所以它不能是简单的 ``content[:60]``——那会在
    句子中间断开，读起来像被截断的日志。而提示词里的每一行都在教模型
    怎么说话，喂进去半句话就是在教它说半句话。

    规则：取第一个句末标点之前的部分；没有标点、或第一句本身就超长，
    才按字数截断。汉字与标点都算一个字符。
    """
    text = " ".join(content.split())
    if not text:
        raise ValueError("记忆内容不能为空白")

    cut = _first_sentence_end(text, limit)
    if cut is not None:
        return text[:cut]
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


# ── 解析 ────────────────────────────────────────────────────


def _json_between_brackets(text: str) -> Any:
    """取第一个 ``[`` 与最后一个 ``]`` 之间的内容再解析。"""
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("回复里找不到 JSON 数组")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"回复里的 JSON 数组解析失败：{exc.msg}") from exc


def extract_json_array(text: str) -> list[Any]:
    """从模型回复里抠出 JSON 数组。

    宽容的只有外面的包装：``​```json`` 围栏、前后那句「好的，这是结果」。
    这不是偷懒——要求「只输出 JSON」的模型里总有一批会加上围栏，
    而为了三个反引号重试一次，成本是一次完整的调用。
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("模型回复是空的")

    try:
        loaded: Any = json.loads(stripped)
    except json.JSONDecodeError:
        loaded = _json_between_brackets(stripped)

    if not isinstance(loaded, list):
        raise ValueError(f"期望一个 JSON 数组，得到 {type(loaded).__name__}")
    return loaded


def _as_float(value: Any, *, default: float, where: str, field: str) -> float:
    """数字字段。

    ``bool`` 要先挡掉：在 Python 里 ``True`` 就是 ``1``，会安安静静地
    变成 ``importance = 1.0``——那是一条「这辈子最重要的事」。
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{where}的 {field} = {value!r} 不是数字")
    return float(value)


def _as_str_tuple(value: Any, *, where: str) -> tuple[str, ...]:
    """字符串数组字段。

    单个字符串是常见偏差（``"entities": "张三"``），语义没有歧义，
    所以接纳它——为了方括号重试一次不划算。数组里有非字符串项则不接纳：
    那说明模型在往里面塞结构，结构是什么它自己也没想好。
    """
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if not isinstance(value, list):
        raise ValueError(f"{where}的 entities = {value!r} 不是数组")

    items: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError(f"{where}的 entities 里有非字符串项：{entry!r}")
        text = entry.strip()
        if text:
            items.append(text)
    return tuple(items)


def _parse_one(item: Any, *, index: int) -> MemoryDraft:
    where = f"第 {index + 1} 条"
    if not isinstance(item, Mapping):
        raise ValueError(f"{where}不是对象，而是 {type(item).__name__}")

    content = item.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"{where}缺少 content（提示词要求第一人称的一句话）")

    kind: Any = item.get("kind", _DEFAULT_KIND)
    if not isinstance(kind, str) or kind not in MEMORY_KINDS:
        raise ValueError(
            f"{where}的 kind = {kind!r} 不是合法记忆类型，合法值：{', '.join(MEMORY_KINDS)}"
        )

    # 「带情绪的瞬间」在模板里由 emotional 这个布尔开关表达，而不是第四个
    # kind 取值——模型更容易答对一个是非题。这里把两种写法合流到
    # kind = "emotional"（半衰期 365 天），不额外加字段。
    if item.get("emotional") is True:
        kind = "emotional"

    return MemoryDraft(
        content=content.strip(),
        kind=cast("MemoryKind", kind),
        importance=_as_float(
            item.get("importance"),
            default=_DEFAULT_IMPORTANCE,
            where=where,
            field="importance",
        ),
        valence=_as_float(
            item.get("valence"),
            default=_DEFAULT_VALENCE,
            where=where,
            field="valence",
        ),
        entities=_as_str_tuple(item.get("entities"), where=where),
    )


def parse_memory_drafts(text: str, *, max_items: int = MAX_ITEMS) -> list[MemoryDraft]:
    """把模型回复解析成一组草稿。

    空数组是**合法答案**（「今天没什么值得记的」），返回空列表，不报错。
    条数超限则整批拒绝：提示词里写死了「最多 N 条」，超限说明它没按约束来，
    这一批的其余部分也不再可信。

    Raises:
        ValueError: 数组解析不出来、条数超限，或任何一条不合法。
    """
    raw_items = extract_json_array(text)
    if not raw_items:
        return []

    if len(raw_items) > max_items:
        raise ValueError(
            f"模型输出了 {len(raw_items)} 条，超过上限 {max_items} 条。"
            "提示词里写了「宁缺毋滥」，超限意味着它没有按约束来。"
        )

    drafts: list[MemoryDraft] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_items):
        draft = _parse_one(item, index=index)
        # 同一批里的重复：模型换个说法把同一件事又说了一遍。
        # 空格归一之后比较，免得只差一个换行就当成两条。
        key = " ".join(draft.content.split())
        if key in seen:
            continue
        seen.add(key)
        drafts.append(draft)
    return drafts


# ── 草稿 ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MemoryDraft:
    """一条还没落库的记忆。

    只装模型说了算的字段。``id`` / ``occurred_at`` / ``created_at`` 不在里面——
    那不是模型该编的东西，编出来的时间是幻觉的一部分，而且会污染时间轴。
    """

    content: str
    kind: MemoryKind = "episodic"
    importance: float = _DEFAULT_IMPORTANCE
    valence: float = _DEFAULT_VALENCE
    entities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("记忆内容不能为空白")
        if self.kind not in MEMORY_KINDS:
            raise ValueError(f"未知的记忆类型 {self.kind!r}，合法值：{', '.join(MEMORY_KINDS)}")
        if not 0.0 <= self.importance <= 1.0:
            raise ValueError(f"重要度必须在 0~1 之间，得到 {self.importance!r}")
        if not -1.0 <= self.valence <= 1.0:
            raise ValueError(f"情绪效价必须在 -1~1 之间，得到 {self.valence!r}")

    @property
    def summary(self) -> str:
        """一句话摘要，注入提示词用。"""
        return summarize(self.content)

    def to_memory(
        self,
        *,
        persona_id: str,
        memory_id: str,
        occurred_at: datetime,
        created_at: datetime,
        source_ref: str | None = None,
        tags: Sequence[str] = (),
    ) -> Memory:
        """补上主键与时间，变成可以落库的 :class:`Memory`。

        ``source`` 固定是 ``consolidation``：这是**归纳出来的**，不是亲历的。
        分开记才能回答「这条是他经历的，还是他总结的」——两者在检索与
        展示上该被区别对待。
        """
        return Memory(
            id=memory_id,
            persona_id=persona_id,
            kind=self.kind,
            content=self.content,
            summary=self.summary,
            importance=self.importance,
            valence=self.valence,
            entities=self.entities,
            tags=tuple(tags),
            source="consolidation",
            source_ref=source_ref,
            occurred_at=occurred_at,
            created_at=created_at,
        )

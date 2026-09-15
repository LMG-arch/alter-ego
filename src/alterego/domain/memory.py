"""领域层：记忆。

记忆不是「一堆文本按时间排」。它是三种**衰减速度完全不同**的东西：

| kind | 内容 | 半衰期 | 例子 |
| --- | --- | --- | --- |
| `episodic` | 具体事件 | 7 天 | "今天下午跟小王吃了个火锅" |
| `semantic` | 归纳知识 | 180 天 | "小王不吃辣" |
| `emotional` | 情绪印记 | 365 天 | "上次他说那句话让我特别难受" |

强度公式：

$$
\\text{strength}(t) = \\text{importance} \\times e^{-\\lambda_{\\text{kind}} \\cdot \\Delta t_{\\text{days}}}
\\times (1 + 0.35 \\cdot \\text{recall\\_count})
$$

**$\\Delta t$ 的基准是「上次回忆时间」而不是创建时间**（`last_recalled_at` 优先于
`occurred_at`）。这模拟「每次想起就重新记牢一点」——复习效应。少了这一条，
记忆强度只跟年龄有关，检索到它也不会让它更牢，那就不像记忆了。

**遗忘不是删除。** `forgotten = 1` 的记忆仍在库里，仍可被强关联「突然想起来」，
只是不参与常规检索。删除是不可逆的，而「想不起来」是暂时的。

依据: docs/design/04-simulation-loop.md § 7、docs/design/03-data-model.md § 6
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Final, Literal, get_args


__all__ = [
    "CONSOLIDATION_IMPORTANCE_FACTOR",
    "DEFAULT_TOP_K",
    "FADE_THRESHOLD",
    "HALF_LIFE_DAYS",
    "MEMORY_KINDS",
    "MEMORY_SOURCES",
    "RECALL_BOOST_PER_RECALL",
    "RECENCY_LAMBDA",
    "RESURRECTION_MIN_IMPORTANCE",
    "RESURRECTION_STRENGTH_RATIO",
    "Memory",
    "MemoryCandidate",
    "MemoryKind",
    "MemorySearchHit",
    "MemorySource",
    "MemoryStats",
    "RetrievalWeights",
    "apply_consolidation",
    "decay_rate",
    "is_faded",
    "mood_alignment",
    "preprocess_for_fts",
    "rank_memories",
    "recency_at",
    "resurrect",
    "should_resurrect",
    "strength_at",
]

MemoryKind = Literal["episodic", "semantic", "emotional"]
"""记忆类型。决定衰减速度。"""

MemorySource = Literal["tick", "conversation", "consolidation", "manual", "npc"]
"""这条记忆是怎么来的。`consolidation` 表示它是归纳出来的，不是亲历的。"""

MEMORY_KINDS: Final[tuple[str, ...]] = get_args(MemoryKind)
MEMORY_SOURCES: Final[tuple[str, ...]] = get_args(MemorySource)

HALF_LIFE_DAYS: Final[Mapping[str, float]] = {
    "episodic": 7.0,
    "semantic": 180.0,
    "emotional": 365.0,
}
"""三种记忆的半衰期（天）。"""

RECALL_BOOST_PER_RECALL: Final[float] = 0.35
"""每被回忆一次，强度乘数增加 0.35。"""

FADE_THRESHOLD: Final[float] = 0.05
"""强度低于此值即视为遗忘。episodic 记忆 28 天后约 0.04，正好落在线下。"""

RECENCY_LAMBDA: Final[float] = 0.03
"""重排公式中新近度项的衰减率（$\\lambda_n$）。"""

RECENCY_SECONDS_PER_DAY: Final[float] = 86400.0

RESURRECTION_MIN_IMPORTANCE: Final[float] = 0.5
"""重要度低于此值的遗忘记忆不会被强关联激活——不重要的事想不起来很正常。"""

RESURRECTION_STRENGTH_RATIO: Final[float] = 0.4
"""想起来之后的强度 = `importance × 0.4`：恢复了，但不如原来清晰。"""

CONSOLIDATION_IMPORTANCE_FACTOR: Final[float] = 0.6
"""巩固后原始 episodic 记忆的重要度乘数——信息已上提，原事件不再那么重要。"""

DEFAULT_TOP_K: Final[int] = 8
"""默认注入提示词的记忆条数。"""


def decay_rate(kind: str) -> float:
    """某种记忆的衰减率 $\\lambda = \\ln 2 / \\text{half\\_life}$。"""
    try:
        half_life = HALF_LIFE_DAYS[kind]
    except KeyError:
        raise ValueError(f"未知的记忆类型 {kind!r}，合法值：{', '.join(MEMORY_KINDS)}") from None
    return math.log(2) / half_life


@dataclass(frozen=True, slots=True)
class Memory:
    """一条记忆。

    字段与 `memory` 表一一对应（表 7）。

    `strength` 是**上次持久化的强度快照**，只用于「不带上 `now` 也能看的场合」
    （记忆页的强度分布、`MemoryStats.avg_strength`）。任何需要当下判断的地方
    都应当调用 `strength_at(memory, now)` 现算——快照会过期，
    而一个过期的强度值会让「这条还记不记得」得到错误答案。

    Attributes:
        persona_id: 归属人格。
        kind: 记忆类型，决定衰减速度。
        content: 完整内容。
        summary: 一句话摘要，注入提示词用。
        occurred_at: 事情发生（或记忆诞生）的时间。
        id: 主键。留空表示「尚未入库」，由 Repository 在 `save` 时分配。
        importance: 固有重要度，0 ~ 1，写入时评定。
        strength: 上次持久化的强度快照。
        valence: 情绪色彩，-1 ~ 1。
        entities: 涉及的人物/地点，用于关系触发。
        tags: 标签。
        source: 来源，见 `MemorySource`。
        source_ref: 来源 id（`tick_id` / `message_id` / 被归纳的 `memory_id` 列表）。
        last_recalled_at: 上次被回忆的时间，衰减的计时起点。
        recall_count: 被回忆次数。
        forgotten: 是否已遗忘。遗忘不等于删除。
        created_at: 入库时间。留空表示尚未入库。
    """

    persona_id: str
    kind: MemoryKind
    content: str
    summary: str
    occurred_at: datetime
    id: str = ""
    importance: float = 0.5
    strength: float = 1.0
    valence: float = 0.0
    entities: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    source: MemorySource = "tick"
    source_ref: str | None = None
    last_recalled_at: datetime | None = None
    recall_count: int = 0
    forgotten: bool = False
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind not in MEMORY_KINDS:
            raise ValueError(f"未知的记忆类型 {self.kind!r}，合法值：{', '.join(MEMORY_KINDS)}")
        if self.source not in MEMORY_SOURCES:
            raise ValueError(f"未知的记忆来源 {self.source!r}，合法值：{', '.join(MEMORY_SOURCES)}")
        if not self.content.strip():
            raise ValueError("记忆的 content 不能为空")
        if not self.summary.strip():
            raise ValueError(
                "记忆的 summary 不能为空——它是唯一会被注入提示词的部分，没有摘要的记忆等于记不下来"
            )
        if not 0.0 <= self.importance <= 1.0:
            raise ValueError(f"importance 必须在 0 ~ 1，得到 {self.importance!r}")
        if self.strength < 0.0:
            raise ValueError(f"strength 不能为负，得到 {self.strength!r}")
        if not -1.0 <= self.valence <= 1.0:
            raise ValueError(f"valence 必须在 -1 ~ 1，得到 {self.valence!r}")
        if self.recall_count < 0:
            raise ValueError(f"recall_count 不能为负，得到 {self.recall_count!r}")

    @property
    def reference_time(self) -> datetime:
        """衰减的计时起点：上次回忆时间优先，其次发生时间。"""
        return self.last_recalled_at or self.occurred_at


def strength_at(memory: Memory, now: datetime) -> float:
    """`now` 时刻这条记忆有多牢。

    `days` 取 `max(0, ...)`：虚拟时间回拨（重放历史）或时钟抖动都可能让
    `now` 早于 `reference_time`，而负的 `days` 会让指数项大于 1——
    记忆越放越牢，这显然不对。
    """
    days = max(0.0, (now - memory.reference_time).total_seconds() / RECENCY_SECONDS_PER_DAY)
    decay = math.exp(-decay_rate(memory.kind) * days)
    boost = 1.0 + RECALL_BOOST_PER_RECALL * memory.recall_count
    return memory.importance * decay * boost


def is_faded(memory: Memory, now: datetime, *, threshold: float = FADE_THRESHOLD) -> bool:
    """按当下的强度判断是否已该遗忘。"""
    return strength_at(memory, now) < threshold


def recency_at(memory: Memory, now: datetime) -> float:
    """新近度 $N = e^{-\\lambda_n \\cdot \\text{days}}$。

    计时起点用的是 `occurred_at` 而不是 `last_recalled_at`：
    「上次回忆」已经通过 `strength_at` 进了重要度项，这里再用一次
    等于把复习效应算两遍。
    """
    days = max(0.0, (now - memory.occurred_at).total_seconds() / RECENCY_SECONDS_PER_DAY)
    return math.exp(-RECENCY_LAMBDA * days)


def mood_alignment(memory: Memory, valence: float) -> float:
    """情绪一致度 $E = 1 - |{\\text{memory.valence} - \\text{valence}}| / 2$。

    让心情好时更容易想起开心的事——心理学上的「心境一致性记忆」。
    它让检索带情绪色彩，而不是机械的关键词匹配。
    """
    return 1.0 - abs(memory.valence - valence) / 2.0


@dataclass(frozen=True, slots=True)
class RetrievalWeights:
    """重排公式的四项权重。

    默认值来自 `03-data-model.md § 6.2`：相关性最重（0.40），情绪一致度最轻（0.15）。
    """

    relevance: float = 0.40
    importance: float = 0.25
    recency: float = 0.20
    mood: float = 0.15

    def __post_init__(self) -> None:
        for name in ("relevance", "importance", "recency", "mood"):
            if getattr(self, name) < 0:
                raise ValueError(f"权重 {name} 不能为负")


DEFAULT_RETRIEVAL_WEIGHTS: Final[RetrievalWeights] = RetrievalWeights()


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """一条候选记忆及其**相关性** $R$。

    相关性由检索层提供：FTS5 BM25 归一化到 [0,1]，或向量检索的余弦相似度。
    领域层算不出它，所以它必须从外面进来。
    """

    memory: Memory
    relevance: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.relevance <= 1.0:
            raise ValueError(f"relevance 必须在 0 ~ 1，得到 {self.relevance!r}")


@dataclass(frozen=True, slots=True)
class MemorySearchHit:
    """一条检索命中，带**分数分解**。

    `importance` 项已经是 $\\text{importance} \\times \\text{strength\\_at}(now)$
    ——即「固有重要度」与「现在还想得起来吗」的乘积。

    Attributes:
        memory: 命中的记忆。
        score: 加权总分。
        relevance: $R$，来自检索层。
        importance: $I$，重要度 × 当下强度。
        recency: $N$，新近度。
        mood: $E$，情绪一致度。
        strength: $\\text{strength\\_at}(now)$，单独留一份便于展示。
    """

    memory: Memory
    score: float
    relevance: float
    importance: float
    recency: float
    mood: float
    strength: float


def rank_memories(
    candidates: Sequence[MemoryCandidate],
    now: datetime,
    *,
    query_valence: float | None = None,
    weights: RetrievalWeights = DEFAULT_RETRIEVAL_WEIGHTS,
    limit: int | None = DEFAULT_TOP_K,
    min_strength: float = FADE_THRESHOLD,
) -> list[MemorySearchHit]:
    """加权重排：$\\text{score} = w_r R + w_i I + w_n N + w_e E$。

    为什么不在 SQL 里算：公式需要当前情绪状态这类运行时参数，而且
    权重是要调的——放在纯函数里才能一条一条写测试、一次一次改权重。

    Args:
        candidates: 候选记忆与它们各自的相关性。
        now: 当下的虚拟时间。
        query_valence: 当前情绪的效价。`None` 表示没有情绪上下文，
            此时情绪一致度按 1.0（「任何记忆都同等一致」）计——它不是
            一个惩罚项，只是暂时失去区分力，排序因此与不带情绪时一致。
        weights: 四项权重。
        limit: 取前 N 条。`None` 表示全部返回。
        min_strength: 强度门槛，低于它的候选直接丢弃（遗忘阈）。

    Returns:
        按 `score` 降序排列的命中列表。分数相同时按 `memory.id` 升序，
        保证同一批候选重排多次结果一致（可复现性 P6）。
    """
    hits: list[MemorySearchHit] = []
    for candidate in candidates:
        memory = candidate.memory
        strength = strength_at(memory, now)
        if strength < min_strength:
            continue
        importance_term = memory.importance * strength
        recency = recency_at(memory, now)
        mood = 1.0 if query_valence is None else mood_alignment(memory, query_valence)
        score = (
            weights.relevance * candidate.relevance
            + weights.importance * importance_term
            + weights.recency * recency
            + weights.mood * mood
        )
        hits.append(
            MemorySearchHit(
                memory=memory,
                score=score,
                relevance=candidate.relevance,
                importance=importance_term,
                recency=recency,
                mood=mood,
                strength=strength,
            )
        )

    hits.sort(key=lambda hit: (-hit.score, hit.memory.id))
    return hits if limit is None else hits[:limit]


def should_resurrect(
    memory: Memory, *, min_importance: float = RESURRECTION_MIN_IMPORTANCE
) -> bool:
    """这条遗忘的记忆值不值得被强关联激活。

    只判「值不值得」，不判「关联够不够强」——后者要知道当前情境，
    属于更上层的判断。
    """
    return memory.forgotten and memory.importance >= min_importance


def resurrect(
    memory: Memory,
    *,
    now: datetime,
    strength_ratio: float = RESURRECTION_STRENGTH_RATIO,
) -> Memory:
    """「突然想起来」之后的记忆。

    强度恢复到 `importance × strength_ratio`——**不如原来清晰**。
    完全恢复等于遗忘从未发生，那「遗忘」就成了一件没有代价的事。
    """
    if not memory.forgotten:
        raise ValueError("只有已遗忘的记忆才需要被激活")
    return replace(
        memory,
        forgotten=False,
        strength=memory.importance * strength_ratio,
        last_recalled_at=now,
        recall_count=memory.recall_count + 1,
    )


def apply_consolidation(
    memories: Sequence[Memory],
    *,
    factor: float = CONSOLIDATION_IMPORTANCE_FACTOR,
) -> list[Memory]:
    """巩固之后，原始 episodic 记忆降权。

    信息已经上提到归纳出的 semantic 记忆里，原来的细节不再那么重要——
    「细节模糊了，但记住了大概」。降的是 `importance`，
    于是 `strength_at` 会连带下降，两者不会互相打架。
    """
    if not 0.0 < factor < 1.0:
        raise ValueError(f"factor 必须在 (0, 1) 之间，得到 {factor!r}")
    return [replace(memory, importance=round(memory.importance * factor, 6)) for memory in memories]


def preprocess_for_fts(text: str) -> str:
    """FTS 全文索引的写入/查询预处理。

    FTS5 内置的 `unicode61` 分词器对中文按**单字**切分，"火锅" 会匹配含
    "火" 或 "锅" 的记忆。`jieba` 可用时先在 Python 侧切好词、用空格连接，
    这样不需要编译任何扩展。

    **写入与查询必须用同一个函数**：索引里存的是切好的词，
    查询时若原样送进去，两边对不上，那条记忆就永远检索不到。
    这也是 `MemoryRepository` 唯一封装这个逻辑的原因。

    `jieba` 属于 `[zh]` 可选依赖。没装时降级为原样返回——
    检索精度下降，但不会报错。
    """
    try:
        import jieba
    except ImportError:
        return text
    return " ".join(jieba.cut_for_search(text))


@dataclass(frozen=True, slots=True)
class MemoryStats:
    """一个记忆库的规模与质量概况。

    `avg_strength` 用的是 `memory.strength` 这一列的快照值，不是现算的——
    正因为 `stats(persona_id)` 没有 `now` 参数，才需要这一列存在。

    Attributes:
        total: 总条数（含已遗忘）。
        active: 未遗忘条数。
        forgotten: 已遗忘条数。
        by_kind: 各类型的条数。
        avg_importance: 平均固有重要度。
        avg_strength: 平均强度快照。
        oldest_at: 最早的发生时间。
        newest_at: 最新的发生时间。
    """

    total: int = 0
    active: int = 0
    forgotten: int = 0
    by_kind: Mapping[str, int] = field(default_factory=dict)
    avg_importance: float = 0.0
    avg_strength: float = 0.0
    oldest_at: datetime | None = None
    newest_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.active + self.forgotten != self.total:
            raise ValueError(
                f"active + forgotten 必须等于 total：{self.active} + {self.forgotten} != {self.total}"
            )
        for name in ("total", "active", "forgotten"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} 不能为负")

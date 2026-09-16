"""仓储契约。

上层（`sim/`、CLI）需要读写记忆与行为日志，但**不该知道它们躺在 SQLite 里**。
``scripts/check_architecture.sh`` 第 3 组红线只允许组装根（``cli*.py``）与
``storage/`` 提到具体存储实现，所以形状必须有一个中立的地方放——就是这里。

三条约定（与 `docs/design/03-data-model.md` § 7 一致）：

1. 所有方法都可能抛 :class:`~alterego.kernel.errors.StorageError`；
2. 调用方在 ``with backend.transaction():`` 里连着调几个写方法时，它们会合并成
   一个事务——**仓储自己不 BEGIN**，事务边界属于调用方；
3. **仓储不含业务逻辑**。「重要度怎么衰减」「什么时候该降权」属于 `domain/`，
   仓储只负责「一行 ↔ 一个对象」。

依据: docs/design/03-data-model.md § 6.9、§ 7
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Protocol


if TYPE_CHECKING:
    from alterego.domain.dataset import ActivityRow, MessageRow, TickRow
    from alterego.domain.memory import Memory, MemoryKind


__all__ = [
    "ActivityRecord",
    "ActivityRepository",
    "DatasetSourceRepository",
    "MemoryRepository",
    "PersonaRecord",
    "PersonaRepository",
    "ScheduleRecord",
    "ScheduleRepository",
    "SourceRecord",
    "SourceRepository",
]


@dataclass(frozen=True, slots=True)
class ActivityRecord:
    """``activity_log`` 的一行。

    它暂时住在这里而不是 ``domain/``：目前只有一个用途——渲染进提示词。
    还没有需要纯函数去处理的规则，为它单开一个领域模型是空壳。

    Attributes:
        id: 行为 id。
        intent: 当时选中的意图，取自 `docs/DESIGN.md` 的意图清单。
        description: 一句话描述做了什么。
        started_at: 开始时间（**虚拟时间**，由 ``ctx.clock`` 决定）。
        category: ``internal`` / ``social`` / ``outbound``。
        location: 地点，可能为空。
        inner_voice: 内心独白，含「想说但没说」的那些。梳理记忆时它比
            ``description`` 更有信息量——一个人记住的往往是自己当时怎么想，
            而不是自己当时做了什么。
        duration_minutes: 持续分钟数，可能为 0（未知）。
    """

    id: str
    intent: str
    description: str
    started_at: datetime
    category: str = ""
    location: str = ""
    inner_voice: str = ""
    duration_minutes: int = 0


class MemoryRepository(Protocol):
    """记忆的读写。"""

    def save(self, memory: Memory) -> None:
        """写入一条记忆。id 相同时覆盖。"""
        ...

    def save_many(self, memories: Sequence[Memory]) -> None:
        """批量写入。一次巩固会产出好几条，逐条写会开好几个事务。"""
        ...

    def get(self, memory_id: str) -> Memory | None:
        """按 id 取一条。不存在返回 `None`。"""
        ...

    def list_recent(
        self,
        persona_id: str,
        *,
        since: datetime,
        kind: MemoryKind | None = None,
        not_consolidated: bool = False,
        limit: int = 100,
    ) -> list[Memory]:
        """按时间列出 ``since`` 之后的记忆，最早的在前面。

        ``not_consolidated=True`` 时只给还没被巩固过的那些。两个方向共用
        这一个方法：梳理要「最近发生的」，巩固要「还没归纳过的」。
        """
        ...

    def mark_consolidated(self, memory_ids: Sequence[str], at: datetime) -> None:
        """标记这批记忆已被巩固。

        标记而不是删除：巩固只是「细节变模糊」，不是「没发生过」。
        ``at`` 是**虚拟时间**，不是墙上时间。
        """
        ...

    def set_importance(self, updates: Sequence[tuple[str, float]]) -> None:
        """批量改重要度，值为 ``(memory_id, importance)``。

        ``apply_consolidation()`` 算出来的新值由调用方传进来——
        仓储不重复一遍衰减公式。
        """
        ...


class ActivityRepository(Protocol):
    """行为日志的读写。"""

    def list_undistilled(
        self,
        persona_id: str,
        *,
        since: datetime,
        limit: int = 200,
    ) -> list[ActivityRecord]:
        """列出 ``since`` 之后发生的、**还没被梳理过**的行为，按时间正序。

        正序而不是倒序：梳理是「读一遍这一天」，倒着读会把因果读反。
        """
        ...

    def mark_distilled(self, activity_ids: Sequence[str], at: datetime) -> None:
        """标记这批行为已被梳理进记忆。"""
        ...

    def list_range(
        self,
        persona_id: str,
        *,
        since: datetime,
        until: datetime,
        limit: int = 500,
    ) -> list[ActivityRecord]:
        """``since`` 到 ``until`` 之间的**全部**行为，按时间正序。

        和 :meth:`list_undistilled` 的区别是**不看 ``distilled_at``**：
        写日记要的是「这一天真实发生过什么」，而不是「还有什么没被梳理进记忆」。
        被梳理过的行为同样是那天的一部分——恰恰是被梳理过的那些往往最要紧。
        """
        ...


# ── 知识库要读的三样东西 ────────────────────────────────────
#
# 下面三个契约是「把库里的东西写成笔记」用的。它们**只读**：知识库是
# 库的**下游**，从不往回写。这一条很要紧——一旦允许它回写，
# 「用户手改了一个 Markdown 文件」就得有冲突解决策略，
# 那是另一个量级的事（docs/plans/2026-09-16-obsidian-vault.md § 9）。


@dataclass(frozen=True, slots=True)
class PersonaRecord:
    """``persona`` 的一行，只取写索引页开头要用的那几个字段。

    刻意**不带** ``persona_json``：整份人格数据有性格、表达、偏好、背景、
    情绪基线，而这里只要一句「我是谁」。全量传过来会诱使调用方
    把整段塞进笔记，那既不是用户想看的，也会随人格演化和文件内容脱节。
    """

    id: str
    name: str
    age: int | None = None
    gender: str = ""
    city: str = ""
    occupation: str = ""
    version: int = 1


@dataclass(frozen=True, slots=True)
class ScheduleRecord:
    """``schedule_block`` 的一行：计划做什么，以及实际几点做的。

    计划和实际**都要**带出来。只写计划，笔记就成了一张没人兑现的时间表；
    而 ``deviation_note`` 是「为什么没按计划来」，那通常是这一天里
    最有意思的一句话。
    """

    id: str
    day: date
    start_at: datetime
    end_at: datetime
    activity: str
    category: str = "other"
    location: str = ""
    actual_start_at: datetime | None = None
    actual_end_at: datetime | None = None
    deviation_note: str = ""


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """``source_item`` 的一行：它搜集到的一条信息。

    ``dropped=True`` 的那些**不该**出现在这里——被判定为提示词注入、
    过长或重复的内容是过滤掉的中间产物，不是它「搜集到的东西」。
    """

    id: str
    url: str
    fetched_at: datetime
    title: str = ""
    summary: str = ""
    source_kind: str = "search"
    published_at: datetime | None = None
    lang: str = ""
    memory_id: str | None = None


class PersonaRepository(Protocol):
    """人设的只读访问。"""

    def get(self, persona_id: str) -> PersonaRecord | None:
        """按 id 取。不存在返回 `None`。"""
        ...

    def find_by_name(self, name: str) -> PersonaRecord | None:
        """按**名字**找。敲命令的人手上有名字，id 是他从没见过的一串字符。"""
        ...

    def list_all(self) -> list[PersonaRecord]:
        """全部人设，按创建时间。用来在「有多个」时报出候选。"""
        ...


class ScheduleRepository(Protocol):
    """日程的只读访问。"""

    def list_day(self, persona_id: str, *, day: date) -> list[ScheduleRecord]:
        """某一天的全部日程块，按开始时间正序。"""
        ...

    def list_range(
        self,
        persona_id: str,
        *,
        since: datetime,
        until: datetime,
        limit: int = 500,
    ) -> list[ScheduleRecord]:
        """一段时间的日程块。用来补写过去几天漏掉的日记。"""
        ...


class SourceRepository(Protocol):
    """搜集到的信息的只读访问。"""

    def list_kept(
        self,
        persona_id: str,
        *,
        since: datetime,
        limit: int = 100,
    ) -> list[SourceRecord]:
        """``since`` 之后**没被丢掉**的条目，按抓取时间正序。

        ``dropped`` 的那些不出现：它们是过滤掉的中间产物
        （提示词注入、过长、重复），不是它「搜集到的东西」。
        """
        ...


# ────────────────────────────────────────────────────────────
# 训练数据集的源。**下面这个契约是只读的**：数据集是库的派生产物，
# 从不往回写（ADR-0011）。这也是它不需要任何迁移的原因。
# ────────────────────────────────────────────────────────────


class DatasetSourceRepository(Protocol):
    """训练数据集要用到的三张源表的只读访问。

    **返回的是 ``domain.dataset`` 里的行，不是本模块的 ``*Record``。**
    看起来不一致，但另一条路更糟：那三个行类型与库里的列一一对应，
    而 ``domain/dataset.py`` 的纯函数直接吃它们——中间再放一层 DTO，
    只会让时间字段在「库里的 TEXT → ``datetime`` → 又变回 TEXT」之间来回转两趟，
    而每一趟都是可能出错的转换点。

    这也正是本模块文件头对 :class:`ActivityRecord` 的解释里说的那种情况：
    「还没有需要纯函数去处理的规则」的行住在这里；**有纯函数要吃它们的行，
    就住在 ``domain/``**。
    """

    def list_conversation_messages(
        self,
        persona_id: str,
        *,
        since: datetime,
        limit: int = 20000,
    ) -> list[MessageRow]:
        """**和用户的**会话里的消息，按 ``(conversation_id, created_at, id)`` 正序。

        只取 ``counterpart_kind = 'user'`` 的会话。和 NPC 的来往不在这一批里：
        那批数据训的是「它怎么和别的角色相处」，与「它怎么和你说话」
        是两件事，混在一起会稀释掉后者。

        正序是有意的——``domain`` 那边合并相邻同向的消息，
        顺序错了合出来的话也就错了。
        """
        ...

    def list_ticks(
        self,
        persona_id: str,
        *,
        since: datetime,
        limit: int = 5000,
    ) -> list[TickRow]:
        """``since`` 之后的推演日志，按 ``(virtual_time, id)`` 正序。

        ``status`` 不在这里筛。失败的轨迹也要取出来——由 ``domain`` 决定
        哪些能用，因为「哪些算成功」是业务判断，不是取数的事。
        """
        ...

    def list_activities(
        self,
        persona_id: str,
        *,
        since: datetime,
        limit: int = 20000,
    ) -> list[ActivityRow]:
        """``since`` 之后的行为日志，按 ``(started_at, id)`` 正序。"""
        ...

    def map_tick_intents(
        self,
        persona_id: str,
        *,
        since: datetime,
    ) -> dict[str, str]:
        """``tick_id → chosen_intent``，给工具调用数据集补「它想做的是什么」。

        单独的查询而不是让调用方从 ``list_ticks`` 里自己挖：行为日志里
        只有被规范化过的 ``intent``，而两者之差恰恰是「它想做」与「它实际做的」。
        """
        ...

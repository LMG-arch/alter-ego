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
from datetime import datetime
from typing import TYPE_CHECKING, Protocol


if TYPE_CHECKING:
    from alterego.domain.memory import Memory, MemoryKind


__all__ = ["ActivityRecord", "ActivityRepository", "MemoryRepository"]


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

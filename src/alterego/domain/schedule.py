"""领域层：日程。

日程回答一个很小但被到处问的问题——**「现在它在干嘛」**。

这个问题的答案决定了三件事：

1. 打扰预算要不要放行（工作时段不可打断 → 主动消息被拦下）
2. 疲劳该累积还是该恢复（睡眠时段每小时恢复 0.8）
3. 情绪的自然回归是否被事件打断

所以 `ScheduleBlock` 虽然只是一张表的镜像，`current_block()` 与
`is_interruptible()` 却是**纯函数**：给定块列表与时间点，返回答案，不查库。
推演层拿到日程后可以反复问，问多少次都不会产生副作用。

依据: docs/design/01-architecture.md § 3、docs/design/03-data-model.md § 4.2（表 13）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, Literal, get_args


__all__ = [
    "SCHEDULE_CATEGORIES",
    "SCHEDULE_SOURCES",
    "SLEEP_CATEGORY",
    "ScheduleBlock",
    "ScheduleCategory",
    "ScheduleSource",
    "current_block",
    "is_interruptible",
]

ScheduleCategory = Literal[
    "sleep",
    "work",
    "meal",
    "commute",
    "leisure",
    "social",
    "chore",
    "other",
]
"""日程分类。与 `schedule_block.category` 的取值一一对应。"""

ScheduleSource = Literal["template", "llm", "manual"]
"""这一块日程是怎么来的：模板生成 / LLM 现编 / 用户手改。"""

SCHEDULE_CATEGORIES: Final[tuple[str, ...]] = get_args(ScheduleCategory)
SCHEDULE_SOURCES: Final[tuple[str, ...]] = get_args(ScheduleSource)

SLEEP_CATEGORY: Final[str] = "sleep"
"""睡眠分类名。疲劳恢复、强制 rest、无 tick 决策都以它为准。"""


@dataclass(frozen=True, slots=True)
class ScheduleBlock:
    """日程中的一段时间。

    字段与 `schedule_block` 表一一对应（表 13）。`actual_start_at` /
    `actual_end_at` 记录**实际**发生的时间——日程是计划，人是会赖床的，
    两者的差值就是 `deviation_note` 想解释的东西。

    Attributes:
        id: 主键。
        persona_id: 归属人格。
        day: 这一天属于哪一天（`YYYY-MM-DD`，虚拟时间的当地日期）。
        start_at: 计划开始。
        end_at: 计划结束。
        activity: 干什么（"工作" / "午餐" / "通勤"）。
        category: 分类，见 `ScheduleCategory`。
        interruptible: 是否允许被主动联系打断。
        location: 地点。
        source: 来源，见 `ScheduleSource`。
        actual_start_at: 实际开始。
        actual_end_at: 实际结束。
        deviation_note: 偏差原因（由 LLM 或规则填写）。
        created_at: 创建时间。
    """

    id: str
    persona_id: str
    day: date
    start_at: datetime
    end_at: datetime
    activity: str
    category: ScheduleCategory
    interruptible: bool = True
    location: str | None = None
    source: ScheduleSource = "template"
    actual_start_at: datetime | None = None
    actual_end_at: datetime | None = None
    deviation_note: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.category not in SCHEDULE_CATEGORIES:
            raise ValueError(
                f"未知的日程分类 {self.category!r}，合法值：{', '.join(SCHEDULE_CATEGORIES)}"
            )
        if self.source not in SCHEDULE_SOURCES:
            raise ValueError(
                f"未知的日程来源 {self.source!r}，合法值：{', '.join(SCHEDULE_SOURCES)}"
            )
        if self.end_at <= self.start_at:
            raise ValueError(
                f"日程块结束时间必须晚于开始时间：{self.start_at.isoformat()} → {self.end_at.isoformat()}"
            )
        if (
            self.actual_start_at is not None
            and self.actual_end_at is not None
            and self.actual_end_at < self.actual_start_at
        ):
            raise ValueError("实际结束时间不能早于实际开始时间")

    @property
    def is_sleep(self) -> bool:
        """是否是睡眠时段。"""
        return self.category == SLEEP_CATEGORY

    @property
    def duration_minutes(self) -> int:
        """计划时长（分钟）。"""
        return int((self.end_at - self.start_at).total_seconds() // 60)

    def contains(self, moment: datetime) -> bool:
        """`moment` 是否落在这块日程内。

        左闭右开：`start_at <= moment < end_at`。
        相邻两块 [09:00, 12:00) 与 [12:00, 13:00) 若都用闭区间，
        12:00 会同时命中两块，「现在在干嘛」就有了两个答案。
        """
        return self.start_at <= moment < self.end_at


def current_block(blocks: list[ScheduleBlock], now: datetime) -> ScheduleBlock | None:
    """`now` 时刻正在进行的日程块。

    同一时刻理论上只应有一块（生成日程时会消解重叠）。若真出现重叠，
    返回**开始时间最晚**的那块——它是后写的，更可能是修正后的结果。

    Returns:
        正在进行的块；不在任何块内（日程有缝隙）时返回 `None`。
    """
    matches = [block for block in blocks if block.contains(now)]
    if not matches:
        return None
    return max(matches, key=lambda block: block.start_at)


def is_interruptible(blocks: list[ScheduleBlock], now: datetime) -> bool:
    """此刻是否允许被打断——打扰预算的关键输入。

    不在任何日程块内时返回 `True`：没有计划意味着没在忙，
    比「不确定所以拦下」更符合直觉（拦下会让主动消息凭空消失）。
    """
    block = current_block(blocks, now)
    return True if block is None else block.interruptible

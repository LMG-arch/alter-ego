"""单次 tick 的工作区：阶段之间传递数据的唯一载体。

``TickContext`` 是「推演引擎」与「插件阶段」之间**唯一的**交互面。所有跨阶段的数据都放在
这里，而不是塞进全局变量或内核单例——这样一次 tick 的完整推理链可以原样写进 ``tick_log``，
``alterego why`` 才有东西可讲。

两个刻意的约束：

* **``state`` 在 tick 内视为不变。** 阶段要改世界，就把改动写进 ``StageResult.changes``，
  由引擎在 tick 末尾一次性合并、一次性持久化。这保证「一个 tick 要么整体成立、要么整体回退」。
* **``rng`` 是注入的。** 阶段里禁止用全局 ``random``：同一颗种子必须能重放同一天
  （设计原则 P6）。

``Persona`` / ``Emotion`` 这类字段现在标注为 ``Any``（并留了 ``TODO(阶段 D/E)`` 的记号）：
它们属于领域层，而领域层还没落地。等它落地后逐个收紧，接口不变、只变注解。

依据: docs/design/04-simulation-loop.md § 2.2
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from random import Random
from typing import Any


__all__ = ["StateSnapshot", "TickContext"]


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """一次 tick 开始时的世界快照，tick 内视为只读。

    阶段不能就地改它——想改就调 :meth:`evolve` 产出一份新的，交给引擎合并。
    这样「谁在什么时候把情绪改成了什么」永远可追溯。
    """

    persona: Any  # TODO(阶段 D): domain.persona.Persona
    emotion: Any  # TODO(阶段 D): domain.emotion.Emotion
    relationships: Mapping[str, Any] = field(default_factory=dict)  # TODO(阶段 D)
    current_block: Any = None  # TODO(阶段 D): domain.schedule.ScheduleBlock | None
    recent_memories: tuple[Any, ...] = ()  # TODO(阶段 D): tuple[domain.memory.Memory, ...]
    today_activity: tuple[Any, ...] = ()  # TODO(阶段 D): tuple[domain.activity.Activity, ...]
    budget: Any = None  # TODO(阶段 F): sim.budget.BudgetUsage
    world: Any = None  # TODO(阶段 D): domain.world.World

    def evolve(self, **changes: Any) -> StateSnapshot:
        """基于当前快照产出一份改了几个字段的新快照。

        未知字段名会被 ``replace`` 拒绝并抛 ``TypeError``——这是故意的：写错字段名时
        宁可当场炸掉，也不要静默地「改了但没生效」。
        """
        return replace(self, **changes)


@dataclass(slots=True)
class TickContext:
    """一次 tick 的完整工作区。由引擎创建，传给每个阶段，最后落盘。"""

    # ── 身份与时间 ──
    tick_id: str
    virtual_now: datetime
    #: 贯穿本 tick 所有事件与日志，用来把「一次推理」串成一条线。
    correlation_id: str

    # ── 状态快照（tick 内视为不变）──
    state: StateSnapshot

    # ── 随机源（tick 内固定，保证可复现）──
    rng: Random

    # ── 数据流：各阶段依次填 ──
    #: ← Sense 写入
    percepts: Any = None
    #: ← Reflect 写入
    reflections: list[str] = field(default_factory=list)
    #: ← Intention 写入（含权重）
    candidates: list[Any] = field(default_factory=list)
    #: ← Intention 写入（最终选中）
    chosen_intent: Any = None
    #: ← Intention / 预算校验写入。**被拦下的意图留在这里，不会消失**
    #: （见 docs/adr/0005-downgrade-instead-of-discard-suppressed-intents.md）
    suppressed: list[Any] = field(default_factory=list)
    #: ← Act 写入
    actions: list[Any] = field(default_factory=list)
    #: ← Express 写入（动态 / 消息 / 独白）
    expressions: list[Any] = field(default_factory=list)
    #: ← 各阶段追加
    llm_calls: list[Any] = field(default_factory=list)
    #: ← 任意阶段追加。这些说明最终写进 ``tick_log``，是「可解释性」的原料
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        """记一句「为什么这么做」。

        空字符串会被忽略：``notes`` 是要展示给人看的，塞进去一片空白只会让列表变长。
        """
        if text:
            self.notes.append(text)

    @property
    def has_chosen_intent(self) -> bool:
        """本 tick 是否选中了意图。没选中通常意味着「它在发呆」——这也是合法结果。"""
        return self.chosen_intent is not None

"""Sense 阶段：把「世界是什么样」翻译成「这一切落到它眼里是什么」。

``order = 10``，六个阶段里唯一没有前置依赖的一个——后面五个都读它写下的
``ctx.percepts``。

**这个阶段不调模型。** 感知必须是确定的：现在几点、在哪、有没有人在等它回话，
这些都是查得出来的事实。让模型参与感知会把「它为什么没注意到你的消息」
变成一个不可复现的答案（P6），而这句话恰恰是用户最想问的那句。

依据: docs/design/04-simulation-loop.md § 3.1
"""

from __future__ import annotations

from dataclasses import dataclass

from alterego.interfaces.simulation import Percepts, StageResult
from alterego.sim.context import TickContext
from alterego.sim.stages.common import (
    clock_text,
    describe_block,
    last_started_at,
    minutes_between,
)


__all__ = ["SenseStage"]


@dataclass(slots=True)
class SenseStage:
    """建立本轮感知。"""

    name: str = "sense"
    order: int = 10
    depends_on: tuple[str, ...] = ()
    enabled: bool = True

    async def run(self, ctx: TickContext) -> StageResult:
        state = ctx.state
        block = state.current_block
        since_user = minutes_between(state.last_user_message_at, ctx.virtual_now)
        idle = minutes_between(last_started_at(state.today_activity), ctx.virtual_now) or 0.0

        percepts = Percepts(
            virtual_now=ctx.virtual_now,
            block=block,
            unread_messages=int(state.unread_messages),
            minutes_since_last_user_message=since_user,
            idle_minutes=idle,
        )

        ctx.note(f"感知：{clock_text(ctx.virtual_now)}，{describe_block(block)}")
        if percepts.unread_messages:
            ctx.note(f"用户在等它回话：{percepts.unread_messages} 条")
        elif since_user is None:
            ctx.note("你们还没说过话")

        return StageResult(ok=True, changes={"percepts": percepts})

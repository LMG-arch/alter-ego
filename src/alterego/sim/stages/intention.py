"""Intention 阶段：此刻它更想做什么，以及那件事允不允许做。

``order = 50``，依赖反思——因为权重里要读情绪。

**权重是算出来的，不是问出来的。** 「晚上十一点还有力气工作吗」这类判断
写成提示词，模型每次给的答案都不一样，``alterego why`` 就没法解释；写成
:func:`~alterego.sim.intents.build_candidates` 之后同一份世界永远得到同一组
权重（P6），而且每组权重背后都带着一句可以原样展示的 ``reason``。

选中之后还有一道**预算闸门**：产生用户可见内容的意图要先问
:func:`~alterego.sim.budget.check_budget`。被拦下的意图**不消失**——
它进 ``ctx.suppressed`` 并落进 ``tick_log``（见 ADR-0005：降级而不是丢弃）。
用户问「它明明想找我说话怎么没发」，答案应该是一条记录，而不是一片空白。

依据: docs/design/04-simulation-loop.md § 4、§ 5
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from alterego.interfaces.simulation import Intent, StageResult
from alterego.sim.budget import BudgetUsage, check_budget
from alterego.sim.context import TickContext
from alterego.sim.intents import (
    IntentCatalog,
    IntentContext,
    build_candidates,
    choose,
)


__all__ = ["IntentionStage"]


@dataclass(slots=True)
class IntentionStage:
    """挑出这一轮要做的（或不做的）那件事。

    ``budget`` 是 ``[simulation.disturb_budget]`` 那一段配置。让它从构造器
    进来而不是自己读配置，是为了让「它一天最多发几条」在一轮推演里完全确定——
    运行期改配置不该让同一轮算出一个不同答案。
    """

    catalog: IntentCatalog
    budget: Any
    name: str = "intention"
    order: int = 50
    depends_on: tuple[str, ...] = ("reflect",)
    enabled: bool = True

    async def run(self, ctx: TickContext) -> StageResult:
        candidates = build_candidates(self.catalog, self._context(ctx))
        ctx.candidates.extend(candidates)

        chosen = choose(candidates, rng=ctx.rng)
        if chosen is None:
            ctx.note("这一轮什么都不想做")
            return StageResult(ok=True, changes={})

        wanted = Intent(type=chosen.intent, urgency=chosen.weight, reason=chosen.reason)
        ctx.note(f"想做：{chosen.intent.name}（权重 {chosen.weight:.2f}，{chosen.reason}）")

        usage = self._usage(ctx)
        gated = self._apply_budget(ctx, wanted, usage)
        if gated is not wanted:
            # 拦下了（可能换来一条降级后的意图），用量要跟着动。
            usage = self._count_suppressed(usage, wanted)
        state = ctx.state.evolve(budget=usage)
        if gated is None:
            return StageResult(ok=True, changes={"state": state})
        return StageResult(ok=True, changes={"chosen_intent": gated, "state": state})

    # ── 内部 ──

    def _context(self, ctx: TickContext) -> IntentContext:
        """把 ``TickContext`` 压成算权重看得到的那一小块世界。"""
        percepts = ctx.percepts
        emotion = ctx.state.emotion
        block = ctx.state.current_block if percepts is None else percepts.block
        since_user = None if percepts is None else percepts.minutes_since_last_user_message
        return IntentContext(
            unread_messages=int(ctx.state.unread_messages),
            hours_since_last_user_reply=(None if since_user is None else since_user / 60.0),
            hour=ctx.virtual_now.hour,
            valence=float(getattr(emotion, "valence", 0.0)),
            fatigue=float(getattr(emotion, "fatigue", 0.0)),
            activity=str(getattr(block, "activity", "") or ""),
            interruptible=bool(getattr(block, "interruptible", True)),
            idle_minutes=float(0.0 if percepts is None else percepts.idle_minutes),
        )

    def _usage(self, ctx: TickContext) -> BudgetUsage:
        """取当日用量。没有就用一条空的——**不返回 ``None``**。

        时间倒流（导入旧库、换时区）会让快照里的 ``day`` 与今天不一样，
        那种情况当成新的一天从头算，比让整轮推演炸掉合理。
        """
        usage = ctx.state.budget
        if usage is None or usage.day != ctx.virtual_now.date():
            return BudgetUsage(day=ctx.virtual_now.date())
        return usage

    @staticmethod
    def _count_suppressed(usage: BudgetUsage, intent: Intent) -> BudgetUsage:
        """给被拦下的意图记一笔。按 ``budget_kind`` 分账。"""
        if intent.type.budget_kind == "post":
            return replace(usage, posts_suppressed=usage.posts_suppressed + 1)
        return replace(usage, messages_suppressed=usage.messages_suppressed + 1)

    def _apply_budget(
        self,
        ctx: TickContext,
        intent: Intent,
        usage: BudgetUsage,
    ) -> Intent | None:
        """过预算闸门。

        Returns:
            放行时返回**原样的意图对象**（调用方靠 ``is`` 判断有没有被拦下）；
            被拦下且规则给了降级目标时返回**降级后的意图**；
            被拦下且无路可退时返回 ``None``。

        ``urgency`` 直接取权重：``reply`` 在有未读时是 1.0，动用紧急额度；
        ``reach_out`` 是 0.06，走正常额度。**不需要为「回复」写特例**——
        而写特例的版本会在有人调整 ``reply`` 权重的那天悄悄失效。
        """
        if not intent.type.outbound:
            return intent

        decision = check_budget(
            intent.type.budget_kind or "message",
            usage=usage,
            now=ctx.virtual_now,
            config=self.budget,
            urgency=intent.urgency,
            block=ctx.state.current_block,
        )
        if decision.allowed:
            return intent

        ctx.suppressed.append(
            {
                "intent": intent.name,
                "reason": decision.reason,
                "urgency": round(intent.urgency, 3),
            }
        )
        ctx.note(f"没发出去：{decision.reason}")

        if decision.downgrade_to is None:
            return None
        replacement = self.catalog.get(decision.downgrade_to)
        if replacement is None:
            ctx.note(f"想降级成 {decision.downgrade_to}，但目录里没有这条")
            return None
        return Intent(type=replacement, reason=f"{intent.name} 被拦下后改做它")

"""Persist 阶段：把这一轮发生的事翻译成「要落库的行」。

``order = 110``，排在最后。**本阶段不调模型、也不开事务**——
它只做翻译：``ctx.actions`` / ``ctx.expressions`` 是一个人看得懂的过程，
``activity_log`` / ``message`` 是库里躺得住的形状，两者之间的换算放在这里，
引擎那边就只剩「开一个事务、把这些行写进去」。

**id 是从 ``tick_id`` 派生的，不是随机的。** ``f"{tick_id}-m0"`` 意味着
重跑同一段时间得到同一批 id，而 ``append`` 是幂等的（``INSERT OR REPLACE``）——
所以「推演失败后重跑」不会让用户看到自己说过两遍话。用 uuid 就会。

**``BudgetUsage`` 的「已发出」在这里加，而「被拦下」在意图阶段加。**
两个阶段的账不能串：意图阶段只知道「想发但被拦了」，这里才知道「真的发出去了」。
一个被拦下的 ``reach_out`` 不会在这里多记一笔，因为它压根没生成出内容。

依据: docs/design/04-simulation-loop.md § 3.6
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alterego.interfaces.repository import ActivityRecord, MessageRecord, SocialPostRecord
from alterego.interfaces.simulation import StageResult
from alterego.sim.budget import record_no_reply, record_reply, record_sent
from alterego.sim.context import TickContext


__all__ = ["PersistStage"]


@dataclass(slots=True)
class PersistStage:
    """把过程翻译成行。

    ``budget`` 是 ``[simulation.disturb_budget]`` 那一段配置。让它从构造器
    进来而不是自己在阶段里读配置，是为了让「熔断阈值是多少」在一次推演里
    完全确定——配置在 tick 中途被改，就已经不是可复现的推演了。
    """

    budget: Any
    name: str = "persist"
    order: int = 110
    depends_on: tuple[str, ...] = ("express",)
    enabled: bool = True

    async def run(self, ctx: TickContext) -> StageResult:
        ctx.activity_records.extend(self._activities(ctx))
        ctx.message_records.extend(self._messages(ctx))
        ctx.post_records.extend(self._posts(ctx))
        usage = self._budget(ctx)
        ctx.note(f"这一轮用了 {self._describe(usage)}")
        return StageResult(
            ok=True,
            changes={
                "state": ctx.state.evolve(budget=usage),
                "activity_records": tuple(ctx.activity_records),
                "message_records": tuple(ctx.message_records),
                "post_records": tuple(ctx.post_records),
            },
        )

    # ── 行为行 ──

    def _activities(self, ctx: TickContext) -> list[ActivityRecord]:
        """一条行为对应一行。

        独白按 ``intent`` 归到对应的那一行上，而不是单独开一行：
        「在做什么」和「当时怎么想」是同一个瞬间的两面，分成两行会让时间线上
        多出一倍看起来一样的条目。
        """
        voices = {
            str(expression.get("intent", "")): str(expression.get("content", ""))
            for expression in ctx.expressions
            if expression.get("kind") == "inner_voice"
        }
        persona_id = str(getattr(ctx.state.persona, "id", "") or "")
        records: list[ActivityRecord] = []
        for index, action in enumerate(ctx.actions):
            name = str(action.get("intent", ""))
            records.append(
                ActivityRecord(
                    id=f"{ctx.tick_id}-a{index}",
                    persona_id=persona_id,
                    intent=name,
                    category=str(action.get("category", "") or "internal"),
                    description=str(action.get("summary", "") or name),
                    started_at=ctx.virtual_now,
                    inner_voice=voices.pop(name, ""),
                    detail={
                        key: value
                        for key, value in action.items()
                        if key not in {"intent", "summary", "category"}
                    },
                    tick_id=ctx.tick_id,
                )
            )

        # 想说但没说的那些也要留一行。用户问「它明明想找我说话怎么没发」，
        # 答案必须是 ``activity_log`` 里躺着的一条，而不是一片空白。
        for index, item in enumerate(ctx.suppressed):
            intent_name = str(_get(item, "intent") or "")
            reason = str(_get(item, "reason") or "")
            records.append(
                ActivityRecord(
                    id=f"{ctx.tick_id}-s{index}",
                    persona_id=persona_id,
                    intent=intent_name,
                    category="outbound",
                    description=f"想做「{intent_name}」，但没做",
                    started_at=ctx.virtual_now,
                    inner_voice=voices.get(intent_name, ""),
                    suppressed_intent=intent_name,
                    suppress_reason=reason,
                    tick_id=ctx.tick_id,
                )
            )
        return records

    # ── 消息行 ──

    def _messages(self, ctx: TickContext) -> list[MessageRecord]:
        """出站消息落 ``message`` 表。

        ``initiative`` 只对 ``reach_out`` 为真：区分「你说话它回」与「它突然找你」
        是这套数据最要紧的一列，而这两者的区别**只有意图知道**——
        从内容上看，「在忙吗」既可能是回复也可能是搭话。
        """
        conversation_id = self._conversation_id(ctx)
        if not conversation_id:
            return []

        persona_id = str(getattr(ctx.state.persona, "id", "") or "")
        records: list[MessageRecord] = []
        for index, expression in enumerate(ctx.expressions):
            if expression.get("kind") != "message":
                continue
            intent_name = str(expression.get("intent", ""))
            records.append(
                MessageRecord(
                    id=f"{ctx.tick_id}-m{index}",
                    conversation_id=conversation_id,
                    direction="outbound",
                    sender_id=persona_id,
                    content=str(expression.get("content", "")),
                    initiative=intent_name == "reach_out",
                    motivation=str(expression.get("motivation", "")),
                    trigger_note=str(expression.get("trigger_note", "")),
                    tick_id=ctx.tick_id,
                    created_at=_as_datetime(expression.get("deliver_at")) or ctx.virtual_now,
                )
            )

        return records

    # ── 动态行 ──

    def _posts(self, ctx: TickContext) -> list[SocialPostRecord]:
        """一条动态对应一行 ``social_post``。

        为什么它不能只当一条行为记进 ``activity_log``：动态是**给别人看的**，
        它有权被点赞、被评论、被单独翻出来重读（``like_count`` / ``visible``
        那几列就是为这件事存在的）。行为日志是它的日记，动态是它的朋友圈——
        同一次动作在两处各有各的寿命。

        情绪快照（``mood_*``）取**发的那一刻**的值：情绪曲线会继续演化，
        而「这条动态是在什么心情下发的」必须停在这一刻。
        """
        persona_id = str(getattr(ctx.state.persona, "id", "") or "")
        emotion = ctx.state.emotion
        valence = getattr(emotion, "valence", None)
        arousal = getattr(emotion, "arousal", None)
        # 「这条动态是做完哪件事之后发的」。对得上就填，对不上留空——
        # 空字符串是「没关联」，编一个不存在的 id 会让详情页报一个查不到的外键。
        activities_by_intent = {
            str(getattr(record, "intent", "")): str(getattr(record, "id", ""))
            for record in ctx.activity_records
        }
        records: list[SocialPostRecord] = []
        for index, expression in enumerate(ctx.expressions):
            if expression.get("kind") != "post":
                continue
            intent_name = str(expression.get("intent", ""))
            records.append(
                SocialPostRecord(
                    id=f"{ctx.tick_id}-p{index}",
                    persona_id=persona_id,
                    content=str(expression.get("content", "")),
                    posted_at=_as_datetime(expression.get("deliver_at")) or ctx.virtual_now,
                    location=str(getattr(ctx.state.current_block, "location", "") or ""),
                    mood_label=str(getattr(emotion, "label", "") or ""),
                    mood_valence=None if valence is None else float(valence),
                    mood_arousal=None if arousal is None else float(arousal),
                    intent_motivation=str(expression.get("motivation", "")),
                    trigger_note=str(expression.get("trigger_note", "")),
                    activity_ref=activities_by_intent.get(intent_name, ""),
                    tick_id=ctx.tick_id,
                )
            )
        return records

    @staticmethod
    def _conversation_id(ctx: TickContext) -> str:
        """这次推演的会话 id。

        从最近的入站消息上取，而不是现拼一个 ``f"{persona_id}:user"``：
        会话 id 的生成规则属于组装根，阶段不该再实现一遍，
        否则两处规则一旦不一致，消息就会被写进一个没人读的会话里。
        """
        for message in reversed(ctx.state.recent_messages):
            found = str(getattr(message, "conversation_id", "") or "")
            if found:
                return found
        return ""

    # ── 预算 ──

    def _budget(self, ctx: TickContext) -> Any:
        """算出这一轮之后的当日用量。

        ``state.budget`` 为 ``None`` 时直接跳过记账：那是「组装根没给预算快照」，
        不是「用量为零」。凭空造一个零用量的对象会让熔断看起来像已经解除。
        """
        usage = ctx.state.budget
        if usage is None or usage.day != ctx.virtual_now.date():
            return None

        for expression in ctx.expressions:
            kind = str(expression.get("kind", ""))
            if kind == "post":
                usage = record_sent(usage, kind="post", at=ctx.virtual_now)
            elif kind == "message":
                usage = record_sent(usage, kind="message", at=ctx.virtual_now)

        replied = any(
            str(expression.get("intent", "")) == "reply" for expression in ctx.expressions
        )
        if replied:
            # 回了话就把「连着没回」清零——熔断是给「一直被无视」用的，
            # 不是给「偶尔晚回一次」用的。
            usage = record_reply(usage)
        elif ctx.state.unread_messages > 0:
            # 用户说了话，它这一轮没回。这不是错误，是要记账的事实。
            usage = record_no_reply(usage, config=self.budget, at=ctx.virtual_now)
        return usage

    @staticmethod
    def _describe(usage: Any) -> str:
        if usage is None:
            return "记账跳过（没有预算记录）"
        return (
            f"主动消息 {usage.messages_sent} 条 / 动态 {usage.posts_sent} 条，"
            f"拦下 {usage.messages_suppressed + usage.posts_suppressed} 条"
        )


def _get(item: Any, key: str) -> Any:
    """``suppressed`` 里既可能是字典也可能是有属性的对象，两种都读。"""
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _as_datetime(value: Any) -> Any:
    return value if hasattr(value, "year") else None

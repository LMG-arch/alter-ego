"""Express 阶段：让它开口。

``order = 90``，依赖行动——先做了，才有话可说。

**只有对外意图才调模型。** 内部意图（工作、休息、心里过一遍）在这一阶段
产出的是一句内心独白，那是拼出来的而不是问出来的：一天 288 个 tick，
每轮都问一次模型既贵，也会让「它今天在想什么」这条时间线变得不可复现。

三种对外意图各有一套模板。**每个模板的占位符必须一个不多一个不少**——
``PromptLibrary.render`` 在多余或缺失时都会直接抛错，而这是故意的：
模板里加了新占位符而调用方忘了填，「静默地用空串顶替」会让它说出
「我是（空的），（空的）」这种话。所以下面三个 ``_values_*`` 方法各自
把该填的填全，而不是共用一个「大而全」的字典再去掉多余的键。

**回多快由 ``domain.conversation`` 决定，不是由这里决定。** 累到不想说话、
正在开会的日程块、连着秒回太多条——这些规则住在领域层，因为它们要能单独
被测。这里只负责把算出来的延迟接到消息的落库时间上。

依据: docs/design/04-simulation-loop.md § 3.5、
docs/design/12-calendar-and-conversation.md § 10
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from alterego.domain.conversation import ReplyDecision, decide_reply
from alterego.domain.emotion import Emotion
from alterego.interfaces.simulation import Intent, StageResult
from alterego.llm.prompts import PromptLibrary
from alterego.sim.context import TickContext
from alterego.sim.intents import (
    REACH_OUT_MOTIVATIONS,
    IntentContext,
    choose_reach_out_motivation,
)
from alterego.sim.stages.common import clock_text, describe_block, weekday_text
from alterego.sim.stages.reflect import baseline_emotion


__all__ = ["ExpressStage", "ExpressionStyle", "current_emotion"]


#: 表达走的是 ``[llm.routing]`` 里的哪一个用途。
#:
#: **是 ``expression`` 而不是 ``chat_reply``。** 后者是提示词**模板**的名字
#: （``prompts/chat_reply.md``），前者是路由表里的**用途**键。三种模板
#: （回话 / 发动态 / 主动找人）共用这一个用途：对它们的要求是同一件事。
PURPOSE: Final[str] = "expression"


#: 人设里没写「语气」的时候，这几条就是它的语气。它们最终会被 ``persona_json``
#: 里真正的表达设定取代（TODO(阶段 D)）——那时候改 ``ExpressionStyle`` 就行。
_DEFAULT_STYLE: dict[str, str] = {
    "tone": "随意、自然，不用敬语，不用客服腔",
    "verbosity": "一两句话，短的时候五六个字也行",
    "emoji_habit": "几乎不用表情符号，偶尔一个",
    "catchphrases": "没有固定的口头禅",
    "typing_quirks": "句末常常不打句号",
}


@dataclass(frozen=True, slots=True)
class ExpressionStyle:
    """它说话的样子。五个字段与 ``chat_reply.md`` 的表达类占位符一一对应。

    **默认值不是随便写的。** 「随意、不用敬语、不用客服腔」这一串是刻意的：
    没有它，模型会默认写出一封得体的商务邮件，而这个项目要的不是客服。
    """

    tone: str = _DEFAULT_STYLE["tone"]
    verbosity: str = _DEFAULT_STYLE["verbosity"]
    emoji_habit: str = _DEFAULT_STYLE["emoji_habit"]
    catchphrases: str = _DEFAULT_STYLE["catchphrases"]
    typing_quirks: str = _DEFAULT_STYLE["typing_quirks"]

    @classmethod
    def from_persona(cls, persona: object) -> ExpressionStyle:
        """从人设里读表达设定，缺什么用什么。

        ``persona_json`` 里的字段名还没有定论，所以**只在真的读到非空字符串时**
        才覆盖默认值——猜不到就保持默认，而不是把一个 ``None`` 塞进提示词
        （那会让模型看到 ``tone: None``，然后开始即兴发挥）。
        """
        raw = getattr(persona, "expression", None)
        if not isinstance(raw, dict):
            return cls()
        found: dict[str, str] = {}
        for name in _DEFAULT_STYLE:
            text = raw.get(name)
            if isinstance(text, str) and text.strip():
                found[name] = text
        return cls(**found)


@dataclass(slots=True)
class ExpressStage:
    """生成文本与内心独白。

    ``prompts`` 是提示词库，``style`` 是语气。两者都从构造器进来，
    所以「换一套语气」不需要改代码，「换一套提示词」也不需要。
    """

    prompts: PromptLibrary
    style: ExpressionStyle = ExpressionStyle()
    name: str = "express"
    order: int = 90
    depends_on: tuple[str, ...] = ("act",)
    enabled: bool = True
    #: 预先定好的回话判定。``None`` 表示自己摇（tick 路径）。
    #:
    #: 会话路径（``sim/conversation.py``）先摇好再传进来，理由是**判定结果要拿在
    #: 调用方手里**：它要告诉用户「它为什么回这么慢」，而从 ``ctx`` 上反推
    #: （看延迟看原因）得到的是一份重建的判定，不是真正用的那份。
    #: 预定时调用方负责已经消耗过那一颗随机数。
    decision: ReplyDecision | None = None
    #: 追加到渲染好的提示词末尾的一段额外要求。空串表示不追加。
    #:
    #: 存在的理由是**模板的占位符是固定的**：``PromptLibrary.render`` 多一个少一个
    #: 占位符都会报错，而「刚才那句别再说」/「这次自己起个话题」这类要求
    #: 是逐次变化的、只有调用方知道。往渲染好的文本末尾加一段，
    #: 比给每个模板加一个 ``{extra}`` 占位符更不容易出错——后者会逼每个
    #: 不使用它的调用方都传一个空串。
    extra: str = ""

    async def run(self, ctx: TickContext) -> StageResult:
        intent = ctx.chosen_intent
        if intent is None:
            return StageResult(ok=True, changes={})

        if intent.name == "reply":
            return await self._reply(ctx, intent)
        if intent.name == "reach_out":
            return await self._reach_out(ctx, intent)
        if intent.name == "post_moment":
            return await self._post(ctx, intent)

        ctx.expressions.append(self._inner_voice(ctx, intent))
        ctx.note(f"心里过了一遍：{intent.reason or intent.type.description}")
        return StageResult(ok=True, changes={"expressions": list(ctx.expressions)})

    # ── 回话 ──

    async def _reply(self, ctx: TickContext, intent: Intent) -> StageResult:
        """回用户的消息。**先问「这一轮回不回」，再问「回什么」。**

        顺序不能反。先花一次模型调用写好一句漂亮话、再被规则判定成
        「累到不想说话」，那次调用就白花了——而规则本来就知道答案。
        """
        state = ctx.state
        decision = self.decision or decide_reply(
            now=ctx.virtual_now,
            block=state.current_block,
            emotion=current_emotion(ctx),
            text_length=len(state.last_inbound_text),
            consecutive_instant=state.consecutive_instant_replies,
            roll=ctx.rng.random(),
        )
        if not decision.responds:
            ctx.suppressed.append({"intent": intent.name, "reason": decision.reason})
            ctx.note(f"这一轮不回：{decision.reason}")
            return StageResult(ok=True, changes={})

        text = await self._generate(ctx, "chat_reply", self._values_chat(ctx))
        if text is None:
            return StageResult(ok=True, changes={})

        ctx.expressions.append(
            {
                "kind": "message",
                "intent": intent.name,
                "content": text,
                "motivation": "",
                "trigger_note": decision.reason,
                "deliver_at": ctx.virtual_now + decision.delay,
            }
        )
        ctx.note(f"回了一句（{decision.reason}，{decision.delay.total_seconds():.0f} 秒后送出）")
        return StageResult(ok=True, changes={"expressions": list(ctx.expressions)})

    # ── 主动开口 ──

    async def _reach_out(self, ctx: TickContext, intent: Intent) -> StageResult:
        """主动找用户说话。

        动机由 :func:`~alterego.sim.intents.choose_reach_out_motivation` 摇出来，
        落进 ``message.motivation``——「它为什么突然找我」这个问题必须能回答，
        而那句话就是答案。
        """
        motivation = choose_reach_out_motivation(ctx.rng, context=_intent_context(ctx))
        text = await self._generate(ctx, "reach_out", self._values_reach(ctx, motivation))
        if text is None:
            return StageResult(ok=True, changes={})

        ctx.expressions.append(
            {
                "kind": "message",
                "intent": intent.name,
                "content": text,
                "motivation": motivation,
                "trigger_note": intent.reason,
                "deliver_at": ctx.virtual_now,
            }
        )
        ctx.note(f"主动开口（{REACH_OUT_MOTIVATIONS[motivation]}）：{_preview(text)}")
        return StageResult(ok=True, changes={"expressions": list(ctx.expressions)})

    async def _post(self, ctx: TickContext, intent: Intent) -> StageResult:
        """发一条动态。"""
        text = await self._generate(ctx, "post_compose", self._values_post(ctx, intent))
        if text is None:
            return StageResult(ok=True, changes={})

        ctx.expressions.append(
            {
                "kind": "post",
                "intent": intent.name,
                "content": text,
                "motivation": "",
                "trigger_note": intent.reason,
                "deliver_at": ctx.virtual_now,
            }
        )
        ctx.note(f"发了条动态：{_preview(text)}")
        return StageResult(ok=True, changes={"expressions": list(ctx.expressions)})

    # ── 生成 ──

    async def _generate(
        self,
        ctx: TickContext,
        template: str,
        values: dict[str, str],
    ) -> str | None:
        """渲染模板并问模型，返回 ``None`` 表示这次没生成出东西。

        所有失败都降级成 ``None`` 加一条 ``note``：模板缺占位符、模型超时、
        返回空串——这三种情况都不该让整轮推演白跑，但它们必须留下痕迹，
        否则「它今天一句话都没说」就成了一段没有原因的空白。
        """
        if ctx.llm_gateway is None:
            ctx.note(f"{template}：没有装配模型，写不出内容")
            return None
        try:
            prompt = self._render(template, values)
            text = (await ctx.llm(PURPOSE, prompt)).strip()
        except Exception as exc:
            ctx.note(f"{template} 没生成出来：{type(exc).__name__}: {exc}")
            return None
        if not text:
            ctx.note(f"{template}：模型返回了空内容")
            return None
        return text

    def _render(self, template: str, values: dict[str, str]) -> str:
        """渲染模板，接上 ``extra``。"""
        prompt = self.prompts.render(template, **values)
        return f"{prompt}\n\n{self.extra}" if self.extra else prompt

    def chat_prompt(self, ctx: TickContext) -> str:
        """这一轮回话真正会发出去的提示词（含 ``extra``）。

        ``alterego chat --show-prompt`` 用它来回答「它到底看到了什么」。
        提示词是这个项目里最难 debug 的东西，而它恰恰是全部行为的来源。
        重渲染而不是缓存一份：渲染是纯函数，两次结果必然一样，
        而缓存会让「调试时看到的那份」与「真正用的那份」有机会不一致。
        """
        return self._render("chat_reply", self._values_chat(ctx))

    # ── 三种模板各自要填的占位符 ──

    def _values_chat(self, ctx: TickContext) -> dict[str, str]:
        """``chat_reply.md`` 的十五个占位符。"""
        emotion = current_emotion(ctx)
        return {
            "persona": _persona_text(ctx),
            "user_name": str(getattr(ctx.state.persona, "user_name", "") or "你"),
            "virtual_now": clock_text(ctx.virtual_now),
            "emotion_label": emotion.label,
            "valence": f"{emotion.valence:+.2f}",
            "fatigue": f"{emotion.fatigue:.2f}",
            "current_block": describe_block(ctx.state.current_block),
            "since_last_talk": _since_last_talk(ctx),
            "memories": _memories(ctx),
            "conversation": _transcript(ctx),
            "tone": self.style.tone,
            "verbosity": self.style.verbosity,
            "emoji_habit": self.style.emoji_habit,
            "catchphrases": self.style.catchphrases,
            "typing_quirks": self.style.typing_quirks,
        }

    def _values_reach(self, ctx: TickContext, motivation: str) -> dict[str, str]:
        """``reach_out.md`` 的十四个占位符。"""
        return {
            "persona": _persona_text(ctx),
            "user_name": str(getattr(ctx.state.persona, "user_name", "") or "你"),
            "virtual_now": clock_text(ctx.virtual_now),
            "weekday": weekday_text(ctx.virtual_now),
            "emotion_label": current_emotion(ctx).label,
            "current_block": describe_block(ctx.state.current_block),
            "reason": REACH_OUT_MOTIVATIONS[motivation],
            "since_last_contact": _since_last_talk(ctx),
            "recent_conversation": _transcript(ctx),
            "memories": _memories(ctx),
            "messages_sent": str(int(getattr(ctx.state.budget, "messages_sent", 0))),
            "target_length": "80 字以内",
            "tone": self.style.tone,
            "emoji_habit": self.style.emoji_habit,
        }

    def _values_post(self, ctx: TickContext, intent: Intent) -> dict[str, str]:
        """``post_compose.md`` 的十一个占位符。"""
        emotion = current_emotion(ctx)
        return {
            "persona": _persona_text(ctx),
            "virtual_now": clock_text(ctx.virtual_now),
            "weekday": weekday_text(ctx.virtual_now),
            "emotion_label": emotion.label,
            "valence": f"{emotion.valence:+.2f}",
            "current_block": describe_block(ctx.state.current_block),
            "trigger": intent.reason or intent.type.description,
            "recent_memories": _memories(ctx),
            "target_length": "120 字以内",
            "tone": self.style.tone,
            "emoji_habit": self.style.emoji_habit,
        }

    # ── 内部意图的独白 ──

    @staticmethod
    def _inner_voice(ctx: TickContext, intent: Intent) -> dict[str, object]:
        """内部意图的独白。

        **拼出来的一句而不是问出来的一句**：内部动作一天发生几十次，
        每次都问模型的话，「它今天心里想过什么」这条时间线每次跑都不一样，
        而它本来应该是可以复现的（P6）。
        """
        tail = intent.reason or intent.type.description
        return {
            "kind": "inner_voice",
            "intent": intent.name,
            "content": f"{clock_text(ctx.virtual_now)}，{intent.type.description}——{tail}。",
            "motivation": "",
            "trigger_note": intent.reason,
            "deliver_at": ctx.virtual_now,
        }


# ── 共用的取值 ───────────────────────────────────────────────


def current_emotion(ctx: TickContext) -> Emotion:
    """当前情绪。**没有就现算一个基准值**，而不是让它当 ``None`` 一路传下去。

    公开而不是私有：会话路径（``sim/conversation.py``）也要拿这份情绪去问
    「要不要开个话题」，而两处各算一次会算出两个不同起点。
    """
    return ctx.state.emotion or baseline_emotion(ctx.virtual_now)


def _intent_context(ctx: TickContext) -> IntentContext:
    """``choose_reach_out_motivation`` 要的那点世界。"""
    emotion = current_emotion(ctx)
    minutes = None if ctx.percepts is None else ctx.percepts.minutes_since_last_user_message
    return IntentContext(
        unread_messages=int(ctx.state.unread_messages),
        hours_since_last_user_reply=None if minutes is None else minutes / 60.0,
        hour=ctx.virtual_now.hour,
        valence=float(emotion.valence),
        fatigue=float(emotion.fatigue),
        idle_minutes=float(0.0 if ctx.percepts is None else ctx.percepts.idle_minutes),
    )


def _preview(text: str) -> str:
    """给 ``note`` 用的短预览。"""
    return text if len(text) <= 40 else text[:40] + "…"


def _persona_text(ctx: TickContext) -> str:
    """把人设说成一小段话。没有就直说没有，不编。"""
    persona = ctx.state.persona
    text = getattr(persona, "summary", None) or getattr(persona, "description", None)
    return str(text) if text else "（人设还没生成，就当是一个普通的年轻人）"


def _transcript(ctx: TickContext) -> str:
    """把最近的对话排成一段文字。空对话时给一句明确的话。"""
    lines = [
        f"{'我' if str(getattr(message, 'direction', '')) == 'outbound' else '对方'}："
        f"{getattr(message, 'content', '')}"
        for message in ctx.state.recent_messages
    ]
    return "\n".join(lines) if lines else "（还没有聊过）"


def _memories(ctx: TickContext) -> str:
    """最近想起的几件事。取摘要而不是全文。"""
    summaries = [
        str(getattr(memory, "summary", "")).strip() for memory in ctx.state.recent_memories
    ]
    picked = [text for text in summaries if text]
    return "\n".join(f"- {text}" for text in picked) if picked else "（想不起来什么特别的）"


def _since_last_talk(ctx: TickContext) -> str:
    """距上次说话多久。用 ``percepts`` 里已经算好的值，不再算一遍。"""
    minutes = None if ctx.percepts is None else ctx.percepts.minutes_since_last_user_message
    if minutes is None:
        return "还没有聊过"
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes:.0f} 分钟前"
    return f"{minutes / 60:.1f} 小时前"

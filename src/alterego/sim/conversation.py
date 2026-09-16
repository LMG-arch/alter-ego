"""会话服务：用户说了一句话，它怎么回。

**为什么不走 tick。** ``04-simulation-loop.md`` § 3.3 说 ``reply`` 意图在有未读
消息时权重 1.0——那是**主动行为的调度**：一个 tick 看一次它想不想回。
而用户在 Web 上打完字是在等回复的，等不来一个 tick（``realtime`` 模式下
一个 tick 是 5 虚拟分钟）。两条路径**共用同一套纯函数**：
:func:`~alterego.domain.conversation.decide_reply` 决定即时 / 正常 / 延迟，
:class:`~alterego.sim.stages.express.ExpressStage` 组提示词、调模型，
:func:`~alterego.sim.engine.read_snapshot` 读同一份世界。区别只在**谁先开口**：
tick 决定「它想不想主动找你」，这里决定「你说了话它怎么回」。

**这一层不睡觉。** :func:`~alterego.domain.conversation.decide_reply` 算出来的
延迟会落进消息的 ``created_at``、会显示给用户、会写进 ``trigger_note``，
但这里绝不 ``asyncio.sleep``。三个理由：真睡着会让测试从毫秒变成分钟；
Web 页面会看起来像卡住了；而延迟对用户的意义是「它现在没空，等一下回」，
不是「浏览器在转圈」。真正的等待是渠道的事。

**顺序为什么是这样。** 先落库入站消息，再读快照，再问「回不回」，最后才调模型。
入站先落库意味着**回复失败也丢不了用户说的话**——用户打的那句话是这套系统里
最不该丢的东西，它是唯一无法重新生成的东西。而「先问回不回」意味着
被规则拦下时一次模型调用都不会花（P3）。

依据: docs/design/12-calendar-and-conversation.md § 10–12，
      docs/plans/2026-09-16-main-body.md § 4（批次 A）
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from random import Random
from typing import Any

from alterego.domain.conversation import (
    ReplyDecision,
    TopicDecision,
    decide_reply,
    find_repetition,
    should_open_topic,
)
from alterego.interfaces.repository import ConversationRecord, MessageRecord
from alterego.interfaces.simulation import Intent
from alterego.kernel.errors import SimulationError
from alterego.llm.prompts import PromptLibrary
from alterego.sim.context import StateSnapshot, TickContext
from alterego.sim.engine import (
    DEFAULT_HISTORY_LIMIT,
    USER_COUNTERPART_ID,
    EnginePorts,
    read_snapshot,
    user_conversation_id,
)
from alterego.sim.intents import IntentCatalog
from alterego.sim.persona_view import PersonaView
from alterego.sim.stages.express import ExpressionStyle, ExpressStage, current_emotion
from alterego.sim.stages.persist import PersistStage
from alterego.sim.stages.sense import SenseStage


__all__ = ["ConversationService", "InboundMessage", "ReplyOutcome"]


#: 复读检查往回看几条。五条足够了：口头禅的周期比这短得多，
#: 而放宽到全部历史会让「上个月说过类似的」也算复读，那反而逼它每次换腔调。
REPETITION_LOOKBACK: int = 5


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """用户发进来的一条消息。

    ``at`` 为 ``None`` 表示「就是现在」——渠道能给出真实到达时间时应当填上，
    那是「它到底多快回的」的原始数据。``message_id`` 同样由渠道给：
    真实 IM 平台都有自己的消息 id，用它才能做到**重投递不产生第二行**
    （``ConversationRepository.append`` 是幂等的）。
    """

    content: str
    sender_id: str = USER_COUNTERPART_ID
    message_id: str = ""
    channel: str = ""
    at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReplyOutcome:
    """一次回复的结果。**成功与「这一轮不回」共用这一个形状。**

    ``reply is None`` 不是错误，是 :func:`~alterego.domain.conversation.decide_reply`
    判了 ``silent``（累到不想说话）。调用方拿到 ``reply is None`` 时该看
    ``decision.reason``，而不是去猜哪里抛了异常——「它今天为什么不回我」
    这个问题必须有一个文件里写得下的答案。
    """

    conversation_id: str
    decision: ReplyDecision
    reply: MessageRecord | None = None
    prompt: str = ""
    topic: TopicDecision | None = None
    repeated_from: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def replied(self) -> bool:
        """这一轮到底有没有吐出话来。"""
        return self.reply is not None

    @property
    def text(self) -> str:
        """回话正文；没回就是空串。"""
        return "" if self.reply is None else self.reply.content

    @property
    def delay_seconds(self) -> float:
        """这条消息打算多久之后送出。渠道拿它去决定什么时候真的发。"""
        return self.decision.delay.total_seconds()


class ConversationService:
    """「你说了话它怎么回」这条路径的组装点。

    它**不是一个阶段**：阶段活在 tick 里、共享 ``ctx``、由引擎统一提交，
    而会话是用户按下回车就发生的事。把它做成阶段会逼出两种糟糕的选择——
    要么为每条消息跑一整轮 tick（连同它并不想做的动作），
    要么给阶段开一个「这次只跑 express」的后门。
    """

    __slots__ = (
        "_budget",
        "_clock",
        "_conversation_id",
        "_gateway",
        "_history_limit",
        "_new_id",
        "_persona",
        "_ports",
        "_prompts",
        "_random",
        "_reply_type",
        "_sense",
        "_style",
    )

    def __init__(
        self,
        *,
        persona: PersonaView,
        ports: EnginePorts,
        clock: Any,
        prompts: PromptLibrary,
        budget: Any,
        gateway: Any = None,
        style: ExpressionStyle | None = None,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        conversation_id: str = "",
        random: Random | None = None,
        new_id: Any = None,
    ) -> None:
        self._persona = persona
        self._ports = ports
        self._clock = clock
        self._prompts = prompts
        self._budget = budget
        self._gateway = gateway
        self._style = ExpressionStyle() if style is None else style
        self._history_limit = max(1, int(history_limit))
        # 会话 id 的规则只在 ``user_conversation_id`` 里写一次：
        # 两条路径各自拼一次的话，一旦拼法不一致，消息会写进一个没人读的会话里，
        # 而症状是「它不记得刚才聊过什么」。
        self._conversation_id = conversation_id or user_conversation_id(persona.id)
        self._random = random
        self._new_id = new_id
        # ``SenseStage`` 是纯的、无状态的，构造一次就够。
        self._sense = SenseStage()
        reply_type = IntentCatalog().get("reply")
        if reply_type is None:  # pragma: no cover - 内置目录里一定有 reply
            raise SimulationError("意图目录里没有 reply", hint="内置意图表被改坏了")
        self._reply_type = reply_type

    @property
    def conversation_id(self) -> str:
        """用户会话的 id（按组装规则算出来的那个，不保证库里已经存在）。"""
        return self._conversation_id

    # ── 主流程 ──────────────────────────────────────────────

    async def reply(self, inbound: InboundMessage) -> ReplyOutcome:
        """收下用户这一句，回一句（或者不回）。

        **本方法不抛业务异常。** 会话路径上没有「这一轮失败了」这个概念：
        用户的消息已经落库了，模型写不出来只是「这一次没说成」，
        与「它决定不说」对用户是同一件事。模型故障会以一条 ``note``
        出现在 ``ReplyOutcome.notes`` 里。
        """
        now = inbound.at or self._clock.virtual_now()
        tick_id = self._id(now)
        conversation = self._accept(inbound, now=now, tick_id=tick_id)

        state = read_snapshot(
            self._ports,
            persona=self._persona,
            conversation_id=conversation.id,
            virtual_now=now,
            history_limit=self._history_limit,
        )
        ctx = self._context(state, now=now, tick_id=tick_id)
        await self._perceive(ctx)

        # 先摇话题、再摇回话模式。**顺序固定**是刻意的：``decide_reply`` 的短路
        # 分支根本不消耗随机数，若把顺序交给调用顺序决定，同一颗种子重放的
        # 就不再是同一段对话（P6）。
        topic = self._topic(ctx)
        decision = self._decide(ctx)
        prompt, text = await self._say(ctx, topic=topic, decision=decision, repeated=None)

        repeated = self._repeated(text, state)
        if repeated is not None:
            prompt, text = await self._say(ctx, topic=topic, decision=decision, repeated=repeated)

        record = await self._commit(ctx, topic=topic, repeated=repeated)
        return ReplyOutcome(
            conversation_id=conversation.id,
            decision=decision,
            reply=record,
            prompt=prompt,
            topic=topic,
            repeated_from=repeated,
            notes=tuple(ctx.notes),
        )

    # ── 收下用户的话 ────────────────────────────────────────

    def _accept(
        self, inbound: InboundMessage, *, now: datetime, tick_id: str
    ) -> ConversationRecord:
        """入站消息先落库，单独一个事务。

        **单独一个事务**而不是跟回复合在一起：用户打的那句话是这套系统里
        唯一无法重新生成的东西，后面无论出什么事，它都必须已经躺在库里了。
        """
        persona_id = self._persona.id
        with self._ports.backend.transaction():
            conversation = self._ports.conversations.ensure(
                ConversationRecord(
                    id=self._conversation_id,
                    persona_id=persona_id,
                    counterpart_id=USER_COUNTERPART_ID,
                    counterpart_kind="user",
                    created_at=now,
                )
            )
            self._ports.conversations.append(
                MessageRecord(
                    id=inbound.message_id or f"msg-{persona_id}-{now:%Y%m%dT%H%M%S}",
                    conversation_id=conversation.id,
                    direction="inbound",
                    sender_id=inbound.sender_id,
                    content=inbound.content,
                    delivered_channels=(inbound.channel,) if inbound.channel else (),
                    created_at=now,
                    tick_id=tick_id,
                )
            )
        return conversation

    # ── 推演工作区 ──────────────────────────────────────────

    def _context(self, state: StateSnapshot, *, now: datetime, tick_id: str) -> TickContext:
        """搭一个只装「回这一句」的推演工作区。

        复用 :class:`~alterego.sim.context.TickContext` 而不是另起一个类型：
        回话用到的表达逻辑（语气、提示词模板、失败降级）全在 ``ExpressStage``
        里，而它要的就是一个 ``ctx``。另起一套类型等于把那些逻辑抄第二遍。
        """
        ctx = TickContext(
            tick_id=tick_id,
            virtual_now=now,
            correlation_id=f"{tick_id}-chat",
            state=state,
            rng=self._rng(tick_id),
            llm_gateway=self._gateway,
        )
        ctx.chosen_intent = Intent(
            type=self._reply_type,
            parameters={},
            urgency=1.0,
            reason="用户在等回话",
        )
        return ctx

    async def _perceive(self, ctx: TickContext) -> None:
        """把感知补上。

        ``chat_reply`` 里的 ``{since_last_talk}`` 要用它。**复用 ``SenseStage``**
        而不是在这里再算一遍「距上次说话多久」——那份换算（``minutes_between``）
        已经在 Sense 里了，抄第二遍就会出现两条路径算出不同数字的情况。
        阶段不改 ``ctx``，所以这里手动应用它的 ``changes``，与引擎里的做法一致。
        """
        result = await self._sense.run(ctx)
        ctx.percepts = result.changes["percepts"]

    # ── 判定 ────────────────────────────────────────────────

    def _topic(self, ctx: TickContext) -> TopicDecision:
        """这一句回话要不要顺带开个新话题。"""
        state = ctx.state
        minutes = None
        if state.last_topic_at is not None:
            minutes = max(int((ctx.virtual_now - state.last_topic_at).total_seconds() // 60), 0)
        return should_open_topic(
            block=state.current_block,
            emotion=current_emotion(ctx),
            consecutive_passive_turns=state.consecutive_passive_turns,
            minutes_since_last_topic=minutes,
            roll=ctx.rng.random(),
        )

    def _decide(self, ctx: TickContext) -> ReplyDecision:
        """这一轮回不回、回多快。

        **在会话这一层摇、把结果传进阶段**，而不是让 ``ExpressStage`` 自己摇：
        判定结果要拿在调用方手里——它要回答「它为什么回这么慢」，
        而从 ``ctx`` 上反推（看延迟、看原因）得到的是一份重建的判定，
        不是真正决定行为的那一份。
        """
        state = ctx.state
        return decide_reply(
            now=ctx.virtual_now,
            block=state.current_block,
            emotion=current_emotion(ctx),
            text_length=len(state.last_inbound_text),
            consecutive_instant=state.consecutive_instant_replies,
            roll=ctx.rng.random(),
        )

    # ── 回话 ────────────────────────────────────────────────

    async def _say(
        self,
        ctx: TickContext,
        *,
        topic: TopicDecision,
        decision: ReplyDecision,
        repeated: str | None,
    ) -> tuple[str, str]:
        """组提示词、问模型，返回 ``(提示词, 正文)``。正文为空串表示没说成。

        ``repeated`` 非空表示这是**重写那一遍**：把「你刚说过这句」追加到提示词末尾。
        ``chat_reply.md`` 的占位符是固定的（多一个少一个 ``PromptLibrary`` 都会报错），
        所以这类逐次变化的要求走 ``ExpressStage.extra`` 追加，而不是新开占位符——
        后者会逼每个不使用它的调用方都传一个空串。
        """
        stage = ExpressStage(
            prompts=self._prompts,
            style=self._style,
            decision=decision,
            extra=_instructions(topic, repeated),
        )
        if repeated is not None:
            # 重写那一遍要把上一次的产出丢掉，否则两条都会落库。
            ctx.expressions.clear()
            ctx.note(f"跟上一条太像了，换一种说法重写（原话：{repeated}）")
        prompt = stage.chat_prompt(ctx)
        await stage.run(ctx)
        return prompt, _text_of(ctx)

    def _repeated(self, text: str, state: StateSnapshot) -> str | None:
        """这一句跟在它最近说过的哪一句太像了？

        比的是**它自己说过的**话，不是用户说过的话：复读的定义是
        「它又把同一句拿出来了」，跟用户重复问同一件事不是一回事。
        """
        if not text:
            return None
        recent = [
            message.content
            for message in reversed(state.recent_messages)
            if message.direction == "outbound" and message.content
        ][:REPETITION_LOOKBACK]
        return find_repetition(text, recent)

    # ── 落库 ────────────────────────────────────────────────

    async def _commit(
        self,
        ctx: TickContext,
        *,
        topic: TopicDecision,
        repeated: str | None,
    ) -> MessageRecord | None:
        """把这一轮翻译成行并写进库。返回出站消息（没回就是 ``None``）。

        翻译交给 :class:`~alterego.sim.stages.persist.PersistStage`：
        ``deliver_at`` → ``created_at``、独白归并、预算记账这套换算
        与 tick 路径必须**逐字相同**，否则「同一句话在两条路径上记的账不一样」。
        """
        result = await PersistStage(budget=self._budget).run(ctx)
        messages: tuple[MessageRecord, ...] = result.changes["message_records"]
        activities: tuple[Any, ...] = result.changes["activity_records"]
        usage = result.changes["state"].budget

        record = self._mark_initiative(messages[0], topic) if messages else None
        with self._ports.backend.transaction():
            if record is not None:
                self._ports.conversations.append(record)
            if activities:
                # 被拦下的那些也要留一行：用户问「它明明想回怎么没回」，
                # 答案必须是一条查得到的记录，而不是一片空白。
                self._ports.activities.append(list(activities))
            if usage is not None:
                self._ports.budgets.save(self._persona.id, usage)
        if repeated is not None:
            ctx.note("重写完这句")
        return record

    @staticmethod
    def _mark_initiative(record: MessageRecord, topic: TopicDecision) -> MessageRecord:
        """把「它是自己起的这个话题」记在 ``initiative`` 上。

        ``PersistStage`` 只把 ``reach_out`` 当主动消息，而回话时自己抛话题
        同样是主动的——:func:`~alterego.sim.transcript.passive_streak` 往后数
        「连着被动接了几轮」靠的就是这一列。不记的话它每一轮都会被算成被动，
        然后连着几轮之后不停地想开新话题，而「想开」和「开过」是两件事。
        """
        if not topic.open_topic:
            return record
        return replace(record, initiative=True, motivation=topic.reason)

    # ── 小工具 ──────────────────────────────────────────────

    def _rng(self, tick_id: str) -> Random:
        """这一句回复的随机源。

        默认种子挂在 **tick id** 上而不是虚拟时刻上：同一虚拟秒里的两条消息
        用同一颗种子会让它们得到同一个模式（两条都秒回，或者两条都放一放），
        那是随机数复用，不是可复现（P6）。

        ``random`` 由组装根注入且**只有这一个入口**。存在的理由是
        :func:`~alterego.domain.conversation.should_open_topic` 的最后一步是
        「摇一个数落进哪个区间」：随机源不可注入，就没办法写「这一轮它会开话题」
        这条断言，只能靠撞概率——而撞概率的测试是一天绿一天红的那种。
        """
        if self._random is not None:
            return self._random
        return Random(f"{self._persona.id}|{tick_id}")

    def _id(self, now: datetime) -> str:
        """这一轮会话的 id。``new_id`` 由组装根注入，规则与引擎一致。"""
        if self._new_id is not None:
            return str(self._new_id())
        return f"chat-{self._persona.id}-{now:%Y%m%dT%H%M%S}"


# ── 提示词追加要求 ──────────────────────────────────────────


def _instructions(topic: TopicDecision, repeated: str | None) -> str:
    """追加到 ``chat_reply`` 提示词末尾的那几行。

    两件事都属于「这一次」而不是「这是一次回话」：要不要起话题由
    :func:`~alterego.domain.conversation.should_open_topic` 摇出来，
    要不要换说法由 :func:`~alterego.domain.conversation.find_repetition` 判出来。
    模板里加占位符表达不了这种逐次变化——那样每个调用方都得为它传一个空串。
    """
    lines: list[str] = []
    if topic.open_topic:
        lines.append(
            "这次别只顺着答。回完这一句之后，自己抛一个新话题出来，"
            "像真的想起一件事那样自然，不要写成「我们聊点别的吧」。"
        )
    if repeated:
        lines.append(f"你上一轮已经说过「{repeated}」，换一种说法，别又是这一句。")
    return "\n".join(lines)


def _text_of(ctx: TickContext) -> str:
    """这一轮生成出来的消息正文。没有就是空串。"""
    for expression in ctx.expressions:
        if expression.get("kind") == "message":
            return str(expression.get("content", ""))
    return ""

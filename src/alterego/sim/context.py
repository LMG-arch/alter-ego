"""单次 tick 的工作区：阶段之间传递数据的唯一载体。

``TickContext`` 是「推演引擎」与「插件阶段」之间**唯一的**交互面。所有跨阶段的数据都放在
这里，而不是塞进全局变量或内核单例——这样一次 tick 的完整推理链可以原样写进 ``tick_log``，
``alterego why`` 才有东西可讲。

两个刻意的约束：

* **``state`` 在 tick 内视为不变。** 阶段要改世界，就把改动写进 ``StageResult.changes``，
  由引擎在 tick 末尾一次性合并、一次性持久化。这保证「一个 tick 要么整体成立、要么整体回退」。
* **``rng`` 是注入的。** 阶段里禁止用全局 ``random``：同一颗种子必须能重放同一天
  （设计原则 P6）。

**``llm()`` 为什么不直接返回网关。** 阶段里若写 ``ctx.registry.get(...)``
再自己调 ``complete``，那么「这一次 tick 一共花了几次模型调用、多少 token」
就得靠每个阶段自觉上报。写成 :meth:`TickContext.llm` 之后，调用次数与用量
是方法内部顺手记下来的，``tick_log.llm_calls`` / ``llm_tokens`` 不可能漏。

``persona`` / ``relationships`` / ``world`` 仍标注为 ``Any``（留着 ``TODO(阶段 D)``）：
它们的领域模型还没落地。等落地后逐个收紧，接口不变、只变注解。

依据: docs/design/04-simulation-loop.md § 2.2
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from random import Random
from typing import TYPE_CHECKING, Any

from alterego.interfaces.simulation import Percepts
from alterego.kernel.errors import SimulationError
from alterego.sim.budget import BudgetUsage


if TYPE_CHECKING:  # pragma: no cover - 只为类型检查器存在
    from alterego.domain.emotion import Emotion
    from alterego.domain.memory import Memory
    from alterego.domain.schedule import ScheduleBlock
    from alterego.interfaces.repository import MessageRecord


__all__ = ["StateSnapshot", "TickContext"]


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """一次 tick 开始时的世界快照，tick 内视为只读。

    阶段不能就地改它——想改就调 :meth:`evolve` 产出一份新的，交给引擎合并。
    这样「谁在什么时候把情绪改成了什么」永远可追溯。
    """

    persona: Any  # TODO(阶段 D): domain.persona.Persona
    emotion: Emotion | None = None
    relationships: Mapping[str, Any] = field(default_factory=dict)  # TODO(阶段 D)
    current_block: ScheduleBlock | None = None
    recent_memories: tuple[Memory, ...] = ()
    today_activity: tuple[Any, ...] = ()  # TODO(阶段 D): tuple[ActivityRecord, ...]
    budget: BudgetUsage | None = None
    world: Any = None  # TODO(阶段 D): domain.world.World
    #: 用户发过来、它还没回的消息条数。0 表示没有人在等它。
    unread_messages: int = 0
    #: 用户最后一次说话的时间（**虚拟时间**）。
    #: ``None`` 表示从没说过话——这与「很久以前说过」是不同的起点，
    #: 混为一谈会让新装好的它在第一天就摆出一副被冷落的样子。
    last_user_message_at: datetime | None = None
    #: 最近的对话消息（含本次入站），按时间升序。用来渲染提示词里的 ``{conversation}``。
    #: **由组装根读好塞进来**：阶段是纯的，读库不该发生在阶段里。
    recent_messages: tuple[MessageRecord, ...] = ()
    #: 用户最近一条消息的正文。回话长度决定它回多快（``domain.conversation``）。
    last_inbound_text: str = ""
    #: 前面已经连着秒回了几条。超过阈值后这一条会故意放一放。
    consecutive_instant_replies: int = 0
    #: 前面连着被动接了几轮（它只顺着对方说，没有自己起话题）。
    #: 连着几轮之后该它开一个新话题了，见 ``domain.conversation.should_open_topic``。
    consecutive_passive_turns: int = 0
    #: 它上一次**主动起话题**的时间（虚拟时间）。``None`` 表示从来没起过——
    #: 这与「很久以前起过」是两回事：前者没有冷却可言。
    last_topic_at: datetime | None = None

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
    percepts: Percepts | None = None
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
    #: ← Persist 写入。要落 ``activity_log`` 的行（:class:`~alterego.interfaces.repository.ActivityRecord`）。
    #: 阶段产出的**行对象**而不是「阶段自己开事务写库」：一个 tick 要么整体
    #: 成立、要么整体回退，所以写入必须由引擎统一在同一个事务里做。
    activity_records: list[Any] = field(default_factory=list)
    #: ← Persist 写入。要落 ``message`` 的行（:class:`~alterego.interfaces.repository.MessageRecord`）。
    message_records: list[Any] = field(default_factory=list)
    #: ← Persist 写入。要落 ``social_post`` 的行（:class:`~alterego.interfaces.repository.SocialPostRecord`）。
    #: **与消息并列而不是共用一份**：动态没有收件人，把它塞进 ``message``
    #: 会让「发给谁」这一列每次都要填一个假值。
    post_records: list[Any] = field(default_factory=list)
    #: ← 各阶段追加
    llm_calls: list[Any] = field(default_factory=list)
    #: ← 任意阶段追加。这些说明最终写进 ``tick_log``，是「可解释性」的原料
    notes: list[str] = field(default_factory=list)
    #: ← Intention 写入。想开话题但时机不对时存下来，等下一个不忙的 tick 再说。
    #: **绝不跨越 tick 存活**——引擎每个 tick 重建 context。要让一句话活过几个
    #: tick，得靠它落进对话或记忆，而不是靠一个内存里的列表。
    pending_topics: list[str] = field(default_factory=list)

    #: 本 tick 用的模型网关。由引擎注入；**故意不进 ``__init__`` 的位置参数**，
    #: 因为「一次不调模型的 tick」（``--dry-run``、纯测试）不该被迫造一个网关。
    llm_gateway: Any = field(default=None, repr=False)

    async def llm(
        self,
        purpose: str,
        prompt: str,
        *,
        tier: str | None = None,
        system: str | None = None,
        temperature: float = 0.8,
        max_tokens: int = 1024,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        """本 tick 内调一次模型，返回纯文本。

        三次副作用都发生在这里，而不是散在各个阶段：

        1. 调用被记进 :attr:`llm_calls`（``tick_log.llm_calls`` 由它汇总）；
        2. 返回的 :class:`~alterego.interfaces.llm.LLMResponse` 被原样留下，
           用量（token）可以从 ``response.prompt_tokens`` 读到，不必再猜；
        3. ``correlation_id`` 传给网关，于是「一次 tick 花了多少钱」在用量表里
           能按这条线串起来。

        ``purpose`` 必须是 ``[llm.routing]`` 里有的用途名——写错会当场报错，
        而不是悄悄走默认档（静默回落会让「我明明配了 cheap 怎么这么贵」变成玄学）。

        Raises:
            SimulationError: 这次推演没有装配模型网关。
        """
        if self.llm_gateway is None:
            raise SimulationError(
                "这次推演没有装配模型网关",
                hint="需要模型的行为要先在组装根建好 LLMGateway 再交给引擎。",
            )
        response = await self.llm_gateway.complete(
            purpose,
            prompt,
            tier=tier,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            json_schema=json_schema,
            response_format="json" if json_schema is not None else "text",
            metadata={"correlation_id": self.correlation_id, "tick_id": self.tick_id},
        )
        self.llm_calls.append(response)
        return str(response.text)

    @property
    def llm_tokens(self) -> int:
        """本 tick 用掉的 token 总数（提示 + 补全）。"""
        return sum(
            int(getattr(call, "prompt_tokens", 0)) + int(getattr(call, "completion_tokens", 0))
            for call in self.llm_calls
        )

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

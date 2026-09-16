"""推演引擎：一次 tick 从「读世界」到「落库」。

一次 tick 的顺序是固定的，而**每一段都只做一件事**：

1. **读快照**（:meth:`SimulationEngine._snapshot`）——把库里的东西读成
   一份 :class:`~alterego.sim.context.StateSnapshot`。这一步是引擎的活，
   不是阶段的活：阶段必须是纯的，而读库不是（P1 的推论）。
2. **插件前置钩子**（``on_tick_pre``）——``04-simulation-loop.md`` § 2.1 的
   20 / 25 号插入点就在这一段。
3. **六个阶段**（按 ``order``）——顺序由 ``depends_on`` 决定，不靠列表顺序。
4. **插件后置钩子**（``on_tick_post``）——40 / 60 / 80 号插入点。
5. **一个事务写下去**——行为、消息、动态、情绪、预算、推演日志。
   要么全成、要么全败：半条消息比没有消息更难解释。

**为什么插件阶段和内置阶段在同一个列表里。** 引擎只看 ``order``，不看是谁写的。
插件通过 ``ctx.registry.register(Stage, ...)`` 注册，组装根用
:func:`default_stages` 把两边拼起来——这样「在 sense 之后、reflect 之前加一件事」
对插件作者是一个 ``order = 20`` 的决定，而不是一次内核改动。

**状态怎么合并。** 每个阶段返回一份 ``StageResult.changes``，引擎负责应用它
（``state`` 换快照、``percepts`` / ``actions`` 等写回 context）。阶段不直接改
``ctx.state``——它是 frozen 的，想改只能 ``evolve()`` 出一份新的交上来。
于是「谁在什么时候把情绪改成了什么」永远可追溯。

依据: docs/design/04-simulation-loop.md § 2、§ 3，docs/DESIGN.md § 9
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timedelta
from random import Random
from typing import Any, Final, cast

from alterego.domain.schedule import (
    SCHEDULE_CATEGORIES,
    ScheduleBlock,
    ScheduleCategory,
    current_block,
)
from alterego.interfaces.repository import (
    ActivityRepository,
    BudgetRepository,
    BudgetUsage,
    ConversationRecord,
    ConversationRepository,
    EmotionRepository,
    MemoryRepository,
    MessageRecord,
    PersonaRepository,
    ScheduleRecord,
    ScheduleRepository,
    SocialPostRepository,
    TickLogDraft,
    TickLogRepository,
)
from alterego.interfaces.simulation import Stage, StageResult
from alterego.interfaces.storage import StorageBackend
from alterego.kernel.logging import get_logger
from alterego.kernel.registry import ServiceRegistry
from alterego.llm.prompts import PromptLibrary
from alterego.sim.context import StateSnapshot, TickContext
from alterego.sim.intents import IntentCatalog
from alterego.sim.persona_view import PersonaView
from alterego.sim.stages import (
    ActStage,
    ExpressStage,
    IntentionStage,
    PersistStage,
    ReflectStage,
    SenseStage,
)
from alterego.sim.transcript import instant_streak, last_inbound_text, last_topic_at, passive_streak


__all__ = [
    "EnginePorts",
    "SimulationEngine",
    "TickOutcome",
    "default_stages",
    "read_snapshot",
    "to_schedule_block",
    "user_conversation_id",
]


_log = get_logger("sim.engine")


#: 默认读多少条历史对话进快照。二十条大致是「今天这一段」——
#: 再多会把提示词撑长，而更早的事本来就该从记忆里来。
DEFAULT_HISTORY_LIMIT: Final[int] = 20

#: 默认读多少条记忆进快照。
DEFAULT_MEMORY_LIMIT: Final[int] = 12

#: 记忆的回看窗口（天）。
DEFAULT_MEMORY_DAYS: Final[int] = 30

#: 今日行为的回看窗口（小时）。
DEFAULT_ACTIVITY_HOURS: Final[int] = 24

#: 回多快算「秒回」。超过这个间隔的回话不参与「连着秒回」计数。
INSTANT_REPLY_SECONDS: Final[float] = 60.0

#: 用户那一侧的 ``counterpart_id``。
#:
#: 是一个固定字符串而不是用户名：用户名会改，而会话不该因为用户改了个名字
#: 就在库里多出一条。
USER_COUNTERPART_ID: Final[str] = "user"

#: 往注册表里查阶段时用的键。理由同 ``sim/stages/act.py`` 的 ``_CAPABILITY_KEY``：
#: ``ServiceRegistry.get*`` 的标注是 ``type[T]``，而 ``Stage`` 是 Protocol。
_STAGE_KEY: Final[type[Stage]] = Stage  # type: ignore[type-abstract]

#: ``StageResult.changes`` 的键 → ``TickContext`` 上的属性名。
#:
#: ``state`` 不走这张表（它是 ``evolve`` 出来的整份快照，要替换而不是追加）。
#: 表里沒有 ``candidates`` / ``suppressed`` / ``notes``：那些由阶段直接往
#: context 的列表里追加，因为它们的语义是「累积」而不是「产出」——
#: 让阶段用 ``changes`` 交一份完整列表，任何一次追加都得先 copy 一遍。
_CHANGE_TARGETS: Final[Mapping[str, str]] = {
    "percepts": "percepts",
    "chosen_intent": "chosen_intent",
    "actions": "actions",
    "expressions": "expressions",
    "activity_records": "activity_records",
    "message_records": "message_records",
    "post_records": "post_records",
}

#: 这次 tick 的结局。
TICK_OK: Final[str] = "ok"
TICK_PARTIAL: Final[str] = "partial"
TICK_FAILED: Final[str] = "failed"


def user_conversation_id(persona_id: str) -> str:
    """人与它之间那个会话的 id。

    **只在这里定义一次。** 会话 id 一旦有两处生成规则，两处只要有一处改过，
    消息就会被写进一个没人读的会话——而那种错误不会报任何异常，
    它只会让界面上的对话停在某一刻。
    """
    return f"{persona_id}:{USER_COUNTERPART_ID}"


@dataclass(frozen=True, slots=True)
class EnginePorts:
    """引擎碰数据的所有出口，一次装齐。

    九个参数如果逐个写进 ``__init__``，构造那一行会变得没法读，也没法在
    测试里一眼看出「这次到底少了哪个口」。装成一个对象之后，缺一个字段是
    ``TypeError``，而「传错了位置」这种更难查的错直接被字段名挡住了。

    **全是 Protocol，没有一个具体类型**——引擎不知道底下是 SQLite。
    ``backend`` 是唯一的例外，但它也只用来开事务（``StorageBackend`` 是接口）。
    """

    backend: StorageBackend
    personas: PersonaRepository
    emotions: EmotionRepository
    schedules: ScheduleRepository
    memories: MemoryRepository
    activities: ActivityRepository
    conversations: ConversationRepository
    budgets: BudgetRepository
    posts: SocialPostRepository
    tick_logs: TickLogRepository


@dataclass(frozen=True, slots=True)
class TickOutcome:
    """一轮推演的结果摘要。**给人和给 CLI 看的**，不是内部结构。

    ``status`` 只有三种值，且它们的区别是「有没有东西落库」：

    - ``ok``：所有阶段都跑完了；
    - ``partial``：有阶段失败，但 ``persist`` 跑完了——这一轮仍然成立，
      只是少了某些环节（比如模型超时导致没生成出话）；
    - ``failed``：``persist`` 没跑完或写库失败。这一轮**什么都没留下**，
      除了 ``tick_log`` 里那条记录它失败过的行。

    把 ``partial`` 和 ``failed`` 分开是有原因的：前者是日常（模型偶尔超时），
    后者是异常。合成一个值之后，用户看到「有 failed」就得去翻日志才知道
    要不要管。
    """

    tick_id: str
    virtual_now: datetime
    status: str
    chosen_intent: str | None = None
    reason: str = ""
    ran: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    llm_calls: int = 0
    llm_tokens: int = 0
    activities: int = 0
    messages: int = 0
    posts: int = 0

    @property
    def ok(self) -> bool:
        """这一轮是不是**完整地**成立了。``partial`` 不算。"""
        return self.status == TICK_OK


class SimulationEngine:
    """把六个阶段串成一圈。

    它不做业务判断——那些在 ``domain/`` 与 ``sim/stages/`` 里。它只做三件事：
    **读快照、按顺序跑阶段、把产出写进一个事务**。这条边界值得守住，
    因为引擎是唯一同时看得见「数据库」和「推演」的地方，一旦开始在这里写规则，
    那些规则就没有办法被单独测，也没有办法被重放。
    """

    __slots__ = (
        "_activity_hours",
        "_clock",
        "_conversation_id",
        "_gateway",
        "_history_limit",
        "_memory_days",
        "_memory_limit",
        "_new_id",
        "_persona",
        "_plugins",
        "_ports",
        "_seed",
        "_stages",
    )

    def __init__(
        self,
        *,
        persona: PersonaView,
        ports: EnginePorts,
        stages: Sequence[Stage],
        clock: Any,
        gateway: Any = None,
        plugins: Any = None,
        conversation_id: str = "",
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        memory_limit: int = DEFAULT_MEMORY_LIMIT,
        memory_days: int = DEFAULT_MEMORY_DAYS,
        activity_hours: int = DEFAULT_ACTIVITY_HOURS,
        seed: str = "",
        new_id: Any = None,
    ) -> None:
        """装一次引擎。

        ``gateway`` 与 ``plugins`` 都可以是 ``None``：前者代表「这一轮不调模型」
        （``--dry-run``、大部分测试），后者代表「没装插件系统」。两者都不是
        异常状态——一个不调模型的 tick 仍然是完整的 tick，它只是说不出话。

        ``seed`` 是**可复现的开关**：同一天同一个时刻配上同一个 seed，
        两次推演会选出同一件事。默认用 ``persona_id + 虚拟时间`` 派生，
        所以默认就是可复现的，而传一个固定的 seed 可以让测试完全钉死随机源。
        """
        self._persona = persona
        self._ports = ports
        self._stages = tuple(stages)
        self._clock = clock
        self._gateway = gateway
        self._plugins = plugins
        self._conversation_id = conversation_id or user_conversation_id(persona.id)
        self._history_limit = max(1, int(history_limit))
        self._memory_limit = max(1, int(memory_limit))
        self._memory_days = max(1, int(memory_days))
        self._activity_hours = max(1, int(activity_hours))
        self._seed = seed
        self._new_id = new_id

    # ── 对外 ────────────────────────────────────────────────

    @property
    def persona(self) -> PersonaView:
        return self._persona

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    @property
    def stages(self) -> tuple[Stage, ...]:
        """按 ``order`` 排好的阶段。插件阶段与内置阶段一视同仁。"""
        return self._ordered()

    async def tick(self) -> TickOutcome:
        """跑一轮。**不抛异常**——所有失败都变成 ``TickOutcome.status``。

        引擎是长期运行的循环最里面那一层（``cli_serve`` / ``alterego chat
        --watch``），一次模型超时不该让整个循环退出。所以这里吞掉所有异常并
        记进 ``errors``：用户要看的是「它这几轮有点不对劲」，而不是一个
        栈回溯。
        """
        started = time.perf_counter()
        virtual_now = self._clock.virtual_now()
        tick_id = self._id(virtual_now)
        state = self._snapshot(virtual_now)
        ctx = TickContext(
            tick_id=tick_id,
            virtual_now=virtual_now,
            correlation_id=tick_id,
            state=state,
            rng=self._rng(virtual_now),
            llm_gateway=self._gateway,
        )

        ran, skipped, errors = await self._drive(ctx)
        status = self._status(ran, errors)
        write_error = self._commit(ctx, started, status)
        if write_error is not None:
            errors.append(write_error)
            status = TICK_FAILED

        return TickOutcome(
            tick_id=tick_id,
            virtual_now=virtual_now,
            status=status,
            chosen_intent=None if ctx.chosen_intent is None else ctx.chosen_intent.name,
            reason="" if ctx.chosen_intent is None else str(ctx.chosen_intent.reason),
            ran=tuple(ran),
            skipped=tuple(skipped),
            errors=tuple(errors),
            notes=tuple(ctx.notes),
            llm_calls=len(ctx.llm_calls),
            llm_tokens=ctx.llm_tokens,
            activities=len(ctx.activity_records),
            messages=len(ctx.message_records),
            posts=len(ctx.post_records),
        )

    # ── 跑阶段 ──────────────────────────────────────────────

    async def _drive(self, ctx: TickContext) -> tuple[list[str], list[str], list[str]]:
        """插件前置钩子 → 阶段 → 插件后置钩子。

        **依赖没成的阶段会被跳过，而不是硬跑。** ``intention`` 拿不到
        ``percepts`` 时会退化成「凭情绪猜」，那个结果看起来正常但全是编的——
        跳过它在 ``tick_log`` 里留下一句「skipped」，比一条编出来的决策有用。
        """
        ran: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        done: set[str] = set()

        self._hook("tick_pre", ctx, errors)
        for stage in self._ordered():
            if not stage.enabled:
                skipped.append(stage.name)
                continue
            unmet = [name for name in stage.depends_on if name not in done]
            if unmet:
                skipped.append(stage.name)
                ctx.note(f"跳过了 {stage.name}：它等的 {', '.join(unmet)} 没跑成")
                continue
            result = await self._run_one(stage, ctx, errors)
            if result is None:
                continue
            ran.append(stage.name)
            done.add(stage.name)
        self._hook("tick_post", ctx, errors)
        return ran, skipped, errors

    async def _run_one(
        self,
        stage: Stage,
        ctx: TickContext,
        errors: list[str],
    ) -> StageResult | None:
        """跑一个阶段并把它的产出合并回 context。失败返回 ``None``。"""
        try:
            result = await stage.run(ctx)
        except Exception as exc:
            message = f"{stage.name} 炸了：{type(exc).__name__}: {exc}"
            errors.append(message)
            ctx.note(message)
            _log.warning(message, extra={"tick_id": ctx.tick_id, "stage": stage.name})
            return None

        self._merge(ctx, result.changes)
        if not result.ok:
            message = f"{stage.name} 没跑成：{result.error}"
            errors.append(message)
            ctx.note(message)
        return result

    @staticmethod
    def _merge(ctx: TickContext, changes: Mapping[str, Any]) -> None:
        """把 ``StageResult.changes`` 应用回 context。

        **不认识的键被安静忽略**，不报错。这不是「宽容」而是必要：
        插件阶段会带自己的键（``order = 20`` 那个位置插进来的天气阶段
        可能想交一份 ``weather``），而一个不认识就炸的引擎等于要求所有插件
        先改内核。真正需要的是那三样东西——``state`` / 上面那张表里的键 /
        阶段直接往 context 追加的列表——它们都在。
        """
        for key, value in changes.items():
            if key == "state":
                ctx.state = value
                continue
            target = _CHANGE_TARGETS.get(key)
            if target is not None:
                setattr(ctx, target, value)

    def _ordered(self) -> tuple[Stage, ...]:
        """按 ``order`` 排序，同序按名字。

        **同序时按名字是为了确定性**（P6）：两个阶段都写 ``order = 20`` 时，
        排序结果不该取决于它们被注册的先后——那会让「同一份世界跑两次」
        得到两个结果，而这种不确定只在插件多起来之后才会出现。
        """
        return tuple(sorted(self._stages, key=lambda stage: (stage.order, stage.name)))

    def _hook(self, name: str, ctx: TickContext, errors: list[str]) -> None:
        """调一次插件钩子。没装插件就什么都不做。"""
        if self._plugins is None:
            return
        try:
            getattr(self._plugins, name)(ctx)
        except Exception as exc:
            message = f"插件钩子 {name} 炸了：{type(exc).__name__}: {exc}"
            errors.append(message)
            ctx.note(message)

    @staticmethod
    def _status(ran: Sequence[str], errors: Sequence[str]) -> str:
        """这一轮的结局。"""
        if not errors:
            return TICK_OK
        return TICK_PARTIAL if "persist" in ran else TICK_FAILED

    # ── 读快照 ──────────────────────────────────────────────

    def _snapshot(self, virtual_now: datetime) -> StateSnapshot:
        """把库读成一份快照。

        实现搬到了模块级的 :func:`read_snapshot`：会话那条路径
        （``sim/conversation.py``）要读**同一份**世界，而两份读法迟早会分岔——
        分岔的后果是「同一时刻、同一个它，在两条路径上心情不一样」。
        """
        return read_snapshot(
            self._ports,
            persona=self._persona,
            conversation_id=self._conversation_id,
            virtual_now=virtual_now,
            history_limit=self._history_limit,
            memory_limit=self._memory_limit,
            memory_days=self._memory_days,
            activity_hours=self._activity_hours,
        )

    # ── 落库 ────────────────────────────────────────────────

    def _commit(self, ctx: TickContext, started: float, status: str) -> str | None:
        """一个事务写下去。返回错误描述（写成功返回 ``None``）。

        **推演日志和推演结果在同一个事务里。** 分成两次提交会出现「日志说
        它发了条动态、但动态表里没有」——而那条日志正是用来回答「它到底做了
        什么」的，它自己不可信就全都不成立了。

        ``status`` 由调用方传进来而不是在这里现算：写库本身还会失败一次，
        而那次失败要覆盖掉前面算出来的结论。
        """
        persona_id = self._persona.id
        draft = self._draft(ctx, status, elapsed_ms=int((time.perf_counter() - started) * 1000))
        try:
            with self._ports.backend.transaction():
                conversation = self._ports.conversations.ensure(
                    ConversationRecord(
                        id=self._conversation_id,
                        persona_id=persona_id,
                        counterpart_id=USER_COUNTERPART_ID,
                        counterpart_kind="user",
                        created_at=ctx.virtual_now,
                    )
                )
                for record in ctx.message_records:
                    self._ports.conversations.append(
                        replace(record, conversation_id=conversation.id)
                    )
                if ctx.activity_records:
                    self._ports.activities.append(ctx.activity_records)
                for post in ctx.post_records:
                    self._ports.posts.append(post)
                if ctx.state.budget is not None:
                    self._ports.budgets.save(persona_id, ctx.state.budget)
                if ctx.state.emotion is not None:
                    self._ports.emotions.append(
                        persona_id,
                        ctx.state.emotion,
                        reason="；".join(ctx.reflections),
                        causes=tuple(_causes(ctx)),
                        tick_id=ctx.tick_id,
                    )
                self._ports.tick_logs.append(draft)
        except Exception as exc:
            message = f"写库失败：{type(exc).__name__}: {exc}"
            _log.error(message, extra={"tick_id": ctx.tick_id})
            return message

        _log.debug(
            "tick 结束",
            extra={
                "tick_id": ctx.tick_id,
                "status": status,
                "ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return None

    def _draft(self, ctx: TickContext, status: str, *, elapsed_ms: int) -> TickLogDraft:
        """``tick_log`` 的那一行。

        ``state_snapshot`` 只留**能解释决策的那几个数**，不留整份快照：
        整份里有人设文档、记忆全文、二十条消息，把它塞进每一行会让这张表
        一天长到几百 MB，而 ``alterego why`` 要读的其实是「当时什么心情、
        在干嘛、有没有人在等」。要更多细节的人应该去查对应的表。

        ``elapsed_ms`` 由调用方量好传进来：它是**墙上时钟**，
        和虚拟时间是两回事（推演可以六十倍速跑）。
        """
        state = ctx.state
        return TickLogDraft(
            id=ctx.tick_id,
            persona_id=self._persona.id,
            virtual_time=ctx.virtual_now,
            status=status,
            created_at=ctx.virtual_now,
            real_duration_ms=elapsed_ms,
            state_snapshot=_snapshot_digest(state),
            percepts={} if ctx.percepts is None else asdict(ctx.percepts),
            candidates=[_candidate_digest(item) for item in ctx.candidates],
            chosen_intent=None if ctx.chosen_intent is None else ctx.chosen_intent.name,
            motivation=str(getattr(ctx.chosen_intent, "reason", "") or ""),
            trigger_note=str(getattr(ctx.chosen_intent, "reason", "") or ""),
            suppressed=[_as_mapping(item) for item in ctx.suppressed],
            memories=[str(getattr(memory, "summary", "")) for memory in state.recent_memories],
            notes=tuple(ctx.notes),
            stage_results=self._stage_results(ctx),
            llm_calls=len(ctx.llm_calls),
            llm_tokens=ctx.llm_tokens,
        )

    @staticmethod
    def _stage_results(ctx: TickContext) -> dict[str, Any]:
        """每个阶段交出哪些键。**只记形状，不记内容**——

        内容在 ``suppressed`` / ``candidates`` / ``notes`` 里已经有了，
        再存一份整段 ``changes`` 会让同一件事在一行里出现两次，
        而两次里的一次会先过期。
        """
        return {
            "chosen_intent": None if ctx.chosen_intent is None else ctx.chosen_intent.name,
            "candidates": len(ctx.candidates),
            "suppressed": len(ctx.suppressed),
            "reflections": list(ctx.reflections),
        }

    def _rng(self, virtual_now: datetime) -> Random:
        """这一轮的随机源。

        **默认从 persona 与虚拟时间派生**，所以同一个时刻跑两次得到同一组
        随机数（P6）。这不是为了测试方便而已：用户问「它当时为什么选了这个」，
        答案必须能重放，而重放的前提是随机源可重建。用 ``uuid4`` 或系统熵
        会让每一次重放都换一个答案，那等于没有解释。
        """
        seed = self._seed or f"{self._persona.id}|{virtual_now.isoformat()}"
        return Random(seed)

    def _id(self, virtual_now: datetime) -> str:
        """这一轮的 id。

        默认从 persona 与虚拟时间派生（同 :meth:`_rng` 的理由）——
        ``persist`` 阶段的行 id 全部基于它，所以可重建的 tick_id 等于
        **可重建的一整批行**。组装根可以传一个 ``new_id`` 换成别的规则
        （比如一个单调递增的计数器）。
        """
        if self._new_id is not None:
            return str(self._new_id())
        return f"tick-{self._persona.id}-{virtual_now.strftime('%Y%m%dT%H%M%S')}"


# ── 组装辅助 ────────────────────────────────────────────────


def read_snapshot(
    ports: EnginePorts,
    *,
    persona: PersonaView,
    conversation_id: str,
    virtual_now: datetime,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    memory_limit: int = DEFAULT_MEMORY_LIMIT,
    memory_days: int = DEFAULT_MEMORY_DAYS,
    activity_hours: int = DEFAULT_ACTIVITY_HOURS,
) -> StateSnapshot:
    """把库读成一份 :class:`StateSnapshot`。

    **容错范围很窄。** 只有两样东西的缺失被当成正常：没有日程（新角色
    还没生成日程）和没有情绪记录（第一次跑）。其余的读不出来都会抛——
    一份悄悄缺了对话历史的快照，会让它突然「不记得刚才说过什么」，
    而那种 bug 从输出上完全看不出来。

    单独一个函数而不是 :class:`SimulationEngine` 的私有方法，是因为
    **两条路径要读同一份世界**：tick 引擎和 ``ConversationService``
    都会在某一刻问「它现在什么心情、在干嘛、有没有人在等」。两份读法
    迟早会分岔，而分岔的症状是「同一时刻、同一个它，在两条路径上心情不一样」，
    从输出上根本看不出来。
    """
    persona_id = persona.id
    messages = _history(ports, conversation_id, limit=history_limit)
    return StateSnapshot(
        persona=persona,
        emotion=ports.emotions.latest(persona_id),
        current_block=current_block(_blocks(ports, persona_id, virtual_now), virtual_now),
        recent_memories=tuple(
            ports.memories.list_recent(
                persona_id,
                since=virtual_now - timedelta(days=memory_days),
                limit=memory_limit,
            )
        ),
        today_activity=tuple(
            ports.activities.list_range(
                persona_id,
                since=virtual_now - timedelta(hours=activity_hours),
                until=virtual_now,
                limit=200,
            )
        ),
        budget=ports.budgets.load(persona_id, day=virtual_now.date()),
        unread_messages=ports.conversations.count_unread(conversation_id),
        last_user_message_at=ports.conversations.last_inbound_at(conversation_id),
        recent_messages=messages,
        last_inbound_text=last_inbound_text(messages),
        consecutive_instant_replies=instant_streak(messages, within_seconds=INSTANT_REPLY_SECONDS),
        consecutive_passive_turns=passive_streak(messages),
        last_topic_at=last_topic_at(messages),
    )


def _blocks(ports: EnginePorts, persona_id: str, virtual_now: datetime) -> list[ScheduleBlock]:
    """这一天的日程，转成领域对象。

    转不过去的块被**丢掉并在日志里说一声**：``ScheduleBlock`` 会校验
    分类名与时间顺序，而库里真出现过坏数据（手改过、导入过）时，
    唯一的选择是丢掉那一块还是放弃整天的日程。丢掉更好——
    日程是「它现在在干嘛」的背景，缺一块的代价远小于全天都没有。
    """
    records = ports.schedules.list_day(persona_id, day=virtual_now.date())
    blocks: list[ScheduleBlock] = []
    for record in records:
        block = to_schedule_block(record, persona_id=persona_id)
        if block is None:
            _log.warning(
                "日程块不合法，已跳过",
                extra={"schedule_id": record.id, "category": record.category},
            )
            continue
        blocks.append(block)
    return blocks


def _history(ports: EnginePorts, conversation_id: str, *, limit: int) -> tuple[MessageRecord, ...]:
    """最近的对话，按时间升序（提示词里的 ``{conversation}`` 要用）。"""
    if not conversation_id:
        return ()
    return tuple(ports.conversations.list_messages(conversation_id, limit=limit))


def default_stages(
    *, registry: ServiceRegistry, budget: Any, prompts: PromptLibrary
) -> list[Stage]:
    """六个内置阶段 + 注册表里的插件阶段。

    插件阶段的来源是 ``ctx.registry.register(Stage, ...)``——``PluginManager``
    在 ``OwnedRegistry`` 里已经给每个注册项记了归属，所以热重载时插件阶段会
    跟着一起消失，这里不需要再维护一份「哪些阶段属于哪个插件」的账。

    ``budget`` 是 ``[simulation.disturb_budget]`` 那一段配置，``prompts``
    是提示词库。两者都从调用方进来而不是在阶段里自己读配置：**一个阶段装了
    哪套阈值、哪套提示词，应当在装配那一刻就定死**，运行期改配置不该让同一轮
    推演算出一个不同答案。

    意图目录每次调用造一份新的（而不是模块级单例）：``IntentCatalog`` 是可变
    的（``add`` 会往里加），共享一份会让某个插件加进去的意图出现在所有人的
    目录里——而「哪个插件加了什么」不该靠约定去保证。
    """
    builtin: list[Stage] = [
        SenseStage(),
        ReflectStage(),
        IntentionStage(catalog=IntentCatalog(), budget=budget),
        ActStage(registry=registry),
        ExpressStage(prompts=prompts),
        PersistStage(budget=budget),
    ]
    return [*builtin, *[stage for _name, stage in registry.get_all(_STAGE_KEY)]]


def to_schedule_block(record: ScheduleRecord, *, persona_id: str) -> ScheduleBlock | None:
    """把库里的一行日程转成领域对象。转不过去返回 ``None``。

    ``persona_id`` 由调用方给而不是从行里取：``ScheduleRecord`` 不带这一列
    （查的时候就是按它筛的），而 ``ScheduleBlock`` 要求它非空。
    与其往契约里加一个恒等于入参的字段，不如在这里显式传一次。

    分类名不认识时**退成 ``other`` 而不是丢掉整块**：分类名是枚举，
    而一块「下午在干嘛」的日程比它的分类重要得多。
    """
    category = record.category if record.category in SCHEDULE_CATEGORIES else "other"
    try:
        return ScheduleBlock(
            id=record.id,
            persona_id=persona_id,
            day=record.day,
            start_at=record.start_at,
            end_at=record.end_at,
            activity=record.activity,
            category=cast("ScheduleCategory", category),
            interruptible=record.interruptible,
            location=record.location or None,
            actual_start_at=record.actual_start_at,
            actual_end_at=record.actual_end_at,
            deviation_note=record.deviation_note or None,
        )
    except ValueError:
        return None


# ── 纯函数小工具 ────────────────────────────────────────────


def _snapshot_digest(state: StateSnapshot) -> dict[str, Any]:
    """快照里能解释决策的那几个数。见 :meth:`SimulationEngine._draft`。"""
    emotion = state.emotion
    block = state.current_block
    budget = state.budget
    return {
        "emotion": None
        if emotion is None
        else {
            "label": str(getattr(emotion, "label", "")),
            "valence": round(float(getattr(emotion, "valence", 0.0)), 3),
            "arousal": round(float(getattr(emotion, "arousal", 0.0)), 3),
            "fatigue": round(float(getattr(emotion, "fatigue", 0.0)), 3),
        },
        "block": None
        if block is None
        else {
            "activity": str(getattr(block, "activity", "")),
            "interruptible": bool(getattr(block, "interruptible", True)),
        },
        "unread_messages": int(state.unread_messages),
        "last_user_message_at": None
        if state.last_user_message_at is None
        else state.last_user_message_at.isoformat(),
        "consecutive_instant_replies": int(state.consecutive_instant_replies),
        "budget": None if budget is None else _budget_digest(budget),
    }


def _budget_digest(usage: BudgetUsage) -> dict[str, Any]:
    return {
        "day": usage.day.isoformat(),
        "messages_sent": int(usage.messages_sent),
        "posts_sent": int(usage.posts_sent),
        "messages_suppressed": int(usage.messages_suppressed),
        "posts_suppressed": int(usage.posts_suppressed),
        "consecutive_no_reply": int(usage.consecutive_no_reply),
        "circuit_until": None if usage.circuit_until is None else usage.circuit_until.isoformat(),
    }


def _candidate_digest(item: Any) -> dict[str, Any]:
    """一个候选意图压成一行。"""
    intent = getattr(item, "intent", None)
    return {
        "intent": str(getattr(intent, "name", intent or "")),
        "weight": round(float(getattr(item, "weight", 0.0)), 3),
        "reason": str(getattr(item, "reason", "")),
    }


def _causes(ctx: TickContext) -> list[str]:
    """这一轮为什么变了情绪。**取被拦下的那些**——

    ``emotion_log.causes_json`` 回答的是「什么撞了它一下」，而那是
    ``domain.emotion`` 里的 ``EmotionalEvent``。此刻引擎手上只有阶段给的
    文本（``reflections``）与被拦下的意图，于是先把后者记进去：
    一条「想发消息被拦下」是当天最可能解释情绪波动的事件。
    """
    causes: list[str] = []
    for item in ctx.suppressed:
        reason = _as_mapping(item).get("reason")
        if reason:
            causes.append(str(reason))
    return causes


def _as_mapping(item: Any) -> dict[str, Any]:
    """``ctx.suppressed`` 里既可能是字典也可能是有属性的对象，两种都收。

    dataclass 走 ``asdict`` 而不是 ``vars``：frozen + slots 的 dataclass
    根本没有 ``__dict__``，用 ``vars`` 会在插件传进来一个数据类时抛异常——
    而那个异常发生在「写推演日志」这一步，等于把一整轮推演赔进去。
    """
    if isinstance(item, Mapping):
        return {str(key): value for key, value in item.items()}
    if is_dataclass(item) and not isinstance(item, type):
        return {str(key): value for key, value in asdict(item).items()}
    if hasattr(item, "__dict__"):
        return {str(key): value for key, value in vars(item).items()}
    return {"value": item}

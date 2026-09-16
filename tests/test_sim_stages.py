"""六个内置阶段的单元测试。

阶段是纯的：``run(ctx)`` 进、``StageResult`` 出，没有 IO、没有全局随机、
没有墙上时钟。所以这里**一个真实数据库都不需要**——所有的「世界」
都是几个 frozen dataclass 拼出来的。

测试的落点不是「它跑了」，而是三条更容易出错的约定：

1. **没有模型也要能跑完。** ``ctx.llm_gateway`` 允许为 ``None``
   （``--dry-run``、纯测试）。任何一处忘了判空都会让整轮推演白跑。
2. **没有数据也要给一句话。** 人设文档缺失、没聊过天、没有日程块——
   这些是新装好的机器的正常状态，而提示词里出现空串会让模型自己编。
3. **被拦下的东西不能消失。** ``ctx.suppressed`` 是接口，不是日志。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from random import Random
from typing import Any

from alterego.domain.emotion import Emotion, infer_label
from alterego.domain.schedule import ScheduleBlock
from alterego.interfaces.repository import MessageRecord
from alterego.interfaces.simulation import (
    Capability,
    CapabilityResult,
    Intent,
    Percepts,
)
from alterego.kernel.config import DisturbBudgetConfig
from alterego.kernel.registry import ServiceRegistry
from alterego.llm.prompts import PromptLibrary
from alterego.sim.budget import BudgetUsage
from alterego.sim.context import StateSnapshot, TickContext
from alterego.sim.intents import REACH_OUT_MOTIVATIONS, IntentCatalog
from alterego.sim.persona_view import PersonaView
from alterego.sim.stages import (
    ActStage,
    ExpressStage,
    IntentionStage,
    PersistStage,
    ReflectStage,
    SenseStage,
)
from alterego.sim.stages.common import (
    clock_text,
    day_key,
    describe_block,
    elapsed_since,
    last_started_at,
    minutes_between,
    weekday_text,
)
from alterego.sim.stages.express import ExpressionStyle
from alterego.sim.stages.reflect import REFLECT_INTERVAL_MINUTES, baseline_emotion


NOW = datetime(2026, 9, 15, 14, 0)
DAY = date(2026, 9, 15)
PERSONA_ID = "p1"

#: 一整天都在免打扰的配置——用来稳定地让预算检查拦下对外意图。
QUIET_ALL_DAY = DisturbBudgetConfig(
    quiet_hours=(
        datetime(2026, 9, 15, 0, 1).time(),
        datetime(2026, 9, 15, 23, 59).time(),
    )
)


# ── 造世界的小工具 ──────────────────────────────────────────


def make_block(**overrides: Any) -> ScheduleBlock:
    """一个「下午在写代码」的日程块。"""
    values: dict[str, Any] = {
        "id": "b1",
        "persona_id": PERSONA_ID,
        "day": DAY,
        "start_at": datetime(2026, 9, 15, 13, 0),
        "end_at": datetime(2026, 9, 15, 18, 0),
        "activity": "写代码",
        "category": "work",
        "location": "公司",
    }
    values.update(overrides)
    return ScheduleBlock(**values)


def make_emotion(**overrides: Any) -> Emotion:
    values: dict[str, Any] = {
        "valence": 0.3,
        "arousal": 0.5,
        "fatigue": 0.2,
        "label": infer_label(0.3, 0.5),
        "updated_at": NOW - timedelta(minutes=30),
    }
    values.update(overrides)
    return Emotion(**values)


def make_persona(**overrides: Any) -> PersonaView:
    values: dict[str, Any] = {
        "id": PERSONA_ID,
        "name": "林晚",
        "user_name": "小陈",
        "age": 27,
        "gender": "女",
        "city": "杭州",
        "occupation": "算法工程师",
        "summary": "我是林晚，做算法的。",
    }
    values.update(overrides)
    return PersonaView(**values)


@dataclass
class FakeMemory:
    """``recent_memories`` 只要求 ``summary`` 一个字段。"""

    summary: str = "上周去看了展"


@dataclass
class FakeActivity:
    """``today_activity`` 只要求 ``started_at``。"""

    started_at: Any = NOW - timedelta(minutes=90)


def make_message(**overrides: Any) -> MessageRecord:
    values: dict[str, Any] = {
        "id": "m1",
        "conversation_id": f"{PERSONA_ID}:user",
        "direction": "inbound",
        "sender_id": "user",
        "content": "在吗",
        "created_at": NOW - timedelta(minutes=5),
    }
    values.update(overrides)
    return MessageRecord(**values)


def make_snapshot(**overrides: Any) -> StateSnapshot:
    values: dict[str, Any] = {
        "persona": make_persona(),
        "emotion": make_emotion(),
        "budget": BudgetUsage(day=DAY),
    }
    values.update(overrides)
    return StateSnapshot(**values)


def make_context(**overrides: Any) -> TickContext:
    """一个上下文。默认「什么都没发生」，各测试只覆盖自己关心的那几项。"""
    values: dict[str, Any] = {
        "tick_id": "t1",
        "virtual_now": NOW,
        "correlation_id": "t1",
        "state": make_snapshot(),
        "rng": FixedRandom(0.5),
    }
    values.update(overrides)
    return TickContext(**values)


def make_intent(name: str, **overrides: Any) -> Intent:
    """从内置目录里取一条意图，做成「这一次」的实例。"""
    values: dict[str, Any] = {"type": intent_type(name), "reason": "测试"}
    values.update(overrides)
    return Intent(**values)


def make_percepts(
    *,
    unread: int = 0,
    idle: float = 0.0,
    minutes: float | None = 15.0,
    block: Any = None,
) -> Percepts:
    return Percepts(
        virtual_now=NOW,
        block=block,
        unread_messages=unread,
        minutes_since_last_user_message=minutes,
        idle_minutes=idle,
    )


def intent_type(name: str, **overrides: Any) -> Any:
    """从内置目录里取一条意图定义，必要时改几个字段。"""
    found = IntentCatalog().get(name)
    assert found is not None
    return replace(found, **overrides) if overrides else found


def make_registry(*capabilities: FakeCapability) -> ServiceRegistry:
    """注册表里的键是 ``Capability`` 这个 Protocol——``ActStage`` 就是这么找的。"""
    registry = ServiceRegistry()
    for index, capability in enumerate(capabilities):
        registry.register(Capability, capability, name=f"cap{index}")
    return registry


class FixedRandom(Random):
    """``random()`` 恒定返回同一个值的随机源。

    ``choose`` 与 ``decide_reply`` 都是「摇一个数、落进哪个区间」。断言
    「选中了哪一条」时必须把那个数固定下来，否则测试会随权重调整而随机翻车。
    """

    def __init__(self, value: float = 0.5) -> None:
        super().__init__(0)
        self.value = value

    def random(self) -> float:
        return self.value


class FakeGateway:
    """假网关。``replies`` 按顺序吐，用完之后一直吐最后一条。

    参数顺序跟真网关一致：``complete(purpose, prompt, ...)``。
    ``purpose`` 在前是因为网关要拿它查路由表，而路由表是整个 ``LLMGateway``
    存在的理由——写成 ``complete(prompt, purpose)`` 会让每次调用都多一个
    位置参数要靠记忆去对。
    """

    def __init__(self, *replies: Any) -> None:
        self.replies: list[Any] = list(replies) or ["好的"]
        self.purposes: list[str] = []
        self.prompts: list[str] = []

    @property
    def calls(self) -> list[str]:
        """提示词序列。大多数断言关心的是「喂给模型的那段话」。"""
        return self.prompts

    async def complete(self, purpose: str, prompt: str, **kwargs: Any) -> Any:
        self.purposes.append(purpose)
        self.prompts.append(prompt)
        text = self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]
        if isinstance(text, Exception):
            raise text
        return FakeResponse(text)


@dataclass
class FakeResponse:
    text: str
    prompt_tokens: int = 10
    completion_tokens: int = 5


@dataclass
class FakeCapability:
    """一个能执行指定意图的能力。"""

    id: str = "cap.post"
    intent_types: frozenset[str] = frozenset({"post_moment"})
    result: CapabilityResult = field(
        default_factory=lambda: CapabilityResult(ok=True, summary="发了条动态")
    )
    boom: Exception | None = None
    seen: list[Intent] = field(default_factory=list)

    async def execute(self, intent: Intent, ctx: TickContext) -> CapabilityResult:
        self.seen.append(intent)
        if self.boom is not None:
            raise self.boom
        return self.result


def run_stage(stage: Any, ctx: TickContext) -> Any:
    """同步地把一个阶段的 ``run`` 跑完，并把它的 ``changes`` 应用到上下文上。

    阶段**不**直接改 ``ctx.state`` / ``ctx.percepts`` 这些引擎拥有的字段：
    它只在 ``StageResult.changes`` 里声明「我要把它们改成这样」，由引擎统一
    落下去（见 ``sim.engine`` 的 ``_CHANGE_TARGETS``）。这里照着做同一步，
    否则断言看的是一个永远不动的上下文，而那是「测试没接上」而不是「阶段错了」。

    这个文件里的测试都是同步的，只有少数几处需要并发（模型返回的过程），
    所以显式驱动而不是把每个测试都写成 ``async def``。
    """
    result = asyncio.run(stage.run(ctx))
    for key, value in result.changes.items():
        setattr(ctx, key, value)
    return result


def express(**overrides: Any) -> ExpressStage:
    values: dict[str, Any] = {"prompts": PromptLibrary()}
    values.update(overrides)
    return ExpressStage(**values)


def persist() -> PersistStage:
    return PersistStage(budget=DisturbBudgetConfig())


def render_chat(stage: ExpressStage, ctx: TickContext) -> str:
    """把 ``chat_reply`` 要填的占位符填出来再渲染，看最终那段话长什么样。

    直接打模板而不是逐字比对：这里要验的是「填的值恰好能被模板找到」，
    逐字比对模板会让每改一次提示词就改一次测试。
    """
    values: Mapping[str, str] = stage._values_chat(ctx)
    return stage.prompts.render("chat_reply", **values)


# ── 1. 共用小工具 ───────────────────────────────────────────


class TestCommon:
    """``common.py`` 是六个阶段共用的换算。这里错一个都会影响整条链。"""

    def test_minutes_between_returns_none_for_a_stranger(self) -> None:
        """「从没说过话」与「很久没说话」必须可区分。"""
        assert minutes_between(None, NOW) is None

    def test_minutes_between_clamps_negative_to_zero(self) -> None:
        """时钟回拨时给 0 而不是负数：负数会一路传进提示词。"""
        assert minutes_between(NOW + timedelta(hours=1), NOW) == 0.0

    def test_minutes_between_counts_fractional_minutes(self) -> None:
        assert minutes_between(NOW - timedelta(seconds=90), NOW) == 1.5

    def test_last_started_at_is_empty_without_activities(self) -> None:
        assert last_started_at([]) is None

    def test_last_started_at_takes_the_maximum_not_the_last(self) -> None:
        """不依赖仓储的 ``ORDER BY``——那会让一句 SQL 悄悄改变推演结果。"""
        earlier = FakeActivity(started_at=NOW - timedelta(hours=3))
        later = FakeActivity(started_at=NOW - timedelta(minutes=10))
        assert last_started_at([later, earlier]) == later.started_at

    def test_last_started_at_ignores_non_datetimes(self) -> None:
        assert last_started_at([FakeActivity(started_at="不是时间")]) is None

    def test_describe_block_says_there_is_nothing_when_block_is_missing(self) -> None:
        assert describe_block(None) == "没有安排"

    def test_describe_block_falls_back_when_activity_is_empty(self) -> None:
        assert describe_block(make_block(activity="")) == "没有安排"

    def test_describe_block_says_what_the_block_is_about(self) -> None:
        assert describe_block(make_block()) == "写代码"

    def test_clock_text_drops_seconds(self) -> None:
        """秒对「现在是下午还是半夜」毫无帮助，却会让模板每次渲染都不同。"""
        assert clock_text(NOW.replace(second=42)) == "2026-09-15 14:00"

    def test_weekday_text_maps_every_day_of_the_week(self) -> None:
        # 2026-09-14 是周一。
        monday = datetime(2026, 9, 14, 12, 0)
        names = [weekday_text(monday + timedelta(days=offset)) for offset in range(7)]
        assert names == ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    def test_day_key_is_the_local_date(self) -> None:
        assert day_key(NOW) == "2026-09-15"


class TestElapsedSince:
    def test_uses_the_fallback_when_there_is_no_previous_moment(self) -> None:
        assert elapsed_since(None, NOW, timedelta(minutes=5)) == timedelta(minutes=5)

    def test_returns_the_real_delta(self) -> None:
        assert elapsed_since(NOW - timedelta(minutes=20), NOW, timedelta()) == timedelta(minutes=20)

    def test_negative_deltas_become_the_fallback(self) -> None:
        """负的 ``elapsed`` 会让 ``update_emotion`` 的指数衰减往反方向推。"""
        fallback = timedelta(minutes=1)
        assert elapsed_since(NOW + timedelta(hours=3), NOW, fallback) == fallback


# ── 2. Sense ────────────────────────────────────────────────


class TestSenseStage:
    def test_writes_percepts_from_the_snapshot(self) -> None:
        ctx = make_context(
            state=make_snapshot(
                current_block=make_block(),
                last_user_message_at=NOW - timedelta(minutes=15),
                today_activity=(FakeActivity(started_at=NOW - timedelta(minutes=30)),),
                unread_messages=2,
            )
        )

        result = run_stage(SenseStage(), ctx)

        assert result.ok
        percepts = ctx.percepts
        assert percepts is not None
        assert percepts.block is ctx.state.current_block
        assert percepts.unread_messages == 2
        assert percepts.minutes_since_last_user_message == 15.0
        assert percepts.idle_minutes == 30.0

    def test_idle_is_zero_when_there_was_never_an_action(self) -> None:
        """``minutes_between`` 返回的 ``None`` 不能原样传下去。"""
        ctx = make_context(state=make_snapshot(today_activity=()))
        run_stage(SenseStage(), ctx)
        assert ctx.percepts is not None
        assert ctx.percepts.idle_minutes == 0.0

    def test_says_so_when_there_is_nothing_to_say(self) -> None:
        ctx = make_context()
        run_stage(SenseStage(), ctx)
        assert "你们还没说过话" in ctx.notes

    def test_says_how_many_messages_are_waiting(self) -> None:
        ctx = make_context(state=make_snapshot(last_user_message_at=NOW, unread_messages=3))
        run_stage(SenseStage(), ctx)
        assert "用户在等它回话：3 条" in ctx.notes

    def test_names_the_current_block(self) -> None:
        ctx = make_context(state=make_snapshot(current_block=make_block()))
        run_stage(SenseStage(), ctx)
        assert any("写代码" in note for note in ctx.notes)

    def test_does_not_call_the_model(self) -> None:
        """感知必须是确定的：让模型参与会把「它为什么没注意到你」变成玄学。"""
        ctx = make_context(llm_gateway=FakeGateway())
        run_stage(SenseStage(), ctx)
        assert ctx.llm_calls == []


# ── 3. Reflect ──────────────────────────────────────────────


class TestReflectStage:
    def test_updates_the_emotion_in_the_snapshot(self) -> None:
        ctx = make_context(percepts=make_percepts())

        result = run_stage(ReflectStage(), ctx)

        assert result.ok
        assert "state" in result.changes
        assert ctx.state.emotion is not None

    def test_uses_a_baseline_when_there_is_no_emotion_yet(self) -> None:
        """新装好的机器没有情绪行，但推演不能因此停住。"""
        ctx = make_context(state=make_snapshot(emotion=None), percepts=make_percepts())
        run_stage(ReflectStage(), ctx)
        assert ctx.state.emotion is not None
        assert ctx.state.emotion.label == baseline_emotion(NOW).label

    def test_the_model_is_not_asked_when_nothing_happened(self) -> None:
        """一天 288 个 tick，每轮都问一次模型既贵又不可复现。"""
        gateway = FakeGateway("有点烦")
        ctx = make_context(
            state=make_snapshot(unread_messages=0),
            percepts=make_percepts(unread=0),
            llm_gateway=gateway,
        )

        run_stage(ReflectStage(), ctx)

        assert gateway.calls == []

    def test_unread_messages_become_an_emotional_event(self) -> None:
        gateway = FakeGateway("有人在等我")
        ctx = make_context(
            state=make_snapshot(unread_messages=2),
            percepts=make_percepts(unread=2),
            llm_gateway=gateway,
        )

        result = run_stage(ReflectStage(), ctx)

        assert result.ok
        assert len(gateway.calls) == 1
        assert "有人在等它回话" in gateway.calls[0]

    def test_boredom_is_an_event_and_gets_a_self_report(self) -> None:
        gateway = FakeGateway("有点无聊")
        ctx = make_context(
            state=make_snapshot(),
            percepts=make_percepts(idle=300.0),
            llm_gateway=gateway,
        )

        run_stage(ReflectStage(), ctx)

        assert len(gateway.calls) == 1
        assert "有点无聊" in ctx.reflections

    def test_sleeping_is_not_boring(self) -> None:
        """在睡的人不无聊。这条判错会让它在半夜给自己加一条「闲得慌」。"""
        gateway = FakeGateway("嗯")
        sleeping = make_block(category="sleep", activity="睡觉")
        ctx = make_context(
            state=make_snapshot(current_block=sleeping),
            percepts=make_percepts(idle=600.0, block=sleeping),
            llm_gateway=gateway,
        )

        run_stage(ReflectStage(), ctx)

        assert gateway.calls == []

    def test_sleeping_restores_fatigue_only_when_it_is_tired(self) -> None:
        gateway = FakeGateway("睡着了好些")
        sleeping = make_block(category="sleep", activity="睡觉")
        ctx = make_context(
            state=make_snapshot(emotion=make_emotion(fatigue=0.8), current_block=sleeping),
            percepts=make_percepts(block=sleeping),
            llm_gateway=gateway,
        )

        run_stage(ReflectStage(), ctx)

        assert len(gateway.calls) == 1
        assert "在睡" in gateway.calls[0]

    def test_no_percepts_means_no_events(self) -> None:
        """感知缺失时不该凭空造事件——事件的来源必须是看得见的东西。"""
        gateway = FakeGateway("嗯")
        ctx = make_context(percepts=None, llm_gateway=gateway)
        run_stage(ReflectStage(), ctx)
        assert gateway.calls == []

    def test_a_broken_model_does_not_break_the_tick(self) -> None:
        gateway = FakeGateway(RuntimeError("超时"))
        ctx = make_context(
            state=make_snapshot(unread_messages=1),
            percepts=make_percepts(unread=1),
            llm_gateway=gateway,
        )

        result = run_stage(ReflectStage(), ctx)

        assert result.ok
        assert any("情绪自述没生成出来" in note for note in ctx.notes)

    def test_an_empty_self_report_is_not_a_reflection(self) -> None:
        gateway = FakeGateway("   ")
        ctx = make_context(
            state=make_snapshot(unread_messages=1),
            percepts=make_percepts(unread=1),
            llm_gateway=gateway,
        )
        run_stage(ReflectStage(), ctx)
        assert all(text.strip() for text in ctx.reflections)

    def test_sensitivity_is_clamped_not_trusted(self) -> None:
        ctx = make_context(percepts=make_percepts())
        assert run_stage(ReflectStage(sensitivity=99.0), ctx).ok

    def test_time_going_backwards_does_not_flip_the_emotion(self) -> None:
        future = make_emotion(updated_at=NOW + timedelta(hours=5))
        ctx = make_context(state=make_snapshot(emotion=future), percepts=make_percepts())
        assert run_stage(ReflectStage(), ctx).ok


# ── 4. Intention ────────────────────────────────────────────


class TestIntentionStage:
    def test_picks_an_intent_and_writes_candidates(self) -> None:
        ctx = make_context(state=make_snapshot(unread_messages=1), percepts=make_percepts(unread=1))

        result = run_stage(IntentionStage(catalog=IntentCatalog(), budget=QUIET_ALL_DAY), ctx)

        assert result.ok
        assert ctx.candidates
        assert ctx.chosen_intent is not None
        assert any("想做：" in note for note in ctx.notes)

    def test_says_it_wants_nothing_when_nothing_fits(self) -> None:
        """没选中意图是合法结果——那就是在发呆。"""
        ctx = make_context(percepts=make_percepts())
        stage = IntentionStage(catalog=IntentCatalog(intents=()), budget=QUIET_ALL_DAY)

        result = run_stage(stage, ctx)

        assert result.ok
        assert result.changes == {}
        assert ctx.chosen_intent is None
        assert "这一轮什么都不想做" in ctx.notes

    def test_internal_intents_skip_the_budget_gate(self) -> None:
        """工作、休息不打扰任何人，所以不受打扰预算约束。"""
        catalog = IntentCatalog(intents=(intent_type("work"),))
        ctx = make_context(percepts=make_percepts())

        run_stage(IntentionStage(catalog=catalog, budget=QUIET_ALL_DAY), ctx)

        assert ctx.chosen_intent is not None
        assert ctx.chosen_intent.name == "work"
        assert ctx.suppressed == []

    def test_an_outbound_intent_is_recorded_even_when_blocked(self) -> None:
        """「想找你但憋住了」和「根本没想过」是两回事（ADR-0005）。"""
        catalog = IntentCatalog(intents=(intent_type("reach_out"),))
        ctx = make_context(percepts=make_percepts())

        result = run_stage(IntentionStage(catalog=catalog, budget=QUIET_ALL_DAY), ctx)

        assert result.ok
        assert ctx.suppressed
        assert ctx.suppressed[0]["intent"] == "reach_out"
        assert any("没发出去" in note for note in ctx.notes)
        assert ctx.chosen_intent is None

    def test_a_blocked_intent_with_a_downgrade_becomes_another_intent(self) -> None:
        catalog = IntentCatalog(intents=(intent_type("reach_out"), intent_type("reflect_internal")))
        # 0.99 让加权抽签落在权重最低的那条上，也就是 reach_out。
        ctx = make_context(percepts=make_percepts(), rng=FixedRandom(0.99))

        run_stage(IntentionStage(catalog=catalog, budget=QUIET_ALL_DAY), ctx)

        assert ctx.chosen_intent is not None
        assert ctx.chosen_intent.name == "reflect_internal"
        assert "被拦下后改做它" in ctx.chosen_intent.reason

    def test_a_downgrade_target_that_is_not_in_the_catalog_is_survivable(self) -> None:
        """插件可以只加一条意图，而默认的降级目标可能不在它的目录里。"""
        catalog = IntentCatalog(intents=(intent_type("reach_out"),))
        ctx = make_context(percepts=make_percepts())

        run_stage(IntentionStage(catalog=catalog, budget=QUIET_ALL_DAY), ctx)

        assert ctx.chosen_intent is None
        assert any("目录里没有这条" in note for note in ctx.notes)

    def test_blocked_messages_and_posts_go_into_separate_buckets(self) -> None:
        cases = (
            ("reach_out", "messages_suppressed"),
            ("post_moment", "posts_suppressed"),
        )
        for name, field_name in cases:
            catalog = IntentCatalog(intents=(intent_type(name),))
            ctx = make_context(percepts=make_percepts())
            run_stage(IntentionStage(catalog=catalog, budget=QUIET_ALL_DAY), ctx)
            usage = ctx.state.budget
            assert usage is not None
            assert getattr(usage, field_name) == 1, name

    def test_a_stale_budget_day_is_treated_as_a_fresh_day(self) -> None:
        """时间倒流（导入旧库、换时区）不该让整轮推演炸掉。"""
        stale = BudgetUsage(day=date(2020, 1, 1), messages_sent=99)
        catalog = IntentCatalog(intents=(intent_type("work"),))
        ctx = make_context(state=make_snapshot(budget=stale), percepts=make_percepts())

        run_stage(IntentionStage(catalog=catalog, budget=DisturbBudgetConfig()), ctx)

        assert ctx.state.budget is not None
        assert ctx.state.budget.day == DAY
        assert ctx.state.budget.messages_sent == 0

    def test_the_chosen_urgency_is_the_weight_that_won(self) -> None:
        """``urgency`` 不是另算的：预算规则里唯一需要它的是「能不能动用紧急额度」。"""
        catalog = IntentCatalog(intents=(intent_type("work"),))
        ctx = make_context(percepts=make_percepts())

        run_stage(IntentionStage(catalog=catalog, budget=DisturbBudgetConfig()), ctx)

        chosen = ctx.chosen_intent
        assert chosen is not None
        weights = {candidate.intent.name: candidate.weight for candidate in ctx.candidates}
        assert chosen.urgency == weights[chosen.name]


# ── 5. Act ──────────────────────────────────────────────────


class TestActStage:
    def test_does_nothing_without_an_intent(self) -> None:
        ctx = make_context()
        assert run_stage(ActStage(registry=ServiceRegistry()), ctx).changes == {}
        assert ctx.actions == []

    def test_an_internal_intent_needs_no_capability(self) -> None:
        """工作、休息这些事本身没有对外动作，但它们同样是「今天做过的事」。"""
        ctx = make_context(chosen_intent=make_intent("work"))

        result = run_stage(ActStage(registry=ServiceRegistry()), ctx)

        assert result.ok
        assert len(ctx.actions) == 1
        assert ctx.actions[0]["capability"] == ""
        assert ctx.actions[0]["summary"] == "推进手头的事：写代码、看文档、整理方案"

    def test_a_capability_is_found_by_intent_type(self) -> None:
        capability = FakeCapability()
        ctx = make_context(chosen_intent=make_intent("post_moment"))

        run_stage(ActStage(registry=make_registry(capability)), ctx)

        assert capability.seen
        assert ctx.actions[0]["ok"] is True
        assert ctx.actions[0]["capability"] == "cap.post"
        assert ctx.actions[0]["summary"] == "发了条动态"

    def test_a_named_capability_wins_over_the_search(self) -> None:
        named = FakeCapability(id="cap.explicit", intent_types=frozenset())
        fallback = FakeCapability(id="cap.by_type")
        wanted = intent_type("post_moment", requires_capability="cap0")
        registry = ServiceRegistry()
        registry.register(Capability, named, name="cap0")
        registry.register(Capability, fallback, name="cap1")
        ctx = make_context(chosen_intent=Intent(type=wanted))

        run_stage(ActStage(registry=registry), ctx)

        assert named.seen
        assert not fallback.seen

    def test_a_capability_that_raises_becomes_a_failed_action(self) -> None:
        """能力抛异常只让这一条行为失败，不该让整个 tick 崩掉。"""
        capability = FakeCapability(boom=RuntimeError("接口挂了"))
        ctx = make_context(chosen_intent=make_intent("post_moment"))

        result = run_stage(ActStage(registry=make_registry(capability)), ctx)

        assert result.ok
        assert ctx.actions[0]["ok"] is False
        assert ctx.actions[0]["error"] == "RuntimeError: 接口挂了"
        assert ctx.notes[-1].startswith("没做成：")

    def test_a_capability_that_reports_failure_is_not_an_exception(self) -> None:
        capability = FakeCapability(
            result=CapabilityResult(ok=False, summary="配额用完了", error="quota")
        )
        ctx = make_context(chosen_intent=make_intent("post_moment"))

        run_stage(ActStage(registry=make_registry(capability)), ctx)

        assert ctx.actions[0]["ok"] is False
        assert ctx.actions[0]["error"] == "quota"

    def test_a_missing_capability_is_not_a_failure(self) -> None:
        """插件没装是正常状态，记一条「想做但做不了」比整轮失败有用。"""
        ctx = make_context(chosen_intent=make_intent("post_moment"))

        result = run_stage(ActStage(registry=ServiceRegistry()), ctx)

        assert result.ok
        assert ctx.actions[0]["ok"] is True
        assert ctx.actions[0]["capability"] == ""

    def test_the_action_carries_the_category_of_its_intent(self) -> None:
        ctx = make_context(chosen_intent=make_intent("work"))
        run_stage(ActStage(registry=ServiceRegistry()), ctx)
        assert ctx.actions[0]["category"] == "internal"


# ── 6. Express ──────────────────────────────────────────────


class TestExpressStage:
    def test_does_nothing_without_an_intent(self) -> None:
        ctx = make_context(llm_gateway=FakeGateway())
        assert run_stage(express(), ctx).changes == {}
        assert ctx.expressions == []

    def test_internal_intents_get_inner_voice_not_a_model_call(self) -> None:
        gateway = FakeGateway("不该被调到")
        ctx = make_context(chosen_intent=make_intent("work"), llm_gateway=gateway)

        run_stage(express(), ctx)

        assert gateway.calls == []
        assert ctx.expressions[0]["kind"] == "inner_voice"
        assert "推进手头的事" in str(ctx.expressions[0]["content"])

    def test_a_short_message_gets_an_instant_reply_decision(self) -> None:
        ctx = make_context(
            chosen_intent=make_intent("reply"),
            state=make_snapshot(last_inbound_text="在吗"),
            percepts=make_percepts(),
            llm_gateway=FakeGateway("在"),
        )

        run_stage(express(), ctx)

        assert ctx.expressions[0]["kind"] == "message"
        assert "随手就回了" in str(ctx.expressions[0]["trigger_note"])

    def test_a_long_message_is_not_answered_instantly(self) -> None:
        ctx = make_context(
            chosen_intent=make_intent("reply"),
            state=make_snapshot(last_inbound_text="今天下午那个方案你看了吗，我觉得第三部分要改"),
            percepts=make_percepts(),
            llm_gateway=FakeGateway("看了"),
        )

        run_stage(express(), ctx)

        assert "随手就回了" not in str(ctx.expressions[0]["trigger_note"])

    def test_an_exhausted_persona_refuses_this_round_and_records_why(self) -> None:
        """先问「回不回」再问「回什么」——顺序反了那次调用就白花了。"""
        gateway = FakeGateway("写了也白写")
        ctx = make_context(
            chosen_intent=make_intent("reply"),
            state=make_snapshot(last_inbound_text="在吗", emotion=make_emotion(fatigue=0.99)),
            percepts=make_percepts(),
            llm_gateway=gateway,
        )

        run_stage(express(), ctx)

        assert gateway.calls == []
        assert ctx.expressions == []
        assert ctx.suppressed[0]["intent"] == "reply"
        assert any("这一轮不回" in note for note in ctx.notes)

    def test_a_busy_block_pushes_the_reply_to_after_it_ends(self) -> None:
        ctx = make_context(
            chosen_intent=make_intent("reply"),
            state=make_snapshot(
                last_inbound_text="在吗",
                current_block=make_block(interruptible=False),
            ),
            percepts=make_percepts(),
            llm_gateway=FakeGateway("等会儿"),
        )

        run_stage(express(), ctx)

        deliver_at = ctx.expressions[0]["deliver_at"]
        assert isinstance(deliver_at, datetime)
        assert deliver_at > NOW

    def test_reaching_out_records_a_motivation(self) -> None:
        """「它为什么突然找我」必须能回答，而那一列就是答案。"""
        ctx = make_context(
            chosen_intent=make_intent("reach_out"),
            percepts=make_percepts(),
            llm_gateway=FakeGateway("在忙吗"),
        )

        run_stage(express(), ctx)

        motivation = str(ctx.expressions[0]["motivation"])
        assert motivation in REACH_OUT_MOTIVATIONS
        assert ctx.expressions[0]["deliver_at"] == NOW

    def test_posting_a_moment_produces_a_post_expression(self) -> None:
        ctx = make_context(
            chosen_intent=make_intent("post_moment"),
            percepts=make_percepts(),
            llm_gateway=FakeGateway("今天天气不错"),
        )

        run_stage(express(), ctx)

        assert ctx.expressions[0]["kind"] == "post"
        assert ctx.expressions[0]["content"] == "今天天气不错"

    def test_a_reply_gets_no_motivation(self) -> None:
        """回话没有「动机」——那一列是留给主动开口的，填上空串会让统计失真。"""
        ctx = make_context(
            chosen_intent=make_intent("reply"),
            state=make_snapshot(last_inbound_text="在吗"),
            percepts=make_percepts(),
            llm_gateway=FakeGateway("在"),
        )

        run_stage(express(), ctx)

        assert ctx.expressions[0]["motivation"] == ""

    def test_no_gateway_is_a_note_not_a_crash(self) -> None:
        ctx = make_context(chosen_intent=make_intent("reach_out"), percepts=make_percepts())

        result = run_stage(express(), ctx)

        assert result.ok
        assert any("没有装配模型" in note for note in ctx.notes)

    def test_a_model_error_becomes_a_note(self) -> None:
        ctx = make_context(
            chosen_intent=make_intent("reach_out"),
            percepts=make_percepts(),
            llm_gateway=FakeGateway(RuntimeError("502")),
        )

        result = run_stage(express(), ctx)

        assert result.ok
        assert any("没生成出来" in note for note in ctx.notes)

    def test_an_empty_answer_is_not_a_message(self) -> None:
        """空串发出去会变成一条空白消息，比不发更糟。"""
        ctx = make_context(
            chosen_intent=make_intent("reach_out"),
            percepts=make_percepts(),
            llm_gateway=FakeGateway("   "),
        )

        run_stage(express(), ctx)

        assert ctx.expressions == []
        assert any("模型返回了空内容" in note for note in ctx.notes)

    def test_the_reply_template_gets_every_placeholder(self) -> None:
        """占位符多一个少一个 ``PromptLibrary.render`` 都会抛错。"""
        ctx = make_context(
            chosen_intent=make_intent("reply"),
            state=make_snapshot(
                last_inbound_text="在吗",
                recent_memories=(FakeMemory(),),
                recent_messages=(
                    make_message(content="在吗"),
                    make_message(id="m2", direction="outbound", content="在"),
                ),
            ),
            percepts=make_percepts(),
            llm_gateway=FakeGateway("在"),
        )

        run_stage(express(), ctx)

        assert ctx.expressions

    def test_the_transcript_labels_both_sides(self) -> None:
        ctx = make_context(
            state=make_snapshot(
                recent_messages=(
                    make_message(content="在吗"),
                    make_message(id="m2", direction="outbound", content="在"),
                )
            )
        )
        rendered = render_chat(express(), ctx)
        assert "对方：在吗" in rendered
        assert "我：在" in rendered

    def test_an_empty_conversation_says_so_instead_of_rendering_nothing(self) -> None:
        ctx = make_context(state=make_snapshot(recent_messages=()))
        assert "（还没有聊过）" in render_chat(express(), ctx)

    def test_memories_are_rendered_as_a_list(self) -> None:
        ctx = make_context(state=make_snapshot(recent_memories=(FakeMemory("看过展"),)))
        assert "- 看过展" in render_chat(express(), ctx)

    def test_no_memories_says_so(self) -> None:
        ctx = make_context(state=make_snapshot(recent_memories=()))
        assert "（想不起来什么特别的）" in render_chat(express(), ctx)

    def test_since_last_talk_reads_the_percepts(self) -> None:
        ctx = make_context(percepts=make_percepts(minutes=5.0))
        assert "5 分钟前" in render_chat(express(), ctx)

    def test_since_last_talk_handles_just_now_and_hours(self) -> None:
        just_now = make_context(percepts=make_percepts(minutes=0.2))
        assert "刚刚" in render_chat(express(), just_now)

        hours = make_context(percepts=make_percepts(minutes=120.0))
        assert "2.0 小时前" in render_chat(express(), hours)

    def test_a_stranger_gets_a_clear_answer(self) -> None:
        ctx = make_context(percepts=make_percepts(minutes=None))
        assert "还没有聊过" in render_chat(express(), ctx)

    def test_a_missing_persona_document_does_not_produce_an_empty_prompt(self) -> None:
        """``{persona}`` 空掉会让模型自己编一个人格，而且每轮编得都不一样。"""
        ctx = make_context(state=make_snapshot(persona=make_persona(summary="")))
        assert "（人设还没生成" in render_chat(express(), ctx)

    def test_the_users_name_is_used_when_known(self) -> None:
        assert "小陈" in render_chat(express(), make_context())

    def test_an_unknown_user_is_still_addressable(self) -> None:
        ctx = make_context(state=make_snapshot(persona=make_persona(user_name="")))
        assert "你" in render_chat(express(), ctx)


class TestExpressionStyle:
    def test_the_default_is_not_a_customer_service_voice(self) -> None:
        """没有这一串默认值，模型会默认写出一封得体的商务邮件。"""
        assert "客服" in ExpressionStyle().tone

    def test_the_style_can_be_read_from_the_persona_document(self) -> None:
        style = ExpressionStyle.from_persona(make_persona(expression={"tone": "很冷淡"}))
        assert style.tone == "很冷淡"
        assert style.verbosity == ExpressionStyle().verbosity

    def test_junk_in_the_document_does_not_reach_the_prompt(self) -> None:
        """猜不到就保持默认，而不是把 ``None`` 塞进提示词。"""
        style = ExpressionStyle.from_persona(
            make_persona(expression={"tone": None, "verbosity": "  ", "emoji_habit": 3})
        )
        assert style == ExpressionStyle()

    def test_a_persona_without_an_expression_block_is_fine(self) -> None:
        assert ExpressionStyle.from_persona(make_persona()) == ExpressionStyle()

    def test_a_persona_without_any_expression_attribute_is_fine(self) -> None:
        assert ExpressionStyle.from_persona(object()) == ExpressionStyle()


# ── 7. Persist ──────────────────────────────────────────────


class TestPersistStage:
    def test_an_action_becomes_one_activity_row(self) -> None:
        ctx = make_context(chosen_intent=make_intent("work"))
        ctx.actions.append(
            {"intent": "work", "category": "internal", "summary": "推进手头的事", "ok": True}
        )

        result = run_stage(persist(), ctx)

        assert result.ok
        assert len(ctx.activity_records) == 1
        record = ctx.activity_records[0]
        assert record.id == "t1-a0"
        assert record.category == "internal"
        assert record.detail == {"ok": True}
        assert record.tick_id == "t1"

    def test_inner_voice_is_attached_to_its_action(self) -> None:
        """「在做什么」和「当时怎么想」是同一个瞬间的两面，不该分成两行。"""
        ctx = make_context()
        ctx.actions.append({"intent": "work", "category": "internal", "summary": "写代码"})
        ctx.expressions.append({"kind": "inner_voice", "intent": "work", "content": "这段逻辑真绕"})

        run_stage(persist(), ctx)

        assert len(ctx.activity_records) == 1
        assert ctx.activity_records[0].inner_voice == "这段逻辑真绕"

    def test_a_suppressed_intent_leaves_a_row(self) -> None:
        """用户问「它明明想找我说话怎么没发」，答案必须是一条记录。"""
        ctx = make_context()
        ctx.suppressed.append({"intent": "reach_out", "reason": "免打扰时段"})

        run_stage(persist(), ctx)

        rows = [record for record in ctx.activity_records if record.suppressed_intent]
        assert len(rows) == 1
        assert rows[0].id == "t1-s0"
        assert rows[0].category == "outbound"
        assert rows[0].suppress_reason == "免打扰时段"

    def test_suppressed_objects_work_the_same_as_dicts(self) -> None:
        @dataclass
        class Item:
            intent: str = "post_moment"
            reason: str = "配额用完了"

        ctx = make_context()
        ctx.suppressed.append(Item())

        run_stage(persist(), ctx)

        assert ctx.activity_records[0].suppress_reason == "配额用完了"

    def test_messages_need_a_conversation_id(self) -> None:
        """拿不到会话 id 时宁可不写，也不要写进一个没人读的会话。"""
        ctx = make_context(state=make_snapshot(recent_messages=()))
        ctx.expressions.append({"kind": "message", "intent": "reach_out", "content": "在忙吗"})

        run_stage(persist(), ctx)

        assert not ctx.message_records

    def test_the_conversation_id_comes_from_the_recent_messages(self) -> None:
        """会话 id 的生成规则属于组装根，阶段不该再实现一遍。"""
        ctx = make_context(state=make_snapshot(recent_messages=(make_message(),)))
        ctx.expressions.append({"kind": "message", "intent": "reply", "content": "在"})

        run_stage(persist(), ctx)

        assert ctx.message_records[0].conversation_id == "p1:user"

    def test_initiative_is_reserved_for_reaching_out(self) -> None:
        ctx = make_context(state=make_snapshot(recent_messages=(make_message(),)))
        ctx.expressions.append({"kind": "message", "intent": "reach_out", "content": "在忙吗"})
        ctx.expressions.append({"kind": "message", "intent": "reply", "content": "在"})

        run_stage(persist(), ctx)

        initiative = {record.content: record.initiative for record in ctx.message_records}
        assert initiative == {"在忙吗": True, "在": False}

    def test_a_post_becomes_a_social_post_row(self) -> None:
        ctx = make_context(
            state=make_snapshot(current_block=make_block(), recent_messages=(make_message(),))
        )
        ctx.actions.append({"intent": "post_moment", "category": "outbound", "summary": "发动态"})
        ctx.expressions.append({"kind": "post", "intent": "post_moment", "content": "天气不错"})

        run_stage(persist(), ctx)

        assert len(ctx.post_records) == 1
        post = ctx.post_records[0]
        assert post.id == "t1-p0"
        assert post.content == "天气不错"
        assert post.location == "公司"
        assert post.tick_id == "t1"
        assert post.activity_ref == "t1-a0"
        assert ctx.state.emotion is not None
        assert post.mood_label == ctx.state.emotion.label

    def test_a_post_without_a_matching_activity_leaves_the_reference_empty(self) -> None:
        """编一个不存在的 id 会让动态详情页报一个查不到的外键。"""
        ctx = make_context(state=make_snapshot(recent_messages=(make_message(),)))
        ctx.expressions.append({"kind": "post", "intent": "post_moment", "content": "天气不错"})

        run_stage(persist(), ctx)

        assert ctx.post_records[0].activity_ref == ""

    def test_a_post_keeps_the_mood_of_the_moment(self) -> None:
        """情绪曲线会继续演化，而「这条动态是在什么心情下发的」必须停在这一刻。"""
        ctx = make_context(state=make_snapshot(emotion=None, recent_messages=(make_message(),)))
        ctx.expressions.append({"kind": "post", "intent": "post_moment", "content": "天气不错"})

        run_stage(persist(), ctx)

        assert ctx.post_records[0].mood_valence is None
        assert ctx.post_records[0].mood_arousal is None

    def test_sent_messages_are_counted(self) -> None:
        ctx = make_context(state=make_snapshot(recent_messages=(make_message(),)))
        ctx.expressions.append({"kind": "message", "intent": "reach_out", "content": "在忙吗"})

        run_stage(persist(), ctx)

        assert ctx.state.budget is not None
        assert ctx.state.budget.messages_sent == 1
        assert ctx.state.budget.last_message_at == NOW

    def test_sent_posts_are_counted(self) -> None:
        ctx = make_context(state=make_snapshot(recent_messages=(make_message(),)))
        ctx.expressions.append({"kind": "post", "intent": "post_moment", "content": "天气不错"})

        run_stage(persist(), ctx)

        assert ctx.state.budget is not None
        assert ctx.state.budget.posts_sent == 1
        assert ctx.state.budget.last_post_at == NOW

    def test_a_reply_clears_the_no_reply_streak(self) -> None:
        """熔断是给「一直被无视」用的，不是给「偶尔晚回一次」用的。"""
        usage = BudgetUsage(day=DAY, consecutive_no_reply=2, messages_sent=1)
        ctx = make_context(state=make_snapshot(budget=usage, recent_messages=(make_message(),)))
        ctx.expressions.append({"kind": "message", "intent": "reply", "content": "在"})

        run_stage(persist(), ctx)

        assert ctx.state.budget is not None
        assert ctx.state.budget.consecutive_no_reply == 0

    def test_not_replying_while_the_user_waits_is_recorded_not_punished(self) -> None:
        usage = BudgetUsage(day=DAY)
        ctx = make_context(
            state=make_snapshot(budget=usage, unread_messages=1, recent_messages=(make_message(),))
        )

        run_stage(persist(), ctx)

        assert ctx.state.budget is not None
        assert ctx.state.budget.consecutive_no_reply == 1

    def test_silence_is_not_counted_when_nobody_is_waiting(self) -> None:
        usage = BudgetUsage(day=DAY)
        ctx = make_context(state=make_snapshot(budget=usage, unread_messages=0))

        run_stage(persist(), ctx)

        assert ctx.state.budget is not None
        assert ctx.state.budget.consecutive_no_reply == 0

    def test_no_budget_snapshot_means_no_accounting(self) -> None:
        """凭空造一个零用量的对象会让熔断看起来像已经解除。"""
        ctx = make_context(state=make_snapshot(budget=None, recent_messages=(make_message(),)))
        ctx.expressions.append({"kind": "message", "intent": "reach_out", "content": "在忙吗"})

        run_stage(persist(), ctx)

        assert ctx.state.budget is None
        assert "记账跳过" in "".join(ctx.notes)

    def test_a_stale_budget_day_is_not_carried_forward(self) -> None:
        ctx = make_context(
            state=make_snapshot(budget=BudgetUsage(day=date(2020, 1, 1), messages_sent=9))
        )
        run_stage(persist(), ctx)
        assert ctx.state.budget is None

    def test_the_result_carries_all_three_kinds_of_rows(self) -> None:
        ctx = make_context(state=make_snapshot(recent_messages=(make_message(),)))
        ctx.expressions.append({"kind": "post", "intent": "post_moment", "content": "天气不错"})

        result = run_stage(persist(), ctx)

        assert set(result.changes) == {
            "activity_records",
            "message_records",
            "post_records",
            "state",
        }

    def test_a_delivery_time_that_is_not_a_datetime_falls_back_to_now(self) -> None:
        ctx = make_context(state=make_snapshot(recent_messages=(make_message(),)))
        ctx.expressions.append(
            {"kind": "message", "intent": "reply", "content": "在", "deliver_at": "十六点"}
        )

        run_stage(persist(), ctx)

        assert ctx.message_records[0].created_at == NOW


# ── 舞台之外 ────────────────────────────────────────────────


class TestBaselineEmotion:
    def test_the_label_matches_the_numbers(self) -> None:
        emotion = baseline_emotion(NOW)
        assert emotion.label == infer_label(emotion.valence, emotion.arousal)

    def test_it_starts_unfatigued_at_the_given_moment(self) -> None:
        emotion = baseline_emotion(NOW)
        assert emotion.fatigue == 0.0
        assert emotion.updated_at == NOW


class TestReflectionInterval:
    def test_the_interval_is_a_plain_constant_for_now(self) -> None:
        """这条常量还没接线：现在只在「有事发生」时反思。留着它是为了别忘。"""
        assert REFLECT_INTERVAL_MINUTES == 120.0

"""``alterego.sim.engine`` 的测试。

引擎是唯一同时看得见「数据库」和「推演」的地方，所以这里测的不是业务规则，
而是**两条边界**：

1. **一次 tick 的顺序与失败传播**——阶段失败不能悄悄变成「这轮什么都没发生」，
   依赖它的阶段必须被跳过而不是硬跑。
2. **一次 tick 只开一个事务**——半条消息比没有消息更难解释。

装配用的全是假的仓储与假的时钟：真的 SQLite 已经在
``tests/test_storage_repositories.py`` 里被验过了，在这里再验一遍只会让
「引擎的顺序错了」被一个「SQL 错了」的报错盖住。真的提示词库
（``PromptLibrary()``）只在 :func:`default_stages` 那一个测试里用到。

依据: docs/design/04-simulation-loop.md § 2、§ 3
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from alterego.domain.emotion import Emotion
from alterego.interfaces.repository import (
    BudgetUsage,
    ConversationRecord,
    MessageRecord,
    ScheduleRecord,
    SocialPostRecord,
    TickLogDraft,
)
from alterego.interfaces.simulation import Stage, StageResult
from alterego.kernel.registry import ServiceRegistry
from alterego.llm.prompts import PromptLibrary
from alterego.sim.context import StateSnapshot, TickContext
from alterego.sim.engine import (
    EnginePorts,
    SimulationEngine,
    TickOutcome,
    default_stages,
    to_schedule_block,
    user_conversation_id,
)
from alterego.sim.persona_view import PersonaView


NOW = datetime(2026, 9, 15, 14, 0)
DAY = date(2026, 9, 15)
PERSONA_ID = "p1"


# ── 假的协作者 ──────────────────────────────────────────────


class FakeClock:
    """虚拟时钟。引擎只调 ``virtual_now()``。"""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def virtual_now(self) -> datetime:
        return self.now


class FakeBackend:
    """只提供事务。``fail=True`` 时开事务就炸。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.transactions = 0
        self.fail = fail

    def transaction(self) -> Any:
        self.transactions += 1
        if self.fail:
            raise RuntimeError("磁盘满了")
        return nullcontext()


class FakePersonas:
    def __init__(self, record: Any = None, document: Mapping[str, Any] | None = None) -> None:
        self.record = record
        self.doc = dict(document or {})
        self.asked: list[str] = []

    def get(self, persona_id: str) -> Any:
        return self.record

    def find_by_name(self, name: str) -> Any:
        return self.record

    def list_all(self) -> list[Any]:
        return [] if self.record is None else [self.record]

    def document(self, persona_id: str) -> dict[str, Any]:
        self.asked.append(persona_id)
        return dict(self.doc)


class FakeEmotions:
    def __init__(self, current: Emotion | None = None) -> None:
        self.current = current
        self.written: list[tuple[str, Emotion, str, tuple[str, ...], str]] = []

    def latest(self, persona_id: str) -> Emotion | None:
        return self.current

    def append(
        self,
        persona_id: str,
        emotion: Emotion,
        *,
        reason: str = "",
        causes: Sequence[str] = (),
        tick_id: str = "",
    ) -> None:
        self.written.append((persona_id, emotion, reason, tuple(causes), tick_id))


class FakeSchedules:
    def __init__(self, rows: Sequence[ScheduleRecord] = ()) -> None:
        self.rows = list(rows)

    def list_day(self, persona_id: str, *, day: date) -> list[ScheduleRecord]:
        return [row for row in self.rows if row.day == day]

    def list_range(self, persona_id: str, *, since: datetime, until: datetime, limit: int = 500):
        return list(self.rows)


class FakeMemories:
    def __init__(self, summaries: Sequence[str] = ()) -> None:
        self.rows = [SimpleNamespace(summary=text) for text in summaries]
        self.queries: list[dict[str, Any]] = []

    def list_recent(self, persona_id: str, *, since: datetime, limit: int = 100) -> list[Any]:
        self.queries.append({"since": since, "limit": limit})
        return self.rows[:limit]


class FakeActivities:
    def __init__(self, rows: Sequence[Any] = ()) -> None:
        self.rows = list(rows)
        self.written: list[Any] = []
        self.queries: list[dict[str, Any]] = []

    def list_range(self, persona_id: str, *, since: datetime, until: datetime, limit: int = 500):
        self.queries.append({"since": since, "until": until, "limit": limit})
        return list(self.rows)

    def append(self, records: Sequence[Any]) -> None:
        self.written.extend(records)


class FakeConversations:
    def __init__(
        self,
        *,
        messages: Sequence[MessageRecord] = (),
        unread: int = 0,
        last_inbound: datetime | None = None,
    ) -> None:
        self.messages = list(messages)
        self.unread = unread
        self.last_inbound = last_inbound
        self.written: list[MessageRecord] = []
        self.ensured: list[ConversationRecord] = []

    def ensure(self, record: ConversationRecord) -> ConversationRecord:
        self.ensured.append(record)
        return record

    def append(self, message: MessageRecord) -> None:
        self.written.append(message)

    def list_messages(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[MessageRecord]:
        return self.messages[-limit:]

    def count_unread(self, conversation_id: str) -> int:
        return self.unread

    def last_inbound_at(self, conversation_id: str) -> datetime | None:
        return self.last_inbound


class FakeBudgets:
    def __init__(self) -> None:
        self.saved: list[tuple[str, BudgetUsage]] = []
        self.asked: list[date] = []

    def load(self, persona_id: str, *, day: date) -> BudgetUsage:
        self.asked.append(day)
        return BudgetUsage(day=day)

    def save(self, persona_id: str, usage: BudgetUsage) -> None:
        self.saved.append((persona_id, usage))


class FakePosts:
    def __init__(self) -> None:
        self.written: list[SocialPostRecord] = []

    def append(self, post: SocialPostRecord) -> None:
        self.written.append(post)

    def list_recent(self, persona_id: str, *, limit: int = 30, visible_only: bool = True):
        return self.written


class FakeTickLogs:
    def __init__(self) -> None:
        self.drafts: list[TickLogDraft] = []

    def append(self, draft: TickLogDraft) -> None:
        self.drafts.append(draft)


def make_ports(**overrides: Any) -> EnginePorts:
    """十张嘴都换成假的。想换哪个就在 ``overrides`` 里点名。"""
    values: dict[str, Any] = {
        "backend": FakeBackend(),
        "personas": FakePersonas(),
        "emotions": FakeEmotions(),
        "schedules": FakeSchedules(),
        "memories": FakeMemories(),
        "activities": FakeActivities(),
        "conversations": FakeConversations(),
        "budgets": FakeBudgets(),
        "posts": FakePosts(),
        "tick_logs": FakeTickLogs(),
    }
    values.update(overrides)
    return EnginePorts(**values)


# ── 假的阶段 ────────────────────────────────────────────────


@dataclass(slots=True)
class FakeStage:
    """一个能记下「我跑过」的阶段。"""

    name: str
    order: int
    depends_on: tuple[str, ...] = ()
    enabled: bool = True
    changes: Mapping[str, Any] | None = None
    boom: Exception | None = None
    ok: bool = True
    log: list[str] | None = None
    capture: Any = None

    async def run(self, ctx: TickContext) -> StageResult:
        if self.log is not None:
            self.log.append(self.name)
        if self.capture is not None:
            self.capture(ctx)
        if self.boom is not None:
            raise self.boom
        if not self.ok:
            return StageResult(ok=False, error=RuntimeError("故意的"))
        return StageResult(ok=True, changes=dict(self.changes or {}))


def make_engine(
    *,
    stages: Sequence[Stage] = (),
    ports: EnginePorts | None = None,
    persona: PersonaView | None = None,
    clock: Any = None,
    gateway: Any = None,
    plugins: Any = None,
    **kwargs: Any,
) -> SimulationEngine:
    return SimulationEngine(
        persona=persona or PersonaView(id=PERSONA_ID, name="林晚"),
        ports=ports or make_ports(),
        stages=stages,
        clock=clock or FakeClock(),
        gateway=gateway,
        plugins=plugins,
        **kwargs,
    )


class FakePlugins:
    """只记下钩子被调用的顺序。"""

    def __init__(self, log: list[str], *, boom: bool = False) -> None:
        self.log = log
        self.boom = boom

    def tick_pre(self, ctx: Any) -> None:
        self.log.append("tick_pre")
        if self.boom:
            raise RuntimeError("前置钩子炸了")

    def tick_post(self, ctx: Any) -> None:
        self.log.append("tick_post")


# ── 顺序与失败传播 ──────────────────────────────────────────


class TestStageOrder:
    async def test_stages_run_in_order(self) -> None:
        log: list[str] = []
        engine = make_engine(
            stages=[
                FakeStage(name="c", order=30, log=log),
                FakeStage(name="a", order=10, log=log),
                FakeStage(name="b", order=20, log=log),
            ]
        )
        outcome = await engine.tick()
        assert log == ["a", "b", "c"]
        assert outcome.ran == ("a", "b", "c")

    async def test_same_order_is_broken_by_name(self) -> None:
        """同序时按名字排。**确定性不是细节**——不然同一份世界跑两次会不一样。"""
        log: list[str] = []
        engine = make_engine(
            stages=[
                FakeStage(name="zeta", order=20, log=log),
                FakeStage(name="alpha", order=20, log=log),
            ]
        )
        await engine.tick()
        assert log == ["alpha", "zeta"]

    async def test_disabled_stage_is_skipped(self) -> None:
        log: list[str] = []
        engine = make_engine(
            stages=[
                FakeStage(name="on", order=10, log=log),
                FakeStage(name="off", order=20, enabled=False, log=log),
            ]
        )
        outcome = await engine.tick()
        assert log == ["on"]
        assert outcome.skipped == ("off",)

    async def test_failed_stage_is_not_marked_as_ran(self) -> None:
        engine = make_engine(stages=[FakeStage(name="sad", order=10, boom=RuntimeError("哐"))])
        outcome = await engine.tick()
        assert outcome.ran == ()
        assert outcome.skipped == ()
        assert outcome.status == "failed"
        assert any("sad 炸了" in text for text in outcome.errors)

    async def test_not_ok_result_is_an_error_but_still_counts_as_ran(self) -> None:
        engine = make_engine(stages=[FakeStage(name="meh", order=10, ok=False)])
        outcome = await engine.tick()
        assert outcome.ran == ("meh",)  # 它跑了，只是没跑成
        assert outcome.status == "failed"
        assert any("meh 没跑成" in text for text in outcome.errors)

    async def test_dependant_of_a_broken_stage_is_skipped(self) -> None:
        log: list[str] = []
        engine = make_engine(
            stages=[
                FakeStage(name="sense", order=10, boom=RuntimeError("没网"), log=log),
                FakeStage(name="reflect", order=20, depends_on=("sense",), log=log),
                FakeStage(name="persist", order=110, log=log),
            ]
        )
        outcome = await engine.tick()
        # 「没跑成」与「被跳过」是两回事：前者去查那个阶段的日志，后者查依赖。
        assert log == ["sense", "persist"]
        assert outcome.ran == ("persist",)
        assert outcome.skipped == ("reflect",)
        assert any("跳过了 reflect" in note for note in outcome.notes)

    async def test_partial_when_persist_survived(self) -> None:
        outcome = await make_engine(
            stages=[
                FakeStage(name="express", order=90, boom=RuntimeError("模型超时")),
                FakeStage(name="persist", order=110),
            ]
        ).tick()
        assert outcome.status == "partial"
        assert not outcome.ok

    async def test_ok_when_nothing_went_wrong(self) -> None:
        outcome = await make_engine(stages=[FakeStage(name="persist", order=110)]).tick()
        assert outcome.status == "ok"
        assert outcome.ok


class TestChangeMerging:
    async def test_state_change_replaces_the_snapshot(self) -> None:
        seen: list[int] = []
        engine = make_engine(
            stages=[
                FakeStage(
                    name="sense",
                    order=10,
                    changes={"state": StateSnapshot(persona=object(), unread_messages=7)},
                ),
                FakeStage(
                    name="reader",
                    order=20,
                    capture=lambda ctx: seen.append(ctx.state.unread_messages),
                ),
            ]
        )
        await engine.tick()
        assert seen == [7]

    async def test_change_lists_land_on_the_context(self) -> None:
        seen: list[list[str]] = []
        engine = make_engine(
            stages=[
                FakeStage(name="a", order=10, changes={"post_records": ["一条动态"]}),
                FakeStage(
                    name="b", order=20, capture=lambda ctx: seen.append(list(ctx.post_records))
                ),
            ]
        )
        await engine.tick()
        assert seen == [["一条动态"]]

    async def test_unknown_change_key_is_ignored(self) -> None:
        """插件会带自己的键（比如 ``weather``），引擎不认识也不能炸。"""
        engine = make_engine(stages=[FakeStage(name="x", order=10, changes={"weather": "晴"})])
        outcome = await engine.tick()
        assert outcome.status == "ok"


# ── 落库 ────────────────────────────────────────────────────


class TestCommit:
    def _engine(self, **overrides: Any) -> tuple[SimulationEngine, EnginePorts]:
        ports = make_ports(**overrides)
        return make_engine(ports=ports), ports

    async def test_one_transaction_per_tick(self) -> None:
        engine, ports = self._engine()
        await engine.tick()
        assert ports.backend.transactions == 1  # type: ignore[attr-defined]

    async def test_tick_log_is_written(self) -> None:
        engine, ports = self._engine()
        outcome = await engine.tick()
        drafts = ports.tick_logs.drafts  # type: ignore[attr-defined]
        assert len(drafts) == 1
        assert drafts[0].id == outcome.tick_id
        assert drafts[0].persona_id == PERSONA_ID

    async def test_budget_is_persisted(self) -> None:
        engine, ports = self._engine()
        await engine.tick()
        saved = ports.budgets.saved  # type: ignore[attr-defined]
        assert len(saved) == 1
        assert saved[0][1].day == DAY

    async def test_emotion_is_persisted_with_its_reason(self) -> None:
        emotion = Emotion(
            valence=0.4,
            arousal=0.5,
            fatigue=0.1,
            label="愉悦",
            updated_at=NOW,
        )
        ports = make_ports(emotions=FakeEmotions(current=emotion))
        engine = make_engine(ports=ports)
        await engine.tick()
        written = ports.emotions.written  # type: ignore[attr-defined]
        assert len(written) == 1
        assert written[0][1] is emotion
        assert written[0][4].startswith("tick-")

    async def test_no_emotion_record_means_nothing_written(self) -> None:
        """没有情绪快照就不写——写一条凭空造出来的基线会污染那条曲线。"""
        engine, ports = self._engine()
        await engine.tick()
        assert ports.emotions.written == []  # type: ignore[attr-defined]

    async def test_messages_get_the_conversation_id(self) -> None:
        message = MessageRecord(
            id="m1",
            conversation_id="",
            direction="outbound",
            sender_id=PERSONA_ID,
            content="在忙吗",
        )
        ports = make_ports()
        engine = make_engine(
            ports=ports,
            stages=[FakeStage(name="persist", order=110, changes={"message_records": [message]})],
        )
        await engine.tick()
        written = ports.conversations.written  # type: ignore[attr-defined]
        assert [item.conversation_id for item in written] == [user_conversation_id(PERSONA_ID)]

    async def test_conversation_is_ensured(self) -> None:
        engine, ports = self._engine()
        await engine.tick()
        ensured = ports.conversations.ensured  # type: ignore[attr-defined]
        assert [record.counterpart_kind for record in ensured] == ["user"]

    async def test_write_failure_turns_the_tick_into_failed(self) -> None:
        ports = make_ports(backend=FakeBackend(fail=True))
        outcome = await make_engine(ports=ports).tick()
        assert outcome.status == "failed"
        assert any("写库失败" in text for text in outcome.errors)

    async def test_tick_log_survives_a_write_failure(self) -> None:
        """日志是事后查「它当时干了什么」的唯一线索，不能跟着一起丢。"""
        ports = make_ports(backend=FakeBackend(fail=True))
        outcome = await make_engine(ports=ports).tick()
        assert ports.tick_logs.drafts == []  # type: ignore[attr-defined]
        assert outcome.tick_id  # 至少 id 还在返回值里


# ── 读快照 ──────────────────────────────────────────────────


class TestSnapshot:
    async def test_budget_is_asked_for_the_current_day(self) -> None:
        ports = make_ports()
        await make_engine(ports=ports).tick()
        assert ports.budgets.asked == [DAY]  # type: ignore[attr-defined]

    async def test_current_block_comes_from_the_today_rows(self) -> None:
        row = ScheduleRecord(
            id="s1",
            day=DAY,
            start_at=datetime(2026, 9, 15, 13, 0),
            end_at=datetime(2026, 9, 15, 15, 0),
            activity="写代码",
            category="work",
        )
        engine = make_engine(ports=make_ports(schedules=FakeSchedules([row])))
        state = engine._snapshot(NOW)
        assert state.current_block is not None
        assert state.current_block.activity == "写代码"

    async def test_a_block_that_does_not_cover_now_is_not_current(self) -> None:
        row = ScheduleRecord(
            id="s1",
            day=DAY,
            start_at=datetime(2026, 9, 15, 8, 0),
            end_at=datetime(2026, 9, 15, 9, 0),
            activity="吃早饭",
        )
        engine = make_engine(ports=make_ports(schedules=FakeSchedules([row])))
        assert engine._snapshot(NOW).current_block is None

    async def test_missing_schedule_is_not_an_error(self) -> None:
        assert make_engine()._snapshot(NOW).current_block is None

    async def test_last_inbound_text_skips_our_own_messages(self) -> None:
        messages = [
            MessageRecord(
                id="m1", conversation_id="c", direction="inbound", sender_id="user", content="在吗"
            ),
            MessageRecord(
                id="m2",
                conversation_id="c",
                direction="outbound",
                sender_id=PERSONA_ID,
                content="在",
            ),
        ]
        engine = make_engine(ports=make_ports(conversations=FakeConversations(messages=messages)))
        assert engine._snapshot(NOW).last_inbound_text == "在吗"

    async def test_unread_and_last_inbound_come_from_the_repository(self) -> None:
        ports = make_ports(conversations=FakeConversations(unread=2, last_inbound=NOW))
        state = make_engine(ports=ports)._snapshot(NOW)
        assert state.unread_messages == 2
        assert state.last_user_message_at == NOW

    async def test_memory_query_uses_the_configured_window(self) -> None:
        ports = make_ports(memories=FakeMemories(["上周搬了家"]))
        state = make_engine(ports=ports, memory_limit=3, memory_days=7)._snapshot(NOW)
        query = ports.memories.queries[-1]  # type: ignore[attr-defined]
        assert query["limit"] == 3
        assert query["since"] == datetime(2026, 9, 8, 14, 0)
        assert state.recent_memories[0].summary == "上周搬了家"

    async def test_persona_is_the_view_it_was_given(self) -> None:
        view = PersonaView(id=PERSONA_ID, name="林晚", summary="我是个算法工程师")
        state = make_engine(persona=view)._snapshot(NOW)
        assert state.persona is view

    async def test_instant_streak_counts_consecutive_quick_replies(self) -> None:
        """连着秒回几条是 ``decide_reply`` 的输入，不是统计指标。"""
        messages = [
            MessageRecord(
                id="m1",
                conversation_id="c",
                direction="inbound",
                sender_id="user",
                content="在吗",
                created_at=datetime(2026, 9, 15, 10, 0, 0),
            ),
            MessageRecord(
                id="m2",
                conversation_id="c",
                direction="outbound",
                sender_id=PERSONA_ID,
                content="在",
                created_at=datetime(2026, 9, 15, 10, 0, 10),
            ),
            MessageRecord(
                id="m3",
                conversation_id="c",
                direction="inbound",
                sender_id="user",
                content="忙什么",
                created_at=datetime(2026, 9, 15, 10, 1, 0),
            ),
            MessageRecord(
                id="m4",
                conversation_id="c",
                direction="outbound",
                sender_id=PERSONA_ID,
                content="写东西",
                created_at=datetime(2026, 9, 15, 10, 1, 20),
            ),
        ]
        engine = make_engine(ports=make_ports(conversations=FakeConversations(messages=messages)))
        assert engine._snapshot(NOW).consecutive_instant_replies == 2

    async def test_a_slow_reply_breaks_the_streak(self) -> None:
        messages = [
            MessageRecord(
                id="m1",
                conversation_id="c",
                direction="inbound",
                sender_id="user",
                content="在吗",
                created_at=datetime(2026, 9, 15, 10, 0, 0),
            ),
            MessageRecord(
                id="m2",
                conversation_id="c",
                direction="outbound",
                sender_id=PERSONA_ID,
                content="刚看到",
                created_at=datetime(2026, 9, 15, 10, 30, 0),
            ),
        ]
        engine = make_engine(ports=make_ports(conversations=FakeConversations(messages=messages)))
        assert engine._snapshot(NOW).consecutive_instant_replies == 0

    async def test_a_trailing_outbound_before_any_inbound_is_not_a_streak(self) -> None:
        messages = [
            MessageRecord(
                id="m1",
                conversation_id="c",
                direction="outbound",
                sender_id=PERSONA_ID,
                content="今天下雨了",
                created_at=NOW,
            ),
        ]
        engine = make_engine(ports=make_ports(conversations=FakeConversations(messages=messages)))
        assert engine._snapshot(NOW).consecutive_instant_replies == 0


# ── 可复现 ──────────────────────────────────────────────────


class TestReproducibility:
    async def test_the_same_moment_gives_a_reproducible_tick_id(self) -> None:
        first = await make_engine().tick()
        second = await make_engine().tick()
        assert first.tick_id == second.tick_id

    async def test_a_new_id_can_be_injected(self) -> None:
        counter = iter(["t-1", "t-2"])
        engine = make_engine(new_id=lambda: next(counter))
        assert (await engine.tick()).tick_id == "t-1"
        assert (await engine.tick()).tick_id == "t-2"

    async def test_the_rng_is_reproducible(self) -> None:
        engine = make_engine()
        assert engine._rng(NOW).random() == engine._rng(NOW).random()

    async def test_a_fixed_seed_overrides_the_derived_one(self) -> None:
        derived = make_engine()._rng(NOW).random()
        seeded = make_engine(seed="fixed")._rng(NOW).random()
        assert derived != seeded

    async def test_two_engines_do_not_share_state(self) -> None:
        first, second = make_engine(), make_engine()
        assert first._rng(NOW) is not second._rng(NOW)


# ── 插件钩子 ────────────────────────────────────────────────


class TestPlugins:
    async def test_hooks_wrap_the_stages(self) -> None:
        log: list[str] = []
        engine = make_engine(
            stages=[FakeStage(name="middle", order=50, log=log)],
            plugins=FakePlugins(log),
        )
        await engine.tick()
        assert log == ["tick_pre", "middle", "tick_post"]

    async def test_a_broken_hook_does_not_stop_the_tick(self) -> None:
        log: list[str] = []
        engine = make_engine(
            stages=[FakeStage(name="persist", order=110, log=log)],
            plugins=FakePlugins(log, boom=True),
        )
        outcome = await engine.tick()
        assert outcome.status == "partial"
        assert any("tick_pre 炸了" in text for text in outcome.errors)

    async def test_no_plugin_manager_is_fine(self) -> None:
        outcome = await make_engine(stages=[FakeStage(name="persist", order=110)]).tick()
        assert outcome.status == "ok"


# ── 组装 ────────────────────────────────────────────────────


class TestDefaultStages:
    def test_six_builtins_in_order(self) -> None:
        stages = default_stages(
            registry=ServiceRegistry(),
            budget=SimpleNamespace(),
            prompts=PromptLibrary(),
        )
        assert [(stage.name, stage.order) for stage in stages] == [
            ("sense", 10),
            ("reflect", 30),
            ("intention", 50),
            ("act", 70),
            ("express", 90),
            ("persist", 110),
        ]

    def test_plugin_stages_join_the_same_list(self) -> None:
        registry = ServiceRegistry()
        plugin_stage = FakeStage(name="weather", order=20)
        registry.register(Stage, plugin_stage, name="weather", owner="demo.weather")
        stages = default_stages(
            registry=registry,
            budget=SimpleNamespace(),
            prompts=PromptLibrary(),
        )
        assert "weather" in [stage.name for stage in stages]

    async def test_a_broken_schedule_row_is_dropped_not_fatal(self) -> None:
        row = ScheduleRecord(
            id="s1",
            day=DAY,
            start_at=datetime(2026, 9, 15, 15, 0),
            end_at=datetime(2026, 9, 15, 13, 0),  # 结束早于开始
            activity="倒着过",
        )
        engine = make_engine(ports=make_ports(schedules=FakeSchedules([row])))
        assert engine._snapshot(NOW).current_block is None


class TestToScheduleBlock:
    def test_a_known_category_passes_through(self) -> None:
        row = ScheduleRecord(
            id="s1",
            day=DAY,
            start_at=datetime(2026, 9, 15, 13, 0),
            end_at=datetime(2026, 9, 15, 15, 0),
            activity="写代码",
            category="work",
        )
        block = to_schedule_block(row, persona_id=PERSONA_ID)
        assert block is not None
        assert block.category == "work"
        assert block.persona_id == PERSONA_ID

    def test_an_unknown_category_falls_back_to_other(self) -> None:
        """分类名是枚举，而「下午在干嘛」比它的分类重要得多。"""
        row = ScheduleRecord(
            id="s1",
            day=DAY,
            start_at=datetime(2026, 9, 15, 13, 0),
            end_at=datetime(2026, 9, 15, 15, 0),
            activity="发呆",
            category="在某个版本里被删掉的分类",
        )
        block = to_schedule_block(row, persona_id=PERSONA_ID)
        assert block is not None
        assert block.category == "other"

    def test_interruptible_is_carried_over(self) -> None:
        """它一旦丢了，「开会时别发消息」就永远不会生效。"""
        row = ScheduleRecord(
            id="s1",
            day=DAY,
            start_at=datetime(2026, 9, 15, 13, 0),
            end_at=datetime(2026, 9, 15, 15, 0),
            activity="开会",
            category="work",
            interruptible=False,
        )
        block = to_schedule_block(row, persona_id=PERSONA_ID)
        assert block is not None
        assert block.interruptible is False

    def test_an_empty_location_becomes_none(self) -> None:
        row = ScheduleRecord(
            id="s1",
            day=DAY,
            start_at=datetime(2026, 9, 15, 13, 0),
            end_at=datetime(2026, 9, 15, 15, 0),
            activity="写代码",
            location="",
        )
        block = to_schedule_block(row, persona_id=PERSONA_ID)
        assert block is not None
        assert block.location is None


class TestConversationId:
    def test_user_conversation_id_is_stable(self) -> None:
        assert user_conversation_id("p1") == "p1:user"

    def test_engine_uses_it_by_default(self) -> None:
        assert make_engine().conversation_id == user_conversation_id(PERSONA_ID)

    def test_it_can_be_overridden(self) -> None:
        assert make_engine(conversation_id="custom").conversation_id == "custom"


class TestOutcome:
    async def test_outcome_counts_what_happened(self) -> None:
        engine = make_engine(
            stages=[
                FakeStage(
                    name="persist",
                    order=110,
                    changes={
                        "activity_records": ["a", "b"],
                        "message_records": ["m"],
                        "post_records": ["p", "q", "r"],
                    },
                )
            ]
        )
        outcome = await engine.tick()
        assert (outcome.activities, outcome.messages, outcome.posts) == (2, 1, 3)

    async def test_outcome_carries_the_notes_the_stages_wrote(self) -> None:
        engine = make_engine(stages=[FakeStage(name="x", order=10)])
        outcome = await engine.tick()
        assert isinstance(outcome.notes, tuple)

    async def test_chosen_intent_name_and_reason_are_reported(self) -> None:
        intent = SimpleNamespace(name="reach_out", reason="有点想你了")
        engine = make_engine(
            stages=[FakeStage(name="intention", order=50, changes={"chosen_intent": intent})]
        )
        outcome = await engine.tick()
        assert outcome.chosen_intent == "reach_out"
        assert outcome.reason == "有点想你了"

    async def test_llm_usage_is_not_invented_when_nothing_was_called(self) -> None:
        outcome = await make_engine().tick()
        assert (outcome.llm_calls, outcome.llm_tokens) == (0, 0)

    async def test_draft_records_the_duration_and_the_status(self) -> None:
        ports = make_ports()
        outcome = await make_engine(ports=ports).tick()
        draft = ports.tick_logs.drafts[0]  # type: ignore[attr-defined]
        assert draft.status == outcome.status
        assert draft.real_duration_ms >= 0

    async def test_draft_digest_keeps_only_what_explains_a_decision(self) -> None:
        """整份快照里有人设文档与记忆全文，塞进每一行会让这张表一天长到几百 MB。"""
        ports = make_ports()
        await make_engine(ports=ports).tick()
        digest = ports.tick_logs.drafts[0].state_snapshot  # type: ignore[attr-defined]
        assert set(digest) == {
            "emotion",
            "block",
            "unread_messages",
            "last_user_message_at",
            "consecutive_instant_replies",
            "budget",
        }

    async def test_draft_has_no_percepts_when_no_stage_made_any(self) -> None:
        ports = make_ports()
        await make_engine(ports=ports).tick()
        assert ports.tick_logs.drafts[0].percepts == {}  # type: ignore[attr-defined]


@pytest.mark.parametrize("status", ["ok", "partial"])
def test_ok_property_only_for_a_complete_tick(status: str) -> None:
    assert TickOutcome(tick_id="t", virtual_now=NOW, status=status).ok is (status == "ok")

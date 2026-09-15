"""``alterego.sim.consolidation`` 的测试。

这一层是**编排**，所以测的不是「公式对不对」，而是「顺序对不对」：

- 素材不够时**一次模型都不调用**；
- ``--dry-run`` 不写库、不标记、也不调用模型；
- 解析失败时**什么都不写**（不能留半批）；
- 空结果是结论不是失败——素材照样标记为处理过。

装配用假的仓储与假的网关，但**提示词用真的**（``PromptLibrary()`` 指向包内
``prompts/``）。模板与渲染代码之间的占位符对不上，是一条最容易漏、
也最难在生产里定位的 bug：它只在真要发请求的那一刻炸。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Any

import pytest

from alterego.domain.memory import Memory
from alterego.interfaces.llm import LLMResponse
from alterego.interfaces.repository import ActivityRecord
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.errors import SimulationError
from alterego.llm import PromptLibrary
from alterego.sim.consolidation import (
    MemoryWorkbench,
    WorkReport,
    consolidate,
    distill,
)


TZ = resolve_timezone("Asia/Shanghai")
NOW = datetime(2026, 9, 15, 18, 0, tzinfo=TZ)
LOGGER = logging.getLogger("test.consolidation")

#: 一份格式正确的模型回复。三条，够说明问题。
GOOD_REPLY = """```json
[
  {
    "kind": "episodic",
    "content": "下午跟阿泽聊到搬家的事，他好像有点烦",
    "importance": 0.6,
    "valence": -0.3,
    "entities": ["阿泽", "搬家"]
  },
  {"kind": "semantic", "content": "我最近好像不太想说话", "importance": 0.5},
  {"content": "很久没这么开心过了", "importance": 0.7, "emotional": true}
]
```"""


# ── 假的协作者 ──────────────────────────────────────────────


class FakeBackend:
    """只提供事务。事务在这里没有真实语义——有真实的仓储时才需要。"""

    def __init__(self) -> None:
        self.transactions = 0

    def transaction(self) -> Any:
        self.transactions += 1
        return nullcontext()


class FakeMemories:
    """内存里的记忆仓储，行为对齐 ``SqliteMemoryRepository``。"""

    def __init__(self, recent: Sequence[Memory] = ()) -> None:
        self.recent = list(recent)
        self.saved: list[Memory] = []
        self.consolidated: list[str] = []
        self.importance: list[tuple[str, float]] = []
        self.queries: list[dict[str, Any]] = []
        self._done: set[str] = set()

    def save(self, memory: Memory) -> None:
        self.saved.append(memory)

    def save_many(self, memories: Sequence[Memory]) -> None:
        self.saved.extend(memories)

    def get(self, memory_id: str) -> Memory | None:
        return next((item for item in self.recent if item.id == memory_id), None)

    def list_recent(
        self,
        persona_id: str,
        *,
        since: datetime,
        kind: str | None = None,
        not_consolidated: bool = False,
        limit: int = 100,
    ) -> list[Memory]:
        self.queries.append(
            {
                "since": since,
                "kind": kind,
                "not_consolidated": not_consolidated,
                "limit": limit,
            }
        )
        found = [
            item
            for item in self.recent
            if item.persona_id == persona_id
            and item.occurred_at >= since
            and (kind is None or item.kind == kind)
            and (not not_consolidated or item.id not in self._done)
        ]
        return found[:limit]

    def mark_consolidated(self, memory_ids: Sequence[str], at: datetime) -> None:
        self.consolidated.extend(memory_ids)
        self._done.update(memory_ids)

    def set_importance(self, updates: Sequence[tuple[str, float]]) -> None:
        self.importance.extend(updates)


class FakeActivities:
    """内存里的行为日志仓储。"""

    def __init__(self, records: Sequence[ActivityRecord] = ()) -> None:
        self.records = list(records)
        self.distilled: list[tuple[tuple[str, ...], datetime]] = []
        self.since: datetime | None = None

    def list_undistilled(
        self,
        persona_id: str,
        *,
        since: datetime,
        limit: int = 200,
    ) -> list[ActivityRecord]:
        self.since = since
        return [item for item in self.records if item.started_at >= since][:limit]

    def mark_distilled(self, activity_ids: Sequence[str], at: datetime) -> None:
        self.distilled.append((tuple(activity_ids), at))


class FakeGateway:
    """假的网关。记下每一次调用，按顺序吐出准备好的回复。"""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, purpose: str, prompt: str, **kwargs: Any) -> LLMResponse:
        self.calls.append({"purpose": purpose, "prompt": prompt, **kwargs})
        text = self.replies.pop(0) if self.replies else "[]"
        return LLMResponse(text=text, model="fake-model")

    @property
    def prompt(self) -> str:
        return str(self.calls[-1]["prompt"])


# ── 造数据 ──────────────────────────────────────────────────


def _activity(index: int, *, hour: int = 18, voice: str = "") -> ActivityRecord:
    started = NOW.replace(hour=hour, minute=index)
    return ActivityRecord(
        id=f"act-{index}",
        intent="work",
        description=f"第 {index} 件事",
        started_at=started,
        category="internal",
        inner_voice=voice,
    )


def _memory(
    memory_id: str,
    *,
    kind: str = "episodic",
    content: str = "某件事",
    importance: float = 0.5,
    minutes_ago: int = 30,
) -> Memory:
    occurred = NOW - timedelta(minutes=minutes_ago)
    return Memory(
        id=memory_id,
        persona_id="p1",
        kind=kind,  # type: ignore[arg-type]
        content=content,
        summary=content,
        importance=importance,
        occurred_at=occurred,
        created_at=occurred,
    )


def _workbench(
    *,
    gateway: FakeGateway | None = None,
    memories: FakeMemories | None = None,
    activities: FakeActivities | None = None,
    new_id: Any = None,
) -> MemoryWorkbench:
    counter = iter(f"m{i}" for i in range(1, 100))
    return MemoryWorkbench(
        persona_id="p1",
        persona_name="林晚",
        user_name="阿泽",
        gateway=gateway or FakeGateway(),  # type: ignore[arg-type]
        prompts=PromptLibrary(),
        memories=memories or FakeMemories(),  # type: ignore[arg-type]
        activities=activities or FakeActivities(),  # type: ignore[arg-type]
        backend=FakeBackend(),  # type: ignore[arg-type]
        new_id=new_id or (lambda: next(counter)),
        logger=LOGGER,
    )


# ────────────────────────────────────────────────────────────
# distill · 梳理数据
# ────────────────────────────────────────────────────────────


class TestDistill:
    async def test_too_few_activities_skips_without_calling_the_model(self) -> None:
        """三条流水账归纳不出认知，只会把「上午开会」写成「上午开了个会」。"""
        gateway = FakeGateway(GOOD_REPLY)
        work = _workbench(gateway=gateway, activities=FakeActivities([_activity(1), _activity(2)]))

        report = await distill(work, now=NOW)

        assert gateway.calls == []
        assert report.saved == 0
        assert report.source_items == 2
        assert report.skipped is not None
        assert "2 条" in report.skipped

    async def test_the_boundary_is_three(self) -> None:
        gateway = FakeGateway(GOOD_REPLY)
        work = _workbench(
            gateway=gateway,
            activities=FakeActivities([_activity(1), _activity(2), _activity(3)]),
        )

        report = await distill(work, now=NOW)

        assert len(gateway.calls) == 1
        assert report.saved == 3

    async def test_it_writes_memories_and_marks_the_source(self) -> None:
        gateway = FakeGateway(GOOD_REPLY)
        memories = FakeMemories()
        activities = FakeActivities([_activity(1), _activity(2), _activity(3)])
        work = _workbench(gateway=gateway, memories=memories, activities=activities)

        report = await distill(work, now=NOW)

        assert report.source_items == 3
        assert report.drafted == 3
        assert report.saved == 3
        assert len(memories.saved) == 3
        assert {item.kind for item in memories.saved} == {"episodic", "semantic", "emotional"}
        assert activities.distilled == [(("act-1", "act-2", "act-3"), NOW)]

    async def test_the_new_memories_point_back_at_their_source(self) -> None:
        memories = FakeMemories()
        work = _workbench(
            gateway=FakeGateway(GOOD_REPLY),
            memories=memories,
            activities=FakeActivities([_activity(1), _activity(2), _activity(3)]),
        )

        await distill(work, now=NOW)

        for memory in memories.saved:
            assert memory.source == "consolidation"
            assert memory.source_ref == "act-1,act-2,act-3"
            assert memory.occurred_at == NOW
            assert memory.persona_id == "p1"

    async def test_it_uses_the_memory_routing_purpose(self) -> None:
        gateway = FakeGateway(GOOD_REPLY)
        work = _workbench(
            gateway=gateway,
            activities=FakeActivities([_activity(1), _activity(2), _activity(3)]),
        )

        await distill(work, now=NOW)

        assert gateway.calls[0]["purpose"] == "memory"
        assert gateway.calls[0]["response_format"] == "json"
        # 梳理是归纳不是创作：温度比默认的 0.8 低。
        assert gateway.calls[0]["temperature"] < 0.8

    async def test_the_prompt_carries_both_names(self) -> None:
        """模板与渲染代码之间的占位符对不上，只会在这里露出来。"""
        gateway = FakeGateway(GOOD_REPLY)
        work = _workbench(
            gateway=gateway,
            activities=FakeActivities([_activity(1), _activity(2), _activity(3)]),
        )

        await distill(work, now=NOW)

        assert "林晚" in gateway.prompt
        assert "阿泽" in gateway.prompt

    async def test_the_prompt_shows_the_inner_voice_on_its_own_line(self) -> None:
        """一个人记住的常常是自己当时怎么想，而不是自己当时做了什么。"""
        gateway = FakeGateway(GOOD_REPLY)
        work = _workbench(
            gateway=gateway,
            activities=FakeActivities(
                [
                    _activity(1, voice="有点烦"),
                    _activity(2),
                    _activity(3),
                ]
            ),
        )

        await distill(work, now=NOW)

        assert "心里想着：有点烦" in gateway.prompt

    async def test_the_prompt_lists_existing_memories(self) -> None:
        gateway = FakeGateway(GOOD_REPLY)
        work = _workbench(
            gateway=gateway,
            memories=FakeMemories([_memory("m-old", content="上周搬过一次家。")]),
            activities=FakeActivities([_activity(1), _activity(2), _activity(3)]),
        )

        await distill(work, now=NOW)

        assert "上周搬过一次家。" in gateway.prompt

    async def test_with_no_memories_it_says_so(self) -> None:
        gateway = FakeGateway(GOOD_REPLY)
        work = _workbench(
            gateway=gateway,
            activities=FakeActivities([_activity(1), _activity(2), _activity(3)]),
        )

        await distill(work, now=NOW)

        assert "还没有记下什么" in gateway.prompt

    async def test_an_empty_result_is_a_conclusion_not_a_failure(self) -> None:
        """不标记的话，同样的素材每次都会被重新问一遍。"""
        activities = FakeActivities([_activity(1), _activity(2), _activity(3)])
        memories = FakeMemories()
        work = _workbench(gateway=FakeGateway("[]"), memories=memories, activities=activities)

        report = await distill(work, now=NOW)

        assert report.saved == 0
        assert report.skipped is None
        assert memories.saved == []
        assert len(activities.distilled) == 1

    async def test_dry_run_touches_nothing(self) -> None:
        gateway = FakeGateway(GOOD_REPLY)
        memories = FakeMemories()
        activities = FakeActivities([_activity(1), _activity(2), _activity(3)])
        work = _workbench(gateway=gateway, memories=memories, activities=activities)

        report = await distill(work, now=NOW, dry_run=True)

        assert gateway.calls == []
        assert memories.saved == []
        assert activities.distilled == []
        assert report.source_items == 3
        assert report.preview

    async def test_dry_run_previews_the_real_materials(self) -> None:
        work = _workbench(
            gateway=FakeGateway(GOOD_REPLY),
            activities=FakeActivities([_activity(1), _activity(2), _activity(3)]),
        )

        report = await distill(work, now=NOW, dry_run=True)

        assert any("第 1 件事" in line for line in report.preview)

    async def test_an_unparsable_reply_writes_nothing(self) -> None:
        """留一半比全丢更危险——那半可能是模型编的。"""
        memories = FakeMemories()
        activities = FakeActivities([_activity(1), _activity(2), _activity(3)])
        work = _workbench(
            gateway=FakeGateway("抱歉，我无法完成这个请求。"),
            memories=memories,
            activities=activities,
        )

        with pytest.raises(SimulationError) as caught:
            await distill(work, now=NOW)

        assert memories.saved == []
        assert activities.distilled == []
        assert "model" in caught.value.context

    async def test_it_looks_back_six_hours(self) -> None:
        """往回看多久是**领域常量**决定的，不是这里随手写的数字。"""
        activities = FakeActivities([_activity(1), _activity(2), _activity(3)])
        work = _workbench(gateway=FakeGateway(GOOD_REPLY), activities=activities)

        await distill(work, now=NOW)

        assert activities.since == NOW - timedelta(hours=6)


# ────────────────────────────────────────────────────────────
# consolidate · 梳理记忆
# ────────────────────────────────────────────────────────────


def _episodics(count: int = 3) -> list[Memory]:
    return [
        _memory(
            f"e{index}", content=f"第 {index} 件小事", importance=0.6, minutes_ago=count - index
        )
        for index in range(count)
    ]


class TestConsolidate:
    async def test_too_few_memories_skips_without_calling_the_model(self) -> None:
        gateway = FakeGateway(GOOD_REPLY)
        work = _workbench(gateway=gateway, memories=FakeMemories(_episodics(2)))

        report = await consolidate(work, now=NOW)

        assert gateway.calls == []
        assert report.skipped is not None
        assert "记忆" in report.skipped

    async def test_it_only_takes_episodic_materials(self) -> None:
        """已经归纳出来的 semantic 再归纳一次，就是「抽象化漂移」。"""
        memories = FakeMemories(_episodics(3))
        work = _workbench(gateway=FakeGateway(GOOD_REPLY), memories=memories)

        await consolidate(work, now=NOW)

        # 挑素材的那一次查询有 not_consolidated 标记；
        # 另外一次是 _existing()（告诉模型「这些你已经记住了」），不带标记。
        picks = [query for query in memories.queries if query["not_consolidated"]]
        assert len(picks) == 1
        assert picks[0]["kind"] == "episodic"
        assert picks[0]["since"] == NOW - timedelta(hours=6)

    async def test_it_writes_new_memories_and_downgrades_the_source(self) -> None:
        memories = FakeMemories(_episodics(3))
        work = _workbench(gateway=FakeGateway(GOOD_REPLY), memories=memories)

        report = await consolidate(work, now=NOW)

        assert report.saved == 3
        assert report.downgraded == 3
        assert dict(memories.importance) == {"e0": 0.36, "e1": 0.36, "e2": 0.36}
        assert memories.consolidated == ["e0", "e1", "e2"]

    async def test_the_new_memories_come_from_consolidation(self) -> None:
        memories = FakeMemories(_episodics(3))
        work = _workbench(gateway=FakeGateway(GOOD_REPLY), memories=memories)

        await consolidate(work, now=NOW)

        assert all(item.source == "consolidation" for item in memories.saved)
        assert all(item.source_ref == "e0,e1,e2" for item in memories.saved)

    async def test_the_prompt_shows_full_content_not_the_summary(self) -> None:
        """摘要写库时已经丢过一次信息，再拿摘要去归纳只会更空。"""
        gateway = FakeGateway(GOOD_REPLY)
        work = _workbench(gateway=gateway, memories=FakeMemories(_episodics(3)))

        await consolidate(work, now=NOW)

        assert "第 0 件小事" in gateway.prompt

    async def test_a_second_run_finds_nothing_left(self) -> None:
        """源记忆被标记之后不该再被归纳一次——否则是在原地打转。"""
        gateway = FakeGateway(GOOD_REPLY, GOOD_REPLY)
        memories = FakeMemories(_episodics(3))
        work = _workbench(gateway=gateway, memories=memories)

        first = await consolidate(work, now=NOW)
        second = await consolidate(work, now=NOW)

        assert first.saved == 3
        assert second.saved == 0
        assert second.skipped is not None
        assert len(gateway.calls) == 1

    async def test_dry_run_touches_nothing(self) -> None:
        gateway = FakeGateway(GOOD_REPLY)
        memories = FakeMemories(_episodics(3))
        work = _workbench(gateway=gateway, memories=memories)

        report = await consolidate(work, now=NOW, dry_run=True)

        assert gateway.calls == []
        assert memories.saved == []
        assert memories.importance == []
        assert memories.consolidated == []
        assert len(report.preview) == 3

    async def test_an_unparsable_reply_downgrades_nothing(self) -> None:
        memories = FakeMemories(_episodics(3))
        work = _workbench(gateway=FakeGateway("我看不出什么规律。"), memories=memories)

        with pytest.raises(SimulationError):
            await consolidate(work, now=NOW)

        assert memories.saved == []
        assert memories.importance == []
        assert memories.consolidated == []


# ────────────────────────────────────────────────────────────
# WorkReport · 预览与真跑同一个形状
# ────────────────────────────────────────────────────────────


class TestWorkReport:
    def test_defaults_are_all_zero(self) -> None:
        report = WorkReport()
        assert (report.source_items, report.drafted, report.saved, report.downgraded) == (
            0,
            0,
            0,
            0,
        )
        assert report.preview == ()
        assert report.skipped is None

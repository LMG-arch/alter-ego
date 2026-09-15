"""``storage/sqlite/repositories.py`` 的测试。

仓储只做「一行 ↔ 一个对象」，所以这里盯的是两件事：

1. **往返不失真**——JSON 列、时区偏移、``None`` 与空串的区别，掉一个
   都要等到提示词里出现「None」才被发现；
2. **筛选与排序真的落在 SQL 里**——不是「全捞回来再在内存里挑」。
   后者在十条数据上看不出问题，在十万条上是每次梳理都全表扫一遍。

用真库（临时文件 + 真迁移），不 mock SQLite。这一层唯一的价值就是它跟
SQLite 说得对不对，绕开 SQLite 就等于什么都没测。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from alterego.domain.memory import Memory, MemoryKind
from alterego.interfaces.llm import LLMUsage
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.errors import StorageError
from alterego.storage.sqlite import (
    SqliteActivityRepository,
    SqliteMemoryRepository,
    SqliteStorageBackend,
    SqliteUsageRepository,
)
from alterego.storage.sqlite.connection import SqliteConnection


TZ = resolve_timezone("Asia/Shanghai")
T0 = datetime(2026, 9, 15, 9, 0, tzinfo=TZ)
PERSONA = "p1"


def _iso(moment: datetime) -> str:
    return moment.isoformat()


# ── 夹具与帮手 ──────────────────────────────────────────────


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[SqliteStorageBackend]:
    """一个迁移到最新版、已经有一条人设的库。

    真跑迁移而不是手写建表：手写的 DDL 会跟 ``migrations/`` 慢慢分家，
    而分家的那一刻起，这组用例就不再说库的真实形状了。
    """
    opened = SqliteStorageBackend.open(tmp_path / "alterego.db")
    try:
        opened.migrate()
        opened.connection.execute(
            "INSERT INTO persona (id, name, persona_json, created_at, updated_at)"
            " VALUES (?, ?, '{}', ?, ?)",
            (PERSONA, "林晚", _iso(T0), _iso(T0)),
        )
        yield opened
    finally:
        opened.close()


@pytest.fixture
def memories(backend: SqliteStorageBackend) -> SqliteMemoryRepository:
    return SqliteMemoryRepository(backend.connection)


@pytest.fixture
def activities(backend: SqliteStorageBackend) -> SqliteActivityRepository:
    return SqliteActivityRepository(backend.connection)


@pytest.fixture
def usages(backend: SqliteStorageBackend) -> SqliteUsageRepository:
    return SqliteUsageRepository(backend.connection, persona_id=PERSONA, tick_id="t1")


def _memory(
    memory_id: str,
    *,
    kind: MemoryKind = "episodic",
    content: str = "某件事",
    summary: str | None = None,
    minutes: int = 0,
    **extra: Any,
) -> Memory:
    """一条默认合法的记忆。``extra`` 能覆盖任何字段，包括 ``persona_id``。"""
    values: dict[str, Any] = {
        "id": memory_id,
        "persona_id": PERSONA,
        "kind": kind,
        "content": content,
        "summary": content if summary is None else summary,
        "occurred_at": T0 + timedelta(minutes=minutes),
        "created_at": T0,
    }
    values.update(extra)
    return Memory(**values)


def _add_activity(
    conn: SqliteConnection,
    activity_id: str,
    *,
    minutes: int = 0,
    intent: str = "work",
    description: str = "做了点什么",
    inner_voice: str = "",
    category: str = "internal",
    location: str = "",
    duration_minutes: int = 25,
) -> None:
    started = T0 + timedelta(minutes=minutes)
    conn.execute(
        """
        INSERT INTO activity_log
            (id, persona_id, intent, category, description, inner_voice,
             location, duration_minutes, started_at, ended_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            activity_id,
            PERSONA,
            intent,
            category,
            description,
            inner_voice,
            location,
            duration_minutes,
            _iso(started),
            _iso(started + timedelta(minutes=duration_minutes)),
        ),
    )


def _ids(memories: list[Memory]) -> list[str]:
    return [memory.id for memory in memories]


# ────────────────────────────────────────────────────────────
# 记忆：往返
# ────────────────────────────────────────────────────────────


class TestMemoryRoundTrip:
    def test_every_field_survives_a_round_trip(self, memories: SqliteMemoryRepository) -> None:
        original = _memory(
            "m1",
            kind="emotional",
            content="很久没这么开心过了",
            summary="开心",
            importance=0.7,
            strength=0.42,
            valence=0.8,
            entities=("阿哲", "楼下"),
            tags=("社交",),
            source="consolidation",
            source_ref="act-1,act-2",
            last_recalled_at=T0,
            recall_count=3,
            forgotten=True,
        )

        memories.save(original)
        loaded = memories.get("m1")

        assert loaded is not None
        assert loaded == original

    def test_the_timezone_offset_is_kept(self, memories: SqliteMemoryRepository) -> None:
        """换算成 UTC 会让「他昨晚几点睡」这句话在库里读不出来。"""
        memories.save(_memory("m1"))

        loaded = memories.get("m1")

        assert loaded is not None
        assert loaded.occurred_at == T0
        assert loaded.occurred_at.utcoffset() == timedelta(hours=8)

    def test_an_unknown_id_returns_none(self, memories: SqliteMemoryRepository) -> None:
        assert memories.get("never-written") is None

    def test_a_missing_id_is_refused_before_it_reaches_sql(
        self, memories: SqliteMemoryRepository
    ) -> None:
        """仓储不替它编一个 id：否则「同一批梳理跑了两遍」会变成两条看不出区别的记忆。"""
        with pytest.raises(StorageError, match="缺少 id"):
            memories.save(_memory(""))

    def test_a_missing_created_at_is_refused(self, memories: SqliteMemoryRepository) -> None:
        with pytest.raises(StorageError, match="缺少 created_at"):
            memories.save(_memory("m1", created_at=None))

    def test_a_duplicate_id_is_refused_with_a_pointer(
        self, memories: SqliteMemoryRepository
    ) -> None:
        memories.save(_memory("m1"))

        with pytest.raises(StorageError, match="写入记忆失败") as caught:
            memories.save(_memory("m1"))

        assert caught.value.context["memory_id"] == "m1"

    def test_save_many_writes_every_row(self, memories: SqliteMemoryRepository) -> None:
        memories.save_many([_memory("m1"), _memory("m2"), _memory("m3")])

        assert memories.get("m3") is not None

    def test_save_many_leaves_nothing_behind_when_one_row_is_bad(
        self, backend: SqliteStorageBackend
    ) -> None:
        """整批在一个事务里：半批新记忆是最难查的中间态。"""
        repo = SqliteMemoryRepository(backend.connection)

        with pytest.raises(StorageError), backend.transaction():
            repo.save_many([_memory("m1"), _memory(""), _memory("m2")])

        assert repo.get("m1") is None


# ────────────────────────────────────────────────────────────
# 记忆：筛选与排序
# ────────────────────────────────────────────────────────────


class TestMemoryListing:
    def test_it_only_returns_this_persona(
        self, memories: SqliteMemoryRepository, backend: SqliteStorageBackend
    ) -> None:
        backend.connection.execute(
            "INSERT INTO persona (id, name, persona_json, created_at, updated_at)"
            " VALUES ('p2', '别人', '{}', ?, ?)",
            (_iso(T0), _iso(T0)),
        )
        memories.save(_memory("m1"))
        memories.save(_memory("m2", persona_id="p2"))

        found = memories.list_recent(PERSONA, since=T0 - timedelta(days=1))

        assert _ids(found) == ["m1"]

    def test_it_drops_memories_before_the_window(self, memories: SqliteMemoryRepository) -> None:
        memories.save(_memory("old", minutes=-120))
        memories.save(_memory("edge", minutes=0))
        memories.save(_memory("new", minutes=30))

        found = memories.list_recent(PERSONA, since=T0)

        assert _ids(found) == ["edge", "new"]

    def test_it_can_filter_by_kind(self, memories: SqliteMemoryRepository) -> None:
        memories.save(_memory("m1", kind="episodic"))
        memories.save(_memory("m2", kind="semantic"))
        memories.save(_memory("m3", kind="emotional"))

        found = memories.list_recent(PERSONA, since=T0 - timedelta(days=1), kind="semantic")

        assert _ids(found) == ["m2"]

    def test_it_can_skip_what_was_already_consolidated(
        self, memories: SqliteMemoryRepository
    ) -> None:
        memories.save(_memory("m1"))
        memories.save(_memory("m2"))
        memories.mark_consolidated(["m1"], T0)

        found = memories.list_recent(PERSONA, since=T0 - timedelta(days=1), not_consolidated=True)

        assert _ids(found) == ["m2"]

    def test_it_honours_the_limit(self, memories: SqliteMemoryRepository) -> None:
        for index in range(5):
            memories.save(_memory(f"m{index}", minutes=index))

        found = memories.list_recent(PERSONA, since=T0 - timedelta(days=1), limit=2)

        assert _ids(found) == ["m0", "m1"]

    def test_it_is_ordered_by_time_then_id(self, memories: SqliteMemoryRepository) -> None:
        """同一秒里的两条若交给 SQLite 自己排，提示词每次都不一样（P6）。"""
        memories.save(_memory("b", minutes=10))
        memories.save(_memory("a", minutes=10))
        memories.save(_memory("早", minutes=-10))

        found = memories.list_recent(PERSONA, since=T0 - timedelta(days=1))

        assert _ids(found) == ["早", "a", "b"]


# ────────────────────────────────────────────────────────────
# 记忆：标记
# ────────────────────────────────────────────────────────────


class TestMemoryMarking:
    def test_consolidating_only_stamps_the_timestamp(
        self, memories: SqliteMemoryRepository, backend: SqliteStorageBackend
    ) -> None:
        """巩固不改变记忆的文字，只改变它在时间里的位置。"""
        original = _memory("m1", content="今天 很累", summary="今天 很累", tags=("工作",))
        memories.save(original)

        memories.mark_consolidated(["m1"], T0)

        assert memories.get("m1") == original
        assert backend.connection.scalar(
            "SELECT consolidated_at FROM memory WHERE id = 'm1'"
        ) == _iso(T0)

    def test_consolidating_does_not_disturb_the_full_text_index(
        self, memories: SqliteMemoryRepository, backend: SqliteStorageBackend
    ) -> None:
        """触发器只在 content / summary / tags_json 变化时重建索引；标记不该走那条路。"""
        memories.save(_memory("m1", content="今天 很累"))

        memories.mark_consolidated(["m1"], T0)

        rows = backend.connection.query(
            "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ?", ("很累",)
        )
        assert [str(row["memory_id"]) for row in rows] == ["m1"]

    def test_it_walks_around_the_parameter_ceiling(
        self, memories: SqliteMemoryRepository, backend: SqliteStorageBackend
    ) -> None:
        """500 条一批。一次梳理可能有几百条，跨过上限时 SQLite 抛的
        「too many SQL variables」跟问题原因毫无关系。"""
        ids = [f"m{index:03d}" for index in range(501)]
        with backend.transaction():
            memories.save_many([_memory(memory_id) for memory_id in ids])

        memories.mark_consolidated(ids, T0)

        assert (
            backend.connection.scalar("SELECT COUNT(*) FROM memory WHERE consolidated_at IS NULL")
            == 0
        )

    def test_importance_can_be_lowered_in_bulk(self, memories: SqliteMemoryRepository) -> None:
        memories.save(_memory("m1", importance=0.6))
        memories.save(_memory("m2", importance=0.6))

        memories.set_importance([("m1", 0.36), ("m2", 0.48)])

        lowered = memories.get("m1")
        other = memories.get("m2")
        assert lowered is not None
        assert other is not None
        assert (lowered.importance, other.importance) == (0.36, 0.48)

    def test_asking_for_no_change_writes_nothing(self, memories: SqliteMemoryRepository) -> None:
        memories.set_importance([])


# ────────────────────────────────────────────────────────────
# 行为日志
# ────────────────────────────────────────────────────────────


class TestActivityRepository:
    def test_it_returns_undistilled_activities_oldest_first(
        self, activities: SqliteActivityRepository, backend: SqliteStorageBackend
    ) -> None:
        """梳理要按事情发生的顺序读——倒着读会把「因为」和「所以」对调。"""
        _add_activity(backend.connection, "a2", minutes=0)
        _add_activity(backend.connection, "a1", minutes=-30)

        found = activities.list_undistilled(PERSONA, since=T0 - timedelta(hours=6))

        assert [item.id for item in found] == ["a1", "a2"]

    def test_it_skips_activities_before_the_window(
        self, activities: SqliteActivityRepository, backend: SqliteStorageBackend
    ) -> None:
        _add_activity(backend.connection, "old", minutes=-400)
        _add_activity(backend.connection, "new", minutes=-10)

        found = activities.list_undistilled(PERSONA, since=T0 - timedelta(hours=6))

        assert [item.id for item in found] == ["new"]

    def test_it_only_returns_this_persona(
        self, activities: SqliteActivityRepository, backend: SqliteStorageBackend
    ) -> None:
        backend.connection.execute(
            "INSERT INTO persona (id, name, persona_json, created_at, updated_at)"
            " VALUES ('p2', '别人', '{}', ?, ?)",
            (_iso(T0), _iso(T0)),
        )
        _add_activity(backend.connection, "mine", minutes=-10)
        backend.connection.execute(
            "INSERT INTO activity_log (id, persona_id, intent, category, description, started_at)"
            " VALUES ('theirs', 'p2', 'work', 'internal', '别人的事', ?)",
            (_iso(T0),),
        )

        found = activities.list_undistilled(PERSONA, since=T0 - timedelta(hours=6))

        assert [item.id for item in found] == ["mine"]

    def test_it_honours_the_limit(
        self, activities: SqliteActivityRepository, backend: SqliteStorageBackend
    ) -> None:
        for index in range(5):
            _add_activity(backend.connection, f"a{index}", minutes=index)

        found = activities.list_undistilled(PERSONA, since=T0 - timedelta(hours=6), limit=3)

        assert [item.id for item in found] == ["a0", "a1", "a2"]

    def test_every_column_reaches_the_record(
        self, activities: SqliteActivityRepository, backend: SqliteStorageBackend
    ) -> None:
        """``SELECT *`` + 按列名取值，所以加了列也不会串位。"""
        _add_activity(
            backend.connection,
            "a1",
            intent="socialize",
            category="social",
            description="和阿哲在楼下抽了根烟",
            inner_voice="他语气不太对",
            location="楼下",
            duration_minutes=20,
        )

        found = activities.list_undistilled(PERSONA, since=T0 - timedelta(hours=6))[0]

        assert found.intent == "socialize"
        assert found.category == "social"
        assert found.description == "和阿哲在楼下抽了根烟"
        assert found.inner_voice == "他语气不太对"
        assert found.location == "楼下"
        assert found.duration_minutes == 20
        assert found.started_at == T0

    def test_a_null_column_comes_back_as_an_empty_string(
        self, activities: SqliteActivityRepository, backend: SqliteStorageBackend
    ) -> None:
        """提示词里出现「None」比出现空白更糟。"""
        backend.connection.execute(
            "INSERT INTO activity_log (id, persona_id, intent, category, description, started_at)"
            " VALUES ('a1', ?, 'work', 'internal', '做了点什么', ?)",
            (PERSONA, _iso(T0)),
        )

        found = activities.list_undistilled(PERSONA, since=T0 - timedelta(hours=6))[0]

        assert found.location == ""
        assert found.inner_voice == ""
        assert found.duration_minutes == 0

    def test_marking_takes_them_out_of_the_next_query(
        self, activities: SqliteActivityRepository, backend: SqliteStorageBackend
    ) -> None:
        _add_activity(backend.connection, "a1", minutes=-30)
        _add_activity(backend.connection, "a2", minutes=-20)

        activities.mark_distilled(["a1"], T0)

        found = activities.list_undistilled(PERSONA, since=T0 - timedelta(hours=6))
        assert [item.id for item in found] == ["a2"]

    def test_marking_everything_leaves_nothing(
        self, activities: SqliteActivityRepository, backend: SqliteStorageBackend
    ) -> None:
        _add_activity(backend.connection, "a1")

        activities.mark_distilled(["a1"], T0)

        assert activities.list_undistilled(PERSONA, since=T0 - timedelta(hours=6)) == []


# ────────────────────────────────────────────────────────────
# 用量账本
# ────────────────────────────────────────────────────────────


def _usage(**overrides: Any) -> LLMUsage:
    values: dict[str, Any] = {
        "purpose": "memory",
        "tier": "cheap",
        "provider_id": "openai_compatible",
        "model": "gpt-4o-mini",
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "latency_ms": 800,
    }
    values.update(overrides)
    return LLMUsage(**values)


class TestUsageRepository:
    def test_a_row_lands_in_the_ledger(
        self, usages: SqliteUsageRepository, backend: SqliteStorageBackend
    ) -> None:
        usages.record(_usage())

        row = backend.connection.query_one("SELECT * FROM llm_usage")
        assert row is not None
        assert row["persona_id"] == PERSONA
        assert row["tick_id"] == "t1"
        assert row["purpose"] == "memory"
        assert row["model"] == "gpt-4o-mini"
        assert row["prompt_tokens"] == 120
        assert row["completion_tokens"] == 30
        assert row["total_tokens"] == 150
        assert row["latency_ms"] == 800
        assert row["success"] == 1

    def test_a_failure_is_recorded_with_its_code(
        self, usages: SqliteUsageRepository, backend: SqliteStorageBackend
    ) -> None:
        usages.record(_usage(success=False, error="llm_rate_limit: 限流", retry_count=2))

        row = backend.connection.query_one("SELECT * FROM llm_usage")
        assert row is not None
        assert row["success"] == 0
        assert row["error"] == "llm_rate_limit: 限流"
        assert row["retry_count"] == 2

    def test_the_cost_is_zero_until_there_is_a_price_list(
        self, usages: SqliteUsageRepository, backend: SqliteStorageBackend
    ) -> None:
        """猜出来的数字比 0 更糟——它会被当真。"""
        usages.record(_usage())

        assert backend.connection.scalar("SELECT cost_usd FROM llm_usage") == 0

    def test_record_many_writes_every_row(
        self, usages: SqliteUsageRepository, backend: SqliteStorageBackend
    ) -> None:
        usages.record_many([_usage(), _usage(purpose="decision"), _usage(purpose="npc")])

        assert backend.connection.scalar("SELECT COUNT(*) FROM llm_usage") == 3

    def test_nothing_to_write_is_a_no_op(
        self, usages: SqliteUsageRepository, backend: SqliteStorageBackend
    ) -> None:
        """空批次是常态（关闭了计量、整批被缓存），不该因此拼出一条空 SQL。"""
        usages.record_many([])

        assert backend.connection.scalar("SELECT COUNT(*) FROM llm_usage") == 0

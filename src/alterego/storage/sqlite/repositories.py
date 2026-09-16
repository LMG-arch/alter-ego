"""SQLite 仓储实现：记忆、行为日志、用量账本、人设、日程、搜集到的信息、训练数据集的源。

**这里只做「一行 ↔ 一个对象」**：SQL、JSON 序列化、往返映射都在这里；
业务规则（重要度怎么衰减、什么时候该拦下一条主动消息）在 `domain/` 与 `sim/`
（见 `docs/design/03-data-model.md` § 6.9）。

两条实现约定：

**不 BEGIN。** 这里一次都不开事务。调用方在 ``with backend.transaction():``
里连着调几个写方法，靠 :class:`~alterego.storage.sqlite.connection.SqliteConnection`
的嵌套事务合并成一个。这很重要——一次记忆巩固要先写新记忆、再给旧记忆降权，
两步分开提交会在中间态上留下一份「新记忆有了、旧记忆还没降权」的库。

**``SELECT *`` + 按列名取值。** 不写死列顺序：``005`` 之后又加过列，
而按位置取值的那份代码不会报错，它只会把 ``location`` 读成 ``inner_voice``。

其中人设 / 日程 / 搜集到的信息是**只读**的，给知识库用。只读不是遗漏：
知识库是库的下游，从不往回写。

训练数据集的源同样只读，同样是下游的取数口。它和上面那些的差别是：
它返回的是 ``domain.dataset`` 的行，而不是 ``interfaces.repository`` 的
``*Record``——理由写在那边的契约文档里。

最后一段（会话 / 预算 / 推演日志 / 动态）是**推演引擎专用的写口**，2026-09-16 加上，
同月拆去 :mod:`alterego.storage.sqlite.engine_repositories`——那一边的模块文档解释了
为什么按「谁在写」而不是「哪张表」切。拆分同时也把这个文件从 1133 行拉回
``AGENTS.md`` § 5 规定的 900 行以内。

依据: docs/design/03-data-model.md § 7、docs/plans/2026-09-16-main-body.md § 4
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from typing import Any, cast

from alterego.domain.dataset import ActivityRow, MessageRow, TickRow
from alterego.domain.memory import Memory, MemoryKind, MemorySource
from alterego.interfaces.llm import LLMUsage
from alterego.interfaces.repository import (
    ActivityRecord,
    PersonaRecord,
    ScheduleRecord,
    SourceRecord,
)
from alterego.kernel.errors import IntegrityError, StorageError
from alterego.storage.sqlite.connection import SqliteConnection


__all__ = [
    "SqliteActivityRepository",
    "SqliteDatasetSourceRepository",
    "SqliteMemoryRepository",
    "SqlitePersonaRepository",
    "SqliteScheduleRepository",
    "SqliteSourceRepository",
    "SqliteUsageRepository",
]


#: 一条 SQL 里最多绑多少个参数。
#:
#: SQLite 的变量上限是编译期常量（新版默认三万多，老版 999）。分块比赌
#: 「这个库够新」便宜：一次梳理可能有几百条行为，跨过上限时 SQLite 抛的
#: 是「too many SQL variables」——那是个跟问题原因毫无关系的报错。
_PARAM_CHUNK: int = 500

_MEMORY_INSERT = """
INSERT INTO memory (
    id, persona_id, kind, content, summary, importance, strength, valence,
    entities_json, tags_json, source, source_ref, occurred_at,
    last_recalled_at, recall_count, forgotten, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_ACTIVITY_SELECT = """
SELECT * FROM activity_log
WHERE persona_id = ?
  AND distilled_at IS NULL
  AND started_at >= ?
ORDER BY started_at ASC, id ASC
LIMIT ?
"""

_ACTIVITY_INSERT = """
INSERT INTO activity_log (
    id, persona_id, actor_kind, actor_id, intent, category, description,
    detail_json, location, outbound, outbound_ref, suppressed_intent,
    suppress_reason, inner_voice, duration_minutes, tick_id, started_at, ended_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_LLM_USAGE_INSERT = """
INSERT INTO llm_usage (
    id, persona_id, tick_id, purpose, tier, provider_id, model,
    prompt_tokens, completion_tokens, total_tokens, latency_ms,
    cost_usd, cache_hit, retry_count, success, error, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


# ── 时间与 JSON 的往返 ──────────────────────────────────────


def _now_iso() -> str:
    """墙上时间。**账单用它，推演不用它。**"""
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _format_dt(value: datetime) -> str:
    """时间 → 库里的 TEXT。

    保留原来的时区偏移：虚拟时间带着它所在时区的偏移，
    换算成 UTC 会让「他昨晚几点睡」这句话在数据库里读不出来。
    同一台机器上的偏移是固定的，所以字符串序仍然是时间序。
    """
    return value.isoformat()


def _parse_dt(value: Any) -> datetime | None:
    """库里的 TEXT → 时间。`None` 原样返回。"""
    if value is None or value == "":
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise StorageError("时间字段不是合法的 ISO 格式", value=text) from exc


def _require_dt(value: Any, *, column: str) -> datetime:
    parsed = _parse_dt(value)
    if parsed is None:
        raise StorageError("必填的时间字段是空的", column=column)
    return parsed


def _load_str_list(raw: Any) -> list[str]:
    """``*_json`` 列 → 字符串列表。

    库里写了 ``CHECK (json_valid(...))``，所以解析失败意味着有人绕过
    SQLite 改了库。这时应该报出来，而不是当成空列表悄悄继续。
    """
    try:
        loaded = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise StorageError("JSON 列解析失败", value=str(raw)) from exc
    if not isinstance(loaded, list):
        raise StorageError("JSON 列不是数组", value=str(raw))
    return [str(item) for item in loaded]


def _load_dict(raw: Any) -> dict[str, Any]:
    """``*_json`` 列 → 字典。

    ``NULL`` 与 ``{}`` 都回空字典：列上写着 ``NOT NULL DEFAULT '{}'``，
    但老库、手改的库都可能留下 ``NULL``，而「没有细节」和「细节是空对象」
    对读的人是一回事。
    """
    if raw is None:
        return {}
    try:
        loaded = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise StorageError("JSON 列解析失败", value=str(raw)) from exc
    if not isinstance(loaded, dict):
        raise StorageError("JSON 列不是对象", value=str(raw))
    return {str(key): value for key, value in loaded.items()}


def _dump_json(items: Sequence[str]) -> str:
    """字符串列表 → JSON。``ensure_ascii=False`` 让人直接读库也能看懂。"""
    return json.dumps(list(items), ensure_ascii=False)


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _placeholders(count: int) -> str:
    return ", ".join("?" * count)


# ── 记忆 ────────────────────────────────────────────────────


def _memory_params(memory: Memory) -> tuple[Any, ...]:
    """把一条 :class:`Memory` 摊成 INSERT 的参数。

    两个必填字段在这里校验而不是在 :meth:`SqliteMemoryRepository.save` 里，
    是为了只写一遍——顺便让类型检查器也能看见 ``created_at`` 不是 `None`。
    """
    if not memory.id:
        raise StorageError(
            "记忆缺少 id",
            hint="id 由调用方生成。仓储不替它编一个——"
            "否则「同一批巩固跑了两遍」会变成两条看不出区别的记忆。",
        )
    created_at = memory.created_at
    if created_at is None:
        raise StorageError(
            "记忆缺少 created_at",
            memory_id=memory.id,
            hint="created_at 是记账时间，不是发生时间；传虚拟时间。",
        )

    return (
        memory.id,
        memory.persona_id,
        memory.kind,
        memory.content,
        memory.summary,
        float(memory.importance),
        float(memory.strength),
        float(memory.valence),
        _dump_json(memory.entities),
        _dump_json(memory.tags),
        memory.source,
        memory.source_ref,
        _format_dt(memory.occurred_at),
        _format_dt(memory.last_recalled_at) if memory.last_recalled_at else None,
        int(memory.recall_count),
        1 if memory.forgotten else 0,
        _format_dt(created_at),
    )


def _row_to_memory(row: sqlite3.Row) -> Memory:
    """一行 → 一条记忆。

    ``kind`` / ``source`` 在库里是普通 TEXT（DDL 里没写 CHECK，取值清单
    由领域层持有），所以要 cast 一次；取值非法时 ``Memory.__post_init__``
    自己会抛 —— 让领域层当唯一的裁判，不在这里再抄一份清单。
    """
    return Memory(
        id=str(row["id"]),
        persona_id=str(row["persona_id"]),
        kind=cast("MemoryKind", row["kind"]),
        content=str(row["content"]),
        summary=str(row["summary"]),
        importance=float(row["importance"]),
        strength=float(row["strength"]),
        valence=float(row["valence"]),
        entities=tuple(_load_str_list(row["entities_json"])),
        tags=tuple(_load_str_list(row["tags_json"])),
        source=cast("MemorySource", row["source"]),
        source_ref=row["source_ref"],
        occurred_at=_require_dt(row["occurred_at"], column="occurred_at"),
        last_recalled_at=_parse_dt(row["last_recalled_at"]),
        recall_count=int(row["recall_count"]),
        forgotten=bool(row["forgotten"]),
        created_at=_parse_dt(row["created_at"]),
    )


class SqliteMemoryRepository:
    """:class:`~alterego.interfaces.repository.MemoryRepository` 的 SQLite 实现。"""

    __slots__ = ("_conn",)

    def __init__(self, conn: SqliteConnection) -> None:
        self._conn = conn

    def save(self, memory: Memory) -> None:
        try:
            self._conn.execute(_MEMORY_INSERT, _memory_params(memory))
        except IntegrityError as exc:
            # 抓的是 kernel 的 ``IntegrityError``，不是 ``sqlite3.IntegrityError``——
            # 连接层已经把后者换掉了。抓错类的那段 ``except`` 永远不会命中，
            # 而它看起来又很像在工作。
            raise IntegrityError(
                "写入记忆失败",
                memory_id=memory.id,
                hint="主键冲突通常意味着同一条记忆被写了两遍。",
                **exc.context,
            ) from exc

    def save_many(self, memories: Sequence[Memory]) -> None:
        """逐条 INSERT。

        没用 ``executemany``：一次巩固最多产出十条，省下的那点时间买不到
        「哪一条写坏了」这个信息——``executemany`` 只会告诉你「有一批坏了」。
        """
        for memory in memories:
            self.save(memory)

    def get(self, memory_id: str) -> Memory | None:
        row = self._conn.query_one("SELECT * FROM memory WHERE id = ?", (memory_id,))
        return None if row is None else _row_to_memory(row)

    def list_recent(
        self,
        persona_id: str,
        *,
        since: datetime,
        kind: MemoryKind | None = None,
        not_consolidated: bool = False,
        limit: int = 100,
    ) -> list[Memory]:
        sql = """
            SELECT * FROM memory
            WHERE persona_id = ?
              AND occurred_at >= ?
        """
        params: list[Any] = [persona_id, _format_dt(since)]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if not_consolidated:
            sql += " AND consolidated_at IS NULL"
        # 时间相同时按 id 排：不这么做，同一秒里的两条记忆顺序由 SQLite 决定，
        # 提示词每次都不一样，输出也跟着飘（P6）。
        sql += " ORDER BY occurred_at ASC, id ASC LIMIT ?"
        params.append(int(limit))

        return [_row_to_memory(row) for row in self._conn.query(sql, params)]

    def mark_consolidated(self, memory_ids: Sequence[str], at: datetime) -> None:
        """给这批记忆盖上巩固时间戳。

        只更新 ``consolidated_at``，**不碰 content / summary / tags_json**——
        所以 ``trg_memory_au`` 不会触发，FTS 索引不受影响。这是刻意的：
        巩固不改变记忆的文字，只改变它在时间里的位置。
        """
        stamp = _format_dt(at)
        for chunk in _chunks(memory_ids, _PARAM_CHUNK):
            self._conn.execute(
                f"UPDATE memory SET consolidated_at = ? WHERE id IN ({_placeholders(len(chunk))})",
                (stamp, *chunk),
            )

    def set_importance(self, updates: Sequence[tuple[str, float]]) -> None:
        """批量改重要度。新值由 ``domain.apply_consolidation()`` 算好传进来。"""
        if not updates:
            return
        self._conn.raw.executemany(
            "UPDATE memory SET importance = ? WHERE id = ?",
            [(float(value), memory_id) for memory_id, value in updates],
        )


# ── 行为日志 ────────────────────────────────────────────────


def _row_to_activity(row: sqlite3.Row) -> ActivityRecord:
    return ActivityRecord(
        id=str(row["id"]),
        intent=str(row["intent"]),
        description=str(row["description"]),
        started_at=_require_dt(row["started_at"], column="started_at"),
        category=str(row["category"] or ""),
        location=str(row["location"] or ""),
        inner_voice=str(row["inner_voice"] or ""),
        duration_minutes=int(row["duration_minutes"] or 0),
        detail=dict(_load_dict(row["detail_json"])),
    )


class SqliteActivityRepository:
    """:class:`~alterego.interfaces.repository.ActivityRepository` 的 SQLite 实现。"""

    __slots__ = ("_conn",)

    def __init__(self, conn: SqliteConnection) -> None:
        self._conn = conn

    def list_undistilled(
        self,
        persona_id: str,
        *,
        since: datetime,
        limit: int = 200,
    ) -> list[ActivityRecord]:
        rows = self._conn.query(
            _ACTIVITY_SELECT,
            (persona_id, _format_dt(since), int(limit)),
        )
        return [_row_to_activity(row) for row in rows]

    def mark_distilled(self, activity_ids: Sequence[str], at: datetime) -> None:
        stamp = _format_dt(at)
        for chunk in _chunks(activity_ids, _PARAM_CHUNK):
            self._conn.execute(
                "UPDATE activity_log SET distilled_at = ? "
                f"WHERE id IN ({_placeholders(len(chunk))})",
                (stamp, *chunk),
            )

    def list_range(
        self,
        persona_id: str,
        *,
        since: datetime,
        until: datetime,
        limit: int = 500,
    ) -> list[ActivityRecord]:
        """按 ``started_at`` 取一段，不筛 ``distilled_at``。

        ``ended_at`` 为 ``NULL`` 的行（还在进行中的行为）也会被取到——
        用 ``started_at`` 当唯一的过滤条件就够了，再加一条
        «``ended_at IS NOT NULL``» 只会让正在发生的那一分钟凭空消失。
        """
        rows = self._conn.query(
            "SELECT * FROM activity_log "
            "WHERE persona_id = ? AND started_at >= ? AND started_at < ? "
            "ORDER BY started_at ASC, id ASC LIMIT ?",
            (persona_id, _format_dt(since), _format_dt(until), int(limit)),
        )
        return [_row_to_activity(row) for row in rows]

    def append(self, records: Sequence[ActivityRecord]) -> None:
        """追写一批行为。

        被拦下的意图也走这里（``suppressed_intent`` + ``suppress_reason`` 两列），
        而且**和真的做成的那件事同一个批次**：它们在 ``tick_log.notes`` 里
        讲的其实是同一个故事，分两个事务写会出现「做成了但没说为什么拦住别的」
        这种半截解释。
        """
        for record in records:
            self._conn.execute(_ACTIVITY_INSERT, _activity_params(record))


# ── 用量账本 ────────────────────────────────────────────────


class SqliteUsageRepository:
    """把 :class:`~alterego.interfaces.llm.LLMUsage` 写进 ``llm_usage``。

    结构上满足 :class:`~alterego.interfaces.llm.UsageSink`，所以网关不需要
    认识这个类名，也不必 import 存储层。

    ``persona_id`` 与 ``tick_id`` 在**构造时**绑定，因为一次 tick 造一个 sink，
    而 ``record()`` 的签名里没有位置放它们。

    ``cost_usd`` 目前恒为 0：本项目还没有价目表，记一个猜出来的数字比记 0
    更糟——猜的数字会被当真。
    """

    __slots__ = ("_conn", "_persona_id", "_tick_id")

    def __init__(
        self, conn: SqliteConnection, *, persona_id: str, tick_id: str | None = None
    ) -> None:
        self._conn = conn
        self._persona_id = persona_id
        self._tick_id = tick_id

    def record(self, usage: LLMUsage) -> None:
        self.record_many([usage])

    def record_many(self, usages: list[LLMUsage]) -> None:
        if not usages:
            return
        self._conn.raw.executemany(
            _LLM_USAGE_INSERT,
            [self._params(usage) for usage in usages],
        )

    def _params(self, usage: LLMUsage) -> tuple[Any, ...]:
        return (
            uuid.uuid4().hex,
            self._persona_id,
            self._tick_id,
            usage.purpose,
            usage.tier,
            usage.provider_id,
            usage.model,
            int(usage.prompt_tokens),
            int(usage.completion_tokens),
            int(usage.total_tokens),
            int(usage.latency_ms),
            0.0,  # cost_usd：见类文档
            0,  # cache_hit：本批次还没有缓存层
            int(usage.retry_count),
            1 if usage.success else 0,
            usage.error,
            _format_dt(usage.at) if usage.at is not None else _now_iso(),
        )


# ── 人设 ────────────────────────────────────────────────────


def _row_to_persona(row: sqlite3.Row) -> PersonaRecord:
    """``occupation`` 这类列允许为空，统一收成空串。

    `None` 和 `""` 在笔记里的渲染**完全不同**：前者会印出「职业：None」，
    后者让那一行整个消失。所以在边界上就把它们统一掉，
    而不是让每个渲染函数自己防一遍。
    """
    return PersonaRecord(
        id=str(row["id"]),
        name=str(row["name"]),
        age=int(row["age"]) if row["age"] is not None else None,
        gender=str(row["gender"] or ""),
        city=str(row["city"] or ""),
        occupation=str(row["occupation"] or ""),
        version=int(row["current_version"]),
    )


class SqlitePersonaRepository:
    """:class:`~alterego.interfaces.repository.PersonaRepository` 的 SQLite 实现。"""

    __slots__ = ("_conn",)

    def __init__(self, conn: SqliteConnection) -> None:
        self._conn = conn

    def get(self, persona_id: str) -> PersonaRecord | None:
        row = self._conn.query_one("SELECT * FROM persona WHERE id = ?", (persona_id,))
        return None if row is None else _row_to_persona(row)

    def find_by_name(self, name: str) -> PersonaRecord | None:
        """按名字找。

        重名时返回**第一个**而不是报错：名字不是主键，数据库没法保证它唯一，
        而调用方（CLI / 插件）拿到一个之后还有 :meth:`list_all` 可以列全部。
        在这里抛异常会让「两个角色都叫林晚」变成一个谁也不知道怎么修的死结。
        """
        row = self._conn.query_one(
            "SELECT * FROM persona WHERE name = ? ORDER BY created_at, id LIMIT 1",
            (name,),
        )
        return None if row is None else _row_to_persona(row)

    def list_all(self) -> list[PersonaRecord]:
        rows = self._conn.query("SELECT * FROM persona ORDER BY created_at, id")
        return [_row_to_persona(row) for row in rows]

    def document(self, persona_id: str) -> dict[str, Any]:
        """整份 ``persona_json``。

        只把 JSON 解出来就返回，**不校验里面有哪些键**：这份文档的形状由
        ``prompts/persona_generate.md`` 决定，把它锁进 dataclass 就等于把
        「人设能长成什么样」钉死在代码里，而它本来应该由提示词与用户输入决定。

        解不开的 JSON 不当成失败：这里唯一的来源是 ``persona_generate`` 的
        输出，摊开一份坏 JSON 会让他连话都说不了；返回空字典能让他退回「一个
        没有性格设定的人」，也能让调用方把「提示词里少了语气」这件事说清楚。
        """
        row = self._conn.query_one("SELECT persona_json FROM persona WHERE id = ?", (persona_id,))
        if row is None:
            return {}
        try:
            loaded = json.loads(str(row["persona_json"]))
        except json.JSONDecodeError:
            return {}
        if not isinstance(loaded, dict):
            return {}
        return {str(key): value for key, value in loaded.items()}


# ── 日程 ────────────────────────────────────────────────────


def _row_to_schedule(row: sqlite3.Row) -> ScheduleRecord:
    """一行日程。

    ``day`` 是 ``YYYY-MM-DD`` 的文本， ``date.fromisoformat`` 认它。
    不用 :func:`_parse_dt`——那个返回 ``datetime``，而 ``day`` 本来就没有
    时刻的概念，升成 datetime 会凭空多出一个 00:00。
    """
    raw_day = str(row["day"])
    try:
        day = date.fromisoformat(raw_day)
    except ValueError as exc:
        raise StorageError("日程的 day 不是合法的 YYYY-MM-DD", value=raw_day) from exc

    return ScheduleRecord(
        id=str(row["id"]),
        day=day,
        start_at=_require_dt(row["start_at"], column="start_at"),
        end_at=_require_dt(row["end_at"], column="end_at"),
        activity=str(row["activity"]),
        category=str(row["category"] or "other"),
        interruptible=bool(row["interruptible"]),
        location=str(row["location"] or ""),
        actual_start_at=_parse_dt(row["actual_start_at"]),
        actual_end_at=_parse_dt(row["actual_end_at"]),
        deviation_note=str(row["deviation_note"] or ""),
    )


class SqliteScheduleRepository:
    """:class:`~alterego.interfaces.repository.ScheduleRepository` 的 SQLite 实现。"""

    __slots__ = ("_conn",)

    def __init__(self, conn: SqliteConnection) -> None:
        self._conn = conn

    def list_day(self, persona_id: str, *, day: date) -> list[ScheduleRecord]:
        rows = self._conn.query(
            "SELECT * FROM schedule_block "
            "WHERE persona_id = ? AND day = ? "
            "ORDER BY start_at ASC, id ASC",
            (persona_id, day.isoformat()),
        )
        return [_row_to_schedule(row) for row in rows]

    def list_range(
        self,
        persona_id: str,
        *,
        since: datetime,
        until: datetime,
        limit: int = 500,
    ) -> list[ScheduleRecord]:
        """按 ``start_at`` 取一段。注意 ``day`` 是当地日期而这里是时间戳：
        跨午夜的一段日程（23:00 → 次日 01:00）只有一个 ``day``，
        用 ``day`` 过滤会把它整个漏掉。"""
        rows = self._conn.query(
            "SELECT * FROM schedule_block "
            "WHERE persona_id = ? AND start_at >= ? AND start_at < ? "
            "ORDER BY start_at ASC, id ASC LIMIT ?",
            (persona_id, _format_dt(since), _format_dt(until), int(limit)),
        )
        return [_row_to_schedule(row) for row in rows]


# ── 搜集到的信息 ────────────────────────────────────────────


def _row_to_source(row: sqlite3.Row) -> SourceRecord:
    return SourceRecord(
        id=str(row["id"]),
        url=str(row["url"]),
        fetched_at=_require_dt(row["fetched_at"], column="fetched_at"),
        title=str(row["title"] or ""),
        summary=str(row["summary"] or ""),
        source_kind=str(row["source_kind"] or "search"),
        published_at=_parse_dt(row["published_at"]),
        lang=str(row["lang"] or ""),
        memory_id=row["memory_id"],
    )


class SqliteSourceRepository:
    """:class:`~alterego.interfaces.repository.SourceRepository` 的 SQLite 实现。"""

    __slots__ = ("_conn",)

    def __init__(self, conn: SqliteConnection) -> None:
        self._conn = conn

    def list_kept(
        self,
        persona_id: str,
        *,
        since: datetime,
        limit: int = 100,
    ) -> list[SourceRecord]:
        """只给 ``dropped = 0`` 的那些。

        被丢掉的三类（提示词注入 / 过长 / 重复）是过滤的中间产物。
        把它们写进知识库不只是混淆——「我看到过一篇讲提示词注入的文章」
        和「我在正文里看到过一段提示词注入」在用户眼里是两回事。
        """
        rows = self._conn.query(
            "SELECT * FROM source_item "
            "WHERE persona_id = ? AND dropped = 0 AND fetched_at >= ? "
            "ORDER BY fetched_at ASC, id ASC LIMIT ?",
            (persona_id, _format_dt(since), int(limit)),
        )
        return [_row_to_source(row) for row in rows]


# ── 训练数据集的源 ──────────────────────────────────────────
#
# 这三个映射**刻意不解析时间**。上面五张表的 ``_row_to_*`` 都走 ``_require_dt``，
# 时间戳畸形就抛 ``StorageError``；这里不抛，理由是这两件事的失败代价不同：
# 知识库与提示词是实时路径，时间错了会算出错误的「今天」；
# 数据集是一次性导出，一条时间戳畸形的行不该让整次导出失败。
# 而且按字符串排序在同一个时区下仍然是时间序（见 ``_format_dt`` 的说明）。
# 真畸形了会在 ``alterego dataset show`` 里一眼看见，跑不掉。


def _row_to_message(row: sqlite3.Row) -> MessageRow:
    return MessageRow(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        direction=str(row["direction"]),
        content=str(row["content"]),
        content_type=str(row["content_type"] or "text"),
        created_at=str(row["created_at"] or ""),
    )


def _row_to_tick(row: sqlite3.Row) -> TickRow:
    return TickRow(
        id=str(row["id"]),
        virtual_time=str(row["virtual_time"] or ""),
        status=str(row["status"]),
        state_snapshot_json=str(row["state_snapshot_json"] or "{}"),
        percepts_json=str(row["percepts_json"] or "{}"),
        candidates_json=str(row["candidates_json"] or "[]"),
        chosen_intent=str(row["chosen_intent"] or ""),
        motivation=str(row["motivation"] or ""),
        trigger_note=str(row["trigger_note"] or ""),
        suppressed_json=str(row["suppressed_json"] or "[]"),
        memories_json=str(row["memories_json"] or "[]"),
        notes_json=str(row["notes_json"] or "[]"),
    )


def _row_to_activity_row(row: sqlite3.Row) -> ActivityRow:
    return ActivityRow(
        id=str(row["id"]),
        intent=str(row["intent"]),
        category=str(row["category"] or ""),
        description=str(row["description"]),
        detail_json=str(row["detail_json"] or "{}"),
        location=str(row["location"] or ""),
        inner_voice=str(row["inner_voice"] or ""),
        duration_minutes=int(row["duration_minutes"] or 0),
        tick_id=str(row["tick_id"] or ""),
        started_at=str(row["started_at"] or ""),
        suppressed_intent=str(row["suppressed_intent"] or ""),
        suppress_reason=str(row["suppress_reason"] or ""),
    )


class SqliteDatasetSourceRepository:
    """:class:`~alterego.interfaces.repository.DatasetSourceRepository` 的 SQLite 实现。"""

    __slots__ = ("_conn",)

    def __init__(self, conn: SqliteConnection) -> None:
        self._conn = conn

    def list_conversation_messages(
        self,
        persona_id: str,
        *,
        since: datetime,
        limit: int = 20000,
    ) -> list[MessageRow]:
        """和用户的会话里的消息。

        ``JOIN conversation`` 是为了筛 ``counterpart_kind``——那个字段在
        ``conversation`` 上，而 ``message`` 只有 ``conversation_id``。
        一次 JOIN 比「先查会话 id 再 IN (…)」便宜，也不用担心 id 多到
        跨过 SQLite 的变量上限。
        """
        rows = self._conn.query(
            "SELECT m.* FROM message AS m "
            "JOIN conversation AS c ON c.id = m.conversation_id "
            "WHERE c.persona_id = ? AND c.counterpart_kind = 'user' AND m.created_at >= ? "
            "ORDER BY m.conversation_id ASC, m.created_at ASC, m.id ASC LIMIT ?",
            (persona_id, _format_dt(since), int(limit)),
        )
        return [_row_to_message(row) for row in rows]

    def list_ticks(
        self,
        persona_id: str,
        *,
        since: datetime,
        limit: int = 5000,
    ) -> list[TickRow]:
        """推演日志。``status`` 不在这里筛——那是业务判断，交给 ``domain/``。"""
        rows = self._conn.query(
            "SELECT * FROM tick_log "
            "WHERE persona_id = ? AND virtual_time >= ? "
            "ORDER BY virtual_time ASC, id ASC LIMIT ?",
            (persona_id, _format_dt(since), int(limit)),
        )
        return [_row_to_tick(row) for row in rows]

    def list_activities(
        self,
        persona_id: str,
        *,
        since: datetime,
        limit: int = 20000,
    ) -> list[ActivityRow]:
        """行为日志。不筛 ``distilled_at``：训练集要的是全部行为，不是没梳理过的那些。"""
        rows = self._conn.query(
            "SELECT * FROM activity_log "
            "WHERE persona_id = ? AND started_at >= ? "
            "ORDER BY started_at ASC, id ASC LIMIT ?",
            (persona_id, _format_dt(since), int(limit)),
        )
        return [_row_to_activity_row(row) for row in rows]

    def map_tick_intents(
        self,
        persona_id: str,
        *,
        since: datetime,
    ) -> dict[str, str]:
        """``tick_id → chosen_intent``。

        没选意图的 tick 不进字典。填一个空串进去会让工具调用数据集
        把「请求」渲染成空白——那是**看起来有内容、实际什么都没有**的样本，
        比缺一条难发现得多。
        """
        rows = self._conn.query(
            "SELECT id, chosen_intent FROM tick_log "
            "WHERE persona_id = ? AND virtual_time >= ? AND chosen_intent IS NOT NULL",
            (persona_id, _format_dt(since)),
        )
        return {str(row["id"]): str(row["chosen_intent"]) for row in rows if row["chosen_intent"]}


def _dump(value: Any) -> str:
    """把任意值序列化成一行 JSON。

    ``default=str`` 是**故意放松的**：这些字段里迟早会出现 ``datetime``、
    ``Enum`` 或某个领域对象，而「序列化失败导致整轮推演白跑」的代价远大于
    「日志里多了一个字符串形式的时间戳」。

    它和上面那个 :func:`_dump_json` 的区别是「值域」：这个吃任意值、
    交给 ``json.dumps`` 自己判断；那个只吃 ``Sequence[str]``。
    ``tick_log`` / ``social_post`` / ``emotion_log`` 的那些 ``*_json`` 列
    用这一个——它们装的是「当时都有哪些候选 / 哪几条原因」这类**混合结构**，
    而 ``image_paths`` 这类纯字符串列表只是恰好也能走这条路。
    """
    return json.dumps(value, ensure_ascii=False, default=str)


def _activity_params(record: ActivityRecord) -> tuple[Any, ...]:
    return (
        record.id,
        record.persona_id,
        record.actor_kind or "persona",
        record.actor_id or None,
        record.intent,
        record.category or "internal",
        record.description,
        _dump(record.detail),
        record.location or None,
        int(record.outbound),
        record.outbound_ref or None,
        record.suppressed_intent or None,
        record.suppress_reason or None,
        record.inner_voice or None,
        int(record.duration_minutes),
        record.tick_id or None,
        _format_dt(record.started_at),
        None,
    )

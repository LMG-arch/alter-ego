"""``SqliteDatasetSourceRepository`` 的测试。

这一层只做取数，所以盯的是三件容易被 SQL 悄悄做错的事：

1. **筛与排真的落在 SQL 里**——不是全捞回来再在内存里挑。
2. **只取和用户的会话。** 和 NPC 的来往进了这一批，会把「它怎么和你说话」
   稀释成「它怎么和所有人说话」，而那是两种不同的数据。
3. **``since`` 真的传到 WHERE 里。** 传了参数却没用在 SQL 里，
   表现是「能跑、能出数据、只是把全库都导出来了」——最难察觉的一种错。

用真库（临时文件 + 真迁移），不 mock SQLite。这一层唯一的价值就是它跟
SQLite 说得对不对。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from alterego.kernel.clock import resolve_timezone
from alterego.storage.sqlite import SqliteStorageBackend
from alterego.storage.sqlite.repositories import SqliteDatasetSourceRepository


TZ = resolve_timezone("Asia/Shanghai")
T0 = datetime(2026, 9, 15, 9, 0, tzinfo=TZ)
PERSONA = "p1"
OTHER = "p2"


def _iso(moment: datetime) -> str:
    return moment.isoformat()


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[SqliteStorageBackend]:
    """一个迁移到最新版、有两个人设、会话与各种日志都齐了的库。"""
    opened = SqliteStorageBackend.open(tmp_path / "alterego.db")
    try:
        opened.migrate()
        conn = opened.connection
        for pid, name in ((PERSONA, "林晚"), (OTHER, "阿泽")):
            conn.execute(
                "INSERT INTO persona (id, name, persona_json, created_at, updated_at)"
                " VALUES (?, ?, '{}', ?, ?)",
                (pid, name, _iso(T0), _iso(T0)),
            )

        # 三个会话：和用户的两轮、和 NPC 的一轮、以及另一个人设和用户的。
        conn.execute(
            "INSERT INTO conversation (id, persona_id, counterpart_id, counterpart_kind,"
            " title, message_count, created_at)"
            " VALUES ('c1', ?, 'user', 'user', '和你的对话', 2, ?)",
            (PERSONA, _iso(T0)),
        )
        conn.execute(
            "INSERT INTO conversation (id, persona_id, counterpart_id, counterpart_kind,"
            " title, message_count, created_at)"
            " VALUES ('c2', ?, 'npc1', 'npc', '和同事的对话', 1, ?)",
            (PERSONA, _iso(T0)),
        )
        conn.execute(
            "INSERT INTO conversation (id, persona_id, counterpart_id, counterpart_kind,"
            " title, message_count, created_at)"
            " VALUES ('c9', ?, 'user', 'user', '别人的对话', 1, ?)",
            (OTHER, _iso(T0)),
        )
        for cid, mid, direction, sender, content, at in (
            ("c1", "m1", "inbound", "user", "我手机 13800138000 打不通了", T0),
            ("c1", "m2", "outbound", PERSONA, "我看看", T0 + timedelta(minutes=1)),
            ("c2", "m3", "inbound", "npc1", "晚上一起吃饭？", T0 + timedelta(minutes=2)),
            ("c9", "m9", "inbound", "user", "别人的话", T0 + timedelta(minutes=3)),
        ):
            conn.execute(
                "INSERT INTO message (id, conversation_id, direction, sender_id, content,"
                " content_type, created_at) VALUES (?, ?, ?, ?, ?, 'text', ?)",
                (mid, cid, direction, sender, content, _iso(at)),
            )

        for tid, at, status, chosen in (
            ("t1", T0, "ok", "social/reply"),
            ("t2", T0 + timedelta(minutes=5), "ok", "social/post"),
            ("t3", T0 + timedelta(minutes=9), "failed", None),
        ):
            conn.execute(
                "INSERT INTO tick_log (id, persona_id, virtual_time, real_duration_ms, status,"
                " chosen_intent, created_at)"
                " VALUES (?, ?, ?, 10, ?, ?, ?)",
                (tid, PERSONA, _iso(at), status, chosen, _iso(at)),
            )

        for aid, at, intent, category, description, tick in (
            ("a1", T0, "social/reply", "social", "回了她一句话", "t1"),
            ("a2", T0 + timedelta(minutes=5), "social/post", "social", "发了一条动态", "t2"),
        ):
            conn.execute(
                "INSERT INTO activity_log (id, persona_id, intent, category, description,"
                " detail_json, started_at, tick_id)"
                " VALUES (?, ?, ?, ?, ?, '{}', ?, ?)",
                (aid, PERSONA, intent, category, description, _iso(at), tick),
            )
        yield opened
    finally:
        opened.close()


@pytest.fixture
def sources(backend: SqliteStorageBackend) -> SqliteDatasetSourceRepository:
    return SqliteDatasetSourceRepository(backend.connection)


# ── 会话消息 ────────────────────────────────────────────────
def test_messages_only_include_the_user_conversation(sources) -> None:
    """和 NPC 的来往不进这一批——那是另一种数据。"""
    found = sources.list_conversation_messages(PERSONA, since=T0 - timedelta(days=1))
    assert {row.id for row in found} == {"m1", "m2"}


def test_messages_only_include_this_persona(sources) -> None:
    """别人的人设和用户的对话不该混进来。"""
    found = sources.list_conversation_messages(OTHER, since=T0 - timedelta(days=1))
    assert {row.id for row in found} == {"m9"}


def test_messages_are_in_chronological_order(sources) -> None:
    """合并相邻同向靠顺序，顺序错了合出来的话也就错了。"""
    found = sources.list_conversation_messages(PERSONA, since=T0 - timedelta(days=1))
    assert [row.created_at for row in found] == sorted(row.created_at for row in found)


def test_messages_respect_since(sources) -> None:
    """``since`` 必须在 WHERE 里，不能传了不用。"""
    found = sources.list_conversation_messages(PERSONA, since=T0 + timedelta(seconds=30))
    assert [row.id for row in found] == ["m2"]


def test_messages_respect_limit(sources) -> None:
    found = sources.list_conversation_messages(PERSONA, since=T0 - timedelta(days=1), limit=1)
    assert [row.id for row in found] == ["m1"]


def test_messages_carry_the_direction_and_content_type(sources) -> None:
    """方向决定它算谁说的话，缺了整轮对话就归错了人。"""
    found = sources.list_conversation_messages(PERSONA, since=T0 - timedelta(days=1))
    assert [(row.direction, row.content_type) for row in found] == [
        ("inbound", "text"),
        ("outbound", "text"),
    ]


def test_messages_keep_the_raw_text_untouched(sources) -> None:
    """仓储不做脱敏——那是 ``domain/redact.py`` 的事，在导出前统一做。"""
    found = sources.list_conversation_messages(PERSONA, since=T0 - timedelta(days=1))
    assert "13800138000" in found[0].content


# ── tick 日志 ───────────────────────────────────────────────
def test_ticks_are_in_virtual_time_order(sources) -> None:
    found = sources.list_ticks(PERSONA, since=T0 - timedelta(days=1))
    assert [row.id for row in found] == ["t1", "t2", "t3"]


def test_ticks_do_not_filter_by_status(sources) -> None:
    """失败的轨迹也要取出来——「哪些算成功」是业务判断，不是取数的事。"""
    found = sources.list_ticks(PERSONA, since=T0 - timedelta(days=1))
    assert {row.status for row in found} == {"ok", "failed"}


def test_ticks_respect_since(sources) -> None:
    found = sources.list_ticks(PERSONA, since=T0 + timedelta(minutes=1))
    assert [row.id for row in found] == ["t2", "t3"]


def test_ticks_respect_limit(sources) -> None:
    found = sources.list_ticks(PERSONA, since=T0 - timedelta(days=1), limit=2)
    assert [row.id for row in found] == ["t1", "t2"]


def test_ticks_keep_json_columns_as_text(sources) -> None:
    """JSON 列原样带出。在这一层解析只会多两个编解码的出错点。"""
    found = sources.list_ticks(PERSONA, since=T0 - timedelta(days=1))
    # 两列的默认值本来就不同（见 001_initial.sql）：感知是一个对象，
    # 候选是一个数组。这里照实报，不在读的时候替库里改形状。
    assert found[0].percepts_json == "{}"
    assert found[0].candidates_json == "[]"


def test_ticks_turn_null_json_columns_into_empty_shapes(sources) -> None:
    """``None`` 要变成 ``[]`` / ``{}``，不能字面量变成 ``"None"``。"""
    backend_row = sources.list_ticks(PERSONA, since=T0 - timedelta(days=1))[0]
    assert backend_row.memories_json == "[]"
    assert backend_row.suppressed_json == "[]"
    assert backend_row.notes_json == "[]"
    assert backend_row.state_snapshot_json == "{}"


def test_ticks_do_not_parse_timestamps(sources) -> None:
    """时间戳保持字符串。

    实时路径上时间戳畸形要报错；一次性导出的路径上，一条畸形行不该让
    整次导出失败。所以这里刻意**不**校验，坏数据会原样出现在
    ``dataset show`` 里，一眼看得见。
    """
    found = sources.list_ticks(PERSONA, since=T0 - timedelta(days=1))
    assert isinstance(found[0].virtual_time, str)
    assert found[0].virtual_time.startswith("2026-09-15")


# ── 行为日志 ────────────────────────────────────────────────
def test_activities_are_in_started_at_order(sources) -> None:
    found = sources.list_activities(PERSONA, since=T0 - timedelta(days=1))
    assert [row.id for row in found] == ["a1", "a2"]


def test_activities_respect_since(sources) -> None:
    found = sources.list_activities(PERSONA, since=T0 + timedelta(minutes=1))
    assert [row.id for row in found] == ["a2"]


def test_activities_respect_limit(sources) -> None:
    found = sources.list_activities(PERSONA, since=T0 - timedelta(days=1), limit=1)
    assert [row.id for row in found] == ["a1"]


def test_activities_carry_the_tick_link(sources) -> None:
    """``tick_id`` 是「它想做的是什么」唯一能接上的地方。"""
    found = sources.list_activities(PERSONA, since=T0 - timedelta(days=1))
    assert [row.tick_id for row in found] == ["t1", "t2"]


def test_activities_include_all_rows_regardless_of_distillation(sources) -> None:
    """训练集要的是全部行为，不是「还没被梳理过」的那些。"""
    found = sources.list_activities(PERSONA, since=T0 - timedelta(days=1))
    assert len(found) == 2


# ── tick → 意图 ─────────────────────────────────────────────
def test_intents_map_tick_id_to_the_chosen_intent(sources) -> None:
    mapping = sources.map_tick_intents(PERSONA, since=T0 - timedelta(days=1))
    assert mapping == {"t1": "social/reply", "t2": "social/post"}


def test_intents_leave_out_ticks_without_a_choice(sources) -> None:
    """没选意图的 tick 不进字典。

    填一个空串进去会让「请求」渲染成空白——那是看起来有内容、
    实际什么都没有的样本，比缺一条难发现得多。
    """
    mapping = sources.map_tick_intents(PERSONA, since=T0 - timedelta(days=1))
    assert "t3" not in mapping
    assert all(value for value in mapping.values())


def test_intents_respect_since(sources) -> None:
    mapping = sources.map_tick_intents(PERSONA, since=T0 + timedelta(minutes=1))
    assert mapping == {"t2": "social/post"}


def test_intents_do_not_leak_across_personas(sources) -> None:
    """另一个人设的 tick 不该出现在这里。"""
    assert sources.map_tick_intents(OTHER, since=T0 - timedelta(days=1)) == {}


def test_intents_come_from_the_same_window_as_the_activities(sources) -> None:
    """两边用同一个 ``since``，否则会拼出「有请求没结果」的样本。"""
    since = T0 - timedelta(days=1)
    activities = sources.list_activities(PERSONA, since=since)
    intents = sources.map_tick_intents(PERSONA, since=since)
    assert {row.tick_id for row in activities} <= set(intents)


# ── 空库 ────────────────────────────────────────────────────
def test_everything_is_empty_on_an_empty_database(backend: SqliteStorageBackend) -> None:
    """没有人设数据时返回空，而不是抛错——还没开始用不是故障。"""
    empty = SqliteDatasetSourceRepository(backend.connection)
    since = T0 - timedelta(days=1)
    assert empty.list_conversation_messages("nobody", since=since) == []
    assert empty.list_ticks("nobody", since=since) == []
    assert empty.list_activities("nobody", since=since) == []
    assert empty.map_tick_intents("nobody", since=since) == {}

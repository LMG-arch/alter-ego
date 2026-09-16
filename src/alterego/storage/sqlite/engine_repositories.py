"""推演引擎的写口。

``repositories.py`` 里那些仓储的调用方是「读」的人（知识库、数据集、CLI）；
这里几个只有一个调用方——推演引擎与它背后的会话服务。它们的共同点是
**写下来就不该再改**：一次 tick 发生过什么是历史，不是可以就地修正的缓存。
所以除了会话表的消息计数，这里没有任何 ``UPDATE``。

与 ``repositories.py`` 分家纯属体量原因：那边已经有八个仓储，再加上这几个
会顶穿 900 行上限（``AGENTS.md`` § 5）。分界线是**谁在写**，不是表属于谁——
``SqliteActivityRepository`` 既被引擎写、又被记忆整理读，所以它留在原处。

依据: docs/design/03-data-model.md, docs/design/04-simulation-loop.md
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime

from alterego.domain.emotion import Emotion
from alterego.interfaces.repository import (
    BudgetUsage,
    ConversationRecord,
    MessageRecord,
    SocialPostRecord,
    TickLogDraft,
)
from alterego.kernel.errors import StorageError
from alterego.storage.sqlite.connection import SqliteConnection
from alterego.storage.sqlite.repositories import (
    _dump,
    _format_dt,
    _load_str_list,
    _now_iso,
    _parse_dt,
)


__all__ = [
    "SqliteBudgetRepository",
    "SqliteConversationRepository",
    "SqliteEmotionRepository",
    "SqliteSocialPostRepository",
    "SqliteTickLogRepository",
]


def _row_to_emotion(row: sqlite3.Row) -> Emotion:
    """一行情绪。

    ``updated_at`` 取 ``recorded_at``：那一列才是「这份情绪算到哪一刻为止」。
    """
    recorded = _parse_dt(row["recorded_at"])
    if recorded is None:
        raise StorageError("emotion_log.recorded_at 解不出时间", value=str(row["recorded_at"]))
    return Emotion(
        valence=float(row["valence"]),
        arousal=float(row["arousal"]),
        fatigue=float(row["fatigue"]),
        label=str(row["label"]),
        updated_at=recorded,
    )


class SqliteEmotionRepository:
    """:class:`~alterego.interfaces.repository.EmotionRepository` 的 SQLite 实现。"""

    __slots__ = ("_conn",)

    def __init__(self, conn: SqliteConnection) -> None:
        self._conn = conn

    def latest(self, persona_id: str) -> Emotion | None:
        row = self._conn.query_one(
            "SELECT * FROM emotion_log WHERE persona_id = ? ORDER BY recorded_at DESC, rowid DESC LIMIT 1",
            (persona_id,),
        )
        return None if row is None else _row_to_emotion(row)

    def append(
        self,
        persona_id: str,
        emotion: Emotion,
        *,
        reason: str = "",
        causes: Sequence[str] = (),
        tick_id: str = "",
    ) -> None:
        """写一条。

        ``id`` 由 ``persona_id + 时间 + tick_id`` 拼出来，**不用 uuid**：
        重跑同一轮推演应当得到同一行（``INSERT OR REPLACE`` 覆盖），
        否则「重跑一次」会在情绪曲线上多出一个点，而多出来的那个点
        看起来和正常的一模一样。
        """
        stamp = _format_dt(emotion.updated_at)
        row_id = f"emo-{persona_id}-{stamp}-{tick_id}" if tick_id else f"emo-{persona_id}-{stamp}"
        self._conn.execute(
            "INSERT OR REPLACE INTO emotion_log ("
            "id, persona_id, valence, arousal, fatigue, label, reason, "
            "causes_json, tick_id, recorded_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                persona_id,
                float(emotion.valence),
                float(emotion.arousal),
                float(emotion.fatigue),
                str(emotion.label),
                reason,
                _dump(list(causes)),
                tick_id or None,
                stamp,
            ),
        )


def _row_to_conversation(row: sqlite3.Row) -> ConversationRecord:
    return ConversationRecord(
        id=str(row["id"]),
        persona_id=str(row["persona_id"]),
        counterpart_id=str(row["counterpart_id"]),
        counterpart_kind=str(row["counterpart_kind"]),
        title=str(row["title"] or ""),
        message_count=int(row["message_count"] or 0),
        last_message_at=_parse_dt(row["last_message_at"]),
        created_at=_parse_dt(row["created_at"]),
    )


def _row_to_message_record(row: sqlite3.Row) -> MessageRecord:
    return MessageRecord(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        direction=str(row["direction"]),
        sender_id=str(row["sender_id"]),
        content=str(row["content"]),
        content_type=str(row["content_type"] or "text"),
        initiative=bool(row["initiative"]),
        motivation=str(row["motivation"] or ""),
        trigger_note=str(row["trigger_note"] or ""),
        delivered_channels=tuple(_load_str_list(row["delivered_channels_json"])),
        read_at=_parse_dt(row["read_at"]),
        replied_at=_parse_dt(row["replied_at"]),
        tick_id=str(row["tick_id"] or ""),
        created_at=_parse_dt(row["created_at"]),
    )


class SqliteConversationRepository:
    """:class:`~alterego.interfaces.repository.ConversationRepository` 的 SQLite 实现。

    ``direction`` 与 ``counterpart_kind`` 都是自由字符串而不是枚举：表里本来
    就是 ``TEXT`` + 注释，塞一个 ``CHECK`` 约束进去会让「以后加一种 direction」
    变成一次迁移。真正把关的是 ``sim/conversation.py`` 与 ``interfaces/channel.py``。
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: SqliteConnection) -> None:
        self._conn = conn

    def ensure(self, record: ConversationRecord) -> ConversationRecord:
        """按 ``(persona_id, counterpart_id)`` 取或建。

        ``UNIQUE (persona_id, counterpart_id)`` 是表上的约束，所以这里
        「先查再插」不会在并发下插出两行——真要撞上，SQLite 会抛
        ``IntegrityError``，而不是静默地多出一个会话。
        """
        rows = self._conn.query(
            "SELECT * FROM conversation WHERE persona_id = ? AND counterpart_id = ?",
            (record.persona_id, record.counterpart_id),
        )
        if rows:
            return _row_to_conversation(rows[0])
        created_at = _format_dt(record.created_at) if record.created_at else _now_iso()
        self._conn.execute(
            "INSERT INTO conversation ("
            "id, persona_id, counterpart_id, counterpart_kind, title, "
            "message_count, last_message_at, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.persona_id,
                record.counterpart_id,
                record.counterpart_kind,
                record.title or None,
                int(record.message_count),
                _format_dt(record.last_message_at) if record.last_message_at else None,
                created_at,
            ),
        )
        return ConversationRecord(
            id=record.id,
            persona_id=record.persona_id,
            counterpart_id=record.counterpart_id,
            counterpart_kind=record.counterpart_kind,
            title=record.title,
            message_count=int(record.message_count),
            last_message_at=record.last_message_at,
            created_at=_parse_dt(created_at),
        )

    def append(self, message: MessageRecord) -> None:
        """写一条消息，并顺手把会话的消息数与最后消息时间推上去。

        同一个 id 再来一次是 ``INSERT OR REPLACE``——推演失败后重跑同一段时间
        是常态，而「重跑一次就多出一条重复消息」会让用户看到自己说过两遍。

        ``last_message_at`` 用 ``CASE`` 比较而不是直接赋值：重跑可能乱序写入，
        而「最后消息时间往回退」会让会话列表的排序变得毫无道理。
        """
        created_at = _format_dt(message.created_at) if message.created_at else _now_iso()
        self._conn.execute(
            "INSERT OR REPLACE INTO message ("
            "id, conversation_id, direction, sender_id, content, content_type, "
            "initiative, motivation, trigger_note, delivered_channels_json, "
            "read_at, replied_at, tick_id, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message.id,
                message.conversation_id,
                message.direction,
                message.sender_id,
                message.content,
                message.content_type,
                int(message.initiative),
                message.motivation or None,
                message.trigger_note or None,
                json.dumps(list(message.delivered_channels), ensure_ascii=False),
                _format_dt(message.read_at) if message.read_at else None,
                _format_dt(message.replied_at) if message.replied_at else None,
                message.tick_id or None,
                created_at,
            ),
        )
        self._conn.execute(
            "UPDATE conversation SET "
            "message_count = message_count + 1, "
            "last_message_at = CASE "
            "  WHEN last_message_at IS NULL OR last_message_at < ? THEN ? "
            "  ELSE last_message_at END "
            "WHERE id = ?",
            (created_at, created_at, message.conversation_id),
        )

    def list_messages(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[MessageRecord]:
        """取**最近的** ``limit`` 条，按时间正序返回。

        用子查询先倒序取够再用外层正序翻回来，是因为「正序 LIMIT」给的是
        **最早**的 N 条——聊天页要的是最新的 N 条，而这两者在任何一段
        超过 N 条的对话里都不一样。
        """
        if before is None:
            rows = self._conn.query(
                "SELECT * FROM ("
                "  SELECT * FROM message WHERE conversation_id = ? "
                "  ORDER BY created_at DESC, id DESC LIMIT ?"
                ") ORDER BY created_at ASC, id ASC",
                (conversation_id, int(limit)),
            )
        else:
            rows = self._conn.query(
                "SELECT * FROM ("
                "  SELECT * FROM message WHERE conversation_id = ? AND created_at < ? "
                "  ORDER BY created_at DESC, id DESC LIMIT ?"
                ") ORDER BY created_at ASC, id ASC",
                (conversation_id, _format_dt(before), int(limit)),
            )
        return [_row_to_message_record(row) for row in rows]

    def count_unread(self, conversation_id: str) -> int:
        """还没回过几条。

        判据是「这条入站消息之后没有出站消息」，不是 ``read_at``：
        已读不回也是一种状态，而它这里关心的是「有人在等它说话」。
        ``COALESCE(..., '')`` 让「从来没回过话」也落到同一个比较里——
        ISO 时间串与空串比较，任何真实时间都大于它。
        """
        rows = self._conn.query(
            "SELECT COUNT(*) AS n FROM message WHERE conversation_id = ? "
            "AND direction = 'inbound' AND created_at > COALESCE("
            "  (SELECT MAX(created_at) FROM message "
            "   WHERE conversation_id = ? AND direction = 'outbound'), '')",
            (conversation_id, conversation_id),
        )
        return int(rows[0]["n"]) if rows else 0

    def last_inbound_at(self, conversation_id: str) -> datetime | None:
        rows = self._conn.query(
            "SELECT MAX(created_at) AS t FROM message "
            "WHERE conversation_id = ? AND direction = 'inbound'",
            (conversation_id,),
        )
        if not rows:
            return None
        return _parse_dt(rows[0]["t"])


class SqliteBudgetRepository:
    """:class:`~alterego.interfaces.repository.BudgetRepository` 的 SQLite 实现。

    整条覆盖而不是增量修改：:class:`~alterego.interfaces.repository.BudgetUsage`
    是 ``sim/budget.py`` 里那些纯函数算出来的**结果**，把「+1」写进 SQL 会让
    预算规则分裂成两处，而它们迟早会不一致。
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: SqliteConnection) -> None:
        self._conn = conn

    def load(self, persona_id: str, *, day: date) -> BudgetUsage:
        rows = self._conn.query(
            "SELECT * FROM budget_usage WHERE persona_id = ? AND day = ?",
            (persona_id, day.isoformat()),
        )
        if not rows:
            return BudgetUsage(day=day)
        row = rows[0]
        return BudgetUsage(
            day=day,
            messages_sent=int(row["messages_sent"] or 0),
            posts_sent=int(row["posts_sent"] or 0),
            messages_suppressed=int(row["messages_suppressed"] or 0),
            posts_suppressed=int(row["posts_suppressed"] or 0),
            consecutive_no_reply=int(row["consecutive_no_reply"] or 0),
            circuit_until=_parse_dt(row["circuit_until"]),
            last_message_at=_parse_dt(row["last_message_at"]),
            last_post_at=_parse_dt(row["last_post_at"]),
        )

    def save(self, persona_id: str, usage: BudgetUsage) -> None:
        self._conn.execute(
            "INSERT INTO budget_usage ("
            "persona_id, day, messages_sent, posts_sent, messages_suppressed, "
            "posts_suppressed, consecutive_no_reply, circuit_until, "
            "last_message_at, last_post_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(persona_id, day) DO UPDATE SET "
            "messages_sent = excluded.messages_sent, "
            "posts_sent = excluded.posts_sent, "
            "messages_suppressed = excluded.messages_suppressed, "
            "posts_suppressed = excluded.posts_suppressed, "
            "consecutive_no_reply = excluded.consecutive_no_reply, "
            "circuit_until = excluded.circuit_until, "
            "last_message_at = excluded.last_message_at, "
            "last_post_at = excluded.last_post_at, "
            "updated_at = excluded.updated_at",
            (
                persona_id,
                usage.day.isoformat(),
                int(usage.messages_sent),
                int(usage.posts_sent),
                int(usage.messages_suppressed),
                int(usage.posts_suppressed),
                int(usage.consecutive_no_reply),
                _format_dt(usage.circuit_until) if usage.circuit_until else None,
                _format_dt(usage.last_message_at) if usage.last_message_at else None,
                _format_dt(usage.last_post_at) if usage.last_post_at else None,
                _now_iso(),
            ),
        )


class SqliteTickLogRepository:
    """:class:`~alterego.interfaces.repository.TickLogRepository` 的 SQLite 实现。

    ``INSERT OR REPLACE`` 让重跑同一段时间是幂等的。所有 ``*_json`` 列在这里
    序列化——``TickLogDraft`` 里装的是真正的 Python 对象，让调用方自己
    ``json.dumps`` 会把「能不能序列化」变成一个运行期惊喜（而且那个惊喜
    出现在一个已经写了一半的事务里）。
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: SqliteConnection) -> None:
        self._conn = conn

    def append(self, draft: TickLogDraft) -> None:
        payload = (
            draft.id,
            draft.persona_id,
            _format_dt(draft.virtual_time),
            int(draft.real_duration_ms),
            draft.status,
            _dump(draft.state_snapshot),
            _dump(draft.percepts),
            _dump(list(draft.candidates)),
            draft.chosen_intent,
            draft.motivation or None,
            draft.trigger_note or None,
            _dump(list(draft.suppressed)),
            _dump(list(draft.memories)),
            _dump(list(draft.notes)),
            _dump(draft.stage_results),
            int(draft.llm_calls),
            int(draft.llm_tokens),
            draft.error,
            _format_dt(draft.created_at),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO tick_log ("
            "id, persona_id, virtual_time, real_duration_ms, status, "
            "state_snapshot_json, percepts_json, candidates_json, chosen_intent, "
            "motivation, trigger_note, suppressed_json, memories_json, notes_json, "
            "stage_results_json, llm_calls, llm_tokens, error, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )


# ── 动态 ────────────────────────────────────────────────────


def _row_to_post(row: sqlite3.Row) -> SocialPostRecord:
    """一行动态。

    ``mood_valence`` / ``mood_arousal`` 是 ``REAL`` 而列可为 ``NULL``：
    发动态那一刻未必有情绪快照（时间倒流到人格刚建好时），
    所以 ``None`` 要原样传出去，不能补成 ``0.0``——``0.0`` 是一句
    「心情平静」的断言，而 ``None`` 是「当时没记」。
    """
    valence = row["mood_valence"]
    arousal = row["mood_arousal"]
    return SocialPostRecord(
        id=str(row["id"]),
        persona_id=str(row["persona_id"]),
        content=str(row["content"]),
        posted_at=_require_post_dt(row["posted_at"]),
        image_paths=tuple(_load_str_list(row["image_paths_json"])),
        location=str(row["location"] or ""),
        mood_label=str(row["mood_label"] or ""),
        mood_valence=None if valence is None else float(valence),
        mood_arousal=None if arousal is None else float(arousal),
        intent_motivation=str(row["intent_motivation"] or ""),
        trigger_note=str(row["trigger_note"] or ""),
        activity_ref=str(row["activity_ref"] or ""),
        like_count=int(row["like_count"] or 0),
        comment_count=int(row["comment_count"] or 0),
        visible=bool(row["visible"]),
        tick_id=str(row["tick_id"] or ""),
    )


def _require_post_dt(value: object) -> datetime:
    """``posted_at`` 是 ``NOT NULL``，解不出来就是库坏了，不静默兜底。"""
    parsed = _parse_dt(value)
    if parsed is None:
        raise StorageError("social_post.posted_at 解不出时间", value=str(value))
    return parsed


class SqliteSocialPostRepository:
    """:class:`~alterego.interfaces.repository.SocialPostRepository` 的 SQLite 实现。"""

    __slots__ = ("_conn",)

    def __init__(self, conn: SqliteConnection) -> None:
        self._conn = conn

    def append(self, post: SocialPostRecord) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO social_post ("
            "id, persona_id, content, image_paths_json, location, mood_label, "
            "mood_valence, mood_arousal, intent_motivation, trigger_note, "
            "activity_ref, like_count, comment_count, visible, tick_id, posted_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                post.id,
                post.persona_id,
                post.content,
                _dump(list(post.image_paths)),
                post.location or None,
                post.mood_label or None,
                post.mood_valence,
                post.mood_arousal,
                post.intent_motivation or None,
                post.trigger_note or None,
                post.activity_ref or None,
                int(post.like_count),
                int(post.comment_count),
                1 if post.visible else 0,
                post.tick_id or None,
                _format_dt(post.posted_at),
            ),
        )

    def list_recent(
        self,
        persona_id: str,
        *,
        limit: int = 30,
        visible_only: bool = True,
    ) -> list[SocialPostRecord]:
        sql = "SELECT * FROM social_post WHERE persona_id = ?"
        if visible_only:
            sql += " AND visible = 1"
        sql += " ORDER BY posted_at DESC, id DESC LIMIT ?"
        rows = self._conn.query(sql, (persona_id, max(1, int(limit))))
        return [_row_to_post(row) for row in rows]

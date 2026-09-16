"""把库里的行变成 JSON。

**为什么不直接 ``json.dumps(asdict(record))``。** 三个理由，都不是洁癖：

1. ``datetime`` 不是 JSON 类型，每个路由都要自己处理一次；
2. **字段是显式列出的**。往库里加一列不会自动出现在浏览器里——自动出现的
   那条路径正好是数据泄漏最安静的一条（新加的列往往是"先随手记一下"的内部字段）；
3. 前端要的字段名与列名本来就该分开：``started_at`` → ``at``、
   ``mood_valence`` → ``mood.valence``。让浏览器读懂数据库命名，
   等于把 schema 焊进前端。

``_iso`` 一律输出**带时区偏移**的 ISO 串。不带偏移的时间在浏览器里会被
``new Date()`` 当成"本地时间"，于是同一句话在两台机器上显示成差几小时的时刻，
而没有任何报错——那种错最难被发现。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from alterego.domain.emotion import Emotion
from alterego.domain.memory import Memory
from alterego.interfaces.repository import (
    ActivityRecord,
    BudgetUsage,
    ConversationRecord,
    MessageRecord,
    PersonaRecord,
    ScheduleRecord,
    SocialPostRecord,
    SourceRecord,
    UsageTotal,
)


__all__ = [
    "activity_view",
    "budget_view",
    "conversation_view",
    "emotion_view",
    "health_view",
    "memory_view",
    "message_view",
    "persona_view",
    "post_view",
    "schedule_view",
    "source_view",
    "usage_total_view",
]


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def message_view(record: MessageRecord) -> dict[str, Any]:
    """一条消息。``direction`` 是 ``inbound`` / ``outbound``，前端据此决定靠左还是靠右。

    **原样透传，不翻译成 ``in`` / ``out``。** ``Channel.direction`` 用的是短
    形式，消息表用的是 ``inbound`` / ``outbound``（``sim/transcript.py`` 里那两个
    常量），而 ``/api/stats`` 数的是后者。在这一层把 ``inbound`` 改写成 ``in``
    只会让「同一列在三个接口里有两个名字」，收益只是前端少打五个字符。

    ``initiative`` 是**布尔**（这一句是不是它自己想说的），不是类别名；
    它主动说的话，说明写在 ``motivation`` 与 ``trigger_note`` 里。
    """
    return {
        "id": record.id,
        "conversation_id": record.conversation_id,
        "direction": record.direction,
        "sender_id": record.sender_id,
        "content": record.content,
        "content_type": record.content_type,
        "initiative": record.initiative,
        "motivation": record.motivation,
        "trigger_note": record.trigger_note,
        "delivered_channels": list(record.delivered_channels),
        "read_at": _iso(record.read_at),
        "replied_at": _iso(record.replied_at),
        "at": _iso(record.created_at),
    }


def conversation_view(record: ConversationRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "persona_id": record.persona_id,
        "counterpart_id": record.counterpart_id,
        "counterpart_kind": record.counterpart_kind,
        "title": record.title,
        "message_count": record.message_count,
        "last_message_at": _iso(record.last_message_at),
        "created_at": _iso(record.created_at),
    }


def post_view(record: SocialPostRecord) -> dict[str, Any]:
    """一条动态。情绪拆成嵌套对象，因为前端总是三个值一起用。"""
    return {
        "id": record.id,
        "content": record.content,
        "at": _iso(record.posted_at),
        "images": list(record.image_paths),
        "location": record.location,
        "mood": {
            "label": record.mood_label,
            "valence": record.mood_valence,
            "arousal": record.mood_arousal,
        },
        "motivation": record.intent_motivation,
        "trigger_note": record.trigger_note,
        "like_count": record.like_count,
        "comment_count": record.comment_count,
    }


def activity_view(record: ActivityRecord) -> dict[str, Any]:
    """一条行为。``inner_voice`` 一并给出——"它当时怎么想"是这个页面的重点。"""
    return {
        "id": record.id,
        "intent": record.intent,
        "description": record.description,
        "at": _iso(record.started_at),
        "category": record.category,
        "location": record.location,
        "inner_voice": record.inner_voice,
        "duration_minutes": record.duration_minutes,
        "tick_id": record.tick_id,
    }


def memory_view(memory: Memory) -> dict[str, Any]:
    return {
        "id": memory.id,
        "kind": memory.kind,
        "content": memory.content,
        "summary": memory.summary,
        "at": _iso(memory.occurred_at),
        "importance": memory.importance,
        "strength": memory.strength,
        "valence": memory.valence,
        "entities": list(memory.entities),
        "tags": list(memory.tags),
        "source": memory.source,
        "recall_count": memory.recall_count,
        "forgotten": memory.forgotten,
    }


def emotion_view(emotion: Emotion) -> dict[str, Any]:
    return {
        "label": emotion.label,
        "valence": emotion.valence,
        "arousal": emotion.arousal,
        "fatigue": emotion.fatigue,
        "at": _iso(emotion.updated_at),
    }


def budget_view(usage: BudgetUsage) -> dict[str, Any]:
    """一天的预算用量。

    ``day`` 是**虚拟时间**的那一天：推演可以 60 倍速跑，所以"今天"
    指的是它自己的今天，不是墙上时间的今天。
    """
    return {
        "day": usage.day.isoformat(),
        "messages_sent": usage.messages_sent,
        "posts_sent": usage.posts_sent,
        "messages_suppressed": usage.messages_suppressed,
        "posts_suppressed": usage.posts_suppressed,
        "consecutive_no_reply": usage.consecutive_no_reply,
        "circuit_until": _iso(usage.circuit_until),
        "last_message_at": _iso(usage.last_message_at),
        "last_post_at": _iso(usage.last_post_at),
    }


def usage_total_view(total: UsageTotal) -> dict[str, Any]:
    """一格 token 用量（按天 / 按用途 / 按模型分好的那一格）。

    **没有金额字段，一行都没有。** 账本里的 ``cost_usd`` 恒为 0——本项目还没有
    价目表（见 ``SqliteUsageRepository`` 的类文档）。把它序列化成 ``0.0``
    再让前端乘上一个猜出来的单价，就是设计文档里点名的那个错误：
    「未配置单价时显示「—」而不是「$0.00」」。要加金额，先有价目表。

    ``total_tokens`` 由服务端算好给出，不让前端拿两个字段相加：
    它是接口的一部分（``LLMUsage.total_tokens`` 也是这么定义的），
    在前端再算一次等于把口径复制到第二个地方。
    """
    return {
        "key": total.key,
        "calls": total.calls,
        "failed": total.failed,
        "succeeded": total.calls - total.failed,
        "prompt_tokens": total.prompt_tokens,
        "completion_tokens": total.completion_tokens,
        "total_tokens": total.total_tokens,
    }


def persona_view(record: PersonaRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "age": record.age,
        "gender": record.gender,
        "city": record.city,
        "occupation": record.occupation,
        "version": record.version,
    }


def schedule_view(record: ScheduleRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "day": record.day.isoformat(),
        "start_at": _iso(record.start_at),
        "end_at": _iso(record.end_at),
        "activity": record.activity,
        "category": record.category,
        "location": record.location,
        "interruptible": record.interruptible,
        "actual_start_at": _iso(record.actual_start_at),
        "actual_end_at": _iso(record.actual_end_at),
        "deviation_note": record.deviation_note,
    }


def source_view(record: SourceRecord) -> dict[str, Any]:
    """它读到的一条信息。

    ``summary`` 是抓取时模型写的那一段，``memory_id`` 是它后来被记成哪条记忆——
    后者为空表示「读了但没记住」，那是一条正常的状态，不是错误。
    """
    return {
        "id": record.id,
        "url": record.url,
        "title": record.title,
        "summary": record.summary,
        "kind": record.source_kind,
        "lang": record.lang,
        "fetched_at": _iso(record.fetched_at),
        "published_at": _iso(record.published_at),
        "memory_id": record.memory_id,
    }


def health_view(statuses: Mapping[str, Any]) -> dict[str, Any]:
    """插件健康状况。写成函数而不是内联在路由里，是为了让测试不必起 HTTP。"""
    return {
        plugin_id: {
            "ok": bool(getattr(status, "ok", False)),
            "detail": str(getattr(status, "detail", "")),
            "hint": getattr(status, "hint", None),
        }
        for plugin_id, status in statuses.items()
    }

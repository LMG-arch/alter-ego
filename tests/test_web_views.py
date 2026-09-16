"""``channels.web.views`` 的测试：库里的行怎么变成 JSON。

这一层存在的理由全都写在它的 docstring 里：``datetime`` 不是 JSON 类型、
字段要**显式列出**（自动出现的那条路径正是数据泄漏最安静的一条）、
前端要的名字与列名本来就该分开。所以这里的用例集中在两件事上——
「哪些字段出去了」与「时间长什么样」。
"""

from __future__ import annotations

from datetime import datetime

from alterego.channels.web.views import (
    activity_view,
    budget_view,
    conversation_view,
    emotion_view,
    health_view,
    memory_view,
    message_view,
    persona_view,
    post_view,
    schedule_view,
    source_view,
)
from alterego.domain.emotion import Emotion
from alterego.domain.memory import Memory
from alterego.interfaces.common import HealthStatus
from alterego.interfaces.repository import (
    ActivityRecord,
    BudgetUsage,
    ConversationRecord,
    MessageRecord,
    PersonaRecord,
    ScheduleRecord,
    SocialPostRecord,
    SourceRecord,
)
from alterego.kernel.clock import resolve_timezone


TZ = resolve_timezone("Asia/Shanghai")
NOW = datetime(2026, 9, 16, 20, 30, tzinfo=TZ)


class TestMessageView:
    def test_the_direction_is_passed_through_verbatim(self) -> None:
        """消息表用的是 ``inbound`` / ``outbound``，**不是** ``Channel.direction`` 的 ``in`` / ``out``。

        在视图层把 ``inbound`` 改写成 ``in`` 只会让同一列在两个接口里有两个名字。
        前提是前端认得这个写法——``app.js`` 的 ``bubble()`` 按 ``inbound`` 判断
        靠左还是靠右，认错一个字的后果是「自己说的话全显示成它说的」。
        """
        inbound = message_view(_message(direction="inbound"))
        outbound = message_view(_message(direction="outbound"))

        assert inbound["direction"] == "inbound"
        assert outbound["direction"] == "outbound"

    def test_initiative_stays_a_boolean(self) -> None:
        """它是「这一句是不是它自己想说的」，不是类别名；说明在 ``motivation`` 里。"""
        view = message_view(_message(direction="outbound", initiative=True, motivation="想起你"))

        assert view["initiative"] is True
        assert view["motivation"] == "想起你"

    def test_the_time_carries_an_offset(self) -> None:
        """不带偏移的时间在浏览器里会被当成「本地时间」，于是同一句话在两台机器上差几小时。"""
        view = message_view(_message())

        assert view["at"] == NOW.isoformat()
        assert view["at"].endswith("+08:00")

    def test_missing_times_are_null_not_the_string_none(self) -> None:
        """``"None"`` 会被前端当成一个合法时间，而 ``null`` 会被当成「没有」。"""
        view = message_view(_message())

        assert view["read_at"] is None
        assert view["replied_at"] is None

    def test_delivered_channels_becomes_a_list(self) -> None:
        """元组过不去 JSON，而且列表才是前端期待的形状。"""
        view = message_view(_message(delivered_channels=("web", "file")))

        assert view["delivered_channels"] == ["web", "file"]

    def test_the_tick_id_does_not_leave_the_building(self) -> None:
        """``tick_id`` 是推演的内部坐标，页面上没有任何地方会用到它。"""
        assert "tick_id" not in message_view(_message())


class TestOtherViews:
    def test_a_post_gives_its_mood_as_one_object(self) -> None:
        """三个值前端总是一起用，摊平成 ``mood_label`` 只是让页面多写三行。"""
        view = post_view(_post())

        assert view["mood"] == {"label": "平静", "valence": 0.2, "arousal": 0.3}
        assert view["at"] == NOW.isoformat()

    def test_a_budget_day_is_a_date_string(self) -> None:
        view = budget_view(_usage())

        assert view["day"] == "2026-09-16"
        assert view["circuit_until"] is None

    def test_a_source_keeps_its_published_time_separate_from_the_fetch_time(self) -> None:
        """一条三年前写的文章今天被读到：两个时间混在一起就再也分不清了。"""
        view = source_view(_source())

        assert view["fetched_at"] == NOW.isoformat()
        assert view["published_at"] is None
        assert view["memory_id"] is None

    def test_a_memory_lists_come_out_as_lists(self) -> None:
        view = memory_view(_memory())

        assert view["entities"] == ["林晚"]
        assert view["forgotten"] is False

    def test_a_persona_does_not_carry_the_whole_document(self) -> None:
        """``PersonaRecord`` 故意不带 ``persona_json``——它只要一句「我是谁」。"""
        view = persona_view(PersonaRecord(id="p1", name="林晚", occupation="算法工程师"))

        assert view["name"] == "林晚"
        assert "persona_json" not in view

    def test_a_schedule_keeps_the_plan_and_the_reality_apart(self) -> None:
        """只有计划没有实际，笔记就成了一张没人兑现的时间表。"""
        view = schedule_view(_schedule())

        assert view["day"] == "2026-09-16"
        assert view["start_at"] == NOW.isoformat()
        assert view["actual_start_at"] is None

    def test_an_activity_carries_the_inner_voice(self) -> None:
        """「它当时怎么想」是这一页的重点，少了它这一页就只是一张流水账。"""
        view = activity_view(_activity())

        assert view["inner_voice"] == "有点困"
        assert view["duration_minutes"] == 30

    def test_an_emotion_is_a_flat_reading(self) -> None:
        view = emotion_view(_emotion())

        assert view["label"] == "平静"
        assert view["fatigue"] == 0.2
        assert view["at"] == NOW.isoformat()

    def test_a_conversation_reports_its_last_message_time(self) -> None:
        view = conversation_view(_conversation())

        assert view["message_count"] == 3
        assert view["last_message_at"] == NOW.isoformat()


class TestHealthView:
    def test_plugin_health_turns_into_plain_json(self) -> None:
        """它写成函数而不是内联在路由里，就是为了让这条断言不必起 HTTP。"""
        view = health_view({"tool.x": HealthStatus(ok=True, detail="好", hint="没事")})

        assert view == {"tool.x": {"ok": True, "detail": "好", "hint": "没事"}}

    def test_an_object_without_those_attributes_does_not_crash(self) -> None:
        """``health_all()`` 的返回值是插件自己给的，宽容读取比要求它写得一字不差现实。"""
        view = health_view({"tool.x": object()})

        assert view == {"tool.x": {"ok": False, "detail": "", "hint": None}}


# ── 造样本 ──────────────────────────────────────────────────


def _message(**kwargs: object) -> MessageRecord:
    base: dict[str, object] = {
        "id": "m1",
        "conversation_id": "c1",
        "direction": "inbound",
        "sender_id": "user",
        "content": "在忙什么",
        "tick_id": "t1",
        "created_at": NOW,
    }
    base.update(kwargs)
    return MessageRecord(**base)  # type: ignore[arg-type]


def _post() -> SocialPostRecord:
    return SocialPostRecord(
        id="s1",
        persona_id="p1",
        content="今天写了一天代码",
        posted_at=NOW,
        image_paths=("a.png",),
        mood_label="平静",
        mood_valence=0.2,
        mood_arousal=0.3,
        intent_motivation="想记一下",
        like_count=2,
        comment_count=0,
    )


def _usage() -> BudgetUsage:
    return BudgetUsage(
        day=NOW.date(),
        messages_sent=1,
        messages_suppressed=2,
        consecutive_no_reply=0,
    )


def _source() -> SourceRecord:
    return SourceRecord(id="src1", url="https://example.test/a", fetched_at=NOW, title="一篇")


def _memory() -> Memory:
    return Memory(
        persona_id="p1",
        kind="episodic",
        content="和林晚聊了会儿",
        summary="聊了一会儿",
        occurred_at=NOW,
        entities=("林晚",),
    )


def _schedule() -> ScheduleRecord:
    return ScheduleRecord(id="sc1", day=NOW.date(), start_at=NOW, end_at=NOW, activity="写代码")


def _activity() -> ActivityRecord:
    return ActivityRecord(
        id="a1",
        intent="work",
        description="写代码",
        started_at=NOW,
        inner_voice="有点困",
        duration_minutes=30,
    )


def _emotion() -> Emotion:
    return Emotion(label="平静", valence=0.2, arousal=0.3, fatigue=0.2, updated_at=NOW)


def _conversation() -> ConversationRecord:
    return ConversationRecord(
        id="c1",
        persona_id="p1",
        counterpart_id="user",
        counterpart_kind="user",
        message_count=3,
        last_message_at=NOW,
    )

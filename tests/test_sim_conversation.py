"""``alterego.sim.conversation`` 的测试。

会话是「用户按下回车之后发生的事」，所以这里测的不是模型说了什么漂亮话，
而是**四条用户看得见的保证**：

1. **用户说的那句话一定会落库**，哪怕后面模型炸了。它是这套系统里唯一
   无法重新生成的东西。
2. **「它这一轮不回」是一个正常结果，不是错误。** ``reply is None`` 加上
   一条查得到的理由，而不是一个异常。
3. **同一个世界。** 会话路径与 tick 路径读快照、算延迟、记账用的是同一批
   函数；一旦分岔，症状是「同一时刻、同一个它，两条路径上不一样」。
4. **它不会复读自己**，而且重写那一遍不会把两条都落库。

装配全是假的：真的 SQLite 已在 ``tests/test_storage_repositories.py`` 里验过，
真的提示词库（``PromptLibrary()``）在这里是需要的——因为要断言的就是
「喂给模型的那段话」。

依据: docs/design/12-calendar-and-conversation.md § 10–12
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from random import Random
from types import SimpleNamespace
from typing import Any

from alterego.domain.emotion import Emotion, infer_label
from alterego.domain.schedule import ScheduleBlock
from alterego.interfaces.repository import (
    BudgetUsage,
    ConversationRecord,
    MessageRecord,
    ScheduleRecord,
    SocialPostRecord,
    TickLogDraft,
)
from alterego.kernel.config import DisturbBudgetConfig
from alterego.llm.prompts import PromptLibrary
from alterego.sim.conversation import ConversationService, InboundMessage
from alterego.sim.engine import EnginePorts, user_conversation_id
from alterego.sim.persona_view import PersonaView
from alterego.sim.transcript import (
    instant_streak,
    last_inbound_text,
    last_topic_at,
    passive_streak,
)


NOW = datetime(2026, 9, 15, 14, 0)
DAY = date(2026, 9, 15)
PERSONA_ID = "p1"
CONVERSATION_ID = f"{PERSONA_ID}:user"


# ── 造世界的小工具 ──────────────────────────────────────────


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


def make_schedule_row(**overrides: Any) -> ScheduleRecord:
    """库里的那一行。快照读的是它，转成 ``ScheduleBlock`` 是 ``to_schedule_block`` 的事。"""
    values: dict[str, Any] = {
        "id": "b1",
        "day": DAY,
        "start_at": datetime(2026, 9, 15, 13, 0),
        "end_at": datetime(2026, 9, 15, 18, 0),
        "activity": "写代码",
        "category": "work",
        "interruptible": True,
        "location": "公司",
    }
    values.update(overrides)
    return ScheduleRecord(**values)


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


def make_message(**overrides: Any) -> MessageRecord:
    values: dict[str, Any] = {
        "id": "m1",
        "conversation_id": CONVERSATION_ID,
        "direction": "inbound",
        "sender_id": "user",
        "content": "在吗",
        "created_at": NOW - timedelta(minutes=5),
    }
    values.update(overrides)
    return MessageRecord(**values)


def instant_history(turns: int) -> list[MessageRecord]:
    """``turns`` 轮「用户问、它秒回」。用来触发「连着秒回太多了，放一放」那条规则。

    顺序必须是**先入站再出站**：``instant_streak`` 是拿后一条出站配前一条入站
    算间隔的，反过来配会算出负数间隔，而负数会被当成「不超时」。
    """
    rows: list[MessageRecord] = []
    for index in range(turns):
        asked = NOW - timedelta(minutes=10 - index)
        rows.append(make_message(id=f"i{index}", content="在吗", created_at=asked))
        rows.append(
            make_message(
                id=f"o{index}",
                direction="outbound",
                sender_id=PERSONA_ID,
                content="在",
                created_at=asked + timedelta(seconds=2),
            )
        )
    return rows


def passive_history(turns: int) -> list[MessageRecord]:
    """``turns`` 轮「用户问、它答」，一问一答都不带 ``initiative``。"""
    rows: list[MessageRecord] = []
    for index in range(turns):
        asked = NOW - timedelta(minutes=20 - index * 2)
        rows.append(make_message(id=f"pi{index}", content=f"第 {index} 个问题", created_at=asked))
        rows.append(
            make_message(
                id=f"po{index}",
                direction="outbound",
                sender_id=PERSONA_ID,
                content=f"第 {index} 个回答",
                created_at=asked + timedelta(seconds=10),
            )
        )
    return rows


# ── 假的协作者 ──────────────────────────────────────────────


class FakeClock:
    """虚拟时钟。会话服务只调 ``virtual_now()``。"""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def virtual_now(self) -> datetime:
        return self.now


class FakeBackend:
    """只提供事务。"""

    def __init__(self) -> None:
        self.transactions = 0

    def transaction(self) -> Any:
        self.transactions += 1
        return nullcontext()


class FakePersonas:
    def __init__(self) -> None:
        self.documents = 0

    def get(self, persona_id: str) -> Any:
        return None

    def find_by_name(self, name: str) -> Any:
        return None

    def list_all(self) -> list[Any]:
        return []

    def document(self, persona_id: str) -> dict[str, Any]:
        self.documents += 1
        return {"summary": "我是林晚，做算法的。"}


class FakeEmotions:
    def __init__(self, current: Emotion | None = None) -> None:
        self.current = current

    def latest(self, persona_id: str) -> Emotion | None:
        return self.current

    def append(self, persona_id: str, emotion: Emotion, **kwargs: Any) -> None:
        self.current = emotion


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

    def list_recent(self, persona_id: str, *, since: datetime, limit: int = 100) -> list[Any]:
        return self.rows[:limit]


class FakeActivities:
    def __init__(self) -> None:
        self.written: list[Any] = []

    def list_range(self, persona_id: str, *, since: datetime, until: datetime, limit: int = 500):
        return []

    def append(self, records: Sequence[Any]) -> None:
        self.written.extend(records)


class FakeConversations:
    """一张内存里的会话表。

    ``ensure`` 幂等（库里那行是主键），``append`` 幂等（同 id 再来一次是覆盖）——
    这两条正是真仓储的契约，假仓储不照做就测不出「重投递不会多一行」。
    """

    def __init__(
        self,
        *,
        messages: Sequence[MessageRecord] = (),
        unread: int = 0,
        last_inbound: datetime | None = None,
    ) -> None:
        self.rows: list[ConversationRecord] = []
        self.messages: list[MessageRecord] = list(messages)
        self.unread = unread
        self.last_inbound = last_inbound

    def ensure(self, record: ConversationRecord) -> ConversationRecord:
        for row in self.rows:
            if row.id == record.id:
                return row
        self.rows.append(record)
        return record

    def append(self, message: MessageRecord) -> None:
        for index, row in enumerate(self.messages):
            if row.id == message.id:
                self.messages[index] = message
                return
        self.messages.append(message)

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

    @property
    def outbound(self) -> list[MessageRecord]:
        """它说过的话。大多数断言关心的是这个。"""
        return [row for row in self.messages if row.direction == "outbound"]

    @property
    def inbound(self) -> list[MessageRecord]:
        return [row for row in self.messages if row.direction == "inbound"]


class FakeBudgets:
    def __init__(self, usage: BudgetUsage | None = None) -> None:
        self.usage = usage
        self.saved: list[tuple[str, BudgetUsage]] = []

    def load(self, persona_id: str, *, day: date) -> BudgetUsage:
        if self.usage is not None and self.usage.day == day:
            return self.usage
        return BudgetUsage(day=day)

    def save(self, persona_id: str, usage: BudgetUsage) -> None:
        self.saved.append((persona_id, usage))
        self.usage = usage


class FakePosts:
    def append(self, post: SocialPostRecord) -> None:  # pragma: no cover - 会话路径不发动态
        raise AssertionError("会话路径不该发动态")

    def list_recent(self, persona_id: str, *, limit: int = 30, visible_only: bool = True):
        return []


class FakeTickLogs:
    def append(self, draft: TickLogDraft) -> None:  # pragma: no cover - 会话路径不写 tick 日志
        raise AssertionError("会话路径不该写 tick 日志")


def make_ports(**overrides: Any) -> EnginePorts:
    """十张嘴都换成假的。想换哪个就在 ``overrides`` 里点名。"""
    values: dict[str, Any] = {
        "backend": FakeBackend(),
        "personas": FakePersonas(),
        "emotions": FakeEmotions(make_emotion()),
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


class FixedRandom(Random):
    """``random()`` 恒定返回同一个值的随机源。

    ``decide_reply`` 的延迟与 ``should_open_topic`` 的最后一个分支都是
    「摇一个数落进哪个区间」。断言「它这一轮会开话题」必须把那个数钉死，
    否则测试会随概率翻车。
    """

    def __init__(self, value: float = 0.5) -> None:
        super().__init__(0)
        self.value = value

    def random(self) -> float:
        return self.value


class StepRandom(Random):
    """按顺序吐出预先定好的几个数，用完之后重复最后一个。"""

    def __init__(self, *values: float) -> None:
        super().__init__(0)
        self.values = list(values) or [0.5]
        self.calls = 0

    def random(self) -> float:
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return value


class FakeGateway:
    """假网关。``replies`` 按顺序吐，用完之后一直吐最后一条。

    参数顺序跟真网关一致：``complete(purpose, prompt, ...)``。
    """

    def __init__(self, *replies: Any) -> None:
        self.replies: list[Any] = list(replies) or ["嗯，我在"]
        self.purposes: list[str] = []
        self.prompts: list[str] = []

    @property
    def calls(self) -> list[str]:
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


# ── 装配 ────────────────────────────────────────────────────


def make_service(
    *,
    ports: EnginePorts | None = None,
    gateway: Any = None,
    persona: PersonaView | None = None,
    clock: Any = None,
    random: Random | None = None,
    new_id: Any = None,
) -> ConversationService:
    return ConversationService(
        persona=make_persona() if persona is None else persona,
        ports=make_ports() if ports is None else ports,
        clock=FakeClock() if clock is None else clock,
        prompts=PromptLibrary(),
        budget=DisturbBudgetConfig(),
        gateway=gateway,
        history_limit=20,
        random=FixedRandom(0.5) if random is None else random,
        new_id=(lambda: "c1") if new_id is None else new_id,
    )


# ── 一、用户说的话必须先落库 ────────────────────────────────


class TestTheUserMessageLandsFirst:
    async def test_both_sides_of_the_conversation_are_stored(self) -> None:
        ports = make_ports()
        service = make_service(ports=ports, gateway=FakeGateway("嗯，我在"))

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert [row.direction for row in ports.conversations.messages] == ["inbound", "outbound"]
        assert ports.conversations.inbound[0].content == "在吗"
        assert outcome.text == "嗯，我在"

    async def test_the_conversation_is_created_with_the_user_as_counterpart(self) -> None:
        ports = make_ports()
        service = make_service(ports=ports, gateway=FakeGateway())

        await service.reply(InboundMessage(content="在吗"))

        assert [row.id for row in ports.conversations.rows] == [CONVERSATION_ID]
        assert ports.conversations.rows[0].counterpart_kind == "user"

    async def test_the_second_message_reuses_the_same_conversation(self) -> None:
        ports = make_ports()
        service = make_service(ports=ports, gateway=FakeGateway())

        await service.reply(InboundMessage(content="在吗"))
        await service.reply(InboundMessage(content="忙不忙"))

        assert len(ports.conversations.rows) == 1

    async def test_the_user_message_survives_a_broken_model(self) -> None:
        """模型炸了也不能丢用户说的话——它是唯一无法重新生成的东西。"""
        ports = make_ports()
        gateway = FakeGateway(RuntimeError("上游 500"))
        service = make_service(ports=ports, gateway=gateway)

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert ports.conversations.inbound[0].content == "在吗"
        assert outcome.reply is None

    async def test_the_channel_of_the_message_is_recorded(self) -> None:
        ports = make_ports()
        service = make_service(ports=ports, gateway=FakeGateway())

        await service.reply(InboundMessage(content="在吗", channel="web"))

        assert ports.conversations.inbound[0].delivered_channels == ("web",)

    async def test_a_channel_supplied_id_is_used_verbatim(self) -> None:
        """渠道自己的消息 id 能保证重投递不会多出一行。"""
        ports = make_ports()
        service = make_service(ports=ports, gateway=FakeGateway())

        await service.reply(InboundMessage(content="在吗", message_id="wx-1"))
        await service.reply(InboundMessage(content="在吗", message_id="wx-1"))

        assert [row.id for row in ports.conversations.inbound] == ["wx-1"]
        assert len(ports.conversations.rows) == 1

    async def test_the_arrival_time_is_honoured(self) -> None:
        early = NOW - timedelta(minutes=3)
        ports = make_ports()
        service = make_service(ports=ports, gateway=FakeGateway())

        await service.reply(InboundMessage(content="在吗", at=early))

        assert ports.conversations.inbound[0].created_at == early


# ── 二、「这一轮不回」是正常结果 ────────────────────────────


class TestSilenceIsAnOutcomeNotAnError:
    async def test_exhausted_fatigue_makes_it_say_nothing(self) -> None:
        ports = make_ports(emotions=FakeEmotions(make_emotion(fatigue=0.99)))
        gateway = FakeGateway("说了也不该用")
        service = make_service(ports=ports, gateway=gateway)

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.replied is False
        assert outcome.text == ""
        assert outcome.reply is None
        assert gateway.calls == []  # 一次模型调用都没花

    async def test_the_reason_is_readable(self) -> None:
        ports = make_ports(emotions=FakeEmotions(make_emotion(fatigue=0.99)))
        service = make_service(ports=ports, gateway=FakeGateway())

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.decision.mode == "silent"
        assert "不想说话" in outcome.decision.reason

    async def test_the_suppression_leaves_a_row_behind(self) -> None:
        """用户问「它明明想回怎么没回」，答案必须查得到。"""
        ports = make_ports(emotions=FakeEmotions(make_emotion(fatigue=0.99)))
        service = make_service(ports=ports, gateway=FakeGateway())

        await service.reply(InboundMessage(content="在吗"))

        assert len(ports.activities.written) == 1
        assert ports.activities.written[0].category == "outbound"
        assert "没做" in ports.activities.written[0].description

    async def test_a_silent_round_stores_no_outbound_message(self) -> None:
        ports = make_ports(emotions=FakeEmotions(make_emotion(fatigue=0.99)))
        service = make_service(ports=ports, gateway=FakeGateway())

        await service.reply(InboundMessage(content="在吗"))

        assert ports.conversations.outbound == []

    async def test_a_broken_model_is_a_note_not_an_exception(self) -> None:
        ports = make_ports()
        service = make_service(ports=ports, gateway=FakeGateway(RuntimeError("上游 500")))

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.replied is False
        assert any("模型" in note or "没有" in note for note in outcome.notes)

    async def test_without_a_gateway_it_answers_the_question_anyway(self) -> None:
        """``gateway=None`` 是合法装配（``--dry-run``、纯测试）。"""
        ports = make_ports()
        service = make_service(ports=ports, gateway=None)

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.replied is False
        assert outcome.prompt  # 提示词照样组出来了，只是没发出去


# ── 三、延迟算出来但不睡 ────────────────────────────────────


class TestTheDelayIsComputedNotSlept:
    async def test_a_busy_block_pushes_the_reply_out(self) -> None:
        rows = [make_schedule_row(activity="开会", category="meeting", interruptible=False)]
        ports = make_ports(schedules=FakeSchedules(rows))
        service = make_service(ports=ports, gateway=FakeGateway())

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.decision.mode == "much_later"
        assert outcome.delay_seconds > 0
        assert "开会" in outcome.decision.reason

    async def test_a_busy_reply_is_timestamped_in_the_future(self) -> None:
        rows = [make_schedule_row(activity="开会", category="meeting", interruptible=False)]
        ports = make_ports(schedules=FakeSchedules(rows))
        service = make_service(ports=ports, gateway=FakeGateway())

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.reply is not None
        assert outcome.reply.created_at is not None
        assert outcome.reply.created_at > NOW

    async def test_a_short_message_is_answered_right_away(self) -> None:
        ports = make_ports()
        service = make_service(ports=ports, gateway=FakeGateway())

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.decision.mode == "instant"
        assert outcome.delay_seconds <= 6.0

    async def test_three_instant_replies_in_a_row_slow_it_down(self) -> None:
        ports = make_ports(conversations=FakeConversations(messages=instant_history(3)))
        service = make_service(ports=ports, gateway=FakeGateway())

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.decision.mode == "normal"
        assert "秒回" in outcome.decision.reason

    async def test_the_reason_travels_with_the_message(self) -> None:
        ports = make_ports()
        service = make_service(ports=ports, gateway=FakeGateway())

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.reply is not None
        assert outcome.reply.trigger_note == outcome.decision.reason


# ── 四、提示词 ──────────────────────────────────────────────


class TestThePrompt:
    async def test_the_user_message_reaches_the_model(self) -> None:
        gateway = FakeGateway()
        service = make_service(gateway=gateway)

        await service.reply(InboundMessage(content="今天在忙什么呀"))

        assert "今天在忙什么呀" in gateway.calls[0]

    async def test_the_prompt_is_the_one_actually_sent(self) -> None:
        """``--show-prompt`` 展示的必须**就是**发出去的那一份，不是重建的。"""
        gateway = FakeGateway()
        service = make_service(gateway=gateway)

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.prompt == gateway.calls[0]

    async def test_the_reply_purpose_is_used(self) -> None:
        gateway = FakeGateway()
        service = make_service(gateway=gateway)

        await service.reply(InboundMessage(content="在吗"))

        assert gateway.purposes == ["expression"]

    async def test_a_broken_model_reports_itself_in_the_prompt(self) -> None:
        """模型没了也要能看见「本来打算喂它什么」。"""
        service = make_service(gateway=FakeGateway(RuntimeError("上游 500")))

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert "在吗" in outcome.prompt


# ── 五、它不复读自己 ────────────────────────────────────────


class TestRepetition:
    async def test_a_repeat_is_rewritten_once(self) -> None:
        history = [
            make_message(
                id="o1",
                direction="outbound",
                sender_id=PERSONA_ID,
                content="嗯嗯，我在的",
            )
        ]
        ports = make_ports(conversations=FakeConversations(messages=history))
        gateway = FakeGateway("嗯嗯，我在的", "在看代码呢，你说")
        service = make_service(ports=ports, gateway=gateway)

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.repeated_from == "嗯嗯，我在的"
        assert outcome.text == "在看代码呢，你说"
        assert len(gateway.calls) == 2

    async def test_the_rewrite_is_told_what_not_to_say(self) -> None:
        history = [
            make_message(
                id="o1",
                direction="outbound",
                sender_id=PERSONA_ID,
                content="嗯嗯，我在的",
            )
        ]
        ports = make_ports(conversations=FakeConversations(messages=history))
        gateway = FakeGateway("嗯嗯，我在的", "在看代码呢，你说")
        service = make_service(ports=ports, gateway=gateway)

        await service.reply(InboundMessage(content="在吗"))

        assert "别又是这一句" in gateway.calls[1]

    async def test_only_the_final_version_is_stored(self) -> None:
        history = [
            make_message(
                id="o1",
                direction="outbound",
                sender_id=PERSONA_ID,
                content="嗯嗯，我在的",
            )
        ]
        ports = make_ports(conversations=FakeConversations(messages=history))
        service = make_service(ports=ports, gateway=FakeGateway("嗯嗯，我在的", "在看代码呢，你说"))

        await service.reply(InboundMessage(content="在吗"))

        assert [row.content for row in ports.conversations.outbound if row.id != "o1"] == [
            "在看代码呢，你说"
        ]

    async def test_a_fresh_sentence_is_not_rewritten(self) -> None:
        history = [
            make_message(id="o1", direction="outbound", sender_id=PERSONA_ID, content="好嘞~")
        ]
        ports = make_ports(conversations=FakeConversations(messages=history))
        gateway = FakeGateway("刚下班，在地铁上")
        service = make_service(ports=ports, gateway=gateway)

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.repeated_from is None
        assert len(gateway.calls) == 1

    async def test_the_users_own_words_are_not_treated_as_repetition(self) -> None:
        """复读的定义是「它又把同一句拿出来了」，用户重复问不是它的错。"""
        history = [make_message(id="i1", direction="inbound", content="在吗")]
        ports = make_ports(conversations=FakeConversations(messages=history))
        gateway = FakeGateway("在吗")
        service = make_service(ports=ports, gateway=gateway)

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.repeated_from is None
        assert len(gateway.calls) == 1


# ── 六、主动开话题 ──────────────────────────────────────────


class TestOpeningATopic:
    def _passive_history(self, turns: int) -> list[MessageRecord]:
        return passive_history(turns)

    async def test_enough_passive_turns_lets_it_start_one(self) -> None:
        ports = make_ports(conversations=FakeConversations(messages=self._passive_history(4)))
        service = make_service(
            ports=ports, gateway=FakeGateway("在看代码，你呢"), random=FixedRandom(0.0)
        )

        outcome = await service.reply(InboundMessage(content="哦"))

        assert outcome.topic is not None
        assert outcome.topic.open_topic is True
        assert outcome.reply is not None
        assert outcome.reply.initiative is True
        assert outcome.reply.motivation == outcome.topic.reason

    async def test_a_fresh_conversation_does_not_open_one(self) -> None:
        ports = make_ports()
        service = make_service(ports=ports, gateway=FakeGateway("在呢"))

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.topic is not None
        assert outcome.topic.open_topic is False
        assert "先顺着" in outcome.topic.reason

    async def test_a_quiet_reply_is_not_marked_as_initiative(self) -> None:
        ports = make_ports()
        service = make_service(ports=ports, gateway=FakeGateway("在呢"))

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.reply is not None
        assert outcome.reply.initiative is False

    async def test_the_instruction_to_open_a_topic_reaches_the_model(self) -> None:
        ports = make_ports(conversations=FakeConversations(messages=self._passive_history(4)))
        gateway = FakeGateway("在看代码，你呢")
        service = make_service(ports=ports, gateway=gateway, random=FixedRandom(0.0))

        await service.reply(InboundMessage(content="哦"))

        assert "自己抛一个新话题" in gateway.calls[0]

    async def test_a_topic_just_opened_cools_down(self) -> None:
        history = self._passive_history(4)
        history.append(
            make_message(
                id="o-topic",
                direction="outbound",
                sender_id=PERSONA_ID,
                content="对了，我跟你说个事",
                initiative=True,
                created_at=NOW - timedelta(minutes=5),
            )
        )
        ports = make_ports(conversations=FakeConversations(messages=history))
        service = make_service(ports=ports, gateway=FakeGateway())

        outcome = await service.reply(InboundMessage(content="哦"))

        assert outcome.topic is not None
        assert outcome.topic.open_topic is False
        assert "别一直换话题" in outcome.topic.reason

    async def test_an_uninterruptible_block_blocks_the_topic_too(self) -> None:
        rows = [make_schedule_row(activity="开会", category="meeting", interruptible=False)]
        ports = make_ports(
            schedules=FakeSchedules(rows),
            conversations=FakeConversations(messages=self._passive_history(4)),
        )
        service = make_service(ports=ports, gateway=FakeGateway())

        outcome = await service.reply(InboundMessage(content="哦"))

        assert outcome.topic is not None
        assert outcome.topic.open_topic is False
        assert "开会" in outcome.topic.reason


# ── 七、同一个世界 ──────────────────────────────────────────


class TestTheSameWorld:
    async def test_the_history_from_the_database_reaches_the_prompt(self) -> None:
        history = [
            make_message(id="i1", direction="inbound", content="晚饭吃了吗"),
            make_message(
                id="o1",
                direction="outbound",
                sender_id=PERSONA_ID,
                content="吃了，煮的面",
            ),
        ]
        ports = make_ports(conversations=FakeConversations(messages=history))
        gateway = FakeGateway()
        service = make_service(ports=ports, gateway=gateway)

        await service.reply(InboundMessage(content="好吃吗"))

        assert "晚饭吃了吗" in gateway.calls[0]
        assert "吃了，煮的面" in gateway.calls[0]

    async def test_the_schedule_block_reaches_the_prompt(self) -> None:
        ports = make_ports(schedules=FakeSchedules([make_schedule_row(activity="写周报")]))
        gateway = FakeGateway()
        service = make_service(ports=ports, gateway=gateway)

        await service.reply(InboundMessage(content="在忙什么"))

        assert "写周报" in gateway.calls[0]

    async def test_the_memories_reach_the_prompt(self) -> None:
        ports = make_ports(memories=FakeMemories(["上周去看了展"]))
        gateway = FakeGateway()
        service = make_service(ports=ports, gateway=gateway)

        await service.reply(InboundMessage(content="在忙什么"))

        assert "上周去看了展" in gateway.calls[0]

    async def test_the_reply_is_charged_to_the_same_budget_column_as_a_tick(self) -> None:
        """两条路径记的账必须落进同一列，否则熔断会在一条路径上失灵。"""
        ports = make_ports()
        service = make_service(ports=ports, gateway=FakeGateway())

        await service.reply(InboundMessage(content="在吗"))

        assert ports.budgets.saved, "回话没记账"
        assert ports.budgets.saved[0][1].messages_sent == 1

    async def test_a_reply_clears_the_no_reply_streak(self) -> None:
        ports = make_ports(budgets=FakeBudgets(BudgetUsage(day=DAY, consecutive_no_reply=2)))
        service = make_service(ports=ports, gateway=FakeGateway())

        await service.reply(InboundMessage(content="在吗"))

        assert ports.budgets.saved[0][1].consecutive_no_reply == 0

    async def test_the_conversation_id_matches_the_tick_path(self) -> None:
        service = make_service()

        assert service.conversation_id == user_conversation_id(PERSONA_ID)


# ── 八、可复现 ──────────────────────────────────────────────


class TestReproducibility:
    async def test_the_same_seed_gives_the_same_delay(self) -> None:
        async def run() -> float:
            service = make_service(gateway=FakeGateway(), random=Random("x"))
            outcome = await service.reply(InboundMessage(content="在吗"))
            return outcome.delay_seconds

        assert await run() == await run()

    async def test_two_messages_in_the_same_second_do_not_share_a_draw(self) -> None:
        """同一颗随机源连用两次会让两条消息拿到同一个数，那是随机数复用而不是可复现。"""
        service = make_service(
            gateway=FakeGateway("在看代码呢", "刚散会，怎么了"),
            random=StepRandom(0.0, 0.5, 0.9, 0.1),
        )

        first = await service.reply(InboundMessage(content="今天在忙什么呀"))
        second = await service.reply(InboundMessage(content="今天在忙什么呀"))

        assert first.delay_seconds != second.delay_seconds

    async def test_neither_clock_is_read_directly(self) -> None:
        """所有时间都来自注入的时钟，这个测试把「墙上时钟」钉在假时钟上。"""
        clock = FakeClock(datetime(2027, 1, 1, 3, 0))
        ports = make_ports()
        service = make_service(ports=ports, clock=clock, gateway=FakeGateway())

        await service.reply(InboundMessage(content="在吗"))

        assert ports.conversations.inbound[0].created_at == datetime(2027, 1, 1, 3, 0)


# ── 九、五个计数纯函数 ──────────────────────────────────────


class TestTranscriptCounters:
    def test_the_last_user_message_is_the_newest_inbound_one(self) -> None:
        rows = [
            make_message(id="i1", content="第一句"),
            make_message(id="o1", direction="outbound", content="回答"),
            make_message(id="i2", content="第二句"),
            make_message(id="o2", direction="outbound", content="回答二"),
        ]

        assert last_inbound_text(rows) == "第二句"

    def test_no_user_message_means_empty_text_not_none(self) -> None:
        assert last_inbound_text([make_message(direction="outbound", content="喂")]) == ""

    def test_three_back_to_back_replies_count_as_a_streak(self) -> None:
        assert instant_streak(instant_history(3), within_seconds=60.0) == 3

    def test_a_slow_reply_breaks_the_streak(self) -> None:
        rows = [
            make_message(id="i1", content="喂", created_at=NOW),
            make_message(
                id="o1", direction="outbound", content="在", created_at=NOW + timedelta(hours=1)
            ),
        ]

        assert instant_streak(rows, within_seconds=60.0) == 0

    def test_a_conversation_it_started_does_not_count_as_passive(self) -> None:
        rows = [
            make_message(id="i1", content="喂"),
            make_message(id="o1", direction="outbound", content="在", initiative=True),
        ]

        assert passive_streak(rows) == 0

    def test_two_answered_turns_count_as_two_passive_turns(self) -> None:
        assert passive_streak(passive_history(2)) == 2

    def test_never_having_started_a_topic_is_not_the_same_as_long_ago(self) -> None:
        rows = [make_message(id="i1", content="喂")]

        assert last_topic_at(rows) is None

    def test_the_topic_timestamp_is_the_last_initiative_message(self) -> None:
        rows = [
            make_message(id="i1", content="喂"),
            make_message(
                id="o1",
                direction="outbound",
                content="对了",
                initiative=True,
                created_at=NOW - timedelta(hours=2),
            ),
            make_message(id="i2", content="喂"),
            make_message(
                id="o2",
                direction="outbound",
                content="嗯",
                initiative=False,
                created_at=NOW - timedelta(minutes=5),
            ),
        ]

        assert last_topic_at(rows) == NOW - timedelta(hours=2)


# ── 十、渠道契约 ────────────────────────────────────────────


class TestOutcomeShape:
    async def test_a_missing_reply_carries_no_text_instead_of_erroring(self) -> None:
        ports = make_ports(emotions=FakeEmotions(make_emotion(fatigue=0.99)))
        service = make_service(ports=ports, gateway=FakeGateway())

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.reply is None
        assert outcome.text == ""
        assert outcome.delay_seconds == 0.0

    async def test_the_notes_explain_what_happened(self) -> None:
        ports = make_ports()
        service = make_service(ports=ports, gateway=FakeGateway())

        outcome = await service.reply(InboundMessage(content="在吗"))

        assert outcome.notes

    async def test_it_uses_the_injected_id_generator(self) -> None:
        ports = make_ports()
        service = make_service(ports=ports, gateway=FakeGateway(), new_id=lambda: "chat-42")

        await service.reply(InboundMessage(content="在吗"))

        assert ports.conversations.outbound[0].tick_id == "chat-42"

    async def test_the_inbound_row_is_tagged_with_the_same_round(self) -> None:
        ports = make_ports()
        service = make_service(ports=ports, gateway=FakeGateway(), new_id=lambda: "chat-42")

        await service.reply(InboundMessage(content="在吗"))

        assert ports.conversations.inbound[0].tick_id == "chat-42"

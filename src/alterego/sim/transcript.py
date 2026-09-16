"""从对话记录里数出「节奏」。

:class:`~alterego.sim.context.StateSnapshot` 上有几个数是**数出来的**，
不是从哪一列直接读出来的：最后一句用户说了什么、连着秒回了几条、
连着被动接了几轮、上一次主动起话题是什么时候。
它们的输入都只是一串 :class:`~alterego.interfaces.repository.MessageRecord`，
所以单独放一个模块，而不是塞进引擎的私有方法里。

**为什么不能各写一份。** 会话那条路径（``sim/conversation.py``）和 tick
引擎要问的是同一批问题。两处各数一遍，迟早会出现「同一条记录，tick 认为
你连着回了三条、会话认为两条」——而这类分岔没有任何输出能看出来，
只会让它在两条路径上表现得像两个人。

四个函数都是纯函数：不取时间、不取随机数、不碰库。

依据: docs/design/12-calendar-and-conversation.md § 10.1、§ 11
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from alterego.interfaces.repository import MessageRecord


__all__ = [
    "instant_streak",
    "last_inbound_text",
    "last_topic_at",
    "passive_streak",
]


INBOUND = "inbound"
OUTBOUND = "outbound"


def last_inbound_text(messages: Sequence[MessageRecord]) -> str:
    """用户最近一条消息的正文。没有就返回空串。

    空串是有意义的输入而非缺失：``decide_reply`` 会把它算成「短消息，随手就回」，
    这正是「用户还没说话」时该有的默认。
    """
    for message in reversed(messages):
        if message.direction == INBOUND:
            return message.content
    return ""


def instant_streak(messages: Sequence[MessageRecord], *, within_seconds: float) -> int:
    """前面连着秒回了几条。

    从最近一条往回走，把每条**入站**消息和紧随其后那条**出站**消息配成一对，
    间隔在 ``within_seconds`` 以内就算一次秒回；遇到超时的、
    或者走到一条还没被回过的入站消息之前就停。

    配对方向是「入站 → 它后面那条出站」，所以往回走时要先把看到的出站消息
    记下来。反过来配（出站在前、入站在后）会算出负数间隔，
    而负数会被当成「不超时」——那是个安静的错，结果永远是 0。

    这条计数不是统计指标，是**给 ``decide_reply`` 用的输入**：
    连着秒回几条之后，它会故意放一放再回（见 ``domain/conversation``）。
    人不会永远秒回，而「永远秒回」正是最容易暴露「对面是机器」的地方。
    """
    streak = 0
    pending_outbound: datetime | None = None
    for message in reversed(messages):
        if message.direction == OUTBOUND:
            if message.created_at is None:
                break
            pending_outbound = message.created_at
            continue
        if message.direction != INBOUND or pending_outbound is None:
            continue
        if message.created_at is None:
            break
        gap = (pending_outbound - message.created_at).total_seconds()
        if gap < 0 or gap > within_seconds:
            break
        streak += 1
        pending_outbound = None
    return streak


def passive_streak(messages: Sequence[MessageRecord]) -> int:
    """前面连着被动接了几轮。

    「被动」= 它回的那条**不是它起的话题**（``initiative`` 为假）。
    和 :func:`instant_streak` 用同一套配对（入站 → 它后面那条出站），
    但看的是 ``initiative`` 而不是时间间隔——「秒回」和「只顺着对方说」
    是两件事，一个关于快慢，一个关于谁在主导。

    用途是 ``should_open_topic``：连着被动接了好几轮之后，
    它该自己抛一个话题了。否则这场对话的信息全由用户提供，
    而「只会一问一答」是第二种最容易暴露「对面是机器」的地方。
    """
    streak = 0
    pending_outbound: MessageRecord | None = None
    for message in reversed(messages):
        if message.direction == OUTBOUND:
            pending_outbound = message
            continue
        if message.direction != INBOUND or pending_outbound is None:
            continue
        if pending_outbound.initiative:
            break
        streak += 1
        pending_outbound = None
    return streak


def last_topic_at(messages: Sequence[MessageRecord]) -> datetime | None:
    """它上一次主动起话题是什么时候。``None`` 表示从来没起过。

    ``None`` 与「很久以前」是两件事：前者说明这次该它开口了（没有冷却可言），
    后者说明刚说过，要等一等。把 ``None`` 当成 0 会让第一次对话永远不主动。
    """
    for message in reversed(messages):
        if message.direction == OUTBOUND and message.initiative:
            return message.created_at
    return None

"""消息渠道契约。

**方向性是本层最重要的一件事。** 企业微信「群机器人」与钉钉「自定义机器人」
是**只能发、不能收**的单向渠道，因此 ``direction`` 必须如实声明——
否则上层会以为用户在那边说话，而实际上永远收不到（见 ADR-0004）。

v1 唯一的入站渠道是本地 Web 界面。

依据: docs/design/02-plugin-api.md § 6.2 + docs/design/05-channels.md
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from alterego.interfaces.common import HealthStatus


__all__ = [
    "Channel",
    "ChannelCapability",
    "Direction",
    "InboundMessage",
    "MessageKind",
    "OutboundMessage",
    "SendResult",
]


#: 渠道方向。``{"out"}`` 表示单向出站。
#
# 下面三个别名**必须在 __all__ 里**：它们不是内部细节，而是 ``Channel``
# 协议三个字段的类型（``direction`` / ``capabilities`` / ``OutboundMessage.kind``）。
# 插件实现渠道时要用它们标注自己的类属性，所以它们是契约的一部分——
# 漏在 __all__ 外面会让「按文档写类型注解」这件事必须靠翻源码才能做到。
Direction = frozenset[Literal["in", "out"]]

#: 渠道能力标签。
ChannelCapability = Literal["text", "markdown", "image", "mention", "card", "long_text"]

MessageKind = Literal["text", "markdown", "image", "card"]


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """一条待发送的消息。"""

    kind: MessageKind
    content: str
    title: str | None = None
    image_path: Path | None = None
    #: 要 @ 的用户标识。**语义由渠道解释**——钉钉认手机号，Telegram 认 user_id，
    #: 因此这里只保证「它是渠道自己认得的字符串」。
    mention: tuple[str, ...] = ()
    mention_all: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """一条收到的消息。只有 ``direction`` 含 ``"in"`` 的渠道会产生它。"""

    channel_id: str
    sender_id: str
    sender_name: str
    content: str
    received_at: datetime
    raw: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SendResult:
    """发送结果。

    ``retryable`` 是给上层的唯一提示：``False`` 表示重试只是浪费时间
    （例如内容超长、机器人已被删除），此时应该走降级而不是重试。
    """

    ok: bool
    error: str | None = None
    retryable: bool = False
    message_id: str | None = None


class Channel(Protocol):
    """一个能触达用户（或可能接收用户消息）的通道。"""

    id: str
    direction: Direction
    capabilities: frozenset[str]

    async def send(self, msg: OutboundMessage) -> SendResult: ...

    def on_receive(self, handler: Callable[[InboundMessage], None]) -> None:
        """注册入站回调。

        单向渠道必须显式抛 :class:`NotImplementedError`，
        **不要**默默接受回调然后再也不调用它——那会让「为什么收不到消息」
        变成一个要读三小时源码才能回答的问题。
        """
        ...

    async def aclose(self) -> None: ...

    def health_check(self) -> HealthStatus: ...

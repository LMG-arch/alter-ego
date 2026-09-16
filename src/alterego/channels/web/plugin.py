"""``channel.web``：浏览器这条渠道。

**它是渠道，不是服务器。** 这个区别看着像吹毛求疵，实际决定了整块代码的形状：

- 它**不开监听、不绑端口**。绑定是 ``alterego serve`` 的事，端口住在 ``[web]``。
- 它**不存消息**。会话与消息本来就在库里，写进去的是引擎。
- 它做的事只有两件：出去的推给 SSE 广播；进来的（页面上的 ``POST``）交给
  ``on_receive`` 装上的那个处理器。

所以它没有任何配置项。凡是「要配一个数」的，都属于服务器或引擎，
不属于这条把字节递过去的管子。

**它由组装根装配，不是被插件加载器发现的。** 一不是妥协，而是一个明确的切分：
这个渠道的监听地址、认证、静态文件都在同一个进程里，装配它的地方就是
``cli_serve.py``。而它**放进注册表的方式与插件完全一致**（同名、同接口），
所以将来真出现一个「第三方的 web 渠道插件」，换掉的是谁去``register``那一行，
而不是任何一个读渠道的人——注册表就是这个接缝。

依据: docs/design/05-channels.md § 3.3
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Final, Literal

from alterego.channels.web.sse import SSEBroker
from alterego.interfaces.channel import InboundMessage, OutboundMessage, SendResult
from alterego.interfaces.common import HealthStatus
from alterego.kernel.logging import get_logger


__all__ = ["CHANNEL_ID", "OUTBOUND_EVENT", "WebChannel", "outbound_payload"]

_log = get_logger("channels.web")

CHANNEL_ID: Final[str] = "web"
"""``Channel.id``，同时也是 ``[routing].outbound = ["web"]`` 里那个名字。

刻意不是插件 id（``channel.web``）：路由说的是「推到哪条渠道」，
插件 id 说的是「哪个包提供它」。两者现在长得像，含义完全不同，
混用会让 ``[routing]`` 在换实现（比如自己再写一个 web 渠道）时立刻出错。
"""

OUTBOUND_EVENT: Final[str] = "message"
"""出站消息在 SSE 上的事件名。前端只听这一个名字就能收到它说的话。"""

_DIRECTION: Final[frozenset[Literal["in", "out"]]] = frozenset({"in", "out"})
_CAPABILITIES: Final[frozenset[str]] = frozenset({"text", "markdown"})


def outbound_payload(msg: OutboundMessage) -> dict[str, Any]:
    """把一条出站消息摊平成 JSON。

    只输出前端真的会用的字段：``metadata`` 里装的是引擎内部的东西
    （渠道选项、重试次数），它不该出现在浏览器里。
    """
    payload: dict[str, Any] = {
        "kind": msg.kind,
        "content": msg.content,
        "title": msg.title,
        "mention": list(msg.mention),
        "mention_all": msg.mention_all,
    }
    if msg.image_path is not None:
        # 只给文件名，不给绝对路径：绝对路径泄漏本机目录结构，
        # 而图片本身要走带鉴权的入口。
        payload["image"] = msg.image_path.name
    return payload


class WebChannel:
    """浏览器渠道（实现 :class:`~alterego.interfaces.channel.Channel`）。"""

    def __init__(self, broker: SSEBroker, logger: logging.Logger | None = None) -> None:
        self.broker = broker
        self.logger = logger if logger is not None else _log
        self._handler: Callable[[InboundMessage], None] | None = None
        self._closed = False
        self._sent = 0
        self._received = 0

    # ── 身份 ────────────────────────────────────────────────

    @property
    def id(self) -> str:
        return CHANNEL_ID

    @property
    def direction(self) -> frozenset[Literal["in", "out"]]:
        return _DIRECTION

    @property
    def capabilities(self) -> frozenset[str]:
        return _CAPABILITIES

    # ── 两个计数，给 /api/status 用 ──────────────────────────

    @property
    def sent(self) -> int:
        return self._sent

    @property
    def received(self) -> int:
        return self._received

    # ── 出站 ────────────────────────────────────────────────

    async def send(self, msg: OutboundMessage) -> SendResult:
        """把它说的话推给所有还连着的页面。

        **没人连着也算成功。** 消息本身已经落库（那是引擎的事），这条渠道
        只负责「此刻在看的页面能不能立刻看到」。把「三个页面都关了」
        判成发送失败，会让引擎去重试一件已经做完的工作。
        """
        if self._closed:
            return SendResult(ok=False, error="渠道已关闭", retryable=False)
        delivered = self.broker.broadcast(OUTBOUND_EVENT, outbound_payload(msg))
        self._sent += 1
        if not delivered:
            self.logger.debug("没有页面在听，这条消息只落库：%s", msg.content[:20])
        return SendResult(ok=True, message_id=f"sse-{self._sent}-{delivered}")

    # ── 入站 ────────────────────────────────────────────────

    def on_receive(self, handler: Callable[[InboundMessage], None]) -> None:
        """装上「收到用户消息之后交给谁」。

        Web 渠道是**双向**的，所以这里不抛 ``NotImplementedError``——
        那条约定是留给单向渠道的（webhook 只能发，收不了）。
        """
        self._handler = handler

    def deliver(self, message: InboundMessage) -> bool:
        """把一条已经成形的入站消息递上去。返回「有没有人接」。

        返回 ``False`` 时路由会答 503：那是**引擎没装配**，
        不是「你这条消息有问题」——两者的下一步动作完全不同。
        """
        self._received += 1
        if self._handler is None:
            return False
        self._handler(message)
        return True

    # ── 生命周期 ────────────────────────────────────────────

    async def aclose(self) -> None:
        """收摊：把所有 SSE 连接正常结束掉。

        服务都停了还让页面保持连接，页面会一直显示「已连接」却拿不到东西——
        那比直接断开更难查。
        """
        self._closed = True
        self.broker.close_all()

    def health_check(self) -> HealthStatus:
        if self._closed:
            return HealthStatus(ok=False, detail="已关闭", hint="重启 alterego serve")
        return HealthStatus(
            ok=True,
            detail=f"{self.broker.subscribers} 个页面连着 · 已发 {self._sent} / 已收 {self._received}",
        )

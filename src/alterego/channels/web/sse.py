"""SSE 广播：把「它刚说了什么」送到浏览器。

**为什么是 SSE 而不是 WebSocket。** 这条连接是**单向**的：它主动说话，
浏览器听着。断线自动重连是浏览器送的，心跳是一条注释帧，不需要自己写
状态机。代价是入站要走普通 ``POST``——那反而更好调试，一个 ``curl`` 就能测。

**丢一条不等于丢历史。** 队列满只有一种情况：这个浏览器被切到后台、
事件循环被节流，而它自己不知道。此时丢掉新事件、记一个计数，
比无限缓存（内存长在服务器上）或断开连接（用户切回来看到一片空白）
都合理：所有消息**本来就都在库里**，页面重新拉一次历史就补齐了。

依据: docs/design/05-channels.md § 3.3
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, ClassVar, Final

from alterego.kernel.errors import ChannelError
from alterego.kernel.logging import get_logger


__all__ = ["CLOSE", "KEEPALIVE", "SSEBroker", "Subscription", "TooManySubscribers", "sse_pack"]

_log = get_logger("channels.web.sse")

KEEPALIVE: Final[str] = ": keepalive\n\n"
"""心跳帧。

冒号开头是 SSE 规范的**注释**，浏览器直接丢掉，但中间那些会掐空闲连接的
反向代理看得见「这条连接还在动」。不发它的话，页面会安静地停在某个瞬间，
而且没有任何报错——那是这个文件里最难查的一类故障。
"""
CLOSE: Final[str] = ""
"""结束信号。

空帧不是合法的 SSE 帧，所以它永远不会被浏览器当成一条消息——
它的唯一读者是本模块的 :meth:`Subscription.next_frame`。
"""

_QUEUE_SIZE: Final[int] = 100
"""单个订阅者的队列深度。

100 条消息远超一次重连间隔里可能发生的事（``realtime`` 模式下 5 虚拟分钟一个 tick）。
"""


class TooManySubscribers(ChannelError):
    """订阅数已达 ``[web].sse_max_connections``。"""

    code: ClassVar[str] = "web_too_many_subscribers"


def sse_pack(event_type: str, data: Mapping[str, Any]) -> str:
    """把一条事件渲染成一帧 SSE 文本。

    ``ensure_ascii=False``：中文按原样出去，抓包时看得懂，流量还更小
    （一个汉字 3 字节，``\\uXXXX`` 是 6）。
    """
    payload = json.dumps(dict(data), ensure_ascii=False, default=str)
    return f"event: {event_type}\ndata: {payload}\n\n"


class Subscription:
    """一个浏览器标签页的收件箱。

    **不是线程安全的**，也不需要是：整个 Web 层跑在 uvicorn 那一个事件循环里。
    """

    __slots__ = ("_queue", "delivered", "dropped")

    def __init__(self, *, size: int = _QUEUE_SIZE) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=size)
        #: 成功投递的帧数。给 ``/api/status`` 用，人能从这两个数看出页面
        #: 「是不是漏了几条」——没有它，漏帧是静默的。
        self.delivered = 0
        self.dropped = 0

    def close(self) -> None:
        """告诉读取方「别再等了」。

        队列满时**挤掉一条旧帧**也要把结束信号放进去：那条旧帧前端重新拉
        历史就回来了，而关不掉的连接会一直占着事件循环。
        """
        if self._queue.full():
            self._queue.get_nowait()
            self.dropped += 1
        self._queue.put_nowait(CLOSE)

    @property
    def pending(self) -> int:
        """还在队列里、没被前端取走的帧数。"""
        return self._queue.qsize()

    def offer(self, frame: str) -> bool:
        """投一帧。队列满时丢**这一帧**并返回 ``False``。"""
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        self.delivered += 1
        return True

    async def next_frame(self, timeout: float) -> str | None:
        """等下一帧。

        三种返回：一帧文本、:data:`CLOSE`（服务端要收了）、``None``（超时）。
        超时不是错误：调用方据此发一条心跳，连接继续。
        """
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None


class SSEBroker:
    """订阅者集合与广播。

    刻意只认字符串帧、不认事件对象：广播的人负责说清「这是什么事件」，
    这个类只负责「把它送到所有还连着的浏览器」。这样 SSE 的形状
    （帧格式、心跳、队列深度）与业务的形状（有哪些事件）互不牵连。
    """

    __slots__ = ("_keepalive", "_logger", "_max", "_size", "_subs")

    def __init__(
        self,
        *,
        max_connections: int = 20,
        queue_size: int = _QUEUE_SIZE,
        keepalive_seconds: float = 20.0,
        logger: Any = None,
    ) -> None:
        self._max = max_connections
        self._size = queue_size
        self._keepalive = keepalive_seconds
        self._subs: list[Subscription] = []
        self._logger = logger if logger is not None else _log

    # ── 订阅 ────────────────────────────────────────────────

    @property
    def keepalive_seconds(self) -> float:
        return self._keepalive

    @property
    def max_connections(self) -> int:
        return self._max

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    @property
    def dropped(self) -> int:
        """所有订阅者加起来丢掉的帧数。"""
        return sum(sub.dropped for sub in self._subs)

    def subscribe(self) -> Subscription:
        """开一个订阅。超上限时抛 :class:`TooManySubscribers`。

        **上限是配置项而不是「应该不会太多」**：每一个订阅者都是一个
        被服务器持有的 ``asyncio.Queue``，而开标签页是不花钱的。
        """
        if len(self._subs) >= self._max:
            raise TooManySubscribers(
                "SSE 订阅数已达上限",
                limit=self._max,
                hint="关掉几个旧标签页，或调高 [web].sse_max_connections",
            )
        sub = Subscription(size=self._size)
        self._subs.append(sub)
        self._logger.debug("SSE 订阅 %d/%d", len(self._subs), self._max)
        return sub

    def unsubscribe(self, sub: Subscription) -> bool:
        """退订。**必须幂等**——断线、异常、超时三条路径都会走到这里。"""
        try:
            self._subs.remove(sub)
        except ValueError:
            return False
        self._logger.debug("SSE 退订 %d/%d", len(self._subs), self._max)
        return True

    # ── 广播 ────────────────────────────────────────────────

    def broadcast(self, event_type: str, data: Mapping[str, Any]) -> int:
        """把一帧送给所有订阅者，返回**真正送到**的数量。

        返回值不是装饰：``0``（没人连着）与 ``3``（三个页面都收到了）
        在排障时是两条完全不同的线索。
        """
        frame = sse_pack(event_type, data)
        return sum(1 for sub in self._subs if sub.offer(frame))

    def close_all(self) -> None:
        """让所有连接结束。

        往每个队列里放一个结束信号——读取方把它解释成「服务端要收了」，
        于是连接是**正常结束**的，前端不会把它当成一次故障去重连
        （``EventSource`` 在异常断开时会自动重连，那正是我们不想要的）。
        """
        subs, self._subs = list(self._subs), []
        for sub in subs:
            sub.close()

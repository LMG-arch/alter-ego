"""事件总线。

内核与插件之间**唯一**的广播通道（``docs/design/01-architecture.md`` § 2.3）：
发出者不知道谁在听，订阅者不知道谁发出。这带来两个直接好处——

- 新增一个功能（比如「把内心独白发到 Web」）不需要改事件发出者的代码
- 删掉任何插件都不会让别的地方 ``ImportError``

设计要点：

===================  ==========================================================
通配符               ``fnmatch`` 风格：``tick.*`` 匹配 ``tick.started``；``*`` 匹配全部
优先级               同一事件多订阅者按 ``priority`` 降序执行，同优先级按注册顺序
同步 vs 异步         内核内部事件用 :meth:`EventBus.publish`（低延迟）；
                     对外推送（渠道）用 :meth:`EventBus.publish_async` 并发派发
异常隔离             单个 handler 抛异常 → 记日志 + 发 ``bus.handler_failed``，
                     **其余 handler 继续执行**
重入保护             同步派发中若 handler 再次 publish，限制深度（默认 8 层），
                     超出则告警并丢弃，避免无限递归
===================  ==========================================================
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from alterego.kernel.clock import Clock
from alterego.kernel.errors import PluginError


__all__ = ["FAILURE_TOPIC", "Event", "EventBus", "Subscription"]

#: 订阅者内部抛异常时广播的主题。载荷见 ``EventBus._report_failure``。
FAILURE_TOPIC: Final[str] = "bus.handler_failed"

_DEFAULT_MAX_DEPTH: Final[int] = 8


@dataclass(frozen=True, slots=True)
class Event:
    """一条事件。

    重要：``payload`` 只做**浅**冻结。约定是「发布后不要修改 payload」——
    它可能同时被多个订阅者读到，改它等于给所有人制造竞态。需要改动时
    请构造新的 ``dict``。
    """

    #: 点分主题，如 ``"tick.completed"``。
    topic: str
    #: 事件载荷，约定永远是 ``dict``（不要传 ``None``）。
    payload: dict[str, Any]
    #: 真实时间（不是虚拟时间）。事件日志与 Web 展示用它。
    timestamp: datetime
    #: 发出者 id：插件 id 或 ``"kernel"``。
    source: str
    #: uuid4，用于去重与追踪。
    event_id: str
    #: 同一 tick 内的事件共享，便于把一次推演串起来。
    correlation_id: str | None = None

    @classmethod
    def create(
        cls,
        topic: str,
        payload: dict[str, Any] | None = None,
        *,
        source: str = "kernel",
        clock: Clock,
        correlation_id: str | None = None,
    ) -> Event:
        """构造一条事件并自动填充时间戳与 id。

        时间戳取 ``clock.now()``（**真实**时间）而不是 ``virtual_now()``：
        事件日志是用来回答「它什么时候做的」的，用户想知道的是现实时间。
        """
        return cls(
            topic=topic,
            payload=dict(payload) if payload else {},
            timestamp=clock.now(),
            source=source,
            event_id=uuid.uuid4().hex,
            correlation_id=correlation_id,
        )


SyncHandler = Callable[[Event], None]
AsyncHandler = Callable[[Event], Awaitable[None]]
Handler = SyncHandler | AsyncHandler


@dataclass(frozen=True, slots=True)
class Subscription:
    """一次订阅。取消订阅时把这个对象传回 :meth:`EventBus.unsubscribe`。"""

    id: str
    #: 订阅时的主题模式（可能含通配符）。
    pattern: str
    handler: Handler
    priority: int
    #: 触发一次后自动取消。
    once: bool
    #: 注册顺序，用于同优先级时的稳定排序。
    order: int
    #: 订阅者归属（插件 id）。热重载时按 owner 批量取消。
    owner: str | None = None


class EventBus:
    """同步与异步双形态的事件总线。

    时钟是必需参数而不是可选项：事件的时间戳必须来自可注入的时钟，
    否则测试里就出现了真实时间（破坏可复现性）。
    """

    def __init__(
        self,
        clock: Clock,
        /,
        *,
        logger: logging.Logger | None = None,
        max_depth: int = _DEFAULT_MAX_DEPTH,
    ) -> None:
        """初始化。

        Args:
            clock: 用于给事件打时间戳的时钟。
            logger: 日志器。插件应传 ``ctx.logger``，便于按插件过滤。
            max_depth: 同步派发的最大重入深度。
        """
        self._clock = clock
        self._logger = logger if logger is not None else logging.getLogger("alterego.kernel.bus")
        self._max_depth = max_depth
        self._subscriptions: list[Subscription] = []
        self._order = 0
        self._depth = 0
        self._notifying_failure = False
        # 持有 task 引用，否则事件循环可能在任务完成前回收它
        self._tasks: set[asyncio.Task[None]] = set()

    # ── 订阅 ────────────────────────────────────────────────

    def subscribe(
        self,
        pattern: str,
        handler: Handler,
        *,
        priority: int = 0,
        once: bool = False,
        owner: str | None = None,
    ) -> Subscription:
        """订阅主题。

        Args:
            pattern: 主题模式，支持 ``fnmatch`` 通配符（``"tick.*"``、``"*"``）。
            handler: 处理函数。可以是同步函数，也可以是协程函数。
            priority: 数值大者先执行。同优先级按注册顺序。
            once: 只触发一次，之后自动取消。
            owner: 订阅者归属，用于按插件批量取消。

        Returns:
            订阅凭据。请保存它，热重载/卸载时需要传回 :meth:`unsubscribe`。

        Raises:
            PluginError: ``pattern`` 为空。
        """
        if not pattern:
            raise PluginError(
                '订阅的主题模式不能为空（想订阅全部请用 "*"）',
                handler=_handler_name(handler),
                owner=owner,
            )
        self._order += 1
        sub = Subscription(
            id=uuid.uuid4().hex,
            pattern=pattern,
            handler=handler,
            priority=priority,
            once=once,
            order=self._order,
            owner=owner,
        )
        self._subscriptions.append(sub)
        self._logger.debug(
            "订阅 %s (priority=%d, once=%s, owner=%s)", pattern, priority, once, owner
        )
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        """取消订阅。**幂等**——重复调用不会报错。

        幂等是刻意的：``on_stop()`` 可能被调用两次（停机 + 热重载），
        取消订阅不该因为「已经取消了」而失败。
        """
        try:
            self._subscriptions.remove(sub)
        except ValueError:
            return

    def unsubscribe_owner(self, owner: str) -> int:
        """取消某个归属（插件）的全部订阅。

        Returns:
            实际取消的订阅数。
        """
        kept = [s for s in self._subscriptions if s.owner != owner]
        removed = len(self._subscriptions) - len(kept)
        self._subscriptions = kept
        return removed

    def clear(self) -> None:
        """清空所有订阅（停机时使用）。"""
        self._subscriptions.clear()

    # ── 查询 ────────────────────────────────────────────────

    @property
    def clock(self) -> Clock:
        """本总线使用的时钟。"""
        return self._clock

    def subscriber_count(self, pattern: str | None = None) -> int:
        """订阅者数量。``pattern`` 为 ``None`` 时返回总数。"""
        if pattern is None:
            return len(self._subscriptions)
        return sum(1 for s in self._subscriptions if s.pattern == pattern)

    def topics(self) -> list[str]:
        """当前被订阅的主题模式（去重、已排序）。"""
        return sorted({s.pattern for s in self._subscriptions})

    # ── 发布 ────────────────────────────────────────────────

    def emit(
        self,
        topic: str,
        payload: dict[str, Any] | None = None,
        *,
        source: str = "kernel",
        correlation_id: str | None = None,
    ) -> Event:
        """构造并同步发布一条事件，返回该事件。

        这是最常用的形式：99% 的调用点只关心「发一下」，不关心 Event 对象。
        """
        event = Event.create(
            topic, payload, source=source, clock=self._clock, correlation_id=correlation_id
        )
        self.publish(event)
        return event

    def publish(self, event: Event) -> None:
        """同步派发。

        同步 handler 立即执行；协程 handler 会被排成任务（若有运行中的事件循环），
        因为同步函数无法 ``await``。渠道推送这类必须等待完成的操作请用
        :meth:`publish_async`。

        单个 handler 抛异常不会中断派发，异常会被记录并广播到
        :data:`FAILURE_TOPIC`。
        """
        if self._depth >= self._max_depth:
            self._logger.warning(
                "事件重入深度超过 %d 层，丢弃事件 topic=%s。"
                "常见原因：某个 handler 订阅了自己发布的事件。",
                self._max_depth,
                event.topic,
            )
            return
        self._depth += 1
        try:
            for sub in self._collect(event.topic):
                self._invoke_sync(sub, event)
        finally:
            self._depth -= 1

    async def publish_async(self, event: Event) -> None:
        """异步并发派发，等待**全部** handler 完成。

        用于对外推送：多个渠道应该并行发送，而不是排队等前面的超时。
        并发意味着顺序不确定，因此这里的 handler 不应有顺序依赖——
        有顺序依赖就说明它们本来就该是同一个 handler 的两段。
        """
        if self._depth >= self._max_depth:
            self._logger.warning(
                "事件重入深度超过 %d 层，丢弃事件 topic=%s", self._max_depth, event.topic
            )
            return
        self._depth += 1
        try:
            pending = [self._dispatch_async(sub, event) for sub in self._collect(event.topic)]
            if pending:
                await asyncio.gather(*pending)
        finally:
            self._depth -= 1

    # ── 内部 ────────────────────────────────────────────────

    def _collect(self, topic: str) -> list[Subscription]:
        """取出匹配该主题的订阅者，按 (priority 降序, 注册顺序) 排序。

        返回**快照**：handler 执行期间新增或取消订阅不会影响本次派发。
        """
        matched = [s for s in self._subscriptions if fnmatch.fnmatchcase(topic, s.pattern)]
        matched.sort(key=lambda s: (-s.priority, s.order))
        return matched

    def _invoke_sync(self, sub: Subscription, event: Event) -> None:
        try:
            result = sub.handler(event)
        except Exception as exc:
            self._report_failure(sub, event, exc)
            return
        finally:
            if sub.once:
                self.unsubscribe(sub)
        if inspect.isawaitable(result):
            self._schedule(result, sub, event)

    async def _dispatch_async(self, sub: Subscription, event: Event) -> None:
        try:
            result = sub.handler(event)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            self._report_failure(sub, event, exc)
        finally:
            if sub.once:
                self.unsubscribe(sub)

    def _schedule(self, awaitable: Awaitable[None], sub: Subscription, event: Event) -> None:
        """把协程 handler 排成任务（同步派发路径）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._logger.warning(
                "订阅者的 handler 是协程，但当前没有运行中的事件循环，事件被丢弃 "
                "topic=%s pattern=%s owner=%s",
                event.topic,
                sub.pattern,
                sub.owner,
            )
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()  # 关掉协程，否则 Python 会报 "was never awaited"
            return
        task = loop.create_task(self._await_and_report(awaitable, sub, event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _await_and_report(
        self, awaitable: Awaitable[None], sub: Subscription, event: Event
    ) -> None:
        try:
            await awaitable
        except Exception as exc:
            self._report_failure(sub, event, exc)

    def _report_failure(self, sub: Subscription, event: Event, exc: BaseException) -> None:
        """记录 handler 异常并广播 :data:`FAILURE_TOPIC`。"""
        self._logger.error(
            "订阅者处理事件失败 topic=%s pattern=%s owner=%s: %s",
            event.topic,
            sub.pattern,
            sub.owner or "-",
            exc,
            exc_info=exc,
        )
        if self._notifying_failure:
            # 连 "报告失败" 这件事都失败了。到此为止——再往下就是无限递归。
            self._logger.error("bus.handler_failed 的处理者本身也抛了异常，停止继续上报")
            return
        self._notifying_failure = True
        try:
            self.publish(
                Event.create(
                    FAILURE_TOPIC,
                    {
                        "topic": event.topic,
                        "pattern": sub.pattern,
                        "owner": sub.owner,
                        "handler": _handler_name(sub.handler),
                        "error": f"{type(exc).__name__}: {exc}",
                        "event_id": event.event_id,
                    },
                    source="kernel",
                    clock=self._clock,
                    correlation_id=event.correlation_id,
                )
            )
        finally:
            self._notifying_failure = False


def _handler_name(handler: Handler) -> str:
    """取 handler 的可读名字，用于日志。"""
    return (
        getattr(handler, "__qualname__", None)
        or getattr(handler, "__name__", None)
        or repr(handler)
    )

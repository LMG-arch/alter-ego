"""``alterego.kernel.bus`` 的行为测试。

优先级最高的是**异常隔离**：一个插件写坏了不能把整个 Agent 拖死。
其余测通配符、优先级、once、异步派发、重入上限。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from alterego.kernel.bus import FAILURE_TOPIC, Event, EventBus
from alterego.kernel.clock import FrozenClock
from alterego.kernel.errors import PluginError


def test_emit_builds_event_and_dispatches(bus: EventBus) -> None:
    seen: list[Event] = []
    bus.subscribe("tick.completed", seen.append)

    event = bus.emit("tick.completed", {"tick_id": "t1"}, source="engine")

    assert len(seen) == 1
    assert seen[0] is event
    assert event.topic == "tick.completed"
    assert event.payload == {"tick_id": "t1"}
    assert event.source == "engine"
    assert event.event_id
    assert event.timestamp.tzinfo is not None


def test_event_without_payload_gets_empty_dict(bus: EventBus) -> None:
    """约定 payload 永远是 dict，不是 None——订阅者不必到处判空。"""
    assert bus.emit("app.started").payload == {}


def test_event_create_copies_payload(bus: EventBus) -> None:
    source_dict: dict[str, Any] = {"a": 1}
    event = Event.create("x", source_dict, clock=bus.clock)
    source_dict["a"] = 999
    assert event.payload == {"a": 1}


def test_wildcard_matches_subtopics(bus: EventBus) -> None:
    seen: list[str] = []
    bus.subscribe("tick.*", lambda e: seen.append(e.topic))

    bus.emit("tick.started")
    bus.emit("tick.completed")
    bus.emit("post.created")

    assert seen == ["tick.started", "tick.completed"]


def test_star_matches_everything(bus: EventBus) -> None:
    seen: list[str] = []
    bus.subscribe("*", lambda e: seen.append(e.topic))
    bus.emit("anything.at.all")
    assert seen == ["anything.at.all"]


def test_non_matching_subscriber_is_not_called(bus: EventBus) -> None:
    seen: list[str] = []
    bus.subscribe("post.created", lambda e: seen.append(e.topic))
    bus.emit("tick.completed")
    assert seen == []


def test_priority_descending_then_registration_order(bus: EventBus) -> None:
    order: list[str] = []
    bus.subscribe("x", lambda e: order.append("low"), priority=0)
    bus.subscribe("x", lambda e: order.append("high"), priority=10)
    bus.subscribe("x", lambda e: order.append("mid-a"), priority=5)
    bus.subscribe("x", lambda e: order.append("mid-b"), priority=5)

    bus.emit("x")

    # 高优先级先执行；同优先级保持注册顺序（否则重启一次行为就变了）
    assert order == ["high", "mid-a", "mid-b", "low"]


def test_once_subscription_fires_exactly_once(bus: EventBus) -> None:
    calls: list[int] = []
    bus.subscribe("x", lambda e: calls.append(1), once=True)

    bus.emit("x")
    bus.emit("x")

    assert calls == [1]
    assert bus.subscriber_count("x") == 0


def test_unsubscribe_is_idempotent(bus: EventBus) -> None:
    """停机与热重载都会调用清理逻辑，取消订阅不能因为「已经取消」而失败。"""
    sub = bus.subscribe("x", lambda e: None)
    bus.unsubscribe(sub)
    bus.unsubscribe(sub)
    assert bus.subscriber_count() == 0


def test_unsubscribe_owner_removes_only_that_plugin(bus: EventBus) -> None:
    bus.subscribe("x", lambda e: None, owner="plugin.a")
    bus.subscribe("x", lambda e: None, owner="plugin.a")
    bus.subscribe("x", lambda e: None, owner="plugin.b")

    assert bus.unsubscribe_owner("plugin.a") == 2
    assert bus.subscriber_count() == 1


def test_empty_pattern_is_rejected(bus: EventBus) -> None:
    """空模式会骗人：它看起来订阅了「无」，实际什么都不匹配。直接拒绝。"""
    with pytest.raises(PluginError):
        bus.subscribe("", lambda e: None)


# ── 异常隔离 ────────────────────────────────────────────────


def test_broken_handler_does_not_stop_others(bus: EventBus) -> None:
    order: list[str] = []

    def boom(event: Event) -> None:
        order.append("boom")
        raise RuntimeError("插件写坏了")

    bus.subscribe("x", boom, priority=10)
    bus.subscribe("x", lambda e: order.append("after"), priority=0)

    bus.emit("x")

    assert order == ["boom", "after"]


def test_failure_is_broadcast_as_event(bus: EventBus) -> None:
    failures: list[Event] = []
    bus.subscribe(FAILURE_TOPIC, failures.append)

    def boom(event: Event) -> None:
        raise RuntimeError("插件写坏了")

    bus.subscribe("x", boom, owner="plugin.bad")
    bus.emit("x", {"k": "v"})

    assert len(failures) == 1
    payload = failures[0].payload
    assert payload["topic"] == "x"
    assert payload["owner"] == "plugin.bad"
    assert payload["error"] == "RuntimeError: 插件写坏了"
    assert payload["pattern"] == "x"


def test_failing_failure_handler_does_not_recurse(bus: EventBus) -> None:
    """「上报失败」本身失败时，必须停下来而不是无限递归。"""

    def boom(event: Event) -> None:
        raise RuntimeError("永远坏")

    bus.subscribe(FAILURE_TOPIC, boom)
    bus.subscribe("x", boom)

    bus.emit("x")  # 不抛异常、不挂死即通过


def test_reentrancy_beyond_limit_is_dropped(bus: EventBus) -> None:
    """handler 订阅自己发布的事件是经典事故，必须有天花板。"""
    depth: list[int] = []
    topic = "loop"

    def reenter(event: Event) -> None:
        depth.append(1)
        bus.emit(topic)

    bus.subscribe(topic, reenter)
    bus.emit(topic)

    # 默认上限 8 层：会进入若干次然后被丢弃，绝不会无限增长
    assert 1 < len(depth) <= 10


def test_depth_counter_is_reset_after_dispatch(bus: EventBus) -> None:
    """一次派发结束后深度必须归零，否则后续事件会被误判为重入。"""
    bus.subscribe("x", lambda e: None)
    for _ in range(20):
        bus.emit("x")


# ── 异步 ────────────────────────────────────────────────────


async def test_publish_async_awaits_coroutine_handlers(bus: EventBus) -> None:
    seen: list[str] = []

    async def handler(event: Event) -> None:
        await asyncio.sleep(0)
        seen.append(event.topic)

    bus.subscribe("x", handler)
    await bus.publish_async(bus_emit(bus, "x"))
    assert seen == ["x"]


async def test_publish_async_runs_handlers_concurrently(bus: EventBus) -> None:
    """渠道并发推送的关键：总耗时应该是 max(各渠道) 而不是 sum。"""
    started: list[str] = []

    def make_handler(tag: str) -> Any:
        async def handler(event: Event) -> None:
            started.append(f"start:{tag}")
            await asyncio.sleep(0.05)
            started.append(f"end:{tag}")

        return handler

    bus.subscribe("x", make_handler("a"), priority=10)
    bus.subscribe("x", make_handler("b"), priority=5)

    await bus.publish_async(bus_emit(bus, "x"))

    # 两个 handler 都开始了，才开始收尾 → 证明是并发而非串行
    assert started[:2] == ["start:a", "start:b"]


async def test_publish_async_isolates_failures(bus: EventBus) -> None:
    seen: list[str] = []

    async def boom(event: Event) -> None:
        raise RuntimeError("渠道挂了")

    bus.subscribe("x", boom, priority=10)
    bus.subscribe("x", lambda e: seen.append("ok"), priority=0)

    await bus.publish_async(bus_emit(bus, "x"))

    assert seen == ["ok"]


async def test_sync_publish_schedules_coroutine_handler(bus: EventBus) -> None:
    """同步派发无法 await，但协程 handler 不该被静默丢弃。"""
    seen: list[str] = []

    async def handler(event: Event) -> None:
        seen.append("ran")

    bus.subscribe("x", handler)
    bus.emit("x")
    await asyncio.sleep(0)

    assert seen == ["ran"]


def test_sync_publish_without_loop_warns_and_drops(bus: EventBus) -> None:
    """没有事件循环时无法执行协程 handler，但同步 handler 仍然要跑。"""
    seen: list[str] = []

    async def handler(event: Event) -> None:
        seen.append("never")

    bus.subscribe("x", handler)
    bus.subscribe("x", lambda e: seen.append("sync"))

    bus.emit("x")

    assert seen == ["sync"]


# ── 查询与清理 ──────────────────────────────────────────────


def test_subscriber_count_and_topics(bus: EventBus) -> None:
    bus.subscribe("a.b", lambda e: None)
    bus.subscribe("a.*", lambda e: None)
    bus.subscribe("a.*", lambda e: None)

    assert bus.subscriber_count() == 3
    assert bus.subscriber_count("a.*") == 2
    assert bus.topics() == ["a.*", "a.b"]


def test_clear_removes_all(bus: EventBus) -> None:
    bus.subscribe("x", lambda e: None)
    bus.clear()
    assert bus.subscriber_count() == 0


def test_bus_exposes_clock(bus: EventBus, clock: FrozenClock) -> None:
    assert bus.clock is clock


def bus_emit(bus: EventBus, topic: str) -> Event:
    """构造事件但不派发——给 ``publish_async`` 测试使用。"""
    return Event.create(topic, clock=bus.clock)

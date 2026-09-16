"""插件的运行期上下文：路径、状态，以及带归属的注册表与事件总线视图。

从 ``kernel/plugin.py`` 拆出来的（清单部分在 :mod:`alterego.kernel.manifest`）。
上下文的职责是**收窄边界**：插件只能通过 :class:`PluginContext` 看到系统。

:class:`OwnedRegistry` / :class:`OwnedBus` 是这里最要紧的两个类：它们让「谁注册的」
这件事**不依赖插件作者记得填参数**——作者照文档写 ``ctx.registry.register(...)``
就行，归属自动记在插件名下，于是 ``on_load`` 失败回滚与热重载清理才真的成立
（设计原则 P3：用机制约束，而不是靠提示词祈祷）。

依据: docs/design/02-plugin-api.md § 5、§ 11
"""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from alterego.kernel.bus import Event, EventBus, Handler, Subscription
from alterego.kernel.clock import Clock
from alterego.kernel.errors import PluginError
from alterego.kernel.manifest import PluginManifest
from alterego.kernel.registry import ServiceRegistry, T
from alterego.kernel.scheduler import Scheduler


__all__ = [
    "OwnedBus",
    "OwnedRegistry",
    "PluginContext",
    "PluginPaths",
    "PluginState",
]


# ── 路径与状态 ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PluginPaths:
    """插件能看到的路径。插件**不通过**这个对象看到项目里的其他任何东西。"""

    data_dir: Path
    cache_dir: Path
    config_dir: Path
    plugin_dir: Path
    alterego_dir: Path

    def ensure_dirs(self) -> None:
        """创建可写目录。只读目录（``plugin_dir`` / ``alterego_dir``）不动。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)


class PluginState:
    """插件的持久化键值存储。

    ``set()`` **不落盘**——写入先记账，由 ``PluginManager`` 在 tick 结束时
    批量 :meth:`flush`。一个 tick 里插件可能改十几次状态，逐次写库没有必要。

    ⚠️ **这条链今天只差最后一环，用之前请先知道。** ``plugin_state`` 表在
    ``migrations/001_initial.sql`` 里，:meth:`PluginManager.flush_state` 也在，
    但**没有任何组装根**往 ``PluginManager`` 传 ``state_loader`` / ``state_sink``
    （``grep -rn state_sink src/`` 只命中 ``kernel/manager.py`` 自己）。
    后果是具体的：插件今天可以正常记账、正常读回自己刚写的东西，
    但**进程重启后拿不回来**——``flush`` 在没有 ``sink`` 时直接丢掉挂起的写入。

    所以：跨重启要留下的东西请放数据库或 ``ctx.config``（``config_dir`` 下的
    用户配置），别放这里。这是已知缺口而非缺陷，跟踪在
    ``docs/design/13-interface-consistency.md``（审计表第 14 行）。
    """

    def __init__(
        self,
        plugin_id: str,
        *,
        initial: Mapping[str, Any] | None = None,
        sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._plugin_id = plugin_id
        self._data: dict[str, Any] = dict(initial or {})
        self._dirty: set[str] = set()
        self._deleted: set[str] = set()
        self._sink = sink

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """写一个值。值必须 JSON 可序列化。"""
        try:
            json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise PluginError(
                "插件状态必须是 JSON 可序列化的",
                plugin=self._plugin_id,
                key=key,
                type=type(value).__name__,
            ) from exc
        self._data[key] = value
        self._dirty.add(key)
        self._deleted.discard(key)

    def delete(self, key: str) -> None:
        if key in self._data:
            del self._data[key]
        self._dirty.discard(key)
        self._deleted.add(key)

    def update(self, mapping: Mapping[str, Any]) -> None:
        for key, value in mapping.items():
            self.set(key, value)

    def keys(self) -> list[str]:
        return sorted(self._data)

    def clear(self) -> None:
        for key in self._data:
            self._deleted.add(key)
        self._data.clear()
        self._dirty.clear()

    def snapshot(self) -> dict[str, Any]:
        return dict(self._data)

    @property
    def has_pending_writes(self) -> bool:
        return bool(self._dirty or self._deleted)

    def take_pending(self) -> dict[str, Any]:
        """取出并清空待写数据，供上层批量落盘。

        删除用 ``None`` 表示——调用方据此删行，不必再区分两个集合。
        """
        pending: dict[str, Any] = {key: self._data.get(key) for key in self._dirty}
        pending.update(dict.fromkeys(self._deleted, None))
        self._dirty.clear()
        self._deleted.clear()
        return pending

    def flush(self) -> None:
        """把待写数据交给 sink。没有 sink 就只是清账（测试与无存储时）。"""
        if not self.has_pending_writes:
            return
        pending = self.take_pending()
        if self._sink is not None:
            self._sink(self._plugin_id, pending)


# ── 上下文 ──────────────────────────────────────────────────


class OwnedRegistry:
    """``ctx.registry`` 的运行期真实类型：把 ``owner`` 预填成插件 id 的视图。

    为什么需要它（设计原则 P3）：文档里插件的写法是
    ``ctx.registry.register(Channel, self, name=...)``——**没有** ``owner``。
    如果归属要靠插件作者记得填，那么「``on_load`` 失败会卸载已注册的实现」
    （``docs/design/02-plugin-api.md`` § 11.1）就只是一句祈祷：漏填的插件会在
    热重载后留下指向半死对象的悬空引用，而且没人会立刻发现。把归属变成机制
    的一部分，作者就**没法**写错。

    其余方法（``get``/``has``/``get_all``/``names``…）原样转发，所以对使用者
    而言它和 :class:`ServiceRegistry` 没有区别。
    """

    __slots__ = ("_owner", "_registry")

    def __init__(self, registry: ServiceRegistry, owner: str) -> None:
        self._registry = registry
        self._owner = owner

    def register(
        self,
        interface: type[T],
        instance: T,
        *,
        name: str,
        priority: int = 0,
        owner: str | None = None,
    ) -> None:
        self._registry.register(
            interface, instance, name=name, priority=priority, owner=owner or self._owner
        )

    def __getattr__(self, name: str) -> Any:
        # 下划线开头的名字不进代理：否则 ``copy``/``pickle`` 这类探针会一路问到
        # 被包装的对象上，报错会变得莫名其妙。
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._registry, name)


class OwnedBus:
    """``ctx.bus`` 的运行期真实类型：``subscribe`` 自动带上插件 id 作为 owner。

    和 :class:`OwnedRegistry` 同理，见它的 docstring。
    """

    __slots__ = ("_bus", "_owner")

    def __init__(self, bus: EventBus, owner: str) -> None:
        self._bus = bus
        self._owner = owner

    def subscribe(
        self,
        pattern: str,
        handler: Handler,
        *,
        priority: int = 0,
        once: bool = False,
        owner: str | None = None,
    ) -> Subscription:
        return self._bus.subscribe(
            pattern, handler, priority=priority, once=once, owner=owner or self._owner
        )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._bus, name)


@dataclass(frozen=True, slots=True)
class PluginContext:
    """插件访问系统的**唯一**入口。

    插件不应该 ``import`` 内核的其他模块、也不应该持有任何全局单例——
    所有能力都从这里取。这样隔离、热重载与测试才成立。

    ``bus`` 与 ``registry`` 的声明类型是「接口」，运行期拿到的是带归属的视图
    （:class:`OwnedBus` / :class:`OwnedRegistry`）：插件注册的服务与订阅的事件会
    自动记在自己名下，``on_load`` 失败或热重载时被整体清理。**插件作者什么都不
    用做**，照文档的写法写就行。
    """

    plugin_id: str
    manifest: PluginManifest
    config: Mapping[str, Any]
    logger: logging.Logger
    bus: EventBus
    registry: ServiceRegistry
    clock: Clock
    scheduler: Scheduler
    state: PluginState
    paths: PluginPaths
    rng: random.Random

    def __post_init__(self) -> None:
        # 只读视图：frozen 只能拦住「换掉整个 config」，拦不住往里塞东西。
        if not isinstance(self.config, MappingProxyType):
            object.__setattr__(self, "config", MappingProxyType(dict(self.config)))
        # 归属视图。已经包过就不重复包（重载路径上可能会再走一遍构造）。
        if not isinstance(self.registry, OwnedRegistry):
            object.__setattr__(self, "registry", OwnedRegistry(self.registry, self.plugin_id))
        if not isinstance(self.bus, OwnedBus):
            object.__setattr__(self, "bus", OwnedBus(self.bus, self.plugin_id))

    # ── 便捷方法 ────────────────────────────────────────────

    def get_service(self, interface: type[T], name: str | None = None) -> T:
        """取一个必需的服务。没有就抛异常——**不要**在拿不到时继续跑。"""
        return self.registry.get(interface, name)

    def get_optional_service(self, interface: type[T], name: str | None = None) -> T | None:
        """取一个可选的服务。拿不到返回 ``None``，由调用方降级。"""
        return self.registry.get_optional(interface, name)

    def publish(
        self,
        topic: str,
        payload: dict[str, Any] | None = None,
        *,
        correlation_id: str | None = None,
    ) -> Event:
        return self.bus.emit(topic, payload, source=self.plugin_id, correlation_id=correlation_id)

    def now(self) -> datetime:
        """当前虚拟时间。**这是插件里唯一允许的时间来源。**"""
        return self.clock.virtual_now()

    def config_value(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

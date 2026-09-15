"""服务注册表。

内核**不 import 具体实现**，而是按*接口类型*查找实现
（``docs/design/01-architecture.md`` § 2.4）::

    # 推演层这样写——它不知道后端是 SQLite 还是别的什么
    storage = ctx.registry.get(StorageBackend)
    llm = ctx.registry.get(LLMProvider, name="strong")

关键收益：删除或替换任何插件都不会导致 ``ImportError``，
只会在启动时给出一句明确的能力缺失提示——而不是等到某个 tick
跑到一半才炸。

多实现解析规则：

======================  ======================================================
只有一个实现            直接返回
多个实现且未指定 name   返回 ``priority`` 最高者；并列则抛 ``PluginError``
未指定 name 且无实现    抛 ``PluginError``，提示需要哪个插件
用 ``get_optional``     无实现返回 ``None``，调用方自行降级
======================  ======================================================
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from alterego.kernel.errors import PluginError


__all__ = ["Registration", "ServiceRegistry"]

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Registration:
    """一条注册记录。"""

    interface: type[Any]
    name: str
    instance: Any
    priority: int
    #: 注册顺序，用于同优先级时的稳定排序与回滚。
    order: int
    #: 归属的插件 id。热重载时按 owner 批量注销。
    owner: str | None = None


class ServiceRegistry:
    """接口 → 实例的登记处。``owner`` 让插件可以被整体卸载。"""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        """初始化。

        Args:
            logger: 日志器。建议传 ``ctx.logger``。
        """
        self._logger = (
            logger if logger is not None else logging.getLogger("alterego.kernel.registry")
        )
        self._by_interface: dict[type[Any], dict[str, Registration]] = {}
        self._order = 0

    # ── 注册 ────────────────────────────────────────────────

    def register(
        self,
        interface: type[T],
        instance: T,
        *,
        name: str,
        priority: int = 0,
        owner: str | None = None,
    ) -> None:
        """注册一个实现。

        Args:
            interface: 接口类型（通常是一个 ``Protocol``）。
            instance: 实现该接口的对象。
            name: 实现名。同一接口下必须唯一。
            priority: 多个实现并存时的优先级，数值大者胜出。
            owner: 归属插件 id，便于热重载时整体注销。

        Raises:
            PluginError: ``name`` 为空，或该 (接口, 名字) 已经被注册。
                重复注册**不覆盖**——静默覆盖会让两个插件争抢同一个名字时
                出现难以定位的行为漂移。
        """
        if not name:
            raise PluginError(
                "服务名不能为空",
                interface=_label(interface),
                owner=owner,
            )
        table = self._by_interface.setdefault(interface, {})
        existing = table.get(name)
        if existing is not None:
            raise PluginError(
                "服务名已被注册",
                interface=_label(interface),
                name=name,
                existing_owner=existing.owner,
                new_owner=owner,
            )
        self._order += 1
        table[name] = Registration(
            interface=interface,
            name=name,
            instance=instance,
            priority=priority,
            order=self._order,
            owner=owner,
        )
        self._logger.debug(
            "注册服务 %s/%s (priority=%d, owner=%s)", _label(interface), name, priority, owner
        )

    def unregister(self, interface: type[Any], name: str) -> None:
        """注销一个实现。**幂等**——不存在时静默返回。

        幂等是刻意的：热重载流程里 ``on_stop()`` 可能失败，
        此时仍要保证注册表干净，不能让清理逻辑自己抛异常。
        """
        table = self._by_interface.get(interface)
        if not table:
            return
        if table.pop(name, None) is not None:
            self._logger.debug("注销服务 %s/%s", _label(interface), name)
        if not table:
            del self._by_interface[interface]

    def unregister_owner(self, owner: str) -> int:
        """注销某插件的全部实现。

        Returns:
            实际注销的数量。
        """
        removed = 0
        for interface in list(self._by_interface):
            table = self._by_interface[interface]
            for name in [n for n, reg in table.items() if reg.owner == owner]:
                del table[name]
                removed += 1
            if not table:
                del self._by_interface[interface]
        if removed:
            self._logger.debug("注销 owner=%s 的 %d 个服务", owner, removed)
        return removed

    def clear(self) -> None:
        """清空注册表（停机时使用）。"""
        self._by_interface.clear()

    # ── 查找 ────────────────────────────────────────────────

    def get(self, interface: type[T], name: str | None = None) -> T:
        """取出实现，取不到就抛异常。

        Raises:
            PluginError: 没有实现，或存在多个同优先级实现而无法判定。
        """
        registration = self._resolve(interface, name)
        if registration is None:
            raise PluginError(
                _missing_message(interface, name, self.names(interface)),
                interface=_label(interface),
                name=name,
            )
        return cast("T", registration.instance)

    def get_optional(self, interface: type[T], name: str | None = None) -> T | None:
        """取出实现，取不到返回 ``None``（调用方自行降级）。

        注意：「取不到」指**没有实现**。如果有多个同优先级实现而无法判定，
        仍然抛 ``PluginError``——歧义是配置错误，不是「暂时没有这个能力」，
        静默返回 ``None`` 会把配置错误伪装成「功能没启用」。
        """
        registration = self._resolve(interface, name)
        return None if registration is None else cast("T", registration.instance)

    def get_all(self, interface: type[T]) -> list[tuple[str, T]]:
        """取出该接口的**全部**实现，按 (priority 降序, 注册顺序) 排序。

        用于「所有渠道都发一遍」这类场景。
        """
        table = self._by_interface.get(interface, {})
        ordered = sorted(table.values(), key=lambda r: (-r.priority, r.order))
        return [(reg.name, cast("T", reg.instance)) for reg in ordered]

    def has(self, interface: type[Any], name: str | None = None) -> bool:
        """是否已注册。``name=None`` 时只判断该接口下有没有任何实现。"""
        table = self._by_interface.get(interface)
        if not table:
            return False
        return bool(table) if name is None else name in table

    def names(self, interface: type[Any]) -> list[str]:
        """该接口下所有实现名（已排序）。"""
        return sorted(self._by_interface.get(interface, {}))

    def interfaces(self) -> list[str]:
        """已注册的接口名（已排序），用于 ``alterego status`` 展示。"""
        return sorted(_label(i) for i in self._by_interface)

    def owner_of(self, interface: type[Any], name: str) -> str | None:
        """查某个实现的归属插件。"""
        table = self._by_interface.get(interface)
        if not table or name not in table:
            return None
        return table[name].owner

    # ── 内部 ────────────────────────────────────────────────

    def _resolve(self, interface: type[Any], name: str | None) -> Registration | None:
        """按名字或优先级解析出一个注册记录。歧义时抛异常。"""
        table = self._by_interface.get(interface, {})
        if name is not None:
            return table.get(name)
        if not table:
            return None
        if len(table) == 1:
            return next(iter(table.values()))
        ordered = sorted(table.values(), key=lambda r: (-r.priority, r.order))
        top = ordered[0]
        tied = [reg.name for reg in ordered if reg.priority == top.priority]
        if len(tied) > 1:
            raise PluginError(
                "存在多个同优先级的实现，必须显式指定 name",
                interface=_label(interface),
                candidates=tied,
                hint=f'例如 get({_label(interface)}, name="{tied[0]}")',
            )
        return top


def _label(interface: type[Any]) -> str:
    """取接口的可读名字（``Protocol`` 类也有 ``__name__``）。"""
    return getattr(interface, "__name__", None) or repr(interface)


def _missing_message(interface: type[Any], name: str | None, available: list[str]) -> str:
    """能力缺失时给出的可执行提示，而不是一个裸的 KeyError。"""
    label = _label(interface)
    if name is None:
        return (
            f"没有可用的 {label} 实现。"
            "请检查 config/alterego.toml 的 [plugins] enabled 是否启用了提供该能力的插件，"
            "并确认它启动成功（`alterego plugins list`）。"
        )
    if available:
        return f"没有名为 {name!r} 的 {label} 实现。已注册的名字：{', '.join(available)}"
    return (
        f"没有名为 {name!r} 的 {label} 实现，且该接口下没有任何实现。"
        "请检查 config/alterego.toml 与已启用的插件。"
    )

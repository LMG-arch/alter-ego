"""``alterego.kernel.registry`` 的行为测试。

这个模块的设计基调是**宁可报错也不猜**：
- 重复注册不覆盖
- 同优先级多实现必须显式指定名字
- ``get_optional`` 在「没有」时返回 ``None``，但在「有歧义」时照样抛异常

因为配置错误伪装成「功能没启用」是非常难排查的一类 bug。
"""

from __future__ import annotations

from typing import Protocol

import pytest

from alterego.kernel.errors import PluginError
from alterego.kernel.registry import ServiceRegistry


class Greeter(Protocol):
    def greet(self) -> str: ...


class Storage(Protocol):
    def write(self, data: str) -> None: ...


class HelloGreeter:
    def greet(self) -> str:
        return "hello"


class HiGreeter:
    def greet(self) -> str:
        return "hi"


class FakeStorage:
    def __init__(self) -> None:
        self.data: list[str] = []

    def write(self, data: str) -> None:
        self.data.append(data)


# ── 注册 ────────────────────────────────────────────────────


def test_register_and_get(registry: ServiceRegistry) -> None:
    greeter = HelloGreeter()
    registry.register(Greeter, greeter, name="hello")
    assert registry.get(Greeter) is greeter
    assert registry.get(Greeter, name="hello") is greeter


def test_register_requires_non_empty_name(registry: ServiceRegistry) -> None:
    """没有名字就无从判断歧义，也就给不出可执行的报错。"""
    with pytest.raises(PluginError):
        registry.register(Greeter, HelloGreeter(), name="")


def test_duplicate_name_is_rejected_not_overwritten(registry: ServiceRegistry) -> None:
    """静默覆盖会让两个插件争抢同一个名字时出现难以定位的行为漂移。"""
    registry.register(Greeter, HelloGreeter(), name="dup", owner="plugin.a")
    with pytest.raises(PluginError) as info:
        registry.register(Greeter, HiGreeter(), name="dup", owner="plugin.b")

    assert info.value.context["name"] == "dup"
    assert info.value.context["existing_owner"] == "plugin.a"
    assert info.value.context["new_owner"] == "plugin.b"
    # 原实现必须完好无损
    assert isinstance(registry.get(Greeter, name="dup"), HelloGreeter)


def test_same_name_on_different_interfaces_is_fine(registry: ServiceRegistry) -> None:
    registry.register(Greeter, HelloGreeter(), name="x")
    registry.register(Storage, FakeStorage(), name="x")
    assert registry.has(Greeter, "x") and registry.has(Storage, "x")


# ── 解析与歧义 ──────────────────────────────────────────────


def test_single_implementation_needs_no_name(registry: ServiceRegistry) -> None:
    greeter = HelloGreeter()
    registry.register(Greeter, greeter, name="only")
    assert registry.get(Greeter) is greeter


def test_multiple_implementations_pick_highest_priority(registry: ServiceRegistry) -> None:
    """优先级就是给这件事用的：插件用 priority 声明「我更该被选中」。"""
    low = HelloGreeter()
    high = HiGreeter()
    registry.register(Greeter, low, name="low", priority=0)
    registry.register(Greeter, high, name="high", priority=10)

    assert registry.get(Greeter) is high
    assert registry.get(Greeter, name="low") is low


def test_tied_priority_raises_with_candidates(registry: ServiceRegistry) -> None:
    registry.register(Greeter, HelloGreeter(), name="a", priority=5)
    registry.register(Greeter, HiGreeter(), name="b", priority=5)

    with pytest.raises(PluginError) as info:
        registry.get(Greeter)

    # 报错必须告诉用户「有哪些选项」，否则等于没说
    assert info.value.context["candidates"] == ["a", "b"]
    assert "name=" in str(info.value.context["hint"])


def test_missing_implementation_lists_registered_names(registry: ServiceRegistry) -> None:
    registry.register(Greeter, HelloGreeter(), name="a")
    registry.register(Greeter, HiGreeter(), name="b")

    with pytest.raises(PluginError) as info:
        registry.get(Greeter, name="c")

    assert "a, b" in str(info.value)


def test_missing_implementation_with_empty_table_gives_setup_hint(
    registry: ServiceRegistry,
) -> None:
    with pytest.raises(PluginError) as info:
        registry.get(Storage)
    # 提示要能直接指导下一步动作
    assert "plugins" in str(info.value)


def test_get_optional_returns_none_when_absent(registry: ServiceRegistry) -> None:
    assert registry.get_optional(Storage) is None
    assert registry.get_optional(Storage, name="nope") is None


def test_get_optional_still_raises_on_ambiguity(registry: ServiceRegistry) -> None:
    """「有歧义」不是「没有」。返回 None 会把配置错误伪装成「功能没启用」。"""
    registry.register(Greeter, HelloGreeter(), name="a", priority=1)
    registry.register(Greeter, HiGreeter(), name="b", priority=1)

    with pytest.raises(PluginError):
        registry.get_optional(Greeter)


# ── get_all ─────────────────────────────────────────────────


def test_get_all_sorted_by_priority(registry: ServiceRegistry) -> None:
    registry.register(Greeter, HelloGreeter(), name="low", priority=0)
    registry.register(Greeter, HiGreeter(), name="high", priority=9)

    result = registry.get_all(Greeter)

    assert [name for name, _ in result] == ["high", "low"]
    assert isinstance(result[0][1], HiGreeter)


def test_get_all_is_empty_for_unknown_interface(registry: ServiceRegistry) -> None:
    assert registry.get_all(Storage) == []


def test_get_all_returns_every_implementation(registry: ServiceRegistry) -> None:
    """渠道推送要的就是「全部」——不能让优先级把别的实现藏起来。"""
    registry.register(Greeter, HelloGreeter(), name="a")
    registry.register(Greeter, HiGreeter(), name="b")
    registry.register(Greeter, HelloGreeter(), name="c")
    assert len(registry.get_all(Greeter)) == 3


# ── 注销 ────────────────────────────────────────────────────


def test_unregister_is_idempotent(registry: ServiceRegistry) -> None:
    """热重载会重复走清理路径，重复注销不能炸。"""
    registry.register(Greeter, HelloGreeter(), name="a")
    registry.unregister(Greeter, "a")
    registry.unregister(Greeter, "a")
    assert not registry.has(Greeter, "a")


def test_unregister_owner_counts_removals(registry: ServiceRegistry) -> None:
    registry.register(Greeter, HelloGreeter(), name="a", owner="plugin.x")
    registry.register(Storage, FakeStorage(), name="b", owner="plugin.x")
    registry.register(Greeter, HiGreeter(), name="c", owner="plugin.y")

    assert registry.unregister_owner("plugin.x") == 2
    assert registry.unregister_owner("plugin.x") == 0
    assert registry.names(Greeter) == ["c"]


def test_clear_removes_everything(registry: ServiceRegistry) -> None:
    registry.register(Greeter, HelloGreeter(), name="a")
    registry.clear()
    assert registry.interfaces() == []
    assert not registry.has(Greeter)


# ── 内省 ────────────────────────────────────────────────────


def test_names_and_interfaces_are_sorted(registry: ServiceRegistry) -> None:
    registry.register(Greeter, HelloGreeter(), name="z")
    registry.register(Greeter, HiGreeter(), name="a")
    registry.register(Storage, FakeStorage(), name="s")

    assert registry.names(Greeter) == ["a", "z"]
    assert registry.interfaces() == ["Greeter", "Storage"]


def test_owner_of(registry: ServiceRegistry) -> None:
    registry.register(Greeter, HelloGreeter(), name="a", owner="plugin.x")
    assert registry.owner_of(Greeter, "a") == "plugin.x"
    assert registry.owner_of(Greeter, "missing") is None


def test_has_with_and_without_name(registry: ServiceRegistry) -> None:
    registry.register(Greeter, HelloGreeter(), name="a")
    assert registry.has(Greeter)
    assert registry.has(Greeter, "a")
    assert not registry.has(Greeter, "b")
    assert not registry.has(Storage)

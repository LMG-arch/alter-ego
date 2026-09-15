"""端到端验收：``plugins/example_plugin`` 必须真的能被加载与执行。

这个文件的存在本身就是一条约束：**示例代码会腐烂**。
README 与设计文档里的示例常年停留在「当时确实能跑」的状态，
等有人照着抄的时候才发现三个字段名已经改了。

所以不写「示例看起来对不对」，而是让内核**真的加载它**：
清单校验、依赖解析、导入、注册、执行、卸载，全部走一遍真实路径。
示例一旦腐烂，CI 立刻变红。

依据: CONTRIBUTING.md § 文档纪律
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from alterego.interfaces.simulation import Capability
from alterego.kernel.bus import EventBus
from alterego.kernel.clock import FrozenClock
from alterego.kernel.config import Config
from alterego.kernel.loader import discover, import_plugin_class
from alterego.kernel.manager import PluginManager
from alterego.kernel.registry import ServiceRegistry
from alterego.kernel.scheduler import Scheduler


PLUGIN_ID = "capability.example"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugins"


class Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []


def make_config(tmp_path: Path, *, enabled: list[str] | None = None) -> Config:
    return Config.load(
        path=None,
        env={},
        overrides={
            "core": {"data_dir": str(tmp_path / "data")},
            "plugins": {"enabled": enabled if enabled is not None else [PLUGIN_ID]},
        },
    )


def make_manager(
    tmp_path: Path, *, clock: FrozenClock, enabled: list[str] | None = None
) -> tuple[PluginManager, ServiceRegistry, EventBus]:
    bus = EventBus(clock, logger=logging.getLogger("test.example.bus"))
    registry = ServiceRegistry(logger=logging.getLogger("test.example.registry"))
    manager = PluginManager(
        config=make_config(tmp_path, enabled=enabled),
        bus=bus,
        registry=registry,
        clock=clock,
        scheduler=Scheduler(clock, logger=logging.getLogger("test.example.sched")),
        logger=logging.getLogger("test.example.manager"),
    )
    return manager, registry, bus


@pytest.fixture
def loaded(tmp_path: Path, clock: FrozenClock) -> Any:
    """加载示例插件，测完保证卸载（免得污染 ``sys.modules``）。"""
    manager, registry, bus = make_manager(tmp_path, clock=clock)
    report = manager.load_all()
    assert report.ok, f"示例插件加载失败：{report.failed}"
    yield manager, registry, bus
    manager.shutdown()


class TestExamplePluginManifest:
    def test_it_is_discoverable(self) -> None:
        result = discover(search_paths=[str(PLUGIN_ROOT)], entry_point_group="alterego.test.none")

        assert PLUGIN_ID in result.ids()
        assert result.failed == []

    def test_the_id_matches_its_kind(self) -> None:
        result = discover(search_paths=[str(PLUGIN_ROOT)], entry_point_group="alterego.test.none")

        found = result.get(PLUGIN_ID)
        assert found is not None
        manifest = found.manifest
        assert manifest.id.split(".", 1)[0] == manifest.kind
        assert manifest.name  # 面向人的名字不能空着

    def test_every_config_field_is_documented(self) -> None:
        """``alterego plugins config`` 会把这些说明原样打给用户。"""
        result = discover(search_paths=[str(PLUGIN_ROOT)], entry_point_group="alterego.test.none")

        found = result.get(PLUGIN_ID)
        assert found is not None
        manifest = found.manifest
        assert manifest.config, "示例插件应该演示配置声明"
        for name, spec in manifest.config.items():
            assert spec.description, f"配置字段 {name} 没有 description"

    def test_it_is_not_enabled_by_default(self) -> None:
        """示例插件不该在别人的实例里默默跑起来。"""
        result = discover(search_paths=[str(PLUGIN_ROOT)], entry_point_group="alterego.test.none")

        found = result.get(PLUGIN_ID)
        assert found is not None
        assert found.manifest.enabled_by_default is False


class TestExamplePluginBehaviour:
    def test_the_entry_point_resolves_to_a_plugin_class(self) -> None:
        result = discover(search_paths=[str(PLUGIN_ROOT)], entry_point_group="alterego.test.none")
        found = result.get(PLUGIN_ID)
        assert found is not None

        cls = import_plugin_class(found)

        assert cls.__name__ == "ExampleCapability"
        assert cls.id == PLUGIN_ID

    def test_it_loads_and_registers_its_capability(self, loaded: Any) -> None:
        _manager, registry, _bus = loaded

        service = registry.get(Capability, name="example")

        assert service.greeting() == "你好!"

    def test_it_uses_the_declared_defaults(self, loaded: Any) -> None:
        _manager, registry, _bus = loaded

        assert registry.get(Capability, name="example").greeting() == "你好!"

    def test_config_overrides_are_honoured(self, tmp_path: Path, clock: FrozenClock) -> None:
        """从清单默认值到运行期行为，这条路必须真的通。"""
        bus = EventBus(clock, logger=logging.getLogger("test.example.bus2"))
        registry = ServiceRegistry(logger=logging.getLogger("test.example.registry2"))
        config = Config.load(
            path=None,
            env={},
            overrides={
                "core": {"data_dir": str(tmp_path / "data")},
                "plugins": {
                    "enabled": [PLUGIN_ID],
                    "config": {PLUGIN_ID: {"greeting": "早上好", "punctuation": "~", "repeat": 2}},
                },
            },
        )
        manager = PluginManager(
            config=config,
            bus=bus,
            registry=registry,
            clock=clock,
            scheduler=Scheduler(clock, logger=logging.getLogger("test.example.sched2")),
            logger=logging.getLogger("test.example.manager2"),
        )
        try:
            assert manager.load_all().ok
            assert registry.get(Capability, name="example").greeting() == "早上好~早上好~"
        finally:
            manager.shutdown()

    async def test_execute_returns_a_human_readable_summary(self, loaded: Any) -> None:
        _manager, registry, _bus = loaded
        service = registry.get(Capability, name="example")

        result = await service.execute(None, None)

        assert result.ok
        assert result.artifacts["text"] == "你好!"
        # summary 会直接显示给用户，所以「执行成功」这种话等于没写。
        assert "你好!" in result.summary

    def test_it_subscribes_to_tick_completed(self, loaded: Any) -> None:
        _manager, _registry, bus = loaded

        assert bus.subscriber_count("tick.completed") == 1


class TestExamplePluginLifecycle:
    def test_shutdown_cleans_up_registrations_and_subscriptions(self, loaded: Any) -> None:
        """这是 ADR-0007 要解决的问题，示例插件必须示范正确的一侧。"""
        manager, registry, bus = loaded
        assert registry.names(Capability)

        manager.shutdown()

        assert registry.names(Capability) == []
        assert bus.subscriber_count("tick.completed") == 0

    def test_stop_is_idempotent(self, tmp_path: Path, clock: FrozenClock) -> None:
        manager, registry, _bus = make_manager(tmp_path, clock=clock)
        assert manager.load_all().ok
        service = registry.get(Capability, name="example")

        service.on_stop()
        service.on_stop()

    def test_config_change_takes_effect_without_a_restart(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        manager, registry, _bus = make_manager(tmp_path, clock=clock)
        try:
            assert manager.load_all().ok
            service = registry.get(Capability, name="example")
            assert service.greeting() == "你好!"

            service.on_config_changed({"greeting": "早", "punctuation": "。", "repeat": 3})

            assert service.greeting() == "早。早。早。"
        finally:
            manager.shutdown()

    def test_health_is_reported(self, loaded: Any) -> None:
        _manager, registry, _bus = loaded

        assert registry.get(Capability, name="example").health().ok

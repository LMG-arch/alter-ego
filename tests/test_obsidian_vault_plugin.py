"""端到端验收：``plugins/obsidian_vault`` 必须真的能被加载与执行。

和 ``test_example_plugin.py`` 走的是**同一条真实路径**：清单校验 → 依赖解析
→ 导入 → 注册 → 描述 → 卸载。区别在于这个文件要证明的东西不一样。

知识库插件是**故意薄**的（见它自己的模块 docstring）：它不做编排，只做声明。
所以这里不测「它整理了知识库」——那件事在 ``sim/vault.py``，由
``tests/test_sim_vault.py`` 和 ``tests/test_cli_vault.py`` 负责。这里测的是：

- 它挂在哪个名字下（``obsidian_vault``，不是 ``capability.obsidian_vault``）；
- 它**没有**挤进推演循环（``intent_types`` 是空的，也不订阅任何事件）；
- 三种状态下 ``describe()`` 说的是不是人话，因为那句话会直接打进终端。

依据: docs/plans/2026-09-16-obsidian-vault.md § 2.2、§ 8
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
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


PLUGIN_ID = "capability.obsidian_vault"
SERVICE_NAME = "obsidian_vault"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugins"


def make_config(
    tmp_path: Path, *, enabled: list[str] | None = None, vault_path: str = ""
) -> Config:
    return Config.load(
        path=None,
        env={},
        overrides={
            "core": {"data_dir": str(tmp_path / "data")},
            "plugins": {
                "enabled": enabled if enabled is not None else [PLUGIN_ID],
                "config": {PLUGIN_ID: {"vault_path": vault_path}},
            },
        },
    )


@contextmanager
def load_plugin(
    tmp_path: Path, clock: FrozenClock, *, vault_path: str = ""
) -> Iterator[tuple[ServiceRegistry, EventBus, Any]]:
    """加载知识库插件，测完保证卸载（免得污染 ``sys.modules``）。"""
    bus = EventBus(clock, logger=logging.getLogger("test.vault.bus"))
    registry = ServiceRegistry(logger=logging.getLogger("test.vault.registry"))
    manager = PluginManager(
        config=make_config(tmp_path, vault_path=vault_path),
        bus=bus,
        registry=registry,
        clock=clock,
        scheduler=Scheduler(clock, logger=logging.getLogger("test.vault.sched")),
        logger=logging.getLogger("test.vault.manager"),
    )
    report = manager.load_all()
    assert report.ok, f"知识库插件加载失败：{report.failed}"
    try:
        yield registry, bus, manager
    finally:
        manager.shutdown()


def discovered() -> Any:
    """在真实的 ``plugins/`` 目录里找到这个插件。"""
    result = discover(search_paths=[str(PLUGIN_ROOT)], entry_point_group="alterego.test.none")
    found = result.get(PLUGIN_ID)
    assert found is not None, f"没找到 {PLUGIN_ID}；找到的是 {result.ids()}"
    return found


@pytest.fixture
def vault_service(tmp_path: Path, clock: FrozenClock) -> Iterator[Any]:
    """默认配置下加载好的插件实例。"""
    with load_plugin(tmp_path, clock) as (registry, _bus, _manager):
        yield registry.get(Capability, name=SERVICE_NAME)


# ────────────────────────────────────────────────────────────
# 清单
# ────────────────────────────────────────────────────────────


class TestManifest:
    def test_it_is_discoverable(self) -> None:
        result = discover(search_paths=[str(PLUGIN_ROOT)], entry_point_group="alterego.test.none")

        assert PLUGIN_ID in result.ids()
        assert result.failed == []

    def test_the_id_matches_its_kind(self) -> None:
        manifest = discovered().manifest

        assert manifest.id.split(".", 1)[0] == manifest.kind
        assert manifest.kind == "capability"

    def test_it_speaks_the_current_plugin_api(self) -> None:
        """``api_version`` 是内核与插件之间的契约版本，改它等于破坏所有插件。"""
        assert discovered().manifest.api_version == 1

    def test_the_config_field_is_documented(self) -> None:
        """``alterego plugins config`` 会把这些说明原样打给用户。"""
        manifest = discovered().manifest

        assert manifest.config, "知识库插件应该声明 vault_path"
        for name, spec in manifest.config.items():
            assert spec.description, f"配置字段 {name} 没有 description"

    def test_it_is_not_enabled_by_default(self) -> None:
        """装上插件包不等于打开功能；用户得自己把它写进 ``[plugins] enabled``。"""
        assert discovered().manifest.enabled_by_default is False

    def test_the_entry_point_resolves_to_the_plugin_class(self) -> None:
        cls = import_plugin_class(discovered())

        assert cls.__name__ == "ObsidianVault"
        assert cls.id == PLUGIN_ID


# ────────────────────────────────────────────────────────────
# 加载与注册
# ────────────────────────────────────────────────────────────


class TestLoading:
    def test_it_registers_under_a_short_name(self, tmp_path: Path, clock: FrozenClock) -> None:
        """注册名是 ``obsidian_vault`` 而不是完整 id。

        完整 id 里那个 ``capability.`` 前缀是**分类**，不是名字；
        ``get_service`` 的调用方关心的是「哪个能力」。
        """
        with load_plugin(tmp_path, clock) as (registry, _bus, _manager):
            assert registry.get(Capability, name=SERVICE_NAME) is not None

    def test_it_does_not_join_the_tick_loop(self, vault_service: Any) -> None:
        """``intent_types`` 是空集合，推演循环不会挑中它。

        整理一次要读整张日程表、问一次模型、重写一遍索引——那是几十秒的
        批处理。塞进意图候选里，推演循环迟早会在一个 30 秒的 tick 中间等它。
        """
        assert vault_service.intent_types == frozenset()

    def test_it_subscribes_to_nothing(self, tmp_path: Path, clock: FrozenClock) -> None:
        """没有需要跟着推演走的事，就不订阅。

        每 30 秒醒一次只为了记一个没人看的 tick 编号是纯噪声。
        """
        with load_plugin(tmp_path, clock) as (_registry, bus, _manager):
            assert bus.subscriber_count("tick.completed") == 0

    def test_shutdown_cleans_up_registrations(self, tmp_path: Path, clock: FrozenClock) -> None:
        """ADR-0007 要求插件自己登记归属，停机时注册表必须回到干净状态。"""
        with load_plugin(tmp_path, clock) as (registry, _bus, manager):
            assert registry.names(Capability)

            manager.shutdown()

            assert registry.names(Capability) == []

    def test_stop_is_idempotent(self, vault_service: Any) -> None:
        """停机与热重载都会调用 ``on_stop``，调两次不能炸。"""
        vault_service.on_stop()
        vault_service.on_stop()


# ────────────────────────────────────────────────────────────
# 它说的话
# ────────────────────────────────────────────────────────────


class TestDescribe:
    def test_without_a_configured_path(self, vault_service: Any) -> None:
        """默认 ``vault_path`` 是空串，意思不是「坏了」。"""
        said = vault_service.describe()

        assert "alterego vault" in said
        assert vault_service.vault_path is None

    def test_when_the_directory_exists(
        self, tmp_path: Path, clock: FrozenClock, vault_path: Path
    ) -> None:
        with load_plugin(tmp_path, clock, vault_path=str(vault_path)) as (registry, _bus, _m):
            service = registry.get(Capability, name=SERVICE_NAME)

            said = service.describe()

            assert str(vault_path) in said
            assert "vault init" not in said

    def test_when_the_directory_is_missing(self, tmp_path: Path, clock: FrozenClock) -> None:
        missing = tmp_path / "还没建的知识库"
        with load_plugin(tmp_path, clock, vault_path=str(missing)) as (registry, _bus, _m):
            service = registry.get(Capability, name=SERVICE_NAME)

            said = service.describe()

            assert "vault init" in said

    @pytest.fixture
    def vault_path(self, tmp_path: Path) -> Path:
        made = tmp_path / "知识库"
        made.mkdir()
        return made


class TestHealth:
    def test_it_is_ok_without_a_configured_path(self, vault_service: Any) -> None:
        """「还没建库」是完全正常的中间状态，不该让 ``plugins doctor`` 报警。"""
        assert vault_service.health().ok is True

    def test_it_is_ok_when_the_directory_is_missing(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        missing = tmp_path / "还没建的知识库"
        with load_plugin(tmp_path, clock, vault_path=str(missing)) as (registry, _bus, _m):
            status = registry.get(Capability, name=SERVICE_NAME).health()

            assert status.ok is True
            assert "vault init" in status.detail

    def test_the_detail_is_the_same_sentence_as_describe(self, vault_service: Any) -> None:
        """两处说不一样的话，用户就得自己判断信哪个。"""
        assert vault_service.health().detail == vault_service.describe()


# ────────────────────────────────────────────────────────────
# 配置热改与执行
# ────────────────────────────────────────────────────────────


class TestConfigChange:
    def test_it_takes_effect_without_a_restart(self, vault_service: Any) -> None:
        assert vault_service.vault_path is None

        vault_service.on_config_changed({"vault_path": "  /tmp/某个库  "})

        assert str(vault_service.vault_path) == str(Path("/tmp/某个库"))

    def test_clearing_it_goes_back_to_the_default(self, tmp_path: Path, clock: FrozenClock) -> None:
        with load_plugin(tmp_path, clock, vault_path=str(tmp_path / "库")) as (registry, _b, _m):
            service = registry.get(Capability, name=SERVICE_NAME)
            assert service.vault_path is not None

            service.on_config_changed({"vault_path": "   "})

            assert service.vault_path is None


class TestExecute:
    async def test_it_returns_a_capability_result(self, vault_service: Any) -> None:
        result = await vault_service.execute(None, None)

        assert result.ok is True
        # summary 会直接显示给用户，「执行成功」这种话等于没写。
        assert "知识库" in result.summary
        assert result.artifacts["vault_path"] == ""

    async def test_it_reports_the_path_when_there_is_one(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        root = tmp_path / "库"
        with load_plugin(tmp_path, clock, vault_path=str(root)) as (registry, _bus, _m):
            service = registry.get(Capability, name=SERVICE_NAME)

            result = await service.execute(None, None)

            assert result.artifacts["vault_path"] == str(root)

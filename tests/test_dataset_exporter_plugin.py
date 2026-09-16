"""端到端验收：``plugins/dataset_exporter`` 必须真的能被加载与执行。

和 ``test_obsidian_vault_plugin.py`` 走的是**同一条真实路径**：清单校验 →
依赖解析 → 导入 → 注册 → 描述 → 卸载。区别在于这里要证明的东西不一样。

导出插件也是**故意薄**的（见它自己的模块 docstring）：它不做编排，只做声明。
所以这里不测「它导出了数据集」——那件事在 ``sim/dataset.py``，由
``tests/test_sim_dataset.py`` 和 ``tests/test_cli_dataset.py`` 负责。这里测的是：

- 它挂在哪个名字下（``dataset_exporter``，不是 ``capability.dataset_exporter``）；
- 它**没有**挤进推演循环（``intent_types`` 是空的，也不订阅任何事件）；
- 三项配置真的会被读进来、改了立刻生效，因为那三项是唯一会改变导出结果的东西。

依据: docs/adr/0011-training-datasets-are-derived-and-redacted.md
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


PLUGIN_ID = "capability.dataset_exporter"
SERVICE_NAME = "dataset_exporter"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugins"


def make_config(
    tmp_path: Path,
    *,
    enabled: list[str] | None = None,
    export_dir: str = "",
    formats: list[str] | None = None,
    redact_terms: list[str] | None = None,
) -> Config:
    return Config.load(
        path=None,
        env={},
        overrides={
            "core": {"data_dir": str(tmp_path / "data")},
            "plugins": {
                "enabled": enabled if enabled is not None else [PLUGIN_ID],
                "config": {
                    PLUGIN_ID: {
                        "export_dir": export_dir,
                        "formats": formats if formats is not None else [],
                        "redact_terms": redact_terms if redact_terms is not None else [],
                    }
                },
            },
        },
    )


@contextmanager
def load_plugin(
    tmp_path: Path,
    clock: FrozenClock,
    *,
    export_dir: str = "",
    formats: list[str] | None = None,
    redact_terms: list[str] | None = None,
) -> Iterator[tuple[ServiceRegistry, EventBus, Any]]:
    """加载导出插件，测完保证卸载（免得污染 ``sys.modules``）。"""
    bus = EventBus(clock, logger=logging.getLogger("test.dataset.bus"))
    registry = ServiceRegistry(logger=logging.getLogger("test.dataset.registry"))
    manager = PluginManager(
        config=make_config(
            tmp_path,
            export_dir=export_dir,
            formats=formats,
            redact_terms=redact_terms,
        ),
        bus=bus,
        registry=registry,
        clock=clock,
        scheduler=Scheduler(clock, logger=logging.getLogger("test.dataset.sched")),
        logger=logging.getLogger("test.dataset.manager"),
    )
    report = manager.load_all()
    assert report.ok, f"导出插件加载失败：{report.failed}"
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
def exporter(tmp_path: Path, clock: FrozenClock) -> Iterator[Any]:
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

    def test_every_config_field_is_documented(self) -> None:
        """``alterego plugins config`` 会把这些说明原样打给用户。"""
        manifest = discovered().manifest

        assert set(manifest.config) == {"export_dir", "formats", "redact_terms"}
        for name, spec in manifest.config.items():
            assert spec.description, f"配置字段 {name} 没有 description"

    def test_the_extra_terms_are_an_array_of_strings(self) -> None:
        """写成逗号分隔的字符串会让人以为能填一个带逗号的词。"""
        spec = discovered().manifest.config["redact_terms"]

        assert spec.type == "array"
        assert spec.item_type == "string"

    def test_it_is_not_enabled_by_default(self) -> None:
        """装上插件包不等于打开功能；用户得自己把它写进 ``[plugins] enabled``。"""
        assert discovered().manifest.enabled_by_default is False

    def test_the_entry_point_resolves_to_the_plugin_class(self) -> None:
        cls = import_plugin_class(discovered())

        assert cls.__name__ == "DatasetExporter"
        assert cls.id == PLUGIN_ID


# ────────────────────────────────────────────────────────────
# 加载与注册
# ────────────────────────────────────────────────────────────


class TestLoading:
    def test_it_registers_under_a_short_name(self, tmp_path: Path, clock: FrozenClock) -> None:
        """注册名是 ``dataset_exporter`` 而不是完整 id。

        完整 id 里那个 ``capability.`` 前缀是**分类**，不是名字；
        ``get_service`` 的调用方关心的是「哪个能力」。
        """
        with load_plugin(tmp_path, clock) as (registry, _bus, _manager):
            assert registry.get(Capability, name=SERVICE_NAME) is not None

    def test_it_does_not_join_the_tick_loop(self, exporter: Any) -> None:
        """``intent_types`` 是空集合，推演循环不会挑中它。

        导出要回看几十天日志、几万条消息，跑一次按秒算。挂进意图候选里，
        推演循环会在一个 30 秒的 tick 中间等它。
        """
        assert exporter.intent_types == frozenset()

    def test_it_subscribes_to_nothing(self, tmp_path: Path, clock: FrozenClock) -> None:
        """没有需要跟着推演走的事，就不订阅。

        挂到 ``tick.completed`` 上等于每 30 秒重写一遍整个训练集。
        """
        with load_plugin(tmp_path, clock) as (_registry, bus, _manager):
            assert bus.subscriber_count("tick.completed") == 0

    def test_shutdown_cleans_up_registrations(self, tmp_path: Path, clock: FrozenClock) -> None:
        """ADR-0007 要求插件自己登记归属，停机时注册表必须回到干净状态。"""
        with load_plugin(tmp_path, clock) as (registry, _bus, manager):
            assert registry.names(Capability)

            manager.shutdown()

            assert registry.names(Capability) == []

    def test_stop_is_idempotent(self, exporter: Any) -> None:
        """停机与热重载都会调用 ``on_stop``，调两次不能炸。"""
        exporter.on_stop()
        exporter.on_stop()


# ────────────────────────────────────────────────────────────
# 配置真的被读进来了
# ────────────────────────────────────────────────────────────


class TestConfig:
    def test_the_defaults_mean_the_command_decides(self, exporter: Any) -> None:
        """空不是「坏了」，是「按 ``alterego dataset`` 的约定来」。"""
        assert exporter.export_dir is None
        assert exporter.formats == ()
        assert exporter.redact_terms == ()

    def test_it_takes_the_configured_directory(self, tmp_path: Path, clock: FrozenClock) -> None:
        target = tmp_path / "别处"
        with load_plugin(tmp_path, clock, export_dir=str(target)) as (registry, _b, _m):
            service = registry.get(Capability, name=SERVICE_NAME)

            assert service.export_dir == target

    def test_it_takes_the_configured_formats(self, tmp_path: Path, clock: FrozenClock) -> None:
        with load_plugin(tmp_path, clock, formats=["chat", "alpaca"]) as (registry, _bus, _manager):
            service = registry.get(Capability, name=SERVICE_NAME)

            assert service.formats == ("chat", "alpaca")

    def test_it_takes_the_configured_extra_terms(self, tmp_path: Path, clock: FrozenClock) -> None:
        with load_plugin(tmp_path, clock, redact_terms=["某个词"]) as (registry, _b, _m):
            service = registry.get(Capability, name=SERVICE_NAME)

            assert service.redact_terms == ("某个词",)


# ────────────────────────────────────────────────────────────
# 它说的话
# ────────────────────────────────────────────────────────────


class TestDescribe:
    def test_without_any_configuration(self, exporter: Any) -> None:
        """默认状态下要把「落点由命令决定」说清，而不是含糊过去。"""
        said = exporter.describe()

        assert "alterego dataset" in said
        assert "[dataset] formats" in said

    def test_it_always_mentions_the_builtin_rules(self, exporter: Any) -> None:
        """八条内置规则**没有开关**，默认那句话说出口也得带着这一层保证。"""
        assert "内置八条" in exporter.describe()

    def test_it_counts_the_extra_terms(self, tmp_path: Path, clock: FrozenClock) -> None:
        with load_plugin(tmp_path, clock, redact_terms=["a", "b"]) as (registry, _b, _m):
            said = registry.get(Capability, name=SERVICE_NAME).describe()

            assert "2 个自定义词" in said

    def test_it_names_the_configured_directory(self, tmp_path: Path, clock: FrozenClock) -> None:
        target = tmp_path / "自定义导出"
        with load_plugin(tmp_path, clock, export_dir=str(target)) as (registry, _b, _m):
            said = registry.get(Capability, name=SERVICE_NAME).describe()

            assert str(target) in said

    def test_it_lists_the_configured_shapes(self, tmp_path: Path, clock: FrozenClock) -> None:
        with load_plugin(tmp_path, clock, formats=["chat", "alpaca"]) as (registry, _b, _m):
            said = registry.get(Capability, name=SERVICE_NAME).describe()

            assert "chat/alpaca" in said

    def test_it_does_not_claim_to_know_whether_anything_was_exported(self, exporter: Any) -> None:
        """「导过没有」要看磁盘，插件碰不到磁盘，所以不许它说这句话。

        说「已经导好了」而其实是空的，比不说更糟——用户会直接拿它去训练。
        真实的答案在 ``alterego dataset list`` 里，靠读 ``manifest.json``。
        """
        said = exporter.describe()

        assert "已经" not in said
        assert "最新" not in said


class TestHealth:
    def test_it_is_ok_even_without_a_configured_directory(self, exporter: Any) -> None:
        """「没配 export_dir」是正常默认状态，不该让 ``plugins doctor`` 报警。"""
        assert exporter.health().ok is True

    def test_the_detail_is_the_same_sentence_as_describe(self, exporter: Any) -> None:
        """两处说不一样的话，用户就得自己判断信哪个。"""
        assert exporter.health().detail == exporter.describe()


# ────────────────────────────────────────────────────────────
# 配置热改与执行
# ────────────────────────────────────────────────────────────


class TestConfigChange:
    def test_it_takes_effect_without_a_restart(self, exporter: Any, tmp_path: Path) -> None:
        assert exporter.export_dir is None

        exporter.on_config_changed(
            {
                "export_dir": f"  {tmp_path / '某个导出'}  ",
                "formats": ["alpaca"],
                "redact_terms": ["词"],
            }
        )

        assert exporter.export_dir == tmp_path / "某个导出"
        assert exporter.formats == ("alpaca",)
        assert exporter.redact_terms == ("词",)

    def test_clearing_it_goes_back_to_the_default(self, tmp_path: Path, clock: FrozenClock) -> None:
        with load_plugin(tmp_path, clock, export_dir=str(tmp_path / "别处")) as (registry, _b, _m):
            service = registry.get(Capability, name=SERVICE_NAME)
            assert service.export_dir is not None

            service.on_config_changed({"export_dir": "", "formats": [], "redact_terms": []})

            assert service.export_dir is None
            assert service.formats == ()

    def test_a_missing_key_is_loud(self, exporter: Any) -> None:
        """键少了要当场炸，不能当成空值收下。

        内核传的是本次热重载的**完整快照**，三个键一定都在。哪天少了
        一个，那是内核断了契约：静默收空会让用户看到「没配」，
        而真正的原因是插件系统的 bug。（同样的写法在 ``obsidian_vault``
        里已经用了，两边保持一致。）
        """
        with pytest.raises(KeyError):
            exporter.on_config_changed({})


class TestExecute:
    async def test_execute_reports_the_configuration(self, exporter: Any) -> None:
        """``intent_types`` 是空的，所以推演循环不会调到这里。

        它是给 ``alterego plugins list`` 和测试用的，返回的是一句摘要
        加上几个供机器读的字段。
        """
        result = await exporter.execute(object(), object())

        assert result.ok is True
        assert result.summary == exporter.describe()
        assert result.artifacts["export_dir"] == ""
        assert result.artifacts["formats"] == ""
        assert result.artifacts["redact_terms"] == 0

    async def test_execute_counts_the_extra_terms(self, tmp_path: Path, clock: FrozenClock) -> None:
        target = tmp_path / "out"
        with load_plugin(
            tmp_path, clock, export_dir=str(target), formats=["chat"], redact_terms=["a", "b"]
        ) as (registry, _bus, _manager):
            service = registry.get(Capability, name=SERVICE_NAME)

            result = await service.execute(object(), object())

            assert result.artifacts["export_dir"] == str(target)
            assert result.artifacts["formats"] == "chat"
            assert result.artifacts["redact_terms"] == 2

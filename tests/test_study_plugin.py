"""端到端验收：``plugins/study`` 必须真的能被加载与执行。

和 ``test_dataset_exporter_plugin.py`` 走的是**同一条真实路径**：清单校验 →
依赖解析 → 导入 → 注册 → 描述 → 卸载。

专项学习插件也是**故意薄**的（见它自己的模块 docstring）：它不做编排，
只做声明。所以这里不测「它学会了什么」——那件事在 ``domain/study.py`` 与
``sim/study.py``，由 ``tests/test_domain_study.py``、``tests/test_sim_study.py``
和 ``tests/test_cli_study.py`` 负责。这里测的是：

- 它挂在哪个名字下（``study``，不是 ``capability.study``）；
- 它**没有**挤进推演循环（``intent_types`` 是空的，也不订阅任何事件）——
  一次 ``study next`` 要花钱，该由人来按，不该由 tick 来选；
- 它**没有**配置项，而且这是设计决定：四个真旋钮在 ``[study]`` 段里，
  抄一份过来就是一份设置的两个真源。用户真在 ``[plugins.config]`` 下写了
  点什么，插件不记、不抛、只记一行日志。

依据: docs/adr/0012-specialized-study-is-a-curriculum-not-a-prompt.md
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


PLUGIN_ID = "capability.study"
SERVICE_NAME = "study"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugins"


def make_config(
    tmp_path: Path,
    *,
    enabled: list[str] | None = None,
    plugin_config: dict[str, Any] | None = None,
) -> Config:
    return Config.load(
        path=None,
        env={},
        overrides={
            "core": {"data_dir": str(tmp_path / "data")},
            "plugins": {
                "enabled": enabled if enabled is not None else [PLUGIN_ID],
                "config": {PLUGIN_ID: plugin_config} if plugin_config is not None else {},
            },
        },
    )


@contextmanager
def load_plugin(
    tmp_path: Path,
    clock: FrozenClock,
    *,
    plugin_config: dict[str, Any] | None = None,
) -> Iterator[tuple[ServiceRegistry, EventBus, PluginManager]]:
    """加载专项学习插件，测完保证卸载（免得污染 ``sys.modules``）。"""
    bus = EventBus(clock, logger=logging.getLogger("test.study.bus"))
    registry = ServiceRegistry(logger=logging.getLogger("test.study.registry"))
    manager = PluginManager(
        config=make_config(tmp_path, plugin_config=plugin_config),
        bus=bus,
        registry=registry,
        clock=clock,
        scheduler=Scheduler(clock, logger=logging.getLogger("test.study.sched")),
        logger=logging.getLogger("test.study.manager"),
    )
    report = manager.load_all()
    assert report.ok, f"专项学习插件加载失败：{report.failed}"
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
def study(tmp_path: Path, clock: FrozenClock) -> Iterator[Any]:
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

    def test_it_declares_no_config_on_purpose(self) -> None:
        """清单里不该出现任何 ``[plugin.config.*]``。

        四个真旋钮（``field`` / ``rounds`` / ``recall_limit`` / ``min_score``）
        在 ``[study]`` 段里，``alterego study`` 直接读那里。在这里再声明一份
        就是一份设置的**两个真源**：改插件这份不会改命令的行为，改命令那份
        又不会改插件说的话——两边都「看起来对」，而其中一个是谎话（P4）。

        哪天要给插件加配置，先回答「命令读哪一份」。答不出来就别加。
        """
        assert discovered().manifest.config == {}

    def test_it_is_not_enabled_by_default(self) -> None:
        """装上插件不等于打开功能；用户得自己把它写进 ``[plugins] enabled``。"""
        assert discovered().manifest.enabled_by_default is False

    def test_the_entry_point_resolves_to_the_plugin_class(self) -> None:
        cls = import_plugin_class(discovered())

        assert cls.__name__ == "Study"
        assert cls.id == PLUGIN_ID


# ────────────────────────────────────────────────────────────
# 加载与注册
# ────────────────────────────────────────────────────────────


class TestLoading:
    def test_it_registers_under_a_short_name(self, tmp_path: Path, clock: FrozenClock) -> None:
        """注册名是 ``study`` 而不是完整 id。

        完整 id 里那个 ``capability.`` 前缀是**分类**，不是名字；
        ``get_service`` 的调用方关心的是「哪个能力」。
        """
        with load_plugin(tmp_path, clock) as (registry, _bus, _manager):
            assert registry.get(Capability, name=SERVICE_NAME) is not None

    def test_it_does_not_join_the_tick_loop(self, study: Any) -> None:
        """``intent_types`` 是空集合，推演循环不会挑中它。

        一次学习要调模型、写文件，按秒算，而且花的是用户的钱。挂进意图
        候选里，推演循环会在一个 30 秒的 tick 中间替用户花钱。
        """
        assert study.intent_types == frozenset()

    def test_it_subscribes_to_nothing(self, tmp_path: Path, clock: FrozenClock) -> None:
        """没有需要跟着推演走的事，就不订阅。

        挂到 ``tick.completed`` 上等于让它自己夜夜开花——用户第二天会
        发现账单，却不一定想得起来是自己昨晚打开了哪个开关。
        """
        with load_plugin(tmp_path, clock) as (_registry, bus, _manager):
            assert bus.subscriber_count("tick.completed") == 0

    def test_shutdown_cleans_up_registrations(self, tmp_path: Path, clock: FrozenClock) -> None:
        """ADR-0007 要求插件自己登记归属，停机时注册表必须回到干净状态。"""
        with load_plugin(tmp_path, clock) as (registry, _bus, manager):
            assert registry.names(Capability)

            manager.shutdown()

            assert registry.names(Capability) == []

    def test_stop_is_idempotent(self, study: Any) -> None:
        """停机与热重载都会调用 ``on_stop``，调两次不能炸。"""
        study.on_stop()
        study.on_stop()


# ────────────────────────────────────────────────────────────
# 配置块：它不是旋钮
# ────────────────────────────────────────────────────────────


class TestConfigBlock:
    def test_an_undeclared_key_does_not_break_the_plugin(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        """用户在 ``[plugins.config."capability.study"]`` 下写了东西也不会炸。

        内核只警告不报错（插件可能故意读清单外的键），所以插件不能假设
        ``ctx.config`` 是空的。**但也仅止于此**：那个键不改行为，
        真正该改的地方是 ``[study]``。这条测试证明的是「不会炸」，
        不是「会生效」——下面两条才是在说后一半。
        """
        with load_plugin(tmp_path, clock, plugin_config={"field": "计算机软件"}) as (
            registry,
            _bus,
            _manager,
        ):
            service = registry.get(Capability, name=SERVICE_NAME)

            assert "occupation" in service.describe()

    def test_a_config_change_is_accepted_and_changes_nothing(self, study: Any) -> None:
        """``on_config_changed`` 必须收下内核给的东西，但不能假装它有用。

        收下：热重载时内核会调它，抛异常等于插件在改动配置后掉线。
        不能假装有用：接进状态里就会在下游变成「改了配置却没生效」，
        而真正的原因是那四个旋钮在 ``[study]`` 段里。
        """
        said_before = study.describe()

        study.on_config_changed({"field": "计算机软件", "rounds": 5})

        assert study.describe() == said_before

    def test_leaving_the_block_out_is_normal(self, tmp_path: Path, clock: FrozenClock) -> None:
        """不写配置块是最常见的用法，不该有任何异常。"""
        with load_plugin(tmp_path, clock) as (registry, _bus, _manager):
            assert registry.get(Capability, name=SERVICE_NAME) is not None


# ────────────────────────────────────────────────────────────
# 它说的话
# ────────────────────────────────────────────────────────────


class TestDescribe:
    def test_it_says_where_the_knobs_are(self, study: Any) -> None:
        """它没有配置项，所以必须说清旋钮在哪，否则用户只会到处试。"""
        assert "[study]" in study.describe()

    def test_it_names_the_source_of_the_direction(self, study: Any) -> None:
        """方向是认出来的，不是猜出来的——这句话得说到。"""
        assert "occupation" in study.describe()

    def test_it_names_where_the_notes_go(self, study: Any) -> None:
        """学到的东西落在知识库的哪一层，用户要能直接去找。"""
        assert "60-专业" in study.describe()

    def test_it_points_at_the_status_command(self, study: Any) -> None:
        """「学到哪了」不在这句话里回答，所以得给出去处。"""
        assert "alterego study status" in study.describe()

    def test_it_does_not_claim_to_know_progress(self, study: Any) -> None:
        """进度是磁盘上的事实（``00-索引/`` 里的文件），插件碰不到磁盘。

        说「已经学到第 5 格」而其实一格都没学，比不说更糟——用户会以为
        它在装懂。真实的答案在 ``alterego study status`` 里。
        """
        said = study.describe()

        assert "已经" not in said
        assert "最新" not in said
        assert "学到第" not in said


class TestHealth:
    def test_it_is_ok_even_before_anything_is_learned(self, study: Any) -> None:
        """「还没开始学」「occupation 认不出方向」都不是插件的问题。

        前者是磁盘上的进度，后者由 ``alterego study next`` 当场告诉用户
        该去哪儿填 ``field``。报 ``ok=False`` 会让 ``plugins doctor``
        在一件没出错的事情上报警。
        """
        assert study.health().ok is True

    def test_the_detail_is_the_same_sentence_as_describe(self, study: Any) -> None:
        """两处说不一样的话，用户就得自己判断信哪个。"""
        assert study.health().detail == study.describe()


# ────────────────────────────────────────────────────────────
# 执行
# ────────────────────────────────────────────────────────────


class TestExecute:
    async def test_execute_reports_the_declaration(self, study: Any) -> None:
        """``intent_types`` 是空的，所以推演循环不会调到这里。

        它是给 ``alterego plugins list``、Web 的插件页和测试用的：
        一句摘要，加上几个供机器读的字段。
        """
        result = await study.execute(object(), object())

        assert result.ok is True
        assert result.summary == study.describe()
        assert result.artifacts["config_section"] == "study"
        assert result.artifacts["state_command"] == "alterego study status"

    async def test_execute_says_which_step_costs_money(self, study: Any) -> None:
        """四个子命令里只有 ``next`` 会花钱，这一点要说出来。

        「哪个按钮要钱」是用户最需要知道、也最容易在文档里丢的一件事。
        """
        result = await study.execute(object(), object())

        assert result.artifacts["paid_command"] == "alterego study next"
        assert "next" not in result.artifacts["free_commands"]

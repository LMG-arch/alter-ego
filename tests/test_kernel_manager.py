"""``alterego.kernel.manager`` 的测试。

这个文件测的是**编排**而不是插件本身：顺序、隔离、熔断、热重载、关闭。
所以大部分用例在验证「一个坏插件不会影响别的插件」这个承诺。

插件是**真的写到磁盘上再被真的 import** 的——不 mock。理由：
``PluginManager`` 的职责之一就是和 ``importlib`` 打交道（缓存、命名空间、
重载），mock 掉这一层等于把最容易出错的部分排除在测试之外。

依据: docs/design/02-plugin-api.md § 4、§ 10、§ 11
"""

from __future__ import annotations

import asyncio
import logging
import textwrap
import time
from dataclasses import replace
from pathlib import Path

import pytest

from alterego.kernel.bus import Event, EventBus
from alterego.kernel.clock import Clock
from alterego.kernel.config import Config, PluginsConfig
from alterego.kernel.errors import PluginDependencyError
from alterego.kernel.loader import DiscoveredPlugin
from alterego.kernel.manager import PluginManager
from alterego.kernel.plugin import Plugin, PluginStatus
from alterego.kernel.registry import ServiceRegistry
from alterego.kernel.scheduler import Scheduler


# ── 夹具与工具 ──────────────────────────────────────────────

#: 所有测试插件共用的导入（``entry = "plugin:Demo"`` 指向的类都叫 ``Demo``）。
_BASE_IMPORTS = """\
from alterego.interfaces.common import HealthStatus
from alterego.kernel.plugin import Plugin
"""


def _source(body: str) -> str:
    """把测试里写的插件代码整理成合法的模块源码。"""
    return _BASE_IMPORTS + "\n\n" + textwrap.dedent(body).lstrip("\n")


def make_config(
    tmp_path: Path,
    *,
    search_paths: list[Path] | None = None,
    enabled: list[str] | None = None,
    threshold: int = 5,
    auto_reload_interval: float = 1.0,
) -> Config:
    """构造一个只指向 ``tmp_path`` 的配置。

    显式传一个真实存在的空 TOML 文件，而不是 ``None``：``None`` 会去读工作目录
    下的 ``config/alterego.toml``，那样测试结果就取决于「你从哪个目录跑的 pytest」。
    """
    config_file = tmp_path / "empty.toml"
    config_file.write_text("# 测试用空配置\n", encoding="utf-8")
    paths = [str(p) for p in (search_paths or [tmp_path / "plugins"])]
    return Config.load(
        path=config_file,
        env={},
        overrides={
            "core": {"data_dir": str(tmp_path / "data")},
            "plugins": {
                "enabled": enabled or [],
                "search_paths": paths,
                "circuit_breaker_threshold": threshold,
                "auto_reload_interval_seconds": auto_reload_interval,
            },
        },
    )


def with_plugin_config(config: Config, plugin_id: str, values: dict[str, object]) -> Config:
    """把 ``[plugins.<id>]`` 塞进一个已有配置。

    ``PluginsConfig`` 是冻结 dataclass，所以这里用 ``dataclasses.replace`` 造一个
    新的，而不是去改原对象。
    """
    plugins = PluginsConfig(
        enabled=config.plugins.enabled,
        search_paths=config.plugins.search_paths,
        auto_reload=config.plugins.auto_reload,
        auto_reload_interval_seconds=config.plugins.auto_reload_interval_seconds,
        circuit_breaker_threshold=config.plugins.circuit_breaker_threshold,
        isolate_failures=config.plugins.isolate_failures,
        config={plugin_id: dict(values)},
    )
    return replace(config, plugins=plugins)


def install(
    root: Path,
    plugin_id: str,
    body: str,
    *,
    requires: tuple[str, ...] = (),
    enabled_by_default: bool = True,
    auto_reload: bool = True,
) -> Path:
    """在 ``root`` 下写一个可被发现的本地插件，返回它的目录。"""
    directory = root / plugin_id.split(".", 1)[1]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.py").write_text(_source(body), encoding="utf-8")

    requires_line = ""
    if requires:
        joined = ", ".join(f'"{item}"' for item in requires)
        requires_line = f"requires = [{joined}]\n"
    (directory / "plugin.toml").write_text(
        "[plugin]\n"
        f'id = "{plugin_id}"\n'
        'version = "0.1.0"\n'
        'api_version = "1"\n'
        f'kind = "{plugin_id.split(".", 1)[0]}"\n'
        'entry = "plugin:Demo"\n'
        f"{requires_line}"
        f"enabled_by_default = {str(enabled_by_default).lower()}\n"
        f"auto_reload = {str(auto_reload).lower()}\n",
        encoding="utf-8",
    )
    return directory


def make_manager(
    tmp_path: Path,
    *,
    clock: Clock,
    bus: EventBus,
    registry: ServiceRegistry,
    config: Config | None = None,
    **kwargs: object,
) -> PluginManager:
    return PluginManager(
        config=config if config is not None else make_config(tmp_path),
        bus=bus,
        registry=registry,
        clock=clock,
        scheduler=Scheduler(clock),
        logger=logging.getLogger("test.manager"),
        **kwargs,  # type: ignore[arg-type]
    )


def collect_events(bus: EventBus, pattern: str = "*") -> list[Event]:
    events: list[Event] = []
    bus.subscribe(pattern, events.append, priority=-100)
    return events


class _LogFile:
    """用文件记录插件钩子调用，用来断言「逆序关闭」这类跨插件顺序。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return [line for line in self.path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def log_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _LogFile:
    path = tmp_path / "hooks.log"
    monkeypatch.setenv("ALT_TEST_LOG", str(path))
    return _LogFile(path)


SIMPLE = """
class Demo(Plugin):
    def on_load(self, ctx):
        self.ctx = ctx
        self.events = []

    def on_start(self):
        self.started = True
"""


# ── 发现与选择 ──────────────────────────────────────────────


class TestSelection:
    def test_discover_finds_local_plugins(self, tmp_path, clock, bus, registry):
        root = tmp_path / "plugins"
        install(root, "channel.a", SIMPLE)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        assert pm.discover().ids() == ["channel.a"]

    def test_load_all_discovers_on_its_own(self, tmp_path, clock, bus, registry):
        install(tmp_path / "plugins", "channel.a", SIMPLE)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        assert pm.load_all().loaded == ["channel.a"]

    def test_enabled_by_default_false_is_skipped(self, tmp_path, clock, bus, registry):
        install(tmp_path / "plugins", "channel.a", SIMPLE, enabled_by_default=False)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        assert pm.enabled_ids() == []
        assert pm.load_all().loaded == []

    def test_explicit_enabled_is_a_whitelist(self, tmp_path, clock, bus, registry):
        root = tmp_path / "plugins"
        install(root, "channel.a", SIMPLE)
        install(root, "channel.b", SIMPLE)
        config = make_config(tmp_path, enabled=["channel.b"])
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry, config=config)
        assert pm.enabled_ids() == ["channel.b"]
        assert pm.load_all().loaded == ["channel.b"]

    def test_enabled_plugin_must_exist(self, tmp_path, clock, bus, registry):
        config = make_config(tmp_path, enabled=["channel.ghost"])
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry, config=config)
        with pytest.raises(PluginDependencyError):
            pm.load_all()

    def test_broken_manifest_is_reported_not_raised(self, tmp_path, clock, bus, registry):
        directory = tmp_path / "plugins" / "broken"
        directory.mkdir(parents=True)
        (directory / "plugin.toml").write_text("[plugin]\nid = 'nope'\n", encoding="utf-8")
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        report = pm.load_all()
        assert report.ok is False
        assert len(report.failed) == 1

    def test_dependency_pulls_in_an_otherwise_disabled_plugin(self, tmp_path, clock, bus, registry):
        root = tmp_path / "plugins"
        install(root, "storage.base", SIMPLE, enabled_by_default=False)
        install(root, "capability.user", SIMPLE, requires=("storage.base>=0.1.0",))
        config = make_config(tmp_path, enabled=["capability.user"])
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry, config=config)
        report = pm.load_all()
        assert report.auto_enabled == ["storage.base"]
        assert pm.order == ("storage.base", "capability.user")

    def test_load_order_is_topological_not_alphabetical(self, tmp_path, clock, bus, registry):
        root = tmp_path / "plugins"
        install(root, "storage.zed", SIMPLE, enabled_by_default=False)
        install(root, "channel.aaa", SIMPLE, requires=("storage.zed>=0.1.0",))
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        assert pm.order == ("storage.zed", "channel.aaa")


class TestStatusSteps:
    """``PluginStatus`` 的每一步都要真的被落到 ``_status`` 上。

    三个「设计了却从没被赋值」的成员（``DISCOVERED`` / ``VALIDATED`` / ``STOPPED``）
    是最难发现的一类缺口：枚举、CLI 的状态标签表、``info`` 的输出格式全都写好了，
    只有赋值那一行忘了。后果是 ``alterego plugins info`` 对「没被启用的插件」
    和「根本不存在的插件」给出同一个答案——空。
    """

    def test_discovered_plugins_are_marked_before_they_are_enabled(
        self, tmp_path, clock, bus, registry
    ):
        install(tmp_path / "plugins", "channel.a", SIMPLE, enabled_by_default=False)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.discover()

        assert pm.status_of("channel.a") == PluginStatus.DISCOVERED

    def test_load_walks_validated_then_loaded_then_started(self, tmp_path, clock, bus, registry):
        """在 ``_load_one`` / ``_start_one`` 的边界上读状态，拼出实际走过的顺序。

        ``LOADING`` 在 ``_load_one`` 内部设置、内部清掉，只能从里面看见，
        所以这里断言的是能观察到的三个边界：进 ``_load_one`` 前是 ``VALIDATED``、
        出来是 ``LOADED``、进 ``_start_one`` 时还是 ``LOADED``。
        """
        install(tmp_path / "plugins", "channel.a", SIMPLE)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)

        seen: list[PluginStatus | None] = []
        load_one, start_one = pm._load_one, pm._start_one

        def spy_load(plugin_id: str) -> bool:
            seen.append(pm.status_of(plugin_id))
            result = load_one(plugin_id)
            seen.append(pm.status_of(plugin_id))
            return result

        def spy_start(plugin_id: str) -> None:
            seen.append(pm.status_of(plugin_id))
            start_one(plugin_id)

        pm._load_one = spy_load  # type: ignore[method-assign]
        pm._start_one = spy_start  # type: ignore[method-assign]
        pm.load_all()

        assert seen == [
            PluginStatus.VALIDATED,
            PluginStatus.LOADED,
            PluginStatus.LOADED,
        ]
        assert pm.status_of("channel.a") == PluginStatus.STARTED

    def test_shutdown_marks_stopped_before_unloaded(self, tmp_path, clock, bus, registry):
        install(tmp_path / "plugins", "channel.a", SIMPLE)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()

        seen: list[PluginStatus] = []
        record = pm.plugin_of("channel.a")
        assert record is not None
        original = record.on_unload

        def spy() -> None:
            seen.append(pm.status_of("channel.a"))
            original()

        record.on_unload = spy  # type: ignore[method-assign]
        pm.shutdown()

        # ``on_unload`` 那一刻插件已经停了（STOPPED），但服务与订阅还在。
        # 少了这个中间态，「服务已经摘了而插件以为自己在跑」就无从定位。
        assert seen == [PluginStatus.STOPPED]
        assert pm.status_of("channel.a") == PluginStatus.UNLOADED

    def test_a_broken_plugin_is_discovered_then_failed(self, tmp_path, clock, bus, registry):
        directory = tmp_path / "plugins" / "broken"
        directory.mkdir(parents=True)
        (directory / "plugin.toml").write_text("[plugin]\nid = 'nope'\n", encoding="utf-8")
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        report = pm.load_all()

        assert report.ok is False
        assert pm.status_of(str(directory)) == PluginStatus.FAILED


# ── 加载与隔离 ──────────────────────────────────────────────

RAISES_ON_LOAD = """
class Iface:
    pass


class Demo(Plugin):
    def on_load(self, ctx):
        ctx.registry.register(Iface, self, name="half_dead")
        ctx.bus.subscribe("tick.started", self.on_event)
        raise RuntimeError("加载炸了")

    def on_event(self, event):
        pass
"""


class TestLoading:
    def test_successful_load_publishes_loaded(self, tmp_path, clock, bus, registry):
        install(tmp_path / "plugins", "channel.a", SIMPLE)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        events = collect_events(bus)
        pm.load_all()
        assert [event.topic for event in events] == ["plugin.loaded"]
        assert pm.status_of("channel.a") is PluginStatus.STARTED
        assert pm.plugin_of("channel.a").started is True
        assert pm.failures_of("channel.a") == 0

    def test_context_paths_live_under_data_dir(self, tmp_path, clock, bus, registry):
        plugin_dir = install(tmp_path / "plugins", "channel.a", SIMPLE)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        context = pm.context_of("channel.a")
        assert context is not None
        assert context.paths.data_dir == tmp_path / "data" / "plugins" / "channel.a"
        assert context.paths.plugin_dir == plugin_dir
        assert context.paths.alterego_dir == tmp_path
        assert context.paths.config_dir == tmp_path / "config"
        assert context.paths.data_dir.is_dir()
        assert context.paths.cache_dir.is_dir()

    def test_context_services_forward_to_the_kernel_objects(self, tmp_path, clock, bus, registry):
        """``ctx.bus`` / ``ctx.registry`` 是带归属的**视图**，不是内核对象本身。

        视图存在的理由见 ``plugin.OwnedRegistry``；这里只验证转发是对的。
        """
        body = """
        class Iface:
            pass


        class Demo(Plugin):
            def on_load(self, ctx):
                ctx.registry.register(Iface, self, name="thing")
                ctx.publish("custom.hello", {"n": 1})
        """
        install(tmp_path / "plugins", "channel.a", body)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        events = collect_events(bus)
        pm.load_all()
        assert registry.interfaces() == ["Iface"]
        assert [event.topic for event in events].count("custom.hello") == 1

    def test_context_registration_is_owned_by_the_plugin(self, tmp_path, clock, bus, registry):
        """插件没传 ``owner=``，但归属仍算在它头上（P3：机制优于祈祷）。"""
        body = """
        class Iface:
            pass


        class Demo(Plugin):
            def on_load(self, ctx):
                ctx.registry.register(Iface, self, name="thing")
                ctx.bus.subscribe("tick.started", self.on_event)

            def on_event(self, event):
                pass
        """
        install(tmp_path / "plugins", "channel.a", body)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        before = bus.subscriber_count()
        pm.load_all()
        assert registry.interfaces() == ["Iface"]
        assert bus.subscriber_count() > before
        pm.shutdown()
        assert registry.interfaces() == []
        assert bus.subscriber_count() == before

    def test_failing_on_load_is_isolated_and_reported(self, tmp_path, clock, bus, registry):
        root = tmp_path / "plugins"
        install(root, "channel.good", SIMPLE)
        install(root, "capability.bad", RAISES_ON_LOAD)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        report = pm.load_all()

        assert report.loaded == ["channel.good"]
        assert report.failed == {"capability.bad": str(PluginStatus.FAILED)}
        assert report.ok is False
        assert pm.status_of("capability.bad") is PluginStatus.FAILED

    def test_rollback_removes_registrations_and_subscriptions(self, tmp_path, clock, bus, registry):
        install(tmp_path / "plugins", "capability.bad", RAISES_ON_LOAD)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        before = bus.subscriber_count()
        pm.load_all()
        # 半死插件留下的是悬空引用：指向一个已经失败的对象，比没有更糟。
        assert registry.interfaces() == []
        assert bus.subscriber_count() == before

    def test_failure_is_published_with_the_hook_name(self, tmp_path, clock, bus, registry):
        install(tmp_path / "plugins", "capability.bad", RAISES_ON_LOAD)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        events = collect_events(bus)
        pm.load_all()
        failures = [event for event in events if event.topic == "plugin.failed"]
        assert len(failures) == 1
        assert failures[0].payload["where"] == "on_load"
        assert failures[0].payload["plugin_id"] == "capability.bad"
        assert "加载炸了" in failures[0].payload["error"]

    def test_missing_import_is_reported_as_a_load_failure(self, tmp_path, clock, bus, registry):
        directory = tmp_path / "plugins" / "bad"
        directory.mkdir(parents=True)
        (directory / "plugin.py").write_text(
            "import a_module_that_does_not_exist\n", encoding="utf-8"
        )
        (directory / "plugin.toml").write_text(
            "[plugin]\n"
            'id = "channel.bad"\n'
            'version = "0.1.0"\n'
            'api_version = "1"\n'
            'kind = "channel"\n'
            'entry = "plugin:Demo"\n',
            encoding="utf-8",
        )
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        assert pm.load_all().failed == {"channel.bad": str(PluginStatus.FAILED)}

    def test_isolate_failures_false_lets_the_exception_escape(self, tmp_path, clock, bus, registry):
        install(tmp_path / "plugins", "capability.bad", RAISES_ON_LOAD)
        config = make_config(tmp_path)
        object.__setattr__(config.plugins, "isolate_failures", False)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry, config=config)
        with pytest.raises(RuntimeError, match="加载炸了"):
            pm.load_all()


RAISES_ON_START = """
def _append(text):
    import os

    with open(os.environ["ALT_TEST_LOG"], "a", encoding="utf-8") as handle:
        handle.write(text + "\\n")


class Demo(Plugin):
    def on_start(self):
        raise RuntimeError("启动炸了")

    def on_stop(self):
        _append("stop")
"""


class TestStarting:
    def test_on_start_failure_still_cleans_up(self, tmp_path, clock, bus, registry, log_file):
        install(tmp_path / "plugins", "channel.a", RAISES_ON_START)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        report = pm.load_all()
        assert report.loaded == []
        assert pm.status_of("channel.a") is PluginStatus.FAILED
        # 启动失败也要尽力收拾：后台任务可能已经跑起来了。
        assert log_file.lines() == ["stop"]

    def test_event_hook_is_armed_after_start(self, tmp_path, clock, bus, registry):
        body = (
            SIMPLE + "\n    def on_event(self, event):\n        self.events.append(event.topic)\n"
        )
        install(tmp_path / "plugins", "channel.a", body)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        bus.emit("tick.started", {}, source="test")
        assert pm.plugin_of("channel.a").events == ["tick.started"]


# ── 运行时钩子 ──────────────────────────────────────────────

RAISES_ON_TICK = """
class Demo(Plugin):
    def on_tick_pre(self, ctx):
        raise RuntimeError("tick 炸了")
"""


class TestRuntimeHooks:
    def test_tick_hooks_reach_every_started_plugin(self, tmp_path, clock, bus, registry):
        body = SIMPLE + (
            "\n    def on_tick_pre(self, ctx):\n"
            "        self.events.append('pre')\n"
            "\n    def on_tick_post(self, ctx):\n"
            "        self.events.append('post')\n"
        )
        install(tmp_path / "plugins", "channel.a", body)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        pm.tick_pre(object())
        pm.tick_post(object())
        assert pm.plugin_of("channel.a").events == ["pre", "post"]

    def test_tick_hook_exception_becomes_a_failure_not_a_crash(
        self, tmp_path, clock, bus, registry
    ):
        root = tmp_path / "plugins"
        install(root, "capability.bad", RAISES_ON_TICK)
        install(root, "channel.a", SIMPLE)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        pm.tick_pre(object())
        assert pm.failures_of("capability.bad") == 1
        assert pm.is_circuit_open("capability.bad") is False
        # 一个插件抛异常不该让别的插件收不到 tick。
        assert pm.status_of("channel.a") is PluginStatus.STARTED

    def test_flush_state_writes_to_the_sink(self, tmp_path, clock, bus, registry):
        body = """
        class Demo(Plugin):
            def on_load(self, ctx):
                self.ctx = ctx
                ctx.state.set("counter", 1)

            def on_start(self):
                self.ctx.state.set("counter", 2)
        """
        written: list[tuple[str, dict[str, object]]] = []
        install(tmp_path / "plugins", "channel.a", body)
        pm = make_manager(
            tmp_path,
            clock=clock,
            bus=bus,
            registry=registry,
            state_sink=lambda pid, data: written.append((pid, dict(data))),
        )
        pm.load_all()
        pm.flush_state()
        # 只有变化的键会被交出去，不是一个全量快照。
        assert written == [("channel.a", {"counter": 2})]

    def test_state_loader_is_used_as_the_initial_value(self, tmp_path, clock, bus, registry):
        body = """
        class Demo(Plugin):
            def on_load(self, ctx):
                self.counter = ctx.state.get("counter")
        """
        install(tmp_path / "plugins", "channel.a", body)
        pm = make_manager(
            tmp_path,
            clock=clock,
            bus=bus,
            registry=registry,
            state_loader=lambda pid: {"counter": 7},
        )
        pm.load_all()
        assert pm.plugin_of("channel.a").counter == 7


# ── 熔断 ────────────────────────────────────────────────────


class TestCircuitBreaker:
    def test_opens_at_the_threshold(self, tmp_path, clock, bus, registry):
        install(tmp_path / "plugins", "channel.a", SIMPLE)
        config = make_config(tmp_path, threshold=2)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry, config=config)
        pm.load_all()
        events = collect_events(bus)

        pm.record_failure("channel.a", RuntimeError("一"), where="send")
        assert pm.is_circuit_open("channel.a") is False
        pm.record_failure("channel.a", RuntimeError("二"), where="send")

        assert pm.is_circuit_open("channel.a") is True
        assert pm.circuit_open() == ("channel.a",)
        opened = [event for event in events if event.topic == "plugin.circuit_opened"]
        assert len(opened) == 1
        assert opened[0].payload == {"plugin_id": "channel.a", "failures": 2}

    def test_does_not_publish_the_same_event_twice(self, tmp_path, clock, bus, registry):
        install(tmp_path / "plugins", "channel.a", SIMPLE)
        config = make_config(tmp_path, threshold=1)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry, config=config)
        pm.load_all()
        events = collect_events(bus)
        for _ in range(3):
            pm.record_failure("channel.a", RuntimeError("x"), where="send")
        assert len([e for e in events if e.topic == "plugin.circuit_opened"]) == 1

    def test_reset_clears_the_flag_and_the_counter(self, tmp_path, clock, bus, registry):
        install(tmp_path / "plugins", "channel.a", SIMPLE)
        config = make_config(tmp_path, threshold=1)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry, config=config)
        pm.load_all()
        pm.record_failure("channel.a", RuntimeError("x"), where="send")
        assert pm.reset("channel.a") is True
        assert pm.reset("channel.a") is False
        assert pm.is_circuit_open("channel.a") is False
        assert pm.failures_of("channel.a") == 0

    def test_open_circuit_stops_tick_delivery(self, tmp_path, clock, bus, registry):
        body = SIMPLE + "\n    def on_tick_pre(self, ctx):\n        self.events.append('pre')\n"
        install(tmp_path / "plugins", "channel.a", body)
        config = make_config(tmp_path, threshold=1)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry, config=config)
        pm.load_all()
        pm.record_failure("channel.a", RuntimeError("x"), where="send")
        pm.tick_pre(object())
        assert pm.plugin_of("channel.a").events == []

    def test_failure_of_an_unknown_plugin_still_counts(self, tmp_path, clock, bus, registry):
        config = make_config(tmp_path, threshold=1)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry, config=config)
        pm.record_failure("channel.ghost", RuntimeError("x"), where="send")
        assert pm.circuit_open() == ("channel.ghost",)


class TestHealth:
    def test_health_all_collects_every_plugin(self, tmp_path, clock, bus, registry):
        body = SIMPLE + (
            "\n    def health(self):\n        return HealthStatus(ok=True, detail='一切正常')\n"
        )
        install(tmp_path / "plugins", "channel.a", body)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        assert pm.health_all()["channel.a"].detail == "一切正常"

    def test_health_all_reports_a_broken_hook(self, tmp_path, clock, bus, registry):
        body = SIMPLE + "\n    def health(self):\n        raise RuntimeError('体检炸了')\n"
        install(tmp_path / "plugins", "channel.a", body)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        status = pm.health_all()["channel.a"]
        assert status.ok is False
        assert "体检炸了" in status.detail

    def test_health_all_reports_the_circuit_and_a_way_out(self, tmp_path, clock, bus, registry):
        install(tmp_path / "plugins", "channel.a", SIMPLE)
        config = make_config(tmp_path, threshold=1)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry, config=config)
        pm.load_all()
        pm.record_failure("channel.a", RuntimeError("x"), where="send")
        status = pm.health_all()["channel.a"]
        assert status.ok is False
        assert status.detail == "已熔断"
        assert status.hint is not None
        assert "reset channel.a" in status.hint

    def test_missing_required_capabilities_names_what_is_missing(
        self, tmp_path, clock, bus, registry
    ):
        class FakeLLM:
            pass

        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        required = {FakeLLM: "LLM 供应商插件（llm.*）"}
        assert pm.missing_required_capabilities(required) == ["LLM 供应商插件（llm.*）"]

        registry.register(FakeLLM, object(), name="fake")
        assert pm.missing_required_capabilities(required) == []


# ── 关闭 ────────────────────────────────────────────────────

RECORDER = """
def _append(text):
    import os

    with open(os.environ["ALT_TEST_LOG"], "a", encoding="utf-8") as handle:
        handle.write(text + "\\n")


class Demo(Plugin):
    def on_start(self):
        _append(self.__class__.__module__ + ":start")

    def on_stop(self):
        _append(self.__class__.__module__ + ":stop")

    def on_unload(self):
        _append(self.__class__.__module__ + ":unload")
"""


def _short_name(line: str) -> tuple[str, str]:
    """把 ``alterego_plugins.storage.base.plugin:start`` 拆成 ``("base", "start")``。"""
    module, hook = line.rsplit(":", 1)
    return module.rsplit(".", 2)[-2], hook


class TestShutdown:
    def test_shutdown_runs_in_reverse_order(self, tmp_path, clock, bus, registry, log_file):
        root = tmp_path / "plugins"
        install(root, "storage.base", RECORDER, enabled_by_default=False)
        install(root, "capability.mid", RECORDER, requires=("storage.base>=0.1.0",))
        install(root, "channel.top", RECORDER, requires=("capability.mid>=0.1.0",))
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        pm.shutdown()

        assert [_short_name(line) for line in log_file.lines()] == [
            ("base", "start"),
            ("mid", "start"),
            ("top", "start"),
            ("top", "stop"),
            ("top", "unload"),
            ("mid", "stop"),
            ("mid", "unload"),
            ("base", "stop"),
            ("base", "unload"),
        ]

    def test_shutdown_is_idempotent(self, tmp_path, clock, bus, registry, log_file):
        install(tmp_path / "plugins", "channel.a", RECORDER)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        pm.shutdown()
        after_first = list(log_file.lines())
        pm.shutdown()
        assert log_file.lines() == after_first
        assert pm.status_of("channel.a") is PluginStatus.UNLOADED
        assert pm.plugins == {}

    def test_shutdown_unregisters_and_unsubscribes(self, tmp_path, clock, bus, registry):
        body = """
        class Iface:
            pass


        class Demo(Plugin):
            def on_load(self, ctx):
                ctx.registry.register(Iface, self, name="thing")
                ctx.bus.subscribe("tick.started", self.on_event)

            def on_event(self, event):
                pass
        """
        install(tmp_path / "plugins", "channel.a", body)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        before = bus.subscriber_count()
        pm.load_all()
        assert registry.interfaces() == ["Iface"]
        pm.shutdown()
        assert registry.interfaces() == []
        # ``on_event`` 的 "*" 订阅也会被摘掉。
        assert bus.subscriber_count() == before

    def test_on_stop_exception_does_not_break_teardown(
        self, tmp_path, clock, bus, registry, log_file
    ):
        body = """
        class Demo(Plugin):
            def on_stop(self):
                raise RuntimeError("停机炸了")

            def on_unload(self):
                import os

                with open(os.environ["ALT_TEST_LOG"], "a", encoding="utf-8") as handle:
                    handle.write("unload\\n")
        """
        install(tmp_path / "plugins", "channel.a", body)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        pm.shutdown()
        assert log_file.lines() == ["unload"]
        assert pm.status_of("channel.a") is PluginStatus.UNLOADED


# ── 热重载 ──────────────────────────────────────────────────


def _v2_source(marker: str) -> str:
    """比第一版**更长**的源码，确保字节数变化（避免 importlib 源码缓存命中）。"""
    body = SIMPLE + (
        "\n    def on_load(self, ctx):\n"
        f"        self.version = {marker!r}\n"
        "        self.ctx = ctx\n"
        "        self.events = []\n"
        "        self.extra = 1  # 让文件长度变化，绕开源码缓存\n"
    )
    return _source(body)


class TestReload:
    def test_reload_picks_up_new_code(self, tmp_path, clock, bus, registry):
        directory = install(tmp_path / "plugins", "channel.a", SIMPLE)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        first = pm.plugin_of("channel.a")

        (directory / "plugin.py").write_text(_v2_source("v2"), encoding="utf-8")
        assert pm.reload("channel.a") is True
        second = pm.plugin_of("channel.a")
        assert second is not first
        assert second.version == "v2"
        assert pm.status_of("channel.a") is PluginStatus.STARTED

    def test_reload_publishes_both_events(self, tmp_path, clock, bus, registry):
        directory = install(tmp_path / "plugins", "channel.a", SIMPLE)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        events = collect_events(bus)
        (directory / "plugin.py").write_text(_v2_source("v2"), encoding="utf-8")
        pm.reload("channel.a")
        topics = [event.topic for event in events if event.topic.startswith("plugin.")]
        assert topics == ["plugin.reloading", "plugin.loaded", "plugin.reloaded"]
        assert events[-1].payload == {"plugin_id": "channel.a", "ok": True}

    def test_reload_tears_down_before_reloading(self, tmp_path, clock, bus, registry, log_file):
        directory = install(tmp_path / "plugins", "channel.a", RECORDER)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        (directory / "plugin.py").write_text(
            _source(RECORDER) + "\n# 一个注释，用来改变文件长度\n", encoding="utf-8"
        )
        pm.reload("channel.a")
        assert [line.rsplit(":", 1)[1] for line in log_file.lines()] == [
            "start",
            "stop",
            "unload",
            "start",
        ]

    def test_reload_failure_does_not_restore_the_old_instance(self, tmp_path, clock, bus, registry):
        directory = install(tmp_path / "plugins", "channel.a", SIMPLE)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        events = collect_events(bus)

        (directory / "plugin.py").write_text("def broken(:\n", encoding="utf-8")
        assert pm.reload("channel.a") is False

        # 旧实例可能已经处于不一致状态，请回来只会让问题更难查（§ 10.3）。
        assert pm.plugin_of("channel.a") is None
        assert pm.status_of("channel.a") is PluginStatus.FAILED
        assert events[-1].topic == "plugin.reloaded"
        assert events[-1].payload["ok"] is False

    def test_reload_of_an_unknown_plugin_is_refused(self, tmp_path, clock, bus, registry):
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        assert pm.reload("channel.ghost") is False

    def test_reload_of_an_installed_package_is_refused(self, tmp_path, clock, bus, registry):
        install(tmp_path / "plugins", "channel.a", SIMPLE)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        found = pm.discovery.plugins["channel.a"]
        pm.discovery.plugins["channel.a"] = DiscoveredPlugin(
            manifest=found.manifest,
            path=found.path,
            entry_point=object(),  # type: ignore[arg-type]
        )
        assert pm.reload("channel.a") is False

    def test_changed_plugins_notices_a_new_file(self, tmp_path, clock, bus, registry):
        directory = install(tmp_path / "plugins", "channel.a", SIMPLE)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        assert pm.changed_plugins() == []
        (directory / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
        assert pm.changed_plugins() == ["channel.a"]

    def test_changed_plugins_ignores_other_suffixes(self, tmp_path, clock, bus, registry):
        directory = install(tmp_path / "plugins", "channel.a", SIMPLE)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        (directory / "notes.txt").write_text("随手记\n", encoding="utf-8")
        assert pm.changed_plugins() == []

    def test_changed_plugins_honours_auto_reload_false(self, tmp_path, clock, bus, registry):
        directory = install(tmp_path / "plugins", "channel.a", SIMPLE, auto_reload=False)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        (directory / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
        assert pm.changed_plugins() == []

    def test_broken_plugin_is_watched_too(self, tmp_path, clock, bus, registry):
        """加载失败的插件也要开始监视，否则用户修好了还得重启一次。"""
        directory = tmp_path / "plugins" / "a"
        directory.mkdir(parents=True)
        (directory / "plugin.py").write_text("def broken(:\n", encoding="utf-8")
        (directory / "plugin.toml").write_text(
            "[plugin]\n"
            'id = "channel.a"\n'
            'version = "0.1.0"\n'
            'api_version = "1"\n'
            'kind = "channel"\n'
            'entry = "plugin:Demo"\n',
            encoding="utf-8",
        )
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        assert pm.status_of("channel.a") is PluginStatus.FAILED
        (directory / "plugin.py").write_text("VALUE = 1\n", encoding="utf-8")
        assert pm.changed_plugins() == ["channel.a"]

    async def test_watch_forever_reloads_on_change(self, tmp_path, clock, bus, registry):
        directory = install(tmp_path / "plugins", "channel.a", SIMPLE)
        config = make_config(tmp_path, auto_reload_interval=0.01)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry, config=config)
        pm.load_all()

        task = asyncio.create_task(pm.watch_forever())
        try:
            (directory / "plugin.py").write_text(_v2_source("v2"), encoding="utf-8")
            # 新文件一定能被 mtime 快照察觉；改已有文件的 mtime 可能落在同一刻。
            (directory / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                plugin = pm.plugin_of("channel.a")
                if getattr(plugin, "version", None) == "v2":
                    break
                await asyncio.sleep(0.02)
            assert getattr(pm.plugin_of("channel.a"), "version", None) == "v2"
        finally:
            pm.stop_watching()
            await asyncio.wait_for(task, timeout=5.0)


# ── 查询与契约 ──────────────────────────────────────────────


class TestQueries:
    def test_plugins_mapping_is_a_snapshot(self, tmp_path, clock, bus, registry):
        install(tmp_path / "plugins", "channel.a", SIMPLE)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        snapshot = pm.plugins
        snapshot.clear()
        assert pm.plugin_of("channel.a") is not None

    def test_queries_are_safe_for_unknown_ids(self, tmp_path, clock, bus, registry):
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        assert pm.status_of("channel.ghost") is None
        assert pm.context_of("channel.ghost") is None
        assert pm.plugin_of("channel.ghost") is None
        assert pm.failures_of("channel.ghost") == 0
        assert pm.order == ()
        assert hasattr(pm.reload_lock, "acquire")
        assert pm.plugins == {}

    def test_a_class_that_is_not_a_plugin_is_rejected(self, tmp_path, clock, bus, registry):
        directory = tmp_path / "plugins" / "a"
        directory.mkdir(parents=True)
        (directory / "plugin.py").write_text("class Demo:\n    pass\n", encoding="utf-8")
        (directory / "plugin.toml").write_text(
            "[plugin]\n"
            'id = "channel.a"\n'
            'version = "0.1.0"\n'
            'api_version = "1"\n'
            'kind = "channel"\n'
            'entry = "plugin:Demo"\n',
            encoding="utf-8",
        )
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        assert pm.load_all().failed == {"channel.a": str(PluginStatus.FAILED)}

    def test_manifest_is_attached_to_the_instance(self, tmp_path, clock, bus, registry):
        install(tmp_path / "plugins", "channel.a", SIMPLE)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        plugin = pm.plugin_of("channel.a")
        assert isinstance(plugin, Plugin)
        assert plugin.manifest is not None
        assert plugin.id == "channel.a"


class TestEventHook:
    def test_on_event_sees_every_topic(self, tmp_path, clock, bus, registry):
        body = (
            SIMPLE + "\n    def on_event(self, event):\n        self.events.append(event.topic)\n"
        )
        install(tmp_path / "plugins", "channel.a", body)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        bus.emit("emotion.changed", {"valence": 0.5}, source="test")
        bus.emit("post.created", {"post_id": "x"}, source="test")
        assert pm.plugin_of("channel.a").events == ["emotion.changed", "post.created"]

    def test_events_after_shutdown_do_not_reach_the_plugin(self, tmp_path, clock, bus, registry):
        body = (
            SIMPLE + "\n    def on_event(self, event):\n        self.events.append(event.topic)\n"
        )
        install(tmp_path / "plugins", "channel.a", body)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        pm.load_all()
        plugin = pm.plugin_of("channel.a")
        pm.shutdown()
        bus.emit("tick.started", {}, source="test")
        assert plugin.events == []


class TestPluginConfig:
    def _install_configurable(self, tmp_path: Path) -> None:
        directory = tmp_path / "plugins" / "a"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "plugin.toml").write_text(
            "[plugin]\n"
            'id = "channel.a"\n'
            'version = "0.1.0"\n'
            'api_version = "1"\n'
            'kind = "channel"\n'
            'entry = "plugin:Demo"\n'
            "\n"
            "[plugin.config]\n"
            "endpoint = {type = 'string', default = 'http://localhost', description = '地址'}\n",
            encoding="utf-8",
        )
        (directory / "plugin.py").write_text(
            _source(
                """
                class Demo(Plugin):
                    def on_load(self, ctx):
                        self.endpoint = ctx.config["endpoint"]
                """
            ),
            encoding="utf-8",
        )

    def test_user_config_overrides_the_manifest_default(self, tmp_path, clock, bus, registry):
        self._install_configurable(tmp_path)
        config = with_plugin_config(make_config(tmp_path), "channel.a", {"endpoint": "http://x"})
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry, config=config)
        assert pm.load_all().loaded == ["channel.a"]
        assert pm.plugin_of("channel.a").endpoint == "http://x"

    def test_manifest_default_is_used_when_config_is_silent(self, tmp_path, clock, bus, registry):
        self._install_configurable(tmp_path)
        pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)
        assert pm.load_all().loaded == ["channel.a"]
        assert pm.plugin_of("channel.a").endpoint == "http://localhost"

"""端到端验收：``plugins/desktop_window`` 必须真的能被加载，并且**在正确的时候**才武装。

和 ``test_study_plugin.py`` / ``test_dataset_exporter_plugin.py`` 走的是**同一条
真实路径**：清单校验 → 依赖解析 → 导入 → 注册 → 描述 → 卸载。

这里最要紧的一条是「**加载 ≠ 武装**」：

``alterego plugins doctor`` 与 ``alterego serve`` 都会走一遍
``manager.load_all()``。如果占全局快捷键这件事写在 ``on_load`` / ``on_start``
里，那么用户只是想看一眼插件健不健康，键盘上的 ``ctrl+alt+a`` 就被拿走了——
而 ``doctor`` 打印的是一切正常。所以武装挂在 ``cli_serve`` 广播的
``serve.listening`` 上，这个文件有一半在测那条线：

- ``load_all()`` 之后 ``armed`` 必须是 ``False``，``health()`` 必须是 ``ok``；
- 收到别的主题、或者载荷里没有地址，仍然不武装；
- 收到 ``serve.listening`` 才武装，并且**记住那个地址**。

另一半是纯逻辑（快捷键与位置的解析），它们**不碰键盘、不碰窗口、不看平台**，
所以在一台没有桌面的机器上也是真跑的——操作系统那一层被整体换成了假货
（``_FakeOS``）。真实调用 ``RegisterHotKey`` 的代码不在这里测：那段只能在
一台有交互桌面的 Windows 上手工验，假装测过比不测更坏。

依据: docs/guide/plugin-development.md § 3、§ 5；plugins/desktop_window/README.md
"""

from __future__ import annotations

import ast
import dataclasses
import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from alterego import cli_serve
from alterego.interfaces.common import HealthStatus
from alterego.interfaces.simulation import Capability
from alterego.kernel.bus import Event, EventBus
from alterego.kernel.clock import FrozenClock
from alterego.kernel.config import Config
from alterego.kernel.loader import discover, import_plugin_class, module_name_for
from alterego.kernel.manager import PluginManager
from alterego.kernel.registry import ServiceRegistry
from alterego.kernel.scheduler import Scheduler


PLUGIN_ID = "capability.desktop_window"
SERVICE_NAME = "desktop_window"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugins"
URL = "http://127.0.0.1:8765/"


# ────────────────────────────────────────────────────────────
# 假的「操作系统」
# ────────────────────────────────────────────────────────────


class _FakeListener:
    """假的快捷键监听器：只把回调记下来，一个键都不占。"""

    def __init__(self, hotkey: Any, on_press: Callable[[], None], ok: bool = True) -> None:
        self.hotkey = hotkey
        self.on_press = on_press
        self.error = "" if ok else f"Windows 没有把 {hotkey.text} 给出来（多半已被别的程序占用）"
        self.starts = 0
        self.stops = 0
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        self.starts += 1
        self._running = not self.error
        return self._running

    def stop(self) -> None:
        self.stops += 1
        self._running = False

    def press(self) -> None:
        """模拟用户按了一下。"""
        self.on_press()


class _FakeWindow:
    """假的窗口：它属于别的进程，而我们在这里把它变成一个记账本。"""

    def __init__(
        self,
        *,
        browser: Any,
        url: str,
        size: tuple[int, int],
        position: tuple[int, int] | None,
        topmost: bool,
        profile_dir: Path,
        logger: Any,
        launch_ok: bool = True,
    ) -> None:
        self.browser = browser
        self.url = url
        self.size = size
        self.position = position
        self.topmost = topmost
        self.profile_dir = profile_dir
        self.launches = 0
        self.closes = 0
        self.resizes: list[tuple[int, int]] = []
        self.topmost_set: list[bool] = []
        self._launch_ok = launch_ok
        self._alive = False
        self._shown = False

    def launch(self, *, timeout: float = 0.0) -> bool:
        self.launches += 1
        if self._launch_ok:
            self._alive, self._shown = True, True
        return self._launch_ok

    def alive(self) -> bool:
        return self._alive

    def shown(self) -> bool:
        return self._shown

    def show(self) -> None:
        self._shown = True

    def hide(self) -> None:
        self._shown = False

    def set_topmost(self, topmost: bool) -> None:
        self.topmost = topmost
        self.topmost_set.append(topmost)

    def resize(self, size: tuple[int, int]) -> None:
        self.size = size
        self.resizes.append(size)

    def close(self) -> None:
        self.closes += 1
        self._alive, self._shown = False, False

    def user_closed_it(self) -> None:
        """用户点了右上角的叉：那个窗口是别的进程的，我们拦不住。"""
        self._alive, self._shown = False, False


class _FakeOS:
    """把插件的 ``win32`` 那一层整体换掉。

    换的是**模块属性**，不是往生产代码里插一个工厂：``plugin.py`` 里写的是
    ``win32.HotkeyListener(...)``，而 ``win32`` 就是「操作系统」这条边界本身。
    在这里换掉它，和换掉一个 ``time.now`` 是一回事，不需要为了测试给生产代码
    多加一层间接。
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        module: Any,
        *,
        windows: bool = True,
        browser: bool = True,
        hotkey_ok: bool = True,
        launch_ok: bool = True,
    ) -> None:
        self.listeners: list[_FakeListener] = []
        self.windows: list[_FakeWindow] = []
        self._browser_found = browser
        self._hotkey_ok = hotkey_ok
        self._launch_ok = launch_ok
        monkeypatch.setattr(module, "IS_WINDOWS", windows)
        monkeypatch.setattr(module, "find_browser", self._find_browser)
        monkeypatch.setattr(module, "HotkeyListener", self._make_listener)
        monkeypatch.setattr(module, "AppWindow", self._make_window)

    def _find_browser(self, configured: str = "") -> Path | None:
        return Path("msedge.exe") if self._browser_found else None

    def _make_listener(self, hotkey: Any, on_press: Callable[[], None], logger: Any) -> Any:
        listener = _FakeListener(hotkey, on_press, ok=self._hotkey_ok)
        self.listeners.append(listener)
        return listener

    def _make_window(self, **kwargs: Any) -> Any:
        window = _FakeWindow(launch_ok=self._launch_ok, **kwargs)
        self.windows.append(window)
        return window

    def press(self) -> None:
        """按一下当前那个人占住的快捷键。"""
        assert self.listeners, "还没有任何监听器，按不到"
        self.listeners[-1].press()

    @property
    def window(self) -> _FakeWindow:
        assert self.windows, "还没有开过窗口"
        return self.windows[-1]


# ────────────────────────────────────────────────────────────
# 装配
# ────────────────────────────────────────────────────────────


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
    """加载这个插件，测完保证卸载（免得污染 ``sys.modules``）。"""
    bus = EventBus(clock, logger=logging.getLogger("test.desktop.bus"))
    registry = ServiceRegistry(logger=logging.getLogger("test.desktop.registry"))
    manager = PluginManager(
        config=make_config(tmp_path, plugin_config=plugin_config),
        bus=bus,
        registry=registry,
        clock=clock,
        scheduler=Scheduler(clock, logger=logging.getLogger("test.desktop.sched")),
        logger=logging.getLogger("test.desktop.manager"),
    )
    report = manager.load_all()
    assert report.ok, f"桌面窗口插件加载失败：{report.failed}"
    try:
        yield registry, bus, manager
    finally:
        manager.shutdown()


def load_failure(
    tmp_path: Path, clock: FrozenClock, *, plugin_config: dict[str, Any]
) -> dict[str, Any]:
    """加载一次（预期失败），把 ``plugin.failed`` 那条事件还回来。

    失败**不会**从 ``load_all()`` 里抛出来：``plugins.isolate_failures`` 默认
    是开的，一个坏插件不该让整台机器起不来。原因记在 ``plugin.failed`` 事件里
    （``report.failed`` 只放状态名），所以要断言的信息得从总线上取。
    """
    failures: list[dict[str, Any]] = []
    bus = EventBus(clock, logger=logging.getLogger("test.desktop.fail.bus"))
    bus.subscribe("plugin.failed", lambda event: failures.append(dict(event.payload)))
    manager = PluginManager(
        config=make_config(tmp_path, plugin_config=plugin_config),
        bus=bus,
        registry=ServiceRegistry(logger=logging.getLogger("test.desktop.fail.registry")),
        clock=clock,
        scheduler=Scheduler(clock, logger=logging.getLogger("test.desktop.fail.sched")),
        logger=logging.getLogger("test.desktop.fail.manager"),
    )
    try:
        report = manager.load_all()
        assert report.ok is False, "这次加载本该失败，却成功了"
    finally:
        manager.shutdown()
    assert failures, "插件失败了，但一条 plugin.failed 都没有"
    return failures[0]


@contextmanager
def loaded(
    tmp_path: Path,
    clock: FrozenClock,
    *,
    plugin_config: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """加载好的插件实例。"""
    with load_plugin(tmp_path, clock, plugin_config=plugin_config) as (registry, _bus, _manager):
        yield registry.get(Capability, name=SERVICE_NAME)


def discovered() -> Any:
    """在真实的 ``plugins/`` 目录里找到这个插件。"""
    result = discover(search_paths=[str(PLUGIN_ROOT)], entry_point_group="alterego.test.none")
    found = result.get(PLUGIN_ID)
    assert found is not None, f"没找到 {PLUGIN_ID}；找到的是 {result.ids()}"
    return found


def plugin_module() -> Any:
    """这个插件的 ``plugin.py`` **模块对象**（不是那个文件）。

    要拿它是因为有几件事只有模块级才看得到：``_Settings`` 的默认值、
    ``SERVE_LISTENING`` 这个字面量。走 :func:`import_plugin_class` 而不是
    自己 ``spec_from_file_location``：那样绕开了加载器，测的就不是真实路径了。
    """
    cls = import_plugin_class(discovered())
    return sys.modules[cls.__module__]


@pytest.fixture(autouse=True)
def _evict_after() -> Iterator[None]:
    """测完把插件模块从 ``sys.modules`` 里清掉（同加载器热重载时的做法）。"""
    yield
    prefix = module_name_for(PLUGIN_ID)
    for key in [name for name in sys.modules if name == prefix or name.startswith(f"{prefix}.")]:
        del sys.modules[key]


@pytest.fixture
def os_layer(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _FakeOS]:
    """造一层假的「操作系统」，装到**这个插件实例真正在用的**那个模块上。

    参数是插件实例而不是模块：同一个 ``plugin.py`` 可能被导入过不止一次
    （加载器每次都会先清掉再导），而 ``monkeypatch`` 只作用在传进来的那个
    模块对象上。拿错了一个，症状是「假货装好了，插件却还在调真的」。
    """

    def build(plugin: Any, **options: Any) -> _FakeOS:
        return _FakeOS(monkeypatch, sys.modules[type(plugin).__module__].win32, **options)

    return build


def listening_event(clock: FrozenClock, *, payload: dict[str, Any] | None = None) -> Event:
    """``cli_serve`` 会广播的那条事件。"""
    return Event.create(
        cli_serve.SERVE_LISTENING,
        {"url": URL, "port": 8765} if payload is None else payload,
        clock=clock,
    )


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

    def test_it_is_not_enabled_by_default(self) -> None:
        """它会占一个**全局**快捷键，所以绝不能默默在别人的实例里跑起来。"""
        assert discovered().manifest.enabled_by_default is False

    def test_every_declared_field_is_actually_read(self) -> None:
        """声明的每一个字段都得有人读。

        声明了却不读等于**对用户撒谎**：``alterego plugins config`` 会把它
        列出来、``templates`` 里会有它的说明，而改它什么都不会发生。
        反过来，代码里读了而清单里没声明，则用户改不到它。
        两个方向都得靠这条：
        """
        declared = set(discovered().manifest.config)
        consumed = {f.name for f in dataclasses.fields(plugin_module()._Settings)}

        assert declared == consumed

    def test_it_does_not_declare_what_the_server_owns(self) -> None:
        """地址、端口、认证方式住在 ``[web]`` 里，不许在这里抄一份。

        ``docs/guide/plugin-development.md`` § 2.4.1：内核或某条命令已经读的值，
        插件只是「知道有这回事」，搬过来就是一份设置的两个真源——
        而 ``alterego serve --port 9000`` 的那天，插件会去开一个连不上的地址。
        """
        declared = set(discovered().manifest.config)

        assert declared & {"port", "url", "host", "auth", "auth_password"} == set()


def test_the_declared_defaults_are_the_ones_the_code_uses() -> None:
    """清单里的 default 与代码里的默认值必须逐字一致。

    同 ``tests/test_settings_metadata.py`` 守着的那条：两份默认值一旦不一致，
    ``alterego plugins config`` 报的那个数就不是真正生效的那个数。
    """
    fields = discovered().manifest.config
    settings = plugin_module()._Settings.from_config({})

    for name, field in sorted(fields.items()):
        assert getattr(settings, name) == field.default, name


# ────────────────────────────────────────────────────────────
# 快捷键与位置的解析（纯函数）
# ────────────────────────────────────────────────────────────


class TestHotkeyParsing:
    def test_it_parses_the_default_shape(self) -> None:
        win32 = plugin_module().win32

        hotkey = win32.parse_hotkey("ctrl+alt+a")

        assert hotkey.vk == 0x41
        assert hotkey.text == "ctrl+alt+a"

    def test_the_written_order_does_not_matter(self) -> None:
        """``alt+ctrl+A`` 与 ``ctrl+alt+a`` 是同一个快捷键。

        日志与 ``describe()`` 里必须长得一样，否则「我怎么改都不生效」
        会变成一个查不出来的问题。
        """
        win32 = plugin_module().win32

        assert win32.parse_hotkey("alt+ctrl+A") == win32.parse_hotkey("ctrl+alt+a")
        assert win32.parse_hotkey("alt+ctrl+A").text == "ctrl+alt+a"

    @pytest.mark.parametrize("written", ["cmd+e", "super+e", "meta+e", "win+e"])
    def test_the_modifier_aliases_agree(self, written: str) -> None:
        win32 = plugin_module().win32

        assert win32.parse_hotkey(written).text == "win+e"

    def test_holding_it_down_does_not_repeat(self) -> None:
        """少了 ``MOD_NOREPEAT``，长按一次会把窗口开关十几次。"""
        win32 = plugin_module().win32

        assert win32.parse_hotkey("ctrl+alt+a").modifiers & win32._MOD_NOREPEAT

    @pytest.mark.parametrize(
        "written",
        ["ctrl+f12", "ctrl+space", "ctrl+pageup", "shift+delete", "ctrl+7", "ctrl+escape"],
    )
    def test_it_accepts_the_documented_keys(self, written: str) -> None:
        win32 = plugin_module().win32

        assert win32.parse_hotkey(written).text == written

    @pytest.mark.parametrize(
        "written",
        [
            "",
            "   ",
            "a",
            "ctrl+",
            "+a",
            "ctrl++a",
            "ctrl+ctrl+a",
            "hyper+a",
            "ctrl+ä",
            "ctrl+f25",
        ],
    )
    def test_it_rejects_what_it_cannot_use(self, written: str) -> None:
        win32 = plugin_module().win32

        with pytest.raises(win32.HotkeyError):
            win32.parse_hotkey(written)

    def test_a_bare_key_is_refused_with_a_reason_the_user_can_act_on(self) -> None:
        """一个裸键会把那个键从**所有**程序手里抢走，而用户往往很晚才发现。"""
        win32 = plugin_module().win32

        with pytest.raises(win32.HotkeyError) as caught:
            win32.parse_hotkey("a")

        assert "修饰键" in str(caught.value)


class TestPositionParsing:
    @pytest.mark.parametrize("written", ["auto", "", "  ", "AUTO"])
    def test_auto_means_let_the_system_decide(self, written: str) -> None:
        win32 = plugin_module().win32

        assert win32.parse_position(written) is None

    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("1024,80", (1024, 80)),
            ("0,0", (0, 0)),
            # 副屏摆在主屏左边时坐标就是负的，把它拒掉等于双屏用户用不了。
            ("-1920,0", (-1920, 0)),
            # 中文输入法打出来的逗号是常事，认下来比让人去猜要好。
            ("1024，80", (1024, 80)),
            (" 12 , 34 ", (12, 34)),
        ],
    )
    def test_it_reads_a_pair(self, written: str, expected: tuple[int, int]) -> None:
        win32 = plugin_module().win32

        assert win32.parse_position(written) == expected

    @pytest.mark.parametrize("written", ["10", "10,20,30", "x,y", "10,abc"])
    def test_it_rejects_what_it_cannot_read(self, written: str) -> None:
        win32 = plugin_module().win32

        with pytest.raises(win32.HotkeyError):
            win32.parse_position(written)


class TestBrowserLookup:
    def test_a_configured_path_wins(self, tmp_path: Path) -> None:
        win32 = plugin_module().win32
        browser = tmp_path / "my-browser.exe"
        browser.write_text("", encoding="utf-8")

        assert win32.find_browser(str(browser)) == browser

    def test_a_configured_path_that_is_not_there_is_not_guessed_around(
        self, tmp_path: Path
    ) -> None:
        """配了却不存在时返回 ``None``，而不是「那就随便找一个吧」。

        猜一个的话，用户配错路径的症状是「窗口还是开出来了」——而他会以为是
        别的原因，因为配置看起来「生效了」。
        """
        win32 = plugin_module().win32

        assert win32.find_browser(str(tmp_path / "nope.exe")) is None

    def test_the_os_layer_does_not_import_alterego(self) -> None:
        """``win32.py`` 是纯操作系统那一层，一个 alterego 的模块都不该 import。

        ``tests/test_interfaces_consistency.py`` 的守卫扫插件目录里的每一个
        ``.py``（它曾经只扫 ``*/plugin.py``），这条在这里再说一遍，是因为
        「帮手模块」正是那条守卫曾经漏掉的东西。

        查的是 **import 节点**，不是源码里有没有出现「alterego」这几个字：
        这个模块里有一个叫 ``alterego-desktop-hotkey`` 的线程名。
        """
        path = Path(plugin_module().win32.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        roots = {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }

        assert path.name == "win32.py"
        assert "alterego" not in roots


# ────────────────────────────────────────────────────────────
# 加载：注册了什么，以及**没有**做什么
# ────────────────────────────────────────────────────────────


class TestLoad:
    def test_it_registers_itself_as_a_capability(self, tmp_path: Path, clock: FrozenClock) -> None:
        with loaded(tmp_path, clock) as plugin:
            assert plugin.id == PLUGIN_ID
            assert plugin.intent_types == frozenset()

    def test_it_does_not_grab_the_keyboard_just_by_being_loaded(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """**这个文件里最重要的一条。**

        ``alterego plugins doctor`` 同样会 ``load_all()``。在这里占住全局快捷键，
        等于用户看了一眼插件健不健康就把键盘上的一个组合键交出去了。

        这里**显式**装一层 Windows（``os_layer`` 默认就是）。不装的话，这条断言
        断的是「运行这个测试的机器是 Windows」——开发机上永远成立，CI 上永远不成立，
        于是它拦不住任何东西。真正要断的是「能用的系统上，光加载不武装」，
        这句话跟机器无关。「不能用的系统上它会说话」是另一条，见
        ``test_off_windows_it_says_so_instead_of_pretending``。
        """
        with loaded(tmp_path, clock) as plugin:
            os_layer(plugin, windows=True)
            # 上面那一行才是「这台机器是 Windows」的来源。下面这句把它钉住：
            # 哪天有人把 ``os_layer`` 删了，这条用例会**在这台机器上**也失败，
            # 而不是安安静静地退回「断的是我自己的系统」。
            assert sys.modules[type(plugin).__module__].win32.IS_WINDOWS is True

            assert plugin.armed is False
            assert plugin.health() == HealthStatus(
                ok=True,
                detail="待命中：它会等 alterego serve 把界面起来，在那之前不占快捷键。",
            )

    def test_the_plugin_is_registered_under_its_service_name(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        """名字是 ``desktop_window``，不是插件 id。

        路由与登记表说的是「哪件事谁能干」，插件 id 说的是「哪个包提供它」。
        """
        with load_plugin(tmp_path, clock) as (registry, _bus, _manager):
            found = registry.get(Capability, name=SERVICE_NAME)

            assert found.id == PLUGIN_ID

    def test_a_bad_hotkey_fails_at_load_instead_of_at_the_first_press(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        """配置写错了要**当场**报出来，不用等到第一次按快捷键才发现「它没反应」。

        清单只能表达「这是个字符串」，表达不了「它得是个能用的快捷键」——
        所以这一条得在 ``on_load`` 里当场做，而 ``plugins doctor`` 会把它报出来。
        """
        failure = load_failure(tmp_path, clock, plugin_config={"hotkey": "a"})

        assert failure["plugin_id"] == PLUGIN_ID
        assert failure["where"] == "on_load"
        assert "修饰键" in failure["error"]

    def test_a_bad_position_fails_at_load_too(self, tmp_path: Path, clock: FrozenClock) -> None:
        failure = load_failure(tmp_path, clock, plugin_config={"position": "左边一点"})

        assert failure["where"] == "on_load"
        assert "位置" in failure["error"]


# ────────────────────────────────────────────────────────────
# 武装：什么时候才真的占住快捷键
# ────────────────────────────────────────────────────────────


class TestArming:
    def test_a_foreign_topic_is_ignored(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """``on_event`` 收到**所有**事件，所以第一行必须是主题判断。"""
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)

            plugin.on_event(Event.create("tick.completed", {"tick_id": "t1"}, clock=clock))

            assert plugin.armed is False
            assert fake.listeners == []

    def test_listening_without_a_url_arms_nothing(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)

            plugin.on_event(listening_event(clock, payload={}))
            plugin.on_event(listening_event(clock, payload={"url": "   "}))

            assert plugin.armed is False
            assert fake.listeners == []

    def test_the_listening_event_arms_it_and_remembers_the_address(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)

            plugin.on_event(listening_event(clock))

            assert plugin.armed is True
            assert plugin.url == URL
            assert plugin.health() == HealthStatus(
                ok=True, detail=f"快捷键 ctrl+alt+a 已就位，地址 {URL}"
            )
            assert fake.listeners[-1].hotkey.text == "ctrl+alt+a"

    def test_the_address_is_the_one_it_was_handed(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """端口是 ``[web]`` 的事。

        这条测的是「插件没有自己造一个地址」：给它 9000 它就用 9000。
        """
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)

            plugin.on_event(
                listening_event(clock, payload={"url": "http://127.0.0.1:9000/", "port": 9000})
            )
            fake.press()

            assert plugin.url == "http://127.0.0.1:9000/"
            assert fake.window.url == "http://127.0.0.1:9000/"

    def test_an_occupied_hotkey_is_reported_not_swallowed(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """快捷键被占了是常事，后果是「这个功能不可用」，不是「插件坏了」。"""
        with loaded(tmp_path, clock) as plugin:
            os_layer(plugin, hotkey_ok=False)

            plugin.on_event(listening_event(clock))

            assert plugin.armed is False
            health = plugin.health()
            assert health.ok is False
            assert "占用" in health.detail

    def test_a_missing_browser_is_reported_not_swallowed(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        with loaded(tmp_path, clock) as plugin:
            os_layer(plugin, browser=False)

            plugin.on_event(listening_event(clock))

            assert plugin.armed is False
            assert "Edge" in plugin.health().detail

    def test_off_windows_it_says_so_instead_of_pretending(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """不在 Windows 上就不占键，并且**说清楚**。

        这里还挡住了一个更隐蔽的失败：``HotkeyListener`` 构造即抛
        ``HotkeyError``，而 ``on_event`` 里抛出去的异常会被总线记成一条
        ``bus.handler_failed``——用户看到的是「事件处理失败」，
        而不是「这个系统上不支持」。
        """
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin, windows=False)

            plugin.on_event(listening_event(clock))

            assert plugin.armed is False
            assert fake.listeners == []
            health = plugin.health()
            assert health.ok is False
            assert "Windows" in health.detail
            assert "Windows" in plugin.describe()

    def test_listening_twice_replaces_the_old_listener(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """重启界面（或者热重载）会再广播一次，旧的那个必须先放掉。"""
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)

            plugin.on_event(listening_event(clock))
            plugin.on_event(listening_event(clock))

            assert [listener.stops for listener in fake.listeners] == [1, 0]

    def test_a_window_cannot_even_be_constructed_off_windows(
        self, tmp_path: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``win32`` 在任何平台上都能 import，但构造即抛。

        换平台不该变成一个 import 期的 ``ImportError``，而是一句
        ``health()`` 里用户读得懂的话。
        """
        win32 = plugin_module().win32
        monkeypatch.setattr(win32, "_HANDLE_LOAD", False)

        with pytest.raises(win32.HotkeyError):
            win32.HotkeyListener(
                win32.parse_hotkey("ctrl+alt+a"), lambda: None, logging.getLogger("t")
            )
        with pytest.raises(win32.HotkeyError):
            win32.AppWindow(
                browser=Path("x"),
                url=URL,
                size=(1, 1),
                position=None,
                topmost=True,
                profile_dir=tmp_path,
                logger=logging.getLogger("t"),
            )


# ────────────────────────────────────────────────────────────
# 窗口：开、收、再叫出来
# ────────────────────────────────────────────────────────────


class TestToggling:
    def test_the_first_press_opens_it_with_the_configured_shape(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)
            plugin.on_event(listening_event(clock))

            fake.press()

            window = fake.window
            assert window.launches == 1
            assert window.url == URL
            assert window.size == (1180, 800)
            assert window.position is None
            assert window.topmost is True
            assert window.profile_dir.name == "browser-profile"

    def test_the_second_press_hides_it_without_closing_it(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """藏起来而不是关掉：关掉的话下一次要重新加载一遍页面。"""
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)
            plugin.on_event(listening_event(clock))
            fake.press()

            fake.press()

            assert fake.window.shown() is False
            assert fake.window.alive() is True
            assert fake.window.closes == 0

    def test_the_third_press_brings_the_same_window_back(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)
            plugin.on_event(listening_event(clock))

            fake.press()
            fake.press()
            fake.press()

            assert len(fake.windows) == 1
            assert fake.window.shown() is True

    def test_a_window_the_user_closed_comes_back_as_a_new_one(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """那个窗口属于浏览器进程，我们拦不住那个叉——所以下一次就重开一个。"""
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)
            plugin.on_event(listening_event(clock))
            fake.press()
            fake.window.user_closed_it()

            fake.press()

            assert len(fake.windows) == 2
            assert fake.window.shown() is True

    def test_a_window_that_never_opens_leaves_a_reason(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """开不出来不能变成「按了没反应」。"""
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin, launch_ok=False)
            plugin.on_event(listening_event(clock))

            fake.press()

            assert fake.window.closes == 1
            assert "窗口" in plugin.health().detail

    def test_the_next_press_after_a_failure_actually_tries_again(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """试一次没成，下一次按还得试——不能就此认定它坏了。

        开起来了就把上一次的原因清掉：窗口已经开起来了，就不该再报上一次的原因。
        一个「上次错了」的提示留在这里，比没有提示更坏：用户会去查一个
        已经不存在的问题。
        """
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin, launch_ok=False)
            plugin.on_event(listening_event(clock))
            fake.press()
            assert plugin.health().ok is False
            fake._launch_ok = True

            fake.press()

            assert fake.window.shown() is True
            assert "窗口" not in plugin.health().detail

    def test_a_press_before_a_url_is_a_no_op(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """没有地址就没有窗口可开。少了 ``_toggle`` 里那道门，

        它会拿着 ``browser=None`` 去造一个 ``AppWindow``。
        """
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)

            plugin._toggle()

            assert fake.windows == []


# ────────────────────────────────────────────────────────────
# 收摊
# ────────────────────────────────────────────────────────────


class TestStop:
    def test_it_lets_go_of_both_the_key_and_the_window(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)
            plugin.on_event(listening_event(clock))
            fake.press()

            plugin.on_stop()

            assert plugin.armed is False
            assert fake.listeners[-1].stops == 1
            assert fake.window.closes == 1

    def test_it_is_idempotent(self, tmp_path: Path, clock: FrozenClock, os_layer: Any) -> None:
        """``on_stop`` 会被调两次（停机 + 热重载）。"""
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)
            plugin.on_event(listening_event(clock))

            plugin.on_stop()
            plugin.on_stop()
            plugin.on_stop()

            assert fake.listeners[-1].stops == 1

    def test_stopping_before_anything_was_armed_is_fine(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        with loaded(tmp_path, clock) as plugin:
            os_layer(plugin)

            plugin.on_stop()

            assert plugin.armed is False

    def test_it_lets_go_when_the_manager_shuts_down(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """真实的卸载路径：``manager.shutdown()`` 之后不该还占着键盘。"""
        with load_plugin(tmp_path, clock) as (registry, _bus, manager):
            plugin = registry.get(Capability, name=SERVICE_NAME)
            fake = os_layer(plugin)
            plugin.on_event(listening_event(clock))

            manager.shutdown()

            assert fake.listeners[-1].stops == 1
            assert plugin.armed is False


# ────────────────────────────────────────────────────────────
# 配置变了
# ────────────────────────────────────────────────────────────


class TestConfigChanged:
    def test_a_shape_change_lands_on_the_open_window(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """用户改完尺寸想看到的是**那个正开着的**窗口变了，不是「下次开才生效」。"""
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)
            plugin.on_event(listening_event(clock))
            fake.press()

            plugin.on_config_changed(
                {"hotkey": "ctrl+alt+a", "width": 900, "height": 500, "always_on_top": False}
            )

            assert fake.window.resizes == [(900, 500)]
            assert fake.window.topmost_set == [False]
            assert len(fake.listeners) == 1  # 快捷键没变，不该重建

    def test_a_hotkey_change_rebuilds_the_listener(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """``RegisterHotKey`` 没有「改一下」的办法，只能拆了重来。"""
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)
            plugin.on_event(listening_event(clock))

            plugin.on_config_changed(
                {"hotkey": "ctrl+shift+d", "width": 1180, "height": 800, "always_on_top": True}
            )

            assert [listener.stops for listener in fake.listeners] == [1, 0]
            assert fake.listeners[-1].hotkey.text == "ctrl+shift+d"
            assert plugin.armed is True

    def test_a_hotkey_change_leaves_the_open_window_alone(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """用户改的是快捷键，不是那个正看着的窗口。

        所以「拆了重来」得拆得刚刚好：只放掉快捷键，别顺手把窗口也关了。
        """
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)
            plugin.on_event(listening_event(clock))
            fake.press()

            plugin.on_config_changed({"hotkey": "ctrl+shift+d"})

            assert fake.window.closes == 0
            assert fake.window.alive() is True
            fake.press()
            assert fake.window.shown() is False

    def test_a_hotkey_change_while_idle_does_not_arm_anything(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """界面还没起来时改配置，不该因此去占键盘。"""
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)

            plugin.on_config_changed({"hotkey": "ctrl+shift+d"})

            assert plugin.armed is False
            assert fake.listeners == []

    def test_a_bad_new_hotkey_is_kept_out_and_explained(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        """静默忽略会让「我改了怎么没反应」变成一个查不出来的问题。"""
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)
            plugin.on_event(listening_event(clock))

            plugin.on_config_changed({"hotkey": "a"})

            assert plugin.armed is True
            assert fake.listeners[-1].hotkey.text == "ctrl+alt+a"
            health = plugin.health()
            assert health.ok is False
            assert "新配置没被采纳" in health.detail

    def test_an_unchanged_config_does_nothing_at_all(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)
            plugin.on_event(listening_event(clock))

            plugin.on_config_changed({"hotkey": "ctrl+alt+a"})

            assert len(fake.listeners) == 1
            assert fake.windows == []


# ────────────────────────────────────────────────────────────
# 能力
# ────────────────────────────────────────────────────────────


class TestCapability:
    async def test_it_says_it_is_not_ready_before_the_interface_exists(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        with loaded(tmp_path, clock) as plugin:
            os_layer(plugin)

            result = await plugin.execute(None, None)

            assert result.ok is False
            assert result.error is not None
            assert "serve" in result.error

    async def test_it_summons_the_window(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)
            plugin.on_event(listening_event(clock))

            result = await plugin.execute(None, None)

            assert result.ok is True
            assert result.artifacts == {"url": URL}
            assert fake.window.shown() is True
            # summary 要写成「干了什么」，不能是一句「执行成功」。
            assert "叫出来" in result.summary

    async def test_it_toggles_like_the_hotkey_does(
        self, tmp_path: Path, clock: FrozenClock, os_layer: Any
    ) -> None:
        with loaded(tmp_path, clock) as plugin:
            fake = os_layer(plugin)
            plugin.on_event(listening_event(clock))
            await plugin.execute(None, None)

            second = await plugin.execute(None, None)

            assert fake.window.shown() is False
            assert "收起来" in second.summary


# ────────────────────────────────────────────────────────────
# 两边共用的那个字面量
# ────────────────────────────────────────────────────────────


class TestConsistencyWithTheAssemblyRoot:
    def test_the_topic_is_the_one_cli_serve_publishes(self) -> None:
        """插件只能 import ``alterego.interfaces.*`` 与 ``alterego.kernel.plugin``。

        ``serve.listening`` 这个名字在两边各写了一份，因为插件**够不着**
        ``cli_serve``。两份字面量靠这条测试钉在一起：改了一处而没改另一处，
        症状会是「界面起来了但快捷键永远没反应」——一句话都不报。
        """
        assert plugin_module().SERVE_LISTENING == cli_serve.SERVE_LISTENING

    def test_cli_serve_actually_publishes_it(self) -> None:
        """光有一个常量不算数，它得真的被广播出去。

        这条测的是一条**调用链**而不是一个值：``_serve`` 把 ``_announce_listening``
        挂到 ``_uvicorn`` 的 ``on_started`` 上。挂错了地方（比如挂到
        ``_banner`` 之前）时，``SERVE_LISTENING`` 还是那个对的值，
        但没有任何人收得到。
        """
        source = Path(cli_serve.__file__).read_text(encoding="utf-8")

        assert "_announce_listening" in source
        assert "on_started=lambda: _announce_listening(bus, clock, config)" in source

    def test_the_announcement_carries_the_url_the_plugin_needs(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        """载荷要真的带地址——回到上面那条：广播了但少一个键，等于没广播。"""
        bus = EventBus(clock, logger=logging.getLogger("test.desktop.announce"))
        seen: list[Any] = []
        bus.subscribe(cli_serve.SERVE_LISTENING, seen.append)
        config = Config.load(
            path=None,
            env={},
            overrides={"core": {"data_dir": str(tmp_path / "data")}, "web": {"port": 9123}},
        )

        cli_serve._announce_listening(bus, clock, config)

        assert [event.payload["url"] for event in seen] == ["http://127.0.0.1:9123/"]
        assert seen[0].payload["port"] == 9123
        assert "token" not in seen[0].payload

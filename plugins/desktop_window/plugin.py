"""桌面窗口插件：一个全局快捷键，把界面叫出来，或者收回去。

## 它是什么

`alterego serve` 起来之后，按一下快捷键，桌面上多一个没有地址栏的窗口，
里面就是那份 Web 界面——聊天、时间线、状态、设置、插件，一个不少。
再按一下收起来，窗口**不关**。

## 为什么是「别人的窗口」而不是自己画一个

因为这样**所有功能就真的是所有功能**。窗口里跑的是 `alterego.channels.web`
那一套页面，也就是浏览器里那一份——同样的路由、同样的数据、同样的卡片。
自己画一个界面意味着把这些页面**再实现一遍**，于是从「一个真源」变成
「两个必须同时改的地方」，而它们已经开始分叉的那一刻，用户看到的是
「网页上有、窗口里没有」，却没有任何一条报错。

代价写在明面上：那个窗口属于浏览器进程，所以我们拦不住用户点右上角的叉
（下一次按快捷键重新开一个）；它要求机器上有一个 Chromium 系浏览器
（Edge 或 Chrome，Windows 自带前者）。

## 为什么地址是**收到**的，不是这里写死的

`[web] port` 住在 `[web]` 里，读它的是 `cli_serve`——这个插件只是「知道有这回事」。
按 `docs/guide/plugin-development.md` § 2.4.1 的那条判断，这类值**不要搬过来**：
搬过来就有两个真源，而 `alterego serve --port 9000` 的那一天，插件会去开
``http://127.0.0.1:8765/``，开出来一个**连不上**的窗口——一个什么都没说错的失败。
同一节的另一半也适用：这个值**没人能改**（插件不开服务、不读 `[web]`），
所以它不会出现在本插件的 `[plugin.config.*]` 里。

于是地址只有一条路进来：`cli_serve` 在服务真的起来之后广播一条
``serve.listening``，载荷里带着 `url`。这就是「不是服务进程时绝不武装」的
那个机制（下一条）。

## 为什么 `on_start` 里什么都不做

因为**加载插件的进程不止一个**。`alterego serve` 会加载它，
`alterego plugins doctor` 也会（`cli_plugins.py` 里同样是一句 `manager.load_all()`），
`alterego plugins enable/disable/list` 同样会。如果占快捷键这件事放在
`on_start` 里，那么用户只是想看一眼插件健不健康，键盘上的 `ctrl+alt+a`
就被永久占掉了——而 `doctor` 打印的却是一切正常。

所以武装这件事挂在**那条事件**上：只有真正在服务的那个进程才会广播它。
`--no-web` 与 `[web] enabled = false` 都**不广播**，于是那两种情况下
插件静静地待着，一个键都不占。这不是判断，是同一个模式序的后果。

## 快捷键只在 Windows 上用

`ctypes` 调 `user32.RegisterHotKey` + `GetMessageW` 是零依赖的做法，
而它是 Win32 专有的。Linux 上 `XGrabKey`、macOS 上 `CGEventTap` 都得走
另一条路（后者还需要辅助功能授权），那不是这个插件现在能诚实承诺的事。
所以非 Windows 上 `health()` 报 `ok=False` 并说明原因——**不假装支持**。
窗口本身（`--app=`）在别的系统上一样能开，限制只在「全局」这两个字上。

依据: docs/guide/plugin-development.md § 2.4.1、§ 3、§ 5；docs/design/02-plugin-api.md § 16
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from alterego.interfaces.common import HealthStatus
from alterego.interfaces.simulation import Capability, CapabilityResult
from alterego.kernel.plugin import Plugin, PluginContext

from . import win32


__all__ = ["PLUGIN_ID", "SERVE_LISTENING", "SERVICE_NAME", "DesktopWindow"]


PLUGIN_ID: Final[str] = "capability.desktop_window"
"""插件 id，与 ``plugin.toml`` 里那个字段逐字一致。"""

SERVICE_NAME: Final[str] = "desktop_window"
"""注册到登记表里的名字。刻意不是 ``PLUGIN_ID``。

路由与登记表说的是「哪件事谁能干」，插件 id 说的是「哪个包提供它」。
两者今天长得像，含义完全不同（先例见 ``channels/web/plugin.py`` 的 ``CHANNEL_ID``）。
"""

SERVE_LISTENING: Final[str] = "serve.listening"
"""``cli_serve`` 在界面真的开始监听之后广播的主题。

**这个字面量在 ``cli_serve.py`` 里也有一份**，而插件不能 import 它
（插件只许 import ``alterego.interfaces.*`` 与 ``alterego.kernel.plugin``，
见 ``tests/test_interfaces_consistency.py``）。两份字面量靠
``tests/test_desktop_window_plugin.py::test_the_topic_is_the_one_cli_serve_publishes``
钉在一起：改了一处而没改另一处，那条测试当场红。
"""


# 默认值在这里各写一份，是为了让 ``_Settings.from_config({})`` 能算出
# 「一个字都没配」时的样子——测试拿它和 ``plugin.toml`` 里声明的 default 比。
_DEFAULT_HOTKEY: Final[str] = "ctrl+alt+a"
_DEFAULT_WIDTH: Final[int] = 1180
_DEFAULT_HEIGHT: Final[int] = 800
_DEFAULT_POSITION: Final[str] = "auto"
_DEFAULT_ON_TOP: Final[bool] = True
_DEFAULT_BROWSER: Final[str] = ""


@dataclass(frozen=True, slots=True)
class _Settings:
    """插件配置的规范化形态：一份已经去过空白、可以直接用的值。"""

    hotkey: str = _DEFAULT_HOTKEY
    width: int = _DEFAULT_WIDTH
    height: int = _DEFAULT_HEIGHT
    position: str = _DEFAULT_POSITION
    always_on_top: bool = _DEFAULT_ON_TOP
    browser: str = _DEFAULT_BROWSER

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> _Settings:
        """从 ``ctx.config`` 里取。

        缺键时退回代码里的默认值，而不是抛 ``KeyError``：``ctx.config`` 在
        正常情况下已经被 ``resolve_config`` 补全了，这里兜的是「有人在测试里
        直接构造了一个上下文」。
        """
        return cls(
            hotkey=str(config.get("hotkey", _DEFAULT_HOTKEY)).strip(),
            width=int(config.get("width", _DEFAULT_WIDTH)),
            height=int(config.get("height", _DEFAULT_HEIGHT)),
            position=str(config.get("position", _DEFAULT_POSITION)).strip(),
            always_on_top=bool(config.get("always_on_top", _DEFAULT_ON_TOP)),
            browser=str(config.get("browser", _DEFAULT_BROWSER)).strip(),
        )

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)


class DesktopWindow(Plugin):
    """把 Web 界面搬进一个可以被快捷键召唤的桌面窗口。"""

    id: str = PLUGIN_ID

    #: **空集合是故意的。**
    #:
    #: 它意味着推演循环永远不会挑中这个能力——而这是对的：在用户桌面上
    #: 开一个窗口是**用户**的动作，不是它能自己决定的事。填进一个
    #: ``intent_type`` 就等于允许它在你没看着的时候弹一个窗口出来，
    #: 而那种事只该由机制挡住（P3），不该靠一句「请不要随便弹」的提示词。
    intent_types: frozenset[str] = frozenset()

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._settings = _Settings()
        self._lock = threading.Lock()
        self._listener: Any = None
        self._window: Any = None
        self._browser: Any = None
        self._url = ""
        self._problem = ""

    # ── 状态 ────────────────────────────────────────────────

    @property
    def armed(self) -> bool:
        """快捷键占上了没有。

        这是「待命」与「真的在管用」之间唯一的那条线，所以它值得一个名字：
        ``alterego plugins doctor`` 要说的是它，``health()`` 判的也是它。
        """
        with self._lock:
            return self._listener is not None

    @property
    def url(self) -> str:
        """界面在哪个地址上。收到 ``serve.listening`` 之前是空串。"""
        with self._lock:
            return self._url

    # ── 生命周期 ────────────────────────────────────────────

    def on_load(self, ctx: PluginContext) -> None:
        """只做两件事：把配置读进来，把能力登记上去。

        **刻意不碰快捷键、不碰窗口**——见模块 docstring 里那一段
        「为什么 ``on_start`` 里什么都不做」，理由对这里同样成立。
        """
        settings = _Settings.from_config(ctx.config)
        # 快捷键与位置在这里就解析一遍：配置写错了要**当场**报错，
        # 而不是等到用户第一次按快捷键才发现「它没反应」。
        win32.parse_hotkey(settings.hotkey)
        win32.parse_position(settings.position)

        self._settings = settings
        self._ctx = ctx
        ctx.registry.register(Capability, self, name=SERVICE_NAME)
        ctx.logger.info(
            "桌面窗口插件就位（快捷键 %s；界面起来之后它才会真的生效）", settings.hotkey
        )

    def on_start(self) -> None:
        """空的，故意的。

        占一个全局快捷键是有副作用的动作，而这个钩子在 `alterego plugins doctor`
        里也会跑。武装改由 :meth:`on_event` 收到 ``serve.listening`` 时进行。
        """

    def on_stop(self) -> None:
        """收摊。**幂等**——停机与热重载都会走到这里。"""
        self._disarm()

    def on_config_changed(self, new_config: dict[str, Any]) -> None:
        """配置变了就地生效，不必重启。

        改快捷键只能把监听器拆了重建（``RegisterHotKey`` 没有「改一下」的办法）；
        改尺寸或置顶则直接作用在已经开着的那个窗口上——那才是用户改完想看到的。
        """
        try:
            settings = _Settings.from_config(new_config)
            win32.parse_hotkey(settings.hotkey)
            win32.parse_position(settings.position)
        except win32.HotkeyError as exc:
            # 不改任何东西，并且**记下来**：静默忽略会让「我改了怎么没反应」
            # 变成一个查不出来的问题，而 health() 会把这句话说给用户听。
            with self._lock:
                self._problem = f"新配置没被采纳：{exc}"
            if self._ctx is not None:
                self._ctx.logger.warning("桌面窗口插件的配置没被采纳：%s", exc)
            return

        with self._lock:
            old, self._settings = self._settings, settings
        if old.hotkey != settings.hotkey or old.browser != settings.browser:
            self._rearm()
            return
        with self._lock:
            window = self._window
        if window is not None and window.alive():
            window.set_topmost(settings.always_on_top)
            window.resize(settings.size)

    def on_event(self, event: Any) -> None:
        """只关心一件事：界面起来了，以及它在哪个地址。

        **只认自己处理得了的主题。** 这个钩子会被投递**所有**事件
        （``PluginManager`` 用 ``"*"`` 订阅的），所以第一行必须是主题判断；
        少了它，每个 tick 都要跑一遍这里。
        """
        if getattr(event, "topic", "") != SERVE_LISTENING:
            return
        payload = getattr(event, "payload", None) or {}
        url = str(payload.get("url", "")).strip()
        if not url:
            return
        self._arm(url)

    # ── 能力 ────────────────────────────────────────────────

    async def execute(self, intent: Any, ctx: Any) -> CapabilityResult:
        """把窗口叫出来。

        推演循环不会走到这里（``intent_types`` 是空的），但它是一条真实的入口：
        任何拿着登记表、按名字取 ``desktop_window`` 的代码都可以让窗口出现。
        """
        with self._lock:
            ready = bool(self._url)
            problem = self._problem
        if not ready:
            return CapabilityResult(
                ok=False,
                summary="桌面窗口还没接上界面",
                error=problem or "界面还没起来（先跑 alterego serve）",
            )
        self._toggle()
        with self._lock:
            shown = bool(self._window is not None and self._window.shown())
        verb = "叫出来了" if shown else "收起来了"
        return CapabilityResult(ok=True, summary=f"把桌面窗口{verb}", artifacts={"url": self._url})

    def describe(self) -> str:
        """一句话说明它现在是什么样。"""
        if not win32.IS_WINDOWS:
            return f"桌面窗口（{self._settings.hotkey}）：只在 Windows 上支持全局快捷键"
        return f"桌面窗口（{self._settings.hotkey}）"

    def health(self) -> HealthStatus:
        """这个插件现在能不能用。

        三种「不能用」各有各的话，因为用户能做的事不一样：换系统、
        关掉占用快捷键的那个程序、先跑 ``alterego serve``。
        把它们合并成一句「不可用」等于把排查的线索也一起删了。
        """
        if not win32.IS_WINDOWS:
            return HealthStatus(
                ok=False,
                detail=(
                    "全局快捷键只用 Windows 的消息队列实现（ctypes 调 user32），"
                    "当前系统不是 Windows，所以这个插件不会占用任何快捷键。"
                ),
            )
        with self._lock:
            problem = self._problem
            url = self._url
        if problem:
            return HealthStatus(ok=False, detail=problem)
        if not url:
            return HealthStatus(
                ok=True,
                detail="待命中：它会等 alterego serve 把界面起来，在那之前不占快捷键。",
            )
        return HealthStatus(ok=True, detail=f"快捷键 {self._settings.hotkey} 已就位，地址 {url}")

    # ── 内部：武装 / 收摊 ───────────────────────────────────

    def _arm(self, url: str) -> None:
        """界面起来了：记住地址，占住快捷键。窗口**先不开**。

        叫出来这件事留给用户按下去的那一刻：``alterego serve`` 起来时弹一个
        窗口出来是**打扰**，而用户那时要的只是服务在跑。
        """
        self._drop_listener()
        with self._lock:
            self._url = url
            self._problem = ""
        if self._ctx is None:
            return
        if not win32.IS_WINDOWS:
            # 不是 Windows 就别去试：``HotkeyListener`` 构造即抛 ``HotkeyError``，
            # 而那个异常会从 ``on_event`` 里穿出去，被总线记成一条
            # ``bus.handler_failed``——用户看到的是「事件处理失败」，
            # 而不是「这个系统上没有全局快捷键」。
            return

        browser = win32.find_browser(self._settings.browser)
        if browser is None:
            with self._lock:
                self._problem = (
                    "没找到 Edge 或 Chrome，窗口开不出来；"
                    "装一个，或者把 [plugin.config.browser] 指向它的可执行文件。"
                )
            self._ctx.logger.warning("桌面窗口插件：%s", self._problem)
            return

        listener = win32.HotkeyListener(
            win32.parse_hotkey(self._settings.hotkey), self._toggle, self._ctx.logger
        )
        if not listener.start():
            with self._lock:
                self._problem = listener.error or "快捷键没有注册成功"
            return
        with self._lock:
            self._browser = browser
            self._listener = listener
            self._problem = ""

    def _drop_listener(self) -> None:
        """只放掉快捷键，**不碰窗口**。幂等。

        和 :meth:`_disarm` 分开是因为「换一个快捷键」不该让用户正看着的
        那个窗口消失——而 ``RegisterHotKey`` 没有「改一下」的办法，
        只能拆了重来。
        """
        with self._lock:
            listener, self._listener = self._listener, None
        if listener is not None:
            listener.stop()

    def _disarm(self) -> None:
        """拆掉快捷键与窗口。**幂等。**"""
        self._drop_listener()
        with self._lock:
            window, self._window = self._window, None
            self._url = ""
            self._browser = None
            self._problem = ""
        if window is not None:
            window.close()

    def _rearm(self) -> None:
        """配置改了之后重来一遍；界面没起来就什么都不做。

        正开着的窗口**不动**：用户改的是快捷键，不是那个窗口。
        """
        with self._lock:
            url = self._url
        if not url:
            return
        self._arm(url)

    # ── 内部：快捷键按下去 ──────────────────────────────────

    def _toggle(self) -> None:
        """开、藏、叫出来——三个动作按当前状态挑一个。

        这段跑在快捷键自己的线程上（``HotkeyListener`` 的消息循环里），
        所以它**必须短**：在这里等上几秒，等待期间按的快捷键会堆在队列里。
        好在真正会慢的只有「第一次开窗口」，而那是用户本来就要等的事。
        """
        with self._lock:
            window = self._window
            browser = self._browser
            url = self._url
            settings = self._settings
            ctx = self._ctx
        if browser is None or ctx is None or not url:
            return
        if window is not None and window.alive():
            if window.shown():
                window.hide()
            else:
                window.show()
            return
        self._open(browser, url, settings, ctx)

    def _open(self, browser: Any, url: str, settings: _Settings, ctx: PluginContext) -> None:
        """真的开一个窗口。失败时把原因留下来，不要变成一个静默的无反应。"""
        window = win32.AppWindow(
            browser=browser,
            url=url,
            size=settings.size,
            position=win32.parse_position(settings.position),
            topmost=settings.always_on_top,
            # 配置目录从 **cache_dir** 里开：那是机器上的一次性东西，
            # 备份与同步都不该带上它（data_dir 是会被备份的那个）。
            profile_dir=ctx.paths.cache_dir / "browser-profile",
            logger=ctx.logger,
        )
        with self._lock:
            self._window = window
        if window.launch():
            # 上一次失败的原因现在是旧的，留着它等于对用户撒谎：
            # 窗口明明开出来了，``health()`` 还在说「开不起来」。
            with self._lock:
                self._problem = ""
            return
        window.close()
        with self._lock:
            self._window = None
            self._problem = "浏览器没有把窗口开起来，原因见日志"
        ctx.logger.warning("桌面窗口插件：%s", self._problem)

"""本插件用到的 Windows 原语：全局快捷键，以及「一个不属于我们的窗口」。

## 为什么单独一个文件

这里**没有一行是业务判断**：把 ``ctrl+alt+a`` 翻成 Win32 的修饰符位与虚拟键码、
在一堆顶层窗口里认出我们刚启动的那一个、把它藏起来或叫出来。这些是操作系统的
事实，和「该不该开窗口」无关。分开放之后，前半可以在一台没有桌面的机器上被
测透（``parse_hotkey`` 是纯函数），后半不必为了「换个快捷键写法」重新读一遍
窗口管理。

## 只用标准库

``ctypes`` / ``subprocess`` / ``shutil``。pywin32、keyboard、pygetwindow
都能把这件事写得短一点，代价是这个插件从「装上就能用」变成「先装三个包」
（设计原则 P5：标准库优先）。

## 它只在 Windows 上有用，但在任何平台上都能 import

``IS_WINDOWS`` 为假时纯函数照常工作，``HotkeyListener`` / ``AppWindow``
构造即抛。这样「换一个平台」不会变成 import 期的 ``ImportError``，
而是一句写在 ``health()`` 里的、用户读得懂的话。

依据: docs/guide/plugin-development.md § 2、docs/design/05-channels.md § 3.3
"""

from __future__ import annotations

import ctypes
import logging
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final


__all__ = [
    "IS_WINDOWS",
    "AppWindow",
    "Hotkey",
    "HotkeyError",
    "HotkeyListener",
    "find_browser",
    "parse_hotkey",
    "parse_position",
]


IS_WINDOWS: Final[bool] = sys.platform == "win32"
"""本模块里那些 Win32 调用能不能用。纯函数不看它。"""


class HotkeyError(ValueError):
    """快捷键写法不合法。

    继承 ``ValueError`` 而不是另起一个体系：它的全部用途是「告诉用户这一行
    字该怎么改」，而配置校验失败在这个项目里就是值错误。
    """


# ── Win32 常量 ──────────────────────────────────────────────

_MOD_ALT: Final[int] = 0x0001
_MOD_CONTROL: Final[int] = 0x0002
_MOD_SHIFT: Final[int] = 0x0004
_MOD_WIN: Final[int] = 0x0008
_MOD_NOREPEAT: Final[int] = 0x4000
"""按住不放时不重复触发。少了这一位，长按一次会开关十几次。"""

_WM_HOTKEY: Final[int] = 0x0312
_WM_QUIT: Final[int] = 0x0012

_HWND_TOPMOST: Final[int] = -1
_HWND_NOTOPMOST: Final[int] = -2

_SWP_NOSIZE: Final[int] = 0x0001
_SWP_NOMOVE: Final[int] = 0x0002
_SWP_NOACTIVATE: Final[int] = 0x0010
_SWP_SHOWWINDOW: Final[int] = 0x0040

_SW_HIDE: Final[int] = 0
_SW_SHOW: Final[int] = 5
_SW_RESTORE: Final[int] = 9

_ASFW_ANY: Final[int] = 0xFFFFFFFF
"""``AllowSetForegroundWindow`` 的参数：允许任何进程抢前台。

没有它，``SetForegroundWindow`` 会被 Windows 拒绝——而症状是「窗口出来了但
焦点还在原来的程序上，打字打进了别人家」。
"""

_GW_OWNER: Final[int] = 4
_HOTKEY_ID: Final[int] = 0xA1B2
"""``RegisterHotKey`` 的编号。取值必须在 0x0000–0xBFFF 之间（应用自用区间）。"""


def _load_user32() -> Any:
    """拿 ``user32`` 的句柄；不在 Windows 上返回 ``None``。

    函数签名在这里显式声明是**必须的**，不是讲究：``ctypes`` 默认把整数参数
    当 32 位 ``int`` 传，而 64 位 Windows 上 ``HWND`` 是 8 字节——不声明的话
    句柄会被截断，症状是 ``SetWindowPos`` 返回成功但窗口没动。
    """
    if not IS_WINDOWS:  # pragma: no cover - 取决于平台
        return None
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL
    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    user32.AllowSetForegroundWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    return user32


_user32: Any = _load_user32()

_HANDLE_LOAD = _user32 is not None


def _as_int(handle: Any) -> int:
    """把 ctypes 交回来的句柄统一成 ``int``。

    ``HWND`` 在 64 位上是 ``c_void_p``，回调里拿到的是 ``int``，而 ``None``
    表示空句柄——三种形态都要能比较，所以只在一处做这件事。
    """
    if handle is None:
        return 0
    if isinstance(handle, int):
        return handle
    return int(getattr(handle, "value", 0) or 0)


# ── 快捷键：纯函数 ──────────────────────────────────────────


#: 修饰键的别名表。值是 ``(显示名, Win32 位)``。
#:
#: 多个别名映射到同一个显示名是**刻意的**：``cmd`` 在 macOS 上是习惯写法，
#: 在这里等价于 ``win``。把别名认下来比让用户去猜我们只认哪一种要友好，
#: 而显示名只有一份，所以日志里不会出现同一个键的两种写法。
_MODIFIER_ALIASES: Final[dict[str, tuple[str, int]]] = {
    "ctrl": ("ctrl", _MOD_CONTROL),
    "control": ("ctrl", _MOD_CONTROL),
    "alt": ("alt", _MOD_ALT),
    "shift": ("shift", _MOD_SHIFT),
    "win": ("win", _MOD_WIN),
    "super": ("win", _MOD_WIN),
    "meta": ("win", _MOD_WIN),
    "cmd": ("win", _MOD_WIN),
}

#: 显示顺序。**不是**用户输入顺序：``alt+ctrl+a`` 与 ``ctrl+alt+a`` 是同一个
#: 快捷键，日志与 ``describe()`` 里必须长得一样，否则「我怎么改都不生效」
#: 会变成一个查不出来的问题。
_MODIFIER_ORDER: Final[tuple[str, ...]] = ("ctrl", "alt", "shift", "win")

#: 具名主键。含标点的键刻意不收：``ctrl++`` 没法无歧义地切分，
#: 而为了一个没人用的写法把解析器变复杂是划不来的。
_NAMED_KEYS: Final[dict[str, int]] = {
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "escape": 0x1B,
    "esc": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
}


def _build_key_table() -> dict[str, int]:
    """字母、数字、F1–F24 的虚拟键码表。"""
    table: dict[str, int] = dict(_NAMED_KEYS)
    for index in range(26):
        table[chr(ord("a") + index)] = 0x41 + index
    for digit in range(10):
        table[str(digit)] = 0x30 + digit
    for index in range(1, 25):
        table[f"f{index}"] = 0x70 + index - 1
    return table


_KEYS: Final[dict[str, int]] = _build_key_table()


@dataclass(frozen=True, slots=True)
class Hotkey:
    """一个解析好的全局快捷键。"""

    #: 修饰键的按位或（``MOD_*``）。
    modifiers: int
    #: 主键的虚拟键码。
    vk: int
    #: 规范化后的写法，用来打日志与显示。
    text: str


def parse_hotkey(text: str) -> Hotkey:
    """把 ``"ctrl+alt+a"`` 解析成 :class:`Hotkey`。

    **至少要求一个修饰键。** 这不是风格问题：一个裸键的全局快捷键会把这个键
    从**所有**程序手里抢走——``a`` 就再也打不出来了，而且用户是在打开某个
    编辑器之后才发现。宁可在这里报错，也不要让一个「能跑起来但毁掉打字」的
    配置生效。

    Raises:
        HotkeyError: 写法不合法，消息里说明该改什么。
    """
    raw = text.strip()
    if not raw:
        raise HotkeyError("快捷键不能为空")
    parts = [part.strip().lower() for part in raw.split("+")]
    if any(not part for part in parts):
        raise HotkeyError(f"快捷键里有一段是空的，是不是多写了一个加号：{raw}")

    *modifier_parts, key_part = parts
    if not modifier_parts:
        raise HotkeyError(
            f"快捷键至少要有一个修饰键（{raw} 是一个裸键，会把整个键盘上的这个键抢走）"
        )

    modifiers = 0
    names: list[str] = []
    for part in modifier_parts:
        found = _MODIFIER_ALIASES.get(part)
        if found is None:
            raise HotkeyError(
                f"不认识的修饰键 {part!r}；只认 " + "、".join(sorted(_MODIFIER_ALIASES))
            )
        name, bit = found
        if name in names:
            raise HotkeyError(f"修饰键 {name} 写了两次：{raw}")
        names.append(name)
        modifiers |= bit

    vk = _KEYS.get(key_part)
    if vk is None:
        raise HotkeyError(
            f"不认识的键 {key_part!r}；可用的是 a–z、0–9、f1–f24，以及 "
            + "、".join(sorted(_NAMED_KEYS))
        )

    ordered = [name for name in _MODIFIER_ORDER if name in names]
    return Hotkey(modifiers=modifiers | _MOD_NOREPEAT, vk=vk, text="+".join([*ordered, key_part]))


def parse_position(text: str) -> tuple[int, int] | None:
    """``"auto"`` → ``None``；``"1024,80"`` → ``(1024, 80)``。

    允许负坐标：显示器摆在主屏左边时那一块的坐标就是负的，把它们拒掉等于
    「双屏用户的副屏用不了这个功能」。

    Raises:
        HotkeyError: 写法不合法。
    """
    raw = text.strip()
    if not raw or raw.lower() == "auto":
        return None
    pieces = [piece.strip() for piece in raw.replace("，", ",").split(",")]
    if len(pieces) != 2:
        raise HotkeyError(f"位置要写成 横,纵（或 auto），收到的是 {raw!r}")
    try:
        return (int(pieces[0]), int(pieces[1]))
    except ValueError as exc:
        raise HotkeyError(f"位置的横纵坐标都要是整数：{raw!r}") from exc


# ── 找浏览器 ────────────────────────────────────────────────


#: 按可信度排列的候选位置。Edge 在前：它在 Windows 上是系统自带的，
#: 不装额外东西就能用（P5 的同一个理由）。
_BROWSER_CANDIDATES: Final[tuple[str, ...]] = (
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)

#: PATH 上的候选名字。
_BROWSER_NAMES: Final[tuple[str, ...]] = ("msedge", "chrome", "chromium")


def find_browser(configured: str = "") -> Path | None:
    """找一个能开无边框窗口的 Chromium 系浏览器。

    先看用户配的，再看系统装在哪，最后看 PATH。返回 ``None`` 表示一个都没有——
    调用方要把这件事说成一句用户读得懂的话，而不是让它变成一个 ``FileNotFoundError``。
    """
    import os

    if configured.strip():
        candidate = Path(os.path.expandvars(configured.strip()))
        return candidate if candidate.is_file() else None

    for pattern in _BROWSER_CANDIDATES:
        candidate = Path(os.path.expandvars(pattern))
        if candidate.is_file():
            return candidate

    for name in _BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


# ── 全局快捷键 ──────────────────────────────────────────────


class HotkeyListener:
    """在一个自己的线程上占住一个全局快捷键。

    **必须在有消息循环的线程上注册。** ``RegisterHotKey`` 把热键投递给
    **注册它的那个线程**的消息队列，所以注册、等待、注销三件事必须在同一个
    线程里；这也是这里要开线程而不是起一个 ``asyncio`` 任务的原因——
    线程有自己的消息队列，而事件循环没有。
    """

    def __init__(self, hotkey: Hotkey, on_press: Any, logger: logging.Logger) -> None:
        if not _HANDLE_LOAD:  # pragma: no cover - 取决于平台
            raise HotkeyError("这个插件只会用 Windows 的消息队列，当前系统不是 Windows")
        self._hotkey = hotkey
        self._on_press = on_press
        self._logger = logger
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._running = False
        self._error = ""

    @property
    def running(self) -> bool:
        """真的占住了这个快捷键吗。"""
        return self._running

    @property
    def error(self) -> str:
        """没占住的原因（占住了就是空串）。"""
        return self._error

    def start(self) -> bool:
        """注册并开始监听。返回是否成功——**失败不抛异常**。

        失败是常事（快捷键被别的程序占了），而它的后果是「这个功能不可用」，
        不是「插件坏了」。所以这里返回布尔值，由调用方把它翻译成一句
        ``health()`` 里的说明。
        """
        if self._thread is not None:
            return self._running
        self._thread = threading.Thread(
            target=self._loop, name="alterego-desktop-hotkey", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=5.0)
        return self._running

    def stop(self) -> None:
        """注销并退出线程。**幂等**——``on_stop`` 可能被调两次。"""
        thread_id = self._thread_id
        if thread_id is not None and _HANDLE_LOAD:
            _user32.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None
        self._running = False

    # ── 线程内部 ────────────────────────────────────────────

    def _loop(self) -> None:
        from ctypes import wintypes

        self._thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
        registered = bool(
            _user32.RegisterHotKey(None, _HOTKEY_ID, self._hotkey.modifiers, self._hotkey.vk)
        )
        if not registered:
            self._error = f"Windows 没有把 {self._hotkey.text} 给出来（多半已被别的程序占用）"
            self._logger.warning("桌面窗口插件占不住快捷键：%s", self._error)
            self._ready.set()
            return

        self._running = True
        self._error = ""
        self._ready.set()
        self._logger.info("桌面窗口插件的快捷键已就位：%s", self._hotkey.text)
        message = wintypes.MSG()
        try:
            while True:
                got = _user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if got <= 0:  # 0 = WM_QUIT，-1 = 出错；两种都该收摊
                    break
                if message.message == _WM_HOTKEY and int(message.wParam) == _HOTKEY_ID:
                    self._fire()
        finally:
            _user32.UnregisterHotKey(None, _HOTKEY_ID)
            self._running = False

    def _fire(self) -> None:
        """按了一下。**绝不让异常跑出去**。

        异常跑出这个函数会穿过 ``GetMessageW`` 的调用栈，直接把线程带走——
        而症状是「按了一次之后快捷键就再也不响应了」，且日志里什么都没有。
        """
        try:
            self._on_press()
        except Exception:
            self._logger.exception("处理快捷键时出错，快捷键仍然可用")


# ── 窗口 ────────────────────────────────────────────────────


def _window_for_pid(pid: int) -> int:
    """这个进程的顶层可见窗口句柄；没有就是 0。

    只认「顶层」与「没有属主」的窗口：Chromium 的内部窗口都是这两者之一，
    不筛掉的话第一个撞上的很可能是某个隐藏的辅助窗口，而症状是
    「窗口开了但藏不起来」。
    """
    from ctypes import wintypes

    found: list[int] = []

    def visit(hwnd: Any, _lparam: Any) -> bool:
        owner = _window_for_pid_owner(hwnd)
        if (
            owner == pid
            and _user32.IsWindowVisible(hwnd)
            and _as_int(_user32.GetWindow(hwnd, _GW_OWNER)) == 0
        ):
            found.append(_as_int(hwnd))
            return False
        return True

    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(visit)
    _user32.EnumWindows(callback, 0)
    return found[0] if found else 0


def _window_for_pid_owner(hwnd: Any) -> int:
    """一个窗口的所属进程 id。"""
    from ctypes import wintypes

    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


class AppWindow:
    """一个我们启动、但不属于我们的窗口。

    **它是别的进程的窗口**，这件事决定了整块代码的形状：句柄能跨进程用
    （移动、置顶、显示、隐藏都是窗口管理器的事），但**不能**给别的进程的窗口
    换消息处理函数。所以「用户点了右上角的叉」我们拦不住——那个窗口就是关了，
    下一次按快捷键重新开一个。这不是妥协，是这条路唯一的走法；
    `docs/adr/0013-*.md` 里写着为什么没走另一条（自己画一个窗口）。

    窗口属于一个**独占的用户配置目录**：Chromium 发现已经有同类进程时会把手上的
    请求交给那个进程，于是 ``Popen`` 拿到的 pid 当场变成孤儿，而句柄再也找不到。
    独占之后，那个 pid 就一定是窗口的主人。
    """

    def __init__(
        self,
        *,
        browser: Path,
        url: str,
        size: tuple[int, int],
        position: tuple[int, int] | None,
        topmost: bool,
        profile_dir: Path,
        logger: logging.Logger,
    ) -> None:
        if not _HANDLE_LOAD:  # pragma: no cover - 取决于平台
            raise HotkeyError("这个插件只会用 Windows 的窗口管理器，当前系统不是 Windows")
        self._browser = browser
        self._url = url
        self._size = size
        self._position = position
        self._topmost = topmost
        self._profile_dir = profile_dir
        self._logger = logger
        self._process: subprocess.Popen[bytes] | None = None
        self._hwnd = 0

    # ── 生命周期 ────────────────────────────────────────────

    def launch(self, *, timeout: float = 25.0) -> bool:
        """开窗口，并等到认出它的句柄。返回是否成功。"""
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._process = subprocess.Popen(
            self._argv(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self._logger.warning("浏览器进程开完就退了（退出码 %s）", self._process.returncode)
                return False
            hwnd = _window_for_pid(self._process.pid)
            if hwnd:
                self._hwnd = hwnd
                self._apply_topmost()
                return True
            time.sleep(0.15)
        self._logger.warning("等了 %s 秒也没等到窗口，浏览器可能把它交给了别的进程", timeout)
        return False

    def alive(self) -> bool:
        """窗口还在吗。进程没了或句柄失效都算不在。"""
        if self._process is None or self._process.poll() is not None:
            return False
        return self._hwnd != 0 and bool(_user32.IsWindow(self._hwnd))

    def shown(self) -> bool:
        """窗口现在是可见的吗（最小化不算）。"""
        if not self.alive():
            return False
        return bool(_user32.IsWindowVisible(self._hwnd)) and not bool(_user32.IsIconic(self._hwnd))

    # ── 三个动作 ────────────────────────────────────────────

    def show(self) -> None:
        """叫出来，并抢到前台。"""
        if not self.alive():
            return
        _user32.AllowSetForegroundWindow(_ASFW_ANY)
        _user32.ShowWindow(self._hwnd, _SW_RESTORE if _user32.IsIconic(self._hwnd) else _SW_SHOW)
        self._apply_topmost()
        _user32.SetForegroundWindow(self._hwnd)

    def hide(self) -> None:
        """藏起来。**不关掉**：关掉的话下一次就得重新开一个，而重开要几秒。"""
        if self.alive():
            _user32.ShowWindow(self._hwnd, _SW_HIDE)

    def set_topmost(self, topmost: bool) -> None:
        """改置顶状态。窗口开着的时候改配置走这里，不必重开。"""
        self._topmost = topmost
        self._apply_topmost()

    def resize(self, size: tuple[int, int]) -> None:
        """改外框尺寸。"""
        self._size = size
        if self.alive():
            _user32.SetWindowPos(
                self._hwnd,
                _HWND_TOPMOST if self._topmost else _HWND_NOTOPMOST,
                0,
                0,
                size[0],
                size[1],
                _SWP_NOMOVE | _SWP_NOACTIVATE,
            )

    def close(self) -> None:
        """结束这个窗口。**幂等**。"""
        process, self._process = self._process, None
        self._hwnd = 0
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - 只在浏览器卡死时
            self._logger.warning("浏览器没有按期退出，强杀它")
            process.kill()

    # ── 内部 ────────────────────────────────────────────────

    def _argv(self) -> list[str]:
        """命令行。

        几个看似可有可无的参数都是**为了让它看起来像一个应用窗口**：
        ``--app`` 去掉地址栏与标签页；``--no-first-run`` / ``--no-default-browser-check``
        挡住首次启动向导；``--hide-crash-restore-bubble`` 挡住「上次没有正常关闭」
        那条黄条——我们关窗口的方式就是结束进程，不挡的话它每次开机都会出现。
        """
        argv = [
            str(self._browser),
            f"--app={self._url}",
            f"--window-size={self._size[0]},{self._size[1]}",
            f"--user-data-dir={self._profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--hide-crash-restore-bubble",
            "--noerrdialogs",
            "--disable-session-crashed-bubble",
        ]
        if self._position is not None:
            argv.append(f"--window-position={self._position[0]},{self._position[1]}")
        return argv

    def _apply_topmost(self) -> None:
        if not (self._hwnd and _user32.IsWindow(self._hwnd)):
            return
        _user32.SetWindowPos(
            self._hwnd,
            _HWND_TOPMOST if self._topmost else _HWND_NOTOPMOST,
            0,
            0,
            0,
            0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
        )

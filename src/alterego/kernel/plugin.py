"""插件契约的门面：插件基类，以及插件作者需要的全部类型。

实际实现分布在三个模块里，这里统一导出，插件只要 ``import alterego.kernel.plugin``
就够了——``docs/design/02-plugin-api.md`` § 4.1 承诺的「内核提供，插件只需 import」
指的是这个模块：

- :mod:`alterego.kernel.manifest` —— ``plugin.toml`` 的解析与配置合成。
- :mod:`alterego.kernel.context` —— :class:`PluginContext` 与归属视图。
- 本模块 —— :class:`Plugin` 基类。

三条设计原则（§ 1.1）：**内核无知** / **声明式契约** / **零内核修改**。

依据: docs/design/02-plugin-api.md § 3–5
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Mapping
from typing import Any

from alterego.interfaces.common import HealthStatus
from alterego.kernel.bus import Event
from alterego.kernel.context import (
    OwnedBus,
    OwnedRegistry,
    PluginContext,
    PluginPaths,
    PluginState,
)
from alterego.kernel.manifest import (
    API_VERSION,
    MASK,
    ConfigField,
    ConfigValueType,
    PluginKind,
    PluginManifest,
    PluginStatus,
    is_api_version_compatible,
    parse_duration,
    resolve_config,
)


__all__ = [
    "API_VERSION",
    "MASK",
    "ConfigField",
    "ConfigValueType",
    "OwnedBus",
    "OwnedRegistry",
    "Plugin",
    "PluginContext",
    "PluginKind",
    "PluginManifest",
    "PluginPaths",
    "PluginState",
    "PluginStatus",
    "is_api_version_compatible",
    "parse_duration",
    "resolve_config",
]


# ── 插件基类 ────────────────────────────────────────────────


class Plugin(ABC):
    """所有插件的基类。

    继承 :class:`ABC` 是为了表明意图，但**刻意不设** ``@abstractmethod``：
    插件按需覆盖自己关心的一两个钩子就够了，强制实现九个空方法只会制造样板代码。
    钩子全部是同步的——那么点工作量不值得让每个插件都变成 async。
    """

    #: 由 ``PluginManager`` 在实例化后注入。
    manifest: PluginManifest

    @property
    def id(self) -> str:
        return self.manifest.id

    def on_load(self, ctx: PluginContext) -> None:
        """加载阶段：注册能力、订阅事件、读配置。

        **不要**在这里做网络请求或耗时操作——它会拖住整个启动。
        抛异常 → 本插件标记 ``Failed``，已注册的实现会被回滚，其余插件继续启动。
        """

    def on_start(self) -> None:
        """启动阶段：此时依赖本插件的插件也都 load 完了。可以起后台任务。"""

    def on_stop(self) -> None:
        """停止阶段（逆拓扑序）。**必须幂等**——可能被调用多次。"""

    def on_unload(self) -> None:
        """卸载阶段：清掉注册表里的引用。"""

    def on_config_changed(self, new_config: Mapping[str, Any]) -> None:
        """配置热更新回调。实现它就能做到「改配置不用重启」。"""

    def on_tick_pre(self, ctx: Any) -> None:
        """每个 tick 开始时调用。"""

    def on_tick_post(self, ctx: Any) -> None:
        """每个 tick 结束时调用。"""

    def on_event(self, event: Event) -> None:
        """收到已订阅的事件。也可以直接在 ``on_load`` 里用 ``ctx.bus.subscribe``。"""

    def health(self) -> HealthStatus:
        """健康检查。``alterego plugins doctor`` 会调用它。"""
        return HealthStatus(ok=True, detail="正常")

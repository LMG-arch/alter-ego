"""示例能力插件——照着抄就能跑。

它演示一个 ``capability`` 插件的最小完整形态：

1. 从 ``alterego.kernel.plugin`` 拿到基类与上下文类型
   （**插件只需要 import 这一个模块**）；
2. 从 ``alterego.interfaces`` 拿到跨层契约（这里是 ``Capability`` 与 ``CapabilityResult``）；
3. 在 ``on_load`` 里注册自己提供的能力、订阅事件；
4. 用 ``ctx.logger`` 打日志、用 ``ctx.now()`` 取时间、用 ``ctx.config`` 读配置；
5. ``on_stop`` 写成幂等的。

细节说明见 ``docs/design/02-plugin-api.md`` § 4 与 § 13。
"""

from __future__ import annotations

from typing import Any

from alterego.interfaces.simulation import Capability, CapabilityResult
from alterego.kernel.plugin import Plugin, PluginContext


class ExampleCapability(Plugin):
    """一句问候。仅用于演示，不做什么有用的事。"""

    #: 这个能力能执行哪些意图。真实的能力会在这里声明 ``{"post_moment"}`` 之类。
    intent_types: frozenset[str] = frozenset()

    #: ``Capability`` 契约要求的稳定标识。
    id: str = "capability.example"

    #: 最近一次 ``tick.completed`` 携带的 tick 编号。
    _last_tick_id: str = ""

    def on_load(self, ctx: PluginContext) -> None:
        # 归属自动记在本插件名下（ADR-0007），所以这里**不要**传 owner。
        #
        # 注册在 ``Capability`` 这个 Protocol 上，而不是自己的类上：
        # 别的插件要靠「我能干什么」找到你，而不是靠 import 你的类——
        # 那会破坏插件隔离（自己家目录之外的代码不该被 import）。
        ctx.registry.register(Capability, self, name="example")
        ctx.bus.subscribe("tick.completed", self._on_tick_completed)

        # 配置已由内核校验、填好默认值、解析过 ${ENV}。
        self._ctx = ctx
        self._greeting = str(ctx.config["greeting"])
        self._punctuation = str(ctx.config["punctuation"])
        self._repeat = int(ctx.config["repeat"])
        ctx.logger.info("示例插件已加载：greeting=%r repeat=%s", self._greeting, self._repeat)

    def on_start(self) -> None:
        # 依赖本插件的其他插件此时都已 load 完成，可以在这里建立连接。
        self._ctx.logger.debug("示例插件已启动")

    def on_stop(self) -> None:
        # 必须幂等：停机与热重载都会调用它。
        self._ctx.logger.debug("示例插件已停止")

    def on_config_changed(self, new_config: dict[str, Any]) -> None:
        """热重载配置后内核会调用这里。不实现的话配置就要等重启才生效。"""
        self._greeting = str(new_config["greeting"])
        self._punctuation = str(new_config["punctuation"])
        self._repeat = int(new_config["repeat"])

    def greeting(self) -> str:
        """同步取一句话，方便在测试与 ``alterego plugins doctor`` 里直接用。"""
        return (self._greeting + self._punctuation) * self._repeat

    async def execute(self, intent: Any, ctx: Any) -> CapabilityResult:  # noqa: ARG002
        text = self.greeting()
        # summary 会被写进 activity_log 并在 Web 上展示 —— 所以要说人话。
        return CapabilityResult(ok=True, summary=f"说了一句「{text}」", artifacts={"text": text})

    def _on_tick_completed(self, event: Any) -> None:
        """只订阅了就一定要处理——不做事的订阅会变成噪声。

        这里记下最后一次 tick 的编号，``health()`` 会把它报出去。
        真实插件通常在这里做「攒够一批再写库」之类的事——
        每个 tick 都写一次盘是最常见的性能坑。
        """
        payload = getattr(event, "payload", None) or {}
        self._last_tick_id = str(payload.get("tick_id", ""))

    def last_tick_id(self) -> str:
        """最近一次 ``tick.completed`` 里带的 tick 编号（没有过就是空串）。"""
        return self._last_tick_id

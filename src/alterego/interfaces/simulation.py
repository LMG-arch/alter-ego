"""推演扩展点契约：阶段、能力、工具、意图类型、提示词来源。

这里的四个 ``Protocol`` 是「**不改内核就能改行为**」的全部入口。它们都由插件实现，
内核只按注册表里的名字取用。

``Intent`` 与 ``TickContext`` 属于 ``sim`` / ``domain`` 层。为了不制造反向依赖，
这里只在类型检查时导入、运行时用字符串注解。

依据: docs/design/02-plugin-api.md § 6.4–6.6、§ 7.2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol


if TYPE_CHECKING:  # pragma: no cover - 只为类型检查器存在
    from alterego.sim.context import TickContext


__all__ = [
    "Capability",
    "CapabilityResult",
    "IntentType",
    "PromptSource",
    "Stage",
    "StageResult",
    "Tool",
]


# ── 推演阶段 ────────────────────────────────────────────────


@dataclass(slots=True)
class StageResult:
    """一个阶段的执行结果。"""

    ok: bool
    #: 阶段对 ``TickContext`` 的修改，由引擎按字段合并。
    changes: dict[str, Any] = field(default_factory=dict)
    error: Exception | None = None


class Stage(Protocol):
    """推演流水线上的一个阶段。

    ``order`` 决定位置（越小越先跑），``depends_on`` 声明前置阶段名。
    某个阶段失败只会让本 tick 标记为 ``partial``，并跳过依赖它的阶段——
    不会让整个 tick 崩掉。
    """

    name: str
    order: int
    depends_on: tuple[str, ...]
    enabled: bool

    async def run(self, ctx: TickContext) -> StageResult: ...


# ── 行为能力 ────────────────────────────────────────────────


@dataclass(slots=True)
class CapabilityResult:
    """一次能力执行的产物。"""

    ok: bool
    #: 一句话说明「它干了什么」，直接写进 ``activity_log`` 并在 Web 上展示。
    #: 写成「执行成功」等于没写。
    summary: str
    artifacts: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class Capability(Protocol):
    """一种被执行的行为类型，例如「发一条动态」。"""

    id: str
    #: 这个能力能执行哪些意图，例如 ``{"post_moment"}``。
    intent_types: frozenset[str]

    async def execute(self, intent: Any, ctx: TickContext) -> CapabilityResult: ...


# ── 工具 ────────────────────────────────────────────────────


class Tool(Protocol):
    """暴露给模型的函数调用工具。"""

    name: str
    description: str
    #: JSON Schema，描述 ``invoke`` 接受的参数。
    parameters: dict[str, Any]

    async def invoke(self, **kwargs: Any) -> Any: ...


# ── 意图类型 ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class IntentType:
    """一种意图的定义。``sim`` 的意图目录就是这些对象的集合。"""

    name: str
    description: str
    category: Literal["internal", "social", "outbound"]
    default_weight: float
    #: 执行它需要哪个 :class:`Capability`；``None`` 表示仅内部状态变化。
    requires_capability: str | None = None
    #: 模型输出该意图时必须提供的参数 schema。
    parameters_schema: dict[str, Any] = field(default_factory=dict)
    #: 是否会产生对用户可见的内容。``True`` 的意图要过打扰预算。
    outbound: bool = False
    #: 对应哪一类预算：``"message"`` / ``"post"`` / ``None``。
    budget_kind: str | None = None


# ── 提示词来源 ──────────────────────────────────────────────


class PromptSource(Protocol):
    """提示词模板的来源。

    默认实现是随包分发的 ``alterego/prompts/*.md``；插件可以让提示词来自
    数据库或远端，从而实现「不改代码就调提示词」。``priority`` 大者先被问到，
    返回 ``None`` 表示「我这里没有，问下一个」。
    """

    id: str
    priority: int

    def load(self, name: str) -> str | None: ...

    def list_names(self) -> list[str]: ...

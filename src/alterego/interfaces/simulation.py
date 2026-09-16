"""推演扩展点契约：阶段、能力、工具、意图类型、提示词来源。

这里的四个 ``Protocol`` 是「**不改内核就能改行为**」的全部入口。它们都由插件实现，
内核只按注册表里的名字取用。

``Intent`` 与 ``TickContext`` 属于 ``sim`` / ``domain`` 层。为了不制造反向依赖，
这里只在类型检查时导入、运行时用字符串注解。

依据: docs/design/02-plugin-api.md § 6.4–6.6、§ 7.2
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol


if TYPE_CHECKING:  # pragma: no cover - 只为类型检查器存在
    from alterego.sim.context import TickContext


__all__ = [
    "Capability",
    "CapabilityResult",
    "Intent",
    "IntentType",
    "Percepts",
    "PromptSource",
    "Stage",
    "StageResult",
    "Tool",
]


# ── 感知 ──────────────────────────────────


@dataclass(frozen=True, slots=True)
class Percepts:
    """一个 tick 开始时它「感觉到」的东西。

    它和 :class:`~alterego.sim.context.StateSnapshot` 的区别是**视角**：
    快照是「世界现在是什么样」，感知是「这一切落到它眼里是什么」。
    分开之后，同一个世界可以有不同的感知（注意力、天气插件、被屏蔽的关键词），
    而「它没回你」就能被回答成「它没感觉到」而不是「它故意的」。

    ``extras`` 是给插件阶段留的：一个天气插件往里面塞 ``temperature`` 不需要
    改这个类，也不需要内核知道什么叫温度。键名冲突不报错——插件自己负责
    自己的命名空间（惯例是用 ``<插件名>.<字段>``）。
    """

    virtual_now: datetime
    #: 当前日程块。调 ``domain.schedule`` 时原样传过去。
    block: Any = None
    #: 用户在等它回话的条数。
    unread_messages: int = 0
    #: 距用户上次说话过了多少分钟。``None`` = 从来没说过话。
    minutes_since_last_user_message: float | None = None
    #: 距上一次它有动作过了多少分钟。刚醒来的那天可以是很大的数。
    idle_minutes: float = 0.0
    #: 插件阶段往里塞的东西。
    extras: Mapping[str, Any] = field(default_factory=dict)


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


@dataclass(frozen=True, slots=True)
class Intent:
    """一次**具体的**意图：某个 :class:`IntentType` 的一次实例。

    和 ``IntentType`` 的区别是「定义」与「这一次」：

    * ``IntentType`` 是 ``reach_out`` 这条规则本身，全局只有一份；
    * ``Intent`` 是「今天下午三点，它想找你说话，因为有点想你」这件事。

    ``parameters`` 里的键由 :attr:`IntentType.parameters_schema` 声明，
    **不做运行期校验**：参数是模型生成的自由文本，而给自由文本加 schema 校验
    只会让「它想说点什么」因为多了一个空格就变成一次失败的 tick。
    真正会被执行的参数（文件名、URL）由对应的 ``Capability`` 自己把关。

    ``urgency`` 与 ``Candidate.weight`` 是同一个数——它就是「有多急」，
    而预算规则里唯一需要它的地方是「超过 0.8 可以动用紧急额度」。
    """

    type: IntentType
    parameters: Mapping[str, Any] = field(default_factory=dict)
    urgency: float = 0.0
    #: 为什么想这么做。写进 ``activity_log`` 与 ``tick_log``。
    reason: str = ""

    @property
    def name(self) -> str:
        """意图名。``ctx.chosen_intent.type.name`` 太长了，而这个名字会到处出现。"""
        return self.type.name


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

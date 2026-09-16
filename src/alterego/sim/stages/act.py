"""Act 阶段：把「想做」变成「做了」，并留下一条能给人看的行为记录。

``order = 70``，依赖意图。

**内部意图不需要能力。** 工作、休息、吃饭这些事本身没有对外的动作，
但它们同样是「它今天做过的事」——``activity_log`` 里少了这些，
时间线上就只剩它跟你说话的那几秒，看起来像一台只会聊天的机器。

对外动作（发动态、发消息）走注册表里的 :class:`~alterego.interfaces.simulation.Capability`。
**能力找不到不算失败**：插件没装、能力被停用都是正常状态，这时候记一条
「想做但做不了」的行为，比让整个 tick 标记成 failed 有用得多。

本阶段**不调模型**。生成文本是下一阶段（``express``）的事——把「做了什么」
和「说了什么」分开，是因为前者要落进行为日志、后者要落进对话，两者的形状不同。

依据: docs/design/04-simulation-loop.md § 3.4
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from alterego.interfaces.simulation import Capability, CapabilityResult, StageResult
from alterego.kernel.registry import ServiceRegistry
from alterego.sim.context import TickContext


__all__ = ["ActStage"]


#: 往注册表里查能力时用的键。
#:
#: ``ServiceRegistry.get*`` 的形参标注是 ``type[T]``，而 ``Capability`` 是
#: ``Protocol``（抽象类），直接传进去 mypy 会报 ``type-abstract``。绕过的办法
#: 是先把类本身存进一个有具体类型标注的常量——注册表只把它当键用，从不实例化，
#: 所以「抽象」这件事在这里完全无所谓。``llm/gateway.py`` 的 ``_PROVIDER_KEY``
#: 是同一个写法。
_CAPABILITY_KEY: Final[type[Capability]] = Capability  # type: ignore[type-abstract]


@dataclass(slots=True)
class ActStage:
    """执行选中的意图。

    行为在 ``ctx.actions`` 里是**普通字典**而不是某个数据类：它下一站是
    ``activity_log.detail_json``，那个列本来就是 JSON。中间夹一个数据类
    只会多两次「对象 ↔ 字典」的搬运，而搬运的地方就是字段名写错的地方。
    """

    registry: ServiceRegistry
    name: str = "act"
    order: int = 70
    depends_on: tuple[str, ...] = ("intention",)
    enabled: bool = True

    async def run(self, ctx: TickContext) -> StageResult:
        intent = ctx.chosen_intent
        if intent is None:
            return StageResult(ok=True, changes={})

        capability = self._find(intent)
        if capability is None:
            ctx.actions.append(
                {
                    "intent": intent.name,
                    "category": intent.type.category,
                    "capability": "",
                    "ok": True,
                    "summary": self._describe(intent),
                    "artifacts": {},
                    "error": None,
                }
            )
            ctx.note(f"做了：{self._describe(intent)}")
            return StageResult(ok=True, changes={"actions": list(ctx.actions)})

        try:
            result = await capability.execute(intent, ctx)
        except Exception as exc:
            result = CapabilityResult(
                ok=False,
                summary=f"{intent.name} 没做成",
                error=f"{type(exc).__name__}: {exc}",
            )

        ctx.actions.append(
            {
                "intent": intent.name,
                "category": intent.type.category,
                "capability": str(getattr(capability, "id", "")),
                "ok": bool(result.ok),
                "summary": result.summary,
                "artifacts": dict(result.artifacts),
                "error": result.error,
            }
        )
        ctx.note(("做了：" if result.ok else "没做成：") + result.summary)
        return StageResult(ok=True, changes={"actions": list(ctx.actions)})

    # ── 内部 ──

    def _find(self, intent: Any) -> Any:
        """找出能干这件事的能力。

        ``requires_capability`` 指名道姓时按名字取；没指名就在所有能力里找
        ``intent_types`` 命中它的那个。**不做优先级仲裁**——两个能力抢同一件事
        是插件作者的配置错误，让先注册的赢比让结果随注册顺序漂移更容易查。
        """
        wanted = intent.type.requires_capability
        if wanted:
            return self.registry.get_optional(_CAPABILITY_KEY, wanted)

        for _name, candidate in self.registry.get_all(_CAPABILITY_KEY):
            types: frozenset[str] = getattr(candidate, "intent_types", frozenset())
            if intent.name in types:
                return candidate
        return None

    @staticmethod
    def _describe(intent: Any) -> str:
        """给一条没有能力执行的行为编一句话。

        用意图自己的描述而不是 ``f"做了 {intent.name}"``：后者在时间线上
        长这样「做了 reflect_internal」，而用户想看的是「心里过了一遍今天的事」。
        """
        description = str(intent.type.description).strip()
        return description or f"做了 {intent.name}"

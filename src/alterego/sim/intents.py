"""意图目录：它能想做的事，以及此刻更想做什么。

**为什么权重是算出来的，不是提示词里写出来的。** 「晚上十一点还有力气工作吗」
「刚被夸过是不是更想找人说话」——这类判断写成提示词，模型每次的答案都不一样，
``alterego why`` 也就没法解释。写成 :func:`build_candidates` 之后，
同一个 ``IntentContext`` 永远得到同一组权重（P6），而权重背后那句 ``reason``
可以原样展示给用户看。

**权重不需要归一化。** 文档 § 4.1 明说这一点：它们只是「相对更想做什么」。
选了哪一条由 :func:`choose` 按 ``ctx.rng`` 加权抽，这样同一颗种子重放同一天，
同一天里它做的每个选择都一模一样。

依据: docs/design/04-simulation-loop.md § 4
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from alterego.interfaces.simulation import IntentType


if TYPE_CHECKING:
    from random import Random


__all__ = [
    "BUILTIN_INTENTS",
    "MAX_CANDIDATES",
    "REACH_OUT_MOTIVATIONS",
    "Candidate",
    "IntentCatalog",
    "IntentContext",
    "build_candidates",
    "choose",
    "choose_reach_out_motivation",
]


#: 进抽签环节的候选上限。文档 § 4.1 定的是 5。
MAX_CANDIDATES: Final[int] = 5

#: 用户多久没说话之后，``reach_out`` 的权重开始打折。文档 § 5 给的是 24 小时。
QUIET_HOURS_BEFORE_DISCOUNT: Final[float] = 24.0

#: 打折力度。文档 § 5 明说**只是降权，不是禁止**——想找你的冲动不该被一次冷落磨平。
LONELY_DISCOUNT: Final[float] = 0.3


#: ``reach_out`` 的六种动机，与文档 § 4.2 一一对应。
#: 键是动机名（落 ``message.motivation`` 列），值是给用户看的中文。
REACH_OUT_MOTIVATIONS: Final[dict[str, str]] = {
    "share_something": "想跟你分享一件事",
    "miss_you": "有点想你",
    "need_comfort": "想找你说说话",
    "ask_question": "有个问题想问你",
    "follow_up": "上次那件事想接着问",
    "just_bored": "就是有点闲",
}

#: 内置意图目录。``research``（``budget_kind="media"``）**刻意不在里面**：
#: 文档 § 4.1 把它标成 v0.3.0+，而一个永远不可能被选中的意图只会让
#: 「它为什么没去查资料」这个问题多一个假答案。等生图/检索能力真的落地了再加。
BUILTIN_INTENTS: Final[tuple[IntentType, ...]] = (
    IntentType(
        name="work",
        description="推进手头的事：写代码、看文档、整理方案",
        category="internal",
        default_weight=0.18,
        parameters_schema={"task": "string"},
    ),
    IntentType(
        name="rest",
        description="歇一会儿：发呆、躺下、闭眼",
        category="internal",
        default_weight=0.12,
        parameters_schema={"how": "string"},
    ),
    IntentType(
        name="eat",
        description="吃饭：点外卖、下厨、出去吃",
        category="internal",
        default_weight=0.08,
        parameters_schema={"what": "string", "where": "string"},
    ),
    IntentType(
        name="commute",
        description="通勤：出门、等车、路上",
        category="internal",
        default_weight=0.05,
        parameters_schema={"from": "string", "to": "string"},
    ),
    IntentType(
        name="entertain",
        description="消遣：看剧、听歌、打游戏",
        category="internal",
        default_weight=0.10,
        parameters_schema={"what": "string"},
    ),
    IntentType(
        name="reflect_internal",
        description="心里过一遍：回想、复盘、琢磨",
        category="internal",
        default_weight=0.15,
        parameters_schema={"about": "string"},
    ),
    IntentType(
        name="socialize",
        description="和 NPC 来往：聊两句、约一下",
        category="social",
        default_weight=0.06,
        parameters_schema={"with_whom": "string", "how": "string"},
    ),
    IntentType(
        name="reply",
        description="回用户的消息",
        category="social",
        #: 有未读时会被 :func:`build_candidates` 抬到 1.0。这是**动态权重**，
        #: 所以这里的默认值只在「没有未读」时才有意义——那种情况下它基本不会被选。
        default_weight=0.0,
        outbound=True,
        budget_kind="message",
        parameters_schema={"content": "string"},
    ),
    IntentType(
        name="post_moment",
        description="发一条动态",
        category="outbound",
        default_weight=0.05,
        outbound=True,
        budget_kind="post",
        parameters_schema={"content": "string", "location": "string"},
    ),
    IntentType(
        name="reach_out",
        description="主动找用户说话",
        category="outbound",
        default_weight=0.06,
        outbound=True,
        budget_kind="message",
        parameters_schema={"motivation": "string", "content": "string"},
    ),
)


@dataclass(frozen=True, slots=True)
class IntentContext:
    """算权重时看得到的世界。

    刻意是**一份扁平快照**而不是 ``TickContext``：权重函数是纯函数，
    给它一个不能改的世界，它就没法偷偷把状态改掉；测试里也不必造一整个
    ``TickContext``（那需要 tick_id、correlation_id、rng、九种数据流……）。
    """

    #: 有未读消息时 ``reply`` 会被抬到最高优先级。
    unread_messages: int = 0
    #: 用户上次说话到现在过了多久。``None`` 表示从没说过。
    hours_since_last_user_reply: float | None = None
    #: 虚拟时间的钟点（0–23）。
    hour: int = 12
    #: 情绪效价，``-1``（低落）~ ``1``（高涨）。
    valence: float = 0.0
    #: 疲劳，``0``（精神）~ ``1``（睁不开眼）。
    fatigue: float = 0.0
    #: 当前日程块在做什么。空串表示没有排定的日程。
    activity: str = ""
    #: 现在这个日程块可不可以打断。``False`` 时它不会想发消息。
    interruptible: bool = True
    #: 上一次行为到现在过了多少分钟。连轴转之后该歇了。
    idle_minutes: float = 0.0


@dataclass(frozen=True, slots=True)
class Candidate:
    """一个候选意图，带权重和理由。"""

    intent: IntentType
    weight: float
    #: 为什么给它这个权重。写进 ``tick_log.notes``，是 ``alterego why`` 的原料。
    reason: str


class IntentCatalog:
    """意图目录。内置 10 条，插件可以往里加。

    对应 ``docs/design/02-plugin-api.md`` § 7 的 ``intent_catalog`` 扩展点。
    加进来的意图与内置的**完全平等**——没有「插件意图权重减半」这种说法，
    那会让插件作者永远在猜自己的代码为什么没生效。
    """

    def __init__(self, intents: tuple[IntentType, ...] = BUILTIN_INTENTS) -> None:
        self._items: dict[str, IntentType] = {}
        for intent in intents:
            self.add(intent)

    def add(self, intent: IntentType) -> None:
        """登记一条意图。重名直接报错。

        重名必须炸掉而不是覆盖：插件静默顶掉 ``reach_out`` 之后，
        用户看到的是「它突然会做一件不存在的事」，而日志里什么都没有。
        """
        if intent.name in self._items:
            raise ValueError(f"意图名重复：{intent.name}")
        self._items[intent.name] = intent

    def get(self, name: str) -> IntentType | None:
        return self._items.get(name)

    def names(self) -> tuple[str, ...]:
        """排序后的意图名。排序是为了让 ``--json`` 输出稳定可 diff。"""
        return tuple(sorted(self._items))

    def __iter__(self) -> Iterator[IntentType]:
        return iter(self._items.values())

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, name: object) -> bool:
        return name in self._items


def build_candidates(
    catalog: IntentCatalog,
    context: IntentContext,
) -> list[Candidate]:
    """按当前世界给每条意图算出权重与理由，**按权重降序、名字升序**返回。

    权重为 0 的意图会被丢掉：留着它们只会让「候选 5 条」这个上限被废动作占满。
    """
    candidates: list[Candidate] = []
    for intent in catalog:
        weight, reason = _weigh(intent, context)
        if weight > 0:
            candidates.append(Candidate(intent=intent, weight=weight, reason=reason))
    # 名字参与排序是为了让权重相同的意图有确定的先后（P6）。
    return sorted(candidates, key=lambda c: (-c.weight, c.intent.name))


def choose(
    candidates: list[Candidate],
    *,
    rng: Random,
    limit: int = MAX_CANDIDATES,
) -> Candidate | None:
    """从权重最高的前 ``limit`` 条里加权抽一条。

    ``candidates`` 已经是降序的，所以切片就是「取前几名」。抽签而不是取第一名，
    是因为人不会每天都选同一个最优解——只取第一名会让它变成一台机器。
    全部候选都为空时返回 ``None``：**没选中意图是合法结果**，那就是在发呆。
    """
    pool = candidates[:limit]
    if not pool:
        return None
    total = sum(c.weight for c in pool)
    if total <= 0:  # pragma: no cover - build_candidates 已经滤掉 0，防御性分支
        return pool[0]
    needle = rng.random() * total
    upto = 0.0
    for candidate in pool:
        upto += candidate.weight
        if needle < upto:
            return candidate
    return pool[-1]


def choose_reach_out_motivation(rng: Random, *, context: IntentContext) -> str:
    """按情绪与情境加权抽一个 ``reach_out`` 的动机。

    六条动机的权重规则（文档 § 4.2）:

    * ``need_comfort`` —— 情绪低落且关系够近时才出现；
    * ``miss_you`` —— 隔得久了才出现；
    * ``follow_up`` —— 上次说话还没过太久（还在同一个话题上）；
    * ``share_something`` / ``ask_question`` / ``just_bored`` —— 常备。

    权重全为 0 时回落到 ``just_bored``：它是最不冒犯的一条，
    而返回空串会让 ``message.motivation`` 变成一个没人知道含义的列。
    """
    weights: dict[str, float] = {
        "share_something": 0.3,
        "ask_question": 0.2,
        "just_bored": 0.2,
    }
    if context.valence <= -0.4:
        weights["need_comfort"] = 0.5
    if context.hours_since_last_user_reply is None or context.hours_since_last_user_reply > 8:
        weights["miss_you"] = 0.4
    if context.hours_since_last_user_reply is not None and (
        context.hours_since_last_user_reply < 4
    ):
        weights["follow_up"] = 0.4

    total = sum(weights.values())
    needle = rng.random() * total
    upto = 0.0
    for name in sorted(weights):
        upto += weights[name]
        if needle < upto:
            return name
    return "just_bored"


# ── 权重 ────────────────────────────────────────────────────


def _weigh(intent: IntentType, ctx: IntentContext) -> tuple[float, str]:
    """给一条意图算权重。

    返回 ``(0, "")`` 表示「此刻完全不会想到这件事」。写成分派而不是一张
    权重表乘以一堆系数，是因为理由不只要算得对，还要**说得出来**——
    系数连乘之后没人能回答「为什么今天 0.043」。
    """
    if intent.name == "reply":
        if ctx.unread_messages <= 0:
            return 0.0, ""
        return 1.0, f"有 {ctx.unread_messages} 条没回"

    if intent.name == "reach_out":
        weight = intent.default_weight
        reason = "想找你"
        if not ctx.interruptible:
            # 不能打断不代表不能想——只是这一 tick 不该发出去。
            # 交给预算检查去拦，这里不动权重，这样「想说但没说」还能被记下来。
            reason = "想找你（但正在忙）"
        if (
            ctx.hours_since_last_user_reply is not None
            and ctx.hours_since_last_user_reply > QUIET_HOURS_BEFORE_DISCOUNT
        ):
            weight *= LONELY_DISCOUNT
            reason = f"想找你，但你已经 {int(ctx.hours_since_last_user_reply)} 小时没理它了"
        return weight, reason

    if intent.name == "work":
        if ctx.hour < 8 or ctx.hour >= 23:
            return 0.0, ""
        weight = intent.default_weight
        reason = "有活在手上"
        if 9 <= ctx.hour < 18:
            weight *= 1.5
            reason = "工作时段，手头的活最要紧"
        if ctx.fatigue > 0.8:
            weight *= 0.3
            reason = "累到没什么心思干活"
        return weight, reason

    if intent.name == "rest":
        weight = intent.default_weight
        reason = "想歇会儿"
        if ctx.fatigue > 0.6:
            weight *= 2.0
            reason = "累了，该歇了"
        if ctx.idle_minutes > 120:
            weight *= 1.3
            reason = "坐太久了，想动一动"
        return weight, reason

    if intent.name == "eat":
        near_meal = ctx.hour in {11, 12, 17, 18, 19}
        weight = intent.default_weight * (3.0 if near_meal else 0.2)
        return weight, "到饭点了" if near_meal else "有点饿"

    if intent.name == "commute":
        if ctx.hour not in {7, 8, 9, 17, 18, 19}:
            return 0.0, ""
        return intent.default_weight * 2.0, "该出门了"

    if intent.name == "entertain":
        weight = intent.default_weight
        reason = "想找点乐子"
        if ctx.hour >= 20 or ctx.hour < 1:
            weight *= 2.0
            reason = "晚上想放松一下"
        if ctx.valence < -0.3:
            weight *= 1.5
            reason = "心情不好，想看点轻松的"
        return weight, reason

    if intent.name == "reflect_internal":
        weight = intent.default_weight
        reason = "心里有点事"
        if ctx.valence < -0.2:
            weight *= 1.8
            reason = "情绪不太对，想自己待会儿"
        if ctx.fatigue > 0.7:
            weight *= 1.5
            reason = "太累了，别的都不想想"
        return weight, reason

    if intent.name == "socialize":
        weight = intent.default_weight
        reason = "想找人聊两句"
        if ctx.valence > 0.3:
            weight *= 1.6
            reason = "心情好，想跟人说说话"
        if ctx.fatigue > 0.7:
            weight *= 0.3
            reason = "累得不想见人"
        return weight, reason

    if intent.name == "post_moment":
        weight = intent.default_weight
        reason = "发生了点想记下来的事"
        if ctx.valence > 0.4:
            weight *= 2.0
            reason = "心情不错，想发点什么"
        if ctx.valence < -0.4:
            weight *= 0.5
            reason = "心情不好，不太想让人看见"
        return weight, reason

    # 插件加进来的意图：没有专门规则就用它自己声明的默认权重。
    # 这是刻意的——一个不知道来路的意图被内核擅自调权，插件作者会很难查。
    return intent.default_weight, "按它自己声明的权重"

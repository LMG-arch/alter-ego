"""打扰预算：它有多克制。

**为什么这是一个纯函数模块。** 「它一天最多主动找你三次」这句话如果写在提示词里，
就是设计原则 P3 说的「提示词祈祷」——模型完全有能力不理。写成
:func:`check_budget` 之后，拦截发生在「意图已经选中、行为还没执行」之间，
模型没有机会绕过，而失败模式也变成「它憋住了」这种可观测、可解释的现象。

**被拦下的意图不会消失。** 降级目标写进 :attr:`BudgetDecision.downgrade_to`，
调用方把它放进 ``TickContext.suppressed``。一个人「想找你但憋住了」和
「根本没想过」是两回事：前者应该变成内心独白，也该在 ``alterego why`` 里看得到
（见 `docs/adr/0005-downgrade-instead-of-discard-suppressed-intents.md`）。

**``downgrade_to=None`` 不是「丢掉」。** 它表示「这一 tick 不换成别的行为，
但这条意图仍在 ``suppressed`` 里、仍会进待办话题」。区别在于：免打扰/熔断这类
原因是**它自己状态的问题**，把冲动转成内心活动是合理的；而最小间隔不够是
**时间不对**，转成内心活动纯属废动作——等一会儿再发就是了。

依据: docs/design/04-simulation-loop.md § 5
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final, Protocol

from alterego.interfaces.repository import BudgetUsage


if TYPE_CHECKING:
    from alterego.kernel.config import DisturbBudgetConfig


__all__ = [
    "HIGH_URGENCY_THRESHOLD",
    "NO_REPLY_CIRCUIT_HOURS",
    "BudgetDecision",
    "BudgetUsage",
    "check_budget",
    "record_no_reply",
    "record_reply",
    "record_sent",
    "roll_over",
]

#: :class:`BudgetUsage` 从接口层重新导出：它的定义在
#: ``interfaces/repository.py``——因为它是「一行 ↔ 一个对象」里的那个对象，
#: 而红线第 16 组禁止 ``storage/`` 引用 ``sim/``。**规则**留在这里，**形状**
#: 住在接口层（一个类型只该有一个家）。


#: 多紧急算「紧急」。紧急时当日主动消息上限放宽到
#: :attr:`~alterego.kernel.config.DisturbBudgetConfig.daily_message_limit_urgent`。
#:
#: **它是一个常量而不是配置项**，因为在 ``0.9`` 与 ``0.85`` 之间调来调去
#: 不会让任何人感觉到差别，而每一个配置项都要在 ``templates/alterego.toml``
#: 里有一行注释、要被设置中心的元数据覆盖（见 ADR-0010）。
#: 真有人需要调的时候再升级成配置项。
HIGH_URGENCY_THRESHOLD: Final[float] = 0.8

#: 连续未回复触发的熔断时长。文档 § 5 写的是 24 小时。
NO_REPLY_CIRCUIT_HOURS: Final[int] = 24

#: 被驳回时的降级目标：把冲动变成内心活动。
INNER = "reflect_internal"


class ScheduleBlockLike(Protocol):
    """:func:`check_budget` 眼里的日程块。

    只要求两个字段，所以 ``domain.schedule.ScheduleBlock`` 与测试用的
    替身都能直接传进来——不必为了调一次预算检查而拼出一个完整的 ScheduleBlock
    （那要编 id、persona_id、day、category……）。

    **写成只读属性而不是普通变量。** ``ScheduleBlock`` 是 frozen dataclass，
    它的字段在类型系统里是只读的；而声明成可写变量会要求实现方能赋值，
    于是 frozen 的类会被判成「不满足这个协议」——一个纯粹由写法引起的假报错。
    """

    @property
    def activity(self) -> str: ...

    @property
    def interruptible(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """一次预算检查的结论。

    Attributes:
        allowed: 放行还是拦下。
        reason: 一句话说明**为什么**。会写进 ``tick_log.notes`` 与
            ``activity_log.suppress_reason``，所以它必须是人能读懂的理由
            （「免打扰时段」），而不是「budget check failed」。
        downgrade_to: 不放行时改做什么。``None`` 表示「这一 tick 不做替代行为」，
            **不代表这条意图被丢弃**——它仍然进 ``suppressed``。
    """

    allowed: bool
    reason: str
    downgrade_to: str | None = None


# ── 纯函数 ──────────────────────────────────────────────────


def roll_over(usage: BudgetUsage, *, today: date) -> BudgetUsage:
    """跨天时把当日用量清零。

    计数器不清零，第二天它会以为「今天已经发过 3 条了」而永远不再开口——
    这是那种只在跨零点之后才出现的 bug，跑测试永远遇不到。
    """
    if usage.day == today:
        return usage
    return replace(usage, day=today, messages_sent=0, posts_sent=0)


def check_budget(
    kind: str | None,
    *,
    usage: BudgetUsage,
    now: datetime,
    config: DisturbBudgetConfig,
    urgency: float = 0.0,
    block: ScheduleBlockLike | None = None,
) -> BudgetDecision:
    """判断一类对外行为现在能不能做。

    ``kind`` 取 ``"message"`` / ``"post"`` / ``None``。``None``（以及未来可能出现的
    ``"media"``）表示**这件事不打扰任何人**——生成一张图不会弹通知，
    所以它不受打扰预算约束，只受成本预算约束（那是 ``[llm.budget]`` 的事）。

    **五条检查的顺序不可交换**（文档 § 5）。先说结论：``now`` 是虚拟时间、
    ``usage`` 是它自己那一天的用量，函数没有副作用、不看时钟、不看环境变量。

    Args:
        kind: 行为类别。
        usage: 当日用量。跨天时函数内部会先 :func:`roll_over`。
        now: 虚拟时间。
        config: ``[disturb_budget]`` 段。
        urgency: 这次行为有多紧急，``0``~``1``。超过
            :data:`HIGH_URGENCY_THRESHOLD` 才放宽消息上限。
        block: 当前所处的日程块。不可打断的块直接拦下。

    Returns:
        结论。``allowed=True`` 时 ``reason`` 说明是哪条检查放行的。
    """
    if kind not in {"message", "post"}:
        return BudgetDecision(allowed=True, reason="这类行为不打扰别人")

    usage = roll_over(usage, today=now.date())

    # ① 免打扰。先看这个，因为「凌晨三点发消息」是最容易让人反感的失败模式，
    #    而且它跟额度、间隔都无关——额度再富裕也不该在那个时候开口。
    if config.in_quiet_hours(now.time()):
        return BudgetDecision(
            allowed=False,
            reason=f"免打扰时段（{_hhmm(config.quiet_hours[0])}–{_hhmm(config.quiet_hours[1])}）",
            downgrade_to=INNER,
        )

    # ② 熔断。连续几次主动都没等到回复，它自己也该察觉了。
    #    注意熔断**只拦主动行为**：用户主动说话时要能回得上（回消息不走这条路径，
    #    见 docs/plans/2026-09-16-main-body.md § 4）。
    if usage.circuit_until is not None and now < usage.circuit_until:
        return BudgetDecision(
            allowed=False,
            reason=f"已连续 {usage.consecutive_no_reply} 次没等到回音，静默到 "
            f"{usage.circuit_until:%m-%d %H:%M}",
            downgrade_to=INNER,
        )

    # ③ 不可打断的日程。开会中途弹一条消息出去，代价不是「烦人」而是「尴尬」。
    if block is not None and not block.interruptible:
        return BudgetDecision(
            allowed=False,
            reason=f"正在「{block.activity}」，这个日程块不可打断",
            downgrade_to=INNER,
        )

    # ④ 日配额。紧急时放宽到 urgent 上限。
    limit, sent, label = _quota(kind, usage=usage, config=config, urgency=urgency)
    if sent >= limit:
        return BudgetDecision(
            allowed=False,
            reason=f"今天的{label}配额（{limit} 条）用完了",
            downgrade_to=INNER,
        )

    # ⑤ 最小间隔。同类行为挨得太近，两条消息会看起来像一个人急了。
    last = usage.last_message_at if kind == "message" else usage.last_post_at
    if last is not None:
        gap = now - last
        floor = timedelta(minutes=config.min_interval_minutes)
        if gap < floor:
            remaining = int((floor - gap).total_seconds() // 60) + 1
            return BudgetDecision(
                allowed=False,
                reason=f"距离上一条{label}才 {int(gap.total_seconds() // 60)} 分钟，再等 {remaining} 分钟",
                downgrade_to=None,
            )

    return BudgetDecision(allowed=True, reason=f"额度与间隔都允许（今天第 {sent + 1} 条{label}）")


def record_sent(usage: BudgetUsage, *, kind: str, at: datetime) -> BudgetUsage:
    """记一次「真的发出去了」。

    只在**发送成功之后**调。发送失败也记账会让它以为说过了，
    然后奇怪地安静一整天。
    """
    if kind == "message":
        return replace(usage, messages_sent=usage.messages_sent + 1, last_message_at=at)
    if kind == "post":
        return replace(usage, posts_sent=usage.posts_sent + 1, last_post_at=at)
    return usage


def record_no_reply(
    usage: BudgetUsage, *, config: DisturbBudgetConfig, at: datetime
) -> BudgetUsage:
    """记一次「发出去但没等到回复」。

    到 :attr:`~alterego.kernel.config.DisturbBudgetConfig.consecutive_no_reply_limit`
    次就熔断 :data:`NO_REPLY_CIRCUIT_HOURS` 小时，同时**把计数归零**——
    不归零的话熔断一解除会立刻再次触发，变成永久静默。
    """
    streak = usage.consecutive_no_reply + 1
    if streak >= config.consecutive_no_reply_limit:
        return replace(
            usage,
            consecutive_no_reply=0,
            circuit_until=at + timedelta(hours=NO_REPLY_CIRCUIT_HOURS),
        )
    return replace(usage, consecutive_no_reply=streak)


def record_reply(usage: BudgetUsage) -> BudgetUsage:
    """记一次「用户回话了」。

    计数归零并解除熔断。**不收时间参数**——另外两个 ``record_*`` 需要它
    是因为要算「下次什么时候能再说」，而这一条只需要把两个字段清掉。
    为了签名整齐而收一个用不到的时间，会让「它到底用没用这个时间」
    变成每次读代码都要重新确认的事。
    """
    if usage.consecutive_no_reply == 0 and usage.circuit_until is None:
        return usage
    return replace(usage, consecutive_no_reply=0, circuit_until=None)


# ── 内部 ────────────────────────────────────────────────────


def _quota(
    kind: str,
    *,
    usage: BudgetUsage,
    config: DisturbBudgetConfig,
    urgency: float,
) -> tuple[int, int, str]:
    """算出这一类行为的 ``(上限, 已用, 中文标签)``。

    中文标签回到 :class:`BudgetDecision.reason` 里：用户读到的是
    「今天的主动消息配额（3 条）用完了」，而不是「kind=message limit=3」。
    """
    if kind == "post":
        return config.daily_post_limit, usage.posts_sent, "动态"
    if urgency > HIGH_URGENCY_THRESHOLD:
        return config.daily_message_limit_urgent, usage.messages_sent, "紧急消息"
    return config.daily_message_limit, usage.messages_sent, "主动消息"


def _hhmm(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"

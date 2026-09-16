"""Reflect 阶段：把发生过的事变成情绪的变化，和几句想明白的话。

``order = 30``，依赖感知。

**先算，再问模型。** 情绪主体由 ``domain.emotion.update_emotion`` 这套纯函数
算出来——回归、冲击、疲劳三步都是确定性的。只有到了「用一句话说出为什么」
这一步才可能调模型（``emotion_update`` 模板），而且**只在有事发生的那一轮调**。

这么拆的理由很实际：一天 288 个 tick，每轮都问一次模型是 288 次调用，
而大多数 tick 里什么都没发生。让模型去确认「没事发生」既贵又不可能复现——
同样的世界跑两遍会得到两条不同的情绪曲线，那时候 P6「可复现」就成了一句空话。

依据: docs/design/04-simulation-loop.md § 3.2、docs/design/06-emotion-model.md
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from alterego.domain.emotion import (
    Emotion,
    EmotionalEvent,
    clamp_sensitivity,
    infer_label,
    update_emotion,
)
from alterego.interfaces.simulation import StageResult
from alterego.sim.context import TickContext
from alterego.sim.stages.common import elapsed_since, weekday_text


__all__ = ["REFLECT_INTERVAL_MINUTES", "ReflectStage", "baseline_emotion"]


#: 反思走的是 ``[llm.routing]`` 里的哪一个用途。
#:
#: 只用在「用一句话说出为什么」那一步，而且**只在有事发生的那一轮调**：
#: 让模型去确认「没事发生」既贵又不可能复现。
PURPOSE: Final[str] = "reflection"

#: 距上次反思超过这么久，即使什么都没发生也让它想一次。
#: 一天最多因此多出 12 次调用，换来的是「情绪曲线不会因为没人理它就冻住」。
REFLECT_INTERVAL_MINUTES: float = 120.0

#: 人设的情绪基线。**暂时是常量**：``PersonaConfig`` 目前没有情绪基线字段，
#: 而把基线塞进故事里（从职业、年龄推）会让「它天生乐观还是悲观」变成一个
#: 猜出来的答案。等 ``domain.persona`` 落地后改成从人设读（TODO(阶段 D)）。
_BASELINE_VALENCE: float = 0.05
_BASELINE_AROUSAL: float = 0.45
_DEFAULT_SENSITIVITY: float = 1.0

#: 多久没有动作就算「闲得慌」。四小时是一个工作日里最长的连续空档。
_BORED_AFTER_MINUTES: float = 240.0


def baseline_emotion(at: datetime) -> Emotion:
    """造一条基线情绪。凌晨的它和下午的它基线一样——这是刻意的简化。"""
    return Emotion(
        valence=_BASELINE_VALENCE,
        arousal=_BASELINE_AROUSAL,
        fatigue=0.0,
        label=infer_label(_BASELINE_VALENCE, _BASELINE_AROUSAL),
        updated_at=at,
    )


@dataclass(slots=True)
class ReflectStage:
    """更新情绪，产出反思。

    ``max_idle_minutes`` 之外还接受一个 ``sensitivity``：人设敏感度，
    0.5 ~ 1.5。默认 1.0 表示「不特别敏感也不特别迟钝」。
    """

    sensitivity: float = _DEFAULT_SENSITIVITY
    name: str = "reflect"
    order: int = 30
    depends_on: tuple[str, ...] = ("sense",)
    enabled: bool = True

    async def run(self, ctx: TickContext) -> StageResult:
        state = ctx.state
        current = state.emotion or baseline_emotion(ctx.virtual_now)
        baseline = baseline_emotion(current.updated_at)

        events = self._events(ctx, current)
        elapsed = elapsed_since(current.updated_at, ctx.virtual_now, timedelta(minutes=0))
        updated, reason = update_emotion(
            current=current,
            events=events,
            baseline=baseline,
            sensitivity=clamp_sensitivity(self.sensitivity),
            elapsed=elapsed,
            block=state.current_block,
        )

        ctx.reflections.append(reason)
        ctx.note(f"情绪：{updated.label}（{updated.valence:+.2f}）—— {reason}")

        await self._ask_model(ctx, updated, events)

        changes: dict[str, object] = {"state": state.evolve(emotion=updated)}
        return StageResult(ok=True, changes=changes)

    # ── 内部 ──

    def _events(self, ctx: TickContext, current: Emotion) -> list[EmotionalEvent]:
        """从感知里推出情绪事件。

        只有**看得见的原因**才变成事件。一个连原因都说不出来的情绪变化，
        在 Web 的情绪曲线上是一个没有解释的抖动——那比不画还不老实。
        """
        percepts = ctx.percepts
        if percepts is None:
            return []

        events: list[EmotionalEvent] = []
        if percepts.unread_messages:
            events.append(
                EmotionalEvent(
                    description=f"有人在等它回话（{percepts.unread_messages} 条）",
                    valence_delta=0.05,
                    arousal_delta=0.08,
                )
            )
        if percepts.idle_minutes >= _BORED_AFTER_MINUTES and not _sleeping(percepts.block):
            events.append(
                EmotionalEvent(
                    description=f"已经 {percepts.idle_minutes / 60:.1f} 小时没做什么了",
                    valence_delta=-0.03,
                    arousal_delta=0.02,
                )
            )
        if _sleeping(percepts.block) and current.fatigue > 0.3:
            events.append(
                EmotionalEvent(
                    description="在睡",
                    fatigue_delta=-0.05,
                )
            )
        return events

    async def _ask_model(
        self,
        ctx: TickContext,
        emotion: Emotion,
        events: list[EmotionalEvent],
    ) -> None:
        """让模型说一句「为什么」。**拿不到模型就跳过，不影响推演。**"""
        if ctx.llm_gateway is None or not events:
            return
        prompt = (
            f"现在是{weekday_text(ctx.virtual_now)}，"
            f"它的情绪是「{emotion.label}」（效价 {emotion.valence:+.2f}，"
            f"疲劳 {emotion.fatigue:.2f}）。\n"
            "刚刚发生的事："
            + "；".join(event.description for event in events)
            + "\n用一句话说出它此刻的感受，不要超过三十个字。"
        )
        try:
            text = await ctx.llm(PURPOSE, prompt, temperature=0.9, max_tokens=80)
        except Exception as exc:
            ctx.note(f"情绪自述没生成出来：{exc}")
            return
        if text.strip():
            ctx.reflections.append(text.strip())


def _sleeping(block: object) -> bool:
    """``block.is_sleep`` 的容错读取。日程块缺失时当作没在睡。"""
    return bool(getattr(block, "is_sleep", False))

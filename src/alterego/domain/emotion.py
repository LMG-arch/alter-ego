"""领域层：情绪。

情绪是**两个连续维度 + 一个疲劳度**，不是一个离散标签：

- `valence` 效价：-1（极负面）~ +1（极正面）
- `arousal` 唤醒度：0（平静/疲惫）~ 1（兴奋/紧张）
- `fatigue` 疲劳度：0 ~ 1

离散标签（"愉快" / "焦虑"）是从这个连续空间**推导**出来的，不是存储的。
为什么必须这样：离散情绪无法表达程度，也无法平滑变化；而「情绪惯性」
「自然回归」这类操作只在连续空间里才有意义。

四条更新规则（顺序不可交换）：

1. **自然回归** —— 情绪向基线回落，效价半衰期 4 小时、唤醒度 2 小时
2. **事件冲击** —— 外部事件直接加减
3. **情绪惯性** —— 同向事件放大（最高 1.4×），反向事件削弱（最低 0.6×）
4. **疲劳累积** —— 清醒每小时 +0.05，睡眠每小时 -0.8

顺序为什么不能换：先回归再冲击，冲击才会落在「已经平静下来的」情绪上；
反过来则等于用旧情绪算惯性，早上的一次好事会按昨晚的坏心情被削弱。

依据: docs/design/04-simulation-loop.md § 6
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final, Literal, get_args

from alterego.domain.schedule import ScheduleBlock


__all__ = [
    "AROUSAL_HALF_LIFE_HOURS",
    "DEFAULT_SENSITIVITY",
    "EMOTION_LABELS",
    "FATIGUE_GAIN_PER_HOUR",
    "FATIGUE_RECOVERY_PER_HOUR",
    "MAX_SENSITIVITY",
    "MIN_SENSITIVITY",
    "SIGNIFICANT_DELTA",
    "VALENCE_HALF_LIFE_HOURS",
    "Emotion",
    "EmotionLabel",
    "EmotionalEvent",
    "apply_event",
    "apply_with_inertia",
    "clamp",
    "clamp_sensitivity",
    "decay_toward",
    "infer_label",
    "update_emotion",
    "update_fatigue",
]

EmotionLabel = Literal["疲惫", "兴奋", "愉快", "焦虑", "低落", "警觉", "平静", "一般"]
"""由 (valence, arousal) 推导出的离散标签。"""

EMOTION_LABELS: Final[tuple[str, ...]] = get_args(EmotionLabel)

VALENCE_HALF_LIFE_HOURS: Final[float] = 4.0
"""效价自然回归的半衰期——开心/难过持续几小时。"""

AROUSAL_HALF_LIFE_HOURS: Final[float] = 2.0
"""唤醒度自然回归的半衰期——兴奋/紧张消退得比心情快。"""

FATIGUE_RECOVERY_PER_HOUR: Final[float] = 0.8
"""睡眠时每小时的疲劳恢复量。"""

FATIGUE_GAIN_PER_HOUR: Final[float] = 0.05
"""清醒时每小时的疲劳累积量——20 小时从 0 到 1。"""

SIGNIFICANT_DELTA: Final[float] = 0.02
"""低于这个幅度的回归不记入 `emotion_log.reason`。

没有这条阈值，`reason` 会塞满「情绪自然回落 +0.42→+0.41」这类噪声，
真正有信息量的变化反而被淹没。
"""

MIN_SENSITIVITY: Final[float] = 0.5
"""最迟钝的人设。"""

MAX_SENSITIVITY: Final[float] = 1.5
"""最敏感的人设。"""

DEFAULT_SENSITIVITY: Final[float] = 1.0
"""没写 `sensitivity` 的人设按中间值处理。"""

_INERTIA_COEFFICIENT: Final[float] = 0.4
"""惯性强度：当前情绪绝对值每满 1.0，效果最多放大/削弱 40%。"""

_FATIGUE_STRONG_THRESHOLD: Final[float] = 0.7
_FATIGUE_EXHAUSTED_THRESHOLD: Final[float] = 0.85


def clamp(value: float, low: float, high: float) -> float:
    """把 `value` 夹到 `[low, high]`。"""
    return max(low, min(high, value))


def clamp_sensitivity(sensitivity: float) -> float:
    """把 `sensitivity` 夹进 `[0.5, 1.5]`。

    人设由 LLM 生成，`sensitivity` 可能越界。**在装载人设时**调用它，
    而不是在 `update_emotion` 里静默取整——静默取整会把「人设写错了」
    变成「情绪有点怪」，这类 bug 能藏几个月。
    """
    return clamp(sensitivity, MIN_SENSITIVITY, MAX_SENSITIVITY)


def infer_label(valence: float, arousal: float) -> EmotionLabel:
    """从二维坐标推导离散标签：四象限映射 + 疲惫特例。

    疲惫必须放在最前面判断：低唤醒 + 非正向的组合既能落进「平静」区
    （arousal 0.2、valence 0.05），也能落进「低落」区，
    但它真正想说的是「累」。先判疲惫，再判象限。
    """
    if arousal < 0.25 and valence < 0.1:
        return "疲惫"
    if valence >= 0.3:
        return "兴奋" if arousal >= 0.6 else "愉快"
    if valence <= -0.3:
        return "焦虑" if arousal >= 0.6 else "低落"
    if arousal >= 0.6:
        return "警觉"
    if arousal <= 0.3:
        return "平静"
    return "一般"


@dataclass(frozen=True, slots=True)
class Emotion:
    """某一时刻的情绪状态。

    Attributes:
        valence: 效价，-1 ~ +1。
        arousal: 唤醒度，0 ~ 1。
        fatigue: 疲劳度，0 ~ 1。
        label: 离散标签。**词表是开放的**：降级路径由
            `infer_label(valence, arousal)` 推导，只会产出 `EMOTION_LABELS`
            里的 8 个；LLM 路径可以给出更细的词（「感动」「委屈」「恼火」）。
            见 `01-architecture.md` § 3.2。
        updated_at: 这次更新的虚拟时间。
    """

    valence: float
    arousal: float
    fatigue: float
    label: str
    updated_at: datetime

    def __post_init__(self) -> None:
        if not -1.0 <= self.valence <= 1.0:
            raise ValueError(f"valence 必须在 -1 ~ 1，得到 {self.valence!r}")
        if not 0.0 <= self.arousal <= 1.0:
            raise ValueError(f"arousal 必须在 0 ~ 1，得到 {self.arousal!r}")
        if not 0.0 <= self.fatigue <= 1.0:
            raise ValueError(f"fatigue 必须在 0 ~ 1，得到 {self.fatigue!r}")
        if not self.label:
            raise ValueError("label 不能为空")

    @property
    def is_exhausted(self) -> bool:
        """疲劳是否已到「累到不想找人」的程度（> 0.85）。"""
        return self.fatigue > _FATIGUE_EXHAUSTED_THRESHOLD

    @property
    def is_tired(self) -> bool:
        """疲劳是否已开始影响意图权重（> 0.7）。"""
        return self.fatigue > _FATIGUE_STRONG_THRESHOLD


@dataclass(frozen=True, slots=True)
class EmotionalEvent:
    """一次会冲击情绪的事件。

    `valence_delta` 是**冲击幅度**，不是结果：负值代表坏事。
    三个 delta 都可为 0——一个只让人精神一振但不动心情的事件
    （"窗外有只鸟"）只有 `arousal_delta`。

    Attributes:
        description: 人话描述，会拼进 `emotion_log.reason`。
        valence_delta: 效价变化量。
        arousal_delta: 唤醒度变化量。
        fatigue_delta: 疲劳变化量。
    """

    description: str
    valence_delta: float = 0.0
    arousal_delta: float = 0.0
    fatigue_delta: float = 0.0

    def __post_init__(self) -> None:
        if not self.description:
            raise ValueError("EmotionalEvent 必须有 description，它要写进 emotion_log.reason")


def decay_toward(
    current: float,
    baseline: float,
    elapsed: timedelta,
    half_life_hours: float,
) -> float:
    """指数回归：每过 `half_life_hours`，与基线的距离减半。

    注意「减半」的对象是**与基线的距离**，不是数值本身。
    基线 +0.2、当前 -0.6 时，一个半衰期后是 -0.2，不是 -0.3。
    """
    if half_life_hours <= 0:
        raise ValueError(f"half_life_hours 必须为正，得到 {half_life_hours!r}")
    hours = elapsed.total_seconds() / 3600
    # 显式标注：typeshed 里 `float.__pow__` 返回 Any，mypy strict 会拒绝。
    factor: float = 0.5 ** (hours / half_life_hours)
    return baseline + (current - baseline) * factor


def apply_event(current: Emotion, event: EmotionalEvent) -> Emotion:
    """事件冲击：直接加减三维，不做惯性修正。

    带惯性的路径是 `update_emotion`；这个函数给「只要一个干净的加减」
    的场合用（例如按预置强度触发一次情绪印记）。
    """
    return replace(
        current,
        valence=clamp(current.valence + event.valence_delta, -1.0, 1.0),
        arousal=clamp(current.arousal + event.arousal_delta, 0.0, 1.0),
        fatigue=clamp(current.fatigue + event.fatigue_delta, 0.0, 1.0),
    )


def apply_with_inertia(current: Emotion, valence_delta: float, sensitivity: float) -> float:
    """带惯性的效价变更，返回**新的效价值**。

    惯性系数：当前情绪越强，同向事件效果越强（最高 1.4×），
    反向事件效果越弱（最低 0.6×）。这模拟「心情好时好事更让人开心，
    但也更难被坏事影响」的黏滞感。

    Args:
        current: 变更前的情绪（只用它的 `valence`）。
        valence_delta: 效价变化量。
        sensitivity: 人设敏感度，0.5（迟钝）~ 1.5（敏感）。

    Raises:
        ValueError: `sensitivity` 越界。它来自人设，应该在装载时用
            `clamp_sensitivity()` 归一，而不是到这里才被发现。
    """
    if not MIN_SENSITIVITY <= sensitivity <= MAX_SENSITIVITY:
        raise ValueError(
            f"sensitivity 必须在 {MIN_SENSITIVITY} ~ {MAX_SENSITIVITY}，得到 {sensitivity!r}"
        )
    same_direction = (current.valence * valence_delta) > 0
    inertia = (
        1.0 + _INERTIA_COEFFICIENT * abs(current.valence)
        if same_direction
        else 1.0 - _INERTIA_COEFFICIENT * abs(current.valence)
    )
    effective = valence_delta * inertia * sensitivity
    return clamp(current.valence + effective, -1.0, 1.0)


def update_fatigue(current: float, elapsed: timedelta, block: ScheduleBlock | None) -> float:
    """疲劳累积与恢复。

    睡眠时**恢复**，清醒时**累积**——两者用同一段经过时间做系数，
    所以 `block` 必须是「这段时间内所处的日程块」。
    """
    hours = elapsed.total_seconds() / 3600
    if block is not None and block.is_sleep:
        return max(0.0, current - hours * FATIGUE_RECOVERY_PER_HOUR)
    return min(1.0, current + hours * FATIGUE_GAIN_PER_HOUR)


def update_emotion(
    current: Emotion,
    events: list[EmotionalEvent],
    baseline: Emotion,
    sensitivity: float,
    elapsed: timedelta,
    block: ScheduleBlock | None = None,
) -> tuple[Emotion, str]:
    """情绪更新的完整一步。返回新情绪与变化原因。

    顺序不可交换：**先回归 → 再冲击 → 最后疲劳**。

    Args:
        current: 本 tick 开始时的情绪。
        events: 本 tick 发生的事件，按发生顺序。
        baseline: 人设的情绪基线，回归的目标。
        sensitivity: 人设敏感度，0.5 ~ 1.5。
        elapsed: 距上次更新的虚拟时间。
        block: 这段时间所处的日程块。睡眠时段疲劳会下降；
            传 `None` 等于「这段时间没在睡」。

    Returns:
        `(新情绪, reason)`。`reason` 会写进 `emotion_log.reason`，
        它是「为什么它突然不高兴了」的唯一线索；没有显著变化时为
        `"无显著变化"`。
    """
    reasons: list[str] = []

    # 1) 自然回归
    valence = decay_toward(current.valence, baseline.valence, elapsed, VALENCE_HALF_LIFE_HOURS)
    arousal = decay_toward(current.arousal, baseline.arousal, elapsed, AROUSAL_HALF_LIFE_HOURS)
    if abs(valence - current.valence) > SIGNIFICANT_DELTA:
        reasons.append(f"情绪自然回落 {current.valence:+.2f}→{valence:+.2f}")

    # 2) 事件冲击（效价含惯性；唤醒度与疲劳按敏感度缩放）
    fatigue = current.fatigue
    for event in events:
        before = valence
        valence = apply_with_inertia(
            replace(current, valence=valence), event.valence_delta, sensitivity
        )
        arousal = clamp(arousal + event.arousal_delta * sensitivity, 0.0, 1.0)
        fatigue = clamp(fatigue + event.fatigue_delta * sensitivity, 0.0, 1.0)
        reasons.append(f"{event.description} 效价 {before:+.2f}→{valence:+.2f}")

    # 3) 疲劳
    new_fatigue = update_fatigue(fatigue, elapsed, block)

    updated = Emotion(
        valence=round(valence, 3),
        arousal=round(arousal, 3),
        fatigue=round(new_fatigue, 3),
        label=infer_label(valence, arousal),
        updated_at=current.updated_at + elapsed,
    )
    return updated, "；".join(reasons) or "无显著变化"

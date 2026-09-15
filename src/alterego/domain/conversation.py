"""对话节奏——回不回、回多快、要不要主动抛话题。

「像人」不是靠提示词里写「请像真人一样」，那是祈祷（P3）。这里给的是可测的机制：

1. **不会每条都秒回**——连着秒回够多次就强制放一次，一直秒回比人还像客服。
2. **睡觉 / 开会的时候不回**——直接沿用日程块的 `interruptible`，
   这就是机制上挡掉「凌晨三点发消息」的同一条约束，不需要另加规则。
3. **不会一直被动应答**——连着被动接够多轮就主动抛话题。
   只会被问一句答一句的不是人，是搜索框。
4. **不会复读自己**——新消息跟最近发过的比一遍，太像就打回重写。

本模块全是纯函数：不读时钟、不取随机数、不碰 IO。
`now` 和 `roll`（一个 `[0, 1)` 的随机数）都由调用方传进来，
`roll` 从 `ctx.rng.random()` 取，于是整条决策链可复现（P6）。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, get_args

from alterego.domain.emotion import Emotion
from alterego.domain.schedule import ScheduleBlock


__all__ = [
    "BUSY_EXTRA_MAX_S",
    "BUSY_EXTRA_MIN_S",
    "DELAYED_HIGH_S",
    "DELAYED_LOW_S",
    "FAST_REPLY_LIMIT",
    "INSTANT_HIGH_S",
    "INSTANT_LOW_S",
    "NORMAL_HIGH_S",
    "NORMAL_LOW_S",
    "PASSIVE_TURNS_BEFORE_TOPIC",
    "REPETITION_THRESHOLD",
    "REPLY_MODES",
    "SHORT_MESSAGE_CHARS",
    "TOPIC_COOLDOWN_MINUTES",
    "ReplyDecision",
    "ReplyMode",
    "TopicDecision",
    "bigrams",
    "decide_reply",
    "find_repetition",
    "should_open_topic",
    "similarity",
]

# ────────────────────────────────────────────────────────────
# 模式
# ────────────────────────────────────────────────────────────

ReplyMode = Literal["instant", "normal", "delayed", "much_later", "silent"]
"""这一轮该怎么回。

- `instant` 秒回
- `normal` 过一会儿正经回
- `delayed` 慢很多：有点累、心情不太好，允许敷衍
- `much_later` 到日程块结束之后（睡觉、开会）
- `silent` **这一轮不回**。调用方必须给它安排一个后续动作（醒了之后补一句），
  「不回」也要是个有交代的不回，不能凭空消失
"""

REPLY_MODES: Final[tuple[str, ...]] = get_args(ReplyMode)

# ── 延迟区间（秒）─────────────────────────────────────────

INSTANT_LOW_S: Final[float] = 1.5
INSTANT_HIGH_S: Final[float] = 6.0
NORMAL_LOW_S: Final[float] = 20.0
NORMAL_HIGH_S: Final[float] = 150.0
DELAYED_LOW_S: Final[float] = 300.0
DELAYED_HIGH_S: Final[float] = 1800.0

BUSY_EXTRA_MIN_S: Final[float] = 60.0
BUSY_EXTRA_MAX_S: Final[float] = 900.0
"""日程块结束之后的附加延迟。人醒了不会立刻摸手机，总还得赖一会儿。"""

FAST_REPLY_LIMIT: Final[int] = 3
"""连着秒回多少次之后必须放一次。"""

SHORT_MESSAGE_CHARS: Final[int] = 6
"""短于这个长度的消息按「随手回一句」处理。"""

TIRED_FATIGUE: Final[float] = 0.85
EXHAUSTED_FATIGUE: Final[float] = 0.95
LOW_MOOD_VALENCE: Final[float] = -0.5

PASSIVE_TURNS_BEFORE_TOPIC: Final[int] = 4
"""连着被动接多少轮之后该主动抛话题了。"""

TOPIC_COOLDOWN_MINUTES: Final[int] = 30
"""两次主动起话题之间的最短间隔，免得变成话痨。"""

REPETITION_THRESHOLD: Final[float] = 0.6
"""跟自己最近发过的消息相似到这个程度就算复读。"""


# ────────────────────────────────────────────────────────────
# 决策结果
# ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReplyDecision:
    """这一轮怎么回。`reason` 会写进 `event_log`，让 `alterego why` 能解释「它为什么还没回」。"""

    mode: ReplyMode
    delay: timedelta
    reason: str

    def __post_init__(self) -> None:
        if self.mode not in REPLY_MODES:
            raise ValueError(f"未知的回复模式：{self.mode}")
        if self.delay < timedelta(0):
            raise ValueError("回复延迟不能为负")
        if self.mode == "silent" and self.delay != timedelta(0):
            raise ValueError("本轮不回（silent）就不该有延迟")

    @property
    def responds(self) -> bool:
        """这一轮到底发不发。"""
        return self.mode != "silent"


@dataclass(frozen=True, slots=True)
class TopicDecision:
    """要不要主动开一个新话题。"""

    open_topic: bool
    reason: str


# ────────────────────────────────────────────────────────────
# 回不回、回多快
# ────────────────────────────────────────────────────────────


def decide_reply(
    *,
    now: datetime,
    block: ScheduleBlock | None,
    emotion: Emotion,
    text_length: int,
    consecutive_instant: int,
    roll: float,
) -> ReplyDecision:
    """决定这一轮的回法。规则按优先级短路，先命中的先算。

    Args:
        now: 虚拟现在。只在「按日程块推迟」时用到。
        block: 当前所处的日程块；`None` 表示没有日程（新角色、模板缺失）。
        emotion: 当前情绪。疲劳与效价各管一段。
        text_length: 收到的消息有多长（字符数）。
        consecutive_instant: 前面已经连着秒回了几条。
        roll: `[0, 1)` 的随机数，从 `ctx.rng.random()` 取。

    Returns:
        含模式、延迟与原因的决策。同一个 `roll` 必然得到同一个结果。
    """
    if text_length < 0:
        raise ValueError(f"消息长度不能为负，收到 {text_length}")
    if consecutive_instant < 0:
        raise ValueError(f"连续秒回次数不能为负，收到 {consecutive_instant}")
    _require_roll(roll)

    # 1) 日程块不可打断：睡觉、开会。等它结束再回。
    #    「凌晨三点发消息」就是靠这一条挡住的，不需要再写别的规则。
    if block is not None and not block.interruptible:
        return ReplyDecision(
            mode="much_later",
            delay=_seconds(
                _gap_seconds(now, block.end_at) + _jitter(BUSY_EXTRA_MIN_S, BUSY_EXTRA_MAX_S, roll)
            ),
            reason=f"正在{block.activity}，等忙完再回",
        )

    # 2) 太累：这一轮不回。调用方要给后续动作，不能让它凭空消失。
    if emotion.fatigue >= EXHAUSTED_FATIGUE:
        return ReplyDecision(mode="silent", delay=timedelta(), reason="累到不想说话，这一轮先不回")

    # 3) 有点累或心情不好：允许慢点回、允许敷衍。先回情绪，再回亲疏。
    if emotion.fatigue >= TIRED_FATIGUE or emotion.valence <= LOW_MOOD_VALENCE:
        return ReplyDecision(
            mode="delayed",
            delay=_seconds(_jitter(DELAYED_LOW_S, DELAYED_HIGH_S, roll)),
            reason="有点累或者心情不太好，慢一点回",
        )

    # 4) 连着秒回太多次了：这一次故意放一放。
    if consecutive_instant >= FAST_REPLY_LIMIT:
        return ReplyDecision(
            mode="normal",
            delay=_seconds(_jitter(NORMAL_LOW_S, NORMAL_HIGH_S, roll)),
            reason=f"连着秒回 {consecutive_instant} 条了，这一条放一放",
        )

    # 5) 短消息就当在手机上随手回一句。
    if text_length <= SHORT_MESSAGE_CHARS:
        return ReplyDecision(
            mode="instant",
            delay=_seconds(_jitter(INSTANT_LOW_S, INSTANT_HIGH_S, roll)),
            reason="短消息，随手就回了",
        )

    # 6) 默认：看见了，过一会儿正经回。
    return ReplyDecision(
        mode="normal",
        delay=_seconds(_jitter(NORMAL_LOW_S, NORMAL_HIGH_S, roll)),
        reason="看到了，过一会儿回",
    )


def should_open_topic(
    *,
    block: ScheduleBlock | None,
    emotion: Emotion,
    consecutive_passive_turns: int,
    minutes_since_last_topic: int | None,
    roll: float,
) -> TopicDecision:
    """要不要主动抛一个新话题。

    Args:
        block: 当前日程块。不可打断时一律不抛。
        emotion: 当前情绪。太累就不主动找话。
        consecutive_passive_turns: 连着多少轮都是「对方问、它答」。
        minutes_since_last_topic: 距离上次主动起话题多久；`None` 表示从来没起过。
        roll: `[0, 1)` 的随机数。

    Returns:
        抛不抛，以及为什么。
    """
    if consecutive_passive_turns < 0:
        raise ValueError(f"被动轮数不能为负，收到 {consecutive_passive_turns}")
    if minutes_since_last_topic is not None and minutes_since_last_topic < 0:
        raise ValueError(f"距上次起话题的分钟数不能为负，收到 {minutes_since_last_topic}")
    _require_roll(roll)

    if block is not None and not block.interruptible:
        return TopicDecision(False, f"正在{block.activity}，不适合主动开话题")
    if emotion.fatigue >= EXHAUSTED_FATIGUE:
        return TopicDecision(False, "太累了，没力气主动找话")
    if consecutive_passive_turns < PASSIVE_TURNS_BEFORE_TOPIC:
        return TopicDecision(
            False,
            f"才被动接了 {consecutive_passive_turns} 轮，先顺着对方的话题走",
        )
    if minutes_since_last_topic is not None and minutes_since_last_topic < TOPIC_COOLDOWN_MINUTES:
        return TopicDecision(
            False,
            f"距上次主动起话题才 {minutes_since_last_topic} 分钟，别一直换话题",
        )

    extra = consecutive_passive_turns - PASSIVE_TURNS_BEFORE_TOPIC
    chance = min(0.2 + 0.15 * extra + max(emotion.valence, 0.0) * 0.3, 0.95)
    if roll < chance:
        return TopicDecision(True, f"连着被动接了 {consecutive_passive_turns} 轮，该主动说点什么了")
    return TopicDecision(False, "虽然接了不少轮，这次先不主动换话题")


# ────────────────────────────────────────────────────────────
# 不复读自己
# ────────────────────────────────────────────────────────────


def _is_noise(char: str) -> bool:
    """空白、标点、符号都不参与「复读」判断。

    中文短句里标点占的比重大得离谱：`「好嘞~」` 和 `「好嘞！」` 若把标点算进去，
    相似度只有 0.33，反倒漏掉最常见的口头禅复读。
    Unicode 分类里 `P*` 是标点、`S*` 是符号（含 emoji 与 `~`），一起去掉。
    """
    return char.isspace() or unicodedata.category(char)[0] in {"P", "S"}


def _content(text: str) -> str:
    return "".join(char for char in text if not _is_noise(char))


def bigrams(text: str) -> frozenset[str]:
    """字符二元组集合。只保留实词字符（见 `_is_noise`）。"""
    cleaned = _content(text)
    if len(cleaned) < 2:
        return frozenset(cleaned)
    return frozenset(cleaned[index : index + 2] for index in range(len(cleaned) - 1))


def similarity(left: str, right: str) -> float:
    """二元组 Jaccard 相似度，`0.0`~`1.0`。

    选这个而不是编辑距离或 embedding：它是**标准库就能算、可复现、可解释**的，
    并且它只看用词不看语序——正好是「复读」的特征。
    判断语义重复不归它管，那是模型的事。
    """
    left_grams = bigrams(left)
    right_grams = bigrams(right)
    if not left_grams and not right_grams:
        return 1.0
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def find_repetition(
    candidate: str,
    recent: Sequence[str],
    *,
    threshold: float = REPETITION_THRESHOLD,
) -> str | None:
    """`candidate` 跟最近发过的哪一条最像？没超过阈值就返回 `None`。

    返回的是**最像的那一条原文**，调用方可以把它塞进重写提示词——
    「换一种说法，不要又是这句」比「请不要重复」有用得多。
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"阈值必须在 0~1 之间，收到 {threshold}")

    best: tuple[float, str] | None = None
    for old in recent:
        score = similarity(candidate, old)
        if score >= threshold and (best is None or score > best[0]):
            best = (score, old)
    return best[1] if best is not None else None


# ────────────────────────────────────────────────────────────
# 内部工具
# ────────────────────────────────────────────────────────────


def _require_roll(roll: float) -> None:
    if not 0.0 <= roll < 1.0:
        raise ValueError(f"roll 必须落在 [0, 1)，收到 {roll}")


def _jitter(low: float, high: float, roll: float) -> float:
    return low + (high - low) * roll


def _seconds(value: float) -> timedelta:
    return timedelta(seconds=round(value))


def _gap_seconds(start: datetime, end: datetime) -> float:
    """`end - start` 的秒数，负数按 0 处理（已经结束了就没有「等它结束」可言）。"""
    return max((end - start).total_seconds(), 0.0)

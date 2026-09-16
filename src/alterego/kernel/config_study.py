"""``[study]`` —— 专项学习的配置。

**为什么这个段不在 ``kernel/config.py`` 里。** 那个文件已经 898 行，而上限是
900（`scripts/check_architecture.sh` 的第 23 项）。再加一个配置类进去，
不是「稍微超一点」，是把自己定的红线划掉了——而那条红线的意义恰恰在于
**它是硬的**：一旦允许「就这一次」，下一个 900 行就会变成 1200 行。

所以从这一版起，配置段的定义可以住在自己的模块里，由 ``config.py``
import 进来挂到 :class:`~alterego.kernel.config.Config` 上。
``kernel/config_values.py`` 早就是这个形状了——它只装转换规则，
这里装一个段的字段。

⚠️ **``config.py`` 现在是 900/900，一个字的余量都没有。**
下一个配置段要么先从这里挪走一段，要么另开一个卫星模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from alterego.kernel.errors import ConfigError


__all__ = ["StudyConfig"]


DEFAULT_ROUNDS: Final[int] = 1
"""一次 ``study next`` 学几个主题。"""

DEFAULT_RECALL_LIMIT: Final[int] = 3
"""讨论到专业话题时最多翻出几篇笔记。"""

DEFAULT_MIN_SCORE: Final[float] = 2.0
"""低于这个分不算命中。0 就是「兼容一切」，等于每次都硬塞。"""


@dataclass(frozen=True)
class StudyConfig:
    """``[study]`` —— 角色自己深耕的那个方向。

    Attributes:
        field: 学什么。**留空表示按人设的 ``occupation`` 认。**
            认不出来时命令会直接说「不知道该学什么」并告诉你去哪儿填，
            而不是随便挑一个方向开始学——学错方向的代价是它写出一堆
            像模像样的空话，而没人会想到去检查。
        rounds: 一次学几个主题。默认 1，也就是一天一个。
        recall_limit: 讨论到专业话题时最多翻出几篇笔记。调高会让
            它更像「什么都懂」，但也更容易把当前对话本身挤掉。
        min_score: 召回的分数门槛。调低会翻出更多擦边笔记；
            调到 0 等于每次对话都硬塞专业笔记，那比不调用更糟。
    """

    field: str = ""
    rounds: int = DEFAULT_ROUNDS
    recall_limit: int = DEFAULT_RECALL_LIMIT
    min_score: float = DEFAULT_MIN_SCORE

    def __post_init__(self) -> None:
        _require_positive("rounds", self.rounds)
        _require_positive("recall_limit", self.recall_limit)
        if self.min_score < 0:
            raise ConfigError(
                "配置项不能为负数",
                key="min_score",
                value=self.min_score,
                hint="门槛是分数，分数没有负的。想关掉召回就把 recall_limit 调成 1 并提高 min_score。",
            )


def _require_positive(name: str, value: float) -> None:
    """必须为正数。

    布尔值排除在外： ``True`` 在 Python 里就是 ``1``，而 ``rounds = true``
    显然不是「学一轮」的意思。这类错必须报出来，不能悄悄当成 1——
    `kernel/config.py` 的同名函数是同一条理由。

    Raises:
        ConfigError: 值为零、负数或不是数字。
    """
    if not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0:
        return
    raise ConfigError("配置项必须为正数", key=name, value=value)

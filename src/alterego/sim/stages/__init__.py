"""推演的六个阶段。

一个 tick 就是把这六个阶段按 ``order`` 跑一遍，每个阶段只做一件事：

===== ===== ============== ==========================================
阶段  order ``depends_on`` 做了什么
===== ===== ============== ==========================================
sense 10    ``()``         把世界读成一份 ``Percepts``
reflect 30   ``sense``      情绪随时间回归，再被事件撞一下
intention 50 ``reflect``    算出此刻最想做的事，然后过一遍预算闸门
act     70   ``intention``  真的去做，留下一条行为
express 90   ``act``        把它做成的事说成人话
persist 110  ``express``    翻译成要落库的行
===== ===== ============== ==========================================

中间那些 order（20 / 25 / 40 / 60 / 80）是留给插件的——
``04-simulation-loop.md`` § 2.1 的插入点表规定了哪些位置可以插。
插件阶段通过 ``alterego.plugins`` 入口点注册，与内置阶段**完全平等**：
引擎只看 ``order``，不看它是谁写的。

**阶段是纯的。** 它们不读配置、不连数据库、不开事务（P1 的推论）。
需要什么就从构造器拿、从 ``ctx`` 拿，产出什么就写进 ``ctx`` 或
``StageResult.changes``。这让「一次 tick 为什么这么做」可以被完整重放，
也让每个阶段可以单独被测——不需要先装一个数据库。

依据: docs/design/04-simulation-loop.md § 2、§ 3
"""

from __future__ import annotations

from alterego.sim.stages.act import ActStage
from alterego.sim.stages.express import ExpressionStyle, ExpressStage
from alterego.sim.stages.intention import IntentionStage
from alterego.sim.stages.persist import PersistStage
from alterego.sim.stages.reflect import ReflectStage, baseline_emotion
from alterego.sim.stages.sense import SenseStage


__all__ = [
    "ActStage",
    "ExpressStage",
    "ExpressionStyle",
    "IntentionStage",
    "PersistStage",
    "ReflectStage",
    "SenseStage",
    "baseline_emotion",
]

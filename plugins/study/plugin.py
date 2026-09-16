"""专项学习插件。

它做的事情很少，而且**故意的**。

## 为什么它这么薄

「按课程表一格一格学本行」拆开看是四步：认出学什么 → 决定这一格学哪个面 →
调模型写一篇笔记 → 把进度记回去。这四步插件一步都做不了：

- 插件拿不到 ``StorageBackend``——人设（``occupation``）在库里，而那是
  组装根（``cli*.py``）才认识的东西（``scripts/check_architecture.sh`` 第 3 组红线）；
- 它也不能自己开文件：写笔记要跨知识库的目录白名单；
- 它更不能自己调模型：调模型必须经过 ``ctx.llm()``（第 21 组红线），
  而召回要用内核自己的二字组切词，那一段的职责是「任何环境下逐字节一致」。

所以真正的编排在 ``domain/study.py``（纯函数：展开课程表、按一句话挑笔记）
与 ``sim/study.py``（编排 + 落盘进度），由 ``alterego study`` 命令驱动。
这个插件负责的是**另外那件事**：让内核知道「这个实例会按课程表学本行」。

这不是敷衍。插件系统的职责是**声明**——声明这个能力在这里、它现在健不健康。
把编排塞进插件只会得到一份拿不到数据库、也调不动模型的代码。

## 它为什么一个配置项都没有

唯一会改变学习行为的四个值（``field`` / ``rounds`` / ``recall_limit`` /
``min_score``）全在 ``[study]`` 段里，``alterego study`` 直接读那里。
在这里再抄一份，就是一份设置的**两个真源**：改插件这份不会改命令的行为，
改命令那份又不会改插件说的话——两边都「看起来对」，而其中一个是谎话
（设计原则 P4：可插拔优于可配置）。

同一个理由，``cli_study.py`` 的模块 docstring 已经为「库在哪」拒绝过一次：
「库在哪」有两个真源时，两条命令会往不同的目录里写，而用户要到很后面才发现。

所以清单里**没有** ``[plugin.config.*]`` 段。那是留白，不是没写完。

## 它订阅了什么

**什么都没订阅。**

一次 ``study next`` 要调模型、要写文件，按秒算，而且花的是用户的钱。
挂到 ``tick.completed`` 上等于让它自己夜夜开花——用户第二天会发现账单，
却不一定想得起来是自己昨晚打开了哪个开关。
想做「定时学」的话，那是调度器的事（``kernel/scheduler.py``），不是这里。

## 它不保证的事情

``describe()`` 只说**学什么、旋钮在哪、进度看哪儿**，不说「学到第几格了」。
那是磁盘上的事实（``00-索引/`` 里的进度文件），而插件不该去碰磁盘——
``alterego study status`` 回答这个问题，两半合起来才是完整答案。

依据: docs/adr/0012-specialized-study-is-a-curriculum-not-a-prompt.md
"""

from __future__ import annotations

from typing import Any

from alterego.interfaces.common import HealthStatus
from alterego.interfaces.simulation import Capability, CapabilityResult
from alterego.kernel.plugin import Plugin, PluginContext


class Study(Plugin):
    """按课程表一格一格补本行，写进知识库的「60-专业」。"""

    #: 空集合是**故意的**：推演循环不会挑中它。
    #:
    #: 理由见模块 docstring。这个插件不是「一次行为」，是一个由用户
    #: 按需触发的、要花钱的批处理入口——它该由人来按，不该由 tick 来选。
    intent_types: frozenset[str] = frozenset()

    id: str = "capability.study"

    def on_load(self, ctx: PluginContext) -> None:
        # 归属自动记在本插件名下（ADR-0007），不要传 owner。
        ctx.registry.register(Capability, self, name="study")

        self._ctx = ctx
        ctx.logger.info("专项学习插件已加载；学什么、一次学几格看 [study] 段")

    def on_start(self) -> None:
        self._ctx.logger.debug("专项学习插件已启动")

    def on_stop(self) -> None:
        # 必须幂等：停机与热重载都会调用它。这个插件不持有资源，所以只是打日志。
        self._ctx.logger.debug("专项学习插件已停止")

    def on_config_changed(self, new_config: dict[str, Any]) -> None:
        """配置变了就说一声——但它的旋钮不在这里。

        内核允许用户在 ``[plugins.config."capability.study"]`` 下写任何键，
        只警告不报错（插件可能故意读清单外的键）。走到这里的键都不是本插件
        声明的，所以**既不记也不抛**：接了就会在下游变成「改了配置却没生效」，
        而真正的原因是那四个旋钮在 ``[study]`` 段里。
        """
        self._ctx.logger.info(
            "专项学习插件的配置块变了（%d 个键），但它的旋钮在 [study] 段里",
            len(new_config),
        )

    def describe(self) -> str:
        """一句话说明这个实例怎么学、旋钮在哪。"""
        return (
            "专项学习：方向由人设的 occupation 认（认不出就不学，不编一个方向），"
            "一次一格写进知识库的 60-专业；"
            "学几格、门槛、召回条数看 [study] 段，学到哪了问 `alterego study status`。"
        )

    def health(self) -> HealthStatus:
        """永远报「正常」，把状态写在 ``detail`` 里。

        这个插件的职责是**说明**，不是「配了才健康」。「还没开始学」
        「occupation 认不出方向」都不是插件的问题——前者是进度，在磁盘上；
        后者由 ``alterego study next`` 当场告诉用户该去哪儿填 ``field``。
        把它报成 ``ok=False`` 会让 ``alterego plugins doctor``
        在一件没出错的事情上报警。
        """
        return HealthStatus(ok=True, detail=self.describe())

    async def execute(self, intent: Any, ctx: Any) -> CapabilityResult:
        """报一句声明，外加几个供机器读的字段。

        ``intent_types`` 是空的，所以推演循环不会调到这里；它是给
        ``alterego plugins list``、Web 的插件页和测试用的。
        """
        return CapabilityResult(
            ok=True,
            summary=self.describe(),
            artifacts={
                "config_section": "study",
                "state_command": "alterego study status",
                "paid_command": "alterego study next",
                "free_commands": "plan,recall,status",
            },
        )

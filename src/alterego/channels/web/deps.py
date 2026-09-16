"""路由要用到的所有东西，装在一个盒子里。

**为什么单独一层。** ``app.py`` 要 import 路由（把 router 挂上去），路由要
拿到配置、仓储、渠道、广播器——如果它们互相 import，就是一对循环依赖。
把"装着这些东西的盒子"放在第三个文件里，两个方向就都只依赖它。
这不是分层洁癖，是为了让 ``import alterego.channels.web.app`` 这个动作
在任何顺序下都成立。

**仓储是可选的（``None``）。** ``alterego serve`` 可以先只开页面、库里还没
数据，或者测试只想验一条设置路由。缺什么就让那条路由答 503 并说清缺什么，
比让整个应用起不来好——"页面能开，但统计页说库没接上"是一个可用的状态，
而"进程直接退出"不是。

**注入的是 Protocol，不是具体实现。** 所以这个文件一行 ``sqlite`` 都不用写，
``channels/`` 也就不会因为知道存储是什么而被架构检查拦住。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from alterego.channels.web.auth import AuthGate
from alterego.channels.web.plugin import WebChannel
from alterego.channels.web.sse import SSEBroker
from alterego.interfaces.repository import (
    ActivityRepository,
    BudgetRepository,
    ConversationRepository,
    EmotionRepository,
    MemoryRepository,
    PersonaRepository,
    ScheduleRepository,
    SocialPostRepository,
    SourceRepository,
    TickLogRepository,
)
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.config import Config
from alterego.kernel.logging import get_logger
from alterego.kernel.registry import ServiceRegistry


__all__ = ["UtteranceHandler", "WebDeps"]

_log = get_logger("channels.web")

#: 「用户说了一句话，让引擎回一句」。
#:
#: 参数是 ``(文本, 会话 id)``，返回已经摊平的回复视图。**故意是一个函数而不是
#: 引擎对象**：路由不该知道 ``ConversationService`` 存在，更不该知道它需要
#: 十个仓储、一个网关和一份提示词库。谁把这些凑齐是组装根的事
#: （``cli_serve.py``），这里只要一个能 ``await`` 的东西。
UtteranceHandler = Callable[[str, str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class WebDeps:
    """一次 Web 请求可能用到的全部依赖。"""

    config: Config
    auth: AuthGate
    broker: SSEBroker
    channel: WebChannel
    registry: ServiceRegistry
    logger: logging.Logger = _log
    #: 插件管理器。没有它，``/api/plugins`` 答 503——页面能开，只是看不到插件。
    manager: Any = None
    #: 当前角色。由组装根解析后注入（可能还没建人设，所以允许空串）。
    persona_id: str = ""
    #: 用户当前对话的那个会话 id。空串表示"还没定"，路由会去问仓储。
    conversation_id: str = ""
    #: 把一句话交给引擎。``None`` 表示引擎没装配（没库、没配模型）。
    respond: UtteranceHandler | None = None

    personas: PersonaRepository | None = None
    conversations: ConversationRepository | None = None
    posts: SocialPostRepository | None = None
    emotions: EmotionRepository | None = None
    activities: ActivityRepository | None = None
    memories: MemoryRepository | None = None
    budgets: BudgetRepository | None = None
    schedules: ScheduleRepository | None = None
    tick_logs: TickLogRepository | None = None
    sources: SourceRepository | None = None

    @property
    def web(self) -> Any:
        """``[web]`` 段的快捷方式。路由里出现 ``deps.config.web.page_size`` 太长。"""
        return self.config.web

    @property
    def now(self) -> datetime:
        """按**配置的时区**取此刻。

        不用 ``datetime.now().astimezone()``：那读的是**进程的**时区，装的可能是
        UTC；而 ``core.timezone`` 才是「它的几点」。两者在开发机上恰好一致
        （都装着东八区），所以本地永远看不出区别——在 UTC 的容器里则差 8 小时，
        而 :func:`alterego.channels.web.routes.stats.budget` 拿这个时刻取**日期**：
        东八区早上 8 点之前，UTC 还停在昨天，于是那一页显示的是**昨天**的额度。

        ``cli.py::_today`` 与 ``kernel/scheduler.py`` 早就按配置时区取时间了，
        ``channels/`` 是漏掉的那一处。``scripts/check_architecture.sh`` 第 5 组
        现在拦这个——这条规则只能靠人记住的时候，它就不是规则。
        """
        return datetime.now(resolve_timezone(self.config.core.timezone))

    @property
    def today(self) -> date:
        """按配置的时区取今天。

        与引擎同口径：``budget_usage.day`` 是**当地日期**（见
        ``sim/stages/common.py::day_key``），而 ``/api/budget`` 读的就是那一列。
        两边算的不是同一天时，页面会显示一个引擎从没写过的那天，
        而症状是「它明明发过消息，额度却显示 0」。
        """
        return self.now.date()

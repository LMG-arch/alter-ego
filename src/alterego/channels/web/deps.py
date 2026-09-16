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

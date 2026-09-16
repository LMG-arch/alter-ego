"""``/api/stats``、``/api/budget``、``/api/sources`` —— 统计页、预算页、信息源页。

**这一页上的每个数字都是「窗口内」的，不是总量。** 仓储层没有任何
``count()`` / ``stats()`` 方法——``MemoryStats`` 这个领域类型从写下来到现在
**一次都没被构造过**（``domain/memory.py`` 里那份注释说的 ``stats(persona_id)``
并不存在）。所以总量要么靠把整张表拉进内存，要么靠一条只读 SQL；
两条路都得先动 ``interfaces/repository.py``，属于接口审计那一批。

于是这里回答的是另一个——而且是统计页真正想看的问题：**这一个月里它活了多少**。
响应里带 ``window_days`` 与 ``since``，前端也照着这个口径写标题
（「近 30 天」而不是「总共」）。一个说不清口径的数字比没有数字更糟。

**预算按虚拟日算。** 推演可以快进，所以「今天」指的是**它自己的今天**。
``serve`` 不跑推演时虚拟时间与墙上时间一致，于是默认值取墙上日期；
真跑了快进之后，要查哪一天应当由调用方用 ``?day=`` 明确指定。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query

from alterego.channels.web.routes.base import Deps, PageLimit, require
from alterego.channels.web.views import budget_view, source_view


__all__ = ["DEFAULT_WINDOW_DAYS", "MAX_SOURCE_AGE_DAYS", "router"]

router = APIRouter(prefix="/api", tags=["stats"])

#: 统计窗口默认多久。30 天：短到「这个月它过得怎么样」还看得清，
#: 长到能盖住一次假期或一阵子忙。
DEFAULT_WINDOW_DAYS: Final[int] = 30

#: 信息源往回找多久。180 天：它读过的东西寿命比记忆短，
#: 一年前的文章在这一页上只是噪声。
MAX_SOURCE_AGE_DAYS: Final[int] = 180

#: 统计时一次最多取多少行。取不到底时数字会偏小，所以响应里带上
#: ``truncated``——一个诚实的「至少这么多」好过一个假的确切值。
SCAN_LIMIT: Final[int] = 2000


@router.get("/stats")
def stats(
    deps: Deps,
    days: Annotated[int, Query(ge=1, le=365)] = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """窗口内的活跃度：发了多少、做了多少、说了多少、记住了多少。"""
    now = deps.now
    since = now - timedelta(days=days)
    payload: dict[str, Any] = {
        "window_days": days,
        "since": since.isoformat(),
        "at": now.isoformat(),
        "truncated": False,
        "posts": 0,
        "activities": 0,
        "messages_in": 0,
        "messages_out": 0,
        "memories": 0,
        "memory_kinds": {},
        "sources": 0,
    }
    if not deps.persona_id:
        return payload

    if deps.posts is not None:
        payload["posts"] = len(deps.posts.list_recent(deps.persona_id, limit=SCAN_LIMIT))

    if deps.activities is not None:
        activity_rows = deps.activities.list_range(
            deps.persona_id, since=since, until=now, limit=SCAN_LIMIT
        )
        payload["activities"] = len(activity_rows)
        payload["truncated"] = payload["truncated"] or len(activity_rows) >= SCAN_LIMIT

    if deps.conversations is not None and deps.conversation_id:
        # 变量名不与上面那个共用：两个仓储回的是两种记录，共用一个名字时
        # 类型检查器只能记住第一个，后面每处属性访问都变成“猜”。
        messages = deps.conversations.list_messages(
            deps.conversation_id, limit=SCAN_LIMIT, before=now
        )
        window = [record for record in messages if (record.created_at or now) >= since]
        payload["messages_in"] = sum(1 for record in window if record.direction == "inbound")
        payload["messages_out"] = sum(1 for record in window if record.direction == "outbound")

    if deps.memories is not None:
        memories = deps.memories.list_recent(deps.persona_id, since=since, limit=SCAN_LIMIT)
        kinds: dict[str, int] = {}
        for memory in memories:
            kinds[memory.kind] = kinds.get(memory.kind, 0) + 1
        payload["memories"] = len(memories)
        payload["memory_kinds"] = kinds

    if deps.sources is not None:
        kept = deps.sources.list_kept(deps.persona_id, since=since, limit=SCAN_LIMIT)
        payload["sources"] = len(kept)

    return payload


@router.get("/budget")
def budget(
    deps: Deps,
    day: date | None = None,
) -> dict[str, Any]:
    """那天用掉了多少额度，以及有没有被拦下来的。

    ``circuit_until`` 不为空表示**它正在主动闭嘴**：联系不上你太多次之后
    会自己停一段时间（见 ``04-simulation-loop.md`` 的打扰预算）。这一页的
    用处就是回答「它怎么不理我了」——所以那个字段要在首屏看得见。
    """
    budgets = require(deps.budgets, "预算存储")
    if not deps.persona_id:
        return {"day": None, "usage": None}
    # 不用 ``date.today()``：它读的是这台机器的今天。
    # 而 ``deps.today`` 读的是 ``core.timezone``——引擎写 ``budget_usage.day``
    # 用的就是那个口径（``sim/stages/common.py::day_key``），两边必须同一天。
    target = day or deps.today
    usage = budgets.load(deps.persona_id, day=target)
    return {"day": target.isoformat(), "usage": budget_view(usage)}


@router.get("/sources")
def sources(
    deps: Deps,
    limit: PageLimit = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """它读到的东西，最近抓到的在前。

    只给**留下来的**那些（``list_kept``）：被过滤掉的中间产物
    （提示词注入、过长、重复）不是它「搜集到的东西」，把它们混进来会让
    这一页看起来像它整天在读垃圾。
    """
    repo = require(deps.sources, "信息源存储")
    if not deps.persona_id:
        return {"items": [], "total": 0}

    since = deps.now - timedelta(days=MAX_SOURCE_AGE_DAYS)
    records = repo.list_kept(deps.persona_id, since=since, limit=limit + offset)
    ordered = list(reversed(records))
    return {
        "items": [source_view(record) for record in ordered[offset : offset + limit]],
        "total": len(records),
    }

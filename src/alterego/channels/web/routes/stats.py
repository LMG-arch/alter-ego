"""``/api/stats``、``/api/stats/tokens``、``/api/budget``、``/api/sources``。

**这一页上的每个数字都是「窗口内」的，不是总量。** 仓储层没有任何
``count()`` / ``stats()`` 方法——``MemoryStats`` 这个领域类型从写下来到现在
**一次都没被构造过**（``domain/memory.py`` 里那份注释说的 ``stats(persona_id)``
并不存在）。所以总量要么靠把整张表拉进内存，要么靠一条只读 SQL；
两条路都得先动 ``interfaces/repository.py``，属于接口审计那一批。

于是这里回答的是另一个——而且是统计页真正想看的问题：**这一个月里它活了多少**。
响应里带 ``window_days`` 与 ``since``，前端也照着这个口径写标题
（「近 30 天」而不是「总共」）。一个说不清口径的数字比没有数字更糟。

**token 那一组是例外：它真的在 SQL 里聚合。** ``/api/stats`` 把行拉回内存再
``len()``，因为它数的是几十上百条；而 ``llm_usage`` 是**每次模型调用一行**，
一个月上万行，把行搬进 Python 只为求三个和是这一页唯一会真长起来的地方。
所以 ``/api/stats/tokens`` 走 :class:`~alterego.interfaces.repository.UsageRepository`
（实现里是一条 ``GROUP BY`` SQL）。两个接口因此口径不同：
前者可能带 ``truncated``，后者的数字是精确的。

**预算按虚拟日算。** 推演可以快进，所以「今天」指的是**它自己的今天**。
``serve`` 不跑推演时虚拟时间与墙上时间一致，于是默认值取墙上日期；
真跑了快进之后，要查哪一天应当由调用方用 ``?day=`` 明确指定。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query

from alterego.channels.web.routes.base import Deps, PageLimit, require
from alterego.channels.web.views import budget_view, source_view, usage_total_view
from alterego.interfaces.repository import UsageGroup


__all__ = [
    "DEFAULT_TOKEN_DAYS",
    "DEFAULT_WINDOW_DAYS",
    "MAX_SOURCE_AGE_DAYS",
    "MAX_USAGE_GROUPS",
    "router",
]

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

#: token 那一组默认看多久。1 天，而 ``/api/stats`` 默认 30 天：
#: 两个窗口服务两个问题。「这一个月它活了多少」看 30 天，
#: 「现在烧得快不快」看 1 天——共用一个默认值会让今天的一次失控
#: 被一个月的总量平掉，而那正是最该被看见的东西。
DEFAULT_TOKEN_DAYS: Final[int] = 1

#: 一次最多回几组。它**不是** ``SCAN_LIMIT`` 那种「取不到底就偏小」的截断：
#: 行数在 SQL 里就定了，这里只是不让响应无限大。按天分组时 365 天刚好 365 组，
#: 碰不到 200；真碰到时响应里会说 ``truncated``。
MAX_USAGE_GROUPS: Final[int] = 200


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


@router.get("/stats/tokens")
def tokens(
    deps: Deps,
    days: Annotated[int, Query(ge=1, le=365)] = DEFAULT_TOKEN_DAYS,
    group: UsageGroup = "purpose",
) -> dict[str, Any]:
    """窗口内烧了多少 token，以及烧在哪了。

    **失败的那几次也算在内。** 账本对每一次**尝试**记一行（``success=0`` 的
    也记），而一次被限流、重试三次才成的调用，确实花了三倍的钱——
    把失败的行滤掉会让这一页比账单好看，而它的作用恰恰是解释账单。
    ``failed`` 单独给一个字段，所以「贵是因为贵」与「贵是因为在重试」
    能分开看。

    **没有金额。** 账本里的 ``cost_usd`` 恒为 0（本项目还没有价目表），
    所以这里一个 ``$`` 都不给——未配置单价时显示「—」而不是「$0.00」。
    这一页要加金额，先要有价目表（见 ``09-observability.md`` § 2.2）。

    **没有进度条/预算余量。** ``llm.budget.max_tokens_per_day`` 是按**虚拟日**
    重置的，而这里是滚动窗口（``now - days``，和 ``/api/stats`` 同一个口径）。
    两者相除会得到一个看着很像进度条、其实分子分母不是一回事的东西。
    真要显示余量，得先问出「它自己的今天」——那是 ``deps.today``，
    和这个窗口不是一件事，所以分开做。

    ``group=day`` 时 ``key`` 是 ``YYYY-MM-DD``。它取的是**写下来那一天**
    （``substr(created_at, 1, 10)``，带时区偏移的那个串），不做 UTC 换算：
    换算会把东八区凌晨的调用记到前一天。
    """
    usages = require(deps.usages, "用量账本")
    now = deps.now
    since = now - timedelta(days=days)
    payload: dict[str, Any] = {
        "window_days": days,
        "since": since.isoformat(),
        "at": now.isoformat(),
        "group": group,
        "truncated": False,
        "totals": {
            "calls": 0,
            "failed": 0,
            "succeeded": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "groups": [],
    }
    if not deps.persona_id:
        return payload

    rows = usages.totals(deps.persona_id, since=since, until=now, group=group)
    # 合计在**截断之前**算：截掉的是「列出来的组」，不是「窗口里花了多少」。
    # 顺序反过来的症状是分组行加不出合计——用户手动验算一次就会以为这页在骗人，
    # 而骗的其实只是它自己的加法。
    #
    # 合计由分组相加得出，不再单独查一次：分组的定义就是「窗口的一个划分」，
    # 两处各算一遍的结果迟早会在某次过滤条件改动后分叉。
    payload["totals"] = {
        "calls": sum(row.calls for row in rows),
        "failed": sum(row.failed for row in rows),
        "succeeded": sum(row.calls - row.failed for row in rows),
        "prompt_tokens": sum(row.prompt_tokens for row in rows),
        "completion_tokens": sum(row.completion_tokens for row in rows),
        "total_tokens": sum(row.total_tokens for row in rows),
    }
    if len(rows) > MAX_USAGE_GROUPS:
        rows = rows[:MAX_USAGE_GROUPS]
        payload["truncated"] = True
    payload["groups"] = [usage_total_view(row) for row in rows]
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

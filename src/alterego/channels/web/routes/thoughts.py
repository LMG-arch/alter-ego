"""``/api/thoughts``、``/api/memory`` —— 内心页与记忆页。

**这两页故意不合并。** 「它想了什么」看的是最近几天的内心独白与情绪，
「它记得什么」看的是一整条记忆（含重要度、强度、被回忆过几次）。
把它们并成一页会让两个完全不同的时间尺度打架：内心是「刚才」，
记忆是「这一年」。

**这一页现在读得到什么、读不到什么。** 读得到：行为的 ``inner_voice``
（它当时心里那句话）、记忆、情绪。读不到：``tick_log`` 里那些**候选意图**
（它想过要做但没做的事）——那份数据一直在写（``TickLogRepository.append``），
但**没有读接口**，而且它住在 ``storage/sqlite`` 里，``channels/`` 不能直接读
（架构红线第 3 组）。要开这一页的「上一轮它想了什么」，得先给
``TickLogRepository`` 加一个只读方法，那是接口审计那一批的事。

把它说清楚比留一个空页面好：用户看到「内心」这一页只有记忆和情绪，
会以为功能没做完；看到页脚写着「候选意图的读取接口还没有」，他就知道
这是什么状态，以及为什么。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query

from alterego.channels.web.routes.base import Deps, PageLimit, require
from alterego.channels.web.views import activity_view, memory_view
from alterego.domain.memory import MemoryKind


__all__ = ["MAX_WINDOW_DAYS", "router"]

router = APIRouter(prefix="/api", tags=["thoughts"])

#: 内心独白往回找多久。7 天：再往前，`inner_voice` 读起来的语境已经变了，
#: 而这一页要的是「它最近在想什么」。
THOUGHT_WINDOW: Final[timedelta] = timedelta(days=7)

#: 记忆往回找多久。10 年是个「实际上不设限」的数：``MemoryRepository.list_recent``
#: 要求 ``since``，而记忆的起点就是它出生的那天。
MAX_WINDOW_DAYS: Final[int] = 3650


@router.get("/thoughts")
def thoughts(
    deps: Deps,
    limit: PageLimit = 50,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> dict[str, Any]:
    """它最近心里说的话。

    只取 ``inner_voice`` **非空**的行为：做过的事到处都有（时间线那一页），
    而这里要的是那些它当时心里嘀咕了一句的时刻。空列表是正常结果——
    有些行为它就是闷头做完的。
    """
    activities = require(deps.activities, "行为日志")
    if not deps.persona_id:
        return {"items": [], "days": days}

    now = datetime.now().astimezone()
    window = now - timedelta(days=days)
    records = activities.list_range(deps.persona_id, since=window, until=now, limit=500)
    voiced = [record for record in records if record.inner_voice.strip()]
    return {
        "days": days,
        "items": [activity_view(record) for record in voiced[-limit:]],
    }


@router.get("/memory")
def memory(
    deps: Deps,
    limit: PageLimit = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    kind: MemoryKind | None = None,
) -> dict[str, Any]:
    """记得的东西，最近发生的在前。

    ``kind`` 交给仓储筛（``MemoryRepository.list_recent`` 有 ``kind`` 参数），
    不在这里过一遍——那是它的职责，而且它那边有索引。类型直接写成
    ``MemoryKind``：FastAPI 会拿它生成一个只有三个合法值的下拉筛选项，
    而写成 ``str`` 就只能等仓储收到一个不认识的 kind 时才报错。

    ``offset`` 在内存里切，理由同 ``/api/feed``：量级还没到需要动协议的时候。
    """
    memories = require(deps.memories, "记忆存储")
    if not deps.persona_id:
        return {"items": [], "total": 0}

    since = datetime.now().astimezone() - timedelta(days=MAX_WINDOW_DAYS)
    records = memories.list_recent(
        deps.persona_id,
        since=since,
        kind=kind,
        limit=limit + offset,
    )
    # 仓储给的是「最早的在前面」，这一页要的是「最近的在前面」：记忆的时间尺度
    # 长，正序会让首屏永远是它刚出生那几天的事。
    ordered = list(reversed(records))
    return {
        "items": [memory_view(item) for item in ordered[offset : offset + limit]],
        "total": len(records),
    }

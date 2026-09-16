"""``/api/feed`` —— 动态页与时间线页。

**「动态」与「时间线」是两页，不是一个查询的两个视图。** 动态是它**发出去了
什么**（有文案、有配图、有发布时的情绪）；时间线是它**经历了什么**
（起床、通勤、被扣下的那条消息、发出去的那条动态）。两条数据线分别是
``social_post`` 与 ``activity``，合并渲染只是为了别让人在两个页面之间来回切。

**点赞能写，评论不能。** ``SocialPostRepository`` 只有 ``append``（同 id 再写
一次是覆盖，也就是 upsert），所以点赞 = 读出那条、把 ``like_count`` 加一、
用同一个 id 写回去——**不新增任何写接口**。评论要存一段文本，得有地方放它
（一张 ``comment`` 表，或一条入站 ``message``），那是动 schema 的事，
与本批「把主体接通」不是一回事，留到接口审计那一批一起做。

**时间线里的活动不带 ``inner_voice`` 之外的推断。** 页面上「它当时想」那句
就是库里那一句，前端不做任何加工；加工过的内心独白读起来很像真的，
但那是页面编的，不是它想的。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, Query

from alterego.channels.web.routes.base import (
    Deps,
    PageLimit,
    bad_request,
    not_found,
    require,
    unavailable,
)
from alterego.channels.web.views import activity_view, post_view


__all__ = ["MAX_DAYS", "router"]

router = APIRouter(prefix="/api/feed", tags=["feed"])

#: 时间线最多往回看多少天。与 ``/api/stats`` 的窗口不是一个概念：
#: 那里是「统计一段」，这里是「翻到哪一天」。90 天够翻，再多就该用检索了。
MAX_DAYS: Final[int] = 90


@router.get("")
def feed(
    deps: Deps,
    limit: PageLimit = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """最近的动态，按发布时间倒序。

    ``offset`` 是在内存里切的：``SocialPostRepository`` 没有 offset 参数，
    而一个人的动态在可预见的将来是几百到几千行——为它加一个参数要动协议、
    实现和每个调用方，现在不值。真到十万行时该分页的是仓储，不是这里。

    **回 ``has_more`` 而不是 ``total``。** 仓储只有 ``list_recent(limit=)``，
    取多少就有多少，数出来的「总数」其实是「这次取了几条」——用户会看到
    「共 30 条」，翻一页变成「共 60 条」。同一个名字指两个东西是最难查的
    一类 bug，所以这一页不叫 total；别的列表页（``/api/memory``、``/api/sources``）
    一次就把窗口取完，它们的 ``total`` 是真的，就继续叫 total。
    """
    posts = require(deps.posts, "动态存储")
    if not deps.persona_id:
        return {"items": [], "offset": 0, "limit": limit, "has_more": False}
    # 多要一条：多出来的那一条只用来回答「还有没有」。
    records = posts.list_recent(deps.persona_id, limit=limit + offset + 1)
    window = records[offset : offset + limit]
    return {
        "items": [post_view(record) for record in window],
        "offset": offset,
        "limit": limit,
        "has_more": len(records) > offset + limit,
    }


@router.get("/timeline")
def timeline(
    deps: Deps,
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = 3,
    limit: PageLimit = 60,
) -> dict[str, Any]:
    """它这些天经历了什么：行为与动态合成一条时间线。

    ``days`` 的默认值 3 是「一眼能看完的一小段」：给 7 天时返回的首屏里
    大半是三天前的东西，而人在这一页想看的通常是「刚发生了什么」。
    """
    now = deps.now
    since = now - timedelta(days=days)
    items: list[dict[str, Any]] = []

    if deps.activities is not None and deps.persona_id:
        items.extend(
            {"kind": "activity", **activity_view(record)}
            for record in deps.activities.list_range(
                deps.persona_id, since=since, until=now, limit=limit
            )
        )
    if deps.posts is not None and deps.persona_id:
        items.extend(
            {"kind": "post", **post_view(record)}
            for record in deps.posts.list_recent(deps.persona_id, limit=limit)
            if record.posted_at >= since
        )

    items.sort(key=lambda item: item["at"] or "", reverse=True)
    return {"since": since.isoformat(), "days": days, "items": items[:limit]}


@router.post("/{post_id}/like")
def like(deps: Deps, post_id: str) -> dict[str, Any]:
    """给一条动态点个赞。

    「赞」是模拟出来的社会反馈里唯一一个**用户能直接参与**的量，所以它值得
    一个写接口；而写它不需要新协议——``append`` 的 upsert 语义就是一次更新
    （见模块头）。返回新的计数，省掉前端再拉一次列表。
    """
    posts = require(deps.posts, "动态存储")
    if not deps.persona_id:
        raise bad_request("还没有人设", hint="先跑 alterego init 建一个。")

    target = next(
        (
            record
            for record in posts.list_recent(deps.persona_id, limit=200)
            if record.id == post_id
        ),
        None,
    )
    if target is None:
        raise not_found(f"没有这条动态：{post_id}")

    updated = replace(target, like_count=target.like_count + 1)
    posts.append(updated)
    return {"id": post_id, "like_count": updated.like_count}


@router.post("/{post_id}/comment")
def comment(post_id: str, payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    """写一条评论。**现在只校验，不落库。**

    与其假装成功（``comment_count += 1`` 而没有那条评论），不如直说还没有
    地方放它：库里没有评论表，仓储也没有写它的方法。假装成功之后，
    用户会以为评论存下来了——那种错要到「刷新之后评论不见了」时才暴露。

    **这里不要 ``deps``。** 落库那天它会需要 ``deps.posts``，但今天它一个
    仓储都不碰；为一个还没写的实现挂一个用不上的参数，读的人会以为
    下面某处已经在用了。
    """
    text = str(payload.get("text", "")).strip()
    if not text:
        raise bad_request("评论是空的")
    # 与其假装成功（``comment_count += 1`` 而没有那条评论），不如直说还没地方放它：
    # 库里没有评论表，仓储也没有写它的方法。假装成功要到「刷新之后评论不见了」
    # 才会暴露，而那时用户已经不再相信这一页了。
    raise unavailable(
        f"评论还没有地方存（{post_id}）",
        hint="补一张评论表要动 schema 与仓储接口，排在接口一致性那一批。",
    )

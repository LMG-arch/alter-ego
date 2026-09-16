"""路由之间共用的那一点点东西。

放这里而不是放进 ``deps.py``：``deps.py`` 一行 FastAPI 都不该 import，
否则「WebDeps 能不能在没装 fastapi 的机器上构造」就变成了一个悬着的问题——
而 ``tests/test_web_*.py`` 里有一半的测试只想验依赖盒子本身。
"""

from __future__ import annotations

from typing import Annotated, Any, TypeVar

from fastapi import Depends, HTTPException, Query, Request

from alterego.channels.web.deps import WebDeps
from alterego.channels.web.sse import SSEBroker, TooManySubscribers


__all__ = [
    "MAX_PAGE",
    "Deps",
    "PageLimit",
    "bad_request",
    "deps_of",
    "not_found",
    "require",
    "subscribe",
    "unavailable",
]

T = TypeVar("T")

#: 一次请求最多要多少条。上限不是「防攻击」——它防的是把整个库拉进浏览器，
#: 那会让页面卡住而用户以为程序死了。
MAX_PAGE = 200


def deps_of(request: Request) -> WebDeps:
    """从 ``app.state`` 里取依赖盒子。

    **不放在模块级全局。** 一个进程里可能有两个 ``create_app()``
    （测试里就是），全局变量会让它们互相串台，而症状是「B 测试看到 A 测试的配置」。
    """
    deps: WebDeps | None = getattr(request.app.state, "deps", None)
    if deps is None:  # pragma: no cover - 只有手工乱拼 app 才会走到
        raise HTTPException(status_code=500, detail="这个应用没有装配依赖")
    return deps


#: 路由签名里写 ``deps: Deps`` 就行。
Deps = Annotated[WebDeps, Depends(deps_of)]

#: ``limit`` 查询参数的统一定义：1..MAX_PAGE 之间。
#:
#: **默认值写在 ``=`` 后面，不放进 ``Query(...)`` 里。** FastAPI 明确拒绝
#: 「``Annotated`` 里的 ``Query`` 带着 default」（装 0.13x 上直接抛
#: ``AssertionError``，**应用在 import 阶段就起不来**），所以默认值只能由
#: 每条路由自己写；写在参数那一行旁边，读的人也不用跳到另一个文件去看。
#: 各条路由的默认值不必相同：``limit`` 默认 30 条消息、``timeline`` 默认 60 条，
#: 是因为这两页一屏能放下多少本来就不一样。
PageLimit = Annotated[int, Query(ge=1, le=MAX_PAGE)]


def unavailable(what: str, hint: str = "") -> HTTPException:
    """503：这个能力现在用不了。

    用 503 而不是 500，是因为它**不是程序错了**：库没接上、模型没配、
    插件管理器没启——都是「装配的时候少了点什么」。页面上要据此显示
    「这个页面暂时没有数据」，而不是一个红色的报错。
    """
    detail: dict[str, Any] = {"error": "unavailable", "what": what}
    if hint:
        detail["hint"] = hint
    return HTTPException(status_code=503, detail=detail)


def bad_request(what: str, hint: str = "") -> HTTPException:
    """400：这次请求本身不成立（空消息、时间格式不对、范围反了）。"""
    detail: dict[str, Any] = {"error": "bad_request", "what": what}
    if hint:
        detail["hint"] = hint
    return HTTPException(status_code=400, detail=detail)


def not_found(what: str) -> HTTPException:
    """404：这个 id 在库里没有。"""
    return HTTPException(status_code=404, detail={"error": "not_found", "what": what})


def require(value: T | None, what: str, *, hint: str = "") -> T:
    """拿到一件必需的东西，没有就答 503。

    ``assert value is not None`` 不行：断言会被 ``-O`` 去掉，而那时
    下面那行会抛 ``AttributeError``，用户看到的是一个 500 与一段栈，
    而不是「会话存储没接上」。
    """
    if value is None:
        raise unavailable(what, hint)
    return value


def subscribe(broker: SSEBroker) -> Any:
    """订阅广播，订阅数满了就答 503 并说清上限。

    上限到了不是「服务器崩了」，是「你开了太多标签页」——两者要分开说，
    否则用户会去重启进程，而其实只要关掉一个页面。
    """
    try:
        return broker.subscribe()
    except TooManySubscribers as exc:
        raise unavailable(
            f"同时连着的页面太多（上限 {broker.max_connections} 个）",
            hint="关掉几个开着这个页面的标签页再刷新",
        ) from exc

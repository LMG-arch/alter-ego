"""``/api/chat`` —— 对话页。

**这一页是内联的，不是事件驱动的。** 用户按下发送之后，页面直接拿 POST 的
响应把两个气泡画出来；同一个回复**不会**再从 SSE 里来一遍。这么做不是偷懒：
如果回复既走响应又走广播，「同一个气泡渲染两遍」这个 bug 就只能在浏览器里
复现，而它的表现是「偶尔重复」，最难查。SSE 推的是**它主动说的那些话**
（走 :meth:`~alterego.channels.web.plugin.WebChannel.send`）——那些是这个
页面没有发起、也就无从从响应里拿到的。

**``POST /api/chat/send`` 会等模型。** 一句回复要好几十秒（要拼提示词、要过
网关、可能要重试），请求就悬着。这是有意的：v1 是单人本地界面，
「让它慢慢想，想好了把气泡画出来」比「先返回一个占位气泡、再用另一个通道
把内容补上」少一整类状态。页面上给一个「它在写…」的提示就够了。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, Request
from fastapi.responses import StreamingResponse

from alterego.channels.web.routes.base import (
    Deps,
    PageLimit,
    bad_request,
    require,
    subscribe,
    unavailable,
)
from alterego.channels.web.sse import KEEPALIVE
from alterego.channels.web.views import message_view


__all__ = ["router"]

router = APIRouter(prefix="/api/chat", tags=["chat"])

#: 一条消息最长多少字。**不是**配置项：它挡的是「浏览器把一段 200KB 的文本
#: 贴进来」，而那个长度与它想说什么无关。真需要长文时它会自己开一个 Markdown
#: 文件，不靠聊天框。
MAX_TEXT: Final[int] = 4000

_ENGINE_HINT: Final[str] = (
    "没有可用的库、或者模型没配好时，serve 只开页面、不接引擎。先跑 alterego doctor 看缺什么。"
)


@router.get("/history")
def history(
    deps: Deps,
    limit: PageLimit = 30,
    before: datetime | None = None,
) -> dict[str, Any]:
    """最近的那些消息，**按时间正序**。

    ``before`` 往更早翻页：前端把最早那条的时间传回来，就能往前接一段。
    正序是有意的——聊天页是从上往下读的；让接口倒序、由浏览器再翻回来，
    会把「新消息该插在哪一头」变成一个需要判断的问题。
    """
    conversations = require(
        deps.conversations,
        "会话存储",
        hint="库还没接上（data 目录或 ALTEREGO_DB 的问题），这一页暂时读不到会话。",
    )
    if not deps.conversation_id:
        # 还没定下「用户会话是哪一个」。这不是错误：人设刚建好、还没说过话时
        # 就是这个状态，页面该显示一句「还没聊过」，而不是一个错误框。
        return {"conversation_id": "", "messages": []}
    records = conversations.list_messages(deps.conversation_id, limit=limit, before=before)
    return {
        "conversation_id": deps.conversation_id,
        "messages": [message_view(record) for record in records],
    }


@router.post("/send")
async def send(
    deps: Deps,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """说一句，等它回一句。

    「它这一轮不回」是一个**正常结果**（响应里 ``reply`` 为 ``null``），
    不是错误：状态码仍然是 200，``reason`` 里写着为什么。
    把它做成 4xx 会逼前端为「它选择不说话」写一段错误处理。
    """
    text = str(payload.get("text", "")).strip()
    if not text:
        raise bad_request("消息是空的", hint="白空格不算消息；想结束对话不用发东西。")
    if len(text) > MAX_TEXT:
        raise bad_request(
            f"一条消息最多 {MAX_TEXT} 个字",
            hint="要给它看长文，存成文件再告诉它路径。",
        )
    respond = deps.respond
    if respond is None:
        raise unavailable("引擎没装配", hint=_ENGINE_HINT)

    outcome = await respond(text, deps.conversation_id)
    return {"text": text, **outcome}


@router.get("/stream")
async def stream(deps: Deps, request: Request) -> StreamingResponse:
    """SSE：它主动说的话往这里推。

    断线重连由浏览器负责（``EventSource`` 自带），所以这里不写重连逻辑——
    但心跳仍然要有：中间的代理会让**长时间没有字节**的连接超时，那时浏览器
    收到的是一次「重连」，用户看到的是它的话晚了几秒才出现。
    """
    sub = subscribe(deps.broker)
    keepalive = float(deps.broker.keepalive_seconds)

    async def frames() -> AsyncIterator[str]:
        try:
            while not await request.is_disconnected():
                frame = await sub.next_frame(keepalive)
                yield KEEPALIVE if frame is None else frame
        finally:
            # 客户端一断开就要还回去。少了这一句，「开过 20 个标签页」之后
            # 第 21 个永远连不上，而那些标签页早就关了。
            deps.broker.unsubscribe(sub)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

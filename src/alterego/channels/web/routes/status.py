"""``/api/health``、``/api/auth``、``/api/status``、``/api/emotion`` —— 总览页与登录。

**``/api/health`` 与 ``/api/auth`` 不要求认证。** 这不是疏忽：页面自己在
拿到 token 之前就得能问出「你还活着吗」，而 ``/api/auth`` 正是那个交 token 的地方。
两条都只回答「服务在不在、要什么凭证」，不含任何它的私人内容。

它们与其余路由**在同一个 router 里**，公开与否由
:data:`alterego.channels.web.app.PUBLIC_PATHS` 那一个集合决定。
不做成两个 router：那样「这条路由安全吗」就取决于它当年被挂到了哪个
router 上，而挂错既不报错也不警告——默认拒绝、例外写在一处才看得住。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, HTTPException, Query, Response

from alterego.channels.web.auth import AuthGate
from alterego.channels.web.routes.base import Deps, bad_request, require
from alterego.channels.web.views import activity_view, emotion_view, health_view, persona_view
from alterego.interfaces.common import HealthStatus


__all__ = ["COOKIE_MAX_AGE", "RECENT_WINDOW", "router"]

#: 登录后 cookie 活多久。30 天是「自家机器上的一个页面」该有的长度：
#: 短到每天都要贴一次 token 只会让人把 token 写进浏览器书签，那更糟。
COOKIE_MAX_AGE: Final[int] = 30 * 24 * 3600

#: 总览页「最近在做什么」回溯多久。
RECENT_WINDOW: Final[timedelta] = timedelta(hours=6)


router = APIRouter(prefix="/api", tags=["status"])


@router.get("/health")
def health(deps: Deps) -> dict[str, Any]:
    """服务活着吗、要什么凭证、它那边怎么样。

    **不返回任何它的私人内容**（心情、消息、记忆都没有）：这条路由不需要认证，
    所以它只能说「我是谁、我怎么进门」这类信息。
    """
    channel = deps.channel.health_check()
    return {
        "ok": True,
        "auth": {"mode": deps.auth.mode, "required": deps.auth.required},
        "channel": {
            "id": deps.channel.id,
            "ok": channel.ok,
            "detail": channel.detail,
            "readers": deps.broker.subscribers,
        },
        "persona_id": deps.persona_id,
        "version": 1,
    }


@router.post("/auth")
def auth(
    deps: Deps,
    response: Response,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """交 token（或密码），换一个 cookie。

    认证关了的时候也照常返回成功：前端不必先读 ``/api/health`` 才知道
    该不该弹登录框——它只要在 401 时才弹。
    """
    gate: AuthGate = deps.auth
    if not gate.required:
        return {"ok": True, "mode": "none"}

    presented = str(payload.get("token") or payload.get("password") or "").strip()
    if not presented:
        raise bad_request("没有给凭证", hint="把启动时打印的那个 token 贴进来。")
    if not gate.check(presented):
        # 401 而不是 403：它要的不是「换个身份」，是「先证明你是这台机器的主人」。
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "what": "token 不对"},
        )

    response.set_cookie(
        gate.cookie_name,
        presented,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        # 本地是 http，所以不能设 secure——设了浏览器会拒绝存这个 cookie，
        # 而症状是「登录成功了，但刷新之后又要登」。
        secure=False,
        path="/",
    )
    return {"ok": True, "mode": gate.mode}


@router.get("/status")
def status(deps: Deps) -> dict[str, Any]:
    """总览：它是谁、现在什么心情、在做什么、有没有人在等、插件还好吗。

    **一个请求里拿齐**，因为这一页是启动时第一眼看到的：分成六个请求会让
    页面上先出现六块「正在加载」，而它们其实是一次查询能答完的东西。
    """
    now = deps.now
    payload: dict[str, Any] = {
        "persona": None,
        "emotion": None,
        "unread": 0,
        "recent_activity": [],
        "plugins": {},
        "channel": {},
        "at": now.isoformat(),
    }

    if deps.personas is not None and deps.persona_id:
        record = deps.personas.get(deps.persona_id)
        payload["persona"] = persona_view(record) if record is not None else None

    if deps.emotions is not None and deps.persona_id:
        emotion = deps.emotions.latest(deps.persona_id)
        payload["emotion"] = emotion_view(emotion) if emotion is not None else None

    if deps.conversations is not None and deps.conversation_id:
        payload["unread"] = deps.conversations.count_unread(deps.conversation_id)

    if deps.activities is not None and deps.persona_id:
        records = deps.activities.list_undistilled(
            deps.persona_id, since=now - RECENT_WINDOW, limit=20
        )
        payload["recent_activity"] = [activity_view(record) for record in records[-5:]]

    if deps.manager is not None:
        payload["plugins"] = health_view(deps.manager.health_all())

    channel: HealthStatus = deps.channel.health_check()
    payload["channel"] = {"ok": channel.ok, "detail": channel.detail, "hint": channel.hint}
    return payload


@router.get("/emotion")
def emotion(
    deps: Deps,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> dict[str, Any]:
    """它现在的心情。

    ``days`` 目前**只被回显**，不参与查询：``EmotionRepository`` 只有
    ``latest``，没有按时间取一串的方法，所以还画不出曲线。留着这个参数是因为
    前端已经按「可以要一段」写好了；等仓储补上 ``list_range``，这里改成
    返回 ``history`` 即可，前端不用动。

    （这不是一句「以后再说」的敷衍：曲线要的数据 ``emotion_log`` 已经在库里了，
    缺的只是一条只读 SQL。补它要动 ``interfaces/repository.py`` 与 storage，
    与本批「把主体接通」不是一件事。）
    """
    emotions = require(deps.emotions, "情绪存储")
    if not deps.persona_id:
        return {"days": days, "latest": None, "history": []}
    latest = emotions.latest(deps.persona_id)
    return {
        "days": days,
        "latest": emotion_view(latest) if latest is not None else None,
        "history": [],
    }

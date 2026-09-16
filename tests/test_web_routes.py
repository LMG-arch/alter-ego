"""``alterego serve`` 的那些 HTTP 路由。

**这里测的是「接口的形状」，不是「界面好不好看」。** 每条路由都有三种结局：
答得出来（200）、这次请求不成立（400/404）、现在用不了（503）。第二种与第三种
分得清是最要紧的一件事——把它们合成一个 500，用户就永远不知道是
「我打错了」还是「它还没准备好」。

**仓储全部是假的。** 真仓储要一个 sqlite 文件、一次迁移、还有写入顺序，
而这里要问的问题是「没有仓储时答什么」「有仓储时字段怎么摆」。假仓储只实现
路由真正调用的那几个方法：多实现一个方法就等于在测一份没人读的代码。

**为什么不用 ``starlette.testclient.TestClient``。** 装在本机的 starlette 1.3
在 **import 那一刻**就为「用 httpx 而不是 httpx2」发一条
``StarletteDeprecationWarning``，而本项目的 pytest 把警告当错误
（``filterwarnings = ["error"]``），于是整个文件在**收集阶段**就挂了——
不是某条用例失败，是一条都跑不到。与其用 ``catch_warnings`` 把它按下去，
不如直接用**我们自己的**依赖（``httpx``，项目里仅有的两个必需依赖之一）
驱动同一个 ASGI 应用：``ASGITransport`` 走的是完整的中间件、依赖注入与
异常处理链，与真服务器看到的路径完全一样，而且不受 starlette 换 API 的影响。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from alterego.channels.web.app import create_app
from alterego.channels.web.auth import AuthGate
from alterego.channels.web.deps import WebDeps
from alterego.channels.web.plugin import CHANNEL_ID, WebChannel
from alterego.channels.web.sse import SSEBroker
from alterego.domain.emotion import Emotion
from alterego.domain.memory import Memory
from alterego.interfaces.repository import (
    ActivityRecord,
    BudgetUsage,
    MessageRecord,
    PersonaRecord,
    SocialPostRecord,
    SourceRecord,
)
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.config import Config
from alterego.kernel.registry import ServiceRegistry


TZ = resolve_timezone("Asia/Shanghai")
NOW = datetime(2026, 9, 16, 20, 30, tzinfo=TZ)
TOKEN = "sesame"


# ═══════════════════════════════════════════════════════════════════════
#  假仓储
# ═══════════════════════════════════════════════════════════════════════


class _FakeConversations:
    def __init__(self, messages: list[MessageRecord] | None = None, unread: int = 0) -> None:
        self.messages = list(messages or [])
        self.unread = unread
        self.asked: list[dict[str, Any]] = []

    def list_messages(
        self, conversation_id: str, *, limit: int = 50, before: datetime | None = None
    ) -> list[MessageRecord]:
        self.asked.append({"conversation_id": conversation_id, "limit": limit, "before": before})
        return self.messages[:limit]

    def count_unread(self, conversation_id: str) -> int:
        return self.unread

    def ensure(self, record: Any) -> Any:  # pragma: no cover - 路由不写
        return record

    def append(self, message: MessageRecord) -> None:  # pragma: no cover - 路由不写
        self.messages.append(message)

    def last_inbound_at(self, conversation_id: str) -> datetime | None:  # pragma: no cover
        return None


class _FakePosts:
    def __init__(self, posts: list[SocialPostRecord] | None = None) -> None:
        self.posts = list(posts or [])
        self.appended: list[SocialPostRecord] = []

    def list_recent(self, persona_id: str, *, limit: int = 20) -> list[SocialPostRecord]:
        return self.posts[:limit]

    def append(self, post: SocialPostRecord) -> None:
        self.appended.append(post)
        self.posts = [post if other.id == post.id else other for other in self.posts]


class _FakeActivities:
    def __init__(self, records: list[ActivityRecord] | None = None) -> None:
        self.records = list(records or [])
        self.windows: list[tuple[datetime, datetime]] = []

    def list_range(
        self, persona_id: str, *, since: datetime, until: datetime, limit: int = 500
    ) -> list[ActivityRecord]:
        self.windows.append((since, until))
        return self.records[:limit]

    def list_undistilled(
        self, persona_id: str, *, since: datetime, limit: int = 200
    ) -> list[ActivityRecord]:
        return [record for record in self.records if record.started_at >= since][:limit]

    def mark_distilled(self, activity_ids: Any, at: datetime) -> None:  # pragma: no cover
        return None

    def append(self, records: Any) -> None:  # pragma: no cover
        return None


class _FakeMemories:
    def __init__(self, memories: list[Memory] | None = None) -> None:
        #: 仓储给的是**正序**（最早的在前面），路由负责翻过来。
        self.memories = list(memories or [])

    def list_recent(
        self,
        persona_id: str,
        *,
        since: datetime,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        window = [item for item in self.memories if kind is None or item.kind == kind]
        return window[:limit]

    def get(self, memory_id: str) -> Memory | None:  # pragma: no cover
        return next((item for item in self.memories if item.id == memory_id), None)

    def save(self, memory: Memory) -> None:  # pragma: no cover
        self.memories.append(memory)

    def save_many(self, memories: Any) -> None:  # pragma: no cover
        return None

    def mark_consolidated(self, memory_ids: Any) -> None:  # pragma: no cover
        return None

    def set_importance(self, updates: Any) -> None:  # pragma: no cover
        return None


class _FakeEmotions:
    def __init__(self, emotion: Emotion | None = None) -> None:
        self.emotion = emotion
        self.written: list[Emotion] = []

    def latest(self, persona_id: str) -> Emotion | None:
        return self.emotion

    def append(self, emotion: Emotion, **kwargs: Any) -> None:  # pragma: no cover
        self.written.append(emotion)


class _FakePersonas:
    def __init__(self, record: PersonaRecord | None = None) -> None:
        self.record = record

    def get(self, persona_id: str) -> PersonaRecord | None:
        return self.record

    def find_by_name(self, name: str) -> PersonaRecord | None:  # pragma: no cover
        return self.record

    def list_all(self) -> list[PersonaRecord]:  # pragma: no cover
        return [self.record] if self.record is not None else []

    def document(self, persona_id: str) -> Any:  # pragma: no cover
        return {}


class _FakeBudgets:
    def __init__(self, usage: BudgetUsage | None = None) -> None:
        self.usage = usage
        self.saved: list[BudgetUsage] = []

    def load(self, persona_id: str, *, day: Any) -> BudgetUsage:
        return self.usage if self.usage is not None else BudgetUsage(day=day)

    def save(self, persona_id: str, usage: BudgetUsage) -> None:  # pragma: no cover
        self.saved.append(usage)


class _FakeSources:
    def __init__(self, records: list[SourceRecord] | None = None) -> None:
        self.records = list(records or [])

    def list_kept(
        self, persona_id: str, *, since: datetime, limit: int = 100
    ) -> list[SourceRecord]:
        return self.records[:limit]


class _Discovery:
    """一份发现结果。真实现里 ``get`` 是给重载走的捷径——路由靠它把
    「没发现」与「发现了但没装起来」分开：前者 404，后者 503。"""

    def __init__(self, plugins: dict[str, Any], failed: list[Any] | None = None) -> None:
        self.plugins = plugins
        self.failed = list(failed or [])

    def ids(self) -> list[str]:
        return sorted(self.plugins)

    def get(self, plugin_id: str) -> Any:
        return self.plugins.get(plugin_id)


class _FakeManager:
    """插件管理器的一半：只长路由会碰的那几个方法。"""

    def __init__(
        self, found: dict[str, Any] | None = None, *, order: list[str] | None = None
    ) -> None:
        self.found = dict(found or {})
        self.order = list(order or [])
        self.reloads: list[str] = []

    def discover(self) -> _Discovery:
        return _Discovery(self.found)

    def health_all(self) -> dict[str, Any]:
        return {}

    def enabled_ids(self) -> list[str]:
        return list(self.order)

    def status_of(self, plugin_id: str) -> Any:
        return None

    def reload(self, plugin_id: str) -> bool:
        self.reloads.append(plugin_id)
        return True


class _Engine:
    """假引擎：记下它被问了什么，回一份定好的结果。"""

    def __init__(self, outcome: dict[str, Any] | None = None) -> None:
        self.outcome = outcome if outcome is not None else _reply()
        self.seen: list[tuple[str, str]] = []

    async def __call__(self, text: str, conversation_id: str) -> dict[str, Any]:
        self.seen.append((text, conversation_id))
        return self.outcome


# ═══════════════════════════════════════════════════════════════════════
#  装配
# ═══════════════════════════════════════════════════════════════════════


def _reply() -> dict[str, Any]:
    return {
        "conversation_id": "c1",
        "reply": {"id": "m2", "content": "刚在写点东西", "direction": "outbound"},
        "decision": {"mode": "reply", "reason": "在", "delay_seconds": 0.0},
        "notes": [],
    }


def _config(tmp_path: Path, **web: Any) -> Config:
    """一份只动了 ``[web]`` 的配置。**不读真实配置文件**，否则本机配置会漏进测试。"""
    section: dict[str, Any] = {"auth": "none", "host": "127.0.0.1"}
    section.update(web)
    return Config.load(
        env={},
        overrides={"core": {"data_dir": str(tmp_path / "data")}, "web": section},
    )


def _deps(config: Config, gate: AuthGate | None = None, **parts: Any) -> WebDeps:
    broker = SSEBroker(max_connections=2, keepalive_seconds=1.0)
    return WebDeps(
        config=config,
        auth=gate if gate is not None else AuthGate(mode="none"),
        broker=broker,
        channel=WebChannel(broker),
        registry=ServiceRegistry(),
        **parts,
    )


@asynccontextmanager
async def _client(deps: WebDeps) -> AsyncIterator[httpx.AsyncClient]:
    """一个连在内存里的浏览器：cookie 会自动跟着走，和真浏览器一样。"""
    transport = httpx.ASGITransport(app=create_app(deps))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _token_gate() -> AuthGate:
    return AuthGate(mode="token", token=TOKEN)


# ═══════════════════════════════════════════════════════════════════════
#  /api/health 与那道门
# ═══════════════════════════════════════════════════════════════════════


async def test_health_is_reachable_without_a_cookie(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), _token_gate(), persona_id="p1")
    async with _client(deps) as client:
        body = (await client.get("/api/health")).json()

    assert body["ok"] is True
    assert body["auth"] == {"mode": "token", "required": True}
    assert body["channel"]["id"] == CHANNEL_ID
    assert body["channel"]["ok"] is True
    assert body["channel"]["readers"] == 0
    assert body["persona_id"] == "p1"
    assert body["version"] == 1


async def test_health_says_nothing_private(tmp_path: Path) -> None:
    """它不需要认证，所以只能用来说「我是谁、我怎么进门」。"""
    deps = _deps(_config(tmp_path), _token_gate())
    async with _client(deps) as client:
        body = (await client.get("/api/health")).json()

    assert set(body) == {"ok", "auth", "channel", "persona_id", "version"}


async def test_guarded_paths_answer_401_and_tell_you_where_to_look(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), _token_gate())
    async with _client(deps) as client:
        response = await client.get("/api/status")

    assert response.status_code == 401
    detail = response.json()["detail"]
    assert detail["error"] == "unauthorized"
    # 说明书里必须指对表格：auth 在 [web] 段，不在 [channels.web]。
    assert "[web]" in detail["hint"]


async def test_the_token_can_arrive_in_the_query_string(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), _token_gate())
    async with _client(deps) as client:
        assert (await client.get(f"/api/status?token={TOKEN}")).status_code == 200


async def test_the_bearer_header_also_works(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), _token_gate())
    async with _client(deps) as client:
        response = await client.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 200


async def test_a_wrong_token_is_401_and_the_right_one_sets_a_cookie(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), _token_gate())
    async with _client(deps) as client:
        assert (await client.post("/api/auth", json={"token": "wrong"})).status_code == 401

        response = await client.post("/api/auth", json={"token": TOKEN})
        assert response.status_code == 200
        assert response.json() == {"ok": True, "mode": "token"}
        assert deps.auth.cookie_name in response.headers["set-cookie"]
        # 换到的 cookie 要真的能用，否则症状是「登录成功但刷新又要登」。
        assert (await client.get("/api/status")).status_code == 200


async def test_empty_credentials_are_400(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), _token_gate())
    async with _client(deps) as client:
        response = await client.post("/api/auth", json={})

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "bad_request"


async def test_auth_answers_ok_even_when_it_is_off(tmp_path: Path) -> None:
    """/api/auth 是公开路由：前端不必先读 /api/health 才知道该不该弹登录框。"""
    deps = _deps(_config(tmp_path))
    async with _client(deps) as client:
        response = await client.post("/api/auth", json={})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "mode": "none"}


async def test_only_the_api_prefix_is_guarded(tmp_path: Path) -> None:
    """页面本身要能先到，才谈得上拿 ?token= 去换 cookie。"""
    deps = _deps(_config(tmp_path), _token_gate())
    async with _client(deps) as client:
        index = await client.get("/")
        script = await client.get("/static/app.js")
        unknown = await client.get("/api/nope")

    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    assert script.status_code == 200
    assert unknown.status_code == 401


# ═══════════════════════════════════════════════════════════════════════
#  /api/chat
# ═══════════════════════════════════════════════════════════════════════


def _message(message_id: str = "m1", **kwargs: Any) -> MessageRecord:
    base: dict[str, Any] = {
        "id": message_id,
        "conversation_id": "c1",
        "direction": "inbound",
        "sender_id": "user",
        "content": "在忙什么",
        "created_at": NOW,
    }
    base.update(kwargs)
    return MessageRecord(**base)


async def test_chat_history_without_a_store_is_503(tmp_path: Path) -> None:
    async with _client(_deps(_config(tmp_path))) as client:
        response = await client.get("/api/chat/history")

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "unavailable"


async def test_chat_history_without_a_conversation_is_an_empty_list(tmp_path: Path) -> None:
    """人设刚建好、还没说过话时就是这个状态——不是错误。"""
    deps = _deps(_config(tmp_path), conversations=_FakeConversations())
    async with _client(deps) as client:
        body = (await client.get("/api/chat/history")).json()

    assert body == {"conversation_id": "", "messages": []}


async def test_chat_history_maps_the_columns_it_means_to_show(tmp_path: Path) -> None:
    store = _FakeConversations(
        [_message("m1"), _message("m2", direction="outbound", initiative=True)]
    )
    deps = _deps(_config(tmp_path), conversations=store, conversation_id="c1")
    async with _client(deps) as client:
        body = (await client.get("/api/chat/history")).json()

    assert body["conversation_id"] == "c1"
    assert [item["id"] for item in body["messages"]] == ["m1", "m2"]
    first = body["messages"][0]
    assert first["direction"] == "inbound"
    assert first["at"].endswith("+08:00")
    # tick_id 是内部字段，不该出现在浏览器里。
    assert "tick_id" not in first


async def test_chat_history_hands_the_paging_arguments_to_the_store(tmp_path: Path) -> None:
    store = _FakeConversations([_message()])
    deps = _deps(_config(tmp_path), conversations=store, conversation_id="c1")
    async with _client(deps) as client:
        await client.get("/api/chat/history", params={"limit": 7, "before": NOW.isoformat()})

    assert store.asked[0]["limit"] == 7
    assert store.asked[0]["before"] == NOW


async def test_chat_send_rejects_an_empty_message(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), respond=_Engine())
    async with _client(deps) as client:
        response = await client.post("/api/chat/send", json={"text": "   "})

    assert response.status_code == 400
    assert response.json()["detail"]["what"] == "消息是空的"


async def test_chat_send_rejects_a_message_that_is_not_a_message(tmp_path: Path) -> None:
    """4000 字的上限挡的是「把一整份文件贴进聊天框」。"""
    deps = _deps(_config(tmp_path), respond=_Engine())
    async with _client(deps) as client:
        response = await client.post("/api/chat/send", json={"text": "字" * 4001})

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "bad_request"


async def test_chat_send_without_an_engine_is_503(tmp_path: Path) -> None:
    async with _client(_deps(_config(tmp_path))) as client:
        response = await client.post("/api/chat/send", json={"text": "在吗"})

    assert response.status_code == 503
    assert "doctor" in response.json()["detail"]["hint"]


async def test_chat_send_returns_the_engine_answer(tmp_path: Path) -> None:
    engine = _Engine()
    deps = _deps(_config(tmp_path), respond=engine, conversation_id="c1")
    async with _client(deps) as client:
        body = (await client.post("/api/chat/send", json={"text": "在忙什么"})).json()

    assert engine.seen == [("在忙什么", "c1")]
    assert body["text"] == "在忙什么"
    assert body["reply"]["content"] == "刚在写点东西"
    assert body["decision"]["mode"] == "reply"
    assert body["notes"] == []


async def test_a_quiet_turn_is_200_not_an_error(tmp_path: Path) -> None:
    """「它这一轮不回」是正常结果；做成 4xx 会逼前端为它写一段错误处理。"""
    engine = _Engine({**_reply(), "reply": None, "decision": {"mode": "quiet", "reason": "在开会"}})
    deps = _deps(_config(tmp_path), respond=engine)
    async with _client(deps) as client:
        response = await client.post("/api/chat/send", json={"text": "在吗"})

    assert response.status_code == 200
    assert response.json()["reply"] is None
    assert response.json()["decision"]["mode"] == "quiet"


async def test_the_stream_answers_503_when_too_many_pages_are_open(tmp_path: Path) -> None:
    """上限到了不是「服务器崩了」，是「你开了太多标签页」。"""
    deps = _deps(_config(tmp_path))
    deps.broker.subscribe()
    deps.broker.subscribe()
    async with _client(deps) as client:
        response = await client.get("/api/chat/stream")

    assert response.status_code == 503
    assert "上限" in response.json()["detail"]["what"]


# ═══════════════════════════════════════════════════════════════════════
#  /api/feed
# ═══════════════════════════════════════════════════════════════════════


def _post(post_id: str = "s1", **kwargs: Any) -> SocialPostRecord:
    base: dict[str, Any] = {
        "id": post_id,
        "persona_id": "p1",
        "content": "今天写了一天代码",
        "posted_at": NOW,
        "mood_label": "平静",
        "like_count": 0,
    }
    base.update(kwargs)
    return SocialPostRecord(**base)


def _activity(activity_id: str = "a1", **kwargs: Any) -> ActivityRecord:
    base: dict[str, Any] = {
        "id": activity_id,
        "intent": "work",
        "description": "写代码",
        "started_at": NOW,
        "inner_voice": "有点困",
        "duration_minutes": 30,
    }
    base.update(kwargs)
    return ActivityRecord(**base)


async def test_feed_without_a_store_is_503(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), persona_id="p1")
    async with _client(deps) as client:
        assert (await client.get("/api/feed")).status_code == 503


async def test_feed_without_a_persona_is_empty(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), posts=_FakePosts([_post()]))
    async with _client(deps) as client:
        body = (await client.get("/api/feed")).json()

    assert body["items"] == []
    assert body["has_more"] is False


async def test_feed_pages_in_memory(tmp_path: Path) -> None:
    store = _FakePosts([_post(f"s{n}") for n in range(5)])
    deps = _deps(_config(tmp_path), posts=store, persona_id="p1")
    async with _client(deps) as client:
        body = (await client.get("/api/feed", params={"limit": 2, "offset": 1})).json()

    assert [item["id"] for item in body["items"]] == ["s1", "s2"]
    assert body["offset"] == 1
    # 后面还有两条，所以「还能往下翻」要说出来。
    assert body["has_more"] is True


async def test_the_last_feed_page_says_it_is_the_last(tmp_path: Path) -> None:
    store = _FakePosts([_post(f"s{n}") for n in range(2)])
    deps = _deps(_config(tmp_path), posts=store, persona_id="p1")
    async with _client(deps) as client:
        body = (await client.get("/api/feed", params={"limit": 5})).json()

    assert len(body["items"]) == 2
    assert body["has_more"] is False


async def test_like_bumps_the_count_without_a_new_endpoint(tmp_path: Path) -> None:
    store = _FakePosts([_post("s1", like_count=3)])
    deps = _deps(_config(tmp_path), posts=store, persona_id="p1")
    async with _client(deps) as client:
        response = await client.post("/api/feed/s1/like")

    assert response.json() == {"id": "s1", "like_count": 4}
    assert store.appended[0].like_count == 4


async def test_like_on_something_that_is_not_there_is_404(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), posts=_FakePosts(), persona_id="p1")
    async with _client(deps) as client:
        response = await client.post("/api/feed/s9/like")

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "not_found"


async def test_like_without_a_persona_is_400(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), posts=_FakePosts([_post()]))
    async with _client(deps) as client:
        assert (await client.post("/api/feed/s1/like")).status_code == 400


async def test_commenting_is_503_because_there_is_nowhere_to_put_it(tmp_path: Path) -> None:
    """假装成功之后，用户要到「刷新一下评论不见了」时才会发现。"""
    deps = _deps(_config(tmp_path), posts=_FakePosts(), persona_id="p1")
    async with _client(deps) as client:
        response = await client.post("/api/feed/s1/comment", json={"text": "好看"})

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "unavailable"


async def test_an_empty_comment_is_400_before_it_is_503(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), posts=_FakePosts(), persona_id="p1")
    async with _client(deps) as client:
        response = await client.post("/api/feed/s1/comment", json={"text": " "})

    assert response.status_code == 400


async def test_timeline_merges_behavior_and_posts(tmp_path: Path) -> None:
    earlier = NOW.replace(hour=8)
    deps = _deps(
        _config(tmp_path),
        posts=_FakePosts([_post("s1", posted_at=NOW)]),
        activities=_FakeActivities([_activity("a1", started_at=earlier)]),
        persona_id="p1",
    )
    async with _client(deps) as client:
        body = (await client.get("/api/feed/timeline")).json()

    assert [item["kind"] for item in body["items"]] == ["post", "activity"]
    assert body["days"] == 3


async def test_the_page_size_is_capped(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), posts=_FakePosts(), persona_id="p1")
    async with _client(deps) as client:
        response = await client.get("/api/feed", params={"limit": 999})

    assert response.status_code == 422


# ═══════════════════════════════════════════════════════════════════════
#  /api/thoughts 与 /api/memory
# ═══════════════════════════════════════════════════════════════════════


async def test_thoughts_keeps_only_the_ones_it_said_to_itself(tmp_path: Path) -> None:
    records = [
        _activity("a1", inner_voice="有点困"),
        _activity("a2", inner_voice=""),
        _activity("a3", inner_voice="算了，先吃饭"),
    ]
    deps = _deps(_config(tmp_path), activities=_FakeActivities(records), persona_id="p1")
    async with _client(deps) as client:
        body = (await client.get("/api/thoughts")).json()

    assert [item["id"] for item in body["items"]] == ["a1", "a3"]
    assert body["days"] == 7


async def test_thoughts_without_a_store_is_503(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), persona_id="p1")
    async with _client(deps) as client:
        assert (await client.get("/api/thoughts")).status_code == 503


async def test_memory_shows_the_newest_first(tmp_path: Path) -> None:
    """仓储给的是正序，这一页要的是倒序——翻这一下是路由的活。"""
    old = Memory(
        persona_id="p1",
        kind="episodic",
        content="早上",
        summary="早上",
        occurred_at=NOW.replace(hour=8),
    )
    new = Memory(
        persona_id="p1",
        kind="semantic",
        content="晚上",
        summary="晚上",
        occurred_at=NOW,
    )
    deps = _deps(_config(tmp_path), memories=_FakeMemories([old, new]), persona_id="p1")
    async with _client(deps) as client:
        body = (await client.get("/api/memory")).json()

    assert [item["content"] for item in body["items"]] == ["晚上", "早上"]
    assert body["total"] == 2
    assert body["items"][0]["entities"] == []


async def test_memory_rejects_a_kind_it_does_not_know(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), memories=_FakeMemories(), persona_id="p1")
    async with _client(deps) as client:
        response = await client.get("/api/memory", params={"kind": "telepathy"})

    assert response.status_code == 422


# ═══════════════════════════════════════════════════════════════════════
#  /api/stats 与 /api/budget
# ═══════════════════════════════════════════════════════════════════════


async def test_stats_counts_everything_it_can(tmp_path: Path) -> None:
    conversations = _FakeConversations(
        [_message("m1"), _message("m2", direction="outbound"), _message("m3")]
    )
    deps = _deps(
        _config(tmp_path),
        posts=_FakePosts([_post()]),
        activities=_FakeActivities([_activity(), _activity("a2")]),
        conversations=conversations,
        memories=_FakeMemories(
            [
                Memory(persona_id="p1", kind="episodic", content="a", summary="a", occurred_at=NOW),
                Memory(persona_id="p1", kind="episodic", content="b", summary="b", occurred_at=NOW),
                Memory(persona_id="p1", kind="semantic", content="c", summary="c", occurred_at=NOW),
            ]
        ),
        sources=_FakeSources([SourceRecord(id="s1", url="https://x.test", fetched_at=NOW)]),
        persona_id="p1",
        conversation_id="c1",
    )
    async with _client(deps) as client:
        body = (await client.get("/api/stats")).json()

    assert body["posts"] == 1
    assert body["activities"] == 2
    assert body["messages_in"] == 2
    assert body["messages_out"] == 1
    assert body["memories"] == 3
    assert body["memory_kinds"] == {"episodic": 2, "semantic": 1}
    assert body["sources"] == 1
    assert body["truncated"] is False
    assert body["window_days"] == 30


async def test_stats_without_a_persona_is_all_zeros(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), posts=_FakePosts([_post()]))
    async with _client(deps) as client:
        body = (await client.get("/api/stats")).json()

    assert body["posts"] == 0
    assert body["memory_kinds"] == {}


async def test_stats_admits_when_it_stopped_counting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取不到底时数字会偏小，所以要说「至少这么多」而不是给个假的确切值。"""
    from alterego.channels.web.routes import stats as stats_module

    monkeypatch.setattr(stats_module, "SCAN_LIMIT", 1)
    deps = _deps(
        _config(tmp_path),
        activities=_FakeActivities([_activity(), _activity("a2")]),
        persona_id="p1",
    )
    async with _client(deps) as client:
        body = (await client.get("/api/stats")).json()

    assert body["truncated"] is True


async def test_budget_reports_the_circuit_so_you_can_see_why_it_went_quiet(
    tmp_path: Path,
) -> None:
    usage = BudgetUsage(
        day=NOW.date(),
        messages_sent=2,
        messages_suppressed=1,
        consecutive_no_reply=5,
        circuit_until=NOW,
    )
    deps = _deps(_config(tmp_path), budgets=_FakeBudgets(usage), persona_id="p1")
    async with _client(deps) as client:
        body = (await client.get("/api/budget", params={"day": "2026-09-16"})).json()

    assert body["day"] == "2026-09-16"
    assert body["usage"]["messages_suppressed"] == 1
    assert body["usage"]["circuit_until"] is not None


async def test_budget_without_a_store_is_503(tmp_path: Path) -> None:
    async with _client(_deps(_config(tmp_path))) as client:
        assert (await client.get("/api/budget")).status_code == 503


async def test_sources_shows_the_kept_ones_newest_first(tmp_path: Path) -> None:
    records = [
        SourceRecord(id="s1", url="https://x.test/1", fetched_at=NOW.replace(hour=8)),
        SourceRecord(id="s2", url="https://x.test/2", fetched_at=NOW),
    ]
    deps = _deps(_config(tmp_path), sources=_FakeSources(records), persona_id="p1")
    async with _client(deps) as client:
        body = (await client.get("/api/sources")).json()

    assert [item["id"] for item in body["items"]] == ["s2", "s1"]
    assert body["total"] == 2


async def test_the_overview_answers_in_one_request(tmp_path: Path) -> None:
    """这一页是启动时第一眼看到的，拆成六个请求会先出现六块「正在加载」。"""
    deps = _deps(
        _config(tmp_path),
        personas=_FakePersonas(PersonaRecord(id="p1", name="林晚", occupation="算法工程师")),
        emotions=_FakeEmotions(
            Emotion(label="平静", valence=0.2, arousal=0.3, fatigue=0.2, updated_at=NOW)
        ),
        conversations=_FakeConversations([_message()], unread=3),
        activities=_FakeActivities([_activity()]),
        persona_id="p1",
        conversation_id="c1",
    )
    async with _client(deps) as client:
        body = (await client.get("/api/status")).json()

    assert body["persona"]["name"] == "林晚"
    assert body["emotion"]["label"] == "平静"
    assert body["unread"] == 3
    assert [item["id"] for item in body["recent_activity"]] == ["a1"]
    assert body["plugins"] == {}
    assert body["channel"]["ok"] is True
    assert body["at"].endswith("+08:00")


async def test_status_without_anything_attached_still_answers(tmp_path: Path) -> None:
    """「页面能开，但什么都还没接上」是一个可用的状态，进程不该因此退出。"""
    async with _client(_deps(_config(tmp_path))) as client:
        body = (await client.get("/api/status")).json()

    assert body["persona"] is None
    assert body["emotion"] is None
    assert body["unread"] == 0
    assert body["recent_activity"] == []


async def test_emotion_days_is_only_echoed_for_now(tmp_path: Path) -> None:
    """仓储只有 latest，画不出曲线——所以 history 是空的，而不是编出来的。"""
    deps = _deps(
        _config(tmp_path),
        emotions=_FakeEmotions(
            Emotion(label="开心", valence=0.6, arousal=0.5, fatigue=0.1, updated_at=NOW)
        ),
        persona_id="p1",
    )
    async with _client(deps) as client:
        body = (await client.get("/api/emotion", params={"days": 14})).json()

    assert body["days"] == 14
    assert body["latest"]["label"] == "开心"
    assert body["history"] == []


# ═══════════════════════════════════════════════════════════════════════
#  /api/plugins
# ═══════════════════════════════════════════════════════════════════════


def _manifest(plugin_id: str = "tool.example", *, api_version: int = 1) -> Any:
    return SimpleNamespace(
        id=plugin_id,
        display_name="例子",
        kind="tool",
        version="0.1.0",
        api_version=api_version,
        source="builtin",
        description="一个例子",
        authors=("LMG-arch",),
        tags=("tool",),
        provides=("tool.example",),
        requires=(),
    )


async def test_plugins_without_a_manager_is_503(tmp_path: Path) -> None:
    async with _client(_deps(_config(tmp_path))) as client:
        response = await client.get("/api/plugins")

    assert response.status_code == 503
    assert "doctor" in response.json()["detail"]["hint"]


async def test_plugins_lists_what_was_found(tmp_path: Path) -> None:
    manager = _FakeManager({"tool.example": SimpleNamespace(manifest=_manifest())}, order=[])
    deps = _deps(_config(tmp_path), manager=manager)
    async with _client(deps) as client:
        body = (await client.get("/api/plugins")).json()

    row = body["items"][0]
    assert row["id"] == "tool.example"
    assert row["name"] == "例子"
    assert row["compatible"] is True
    assert row["enabled"] is False
    assert row["loaded"] is False
    assert row["health"] is None
    assert body["loaded"] == []
    assert body["failed"] == []


async def test_an_incompatible_api_version_is_flagged_not_hidden(tmp_path: Path) -> None:
    manager = _FakeManager({"tool.example": SimpleNamespace(manifest=_manifest(api_version=99))})
    deps = _deps(_config(tmp_path), manager=manager)
    async with _client(deps) as client:
        row = (await client.get("/api/plugins")).json()["items"][0]

    assert row["compatible"] is False


async def test_reloading_something_that_was_never_discovered_is_404(tmp_path: Path) -> None:
    deps = _deps(_config(tmp_path), manager=_FakeManager())
    async with _client(deps) as client:
        response = await client.post("/api/plugins/tool.example/reload")

    assert response.status_code == 404


async def test_reloading_something_that_is_not_loaded_is_503(tmp_path: Path) -> None:
    """「重载失败了，它现在是停用状态」是一个可用的回答；500 不是。"""
    manager = _FakeManager({"tool.example": SimpleNamespace(manifest=_manifest())}, order=[])
    deps = _deps(_config(tmp_path), manager=manager)
    async with _client(deps) as client:
        response = await client.post("/api/plugins/tool.example/reload")

    assert response.status_code == 503


async def test_reloading_a_loaded_plugin_reports_its_new_state(tmp_path: Path) -> None:
    manager = _FakeManager(
        {"tool.example": SimpleNamespace(manifest=_manifest())}, order=["tool.example"]
    )
    deps = _deps(_config(tmp_path), manager=manager)
    async with _client(deps) as client:
        body = (await client.post("/api/plugins/tool.example/reload")).json()

    assert body == {"id": "tool.example", "ok": True, "status": ""}
    assert manager.reloads == ["tool.example"]


# ═══════════════════════════════════════════════════════════════════════
#  /api/settings
# ═══════════════════════════════════════════════════════════════════════


async def test_the_schema_carries_no_values(tmp_path: Path) -> None:
    """骨架、补全脚本、文档生成只要这一份；它们的寿命比某台机器的配置长。"""
    async with _client(_deps(_config(tmp_path))) as client:
        body = (await client.get("/api/settings/schema")).json()

    keys = {item["key"] for item in body["settings"]}
    assert "web.host" in keys
    assert "web.auth_password" in keys
    assert body["version"] == 1
    assert body["groups"]
    assert all(item["current"] is None for item in body["settings"])


async def test_current_values_never_hand_out_a_secret(tmp_path: Path) -> None:
    """密钥只回「设了没有」：连掩码都不给，因为掩码会泄漏长度。"""
    async with _client(_deps(_config(tmp_path))) as client:
        body = (await client.get("/api/settings")).json()

    assert body["values"]["web.auth_password"] == {"set": False}
    assert body["values"]["web.port"] == 8765


async def test_source_is_null_when_there_is_no_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    async with _client(_deps(_config(tmp_path))) as client:
        body = (await client.get("/api/settings")).json()

    assert body["source"] is None


async def test_a_secret_cannot_be_written_from_the_page(tmp_path: Path) -> None:
    """能在页面上填的密钥，迟早会被写进某份被提交的文件里。"""
    async with _client(_deps(_config(tmp_path))) as client:
        response = await client.post(
            "/api/settings", json={"key": "web.auth_password", "value": "hunter2"}
        )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "secret_not_writable"


async def test_writing_without_a_config_file_is_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    async with _client(_deps(_config(tmp_path))) as client:
        response = await client.post("/api/settings", json={"key": "web.port", "value": "9000"})

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "unavailable"


def _file_config(tmp_path: Path) -> Config:
    """一份真的落在磁盘上的配置：写设置需要有一个地方可写。"""
    path = tmp_path / "alterego.toml"
    path.write_text("[web]\nport = 8765\n", encoding="utf-8")
    return Config.load(path=path, env={})


async def test_writing_a_good_value_returns_what_it_really_became(tmp_path: Path) -> None:
    """回显用户输入会让一个没生效的改动看起来也成功了。"""
    config = _file_config(tmp_path)
    async with _client(_deps(config)) as client:
        response = await client.post("/api/settings", json={"key": "web.port", "value": "9000"})

    body = response.json()
    assert response.status_code == 200
    assert body["before"] == 8765
    assert body["after"] == 9000
    assert body["requires_restart"] is True
    assert body["restart_needed"] == ["web.port"]
    assert "9000" in Path(body["source"]).read_text(encoding="utf-8")


async def test_writing_a_bad_value_is_400_and_quotes_its_own_sentence(tmp_path: Path) -> None:
    """校验器那句中文要原封不动地传上去：页面直接拿它显示，不再自己编一句。"""
    async with _client(_deps(_file_config(tmp_path))) as client:
        response = await client.post("/api/settings", json={"key": "web.port", "value": "99999"})

    assert response.status_code == 400
    assert response.json()["detail"]["what"] == "web.port 不能大于 65535"


async def test_an_unknown_key_is_404_and_lists_the_groups(tmp_path: Path) -> None:
    async with _client(_deps(_config(tmp_path))) as client:
        response = await client.post("/api/settings", json={"key": "nope.nope", "value": "1"})

    assert response.status_code == 404
    assert response.json()["detail"]["groups"]


@pytest.mark.parametrize("payload", [{}, {"key": "", "value": "1"}])
async def test_a_payload_that_is_not_a_pair_is_rejected(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    async with _client(_deps(_config(tmp_path))) as client:
        response = await client.post("/api/settings", json=payload)

    assert response.status_code == 400


async def test_a_giant_value_is_400(tmp_path: Path) -> None:
    async with _client(_deps(_config(tmp_path))) as client:
        response = await client.post("/api/settings", json={"key": "web.host", "value": "x" * 4001})

    assert response.status_code == 400
    assert "4000" in response.json()["detail"]["what"]


# ═══════════════════════════════════════════════════════════════════════
#  两个应用共处一个进程
# ═══════════════════════════════════════════════════════════════════════


async def test_two_apps_in_one_process_do_not_share_their_deps(tmp_path: Path) -> None:
    """依赖盒子挂在 ``app.state`` 而不是模块级全局，否则 B 会看到 A 的配置。"""
    first = _deps(_config(tmp_path), _token_gate(), persona_id="p1")
    second = _deps(_config(tmp_path, port=9999), persona_id="p2")

    async with _client(first) as client:
        assert (await client.get("/api/health")).json()["persona_id"] == "p1"
        assert (await client.get("/api/status")).status_code == 401
    async with _client(second) as client:
        assert (await client.get("/api/health")).json()["persona_id"] == "p2"
        assert (await client.get("/api/status")).status_code == 200


async def test_replacing_the_gate_rebuilds_the_middleware(tmp_path: Path) -> None:
    """``dataclasses.replace`` 出来的依赖盒子要能直接被 ``create_app`` 用上。"""
    deps = _deps(_config(tmp_path), persona_id="p1")
    strict = replace(deps, auth=_token_gate())

    async with _client(deps) as client:
        assert (await client.get("/api/status")).status_code == 200
    async with _client(strict) as client:
        assert (await client.get("/api/status")).status_code == 401

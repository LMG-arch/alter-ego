"""那道认证门本身。

**这里不经过任何服务器。** 路由测试是从「请求进、响应出」的层面看的；这一份
要问的是更底下的一件事：中间件在**还没走到任何路由**的时候，是怎么判的。
所以这里手工拼 ASGI 的 ``scope`` / ``receive`` / ``send`` 三件套，后面挂一个
只记录「我被调用了没有」的假应用——把关的全部意义就是「该拦的别放进去」，
而拦住之后假应用一次都不该被调用。

**为什么这些用例值得单独存在。** 白名单是靠「默认全拦 + 两条例外」实现的，
这个方向一旦反过来（默认放行、逐条加防守），漏掉一条的症状是某个接口
悄悄变成公开的：不报错、不告警，只是能被任何本机进程读走。这种错误只能靠
「把例外全都列出来测一遍，再把非例外也列出来测一遍」来发现。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from alterego.channels.web.app import (
    PUBLIC_PATHS,
    AuthMiddleware,
    create_app,
    describe_missing_extra,
)
from alterego.channels.web.auth import AuthGate
from alterego.channels.web.deps import WebDeps
from alterego.channels.web.plugin import WebChannel
from alterego.channels.web.sse import SSEBroker
from alterego.kernel.config import Config
from alterego.kernel.errors import ChannelError
from alterego.kernel.registry import ServiceRegistry


TOKEN = "sesame"


class _Recorder:
    """假应用：记下谁进来了，然后回一个最小的 200。"""

    def __init__(self) -> None:
        self.paths: list[str] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.paths.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _run(
    app: Any,
    *,
    path: str = "/api/status",
    method: str = "GET",
    headers: tuple[tuple[bytes, bytes], ...] = (),
    query: str = "",
    scope_type: str = "http",
) -> tuple[list[dict[str, Any]], Any]:
    """把一次请求喂给中间件，收回它发出去的消息。

    第二个返回值是**中间件自己**回的 JSON（拦下时才有）；放行的时候里面那个
    假应用回的是 ``b"ok"``，解析不出来就是 ``None``——这正好用来分辨
    「是门答的」还是「是门后面的东西答的」。
    """
    sent: list[dict[str, Any]] = []
    scope = {
        "type": scope_type,
        "method": method,
        "path": path,
        "query_string": query.encode("latin-1"),
        "headers": list(headers),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent, _json_body(sent)


def _json_body(sent: list[dict[str, Any]]) -> Any:
    for message in reversed(sent):
        if message["type"] == "http.response.body":
            try:
                return json.loads(message["body"])
            except json.JSONDecodeError:
                return None
    return None


def _app(gate: AuthGate) -> tuple[AuthMiddleware, _Recorder]:
    behind = _Recorder()
    return AuthMiddleware(behind, gate), behind


def _token_gate() -> AuthGate:
    return AuthGate(mode="token", token=TOKEN)


# ═══════════════════════════════════════════════════════════════════════
#  该拦的
# ═══════════════════════════════════════════════════════════════════════


async def test_a_guarded_path_without_a_credential_never_reaches_the_route() -> None:
    app, behind = _app(_token_gate())
    sent, body = await _run(app)

    assert behind.paths == []
    assert sent[0]["status"] == 401
    assert sent[-1]["type"] == "http.response.body"
    assert body["detail"]["error"] == "unauthorized"
    # 说明书必须指对表格：auth 住在 [web] 段。
    assert "[web]" in body["detail"]["hint"]


async def test_a_wrong_credential_is_still_401() -> None:
    app, behind = _app(_token_gate())
    _, body = await _run(app, headers=((b"authorization", b"Bearer nope"),))

    assert behind.paths == []
    assert body["detail"]["error"] == "unauthorized"


async def test_the_openapi_document_is_guarded_too() -> None:
    """它列着所有接口，本身就该和接口一样对待。"""
    app, behind = _app(_token_gate())
    sent, _ = await _run(app, path="/api/openapi.json")

    assert behind.paths == []
    assert sent[0]["status"] == 401


async def test_a_malformed_cookie_is_not_a_third_kind_of_failure() -> None:
    """畸形 cookie 当成「没带」——它不该变成 500，也不该变成放行。"""
    app, behind = _app(_token_gate())
    sent, _ = await _run(app, headers=((b"cookie", b"=;;;"),))

    assert behind.paths == []
    assert sent[0]["status"] == 401


async def test_an_authorization_scheme_we_do_not_know_is_ignored() -> None:
    """``Basic ...`` 不是我们的凭据；认错了就等于把门开给任何人。"""
    app, behind = _app(_token_gate())
    sent, _ = await _run(app, headers=((b"authorization", b"Basic c2VzYW1l"),))

    assert behind.paths == []
    assert sent[0]["status"] == 401


async def test_only_the_head_of_the_request_matters_not_its_method() -> None:
    app, behind = _app(_token_gate())
    sent, _ = await _run(app, method="POST", path="/api/chat/send")

    assert behind.paths == []
    assert sent[0]["status"] == 401


# ═══════════════════════════════════════════════════════════════════════
#  该放的
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("path", ["/api/health", "/api/auth", "/api/health/"])
async def test_the_short_whitelist_goes_through(path: str) -> None:
    """白名单短到能一眼看完——所以它值得逐条测。"""
    app, behind = _app(_token_gate())
    sent, _ = await _run(app, path=path)

    assert behind.paths == [path]
    assert sent[0]["status"] == 200


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/static/style.css", "/favicon.ico"])
async def test_static_things_are_not_guarded(path: str) -> None:
    """页面本身要先能到，才谈得上拿 ?token= 去换 cookie。"""
    app, behind = _app(_token_gate())
    _, _ = await _run(app, path=path)

    assert behind.paths == [path]


async def test_the_whitelist_holds_exactly_two_paths() -> None:
    """门的方向是「默认拦下」；多一条例外要是一次有意的改动，不是顺手加的。"""
    assert frozenset({"/api/health", "/api/auth"}) == PUBLIC_PATHS


async def test_a_non_http_scope_is_not_our_business() -> None:
    """lifespan 之类的消息没有请求头，交给里面的人处理。"""
    app, behind = _app(_token_gate())
    await _run(app, scope_type="lifespan")

    assert behind.paths == ["/api/status"]


async def test_the_gate_is_skipped_entirely_when_auth_is_off() -> None:
    """关了认证就一次比较都不做——而不是「比完发现不用比」。"""
    app, behind = _app(AuthGate(mode="none"))
    _, _ = await _run(app)

    assert behind.paths == ["/api/status"]


# ═══════════════════════════════════════════════════════════════════════
#  凭据的四种递法
# ═══════════════════════════════════════════════════════════════════════


async def test_the_bearer_header_is_the_first_thing_looked_at() -> None:
    app, behind = _app(_token_gate())
    _, _ = await _run(
        app, headers=((b"authorization", f"Bearer {TOKEN}".encode()),), query="token=wrong"
    )

    assert behind.paths == ["/api/status"]


async def test_the_query_token_works() -> None:
    app, behind = _app(_token_gate())
    _, _ = await _run(app, query=f"token={TOKEN}")

    assert behind.paths == ["/api/status"]


async def test_the_cookie_is_the_last_thing_looked_at() -> None:
    app, behind = _app(_token_gate())
    _, _ = await _run(app, headers=((b"cookie", f"alterego_token={TOKEN}".encode()),))

    assert behind.paths == ["/api/status"]


async def test_a_cookie_with_other_names_in_it_still_works() -> None:
    """浏览器会带上同名站点下的其它 cookie；切分器要在它们中间找准那一个。"""
    app, behind = _app(_token_gate())
    jar = f"theme=dark; alterego_token={TOKEN}; lang=zh"
    _, _ = await _run(app, headers=((b"cookie", jar.encode()),))

    assert behind.paths == ["/api/status"]


async def test_header_names_are_case_insensitive() -> None:
    app, behind = _app(_token_gate())
    _, _ = await _run(
        app,
        headers=(
            (b"Authorization", f"Bearer {TOKEN}".encode()),
            (b"COOKIE", b"theme=dark"),
        ),
    )

    assert behind.paths == ["/api/status"]


async def test_the_password_mode_accepts_the_password_and_nothing_else() -> None:
    app, behind = _app(AuthGate(mode="password", password="hunter2"))
    await _run(app, headers=((b"authorization", b"Bearer hunter2"),))

    assert behind.paths == ["/api/status"]

    strict, blocked = _app(AuthGate(mode="password", password="hunter2"))
    sent, _ = await _run(strict, headers=((b"authorization", b"Bearer sesame"),))

    assert blocked.paths == []
    assert sent[0]["status"] == 401


# ═══════════════════════════════════════════════════════════════════════
#  缺可选依赖时那句话
# ═══════════════════════════════════════════════════════════════════════


def test_missing_extra_says_which_package_to_install() -> None:
    text = describe_missing_extra(ImportError("No module named 'fastapi'", name="fastapi"))

    assert "alterego[web]" in text
    assert "fastapi" in text


def test_create_app_turns_a_missing_fastapi_into_something_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """延迟 import 的全部意义：命令行上看到的是「装哪个包」，不是一条 traceback。"""
    monkeypatch.setitem(sys.modules, "fastapi", None)
    deps = _deps(tmp_path)

    with pytest.raises(ChannelError) as caught:
        create_app(deps)

    assert "alterego[web]" in str(caught.value.message)


def _deps(tmp_path: Path) -> WebDeps:
    broker = SSEBroker(max_connections=1)
    return WebDeps(
        config=Config.load(
            env={},
            overrides={"core": {"data_dir": str(tmp_path / "data")}, "web": {"auth": "none"}},
        ),
        auth=AuthGate(mode="none"),
        broker=broker,
        channel=WebChannel(broker),
        registry=ServiceRegistry(),
    )

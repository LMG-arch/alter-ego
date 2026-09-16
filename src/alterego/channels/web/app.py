"""把路由、静态文件与认证装成一个 FastAPI 应用。

**这个模块在导入时不 import fastapi。** ``alterego`` 的必需依赖只有
pydantic 与 httpx；Web 界面是可选的一组（``alterego[web]``）。如果在模块
顶层 import fastapi，那么 ``import alterego.channels.web.app`` 会直接抛
``ModuleNotFoundError: No module named 'fastapi'``——那句话没有告诉用户
该装什么。延迟到 :func:`create_app` 里 import，就能把它换成一句能照做的话。

**认证是一道 ASGI 中间件，不是一个 ``Depends``。** 用 ``Depends`` 就得在
每个路由上写一遍，而漏写一个路由的后果是它悄悄变成了公开的——这种错误
不会报错、不会失败，只会一直存在。中间件默认**全部拦下**，例外是两个
显式写出来的路径（``/api/health`` 与 ``/api/auth``）：白名单短到能一眼看完，
而默认是关的。

**静态文件不拦。** 它们里面没有任何私人内容，而登录流程恰恰需要它们先到：
用户打开 ``http://127.0.0.1:8765/?token=xxx``，页面拿到 token 再
``POST /api/auth`` 换成 cookie。如果连页面本身都要认证，第一次访问就没有
入口了。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs

from alterego.channels.web.auth import AuthGate
from alterego.channels.web.deps import WebDeps


__all__ = ["STATIC_DIR", "AuthMiddleware", "create_app", "describe_missing_extra"]

#: 静态资源目录。``alterego/channels/web/static/`` 随包一起走
#: （``pyproject.toml`` 的 ``packages`` 已经带上，不要在那儿重复列）。
STATIC_DIR: Final[Path] = Path(__file__).parent / "static"

#: 不需要认证的路径。**只有这两条**，而且都不返回私人内容：
#: ``/api/health`` 说「进程活着吗、认证开着吗」，``/api/auth`` 就是用来换 cookie 的。
PUBLIC_PATHS: Final[frozenset[str]] = frozenset({"/api/health", "/api/auth"})

#: 需要认证的前缀。静态文件与 ``/`` 都不在这个前缀下。
GUARDED_PREFIX: Final[str] = "/api/"

_COOKIE_HEADER: Final[str] = "cookie"
_AUTH_HEADER: Final[str] = "authorization"


def describe_missing_extra(exc: ImportError) -> str:
    """缺可选依赖时该显示的那句话。

    单独一个函数是为了让 ``cli_serve`` 能原样转述给命令行用户：命令行里
    看到的应该是「装哪个包」，而不是一条 ImportError 的 traceback。
    """
    return (
        f"Web 界面需要可选依赖 fastapi 与 uvicorn，但现在缺了：{exc.name}。"
        '装法：pip install "alterego[web]"（或者 pip install -e ".[web]" 装本地这一份）。'
    )


def _headers(scope: dict[str, Any]) -> dict[str, str]:
    """请求头摊成一个小写键的字典。

    ASGI 给的是一串 ``(bytes, bytes)``，而 HTTP 头的名字大小写不敏感。
    转成小写字典才能用 ``headers.get("cookie")`` 取——重复的头会被后面的
    盖掉，而 ``cookie`` 重复出现本来就是畸形请求。
    """
    return {
        key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope["headers"]
    }


def _cookie(scope: dict[str, Any], name: str) -> str | None:
    """按名字取一个 cookie。名字由调用方给（:attr:`AuthGate.cookie_name`）。

    用标准库的 ``SimpleCookie`` 而不是自己切分号：cookie 值里可以有分号、
    引号、逗号，手写的切分器迟早会在某个合法值上错一次，而错的那次
    表现成「登录莫名其妙失效」。
    """
    from http.cookies import CookieError, SimpleCookie

    raw = _headers(scope).get(_COOKIE_HEADER)
    if not raw:
        return None
    # 不写 ``SimpleCookie[str]``：标准库里它不带类型参数，写上会被 mypy 当成错。
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except CookieError:
        # 畸形 cookie 当成没带 cookie：它不是「认证失败」之外的第三种情况。
        return None
    morsel = jar.get(name)
    return morsel.value if morsel is not None else None


def _presented(scope: dict[str, Any]) -> str | None:
    """从这一次请求里找出用户递过来的凭据（只看请求头与查询参数）。

    先看 ``Authorization: Bearer``，再看 ``?token=``。这个顺序是从
    「最不可能被误存下来」到「最容易被误存下来」：命令行带的是请求头，
    刚打开页面时带的是查询参数，两者都比 cookie 不容易被留在磁盘上。
    cookie 由 :func:`_cookie` 单独处理，作为最后一道。
    """
    headers = _headers(scope)
    scheme, _, credential = headers.get(_AUTH_HEADER, "").partition(" ")
    if scheme.lower() == "bearer" and credential.strip():
        return credential.strip()

    # 先声明成 ``str``：``scope`` 是 ``dict[str, Any]``，不声明的话
    # ``parse_qs`` 的返回值也整串是 ``Any``，最后这里就变成「从 Any 里返回 Any」。
    query: str = scope.get("query_string", b"").decode("latin-1")
    tokens = parse_qs(query).get("token")
    if tokens and tokens[0]:
        return tokens[0]
    return None


class AuthMiddleware:
    """默认拦下 ``/api/`` 下的所有请求，除了 :data:`PUBLIC_PATHS`。

    纯 ASGI（不继承 ``BaseHTTPMiddleware``）：这一层只做一次字符串比较和
    几次字典查询，不需要 Starlette 那套请求/响应的包装。包一层带来的
    额外开销与额外的出错可能都不值当。
    """

    def __init__(self, app: Any, gate: AuthGate) -> None:
        self.app = app
        self.gate = gate

    def _guarded(self, path: str) -> bool:
        if not path.startswith(GUARDED_PREFIX):
            return False
        return path.rstrip("/") not in PUBLIC_PATHS

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not self.gate.required:
            await self.app(scope, receive, send)
            return
        if not self._guarded(scope["path"]):
            await self.app(scope, receive, send)
            return

        presented = _presented(scope) or _cookie(scope, self.gate.cookie_name)
        if self.gate.check(presented):
            await self.app(scope, receive, send)
            return

        from fastapi.responses import JSONResponse

        response = JSONResponse(
            status_code=401,
            content={
                "detail": {
                    "error": "unauthorized",
                    "what": "没有通过认证",
                    "hint": (
                        "在 config/alterego.toml 的 [web] 里看 auth 是怎么设的；"
                        "token 模式下首次打开页面要带 ?token=<那个串>，"
                        "它写在项目根目录的 .alterego-token 里。"
                    ),
                }
            },
        )
        await response(scope, receive, send)


def create_app(deps: WebDeps) -> Any:
    """装出一个可以直接交给 uvicorn 的应用。

    参数是 :class:`~alterego.channels.web.deps.WebDeps`：路由需要的一切
    （配置、认证、SSE、仓储、引擎回话入口）都在里面，所以这个函数**不读
    环境、不连数据库**，它只做拼装。这样测试可以直接塞一个半成品的
    ``WebDeps`` 进来，验证路由在「没有仓储」时答的是 503。
    """
    from alterego.kernel.errors import AlterEgoError, ChannelError

    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import FileResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:
        raise ChannelError(describe_missing_extra(exc), missing=exc.name) from exc

    from alterego.channels.web.routes import chat, feed, plugins, settings, stats, status, thoughts

    app = FastAPI(
        title="alterego",
        version="1",
        # 文档界面先关掉：Swagger UI 的 HTML 是从 CDN 拉的，一个「本机跑的
        # 陪伴程序」不该在打开设置页时去连 jsdelivr。openapi.json 留着
        # （它在 /api/ 下，受同一道门保护），需要看接口的话用它。
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.deps = deps
    app.add_middleware(AuthMiddleware, gate=deps.auth)

    for module in (status, chat, feed, thoughts, stats, plugins, settings):
        app.include_router(module.router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> Any:
            """首页。**不用认证**——页面本身要能先到，才能拿 ?token= 去换 cookie。"""
            return FileResponse(STATIC_DIR / "index.html")

    @app.exception_handler(AlterEgoError)
    async def _on_alterego_error(request: Request, exc: AlterEgoError) -> Any:
        """把内核异常翻成 JSON。

        渠道类错误（比如「这个渠道根本没配好」）答 503：那是服务端还没准备好，
        不是这次请求写错了。其余按 500——但它仍然带 ``code`` 与 ``message``，
        因为前端要说出来的话是异常里那句中文，不是 ``Internal Server Error``。
        """
        del request
        code = 503 if isinstance(exc, ChannelError) else 500
        return JSONResponse(status_code=code, content={"detail": exc.to_dict()})

    return app

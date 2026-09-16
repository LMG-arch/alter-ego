"""``alterego serve`` —— 常驻进程：Web 界面 + 插件。

**这是第八个组装根**，与 ``cli.py`` / ``cli_db.py`` / ``cli_memory.py`` /
``cli_plugins.py`` / ``cli_study.py`` / ``cli_vault.py`` / ``cli_dataset.py`` /
``cli_chat.py`` 并列：只有这几个文件知道「存储用的是 SQLite」「模型走的是哪家
供应商」，其余代码一律只认 ``StorageBackend`` / ``ConversationRepository`` /
``LLMGateway`` / ``Channel`` 这些协议（``scripts/check_architecture.sh``
第 3 组红线把这件事钉住了）。

**为什么需要一个常驻进程。** 其余每个命令都是「开进程 → 连库 → 干一件事 →
退出」，插件在那种模式下每次都要重新发现、重新加载一遍；而界面要的是
**同一台插件管理器活很久**：``/api/plugins`` 报的熔断计数、健康状态、热重载，
只有在常驻进程里才是真的。``alterego plugins list`` 每次开一个新进程，
所以那边看到的熔断计数永远是零（``cmd_plugins_reset`` 的注释里写着这件事）。

**这一层只做装配，不做 HTTP。** 路由、认证、SSE 全在
``alterego.channels.web`` 里；这里负责把「配置 + SQLite + 供应商 + 插件管理器」
接上那个包，然后交给 uvicorn。反过来说，``channels/web`` 一行都不知道
SQLite 在哪儿、模型走哪一家。

**它把自己注册成一个渠道。** ``WebChannel`` 挂在本进程这**一台**
``ServiceRegistry`` 的 ``Channel`` 名下，名字就是 ``"web"``——插件要发消息时
``registry.get_all(Channel)`` 找的就是它。**没有 ``WebChannelPlugin``**：
包内没有内置插件的搜索路径（``[project.entry-points."alterego.plugins"]`` 是空的，
扫描目录由 ``Config.plugins.search_paths`` 决定，默认只有仓库根的 ``plugins/``），
所以 ``channels/web/plugin.toml`` 永远不会被发现，这条路只能由组装根走。

**不加 ``--reload``。** uvicorn 的热重载要按 import 字符串重新导入这个应用，
那是**第二个进程**：装第二遍插件、开第二条库连接、两份 SSE 连接表。
本项目里「同一台插件管理器活很久」才是要保住的东西，所以热重载留给
``manager.reload(plugin_id)``（也就是界面上后台页那个按钮）。

依据: docs/design/05-channels.md § 7、docs/plans/2026-09-16-main-body.md § 5（批次 C）
"""

from __future__ import annotations

import argparse
import asyncio
import random
import threading
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Final

from alterego.channels.web.app import create_app, describe_missing_extra
from alterego.channels.web.auth import AuthGate, gate_for, token_path
from alterego.channels.web.deps import UtteranceHandler, WebDeps
from alterego.channels.web.plugin import CHANNEL_ID, WebChannel
from alterego.channels.web.sse import SSEBroker
from alterego.channels.web.views import message_view
from alterego.cli_chat import PURPOSE, _service
from alterego.cli_db import _open_db, _require_sqlite, _resolve_persona
from alterego.cli_io import _RULE, _err, _out, _pad
from alterego.cli_memory import _fail, _providers
from alterego.cli_plugins import _print_load_report
from alterego.interfaces.channel import Channel
from alterego.interfaces.repository import PersonaRecord
from alterego.kernel.bus import EventBus
from alterego.kernel.clock import RealClock, resolve_timezone
from alterego.kernel.config import Config, WebConfig
from alterego.kernel.errors import AlterEgoError, ChannelError
from alterego.kernel.logging import get_logger, setup_logging
from alterego.kernel.manager import PluginManager
from alterego.kernel.registry import ServiceRegistry
from alterego.kernel.scheduler import Scheduler
from alterego.llm.providers import OpenAICompatibleProvider
from alterego.sim.conversation import InboundMessage
from alterego.sim.engine import user_conversation_id
from alterego.storage.sqlite import (
    SqliteActivityRepository,
    SqliteBudgetRepository,
    SqliteConversationRepository,
    SqliteEmotionRepository,
    SqliteMemoryRepository,
    SqlitePersonaRepository,
    SqliteScheduleRepository,
    SqliteSocialPostRepository,
    SqliteSourceRepository,
    SqliteStorageBackend,
    SqliteTickLogRepository,
)


__all__ = ["add_serve_parser", "cmd_serve"]

_log = get_logger("cli.serve")

#: 列标签占的**显示宽度**。与 ``cli_chat`` / ``cli_memory`` 对齐（中文一个字占两列）。
_LABEL: Final[int] = 10

#: 回话走的是 ``[llm.routing]`` 里的哪一个用途。与 ``cli_chat`` 共用同一个常量，
#: 不在这里再写一遍字面量：两处一旦不一致，症状是「命令行答得上来、网页答不上来」。
_ROUTING_PURPOSE: Final[str] = PURPOSE


# ── 配置与装配 ──────────────────────────────────────────────


def _serve_config() -> Config:
    """读配置。单独一个函数，是为了给测试一个能换掉的接口。"""
    return Config.load()


def _web_config(web: WebConfig, *, host: str | None, port: int | None) -> WebConfig:
    """把 ``--host`` / ``--port`` 盖到 ``[web]`` 上。

    用 ``dataclasses.replace`` 而不是自己再拼一个 ``WebConfig``：
    ``__post_init__`` 里的那三条检查（端口范围、``auth = "password"`` 得有密码、
    非本机监听不许关认证）只在构造时跑一次，自己拼就等于把它们关掉。
    所以 ``--host 0.0.0.0`` 配上 ``auth = "none"`` 会在**这里**当场报错，
    而不是等有人从外面连上来之后才发现这台服务没有认证。

    没给参数时原样返回，不做「就地改一遍」——配置对象是 frozen 的，
    而把它换掉意味着 ``deps.config.web`` 与它不再是同一个东西。
    """
    if host is None and port is None:
        return web
    return replace(
        web,
        host=web.host if host is None else host,
        port=web.port if port is None else port,
    )


def _kernel(config: Config, registry: ServiceRegistry) -> PluginManager:
    """装一台插件管理器，**并且把登记表交出来**。

    没有直接复用 ``cli_plugins._manager``：那台把 ``ServiceRegistry`` 关在
    自己肚子里，而这里要往**同一台**上注册 ``WebChannel``——插件要发消息时
    ``registry.get_all(Channel)`` 找的就是它。少这一台，插件拿到的是另一个
    空登记表，症状是「插件里取渠道总是 ``None``」，而只有真写一个会发消息的
    插件才会碰到。

    ``rng`` 显式按 ``config.core.random_seed`` 播种：不播种的话每次启动
    抖动都不一样，而本项目的推演要能重放（P6）。
    """
    clock = RealClock(tz=resolve_timezone(config.core.timezone))
    return PluginManager(
        config=config,
        bus=EventBus(clock, logger=_log),
        registry=registry,
        clock=clock,
        scheduler=Scheduler(clock, logger=_log),
        logger=_log,
        rng=random.Random(config.core.random_seed),
    )


def _responder(
    config: Config,
    backend: SqliteStorageBackend,
    providers: Mapping[str, OpenAICompatibleProvider],
    persona: PersonaRecord,
) -> UtteranceHandler:
    """「用户说了一句话，让引擎回一句」这件事本身。

    它是**闭包**而不是一个类：它要的东西（库连接、供应商、人设）在装配完成
    之后就再也不会变，做成类会把「这些是常量」写成五六个字段。库连接尤其
    不能在这里开——每次请求开一次连接，等于每说一句话都重跑一遍 PRAGMA，
    还可能撞上别人手里的写锁。

    回出来的形状是 ``app.js`` 的 ``bubble()`` 直接能画的那一种
    （:func:`alterego.channels.web.views.message_view`），所以浏览器那一侧
    不需要认识 ``MessageRecord``。``decision`` 一起带上，是因为「它没秒回」
    是这个项目要模拟的东西之一，界面上得有个能读的解释（同 ``cli_chat._pace``）。
    """
    service = _service(config, backend, providers, persona=persona)

    async def respond(text: str, conversation_id: str) -> dict[str, Any]:
        # 消息 id 由这一层自己发（同 ``cli_chat._turn``）：库里按 id 去重，
        # 而两句话完全可能落在同一秒里。碰撞的代价是第一句被当成重发丢掉。
        inbound = InboundMessage(
            content=text,
            channel=CHANNEL_ID,
            message_id=f"web-{uuid.uuid4().hex}",
        )
        outcome = await service.reply(inbound)
        return {
            "conversation_id": outcome.conversation_id or conversation_id,
            "reply": message_view(outcome.reply) if outcome.reply is not None else None,
            "decision": {
                "mode": outcome.decision.mode,
                "reason": outcome.decision.reason,
                "delay_seconds": outcome.delay_seconds,
            },
            # 模型没写出来时那句「这一次没说成」在这里（见 ReplyOutcome.notes）。
            # 不带上的话，界面上「它决定不说」和「它说失败了」长得一模一样。
            "notes": list(outcome.notes),
        }

    return respond


def _deps(
    config: Config,
    backend: SqliteStorageBackend,
    providers: Mapping[str, OpenAICompatibleProvider],
    persona: PersonaRecord,
    *,
    manager: PluginManager,
    registry: ServiceRegistry,
) -> WebDeps:
    """把界面要的那些出口一次装齐。

    ``conversation_id`` 用的是 ``user_conversation_id(persona.id)``，
    与 ``ConversationService`` 内部算的是同一个——这条规则只写在一个地方，
    两边各拼一次的话，一旦拼法不一致，消息会写进一个没人读的会话里，
    而症状是「它不记得刚才聊过什么」。
    """
    conn = backend.connection
    broker = SSEBroker(
        max_connections=config.web.sse_max_connections,
        keepalive_seconds=float(config.web.sse_keepalive_seconds),
        logger=_log,
    )
    channel = WebChannel(broker, logger=_log)
    # 注册必须在 ``manager.load_all()`` **之前**：插件在加载时可能就去取渠道
    # （``on_start`` 里发一条「我起来了」）。反过来则拿到的是空登记表。
    registry.register(Channel, channel, name=CHANNEL_ID)
    return WebDeps(
        config=config,
        auth=gate_for(config.web, project_root=config.data_dir.parent),
        broker=broker,
        channel=channel,
        registry=registry,
        logger=_log,
        manager=manager,
        persona_id=persona.id,
        conversation_id=user_conversation_id(persona.id),
        respond=_responder(config, backend, providers, persona),
        personas=SqlitePersonaRepository(conn),
        conversations=SqliteConversationRepository(conn),
        posts=SqliteSocialPostRepository(conn),
        emotions=SqliteEmotionRepository(conn),
        activities=SqliteActivityRepository(conn),
        memories=SqliteMemoryRepository(conn),
        budgets=SqliteBudgetRepository(conn),
        schedules=SqliteScheduleRepository(conn),
        tick_logs=SqliteTickLogRepository(conn),
        sources=SqliteSourceRepository(conn),
    )


# ── 输出 ────────────────────────────────────────────────────


def _url(config: Config) -> str:
    """给用户点的那条链接。

    ``0.0.0.0`` 是「听所有网卡」而不是一个能打开的地址，所以显示成
    ``127.0.0.1``：照原样打出来会让人点开一个连不上的链接，然后去怀疑
    服务根本没起来。
    """
    host = config.web.host
    shown = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{shown}:{config.web.port}/"


def _login_line(config: Config, gate: AuthGate, *, fresh_token: bool) -> str:
    """怎么进得去这一句。

    token **只在这一次是新生成的时候**打出来：一个刚生成的串此前没写进任何
    用户看得见的地方，不打出来他就只能自己去翻文件；而一个已经存在的串再打
    一遍是纯粹的泄露——``serve`` 的 stdout 经常被重定向进某个日志文件，
    而日志会被贴出来。
    """
    if gate.mode == "none":
        return '没开认证（auth = "none"，只会听本机）'
    if gate.mode == "password":
        return "要密码，用的是 [web] auth_password"
    if fresh_token:
        return f"首次运行，先打开 {_url(config)}?token={gate.token}"
    return f"token 在 {token_path(config.data_dir.parent)} 里（打开页面时带 ?token=）"


def _banner(
    config: Config,
    persona: PersonaRecord,
    gate: AuthGate,
    conversation_id: str,
    *,
    fresh_token: bool,
) -> None:
    """开场那几行。**先打再起服务**，出问题时这几行是唯一线索。"""
    _out(_RULE)
    _out(f"{_pad('人设', _LABEL)}{persona.name}（{persona.id}）")
    _out(f"{_pad('界面', _LABEL)}{_url(config)}")
    _out(f"{_pad('登录', _LABEL)}{_login_line(config, gate, fresh_token=fresh_token)}")
    _out(f"{_pad('会话', _LABEL)}{conversation_id}")
    _out(f"{_pad('回话', _LABEL)}[llm.routing] {_ROUTING_PURPOSE}（账记在 llm_usage 表）")
    _out(f"{_pad('配置', _LABEL)}{config.source or '内置默认值（还没跑过 alterego init）'}")
    _out(_RULE)


# ── 收摊与阻塞 ──────────────────────────────────────────────


def _wait_forever() -> None:
    """什么都不做地活着，直到 Ctrl+C。

    ``--no-web`` 时用得上。**不能就这么退出**：这个进程存在的意义就是让插件
    与它开着的那些连接长期在，立刻退出等于什么都没做，而退出码还是 0——
    一个骗人的成功。用 ``threading.Event().wait()`` 而不是自己转一个
    ``asyncio`` 循环：这里没有别的事要等，而一个空转的循环会白烧一个核。
    """
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        # Ctrl+C 在这个位置只表示「不跑了」，不是错误。
        _out("")


async def _shutdown(deps: WebDeps, manager: PluginManager, providers: Mapping[str, Any]) -> None:
    """收摊。顺序是**插件 → 渠道 → 供应商**，与装配相反。

    插件先停、渠道后关：插件的 ``on_stop`` 里可能还在往里发东西（一句「我先
    下去了」），渠道先关掉的话那些消息会静静地掉在地上，而症状是「它下线的
    时候没说话」。顺序颠倒不会报错，只会少一句话，所以这里写清楚。
    供应商最后收，理由同 ``cli_chat._drive``：网络连接是别人开的那一层，
    这里只负责关掉自己拿到的那些。
    """
    manager.shutdown()
    await deps.channel.aclose()
    for provider in providers.values():
        await provider.aclose()


async def _uvicorn(app: Any, *, host: str, port: int) -> None:
    """把应用交给 uvicorn，跑到 Ctrl+C。

    ``log_config=None`` 是关键：不写的话 uvicorn 会**换掉整套 logging 配置**，
    于是 :func:`alterego.kernel.logging.setup_logging` 装好的那个 handler
    （带脱敏过滤器）被顶掉，而症状是「日志里突然出现明文密钥」。红线第 19 条
    把 handler 的构造权收归内核，就是为了让脱敏只有一处。

    import 写在函数里（理由同 ``channels/web/app.py``）：模块顶层 import
    uvicorn 会让 ``import alterego.cli_serve`` 直接抛 ``ModuleNotFoundError``，
    那句话没有告诉用户该装什么。
    """
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - 取决于环境里装没装可选依赖
        raise ChannelError(describe_missing_extra(exc), missing=exc.name) from exc
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,
        server_header=False,
        date_header=False,
    )
    await uvicorn.Server(config).serve()


async def _serve(
    config: Config,
    backend: SqliteStorageBackend,
    providers: Mapping[str, OpenAICompatibleProvider],
    *,
    no_web: bool,
    persona_name: str | None,
) -> int:
    """装配 → 起服务 → 收摊。"""
    registry = ServiceRegistry(logger=_log)
    manager = _kernel(config, registry)
    persona = _resolve_persona(backend.connection, name=persona_name)
    # token 是不是刚生成的，只有**构造之前**问得出来（``gate_for`` 会顺手写盘）。
    fresh_token = not token_path(config.data_dir.parent).exists()
    deps = _deps(config, backend, providers, persona, manager=manager, registry=registry)

    report = manager.load_all()
    _banner(config, persona, deps.auth, deps.conversation_id, fresh_token=fresh_token)
    _print_load_report(report)

    start = not no_web and config.web.enabled
    if not start:
        _out(
            "只装插件，不开界面"
            f"（{'--no-web' if no_web else '[web] enabled = false'}）；Ctrl+C 结束。"
        )
    try:
        if start:
            app = create_app(deps)
            _log.info("Web 界面起在 %s", _url(config))
            await _uvicorn(app, host=config.web.host, port=config.web.port)
            return 0
        _wait_forever()
        return 0
    finally:
        await _shutdown(deps, manager, providers)


# ── 命令 ────────────────────────────────────────────────────


def _port(text: str) -> int:
    """``--port`` 的值。范围在这里就拦下。

    用 argparse 的 ``type`` 而不是留到构造 ``WebConfig`` 时再报错：端口敲错了
    是**命令写错了**，让 argparse 打用法提示并退 2，好过一路装到一半才抛
    ``ConfigError``——那时候插件已经加载过一轮了。
    """
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"端口得是数字：{text}") from exc
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"端口得在 1..65535 之间：{value}")
    return value


def _run(args: argparse.Namespace) -> int:
    """装配 → 起服务。"""
    config = _serve_config()
    _require_sqlite(config)
    try:
        config = replace(config, web=_web_config(config.web, host=args.host, port=args.port))
    except AlterEgoError as exc:
        # 非法的 --host/--port 组合在 ``WebConfig.__post_init__`` 里就被拦下，
        # 这里只是把那句话按本项目统一的样子打出来。
        return _fail(exc)
    if not config.web.enabled and not args.no_web:
        _err("配置里 [web] enabled = false，这次只装插件、不开界面。")
    setup_logging(level=config.core.log_level, log_format=config.core.log_format)

    try:
        providers = _providers(config)
        with _open_db(config) as backend:
            return asyncio.run(
                _serve(
                    config,
                    backend,
                    providers,
                    no_web=bool(args.no_web),
                    persona_name=args.persona,
                )
            )
    except AlterEgoError as exc:
        return _fail(exc)


def cmd_serve(args: argparse.Namespace) -> int:
    """``alterego serve``。"""
    return _run(args)


def add_serve_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """把 ``serve`` 命令挂到顶层子命令上。

    **它没有子命令层**（同 ``chat``）：``serve`` 本身就是一个动作。
    """
    serve_parser = commands.add_parser(
        "serve",
        help="起常驻进程：Web 界面 + 插件（Ctrl+C 结束）",
    )
    serve_parser.add_argument(
        "--host",
        default=None,
        metavar="IP",
        help="只听哪个地址（默认取 [web] host）",
    )
    serve_parser.add_argument(
        "--port",
        default=None,
        metavar="N",
        type=_port,
        help="听哪个端口（默认取 [web] port）",
    )
    serve_parser.add_argument(
        "--no-web",
        action="store_true",
        help="只装插件、不开界面（这样不需要 fastapi/uvicorn）",
    )
    serve_parser.add_argument(
        "--persona",
        default=None,
        metavar="NAME",
        help="开哪个人设的界面（默认：库里只有一个时自动选它）",
    )
    serve_parser.set_defaults(handler=cmd_serve)

"""``alterego memory`` —— 记忆梳理命令。

**这是第三个组装根**，与 ``cli.py`` / ``cli_db.py`` 并列：只有这几个文件知道
「存储用的是 SQLite」「模型走的是哪个供应商」，其余代码一律只认
``StorageBackend`` / ``MemoryRepository`` / ``LLMGateway`` 这些协议。
``scripts/check_architecture.sh`` 第 3 组红线把这件事钉住了。

两条命令，分得很清楚：

- ``distill``（梳理**数据**）：读过没梳理过的行为日志，提炼成记忆；
- ``consolidate``（梳理**记忆**）：把零散的 episodic 记忆归纳成 semantic。

两条都能 ``--dry-run``：先看看素材长什么样，再决定要不要花这笔钱。

依据: docs/design/03-data-model.md § 6.5、docs/design/05-channels.md § 8.1
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

from alterego.cli_db import _open_db, _require_sqlite
from alterego.cli_io import _RULE, _err, _out
from alterego.domain.memory import CONSOLIDATION_IMPORTANCE_FACTOR
from alterego.interfaces.llm import LLMProvider
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.config import Config
from alterego.kernel.errors import AlterEgoError, ConfigError, StorageError
from alterego.kernel.logging import get_logger
from alterego.kernel.registry import ServiceRegistry
from alterego.llm import LLMGateway, PromptLibrary
from alterego.llm.providers import OpenAICompatibleProvider
from alterego.sim.consolidation import (
    PURPOSE,
    MemoryWorkbench,
    WorkReport,
    consolidate,
    distill,
)
from alterego.storage.sqlite import (
    SqliteActivityRepository,
    SqliteConnection,
    SqliteMemoryRepository,
    SqliteStorageBackend,
    SqliteUsageRepository,
)


__all__ = ["add_memory_parser", "cmd_memory_consolidate", "cmd_memory_distill"]

_log = get_logger("cli.memory")

_DECLINED: Final[dict[str, str]] = {
    "distill": "模型认为这段时间没有值得记住的事。素材已标记为梳理过，不会重复问。",
    "consolidate": "模型认为这些经历还归纳不出新的认识。源记忆已标记为巩固过。",
}
"""模型返回空数组时说什么。它不是失败，所以不算错误。"""


# ── 配置与装配 ──────────────────────────────────────────────


def _memory_config() -> Config:
    """读配置。单独一个函数，是为了给测试一个能换掉的接口。"""
    return Config.load()


def _now(config: Config) -> datetime:
    """当前时刻，按配置的时区。

    这里是组装根，取墙上时间是**对的**——推演循环里那个「虚拟现在」由
    ``ctx.clock`` 提供（P6），但手动敲命令时，现在就是现在。
    """
    return datetime.now(resolve_timezone(config.core.timezone))


def _provider(
    name: str, spec: Mapping[str, Any], *, timeout_sec: float
) -> OpenAICompatibleProvider:
    """按配置里的一个段落造一个供应商。

    ``base_url`` 宁可在这里报错，也不要在别处猜一个默认值：猜错了会打到
    别人的端点上，而那种失败长得像网络故障，查起来要很久。
    """
    base_url = spec.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ConfigError(
            "模型供应商没有配置 base_url",
            provider=name,
            hint=(
                f"在 config/alterego.toml 里补上：\n"
                f"  [llm.providers.{name}]\n"
                f'  base_url = "https://你的端点/v1"\n'
                f'  api_key_env = "你的密钥环境变量名"\n'
                f'  model = "你的模型名"'
            ),
        )

    model = spec.get("model")
    models = spec.get("models")
    headers = spec.get("extra_headers")
    return OpenAICompatibleProvider(
        name,
        base_url=base_url,
        api_key_env=str(spec.get("api_key_env") or ""),
        model=model if isinstance(model, str) else "",
        models=tuple(str(item) for item in models) if isinstance(models, (list, tuple)) else (),
        timeout_sec=timeout_sec,
        extra_headers=(
            {str(key): str(value) for key, value in headers.items()}
            if isinstance(headers, Mapping)
            else None
        ),
    )


def _providers(config: Config) -> dict[str, OpenAICompatibleProvider]:
    """造出配置里写到的**每一个**供应商，不只是这次要用的那个。

    只装用到的那一个，会让「改了 ``[llm.routing]`` 却忘了同步装配代码」
    变成一次运行期的失败；全装上，路由表改到哪里都能落地。
    """
    specs = config.llm.providers
    if not specs:
        raise ConfigError(
            "配置里一个模型供应商都没有",
            hint=(
                "在 config/alterego.toml 里加上：\n"
                "  [llm.providers.openai_compatible]\n"
                '  base_url = "https://api.openai.com/v1"\n'
                '  api_key_env = "OPENAI_API_KEY"\n'
                '  model = "gpt-4o-mini"'
            ),
        )
    return {
        str(name): _provider(str(name), spec, timeout_sec=float(config.llm.timeout_seconds))
        for name, spec in specs.items()
        if isinstance(spec, Mapping)
    }


def _registry(providers: Mapping[str, OpenAICompatibleProvider]) -> ServiceRegistry:
    """把供应商装进注册处。网关按 ``[llm.routing]`` 里的名字来取。

    键必须是**接口**（``LLMProvider``）而不是具体类。注册表按接口类型索引，
    用具体类注册、拿 Protocol 去查，查出来只会是「一个都没有」——
    而这个错误直到第一次真调模型才会露面。
    """
    registry = ServiceRegistry(logger=_log)
    for name, provider in providers.items():
        registry.register(LLMProvider, provider, name=name)
    return registry


def _resolve_persona(conn: SqliteConnection, *, name: str | None) -> tuple[str, str]:
    """找到这次要梳理谁，返回 ``(id, name)``。

    ``--persona`` 给的是**名字**不是 id：敲命令的人手上有名字，
    而 id 是他从没见过的一串字符。
    """
    if name:
        rows = conn.query("SELECT id, name FROM persona WHERE name = ?", (name,))
    else:
        rows = conn.query("SELECT id, name FROM persona ORDER BY created_at, id")

    if not rows:
        raise StorageError(
            "数据库里还没有人设",
            name=name or "",
            hint="先跑 `alterego init` 生成一个人设，再回来梳理记忆。",
        )
    if len(rows) > 1:
        raise StorageError(
            "有多个人设，请用 --persona 指明是哪一个",
            candidates=", ".join(str(row["name"]) for row in rows),
        )
    return str(rows[0]["id"]), str(rows[0]["name"])


def _workbench(
    config: Config,
    backend: SqliteStorageBackend,
    providers: Mapping[str, OpenAICompatibleProvider],
    *,
    persona_id: str,
    persona_name: str,
) -> MemoryWorkbench:
    """把一次梳理要用的东西全装上。

    用量账本（``SqliteUsageRepository``）挂在这里而不是网关内部：网关只认识
    ``UsageSink`` 这个形状，它不知道账记在 SQLite 里——那正是第 4 组红线
    要求的（``llm/`` 不得依赖 ``storage/``）。
    """
    return MemoryWorkbench(
        persona_id=persona_id,
        persona_name=persona_name,
        user_name=config.core.user_name,
        gateway=LLMGateway(
            _registry(providers),
            config.llm.routing,
            max_retries=config.llm.max_retries,
            usage_sink=SqliteUsageRepository(backend.connection, persona_id=persona_id),
            logger=_log,
        ),
        prompts=PromptLibrary(),
        memories=SqliteMemoryRepository(backend.connection),
        activities=SqliteActivityRepository(backend.connection),
        backend=backend,
        new_id=lambda: uuid.uuid4().hex,
        logger=_log,
    )


async def _drive(
    work: MemoryWorkbench,
    providers: Mapping[str, OpenAICompatibleProvider],
    *,
    kind: str,
    now: datetime,
    dry_run: bool,
) -> WorkReport:
    """跑一次梳理，无论如何都把网络连接收掉。

    供应商的 ``httpx.AsyncClient`` 绑在事件循环上，而命令行只跑一次循环；
    不显式 ``aclose()``，进程退出时会留下一串未关闭连接的告警。
    """
    try:
        if kind == "consolidate":
            return await consolidate(work, now=now, dry_run=dry_run)
        return await distill(work, now=now, dry_run=dry_run)
    finally:
        for provider in providers.values():
            await provider.aclose()


# ── 输出 ────────────────────────────────────────────────────


def _report(report: WorkReport, *, dry_run: bool, declined: str) -> int:
    if report.skipped is not None:
        _out(f"跳过      {report.skipped}")
        return 0

    if dry_run:
        _out(f"素材      {report.source_items} 条")
        _out(_RULE)
        for line in report.preview:
            _out(f"  {line}")
        _out("")
        _out("这只是预演：没有调用模型，也没有写任何东西。")
        return 0

    _out(f"素材      {report.source_items} 条")
    _out(f"生成      {report.drafted} 条")
    _out(f"写入      {report.saved} 条")
    if report.downgraded:
        _out(f"降权      {report.downgraded} 条（×{CONSOLIDATION_IMPORTANCE_FACTOR}）")
    _out("")
    if report.saved == 0:
        # 不是失败。模型说「今天没什么值得记的」是一个结论，
        # 素材照样被标记为处理过，不会每次都被重新问一遍。
        _out(declined)
    return 0


def _fail(exc: AlterEgoError) -> int:
    """把异常打成人看得懂的样子，并给出与 ``main()`` 一致的退出码。

    不直接 ``str(exc)``：它会把上下文一起塞进括号里，于是同一句话打两遍，
    而第一遍的引号与转义还都是给机器看的。这里只取 ``message``，
    上下文逐行列出，空值跳过。
    """
    _err(f"错误：{exc.message}")
    for key, value in exc.context.items():
        if value is None or value == "":
            continue
        _err(f"  {key}：{value}")
    return 2


# ── 命令 ────────────────────────────────────────────────────


def _run(args: argparse.Namespace, *, kind: str) -> int:
    """两条命令共用的骨架：装配 → 跑 → 报告。"""
    config = _memory_config()
    _require_sqlite(config)
    now = _now(config)
    dry_run = bool(args.dry_run)

    try:
        # --dry-run 只是把素材摊开给你看。一台还没配密钥的机器也应该能跑它——
        # 「先看看会交给模型什么、再把密钥填上」是更自然的顺序。
        providers: dict[str, OpenAICompatibleProvider] = {} if dry_run else _providers(config)
        with _open_db(config) as backend:
            persona_id, persona_name = _resolve_persona(backend.connection, name=args.persona)
            work = _workbench(
                config,
                backend,
                providers,
                persona_id=persona_id,
                persona_name=persona_name,
            )
            report = asyncio.run(_drive(work, providers, kind=kind, now=now, dry_run=dry_run))
    except AlterEgoError as exc:
        return _fail(exc)

    _out(f"人设      {persona_name}（{persona_id}）")
    _out(f"时刻      {now:%Y-%m-%d %H:%M}")
    if not dry_run:
        _out(f"计费      [llm.routing] {PURPOSE}（账记在 llm_usage 表）")
    _out(_RULE)
    return _report(report, dry_run=dry_run, declined=_DECLINED[kind])


def cmd_memory_distill(args: argparse.Namespace) -> int:
    """梳理**数据**：把做过的事（行为日志）提炼成记忆。"""
    return _run(args, kind="distill")


def cmd_memory_consolidate(args: argparse.Namespace) -> int:
    """梳理**记忆**：把零散的经历归纳成认知。"""
    return _run(args, kind="consolidate")


# ── 命令行 ──────────────────────────────────────────────────


def _add_common(parser: argparse.ArgumentParser) -> None:
    """两条命令共有的两个开关。"""
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只把将要交给模型的素材摊开给你看，不调用模型、不写库",
    )
    parser.add_argument(
        "--persona",
        default=None,
        metavar="NAME",
        help="梳理哪个人设（默认：库里只有一个时自动选它）",
    )


def add_memory_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """把 ``memory`` 命令组挂到顶层子命令上。"""
    memory_parser = commands.add_parser("memory", help="记忆：梳理数据、巩固记忆")
    memory_commands = memory_parser.add_subparsers(
        dest="subcommand",
        metavar="{distill,consolidate}",
    )
    # 只敲到 `alterego memory` 时要打**这一层**的帮助，而不是顶层的。
    memory_parser.set_defaults(subparser=memory_parser)

    distill_parser = memory_commands.add_parser(
        "distill",
        help="梳理数据：把做过的事提炼成记忆",
    )
    _add_common(distill_parser)
    distill_parser.set_defaults(handler=cmd_memory_distill)

    consolidate_parser = memory_commands.add_parser(
        "consolidate",
        help="梳理记忆：把零散经历归纳成认知",
    )
    _add_common(consolidate_parser)
    consolidate_parser.set_defaults(handler=cmd_memory_consolidate)

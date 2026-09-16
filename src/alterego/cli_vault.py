"""``alterego vault`` —— Obsidian 知识库命令。

**这是第四个组装根**，与 ``cli.py`` / ``cli_db.py`` / ``cli_memory.py`` 并列。
只有这几个文件知道「存储用的是 SQLite」「模型走的是哪个供应商」，
其余代码一律只认 ``StorageBackend`` / ``ScheduleRepository`` / ``LLMGateway``
这些协议。``scripts/check_architecture.sh`` 第 3 组红线把这件事钉住了。

五个子命令，按使用顺序：

| 命令 | 干什么 | 花钱吗 |
| --- | --- | --- |
| ``init`` | 把库搭起来：目录、``.obsidian/``、索引页 | 不花 |
| ``sync`` | 库里的日程 / 想法 / 搜集到的 → 笔记，然后重建索引 | 不花 |
| ``organize`` | 让角色自己把收集箱里的东西归位，然后重建索引 | 花一次 |
| ``build`` | 手改过文件之后，重算索引并校验 | 不花 |
| ``status`` | 现在库里什么样，有没有坏链 | 不花 |

**全部用只读方式打开数据库。**

这不是保守，是这个功能的定义：知识库是库的**下游**，从不往回写
（`docs/plans/2026-09-16-obsidian-vault.md` § 6）。一旦允许回写，
「用户手改了一个 Markdown 文件」就得有冲突解决策略，
而那个策略没人会记得去维护。只读打开把这句设计声明变成了代码事实。

依据: docs/plans/2026-09-16-obsidian-vault.md § 3、§ 8
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from alterego.cli_db import _peek_db, _require_sqlite, _resolve_persona
from alterego.cli_io import _RULE, _err, _out
from alterego.domain.vault import CONTENT_FOLDERS, FOLDER_INBOX, FOLDER_INDEX, validate
from alterego.interfaces.llm import LLMProvider
from alterego.interfaces.repository import PersonaRecord
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.config import Config
from alterego.kernel.errors import AlterEgoError, ConfigError, StorageError
from alterego.kernel.logging import get_logger
from alterego.kernel.registry import ServiceRegistry
from alterego.llm import LLMGateway, PromptLibrary
from alterego.llm.providers import OpenAICompatibleProvider
from alterego.sim.vault import (
    PURPOSE,
    BuildReport,
    OrganizeReport,
    SyncReport,
    VaultWorkbench,
    build,
    init_vault,
    organize,
    scan,
    sync,
)
from alterego.storage.sqlite import (
    SqliteActivityRepository,
    SqliteMemoryRepository,
    SqliteScheduleRepository,
    SqliteSourceRepository,
    SqliteStorageBackend,
    SqliteUsageRepository,
)


__all__ = [
    "add_vault_parser",
    "cmd_vault_build",
    "cmd_vault_init",
    "cmd_vault_organize",
    "cmd_vault_status",
    "cmd_vault_sync",
]

_log = get_logger("cli.vault")

_VAULT_EXPORT_DIR: Final[str] = "exports"
"""默认把库建在这个目录下。``.gitignore`` 已经忽略了 ``exports/*``。"""

_MAX_ISSUES_SHOWN: Final[int] = 20
"""``status`` 最多列多少条问题。剩下一句「还有 N 条」带过。"""


# ── 配置与装配 ──────────────────────────────────────────────


def _vault_config() -> Config:
    """读配置。单独一个函数，是为了给测试一个能换掉的接口。"""
    return Config.load()


def _now(config: Config) -> datetime:
    """当前时刻，按配置的时区。

    这里是组装根，取墙上时间是**对的**——推演循环里那个「虚拟现在」由
    ``ctx.clock`` 提供（P6），但手动敲命令时，现在就是现在。
    """
    return datetime.now(resolve_timezone(config.core.timezone))


def _require_database(config: Config) -> None:
    """确认库文件在。

    ``cli_db._peek_db`` 在库不存在时会返回一个**内存库**，那是为了让
    ``db status`` 能跑。这里不行——后面要真查 ``persona`` / ``schedule_block``，
    内存库里没有表，会变成一句 ``no such table: persona`` 的运维日志。
    提前拦下来，说一句人话。
    """
    if not config.database_path.is_file():
        raise StorageError(
            "数据库还不存在",
            path=str(config.database_path),
            hint="先跑 `alterego init` 生成一个人设，再回来建知识库。",
        )


def _vault_root(raw: str | None, *, persona: PersonaRecord) -> Path:
    """库建在哪。

    ``--vault`` 是给「我有好几个角色，各自的库想分开放」的人用的。
    默认按角色名走：同一个实例里有两个人设时，它们**天然不会**写进同一个库，
    这一点比位置好不好看要紧得多——两个角色互相改对方的日记是没法修的。
    """
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.cwd() / _VAULT_EXPORT_DIR / f"{persona.name}的知识库").resolve()


def _registry(providers: Mapping[str, OpenAICompatibleProvider]) -> ServiceRegistry:
    """把供应商装进注册处。键必须是**接口**而不是具体类。"""
    registry = ServiceRegistry(logger=_log)
    for name, provider in providers.items():
        registry.register(LLMProvider, provider, name=name)
    return registry


def _workbench(
    config: Config,
    backend: SqliteStorageBackend,
    providers: Mapping[str, OpenAICompatibleProvider],
    *,
    persona: PersonaRecord,
    root: Path,
    ledger: SqliteStorageBackend | None = None,
) -> VaultWorkbench:
    """把一次知识库任务要用的东西全装上。

    用量账本（``SqliteUsageRepository``）挂在这里而不是网关内部：网关只认识
    ``UsageSink`` 这个形状，它不知道账记在 SQLite 里——那正是第 4 组红线
    要求的（``llm/`` 不得依赖 ``storage/``）。

    ``ledger`` 是单独的一条**可写**连接，只有真的要花钱的命令才传。
    内容连接是只读的（知识库是库的下游，从不往回写），但钱花过就得记账；
    把账本挂在只读连接上不会报错，只会在日志里留一句
    ``attempt to write a readonly database``，而命令照样打印「账记在
    llm_usage 表」——那比不记账还难查。

    三个仓储都是**只读**接口（``list_range`` / ``list_kept`` / ``list_recent``），
    和上面「知识库是下游」那句话是同一件事。
    """
    conn = backend.connection
    sink = backend if ledger is None else ledger
    return VaultWorkbench(
        persona=persona,
        user_name=config.core.user_name,
        root=root,
        gateway=LLMGateway(
            _registry(providers),
            config.llm.routing,
            max_retries=config.llm.max_retries,
            usage_sink=SqliteUsageRepository(sink.connection, persona_id=persona.id),
            logger=_log,
        ),
        prompts=PromptLibrary(),
        schedules=SqliteScheduleRepository(conn),
        activities=SqliteActivityRepository(conn),
        sources=SqliteSourceRepository(conn),
        memories=SqliteMemoryRepository(conn),
        logger=_log,
    )


async def _drive(
    work: VaultWorkbench,
    providers: Mapping[str, OpenAICompatibleProvider],
    *,
    kind: str,
    now: datetime,
    dry_run: bool,
    lookback_days: int,
) -> SyncReport | OrganizeReport | BuildReport:
    """跑一次任务，无论如何都把网络连接收掉。

    供应商的 ``httpx.AsyncClient`` 绑在事件循环上，而命令行只跑一次循环；
    不显式 ``aclose()``，进程退出时会留下一串未关闭连接的告警。
    """
    try:
        if kind == "sync":
            return sync(work, now=now, lookback_days=lookback_days)
        if kind == "organize":
            return await organize(work, now=now, dry_run=dry_run)
        return build(work, now=now)
    finally:
        for provider in providers.values():
            await provider.aclose()


# ── 输出 ────────────────────────────────────────────────────


def _issues_block(issues: Sequence[object], *, label: str = "问题") -> None:
    if not issues:
        _out(f"{label}      0 处")
        return
    _out(f"{label}      {len(issues)} 处")
    for issue in issues[:_MAX_ISSUES_SHOWN]:
        _out(f"  · {issue}")
    hidden = len(issues) - _MAX_ISSUES_SHOWN
    if hidden > 0:
        _out(f"  · ……还有 {hidden} 条")


def _report_sync(report: SyncReport, *, root: Path) -> int:
    if report.skipped is not None:
        _out(f"跳过      {report.skipped}")
        return 0
    _out(f"库        {root}")
    _out(_RULE)
    _out(f"日程      {report.days} 天")
    _out(f"想法      {report.thoughts} 条（内心独白单独成篇）")
    _out(f"读到的    {report.sources} 条")
    _out(f"记得的事  {report.memories} 条")
    _out(f"写出      {report.written} 个文件")
    _out("")
    if report.inbox:
        _out(f"收集箱里还有 {report.inbox} 篇等着整理：`alterego vault organize`")
    else:
        _out("收集箱是空的，没有等着整理的东西。")
    return 0


def _report_organize(report: OrganizeReport, *, root: Path, dry_run: bool) -> int:
    if report.skipped is not None:
        _out(f"跳过      {report.skipped}")
        return 0

    _out(f"库        {root}")
    _out(f"收集箱    {report.read} 篇")
    _out(_RULE)

    if dry_run:
        for line in report.preview:
            _out(f"  {line}")
        remaining = report.read - len(report.preview)
        if remaining > 0:
            _out(f"  ……还有 {remaining} 篇这次没读出来")
        _out("")
        _out("这只是预演：没有调用模型，也没有写任何东西。")
        return 0

    _out(f"归位      {report.filed} 篇")
    if report.rejected:
        _out(f"跳过      {len(report.rejected)} 条（留在收集箱里，下次还会问）")
        for label, reason in report.rejected:
            _out(f"  · {label}：{reason}")
    _out("")
    _issues_block(report.issues)
    return 0


def _report_build(report: BuildReport, *, root: Path) -> int:
    _out(f"库        {root}")
    _out(_RULE)
    _out(f"扫描      {report.scanned} 篇")
    _out(f"重建      {report.indexed} 个索引页")
    _out("")
    _issues_block(report.issues)
    return 0


def _report_status(work: VaultWorkbench, *, now: datetime) -> int:
    """把库扫一遍，报数，不写任何东西。"""
    if not work.root.is_dir():
        _out(f"知识库 · {work.root}")
        _out(_RULE)
        _out("还没建。跑 `alterego vault init` 把它搭起来。")
        return 0

    notes = scan(work.root, now=now, logger=_log)
    issues = validate(notes)

    _out(f"知识库 · {work.root}")
    _out(f"人设      {work.persona.name}（{work.persona.id}）")
    _out(_RULE)

    for folder in CONTENT_FOLDERS:
        count = sum(1 for note in notes if note.folder == folder)
        _out(f"  {folder}    {count} 篇")
    inbox = sum(1 for note in notes if note.folder == FOLDER_INBOX)
    index = sum(1 for note in notes if note.folder == FOLDER_INDEX)
    _out("")
    _out(f"索引      {index} 个")
    _out(f"收集箱    {inbox} 篇")
    _out("")
    _issues_block(issues)

    if inbox and not issues:
        _out("")
        _out("收集箱里堆着东西：`alterego vault organize` 让角色自己整理。")
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


def _prepare(
    args: argparse.Namespace,
) -> tuple[Config, SqliteStorageBackend, PersonaRecord, Path, datetime]:
    """五个命令共用的开头：读配置 → 开只读库 → 找到人设 → 算出库在哪。

    这一步里**先开连接、后找人设**，于是两条最常见的错误路径（没有这个
    名字、有多个人设）都是「连接已经开着的时候抛出去」。不在这里关掉，
    那个连接只能等垃圾回收，而回收时刻由解释器决定——它会变成一条飘忽的
    ``unclosed database`` 警告，落在哪一次运行头上全看运气。
    """
    config = _vault_config()
    _require_sqlite(config)
    _require_database(config)
    now = _now(config)
    backend = _peek_db(config)
    try:
        persona = _resolve_persona(backend.connection, name=args.persona)
        root = _vault_root(args.vault, persona=persona)
    except Exception:
        backend.close()
        raise
    return config, backend, persona, root, now


def _run(args: argparse.Namespace, *, kind: str) -> int:
    """四个「跑一次」的命令共用的骨架：装配 → 跑 → 报告。"""
    config, backend, persona, root, now = _prepare(args)
    dry_run = bool(getattr(args, "dry_run", False))
    lookback_days = int(getattr(args, "lookback_days", 7) or 7)
    # 先置空：下面取供应商就可能抛，而 ``except`` 里要关它。
    ledger: SqliteStorageBackend | None = None

    try:
        # 只有真的要整理时才需要供应商。别的命令配上空注册处照样跑得动——
        # 一台还没填密钥的机器应该能先把自己的日程写成笔记。
        needs_llm = kind == "organize" and not dry_run
        providers = _providers(config) if needs_llm else {}
        # 账本单开一条可写连接。内容连接是只读的，两者不能共用。
        ledger = SqliteStorageBackend.open(config.database_path) if needs_llm else None
        work = _workbench(
            config,
            backend,
            providers,
            persona=persona,
            root=root,
            ledger=ledger,
        )
        report = asyncio.run(
            _drive(
                work,
                providers,
                kind=kind,
                now=now,
                dry_run=dry_run,
                lookback_days=lookback_days,
            )
        )
    except AlterEgoError as exc:
        backend.close()
        if ledger is not None:
            ledger.close()
        return _fail(exc)

    _out(f"人设      {persona.name}（{persona.id}）")
    _out(f"时刻      {now:%Y-%m-%d %H:%M}")
    if needs_llm:
        _out(f"计费      [llm.routing] {PURPOSE}（账记在 llm_usage 表）")
    _out(_RULE)
    backend.close()
    if ledger is not None:
        ledger.close()

    if isinstance(report, SyncReport):
        return _report_sync(report, root=root)
    if isinstance(report, OrganizeReport):
        return _report_organize(report, root=root, dry_run=dry_run)
    return _report_build(report, root=root)


def _providers(config: Config) -> dict[str, OpenAICompatibleProvider]:
    """造出配置里写到的供应商。

    和 ``cli_memory._providers`` 同一件事。这里只装这次用得上的那个是**不行的**：
    ``[llm.routing] vault`` 可能指向一个别名（``vault = "cheap"``），
    而别名指向谁只有把整张表装齐了才知道。少装一个的话，用户改一次路由表
    就会在运行期撞上一句「provider 不存在」。
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

    built: dict[str, OpenAICompatibleProvider] = {}
    for name, spec in specs.items():
        if not isinstance(spec, Mapping):
            continue
        base_url = spec.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ConfigError(
                "模型供应商没有配置 base_url",
                provider=str(name),
                hint=(
                    f"在 config/alterego.toml 里补上：\n"
                    f"  [llm.providers.{name}]\n"
                    f'  base_url = "https://你的端点/v1"\n'
                    f'  api_key_env = "你的密钥环境变量名"\n'
                    f'  model = "你的模型名"'
                ),
            )
        headers = spec.get("extra_headers")
        models = spec.get("models")
        model = spec.get("model")
        built[str(name)] = OpenAICompatibleProvider(
            str(name),
            base_url=base_url,
            api_key_env=str(spec.get("api_key_env") or ""),
            model=model if isinstance(model, str) else "",
            models=(
                tuple(str(item) for item in models) if isinstance(models, (list, tuple)) else ()
            ),
            timeout_sec=float(config.llm.timeout_seconds),
            extra_headers=(
                {str(key): str(value) for key, value in headers.items()}
                if isinstance(headers, Mapping)
                else None
            ),
        )
    return built


def cmd_vault_init(args: argparse.Namespace) -> int:
    """把库搭起来。重复跑是安全的：已有内容不会被动。"""
    config, backend, persona, root, now = _prepare(args)
    try:
        work = _workbench(config, backend, {}, persona=persona, root=root)
        created = init_vault(work, now=now)
    except AlterEgoError as exc:
        backend.close()
        return _fail(exc)
    backend.close()

    _out(f"人设      {persona.name}（{persona.id}）")
    _out(f"库        {root}")
    _out(_RULE)
    _out(f"建好      {len(created)} 个文件")
    _out("")
    _out("用 Obsidian「打开文件夹作为库」指到上面这个路径就行，不用装任何插件。")
    _out("下一步：`alterego vault sync` 把已有的日程搬进去。")
    return 0


def cmd_vault_sync(args: argparse.Namespace) -> int:
    """把库里的事渲染成笔记。不调用模型，不花钱。"""
    return _run(args, kind="sync")


def cmd_vault_organize(args: argparse.Namespace) -> int:
    """让角色自己把收集箱里的东西归位。--dry-run 先看看有什么。"""
    return _run(args, kind="organize")


def cmd_vault_build(args: argparse.Namespace) -> int:
    """按实际文件重算索引页，并校验四条不变量。"""
    return _run(args, kind="build")


def cmd_vault_status(args: argparse.Namespace) -> int:
    """现在库里什么样，有没有坏链。只读，一个文件都不写。"""
    config, backend, persona, root, now = _prepare(args)
    work = _workbench(config, backend, {}, persona=persona, root=root)
    try:
        return _report_status(work, now=now)
    except AlterEgoError as exc:
        return _fail(exc)
    finally:
        backend.close()


# ── 命令行 ──────────────────────────────────────────────────


def _add_common(parser: argparse.ArgumentParser) -> None:
    """五个命令共有的两个开关。"""
    parser.add_argument(
        "--vault",
        default=None,
        metavar="DIR",
        help="知识库建在哪（默认：exports/<角色名>的知识库）",
    )
    parser.add_argument(
        "--persona",
        default=None,
        metavar="NAME",
        help="给哪个人设建（默认：库里只有一个时自动选它）",
    )


def add_vault_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """把 ``vault`` 命令组挂到顶层子命令上。"""
    vault_parser = commands.add_parser("vault", help="知识库：Obsidian，它自己写、自己整理")
    vault_commands = vault_parser.add_subparsers(
        dest="subcommand",
        metavar="{init,sync,organize,build,status}",
    )
    # 只敲到 `alterego vault` 时要打**这一层**的帮助，而不是顶层的。
    vault_parser.set_defaults(subparser=vault_parser)

    init_parser = vault_commands.add_parser("init", help="把库搭起来：目录、.obsidian、索引页")
    _add_common(init_parser)
    init_parser.set_defaults(handler=cmd_vault_init)

    sync_parser = vault_commands.add_parser("sync", help="库里的日程 / 想法 / 搜集到的 → 笔记")
    _add_common(sync_parser)
    sync_parser.add_argument(
        "--lookback-days",
        type=int,
        default=7,
        metavar="N",
        help="往回看几天（默认 7）",
    )
    sync_parser.set_defaults(handler=cmd_vault_sync)

    organize_parser = vault_commands.add_parser(
        "organize",
        help="让角色自己把收集箱里的东西归位（会调用模型）",
    )
    _add_common(organize_parser)
    organize_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只把收集箱摊开给你看，不调用模型、不写文件",
    )
    organize_parser.set_defaults(handler=cmd_vault_organize)

    build_parser = vault_commands.add_parser("build", help="重算索引页并校验（手改过文件之后用）")
    _add_common(build_parser)
    build_parser.set_defaults(handler=cmd_vault_build)

    status_parser = vault_commands.add_parser("status", help="现在库里什么样，有没有坏链")
    _add_common(status_parser)
    status_parser.set_defaults(handler=cmd_vault_status)

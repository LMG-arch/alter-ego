"""``alterego study`` —— 专项学习：它自己把本行的东西一格一格补起来。

第六个组装根（``cli`` / ``cli_db`` / ``cli_memory`` / ``cli_vault`` /
``cli_dataset`` 之后的这个）。

**为什么和 ``cli_vault.py`` 拆成两个文件而不是塞进一个。** 它们共用一个知识库、
一套装配、一句话能说清的同一件事，但命令组是两件事：``alterego vault`` 管的是
**搬进来和整理好**（日程、想法、读到的东西），``alterego study`` 管的是
**往外学**。混在一起之后，``vault`` 这一组会开始需要 ``[study]`` 段的配置，
而一个只想把日程写成笔记的人不该被要求先填「你学什么专业」。

**共用的那几样直接从 ``cli_vault`` 拿。** ``_prepare`` / ``_providers`` /
``_registry`` / ``_fail`` 在两边必须是同一份：「库在哪」有两个真源时，
``vault`` 和 ``study`` 会往不同的目录里写，而用户要到很后面才会发现。
``cli_db`` 被 ``cli_vault`` 复用也是这个形状，不是新做法。

依据: docs/plans/2026-09-16-specialized-study.md § 6–7
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from alterego.cli_db import _open_db
from alterego.cli_io import _RULE, _out, _pad
from alterego.cli_vault import _fail, _issues_block, _prepare, _providers, _registry
from alterego.domain.study import DEFAULT_CONTEXT_BUDGET, Recall, Topic, render_context
from alterego.interfaces.repository import PersonaRecord
from alterego.kernel.config import Config
from alterego.kernel.errors import AlterEgoError
from alterego.kernel.logging import get_logger
from alterego.llm import LLMGateway, PromptLibrary
from alterego.llm.providers import OpenAICompatibleProvider
from alterego.sim.study import (
    PURPOSE,
    LearnReport,
    ReadReport,
    StudyWorkbench,
    learn,
    plan,
    read,
    recall,
    resolve,
)
from alterego.storage.sqlite import SqliteStorageBackend, SqliteUsageRepository


__all__ = [
    "add_study_parser",
    "cmd_study_next",
    "cmd_study_plan",
    "cmd_study_recall",
    "cmd_study_status",
]


_log = get_logger("cli.study")

_LABEL_COLUMNS: Final[int] = 10
"""左边那一列（「领域」「进度」…）的宽度，和别的组装根对齐。"""


# ── 装配 ────────────────────────────────────────────────────


def _workbench(
    config: Config,
    backend: SqliteStorageBackend,
    providers: Mapping[str, OpenAICompatibleProvider],
    *,
    persona: PersonaRecord,
    root: Path,
    ledger: SqliteStorageBackend | None = None,
) -> StudyWorkbench:
    """把一次学习任务要用的东西装上。

    参数顺序和 ``cli_vault._workbench`` 一模一样，不是巧合：两边装的其实是
    同一套东西，读起来应该长得也一样。

    账本（``SqliteUsageRepository``）挂在网关外面而不是里面：网关只认识
    ``UsageSink`` 这个形状，它不知道账记在 SQLite 里——那正是第 4 组红线
    要求的（``llm/`` 不得依赖 ``storage/``）。

    ``ledger`` 是单独一条**可写**连接，只有真的要花钱的命令才传。账本挂在
    只读连接上不会报错，只会在日志里留一句 ``attempt to write a readonly
    database``，而命令照样打印「账记在 llm_usage 表」——那比不记账还难查。
    """
    sink = backend if ledger is None else ledger
    return StudyWorkbench(
        persona=persona,
        root=root,
        gateway=LLMGateway(
            _registry(providers),
            config.llm.routing,
            max_retries=config.llm.max_retries,
            usage_sink=SqliteUsageRepository(sink.connection, persona_id=persona.id),
            logger=_log,
        ),
        prompts=PromptLibrary(),
        logger=_log,
        field=config.study.field,
        rounds=config.study.rounds,
        recall_limit=config.study.recall_limit,
        min_score=config.study.min_score,
    )


async def _drive_learn(
    work: StudyWorkbench,
    providers: Mapping[str, OpenAICompatibleProvider],
    *,
    now: datetime,
    rounds: int,
    dry_run: bool,
) -> LearnReport:
    """跑一次学习，无论如何都把网络连接收掉。

    供应商的 ``httpx.AsyncClient`` 绑在事件循环上，而命令行只跑一次循环；
    不显式 ``aclose()``，进程退出时会留下一串未关闭连接的告警。
    """
    try:
        return await learn(work, now=now, rounds=rounds, dry_run=dry_run)
    finally:
        for provider in providers.values():
            await provider.aclose()


# ── 输出 ────────────────────────────────────────────────────


def _line(label: str, value: str) -> None:
    _out(f"{_pad(label, _LABEL_COLUMNS)}{value}")


def _field_line(report: ReadReport) -> bool:
    """打「学什么」那一行。返回是否认出了领域。"""
    field = report.field
    if field is None:
        _line("学什么", "还不知道")
        _out("")
        _out(report.hint)
        return False
    origin = (
        f"（从 occupation 认出来的：{field.evidence}）"
        if field.evidence
        else "（[study] field 里填的）"
    )
    _line("领域", f"{field.label}{origin}")
    return True


def _report_status(report: ReadReport) -> int:
    """现在学到哪了。只读，不花钱。"""
    _line("库", str(report.root))
    if not _field_line(report):
        _out("")
        _out("认不出来就**不学**：瞎猜一个方向会让它写出一堆像模像样的空话，")
        _out("而没人会想到去检查。填上 `[study] field` 再说。")
        return 0

    _line("进度", f"{report.done} 格（第 {report.rounds_done} 轮学满）")
    _line("上次", report.last_at or "（还没学过）")
    _line("笔记", f"{report.total} 篇")
    if report.notes:
        _out(_RULE)
        for note in report.notes:
            _out(f"  · {note.title}")
            _out(f"      {note.path}")
    if report.unindexed:
        _out(_RULE)
        _line("没进索引", f"{len(report.unindexed)} 篇")
        for path in report.unindexed:
            _out(f"  · {path}")
        _out("")
        _out("`alterego vault build` 能把它们补进索引——孤儿的链接在 Obsidian 里点不开。")
    return 0


def _report_plan(report: ReadReport, topics: Sequence[Topic], *, rounds: int) -> int:
    """接下来学哪几格。只读，不花钱。"""
    _line("库", str(report.root))
    if not _field_line(report):
        return 0

    _line("进度", f"{report.done} 格（第 {report.rounds_done} 轮学满）")
    _out(_RULE)
    if not topics:
        _out(f"说要几格，但一格都排不出来——`--rounds` 得是正整数（现在是 {rounds}）。")
        return 0
    _out(f"接下来 {len(topics)} 格：")
    for index, topic in enumerate(topics, start=1):
        _out(f"  {index}. {topic.title}")
    _out("")
    _out("题面是**算出来的**，不是模型挑的：顺序固定，「上次学到哪」才有答案。")
    _out("真学下来跑 `alterego study next`（那一步会调用模型）。")
    return 0


def _report_recall(report: Recall, *, query: str, root: Path) -> int:
    """给一句话，看它会不会翻出专业笔记。"""
    _line("库", str(root))
    _line("问题", query)
    _line("门槛", f"{report.threshold:.1f}")
    _line("翻出", f"{len(report.matches)} 篇（在 {report.considered} 篇里找的）")
    _out(_RULE)
    if not report.matches:
        _out("一篇都没够门槛。**这是正常结果**——不是每句聊天都该翻出专业笔记。")
        _out("门槛调到 0 等于每次都硬塞，那比不翻更糟。")
        return 0
    for match in report.matches:
        _out(f"  · {match.title}（{match.score:.1f} 分）")
        _out(f"      命中：{' / '.join(match.terms)}")
        _out(f"      {root / match.path}")
    context = render_context(report, budget=DEFAULT_CONTEXT_BUDGET)
    _out(_RULE)
    _out(f"真到聊天里会拼进上下文的是这些（{len(context)} 字）：")
    _out("")
    _out(context)
    return 0


def _report_learn(report: LearnReport, *, rounds: int, dry_run: bool) -> int:
    """``next`` 干了什么。"""
    if report.skipped is not None:
        _line("跳过", report.skipped)
        return 0

    _line("领域", report.field_label)

    if dry_run:
        _line("这次会学", f"{len(report.preview)} 格")
        for index, topic in enumerate(report.preview, start=1):
            _out(f"  {index}. {topic.title}")
        _out("")
        _out("这只是预演：**没有调用模型，也没有写任何文件**。去掉 --dry-run 再跑。")
        return 0

    _line("学完", f"{len(report.learned)} 格（要了 {rounds} 格）")
    for path in report.learned:
        _out(f"  · {path}")
    if report.failed:
        _out("")
        _line("没写成", f"{len(report.failed)} 格——**没记进度，下次还会来**")
        for title, reason in report.failed:
            _out(f"  · {title}：{reason}")
    _out("")
    _issues_block(report.issues)
    if report.learned:
        _out("")
        _out("笔记在 60-专业 目录下，Obsidian 里打开就能看。")
    return 0


# ── 命令 ────────────────────────────────────────────────────


def cmd_study_status(args: argparse.Namespace) -> int:
    """现在学到哪了。只读，一个文件都不写，一分钱都不花。"""
    config, backend, persona, root, now = _prepare(args)
    work = _workbench(config, backend, {}, persona=persona, root=root)
    try:
        report = read(work, now=now)
    except AlterEgoError as exc:
        return _fail(exc)
    finally:
        backend.close()
    _line("人设", f"{persona.name}（{persona.id}）")
    return _report_status(report)


def cmd_study_plan(args: argparse.Namespace) -> int:
    """接下来会学哪几格。只读，不花钱。"""
    config, backend, persona, root, now = _prepare(args)
    rounds = int(getattr(args, "rounds", 0) or 0)
    work = _workbench(config, backend, {}, persona=persona, root=root)
    try:
        report = read(work, now=now)
        topics = plan(work, rounds=rounds)
    except AlterEgoError as exc:
        return _fail(exc)
    finally:
        backend.close()
    _line("人设", f"{persona.name}（{persona.id}）")
    return _report_plan(report, topics, rounds=rounds or work.rounds)


def cmd_study_recall(args: argparse.Namespace) -> int:
    """一句话 → 它会翻出哪几篇专业笔记。只读，不花钱。"""
    config, backend, persona, root, now = _prepare(args)
    query = str(args.query)
    work = _workbench(config, backend, {}, persona=persona, root=root)
    try:
        result = recall(
            work,
            now=now,
            query=query,
            limit=int(getattr(args, "limit", 0) or 0),
            min_score=float(getattr(args, "min_score", 0.0) or 0.0),
        )
    except AlterEgoError as exc:
        return _fail(exc)
    finally:
        backend.close()
    _line("人设", f"{persona.name}（{persona.id}）")
    return _report_recall(result, query=query, root=root)


def cmd_study_next(args: argparse.Namespace) -> int:
    """真的学几格。**这一步会调用模型、会花钱。**"""
    config, backend, persona, root, now = _prepare(args)
    dry_run = bool(getattr(args, "dry_run", False))
    rounds = int(getattr(args, "rounds", 0) or 0)
    ledger: SqliteStorageBackend | None = None

    _line("人设", f"{persona.name}（{persona.id}）")
    _line("时刻", f"{now:%Y-%m-%d %H:%M}")

    # 先认领域，再决定要不要造供应商。预演不花钱，而认不出「学什么」时
    # `learn()` 会直接返回「跳过」——**一行都不写**，所以那一步也不该
    # 要求用户先填密钥。探测只用空注册处：``resolve()`` 只看配置和人设。
    probe = _workbench(config, backend, {}, persona=persona, root=root)
    needs_llm = not dry_run and resolve(probe) is not None
    providers = _providers(config) if needs_llm else {}

    if needs_llm:
        _line("计费", f"[llm.routing] {PURPOSE}（账记在 llm_usage 表）")
    _out(_RULE)

    try:
        ledger = _open_db(config) if needs_llm else None
        work = _workbench(
            config,
            backend,
            providers,
            persona=persona,
            root=root,
            ledger=ledger,
        )
        report = asyncio.run(_drive_learn(work, providers, now=now, rounds=rounds, dry_run=dry_run))
    except AlterEgoError as exc:
        return _fail(exc)
    finally:
        backend.close()
        if ledger is not None:
            ledger.close()

    return _report_learn(report, rounds=rounds or work.rounds, dry_run=dry_run)


# ── 命令行 ──────────────────────────────────────────────────


def _add_common(parser: argparse.ArgumentParser) -> None:
    """四个命令共有的两个开关，和 ``alterego vault`` 完全一样。"""
    parser.add_argument(
        "--vault",
        default=None,
        metavar="DIR",
        help="知识库在哪（默认：exports/<角色名>的知识库，和 vault 命令是同一个）",
    )
    parser.add_argument(
        "--persona",
        default=None,
        metavar="NAME",
        help="学谁（默认：库里只有一个时自动选它）",
    )


def add_study_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """把 ``study`` 命令组挂到顶层子命令上。"""
    study_parser = commands.add_parser("study", help="专项学习：它自己一格一格补本行的东西")
    study_commands = study_parser.add_subparsers(
        dest="subcommand",
        metavar="{next,plan,recall,status}",
    )
    # 只敲到 `alterego study` 时要打**这一层**的帮助，而不是顶层的。
    study_parser.set_defaults(subparser=study_parser)

    next_parser = study_commands.add_parser("next", help="学几格，写进 60-专业（会调用模型）")
    _add_common(next_parser)
    next_parser.add_argument(
        "--rounds",
        type=int,
        default=0,
        metavar="N",
        help="这次学几格（默认：[study] rounds）",
    )
    next_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只说这次会学什么，不调用模型、不写文件",
    )
    next_parser.set_defaults(handler=cmd_study_next)

    plan_parser = study_commands.add_parser("plan", help="接下来会学哪几格（不花钱）")
    _add_common(plan_parser)
    plan_parser.add_argument(
        "--rounds",
        type=int,
        default=0,
        metavar="N",
        help="看几格（默认：[study] rounds）",
    )
    plan_parser.set_defaults(handler=cmd_study_plan)

    recall_parser = study_commands.add_parser("recall", help="给一句话，看它会翻出哪几篇（不花钱）")
    _add_common(recall_parser)
    recall_parser.add_argument("query", metavar="一句话", help="模拟对方说的一句话")
    recall_parser.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="最多翻几篇（默认：[study] recall_limit）",
    )
    recall_parser.add_argument(
        "--min-score",
        dest="min_score",
        type=float,
        default=0.0,
        metavar="F",
        help="分数门槛（默认：[study] min_score）",
    )
    recall_parser.set_defaults(handler=cmd_study_recall)

    status_parser = study_commands.add_parser("status", help="学到哪了、库里有什么（不花钱）")
    _add_common(status_parser)
    status_parser.set_defaults(handler=cmd_study_status)

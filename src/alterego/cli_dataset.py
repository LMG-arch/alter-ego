"""``alterego dataset`` —— 训练数据集导出命令。

**这是第五个组装根**，与 ``cli.py`` / ``cli_db.py`` / ``cli_memory.py`` /
``cli_vault.py`` 并列。只有这几个文件知道「存储用的是 SQLite」，其余代码
一律只认 ``DatasetSourceRepository`` 这样的协议。``scripts/check_architecture.sh``
第 3 组红线把这件事钉住了。

四个子命令：

| 命令 | 干什么 | 花钱吗 |
| --- | --- | --- |
| ``build`` | 取数 → 脱敏 → 写 ``.jsonl`` / ``manifest.json`` / ``README.md`` | 不花 |
| ``list`` | 磁盘上那一批什么样、该不该重跑 | 不花 |
| ``paths`` | 会落在哪几个文件（还没跑过也能问） | 不花 |
| ``show`` | 现场渲染几条出来看，一个文件都不写 | 不花 |

**一个模型都不调。** 这不是省事，是设计：训练集要的是「它当时到底怎么想的」，
而不是「现在问一遍模型当时大概怎么想的」——后者是伪造标注，而且不可复现（P6）、
每次重跑都要钱。所以这里既没有 ``[llm.routing]`` 的键，也没有 ``--provider``
之类的开关可以配错。

**全部用只读方式打开数据库。** 数据集是库的**下游**：只读 ``message`` /
``tick_log`` / ``activity_log`` 三张已有的表，不建新表、不往回写（ADR-0011）。
只读打开把这句设计声明变成了代码事实——一旦允许回写，「用户手改了一个
``.jsonl``」就得有冲突解决策略，而那个策略没人会记得去维护。

依据: docs/plans/2026-09-16-training-datasets.md § 3、§ 8
      docs/adr/0011-training-datasets-are-derived-and-redacted.md
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Final, cast

from alterego.cli_db import _peek_db, _require_sqlite, _resolve_persona
from alterego.cli_io import _RULE, _err, _human_size, _out, _pad
from alterego.domain.dataset import FORMATS, FormatName, spec_for
from alterego.domain.redact import REDACTION_RULES, digest
from alterego.interfaces.repository import PersonaRecord
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.config import Config
from alterego.kernel.errors import AlterEgoError, StorageError
from alterego.kernel.logging import get_logger
from alterego.sim.dataset import (
    MANIFEST_NAME,
    README_NAME,
    DatasetReport,
    DatasetStatus,
    DatasetWorkbench,
    PreviewItem,
    build,
    planned_paths,
    preview,
    scan,
)
from alterego.storage.sqlite import SqliteDatasetSourceRepository, SqliteStorageBackend


__all__ = [
    "add_dataset_parser",
    "cmd_dataset_build",
    "cmd_dataset_list",
    "cmd_dataset_paths",
    "cmd_dataset_show",
]

_log = get_logger("cli.dataset")

_LABEL_COLUMNS: Final[int] = 10
"""左列标签的宽度。``人设`` 占四列，补到十列正好对齐。"""

_KIND_COLUMNS: Final[int] = 16
"""数据集名一列的宽度。最长的 ``思考推理训练集`` 占十四列。"""

_FILE_COLUMNS: Final[int] = 30
"""文件名一列的宽度。``conversation.sharegpt.jsonl`` 是二十八个字符。"""

_DEFAULT_PER_KIND: Final[int] = 2
"""``show`` 每类默认看几条。两条够判断形状对不对，又不会刷屏。"""

_RULE_LABELS: Final[dict[str, str]] = {rule.name: rule.label for rule in REDACTION_RULES}
"""规则名 → 人话。``phone_cn`` 是给代码看的，``手机号`` 是给人看的。"""


# ── 配置与装配 ──────────────────────────────────────────────


def _dataset_config() -> Config:
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
    ``db status`` 能跑。这里不行——后面要真查 ``message`` / ``tick_log`` /
    ``activity_log``，内存库里没有表，会变成一句 ``no such table: message``
    的运维日志。提前拦下来，说一句人话。
    """
    if not config.database_path.is_file():
        raise StorageError(
            "数据库还不存在",
            path=str(config.database_path),
            hint="先跑 `alterego init` 生成一个人设，再回来导出数据集。",
        )


def _dataset_root(raw: str | None, *, persona: PersonaRecord, config: Config) -> Path:
    """数据集落在哪。

    ``--out`` 是给「我想直接导出到移动硬盘」的人用的。默认按角色名走：
    多个人设共用一份 ``[dataset] export_dir`` 时，它们**天然不会**写进同一个
    目录——两份脱敏规则不同、回答习惯不同的对话混在一起，
    训出来的模型会同时学会两个人的腔调，而且这事事后没法拆。
    """
    if raw:
        return Path(raw).expanduser().resolve()
    base = config.dataset.export_dir
    if not base.is_absolute():
        base = Path.cwd() / base
    return (base / persona.name).resolve()


def _workbench(
    config: Config,
    backend: SqliteStorageBackend,
    *,
    persona: PersonaRecord,
    root: Path,
) -> DatasetWorkbench:
    """把工作台装起来。这里只递协议，``sim/`` 不知道底下是 SQLite。"""
    # system_prompt 故意留空。真正的那份人设提示词还没定下来：它要由
    # persona_json 渲染（prompts/persona_generate.md），而 PersonaRecord
    # 有意不带 persona_json（见 interfaces/repository.py）。现在编一份塞进
    # 训练集，等于让模型学会一套运行时根本不会发给它的前言——比不带更糟。
    # 位置已经留好了：DatasetWorkbench.system_prompt。
    return DatasetWorkbench(
        persona=persona,
        user_name=config.core.user_name,
        root=root,
        sources=SqliteDatasetSourceRepository(backend.connection),
        logger=_log,
        redact_terms=config.dataset.redact_terms,
    )


def _formats(args: argparse.Namespace, config: Config) -> tuple[FormatName, ...]:
    """这一批出哪几种形状。

    ``--format`` 给了就只用它，没给就用 ``[dataset] formats`` 里那串。
    刻意**不做「命令行与配置合并」**：两个来源叠在一起时，用户永远说不清
    到底哪几个文件会被写出来，而多写一个文件比少写一个更难发现。
    想一次出多种就把它写进配置，一次跑完。
    """
    chosen = getattr(args, "fmt", None)
    if chosen:
        return (cast("FormatName", chosen),)
    return tuple(cast("FormatName", item) for item in config.dataset.formats)


def _current_digest(config: Config) -> str:
    """当前配置算出来的脱敏指纹，用来判断磁盘上那份是不是旧规则脱的。

    和 ``sim.dataset.build`` 里写进 manifest 的那个值走**同一个函数**，
    否则两边迟早算得不一样，然后「该不该重跑」就永远说不准。
    """
    return digest(extra_terms=config.dataset.redact_terms)


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


def _prepare(args: argparse.Namespace) -> tuple[Config, SqliteStorageBackend, PersonaRecord, Path]:
    """四个命令共用的开头：读配置 → 开只读库 → 找到人设 → 算出路往哪写。

    这一步里**先开连接、后找人设**，于是两条最常见的错误路径（没有这个
    名字、有多个人设）都是「连接已经开着的时候抛出去」。不在这里关掉，
    那个连接只能等垃圾回收，而回收时刻由解释器决定——它会变成一条飘忽的
    ``unclosed database`` 警告，落在哪一次运行头上全看运气。
    """
    config = _dataset_config()
    _require_sqlite(config)
    _require_database(config)
    backend = _peek_db(config)
    try:
        persona = _resolve_persona(backend.connection, name=args.persona)
        root = _dataset_root(args.out, persona=persona, config=config)
    except Exception:
        backend.close()
        raise
    return config, backend, persona, root


# ── 输出 ────────────────────────────────────────────────────


def _rule_label(name: str) -> str:
    """规则名说成人话。

    ``extra:<词>`` 是 ``domain.redact`` 给用户自定义条目用的名字——取冒号
    后面那段，那才是用户自己填进去、认得出的东西。认不出来的名字原样打回去，
    总比打一句「未知规则」强：至少用户能照着去搜。
    """
    if name.startswith("extra:"):
        return f"自定义「{name.partition(':')[2]}」"
    return _RULE_LABELS.get(name, name)


def _file_line(label: str, samples: int, filename: str, byte_count: int) -> str:
    """一行「哪个数据集、多少条、落在哪个文件、多大」。"""
    return (
        f"  {_pad(label, _KIND_COLUMNS)} {samples:>6} 条  "
        f"{_pad(filename, _FILE_COLUMNS)} {_human_size(byte_count)}"
    )


def _redaction_line(counts: Sequence[tuple[str, int]]) -> str:
    """脱敏明细。不脱敏才是常态，所以那一种单独说，不留一个空括号。"""
    if not counts:
        return "0 处"
    total = sum(count for _, count in counts)
    detail = "、".join(f"{_rule_label(name)} {count}" for name, count in counts)
    return f"{total} 处（{detail}）"


def _report_build(report: DatasetReport, *, root: Path, days: int) -> int:
    """``build`` 干了什么。

    先报数、后报路。用户最先想知道的是「拿到东西了吗」，
    而不是「文件叫什么」——后者跑失败了也照样能背出来。
    """
    total_samples = report.samples
    total_bytes = report.byte_count

    _out(f"{_pad('目录', _LABEL_COLUMNS)}{root}")
    _out(f"{_pad('范围', _LABEL_COLUMNS)}最近 {days} 天")
    _out(f"{_pad('形状', _LABEL_COLUMNS)}{'、'.join(report.fmts) or '—'}")
    _out(_RULE)

    if report.files:
        for item in report.files:
            _out(
                _file_line(
                    spec_for(item.kind).label,
                    item.samples,
                    item.filename,
                    item.byte_count,
                )
            )
    else:
        _out("  一个文件都没写出来")
    for kind, reason in report.skipped:
        _out(f"  {_pad(spec_for(kind).label, _KIND_COLUMNS)}   空 · {reason}")
    _out(f"  {_pad('脱敏', _KIND_COLUMNS)}      {_redaction_line(report.redactions)}")
    if report.removed:
        _out("")
        if report.dry_run:
            _out(f"  会删掉 {len(report.removed)} 个上一次留下的文件（预演，没删）：")
        else:
            _out(f"  删掉 {len(report.removed)} 个上一次留下的文件：")
        for name in report.removed:
            _out(f"    {name}")
        _out("  它们和这次要写的形状不一样。同一段内容两种形状混在一起，")
        _out("  按 *.jsonl 整体训练会把每段对话学两遍。")

    _out(_RULE)
    _out(f"{_pad('合计', _LABEL_COLUMNS)}{total_samples} 条 / {_human_size(total_bytes)}")
    _out("")

    if report.dry_run:
        _out("这只是预演：**一个文件都没写**。去掉 --dry-run 再跑一次才会落盘。")
        return 0
    if total_samples == 0:
        _out("没有可导出的样本。最可能的原因是这段时间里还没有对话记录——")
        _out(f"用 `--days` 把范围开大（当前 {days} 天）再试一次。")
        return 0

    _out(f"{_pad('说明页', _LABEL_COLUMNS)}{root / README_NAME}")
    _out(f"{_pad('导出记录', _LABEL_COLUMNS)}{root / MANIFEST_NAME}")
    _out("")
    _out("这两页就是「数据页面」：README.md 写着有什么、脱了什么、做不到什么。")
    return 0


def _report_paths(*, root: Path, formats: Sequence[FormatName]) -> int:
    """会落在哪几个文件。**没跑过也能问**，所以这一条不读 manifest。"""
    _out(f"{_pad('目录', _LABEL_COLUMNS)}{root}")
    _out(_RULE)
    for fmt in formats:
        if len(formats) > 1:
            _out(f"[{fmt}]")
        for kind, path in planned_paths(root, fmt=fmt):
            _out(f"  {_pad(spec_for(kind).label, _KIND_COLUMNS)} {path}")
        _out("")
    _out("还没跑过的话上面这些文件都还不存在——`build` 会把它们写出来。")
    _out("空的数据集不会写成空文件：`build` 会说清是哪一种空，")
    _out("而不是留一个 0 字节的 .jsonl 让人以为文件坏了。")
    return 0


def _report_status(status: DatasetStatus, *, root: Path) -> int:
    """磁盘上那一批什么样。只读，一个文件都不写。"""
    if not status.exists:
        _out(f"{_pad('目录', _LABEL_COLUMNS)}{root}")
        _out(_RULE)
        _out(status.error or "还没跑过。`alterego dataset build` 生成第一批。")
        return 0

    _out(f"{_pad('目录', _LABEL_COLUMNS)}{root}")
    _out(f"{_pad('生成于', _LABEL_COLUMNS)}{status.generated_at}")
    _out(f"{_pad('形状', _LABEL_COLUMNS)}{'、'.join(status.formats) or '—'}")
    if status.stale:
        _out(
            f"{_pad('状态', _LABEL_COLUMNS)}该重跑了——脱敏配置变过"
            f"（{status.redact_digest or '无记录'} → {status.current_digest}）"
        )
    else:
        _out(f"{_pad('状态', _LABEL_COLUMNS)}是最新的（脱敏指纹 {status.redact_digest}）")
    _out(_RULE)

    if status.files:
        for item in status.files:
            _out(
                _file_line(
                    spec_for(item.kind).label,
                    item.samples,
                    item.filename,
                    item.byte_count,
                )
            )
    else:
        _out("  一个文件都没有")
    for kind, reason in status.skipped:
        _out(f"  {_pad(spec_for(kind).label, _KIND_COLUMNS)}   空 · {reason}")

    if status.unlisted:
        _out("")
        _out(f"  目录里还有 {len(status.unlisted)} 个文件不在这份记录里：")
        for name in status.unlisted:
            _out(f"    {name}")
        _out("  重跑 `alterego dataset build` 会清掉它们——先确认要哪一种形状。")

    _out("")
    _out(f"{_pad('脱敏', _LABEL_COLUMNS)}{_redaction_line(status.redactions)}")
    _out(f"{_pad('合计', _LABEL_COLUMNS)}{status.samples} 条")
    _out("")

    if status.stale:
        _out("重跑：`alterego dataset build`")
        _out("")

    _out(f"{_pad('说明页', _LABEL_COLUMNS)}{root / README_NAME}")
    _out(f"{_pad('导出记录', _LABEL_COLUMNS)}{root / MANIFEST_NAME}")
    for item in status.files:
        _out(f"  {_pad(item.filename, _FILE_COLUMNS)} {root / item.filename}")
    return 0


def _report_show(
    items: Sequence[PreviewItem],
    *,
    root: Path,
    fmt: FormatName,
    days: int,
) -> int:
    """现场渲染出来的样子。**一个文件都不写。**"""
    _out(f"{_pad('形状', _LABEL_COLUMNS)}{fmt}")
    _out(f"{_pad('范围', _LABEL_COLUMNS)}最近 {days} 天")
    _out(f"{_pad('目录', _LABEL_COLUMNS)}{root}")
    _out(_RULE)

    for index, item in enumerate(items):
        _out(f"{item.label} · 来自 {item.source}")
        _out(f"  共 {item.total} 条，下面看 {len(item.shown)} 条 · 教它「{item.teaches}」")
        if not item.shown:
            _out("  （这一类是空的。`alterego dataset list` 会说清是哪一种空。）")
        for sample in item.shown:
            for line in json.dumps(sample, ensure_ascii=False, indent=2).splitlines():
                _out(f"    {line}")
        if index + 1 < len(items):
            _out("")

    _out("")
    _out("这是照着现在的库和现在的脱敏规则**现场**渲染的，不是磁盘上那一份。")
    _out("它一条记录都不写；要落盘跑 `alterego dataset build`。")
    return 0


# ── 命令 ────────────────────────────────────────────────────


def cmd_dataset_build(args: argparse.Namespace) -> int:
    """取数 → 脱敏 → 落盘。不调用模型，不花钱。"""
    config, backend, persona, root = _prepare(args)
    now = _now(config)
    days = int(getattr(args, "days", 0) or 0) or config.dataset.lookback_days
    dry_run = bool(getattr(args, "dry_run", False))
    formats = _formats(args, config)

    try:
        work = _workbench(config, backend, persona=persona, root=root)
        report = build(work, fmts=formats, now=now, days=days, dry_run=dry_run)
    except AlterEgoError as exc:
        return _fail(exc)
    finally:
        backend.close()

    _out(f"{_pad('人设', _LABEL_COLUMNS)}{persona.name}（{persona.id}）")
    _out(f"{_pad('时刻', _LABEL_COLUMNS)}{now:%Y-%m-%d %H:%M}")
    return _report_build(report, root=root, days=days)


def cmd_dataset_list(args: argparse.Namespace) -> int:
    """磁盘上那一批什么样、该不该重跑。只读。"""
    config, backend, persona, root = _prepare(args)
    backend.close()

    try:
        status = scan(
            root,
            current_digest=_current_digest(config),
            logger=_log,
        )
    except AlterEgoError as exc:
        return _fail(exc)

    _out(f"{_pad('人设', _LABEL_COLUMNS)}{persona.name}（{persona.id}）")
    return _report_status(status, root=root)


def cmd_dataset_paths(args: argparse.Namespace) -> int:
    """会落在哪几个文件。还没跑过也能问。"""
    config, backend, persona, root = _prepare(args)
    backend.close()

    _out(f"{_pad('人设', _LABEL_COLUMNS)}{persona.name}（{persona.id}）")
    return _report_paths(root=root, formats=_formats(args, config))


def cmd_dataset_show(args: argparse.Namespace) -> int:
    """现场渲染几条出来看。一个文件都不写。"""
    config, backend, persona, root = _prepare(args)
    now = _now(config)
    days = int(getattr(args, "days", 0) or 0) or config.dataset.lookback_days
    fmt = _formats(args, config)[0]
    per_kind = max(int(getattr(args, "limit", 0) or _DEFAULT_PER_KIND), 0)

    try:
        work = _workbench(config, backend, persona=persona, root=root)
        items = preview(work, fmt=fmt, now=now, days=days, per_kind=per_kind)
    except AlterEgoError as exc:
        return _fail(exc)
    finally:
        backend.close()

    _out(f"{_pad('人设', _LABEL_COLUMNS)}{persona.name}（{persona.id}）")
    return _report_show(items, root=root, fmt=fmt, days=days)


# ── 命令行 ──────────────────────────────────────────────────


def _add_common(parser: argparse.ArgumentParser) -> None:
    """四个命令共有的两个开关。"""
    parser.add_argument(
        "--persona",
        default=None,
        metavar="NAME",
        help="给哪个人设导出（默认：库里只有一个时自动选它）",
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="DIR",
        help="导出到哪（默认：[dataset] export_dir/<角色名>）",
    )


def add_dataset_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """把 ``dataset`` 命令组挂到顶层子命令上。"""
    dataset_parser = commands.add_parser(
        "dataset",
        help="训练数据集：对话 / 思考 / 工具调用，脱敏后导出成 JSONL",
    )
    dataset_commands = dataset_parser.add_subparsers(
        dest="subcommand",
        metavar="{build,list,paths,show}",
    )
    # 只敲到 `alterego dataset` 时要打**这一层**的帮助，而不是顶层的。
    dataset_parser.set_defaults(subparser=dataset_parser)

    build_parser = dataset_commands.add_parser(
        "build",
        help="取数 → 脱敏 → 写 .jsonl / manifest.json / README.md",
    )
    _add_common(build_parser)
    build_parser.add_argument(
        "--format",
        dest="fmt",
        choices=FORMATS,
        default=None,
        metavar="{chat,sharegpt,alpaca}",
        help="只出这一种形状（默认：按 [dataset] formats）",
    )
    build_parser.add_argument(
        "--days",
        type=int,
        default=0,
        metavar="N",
        help="往回看几天（默认：[dataset] lookback_days）",
    )
    build_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只算不写：报出条数、字节数与摘要，一个文件都不落盘",
    )
    build_parser.set_defaults(handler=cmd_dataset_build)

    list_parser = dataset_commands.add_parser(
        "list",
        help="磁盘上那一批什么样、该不该重跑",
    )
    _add_common(list_parser)
    list_parser.set_defaults(handler=cmd_dataset_list)

    paths_parser = dataset_commands.add_parser(
        "paths",
        help="会落在哪几个文件（还没跑过也能问）",
    )
    _add_common(paths_parser)
    paths_parser.add_argument(
        "--format",
        dest="fmt",
        choices=FORMATS,
        default=None,
        metavar="{chat,sharegpt,alpaca}",
        help="按这一种形状算路径（默认：按 [dataset] formats）",
    )
    paths_parser.set_defaults(handler=cmd_dataset_paths)

    show_parser = dataset_commands.add_parser(
        "show",
        help="现场渲染几条出来看，不写文件",
    )
    _add_common(show_parser)
    show_parser.add_argument(
        "--format",
        dest="fmt",
        choices=FORMATS,
        default=None,
        metavar="{chat,sharegpt,alpaca}",
        help="渲染成哪一种形状（默认：按 [dataset] formats 的第一个）",
    )
    show_parser.add_argument(
        "--days",
        type=int,
        default=0,
        metavar="N",
        help="往回看几天（默认：[dataset] lookback_days）",
    )
    show_parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_PER_KIND,
        metavar="N",
        help=f"每一类看几条（默认 {_DEFAULT_PER_KIND}）",
    )
    show_parser.set_defaults(handler=cmd_dataset_show)

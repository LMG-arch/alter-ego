"""训练数据集管线：从库里取数、脱敏、落盘。

本模块是**编排**，不是规则。规则在 ``domain/dataset.py``（怎么把行拼成样本）
与 ``domain/redact.py``（什么该摘掉）里；这里只干四件事：取数、调那两处、
写文件、报数。

## 三步，只有一步会花钱

| 步骤 | 干什么 | 会失败吗 | 花钱吗 |
| --- | --- | --- | --- |
| 取数 | 走 ``DatasetSourceRepository`` 读三张源表 | 不会（只读） | 不花 |
| 脱敏 + 拼样本 | 调 ``domain`` 的纯函数 | 不会（纯函数） | 不花 |
| 落盘 | 写 ``.jsonl`` / ``manifest.json`` / ``README.md`` | 会（磁盘、权限） | 不花 |

**一步 LLM 都不调。** 这不是省事，是设计：训练集要的是「它当时到底怎么想的」，
而不是「现在问一遍模型当时大概怎么想的」。后者是伪造标注，而且不可复现（P6）、
每次重跑都要钱。所以本模块里没有 ``LLMGateway``，也没有对应的
``[llm.routing]`` 键——数据集的用途名根本不需要存在。

## 依赖从哪里来

``DatasetWorkbench`` 装的全是协议，所以 ``sim/`` 不知道底下是 SQLite 还是别的。
具体实现由组装根（``cli_dataset.py``）装好递进来。

## 时间从哪来

``now`` 由调用方传入，本模块一次取墙上时间的调用都没有（P6）。
``since`` 由 ``now - timedelta(days=...)`` 算出来，所以同样的 ``now``
配同样的库必然得到同样的一批样本——黄金测试靠的就是这一点。

依据: docs/plans/2026-09-16-training-datasets.md § 3、§ 6
      docs/adr/0011-training-datasets-are-derived-and-redacted.md
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

from alterego.domain.dataset import (
    DATASET_SPECS,
    DatasetName,
    ExportedFile,
    FormatName,
    MessageRow,
    TrainingSample,
    build_conversation_samples,
    build_reasoning_samples,
    build_tooluse_samples,
    redact_samples,
    render_manifest,
    render_readme,
    render_sample,
    to_jsonl,
)
from alterego.domain.redact import RedactionRule, build_rules, digest
from alterego.interfaces.repository import DatasetSourceRepository, PersonaRecord


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "MANIFEST_NAME",
    "README_NAME",
    "DatasetReport",
    "DatasetStatus",
    "DatasetWorkbench",
    "PreviewItem",
    "build",
    "file_name",
    "planned_paths",
    "preview",
    "scan",
]


MANIFEST_NAME: Final[str] = "manifest.json"
_JSONL_SUFFIX: Final[str] = ".jsonl"
"""机器可读的导出记录。``scan`` 只认它。"""

README_NAME: Final[str] = "README.md"
"""给人看的说明。当前的「数据页面」就是这一页。"""

DEFAULT_LOOKBACK_DAYS: Final[int] = 30
"""默认往回看多少天。

比知识库的 7 天长：知识库要的是「最近的事」，数据集要的是「足够多的样本」。
30 天是个起点，用户在 ``[dataset]`` 里可以改。
"""

_MAX_MESSAGES: Final[int] = 20000
_MAX_TICKS: Final[int] = 5000
_MAX_ACTIVITIES: Final[int] = 20000
"""一次最多取多少行。

上限存在的理由不是内存，是**样本质量**：一次导出十万条，
你既读不完也训不动，只会得到一个不知道该不该信任的目录。
"""

_NO_SOURCE_DATA: Final[str] = "库里这个时间段还没有对应的记录"
_NO_SAMPLES: Final[str] = "取到 {rows} 行，但一行都没拼成样本——字段缺失或全是空内容，看 debug 日志"
_NO_CHOSEN_INTENT: Final[str] = (
    "取到 {rows} 条行为，但没有一条挂在记下了「选中意图」的 tick 上——工具调用的「请求」拼不出来"
)
_UPSTREAM_PENDING: Final[str] = "上游还没落地：推演引擎不写入 tick_log，这一类必然是空的"


# ── 装配 ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DatasetWorkbench:
    """跑一次数据集导出需要的一切。

    装的全是协议，所以 ``sim/`` 不知道底下是 SQLite 还是别的什么。
    """

    persona: PersonaRecord
    user_name: str
    root: Path
    sources: DatasetSourceRepository
    logger: logging.Logger
    system_prompt: str = ""
    """所有格式下都会带上的系统提示。

    ``chat`` 放进 ``messages`` 的第一条，``sharegpt`` 放进 ``system``，
    ``alpaca`` 放进 ``instruction``。留空就不带——空字符串不该渲染出一个
    内容为空的 system 轮，那会让训练框架把「空系统提示」当成一种真实情况学下去。
    """
    redact_terms: tuple[str, ...] = ()
    """用户额外要摘掉的字面量，来自 ``[dataset] redact_terms``。"""


# ── 报告 ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DatasetReport:
    """``build`` 干了什么。"""

    root: Path
    fmts: tuple[FormatName, ...] = ()
    generated_at: str = ""
    redact_digest: str = ""
    files: tuple[ExportedFile, ...] = ()
    redactions: tuple[tuple[str, int], ...] = ()
    skipped: tuple[tuple[DatasetName, str], ...] = ()
    removed: tuple[str, ...] = ()
    """这次清掉了哪些上一批留下的 ``.jsonl``。

    不清理的后果不是「多一点冗余」：``formats`` 从 ``["chat"]`` 改成
    ``["sharegpt"]`` 之后两种形状并存，谁拿 ``*.jsonl`` 去微调就会把同一段
    对话学两遍，而 README 只描述了其中一种。

    ``dry_run`` 时这里装的是「会删掉哪些」——没真删。
    """
    dry_run: bool = False

    @property
    def samples(self) -> int:
        """一共导出多少条。"""
        return sum(item.samples for item in self.files)

    @property
    def byte_count(self) -> int:
        """一共多少字节。"""
        return sum(item.byte_count for item in self.files)

    @property
    def replaced(self) -> int:
        """脱敏替换了多少处。"""
        return sum(count for _, count in self.redactions)


@dataclass(frozen=True, slots=True)
class DatasetStatus:
    """磁盘上那个目录的现状。"""

    root: Path
    exists: bool = False
    generated_at: str = ""
    formats: tuple[str, ...] = ()
    redact_digest: str = ""
    current_digest: str = ""
    files: tuple[ExportedFile, ...] = ()
    redactions: tuple[tuple[str, int], ...] = ()
    skipped: tuple[tuple[DatasetName, str], ...] = ()
    unlisted: tuple[str, ...] = ()
    """目录里存在、但 manifest 没提到的 ``.jsonl``。

    ``build`` 正常情况下会把它们清掉；这里再报一次，是为了那张 dump 进
    目录、或者上一次 ``build`` 本身什么都没写出来（于是没敢清理）的情况。
    """
    error: str = ""

    @property
    def stale(self) -> bool:
        """磁盘上的数据集是不是用**旧规则**脱的。

        ``redact_digest`` 为空说明磁盘上那份没有记录（早期版本写的、
        或者用户手改了），同样按「该重跑」处理——宁可多跑一次，
        也不要让一份可能没脱干净的数据集被当成合格品。
        """
        return self.redact_digest != self.current_digest

    @property
    def samples(self) -> int:
        return sum(item.samples for item in self.files)


@dataclass(frozen=True, slots=True)
class PreviewItem:
    """``show`` 要打印的一类数据集。"""

    kind: DatasetName
    label: str
    source: str
    teaches: str
    total: int
    shown: tuple[dict[str, Any], ...] = ()


# ── 小工具 ──────────────────────────────────────────────────


def file_name(kind: DatasetName, fmt: FormatName) -> str:
    """``conversation.chat.jsonl``。

    格式进文件名而不是只进目录名：同一个目录里同时放三份不同格式，
    比让用户来回改配置再重跑好用得多。测出来哪份更适合，直接比较就行。
    """
    return f"{kind}.{fmt}.jsonl"


def planned_paths(root: Path, *, fmt: FormatName) -> tuple[tuple[DatasetName, Path], ...]:
    """这一批会写/已经写在哪几个文件里。

    纯函数：不碰磁盘。``dataset paths`` 靠它命中「还没跑过就告诉我落在哪」。
    """
    return tuple((spec.name, root / file_name(spec.name, fmt)) for spec in DATASET_SPECS)


def _write(path: Path, text: str) -> None:
    """原子地写一个文件。

    先写 ``.tmp`` 再 ``replace``。半截 JSONL 比没有文件更糟：训练框架会
    默默少读几条，而且它看起来像一份正常的数据集。

    显式写 ``newline="\\n"``：Windows 上默认会把 ``\\n`` 变成 ``\\r\\n``，
    而仓库是 ``core.autocrlf=false``——一旦写进去，每次重跑在 git 里都成了
    「整个文件都变了」，diff 也就没用了。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def _jsonl_names(root: Path) -> tuple[str, ...]:
    """目录里的 ``.jsonl`` 文件名，排好序。读不动就当空目录。"""
    try:
        return tuple(sorted(path.name for path in root.glob(f"*{_JSONL_SUFFIX}") if path.is_file()))
    except OSError as exc:
        logging.getLogger(__name__).debug("列不了 %s：%s", root, exc)
        return ()


def _sweep(root: Path, *, keep: frozenset[str], logger: logging.Logger) -> tuple[str, ...]:
    """删掉这一批不会写的 ``.jsonl``，返回删了哪些。

    这个目录是**派生产物**（ADR-0011），它唯一正确的状态就是「和这次 build
    的配置一致」。所以旧文件不是「多余的冗余」，而是一种会误导人的东西：
    ``formats`` 从 ``["chat"]`` 改成 ``["sharegpt"]`` 之后两种形状并存，
    谁按 ``*.jsonl`` 去微调，就会把同一段对话学两遍，而 README 只描述了
    其中一种。

    删不掉就跳过（Windows 上文件可能正被别的程序占着），并记一条 WARNING：
    宁可留一个旧文件，也不要让整次导出失败。
    """
    removed: list[str] = []
    for name in _jsonl_names(root):
        if name in keep:
            continue
        try:
            (root / name).unlink()
        except OSError as exc:
            logger.warning("删不掉上一批的 %s：%s", root / name, exc)
            continue
        removed.append(name)
    return tuple(removed)


def _unlisted(root: Path, *, files: Sequence[ExportedFile]) -> tuple[str, ...]:
    """写完之后，目录里还有哪些 ``.jsonl`` 不在这批记录里。"""
    known = {item.filename for item in files}
    return tuple(name for name in _jsonl_names(root) if name not in known)


def _measure(text: str) -> tuple[int, str]:
    """一段待写文本 → ``(字节数, sha256)``。

    量的是**编码之后的字节**而不是字符数：中文在 UTF-8 里占三字节，
    报字符数会让「这个文件多大」差出三倍。
    """
    payload = text.encode("utf-8")
    return len(payload), hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class _Collected:
    """取数 + 脱敏的结果。`rows` 是「源表里有多少行」，用来解释空样本。"""

    samples: dict[DatasetName, list[TrainingSample]]
    hits: dict[str, int]
    rows: dict[DatasetName, int]


def _collect(
    work: DatasetWorkbench,
    *,
    since: datetime,
    rules: Sequence[RedactionRule],
) -> _Collected:
    """三张源表 → 三类样本（已脱敏）。"""
    persona_id = work.persona.id

    messages: list[MessageRow] = work.sources.list_conversation_messages(
        persona_id, since=since, limit=_MAX_MESSAGES
    )
    ticks = work.sources.list_ticks(persona_id, since=since, limit=_MAX_TICKS)
    activities = work.sources.list_activities(persona_id, since=since, limit=_MAX_ACTIVITIES)
    intents = work.sources.map_tick_intents(persona_id, since=since)

    raw: dict[DatasetName, list[TrainingSample]] = {
        "conversation": build_conversation_samples(messages, system=work.system_prompt),
        "reasoning": build_reasoning_samples(ticks, system=work.system_prompt),
        "tooluse": build_tooluse_samples(
            activities, system=work.system_prompt, intents_by_tick=intents
        ),
    }
    rows: dict[DatasetName, int] = {
        "conversation": len(messages),
        "reasoning": len(ticks),
        "tooluse": len(activities),
    }

    cleaned: dict[DatasetName, list[TrainingSample]] = {}
    totals: dict[str, int] = {}
    for kind, samples in raw.items():
        summary = redact_samples(samples, rules=rules)
        cleaned[kind] = list(summary.samples)
        for rule_name, count in summary.hits:
            totals[rule_name] = totals.get(rule_name, 0) + count

    work.logger.debug(
        "取到 %d 条消息、%d 个 tick、%d 条行为，拼出 %d 条样本",
        len(messages),
        len(ticks),
        len(activities),
        sum(len(items) for items in cleaned.values()),
    )
    return _Collected(samples=cleaned, hits=totals, rows=rows)


def _skip_reason(kind: DatasetName, *, rows: int, upstream_ready: bool) -> str:
    """某一类为什么是空的。

    分三种说法，因为它们对用户意味着完全不同的事：
    「上游还没写进来」只能等；「你时间范围开小了」自己就能解决；
    而「取到行但拼不出样本」是真出事了，得去看 debug 日志。
    含糊地说一句「没有数据」，后两种就都藏起来了。
    """
    if rows == 0:
        return _UPSTREAM_PENDING if not upstream_ready else _NO_SOURCE_DATA
    if kind == "tooluse":
        return _NO_CHOSEN_INTENT.format(rows=rows)
    return _NO_SAMPLES.format(rows=rows)


# ── build ───────────────────────────────────────────────────


def build(
    work: DatasetWorkbench,
    *,
    fmts: Sequence[FormatName] = ("chat",),
    now: datetime,
    days: int = DEFAULT_LOOKBACK_DAYS,
    dry_run: bool = False,
) -> DatasetReport:
    """从库里导出三类数据集，写 ``.jsonl`` + ``manifest.json`` + ``README.md``。

    Args:
        work: 装配好的工作台。
        fmts: 要产出哪几种形状，见 ``domain.dataset.FORMATS``。几种形状共用同一次
            取数，只多花磁盘——想比哪份训出来好，一次跑出来比来回改配置省事。
        now: 「现在」。由调用方传入，本模块不取墙上时间（P6）。
        days: 往回看多少天。
        dry_run: 只算不写。此时 ``files`` 里的字节数与 sha256 仍是**真实值**
            ——它们量的是「本来会写下去的那串字节」，所以 dry-run 的报告
            可以直接拿来和已经落盘的那份比对。

    Returns:
        这次导出干了什么。没样本的数据集不会写空文件，而是进 ``skipped``，
        附一句说清为什么——空的 ``.jsonl`` 看起来像坏了，而「没数据」
        与「功能没做」是两件事，不该长得一样。

    ``manifest.json`` 与 ``README.md`` 只写一次，描述**整个目录**：
    有几种形状就写几种。每个形状各写一份说明的话，后写的会把先写的盖掉，
    于是用户在说明页里找不到另一份文件——而那份文件就在同一个目录里。
    """
    rules = build_rules(user_name=work.user_name, extra_terms=work.redact_terms)
    since = now - timedelta(days=days)
    generated_at = now.isoformat(timespec="seconds")

    collected = _collect(work, since=since, rules=rules)
    samples_by_kind = collected.samples
    rows = collected.rows
    totals = collected.hits
    redactions = tuple(sorted(totals.items(), key=lambda pair: (-pair[1], pair[0])))

    files: list[ExportedFile] = []
    skipped: list[tuple[DatasetName, str]] = []
    for spec in DATASET_SPECS:
        samples = samples_by_kind.get(spec.name, [])
        if not samples:
            # 空不空与形状无关：同一次导出里每一种形状都是空的。
            skipped.append(
                (
                    spec.name,
                    _skip_reason(
                        spec.name, rows=rows.get(spec.name, 0), upstream_ready=spec.upstream_ready
                    ),
                )
            )
            continue
        for fmt in fmts:
            text = to_jsonl(samples, fmt)
            size, sha = _measure(text)
            name = file_name(spec.name, fmt)
            if not dry_run:
                _write(work.root / name, text)
            files.append(
                ExportedFile(
                    kind=spec.name,
                    filename=name,
                    samples=len(samples),
                    byte_count=size,
                    sha256=sha,
                )
            )

    keep = frozenset(item.filename for item in files)
    if dry_run:
        # 预演也要报「会删什么」：清理是不可逆的，不该只有真跑才看得见。
        removed = tuple(name for name in _jsonl_names(work.root) if name not in keep)
    elif files:
        # 只在**这次真写出了东西**的时候才清。否则 ``build --days 1`` 在空
        # 区间上跑一次，就会把一份本来好好的 30 天数据集整个抹掉——那是个
        # 一敲就中的陷阱。
        removed = _sweep(work.root, keep=keep, logger=work.logger)
    else:
        removed = ()

    report = DatasetReport(
        root=work.root,
        fmts=tuple(fmts),
        generated_at=generated_at,
        redact_digest=digest(extra_terms=work.redact_terms),
        files=tuple(files),
        redactions=redactions,
        skipped=tuple(skipped),
        removed=removed,
        dry_run=dry_run,
    )

    if not dry_run:
        unlisted = _unlisted(work.root, files=report.files)
        manifest = render_manifest(
            persona_name=work.persona.name,
            generated_at=generated_at,
            formats=report.fmts,
            redact_digest=report.redact_digest,
            redact_terms=work.redact_terms,
            files=report.files,
            redactions=report.redactions,
            skipped=report.skipped,
        )
        if unlisted:
            # 直接往字典里补一个键，而不是给 ``render_manifest`` 再加参数：
            # 那个模块已经贴着 900 行上限了，而这条信息的归属很明确——
            # 「写完之后目录里还剩下什么」只有编排层知道。
            manifest["unlisted"] = list(unlisted)
        _write(
            work.root / MANIFEST_NAME,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        readme = render_readme(
            persona_name=work.persona.name,
            generated_at=generated_at,
            formats=report.fmts,
            redact_digest=report.redact_digest,
            redact_terms=work.redact_terms,
            files=report.files,
            redactions=report.redactions,
            skipped=report.skipped,
        )
        if unlisted:
            readme += _unlisted_section(unlisted)
        _write(work.root / README_NAME, readme)
        work.logger.info(
            "数据集写入 %s：%d 条、%d 个文件、脱敏 %d 处",
            work.root,
            report.samples,
            len(report.files),
            report.replaced,
        )

    return report


def _unlisted_section(unlisted: Sequence[str]) -> str:
    """README 末尾的「这些文件不在这批记录里」。

    拼在 ``render_readme`` 的结果后面，而不是给那个函数加参数：领域层
    ``domain/dataset.py`` 已经贴着 900 行上限，而这段文字描述的是
    「目录里还剩什么」——那只有编排层知道。
    """
    lines = [
        "",
        "## 目录里还有别的东西",
        "",
        "下面这些 ``.jsonl`` 在目录里，但**不属于这一批**，所以上面那张表没提它们：",
        "",
    ]
    lines.extend(f"- `{name}`" for name in unlisted)
    lines.extend(
        [
            "",
            "多半是改过 `[dataset] formats` 之后留下的旧形状。",
            "按 `*.jsonl` 整体训练会把同一段内容学两遍——先确认要哪一份。",
            "重跑一次 `alterego dataset build` 会清掉它们（只清 `.jsonl`，不动你自己的文件）。",
            "",
        ]
    )
    return "\n".join(lines)


# ── scan ────────────────────────────────────────────────────


def _load_manifest(root: Path, logger: logging.Logger) -> dict[str, Any] | None:
    """读 ``manifest.json``。读不动就返回 ``None``，不抛。

    ``dataset list`` 最需要出结果的时刻，恰恰是目录被手改坏了的时刻。
    让它整个崩掉，用户就只能去翻日志。
    """
    path = root / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("读不了 %s：%s", path, exc)
        return None
    if not isinstance(loaded, dict):
        logger.warning("%s 不是一个 JSON 对象，忽略", path)
        return None
    return loaded


def scan(
    root: Path,
    *,
    current_digest: str,
    logger: logging.Logger,
) -> DatasetStatus:
    """看磁盘上那份数据集是什么样。

    只认 ``manifest.json``，不重新扫 ``.jsonl``：文件可能被用户手改过，
    而 manifest 记的是「我们**声称**写了什么」。两者不一致本身是信息，
    但那是 ``build`` 该修正的事，不是 ``list`` 该猜的事。

    ``current_digest`` 是**当前**配置算出来的规则指纹，用来判断磁盘上那份
    是不是旧规则脱的（``DatasetStatus.stale``）。
    """
    if not root.is_dir():
        return DatasetStatus(root=root, current_digest=current_digest)

    manifest = _load_manifest(root, logger)
    if manifest is None:
        return DatasetStatus(
            root=root,
            exists=False,
            current_digest=current_digest,
            error=f"目录在，但没有可用的 {MANIFEST_NAME}（没跑过 build，或文件坏了）",
        )

    raw_formats = manifest.get("formats")
    formats = tuple(str(item) for item in raw_formats) if isinstance(raw_formats, list) else ()

    files: list[ExportedFile] = []
    for item in manifest.get("files") or []:
        if not isinstance(item, dict):
            continue
        files.append(
            ExportedFile(
                kind=cast("DatasetName", item.get("dataset", "")),
                filename=item.get("file", ""),
                samples=int(item.get("samples") or 0),
                byte_count=int(item.get("bytes") or 0),
                sha256=item.get("sha256", ""),
            )
        )

    redactions = tuple(
        (str(item.get("rule", "")), int(item.get("count") or 0))
        for item in (manifest.get("redactions") or [])
        if isinstance(item, dict)
    )
    skipped: tuple[tuple[DatasetName, str], ...] = tuple(
        (cast("DatasetName", item.get("dataset", "")), str(item.get("reason", "")))
        for item in (manifest.get("skipped") or [])
        if isinstance(item, dict)
    )

    return DatasetStatus(
        root=root,
        exists=True,
        generated_at=str(manifest.get("generated_at") or ""),
        formats=formats,
        redact_digest=str(manifest.get("redact_digest") or ""),
        current_digest=current_digest,
        files=tuple(files),
        redactions=redactions,
        skipped=skipped,
        unlisted=_unlisted(root, files=files),
    )


# ── preview ─────────────────────────────────────────────────


def preview(
    work: DatasetWorkbench,
    *,
    fmt: FormatName = "chat",
    now: datetime,
    days: int = DEFAULT_LOOKBACK_DAYS,
    per_kind: int = 2,
) -> tuple[PreviewItem, ...]:
    """现场渲染几条样本给用户看，**不写任何文件**。

    现场渲染而不是读磁盘上的 ``.jsonl``：用户真正想知道的是
    「照着现在的库和现在的规则跑一遍，会得到什么」，而不是
    「上次跑得到了什么」。想看重跑会得到什么的唯一时机，
    就是还没决定要不要重跑的时候。
    """
    rules = build_rules(user_name=work.user_name, extra_terms=work.redact_terms)
    since = now - timedelta(days=days)
    collected = _collect(work, since=since, rules=rules)

    items: list[PreviewItem] = []
    for spec in DATASET_SPECS:
        cleaned = collected.samples.get(spec.name, [])
        shown = tuple(render_sample(sample, fmt) for sample in cleaned[: max(per_kind, 0)])
        items.append(
            PreviewItem(
                kind=spec.name,
                label=spec.label,
                source=spec.source,
                teaches=spec.teaches,
                total=len(cleaned),
                shown=shown,
            )
        )
    return tuple(items)

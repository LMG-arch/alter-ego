"""知识库：把发生过的事写成笔记，再让它自己整理。

本模块是**编排**，不是规则。规则在 `domain/vault.py`（库结构、索引、四条
不变量）和 `domain/knowledge.py`（一篇笔记的语法）；模型调用在
`llm/gateway.py`。这里只负责按对的顺序把它们串起来，以及**碰文件系统**。

## 三步，两块钱

| 命令 | 干什么 | 花钱吗 | 会失败吗 |
| --- | --- | --- | --- |
| ``sync`` | 库里的日程 / 想法 / 搜集到的 → 笔记 | **不花** | 不会，纯渲染 |
| ``organize`` | 收集箱里的东西 → 归到该去的目录 | 花一次 | 会，模型可以胡说 |
| ``build`` | 按实际文件重算索引页 | **不花** | 不会，纯推导 |

这个切分不是随手分的。前两步的差别决定了整个设计：

**第一步永远免费、永远成功。** 日程表、行为日志、搜集到的链接——这些已经是
结构化数据了，渲染成 Markdown 是纯函数，跑一百遍结果一样。所以哪怕模型配额
用光、API 挂了，它照样每天有自己的日程页，知识库照样在长。

**第二步才是「它自己在整理」。** 这一步只有模型能决定：这条素材算想法还是算
读到的东西？该和哪篇互链？这一步可能会失败——模型可能返回一坨看不懂的东西。
所以 `domain/vault.py` 的 `parse_organize_plan` 逐条把关，**坏的留下、好的照收**，
不让一条坏决定废掉整次整理。

**第三步是收尾。** 索引页由实际文件推导，不是让模型写——它写不出「共 87 篇」
这种数。这一步保证 `alterego vault status` 报出来的东西和索引页上写的一致。

## 依赖从哪里来

和生产路径上一样，这些该由 ``PluginContext`` 提供；但本批次由 CLI 驱动，
没有 tick 也没有插件上下文，所以 :class:`VaultWorkbench` 显式装着它们。
它装的**全是协议**（``ScheduleRepository`` / ``LLMGateway`` / ``Path``），
所以 `sim/` 依然不认识 SQLite，也不认识任何模型供应商——
``scripts/check_architecture.sh`` 第 3 组和第 7 组红线钉住了这两件事。

## 时间从哪来

``now`` 由调用方传进来，这个模块里一次取墙上时间的调用都没有（P6）。
这不只是洁癖：`sync` 是幂等的，同一个 ``now`` 跑两遍必须产出逐字节相同的
文件，否则每跑一次 git 就多一次无意义的 diff。

依据: docs/plans/2026-09-16-obsidian-vault.md § 3、§ 6、§ 7
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from alterego.domain.knowledge import Note, note_from_text, slugify
from alterego.domain.memory import Memory
from alterego.domain.vault import (
    CONTENT_FOLDERS,
    FOLDER_INBOX,
    FOLDER_INDEX,
    FOLDER_SCHEDULE,
    FOLDER_SOURCES,
    FOLDERS,
    STATE_FILENAME,
    TYPES,
    VaultIssue,
    build_folder_index,
    build_root_index,
    parse_organize_plan,
    resolve_path,
    validate,
)
from alterego.interfaces.repository import (
    ActivityRecord,
    ActivityRepository,
    MemoryRepository,
    PersonaRecord,
    ScheduleRecord,
    ScheduleRepository,
    SourceRecord,
    SourceRepository,
)
from alterego.kernel.errors import SimulationError
from alterego.llm import LLMGateway, PromptLibrary


__all__ = [
    "BuildReport",
    "OrganizeReport",
    "SyncReport",
    "VaultWorkbench",
    "build",
    "init_vault",
    "organize",
    "scan",
    "sync",
]


PURPOSE: Final[str] = "vault"
"""路由用的用途名，对应 ``[llm.routing] vault``。"""

_TEMPLATE: Final[str] = "vault_organize"
"""整理用的提示词模板。"""

_LOOKBACK_DAYS: Final[int] = 7
"""``sync`` 默认往回看多少天。"""

_TEMPERATURE: Final[float] = 0.5
"""比默认的 0.8 低。分类是判断，不是创作。"""

_MAX_TOKENS: Final[int] = 3072
"""几十条决定的 JSON 大概两千 token，留一半余量。"""

_MAX_FETCH: Final[int] = 400
"""一次最多从库里读多少行。"""

_MAX_INBOX: Final[int] = 40
"""一次最多整理收集箱里的多少篇。太多了模型会开始漏。"""

_MAX_CATALOG: Final[int] = 150
"""目录树里最多列多少篇已有笔记。"""

_NO_LOCATION: Final[str] = "—"
"""没有地点时表格里写什么。空单元格在 Obsidian 的表格里会塌成一格。"""

_OBSIDIAN_DIR: Final[str] = ".obsidian"

_APP_JSON: Final[dict[str, Any]] = {
    "alwaysUpdateLinks": True,
    "newFileLocation": "folder",
    "newFileFolderPath": FOLDER_INBOX,
    "trashOption": "local",
    "useMarkdownLinks": False,
    "readableLineLength": True,
    "strictLineBreaks": False,
}
"""``.obsidian/app.json``。

``newFileLocation`` / ``newFileFolderPath`` 指向收集箱，是**故意的**：
用户自己在 Obsidian 里随手新建一篇笔记，它会落到收集箱里，
下次 ``organize`` 就被角色顺带整理了。让用户和角色共用一个入口，
比教用户「请把东西放到 99-收集箱」实际得多。
"""

_CORE_PLUGINS: Final[list[str]] = [
    "file-explorer",
    "global-search",
    "switcher",
    "graph",
    "backlink",
    "outgoing-link",
    "tag-pane",
    "page-preview",
    "templates",
    "note-composer",
    "command-palette",
    "editor-status",
    "bookmarks",
    "outline",
    "word-count",
    "file-recovery",
]
"""``.obsidian/core-plugins.json``。

刻意**不启用** ``daily-notes``：日程页由本模块生成，开了它 Obsidian 会在
同一个目录里另建一套 ``YYYY-MM-DD.md``，两边互相覆盖。
"""


# ── 装配 ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VaultWorkbench:
    """跑一次知识库任务需要的一切。

    装的全是协议，所以 `sim/` 不知道底下是 SQLite 还是别的什么。
    """

    persona: PersonaRecord
    user_name: str
    root: Path
    gateway: LLMGateway
    prompts: PromptLibrary
    schedules: ScheduleRepository
    activities: ActivityRepository
    sources: SourceRepository
    memories: MemoryRepository
    logger: logging.Logger


# ── 报告 ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SyncReport:
    """``sync`` 干了什么。"""

    days: int = 0
    thoughts: int = 0
    sources: int = 0
    memories: int = 0
    written: int = 0
    inbox: int = 0
    skipped: str | None = None


@dataclass(frozen=True, slots=True)
class OrganizeReport:
    """``organize`` 干了什么。"""

    read: int = 0
    filed: int = 0
    rejected: tuple[tuple[str, str], ...] = ()
    preview: tuple[str, ...] = ()
    issues: tuple[VaultIssue, ...] = ()
    skipped: str | None = None


@dataclass(frozen=True, slots=True)
class BuildReport:
    """``build`` 干了什么。"""

    scanned: int = 0
    indexed: int = 0
    issues: tuple[VaultIssue, ...] = ()


# ── 小工具 ──────────────────────────────────────────────────


def _hhmm(value: datetime, tz: Any) -> str:
    """一个时刻 → ``HH:MM``。带时区的先转成本地时间。

    库里存的是 UTC，直接取 ``%H:%M`` 会让晚上八点的事写成十二点。
    """
    return f"{value.astimezone(tz):%H:%M}" if value.tzinfo else f"{value:%H:%M}"


def _local_date(value: datetime, tz: Any) -> date:
    return value.astimezone(tz).date() if value.tzinfo else value.date()


def _short(text: str, *, limit: int = 24) -> str:
    """把一段话压成一行标题。

    换行和连续空白都要拍平——标题进的是文件名，而文件名里不能有换行。
    """
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[: limit - 1]}…"


def _inbox_name(title: str, key: str) -> str:
    """收集箱里的文件名：``标题-短id``。

    带上 id 的前 8 位是为了**幂等**。只按标题命名的话，两条标题一样的链接
    会互相覆盖，用户不会知道少了一条。id 由数据决定，所以同一条数据渲染
    两次得到同一个文件名，重跑不会堆出一堆副本。
    """
    return f"{slugify(title)}-{key[:8]}"


def _write(root: Path, note: Note) -> Path:
    """原子地写一篇笔记，返回落盘路径。

    先写 ``.md.tmp`` 再 ``replace``。半截文件比没有文件更糟：Obsidian 会
    打开它、缓存它、把坏内容显示给用户，而且它看起来像一篇正常的笔记。

    显式写 ``newline="\\n"``：Windows 上默认会把 ``\\n`` 变成 ``\\r\\n``，
    而仓库的 ``core.autocrlf=false``——一旦写进去，每篇笔记在 git 里都成了
    「整个文件都变了」。
    """
    path = root / note.path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(note.render(), encoding="utf-8", newline="\n")
    tmp.replace(path)
    return path


def scan(root: Path, *, now: datetime, logger: logging.Logger | None = None) -> tuple[Note, ...]:
    """把库里的 ``.md`` 全读回来。

    读不动的那几个**跳过并记日志**，不让整个库卡住。用户手写的文件可能是
    GBK、可能是个空目录占位、可能权限不对——它们不该让 ``vault status``
    彻底跑不起来，那正是最需要看到输出的时候。

    点开头的目录（``.obsidian/``、``.trash/``）整个跳过：
    里面是 Obsidian 自己的东西，不是用户的笔记。
    """
    if not root.is_dir():
        return ()
    notes: list[Note] = []
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            if logger is not None:
                logger.warning(
                    "读不动这个文件，先跳过", extra={"path": str(path), "error": str(exc)}
                )
            continue
        notes.append(note_from_text(relative.as_posix(), text, fallback_created=now))
    return tuple(notes)


def _inbox_notes(notes: Sequence[Note]) -> list[Note]:
    return [note for note in notes if note.folder == FOLDER_INBOX]


# ── 渲染（纯函数） ──────────────────────────────────────────


def _planned_table(rows: Sequence[ScheduleRecord], tz: Any) -> str:
    if not rows:
        return "（这一天没有排计划。）"
    lines = ["| 时间 | 做什么 | 在哪 |", "| --- | --- | --- |"]
    lines.extend(
        f"| {_hhmm(row.start_at, tz)}–{_hhmm(row.end_at, tz)} | {row.activity} |"
        f" {row.location or _NO_LOCATION} |"
        for row in rows
    )
    return "\n".join(lines)


def _actual_table(rows: Sequence[ActivityRecord], tz: Any) -> str:
    if not rows:
        return "（这一天的行为日志是空的。）"
    lines = ["| 时间 | 做了什么 | 在哪 |", "| --- | --- | --- |"]
    lines.extend(
        f"| {_hhmm(row.started_at, tz)} | {row.description} | {row.location or _NO_LOCATION} |"
        for row in rows
    )
    return "\n".join(lines)


def _deviations(rows: Sequence[ScheduleRecord]) -> list[str]:
    """计划里写了「为什么没按计划来」的那些。"""
    return [
        f"- 计划 {row.activity}：{row.deviation_note}" for row in rows if row.deviation_note.strip()
    ]


def render_day(
    *,
    day: date,
    created: datetime,
    tz: Any,
    schedules: Sequence[ScheduleRecord],
    activities: Sequence[ActivityRecord],
    thoughts: Sequence[ActivityRecord],
) -> Note:
    """一天的日记：打算做什么、实际做了什么、心里想着什么。

    三块并排放在一页里，而不是拆成三篇——「计划 20:00 读书，实际 21:30 才坐下」
    这个对照本身就是内容，分开写就看不出来了。

    ``thoughts`` 是当天那些有内心独白的行为。这里只写时间和那句话，
    **不放 ``[[链接]]``**：独白单独成篇进了收集箱，而收集箱里的笔记会被
    ``organize`` 改名——日程页是**派生**的、每次 ``sync`` 重写，它引用的
    名字却由另一步决定，放链接等于每整理一次就在日程里留几条坏链。
    想找那篇，``00-索引/想法.md`` 里全在。
    """
    lines = [f"# {day:%Y-%m-%d}", "", "## 今天打算", "", _planned_table(schedules, tz), ""]
    lines += ["## 实际发生", "", _actual_table(activities, tz), ""]

    deviations = _deviations(schedules)
    if deviations:
        lines += ["### 没按计划来的地方", "", *deviations, ""]

    if thoughts:
        lines += ["## 心里想着", ""]
        lines.extend(
            f"- {_hhmm(record.started_at, tz)} {_short(record.inner_voice)}" for record in thoughts
        )
        lines.append("")

    return Note(
        path=f"{FOLDER_SCHEDULE}/{day:%Y-%m-%d}.md",
        title=f"{day:%Y-%m-%d}",
        type=TYPES[FOLDER_SCHEDULE],
        created=created,
        tags=("日程", f"{day:%Y-%m}"),
        body="\n".join(lines).rstrip(),
    )


def render_thought(record: ActivityRecord, *, created: datetime, tz: Any) -> Note:
    """一条被拦下来的内心独白，单独成篇进收集箱。

    它比别的东西更值得单独放：``inner_voice`` 是「想说但没说」的那部分，
    而描述里那句 ``suppressed_intent`` 已经写在行为日志里了。
    """
    title = _short(record.inner_voice) or "没说完的一句话"
    body = "\n\n".join(
        [
            f"# {title}",
            record.inner_voice.strip(),
            f"- 本来想做：{record.description}",
            f"- 当时是：{_hhmm(record.started_at, tz)}",
        ]
    )
    return Note(
        path=f"{FOLDER_INBOX}/{_inbox_name(title, record.id)}.md",
        title=title,
        type=TYPES[FOLDER_INBOX],
        created=created,
        tags=("想法",),
        body=body,
    )


def render_source(record: SourceRecord, *, created: datetime, tz: Any) -> Note:
    """一条搜集到的东西，进收集箱等它自己决定算哪一类。"""
    title = record.title.strip() or _short(record.summary, limit=30) or "一条没有标题的链接"
    lines = [f"# {title}", ""]
    if record.summary.strip():
        lines += [f"> {record.summary.strip()}", ""]
    lines.append(f"- 来源：{record.url}")
    lines.append(f"- 抓到的：{_hhmm(record.fetched_at, tz)}")
    if record.published_at is not None:
        lines.append(f"- 它自己标的日期：{record.published_at:%Y-%m-%d}")
    if record.lang:
        lines.append(f"- 语言：{record.lang}")

    tags = ["读到的"]
    if record.source_kind:
        tags.append(record.source_kind)

    return Note(
        path=f"{FOLDER_INBOX}/{_inbox_name(title, record.id)}.md",
        title=title,
        type=TYPES[FOLDER_SOURCES],
        created=created,
        tags=tuple(tags),
        body="\n".join(lines),
    )


def render_memory(memory: Memory, *, created: datetime, tz: Any) -> Note:
    """一条记忆，进收集箱。"""
    title = memory.summary.strip() or _short(memory.content, limit=30) or "一件记得的事"
    tags = ("记得的事", memory.kind, *memory.tags)
    body = "\n".join(
        [
            f"# {title}",
            "",
            memory.content.strip(),
            "",
            f"- 记得的时候：{_hhmm(memory.occurred_at, tz)}",
            f"- 这一类：{memory.kind}",
        ]
    )
    return Note(
        path=f"{FOLDER_INBOX}/{_inbox_name(title, memory.id)}.md",
        title=title,
        type=TYPES[FOLDER_INBOX],
        created=created,
        tags=tags,
        body=body,
    )


# ── 整理进度 ────────────────────────────────────────────────


def _state_path(root: Path) -> Path:
    return root / FOLDER_INDEX / STATE_FILENAME


def _load_state(root: Path) -> dict[str, Any]:
    """读整理进度。文件坏了就当没有——它只是个缓存，丢了最坏是重问一次模型。"""
    path = _state_path(root)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_state(root: Path, state: dict[str, Any]) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )


# ── 三个动作 ────────────────────────────────────────────────


def init_vault(work: VaultWorkbench, *, now: datetime) -> tuple[Path, ...]:
    """把库搭起来：目录、``.obsidian/``、索引页。

    索引页要**一次写全套**：总索引链到每个内容目录的索引页，缺一张就是一条
    坏链。刚 init 完的库在 ``vault status`` 里报「5 处链接指向不存在的笔记」
    会让人以为搭坏了，而其实只是这一步少写了几张空页。

    重复调用是安全的——已经写下的东西不会被动。用户把整个库删了再跑一次，
    或者第一次 ``init`` 之后手改坏了想重来，都走到这里。
    """
    created: list[Path] = []
    for folder in FOLDERS:
        (work.root / folder).mkdir(parents=True, exist_ok=True)

    obsidian = work.root / _OBSIDIAN_DIR
    obsidian.mkdir(parents=True, exist_ok=True)
    for name, payload in (("app.json", _APP_JSON), ("core-plugins.json", _CORE_PLUGINS)):
        path = obsidian / name
        if not path.is_file():
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
                newline="\n",
            )
            created.append(path)

    root_note = _root_index(work, (), now=now)
    created.append(_write(work.root, root_note))
    created.extend(
        _write(work.root, build_folder_index(folder, created=now, notes=(), parent=root_note.stem))
        for folder in CONTENT_FOLDERS
    )
    work.logger.info("知识库已就位", extra={"root": str(work.root)})
    return tuple(created)


def _root_index(work: VaultWorkbench, notes: Sequence[Note], *, now: datetime) -> Note:
    """总索引页。库的开场白写在它的开头。"""
    intro = f"我是 {work.persona.name}。这里记我的日程、看到的东西、想到的事。"
    return build_root_index(
        name=f"{work.persona.name}的知识库",
        created=now,
        notes=notes,
        total=len(notes),
        intro=intro,
    )


def sync(work: VaultWorkbench, *, now: datetime, lookback_days: int = _LOOKBACK_DAYS) -> SyncReport:
    """把库里的事渲染成笔记。**不调模型，不花钱，不会失败。**

    这一步只做搬字：``schedule_block`` → 日程页，``activity_log`` 的
    内心独白 → 收集箱，``source_item`` / ``memory`` → 收集箱。

    日程页**直接落到它的目录**，不经过收集箱。因为日程的归属是确定的，
    让它去问模型「这个算不算日程」纯属浪费——真正的判断在「这条独白是
    自我觉察还是关于某个人的」这类问题上，那才值得花一次调用。

    幂等靠**文件名由数据决定**：日程页叫 ``2026-09-16.md``，收集箱里每条叫
    ``标题-短id``。同一条数据算出来的路径永远一样，重跑就是原样覆盖一遍。
    """
    since = now - timedelta(days=lookback_days)
    tz = now.tzinfo
    work.root.mkdir(parents=True, exist_ok=True)

    persona_id = work.persona.id
    schedules = work.schedules.list_range(persona_id, since=since, until=now, limit=_MAX_FETCH)
    activities = work.activities.list_range(persona_id, since=since, until=now, limit=_MAX_FETCH)
    sources = work.sources.list_kept(persona_id, since=since, limit=_MAX_FETCH)
    memories = work.memories.list_recent(persona_id, since=since, limit=_MAX_FETCH)

    thoughts = [record for record in activities if record.inner_voice.strip()]
    thought_notes = {
        record.id: render_thought(record, created=record.started_at, tz=tz) for record in thoughts
    }

    days = sorted(
        {row.day for row in schedules} | {_local_date(row.started_at, tz) for row in activities}
    )
    written = 0
    for day in days:
        on_day = [record for record in thoughts if _local_date(record.started_at, tz) == day]
        note = render_day(
            day=day,
            created=datetime(day.year, day.month, day.day, tzinfo=tz),
            tz=tz,
            schedules=[row for row in schedules if row.day == day],
            activities=[row for row in activities if _local_date(row.started_at, tz) == day],
            thoughts=on_day,
        )
        _write(work.root, note)
        written += 1

    inbox_written = 0
    for note in [*thought_notes.values()]:
        _write(work.root, note)
        inbox_written += 1
    for record in sources:
        _write(work.root, render_source(record, created=record.fetched_at, tz=tz))
        inbox_written += 1
    for memory in memories:
        _write(work.root, render_memory(memory, created=memory.occurred_at, tz=tz))
        inbox_written += 1

    inbox = (
        len(list((work.root / FOLDER_INBOX).glob("*.md")))
        if (work.root / FOLDER_INBOX).is_dir()
        else 0
    )
    work.logger.info(
        "知识库已同步",
        extra={"days": len(days), "written": written, "inbox": inbox_written},
    )
    # 索引是**派生**的，笔记一写下来它就过时了。这里顺手重建一次。
    # 不建的话，用户打开 Obsidian 看到的是 ``init`` 时那几张空索引页，
    # 而旁边目录里明明躺着一堆笔记——那看起来像同步失败了。
    build(work, now=now)
    return SyncReport(
        days=len(days),
        thoughts=len(thoughts),
        sources=len(sources),
        memories=len(memories),
        written=written + inbox_written,
        inbox=inbox,
    )


def build(work: VaultWorkbench, *, now: datetime) -> BuildReport:
    """按实际文件重算所有索引页，然后校验四条不变量。

    索引是**派生**的，不是记着的——扫描目录在前、生成索引在后，所以
    「索引里有的笔记其实被删了」和「新笔记没进索引」这两种情况同时消失。

    校验放在最后，而且**只报不拦**：角色刚起的链接名可能指向一篇还没写的
    笔记，那不算错。它会出现在 ``vault status`` 的输出里，用户（或者它自己
    下一轮）看得到就够了。
    """
    notes = [
        note for note in scan(work.root, now=now, logger=work.logger) if note.folder != FOLDER_INDEX
    ]
    root_note = _root_index(work, notes, now=now)
    fresh = [*notes, root_note]
    fresh.extend(
        build_folder_index(folder, created=now, notes=notes, parent=root_note.stem)
        for folder in CONTENT_FOLDERS
    )
    for note in fresh:
        _write(work.root, note)

    after = scan(work.root, now=now, logger=work.logger)
    issues = validate(after)
    work.logger.info("索引已重建", extra={"notes": len(notes), "issues": len(issues)})
    return BuildReport(scanned=len(notes), indexed=len(fresh), issues=issues)


def _catalog(notes: Sequence[Note]) -> str:
    """给模型看的已有笔记清单：``名字 · 标题``。"""
    listed = [note for note in notes if note.folder in CONTENT_FOLDERS][:_MAX_CATALOG]
    if not listed:
        return "（还没有任何笔记，这是第一次整理。）"
    lines = [
        f"- {note.stem} · {note.title}（{TYPES.get(note.folder, note.folder)}）" for note in listed
    ]
    hidden = len(notes) - len(listed)
    if hidden > 0:
        lines.append(f"（还有 {hidden} 篇没列出来）")
    return "\n".join(lines)


def _folder_layout() -> str:
    return "\n".join(f"- {folder} · {TYPES[folder]}" for folder in CONTENT_FOLDERS)


def _format_inbox(notes: Sequence[Note]) -> str:
    blocks = [f"### {note.path}\n\n{note.body.strip()}\n" for note in notes]
    return "\n".join(blocks)


async def organize(work: VaultWorkbench, *, now: datetime, dry_run: bool = False) -> OrganizeReport:
    """让角色自己把收集箱里的东西归位。

    ``dry_run`` 不调模型，只把收集箱摊开给人看——花不花钱应该先看得见。
    """
    notes = scan(work.root, now=now, logger=work.logger)
    inbox = _inbox_notes(notes)

    if not inbox:
        return OrganizeReport(
            issues=validate(notes),
            skipped="收集箱是空的。先跑 `alterego vault sync` 把素材搬进来。",
        )

    preview = tuple(f"{note.path} · {note.title}" for note in inbox[:_MAX_INBOX])
    if dry_run:
        return OrganizeReport(read=len(inbox), preview=preview, issues=validate(notes))

    batch = inbox[:_MAX_INBOX]
    prompt = work.prompts.render(
        _TEMPLATE,
        persona_name=work.persona.name,
        user_name=work.user_name,
        folders=_folder_layout(),
        catalog=_catalog(notes),
        inbox=_format_inbox(batch),
        max_items=len(batch),
    )
    try:
        response = await work.gateway.complete(
            PURPOSE,
            prompt,
            temperature=_TEMPERATURE,
            max_tokens=_MAX_TOKENS,
            response_format="json",
        )
    except Exception as exc:
        raise SimulationError("知识库整理时模型调用失败", purpose=PURPOSE) from exc

    plan = parse_organize_plan(response.text, inbox_stems=[note.stem for note in batch])
    if not plan.decisions:
        # 一条都没过关：**一个字节都不动，连进度都不记**。
        # 「最后一次整理」指的是真的归位过东西的那一次；把一次失败的尝试
        # 记上去，下次来看进度的人会以为收集箱已经清过了。
        work.logger.warning("整理结果没有一条能用", extra={"rejected": len(plan.rejected)})
        return OrganizeReport(read=len(inbox), rejected=plan.rejected, preview=preview)

    by_stem = {note.stem: note for note in batch}
    taken = {note.path for note in notes}
    filed = 0

    for decision in plan.decisions:
        source = by_stem[decision.inbox_stem]
        note = resolve_path(
            decision,
            taken=taken,
            created=source.created,
            body=source.body,
        )
        _write(work.root, note)
        taken.add(note.path)
        obsolete = work.root / source.path
        if obsolete.is_file() and source.path != note.path:
            obsolete.unlink()
        filed += 1

    state = _load_state(work.root)
    state["organized"] = sorted(
        {*state.get("organized", []), *(d.inbox_stem for d in plan.decisions)}
    )
    state["last_organize"] = now.isoformat()
    _save_state(work.root, state)

    report = build(work, now=now)
    work.logger.info("整理完成", extra={"filed": filed, "rejected": len(plan.rejected)})
    return OrganizeReport(
        read=len(inbox),
        filed=filed,
        rejected=plan.rejected,
        preview=preview,
        issues=report.issues,
    )

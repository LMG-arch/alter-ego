"""库结构：目录怎么排、索引怎么长、以及四条不变量的校验。

这个模块只关心**库**，不关心文件系统和数据库——给它一串
:class:`~alterego.domain.knowledge.Note`，它告诉你这堆笔记合不合格、索引该长什么样。
真正落盘在 `sim/vault.py`。

## 四条不变量

「把每天的日程和自己搜集的东西条理清晰地写好」是需求的原话，而
「条理清晰」不是一个形容词，是**四条能自动检查的规矩**：

| # | 规矩 | 谁保证 |
| --- | --- | --- |
| 1 | 每篇都有 frontmatter，含 ``title`` / ``type`` / ``created`` / ``tags`` | :func:`validate` |
| 2 | 每个 ``[[链接]]`` 都指向存在的笔记 | :func:`validate` |
| 3 | 没有重名的笔记（链接写 ``[[阿哲]]`` 时不该有歧义） | :func:`validate` |
| 4 | 每篇笔记都被它所在目录的索引页覆盖（没有孤儿） | :func:`validate` |

这四条**由代码保证，不交给模型**。理由是这个项目的第一条硬约束
（``AGENTS.md`` § 2 的 P3）：机制约束优于提示词祈祷。写一句「请确保链接有效」
进提示词，模型大概会照做；但那个「大概」会随着模型、温度、上下文长度漂移，
而坏链是**写进用户文件里**的，用户看不到我们事后有没有补救。

## 角色能决定什么

代码管骨架，角色管分类。:class:`OrganizeDecision` 就是角色那份决定的形状：
这条素材归到哪个目录、叫什么名字、打什么标签、和哪些笔记互链。
:func:`parse_organize_plan` 是它和代码之间的那道关。

**逐条跳过，不是整批丢弃。** 这和记忆梳理相反
（`domain/consolidation.py` 那里必须整批丢），因为语义不同：
一条整理决定坏了，代价是**这一篇留在收集箱**，别的篇不受影响，
也不存在「半批中间态污染下一次输入」的问题。
反过来，一条笔记路径写错就让整次整理白费，代价明显更大。

依据: docs/plans/2026-09-16-obsidian-vault.md § 4–5
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

from alterego.domain.knowledge import Note, parse_wikilinks, slugify


__all__ = [
    "CONTENT_FOLDERS",
    "FOLDERS",
    "FOLDER_INBOX",
    "FOLDER_INDEX",
    "STATE_FILENAME",
    "TYPES",
    "OrganizeDecision",
    "OrganizePlan",
    "VaultIssue",
    "build_folder_index",
    "build_root_index",
    "parse_organize_plan",
    "resolve_path",
    "validate",
]


# ── 布局 ────────────────────────────────────────────────────

FOLDER_INDEX: Final[str] = "00-索引"
FOLDER_SCHEDULE: Final[str] = "10-日程"
FOLDER_THOUGHTS: Final[str] = "20-想法"
FOLDER_SOURCES: Final[str] = "30-读到的"
FOLDER_MEMORIES: Final[str] = "40-记得的事"
FOLDER_PEOPLE: Final[str] = "50-见过的人"
FOLDER_INBOX: Final[str] = "99-收集箱"

FOLDERS: Final[tuple[str, ...]] = (
    FOLDER_INDEX,
    FOLDER_SCHEDULE,
    FOLDER_THOUGHTS,
    FOLDER_SOURCES,
    FOLDER_MEMORIES,
    FOLDER_PEOPLE,
    FOLDER_INBOX,
)
"""库里的全部目录，**顺序即显示顺序**。

名字带两位数前缀是为了让 Obsidian 的文件树永远按这个顺序排。
不依赖 Obsidian 的排序设置，不依赖用户手动拖——文件系统层面就是对的，
换个编辑器打开也一样。
"""

CONTENT_FOLDERS: Final[tuple[str, ...]] = (
    FOLDER_SCHEDULE,
    FOLDER_THOUGHTS,
    FOLDER_SOURCES,
    FOLDER_MEMORIES,
    FOLDER_PEOPLE,
)
"""角色可以把收集箱里的东西放进去的目录。索引与收集箱不在其中。"""

TYPES: Final[dict[str, str]] = {
    FOLDER_SCHEDULE: "日程",
    FOLDER_THOUGHTS: "想法",
    FOLDER_SOURCES: "读到的",
    FOLDER_MEMORIES: "记得的事",
    FOLDER_PEOPLE: "见过的人",
    FOLDER_INBOX: "待整理",
    FOLDER_INDEX: "索引",
}
"""每个目录对应的 ``type`` 字段值。"""

STATE_FILENAME: Final[str] = ".vault-state.json"
"""整理进度记在这儿。以点开头，Obsidian **不显示**，不会污染用户的库。"""

_SECTION_INDEX: Final[str] = "## 相关"


# ── 校验 ────────────────────────────────────────────────────


IssueKind = Literal[
    "missing_frontmatter",
    "bad_path",
    "broken_link",
    "duplicate_stem",
    "orphan",
]


@dataclass(frozen=True, slots=True)
class VaultIssue:
    """一条不合规的地方。

    ``detail`` 要写成**能照着修**的句子。``alterego vault status`` 会把它
    原样打出来，而看它的人（用户或角色自己）手上只有这一行。
    """

    kind: IssueKind
    path: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}：{self.detail}"


def _stem_index(notes: Sequence[Note]) -> dict[str, list[str]]:
    """笔记名 → 拥有它的路径列表。长度大于一就是歧义。"""
    index: dict[str, list[str]] = {}
    for note in notes:
        index.setdefault(note.stem, []).append(note.path)
    return index


def _link_targets(stems: dict[str, list[str]], paths: set[str]) -> set[str]:
    """所有「链接写得出、也解得开」的名字。

    两种写法都合法：``[[阿哲]]`` 和 ``[[50-见过的人/阿哲]]``。
    """
    targets = set(stems)
    targets.update(path.removesuffix(".md") for path in paths)
    return targets


def validate(notes: Sequence[Note]) -> tuple[VaultIssue, ...]:
    """检查四条不变量，返回全部问题（不是第一个）。

    一次报全，是因为修的时候通常要一起修——一次报一条会让人来回跑。
    没有问题时返回空元组，调用方用 ``if issues:`` 判断就够了。
    """
    issues: list[VaultIssue] = []
    paths = {note.path for note in notes}
    stems = _stem_index(notes)

    for note in notes:
        if not note.path.endswith(".md"):
            issues.append(VaultIssue("bad_path", note.path, "笔记必须是 .md 结尾"))
        elif note.folder not in FOLDERS:
            issues.append(
                VaultIssue(
                    "bad_path", note.path, f"目录不在库的布局里（应为 {'/'.join(FOLDERS)} 之一）"
                )
            )
        if not note.title.strip():
            issues.append(VaultIssue("missing_frontmatter", note.path, "缺 title"))

    for stem, owners in sorted(stems.items()):
        if len(owners) > 1:
            issues.append(
                VaultIssue(
                    "duplicate_stem",
                    owners[0],
                    f"「{stem}」同时存在于 {'、'.join(owners)}；链接 [[{stem}]] 会有歧义",
                )
            )

    known = _link_targets(stems, paths)
    issues.extend(
        VaultIssue("broken_link", note.path, f"链接 [[{target}]] 指向的笔记不存在")
        for note in notes
        for target in note.links
        if target not in known
    )

    issues.extend(_orphans(notes))
    return tuple(issues)


def _orphans(notes: Sequence[Note]) -> list[VaultIssue]:
    """每篇内容笔记都得被它所在目录的索引页提到。

    这条是在验**索引生成器**本身：索引由代码从实际文件推导，
    既然如此就不该漏。一旦漏了，说明生成逻辑和数据形状对不上了——
    那正是最需要被早发现的那类问题。
    """
    indexed: dict[str, set[str]] = {}
    for note in notes:
        if note.folder != FOLDER_INDEX:
            continue
        for target in parse_wikilinks(note.body):
            indexed.setdefault(target, set()).add(note.path)

    issues: list[VaultIssue] = []
    for note in notes:
        if note.folder in {FOLDER_INDEX, FOLDER_INBOX}:
            continue
        # 链接可能写成 [[阿哲]]，也可能写成 [[50-见过的人/阿哲]]。
        if note.stem in indexed or note.path.removesuffix(".md") in indexed:
            continue
        issues.append(
            VaultIssue(
                "orphan",
                note.path,
                f"没有被 {FOLDER_INDEX} 下的索引页提到（链接名应为 [[{note.stem}]]）",
            )
        )
    return issues


# ── 索引页 ──────────────────────────────────────────────────


def _link(note: Note) -> str:
    """索引页里的一行：``- [[名字]] · 标题 · 日期``。

    名字在前、标题在后，是因为**名字才是链接目标**。倒过来会在
    「标题和文件名不一样」的时候让人对不上号。
    """
    stamp = f"{note.created:%Y-%m-%d}"
    if note.title == note.stem:
        return f"- [[{note.stem}]] · {stamp}"
    return f"- [[{note.stem}]] · {note.title} · {stamp}"


def build_folder_index(
    folder: str,
    *,
    created: datetime,
    notes: Sequence[Note],
    intro: str = "",
    parent: str = "",
) -> Note:
    """给一个目录生成索引页。

    没有条目时也照样生成，正文写一句「还是空的」——空文件在 Obsidian 里
    看起来像坏了，而一句「还是空的」明确表示这是正常状态。

    ``parent`` 是总索引页的名字（不带 ``.md``）。传了才会加 ``## 相关`` ——
    名字必须由调用方给：写死一个「索引」就是一条指向不存在笔记的坏链，
    而坏链正是这个模块存在的理由。
    """
    type_name = TYPES.get(folder, folder)
    entries = sorted(
        (note for note in notes if note.folder == folder),
        key=lambda note: (note.created, note.stem),
        reverse=True,
    )

    lines = [f"# {type_name}", "", intro.strip(), ""] if intro.strip() else [f"# {type_name}", ""]
    if entries:
        lines.append("## 全部")
        lines.append("")
        lines.extend(_link(note) for note in entries)
    else:
        lines.append("还是空的。")
    if parent:
        lines += ["", _SECTION_INDEX, "", f"- [[{parent}]]"]

    return Note(
        path=f"{FOLDER_INDEX}/{slugify(type_name)}.md",
        title=type_name,
        type=TYPES[FOLDER_INDEX],
        created=created,
        tags=("索引",),
        body="\n".join(lines),
    )


def build_root_index(
    *,
    name: str,
    created: datetime,
    notes: Sequence[Note],
    total: int,
    intro: str = "",
) -> Note:
    """生成总索引页，也就是 Obsidian 图谱的入口。

    ``total`` 单独传而不是从 ``notes`` 数出来：索引页自己在 ``notes`` 里，
    算进去会让「共几篇」永远多一篇，而这种差一错误没人会去查。
    """
    lines = [
        f"# {name}",
        "",
        intro.strip() or "这是我自己的地方。想到什么就写下来，慢慢就长成这样了。",
        "",
        f"共 {total} 篇。",
        "",
    ]
    for folder in CONTENT_FOLDERS:
        type_name = TYPES[folder]
        count = sum(1 for note in notes if note.folder == folder)
        lines.append(f"- [[{type_name}]] · {count} 篇")
    lines.append("")
    lines.append(_SECTION_INDEX)
    lines.append("")
    lines.extend(f"- [[{TYPES[folder]}]]" for folder in CONTENT_FOLDERS)

    return Note(
        path=f"{FOLDER_INDEX}/{slugify(name)}.md",
        title=name,
        type=TYPES[FOLDER_INDEX],
        created=created,
        tags=("索引",),
        body="\n".join(lines),
    )


# ── 角色的整理决定 ──────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OrganizeDecision:
    """角色对**一篇**收集箱笔记的去向决定。

    Attributes:
        inbox: 收集箱里的文件名（带或不带 ``99-收集箱/`` 前缀都接受）。
        folder: 放进哪个目录。必须是 :data:`CONTENT_FOLDERS` 之一。
        title: 它给这篇起的名字。同名文件已经存在时会自动加后缀。
        tags: 标签。
        links: 想链到的笔记名。**链接目标不存在也不拒收**——见
            :func:`parse_organize_plan` 的说明。
        body: 它重写过的正文。留空表示「照原样留着」。
    """

    inbox: str
    folder: str
    title: str
    tags: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    body: str = ""

    @property
    def inbox_stem(self) -> str:
        return self.inbox.rsplit("/", 1)[-1].removesuffix(".md")


@dataclass(frozen=True, slots=True)
class OrganizePlan:
    """一次整理里，哪些决定被采纳了、哪些没有、为什么。

    ``rejected`` 是**给人看的**，所以带上原因而不是只报数量——
    「3 条被跳过」帮不上任何忙，「2 条引用不存在的收集箱文件」能。
    """

    decisions: tuple[OrganizeDecision, ...] = ()
    rejected: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return bool(self.decisions)


def _clean_tags(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    tags: list[str] = []
    for item in values:
        text = str(item).strip().lstrip("#")
        if text and text not in tags:
            tags.append(text)
    return tuple(tags)


def _clean_links(values: Any) -> tuple[str, ...]:
    """链接名**只保留，不校验**。

    指向不存在的笔记不算错——角色刚想到一个新名字，那篇笔记还没写呢。
    真去检查就会发现：知识库的链接是**会往前指的**，把这一点禁掉
    等于要求它只能引用已经写好的东西。

    真正的坏链风险在别处：素材渲染出来的链接由代码生成，一定有效；
    角色加的链接由 :func:`validate` 在整理完之后统一报出来，
    那时候人（或它自己）能看到。
    """
    if not isinstance(values, list):
        return ()
    names: list[str] = []
    for item in values:
        text = str(item).strip().removesuffix(".md").strip()
        if text and text not in names:
            names.append(text)
    return tuple(names)


def parse_organize_plan(text: str, *, inbox_stems: Iterable[str] = ()) -> OrganizePlan:
    """把角色的回复解析成整理计划。

    拦在门口的是**结构性**错误：找不到 JSON、不是数组、缺 ``folder``、
    目录名不在布局里、收集箱里根本没有那一篇。这些一旦放过，
    后面就是拿着空字符串去拼路径。

    放行的则是有余地的东西：标签写法、链接名、正文长短。
    一条笔记的标签起得不好，不值得让整次整理停下——那是内容问题，
    不是结构问题，而内容本来就归它自己管。

    **收集箱文件要查、链接不查**，这个不对称是故意的：
    前者是这次任务的输入（不存在的文件无从移动），
    后者指向的是未来（角色刚想到一个名字，那篇笔记还没写呢）。
    """
    try:
        payload = _json_array(text)
    except ValueError as exc:
        return OrganizePlan(rejected=(("整批", str(exc)),))

    known = {stem.removesuffix(".md") for stem in inbox_stems}
    decisions: list[OrganizeDecision] = []
    rejected: list[tuple[str, str]] = []

    for index, raw in enumerate(payload):
        label = f"第 {index + 1} 条"
        if not isinstance(raw, dict):
            rejected.append((label, "不是一个对象"))
            continue
        folder = str(raw.get("folder", "")).strip().rstrip("/")
        if folder not in CONTENT_FOLDERS:
            rejected.append((label, f"目录「{folder}」不在布局里"))
            continue
        title = str(raw.get("title", "")).strip()
        if not title:
            rejected.append((label, "没有标题"))
            continue
        inbox = str(raw.get("inbox", "")).strip()
        if not inbox:
            rejected.append((label, "没说是收集箱里的哪一篇"))
            continue
        decision = OrganizeDecision(
            inbox=inbox,
            folder=folder,
            title=title,
            tags=_clean_tags(raw.get("tags")),
            links=_clean_links(raw.get("links")),
            body=str(raw.get("body", "")).strip(),
        )
        if known and decision.inbox_stem not in known:
            rejected.append((label, f"收集箱里没有「{decision.inbox_stem}」"))
            continue
        decisions.append(decision)

    return OrganizePlan(decisions=tuple(decisions), rejected=tuple(rejected))


def _json_array(text: str) -> list[Any]:
    """从回复里取出那个 JSON 数组。

    逐个 ``[`` 试着解析，返回第一个解析得通的。**不能图省事取「第一个 ``[``
    到最后一个 ``]`` 之间」**——它总要先说一句「我把这条放进 [20-想法] 了」，
    那个方括号里不是 JSON，按那种切法切出来的东西谁也解析不了，
    而报出来的错看起来像是模型坏了，其实是这里切错了。

    解析都失败时把第一个错误原样带出来：真实的语法问题（少个逗号、
    引号用了中文）比一句笼统的「格式不对」好定位得多。
    """
    decoder = json.JSONDecoder()
    first_error: str | None = None
    for index, char in enumerate(text):
        if char != "[":
            continue
        try:
            payload, _ = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            if first_error is None:
                first_error = exc.msg
            continue
        if isinstance(payload, list):
            return payload
    if first_error is None:
        raise ValueError("回复里找不到 JSON 数组")
    raise ValueError(f"JSON 解析失败：{first_error}")


def resolve_path(
    decision: OrganizeDecision,
    *,
    taken: Iterable[str] = (),
    created: datetime,
    body: str = "",
    index_hint: Sequence[str] = (),
) -> Note:
    """把一条决定定址成真正的 :class:`Note`。

    撞名时加 ``-2`` / ``-3`` 后缀而不是覆盖：覆盖会**悄悄毁掉**另一篇笔记，
    而用户在 Obsidian 里是看不到「刚才那次整理删了东西」的。
    宁可多一个 `怕麻烦别人-2`，让用户自己合并。

    ``index_hint`` 是它想链到的名字，会被渲染进正文末尾的 ``## 相关``。
    只写链接名、不写标题——``[[阿哲]]`` 能自己显示成「阿哲」。
    """
    folder = decision.folder
    stem = slugify(decision.title)
    existing = set(taken)

    candidate = stem
    suffix = 2
    while f"{folder}/{candidate}.md" in existing:
        candidate = f"{stem}-{suffix}"
        suffix += 1

    links = [name for name in (*decision.links, *index_hint) if name and name != candidate]
    text = (decision.body or body).strip()

    if links:
        related = [_SECTION_INDEX, "", *(f"- [[{name}]]" for name in links)]
        text = f"{text}\n\n" + "\n".join(related) if text else "\n".join(related)

    if not text.lstrip().startswith("#"):
        text = f"# {decision.title}\n\n{text}".rstrip()

    return Note(
        path=f"{folder}/{candidate}.md",
        title=decision.title,
        type=TYPES.get(folder, folder),
        created=created,
        tags=decision.tags,
        body=text,
    )

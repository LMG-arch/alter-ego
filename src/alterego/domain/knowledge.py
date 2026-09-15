"""笔记语法：一篇 Markdown 笔记是什么，以及怎么写才不会被 Obsidian 嫌弃。

这个模块只管**一篇笔记自身**的规矩，不管它在库里放在哪（那是 `domain/vault.py`）。
四件事，全是纯函数：

1. :func:`slugify` —— 一个标题变成能安全落盘的文件名；
2. :func:`render_frontmatter` / :func:`parse_frontmatter` —— YAML 头部读写；
3. :func:`parse_wikilinks` —— 从正文里找出 ``[[双链]]``；
4. :class:`Note` —— 上面三样拼起来的一篇笔记。

**为什么手写 frontmatter 而不是 `pyyaml`。** 项目只有两个必需依赖
（`AGENTS.md` § 5），而 frontmatter 用到的是 YAML 里最窄的一个子集：
``key: value`` 加多行列表。为此引入一个解析器，代价是不可逆的——
依赖一旦进来就很难出去。四十行换一条设计原则，划算。

**为什么要 `slugify`。** 角色给笔记起的名字直接来自它的输出，
而 Obsidian 的库最终落在文件系统上。Windows 不允许 ``< > : " / \\ | ? *``，
不允许以点结尾，还留着 ``CON`` / ``NUL`` 这些 1985 年的设备名。
让模型的输出直接当文件名，等于把操作系统的历史包袱交给它去猜。

**为什么要解析双链。** 见 `domain/vault.py` § 校验——坏链比没有链更糟，
而「链接目标存在吗」这个问题必须先能把链接**抠出来**才能问。

依据: docs/plans/2026-09-16-obsidian-vault.md § 4–5
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final


__all__ = [
    "FRONTMATTER_DELIMITER",
    "MAX_NAME_LENGTH",
    "RESERVED_NAMES",
    "Note",
    "NoteDraft",
    "NoteError",
    "frontmatter_lines",
    "heading_of",
    "normalize_link_target",
    "note_from_text",
    "parse_frontmatter",
    "parse_wikilinks",
    "render_frontmatter",
    "render_note",
    "slugify",
]


class NoteError(ValueError):
    """笔记不合法。属于 `domain/`，所以不继承项目自己的错误基类。"""


FRONTMATTER_DELIMITER: Final[str] = "---"
"""YAML 头部的分界。Obsidian 只认三个连字符，不能多也不能少。"""

MAX_NAME_LENGTH: Final[int] = 80
"""文件名长度上限（字符）。"""

RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
"""Windows 保留的设备名。它们**带扩展名也照样被保留**（``CON.md`` 同样非法）。"""

_ILLEGAL_CHARS: Final[re.Pattern[str]] = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")

_WIKILINK_RE: Final[re.Pattern[str]] = re.compile(r"\[\[([^\[\]\n]+?)\]\]")
_FENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(`{3,}|~{3,}).*?^\1\s*$", re.DOTALL | re.MULTILINE
)
_INLINE_CODE_RE: Final[re.Pattern[str]] = re.compile(r"`[^`\n]*`")
_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

_NUMBER_LIKE: Final[re.Pattern[str]] = re.compile(r"^-?\d+(?:\.\d+)?$")

#: 值里出现这些字符时，YAML 会把它读成别的东西。宁可加引号。
_NEEDS_QUOTING: Final[tuple[str, ...]] = (":", "#", "\n", "\t")

#: 这些首字符在 YAML 里有语法含义（列表项、锚点、标签、引号……）。
_LEADING_SPECIALS: Final[str] = "-?*&!%@`[]{}>|'\""

_AMBIGUOUS_WORDS: Final[frozenset[str]] = frozenset(
    {"true", "false", "yes", "no", "on", "off", "null", "none", "~"}
)


# ── 文件名 ──────────────────────────────────────────────────


def slugify(name: str, *, fallback: str = "未命名", max_length: int = MAX_NAME_LENGTH) -> str:
    """把一段文字变成能安全落盘的文件名（不含扩展名）。

    保留中文与空格：``怕麻烦别人`` 和 ``2026-09-16`` 都是合法且可读的名字，
    把它们转成拼音或下划线只会让用户在文件管理器里认不出来。

    Args:
        name: 想用的名字，通常来自角色的输出。
        fallback: 全部字符都被剔除时用什么。默认 ``未命名``。
        max_length: 长度上限，超出直接截断（不做省略号，文件名的可读性
            不如它带来的麻烦重要）。

    Returns:
        不含路径分隔符、不以点或空格结尾、非空的名字。
    """
    text = unicodedata.normalize("NFC", name)
    text = _ILLEGAL_CHARS.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    # Windows 上以点结尾的名字会被静默去掉那个点，于是「同名」却打不开。
    text = text.strip(". ")

    if len(text) > max_length:
        text = text[:max_length].rstrip(". ")

    if not text:
        return fallback

    # 保留名大小写不敏感，且带扩展名也算：这里直接改掉，不报错——
    # 一个叫「CON」的想法不值得让整次整理失败。
    if text.upper() in RESERVED_NAMES:
        return f"{text}_"
    return text


def normalize_link_target(target: str) -> str:
    """把 ``[[...]]`` 里的内容归一成「用在文件上的那个名字」。

    ``[[50-见过的人/阿哲|阿哲]]`` 与 ``[[阿哲]]`` 指向同一篇笔记，
    ``[[2026-09-16#下午|那天下午]]`` 也是。别名和小节都不是目标的一部分。

    Obsidian 支持「只写文件名」，所以这里**保留路径部分**：
    带路径的链接更精确，去掉反而会制造歧义。
    """
    text = target.split("|", 1)[0]
    text = text.split("#", 1)[0]
    return text.strip().removesuffix(".md").strip()


def parse_wikilinks(text: str) -> tuple[str, ...]:
    """找出正文里所有的 ``[[双链]]``，按出现顺序返回目标（已归一化）。

    **跳过代码块和行内代码。** 提示词模板里经常写 ``[[链接]]`` 当例子，
    素材正文里也可能引用一段代码。把那些当成真链接，会让校验报一堆
    根本不存在的坏链，而每一条都在教角色「别写链接」。

    去重保留首次出现的顺序：索引页里同一篇笔记被链两次没有意义，
    但顺序影响可读性，所以不用 `set`。
    """
    stripped = _FENCE_RE.sub(" ", text)
    stripped = _INLINE_CODE_RE.sub(" ", stripped)

    seen: dict[str, None] = {}
    for raw in _WIKILINK_RE.findall(stripped):
        target = normalize_link_target(raw)
        if target:
            seen.setdefault(target, None)
    return tuple(seen)


# ── frontmatter ─────────────────────────────────────────────


def _quote(value: str) -> str:
    """按需给标量加双引号。"""
    needs = (
        not value
        or value != value.strip()
        or value.lower() in _AMBIGUOUS_WORDS
        or bool(_NUMBER_LIKE.match(value))
        or any(marker in value for marker in _NEEDS_QUOTING)
        or value[0] in _LEADING_SPECIALS
    )
    if not needs:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def frontmatter_lines(values: Mapping[str, str | Sequence[str]]) -> list[str]:
    """把字段渲染成 frontmatter 的**内部行**（不含分界符）。

    列表用多行 ``- item`` 而不是 ``[a, b]``：Obsidian 的 properties 面板
    把多行列表显示成可点的小标签，把行内列表显示成一整串文本。
    差别只在这里，但用户天天看的是那一栏。

    键的顺序**按传入顺序**，不排序。调用方传 ``title, type, created, tags``
    时希望它就长这样，而不是被字母序打乱成 ``created, tags, title, type``。
    """
    lines: list[str] = []
    for key, value in values.items():
        if isinstance(value, str):
            lines.append(f"{key}: {_quote(value)}")
            continue
        items = [str(item) for item in value]
        if not items:
            # 空列表写 `tags: []`：写成 `tags:` 会被读成空值，
            # Obsidian 那边表现成「这个属性没值」，和「没有标签」不是一回事。
            lines.append(f"{key}: []")
            continue
        lines.append(f"{key}:")
        lines.extend(f"  - {_quote(item)}" for item in items)
    return lines


def render_frontmatter(values: Mapping[str, str | Sequence[str]]) -> str:
    """渲染完整的 frontmatter 块（含首尾分界符与尾随换行）。"""
    if not values:
        raise NoteError("frontmatter 不能为空——每篇笔记都要能被检索到")
    body = "\n".join(frontmatter_lines(values))
    return f"{FRONTMATTER_DELIMITER}\n{body}\n{FRONTMATTER_DELIMITER}\n"


def _unquote(value: str) -> str:
    """去掉包裹的双引号并还原转义。没有引号就原样返回。"""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, str | list[str]], str]:
    """把一篇笔记拆成 ``(frontmatter, 正文)``。

    没有 frontmatter 时返回 ``({}, 原文)``——**不报错**。
    校验那道关会另外问「它该不该有」，而这个函数只管「有没有、是什么」。
    两件事混在一起会让「缺 frontmatter」这条错误信息变得难写。
    """
    if not text.startswith(FRONTMATTER_DELIMITER):
        return {}, text

    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        return {}, text

    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == FRONTMATTER_DELIMITER:
            end = index
            break
    if end is None:
        return {}, text

    values: dict[str, str | list[str]] = {}
    current: str | None = None
    filled: set[str] = set()

    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith("  - ") or raw.startswith("- "):
            item = _unquote(raw.lstrip()[2:].strip())
            if current is None:
                continue
            existing = values.get(current)
            if isinstance(existing, list):
                existing.append(item)
            else:
                values[current] = [item]
            filled.add(current)
            continue

        key, _, raw_value = raw.partition(":")
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            continue
        # 空的 ``key:`` 先按「可能是列表头」占位，走完再收——
        # 到底是有几张列表项还是一个空值，得看后面几行才知道。
        values[key] = [] if (not raw_value or raw_value == "[]") else _unquote(raw_value)
        current = key

    for key, value in values.items():
        if isinstance(value, list) and not value and key not in filled:
            values[key] = ""

    return values, "\n".join(lines[end + 1 :]).lstrip("\n")


# ── 一篇笔记 ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Note:
    """一篇笔记。

    Attributes:
        path: 相对库根的 POSIX 路径，例如 ``10-日程/2026-09/2026-09-16.md``。
            用 POSIX 分隔符而不是 `pathlib`：它是**库里的地址**，不是本机路径。
            库可能被同步到别的操作系统上，而 ``[[链接]]`` 里的斜杠永远是斜杠。
        title: 标题。进 frontmatter，也进索引页。
        type: 分类名，对应所在目录（``日程`` / ``想法`` / …）。
        created: 创建时间（**虚拟时间**）。复盘时对不上时间戳的知识库没法用。
        tags: 标签。
        body: 正文，不含 frontmatter。
    """

    path: str
    title: str
    type: str
    created: datetime
    tags: tuple[str, ...] = ()
    body: str = ""

    @property
    def stem(self) -> str:
        """文件名去掉扩展名。``[[阿哲]]`` 这种只写名字的链接靠它匹配。"""
        name = self.path.rsplit("/", 1)[-1]
        return name.removesuffix(".md")

    @property
    def folder(self) -> str:
        """所在目录（相对库根）。库根下的文件返回空串。"""
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""

    @property
    def links(self) -> tuple[str, ...]:
        """正文里指向别的笔记的链接（已归一化）。"""
        return parse_wikilinks(self.body)

    def frontmatter(self) -> dict[str, str | list[str]]:
        """按固定顺序给出四个字段。

        顺序固定，是为了让 diff 干净：同一篇笔记重新渲染不该产生
        一堆只有字段顺序变了的变化，那会让人以为内容被改过（P6）。
        """
        return {
            "title": self.title,
            "type": self.type,
            "created": self.created.isoformat(),
            "tags": list(self.tags),
        }

    def render(self) -> str:
        """整篇笔记的最终文本，以换行结尾。"""
        body = self.body.strip()
        text = render_frontmatter(self.frontmatter())
        if body:
            text += f"\n{body}\n"
        return text


@dataclass(frozen=True, slots=True)
class NoteDraft:
    """一篇还没定下路径的笔记。渲染器产出它，再由 `domain/vault.py` 定址。"""

    title: str
    type: str
    created: datetime
    body: str
    tags: tuple[str, ...] = field(default_factory=tuple)

    def at(self, folder: str, *, filename: str | None = None) -> Note:
        """给它一个位置，变成真正的 :class:`Note`。"""
        name = slugify(filename or self.title)
        path = f"{folder}/{name}.md" if folder else f"{name}.md"
        return Note(
            path=path,
            title=self.title,
            type=self.type,
            created=self.created,
            tags=tuple(self.tags),
            body=self.body,
        )


def render_note(
    *,
    path: str,
    title: str,
    type: str,
    created: datetime,
    tags: Sequence[str] = (),
    body: str = "",
) -> str:
    """一步渲染出笔记文本。给不打算构造对象的调用方用。"""
    return Note(
        path=path,
        title=title,
        type=type,
        created=created,
        tags=tuple(tags),
        body=body,
    ).render()


def note_from_text(path: str, text: str, *, fallback_created: datetime) -> Note:
    """从磁盘上的文本读回一篇笔记。:meth:`Note.render` 的逆运算。

    正文没有标题时**补一个**上去，而不是只填 ``title`` 字段：标题在
    Obsidian 里是节点名，一篇「有 title 但正文第一行是空」的笔记打开是
    一片空白，用户不知道该拿它怎么办。

    Args:
        path: 相对库根的 POSIX 路径。
        text: 文件内容。
        fallback_created: frontmatter 里没有 ``created`` 或格式不对时用什么。
            **不用墙上时间**——那会让「同一份内容扫两次」产出两个不同的结果，
            而扫描是只读操作，不该有时间副作用（P6）。
    """
    values, body = parse_frontmatter(text)
    stem = path.rsplit("/", 1)[-1].removesuffix(".md")
    title = _as_text(values.get("title")) or heading_of(body, fallback=stem)
    created = _parse_iso(_as_text(values.get("created"))) or fallback_created
    tags = values.get("tags")

    return Note(
        path=path,
        title=title,
        type=_as_text(values.get("type")),
        created=created,
        tags=tuple(tags) if isinstance(tags, list) else (),
        body=body,
    )


def _as_text(value: str | list[str] | None) -> str:
    """frontmatter 的一个值 → 字符串。列表取第一项。"""
    if value is None:
        return ""
    if isinstance(value, list):
        return value[0] if value else ""
    return value


def _parse_iso(text: str) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def heading_of(text: str, *, fallback: str) -> str:
    """取正文里第一个一级标题。没有就用 ``fallback``。

    整理时可能只给出一段正文而忘了标题，而标题**必须**有——
    Obsidian 的图谱用文件名当节点，索引页用标题当条目，
    空标题会同时毁掉这两处。
    """
    match = _HEADING_RE.search(text)
    return match.group(1).strip() if match else fallback

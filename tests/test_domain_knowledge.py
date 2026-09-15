"""``domain/knowledge.py`` 的规矩：一篇笔记长什么样。

这一层全是纯函数，所以测试直接断言字符串就够了，不该需要临时文件。
真正要落盘的那些验证在 ``test_sim_vault.py`` 里。

三件事分开测：文件名（``slugify``）、frontmatter（渲染与解析）、
双链（``parse_wikilinks``）。它们出问题的后果不一样——
文件名错了是打不开，frontmatter 错了是 Obsidian 认不出属性，
双链错了是校验报一堆不存在的坏链。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from alterego.domain.knowledge import (
    MAX_NAME_LENGTH,
    Note,
    NoteDraft,
    NoteError,
    frontmatter_lines,
    heading_of,
    normalize_link_target,
    note_from_text,
    parse_frontmatter,
    parse_wikilinks,
    render_frontmatter,
    render_note,
    slugify,
)


T0 = datetime(2026, 9, 16, 21, 40)


# ────────────────────────────────────────────────────────────
# 文件名
# ────────────────────────────────────────────────────────────


class TestSlugify:
    """名字最终会落到文件系统上，而文件系统比模型记仇得多。"""

    def test_it_keeps_chinese(self) -> None:
        assert slugify("怕麻烦别人") == "怕麻烦别人"

    def test_it_keeps_spaces(self) -> None:
        """空格换成下划线只会让人在文件管理器里认不出来。"""
        assert slugify("跟阿哲说 不用不用") == "跟阿哲说 不用不用"

    def test_it_collapses_whitespace(self) -> None:
        assert slugify("今天   有点\t闷") == "今天 有点 闷"

    @pytest.mark.parametrize("bad", ["<", ">", ":", '"', "/", "\\", "|", "?", "*"])
    def test_it_replaces_characters_windows_forbids(self, bad: str) -> None:
        assert bad not in slugify(f"前后{bad}中间")

    def test_it_flattens_a_path(self) -> None:
        """模型把「目录/名字」整个当标题给过来时，不能凭空多出一层目录。"""
        assert slugify("20-想法/怕麻烦别人") == "20-想法 怕麻烦别人"

    def test_it_strips_trailing_dots(self) -> None:
        """Windows 会把结尾的点**静默**去掉，于是「同名」却打不开。"""
        assert slugify("等等再说...") == "等等再说"

    def test_it_strips_leading_and_trailing_space(self) -> None:
        assert slugify("  阿哲  ") == "阿哲"

    def test_it_falls_back_when_nothing_is_left(self) -> None:
        assert slugify("///") == "未命名"

    def test_it_falls_back_on_an_empty_name(self) -> None:
        assert slugify("") == "未命名"

    def test_it_uses_a_custom_fallback(self) -> None:
        assert slugify("///", fallback="无题") == "无题"

    def test_it_truncates_long_names(self) -> None:
        assert len(slugify("长" * 200)) == MAX_NAME_LENGTH

    def test_it_does_not_leave_a_trailing_dot_after_truncating(self) -> None:
        """截断可能正好切在点上——切完还得再收拾一次。"""
        assert not slugify("字" * (MAX_NAME_LENGTH - 1) + "...").endswith(".")

    @pytest.mark.parametrize("reserved", ["CON", "PRN", "AUX", "NUL", "COM1", "LPT9"])
    def test_it_saves_windows_device_names(self, reserved: str) -> None:
        assert slugify(reserved) == f"{reserved}_"

    def test_it_is_case_insensitive_about_device_names(self) -> None:
        assert slugify("con") == "con_"


# ────────────────────────────────────────────────────────────
# 双链
# ────────────────────────────────────────────────────────────


class TestNormalizeLinkTarget:
    """``[[a|b]]`` 和 ``[[a]]`` 指向同一篇笔记，归一后才能比较。"""

    def test_it_keeps_a_plain_name(self) -> None:
        assert normalize_link_target("阿哲") == "阿哲"

    def test_it_keeps_the_folder_part(self) -> None:
        """带路径的链接更精确，去掉反而会制造歧义。"""
        assert normalize_link_target("50-见过的人/阿哲") == "50-见过的人/阿哲"

    def test_it_drops_the_alias(self) -> None:
        assert normalize_link_target("阿哲|小哲") == "阿哲"

    def test_it_drops_the_section(self) -> None:
        assert normalize_link_target("2026-09-16#下午") == "2026-09-16"

    def test_it_drops_the_extension(self) -> None:
        assert normalize_link_target("阿哲.md") == "阿哲"

    def test_it_trims_spaces(self) -> None:
        assert normalize_link_target("  阿哲  ") == "阿哲"


class TestParseWikilinks:
    def test_it_finds_a_link(self) -> None:
        assert parse_wikilinks("今天见了 [[阿哲]]。") == ("阿哲",)

    def test_it_keeps_the_order(self) -> None:
        assert parse_wikilinks("[[甲]] 然后 [[乙]]") == ("甲", "乙")

    def test_it_deduplicates(self) -> None:
        assert parse_wikilinks("[[甲]] 又见 [[甲]]") == ("甲",)

    def test_it_normalizes_while_parsing(self) -> None:
        assert parse_wikilinks("[[阿哲|小哲]]") == ("阿哲",)

    def test_it_skips_code_fences(self) -> None:
        """提示词模板里常写 ``[[链接]]`` 当例子。当真了会报一堆假坏链。"""
        text = "正文 [[真的]]\n\n```\n[[假的]]\n```\n"
        assert parse_wikilinks(text) == ("真的",)

    def test_it_skips_inline_code(self) -> None:
        assert parse_wikilinks("写法是 `[[假的]]`，所以我链 [[真的]]") == ("真的",)

    def test_it_ignores_an_empty_link(self) -> None:
        assert parse_wikilinks("[[|别名]]") == ()

    def test_it_returns_nothing_when_there_is_no_link(self) -> None:
        assert parse_wikilinks("今天有点闷。") == ()


# ────────────────────────────────────────────────────────────
# frontmatter
# ────────────────────────────────────────────────────────────


class TestFrontmatterLines:
    def test_it_keeps_the_given_order(self) -> None:
        """顺序固定，diff 才干净——字段重排会让人以为内容被改过。"""
        lines = frontmatter_lines({"title": "阿哲", "type": "想法"})
        assert lines == ["title: 阿哲", "type: 想法"]

    def test_an_empty_list_is_rendered_as_brackets(self) -> None:
        """``tags:`` 会被读成空值，和「没有标签」不是一回事。"""
        assert frontmatter_lines({"tags": []}) == ["tags: []"]

    def test_a_list_becomes_multiple_lines(self) -> None:
        """多行列表在 Obsidian 的属性面板里显示成可点的小标签。"""
        assert frontmatter_lines({"tags": ["想法", "心情"]}) == [
            "tags:",
            "  - 想法",
            "  - 心情",
        ]

    def test_it_quotes_an_empty_string(self) -> None:
        assert frontmatter_lines({"type": ""}) == ['type: ""']

    @pytest.mark.parametrize("word", ["true", "false", "no", "null", "~"])
    def test_it_quotes_yaml_ambiguous_words(self, word: str) -> None:
        """不加引号的话，``title: no`` 在某些解析器里会变成布尔值。"""
        assert frontmatter_lines({"title": word}) == [f'title: "{word}"']

    def test_it_quotes_number_like_values(self) -> None:
        assert frontmatter_lines({"title": "2026"}) == ['title: "2026"']

    def test_it_quotes_values_containing_a_colon(self) -> None:
        assert frontmatter_lines({"title": "他说: 好"}) == ['title: "他说: 好"']

    def test_it_does_not_quote_a_full_width_colon(self) -> None:
        """YAML 只认半角冒号。全角的是普通字符，中文标题里到处都是。"""
        assert frontmatter_lines({"title": "他说：好"}) == ["title: 他说：好"]

    @pytest.mark.parametrize("leader", ["-", "?", "*", "@", "#"])
    def test_it_quotes_leading_specials(self, leader: str) -> None:
        assert frontmatter_lines({"title": f"{leader}开头"})[0].startswith('title: "')

    def test_it_escapes_quotes(self) -> None:
        """首字符是引号时整个值都要括起来——不然那对引号会配错。"""
        assert frontmatter_lines({"title": '"好"'}) == ['title: "\\"好\\""']

    def test_it_leaves_a_quote_in_the_middle_alone(self) -> None:
        assert frontmatter_lines({"title": '他说"好"'}) == ['title: 他说"好"']


class TestRenderFrontmatter:
    def test_it_wraps_in_delimiters(self) -> None:
        assert render_frontmatter({"title": "阿哲"}) == "---\ntitle: 阿哲\n---\n"

    def test_it_refuses_to_render_nothing(self) -> None:
        """每篇笔记都要能被检索到——一个空的 frontmatter 等于没有元数据。"""
        with pytest.raises(NoteError, match="不能为空"):
            render_frontmatter({})


class TestParseFrontmatter:
    def test_it_round_trips(self) -> None:
        values = {"title": "怕麻烦别人", "type": "想法", "tags": ["想法", "心情"]}
        parsed, body = parse_frontmatter(render_frontmatter(values) + "\n正文\n")
        assert parsed == values
        assert body == "正文"

    def test_it_returns_the_text_untouched_when_there_is_none(self) -> None:
        """不报错是有意的：这儿只管「有没有」，「该不该有」是校验那关的事。"""
        assert parse_frontmatter("# 随便写点什么\n") == ({}, "# 随便写点什么\n")

    def test_it_ignores_an_unterminated_block(self) -> None:
        text = "---\ntitle: 阿哲\n\n正文\n"
        assert parse_frontmatter(text) == ({}, text)

    def test_it_reads_a_list(self) -> None:
        parsed, _ = parse_frontmatter("---\ntags:\n  - 甲\n  - 乙\n---\n")
        assert parsed["tags"] == ["甲", "乙"]

    def test_it_distinguishes_an_empty_value_from_an_empty_list(self) -> None:
        with_items, _ = parse_frontmatter("---\ntags:\n  - 甲\n---\n")
        without_items, _ = parse_frontmatter("---\ntags:\n其他的: 有\n---\n")
        assert with_items["tags"] == ["甲"]
        assert without_items["tags"] == ""

    def test_it_skips_comment_lines(self) -> None:
        parsed, _ = parse_frontmatter("---\n# 注释\ntitle: 阿哲\n---\n")
        assert parsed == {"title": "阿哲"}

    def test_it_unquotes(self) -> None:
        parsed, _ = parse_frontmatter('---\ntitle: "他说\\"好\\""\n---\n')
        assert parsed["title"] == '他说"好"'


# ────────────────────────────────────────────────────────────
# 一篇笔记
# ────────────────────────────────────────────────────────────


class TestNote:
    @pytest.fixture
    def note(self) -> Note:
        return Note(
            path="20-想法/怕麻烦别人.md",
            title="怕麻烦别人",
            type="想法",
            created=T0,
            tags=("心情",),
            body="今天 [[阿哲]] 问我怎么了。",
        )

    def test_stem_drops_the_extension(self, note: Note) -> None:
        assert note.stem == "怕麻烦别人"

    def test_folder_is_the_directory(self, note: Note) -> None:
        assert note.folder == "20-想法"

    def test_folder_is_empty_at_the_root(self) -> None:
        note = Note(path="索引.md", title="索引", type="索引", created=T0)
        assert note.folder == ""

    def test_it_exposes_its_links(self, note: Note) -> None:
        assert note.links == ("阿哲",)

    def test_frontmatter_has_a_fixed_order(self, note: Note) -> None:
        assert list(note.frontmatter()) == ["title", "type", "created", "tags"]

    def test_render_starts_with_frontmatter(self, note: Note) -> None:
        assert note.render().startswith("---\ntitle: 怕麻烦别人\n")

    def test_render_ends_with_a_newline(self, note: Note) -> None:
        assert note.render().endswith("\n")

    def test_render_leaves_a_blank_line_after_the_frontmatter(self, note: Note) -> None:
        assert "---\n\n今天" in note.render()

    def test_render_copes_with_an_empty_body(self) -> None:
        note = Note(path="20-想法/空.md", title="空", type="想法", created=T0)
        assert note.render() == (
            '---\ntitle: 空\ntype: 想法\ncreated: "2026-09-16T21:40:00"\ntags: []\n---\n'
        )


class TestNoteDraft:
    def test_at_uses_the_title_as_the_filename(self) -> None:
        note = NoteDraft(title="怕麻烦别人", type="想法", created=T0, body="x").at("20-想法")
        assert note.path == "20-想法/怕麻烦别人.md"

    def test_at_accepts_an_explicit_filename(self) -> None:
        note = NoteDraft(title="怕麻烦别人", type="想法", created=T0, body="x").at(
            "20-想法", filename="自作多情"
        )
        assert note.path == "20-想法/自作多情.md"

    def test_at_supports_the_vault_root(self) -> None:
        note = NoteDraft(title="索引", type="索引", created=T0, body="x").at("")
        assert note.path == "索引.md"


class TestRenderNote:
    def test_it_renders_in_one_step(self) -> None:
        text = render_note(path="20-想法/x.md", title="x", type="想法", created=T0, body="正文")
        assert "title: x" in text
        assert text.endswith("\n")


# ────────────────────────────────────────────────────────────
# 读回磁盘上的笔记
# ────────────────────────────────────────────────────────────


class TestNoteFromText:
    def test_it_reads_a_full_note(self) -> None:
        text = Note(
            path="20-想法/x.md", title="怕麻烦别人", type="想法", created=T0, tags=("心情",)
        ).render()
        note = note_from_text("20-想法/x.md", text, fallback_created=T0)
        assert note.title == "怕麻烦别人"
        assert note.type == "想法"
        assert note.tags == ("心情",)

    def test_it_round_trips_a_rendered_note(self) -> None:
        """扫描是只读操作，读回来的应该和写出去的是同一篇。"""
        original = Note(
            path="20-想法/x.md",
            title="怕麻烦别人",
            type="想法",
            created=T0,
            tags=("心情",),
            body="正文。",
        )
        note = note_from_text(original.path, original.render(), fallback_created=T0)
        assert note == original

    def test_it_takes_the_title_from_the_body_when_there_is_no_frontmatter(self) -> None:
        note = note_from_text("99-收集箱/abc.md", "# 今天有点闷\n\n正文\n", fallback_created=T0)
        assert note.title == "今天有点闷"

    def test_it_falls_back_to_the_filename(self) -> None:
        """一篇既没有 title 也没有一级标题的笔记，总得有个名字。"""
        note = note_from_text("99-收集箱/abc-1234.md", "正文\n", fallback_created=T0)
        assert note.title == "abc-1234"

    def test_the_fallback_title_has_no_extension(self) -> None:
        """带 ``.md`` 的标题会让索引页里出现「abc-1234 · abc-1234.md」。"""
        note = note_from_text("99-收集箱/abc.md", "正文\n", fallback_created=T0)
        assert not note.title.endswith(".md")

    def test_it_uses_the_fallback_created(self) -> None:
        note = note_from_text("99-收集箱/abc.md", "正文\n", fallback_created=T0)
        assert note.created == T0

    def test_it_falls_back_on_an_unparsable_created(self) -> None:
        text = "---\ntitle: x\ncreated: 昨天\n---\n"
        note = note_from_text("99-收集箱/abc.md", text, fallback_created=T0)
        assert note.created == T0

    def test_it_prefers_the_created_in_the_frontmatter(self) -> None:
        text = "---\ntitle: x\ncreated: 2026-01-01T08:00:00\n---\n"
        note = note_from_text("99-收集箱/abc.md", text, fallback_created=T0)
        assert note.created == datetime(2026, 1, 1, 8, 0)

    def test_it_treats_a_single_tag_as_no_tags(self) -> None:
        """``tags: 心情`` 是错的写法，但不值得为此让扫描停下。"""
        note = note_from_text(
            "99-收集箱/abc.md", "---\ntitle: x\ntags: 心情\n---\n", fallback_created=T0
        )
        assert note.tags == ()


class TestHeadingOf:
    def test_it_finds_the_first_h1(self) -> None:
        assert heading_of("# 甲\n\n## 乙\n", fallback="x") == "甲"

    def test_it_falls_back_when_there_is_none(self) -> None:
        assert heading_of("正文\n", fallback="x") == "x"

    def test_it_ignores_lower_levels(self) -> None:
        assert heading_of("## 乙\n", fallback="x") == "x"

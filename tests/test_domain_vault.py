"""``domain/vault.py`` 的规矩：库长什么样，以及什么叫「条理清晰」。

四条不变量在这里被逐条检验：
文件名合法、目录在布局里、标题非空、没有坏链、内容笔记都被索引页提到。

索引生成器也在这里测，而且顺序要紧：``validate`` 会检查「每篇内容笔记
都被索引页提到」，而索引页正是 ``build_folder_index`` 生成的——
**检查器与生成器放在同一个文件里测**，才不会出现「生成器违反了它自己的
不变量」这种谁都没发现的情况（上一版就出过一次：索引页链了一个
根本不存在的「索引」）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

import pytest

from alterego.domain.knowledge import Note, parse_wikilinks
from alterego.domain.vault import (
    CONTENT_FOLDERS,
    FOLDER_INBOX,
    FOLDER_INDEX,
    FOLDER_MEMORIES,
    FOLDER_PEOPLE,
    FOLDER_SCHEDULE,
    FOLDER_SOURCES,
    FOLDER_THOUGHTS,
    FOLDERS,
    STATE_FILENAME,
    TYPES,
    OrganizeDecision,
    build_folder_index,
    build_root_index,
    parse_organize_plan,
    resolve_path,
    validate,
)


T0 = datetime(2026, 9, 16, 21, 40)
T1 = T0 + timedelta(days=1)
T2 = T0 + timedelta(days=2)


def note(
    folder: str,
    stem: str,
    *,
    title: str | None = None,
    created: datetime = T0,
    body: str = "",
    kind: str = "",
) -> Note:
    """造一篇内容笔记。默认 title 和 stem 同名，省掉一半的重复。"""
    return Note(
        path=f"{folder}/{stem}.md",
        title=title if title is not None else stem,
        type=kind or TYPES.get(folder, folder),
        created=created,
        body=body,
    )


def index_page(name: str = "想法", *, body: str, folder: str = FOLDER_INDEX) -> Note:
    return Note(
        path=f"{folder}/{name}.md",
        title=name,
        type=TYPES[FOLDER_INDEX],
        created=T0,
        tags=("索引",),
        body=body,
    )


def full_index(
    contents: Sequence[Note], *, name: str = "阿哲的知识库", created: datetime = T0
) -> list[Note]:
    """生成一整套索引页：库首页加上每个内容目录各一页。

    整套是**必须的**——库首页会链到每一个内容目录的索引页，缺一个就是
    一条坏链。``build()`` 正是这么做的，所以这里也得这么造，否则测出来的是
    一个真实产品里不会存在的库。
    """
    root = build_root_index(name=name, created=created, notes=contents, total=len(contents))
    pages = [
        build_folder_index(folder, created=created, notes=contents, parent=root.stem)
        for folder in CONTENT_FOLDERS
    ]
    return [root, *pages]


# ────────────────────────────────────────────────────────────
# 布局
# ────────────────────────────────────────────────────────────


class TestLayout:
    def test_the_index_is_not_a_content_folder(self) -> None:
        """索引页由代码生成，不让角色往里放东西。"""
        assert FOLDER_INDEX not in CONTENT_FOLDERS
        assert FOLDER_INDEX in FOLDERS

    def test_the_inbox_is_not_a_content_folder(self) -> None:
        """收集箱是暂存区，不是终点。"""
        assert FOLDER_INBOX in FOLDERS
        assert FOLDER_INBOX not in CONTENT_FOLDERS

    def test_every_folder_has_a_type_name(self) -> None:
        assert set(TYPES) == set(FOLDERS)

    def test_the_state_file_is_dot_prefixed(self) -> None:
        """点开头的文件 Obsidian 会藏起来，不会污染图谱。"""
        assert STATE_FILENAME.startswith(".")

    def test_the_inbox_sorts_last(self) -> None:
        """目录名前置数字就是排序用的，收集箱要排在最后。"""
        assert FOLDERS[-1] == FOLDER_INBOX


# ────────────────────────────────────────────────────────────
# 校验
# ────────────────────────────────────────────────────────────


def kinds(issues: tuple[object, ...]) -> list[str]:
    return [issue.kind for issue in issues]  # type: ignore[attr-defined]


class TestValidate:
    def test_a_clean_vault_has_no_issues(self) -> None:
        notes = [
            note(FOLDER_THOUGHTS, "怕麻烦别人"),
            note(FOLDER_SOURCES, "一篇讲睡眠的文章"),
            index_page("想法", body="- [[怕麻烦别人]]"),
            index_page("读到的", body="- [[一篇讲睡眠的文章]]"),
        ]
        assert validate(notes) == ()

    def test_end_to_end_with_the_generated_index(self) -> None:
        """校验器和索引生成器必须互相认可——这是上一版出过问题的地方。"""
        contents = [
            note(FOLDER_THOUGHTS, "怕麻烦别人"),
            note(FOLDER_THOUGHTS, "今天很闷"),
            note(FOLDER_PEOPLE, "阿哲"),
        ]
        assert validate([*contents, *full_index(contents)]) == ()

    def test_it_flags_a_missing_folder_index(self) -> None:
        """手删掉一个目录的索引页，库首页立刻多出一条坏链。

        这不是缺陷，是这套检查的意义所在——它把「索引不完整」变成了
        一句能看懂的话。
        """
        contents = [note(FOLDER_THOUGHTS, "怕麻烦别人")]
        pages = full_index(contents)
        assert validate([*contents, *pages[1:]]) != ()

    def test_the_root_index_does_not_link_to_a_nonexistent_index_page(self) -> None:
        """索引页里写死一个「索引」就是一条指向不存在笔记的坏链。"""
        root = build_root_index(name="阿哲的知识库", created=T0, notes=[], total=0)
        assert "索引" not in parse_wikilinks(root.body)

    def test_it_flags_a_non_markdown_file(self) -> None:
        bad = Note(path="20-想法/x.txt", title="x", type="想法", created=T0)
        assert "bad_path" in kinds(validate([bad]))

    def test_it_flags_a_file_outside_the_layout(self) -> None:
        bad = Note(path="随便一个目录/x.md", title="x", type="想法", created=T0)
        assert "bad_path" in kinds(validate([bad]))

    def test_it_flags_a_file_at_the_vault_root(self) -> None:
        bad = Note(path="x.md", title="x", type="想法", created=T0)
        assert "bad_path" in kinds(validate([bad]))

    def test_it_flags_a_missing_title(self) -> None:
        bad = note(FOLDER_THOUGHTS, "x", title="   ")
        assert "missing_frontmatter" in kinds(validate([bad]))

    def test_it_flags_a_broken_link(self) -> None:
        bad = index_page("想法", body="- [[根本没写过的东西]]")
        assert "broken_link" in kinds(validate([bad]))

    def test_a_link_with_a_folder_prefix_still_resolves(self) -> None:
        notes = [
            note(FOLDER_THOUGHTS, "怕麻烦别人"),
            index_page("想法", body="- [[20-想法/怕麻烦别人]]"),
        ]
        assert validate(notes) == ()

    def test_it_flags_duplicate_stems(self) -> None:
        """``[[同名]]`` 有歧义，Obsidian 会挑一个，而挑哪个不由我们定。"""
        notes = [
            note(FOLDER_THOUGHTS, "阿哲"),
            note(FOLDER_PEOPLE, "阿哲"),
            index_page("想法", body="- [[阿哲]]"),
            index_page("见过的人", body="- [[阿哲]]"),
        ]
        assert "duplicate_stem" in kinds(validate(notes))

    def test_it_flags_an_orphan(self) -> None:
        """内容笔记没被索引页提到，用户在知识库里就找不到它。"""
        notes = [
            note(FOLDER_THOUGHTS, "怕麻烦别人"),
            index_page("想法", body="- [[别的什么]]"),
        ]
        assert "orphan" in kinds(validate(notes))

    def test_the_inbox_is_exempt_from_the_orphan_check(self) -> None:
        """还没归位的东西当然不在索引里。"""
        assert validate([note(FOLDER_INBOX, "abc-1234")]) == ()

    def test_it_reports_every_problem_not_just_the_first(self) -> None:
        """一次报全，是因为修的时候通常要一起修。"""
        bad = [
            Note(path="20-想法/x.txt", title="", type="想法", created=T0),
            Note(path="随便/x.md", title="x", type="想法", created=T0),
        ]
        assert len(validate(bad)) >= 3

    def test_an_issue_reads_like_a_sentence(self) -> None:
        issues = validate([note(FOLDER_THOUGHTS, "x", title="")])
        assert str(issues[0]).startswith("20-想法/x.md：")


# ────────────────────────────────────────────────────────────
# 索引页
# ────────────────────────────────────────────────────────────


class TestBuildFolderIndex:
    def test_it_lands_in_the_index_folder(self) -> None:
        page = build_folder_index(FOLDER_THOUGHTS, created=T0, notes=[])
        assert page.path == f"{FOLDER_INDEX}/想法.md"

    def test_it_uses_the_type_name_as_title(self) -> None:
        assert build_folder_index(FOLDER_SOURCES, created=T0, notes=[]).title == "读到的"

    def test_an_empty_folder_says_so(self) -> None:
        """空文件在 Obsidian 里看起来像坏了；一句话说明这是正常状态。"""
        assert "还是空的" in build_folder_index(FOLDER_THOUGHTS, created=T0, notes=[]).body

    def test_it_only_lists_its_own_folder(self) -> None:
        notes = [note(FOLDER_THOUGHTS, "甲"), note(FOLDER_PEOPLE, "乙")]
        body = build_folder_index(FOLDER_THOUGHTS, created=T0, notes=notes).body
        assert "[[甲]]" in body
        assert "[[乙]]" not in body

    def test_it_lists_newest_first(self) -> None:
        notes = [
            note(FOLDER_THOUGHTS, "旧的", created=T0),
            note(FOLDER_THOUGHTS, "新的", created=T2),
        ]
        body = build_folder_index(FOLDER_THOUGHTS, created=T2, notes=notes).body
        assert body.index("[[新的]]") < body.index("[[旧的]]")

    def test_it_includes_the_title_in_the_entry(self) -> None:
        notes = [note(FOLDER_THOUGHTS, "怕麻烦别人", title="跟阿哲说「不用」之后")]
        body = build_folder_index(FOLDER_THOUGHTS, created=T0, notes=notes).body
        assert "跟阿哲说「不用」之后" in body

    def test_it_omits_the_related_section_without_a_parent(self) -> None:
        body = build_folder_index(FOLDER_THOUGHTS, created=T0, notes=[]).body
        assert "## 相关" not in body

    def test_it_links_back_to_the_parent(self) -> None:
        body = build_folder_index(FOLDER_THOUGHTS, created=T0, notes=[], parent="阿哲的知识库").body
        assert "## 相关" in body
        assert "[[阿哲的知识库]]" in body

    def test_the_parent_link_resolves(self) -> None:
        pages = full_index([])
        assert "## 相关" in pages[1].body
        assert validate(pages) == ()

    def test_it_carries_an_intro(self) -> None:
        body = build_folder_index(FOLDER_THOUGHTS, created=T0, notes=[], intro="我想的东西。").body
        assert "我想的东西。" in body


class TestBuildRootIndex:
    def test_it_lands_in_the_index_folder(self) -> None:
        page = build_root_index(name="阿哲的知识库", created=T0, notes=[], total=0)
        assert page.path == f"{FOLDER_INDEX}/阿哲的知识库.md"

    def test_it_links_to_every_content_folder(self) -> None:
        body = build_root_index(name="库", created=T0, notes=[], total=0).body
        for folder in CONTENT_FOLDERS:
            assert f"[[{TYPES[folder]}]]" in body

    def test_it_does_not_link_to_the_index_or_the_inbox(self) -> None:
        body = build_root_index(name="库", created=T0, notes=[], total=0).body
        assert f"[[{TYPES[FOLDER_INDEX]}]]" not in body
        assert f"[[{TYPES[FOLDER_INBOX]}]]" not in body

    def test_it_prints_the_total(self) -> None:
        assert "共 42 篇" in build_root_index(name="库", created=T0, notes=[], total=42).body


# ────────────────────────────────────────────────────────────
# 角色交回来的整理计划
# ────────────────────────────────────────────────────────────


def payload(*items: str) -> str:
    return "好的，我整理了一下：\n\n[\n  " + ",\n  ".join(items) + "\n]\n\n希望这样放得对。"


class TestParseOrganizePlan:
    def test_it_reads_a_decision(self) -> None:
        text = payload('{"inbox": "abc-1234", "folder": "20-想法", "title": "怕麻烦别人"}')
        decision = parse_organize_plan(text, inbox_stems=["abc-1234"]).decisions[0]
        assert decision.folder == FOLDER_THOUGHTS
        assert decision.title == "怕麻烦别人"

    def test_it_ignores_the_prose_around_the_json(self) -> None:
        """模型总会先说一句「好的」。为此去逼它只输出 JSON 是浪费。"""
        plan = parse_organize_plan(payload('{"inbox": "a", "folder": "20-想法", "title": "x"}'))
        assert plan.ok

    def test_it_reports_a_missing_json_array(self) -> None:
        plan = parse_organize_plan("我觉得都挺好的。", inbox_stems=["a"])
        assert plan.rejected[0][0] == "整批"
        assert not plan.ok

    def test_it_reports_invalid_json(self) -> None:
        plan = parse_organize_plan("[{坏了}]", inbox_stems=["a"])
        assert "JSON" in plan.rejected[0][1]

    def test_it_survives_a_stray_bracket_in_the_prose(self) -> None:
        """它总爱说「我把这条放进 [20-想法] 了」。取最后一个 ``]`` 才是对的。

        反过来说，取**第一个** ``]`` 就会把一个半截的 JSON 交上去——
        而那个错误看起来像模型坏了，其实是我们切错了。
        """
        text = (
            "我把它放进 [20-想法] 了。\n"
            '[{"inbox": "a", "folder": "20-想法", "title": "x"}]\n'
            "就这些。"
        )
        assert parse_organize_plan(text, inbox_stems=["a"]).ok

    def test_it_rejects_an_unknown_folder(self) -> None:
        """目录名不在布局里时放过，后面就是拿空字符串去拼路径。"""
        text = payload('{"inbox": "a", "folder": "档案", "title": "x"}')
        plan = parse_organize_plan(text, inbox_stems=["a"])
        assert plan.decisions == ()
        assert "档案" in plan.rejected[0][1]

    def test_it_accepts_a_folder_with_a_trailing_slash(self) -> None:
        text = payload('{"inbox": "a", "folder": "20-想法/", "title": "x"}')
        assert parse_organize_plan(text, inbox_stems=["a"]).ok

    def test_it_rejects_a_missing_title(self) -> None:
        text = payload('{"inbox": "a", "folder": "20-想法", "title": "  "}')
        assert "标题" in parse_organize_plan(text, inbox_stems=["a"]).rejected[0][1]

    def test_it_rejects_a_missing_inbox(self) -> None:
        text = payload('{"folder": "20-想法", "title": "x"}')
        assert "收集箱" in parse_organize_plan(text, inbox_stems=["a"]).rejected[0][1]

    def test_it_rejects_an_unknown_inbox_file(self) -> None:
        text = payload('{"inbox": "根本没有", "folder": "20-想法", "title": "x"}')
        plan = parse_organize_plan(text, inbox_stems=["a"])
        assert "根本没有" in plan.rejected[0][1]

    def test_it_skips_the_inbox_check_when_no_stems_are_given(self) -> None:
        """没给清单就无从查，那就只做结构校验。"""
        text = payload('{"inbox": "随便", "folder": "20-想法", "title": "x"}')
        assert parse_organize_plan(text).ok

    def test_it_accepts_an_inbox_path_and_a_plain_stem(self) -> None:
        text = payload('{"inbox": "99-收集箱/abc.md", "folder": "20-想法", "title": "x"}')
        assert parse_organize_plan(text, inbox_stems=["abc"]).ok

    def test_it_keeps_the_good_ones_when_one_is_bad(self) -> None:
        """逐条跳过，不是整批丢弃——一条笔记标签起得不好不值得全停。"""
        text = payload(
            '{"inbox": "a", "folder": "档案", "title": "x"}',
            '{"inbox": "b", "folder": "20-想法", "title": "好的那条"}',
        )
        plan = parse_organize_plan(text, inbox_stems=["a", "b"])
        assert [decision.title for decision in plan.decisions] == ["好的那条"]
        assert len(plan.rejected) == 1

    def test_it_numbers_the_rejections(self) -> None:
        """「第 2 条」比「有 1 条被跳过」有用得多。"""
        text = payload(
            '{"inbox": "a", "folder": "20-想法", "title": "x"}',
            '{"inbox": "b", "folder": "档案", "title": "y"}',
        )
        assert parse_organize_plan(text, inbox_stems=["a", "b"]).rejected[0][0] == "第 2 条"

    def test_it_rejects_a_non_object_entry(self) -> None:
        assert "对象" in parse_organize_plan('["我是一个字符串"]').rejected[0][1]


class TestParseOrganizePlanTagsAndLinks:
    def test_it_strips_the_hash_from_tags(self) -> None:
        text = payload('{"inbox": "a", "folder": "20-想法", "title": "x", "tags": ["#心情"]}')
        assert parse_organize_plan(text).decisions[0].tags == ("心情",)

    def test_it_deduplicates_tags(self) -> None:
        text = payload('{"inbox": "a", "folder": "20-想法", "title": "x", "tags": ["甲", "甲"]}')
        assert parse_organize_plan(text).decisions[0].tags == ("甲",)

    def test_tags_that_are_not_a_list_become_empty(self) -> None:
        text = payload('{"inbox": "a", "folder": "20-想法", "title": "x", "tags": "心情"}')
        assert parse_organize_plan(text).decisions[0].tags == ()

    def test_it_drops_the_extension_from_links(self) -> None:
        text = payload('{"inbox": "a", "folder": "20-想法", "title": "x", "links": ["甲.md"]}')
        assert parse_organize_plan(text).decisions[0].links == ("甲",)

    def test_it_keeps_a_link_to_a_note_that_does_not_exist_yet(self) -> None:
        """链接是往前指的——它刚想到一个名字，那篇笔记还没写呢。"""
        text = payload('{"inbox": "a", "folder": "20-想法", "title": "x", "links": ["还没写"]}')
        plan = parse_organize_plan(text, inbox_stems=["a"])
        assert plan.ok
        assert plan.decisions[0].links == ("还没写",)

    def test_it_normalizes_the_inbox_stem(self) -> None:
        decision = OrganizeDecision(inbox="99-收集箱/abc.md", folder=FOLDER_THOUGHTS, title="x")
        assert decision.inbox_stem == "abc"


# ────────────────────────────────────────────────────────────
# 定址
# ────────────────────────────────────────────────────────────


def decision(**overrides: object) -> OrganizeDecision:
    fields: dict[str, object] = {
        "inbox": "abc-1234",
        "folder": FOLDER_THOUGHTS,
        "title": "怕麻烦别人",
    }
    fields.update(overrides)
    return OrganizeDecision(**fields)  # type: ignore[arg-type]


class TestResolvePath:
    def test_it_builds_a_path(self) -> None:
        assert resolve_path(decision(), created=T0).path == "20-想法/怕麻烦别人.md"

    def test_it_fills_in_the_type_from_the_folder(self) -> None:
        assert resolve_path(decision(), created=T0).type == TYPES[FOLDER_THOUGHTS]

    def test_it_renames_instead_of_overwriting(self) -> None:
        """覆盖会**悄悄毁掉**另一篇笔记，而用户在 Obsidian 里看不见这件事。"""
        note = resolve_path(decision(), taken=["20-想法/怕麻烦别人.md"], created=T0)
        assert note.path == "20-想法/怕麻烦别人-2.md"

    def test_it_keeps_counting(self) -> None:
        taken = ["20-想法/怕麻烦别人.md", "20-想法/怕麻烦别人-2.md"]
        assert resolve_path(decision(), taken=taken, created=T0).path.endswith("-3.md")

    def test_a_collision_does_not_change_the_title(self) -> None:
        note = resolve_path(decision(), taken=["20-想法/怕麻烦别人.md"], created=T0)
        assert note.title == "怕麻烦别人"

    def test_it_falls_back_to_the_original_body(self) -> None:
        note = resolve_path(decision(), created=T0, body="原来的正文")
        assert "原来的正文" in note.body

    def test_its_own_body_wins(self) -> None:
        note = resolve_path(decision(body="改写过的"), created=T0, body="原来的正文")
        assert "改写过的" in note.body
        assert "原来的正文" not in note.body

    def test_it_prepends_a_heading(self) -> None:
        note = resolve_path(decision(), created=T0, body="正文")
        assert note.body.startswith("# 怕麻烦别人")

    def test_it_does_not_double_a_heading(self) -> None:
        note = resolve_path(decision(), created=T0, body="# 我自己起的\n\n正文")
        assert note.body.count("# ") == 1

    def test_it_renders_the_related_section(self) -> None:
        note = resolve_path(decision(links=("阿哲",)), created=T0, body="正文")
        assert "## 相关" in note.body
        assert "- [[阿哲]]" in note.body

    def test_the_related_section_satisfies_the_link_check(self) -> None:
        """``resolve_path`` 造出来的链接必须指向真的会存在的笔记。"""
        existing = note(FOLDER_PEOPLE, "阿哲")
        new = resolve_path(decision(links=("阿哲",)), created=T0, body="正文")
        page = index_page("想法", body=f"- [[{new.stem}]]")
        page2 = index_page("见过的人", body="- [[阿哲]]")
        assert validate([existing, new, page, page2]) == ()

    def test_it_merges_the_index_hint(self) -> None:
        note = resolve_path(decision(), created=T0, body="正文", index_hint=("阿哲",))
        assert "- [[阿哲]]" in note.body

    def test_it_drops_a_link_to_itself(self) -> None:
        """一篇链到自己的笔记在 Obsidian 的图谱里是个自环。"""
        note = resolve_path(decision(), created=T0, body="正文", index_hint=("怕麻烦别人",))
        assert "[[怕麻烦别人]]" not in note.body

    def test_it_copes_with_an_empty_body(self) -> None:
        note = resolve_path(decision(), created=T0)
        assert note.body.startswith("# 怕麻烦别人")

    def test_it_sanitizes_the_filename(self) -> None:
        note = resolve_path(decision(title="20-想法/怕麻烦别人"), created=T0)
        assert note.path == "20-想法/20-想法 怕麻烦别人.md"

    def test_it_uses_the_decision_time_as_created(self) -> None:
        assert resolve_path(decision(), created=T1).created == T1


class TestEveryFolderAcceptsADecision:
    @pytest.mark.parametrize("folder", CONTENT_FOLDERS)
    def test_it_resolves(self, folder: str) -> None:
        assert resolve_path(decision(folder=folder), created=T0).path.startswith(f"{folder}/")

    @pytest.mark.parametrize("folder", [FOLDER_SOURCES, FOLDER_MEMORIES, FOLDER_PEOPLE])
    def test_the_type_is_not_the_folder_name(self, folder: str) -> None:
        """``30-读到的`` 的 type 是「读到的」而不是「30-读到的」。"""
        assert resolve_path(decision(folder=folder), created=T0).type == TYPES[folder]

    def test_a_schedule_page_is_still_a_content_note(self) -> None:
        """日程页由代码直接写进 10-日程——它的归属是确定的，问模型是浪费。

        但它照样是内容笔记：照样要进索引，照样会被 orphan 检查盯上。
        """
        page = note(FOLDER_SCHEDULE, "2026-09-16")
        assert FOLDER_SCHEDULE in CONTENT_FOLDERS
        assert validate([page])[0].kind == "orphan"
        assert validate([page, index_page("日程", body="- [[2026-09-16]]")]) == ()

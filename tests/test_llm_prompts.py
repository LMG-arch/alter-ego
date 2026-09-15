"""``alterego.llm.prompts`` 的测试。

这个模块存在的理由就是**两侧都校验**：模板里有占位符而调用方没给，报错；
调用方给了模板里没有的，也报错。所以绝大多数用例都在往这两条线上撞。

另有一条不那么显眼但要紧：这里的替换**不能**用 `str.format`。
模板里含 JSON 示例，`format` 会把 `{"kind": ...}` 当成占位符——
而它吞掉的偏偏是「要求模型输出什么形状」那一段。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alterego.kernel.errors import PromptError
from alterego.llm.prompts import PROMPTS_DIR, PromptLibrary, PromptTemplate, placeholders, render


# ────────────────────────────────────────────────────────────
# 认出占位符
# ────────────────────────────────────────────────────────────


class TestPlaceholders:
    def test_they_come_out_in_the_order_they_appear(self) -> None:
        """报错时按模板里的自然顺序列缺失项，才跟人眼看到的那段文字对得上。"""
        assert placeholders("先 {a} 后 {b} 再 {c}") == ("a", "b", "c")

    def test_repeats_are_reported_once(self) -> None:
        assert placeholders("{a} {b} {a}") == ("a", "b")

    def test_a_template_without_any_is_fine(self) -> None:
        assert placeholders("什么都不需要") == ()

    @pytest.mark.parametrize(
        "text",
        [
            "{记忆}",  # 中文：没有 re.ASCII 就会被当成占位符
            "{Name}",  # 大写
            "{a.b}",  # 点不是标识符的一部分
            "{1x}",  # 数字开头
            "{}",  # 空
            '{"kind": "episodic"}',  # JSON 示例：`{` 后面紧跟引号
            "{ a }",  # 带空格
        ],
    )
    def test_these_are_not_placeholders(self, text: str) -> None:
        assert placeholders(text) == ()


# ────────────────────────────────────────────────────────────
# 渲染
# ────────────────────────────────────────────────────────────


class TestRender:
    def test_values_go_in(self) -> None:
        assert render("你是 {name}。", {"name": "林晚"}) == "你是 林晚。"

    def test_values_are_stringified(self) -> None:
        assert render("{n} 条", {"n": 3}) == "3 条"

    def test_a_missing_value_is_refused(self) -> None:
        """否则发出去的是带 ``{activities}`` 字面量的提示词，模型会一本正经地围绕它编内容。"""
        with pytest.raises(PromptError, match="缺少占位符取值") as caught:
            render("你是 {name}，今天 {activities}。", {"name": "林晚"})

        assert caught.value.context["missing"] == "activities"

    def test_an_unused_value_is_refused(self) -> None:
        """「改了模板忘了改调用方」会静默地一直传下去，直到有人发现提示词少了一整段。"""
        with pytest.raises(PromptError, match="用不到的取值") as caught:
            render("你是 {name}。", {"name": "林晚", "activities": "…"})

        assert caught.value.context["unused"] == "activities"

    def test_the_source_is_carried_into_the_error(self) -> None:
        with pytest.raises(PromptError) as caught:
            render("{a}", {}, source="memory_consolidate")

        assert caught.value.context["source"] == "memory_consolidate"

    def test_a_json_example_survives(self) -> None:
        """用 ``str.format`` 的话，这段示例会被吃掉一半。"""
        text = render('给出 {"kind": "episodic"} 这样的 JSON。我是 {name}。', {"name": "林晚"})

        assert '{"kind": "episodic"}' in text

    def test_a_backslash_in_a_value_is_not_an_escape(self) -> None:
        """模型输出里带正则或 Windows 路径都是可能的。

        ``re.sub`` 的替换串会把 ``\\1`` 当分组引用、把 ``\\g<0>`` 当整体引用，
        所以实现用的是一个 lambda。
        """
        assert render("{text}", {"text": r"C:\1 与 \g<0>"}) == r"C:\1 与 \g<0>"

    def test_a_brace_in_a_value_does_not_re_render(self) -> None:
        """替换结果里再出现 ``{x}``，不该被当成第二轮占位符。"""
        assert render("{a}", {"a": "{b}"}) == "{b}"


# ────────────────────────────────────────────────────────────
# 模板对象
# ────────────────────────────────────────────────────────────


class TestPromptTemplate:
    def test_it_knows_what_it_needs(self) -> None:
        template = PromptTemplate(name="t", text="你是 {name}。")
        assert template.placeholders == ("name",)

    def test_a_hand_made_template_has_no_path(self) -> None:
        assert PromptTemplate(name="t", text="x").path is None

    def test_rendering_names_itself_in_the_error(self) -> None:
        with pytest.raises(PromptError) as caught:
            PromptTemplate(name="memory_consolidate", text="{a}").render()

        assert caught.value.context["source"] == "memory_consolidate"


# ────────────────────────────────────────────────────────────
# 模板库
# ────────────────────────────────────────────────────────────


def _write(directory: Path, name: str, text: str) -> None:
    (directory / f"{name}.md").write_text(text, encoding="utf-8")


class TestPromptLibrary:
    def test_names_are_sorted(self, tmp_path: Path) -> None:
        _write(tmp_path, "b", "乙")
        _write(tmp_path, "a", "甲")

        assert PromptLibrary(tmp_path).names() == ("a", "b")

    def test_it_only_picks_up_legal_template_names(self, tmp_path: Path) -> None:
        """文件名就是模板名，所以它得像个名字——否则 ``get()`` 永远取不到它。"""
        _write(tmp_path, "good_name", "甲")
        _write(tmp_path, "UPPER", "乙")
        _write(tmp_path, "带中文", "丙")
        (tmp_path / "notes.txt").write_text("不是模板", encoding="utf-8")
        (tmp_path / "readme.md.bak").write_text("也不是", encoding="utf-8")

        assert PromptLibrary(tmp_path).names() == ("good_name",)

    def test_a_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        """「没有目录」与「目录是空的」在调用方眼里是同一件事。"""
        assert PromptLibrary(tmp_path / "nope").names() == ()

    def test_get_reads_the_file(self, tmp_path: Path) -> None:
        _write(tmp_path, "t", "你是 {name}。")

        template = PromptLibrary(tmp_path).get("t")

        assert template.name == "t"
        assert template.text == "你是 {name}。"
        assert template.path == tmp_path / "t.md"

    def test_get_is_cached(self, tmp_path: Path) -> None:
        """每次巩固都重读一遍磁盘，只会把「为什么不生效」变成「你是不是忘了重启」。"""
        _write(tmp_path, "t", "第一版")
        library = PromptLibrary(tmp_path)

        first = library.get("t")
        _write(tmp_path, "t", "第二版")

        assert library.get("t") is first
        assert library.get("t").text == "第一版"

    def test_clearing_the_cache_picks_up_an_edit(self, tmp_path: Path) -> None:
        _write(tmp_path, "t", "第一版")
        library = PromptLibrary(tmp_path)
        library.get("t")

        _write(tmp_path, "t", "第二版")
        library.clear()

        assert library.get("t").text == "第二版"

    def test_an_illegal_name_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(PromptError, match="模板名非法") as caught:
            PromptLibrary(tmp_path).get("../etc/passwd")

        assert caught.value.context["name"] == "../etc/passwd"

    def test_a_missing_template_lists_what_there_is(self, tmp_path: Path) -> None:
        """报错时要能回答「那有哪些」，否则只能跑去翻目录。"""
        _write(tmp_path, "a", "甲")
        _write(tmp_path, "b", "乙")

        with pytest.raises(PromptError, match="找不到提示词模板") as caught:
            PromptLibrary(tmp_path).get("c")

        assert caught.value.context["available"] == "a, b"

    def test_a_missing_template_says_so_when_there_are_none(self, tmp_path: Path) -> None:
        with pytest.raises(PromptError) as caught:
            PromptLibrary(tmp_path).get("c")

        assert caught.value.context["available"] == "（目录里没有模板）"

    def test_an_empty_template_is_refused(self, tmp_path: Path) -> None:
        """空模板不是「什么都不说」，它是一次什么都没问的付费调用。"""
        _write(tmp_path, "t", "   \n  ")

        with pytest.raises(PromptError, match="模板是空的"):
            PromptLibrary(tmp_path).get("t")

    def test_render_delegates(self, tmp_path: Path) -> None:
        _write(tmp_path, "t", "你是 {name}。")

        assert PromptLibrary(tmp_path).render("t", name="林晚") == "你是 林晚。"

    def test_render_names_the_template_in_the_error(self, tmp_path: Path) -> None:
        _write(tmp_path, "t", "你是 {name}。")

        with pytest.raises(PromptError) as caught:
            PromptLibrary(tmp_path).render("t")

        assert caught.value.context["source"] == "t"

    def test_preload_takes_the_whole_directory(self, tmp_path: Path) -> None:
        _write(tmp_path, "a", "甲")
        _write(tmp_path, "b", "乙")

        loaded = PromptLibrary(tmp_path).preload()

        assert [template.name for template in loaded] == ["a", "b"]

    def test_the_directory_is_readable(self, tmp_path: Path) -> None:
        assert PromptLibrary(tmp_path).directory == tmp_path


# ────────────────────────────────────────────────────────────
# 随包的那八个模板
# ────────────────────────────────────────────────────────────


class TestShippedTemplates:
    """这一组盯的是「模板与渲染它的代码有没有分家」。

    那是本项目最容易漏、也最难在生产里定位的一类 bug：模板里多一个
    ``{user_name}``，渲染方没传，于是模型收到一段带字面量大括号的提示词，
    然后围绕那个括号一本正经地编内容。第一轮测试全绿，问题在用户面前才出现。
    """

    def test_the_directory_ships_with_the_package(self) -> None:
        assert PROMPTS_DIR.is_dir()
        assert (PROMPTS_DIR / "memory_consolidate.md").is_file()

    def test_there_are_eight_of_them(self) -> None:
        """数量写死是有意的：加了模板要顺手在这里加一条，不然没人会注意到。"""
        assert PromptLibrary().names() == (
            "chat_reply",
            "emotion_update",
            "intention",
            "memory_consolidate",
            "persona_generate",
            "post_compose",
            "reach_out",
            "vault_organize",
        )

    def test_every_template_can_be_rendered(self) -> None:
        library = PromptLibrary()

        for name in library.names():
            template = library.get(name)
            values = {key: f"<{key}>" for key in template.placeholders}
            text = template.render(**values)

            for key, value in values.items():
                assert value in text, f"{name} 漏掉了 {{{key}}}"

    def test_the_consolidation_template_is_written_for_a_person(self) -> None:
        """内容是给「一个人回忆自己的今天」用的，不是给日志解析器看的。"""
        template = PromptLibrary().get("memory_consolidate")
        text = template.render(
            persona_name="林晚",
            activities="- 18:00 和阿哲聊了会儿",
            existing_memories="（还没有记下什么）",
            max_items=10,
            user_name="你",
        )

        assert "林晚" in text
        assert "和阿哲聊了会儿" in text

    def test_the_vault_template_is_written_for_a_person(self) -> None:
        """整理自己的知识库是「把这件事看明白」，不是给文件管理器写规则。"""
        template = PromptLibrary().get("vault_organize")
        text = template.render(
            persona_name="林晚",
            user_name="你",
            folders="- 20-想法 · 想法\n- 30-读到的 · 读到的",
            catalog="- 和阿哲吵架 · 和阿哲吵架（20-想法）",
            inbox="### 99-收集箱/xyz.md\n\n今天有点不想说话。\n",
            max_items=10,
        )

        assert "林晚" in text
        assert "不想说话" in text
        assert "20-想法" in text
        assert "最多 10 条" in text

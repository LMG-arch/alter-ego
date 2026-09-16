"""``alterego plugins`` 的测试。

这组用例走的是**真实路径**：真的去 ``plugins/`` 目录里发现、真的导入模块、
真的走 ``on_load`` / ``on_start`` / ``health``。插件体系最怕的失败模式是
「文档说这样写能跑，实际跑不起来」，而只有把随包的示例插件真的装一遍，
这句话才有内容。

所以这里**没有**假的插件、没有假的 ``PluginManager``。唯一被造出来的是
**坏掉的插件目录**——那是真实世界里最常见的故障，而且它只能靠造一个坏的
才能测：坏掉的插件往往没有 ``plugin.py`` 可导入，用 mock 反而测不到
「导入失败时这条命令还活不活着」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alterego.cli import build_parser, main
from alterego.interfaces.common import HealthStatus
from alterego.kernel.config import Config


#: 随包的三个示例插件。用例直接拿它们当被测对象——它们腐烂了，
#: ``test_example_plugin.py`` 会先红，这里再红一次也无妨。
PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugins"
EXAMPLE = "capability.example"
VAULT = "capability.obsidian_vault"


# ── 夹具 ────────────────────────────────────────────────────


def _make_config(
    tmp_path: Path,
    *,
    enabled: list[str] | None = None,
    search_paths: list[str] | None = None,
) -> Config:
    """一份指向临时数据目录、但插件搜索路径指向仓库 ``plugins/`` 的配置。

    搜索路径必须是绝对路径：相对路径按**进程的工作目录**解析，而 pytest
    的工作目录取决于从哪里调起来的。写死成 ``Path("plugins")`` 的话，
    从别的目录跑测试就会「一个插件都没发现」，而那种红是假的。
    """
    plugins: dict[str, Any] = {
        "search_paths": [str(PLUGIN_ROOT)] if search_paths is None else search_paths,
    }
    if enabled is not None:
        plugins["enabled"] = list(enabled)
    return Config.load(
        path=None,
        env={},
        overrides={"core": {"data_dir": str(tmp_path / "data")}, "plugins": plugins},
    )


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把「配置从哪来」换成一个恒定的接口。

    这是这组用例里**唯一**被 monkeypatch 的东西，而且换掉的是一个
    「读哪个文件」的函数，不是任何判断逻辑——所以被测的行为没变。
    """
    monkeypatch.setattr("alterego.cli_plugins._plugins_config", lambda: _make_config(tmp_path))
    return tmp_path


def _enable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *plugin_ids: str) -> None:
    """把配置改成「只启用这几个」。````[plugins] enabled`` 是白名单语义。"""
    monkeypatch.setattr(
        "alterego.cli_plugins._plugins_config",
        lambda: _make_config(tmp_path, enabled=list(plugin_ids)),
    )


def _make_broken_plugin(root: Path, name: str) -> Path:
    """造一个「清单读不出来」的插件目录：缺了必填的 ``id``。

    ``id`` 缺了而不是写成非法值，是因为这里想测的不是校验规则（那条
    ``test_plugin_manifest.py`` 管），而是「清单坏了的时候这条命令还活不活着」。
    """
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "plugin.toml").write_text(
        '[plugin]\nversion = "0.1.0"\napi_version = 1\nkind = "tool"\nentry = "plugin:X"\n',
        encoding="utf-8",
    )
    return directory


def _make_plugin(
    root: Path,
    name: str,
    *,
    plugin_id: str,
    body: str | None = None,
    config_table: str = "",
) -> Path:
    """造一个清单合法（因而能被发现）的插件目录。

    ``body`` 是 ``plugin.py`` 的内容；不给就不写文件——发现阶段不需要它。
    ``config_table`` 追加在清单末尾，用来造带配置项的插件。
    """
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "plugin.toml").write_text(
        f'[plugin]\nid = "{plugin_id}"\nversion = "0.1.0"\napi_version = 1\n'
        'kind = "tool"\nentry = "plugin:X"\n' + config_table,
        encoding="utf-8",
    )
    if body is not None:
        (directory / "plugin.py").write_text(body, encoding="utf-8")
    return directory


# ────────────────────────────────────────────────────────────
# 命令行
# ────────────────────────────────────────────────────────────


class TestCommandLine:
    def test_the_group_is_registered(self) -> None:
        parser = build_parser()

        assert parser.parse_args(["plugins", "list"]).handler.__name__ == "cmd_plugins_list"
        assert parser.parse_args(["plugins", "doctor"]).handler.__name__ == "cmd_plugins_doctor"
        assert parser.parse_args(["plugins", "info", "x.y"]).handler.__name__ == "cmd_plugins_info"
        assert parser.parse_args(["plugins", "reload", "x.y"]).handler.__name__ == (
            "cmd_plugins_reload"
        )
        assert parser.parse_args(["plugins", "reset"]).handler.__name__ == "cmd_plugins_reset"

    def test_plugin_id_is_mandatory_where_it_should_be(self) -> None:
        parser = build_parser()

        # reset 可以不带 id（只报告现状）；其余三个不行。
        assert parser.parse_args(["plugins", "reset"]).plugin_id is None
        assert parser.parse_args(["plugins", "reset", "x.y"]).plugin_id == "x.y"
        with pytest.raises(SystemExit):
            parser.parse_args(["plugins", "info"])

    def test_the_group_shows_its_own_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        # 敲到 `alterego plugins` 这一层时要打**这一层**的帮助，
        # 而不是回落到顶层帮助——后者看着像「plugins 后面没东西可敲」。
        assert main(["plugins"]) == 0

        shown = capsys.readouterr().out
        assert "list" in shown
        assert "doctor" in shown
        assert "不导入插件代码" in shown

    def test_the_top_level_help_mentions_the_group(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main([])

        assert "plugins" in capsys.readouterr().out


# ────────────────────────────────────────────────────────────
# list
# ────────────────────────────────────────────────────────────


class TestList:
    def test_it_finds_the_plugins_that_ship_with_the_project(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["plugins", "list"]) == 0

        shown = capsys.readouterr().out
        assert EXAMPLE in shown
        assert VAULT in shown
        assert "发现 3 个" in shown

    def test_none_of_the_examples_are_enabled_by_default(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 三个示例清单里都是 enabled_by_default = false。这不是巧合：
        # 示例不该在用户没要求的时候自己跑起来。
        assert main(["plugins", "list"]) == 0

        shown = capsys.readouterr().out
        assert "启用 0 个" in shown
        assert "都被发现了，但一个都没启用" in shown

    def test_it_hands_over_the_line_to_copy(self, capsys: pytest.CaptureFixture[str]) -> None:
        # 「请去配置」不是帮助。这里要给出能直接抄进文件的那一行。
        assert main(["plugins", "list"]) == 0

        shown = capsys.readouterr().out
        assert "[plugins]" in shown
        assert 'enabled = ["capability.dataset_exporter"]' in shown

    def test_enabled_plugins_are_marked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _enable(tmp_path, monkeypatch, EXAMPLE)

        assert main(["plugins", "list"]) == 0

        shown = capsys.readouterr().out
        assert "启用 1 个" in shown
        assert f"已启用  {EXAMPLE}" in shown
        assert "都被发现了，但一个都没启用" not in shown

    def test_nothing_found_says_where_it_looked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 空表是最没用的输出。发现不到插件时，用户想知道的是「它找过哪里」。
        empty = tmp_path / "nowhere"
        empty.mkdir()
        monkeypatch.setattr(
            "alterego.cli_plugins._plugins_config",
            lambda: _make_config(tmp_path, search_paths=[str(empty)]),
        )

        assert main(["plugins", "list"]) == 0

        shown = capsys.readouterr().out
        assert "一个插件都没发现" in shown
        assert str(empty) in shown
        assert "example_plugin" in shown

    def test_a_broken_manifest_is_reported_not_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 插件「没生效」十有八九坏在清单上。清单坏了的插件不会出现在表里，
        # 所以必须有单独一节说它——否则用户会以为那个目录被忽略了。
        root = tmp_path / "plugroot"
        broken = _make_broken_plugin(root, "broken_one")
        monkeypatch.setattr(
            "alterego.cli_plugins._plugins_config",
            lambda: _make_config(tmp_path, search_paths=[str(root)]),
        )

        assert main(["plugins", "list"]) == 0

        shown = capsys.readouterr().out
        assert "清单读不出来的目录" in shown
        assert str(broken) in shown
        assert "id 非法" in shown

    def test_listing_does_not_import_the_plugins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # list 存在的理由就是「插件代码导入不了时它照样能跑」。
        # 用一个 import 就会炸的插件证明这一点。
        root = tmp_path / "plugroot"
        _make_plugin(root, "exploding", plugin_id="tool.exploding", body="raise RuntimeError\n")
        monkeypatch.setattr(
            "alterego.cli_plugins._plugins_config",
            lambda: _make_config(tmp_path, search_paths=[str(root)]),
        )

        assert main(["plugins", "list"]) == 0

        shown = capsys.readouterr().out
        assert "tool.exploding" in shown

    def test_the_config_file_is_named_in_the_header(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 没跑过 `alterego init` 时必须写出来：插件发现不到的第一大原因，
        # 就是用户以为自己在改某个配置文件，而那个文件根本不存在。
        assert main(["plugins", "list"]) == 0

        assert "内置默认值" in capsys.readouterr().out


# ────────────────────────────────────────────────────────────
# doctor
# ────────────────────────────────────────────────────────────


class TestDoctor:
    def test_with_nothing_enabled_it_still_exits_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["plugins", "doctor"]) == 0

        assert "没有需要体检的插件" in capsys.readouterr().out

    def test_it_actually_loads_the_example(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _enable(tmp_path, monkeypatch, EXAMPLE)

        assert main(["plugins", "doctor"]) == 0

        shown = capsys.readouterr().out
        assert "已启动 1 个" in shown
        assert EXAMPLE in shown
        assert "正常" in shown

    def test_it_loads_several_and_tolerates_transitive_enabling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _enable(tmp_path, monkeypatch, EXAMPLE, VAULT)

        assert main(["plugins", "doctor"]) == 0

        shown = capsys.readouterr().out
        assert "已启动 2 个" in shown
        assert EXAMPLE in shown
        assert VAULT in shown

    def test_an_enabled_but_absent_plugin_is_a_dependency_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 白名单里写了一个根本不存在的 id。退出码必须是 3（EXIT_DEPENDENCY_ERROR），
        # 好让脚本把「配置写错了」和「插件之间接不起来」分开。
        _enable(tmp_path, monkeypatch, "capability.nobody")

        assert main(["plugins", "doctor"]) == 3

        assert "capability.nobody" in capsys.readouterr().err

    def test_a_plugin_that_cannot_be_imported_is_a_failure_not_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "plugroot"
        _make_plugin(root, "exploding", plugin_id="tool.exploding", body="raise RuntimeError\n")
        monkeypatch.setattr(
            "alterego.cli_plugins._plugins_config",
            lambda: _make_config(tmp_path, enabled=["tool.exploding"], search_paths=[str(root)]),
        )

        # 非零退出，但**不抛异常**：一个坏插件不该让体检本身跑不了。
        assert main(["plugins", "doctor"]) == 1

        shown = capsys.readouterr().out
        assert "tool.exploding" in shown
        assert "失败" in shown


# ────────────────────────────────────────────────────────────
# info
# ────────────────────────────────────────────────────────────


class TestInfo:
    def test_it_prints_the_manifest(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["plugins", "info", EXAMPLE]) == 0

        shown = capsys.readouterr().out
        assert "示例能力" in shown
        assert "capability" in shown
        assert "0.1.0" in shown
        assert "plugin:ExampleCapability" in shown

    def test_it_says_whether_the_api_version_matches(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from alterego.kernel.manifest import API_VERSION

        assert main(["plugins", "info", EXAMPLE]) == 0

        shown = capsys.readouterr().out
        assert f"内核 {API_VERSION}" in shown
        assert "兼容" in shown

    def test_it_shows_the_declared_config_and_its_current_values(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["plugins", "info", EXAMPLE]) == 0

        shown = capsys.readouterr().out
        assert "greeting" in shown
        assert "你好" in shown
        assert "清单默认值" in shown

    def test_enum_choices_are_shown(self, capsys: pytest.CaptureFixture[str]) -> None:
        # 一排看不懂的英文值就是设置页的经典毛病，插件配置也一样。
        assert main(["plugins", "info", EXAMPLE]) == 0

        shown = capsys.readouterr().out
        assert "可选" in shown

    def test_it_does_not_import_the_plugin(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["plugins", "info", EXAMPLE]) == 0

        # info 也刻意不 load，所以它只能说「配置上会不会加载」，
        # 并且必须把下一句指出来——不然「已启用却没跑起来」就没地方追问了。
        assert "要确认它真的装得起来" in capsys.readouterr().out

    def test_an_unknown_id_lists_what_is_available(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["plugins", "info", "capability.nope"]) == 2

        err = capsys.readouterr().err
        assert "没有发现叫 capability.nope 的插件" in err
        assert EXAMPLE in err

    def test_a_plugin_without_config_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "plugroot"
        _make_plugin(root, "plain", plugin_id="tool.plain")
        monkeypatch.setattr(
            "alterego.cli_plugins._plugins_config",
            lambda: _make_config(tmp_path, search_paths=[str(root)]),
        )

        assert main(["plugins", "info", "tool.plain"]) == 0

        assert "没有声明任何配置项" in capsys.readouterr().out

    def test_it_says_it_will_load_when_the_whitelist_has_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _enable(tmp_path, monkeypatch, EXAMPLE)

        assert main(["plugins", "info", EXAMPLE]) == 0

        assert "会被加载" in capsys.readouterr().out

    def test_a_secret_field_is_masked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 这段输出经常被原样贴进 issue，而贴的人不会记得打码。
        root = tmp_path / "plugroot"
        _make_plugin(
            root,
            "secretive",
            plugin_id="tool.secretive",
            config_table=(
                "\n[plugin.config.token]\n"
                'type = "string"\n'
                'description = "接口密钥。"\n'
                "secret = true\n"
                'default = "sk-live-abcdef"\n'
            ),
        )
        monkeypatch.setattr(
            "alterego.cli_plugins._plugins_config",
            lambda: _make_config(tmp_path, search_paths=[str(root)]),
        )

        assert main(["plugins", "info", "tool.secretive"]) == 0

        shown = capsys.readouterr().out
        assert "密钥" in shown
        assert "sk-live-abcdef" not in shown
        assert "***" in shown

    def test_a_field_that_cannot_be_resolved_is_shown_as_such(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 必填但没有默认值、又没配值。这里不该抛异常——info 是拿来看
        # 「为什么配不对」的，它自己先炸掉就本末倒置了。
        root = tmp_path / "plugroot"
        _make_plugin(
            root,
            "demanding",
            plugin_id="tool.demanding",
            config_table=(
                "\n[plugin.config.endpoint]\n"
                'type = "string"\n'
                'description = "服务地址。"\n'
                "required = true\n"
            ),
        )
        monkeypatch.setattr(
            "alterego.cli_plugins._plugins_config",
            lambda: _make_config(tmp_path, search_paths=[str(root)]),
        )

        assert main(["plugins", "info", "tool.demanding"]) == 0

        shown = capsys.readouterr().out
        assert "现在解析不了" in shown
        assert "endpoint" in shown
        # 值留空时的占位符，不是把上一项的值串下来。
        assert "当前值 —" in shown


# ────────────────────────────────────────────────────────────
# reload / reset
# ────────────────────────────────────────────────────────────


class TestReload:
    def test_a_local_plugin_can_be_reloaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _enable(tmp_path, monkeypatch, EXAMPLE)

        assert main(["plugins", "reload", EXAMPLE]) == 0

        shown = capsys.readouterr().out
        assert "结果      成功" in shown
        assert "已启动" in shown

    def test_a_plugin_that_is_not_enabled_cannot_be_reloaded(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 示例默认不启用。对着一个没加载的插件说「重载成功」是撒谎。
        assert main(["plugins", "reload", EXAMPLE]) == 2

        err = capsys.readouterr().err
        assert "不在这次加载的插件里" in err
        assert "没有被启用" in err

    def test_an_unknown_id_is_rejected(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["plugins", "reload", "capability.nope"]) == 2

        assert "没有发现叫" in capsys.readouterr().err

    def test_a_failed_reload_says_the_old_instance_is_not_coming_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # 重载失败是这条命令唯一真正要处理的结果，而它没法靠造一个真插件
        # 复现（真插件要么一直好、要么一开始就装不上）。所以这里换掉
        # ``PluginManager.reload`` 这一个方法，其余全是真的。
        _enable(tmp_path, monkeypatch, EXAMPLE)
        _force_reload_result(monkeypatch, False)
        _make_plugin(tmp_path / "plugroot", "exploding", plugin_id="capability.exploding")
        monkeypatch.setattr(
            "alterego.cli_plugins._plugins_config",
            lambda: _make_config(
                tmp_path,
                enabled=[EXAMPLE, "capability.exploding"],
                search_paths=[str(PLUGIN_ROOT), str(tmp_path / "plugroot")],
            ),
        )

        assert main(["plugins", "reload", EXAMPLE]) == 1

        shown = capsys.readouterr().out
        assert "结果      失败" in shown
        assert "不会恢复成旧实例" in shown
        assert "本命令" in shown


def _force_reload_result(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """把 ``reload`` 的返回值钉死，其余一概不动。

    造出来的 ``PluginManager`` 是真的，``load_all`` 是真的，插件也是真的——
    被换掉的只有一个布尔返回值。这样测到的仍然是「这个方法返回 False 时
    这条命令怎么办」，而不是一个凭空捏造的分支。
    """
    from alterego.cli_plugins import _manager as real_manager

    def wrapper(config: Config) -> Any:
        manager = real_manager(config)
        manager.reload = lambda plugin_id: value  # type: ignore[method-assign]
        return manager

    monkeypatch.setattr("alterego.cli_plugins._manager", wrapper)


class TestReset:
    def test_reporting_only_lists_nothing_when_nothing_is_open(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["plugins", "reset"]) == 0

        assert "没有插件处于熔断状态" in capsys.readouterr().out

    def test_resetting_a_named_plugin_says_it_was_never_open(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["plugins", "reset", EXAMPLE]) == 0

        shown = capsys.readouterr().out
        assert "本来就没有被熔断" in shown
        # 必须说清楚为什么：熔断计数活在进程内存里，而这条命令每次都是新进程。
        assert "内存里" in shown

    def test_an_unknown_id_is_rejected(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["plugins", "reset", "capability.nope"]) == 2

        assert "没有发现叫" in capsys.readouterr().err


# ────────────────────────────────────────────────────────────
# 几个纯格式函数
# ────────────────────────────────────────────────────────────


class TestFormatters:
    """直接对着测，不绕命令行。

    这四个是纯函数：进来一个值，出去一行字。绕一圈 ``main()`` 反而看不到
    全部分支（比如「配置值是空字符串」和「配置值没设置」是两条线），
    而它们本来就不需要一整个进程才能测。
    """

    def test_render_value_distinguishes_unset_from_empty(self) -> None:
        from alterego.cli_plugins import _render_value

        # 「没设置」和「设置成了空字符串」是两回事，混成一个就会让人
        # 在配置里来回找一个不存在的拼写错误。
        assert _render_value(None) == "未设置"
        assert _render_value("") == "（空字符串）"
        assert _render_value("你好") == "你好"

    def test_render_value_uses_chinese_for_booleans(self) -> None:
        from alterego.cli_plugins import _render_value

        assert _render_value(True) == "是"
        assert _render_value(False) == "否"
        assert _render_value(3) == "3"

    def test_health_line_puts_detail_and_hint_on_their_own_lines(self) -> None:
        from alterego.cli_plugins import _health_line

        assert _health_line("a.b", HealthStatus(ok=True)) == ["a.b  正常"]

        rich = _health_line("a.b", HealthStatus(ok=False, detail="连不上", hint="检查密钥"))
        assert rich[0] == "a.b  异常"
        assert rich[1].strip() == "连不上"
        assert rich[2].strip() == "提示：检查密钥"

    def test_config_label_says_so_when_there_is_no_file(self) -> None:
        from alterego.cli_plugins import _config_label

        # 空白表头会把人引去找别的原因，而真实原因恰恰是「没有配置文件」。
        assert "内置默认值" in _config_label(Config.load(path=None, env={}))

    def test_the_load_report_prints_the_side_deals(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from alterego.cli_plugins import _print_load_report
        from alterego.kernel.manager import LoadReport

        _print_load_report(
            LoadReport(
                loaded=["a.b"],
                auto_enabled=["a.c"],
                skipped_optional={"a.b": ["a.d"]},
                circuit_open=["a.e"],
            )
        )

        shown = capsys.readouterr().out
        assert "顺带启用的：a.c" in shown
        assert "a.b → a.d" in shown
        assert "alterego plugins reset a.e 恢复" in shown

    def test_the_load_report_is_silent_when_there_is_nothing_extra(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from alterego.cli_plugins import _print_load_report
        from alterego.kernel.manager import LoadReport

        _print_load_report(LoadReport(loaded=["a.b"]))

        assert capsys.readouterr().out == ""

    def test_a_plugin_installed_as_a_package_is_labelled_as_such(self) -> None:
        from alterego.cli_plugins import _source_text
        from alterego.kernel.manifest import PluginManifest

        # 本地插件与 pip 装进来的插件，用户要采取的行动完全不同
        # （一个是「去看那个目录」，一个是「去翻 site-packages」）。
        packed = PluginManifest.parse(
            {
                "id": "tool.packed",
                "version": "0.1.0",
                "api_version": 1,
                "kind": "tool",
                "entry": "plugin:X",
            },
            source="entry_point",
        )
        assert _source_text(packed) == "已安装的包"

    def test_the_config_label_names_the_file_when_there_is_one(self, tmp_path: Path) -> None:
        from alterego.cli_plugins import _config_label

        path = tmp_path / "alterego.toml"
        path.write_text('[core]\ndata_dir = "d"\n', encoding="utf-8")

        assert _config_label(Config.load(path=path, env={}, require_file=True)) == str(path)

    def test_an_unknown_id_when_nothing_was_discovered_points_at_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "nowhere"
        root.mkdir()
        monkeypatch.setattr(
            "alterego.cli_plugins._plugins_config",
            lambda: _make_config(tmp_path, search_paths=[str(root)]),
        )

        assert main(["plugins", "info", "tool.nope"]) == 2

        err = capsys.readouterr().err
        assert "一个插件都没发现" in err
        assert "alterego plugins list" in err


# ────────────────────────────────────────────────────────────
# 清单里那些不常被看到的部分
# ────────────────────────────────────────────────────────────


class TestManifestDetails:
    """依赖、提供、环境变量来源。

    这三样都是**契约**的一部分：插件作者要看它们确认自己的声明被读进去了，
    用户在排查「为什么没装上」时也要看它们。所以它们得有测试。
    """

    def _one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: str) -> str:
        root = tmp_path / "plugroot"
        _make_plugin(root, "detailed", plugin_id="tool.detailed", config_table=table)
        monkeypatch.setattr(
            "alterego.cli_plugins._plugins_config",
            lambda: _make_config(tmp_path, search_paths=[str(root)]),
        )
        return "tool.detailed"

    def test_dependencies_and_provides_are_shown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        plugin_id = self._one(tmp_path, monkeypatch, "")
        # 依赖得写在 [plugin] 表里，所以这里直接改一趟文件。
        path = tmp_path / "plugroot" / "detailed" / "plugin.toml"
        path.write_text(
            path.read_text(encoding="utf-8")
            + 'requires = ["tool.other >= 0.1.0"]\n'
            + 'optional = ["tool.maybe >= 0.1.0"]\n'
            + 'provides = ["tool.detail"]\n',
            encoding="utf-8",
        )

        assert main(["plugins", "info", plugin_id]) == 0

        shown = capsys.readouterr().out
        assert "tool.other >= 0.1.0" in shown
        assert "tool.maybe >= 0.1.0" in shown
        assert "tool.detail" in shown
        assert "可选依赖缺失" not in shown

    def test_a_field_sourced_from_an_environment_variable_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        plugin_id = self._one(
            tmp_path,
            monkeypatch,
            "\n[plugin.config.key]\n"
            'type = "string"\n'
            'description = "从环境变量读。"\n'
            'env = "ALTEREGO_FIELD_KEY"\n',
        )

        assert main(["plugins", "info", plugin_id]) == 0

        # 值没设时得说清楚「这一项优先从环境变量取」，否则用户会去翻
        # 配置文件找一个根本不存在的键。
        assert "环境变量 ALTEREGO_FIELD_KEY" in capsys.readouterr().out

    def test_a_field_without_a_description_prints_no_blank_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        plugin_id = self._one(
            tmp_path,
            monkeypatch,
            '\n[plugin.config.aaa]\ntype = "string"\ndefault = "x"\n'
            '\n[plugin.config.zzz]\ntype = "string"\n'
            'description = "有说明。"\ndefault = "y"\n',
        )

        assert main(["plugins", "info", plugin_id]) == 0

        lines = capsys.readouterr().out.splitlines()
        # 没有 description 的字段后面必须**直接**接下一个字段的抬头，
        # 而不是留下一行只有缩进的空说明——那看起来像「说明是空的」，
        # 而实际含义是「这个字段没写说明」。
        index = next(i for i, line in enumerate(lines) if "当前值 x" in line)
        assert lines[index + 1].startswith("  zzz")
        assert "有说明。" in lines[index + 3]

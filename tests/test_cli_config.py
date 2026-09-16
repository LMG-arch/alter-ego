"""``alterego config`` 的五条命令。

这一组测试守的是**「命令行与设置页说同一句话」**这条性质：输出的每一句说明都
来自内核的元数据目录，所以这里断言的是「它把元数据里的话说出来了」，
而不是「它写了一句我认识的文案」——后者会在文案微调时变成假失败。

``_config()`` 是模块级的，就是为了让这些测试把工作目录换成一个临时目录：
否则它们会去读开发机上真实的 ``config/alterego.toml``，在别人机器上绿、
在自己机器上红（``cli_birthday`` 与 ``cli_memory`` 用的是同一个办法）。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from alterego import cli
from alterego import cli_config as config_cli
from alterego.kernel.config import Config
from alterego.kernel.settings import Setting, SettingKind
from alterego.kernel.settings_catalog import all_settings, get_setting_metadata, section_paths


CONFIG_TOML = """\
[core]
log_level = "INFO"
user_name = "阿岚"
oops = 1

[disturb_budget]
daily_message_limit = 3

[llm.routing]
decision = "strong"
"""


def _metadata(key: str) -> Setting:
    """从真实目录里取一条元数据，省得在每个测试里重复 rpartition 三次。

    用它而不是手搓一个 ``Setting``，是为了让这些断言测的**就是**设置页会看到的
    那一句话——搓一个出来只会证明「我写的那句话在输出里」。
    """
    parent, _, name = key.rpartition(".")
    found = get_setting_metadata(section_paths[parent], name)
    assert found is not None, f"{key} 在元数据目录里不存在"
    return found


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个只有临时配置文件的「工作目录」。"""
    path = tmp_path / "alterego.toml"
    path.write_text(CONFIG_TOML, encoding="utf-8", newline="")
    monkeypatch.setattr(config_cli, "_config", lambda: Config.load(path=path, env={}))
    return path


@pytest.fixture
def bare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """没有配置文件的机器：读得到默认值，但没地方可以写。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_cli, "_config", lambda: Config.load(env={}))
    return tmp_path


def _run(*argv: str) -> int:
    return cli.main(list(argv))


# ═══════════════════════════════════════════════════════════════
#  参数树
# ═══════════════════════════════════════════════════════════════


def test_config_group_is_registered(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        _run("config", "--help")
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert "explain" in out
    assert "schema" in out


def test_bare_config_prints_its_own_help(capsys: pytest.CaptureFixture[str]) -> None:
    """只敲 `alterego config` 时要打**这一层**的帮助，不是顶层帮助。"""
    assert _run("config") == 0
    out = capsys.readouterr().out
    assert "只有 set 会改文件" in out


# ═══════════════════════════════════════════════════════════════
#  show
# ═══════════════════════════════════════════════════════════════


def test_show_lists_every_setting(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("config", "show") == 0
    out = capsys.readouterr().out
    missing = [setting.key for setting in all_settings() if setting.key not in out]
    assert not missing, f"show 漏掉了 {missing}"


def test_show_prints_the_effect_of_each_setting(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """「改了会怎样」必须打出来——这是设置页存在的全部理由。"""
    assert _run("config", "show", "core.log_level") == 0
    out = capsys.readouterr().out
    setting = _metadata("core.log_level")
    assert setting.effect in out
    assert setting.description in out


def test_show_filters_by_section(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("config", "show", "llm.routing") == 0
    out = capsys.readouterr().out
    assert "llm.routing.decision" in out
    assert "core.log_level" not in out


def test_show_filters_by_group(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("config", "show", "预算") == 0
    out = capsys.readouterr().out
    assert "llm.budget.daily_usd_limit" in out
    assert "core.log_level" not in out


def test_show_marks_restart_and_danger(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """需要重启的项必须当场标出来：「改完没生效」是这类设置最常见的困惑。"""
    assert _run("config", "show", "storage") == 0
    out = capsys.readouterr().out
    assert "[需重启]" in out
    assert "[危险]" in out
    assert "[高级]" in out


def test_show_prints_the_current_value(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("config", "show", "disturb_budget.daily_message_limit") == 0
    out = capsys.readouterr().out
    assert "配置文件" in out
    assert "alterego.toml" in out
    assert "每天最多主动发几条消息" in out


def test_show_carries_the_unit(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("config", "show", "retention.tick_log_keep_days") == 0
    assert "天" in capsys.readouterr().out


def test_show_reports_an_unknown_filter(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("config", "show", "根本没有这一段") == 2
    err = capsys.readouterr().err
    assert "没有匹配" in err
    assert "llm" in err


def test_show_json_is_machine_readable(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("config", "show", "core", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"].endswith("alterego.toml")
    keys = [item["key"] for item in payload["settings"]]
    assert "core.log_level" in keys
    assert all(item["annotated"] for item in payload["settings"])
    assert all(item["kind"] in {str(kind) for kind in SettingKind} for item in payload["settings"])


def test_show_json_carries_the_current_value(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("config", "show", "disturb_budget.daily_message_limit", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["settings"][0]["current"] == 3
    assert payload["settings"][0]["default"] == 3
    assert payload["settings"][0]["unit"] == "条"


def test_show_lists_unannotated_keys(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """配置文件里有、内核不认识的键要**说出来**，而不是静默忽略。

    静默忽略会让用户以为自己的配置丢了。
    """
    assert _run("config", "show", "core") == 0
    out = capsys.readouterr().out
    assert "未标注" in out
    assert "core.oops" in out


def test_show_json_lists_unannotated_keys(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("config", "show", "core", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["unannotated"] == [{"key": "core.oops", "value": 1}]


def test_show_survives_an_unreadable_file_for_the_unannotated_part(
    workspace: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未标注的键只是「顺带一提」，读不到文件不该让整条命令失败。

    这里把 ``source`` 换成一个目录，而不是去 monkeypatch ``Path.read_text``：
    后者会连配置加载一起改掉，测出来的就是「命令失败」而不是「这一段降级」。
    """
    broken = dataclasses.replace(config_cli._config(), source=tmp_path)
    monkeypatch.setattr(config_cli, "_config", lambda: broken)
    assert _run("config", "show", "core", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["unannotated"] == [{"key": "core.oops", "value": None}]


# ═══════════════════════════════════════════════════════════════
#  explain
# ═══════════════════════════════════════════════════════════════


def test_explain_prints_everything_a_person_needs(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("config", "explain", "llm.routing.decision") == 0
    out = capsys.readouterr().out
    setting = _metadata("llm.routing.decision")
    assert setting.label in out
    assert setting.description in out
    assert setting.effect in out
    assert "当前值" in out
    assert "默认值" in out
    assert "[llm.routing] 段里" in out
    assert "生效方式" in out


def test_explain_lists_choices_with_their_consequences(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """枚举的每个选项都要带上「选了它会发生什么」，否则下拉框等于没写。"""
    assert _run("config", "explain", "llm.budget.on_exceed") == 0
    out = capsys.readouterr().out
    setting = _metadata("llm.budget.on_exceed")
    assert setting.choices
    for choice in setting.choices:
        assert choice.value in out
        assert choice.label in out
        assert choice.consequence in out


def test_explain_shows_the_range_and_the_restart_note(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("config", "explain", "web.port") == 0
    out = capsys.readouterr().out
    assert "取值范围  1 ~ 65535 端口" in out
    assert "重启后生效" in out


def test_explain_says_when_a_setting_takes_effect_immediately(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("config", "explain", "disturb_budget.daily_message_limit") == 0
    assert "立即生效" in capsys.readouterr().out


def test_explain_warns_about_a_dangerous_setting(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("config", "explain", "storage.db_path") == 0
    assert "危险项" in capsys.readouterr().out


def test_explain_suggests_a_correction_for_a_typo(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """键写错时要说「你是不是想写 X」——错法多半是手指而不是理解。"""
    assert _run("config", "explain", "llm.routing.decisoin") == 2
    err = capsys.readouterr().err
    assert "没有这个键" in err
    assert "你是不是想写 decision" in err


def test_explain_suggests_a_correction_for_a_bad_section(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("config", "explain", "llm.routng.decision") == 2
    assert "没有这个配置段" in capsys.readouterr().err


def test_explain_refuses_a_section_name(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """段不是设置项：这时要指路到 `config show <段>`。"""
    assert _run("config", "explain", "llm") == 2
    err = capsys.readouterr().err
    assert "是一个配置段" in err
    assert "config show llm" in err


def test_explain_refuses_a_section_name_even_when_nested(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("config", "explain", "llm.routing") == 2
    assert "是一个配置段" in capsys.readouterr().err


def test_explain_marks_a_setting_nobody_documented(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """只有类型没有说明的项（元数据的第三层）要标成「未标注」，而不是编一句话。"""
    monkeypatch.setattr(config_cli, "get_setting_metadata", lambda cls, name: None)
    assert _run("config", "explain", "core.log_level") == 0
    out = capsys.readouterr().out
    assert "未标注" in out
    assert "没有说明" in out


# ═══════════════════════════════════════════════════════════════
#  get
# ═══════════════════════════════════════════════════════════════


def test_get_prints_a_bare_string(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`$(alterego config get core.user_name)` 要拿到 `阿岚`，不是 `"阿岚"`。"""
    assert _run("config", "get", "core.user_name") == 0
    assert capsys.readouterr().out.strip() == "阿岚"


def test_get_prints_a_number_as_json(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("config", "get", "disturb_budget.daily_message_limit") == 0
    assert capsys.readouterr().out.strip() == "3"


def test_get_prints_a_list_as_json(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("config", "get", "disturb_budget.quiet_hours") == 0
    assert json.loads(capsys.readouterr().out) == ["23:30", "08:00"]


def test_get_prints_a_bool_as_json(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """布尔值必须走 JSON 分支：`true` 在 shell 里比 `是` 好用。"""
    assert _run("config", "get", "storage.foreign_keys") == 0
    assert capsys.readouterr().out.strip() == "true"


def test_get_says_when_a_value_is_unset(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """空值不打印空行——那与「命令失败了」看不出来。"""
    assert _run("config", "get", "channels.options") == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    assert "未设置" in captured.err


def test_get_rejects_an_unknown_key(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("config", "get", "core.nope") == 2
    assert "没有这个键" in capsys.readouterr().err


# ═══════════════════════════════════════════════════════════════
#  set
# ═══════════════════════════════════════════════════════════════


def test_set_writes_the_file(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("config", "set", "disturb_budget.daily_message_limit", "5") == 0
    out = capsys.readouterr().out
    assert "3  →  5" in out
    assert "alterego.toml" in out
    assert "daily_message_limit = 5" in workspace.read_text(encoding="utf-8")


def test_set_keeps_a_backup_by_default(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("config", "set", "core.log_level", "DEBUG") == 0
    assert "备份" in capsys.readouterr().out
    assert workspace.with_name("alterego.toml.bak").exists()


def test_set_can_skip_the_backup(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("config", "set", "core.log_level", "DEBUG", "--no-backup") == 0
    assert "未备份" in capsys.readouterr().out
    assert not workspace.with_name("alterego.toml.bak").exists()


def test_set_honours_the_auto_backup_setting(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`settings.auto_backup = false` 时不留备份——这是那一项存在的理由。"""
    monkeypatch.setattr(
        config_cli,
        "_config",
        lambda: Config.load(path=workspace, env={}, overrides={"settings": {"auto_backup": False}}),
    )
    assert _run("config", "set", "core.log_level", "DEBUG") == 0
    assert "未备份" in capsys.readouterr().out
    assert not workspace.with_name("alterego.toml.bak").exists()


def test_set_dry_run_changes_nothing(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = workspace.read_text(encoding="utf-8")
    assert _run("config", "set", "disturb_budget.daily_message_limit", "5", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "daily_message_limit = 5" in out
    assert "没有被改动" in out
    assert workspace.read_text(encoding="utf-8") == before


def test_set_warns_about_a_restart(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("config", "set", "web.port", "9000") == 0
    assert "[需重启]" in capsys.readouterr().out


def test_set_rejects_a_bad_value_without_touching_the_file(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = workspace.read_text(encoding="utf-8")
    assert _run("config", "set", "llm.routing.decision", "聪明") == 2
    assert "只能填" in capsys.readouterr().err
    assert workspace.read_text(encoding="utf-8") == before


def test_set_rejects_a_value_out_of_range(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """范围由元数据说，所以这一条走的是渲染那一层，不用等加载校验。"""
    assert _run("config", "set", "web.port", "99999") == 2
    err = capsys.readouterr().err
    assert "不能大于 65535" in err
    assert "地址已被使用" in err


def test_set_reports_what_the_loader_says(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """元数据放行、加载校验拒绝时，报的是加载校验的话，且文件不被改坏。

    用的是**跨字段**的那种非法：``daily_message_limit`` 单独看完全合法
    （0 以上），但 ``DisturbBudgetConfig`` 要求紧急上限不低于日常上限，
    而默认的紧急上限是 5。这种错只有真的把内容加载一遍才会被发现。
    """
    before = workspace.read_text(encoding="utf-8")
    assert _run("config", "set", "disturb_budget.daily_message_limit", "7") == 2
    assert "紧急上限不能低于普通上限" in capsys.readouterr().err
    assert workspace.read_text(encoding="utf-8") == before


def test_set_without_a_config_file(bare: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """没有配置文件时先 `alterego init`，而不是凭空造一个。"""
    assert _run("config", "set", "core.log_level", "DEBUG") == 2
    err = capsys.readouterr().err
    assert "没有地方可以写" in err
    assert "alterego init" in err


def test_set_creates_a_missing_section(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("config", "set", "storage.journal_mode", "WAL") == 0
    text = workspace.read_text(encoding="utf-8")
    assert "[storage]" in text
    assert 'journal_mode = "WAL"' in text


def test_set_keeps_the_earlier_sections_intact(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("config", "set", "disturb_budget.daily_message_limit", "4") == 0
    text = workspace.read_text(encoding="utf-8")
    assert 'log_level = "INFO"' in text
    assert "oops = 1" in text
    assert 'decision = "strong"' in text


# ═══════════════════════════════════════════════════════════════
#  schema
# ═══════════════════════════════════════════════════════════════


def test_schema_without_json_prints_a_summary(
    bare: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("config", "schema") == 0
    out = capsys.readouterr().out
    assert f"共 {len(all_settings())} 个设置项" in out
    assert f"{len(section_paths)} 个配置段" in out
    assert "加 --json" in out


def test_schema_json_does_not_need_a_config_file(
    bare: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """schema 回答的是「有哪些设置」，不是「你设成了什么」——所以它不读文件。"""
    assert _run("config", "schema", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == 1
    assert len(payload["settings"]) == len(all_settings())
    assert payload["sections"]["llm"] == "LLMConfig"
    assert payload["groups"] == list(dict.fromkeys(s.group for s in all_settings()))


def test_schema_json_serialises_every_kind(bare: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``Path`` 与 ``time`` 不是 JSON 类型，必须转掉——否则整条命令会抛异常。"""
    assert _run("config", "schema", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    by_key = {item["key"]: item for item in payload["settings"]}
    assert by_key["core.data_dir"]["default"] == "data"
    assert by_key["storage.db_path"]["default"] == str(Path("data/alterego.db"))
    assert by_key["disturb_budget.quiet_hours"]["default"] == ["23:30", "08:00"]
    assert all(item["kind"] in {str(kind) for kind in SettingKind} for item in payload["settings"])


def test_schema_json_marks_the_enum_choices(bare: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("config", "schema", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    by_key = {item["key"]: item for item in payload["settings"]}
    assert [choice["value"] for choice in by_key["simulation.mode"]["choices"]] == [
        "realtime",
        "fast",
        "turbo",
    ]
    assert all(choice["consequence"] for choice in by_key["simulation.mode"]["choices"])
    # schema 不读配置文件，所以「当前值」一律是 None，而不是悄悄用默认值顶上。
    assert by_key["simulation.mode"]["current"] is None


def test_schema_json_carries_the_flags(bare: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("config", "schema", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    by_key = {item["key"]: item for item in payload["settings"]}
    assert by_key["storage.db_path"]["danger"] is True
    assert by_key["storage.db_path"]["requires_restart"] is True
    assert by_key["core.data_dir"]["requires_restart"] is True
    assert by_key["disturb_budget.daily_message_limit"]["requires_restart"] is False
    assert all(item["annotated"] for item in payload["settings"])

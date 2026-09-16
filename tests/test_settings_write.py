"""设置写回：文本替换、渲染、原子落盘。

这一组测试守着三条不能再退的底线：

1. **改一个键不能碰别的地方**。用户的注释、空行、键的顺序、等号两边的空格，
   全都要原样留着。重写整个文件会抹掉它们，而用户不会把「注释全没了」理解成
   「保存成功」。（``docs/design/10-settings-center.md`` § 5.3）
2. **落盘前用加载时同一个校验器验一遍**。新值不合法时，原文件**一个字都没动**，
   也不留下临时文件。
3. **写不进去的项要拒绝，而不是写进去再说**。密钥与整段表都是这种。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from alterego.kernel.config import Config
from alterego.kernel.errors import ConfigError
from alterego.kernel.settings import Choice, Setting, SettingKind
from alterego.kernel.settings_catalog import get_setting_metadata, section_paths
from alterego.kernel.settings_write import (
    patch_text,
    render_value,
    save_setting,
    toml_literal,
)


# ═══════════════════════════════════════════════════════════════
#  测试用的元数据与配置文件
# ═══════════════════════════════════════════════════════════════


def _setting(key: str) -> Setting:
    """从真实目录里取一条元数据。用它而不是手搓的，是为了测的就是真值。"""
    cls = section_paths[key.rpartition(".")[0]]
    found = get_setting_metadata(cls, key.rpartition(".")[2])
    assert found is not None, f"{key} 在元数据目录里不存在"
    return found


LIMIT = _setting("disturb_budget.daily_message_limit")  # int, 下限 0，无上限
PORT = _setting("web.port")  # int, 1..65535（这一项才有上限）
DECISION = _setting("llm.routing.decision")  # enum: strong / cheap
DATA_DIR = _setting("core.data_dir")  # path
QUIET = _setting("disturb_budget.quiet_hours")  # list
LOG_LEVEL = _setting("core.log_level")  # str


CONFIG_TOML = """\
# 拟我 · 配置文件
# 这个文件是要提交的，所以这里不能出现任何密钥。

[core]
log_level = "INFO"          # DEBUG 会打出每次模型调用的原文
timezone = "Asia/Shanghai"

[disturb_budget]
daily_message_limit = 3     # 一天最多主动发几条
quiet_hours = ["23:30", "08:00"]

[llm.routing]
decision = "strong"
expression = "strong"
"""


def _write(tmp_path: Path, text: str = CONFIG_TOML, name: str = "alterego.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _load(path: Path) -> Config:
    return Config.load(path=path, env={})


# ═══════════════════════════════════════════════════════════════
#  render_value
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("raw", ["true", "TRUE", "yes", "on", "1"])
def test_bool_true(raw: str) -> None:
    assert render_value(_setting("settings.auto_backup"), raw) == "true"


@pytest.mark.parametrize("raw", ["false", "False", "no", "off", "0"])
def test_bool_false(raw: str) -> None:
    assert render_value(_setting("settings.auto_backup"), raw) == "false"


def test_bool_rejects_whatever_else() -> None:
    with pytest.raises(ConfigError) as caught:
        render_value(_setting("settings.auto_backup"), "开")
    assert "true 或 false" in str(caught.value)


def test_int_is_rendered_without_a_decimal_point() -> None:
    """整数写成 ``3`` 而不是 ``3.0``：TOML 里那是两种类型。"""
    assert render_value(LIMIT, " 5 ") == "5"


def test_int_rejects_a_fraction() -> None:
    with pytest.raises(ConfigError, match="整数"):
        render_value(LIMIT, "2.5")


def test_int_respects_the_range_from_the_metadata() -> None:
    with pytest.raises(ConfigError) as caught:
        render_value(PORT, "99999")
    assert "不能大于 65535" in str(caught.value)
    # 提示里带上 effect：用户看到的不是「越界了」，而是「越界之后会发生什么」。
    assert PORT.effect in str(caught.value)


def test_int_respects_the_lower_bound() -> None:
    with pytest.raises(ConfigError) as caught:
        render_value(LIMIT, "-1")
    assert "不能小于 0" in str(caught.value)


def test_float_keeps_the_decimal_point() -> None:
    assert render_value(_setting("persona.evolution_max_field_delta"), "1") == "1.0"
    assert render_value(_setting("persona.evolution_max_field_delta"), "0.15") == "0.15"


def test_float_rejects_a_word() -> None:
    with pytest.raises(ConfigError, match="数字"):
        render_value(_setting("persona.evolution_max_field_delta"), "一点点")


def test_float_rejects_infinity() -> None:
    """``inf`` / ``nan`` 不是「很大的数」，它们会让后面所有比较都变成 False。

    用一项**没有上下限**的浮点来测：有上限时先被范围挡住，这道守卫就走不到。
    """
    unbounded = Setting(
        key="persona.some_ratio",
        label="某个比例",
        description="一项没有上下限的浮点设置。",
        kind=SettingKind.FLOAT,
        default=0.5,
        group="人设",
        effect="调高会让那一维变化得更烈。",
    )
    with pytest.raises(ConfigError, match="有限"):
        render_value(unbounded, "inf")
    with pytest.raises(ConfigError, match="有限"):
        render_value(unbounded, "nan")


def test_enum_accepts_a_known_value() -> None:
    assert render_value(DECISION, "cheap") == '"cheap"'


def test_enum_lists_the_alternatives() -> None:
    with pytest.raises(ConfigError) as caught:
        render_value(DECISION, "smart")
    message = str(caught.value)
    assert "strong" in message
    assert "cheap" in message
    assert DECISION.label in message


def test_list_is_comma_separated() -> None:
    assert render_value(QUIET, "23:30, 08:00") == '["23:30", "08:00"]'


def test_list_accepts_empty() -> None:
    assert render_value(QUIET, "") == "[]"


def test_path_is_quoted() -> None:
    assert render_value(DATA_DIR, r"C:\data") == '"C:\\\\data"'


def test_string_escapes_quotes_and_backslashes() -> None:
    assert toml_literal('a"b') == '"a\\"b"'
    assert toml_literal("a\\b") == '"a\\\\b"'
    assert toml_literal("你好") == '"你好"'


def test_secret_is_refused() -> None:
    """密钥不写进配置文件——那个文件是要提交的（§ 5.2）。"""
    secret = Setting(
        key="llm.providers.deepseek.api_key",
        label="接口密钥",
        description="调模型用的密钥。",
        kind=SettingKind.SECRET,
        group="模型",
        effect="换了密钥就会用新账号计费。",
    )
    with pytest.raises(ConfigError) as caught:
        render_value(secret, "sk-123")
    assert "环境变量" in str(caught.value)
    assert "git" in str(caught.value)


def test_mapping_is_refused() -> None:
    """整段表不是「一个值」：它每一行都是一个独立的设置项。"""
    mapping = Setting(
        key="llm.providers",
        label="模型供应商",
        description="每个供应商一段。",
        kind=SettingKind.MAPPING,
        group="模型",
        effect="加一段就多一个可用的供应商。",
    )
    with pytest.raises(ConfigError, match="一整段表"):
        render_value(mapping, "{a=1}")


def test_render_value_shape_of_a_string_field() -> None:
    """``core.user_name`` 是普通字符串，所以任何字符串都放行。

    拦住它的是配置类的 ``__post_init__``，不是元数据——"""
    assert render_value(_setting("core.user_name"), "阿岚") == '"阿岚"'


# ═══════════════════════════════════════════════════════════════
#  patch_text —— 改一行，别的一个字节都不动
# ═══════════════════════════════════════════════════════════════


def test_patch_replaces_the_value_and_keeps_the_comment() -> None:
    updated = patch_text(CONFIG_TOML, "disturb_budget.daily_message_limit", "5")
    assert "daily_message_limit = 5     # 一天最多主动发几条" in updated
    assert "daily_message_limit = 3" not in updated


def test_patch_keeps_everything_else_byte_for_byte() -> None:
    """除了那一行，其余每一行都必须还是原来的样子。"""
    updated = patch_text(CONFIG_TOML, "core.log_level", '"DEBUG"')
    before = CONFIG_TOML.splitlines()
    after = updated.splitlines()
    assert len(before) == len(after)
    changed = [
        index for index, pair in enumerate(zip(before, after, strict=True)) if pair[0] != pair[1]
    ]
    assert len(changed) == 1, f"多改了这些行：{changed}"
    assert after[changed[0]] == 'log_level = "DEBUG"          # DEBUG 会打出每次模型调用的原文'


def test_patch_handles_a_dotted_key_written_at_the_top_of_a_section() -> None:
    """``[core]`` 段里写 ``timezone = ...`` 与顶层写 ``core.timezone = ...`` 等价。"""
    text = 'core.timezone = "Asia/Shanghai"\n\n[core]\nlog_level = "INFO"\n'
    updated = patch_text(text, "core.timezone", '"UTC"')
    assert updated == 'core.timezone = "UTC"\n\n[core]\nlog_level = "INFO"\n'


def test_patch_finds_a_dotted_key_inside_another_section() -> None:
    """``[llm]`` 段里写 ``routing.decision = ...`` 也要认得出来。

    认不出来会补出第二个 ``decision``，于是文件里同一个键出现两次，
    下次启动直接报 TOML 非法。这是这一条测试存在的全部理由。
    """
    text = '[llm]\nrouting.decision = "strong"\n'
    updated = patch_text(text, "llm.routing.decision", '"cheap"')
    assert updated == '[llm]\nrouting.decision = "cheap"\n'
    assert updated.count("routing.decision") == 1


def test_patch_inserts_missing_key_at_the_end_of_its_section() -> None:
    """段在、键不在：补在**这一段末尾**，不是文件末尾。"""
    text = '[core]\nlog_level = "INFO"\n\n[llm]\nmax_retries = 3\n'
    updated = patch_text(text, "core.locale", '"zh_CN"')
    assert updated == '[core]\nlog_level = "INFO"\nlocale = "zh_CN"\n\n[llm]\nmax_retries = 3\n'


def test_patch_inserts_a_missing_section_at_the_end() -> None:
    text = '[core]\nlog_level = "INFO"\n'
    updated = patch_text(text, "settings.auto_backup", "true")
    assert updated == '[core]\nlog_level = "INFO"\n\n[settings]\nauto_backup = true\n'


def test_patch_does_not_append_a_blank_line_to_an_empty_file() -> None:
    updated = patch_text("", "settings.auto_backup", "true")
    assert updated == "[settings]\nauto_backup = true"


def test_patch_replaces_a_multi_line_array_as_a_whole() -> None:
    """跨行数组整体算一条语句。只换第一行会留下几行孤儿，文件就非法了。"""
    text = '[dataset]\nformats = [\n  "chat",\n  "alpaca",\n]\n\n[core]\nlog_level = "INFO"\n'
    updated = patch_text(text, "dataset.formats", '["chat"]')
    assert updated == '[dataset]\nformats = ["chat"]\n\n[core]\nlog_level = "INFO"\n'
    assert "alpaca" not in updated


def test_patch_keeps_crlf() -> None:
    text = '[core]\r\nlog_level = "INFO"\r\n'
    updated = patch_text(text, "core.log_level", '"DEBUG"')
    assert updated == '[core]\r\nlog_level = "DEBUG"\r\n'
    assert "\n" not in updated.replace("\r\n", "")


def test_patch_respects_a_file_without_a_trailing_newline() -> None:
    updated = patch_text('[core]\nlog_level = "INFO"', "core.log_level", '"DEBUG"')
    assert updated == '[core]\nlog_level = "DEBUG"'


def test_patch_is_pure() -> None:
    """同样的输入永远得到同样的输出——它是纯函数，不碰文件系统。"""
    assert patch_text(CONFIG_TOML, "core.log_level", '"DEBUG"') == patch_text(
        CONFIG_TOML, "core.log_level", '"DEBUG"'
    )


def test_patch_refuses_a_key_without_a_section() -> None:
    with pytest.raises(ConfigError, match="段名"):
        patch_text(CONFIG_TOML, "log_level", '"DEBUG"')


def test_patch_output_is_still_valid_toml() -> None:
    """改完之后必须是合法 TOML，而且改的那个键真的是新值。"""
    once = patch_text(CONFIG_TOML, "core.log_level", '"DEBUG"')
    updated = patch_text(once, "settings.auto_backup", "false")
    document: dict[str, Any] = tomllib.loads(updated)
    assert document["core"]["log_level"] == "DEBUG"
    assert document["settings"]["auto_backup"] is False
    assert document["llm"]["routing"]["decision"] == "strong"


# ═══════════════════════════════════════════════════════════════
#  save_setting —— 原子落盘
# ═══════════════════════════════════════════════════════════════


def test_save_round_trip(tmp_path: Path) -> None:
    path = _write(tmp_path)
    config = save_setting(path, LIMIT.key, "5", setting=LIMIT)

    assert config.disturb_budget.daily_message_limit == 5
    text = path.read_text(encoding="utf-8")
    assert "daily_message_limit = 5     # 一天最多主动发几条" in text
    assert "# 拟我 · 配置文件" in text, "注释必须还在"
    assert not path.with_name(path.name + ".tmp").exists()


def test_save_keeps_a_backup(tmp_path: Path) -> None:
    path = _write(tmp_path)
    save_setting(path, LIMIT.key, "5", setting=LIMIT)
    backup = path.with_name(path.name + ".bak")
    assert backup.read_text(encoding="utf-8") == CONFIG_TOML


def test_save_can_skip_the_backup(tmp_path: Path) -> None:
    path = _write(tmp_path)
    save_setting(path, LIMIT.key, "5", setting=LIMIT, backup=False)
    assert not path.with_name(path.name + ".bak").exists()


def test_save_leaves_the_file_alone_when_the_metadata_says_no(tmp_path: Path) -> None:
    """元数据里的范围先拦一道：文件一个字节都不能动，临时文件也不能留。"""
    path = _write(tmp_path)
    with pytest.raises(ConfigError):
        save_setting(path, PORT.key, "99999", setting=PORT)
    assert path.read_text(encoding="utf-8") == CONFIG_TOML
    assert not path.with_name(path.name + ".tmp").exists()


def test_save_leaves_the_file_alone_when_the_loader_says_no(tmp_path: Path) -> None:
    """元数据拦不住的，由**加载时同一个**校验器拦。

    这条测试要证明的是「写进去的那份内容被真的加载过一遍」，而不是
    「我们又实现了一套校验」。所以用的是**跨字段**的那种非法：
    ``daily_message_limit`` 单独看完全合法（0 以上），但 ``DisturbBudgetConfig``
    要求紧急上限不低于日常上限，而默认的紧急上限是 5。
    """
    path = _write(tmp_path)
    with pytest.raises(ConfigError, match="紧急上限不能低于普通上限"):
        save_setting(path, LIMIT.key, "7", setting=LIMIT)
    assert path.read_text(encoding="utf-8") == CONFIG_TOML
    assert not path.with_name(path.name + ".tmp").exists()


def test_save_creates_a_missing_section(tmp_path: Path) -> None:
    """段不存在时补一段，补完之后要能真的加载起来。

    ``dataset.formats`` 是最难的那种：它补出来的值是列表，而 ``DatasetConfig``
    的 ``__post_init__`` 会拿它跟 ``domain.dataset.FORMATS`` 比对。
    """
    path = _write(tmp_path)
    setting = _setting("dataset.formats")
    config = save_setting(path, setting.key, "chat, alpaca", setting=setting)
    assert list(config.dataset.formats) == ["chat", "alpaca"]
    text = path.read_text(encoding="utf-8")
    assert "[dataset]" in text
    assert 'formats = ["chat", "alpaca"]' in text


def test_save_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        save_setting(tmp_path / "nope.toml", LIMIT.key, "5", setting=LIMIT)
    assert "alterego init" in str(caught.value)


def test_save_reports_a_broken_file(tmp_path: Path) -> None:
    """文件已经被手改坏了：先报「你的文件现在读不了」，别拿坏文本继续改。"""
    path = _write(tmp_path, '[core\nlog_level = "INFO"\n')
    with pytest.raises(ConfigError, match="不是合法 TOML"):
        save_setting(path, LIMIT.key, "5", setting=LIMIT)
    assert path.read_text(encoding="utf-8") == '[core\nlog_level = "INFO"\n'


def test_save_reports_a_path_that_is_not_a_file(tmp_path: Path) -> None:
    """目录当成文件传进来：报成「这个路径不是一个文件」，而不是一个 OSError。"""
    directory = tmp_path / "conf.toml"
    directory.mkdir()
    with pytest.raises(ConfigError, match="不是一个文件"):
        save_setting(directory, LIMIT.key, "5", setting=LIMIT)


def test_save_reports_an_unreadable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """权限等造成的读失败：报成配置错误，不要抛一个 OSError 上去。"""
    path = _write(tmp_path)

    def explode(self: Path, **kwargs: Any) -> str:
        raise PermissionError("被占用了")

    monkeypatch.setattr(Path, "read_text", explode)
    with pytest.raises(ConfigError, match="无法读取"):
        save_setting(path, LIMIT.key, "5", setting=LIMIT)


def test_save_returns_the_reloaded_config(tmp_path: Path) -> None:
    """返回的是**重新加载**出来的配置，不是原地改过的那一份。

    原地改会绕过 ``_collapse_extras()`` 与 ``__post_init__``，是「假生效」。
    """
    path = _write(tmp_path)
    config = save_setting(path, "core.user_name", "阿岚", setting=_setting("core.user_name"))
    assert config.core.user_name == "阿岚"
    assert _load(path).core.user_name == "阿岚"


def test_save_writes_a_value_the_loader_reads_back_identically(tmp_path: Path) -> None:
    path = _write(tmp_path)
    save_setting(path, QUIET.key, "22:00, 07:30", setting=QUIET)
    assert [item.strftime("%H:%M") for item in _load(path).disturb_budget.quiet_hours] == [
        "22:00",
        "07:30",
    ]


def test_choices_are_used_verbatim() -> None:
    """枚举的字面量不加引号以外的东西：`strong` 就写成 `"strong"`。"""
    choice = Choice(value="degrade", label="降级", consequence="改用便宜模型继续跑。")
    setting = Setting(
        key="llm.budget.on_exceed",
        label="超出预算怎么办",
        description="当日花费超过上限之后的行为。",
        kind=SettingKind.ENUM,
        default="degrade",
        group="预算",
        choices=(choice,),
        effect="改成 stop 之后它会直接停下，不再回你消息。",
    )
    assert render_value(setting, "degrade") == '"degrade"'

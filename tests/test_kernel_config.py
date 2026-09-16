"""内核配置系统的行为测试。

这些用例是本项目的「配置契约」：改了 ``config.py`` 或改了
``defaults.toml`` / ``templates/alterego.toml``，这里必须仍然通过。
"""

from __future__ import annotations

import logging
from dataclasses import FrozenInstanceError
from datetime import time
from pathlib import Path

import pytest

from alterego.domain.dataset import FORMATS
from alterego.kernel.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_CONFIG_PATHS,
    Config,
    DisturbBudgetConfig,
    LLMRoutingConfig,
    SimulationConfig,
    WebConfig,
)
from alterego.kernel.errors import ConfigError


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "templates" / "alterego.toml"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "alterego.toml"
    path.write_text(text, encoding="utf-8")
    return path


# ═══════════════════════════════════════════════════════════════════════
#  默认值与随包数据
# ═══════════════════════════════════════════════════════════════════════


def test_load_without_any_file_still_works() -> None:
    """没有配置文件时用默认值静默继续——绝大多数命令不该强制 init。"""
    config = Config.load()
    assert config.simulation.mode == "realtime"
    assert config.disturb_budget.daily_message_limit == 3
    assert config.source is None


def test_explicit_missing_path_is_an_error(tmp_path: Path) -> None:
    """显式指定的路径不存在时直接报错，不静默换用默认值。"""
    with pytest.raises(ConfigError) as excinfo:
        Config.load(path=tmp_path / "nope.toml")
    assert excinfo.value.context["path"].endswith("nope.toml")


def test_missing_file_can_be_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``alterego init`` 之外不该强制要求配置文件，但 init 自己要知道去哪找。"""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError) as excinfo:
        Config.load(require_file=True)
    assert excinfo.value.context["searched"] == [str(p) for p in DEFAULT_CONFIG_PATHS]


def test_packaged_defaults_file_ships_with_the_package() -> None:
    """``defaults.toml`` 是发行版的选型声明，丢了会让默认值静默变空。"""
    assert DEFAULT_CONFIG_PATH.is_file(), f"缺少随包默认值：{DEFAULT_CONFIG_PATH}"


def test_packaged_defaults_supply_the_implementation_choices() -> None:
    """内核代码里不许写具体技术名，这些值只能由数据文件提供（P1）。"""
    config = Config.load()
    assert config.llm.default_provider == "openai_compatible"
    assert config.storage.backend == "sqlite"
    assert config.llm.routing.strong == "openai_compatible"
    assert config.llm.routing.cheap == "openai_compatible"


def test_user_config_overrides_packaged_defaults() -> None:
    config = Config.load(
        overrides={"llm": {"default_provider": "my_provider"}, "storage": {"backend": "me"}}
    )
    assert config.llm.default_provider == "my_provider"
    assert config.storage.backend == "me"


def test_nested_sections_are_real_dataclasses() -> None:
    """``from __future__ import annotations`` 下很容易悄悄退化成 dict。

    退化了不会报错，只会让 ``config.simulation.mode`` 变成下标访问——
    所以在这里钉死。
    """
    config = Config.load()
    assert isinstance(config.simulation, SimulationConfig)
    assert isinstance(config.llm.routing, LLMRoutingConfig)
    assert isinstance(config.web, WebConfig)


def test_config_is_frozen() -> None:
    config = Config.load()
    with pytest.raises(FrozenInstanceError):
        config.core.log_level = "DEBUG"  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════
#  加载优先级
# ═══════════════════════════════════════════════════════════════════════


def test_env_beats_file(tmp_path: Path) -> None:
    path = _write(tmp_path, '[core]\nlog_level = "WARNING"\n')
    config = Config.load(path=path, env={"ALTEREGO_CORE__LOG_LEVEL": "DEBUG"})
    assert config.core.log_level == "DEBUG"
    assert config.source == path


def test_overrides_beat_env() -> None:
    config = Config.load(
        overrides={"core": {"log_level": "ERROR"}},
        env={"ALTEREGO_CORE__LOG_LEVEL": "DEBUG"},
    )
    assert config.core.log_level == "ERROR"


def test_double_underscore_env_maps_to_nesting() -> None:
    config = Config.load(
        env={
            "ALTEREGO_SIMULATION__MODE": "fast",
            "ALTEREGO_WEB__PORT": "9000",
            "ALTEREGO_PLUGINS__AUTO_RELOAD": "true",
        }
    )
    assert config.simulation.mode == "fast"
    assert config.web.port == 9000
    assert config.plugins.auto_reload is True


def test_single_level_env_is_ignored_not_guessed() -> None:
    """``ALTEREGO_XXX`` 定位不到配置段，只能忽略——猜错比不猜更糟。"""
    config = Config.load(env={"ALTEREGO_SOMETHING": "1"})
    assert config.unknown_keys == ()


def test_string_values_are_coerced_from_env() -> None:
    """环境变量全是字符串，``"8765"`` 必须变成 ``int`` 而不是一路裸奔。"""
    config = Config.load(env={"ALTEREGO_WEB__PORT": "9123"})
    assert config.web.port == 9123
    assert isinstance(config.web.port, int)


# ═══════════════════════════════════════════════════════════════════════
#  TOML 解析与错误
# ═══════════════════════════════════════════════════════════════════════


def test_broken_toml_reports_the_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "[core\nlog_level = 1\n")
    with pytest.raises(ConfigError) as excinfo:
        Config.load(path=path)
    assert excinfo.value.context["path"] == str(path)


def test_missing_env_reference_is_an_error() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Config.load(env={}, overrides={"llm": {"providers": {"p": {"api_key": "${NOPE}"}}}})
    assert "NOPE" in str(excinfo.value)


def test_env_reference_is_resolved() -> None:
    config = Config.load(
        env={"MY_KEY": "abc123"},
        overrides={"llm": {"providers": {"p": {"api_key": "${MY_KEY}"}}}},
    )
    assert config.llm.providers["p"]["api_key"] == "abc123"  # type: ignore[index]


# ═══════════════════════════════════════════════════════════════════════
#  ``[<section>.<用户起的名字>]`` 的折叠
# ═══════════════════════════════════════════════════════════════════════


def test_named_sections_collapse_into_their_containers(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
[channels.file]
path = "data/outbox"

[llm.my_provider]
api_key = "k"

[plugins.config."my.plugin"]
threshold = 3
""",
    )
    config = Config.load(path=path, env={})
    assert config.unknown_keys == ()
    assert config.channels.options_for("file") == {"path": "data/outbox"}
    assert config.llm.providers["my_provider"] == {"api_key": "k"}  # type: ignore[index]
    assert config.plugins.config_for("my.plugin") == {"threshold": 3}


def test_unknown_top_level_key_only_warns() -> None:
    """插件可能需要内核不认识的键（P4），所以只记录不失败。"""
    config = Config.load(overrides={"telemetry": {"enabled": True}})
    assert config.unknown_keys == ("telemetry",)


def test_a_typo_inside_a_nested_section_is_reported(tmp_path: Path) -> None:
    """嵌套段里的拼写错误必须被抓到，用点分路径报出来。

    这里原先只看**顶层键**。于是 ``[llm.routing]`` 里的
    ``decision = "strong"`` 被敲成 ``decisionn = "strong"`` 会完全无声：
    键落在已知的 ``llm`` 段里，既不进 ``unknown_keys``，也不会被
    ``build_section`` 认领（后者只遍历 dataclass 自己的字段名），
    就这么消失了。用户改完配置、重启、行为照旧、还没有任何提示——
    正是 ``07-model-routing-and-media.md`` § 3.1 列为不可接受的「静默失效」。

    修复后 ``unknown_keys`` 递归到嵌套段。断言的第二行是重点：
    路由值仍然是**默认的** ``"strong"``，也就是说那句 typo 确实一个字都没生效。
    """
    path = _write(tmp_path, '[llm.routing]\ndecisionn = "strong"\n')
    config = Config.load(path=path, env={})
    assert config.unknown_keys == ("llm.routing.decisionn",)
    assert config.llm.routing.decision == LLMRoutingConfig().decision


def test_every_nested_section_reports_its_own_stray_keys(tmp_path: Path) -> None:
    """递归得走到底：两层的段和只剩一层的段都要报，路径要能直接指出改哪一行。

    能被递归进去的只有**注解是 dataclass 的字段**，目前是这三个：
    ``llm.routing`` / ``llm.budget`` / ``routing.filters``。
    直接写在 ``[llm]`` 下面的未知键走的是另一条路（被 :func:`_collapse_extras`
    折进 ``llm.providers``，当成插件键），因此不该出现在这里。
    """
    path = _write(
        tmp_path,
        """
[llm.routing]
persona = "strong"
personas = "strong"

[llm.budget]
daily_usd_limit = 2.0
daily_usd_limits = 2.0

[routing.filters]
suppress_quiet_hours = true
suppress_quiet_hour = true
""",
    )
    config = Config.load(path=path, env={})
    assert config.unknown_keys == (
        "llm.routing.personas",
        "llm.budget.daily_usd_limits",
        "routing.filters.suppress_quiet_hour",
    )
    assert config.llm.routing.persona == "strong"
    assert config.routing.filters.suppress_quiet_hours is True


def test_unknown_keys_are_logged_and_never_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """字段文档说「只告警，不失败」——那就得真的有告警。

    ``unknown_keys`` 曾经是个只写不读的字段：除了测试没人看它，
    于是「只告警」实际上是「不告警」。这条测试把承诺钉住。
    """
    path = _write(tmp_path, "[telemetry]\nenabled = true\n")
    with caplog.at_level(logging.WARNING, logger="alterego.kernel.config"):
        config = Config.load(path=path, env={})
    assert config.unknown_keys == ("telemetry",)
    assert any("telemetry" in record.getMessage() for record in caplog.records)
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_plugin_owned_keys_are_not_reported_as_unknown(tmp_path: Path) -> None:
    """开放的容器段不参与递归——里面的键归插件解释，内核无从判断。

    ``llm.providers`` / ``channels.options`` / ``plugins.config`` 都是
    ``Mapping[str, Any]``。如果递归不看注解类型，插件写的每一个键都会
    变成一条假告警，那比不告警更糟。
    """
    path = _write(
        tmp_path,
        """
[llm.providers.mine]
api_key = "k"
weird_plugin_only_key = 1

[channels.file]
path = "data/outbox"

[plugins.config."my.plugin"]
anything = [1, 2, 3]
""",
    )
    config = Config.load(path=path, env={})
    assert config.unknown_keys == ()


def test_options_for_unknown_channel_is_empty() -> None:
    assert Config.load().channels.options_for("nope") == {}


def test_config_for_unknown_plugin_is_empty() -> None:
    assert Config.load().plugins.config_for("nope.plugin") == {}


def test_the_name_it_calls_you_by_is_configurable() -> None:
    """提示词里不写「用户」而写它给你起的名字——所以这个值不能是空的。"""
    assert Config.load().core.user_name == "你"
    config = Config.load(overrides={"core": {"user_name": "小满"}})
    assert config.core.user_name == "小满"


# ═══════════════════════════════════════════════════════════════════════
#  校验
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"core": {"log_level": "VERBOSE"}}, id="log_level"),
        pytest.param({"core": {"timezone": "Not/AZone"}}, id="timezone"),
        pytest.param({"core": {"user_name": "   "}}, id="user_name_blank"),
        pytest.param({"simulation": {"mode": "warpspeed"}}, id="mode"),
        pytest.param({"simulation": {"tick_interval_minutes": 0}}, id="tick_interval_zero"),
        pytest.param(
            {"simulation": {"mode": "turbo", "tick_interval_minutes": 5}}, id="turbo_too_fine"
        ),
        pytest.param({"disturb_budget": {"daily_message_limit_urgent": 1}}, id="urgent_below"),
        pytest.param({"disturb_budget": {"quiet_hours": ["08:00", "08:00"]}}, id="quiet_identical"),
        pytest.param({"llm": {"timeout_seconds": 0}}, id="timeout_zero"),
        pytest.param({"llm": {"budget": {"monthly_usd_limit": 1.0}}}, id="monthly_below_daily"),
        pytest.param({"llm": {"budget": {"on_exceed": "explode"}}}, id="on_exceed"),
        pytest.param({"persona": {"evolution_max_field_delta": 0.0}}, id="delta_zero"),
        pytest.param({"web": {"port": 70000}}, id="port"),
        pytest.param({"web": {"auth": "none", "host": "0.0.0.0"}}, id="open_listener"),
        pytest.param({"storage": {"journal_mode": "ROCKS"}}, id="journal_mode"),
        pytest.param({"retention": {"tick_log_keep_days": 0}}, id="retention"),
        pytest.param({"channels": {"enabled": ["a", "a"]}}, id="duplicate_channel"),
        pytest.param(
            {"plugins": {"auto_reload": True, "auto_reload_interval_seconds": 0.1}},
            id="reload_too_fast",
        ),
        pytest.param({"routing": {"level": "sometimes"}}, id="routing_level"),
    ],
)
def test_invalid_values_are_rejected_at_build_time(overrides: dict[str, object]) -> None:
    """宁可启动时退出码 2，也不要跑了一半才发现配置写错。"""
    with pytest.raises(ConfigError) as excinfo:
        Config.load(env={}, overrides=overrides)
    assert excinfo.value.code == "config_error"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param({"simulation": 3}, "配置段必须是表", id="section_is_a_scalar"),
        pytest.param({"core": {"log_level": 1}}, "期望字符串", id="string"),
        pytest.param({"core": {"user_name": 7}}, "期望字符串", id="user_name"),
        pytest.param({"simulation": {"tick_interval_minutes": "soon"}}, "期望整数", id="integer"),
        pytest.param({"llm": {"budget": {"monthly_usd_limit": "一分钱"}}}, "期望数字", id="number"),
        pytest.param({"web": {"enabled": "maybe"}}, "期望布尔值", id="boolean"),
        pytest.param(
            {"disturb_budget": {"quiet_hours": ["深夜", "08:00"]}}, "期望 HH:MM", id="time"
        ),
    ],
)
def test_a_wrong_type_is_named_at_build_time(overrides: dict[str, object], message: str) -> None:
    """类型写错要当场点名——拖到推演中途才 TypeError 就没人知道是哪一行配置。"""
    with pytest.raises(ConfigError) as excinfo:
        Config.load(env={}, overrides=overrides)

    assert message in str(excinfo.value)


def test_a_boolean_written_as_a_string_is_still_accepted() -> None:
    """环境变量全是字符串：``ALTEREGO_WEB__ENABLED=false`` 得能认出来。"""
    config = Config.load(env={"ALTEREGO_WEB__ENABLED": "false"})

    assert config.web.enabled is False


def test_a_seed_of_none_means_every_run_differs() -> None:
    """``random_seed = null`` 是「不复现」的显式写法，不能被当成「没写」。"""
    config = Config.load(env={}, overrides={"core": {"random_seed": None}})

    assert config.core.random_seed is None


def test_localhost_without_auth_is_allowed() -> None:
    """本机监听关掉认证是合理的——只是不能暴露到局域网。"""
    config = Config.load(overrides={"web": {"auth": "none", "host": "127.0.0.1"}})
    assert config.web.auth == "none"


# ═══════════════════════════════════════════════════════════════════════
#  静默时段
# ═══════════════════════════════════════════════════════════════════════


def test_quiet_hours_handle_midnight_crossing() -> None:
    config = DisturbBudgetConfig(quiet_hours=(time(23, 30), time(8, 0)))
    assert config.in_quiet_hours(time(23, 45)) is True
    assert config.in_quiet_hours(time(3, 0)) is True
    assert config.in_quiet_hours(time(8, 0)) is False
    assert config.in_quiet_hours(time(12, 0)) is False
    assert config.in_quiet_hours(time(23, 0)) is False


def test_quiet_hours_within_one_day() -> None:
    config = DisturbBudgetConfig(quiet_hours=(time(1, 0), time(6, 0)))
    assert config.in_quiet_hours(time(3, 0)) is True
    assert config.in_quiet_hours(time(0, 59)) is False
    assert config.in_quiet_hours(time(6, 0)) is False


# ═══════════════════════════════════════════════════════════════════════
#  路由
# ═══════════════════════════════════════════════════════════════════════


def test_routing_resolves_one_level_of_indirection() -> None:
    routing = LLMRoutingConfig(strong="big", cheap="small")
    assert routing.resolve("decision") == "big"
    assert routing.resolve("reflection") == "small"


def test_routing_passes_through_a_direct_provider_name() -> None:
    routing = LLMRoutingConfig(decision="direct")
    assert routing.resolve("decision") == "direct"


def test_routing_rejects_unknown_purpose() -> None:
    """静默回落会让「我明明配了没生效」变成玄学。"""
    with pytest.raises(ConfigError) as excinfo:
        Config.load().llm.routing.resolve("telepathy")
    assert "decision" in excinfo.value.context["supported"]


# ═══════════════════════════════════════════════════════════════════════
#  派生路径与副作用
# ═══════════════════════════════════════════════════════════════════════


def test_derived_paths(tmp_path: Path) -> None:
    config = Config.load(
        env={},
        overrides={"core": {"data_dir": str(tmp_path / "d")}, "storage": {"db_path": "a.db"}},
    )
    assert config.data_dir == tmp_path / "d"
    assert config.database_path == tmp_path / "d" / "a.db"
    assert config.backups_dir == tmp_path / "d" / "backups"
    assert config.outbox_dir == tmp_path / "d" / "outbox"


def test_absolute_db_path_is_respected(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere" / "x.db"
    config = Config.load(env={}, overrides={"storage": {"db_path": str(target)}})
    assert config.database_path == target


def test_plugin_search_paths_are_absolute(tmp_path: Path) -> None:
    config = Config.load(env={}, overrides={"plugins": {"search_paths": ["plugins"]}})
    assert all(p.is_absolute() for p in config.plugin_search_paths)


def test_ensure_directories_creates_everything(tmp_path: Path) -> None:
    config = Config.load(env={}, overrides={"core": {"data_dir": str(tmp_path / "d")}})
    config.ensure_directories()
    assert config.data_dir.is_dir()
    assert config.backups_dir.is_dir()
    assert config.outbox_dir.is_dir()


# ═══════════════════════════════════════════════════════════════════════
#  展示与脱敏
# ═══════════════════════════════════════════════════════════════════════


def test_secrets_are_masked_by_default() -> None:
    """配置经常被贴到 issue 里。"""
    config = Config.load(
        env={},
        overrides={
            "llm": {"providers": {"p": {"api_key": "sk-real", "base_url": "https://x"}}},
            "channels": {"dingtalk_webhook": {"webhook_url": "https://secret"}},
        },
    )
    dumped = config.redacted()
    assert dumped["llm"]["providers"]["p"]["api_key"] == "***"
    assert dumped["llm"]["providers"]["p"]["base_url"] == "https://x"
    assert dumped["channels"]["options"]["dingtalk_webhook"]["webhook_url"] == "***"


def test_redaction_can_be_turned_off() -> None:
    config = Config.load(env={}, overrides={"llm": {"providers": {"p": {"api_key": "sk-real"}}}})
    assert config.to_dict(redact=False)["llm"]["providers"]["p"]["api_key"] == "sk-real"


def test_dump_is_json_friendly() -> None:
    """``alterego config --json`` 直接 ``json.dumps``，不能出现 Path/datetime。"""
    dumped = Config.load().to_dict()
    assert isinstance(dumped["core"]["data_dir"], str)
    assert isinstance(dumped["disturb_budget"]["quiet_hours"], list)


# ═══════════════════════════════════════════════════════════════════════
#  与权威模板保持同步
# ═══════════════════════════════════════════════════════════════════════


def test_authoritative_template_covers_every_key() -> None:
    """``templates/alterego.toml`` 是所有配置项的权威参考。

    它一旦出现内核不认识的键，要么是模板写错了，要么是 ``config.py``
    漏了新字段——两种都必须当场发现。

    ⚠️ 这条断言在 ``unknown_keys`` 只会看顶层键的时候是**瞎的**：
    模板里 ``[llm.routing]`` 少了个字段、或者多写了一个不存在的用途，
    都测不出来。现在检测递归了，这句话才名副其实。
    """
    config = Config.load(path=TEMPLATE, env={})
    assert config.unknown_keys == ()
    assert config.source == TEMPLATE


def test_authoritative_template_loads_real_values() -> None:
    config = Config.load(path=TEMPLATE, env={})
    assert config.simulation.mode == "realtime"
    assert config.llm.budget.on_exceed == "degrade"
    assert config.channels.enabled == ("channel.file", "channel.web")


# ═══════════════════════════════════════════════════════════════════════
#  训练数据集
# ═══════════════════════════════════════════════════════════════════════


def test_the_format_names_are_a_mirror_of_the_domain() -> None:
    """``DATASET_FORMATS`` 必须与 ``domain.dataset.FORMATS`` 一模一样。

    内核不能 import domain（第 4 条红线：内核不引用任何上层模块），
    所以这份名单在内核里被抄了一遍。抄件与原件对不上时，症状是
    「配置填得出、内核也放行，但导出到一半才说没这种形状」——
    一个只有真跑一次才看得见的错。把两边钉在一起，改一边就红。

    它住在 ``config_values.py`` 而不是 ``config.py``：后者顶在 900/900
    （``scripts/check_architecture.sh`` 第 23 项），一个字的余量都没有。
    """
    from alterego.domain import dataset as domain_dataset
    from alterego.kernel.config_values import DATASET_FORMATS

    assert frozenset(domain_dataset.FORMATS) == DATASET_FORMATS
    # 顺序也要一致：README 用 formats[0] 当主形状，命令行的 choices
    # 也是按这个顺序展示的。
    assert tuple(sorted(DATASET_FORMATS)) == tuple(sorted(domain_dataset.FORMATS))


def test_dataset_defaults_are_conservative() -> None:
    """默认只出一种形状、不脱任何自定义词——多出几份是用户主动要的。"""
    config = Config.load(path=None, env={})

    assert config.dataset.export_dir == Path("exports/datasets")
    assert config.dataset.formats == ("chat",)
    assert config.dataset.redact_terms == ()
    assert config.dataset.lookback_days == 30


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"formats": []}, "formats 不能为空"),
        ({"formats": ["yaml"]}, "不认识的形状"),
        ({"redact_terms": ["", "  "]}, "空条目"),
    ],
)
def test_bad_dataset_options_are_rejected(overrides: dict[str, object], message: str) -> None:
    """每个坏值都要说清自己的后果，不能混成一句「配置非法」。

    形状一个都没选 → 一个文件都不会写，用户以为工具坏了；
    形状不认识 → 内核放行、导出到一半才炸；
    脱敏词是空的 → 用户以为脱过了，其实那条规则被静默跳过。
    """
    with pytest.raises(ConfigError) as caught:
        Config.load(path=None, env={}, overrides={"dataset": overrides})

    assert message in caught.value.message
    # 报错要带上「怎么办」：不是一句 hint，就是一份能用的清单。
    assert caught.value.context


def test_an_unknown_format_lists_what_is_supported() -> None:
    """报「不认识 yaml」时得顺手把认得的都列出来。

    只说「不认识」的话，用户要去翻文档才知道该填什么。
    """
    with pytest.raises(ConfigError) as caught:
        Config.load(path=None, env={}, overrides={"dataset": {"formats": ["yaml"]}})

    assert caught.value.context["unknown"] == ["yaml"]
    assert set(caught.value.context["supported"]) == set(FORMATS)


@pytest.mark.parametrize("bad", [0, -3])
def test_a_non_positive_lookback_is_rejected(bad: int) -> None:
    """回溯天数非正的话窗口是空的，导出结果永远是零条还不报错。"""
    with pytest.raises(ConfigError) as caught:
        Config.load(path=None, env={}, overrides={"dataset": {"lookback_days": bad}})

    # 这条复用内核的通用正数检查，所以名字在 context 里而不是消息里。
    assert caught.value.context["key"] == "lookback_days"
    assert caught.value.context["value"] == bad


def test_several_dataset_formats_are_kept_in_the_given_order() -> None:
    """多份形状按写的顺序来——README 拿第一个当主形状讲。"""
    config = Config.load(
        path=None,
        env={},
        overrides={"dataset": {"formats": ["alpaca", "chat"]}},
    )

    assert config.dataset.formats == ("alpaca", "chat")


def test_a_relative_export_dir_stays_relative() -> None:
    """相对路径不在内核里解析。

    「相对」相对的是**命令跑起来时的工作目录**，内核在导入期并不知道
    那个目录是哪儿。在这里 ``resolve()`` 会让配置的含义变成
    「相对内核被 import 的那一刻」，那是另一种、更说不清的路径——
    组装根 ``cli_dataset`` 才负责把它接上 ``Path.cwd()``。
    """
    config = Config.load(path=None, env={}, overrides={"dataset": {"export_dir": "here"}})

    assert config.dataset.export_dir == Path("here")
    assert not config.dataset.export_dir.is_absolute()


def test_an_absolute_export_dir_is_kept_verbatim(tmp_path: Path) -> None:
    config = Config.load(
        path=None, env={}, overrides={"dataset": {"export_dir": str(tmp_path / "out")}}
    )

    assert config.dataset.export_dir == tmp_path / "out"


# ═══════════════════════════════════════════════════════════════════════
#  [study] —— 专项学习
# ═══════════════════════════════════════════════════════════════════════


def test_study_defaults_learn_one_step_at_a_time() -> None:
    """默认一次只学一格。

    一次学完整个方向，就把「我一个星期前还不懂这个」这件事抹掉了，
    而那正是这个功能唯一能证明自己的东西。
    """
    config = Config.load(path=None, env={})

    assert config.study.field == ""
    assert config.study.rounds == 1
    assert config.study.recall_limit == 3
    assert config.study.min_score == 2.0


def test_a_blank_field_is_not_an_error() -> None:
    """方向留空是**有意义**的：意思是从人设的 ``occupation`` 里认。

    认不出来时由命令直接说「不知道该学什么」，而不是在内核里就拦下来——
    内核不知道人设里写了什么，也不知道命令行在干什么。
    """
    config = Config.load(path=None, env={}, overrides={"study": {"field": "  "}})

    assert config.study.field.strip() == ""


@pytest.mark.parametrize("bad_key", ["rounds", "recall_limit"])
@pytest.mark.parametrize("bad", [0, -1])
def test_a_non_positive_study_count_is_rejected(bad_key: str, bad: int) -> None:
    """``rounds = 0`` 会让 ``study next`` 什么都不学却报告成功。

    这比报错更糟：用户以为它学了，其实一格都没动，而且进度文件也没变，
    于是下次跑还是同一个结果——静默的空转。
    """
    with pytest.raises(ConfigError) as caught:
        Config.load(path=None, env={}, overrides={"study": {bad_key: bad}})

    assert caught.value.context["key"] == bad_key
    assert caught.value.context["value"] == bad


@pytest.mark.parametrize("bad_key", ["rounds", "recall_limit"])
def test_a_boolean_study_count_is_rejected(bad_key: str) -> None:
    """``rounds = true`` 不是「学一轮」的意思。

    Python 里 ``True`` 就是 ``1``，不特判的话它会被静默接受，
    而用户看到的是「学到了」，实际上什么差别都看不出来。
    """
    with pytest.raises(ConfigError) as caught:
        Config.load(path=None, env={}, overrides={"study": {bad_key: True}})

    assert caught.value.context["key"] == bad_key


def test_a_negative_score_threshold_is_rejected() -> None:
    """门槛是分数，分数没有负的。

    负数不会报错得那么明显：它比 0 还松，等于「随便什么都算命中」，
    于是每轮对话都被塞进一大堆不相干的专业笔记。
    """
    with pytest.raises(ConfigError) as caught:
        Config.load(path=None, env={}, overrides={"study": {"min_score": -1.0}})

    assert caught.value.context["key"] == "min_score"
    # 报「不能为负」的同时必须给出替代做法，否则用户只会把它填成 0——
    # 而那比不调用更糟。
    assert "recall_limit" in caught.value.context["hint"]


def test_a_zero_score_threshold_is_allowed() -> None:
    """0 是合法的：它是「把门槛关掉」，不是错误配置。

    拦住它只会让用户去写一个 0.000001，那更说不清。
    """
    config = Config.load(path=None, env={}, overrides={"study": {"min_score": 0}})

    assert config.study.min_score == 0


def test_study_config_is_frozen() -> None:
    """配置对象创建后不可改——改了它，下一次读配置的人看到的是另一份值。"""
    config = Config.load(path=None, env={})

    with pytest.raises(FrozenInstanceError):
        config.study.rounds = 5  # type: ignore[misc]

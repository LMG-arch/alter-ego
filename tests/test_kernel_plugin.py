"""内核插件层测试（一）：清单、配置字段、状态与上下文。

这一层是整个项目「零内核修改就能加功能」承诺的兑现处，所以错误信息本身也是被测对象——
用户看到的应该是一条能照着做的提示，而不是「插件清单非法」。
"""

from __future__ import annotations

import json
import logging
import random
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from alterego.kernel.bus import EventBus
from alterego.kernel.clock import FrozenClock
from alterego.kernel.errors import PluginError, PluginManifestError
from alterego.kernel.plugin import (
    API_VERSION,
    MASK,
    ConfigField,
    Plugin,
    PluginContext,
    PluginManifest,
    PluginPaths,
    PluginState,
    is_api_version_compatible,
    parse_duration,
    resolve_config,
)
from alterego.kernel.registry import ServiceRegistry
from alterego.kernel.scheduler import Scheduler


LOGGER = logging.getLogger("test.plugin")

BASE_MANIFEST: dict[str, Any] = {
    "id": "channel.file",
    "version": "0.1.0",
    "api_version": API_VERSION,
    "kind": "channel",
    "entry": "plugin:FileChannel",
}


def make_manifest(**overrides: Any) -> PluginManifest:
    data = dict(BASE_MANIFEST)
    data.update(overrides)
    return PluginManifest.parse(data)


def field(**kwargs: Any) -> ConfigField:
    return ConfigField(name=kwargs.pop("name", "value"), **kwargs)


# ── 时长解析 ────────────────────────────────────────────────


class TestParseDuration:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("30s", timedelta(seconds=30)),
            ("500ms", timedelta(seconds=0.5)),
            ("5m", timedelta(minutes=5)),
            ("2h", timedelta(hours=2)),
            ("1d", timedelta(days=1)),
            (" 90 m ", timedelta(minutes=90)),
        ],
    )
    def test_supported_units(self, text: str, expected: timedelta) -> None:
        assert parse_duration(text) == expected

    def test_timedelta_passes_through(self) -> None:
        given = timedelta(minutes=3)
        assert parse_duration(given) is given

    def test_bare_number_is_seconds(self) -> None:
        # 裸数字按秒解释，但这是笔误的高发区，所以一定要能用。
        assert parse_duration(90) == timedelta(seconds=90)

    @pytest.mark.parametrize("text", ["", "10", "3 hours", "1w", "abc"])
    def test_bad_text_is_rejected(self, text: str) -> None:
        with pytest.raises(ValueError):
            parse_duration(text)

    def test_non_string_is_rejected(self) -> None:
        with pytest.raises(TypeError):
            parse_duration(["10s"])


# ── 配置字段 ────────────────────────────────────────────────


class TestConfigFieldValidation:
    def test_defaults_to_string(self) -> None:
        spec = field()
        assert spec.type == "string"
        assert spec.required is False
        assert spec.secret is False

    def test_unknown_type_is_rejected(self) -> None:
        with pytest.raises(PluginManifestError) as caught:
            field(type="datetime")

        assert caught.value.context["field"] == "value"
        assert caught.value.context["type"] == "datetime"
        assert "duration" in caught.value.context["supported"]

    def test_unknown_item_type_is_rejected(self) -> None:
        with pytest.raises(PluginManifestError):
            field(type="array", item_type="datetime")

    def test_required_and_default_are_mutually_exclusive(self) -> None:
        # 两者同时存在时，「缺省值」到底算不算「已填」没人说得清。
        with pytest.raises(PluginManifestError) as caught:
            field(required=True, default="x")

        assert "互斥" in caught.value.context["hint"]

    def test_item_type_only_for_array(self) -> None:
        with pytest.raises(PluginManifestError):
            field(type="string", item_type="string")

    def test_redacted_line_mentions_secret(self) -> None:
        assert "敏感" in field(name="token", secret=True).redacted()
        assert "敏感" not in field(name="url").redacted()


class TestConfigFieldCoercion:
    def test_string_from_number(self) -> None:
        assert field().coerce(42) == "42"

    def test_integer_accepts_numeric_string(self) -> None:
        assert field(type="integer").coerce("7") == 7

    def test_integer_rejects_bool(self) -> None:
        # ``True`` 是 ``int`` 的子类，不特判就会静默变成 1。
        with pytest.raises(PluginManifestError):
            field(type="integer").coerce(True)

    def test_integer_rejects_garbage(self) -> None:
        with pytest.raises(PluginManifestError):
            field(type="integer").coerce("七")

    def test_number(self) -> None:
        assert field(type="number").coerce("1.5") == 1.5

    @pytest.mark.parametrize("value", ["true", "1", "yes", "on", True, 1])
    def test_boolean_true_forms(self, value: Any) -> None:
        assert field(type="boolean").coerce(value) is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", False, 0])
    def test_boolean_false_forms(self, value: Any) -> None:
        assert field(type="boolean").coerce(value) is False

    def test_boolean_rejects_other_words(self) -> None:
        with pytest.raises(PluginManifestError):
            field(type="boolean").coerce("maybe")

    def test_array(self) -> None:
        assert field(type="array").coerce(["a", "b"]) == ["a", "b"]

    def test_array_items_are_coerced(self) -> None:
        assert field(type="array", item_type="integer").coerce(["1", 2]) == [1, 2]

    def test_array_rejects_comma_string(self) -> None:
        # TOML 里写 "a,b" 是手滑，不是数组。
        with pytest.raises(PluginManifestError):
            field(type="array").coerce("a,b")

    def test_array_rejects_scalar(self) -> None:
        with pytest.raises(PluginManifestError):
            field(type="array").coerce(1)

    def test_object(self) -> None:
        assert field(type="object").coerce({"k": 1}) == {"k": 1}

    def test_object_rejects_list(self) -> None:
        with pytest.raises(PluginManifestError):
            field(type="object").coerce([1])

    def test_duration(self) -> None:
        assert field(type="duration").coerce("15m") == timedelta(minutes=15)

    def test_path(self) -> None:
        assert field(type="path").coerce("data") == Path("data")

    def test_path_must_exist(self, tmp_path: Path) -> None:
        spec = field(type="path", must_exist=True)

        assert spec.coerce(tmp_path) == tmp_path
        with pytest.raises(PluginManifestError) as caught:
            spec.coerce(tmp_path / "nope")

        assert caught.value.context["reason"] == "路径不存在"


class TestConfigFieldConstraints:
    def test_choices(self) -> None:
        spec = field(choices=("low", "high"))

        assert spec.coerce("low") == "low"
        with pytest.raises(PluginManifestError):
            spec.coerce("medium")

    def test_min_and_max(self) -> None:
        spec = field(type="integer", min=1, max=10)

        assert spec.coerce(10) == 10
        with pytest.raises(PluginManifestError) as low:
            spec.coerce(0)
        assert "不能小于 1" in low.value.context["reason"]
        with pytest.raises(PluginManifestError) as high:
            spec.coerce(11)
        assert "不能大于 10" in high.value.context["reason"]

    def test_length_bounds(self) -> None:
        spec = field(min_length=2, max_length=4)

        assert spec.coerce("abc") == "abc"
        with pytest.raises(PluginManifestError):
            spec.coerce("a")
        with pytest.raises(PluginManifestError):
            spec.coerce("abcde")

    def test_pattern(self) -> None:
        spec = field(pattern=r"^https://")

        assert spec.coerce("https://x") == "https://x"
        with pytest.raises(PluginManifestError):
            spec.coerce("http://x")

    def test_item_count_bounds(self) -> None:
        spec = field(type="array", min_items=1, max_items=2)

        assert spec.coerce(["a"]) == ["a"]
        with pytest.raises(PluginManifestError):
            spec.coerce([])
        with pytest.raises(PluginManifestError):
            spec.coerce(["a", "b", "c"])

    def test_secret_value_is_masked_in_the_error(self) -> None:
        """报错信息里绝不能把密钥再打印一遍——那正是它要防的事。"""
        spec = field(name="token", secret=True, min_length=100)

        with pytest.raises(PluginManifestError) as caught:
            spec.coerce("hunter2")

        assert caught.value.context["value"] == MASK
        assert "hunter2" not in str(caught.value)

    def test_non_secret_value_is_shown(self) -> None:
        with pytest.raises(PluginManifestError) as caught:
            field(type="integer").coerce("七")

        assert caught.value.context["value"] == "'七'"

    def test_hint_points_at_the_right_file(self) -> None:
        with pytest.raises(PluginManifestError) as caught:
            field(name="port", type="integer").coerce("x")

        assert "config/alterego.toml" in caught.value.context["hint"]
        assert "port" in caught.value.context["hint"]


# ── 清单解析 ────────────────────────────────────────────────


class TestManifestParsing:
    def test_minimal_manifest(self) -> None:
        manifest = make_manifest()

        assert manifest.id == "channel.file"
        assert manifest.module == "plugin"
        assert manifest.class_name == "FileChannel"
        assert manifest.source == "local"

    def test_display_name_falls_back_to_id(self) -> None:
        assert make_manifest().display_name == "channel.file"
        assert make_manifest(name="文件渠道").display_name == "文件渠道"

    @pytest.mark.parametrize(
        "bad_id",
        ["file", "channel.File", "Channel.file", "channel-file", "1channel.file", "channel."],
    )
    def test_bad_id_is_rejected(self, bad_id: str) -> None:
        with pytest.raises(PluginManifestError) as caught:
            make_manifest(id=bad_id)

        assert caught.value.context["id"] == bad_id
        assert "<kind>.<name>" in caught.value.context["hint"]

    @pytest.mark.parametrize("bad_version", ["1.0", "v1.0.0", "1.0.0.0", "", 1])
    def test_bad_version_is_rejected(self, bad_version: Any) -> None:
        with pytest.raises(PluginManifestError) as caught:
            make_manifest(version=bad_version)

        assert "SemVer" in caught.value.message

    def test_prerelease_version_is_accepted(self) -> None:
        assert make_manifest(version="0.2.0-beta.1").version == "0.2.0-beta.1"

    @pytest.mark.parametrize("bad_kind", ["plugin", "CHANNEL", "", None, 3])
    def test_bad_kind_is_rejected(self, bad_kind: Any) -> None:
        with pytest.raises(PluginManifestError) as caught:
            make_manifest(kind=bad_kind)

        assert "channel" in caught.value.context["supported"]

    @pytest.mark.parametrize("bad_entry", ["plugin", "plugin:", ":Class", "plugin:my.Class", 5])
    def test_bad_entry_is_rejected(self, bad_entry: Any) -> None:
        with pytest.raises(PluginManifestError) as caught:
            make_manifest(entry=bad_entry)

        assert "模块:类名" in caught.value.context["hint"]

    def test_entry_may_point_at_a_submodule(self) -> None:
        manifest = make_manifest(entry="channels.file:FileChannel")

        assert manifest.module == "channels.file"
        assert manifest.class_name == "FileChannel"

    @pytest.mark.parametrize("bad_api", ["abc", None, True, 0, -1])
    def test_bad_api_version_is_rejected(self, bad_api: Any) -> None:
        with pytest.raises(PluginManifestError):
            make_manifest(api_version=bad_api)

    def test_api_version_may_be_a_string(self) -> None:
        # TOML 里 ``api_version = "1"`` 与 ``= 1`` 都应该能用。
        assert make_manifest(api_version="1").api_version == 1

    def test_future_api_version_is_rejected(self) -> None:
        # 这条路径曾经会先把 ``TypeError`` 抛出来（重复的 plugin= 关键字），
        # 于是「版本不兼容」变成了「加载器崩溃」。
        with pytest.raises(PluginManifestError) as caught:
            make_manifest(api_version=API_VERSION + 1)

        assert caught.value.context["plugin_api_version"] == API_VERSION + 1
        assert caught.value.context["kernel_api_version"] == API_VERSION
        assert caught.value.context["plugin"] == "channel.file"
        assert "api_version" in caught.value.context["hint"]

    def test_path_and_source_are_recorded(self, tmp_path: Path) -> None:
        manifest = PluginManifest.parse(BASE_MANIFEST, path=tmp_path, source="entry_point")

        assert manifest.path == tmp_path
        assert manifest.source == "entry_point"

    def test_string_becomes_single_element_tuple(self) -> None:
        # TOML 里 ``authors = "我"`` 很常见，不该让写插件的人为此报错。
        manifest = make_manifest(authors="我", tags="demo", requires="storage.x")

        assert manifest.authors == ("我",)
        assert manifest.tags == ("demo",)
        assert manifest.requires == ("storage.x",)

    def test_list_becomes_tuple(self) -> None:
        assert make_manifest(tags=["a", "b"]).tags == ("a", "b")

    def test_string_field_rejects_a_list(self) -> None:
        with pytest.raises(PluginManifestError):
            make_manifest(tags={"not": "a list"})

    def test_boolean_defaults(self) -> None:
        manifest = make_manifest()

        assert manifest.enabled_by_default is True
        assert manifest.auto_reload is True
        assert manifest.priority == 0

    def test_wrong_types_fall_back_to_defaults(self) -> None:
        # ``enabled_by_default = "yes"`` 是写错了，不是真话；宁可用默认值。
        manifest = make_manifest(enabled_by_default="yes", priority="high", auto_reload=1)

        assert manifest.enabled_by_default is True
        assert manifest.priority == 0
        assert manifest.auto_reload is True

    def test_explicit_false_is_kept(self) -> None:
        manifest = make_manifest(enabled_by_default=False, priority=-5, auto_reload=False)

        assert manifest.enabled_by_default is False
        assert manifest.priority == -5
        assert manifest.auto_reload is False

    def test_error_context_names_the_manifest_file(self, tmp_path: Path) -> None:
        data = dict(BASE_MANIFEST, version="not-semver")

        with pytest.raises(PluginManifestError) as caught:
            PluginManifest.parse(data, path=tmp_path)

        assert str(tmp_path / "plugin.toml") == caught.value.context["manifest"]


class TestManifestConfigTable:
    def test_config_is_parsed(self) -> None:
        manifest = make_manifest(
            config={
                "webhook_url": {"type": "string", "required": True, "description": "地址"},
                "retries": {"type": "integer", "default": 3},
            }
        )

        assert set(manifest.config) == {"webhook_url", "retries"}
        assert manifest.config["webhook_url"].required is True
        assert manifest.config_field("retries").default == 3  # type: ignore[union-attr]

    def test_config_field_missing_returns_none(self) -> None:
        assert make_manifest().config_field("nope") is None

    def test_config_must_be_a_table(self) -> None:
        with pytest.raises(PluginManifestError) as caught:
            make_manifest(config=["not", "a", "table"])

        assert caught.value.context["got"] == "list"

    def test_field_declaration_must_be_a_table(self) -> None:
        with pytest.raises(PluginManifestError) as caught:
            make_manifest(config={"url": "just-a-string"})

        assert caught.value.context["field"] == "url"
        assert "webhook_url" in caught.value.context["hint"]

    def test_typo_in_key_is_rejected(self) -> None:
        """``requierd`` 静默失效会表现为「必填校验没生效」——最难查的一类症状。"""
        with pytest.raises(PluginManifestError) as caught:
            make_manifest(config={"url": {"requierd": True}})

        assert caught.value.context["unknown"] == ["requierd"]
        assert "required" in caught.value.context["supported"]

    def test_nested_schema_is_rejected_with_a_workaround(self) -> None:
        with pytest.raises(PluginManifestError) as caught:
            make_manifest(config={"obj": {"type": "object", "schema": {"a": {"type": "string"}}}})

        assert "v1" in caught.value.message
        assert caught.value.context["hint"]

    def test_redacted_config_masks_only_secret_fields(self) -> None:
        manifest = make_manifest(
            config={"token": {"type": "string", "secret": True}, "url": {"type": "string"}}
        )

        redacted = manifest.redacted_config({"token": "hunter2", "url": "https://x"})

        assert redacted == {"token": MASK, "url": "https://x"}

    def test_redacted_config_keeps_undeclared_keys(self) -> None:
        assert make_manifest().redacted_config({"extra": 1}) == {"extra": 1}


class TestApiVersionCompatibility:
    @pytest.mark.parametrize(
        ("plugin", "kernel", "expected"),
        [
            (1, 1, True),  # 同版本
            (1, 2, True),  # 内核向后兼容一个大版本
            (2, 2, True),
            (2, 1, False),  # 插件要求的比内核新
            (1, 3, False),  # 超出兼容窗口
            (3, 3, True),
        ],
    )
    def test_window(self, plugin: int, kernel: int, expected: bool) -> None:
        assert is_api_version_compatible(plugin, kernel) is expected

    def test_defaults_to_the_current_kernel_version(self) -> None:
        assert is_api_version_compatible(API_VERSION) is True
        assert is_api_version_compatible(API_VERSION + 1) is False


# ── 配置合并 ────────────────────────────────────────────────


class TestResolveConfig:
    manifest = make_manifest(
        config={
            "mode": {"type": "string", "default": "auto"},
            "token": {"type": "string", "env": "FILE_TOKEN"},
            "required_one": {"type": "string", "required": True},
        }
    )

    def test_defaults_are_filled_in(self) -> None:
        values = resolve_config(self.manifest, {"required_one": "x"})

        assert values == {"mode": "auto", "required_one": "x"}

    def test_user_config_beats_default(self) -> None:
        values = resolve_config(self.manifest, {"mode": "manual", "required_one": "x"})

        assert values["mode"] == "manual"

    def test_environment_beats_user_config(self) -> None:
        # 这是「容器里改一个环境变量就能换掉镜像里的配置」的前提。
        values = resolve_config(
            self.manifest,
            {"required_one": "x", "token": "from-file"},
            environ={"FILE_TOKEN": "from-env"},
        )

        assert values["token"] == "from-env"

    def test_empty_environment_value_is_ignored(self) -> None:
        values = resolve_config(
            self.manifest, {"required_one": "x", "token": "from-file"}, environ={"FILE_TOKEN": ""}
        )

        assert values["token"] == "from-file"

    def test_fields_without_env_never_read_the_environment(self) -> None:
        # 不写 ``env`` 就不看环境变量——不做任何猜测（P2）。
        values = resolve_config(self.manifest, {"required_one": "x", "mode": "manual"})

        assert values["mode"] == "manual"

    def test_required_missing_is_reported_with_a_hint(self) -> None:
        with pytest.raises(PluginManifestError) as caught:
            resolve_config(self.manifest, {"mode": "auto"})

        assert caught.value.context["missing"] == ["required_one"]
        assert f"[plugins.{self.manifest.id}]" in caught.value.context["hint"]

    def test_hint_mentions_the_env_var_when_declared(self) -> None:
        manifest = make_manifest(
            config={"token": {"type": "string", "required": True, "env": "FILE_TOKEN"}}
        )

        with pytest.raises(PluginManifestError) as caught:
            resolve_config(manifest, {})

        assert "FILE_TOKEN" in caught.value.context["hint"]

    def test_provided_value_is_coerced(self) -> None:
        manifest = make_manifest(config={"port": {"type": "integer"}})

        assert resolve_config(manifest, {"port": "8080"}) == {"port": 8080}

    def test_environment_value_is_coerced(self) -> None:
        manifest = make_manifest(config={"port": {"type": "integer", "env": "PORT"}})

        assert resolve_config(manifest, None, environ={"PORT": "8080"}) == {"port": 8080}

    def test_undeclared_key_is_kept_but_warned(self, caplog: Any) -> None:
        # 不失败：插件可能故意读清单外的键。但要说一声，否则写错键名
        # 会表现为「配置没生效」。
        with caplog.at_level(logging.WARNING):
            values = resolve_config(self.manifest, {"required_one": "x", "typo": 1}, logger=LOGGER)

        assert values["typo"] == 1
        assert "typo" in caplog.text

    def test_secret_real_value_reaches_the_plugin(self) -> None:
        # 脱敏只发生在面向人的输出上；插件自己需要真值。
        manifest = make_manifest(config={"token": {"type": "string", "secret": True}})

        assert resolve_config(manifest, {"token": "hunter2"}) == {"token": "hunter2"}


# ── 路径 ────────────────────────────────────────────────────


class TestPluginPaths:
    def test_ensure_dirs_creates_only_writable_ones(self, tmp_path: Path) -> None:
        readonly = tmp_path / "plugin-src"
        paths = PluginPaths(
            data_dir=tmp_path / "data" / "plugins" / "channel.file",
            cache_dir=tmp_path / "data" / "plugins" / "channel.file" / "cache",
            config_dir=tmp_path / "config",
            plugin_dir=readonly,
            alterego_dir=tmp_path,
        )

        paths.ensure_dirs()

        assert paths.data_dir.is_dir()
        assert paths.cache_dir.is_dir()
        assert not paths.config_dir.exists()  # 只读目录不该被顺手创建
        assert not readonly.exists()


# ── 状态 ────────────────────────────────────────────────────


class TestPluginState:
    def test_get_set_keys(self) -> None:
        state = PluginState("channel.file")

        state.set("cursor", 42)

        assert state.get("cursor") == 42
        assert state.get("nope") is None
        assert state.get("nope", "fallback") == "fallback"
        assert state.keys() == ["cursor"]

    def test_initial_values_are_loaded(self) -> None:
        assert PluginState("x", initial={"a": 1}).get("a") == 1

    def test_plugin_id_is_exposed(self) -> None:
        assert PluginState("channel.file").plugin_id == "channel.file"

    def test_non_json_value_is_rejected_immediately(self) -> None:
        # 等到 flush 时才炸，会指着一个跟错误无关的 tick。
        state = PluginState("x")

        with pytest.raises(PluginError) as caught:
            state.set("handler", lambda: None)

        assert caught.value.context["key"] == "handler"
        assert caught.value.context["type"] == "function"
        assert state.keys() == []

    def test_json_serializable_only_after_conversion_is_rejected(self) -> None:
        state = PluginState("x")

        with pytest.raises(PluginError):
            state.set("when", object())

        assert json.dumps(state.snapshot()) == "{}"

    def test_delete_removes_and_records(self) -> None:
        state = PluginState("x", initial={"a": 1, "b": 2})

        state.delete("a")

        assert state.keys() == ["b"]
        assert state.take_pending() == {"a": None}

    def test_delete_of_absent_key_is_recorded_anyway(self) -> None:
        # 幂等删除：调用方不需要先判断它存不存在。
        state = PluginState("x")

        state.delete("ghost")

        assert state.take_pending() == {"ghost": None}

    def test_set_after_delete_clears_the_deletion(self) -> None:
        state = PluginState("x")

        state.delete("a")
        state.set("a", 1)

        assert state.take_pending() == {"a": 1}

    def test_delete_after_set_drops_the_write(self) -> None:
        state = PluginState("x")

        state.set("a", 1)
        state.delete("a")

        assert state.take_pending() == {"a": None}

    def test_update(self) -> None:
        state = PluginState("x")

        state.update({"a": 1, "b": 2})

        assert state.keys() == ["a", "b"]
        assert state.take_pending() == {"a": 1, "b": 2}

    def test_clear(self) -> None:
        state = PluginState("x", initial={"a": 1, "b": 2})

        state.clear()

        assert state.keys() == []
        assert state.take_pending() == {"a": None, "b": None}

    def test_snapshot_is_a_copy(self) -> None:
        state = PluginState("x", initial={"a": 1})

        snapshot = state.snapshot()
        snapshot["a"] = 999

        assert state.get("a") == 1

    def test_pending_is_empty_until_something_changes(self) -> None:
        state = PluginState("x", initial={"a": 1})

        assert state.has_pending_writes is False
        assert state.take_pending() == {}

    def test_take_pending_drains(self) -> None:
        state = PluginState("x")
        state.set("a", 1)

        assert state.has_pending_writes is True
        assert state.take_pending() == {"a": 1}
        assert state.take_pending() == {}

    def test_flush_hands_pending_to_the_sink(self) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []
        state = PluginState("x", initial={"a": 1}, sink=lambda pid, data: calls.append((pid, data)))

        state.set("a", 2)
        state.flush()

        assert calls == [("x", {"a": 2})]
        assert state.has_pending_writes is False

    def test_flush_without_changes_does_not_call_the_sink(self) -> None:
        calls: list[Any] = []
        state = PluginState("x", initial={"a": 1}, sink=lambda *args: calls.append(args))

        state.flush()

        assert calls == []

    def test_flush_without_sink_just_settles_the_books(self) -> None:
        state = PluginState("x")
        state.set("a", 1)

        state.flush()

        assert state.has_pending_writes is False
        assert state.get("a") == 1


# ── 上下文 ──────────────────────────────────────────────────


class FakeChannel:
    """一个只用来注册的假实现。"""

    id = "channel.fake"


def build_context(
    *,
    clock: FrozenClock,
    bus: EventBus,
    registry: ServiceRegistry,
    base: Path,
    rng: random.Random | None = None,
) -> PluginContext:
    """造一个上下文。做成函数是因为有的测试要连着造两个来对比。"""
    manifest = make_manifest()
    return PluginContext(
        plugin_id=manifest.id,
        manifest=manifest,
        config={"mode": "auto"},
        logger=LOGGER,
        bus=bus,
        registry=registry,
        clock=clock,
        scheduler=Scheduler(clock, rng=random.Random(0)),
        state=PluginState(manifest.id),
        paths=PluginPaths(
            data_dir=base / "data",
            cache_dir=base / "cache",
            config_dir=base / "config",
            plugin_dir=base,
            alterego_dir=base,
        ),
        rng=rng if rng is not None else random.Random(7),
    )


@pytest.fixture
def context(
    clock: FrozenClock, bus: EventBus, registry: ServiceRegistry, tmp_path: Path
) -> PluginContext:
    return build_context(clock=clock, bus=bus, registry=registry, base=tmp_path)


class TestPluginContext:
    def test_config_is_read_only(self, context: PluginContext) -> None:
        # frozen 只拦住「换掉整个 config」，拦不住往里塞东西。
        assert isinstance(context.config, MappingProxyType)
        with pytest.raises(TypeError):
            context.config["mode"] = "manual"  # type: ignore[index]

    def test_config_value(self, context: PluginContext) -> None:
        assert context.config_value("mode") == "auto"
        assert context.config_value("missing", 1) == 1

    def test_get_service(self, context: PluginContext) -> None:
        channel = FakeChannel()
        context.registry.register(FakeChannel, channel, name="channel.fake")

        assert context.get_service(FakeChannel) is channel

    def test_get_service_raises_when_absent(self, context: PluginContext) -> None:
        with pytest.raises(PluginError):
            context.get_service(FakeChannel)

    def test_get_optional_service_returns_none(self, context: PluginContext) -> None:
        assert context.get_optional_service(FakeChannel) is None

    def test_publish_marks_the_plugin_as_the_source(self, context: PluginContext) -> None:
        seen: list[Any] = []
        context.bus.subscribe("thing.happened", seen.append)

        event = context.publish("thing.happened", {"a": 1})

        assert event.source == "channel.file"
        assert seen[0].topic == "thing.happened"

    def test_now_follows_the_virtual_clock(
        self, context: PluginContext, clock: FrozenClock
    ) -> None:
        assert context.now() == clock.virtual_now()

        clock.advance(timedelta(hours=3))

        assert context.now() == clock.virtual_now()

    def test_rng_is_injected_not_global(
        self,
        clock: FrozenClock,
        bus: EventBus,
        registry: ServiceRegistry,
        tmp_path: Path,
    ) -> None:
        """同一颗种子必须给出同一串随机数——这是 P6「可重放」的前提。"""
        first = build_context(
            clock=clock, bus=bus, registry=registry, base=tmp_path, rng=random.Random(1)
        )
        second = build_context(
            clock=clock, bus=bus, registry=registry, base=tmp_path, rng=random.Random(1)
        )

        assert first.rng.random() == second.rng.random()


# ── 插件基类 ────────────────────────────────────────────────


class TestPluginBase:
    def test_id_comes_from_the_manifest(self) -> None:
        plugin = Plugin()
        plugin.manifest = make_manifest()

        assert plugin.id == "channel.file"

    def test_default_health_is_ok(self) -> None:
        status = Plugin().health()

        assert status.ok is True
        assert status.detail == "正常"
        assert status.hint is None

    def test_hooks_are_no_ops_by_default(self, context: PluginContext) -> None:
        """九个钩子全是空方法——这是「按需覆盖」而不是「必须实现」。"""
        plugin = Plugin()
        plugin.manifest = make_manifest()

        plugin.on_load(context)
        plugin.on_start()
        plugin.on_stop()
        plugin.on_unload()
        plugin.on_config_changed({"a": 1})
        plugin.on_tick_pre(None)
        plugin.on_tick_post(None)
        plugin.on_event(context.publish("x", {}))

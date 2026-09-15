"""内核插件加载器测试：版本约束、发现、导入与依赖编排。

加载器处理的是**声明层面**的错误（清单写错、依赖成环）。这一类的错误信息
是用户唯一能拿到的线索，所以每条断言都连带钉住提示文字。
"""

from __future__ import annotations

import importlib.metadata
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

from alterego.kernel.errors import (
    PluginDependencyError,
    PluginLoadError,
    PluginManifestError,
)
from alterego.kernel.loader import (
    PLUGIN_NAMESPACE,
    DiscoveredPlugin,
    DiscoveryResult,
    Requirement,
    VersionConstraint,
    discover,
    import_plugin_class,
    load_manifest,
    module_name_for,
    parse_requirement,
    parse_semver,
    resolve_load_order,
)
from alterego.kernel.plugin import API_VERSION, Plugin, PluginManifest


LOGGER = logging.getLogger("test.loader")


@pytest.fixture(autouse=True)
def clean_plugin_namespace() -> Any:
    """插件是往真实 ``sys.modules`` 里注册的，测完必须擦干净。"""
    yield None
    for key in [
        k for k in sys.modules if k == PLUGIN_NAMESPACE or k.startswith(f"{PLUGIN_NAMESPACE}.")
    ]:
        del sys.modules[key]


# ── 版本 ────────────────────────────────────────────────────


class TestParseSemver:
    def test_plain_version(self) -> None:
        assert parse_semver("1.2.3") == (1, 2, 3)

    def test_surrounding_whitespace_is_ignored(self) -> None:
        assert parse_semver("  0.10.2 ") == (0, 10, 2)

    def test_prerelease_suffix_is_ignored(self) -> None:
        # ``-beta.1`` 只影响排序，不影响「能不能装」。
        assert parse_semver("1.0.0-beta.1") == (1, 0, 0)
        assert parse_semver("1.0.0+build.7") == (1, 0, 0)

    @pytest.mark.parametrize("text", ["1.0", "v1.0.0", "1", "", "latest"])
    def test_bad_version_is_rejected(self, text: str) -> None:
        with pytest.raises(PluginManifestError) as caught:
            parse_semver(text)

        assert caught.value.context["version"] == text


class TestVersionConstraint:
    @pytest.mark.parametrize(
        ("operator", "bound", "actual", "expected"),
        [
            (">=", (0, 1, 0), (0, 1, 0), True),
            (">=", (0, 1, 0), (0, 2, 0), True),
            (">=", (0, 1, 0), (0, 0, 9), False),
            ("<=", (0, 1, 0), (0, 1, 0), True),
            ("<=", (0, 1, 0), (0, 0, 9), True),
            ("<=", (0, 1, 0), (0, 2, 0), False),
            ("==", (0, 1, 0), (0, 1, 0), True),
            ("==", (0, 1, 0), (0, 1, 1), False),
        ],
    )
    def test_comparisons(
        self,
        operator: str,
        bound: tuple[int, int, int],
        actual: tuple[int, int, int],
        expected: bool,
    ) -> None:
        assert VersionConstraint(operator=operator, version=bound).satisfies(actual) is expected

    @pytest.mark.parametrize(
        ("actual", "expected"),
        [
            ((0, 1, 0), True),  # 下界含
            ((0, 1, 9), True),  # 允许最后一个数字变
            ((0, 2, 0), False),  # 不允许倒数第二个数字变
            ((0, 0, 9), False),
        ],
    )
    def test_compatible_release_operator(
        self, actual: tuple[int, int, int], expected: bool
    ) -> None:
        assert VersionConstraint(operator="~=", version=(0, 1, 0)).satisfies(actual) is expected

    def test_str_uses_the_raw_form(self) -> None:
        assert str(VersionConstraint(">=", (0, 1, 0), raw=">= 0.1.0")) == ">= 0.1.0"

    def test_str_without_raw_reconstructs(self) -> None:
        assert str(VersionConstraint(">=", (0, 1, 0))) == ">= 0.1.0"


class TestParseRequirement:
    def test_bare_plugin_id(self) -> None:
        requirement = parse_requirement("storage.my_backend")

        assert requirement.plugin_id == "storage.my_backend"
        assert requirement.constraint is None
        assert str(requirement) == "storage.my_backend"

    @pytest.mark.parametrize("operator", [">=", "<=", "==", "~="])
    def test_all_operators(self, operator: str) -> None:
        requirement = parse_requirement(f"storage.my_backend {operator} 0.1.0")

        assert requirement.constraint is not None
        assert requirement.constraint.operator == operator
        assert str(requirement.constraint) == f"{operator} 0.1.0"

    def test_extra_spaces_are_tolerated(self) -> None:
        assert parse_requirement("  llm.my_provider   >=0.2.0  ").constraint is not None

    @pytest.mark.parametrize(
        "text",
        [
            "storage",
            "storage.my_backend >",
            "storage.my_backend => 1.0.0",
            "storage.my_backend 0.1.0",  # 漏了比较符——不能静默当成「无约束」
            "",
        ],
    )
    def test_malformed_declaration(self, text: str) -> None:
        with pytest.raises(PluginManifestError) as caught:
            parse_requirement(text)

        assert caught.value.context["requirement"] == text
        assert "storage.my_backend >= 0.1.0" in caught.value.context["hint"]

    def test_operator_without_version(self) -> None:
        with pytest.raises(PluginManifestError) as caught:
            parse_requirement("storage.my_backend >=")

        assert "缺少版本号" in caught.value.message

    def test_non_semver_version(self) -> None:
        with pytest.raises(PluginManifestError):
            parse_requirement("storage.my_backend >= 1.0")


class TestRequirementCheck:
    def test_no_constraint_always_passes(self) -> None:
        Requirement(plugin_id="storage.x").check("0.0.1", dependent="channel.y")

    def test_satisfied_constraint_passes(self) -> None:
        requirement = parse_requirement("storage.x >= 0.1.0")

        requirement.check("0.3.0", dependent="channel.y")

    def test_unsatisfied_constraint_reports_both_sides(self) -> None:
        requirement = parse_requirement("storage.x >= 0.5.0")

        with pytest.raises(PluginDependencyError) as caught:
            requirement.check("0.1.0", dependent="channel.y")

        context = caught.value.context
        assert context["plugin"] == "channel.y"
        assert context["requires"] == "storage.x"
        assert context["constraint"] == ">= 0.5.0"
        assert context["found"] == "0.1.0"
        assert "storage.x" in context["hint"]


# ── 命名与发现结果 ──────────────────────────────────────────


class TestModuleName:
    def test_namespace_prefix(self) -> None:
        assert module_name_for("channel.file") == "alterego_plugins.channel.file"

    def test_uses_the_dedicated_namespace(self) -> None:
        # 撞上 pip 包名或项目自身模块会非常难查，所以命名空间是刻意分开的。
        assert not module_name_for("kernel.bus").startswith("alterego.")


class TestDiscoveryResult:
    def make(self, plugin_id: str) -> DiscoveredPlugin:
        return DiscoveredPlugin(manifest=make_manifest(plugin_id))

    def test_len_and_contains(self) -> None:
        result = DiscoveryResult(plugins={"channel.a": self.make("channel.a")})

        assert len(result) == 1
        assert "channel.a" in result
        assert "channel.b" not in result

    def test_get(self) -> None:
        plugin = self.make("channel.a")
        result = DiscoveryResult(plugins={"channel.a": plugin})

        assert result.get("channel.a") is plugin
        assert result.get("channel.b") is None

    def test_ids_are_sorted(self) -> None:
        result = DiscoveryResult(
            plugins={"channel.b": self.make("channel.b"), "channel.a": self.make("channel.a")}
        )

        assert result.ids() == ["channel.a", "channel.b"]

    def test_merge_combines_plugins_and_failures(self) -> None:
        left = DiscoveryResult(plugins={"channel.a": self.make("channel.a")})
        right = DiscoveryResult(
            plugins={"channel.b": self.make("channel.b")},
            failed=[(Path("/tmp/x"), PluginManifestError("坏的"))],
        )

        left.merge(right)

        assert left.ids() == ["channel.a", "channel.b"]
        assert len(left.failed) == 1

    def test_plugin_properties(self) -> None:
        plugin = self.make("channel.file")

        assert plugin.id == "channel.file"
        assert plugin.module_name == "alterego_plugins.channel.file"


# ── 清单文件 ────────────────────────────────────────────────


def write_manifest(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "plugin.toml"
    path.write_text(body, encoding="utf-8")
    return path


VALID_TOML = """
[plugin]
id = "channel.demo"
version = "0.1.0"
api_version = 1
kind = "channel"
entry = "plugin:DemoChannel"
name = "演示渠道"
"""


class TestLoadManifest:
    def test_valid_file(self, tmp_path: Path) -> None:
        path = write_manifest(tmp_path / "demo", VALID_TOML)

        manifest = load_manifest(path)

        assert manifest.id == "channel.demo"
        assert manifest.name == "演示渠道"
        assert manifest.path == path.parent

    def test_base_dir_overrides_the_parent(self, tmp_path: Path) -> None:
        path = write_manifest(tmp_path / "demo", VALID_TOML)

        assert load_manifest(path, base_dir=tmp_path).path == tmp_path

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(PluginManifestError) as caught:
            load_manifest(tmp_path / "nope" / "plugin.toml")

        assert "找不到" in caught.value.message

    def test_invalid_toml(self, tmp_path: Path) -> None:
        path = write_manifest(tmp_path / "demo", "[plugin\nid = 1")

        with pytest.raises(PluginManifestError) as caught:
            load_manifest(path)

        assert caught.value.context["reason"]

    def test_missing_plugin_table(self, tmp_path: Path) -> None:
        path = write_manifest(tmp_path / "demo", "[other]\nid = 1\n")

        with pytest.raises(PluginManifestError) as caught:
            load_manifest(path)

        assert "[plugin]" in caught.value.message
        assert "id / version" in caught.value.context["hint"]

    def test_non_table_plugin_key(self, tmp_path: Path) -> None:
        path = write_manifest(tmp_path / "demo", 'plugin = "nope"\n')

        with pytest.raises(PluginManifestError):
            load_manifest(path)


# ── 发现 ────────────────────────────────────────────────────


def make_manifest(plugin_id: str, **overrides: Any) -> PluginManifest:
    kind = plugin_id.split(".", 1)[0]
    data: dict[str, Any] = {
        "id": plugin_id,
        "version": "0.1.0",
        "api_version": API_VERSION,
        "kind": kind,
        "entry": "plugin:Cls",
    }
    data.update(overrides)
    return PluginManifest.parse(data)


def make_discovered(
    plugin_id: str, *, path: Path | None = None, **overrides: Any
) -> DiscoveredPlugin:
    return DiscoveredPlugin(manifest=make_manifest(plugin_id, **overrides), path=path)


class TestDiscover:
    def test_finds_local_plugins(self, tmp_path: Path) -> None:
        write_manifest(tmp_path / "demo", VALID_TOML)

        result = discover(search_paths=[str(tmp_path)], entry_point_group="alterego.test.none")

        assert result.ids() == ["channel.demo"]
        assert result.get("channel.demo").path == tmp_path / "demo"  # type: ignore[union-attr]

    def test_ignores_directories_without_a_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "not-a-plugin").mkdir()
        (tmp_path / "a-file.txt").write_text("x", encoding="utf-8")

        result = discover(search_paths=[str(tmp_path)], entry_point_group="alterego.test.none")

        assert result.ids() == []
        assert result.failed == []

    def test_missing_search_path_is_not_an_error(self, tmp_path: Path) -> None:
        # 默认搜索路径里有一半在全新机器上并不存在，这不该报错。
        result = discover(
            search_paths=[str(tmp_path / "nope")], entry_point_group="alterego.test.none"
        )

        assert len(result) == 0

    def test_broken_manifest_lands_in_failed(self, tmp_path: Path) -> None:
        """一个坏掉的插件目录不该让 ``alterego plugins list`` 整个跑不起来。"""
        write_manifest(tmp_path / "bad", '[plugin]\nid = "nope"\n')

        result = discover(search_paths=[str(tmp_path)], entry_point_group="alterego.test.none")

        assert len(result) == 0
        assert result.failed[0][0] == tmp_path / "bad"

    def test_earlier_search_path_wins(self, tmp_path: Path) -> None:
        first = tmp_path / "user"
        second = tmp_path / "bundled"
        write_manifest(first / "demo", VALID_TOML)
        write_manifest(second / "demo", VALID_TOML)

        result = discover(
            search_paths=[str(first), str(second)], entry_point_group="alterego.test.none"
        )

        assert result.get("channel.demo").path == first / "demo"  # type: ignore[union-attr]

    def test_duplicate_in_the_same_path_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        write_manifest(tmp_path / "demo-a", VALID_TOML)
        write_manifest(tmp_path / "demo-b", VALID_TOML)

        with caplog.at_level(logging.WARNING):
            result = discover(
                search_paths=[str(tmp_path)],
                entry_point_group="alterego.test.none",
                logger=LOGGER,
            )

        assert result.ids() == ["channel.demo"]
        assert "重复定义" in caplog.text

    def test_home_expansion(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        write_manifest(tmp_path / ".alterego" / "plugins" / "demo", VALID_TOML)

        result = discover(
            search_paths=["~/.alterego/plugins"], entry_point_group="alterego.test.none"
        )

        assert result.ids() == ["channel.demo"]


# ── pip 入口点 ──────────────────────────────────────────────


class FakeEntryPoint:
    """``importlib.metadata.EntryPoint`` 的最小替身。

    只测自己用的那两个属性：真实的 ``EntryPoint`` 需要装一个包才能造出来。
    """

    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value


@pytest.fixture
def entry_points(monkeypatch: pytest.MonkeyPatch) -> Any:
    """替换 ``importlib.metadata.entry_points``，并保证测完擦掉导入过的模块。"""
    created: list[str] = []

    def install(items: list[FakeEntryPoint]) -> None:
        created.extend(item.value.split(":", 1)[0] for item in items)
        monkeypatch.setattr(importlib.metadata, "entry_points", lambda *, group: items)

    yield install

    for name in created:
        for key in [k for k in sys.modules if k == name or k.startswith(f"{name}.")]:
            del sys.modules[key]


class TestEntryPointDiscovery:
    def test_reads_a_sibling_plugin_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry_points: Any
    ) -> None:
        package = tmp_path / "ep_pkg"
        write_manifest(package, VALID_TOML)
        (package / "__init__.py").write_text("", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        entry_points([FakeEntryPoint("demo", "ep_pkg:DemoChannel")])

        result = discover(
            search_paths=[str(tmp_path / "empty")], entry_point_group="alterego.test.ep"
        )

        plugin = result.get("channel.demo")
        assert plugin is not None
        assert plugin.path == package
        assert plugin.entry_point == "ep_pkg:DemoChannel"

    def test_falls_back_to_a_module_level_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry_points: Any
    ) -> None:
        (tmp_path / "ep_module.py").write_text(
            "MANIFEST = {\n"
            '    "id": "llm.demo",\n'
            '    "version": "0.1.0",\n'
            '    "api_version": 1,\n'
            '    "kind": "llm",\n'
            '    "entry": "plugin:Demo",\n'
            "}\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        entry_points([FakeEntryPoint("demo", "ep_module:Demo")])

        result = discover(
            search_paths=[str(tmp_path / "empty")], entry_point_group="alterego.test.ep"
        )

        plugin = result.get("llm.demo")
        assert plugin is not None
        assert plugin.manifest.source == "entry_point"

    def test_missing_manifest_is_skipped_with_a_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        entry_points: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        (tmp_path / "ep_bare.py").write_text("VALUE = 1\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        entry_points([FakeEntryPoint("bare", "ep_bare:Demo")])

        with caplog.at_level(logging.ERROR):
            result = discover(
                search_paths=[str(tmp_path / "empty")],
                entry_point_group="alterego.test.ep",
                logger=LOGGER,
            )

        assert len(result) == 0
        assert "找不到" in caplog.text

    def test_broken_manifest_is_skipped_with_a_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        entry_points: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        package = tmp_path / "ep_broken"
        write_manifest(package, '[plugin]\nid = "nope"\n')
        (package / "__init__.py").write_text("", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        entry_points([FakeEntryPoint("broken", "ep_broken:Demo")])

        with caplog.at_level(logging.ERROR):
            result = discover(
                search_paths=[str(tmp_path / "empty")],
                entry_point_group="alterego.test.ep",
                logger=LOGGER,
            )

        # 一个装坏了的第三方包不该让整个启动失败。
        assert len(result) == 0
        assert "清单有误" in caplog.text

    def test_import_failure_is_skipped_with_a_log(
        self, tmp_path: Path, entry_points: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        entry_points([FakeEntryPoint("ghost", "no_such_package_anywhere:Demo")])

        with caplog.at_level(logging.ERROR):
            result = discover(
                search_paths=[str(tmp_path / "empty")],
                entry_point_group="alterego.test.ep",
                logger=LOGGER,
            )

        assert len(result) == 0
        assert "导入 entry point" in caplog.text

    def test_local_copy_wins_over_the_installed_one(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        entry_points: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """本地目录优先于 pip 安装的版本，但要**说一声**。

        静默覆盖会让「我明明卸载了它怎么还在」变成一个谜。
        """
        local = tmp_path / "local"
        write_manifest(local / "demo", VALID_TOML)

        package = tmp_path / "ep_pkg"
        write_manifest(package, VALID_TOML)
        (package / "__init__.py").write_text("", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        entry_points([FakeEntryPoint("demo", "ep_pkg:DemoChannel")])

        with caplog.at_level(logging.WARNING):
            result = discover(
                search_paths=[str(local)], entry_point_group="alterego.test.ep", logger=LOGGER
            )

        assert result.get("channel.demo").path == local / "demo"  # type: ignore[union-attr]
        assert "使用本地版本" in caplog.text


# ── 导入 ────────────────────────────────────────────────────

PLUGIN_BODY = """
from alterego.kernel.plugin import Plugin

from .helper import GREETING


class DemoChannel(Plugin):
    greeting = GREETING
"""

HELPER_BODY = 'GREETING = "你好"\n'


def install_local_plugin(
    root: Path,
    *,
    plugin_id: str = "channel.demo",
    entry: str = "plugin:DemoChannel",
    body: str = PLUGIN_BODY,
    with_init: bool = False,
    with_helper: bool = True,
    module_filename: str = "plugin.py",
) -> DiscoveredPlugin:
    directory = root / plugin_id.split(".", 1)[1]
    directory.mkdir(parents=True)
    (directory / module_filename).write_text(body, encoding="utf-8")
    if with_helper:
        (directory / "helper.py").write_text(HELPER_BODY, encoding="utf-8")
    if with_init:
        (directory / "__init__.py").write_text("", encoding="utf-8")
    return DiscoveredPlugin(
        manifest=make_manifest(plugin_id, entry=entry),
        path=directory,
    )


class TestImportPluginClass:
    def test_imports_a_local_plugin_without_init_py(self, tmp_path: Path) -> None:
        """插件目录**不**需要 ``__init__.py``——用户往 plugins/ 里丢一个目录就该生效。"""
        plugin = install_local_plugin(tmp_path)

        cls = import_plugin_class(plugin)

        assert issubclass(cls, Plugin)
        assert cls.__name__ == "DemoChannel"
        assert cls.greeting == "你好"

    def test_imports_a_local_plugin_with_init_py(self, tmp_path: Path) -> None:
        cls = import_plugin_class(install_local_plugin(tmp_path, with_init=True))

        assert cls.__name__ == "DemoChannel"

    def test_package_gets_its_own_namespace(self, tmp_path: Path) -> None:
        # 插件内部的 ``from .helper import ...`` 走的是插件自己的命名空间，
        # 不会污染全局，也不会和 pip 包撞名。
        plugin = install_local_plugin(tmp_path)
        import_plugin_class(plugin)

        assert f"{PLUGIN_NAMESPACE}.channel.demo.plugin" in sys.modules
        assert f"{PLUGIN_NAMESPACE}.channel.demo.helper" in sys.modules
        assert "helper" not in sys.modules

    def test_full_form_entry_is_rejected_with_a_useful_hint(self, tmp_path: Path) -> None:
        """``entry`` 的模块路径是相对于插件根目录的，不是插件 id。

        这两者长得几乎一样（``channel.demo`` vs ``demo``），写错了必须明确指出来，
        否则症状就是「插件莫名其妙没加载」。
        """
        plugin = install_local_plugin(tmp_path, entry="channel.demo:DemoChannel")

        with pytest.raises(PluginManifestError) as caught:
            import_plugin_class(plugin)

        assert "相对于插件根目录" in caught.value.context["hint"]
        assert "plugin:DemoChannel" in caught.value.context["hint"]

    def test_reimport_replaces_the_previous_module(self, tmp_path: Path) -> None:
        """热重载要求第二次导入拿到的是**新**代码。"""
        plugin = install_local_plugin(tmp_path)
        first = import_plugin_class(plugin)

        (plugin.path / "plugin.py").write_text(  # type: ignore[operator]
            PLUGIN_BODY.replace("greeting = GREETING", 'greeting = "改过了"'), encoding="utf-8"
        )
        second = import_plugin_class(plugin)

        assert first is not second
        assert second.greeting == "改过了"

    def test_missing_module_is_reported_with_the_path(self, tmp_path: Path) -> None:
        plugin = install_local_plugin(tmp_path)
        (plugin.path / "plugin.py").unlink()  # type: ignore[operator]

        with pytest.raises(PluginManifestError) as caught:
            import_plugin_class(plugin)

        assert "无法导入" in caught.value.message
        assert str(plugin.path / "plugin.py") in caught.value.context["hint"]

    def test_syntax_error_is_reported_as_a_load_failure(self, tmp_path: Path) -> None:
        """语法错误是插件作者手滑，不是清单写错——错误信息要给到文件与行号。"""
        plugin = install_local_plugin(tmp_path, body="def broken(:\n")

        with pytest.raises(PluginLoadError) as caught:
            import_plugin_class(plugin)

        assert caught.value.context["plugin"] == "channel.demo"
        assert caught.value.context["line"] == 1
        assert caught.value.context["file"].endswith("plugin.py")
        assert "自动重载" in caught.value.context["hint"]

    def test_missing_class(self, tmp_path: Path) -> None:
        plugin = install_local_plugin(tmp_path, body="X = 1\n")

        with pytest.raises(PluginManifestError) as caught:
            import_plugin_class(plugin)

        assert "类不存在" in caught.value.message
        assert caught.value.context["class_name"] == "DemoChannel"

    def test_class_must_be_a_plugin(self, tmp_path: Path) -> None:
        plugin = install_local_plugin(tmp_path, body="class DemoChannel:\n    pass\n")

        with pytest.raises(PluginManifestError) as caught:
            import_plugin_class(plugin)

        assert "不是 Plugin 的子类" in caught.value.message
        assert "Plugin" in caught.value.context["hint"]

    def test_missing_third_party_dependency_is_a_load_failure(self, tmp_path: Path) -> None:
        # 插件少装了一个包，问题不在 plugin.toml 里。
        plugin = install_local_plugin(
            tmp_path, body="import nonexistent_package_xyz\n\nclass DemoChannel: pass\n"
        )

        with pytest.raises(PluginLoadError) as caught:
            import_plugin_class(plugin)

        assert caught.value.context["module"] == "nonexistent_package_xyz"


# ── 依赖编排 ────────────────────────────────────────────────


class TestResolveLoadOrder:
    def catalog(self, *plugins: DiscoveredPlugin) -> dict[str, DiscoveredPlugin]:
        return {plugin.id: plugin for plugin in plugins}

    def test_single_plugin(self) -> None:
        catalog = self.catalog(make_discovered("channel.a"))

        resolution = resolve_load_order(catalog, ["channel.a"])

        assert resolution.order == ["channel.a"]
        assert resolution.auto_enabled == []
        assert resolution.skipped_optional == {}

    def test_enabled_plugin_must_exist(self) -> None:
        with pytest.raises(PluginDependencyError) as caught:
            resolve_load_order(self.catalog(), ["channel.ghost"])

        assert caught.value.context["missing"] == ["channel.ghost"]
        assert caught.value.context["available"] == []
        assert "plugins doctor" in caught.value.context["hint"]

    def test_dependencies_may_not_be_enabled_explicitly(self) -> None:
        # 用户只需要列出他想要的那几个；依赖会被自动带上。
        catalog = self.catalog(
            make_discovered("channel.a", requires=["storage.db"]),
            make_discovered("storage.db"),
        )

        resolution = resolve_load_order(catalog, ["channel.a"])

        assert resolution.auto_enabled == ["storage.db"]
        assert resolution.order == ["storage.db", "channel.a"]

    def test_auto_enable_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        catalog = self.catalog(
            make_discovered("channel.a", requires=["storage.db"]),
            make_discovered("storage.db"),
        )

        with caplog.at_level(logging.WARNING):
            resolve_load_order(catalog, ["channel.a"], logger=LOGGER)

        assert "自动启用" in caplog.text

    def test_dependency_chain_is_expanded_transitively(self) -> None:
        catalog = self.catalog(
            make_discovered("channel.a", requires=["capability.b"]),
            make_discovered("capability.b", requires=["storage.c"]),
            make_discovered("storage.c"),
        )

        resolution = resolve_load_order(catalog, ["channel.a"])

        assert resolution.order == ["storage.c", "capability.b", "channel.a"]
        assert sorted(resolution.auto_enabled) == ["capability.b", "storage.c"]

    def test_missing_dependency_is_reported(self) -> None:
        catalog = self.catalog(make_discovered("channel.a", requires=["storage.ghost"]))

        with pytest.raises(PluginDependencyError) as caught:
            resolve_load_order(catalog, ["channel.a"])

        assert caught.value.context["plugin"] == "channel.a"
        assert caught.value.context["requires"] == "storage.ghost"

    def test_version_mismatch_is_reported(self) -> None:
        catalog = self.catalog(
            make_discovered("channel.a", requires=["storage.db >= 0.5.0"]),
            make_discovered("storage.db", version="0.1.0"),
        )

        with pytest.raises(PluginDependencyError) as caught:
            resolve_load_order(catalog, ["channel.a"])

        assert caught.value.context["found"] == "0.1.0"

    def test_version_satisfied_passes(self) -> None:
        catalog = self.catalog(
            make_discovered("channel.a", requires=["storage.db >= 0.1.0"]),
            make_discovered("storage.db", version="0.1.0"),
        )

        assert resolve_load_order(catalog, ["channel.a"]).order == ["storage.db", "channel.a"]

    def test_cycle_names_every_plugin_on_it(self) -> None:
        catalog = self.catalog(
            make_discovered("channel.a", requires=["capability.b"]),
            make_discovered("capability.b", requires=["channel.a"]),
        )

        with pytest.raises(PluginDependencyError) as caught:
            resolve_load_order(catalog, ["channel.a"])

        cycle = caught.value.context["cycle"]
        assert "成环" in caught.value.message
        # 从哪个节点开始报是确定的（同层按 id 排序），但断言「环的内容」
        # 比断言「从哪开始」更有意义。
        assert cycle[0] == cycle[-1]
        assert set(cycle) == {"channel.a", "capability.b"}
        assert "capability.b -> channel.a -> capability.b" in caught.value.context["hint"]

    def test_self_dependency_is_a_cycle(self) -> None:
        catalog = self.catalog(make_discovered("channel.a", requires=["channel.a"]))

        with pytest.raises(PluginDependencyError) as caught:
            resolve_load_order(catalog, ["channel.a"])

        assert caught.value.context["cycle"] == ["channel.a", "channel.a"]

    def test_missing_optional_dependency_is_skipped(self) -> None:
        # optional 缺失不是错误，但要让用户知道功能会降级。
        catalog = self.catalog(make_discovered("channel.a", optional=["tool.ghost"]))

        resolution = resolve_load_order(catalog, ["channel.a"])

        assert resolution.skipped_optional == {"channel.a": ["tool.ghost"]}
        assert resolution.order == ["channel.a"]

    def test_skipped_optional_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        catalog = self.catalog(make_discovered("channel.a", optional=["tool.ghost"]))

        with caplog.at_level(logging.INFO):
            resolve_load_order(catalog, ["channel.a"], logger=LOGGER)

        assert "功能可能降级" in caplog.text

    def test_present_optional_is_not_reported(self) -> None:
        catalog = self.catalog(
            make_discovered("channel.a", optional=["tool.b"]),
            make_discovered("tool.b"),
        )

        resolution = resolve_load_order(catalog, ["channel.a"])

        assert resolution.skipped_optional == {}
        # 没被 requires 也没被启用，所以不该进加载列表。
        assert resolution.order == ["channel.a"]

    def test_optional_is_not_auto_enabled(self) -> None:
        catalog = self.catalog(
            make_discovered("channel.a", optional=["tool.b"]), make_discovered("tool.b")
        )

        assert "tool.b" not in resolve_load_order(catalog, ["channel.a"])

    def test_order_is_deterministic_and_sorted_within_a_layer(self) -> None:
        catalog = self.catalog(
            make_discovered("channel.c"), make_discovered("channel.a"), make_discovered("channel.b")
        )

        assert resolve_load_order(catalog, ["channel.c", "channel.a", "channel.b"]).order == [
            "channel.a",
            "channel.b",
            "channel.c",
        ]

    def test_dependents_come_after_all_their_dependencies(self) -> None:
        catalog = self.catalog(
            make_discovered("channel.a", requires=["capability.b", "storage.c"]),
            make_discovered("capability.b"),
            make_discovered("storage.c"),
        )

        order = resolve_load_order(catalog, ["channel.a"]).order

        assert order[-1] == "channel.a"
        assert set(order[:2]) == {"capability.b", "storage.c"}

    def test_accepts_a_discovery_result(self) -> None:
        result = DiscoveryResult(
            plugins={
                "channel.a": make_discovered("channel.a", requires=["storage.db"]),
                "storage.db": make_discovered("storage.db"),
            }
        )

        assert resolve_load_order(result, ["channel.a"]).order == ["storage.db", "channel.a"]

    def test_resolution_contains(self) -> None:
        resolution = resolve_load_order(self.catalog(make_discovered("channel.a")), ["channel.a"])

        assert "channel.a" in resolution

    def test_exit_code_constant_matches_the_design_doc(self) -> None:
        from alterego.kernel.loader import EXIT_DEPENDENCY_ERROR

        assert EXIT_DEPENDENCY_ERROR == 3

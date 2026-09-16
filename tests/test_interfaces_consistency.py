"""接口一致性：包导出的名字、模块声明的名字、代码里的字面量三者必须对得上。

这个文件是「检查所有的接口是否一致」这句话的可执行版本。人眼审计只能证明
「今天是一致的」；下面这几条测试证明「明天改坏了会红」。

它守着四件事：

1. 每个接口模块的 ``__all__`` **覆盖它的每一个模块级公开名**。
   ``Direction`` / ``ChannelCapability`` / ``MessageKind`` 就是漏在这儿的——
   它们是 ``Channel`` 协议三个字段的类型，是契约的一部分，却不在 ``__all__`` 里。
   后果不是报错，而是「照文档写类型注解」必须靠翻源码才能做到。
2. ``alterego.interfaces.__init__`` 转发六个子模块的全部公开名，**一个不漏**。
   部分转发比完全不转发更坏：拿到 ``ImportError`` 的人先去怀疑自己写错了。
   要么全导，要么一个都不导，没有中间地带。
3. ``kernel/plugin.py`` 这个门面导出的每个名字都真实存在，而且来源模块自己也声明了它。
4. 字面量型枚举与文档逐字一致：``PluginKind`` 八类、``ConfigValueType`` 八种、
   清单允许的键 = ``PluginManifest`` 的字段减去发现过程填的那两个。

依据: docs/design/13-interface-consistency.md
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import pkgutil
from pathlib import Path
from typing import Any, get_args

import pytest

import alterego.interfaces
from alterego.kernel import manifest as manifest_module
from alterego.kernel.plugin import API_VERSION, PluginManifest


pytestmark = pytest.mark.architecture

SRC = Path(__file__).resolve().parent.parent / "src" / "alterego"
INTERFACES = SRC / "interfaces"

#: 六个契约模块。``__init__`` 不在其中——它是门面，不是契约本身。
SUBMODULES = ("channel", "common", "llm", "repository", "simulation", "storage")


def _public_module_level_names(path: Path) -> set[str]:
    """一个模块里所有「看起来是公开的」模块级定义名。

    只取 ``ClassDef`` 与赋值语句的目标：import 进来的名字不会被算进来
    （那正是这一条要区分的——「定义了什么」而不是「借来了什么」）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            found.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.add(node.target.id)
    return {name for name in found if not name.startswith("_")}


@pytest.mark.parametrize("module_name", SUBMODULES)
def test_module_all_covers_every_public_name(module_name: str) -> None:
    """``__all__`` 漏掉一个公开名 = 那个名字在包外面拿不到（P2 显式优于隐式）。"""
    module = importlib.import_module(f"alterego.interfaces.{module_name}")
    declared: set[str] = set(module.__all__)
    defined = _public_module_level_names(INTERFACES / f"{module_name}.py")

    missing = sorted(defined - declared)
    assert not missing, (
        f"alterego.interfaces.{module_name} 定义了但没导出：{missing}。"
        f"它们是契约的一部分就该进 __all__；是内部细节就该以 _ 开头。"
    )


@pytest.mark.parametrize("module_name", SUBMODULES)
def test_every_exported_name_resolves(module_name: str) -> None:
    """``__all__`` 里写了一个不存在的名字，是 ``from x import *`` 才会暴露的错。"""
    module = importlib.import_module(f"alterego.interfaces.{module_name}")
    missing = sorted(name for name in module.__all__ if not hasattr(module, name))
    assert not missing, f"alterego.interfaces.{module_name}.__all__ 里的名字取不到：{missing}"


@pytest.mark.parametrize("module_name", SUBMODULES)
def test_module_all_is_sorted(module_name: str) -> None:
    """``__all__`` 的排序由 ruff 的 RUF022 把关，这里不再复刻一份。

    刻意写成一个「断言 ruff 管过的事」的占位反而有害：两套排序规则哪天不一致，
    就会出现「ruff 说对、测试说错」的假红。真权威是 ``ruff check``（门禁第 2 道）。
    """
    module = importlib.import_module(f"alterego.interfaces.{module_name}")
    assert len(set(module.__all__)) == len(module.__all__), (
        f"alterego.interfaces.{module_name}.__all__ 里有重复名字：{module.__all__}"
    )


def test_package_all_is_the_union_of_submodule_alls() -> None:
    """门面要么全导，要么一个都不导——部分转发是最坏的那种。"""
    union: set[str] = set()
    for module_name in SUBMODULES:
        union.update(importlib.import_module(f"alterego.interfaces.{module_name}").__all__)

    assert set(alterego.interfaces.__all__) == union, (
        "alterego.interfaces.__all__ 与六个子模块的并集不一致。"
        "门面漏掉一个名字，插件作者拿到的是 ImportError 而不是那个类。"
    )


def test_package_all_has_no_duplicates() -> None:
    assert len(set(alterego.interfaces.__all__)) == len(alterego.interfaces.__all__)


def test_interfaces_submodule_list_is_complete() -> None:
    """新增一个接口模块却不进 ``SUBMODULES``，上面四条就全都检查不到它。"""
    on_disk = {
        info.name
        for info in pkgutil.iter_modules(alterego.interfaces.__path__)
        if info.name != "__init__"
    }
    assert on_disk == set(SUBMODULES), (
        f"interfaces/ 下的模块是 {sorted(on_disk)}，而测试只检查 {sorted(SUBMODULES)}。"
        "新增一个契约模块时，把它加进本文件的 SUBMODULES。"
    )


def test_plugin_facade_exports_resolve() -> None:
    """``kernel/plugin.py`` 是插件唯一需要 import 的模块，导出的名字必须真存在。"""
    facade = importlib.import_module("alterego.kernel.plugin")
    missing = sorted(name for name in facade.__all__ if not hasattr(facade, name))
    assert not missing, f"alterego.kernel.plugin.__all__ 里的名字取不到：{missing}"


def test_plugin_kinds_match_the_documented_eight() -> None:
    """``docs/design/02-plugin-api.md`` § 2 列了八类，代码就该是这八类。

    ``image`` 与 ``source`` 的接口要到 v0.2.0 / v0.3.0 才落地，但清单层面今天
    就接受它们——理由是 ``kind`` 是纯元数据，且照文档写的 ``plugin.toml``
    不该在解析阶段被拒。哪一类「今天能用」是文档的事，不是这个集合的事。
    """
    documented = {
        "llm",
        "storage",
        "channel",
        "capability",
        "stage",
        "tool",
        "image",
        "source",
    }
    assert set(get_args(manifest_module.PluginKind)) == documented
    assert documented == manifest_module._KINDS, (
        "Literal 与 _KINDS 必须同步：_KINDS 才是校验真正用的那个，"
        "只改 Literal 等于没改（mypy 管不到运行时的 TOML）。"
    )


def test_config_value_types_match_the_documented_eight() -> None:
    assert set(get_args(manifest_module.ConfigValueType)) == manifest_module._VALUE_TYPES


def test_manifest_known_keys_are_derived_from_the_dataclass() -> None:
    """允许的清单键从数据类推导，所以「加了字段忘了放开」不可能发生。

    这条测试守的是推导本身：如果有人把手抄的清单写回去，它立刻红。
    """
    fields = {f.name for f in dataclasses.fields(PluginManifest)}
    expected = fields - {"path", "source"}
    assert expected == manifest_module._KNOWN_MANIFEST_KEYS, (
        "_KNOWN_MANIFEST_KEYS 不再等于「数据类字段减去 path/source」。"
        "path 与 source 由发现过程填，不是插件作者写的。"
    )


def test_unknown_manifest_key_is_rejected() -> None:
    """拼错的顶层键必须当场报错，而不是被静默忽略。

    ``enabledByDefault = false`` 是最典型的例子：一个只想写「这个插件默认别开」
    的作者，会得到一个默认开着的插件。这类「配错了反而更开放」的降级最难发现。
    """
    data: dict[str, Any] = {
        "id": "tool.demo",
        "version": "0.1.0",
        "api_version": API_VERSION,
        "kind": "tool",
        "entry": "plugin:Demo",
        "enabledByDefault": False,
    }
    with pytest.raises(manifest_module.PluginManifestError) as caught:
        PluginManifest.parse(data)

    assert caught.value.context["unknown"] == ["enabledByDefault"]
    assert "enabled_by_default" in caught.value.context["supported"]

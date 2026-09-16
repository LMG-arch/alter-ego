"""设置元数据的目录 —— 每个配置字段那一条「改了会怎样」。

## 这里装的是什么

:mod:`alterego.kernel.settings` 定义 **值类型**（``Setting`` / ``Choice`` /
``SettingKind``），这里装 **内容**：``SETTING_METADATA`` 以及围着它的几个查询。

分开是为了方向。目录必须认识全部配置类，而配置类不该反过来认识目录；
挤在一起就只能靠「导入时产生副作用」把它们接上，而本项目不要隐式机制（P2）。

## 为什么拆成三个文件

一共九十几条元数据，每条带一句真实的 ``effect``，写在一个文件里会顶到
900 行——而 900 行是硬红线（``scripts/check_architecture.sh`` 第 23 项），
不是「建议」。所以按**谁来用**拆：

| 模块 | 管什么 |
| --- | --- |
| :mod:`~alterego.kernel.settings_catalog_agent` | 它怎么活：常规、推演、打扰预算、人设、学习、设置中心 |
| :mod:`~alterego.kernel.settings_catalog_model` | 它花多少钱、学什么、导出什么 |
| :mod:`~alterego.kernel.settings_catalog_infra` | 它把东西放哪、往哪说话、装什么插件 |

三个模块都只导出 ``SETTINGS: tuple[Setting, ...]``，不认识注册表，
因此不存在循环导入。

## 段路径是走出来的，不是写下来的

``CONFIG_DATACLASSES`` 和各段的路径由 :func:`_collect` 从 :class:`Config`
递归走一遍得到。手维护一张「有哪些配置段」的清单是注定要腐烂的：
新增一个段却忘了登记，设置页就会把它整段显示成「未知项」，
而写代码的人不会收到任何提示。走一遍就没有这个问题。

## 键写错会立刻炸

:func:`_register` 会核对两件事：键的前半段必须是一个真实存在的段路径，
后半段必须是那个段里真实存在的字段。任何一条不成立就在**导入时**抛
``ValueError``——不是等 CI 跑测试，也不是等用户在页面上看见一个空白项。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import fields, is_dataclass
from typing import Any

from alterego.kernel.config import Config
from alterego.kernel.config_values import type_hints
from alterego.kernel.settings import Setting
from alterego.kernel.settings_catalog_agent import SETTINGS as _AGENT_SETTINGS
from alterego.kernel.settings_catalog_infra import SETTINGS as _INFRA_SETTINGS
from alterego.kernel.settings_catalog_model import SETTINGS as _MODEL_SETTINGS


__all__ = [
    "CONFIG_DATACLASSES",
    "SETTING_METADATA",
    "all_settings",
    "get_setting_metadata",
    "groups",
    "section_paths",
    "settings_for",
]


# ═══════════════════════════════════════════════════════════════════════
#  段：从 Config 走一遍得到
# ═══════════════════════════════════════════════════════════════════════


#: 段路径 → 配置类。``""`` 是 :class:`Config` 自己（顶层不是段，不参与元数据）。
_SECTIONS: dict[str, type[Any]] = {}


def _collect(path: str, cls: type[Any]) -> None:
    """递归收集所有配置段，保留声明顺序。"""
    if path in _SECTIONS:
        return
    _SECTIONS[path] = cls
    hints = type_hints(cls)
    for info in fields(cls):
        annotation = hints.get(info.name)
        if isinstance(annotation, type) and is_dataclass(annotation):
            nested = f"{path}.{info.name}" if path else info.name
            _collect(nested, annotation)


_collect("", Config)

#: 全部配置段（不含 :class:`Config` 自己），顺序 = 声明顺序。
CONFIG_DATACLASSES: tuple[type[Any], ...] = tuple(cls for path, cls in _SECTIONS.items() if path)

#: 段路径 → 配置类，供 ``cli_config`` 把 ``llm.routing.decision`` 拆成「段 + 字段」。
section_paths: Mapping[str, type[Any]] = {path: cls for path, cls in _SECTIONS.items() if path}


# ═══════════════════════════════════════════════════════════════════════
#  注册表
# ═══════════════════════════════════════════════════════════════════════


#: 配置类 → 字段名 → 元数据。键是类而不是段路径，是为了让
#: ``get_setting_metadata(CoreConfig, "timezone")`` 这种调用不用先算路径。
SETTING_METADATA: dict[type[Any], dict[str, Setting]] = {}


def _register(entries: Iterable[Setting]) -> None:
    """把一批元数据挂进注册表，顺手核对键与字段。

    Raises:
        ValueError: 键的前半段不是段路径，或后半段不是该段的字段，或键重复。
    """
    for setting in entries:
        parent, _, name = setting.key.rpartition(".")
        cls = _SECTIONS.get(parent)
        if cls is None:
            known = ", ".join(sorted(path for path in _SECTIONS if path))
            raise ValueError(f"设置项 {setting.key} 的段 {parent!r} 不存在；已知的段：{known}")
        declared = {info.name for info in fields(cls)}
        if name not in declared:
            raise ValueError(
                f"设置项 {setting.key} 指向 {cls.__name__}，但它没有字段 {name!r}；"
                f"它有：{', '.join(sorted(declared))}"
            )
        bucket = SETTING_METADATA.setdefault(cls, {})
        if name in bucket:
            raise ValueError(f"设置项 {setting.key} 被注册了两次")
        bucket[name] = setting


# 顺序即 ``all_settings()`` 的顺序，也就是设置页/CLI 分组的顺序：
# 先「它怎么活」，再「它花多少钱」，最后「它把东西放哪」。
_register(_AGENT_SETTINGS)
_register(_MODEL_SETTINGS)
_register(_INFRA_SETTINGS)


# ═══════════════════════════════════════════════════════════════════════
#  查询
# ═══════════════════════════════════════════════════════════════════════


def get_setting_metadata(cls: type[Any], name: str) -> Setting | None:
    """取某个配置字段的元数据。没有就返回 ``None``（**不猜**）。

    猜是 :func:`alterego.kernel.settings.infer_setting` 的事，由调用方明确决定
    要不要退到那一层。这里混着做，就没人分得清「标注过」和「猜出来的」了。
    """
    return SETTING_METADATA.get(cls, {}).get(name)


def settings_for(cls: type[Any]) -> Mapping[str, Setting]:
    """某个配置段的全部元数据（字段名 → 元数据）。"""
    return dict(SETTING_METADATA.get(cls, {}))


def all_settings() -> tuple[Setting, ...]:
    """全部元数据，按注册顺序（= 分组的顺序）。"""
    return tuple(setting for bucket in SETTING_METADATA.values() for setting in bucket.values())


def groups() -> tuple[str, ...]:
    """出现过的分组名，按首次出现的顺序。

    顺序由注册顺序决定，因此「预算」永远排在「模型」前面——
    不做字典序排序：``sorted`` 会让中文分组名按 Unicode 码点排，
    结果既不是用户预期也不是任何人的意图。
    """
    seen: dict[str, None] = {}
    for setting in all_settings():
        if setting.group:
            seen.setdefault(setting.group, None)
    return tuple(seen)

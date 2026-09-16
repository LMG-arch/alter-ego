"""设置项的展示元数据 —— 让「改之前就知道会发生什么」变成机制（P3）。

## 这个模块解决什么问题

用户打开设置页看到::

    daily_message_limit = 3
    on_exceed = "degrade"
    min_interval_minutes = 90

他会问四个问题，而配置文件一个都答不了：``3`` 是「最多 3 条」还是「3 条以内
最好」？``degrade`` 还有别的选法吗？各自什么后果？我最多个小时收一条消息，
这跟「每天 3 条」哪个先触发？

**改错设置的代价比找不到设置高得多。** 一个用户把 ``min_interval_minutes``
从 90 改成 5，第二天收到 20 条消息，他会认为这个项目坏了——而其实是他自己改的。
所以设计目标不是「能改」，而是**改之前就知道会发生什么**，见
``docs/design/10-settings-center.md`` § 1，依据是
``docs/adr/0010-every-setting-carries-display-metadata.md``。

## 为什么目录不在这里

``Setting`` / ``Choice`` / ``SettingKind`` 是**值类型**，住在这里；
而「每个配置字段的那一条元数据」住在 :mod:`alterego.kernel.settings_catalog`。

分开的理由是**方向**：目录必须 import 全部配置类，而配置类不该反过来依赖目录。
如果目录和类型挤在同一个模块里，要么出现循环导入，要么得靠「导入时产生副作用」
这种隐式机制——本项目明确不要隐式（P2）。所以：

- 想拿一条元数据 → ``from alterego.kernel.settings_catalog import get_setting_metadata``
- 想构造一条元数据 → ``from alterego.kernel.settings import Setting, SettingKind, Choice``

## ``description`` 与 ``effect`` 的分工

| 字段 | 回答的问题 | 例子 |
| --- | --- | --- |
| ``description`` | **这是什么** | 「每天主动给你发消息的次数上限（不含回复你的消息）」 |
| ``effect`` | **改了会怎样** | 「调高会让它更频繁地找你；设为 0 则它只在回复你时说话。」 |

只有 ``description`` 的页面是说明书，用户读完还是不知道该不该改。
``effect`` 必须是**可验证的因果陈述**，不是营销话术：
❌「影响拟人程度」 ✅「设为 0 则它只在回复你时说话」。
``tests/test_settings_metadata.py`` 会把这条要求变成 CI 断言。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import MISSING, dataclass, fields, is_dataclass
from datetime import time
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

from alterego.kernel.config import _SECRET_KEY_PATTERN
from alterego.kernel.config_values import type_hints


__all__ = ["Choice", "Setting", "SettingKind", "infer_setting", "is_annotated"]


class SettingKind(StrEnum):
    """一个设置项该用什么控件渲染。

    值刻意与 ``plugin.toml`` 里 ``[config.<键>] type`` 的写法一致：
    插件作者写 ``type = "enum"`` 时看到的就是这里的 ``SettingKind.ENUM``，
    ``Setting`` 与 ``ConfigField`` 共用渲染器才不需要一张对照表
    （``docs/design/10-settings-center.md`` § 2.3）。
    """

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"
    ENUM = "enum"
    DURATION = "duration"
    PATH = "path"
    SECRET = "secret"
    LIST = "list"
    MAPPING = "mapping"


@dataclass(frozen=True, slots=True)
class Choice:
    """枚举值的一个选项。

    ``consequence`` 不是装饰：下拉框里一排 ``degrade`` / ``stop`` / ``warn``
    对用户等于没写。每个选项都要说清「选了它会发生什么」，
    ``tests/test_settings_metadata.py`` 会断言它非空。
    """

    value: str
    """写进配置文件的那个字面量。"""

    label: str
    """中文短名：「降级但不停止」。"""

    consequence: str
    """「超出预算后改用便宜模型，Agent 继续生活，只是变笨」。"""


@dataclass(frozen=True, slots=True)
class Setting:
    """一个配置项的完整展示元数据。

    ``default`` / ``minimum`` / ``maximum`` 是**给界面用的**，不参与加载校验：
    真正在加载时把关的是各配置类的 ``__post_init__``。两处写不一致时以
    ``__post_init__`` 为准——那是唯一能拦住「配置文件被手工改坏」的地方。
    """

    key: str
    """点分路径，如 ``"llm.routing.decision"``。**全局唯一**。"""

    label: str
    """界面上那一行的标题：「意图决策用哪个模型」。"""

    description: str
    """这个设置**是什么**。"""

    kind: SettingKind
    default: Any = None
    group: str = ""
    """设置页分组：「模型」「打扰预算」。同名即同组，渲染时按首次出现排序。"""

    choices: tuple[Choice, ...] = ()
    """``kind = enum`` 时必填。"""

    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    """「分钟」「美元」「条」「张」。"""

    effect: str = ""
    """**改了会发生什么**。空表示「这条是推断出来的，没人标注过」。"""

    depends_on: str = ""
    """例：「仅在 mode = fast 时生效」。"""

    requires_restart: bool = False
    danger: bool = False
    """改错会让 Agent 行为异常（``data_dir`` / ``random_seed`` / ``simulation.mode``）。"""

    advanced: bool = False
    """默认折叠。"""

    def __post_init__(self) -> None:
        segments = self.key.split(".")
        if len(segments) < 2 or not all(segments):
            # 空段（``"core."`` / ``"a..b"``）与没有段名（``"log_level"``）都要拦：
            # 这个键是拿来找「写进哪一段的哪一行」的，空段会让它落进别的段里。
            raise ValueError(f"设置项的 key 必须是「段.键」这样的点分路径，拿到的是 {self.key!r}")
        if self.kind is SettingKind.ENUM and not self.choices:
            # 枚举没有选项 = 用户面对一个空白下拉框，且无从知道合法值是什么。
            # 这属于「写元数据的人写漏了」，必须在导入时就炸，不能等到渲染。
            raise ValueError(f"{self.key} 是枚举但没有 choices")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(f"{self.key} 的 minimum > maximum")


def is_annotated(setting: Setting) -> bool:
    """这条元数据是人标注过的，还是 :func:`infer_setting` 猜出来的。

    猜出来的项在界面上要带「未标注」标记——用户看到自己的配置有黄标，
    比看到自己的配置**不见了**要好。
    """
    return bool(setting.effect)


def infer_setting(
    cls: type[Any], name: str, *, prefix: str = "", value: Any = None
) -> Setting | None:
    """从一个 dataclass 字段的类型和默认值猜一条元数据。

    这是元数据的**第三层**（``docs/design/10-settings-center.md`` § 3）。
    前两层是内核 ``Setting`` 与插件 ``plugin.toml``；这一层存在的唯一理由是
    用户会自己在 ``alterego.toml`` 里写内核不认识的键，而设置页遇到未知键
    不能报错也不能静默忽略——那会让用户觉得「我的配置丢了」。

    猜不出来返回 ``None``，调用方据此标成「无法展示」，只在「高级」里以
    TOML 原文出现。**不要为了「总能猜出来」而回落成字符串**：把一个布尔值
    当字符串渲染，用户会以为这个开关坏了。

    Args:
        cls: 字段所属的配置类。
        name: 字段名。
        prefix: 点分路径的前半段（段名）。留空视为「猜不出来」——
            没有段名的键写不进任何一段，谈不上展示。
        value: 当前值，只用来填 ``default``。

    Returns:
        猜出来的 :class:`Setting`（``effect`` 为空，即「未标注」）；
        类型认不出来时返回 ``None``。
    """
    if value is None:
        for info in fields(cls):
            if info.name == name:
                value = info.default if info.default is not MISSING else None
                break
    annotation = type_hints(cls).get(name)
    if annotation is None:
        return None
    if not prefix:
        # 没有段名就没有归属：这个键在 TOML 里只能写在文件顶层，而配置模型里
        # 没有顶层设置项，设置页也无处安放它。返回「猜不出来」比返回一个
        # 写不进任何段的键好——后者会在保存时才发现。
        return None
    key = f"{prefix}.{name}"
    kind = _kind_of(annotation)
    if kind is None:
        return None
    if _SECRET_KEY_PATTERN.search(name):
        # 名字里带 api_key / token / secret 的，一律按密钥渲染：只显示状态，不回显值。
        # 判断依据与 ``Config.to_dict(redact=True)`` 完全一致——同一个正则，不是抄了一份。
        kind = SettingKind.SECRET
    return Setting(
        key=key,
        label=name,
        description="",
        kind=kind,
        default=value,
        choices=_choices_of(annotation),
        advanced=True,
    )


def _kind_of(annotation: Any) -> SettingKind | None:
    """类型注解 → 控件种类。认不出来返回 ``None``。"""
    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is Literal:
        return SettingKind.ENUM if all(isinstance(arg, str) for arg in args) else None
    if origin is not None:
        if origin in (tuple, frozenset, list):
            return SettingKind.LIST
        # ``Mapping[str, Any]`` / ``dict`` 这类开放容器：内容由使用方解释
        if isinstance(origin, type) and issubclass(origin, Mapping):
            return SettingKind.MAPPING
        # ``int | None`` / ``str | None``：按非 None 的那一侧算
        candidates = [arg for arg in args if arg is not type(None)]
        return _kind_of(candidates[0]) if len(candidates) == 1 else None
    if is_dataclass(annotation):
        return SettingKind.MAPPING
    if annotation is bool:
        return SettingKind.BOOL
    if annotation is int:
        return SettingKind.INT
    if annotation is float:
        return SettingKind.FLOAT
    if annotation is Path:
        return SettingKind.PATH
    if annotation is time:
        return SettingKind.DURATION
    if annotation is str:
        return SettingKind.STR
    return None


def _choices_of(annotation: Any) -> tuple[Choice, ...]:
    """``Literal`` 的字面量直接变成选项。

    ``consequence`` 故意留空：猜出来的选项没人能说明后果，
    硬编一句「见文档」是假信息。空 consequence 配上空的 ``effect``
    一起来标记「未标注」，界面会提示用户这里需要补。
    """
    if get_origin(annotation) is not Literal:
        return ()
    return tuple(Choice(value=arg, label=arg, consequence="") for arg in get_args(annotation))

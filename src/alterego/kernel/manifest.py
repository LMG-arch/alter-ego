"""插件清单与插件配置：``plugin.toml`` 的解析、校验与取值。

它是 ``kernel/plugin.py`` 拆出来的一半（另一半是 :mod:`alterego.kernel.context`）。
拆的理由不是「文件太大」，而是**清单只需要更少的知识**：它不认识上下文、不认识插件
基类，所以可以单独被覆盖，也更容易看出「哪些信息是声明式的」。

职责边界：

- :class:`PluginManifest` —— 一份 ``plugin.toml`` 解析成的只读事实。
- :class:`ConfigField` —— 某个配置字段的声明（类型、默认值、是否必填）。
- :func:`resolve_config` —— 把「清单默认值 / 用户配置 / 环境变量」三层合成最终值。

**这里不知道任何具体插件的存在**（设计原则 P1）。

依据: docs/design/02-plugin-api.md § 3
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from alterego.kernel.errors import PluginManifestError


__all__ = [
    "API_VERSION",
    "MASK",
    "ConfigField",
    "ConfigValueType",
    "PluginKind",
    "PluginManifest",
    "PluginStatus",
    "is_api_version_compatible",
    "parse_duration",
    "resolve_config",
]


#: 当前插件 API 主版本。递增它即代表不兼容变更。
#:
#: 兼容窗口是「向下一个大版本」（见 § 1.3）：内核 2 能跑 api_version=1 的插件，
#: 内核 1 跑不了 api_version=2 的插件。
API_VERSION: int = 1

#: 插件配置字段被脱敏后的占位符。
MASK: str = "***"

#: 插件类型。**八类，与 ``docs/design/02-plugin-api.md`` § 2 逐字一致。**
#:
#: ``image`` 与 ``source`` 的接口（``interfaces/image.py`` / ``interfaces/source.py``）
#: 要到 v0.2.0 / v0.3.0 才落地（见 ``docs/design/06-roadmap.md`` 阶段 L / M），
#: 但**清单层面今天就接受它们**——理由有三条：
#:
#: 1. ``kind`` 目前是**纯元数据**：内核只用它来显示，以及措辞一句
#:    「entry 指错地方了」的提示（见 ``loader._entry_hint``）。内核**不校验**
#:    「声明 channel 就必须注册 Channel」——今天不校验，以后也不该用一个
#:    半吊子的校验器代替（没校验可查，错校验难查）。
#: 2. 一份照设计文档写出来的 ``plugin.toml`` 不该在解析阶段被拒。拒绝它会让
#:    「文档说要写 kind = "image"」和「内核说这不是合法 kind」同时为真，
#:    而插件作者无从判断该信哪一个。
#: 3. 加两个字符串的成本是零：没有任何分支读这两个值。
#:
#: 「能通过解析」不等于「今天能用」——那张「今日可用」的表在
#: ``docs/guide/plugin-development.md`` § 1.2 与 ``docs/design/13-interface-consistency.md``。
PluginKind = Literal["llm", "storage", "channel", "capability", "stage", "tool", "image", "source"]
ConfigValueType = Literal[
    "string", "integer", "number", "boolean", "array", "object", "duration", "path"
]

_KINDS: frozenset[str] = frozenset(
    {"llm", "storage", "channel", "capability", "stage", "tool", "image", "source"}
)
_VALUE_TYPES: frozenset[str] = frozenset(
    {"string", "integer", "number", "boolean", "array", "object", "duration", "path"}
)

#: 插件 id 形如 ``<kind>.<name>``：全小写 + 下划线。
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
#: ``entry`` 形如 ``plugin:ClassName`` 或 ``pkg.mod:ClassName``。
_ENTRY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)$")
_DURATION_UNITS: dict[str, float] = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}


class PluginStatus(StrEnum):
    """插件在生命周期中的位置。名字与设计文档 § 4.2 的状态机一一对应。"""

    DISCOVERED = "discovered"
    VALIDATED = "validated"
    LOADING = "loading"
    LOADED = "loaded"
    STARTED = "started"
    STOPPED = "stopped"
    UNLOADED = "unloaded"
    FAILED = "failed"


# ── 配置字段 ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ConfigField:
    """``plugin.toml`` 里 ``[config]`` 的一个字段声明。

    声明式的好处是**错误信息可以在插件加载前就准备好**：用户少填一个字段时，
    我们连「改哪个文件的哪一节」都能直接告诉他。
    """

    name: str
    type: ConfigValueType = "string"
    required: bool = False
    default: Any = None
    description: str = ""
    #: ``True`` 时该字段在日志、CLI、Web 中**永远**显示为 ``***``。
    secret: bool = False
    #: 从哪个环境变量读取。环境变量优先于用户配置文件（容器部署友好）。
    env: str | None = None
    choices: tuple[Any, ...] = ()
    min: float | None = None
    max: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    item_type: ConfigValueType | None = None
    min_items: int | None = None
    max_items: int | None = None
    must_exist: bool = False

    def __post_init__(self) -> None:
        if self.type not in _VALUE_TYPES:
            raise PluginManifestError(
                "配置字段的 type 非法",
                field=self.name,
                type=self.type,
                supported=sorted(_VALUE_TYPES),
            )
        if self.item_type is not None and self.item_type not in _VALUE_TYPES:
            raise PluginManifestError(
                "配置字段的 item_type 非法",
                field=self.name,
                item_type=self.item_type,
                supported=sorted(_VALUE_TYPES),
            )
        if self.required and self.default is not None:
            # 两者互斥：同时存在时「缺省值」到底算不算「已填」没人说得清。
            raise PluginManifestError(
                "配置字段不能同时声明 required = true 与 default",
                field=self.name,
                hint="二者互斥：要么必填，要么给缺省值",
            )
        if self.type != "array" and self.item_type is not None:
            raise PluginManifestError(
                "item_type 只能用于 array 类型", field=self.name, type=self.type
            )

    def coerce(self, value: Any) -> Any:
        """把 ``value`` 转成声明的类型，并跑完所有约束。

        转换失败时抛 :class:`~alterego.kernel.errors.PluginManifestError`。
        """
        try:
            coerced = self._coerce_raw(value)
        except (TypeError, ValueError) as exc:
            raise self._reject(value, reason=f"无法解析为 {self.type}（{exc}）") from exc
        self._check_constraints(coerced, original=value)
        return coerced

    def _coerce_raw(self, value: Any) -> Any:
        match self.type:
            case "string":
                return value if isinstance(value, str) else str(value)
            case "integer":
                return _to_int(value)
            case "number":
                return _to_float(value)
            case "boolean":
                return _to_bool(value)
            case "array":
                if isinstance(value, str):
                    # TOML 里写 ["a","b"] 是数组；写 "a,b" 是手滑。
                    raise TypeError('期望数组而不是字符串，请写成 ["a", "b"]')
                if not isinstance(value, (list, tuple)):
                    raise TypeError("期望数组")
                items = list(value)
                if self.item_type is not None:
                    items = [ConfigField(self.name, self.item_type).coerce(v) for v in items]
                return items
            case "object":
                if not isinstance(value, Mapping):
                    raise TypeError("期望表（table）")
                return dict(value)
            case "duration":
                return parse_duration(value)
            case "path":
                if not isinstance(value, (str, Path)):
                    raise TypeError("期望路径字符串")
                return Path(value)
        raise TypeError(f"未知类型 {self.type}")  # pragma: no cover - __post_init__ 已挡

    def _check_constraints(self, value: Any, *, original: Any) -> None:
        if self.choices and value not in self.choices:
            raise self._reject(original, reason=f"必须是 {list(self.choices)} 之一")
        if self.min is not None and isinstance(value, (int, float)) and value < self.min:
            raise self._reject(original, reason=f"不能小于 {self.min}")
        if self.max is not None and isinstance(value, (int, float)) and value > self.max:
            raise self._reject(original, reason=f"不能大于 {self.max}")
        if isinstance(value, str):
            if self.min_length is not None and len(value) < self.min_length:
                raise self._reject(original, reason=f"长度不能小于 {self.min_length}")
            if self.max_length is not None and len(value) > self.max_length:
                raise self._reject(original, reason=f"长度不能大于 {self.max_length}")
            if self.pattern is not None and not re.search(self.pattern, value):
                raise self._reject(original, reason=f"必须匹配 {self.pattern!r}")
        if isinstance(value, list):
            if self.min_items is not None and len(value) < self.min_items:
                raise self._reject(original, reason=f"至少 {self.min_items} 项")
            if self.max_items is not None and len(value) > self.max_items:
                raise self._reject(original, reason=f"至多 {self.max_items} 项")
        if isinstance(value, Path) and self.must_exist and not value.exists():
            raise self._reject(original, reason="路径不存在")

    def _reject(self, value: Any, *, reason: str) -> PluginManifestError:
        shown = MASK if self.secret else repr(value)
        return PluginManifestError(
            "插件配置项非法",
            field=self.name,
            value=shown,
            reason=reason,
            hint=f"检查 config/alterego.toml 的 [plugins.<id>] 一节里的 {self.name}",
        )

    def redacted(self) -> str:
        """给 ``alterego plugins config`` 展示用的一行摘要。"""
        return f"{self.name}: {self.type}" + ("（敏感）" if self.secret else "")


def parse_duration(value: Any) -> timedelta:
    """把 ``"30s"`` / ``"5m"`` / ``"2h"`` / ``"1d"`` 解析成 :class:`~datetime.timedelta`。"""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        # 裸数字按秒解释——但这几乎总是笔误，所以在错误信息里说清楚。
        return timedelta(seconds=float(value))
    if not isinstance(value, str):
        raise TypeError('期望形如 "30s" 的时长字符串')
    matched = _DURATION_RE.match(value.strip())
    if matched is None:
        raise ValueError(f"无法解析时长 {value!r}；支持的单位是 ms/s/m/h/d")
    amount, unit = matched.groups()
    return timedelta(seconds=float(amount) * _DURATION_UNITS[unit])


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("布尔值不是整数")
    return int(value)


def _to_float(value: Any) -> float:
    if isinstance(value, bool):
        raise TypeError("布尔值不是数值")
    return float(value)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        raise TypeError(f"无法解析布尔值 {value!r}")
    if isinstance(value, (int, float)):
        return bool(value)
    raise TypeError("期望布尔值")


# ── 清单 ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """``plugin.toml`` 解析后的结果（已校验）。"""

    id: str
    version: str
    api_version: int
    kind: PluginKind
    entry: str
    name: str = ""
    description: str = ""
    authors: tuple[str, ...] = ()
    license: str = ""
    homepage: str = ""
    tags: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    enabled_by_default: bool = True
    priority: int = 0
    auto_reload: bool = True
    config: Mapping[str, ConfigField] = field(default_factory=dict)
    #: 插件目录（本地插件才有）。
    path: Path | None = None
    #: ``"local"`` 或 ``"entry_point"``。本地优先（便于覆盖调试）。
    source: str = "local"

    @property
    def display_name(self) -> str:
        return self.name or self.id

    @property
    def module(self) -> str:
        return self.entry.split(":", 1)[0]

    @property
    def class_name(self) -> str:
        return self.entry.split(":", 1)[1]

    def config_field(self, name: str) -> ConfigField | None:
        return self.config.get(name)

    def redacted_config(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """按清单里的 ``secret = true`` 打码，用于任何面向人的输出。"""
        out: dict[str, Any] = {}
        for key, value in values.items():
            spec = self.config.get(key)
            out[key] = MASK if spec is not None and spec.secret else value
        return out

    # ── 解析与校验 ──────────────────────────────────────────

    @classmethod
    def parse(
        cls,
        data: Mapping[str, Any],
        *,
        path: Path | None = None,
        source: str = "local",
    ) -> PluginManifest:
        """从 ``plugin.toml`` 的 ``[plugin]`` 表构造并校验。

        错误信息会指出**具体哪个字段**错了，因为「清单非法」这四个字
        对写插件的人毫无帮助。
        """
        plugin_id = data.get("id")
        if not isinstance(plugin_id, str) or not _ID_RE.match(plugin_id):
            raise PluginManifestError(
                "插件 id 非法",
                id=plugin_id,
                path=str(path) if path else None,
                hint="id 必须是 <kind>.<name> 形式：全小写字母、数字与下划线",
            )

        def where() -> dict[str, Any]:
            return {
                "plugin": plugin_id,
                "manifest": str(path / "plugin.toml") if path else None,
            }

        # 先查「有没有拼错的键」，再查值。
        #
        # 与 ``_parse_config_table`` 拒绝未知的**配置字段**是同一件事，理由也一样：
        # 静默忽略一个键，等于允许插件带着一份「你以为配了、其实没配」的配置跑起来。
        # ``enabled_by_default`` 拼成 ``enabledByDefault`` 是最典型的例子——
        # 一个只想写「这个插件默认别开」的作者，会得到一个默认开着的插件。
        # 这类「配错了反而更开放」的降级最难发现，所以宁可当场报错。
        unknown_keys = sorted(set(data) - _KNOWN_MANIFEST_KEYS)
        if unknown_keys:
            raise PluginManifestError(
                "清单里有无法识别的键",
                **where(),
                unknown=unknown_keys,
                supported=sorted(_KNOWN_MANIFEST_KEYS),
                hint=(
                    "键名拼错会被静默忽略，插件会带着一份你没真正配上的清单跑起来。"
                    "字段表见 docs/guide/plugin-development.md § 2。"
                ),
            )

        version = data.get("version")
        if not isinstance(version, str) or not _SEMVER_RE.match(version):
            raise PluginManifestError("插件 version 必须是 SemVer", **where(), version=version)

        api_version = _parse_api_version(data.get("api_version"), plugin_id)
        if not is_api_version_compatible(api_version):
            # 注意：``where()`` 里已经有 ``plugin`` 这个键了，这里不能再传一次，
            # 否则 Python 会抛 ``TypeError`` 而不是让插件被判为 Failed——
            # 一个坏插件不应该把一个可恢复的错误变成一次崩溃。
            raise PluginManifestError(
                "插件要求的 API 版本与内核不兼容",
                **where(),
                plugin_api_version=api_version,
                kernel_api_version=API_VERSION,
                hint=(
                    f"内核支持 api_version {API_VERSION - 1}~{API_VERSION}；"
                    "请升级 AlterEgo 或改用兼容的插件版本"
                ),
            )

        kind = data.get("kind")
        if kind not in _KINDS:
            raise PluginManifestError(
                "插件 kind 非法", **where(), kind=kind, supported=sorted(_KINDS)
            )

        entry = data.get("entry")
        if not isinstance(entry, str) or not _ENTRY_RE.match(entry):
            raise PluginManifestError(
                "插件 entry 非法",
                **where(),
                entry=entry,
                hint='格式为 "模块:类名"，例如 "plugin:MyChannel"',
            )

        return cls(
            id=plugin_id,
            version=version,
            api_version=api_version,
            kind=kind,
            entry=entry,
            name=_as_str(data.get("name")),
            description=_as_str(data.get("description")),
            authors=_as_str_tuple(data.get("authors")),
            license=_as_str(data.get("license")),
            homepage=_as_str(data.get("homepage")),
            tags=_as_str_tuple(data.get("tags")),
            requires=_as_str_tuple(data.get("requires")),
            provides=_as_str_tuple(data.get("provides")),
            optional=_as_str_tuple(data.get("optional")),
            enabled_by_default=_as_bool(data.get("enabled_by_default"), default=True),
            priority=_as_int(data.get("priority"), default=0),
            auto_reload=_as_bool(data.get("auto_reload"), default=True),
            config=_parse_config_table(data.get("config"), plugin_id),
            path=path,
            source=source,
        )


#: ``plugin.toml`` 的 ``[plugin]`` 表里**允许出现**的键。
#:
#: 从 :class:`PluginManifest` 自己的字段**推导**，不手抄：手抄的清单一定会漂，
#: 漂成两种后果——「写对了的键被判成非法」或者更糟的「拼错的键被静默忽略」。
#: 推导一次，加字段时自动跟着变，不可能不一致。
#:
#: 去掉 ``path`` 与 ``source``：它们由发现过程填（本地目录 / entry point），
#: 不是插件作者写的，写进 ``plugin.toml`` 只会被忽略——那正是这里要拦的。
#:
#: 位置在类定义**之后**，因为推导需要字段已经存在。
_KNOWN_MANIFEST_KEYS: frozenset[str] = frozenset(
    f.name for f in dataclasses.fields(PluginManifest) if f.name not in {"path", "source"}
)


def is_api_version_compatible(
    plugin_api_version: int, kernel_api_version: int = API_VERSION
) -> bool:
    """插件 API 版本是否落在内核的兼容窗口内。

    窗口是「内核版本及之前一个大版本」：内核 2 能跑 ``api_version`` 1 与 2，
    内核 1 跑不了 2。这样「插件要求更高的内核」会被明确拒绝，
    而不是装上去然后在某个字段上炸掉。
    """
    return 0 <= kernel_api_version - plugin_api_version <= 1


def _parse_api_version(value: Any, plugin_id: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise PluginManifestError(
            "插件 api_version 必须是整数", plugin=plugin_id, api_version=value
        )
    try:
        parsed = int(value)
    except ValueError as exc:
        raise PluginManifestError(
            "插件 api_version 必须是整数", plugin=plugin_id, api_version=value
        ) from exc
    if parsed < 1:
        raise PluginManifestError(
            "插件 api_version 必须 >= 1", plugin=plugin_id, api_version=parsed
        )
    return parsed


def _parse_config_table(raw: Any, plugin_id: str) -> dict[str, ConfigField]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise PluginManifestError(
            "plugin.toml 的 [config] 必须是表", plugin=plugin_id, got=type(raw).__name__
        )
    fields: dict[str, ConfigField] = {}
    for name, spec in raw.items():
        if not isinstance(spec, Mapping):
            raise PluginManifestError(
                "配置字段声明必须是表",
                plugin=plugin_id,
                field=name,
                hint='例如 webhook_url = { type = "string", required = true }',
            )
        unknown = set(spec) - _CONFIG_FIELD_KEYS
        if "schema" in unknown:
            raise PluginManifestError(
                "v1 尚不支持嵌套的 object.schema",
                plugin=plugin_id,
                field=name,
                hint='把嵌套结构拆成多个平铺字段，或用 type = "string" 存 JSON',
            )
        if unknown:
            raise PluginManifestError(
                "配置字段声明里有未知键",
                plugin=plugin_id,
                field=name,
                unknown=sorted(unknown),
                supported=sorted(_CONFIG_FIELD_KEYS),
            )
        fields[str(name)] = ConfigField(
            name=str(name),
            type=spec.get("type", "string"),
            required=_as_bool(spec.get("required"), default=False),
            default=spec.get("default"),
            description=_as_str(spec.get("description")),
            secret=_as_bool(spec.get("secret"), default=False),
            env=spec.get("env") if isinstance(spec.get("env"), str) else None,
            choices=tuple(spec.get("choices", ())),
            min=_as_optional_float(spec.get("min")),
            max=_as_optional_float(spec.get("max")),
            min_length=_as_optional_int(spec.get("min_length")),
            max_length=_as_optional_int(spec.get("max_length")),
            pattern=spec.get("pattern") if isinstance(spec.get("pattern"), str) else None,
            item_type=spec.get("item_type"),
            min_items=_as_optional_int(spec.get("min_items")),
            max_items=_as_optional_int(spec.get("max_items")),
            must_exist=_as_bool(spec.get("must_exist"), default=False),
        )
    return fields


#: ``ConfigField`` 允许在 ``plugin.toml`` 里出现的键。用来把拼错的键名
#: （``requierd``、``defualt``）当场拒掉，而不是让它静默失效。
_CONFIG_FIELD_KEYS: frozenset[str] = frozenset(f.name for f in dataclasses.fields(ConfigField))


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    raise PluginManifestError("期望字符串数组", got=type(value).__name__)


def _as_bool(value: Any, *, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_int(value: Any, *, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _as_optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# ── 配置解析 ────────────────────────────────────────────────


def resolve_config(
    manifest: PluginManifest,
    provided: Mapping[str, Any] | None,
    *,
    environ: Mapping[str, str] | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """合并默认值、用户配置与环境变量，得到插件**最终**的配置字典。

    优先级：**环境变量 > 用户配置 > 清单默认值**。

    为什么环境变量排在用户配置前面：这是「容器里改一个环境变量就能换掉
    写在镜像里的配置」这个用法的前提。清单里显式写了 ``env`` 的字段才走这条路径，
    不写就不看环境变量——不做任何猜测（P2）。

    Returns:
        已校验、已填默认值的配置字典。**密钥字段是真实值**——插件自己需要它，
        脱敏只发生在面向人的输出上（:meth:`PluginManifest.redacted_config`）。
    """
    env = environ if environ is not None else {}
    values: dict[str, Any] = {}

    for name, spec in manifest.config.items():
        if spec.default is not None:
            values[name] = spec.coerce(spec.default)
    if provided:
        for name, value in provided.items():
            field_spec = manifest.config.get(name)
            values[name] = field_spec.coerce(value) if field_spec is not None else value
    for name, spec in manifest.config.items():
        if spec.env is None:
            continue
        raw = env.get(spec.env)
        if raw is not None and raw != "":
            values[name] = spec.coerce(raw)

    missing = [
        name for name, spec in manifest.config.items() if spec.required and name not in values
    ]
    if missing:
        first = manifest.config[missing[0]]
        hint_env = f"，或设置环境变量 {first.env}" if first.env else ""
        raise PluginManifestError(
            "插件缺少必填配置",
            plugin=manifest.id,
            missing=missing,
            hint=(f"在 config/alterego.toml 的 [plugins.{manifest.id}] 中补齐{hint_env}"),
        )

    if logger is not None:
        extra = sorted(set(provided) - set(manifest.config)) if provided else []
        if extra:
            # 不失败：插件可能故意读清单外的键。但要说一声，否则写错了键名
            # 会表现为「配置没生效」这种最难查的症状。
            logger.warning("插件 %s 的配置里有清单未声明的键：%s", manifest.id, ", ".join(extra))
    return values

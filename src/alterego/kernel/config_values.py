"""配置值的类型构造。

从 ``config.py`` 里搬出来的：这里全是**纯函数**——给一个类型注解和一个原始值，
还一个正确类型的值回来。它们不碰文件、不碰环境变量，也就没有理由跟
「读哪个文件、合并哪几层」挤在同一个模块里。

``Config.load`` 把已合并的字典交给 :func:`build_section`，
从这里往下就只剩「类型对不对」这一件事。

:func:`type_hints` 也被 ``config.py`` 用来判断「一个段能不能往里递归」，
所以它是对外公开的——两处都要解析注解字符串，没必要各缓存一份。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import time
from pathlib import Path
from types import UnionType
from typing import Any, Literal, get_args, get_origin, get_type_hints

from alterego.kernel.errors import ConfigError


__all__ = ["build_section", "type_hints"]


def build_section(cls: type[Any], data: Mapping[str, Any]) -> Any:
    """按 dataclass 的字段类型递归构造，并做宽松类型转换。

    「宽松」指的是 ``"3"`` → ``3`` 这种 TOML/环境变量带来的字符串，
    而不是「错的东西也接受」——转换不了仍然报错。
    """
    hints = type_hints(cls)
    kwargs: dict[str, Any] = {}
    for info in fields(cls):
        if info.name not in data:
            continue
        kwargs[info.name] = _coerce(hints[info.name], data[info.name], info.name)
    return cls(**kwargs)


#: ``get_type_hints`` 很贵（要解析全部注解字符串），而配置类就那么几个。
#: 刻意不用 ``functools.cache``：它的 ``Hashable`` 约束对 ``type[...]`` 不友好，
#: 而这里一个普通 dict 就够——进程生命周期内不会失效。
_TYPE_HINTS: dict[type[Any], dict[str, Any]] = {}


def type_hints(cls: type[Any]) -> dict[str, Any]:
    """取解析后的字段类型（按类缓存）。

    **必须走 ``get_type_hints``**：本模块用了 ``from __future__ import annotations``，
    于是 ``fields(cls)[i].type`` 是字符串 ``"SimulationConfig"`` 而不是类对象，
    直接拿它做 ``is_dataclass()`` 判断会一律失败，嵌套配置段就会悄悄退化成 dict。
    这个坑是静默的：不报错，只是 ``cfg.simulation.mode`` 突然变成 ``dict``。
    """
    cached = _TYPE_HINTS.get(cls)
    if cached is None:
        cached = get_type_hints(cls)
        _TYPE_HINTS[cls] = cached
    return cached


def _coerce(annotation: Any, value: Any, key: str) -> Any:
    """把配置里的原始值变成目标类型。"""
    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is Literal:
        if value not in args:
            raise ConfigError("取值不在允许范围内", key=key, value=value, supported=list(args))
        return value

    # ``int | None`` 这类可选值：None 原样保留，否则按非 None 的那一侧转换
    if origin is UnionType:
        inner_types = [arg for arg in args if arg is not type(None)]
        if value is None or len(inner_types) != 1:
            return value
        return _coerce(inner_types[0], value, key)

    if origin in (tuple, frozenset, list):
        items = value if isinstance(value, (list, tuple)) else [value]
        inner = args[0] if args else str
        if origin is list:
            return [_coerce(inner, item, key) for item in items]
        return origin(_coerce(inner, item, key) for item in items)

    if isinstance(annotation, type) and is_dataclass(annotation):
        if not isinstance(value, Mapping):
            raise ConfigError("配置段必须是表（table）", key=key, value=value)
        return build_section(annotation, value)

    if origin is not None:
        # 其他泛型（如 Mapping[str, Any]）：原样保留，由使用方自己解释
        return value

    return _cast_scalar(annotation, value, key)


def _cast_scalar(annotation: Any, value: Any, key: str) -> Any:
    """标量转换。``bool`` 必须显式判断，因为 ``isinstance(True, int)`` 为真。"""
    if annotation is Path and isinstance(value, str):
        return Path(value)
    if annotation is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false", "1", "0", "yes", "no"}:
            return value.lower() in {"true", "1", "yes"}
        raise ConfigError("期望布尔值", key=key, value=value)
    if annotation is int and isinstance(value, (int, float, str)) and not isinstance(value, bool):
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError("期望整数", key=key, value=value) from exc
    if annotation is float and isinstance(value, (int, float, str)) and not isinstance(value, bool):
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError("期望数字", key=key, value=value) from exc
    if annotation is time and isinstance(value, str):
        try:
            hour, minute = value.split(":")[:2]
            return time(int(hour), int(minute))
        except ValueError as exc:
            raise ConfigError(
                "期望 HH:MM 形式的时间",
                key=key,
                value=value,
                hint='例如 quiet_hours = ["23:30", "08:00"]',
            ) from exc
    if annotation is str and not isinstance(value, str):
        raise ConfigError("期望字符串", key=key, value=value)
    return value

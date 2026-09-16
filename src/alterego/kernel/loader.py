"""插件的发现、清单解析、依赖编排与模块导入。

本模块只做「让插件可被 import、可被排序」这两件事，不负责生命周期——
那是 :mod:`alterego.kernel.manager` 的职责。分开的理由是它们出错的方式完全不同：
Loader 的问题是**声明层面**的（清单写错了、依赖成环了），
Manager 的问题是**运行层面**的（某个插件 on_start 挂了、运行时熔断了）。

依据: docs/design/02-plugin-api.md § 3.4、§ 8–9
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import logging
import re
import sys
import tomllib
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from alterego.kernel.errors import (
    PluginDependencyError,
    PluginLoadError,
    PluginManifestError,
)
from alterego.kernel.plugin import Plugin, PluginManifest


__all__ = [
    "DEFAULT_ENTRY_POINT_GROUP",
    "DEFAULT_SEARCH_PATHS",
    "EXIT_DEPENDENCY_ERROR",
    "DiscoveredPlugin",
    "DiscoveryResult",
    "Requirement",
    "Resolution",
    "VersionConstraint",
    "discover",
    "import_plugin_class",
    "module_name_for",
    "parse_requirement",
    "parse_semver",
    "resolve_load_order",
]


#: 本地插件的模块名根。用独立命名空间是为了避免与 pip 包名、项目自身模块撞名。
PLUGIN_NAMESPACE: Final[str] = "alterego_plugins"

#: pip 分发的插件通过这个 entry point 组被发现。
DEFAULT_ENTRY_POINT_GROUP: Final[str] = "alterego.plugins"

#: 默认的本地插件搜索路径。
DEFAULT_SEARCH_PATHS: Final[tuple[str, ...]] = ("plugins", "~/.alterego/plugins")

#: 依赖成环时的进程退出码（与 ``docs/design/02-plugin-api.md`` § 8.2 一致）。
EXIT_DEPENDENCY_ERROR: Final[int] = 3

_MANIFEST_FILENAME: Final[str] = "plugin.toml"
_MANIFEST_TABLE: Final[str] = "plugin"
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
# 比较符与版本号是一体的：不能把 ``"storage.x 0.1.0"``（漏了 ``>=``）当成
# 「无约束」默默放过——那会让一条写错的依赖看起来生效了。
_REQUIREMENT_RE = re.compile(
    r"^\s*([a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)\s*(?:(>=|<=|==|~=)\s*(\S*))?\s*$"
)


# ── 版本 ────────────────────────────────────────────────────


def parse_semver(text: str) -> tuple[int, int, int]:
    """把 ``"0.1.2"`` 解析成 ``(0, 1, 2)``。

    预发布后缀（``-beta.1``）会被接受但忽略：它只影响排序，不影响「能不能装」。
    """
    matched = _SEMVER_RE.match(text.strip())
    if matched is None:
        raise PluginManifestError("版本号不是 SemVer", version=text)
    major, minor, patch = matched.groups()
    return int(major), int(minor), int(patch)


@dataclass(frozen=True, slots=True)
class VersionConstraint:
    """``>= 0.1.0`` 这样的版本约束。"""

    operator: str
    version: tuple[int, int, int]
    raw: str = ""

    def satisfies(self, actual: tuple[int, int, int]) -> bool:
        match self.operator:
            case ">=":
                return actual >= self.version
            case "<=":
                return actual <= self.version
            case "==":
                return actual == self.version
            case "~=":
                # PEP 440 的兼容版本：``~= 0.1.0`` 等价于 ``>= 0.1.0, < 0.2.0``。
                # 即「允许最后一个数字变，不允许倒数第二个变」。
                return self.version <= actual < _bump_compatible(self.version)
            case _:  # pragma: no cover - parse_requirement 已挡
                raise PluginManifestError("未知的版本比较符", operator=self.operator)

    def __str__(self) -> str:
        return self.raw or f"{self.operator} {'.'.join(map(str, self.version))}"


def _bump_compatible(version: tuple[int, int, int]) -> tuple[int, int, int]:
    major, minor, _patch = version
    return major, minor + 1, 0


@dataclass(frozen=True, slots=True)
class Requirement:
    """依赖声明的一条：``"storage.my_backend >= 0.1.0"``。"""

    plugin_id: str
    constraint: VersionConstraint | None = None
    raw: str = ""

    def check(self, actual_version: str, *, dependent: str) -> None:
        """版本不满足时抛 :class:`~alterego.kernel.errors.PluginDependencyError`。"""
        if self.constraint is None:
            return
        actual = parse_semver(actual_version)
        if not self.constraint.satisfies(actual):
            raise PluginDependencyError(
                "插件依赖的版本不满足",
                plugin=dependent,
                requires=self.plugin_id,
                constraint=str(self.constraint),
                found=actual_version,
                hint=f"升级 {self.plugin_id}，或放宽 {dependent} 的 requires",
            )

    def __str__(self) -> str:
        return self.raw or self.plugin_id


def parse_requirement(text: str) -> Requirement:
    """解析一条依赖声明。支持 ``>=``、``<=``、``==``、``~=`` 与无约束。"""
    matched = _REQUIREMENT_RE.match(text)
    if matched is None:
        raise PluginManifestError(
            "依赖声明格式非法",
            requirement=text,
            hint='形如 "storage.my_backend >= 0.1.0"，或只写 "llm.my_provider"',
        )
    plugin_id, operator, version = matched.groups()
    if operator is None:
        return Requirement(plugin_id=plugin_id, constraint=None, raw=text)
    if not version:
        raise PluginManifestError("依赖声明缺少版本号", requirement=text)
    return Requirement(
        plugin_id=plugin_id,
        constraint=VersionConstraint(
            operator=operator, version=parse_semver(version), raw=f"{operator} {version}"
        ),
        raw=text,
    )


# ── 发现结果 ────────────────────────────────────────────────


def module_name_for(plugin_id: str) -> str:
    """插件模块的完整名字。

    ``channel.my_webhook`` → ``alterego_plugins.channel.my_webhook``。

    id 里的点在这里恰好成了包的层级——这不是巧合：``<kind>.<name>`` 本身
    就是一棵两层的命名空间，直接映射过去即可，不需要再发明一套编码。
    """
    return f"{PLUGIN_NAMESPACE}.{plugin_id}"


@dataclass(frozen=True, slots=True)
class DiscoveredPlugin:
    """一个被找到并解析成功的插件。"""

    manifest: PluginManifest
    #: 本地插件：插件目录。pip 插件：其模块所在目录。
    path: Path | None = None
    #: pip 插件的 entry point 值（本地插件为 ``None``）。
    entry_point: str | None = None

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def module_name(self) -> str:
        return module_name_for(self.manifest.id)


@dataclass(slots=True)
class DiscoveryResult:
    """发现阶段的全部产物。

    ``failed`` 不抛异常而是收集起来——一个坏掉的插件目录不应该让
    ``alterego plugins list`` 都跑不了。用户需要能看到「哪个插件坏了、为什么」。
    """

    plugins: dict[str, DiscoveredPlugin] = field(default_factory=dict)
    failed: list[tuple[Path, PluginManifestError]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.plugins)

    def __contains__(self, plugin_id: object) -> bool:
        return plugin_id in self.plugins

    def get(self, plugin_id: str) -> DiscoveredPlugin | None:
        return self.plugins.get(plugin_id)

    def ids(self) -> list[str]:
        return sorted(self.plugins)

    def merge(self, other: DiscoveryResult) -> None:
        self.failed.extend(other.failed)
        self.plugins.update(other.plugins)


# ── 发现 ────────────────────────────────────────────────────


def discover(
    *,
    search_paths: Sequence[str] = DEFAULT_SEARCH_PATHS,
    entry_point_group: str = DEFAULT_ENTRY_POINT_GROUP,
    logger: logging.Logger | None = None,
) -> DiscoveryResult:
    """扫出所有可用的插件。

    本地 drop-in 优先于 pip 分发的同名插件（便于用户覆盖调试），并记录一条警告——
    静默覆盖会让「我明明卸载了它怎么还在」变成一个谜。
    """
    result = _discover_local(search_paths, logger=logger)
    local_ids = set(result.plugins)

    for plugin in _discover_entry_points(entry_point_group, logger=logger):
        if plugin.id in local_ids:
            if logger is not None:
                logger.warning(
                    "插件 %s 同时存在于本地目录与已安装的包里，使用本地版本（%s）",
                    plugin.id,
                    result.plugins[plugin.id].path,
                )
            continue
        result.plugins[plugin.id] = plugin

    return result


def _discover_local(
    search_paths: Sequence[str], *, logger: logging.Logger | None
) -> DiscoveryResult:
    result = DiscoveryResult()
    for raw in search_paths:
        root = Path(raw).expanduser()
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / _MANIFEST_FILENAME
            if not manifest_path.is_file():
                continue
            try:
                manifest = load_manifest(manifest_path, base_dir=child)
            except PluginManifestError as exc:
                result.failed.append((child, exc))
                if logger is not None:
                    logger.error("插件清单有误：%s", exc)
                continue
            existing = result.plugins.get(manifest.id)
            if existing is not None:
                # 前面的 search_path 已经提供过它了。
                if logger is not None:
                    logger.warning(
                        "插件 %s 在 %s 中重复定义，忽略 %s",
                        manifest.id,
                        existing.path,
                        child,
                    )
                continue
            result.plugins[manifest.id] = DiscoveredPlugin(manifest=manifest, path=child)
    return result


def _discover_entry_points(group: str, *, logger: logging.Logger | None) -> list[DiscoveredPlugin]:
    try:
        eps = importlib.metadata.entry_points(group=group)
    except Exception as exc:  # pragma: no cover - 极端的元数据损坏
        if logger is not None:
            logger.warning("读取 entry point 组 %s 失败：%s", group, exc)
        return []

    found: list[DiscoveredPlugin] = []
    for ep in eps:
        try:
            module = importlib.import_module(ep.value.split(":", 1)[0])
            manifest = _manifest_from_entry_point(ep, module)
        except PluginManifestError as exc:
            if logger is not None:
                logger.error("entry point %s 的清单有误：%s", ep.name, exc)
            continue
        except Exception as exc:  # pragma: no cover - 第三方包自身的问题
            if logger is not None:
                logger.error("导入 entry point %s 失败：%s", ep.name, exc)
            continue
        found.append(
            DiscoveredPlugin(
                manifest=manifest,
                path=Path(module.__file__).parent if module.__file__ else None,
                entry_point=ep.value,
            )
        )
    return found


def _manifest_from_entry_point(ep: Any, module: ModuleType) -> PluginManifest:
    """pip 插件的清单来源。

    优先读模块旁边的 ``plugin.toml``（与本地插件同一套写法）；
    没有时退回模块级的 ``MANIFEST`` 字典。两者都没有就只能报错——
    猜一个清单出来会让「只声明、不猜测」（P2）当场失效。
    """
    module_file = getattr(module, "__file__", None)
    if module_file:
        candidate = Path(module_file).parent / _MANIFEST_FILENAME
        if candidate.is_file():
            return load_manifest(candidate, base_dir=candidate.parent)

    raw = getattr(module, "MANIFEST", None)
    if isinstance(raw, Mapping):
        return PluginManifest.parse(raw, path=None, source="entry_point")

    raise PluginManifestError(
        "找不到插件的 plugin.toml",
        entry_point=ep.value,
        hint=f"在 {Path(module_file).parent if module_file else '模块目录'} 下放一个 plugin.toml",
    )


def load_manifest(path: Path, *, base_dir: Path | None = None) -> PluginManifest:
    """读 ``plugin.toml`` 并解析其中的 ``[plugin]`` 表。"""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise PluginManifestError("找不到插件清单", manifest=str(path)) from exc
    except tomllib.TOMLDecodeError as exc:
        raise PluginManifestError(
            "插件清单不是合法的 TOML", manifest=str(path), reason=str(exc)
        ) from exc

    table = data.get(_MANIFEST_TABLE)
    if not isinstance(table, Mapping):
        raise PluginManifestError(
            f"插件清单缺少 [{_MANIFEST_TABLE}] 表",
            manifest=str(path),
            hint=f"[{_MANIFEST_TABLE}] 里要写 id / version / api_version / kind / entry",
        )
    # 顶层除 [plugin] 之外的表一律拒掉。看起来很像宽松处理更友好，其实相反：
    # 常见写法 `[config]`（少了 plugin. 前缀）会被**整段静默忽略**，
    # 于是插件带着「一份作者以为配了、实际一个字都没生效」的清单跑起来，
    # 而症状要等到功能不工作时才显形。这与 ``PluginManifest.parse``
    # 拒掉拼错的顶层键是同一个理由（见 docs/design/13-interface-consistency.md）。
    stray = sorted(set(data) - {_MANIFEST_TABLE})
    if stray:
        raise PluginManifestError(
            "插件清单里有 [plugin] 之外的顶层键",
            manifest=str(path),
            unknown=stray,
            hint=(
                f"配置字段要写在 [{_MANIFEST_TABLE}.config.<字段名>] 里，"
                f"例如 [{_MANIFEST_TABLE}.config.token]；"
                f"顶层只允许 [{_MANIFEST_TABLE}]"
            ),
        )
    return PluginManifest.parse(table, path=base_dir or path.parent, source="local")


# ── 导入 ────────────────────────────────────────────────────


def import_plugin_class(plugin: DiscoveredPlugin) -> type[Plugin]:
    """把插件目录里的 ``entry`` 指向的类取出来。

    本地插件以 ``alterego_plugins.<id>`` 为包名导入，包的 ``__path__``
    指向插件目录本身，因此插件内部的 ``import helpers`` 走的是
    ``alterego_plugins.<id>.helpers``——既不会污染全局命名空间，
    也不会和 pip 包撞名（见 § 9.1）。
    """
    manifest = plugin.manifest
    package_name = plugin.module_name
    _evict(package_name)

    if plugin.entry_point is not None:
        module = importlib.import_module(manifest.module)
        return _resolve_class(module, manifest)

    if plugin.path is None:  # pragma: no cover - 本地插件一定有 path
        raise PluginManifestError("本地插件缺少目录", plugin=manifest.id)

    importlib.invalidate_caches()
    _install_package(package_name, plugin.path)

    # ``entry`` 里的模块路径是**相对于插件根目录**的（§ 3.1），
    # 所以这里不做任何「猜一个正确路径」的尝试：猜错会表现为
    # 「插件莫名其妙没加载」，比一条明确的报错难查得多。
    module_path = manifest.module
    try:
        module = importlib.import_module(f"{package_name}.{module_path}")
    except ImportError as exc:
        if _is_the_plugins_own_dependency(exc, package_name):
            raise PluginLoadError(
                "插件导入了不存在的模块",
                plugin=manifest.id,
                module=getattr(exc, "name", None),
                reason=str(exc),
                hint=f"安装该插件声明的依赖，或检查 {plugin.path} 下的 import 语句",
            ) from exc
        raise PluginManifestError(
            "插件的 entry 指向的模块无法导入",
            plugin=manifest.id,
            module=f"{package_name}.{module_path}",
            reason=str(exc),
            hint=_entry_hint(plugin, module_path),
        ) from exc
    except SyntaxError as exc:
        # 语法错误是插件作者手滑，不是清单写错——分开报，否则用户会去翻 plugin.toml。
        raise PluginLoadError(
            "插件模块有语法错误",
            plugin=manifest.id,
            file=str(exc.filename),
            line=exc.lineno,
            reason=exc.msg,
            hint=f"修好 {exc.filename} 后保存即可，内核会自动重载",
        ) from exc
    return _resolve_class(module, manifest)


def _is_the_plugins_own_dependency(exc: ImportError, package_name: str) -> bool:
    """判断这个 ImportError 是「插件依赖的第三方模块缺失」而不是「entry 写错了」。

    ``ModuleNotFoundError.name`` 是那个找不到的模块名：它落在插件自己的命名空间里
    说明是 ``entry`` 指错了地方，落在别处说明插件少装了一个依赖。
    """
    missing = getattr(exc, "name", None)
    if not isinstance(missing, str) or not missing:
        return False
    return not (missing == package_name or missing.startswith(f"{package_name}."))


def _entry_hint(plugin: DiscoveredPlugin, module_path: str) -> str:
    relative = f"{module_path.replace('.', '/')}.py"
    hint = f"检查 {plugin.path / relative if plugin.path else relative} 是否存在"
    kind = plugin.manifest.kind
    if module_path == plugin.manifest.id or module_path.startswith(f"{kind}."):
        hint = (
            "entry 里的模块路径相对于插件根目录，不是插件 id；"
            f'例如 entry = "plugin:{plugin.manifest.class_name}" 指插件目录下的 plugin.py'
        )
    return hint


def _resolve_class(module: ModuleType, manifest: PluginManifest) -> type[Plugin]:
    candidate = getattr(module, manifest.class_name, None)
    if candidate is None:
        raise PluginManifestError(
            "entry 指向的类不存在",
            plugin=manifest.id,
            module=module.__name__,
            class_name=manifest.class_name,
        )
    if not isinstance(candidate, type) or not issubclass(candidate, Plugin):
        raise PluginManifestError(
            "entry 指向的类不是 Plugin 的子类",
            plugin=manifest.id,
            class_name=manifest.class_name,
            hint="让这个类继承 alterego.kernel.plugin.Plugin",
        )
    return candidate


def _install_package(name: str, directory: Path) -> None:
    """把 ``directory`` 注册成名为 ``name`` 的包（不需要 ``__init__.py``）。"""
    for parent in _ancestors(name):
        if parent in sys.modules:
            continue
        parent_module = ModuleType(parent)
        parent_module.__path__ = []
        parent_module.__package__ = parent
        sys.modules[parent] = parent_module

    spec = importlib.util.spec_from_file_location(
        name, directory / "__init__.py", submodule_search_locations=[str(directory)]
    )
    if spec is None:  # pragma: no cover - 目录一定存在
        raise PluginManifestError("无法为插件目录建立模块规格", plugin=name)
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(directory)]
    sys.modules[name] = module


def _ancestors(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts) - 1)]


def _evict(prefix: str) -> None:
    """把该插件之前导入过的模块全部清掉，为热重载让路。"""
    for key in [k for k in sys.modules if k == prefix or k.startswith(f"{prefix}.")]:
        del sys.modules[key]
    importlib.invalidate_caches()


# ── 依赖编排 ────────────────────────────────────────────────


@dataclass(slots=True)
class Resolution:
    """依赖编排的结果。"""

    #: 按拓扑序排列的插件 id——先出现的先加载。
    order: list[str] = field(default_factory=list)
    #: 因为别人的 ``requires`` 而被自动启用的插件。
    auto_enabled: list[str] = field(default_factory=list)
    #: 被跳过的可选依赖：``{插件 id: [缺失的 optional id]}``。
    skipped_optional: dict[str, list[str]] = field(default_factory=dict)

    def __contains__(self, plugin_id: object) -> bool:
        return plugin_id in self.order

    def __len__(self) -> int:
        return len(self.order)


def resolve_load_order(
    plugins: Mapping[str, DiscoveredPlugin] | DiscoveryResult,
    enabled: Iterable[str],
    *,
    logger: logging.Logger | None = None,
) -> Resolution:
    """算出加载顺序。

    步骤与设计文档 § 8.2 的流程图一致：校验 enabled → 建图 → 检环 → 拓扑排序
    → 自动启用 ``requires`` 目标。

    Raises:
        PluginDependencyError: ``enabled`` 里有找不到的插件、``requires``
            的目标不存在或版本不满足、或依赖成环。
    """
    catalog = plugins.plugins if isinstance(plugins, DiscoveryResult) else plugins
    resolution = Resolution()

    missing = sorted(set(enabled) - set(catalog))
    if missing:
        raise PluginDependencyError(
            "配置里启用的插件不存在",
            missing=missing,
            available=sorted(catalog),
            hint="运行 `alterego plugins doctor` 查看每个插件为什么没被发现",
        )

    required_set = set(enabled)
    pending = deque(sorted(required_set))
    while pending:
        current = pending.popleft()
        for requirement in _requirements_of(catalog[current]):
            target = requirement.plugin_id
            if target in required_set:
                continue
            found = catalog.get(target)
            if found is None:
                raise PluginDependencyError(
                    "插件声明的依赖没有被发现",
                    plugin=current,
                    requires=target,
                    hint=f"确认 {target} 已放在 plugins/ 下并启用它",
                )
            required_set.add(target)
            resolution.auto_enabled.append(target)
            pending.append(target)
            if logger is not None:
                logger.warning("插件 %s 依赖 %s，已自动启用", current, target)

    for plugin_id in sorted(required_set):
        for requirement in _requirements_of(catalog[plugin_id]):
            dependency = catalog.get(requirement.plugin_id)
            if dependency is not None:
                requirement.check(dependency.manifest.version, dependent=plugin_id)

    for plugin_id in sorted(required_set):
        plugin = catalog[plugin_id]
        skipped = [dep for dep in plugin.manifest.optional if dep not in catalog]
        if skipped:
            resolution.skipped_optional[plugin_id] = skipped
            if logger is not None:
                logger.info(
                    "插件 %s 的可选依赖 %s 不存在，功能可能降级",
                    plugin_id,
                    ", ".join(skipped),
                )

    resolution.order = _topological_order(catalog, required_set)
    return resolution


def _requirements_of(plugin: DiscoveredPlugin) -> list[Requirement]:
    return [parse_requirement(text) for text in plugin.manifest.requires]


def _topological_order(catalog: Mapping[str, DiscoveredPlugin], subset: set[str]) -> list[str]:
    """确定性拓扑排序（同层按 id 排序），并在成环时报出环上的插件。"""
    order: list[str] = []
    visiting: list[str] = []
    done: set[str] = set()

    def visit(plugin_id: str) -> None:
        if plugin_id in done:
            return
        if plugin_id in visiting:
            cycle = [*visiting[visiting.index(plugin_id) :], plugin_id]
            raise PluginDependencyError(
                "插件依赖成环",
                cycle=cycle,
                hint=f"断开环上任意一条 requires，例如 {' -> '.join(cycle)}",
            )
        visiting.append(plugin_id)
        for requirement in _requirements_of(catalog[plugin_id]):
            if requirement.plugin_id in subset:
                visit(requirement.plugin_id)
        visiting.pop()
        done.add(plugin_id)
        order.append(plugin_id)

    for plugin_id in sorted(subset):
        visit(plugin_id)
    return order

"""插件的生命周期编排：加载、启动、隔离、熔断、热重载、关闭。

这里是「插件出错不影响内核」这句话的落地处。核心是四件事：

1. **顺序** —— 按拓扑序加载与启动，逆序关闭。
2. **隔离** —— 每个阶段单独 try，失败只影响该插件；``on_load`` 失败还要回滚它
   已经注册进注册表和事件总线的东西，否则会留下指向半死对象的引用。
3. **熔断** —— 同一个插件连续失败到阈值就停用它，直到用户显式 ``reset``。
   一个每次 tick 都抛异常的插件如果只是「记日志然后继续」，日志会被淹掉。
4. **热重载** —— 改磁盘上的插件代码即生效，失败不恢复旧实例（状态可能已不一致）。

依据: docs/design/02-plugin-api.md § 4、§ 10、§ 11
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alterego.interfaces.common import HealthStatus
from alterego.kernel.bus import Event, EventBus
from alterego.kernel.clock import Clock
from alterego.kernel.config import Config
from alterego.kernel.errors import PluginError
from alterego.kernel.loader import (
    DEFAULT_ENTRY_POINT_GROUP,
    DiscoveredPlugin,
    DiscoveryResult,
    Resolution,
    discover,
    import_plugin_class,
    resolve_load_order,
)
from alterego.kernel.plugin import (
    Plugin,
    PluginContext,
    PluginManifest,
    PluginPaths,
    PluginState,
    PluginStatus,
    resolve_config,
)
from alterego.kernel.registry import ServiceRegistry
from alterego.kernel.scheduler import Scheduler


__all__ = ["LoadReport", "PluginManager"]


#: 热重载监视的文件后缀。
_WATCHED_SUFFIXES: frozenset[str] = frozenset({".py", ".toml"})


@dataclass(slots=True)
class LoadReport:
    """一次加载流程的结果，供 CLI 与日志展示。"""

    loaded: list[str] = field(default_factory=list)
    #: ``{插件 id: 失败原因}``
    failed: dict[str, str] = field(default_factory=dict)
    #: ``{插件 id: [缺失的可选依赖]}``
    skipped_optional: dict[str, list[str]] = field(default_factory=dict)
    auto_enabled: list[str] = field(default_factory=list)
    circuit_open: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


@dataclass(slots=True)
class _Record:
    plugin: Plugin
    context: PluginContext
    status: PluginStatus
    failures: int = 0


class PluginManager:
    """插件系统的门面。

    典型用法（见 ``docs/design/01-architecture.md`` § 6.1 的启动时序）::

        pm = PluginManager(
            config=config,
            bus=bus,
            registry=registry,
            clock=clock,
            scheduler=scheduler,
            logger=logger,
        )
        report = pm.load_all()
        ...
        pm.shutdown()  # 逆拓扑序，幂等
    """

    def __init__(
        self,
        *,
        config: Config,
        bus: EventBus,
        registry: ServiceRegistry,
        clock: Clock,
        scheduler: Scheduler,
        logger: logging.Logger,
        rng: random.Random | None = None,
        environ: Mapping[str, str] | None = None,
        state_loader: Callable[[str], Mapping[str, Any]] | None = None,
        state_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
        entry_point_group: str = DEFAULT_ENTRY_POINT_GROUP,
    ) -> None:
        """装上管理器。

        ``state_loader`` / ``state_sink`` 是插件状态落盘的两个方向：装进来时读一次，
        tick 结束时批量写回。**它们必须由组装根提供**（内核无知，P1——``kernel/``
        不认识 ``plugin_state`` 这张表），所以内核只留接口。

        ⚠️ 现状：**还没有组装根传这两个参数**，因此插件状态今天不跨重启
        （见 :class:`~alterego.kernel.context.PluginState` 与
        ``docs/design/13-interface-consistency.md``）。
        """
        self._config = config
        self._bus = bus
        self._registry = registry
        self._clock = clock
        self._scheduler = scheduler
        self._logger = logger
        self._rng = rng if rng is not None else random.Random()
        self._environ = environ
        self._state_loader = state_loader
        self._state_sink = state_sink
        self._entry_point_group = entry_point_group

        self._threshold = config.plugins.circuit_breaker_threshold
        self._isolate = config.plugins.isolate_failures

        self._records: dict[str, _Record] = {}
        self._status: dict[str, PluginStatus] = {}
        self._discovery = DiscoveryResult()
        self._circuit_open: set[str] = set()
        self._order: list[str] = []
        self._mtimes: dict[str, dict[Path, float]] = {}

        # 热重载期间暂停 tick：否则会出现「一半旧插件、一半新插件」的中间态。
        self._reload_lock = threading.Lock()
        self._watching = False

    # ── 查询 ────────────────────────────────────────────────

    @property
    def discovery(self) -> DiscoveryResult:
        return self._discovery

    @property
    def order(self) -> tuple[str, ...]:
        """当前加载顺序（拓扑序）。"""
        return tuple(self._order)

    @property
    def plugins(self) -> Mapping[str, Plugin]:
        return {pid: record.plugin for pid, record in self._records.items()}

    @property
    def reload_lock(self) -> threading.Lock:
        """tick 调度器在开始一轮之前应该先拿到这把锁。"""
        return self._reload_lock

    def status_of(self, plugin_id: str) -> PluginStatus | None:
        return self._status.get(plugin_id)

    def context_of(self, plugin_id: str) -> PluginContext | None:
        record = self._records.get(plugin_id)
        return record.context if record is not None else None

    def plugin_of(self, plugin_id: str) -> Plugin | None:
        record = self._records.get(plugin_id)
        return record.plugin if record is not None else None

    def failures_of(self, plugin_id: str) -> int:
        record = self._records.get(plugin_id)
        return record.failures if record is not None else 0

    def is_circuit_open(self, plugin_id: str) -> bool:
        return plugin_id in self._circuit_open

    def circuit_open(self) -> tuple[str, ...]:
        return tuple(sorted(self._circuit_open))

    def health_all(self) -> dict[str, HealthStatus]:
        """收集所有插件的健康状态，供 ``alterego plugins doctor`` 展示。"""
        report: dict[str, HealthStatus] = {}
        for plugin_id in self._order:
            if plugin_id in self._circuit_open:
                report[plugin_id] = HealthStatus(
                    ok=False,
                    detail="已熔断",
                    hint=f"运行 alterego plugins reset {plugin_id}",
                )
                continue
            plugin = self.plugin_of(plugin_id)
            if plugin is None:
                continue
            try:
                report[plugin_id] = plugin.health()
            except Exception as exc:
                report[plugin_id] = HealthStatus(
                    ok=False, detail=f"health() 抛异常：{exc}", hint="检查插件实现"
                )
        return report

    def missing_required_capabilities(self, required: Mapping[type[Any], str]) -> list[str]:
        """检查必需能力是否齐备。

        设计文档 § 6.1 要求「无 LLM / 无存储」时明确报错并指出缺的是哪个插件——
        所以这里返回的是人能读的说明，而不是一个裸的 ``False``。
        """
        missing: list[str] = []
        for interface, label in required.items():
            if not self._registry.has(interface):
                missing.append(label)
        return missing

    # ── 发现与编排 ──────────────────────────────────────────

    def discover(self) -> DiscoveryResult:
        self._discovery = discover(
            search_paths=[str(p) for p in self._config.plugin_search_paths],
            entry_point_group=self._entry_point_group,
            logger=self._logger,
        )
        self._mark_discovered(self._discovery)
        return self._discovery

    def _mark_discovered(self, found: DiscoveryResult) -> None:
        """给每个发现到的插件打上 ``DISCOVERED``。

        清单已经过校验（非法的那批进了 ``failed``），所以这里同时可以打
        ``VALIDATED``——``load_manifest`` 只在解析成功时才产出 ``DiscoveredPlugin``。
        两个状态因此**都**在发现阶段落地，``VALIDATED`` 不是多余的。

        为什么值得记这两个状态：``alterego plugins list`` 报的是「发现有谁」，
        ``info`` 报的是「这一个现在处于哪一步」。第二件事以前只能回答
        「配置里列了但库里没有」——因为没被启用的插件在 ``_status`` 里是**空的**，
        和「根本没有这个插件」长得一模一样。
        """
        for plugin_id in found.plugins:
            self._status.setdefault(plugin_id, PluginStatus.DISCOVERED)

    def enabled_ids(self) -> list[str]:
        """要加载哪些插件。

        显式配了 ``[plugins] enabled`` 就按它来（白名单）；没配则加载所有
        ``enabled_by_default = true`` 的插件——清单里的这个字段正是为此存在。
        """
        explicit = self._config.plugins.enabled
        if explicit:
            return sorted(explicit)
        return sorted(
            pid
            for pid, found in self._discovery.plugins.items()
            if found.manifest.enabled_by_default
        )

    # ── 加载 ────────────────────────────────────────────────

    def load_all(self) -> LoadReport:
        """发现 → 校验 → 拓扑排序 → 加载 → 启动。

        Raises:
            PluginDependencyError: ``enabled`` 里有不存在的插件、依赖缺失或成环。
                CLI 应把成环翻译成退出码 3（见 :data:`alterego.kernel.loader.EXIT_DEPENDENCY_ERROR`）。
        """
        report = LoadReport()
        self.discover()
        for path, error in self._discovery.failed:
            report.failed[str(path)] = str(error)
            self._status[str(path)] = PluginStatus.FAILED

        resolution: Resolution = resolve_load_order(
            self._discovery, self.enabled_ids(), logger=self._logger
        )
        report.auto_enabled = sorted(resolution.auto_enabled)
        report.skipped_optional = {
            pid: list(deps) for pid, deps in sorted(resolution.skipped_optional.items())
        }

        for plugin_id in resolution.order:
            found = self._discovery.plugins[plugin_id]
            if found.manifest.optional:
                self._logger.debug("%s 的可选依赖：%s", plugin_id, found.manifest.optional)

        self._order = list(resolution.order)
        for plugin_id in self._order:
            self._status[plugin_id] = PluginStatus.VALIDATED
        for plugin_id in self._order:
            if not self._load_one(plugin_id):
                report.failed[plugin_id] = self._describe_status(plugin_id)
        for plugin_id in self._order:
            if self._status.get(plugin_id) is PluginStatus.FAILED:
                continue
            self._start_one(plugin_id)
        report.loaded = [
            pid for pid in self._order if self._status.get(pid) is PluginStatus.STARTED
        ]
        report.circuit_open = list(self.circuit_open())
        return report

    def _load_one(self, plugin_id: str) -> bool:
        found = self._discovery.plugins.get(plugin_id)
        if found is None:  # pragma: no cover - 调用方保证存在
            return False
        manifest = found.manifest
        self._status[plugin_id] = PluginStatus.LOADING
        # 无论加载成败都开始监视文件：用户修好一个坏插件后应该自动生效，
        # 而不是必须先重启一次才能享受热重载。
        self._watch_mtimes(found)
        try:
            plugin = self._instantiate(found)
            context = self._make_context(manifest)
            plugin.on_load(context)
        except Exception as exc:
            self._fail(plugin_id, "on_load", exc)
            # 回滚：插件可能已经注册了实现、订阅了事件，留着就是悬空引用。
            self._registry.unregister_owner(plugin_id)
            self._bus.unsubscribe_owner(plugin_id)
            self._status[plugin_id] = PluginStatus.FAILED
            return False

        self._records[plugin_id] = _Record(
            plugin=plugin, context=context, status=PluginStatus.LOADED
        )
        self._status[plugin_id] = PluginStatus.LOADED
        self._publish("plugin.loaded", {"plugin_id": plugin_id, "version": manifest.version})
        return True

    def _instantiate(self, found: DiscoveredPlugin) -> Plugin:
        try:
            plugin_class = import_plugin_class(found)
            plugin = plugin_class()
        except PluginError:
            raise
        except Exception as exc:
            raise PluginError(
                "插件导入或实例化失败",
                plugin=found.id,
                reason=f"{type(exc).__name__}: {exc}",
                hint="检查 plugin.toml 的 entry，以及插件模块的 import 是否可用",
            ) from exc
        plugin.manifest = found.manifest
        return plugin

    def _make_context(self, manifest: PluginManifest) -> PluginContext:
        raw_config = self._config.plugins.config_for(manifest.id)
        config = resolve_config(manifest, raw_config, environ=self._environ, logger=self._logger)
        # ``data_dir`` 是 ``<项目根>/data``，所以上一级就是项目根。
        project_root = self._config.data_dir.parent
        plugin_data = self._config.data_dir / "plugins" / manifest.id
        paths = PluginPaths(
            data_dir=plugin_data,
            cache_dir=plugin_data / "cache",
            config_dir=project_root / "config",
            plugin_dir=manifest.path or project_root,
            alterego_dir=project_root,
        )
        paths.ensure_dirs()
        initial: Mapping[str, Any] = {}
        if self._state_loader is not None:
            initial = self._state_loader(manifest.id) or {}

        return PluginContext(
            plugin_id=manifest.id,
            manifest=manifest,
            config=config,
            logger=self._plugin_logger(manifest.id),
            bus=self._bus,
            registry=self._registry,
            clock=self._clock,
            scheduler=self._scheduler,
            state=PluginState(manifest.id, initial=initial, sink=self._state_sink),
            paths=paths,
            rng=random.Random(self._rng.random()),
        )

    def _plugin_logger(self, plugin_id: str) -> logging.Logger:
        """带插件 id 的 logger。

        名字里带 id 意味着日志行天然可过滤；``alterego.logs --plugin X``
        不需要任何额外字段就能工作。
        """
        return (
            self._logger.getChild(plugin_id) if self._logger.name else logging.getLogger(plugin_id)
        )

    def _start_one(self, plugin_id: str) -> bool:
        record = self._records.get(plugin_id)
        if record is None:
            return False
        try:
            record.plugin.on_start()
        except Exception as exc:
            self._fail(plugin_id, "on_start", exc)
            # 启动失败也要尽力收拾干净，否则后台任务会留在那儿。
            self._safe_call(plugin_id, "on_stop", record.plugin.on_stop)
            self._status[plugin_id] = PluginStatus.FAILED
            return False
        record.status = PluginStatus.STARTED
        self._status[plugin_id] = PluginStatus.STARTED
        self._arm_event_hook(record)
        return True

    def _arm_event_hook(self, record: _Record) -> None:
        """把 ``on_event`` 接到总线上。

        覆盖 ``on_event`` 就能收到**所有**事件。如果你在 ``on_load`` 里又
        ``ctx.bus.subscribe`` 了同一个主题，就会收到两次——二选一。
        """
        self._bus.subscribe("*", record.plugin.on_event, owner=record.plugin.manifest.id)

    # ── 关闭 ────────────────────────────────────────────────

    def shutdown(self) -> None:
        """逆拓扑序停止并卸载。幂等，可以重复调用。"""
        for plugin_id in reversed(self._order):
            self._teardown(plugin_id)
        self._records.clear()

    def _teardown(self, plugin_id: str) -> None:
        record = self._records.get(plugin_id)
        if record is None:
            self._status[plugin_id] = PluginStatus.UNLOADED
            return
        # ``STOPPED`` 落在 ``on_stop`` 之后、``on_unload`` 之前。
        # 它是 ``Loaded`` 与 ``Unloaded`` 之间唯一可观察的中间态：插件已经停了，
        # 但它注册的服务与订阅**还在**（下面两行才撤）。写成「两个状态」
        # 而不是三个，会让「服务已经摘了但插件以为自己在跑」这类问题无法定位。
        self._safe_call(plugin_id, "on_stop", record.plugin.on_stop)
        self._status[plugin_id] = PluginStatus.STOPPED
        self._safe_call(plugin_id, "on_unload", record.plugin.on_unload)
        self._registry.unregister_owner(plugin_id)
        self._bus.unsubscribe_owner(plugin_id)
        try:
            record.context.state.flush()
        except Exception as exc:  # pragma: no cover - sink 由上层提供
            self._logger.error("插件 %s 的状态落盘失败：%s", plugin_id, exc)
        record.status = PluginStatus.UNLOADED
        self._status[plugin_id] = PluginStatus.UNLOADED

    # ── 失败与熔断 ──────────────────────────────────────────

    def record_failure(self, plugin_id: str, exc: Exception, *, where: str) -> None:
        """外部调用点（渠道发送、能力执行、推演阶段）报告一次插件失败。

        连续失败到阈值即熔断：插件被停用，直到 :meth:`reset`。
        """
        record = self._records.get(plugin_id)
        if record is not None:
            record.failures += 1
            count = record.failures
        else:
            count = 1
        self._logger.warning("插件 %s 在 %s 中失败（第 %d 次）：%s", plugin_id, where, count, exc)
        if count < self._threshold or plugin_id in self._circuit_open:
            return
        self._circuit_open.add(plugin_id)
        self._logger.error(
            "插件 %s 连续失败 %d 次，已熔断。运行 `alterego plugins reset %s` 恢复。",
            plugin_id,
            count,
            plugin_id,
        )
        self._publish(
            "plugin.circuit_opened",
            {"plugin_id": plugin_id, "failures": count},
        )

    def reset(self, plugin_id: str) -> bool:
        """解除熔断并清零计数。返回之前是否处于熔断状态。"""
        was_open = plugin_id in self._circuit_open
        self._circuit_open.discard(plugin_id)
        record = self._records.get(plugin_id)
        if record is not None:
            record.failures = 0
        return was_open

    def _fail(self, plugin_id: str, where: str, exc: Exception) -> None:
        self._logger.error("插件 %s 的 %s 失败：%s", plugin_id, where, exc)
        self._publish(
            "plugin.failed",
            {"plugin_id": plugin_id, "where": where, "error": str(exc)},
        )
        if not self._isolate:
            raise exc

    def _safe_call(self, plugin_id: str, where: str, action: Callable[[], Any]) -> None:
        try:
            action()
        except Exception as exc:
            # 清理钩子里的异常不能再往外冒，否则一个坏插件会让整个关闭流程中断。
            self._logger.warning("插件 %s 的 %s 抛异常：%s", plugin_id, where, exc)

    # ── 运行时钩子 ──────────────────────────────────────────

    def tick_pre(self, ctx: Any) -> None:
        self._for_each_running("on_tick_pre", ctx)

    def tick_post(self, ctx: Any) -> None:
        self._for_each_running("on_tick_post", ctx)

    def _for_each_running(self, hook: str, *args: Any) -> None:
        for plugin_id in self._order:
            if plugin_id in self._circuit_open:
                continue
            record = self._records.get(plugin_id)
            if record is None or record.status is not PluginStatus.STARTED:
                continue
            try:
                getattr(record.plugin, hook)(*args)
            except Exception as exc:
                self.record_failure(plugin_id, exc, where=hook)

    def flush_state(self) -> None:
        """把所有插件挂起的状态写入存储。

        ⚠️ **今天没有人调用它，调了也不写。** 设计上由 Persist 阶段每 tick 调一次，
        但 (a) 组装根没有把 ``state_sink`` 传进来，(b) 全仓库 ``grep flush_state``
        只有这个定义。没有 ``sink`` 时 :meth:`PluginState.flush` 会丢弃挂起的写入，
        所以这是个**静默无操作**——留着兑现的接口，不是能用的功能。
        跟踪项：``docs/design/13-interface-consistency.md`` 审计表第 14 行。
        """
        for record in self._records.values():
            record.context.state.flush()

    # ── 热重载 ──────────────────────────────────────────────

    def reload(self, plugin_id: str) -> bool:
        """重新加载一个本地插件。

        失败**不恢复旧实例**：旧实例可能已经因为卸载而处于不一致状态，
        把它请回来只会让问题更难查（设计文档 § 10.3）。
        """
        found = self._discovery.plugins.get(plugin_id)
        if found is None:
            self._logger.error("没有叫 %s 的插件，无法重载", plugin_id)
            return False
        if found.entry_point is not None:
            self._logger.warning("插件 %s 来自已安装的包，不支持热重载；请重启 AlterEgo", plugin_id)
            return False

        with self._reload_lock:
            self._publish("plugin.reloading", {"plugin_id": plugin_id})
            self._teardown(plugin_id)
            self._records.pop(plugin_id, None)

            refreshed = discover(
                search_paths=[str(p) for p in self._config.plugin_search_paths],
                entry_point_group=self._entry_point_group,
                logger=self._logger,
            )
            self._discovery.plugins.update(refreshed.plugins)

            ok = self._load_one(plugin_id)
            if ok:
                ok = self._start_one(plugin_id)
            if not ok:
                self._status[plugin_id] = PluginStatus.FAILED
                self._logger.error(
                    "插件 %s 重载失败，已停用；修好代码后运行 `alterego plugins reload %s` 再试",
                    plugin_id,
                    plugin_id,
                )
            self._publish("plugin.reloaded", {"plugin_id": plugin_id, "ok": ok})
            return ok

    def changed_plugins(self) -> list[str]:
        """哪些本地插件的文件变了（需要重载）。"""
        changed: list[str] = []
        for plugin_id, snapshot in self._mtimes.items():
            found = self._discovery.plugins.get(plugin_id)
            if found is None or found.path is None:
                continue
            if not found.manifest.auto_reload:
                continue
            current = _scan_mtimes(found.path)
            if current != snapshot:
                changed.append(plugin_id)
        return sorted(changed)

    def _watch_mtimes(self, found: DiscoveredPlugin) -> None:
        if found.path is not None:
            self._mtimes[found.id] = _scan_mtimes(found.path)

    def stop_watching(self) -> None:
        self._watching = False

    async def watch_forever(self) -> None:
        """轮询本地插件目录的 mtime，变了就重载。

        用轮询而不是文件系统事件，是为了不引入 ``watchdog`` 依赖（P5）。
        插件数量是个位数量级，1 秒一次 ``stat`` 的开销可以忽略。
        """
        self._watching = True
        interval = self._config.plugins.auto_reload_interval_seconds
        while self._watching:
            await asyncio.sleep(interval)
            for plugin_id in self.changed_plugins():
                self._logger.info("检测到插件 %s 的文件变化，重新加载", plugin_id)
                self.reload(plugin_id)

    # ── 内部工具 ────────────────────────────────────────────

    def _describe_status(self, plugin_id: str) -> str:
        status = self._status.get(plugin_id)
        return str(status) if status is not None else "unknown"

    def _publish(self, topic: str, payload: dict[str, Any]) -> Event:
        return self._bus.emit(topic, payload, source="plugin_manager")


def _scan_mtimes(directory: Path) -> dict[Path, float]:
    """插件目录下所有被监视文件（``.py`` / ``.toml``）的 mtime。

    用「目录内容整体比较」而不是「逐个文件比较」，是为了让**新增与删除文件**
    也算变化：``importlib`` 的缓存是按模块名记的，删掉一个 helpers 模块
    同样需要重载。
    """
    snapshot: dict[Path, float] = {}
    try:
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix in _WATCHED_SUFFIXES:
                snapshot[path] = path.stat().st_mtime
    except OSError as exc:  # pragma: no cover - 目录被删掉等罕见情况
        snapshot[Path(f"<error:{exc}>")] = time.time()
    return snapshot

"""训练数据集导出插件。

它做的事情很少，而且**故意的**。

## 为什么它这么薄

「把对话整理成训练集」拆开看是四步：读数据库 → 拼样本 → 脱敏 → 落盘。
这四步插件一步都做不了：

- 插件拿不到 ``StorageBackend``——那是组装根（``cli*.py``）才认识的东西
  （``scripts/check_architecture.sh`` 第 3 组红线）；
- 插件也不该自己开文件写数据。它连「导出根在哪」都不知道，
  因为真实路径里含角色名，而角色名在数据库里。

所以真正的编排放在 ``sim/dataset.py``，由 ``alterego dataset`` 命令驱动。
这个插件负责的是**另外那件事**：让内核知道「这个实例会把对话导出成训练集」。

这不是敷衍。插件系统的职责是**声明**——声明这个能力在这里、它的配置是什么、
它现在健不健康。把编排塞进插件只会得到一份拿不到数据的代码。

## 它订阅了什么

**什么都没订阅。**

导出是一个要回看几十天日志、几万条消息的批处理，跑一次按秒算。
把它挂到 ``tick.completed`` 上，推演循环会在一个 30 秒的 tick 中间等它，
而且每 30 秒就把整个训练集重写一遍。想做「定时重导」的话，
那是调度器的事（``kernel/scheduler.py``），不是这里。

## 它不保证的事情

``describe()`` 只说**数据从哪来、脱成什么样**，不说「导过没有」。
「导过没有」要看磁盘，而插件不该去碰磁盘——``alterego dataset list``
读 ``manifest.json`` 回答这个问题，那是拆开两半的完整答案。

依据: docs/adr/0011-training-datasets-are-derived-and-redacted.md
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alterego.interfaces.common import HealthStatus
from alterego.interfaces.simulation import Capability, CapabilityResult
from alterego.kernel.plugin import Plugin, PluginContext


class DatasetExporter(Plugin):
    """把库里的对话、思考、工具调用整理成可训练的 JSONL。"""

    #: 空集合是**故意的**：推演循环不会挑中它。
    #:
    #: 理由见模块 docstring 最后一段。这个插件不是「一次行为」，
    #: 是一个由用户按需触发的批处理入口。
    intent_types: frozenset[str] = frozenset()

    id: str = "capability.dataset_exporter"

    _export_dir: Path | None = None
    _formats: tuple[str, ...] = ()
    _redact_terms: tuple[str, ...] = ()

    def on_load(self, ctx: PluginContext) -> None:
        # 归属自动记在本插件名下（ADR-0007），不要传 owner。
        ctx.registry.register(Capability, self, name="dataset_exporter")

        self._ctx = ctx
        self._absorb(
            ctx.config["export_dir"],
            ctx.config["formats"],
            ctx.config["redact_terms"],
        )

        if self._export_dir is None:
            ctx.logger.info(
                "数据集导出插件已加载；没配 export_dir，按 `alterego dataset` 的默认位置来"
            )
        else:
            ctx.logger.info("数据集导出插件已加载：export_dir=%s", self._export_dir)

    def on_start(self) -> None:
        self._ctx.logger.debug("数据集导出插件已启动")

    def on_stop(self) -> None:
        # 必须幂等：停机与热重载都会调用它。这个插件不持有资源，所以只是打日志。
        self._ctx.logger.debug("数据集导出插件已停止")

    def on_config_changed(self, new_config: dict[str, Any]) -> None:
        """改了三项中的任何一项都立刻生效，不用重启。"""
        self._absorb(
            new_config["export_dir"],
            new_config["formats"],
            new_config["redact_terms"],
        )

    def _absorb(self, export_dir: Any, formats: Any, redact_terms: Any) -> None:
        """把三份配置收成内部状态。缺项按空算，不抛——配置由内核校验。"""
        raw = str(export_dir or "").strip()
        self._export_dir = Path(raw).expanduser() if raw else None
        self._formats = tuple(str(item) for item in formats or ())
        self._redact_terms = tuple(str(item) for item in redact_terms or ())

    @property
    def export_dir(self) -> Path | None:
        """配置里指定的导出根。没配就是 ``None``。

        ``None`` 不是错误状态——它表示「落在哪由 ``alterego dataset`` 决定」。
        真实路径是 ``<导出根>/<角色名>/``，而插件看不到数据库，
        所以不知道角色叫什么叫什么（ADR-0011 §三）。
        """
        return self._export_dir

    @property
    def formats(self) -> tuple[str, ...]:
        """会导出哪几种样本形状。空表示交给命令按 ``[dataset] formats`` 决定。"""
        return self._formats

    @property
    def redact_terms(self) -> tuple[str, ...]:
        """用户额外要求脱掉的词。空表示只用内置的八条规则。"""
        return self._redact_terms

    def describe(self) -> str:
        """一句话说明这份训练集是怎么来的。"""
        where = (
            "落点交给 `alterego dataset` 决定"
            if self._export_dir is None
            else f"落在 {self._export_dir}/<角色名>/"
        )
        shapes = "/".join(self._formats) if self._formats else "按 [dataset] formats"
        extra = f"，另外脱掉 {len(self._redact_terms)} 个自定义词" if self._redact_terms else ""
        return f"训练集：{where}，形状 {shapes}；内置八条脱敏规则一定生效{extra}。"

    def health(self) -> HealthStatus:
        """永远报「正常」，把状态写在 ``detail`` 里。

        这个插件的职责是**说明**配置，不是「配了才健康」。
        「没配 export_dir」是一个完全正常的默认状态——留空表示按约定
        落到 ``exports/datasets/<角色名>/``。把它报成 ``ok=False``
        会让 ``alterego plugins doctor`` 在一件没出错的事情上报警。
        """
        return HealthStatus(ok=True, detail=self.describe())

    async def execute(self, intent: Any, ctx: Any) -> CapabilityResult:
        """报一句配置摘要。

        ``intent_types`` 是空的，所以推演循环不会调到这里；它是给
        ``alterego plugins list`` 和测试用的。
        """
        return CapabilityResult(
            ok=True,
            summary=self.describe(),
            artifacts={
                "export_dir": str(self._export_dir) if self._export_dir else "",
                "formats": ",".join(self._formats),
                "redact_terms": len(self._redact_terms),
            },
        )

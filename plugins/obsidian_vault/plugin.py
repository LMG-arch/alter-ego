"""Obsidian 知识库插件。

它做的事情很少，而且**故意的**。

## 为什么它这么薄

「让角色自己写知识库」这件事拆开看是四步：读数据库 → 渲染笔记 →
问模型怎么归类 → 落盘重建索引。中间这一步要 ``LLMGateway``，
最后一步要 ``StorageBackend``，而这两样东西**插件都拿不到**：

- ``PluginContext`` 里没有 ``llm()``（那是 v0.2.0 才有的东西），
  绕过它自己去 new 一个客户端会被 ``scripts/check_architecture.sh``
  第 7 组红线拦下；
- 插件也拿不到 ``ScheduleRepository`` / ``SourceRepository``——
  那些是组装根（``cli*.py``）才认识的东西，插件不该认识（第 3 组红线）。

所以真正的整理放在 ``sim/vault.py``，由 ``alterego vault`` 命令驱动。
这个插件负责的是**另外那件事**：让内核知道知识库存在。

这不是敷衍。上面那三段分别是「读什么」「想什么」「写哪里」，
而插件系统的职责是**声明**——声明这个能力在这里、它的配置是什么、
它现在健不健康。把编排塞进插件只会得到一份动不了的代码：
它拿不到别的插件的能力（第 6 组红线：插件之间不能互相 import），
也拿不到推演上下文里的状态。

## 它订阅了什么

**什么都没订阅。**

``plugins/example_plugin`` 订阅了 ``tick.completed``，那是为了演示订阅机制。
这里不订阅，是因为它没有需要跟着推演走的事——每 30 秒醒一次只为了记一个
tick 编号，然后这个编号再也没人看，那是纯噪声。

真到了「每个 tick 都该顺手整理一下」的那天，再订阅。

依据: docs/plans/2026-09-16-obsidian-vault.md § 2.2、§ 8
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alterego.interfaces.common import HealthStatus
from alterego.interfaces.simulation import Capability, CapabilityResult
from alterego.kernel.plugin import Plugin, PluginContext


class ObsidianVault(Plugin):
    """角色自己的 Obsidian 知识库。"""

    #: 空集合是**故意的**：推演循环不会挑中它。
    #:
    #: 整理一次要读整张日程表、问一次模型、重写一遍索引——那是一个
    #: 几十秒的批处理，不是一次 tick 里该做的事。把它塞进意图候选里，
    #: 推演循环迟早会在一个 30 秒的 tick 中间等它。
    intent_types: frozenset[str] = frozenset()

    id: str = "capability.obsidian_vault"

    _vault_path: Path | None = None

    def on_load(self, ctx: PluginContext) -> None:
        # 归属自动记在本插件名下（ADR-0007），不要传 owner。
        ctx.registry.register(Capability, self, name="obsidian_vault")

        raw = str(ctx.config["vault_path"]).strip()
        self._vault_path = Path(raw).expanduser() if raw else None
        self._ctx = ctx

        if self._vault_path is None:
            ctx.logger.info("知识库插件已加载；没配 vault_path，按 `alterego vault` 的默认位置来")
        else:
            ctx.logger.info("知识库插件已加载：vault_path=%s", self._vault_path)

    def on_start(self) -> None:
        self._ctx.logger.debug("知识库插件已启动")

    def on_stop(self) -> None:
        # 必须幂等：停机与热重载都会调用它。这个插件不持有资源，所以只是打日志。
        self._ctx.logger.debug("知识库插件已停止")

    def on_config_changed(self, new_config: dict[str, Any]) -> None:
        """改了 ``vault_path`` 立刻生效，不用重启。"""
        raw = str(new_config["vault_path"]).strip()
        self._vault_path = Path(raw).expanduser() if raw else None

    @property
    def vault_path(self) -> Path | None:
        r"""配置里指定的库路径。没配就是 ``None``。

        ``None`` 不是错误状态——它表示「库在哪由 ``alterego vault`` 命令决定」。
        命令知道角色叫什么，插件不知道（角色名在数据库里，而插件看不到数据库）。
        """
        return self._vault_path

    def describe(self) -> str:
        """一句话说明知识库现在什么样。"""
        if self._vault_path is None:
            return "知识库：位置交给 `alterego vault` 决定，插件这边不做检查。"
        if self._vault_path.is_dir():
            return f"知识库：{self._vault_path}（目录在，内容用 `alterego vault status` 看）。"
        return f"知识库：{self._vault_path} 还不存在，跑 `alterego vault init` 建一个。"

    def health(self) -> HealthStatus:
        """永远报「正常」，把状态写在 ``detail`` 里。

        这个插件的职责是**说明**知识库的状态，不是「有库才健康」。
        「还没建库」是一个完全正常的中间状态——用户刚装好、还没跑
        ``vault init`` 就落在这一格里。把它报成 ``ok=False`` 会让
        ``alterego plugins doctor`` 在一件没出错的事情上报警。
        """
        return HealthStatus(ok=True, detail=self.describe())

    async def execute(self, intent: Any, ctx: Any) -> CapabilityResult:
        """报一句状态。

        ``intent_types`` 是空的，所以推演循环不会调到这里；它是给
        ``alterego vault status`` 和测试用的。
        """
        detail = self.describe()
        return CapabilityResult(
            ok=True,
            summary=detail,
            artifacts={"vault_path": str(self._vault_path) if self._vault_path else ""},
        )

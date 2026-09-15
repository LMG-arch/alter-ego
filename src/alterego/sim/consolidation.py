"""记忆梳理：把素材交给模型，把结果写回库。

本模块是**编排**，不是规则。规则在 `domain/consolidation.py`（纯函数），
存储细节在 `storage/sqlite/repositories.py`，模型调用在 `llm/gateway.py`。
这里只负责按对的顺序把它们串起来。

两条路径，一条流水线：

| 命令 | 读什么 | 写出什么 |
| --- | --- | --- |
| ``distill``（梳理数据） | ``activity_log`` 里没梳理过的行为 | 新的 episodic / semantic / emotional 记忆 |
| ``consolidate``（梳理记忆） | 没巩固过的 episodic 记忆 | 归纳出的 semantic 记忆 + 源记忆降权 |

两者共用 ``prompts/memory_consolidate.md``：对模型来说，
「把今天发生的事整理成我会记得的几条」不区分素材是行为日志还是零散记忆。

**依赖从哪里来**：推演循环里这些该由 ``PluginContext`` 提供，但本批次由 CLI
驱动——没有 tick，也没有插件上下文，所以 :class:`MemoryWorkbench` 显式装着
它们。它装的**全是协议**（``MemoryRepository`` / ``ActivityRepository`` /
``StorageBackend`` / ``LLMGateway``），所以 `sim/` 依然不认识 SQLite，
也不认识任何供应商。

**主键由调用方给**（``new_id``）：`sim/` 自己不碰随机源。这不只是洁癖——
测试里换成计数器，同一个输入就能产出同一个库。

依据: docs/design/03-data-model.md § 6.5、docs/design/01-architecture.md § 4
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from alterego.domain.consolidation import (
    MAX_ITEMS,
    MIN_SOURCE_ITEMS,
    WINDOW_HOURS,
    MemoryDraft,
    parse_memory_drafts,
)
from alterego.domain.memory import Memory, apply_consolidation
from alterego.interfaces.repository import (
    ActivityRecord,
    ActivityRepository,
    MemoryRepository,
)
from alterego.interfaces.storage import StorageBackend
from alterego.kernel.errors import SimulationError
from alterego.llm import LLMGateway, PromptLibrary


__all__ = ["MemoryWorkbench", "WorkReport", "consolidate", "distill"]


PURPOSE: Final[str] = "memory"
"""路由用的用途名，对应 ``[llm.routing] memory``。"""

_TEMPERATURE: Final[float] = 0.4
"""比默认的 0.8 低。梳理是归纳，不是创作——这时候的想象力是噪声。"""

_MAX_TOKENS: Final[int] = 2048
"""十条记忆的 JSON 大约一千 token，留一倍余量给模型的啰嗦。"""

_MAX_SOURCE: Final[int] = 200
"""一次最多读多少条素材。"""

_MAX_EXISTING: Final[int] = 20
"""``{existing_memories}`` 里放多少条已有记忆。"""

_EXISTING_WINDOW_DAYS: Final[int] = 30
"""「已有的记忆」往回看多少天。"""

_TEMPLATE: Final[str] = "memory_consolidate"
"""两条路径共用的模板名。对模型来说，「把今天发生的事整理成我会记得的几条」
不区分素材是行为日志还是零散记忆。"""


@dataclass(frozen=True, slots=True)
class MemoryWorkbench:
    """梳理记忆需要的一整套东西。

    Attributes:
        persona_id: 谁的记忆。
        persona_name: 提示词里第一人称的名字。
        user_name: 提示词里对用户的称呼。
        gateway: 模型调用入口。**唯一允许发请求的地方**。
        prompts: 提示词模板库。
        memories: 记忆仓储。
        activities: 行为日志仓储。
        backend: 只用来开事务。有了它，一次梳理的几笔写入才能一起成功或一起回滚。
        new_id: 生成记忆主键。由调用方注入，`sim/` 不碰随机源。
        logger: 日志。
    """

    persona_id: str
    persona_name: str
    user_name: str
    gateway: LLMGateway
    prompts: PromptLibrary
    memories: MemoryRepository
    activities: ActivityRepository
    backend: StorageBackend
    new_id: Callable[[], str]
    logger: logging.Logger


@dataclass(frozen=True, slots=True)
class WorkReport:
    """一次梳理做了什么。

    ``--dry-run`` 与真跑返回同一个形状：调用方不必为预览写一套分支。
    被跳过时 ``skipped`` 有话说，其余字段全是 0。
    """

    source_items: int = 0
    drafted: int = 0
    saved: int = 0
    downgraded: int = 0
    preview: tuple[str, ...] = ()
    skipped: str | None = None


# ── 渲染 ────────────────────────────────────────────────────


def _format_activities(activities: Sequence[ActivityRecord]) -> str:
    """行为日志 → 提示词里的「今天发生的事」。

    ``inner_voice`` 单独一行：一个人记住的常常是自己当时怎么想，
    而不是自己当时做了什么。把它和 ``description`` 挤在一行里，
    两者都变得像日志条目。
    """
    lines: list[str] = []
    for item in activities:
        head = f"- {item.started_at:%H:%M} {item.description}"
        if item.location:
            head += f"（在{item.location}）"
        lines.append(head)
        if item.inner_voice:
            lines.append(f"  心里想着：{item.inner_voice}")
    return "\n".join(lines)


def _format_memories(memories: Sequence[Memory]) -> str:
    """已有记忆 → 提示词里的「已有的记忆（不要重复）」。

    这里用 ``summary``：它本来就是为了进提示词而存在的字段。
    """
    if not memories:
        return "（还没有记下什么）"
    return "\n".join(f"- {memory.summary}" for memory in memories)


def _format_memory_materials(memories: Sequence[Memory]) -> str:
    """待归纳的记忆 → 提示词里的「今天发生的事」。

    这里用 ``content`` 而不是 ``summary``：摘要写库时已经丢过一次信息，
    再拿摘要去归纳，第二批归纳出来的东西会比第一批更空——这正是
    `docs/design/11-optimization-roadmap.md` § 2.3 说的「抽象化漂移」。
    """
    return "\n".join(f"- {memory.occurred_at:%m-%d %H:%M} {memory.content}" for memory in memories)


def _source_ref(ids: Sequence[str]) -> str:
    """来源 id 串成一个字段。

    不截断：``source_ref`` 是「这条记忆从哪儿来」唯一的答案，而它只在排查时
    被读——平时没人看它，所以长一点没关系。
    """
    return ",".join(ids)


# ── 与模型的一次来回 ────────────────────────────────────────


async def _ask(work: MemoryWorkbench, prompt: str) -> list[MemoryDraft]:
    """问一次模型，把回复解析成草稿。

    解析失败**整批丢弃**并上抛，不写半批。理由见 `domain/consolidation.py`
    的模块文档：留下来的半批会出现在下一次的 ``{existing_memories}`` 里，
    而它可能是模型编的。
    """
    response = await work.gateway.complete(
        PURPOSE,
        prompt,
        temperature=_TEMPERATURE,
        max_tokens=_MAX_TOKENS,
        response_format="json",
    )

    try:
        return parse_memory_drafts(response.text)
    except ValueError as exc:
        work.logger.warning("记忆梳理的输出无法解析，整批丢弃：%s", exc)
        raise SimulationError(
            "模型返回的记忆梳理结果无法解析，这一批已丢弃",
            detail=str(exc),
            model=response.model,
            hint="原始回复已写进日志；若是格式问题，可以换一个更强的模型或检查提示词模板。",
        ) from exc


def _render_prompt(work: MemoryWorkbench, materials: str, existing: Sequence[Memory]) -> str:
    return work.prompts.get(_TEMPLATE).render(
        persona_name=work.persona_name,
        user_name=work.user_name,
        activities=materials,
        existing_memories=_format_memories(existing),
        max_items=MAX_ITEMS,
    )


def _existing(work: MemoryWorkbench, now: datetime) -> list[Memory]:
    """最近一个月的记忆，用来告诉模型「这些你已经记住了」。"""
    return work.memories.list_recent(
        work.persona_id,
        since=now - timedelta(days=_EXISTING_WINDOW_DAYS),
        limit=_MAX_EXISTING,
    )


def _too_few(count: int, what: str) -> WorkReport:
    return WorkReport(
        source_items=count,
        skipped=f"这段时间只有 {count} 条{what}，少于 {MIN_SOURCE_ITEMS} 条，不值得调用一次模型。",
    )


# ── 梳理数据 ────────────────────────────────────────────────


async def distill(
    work: MemoryWorkbench,
    *,
    now: datetime,
    dry_run: bool = False,
) -> WorkReport:
    """把做过的事（``activity_log``）提炼成记忆。

    素材不足时**一次模型都不调用**，直接返回 ``skipped``。
    这不是省钱的技巧——三条流水账归纳不出认知，只会把
    「上午开会」写成「上午开了个会」，然后占用一个记忆槽位。
    """
    activities = work.activities.list_undistilled(
        work.persona_id,
        since=now - timedelta(hours=WINDOW_HOURS),
        limit=_MAX_SOURCE,
    )
    if len(activities) < MIN_SOURCE_ITEMS:
        return _too_few(len(activities), "行为")

    materials = _format_activities(activities)
    if dry_run:
        return WorkReport(source_items=len(activities), preview=tuple(materials.splitlines()))

    drafts = await _ask(work, _render_prompt(work, materials, _existing(work, now)))
    source_ref = _source_ref([item.id for item in activities])
    memories = [
        draft.to_memory(
            persona_id=work.persona_id,
            memory_id=work.new_id(),
            occurred_at=now,
            created_at=now,
            source_ref=source_ref,
        )
        for draft in drafts
    ]

    with work.backend.transaction():
        work.memories.save_many(memories)
        # 空结果也要标记：模型说「今天没什么值得记的」是一个结论，
        # 不是一次失败。不标记的话，同样的素材每次都会被重新问一遍。
        work.activities.mark_distilled([item.id for item in activities], now)

    if not memories:
        work.logger.info(
            "模型认为这段时间没有值得记住的事，%d 条行为已标记为梳理过", len(activities)
        )

    return WorkReport(
        source_items=len(activities),
        drafted=len(drafts),
        saved=len(memories),
    )


# ── 梳理记忆 ────────────────────────────────────────────────


async def consolidate(
    work: MemoryWorkbench,
    *,
    now: datetime,
    dry_run: bool = False,
) -> WorkReport:
    """把零散的经历归纳成认知：episodic → semantic。

    归纳完，源记忆**降权但不删除**（``apply_consolidation``）：
    细节模糊了，但没发生过和被忘记是两件事。

    只挑 episodic 作素材。已经归纳出来的 semantic 再归纳一次，
    就是「抽象化漂移」——越归纳越像套话。
    """
    sources = work.memories.list_recent(
        work.persona_id,
        since=now - timedelta(hours=WINDOW_HOURS),
        kind="episodic",
        not_consolidated=True,
        limit=_MAX_SOURCE,
    )
    if len(sources) < MIN_SOURCE_ITEMS:
        return _too_few(len(sources), "记忆")

    materials = _format_memory_materials(sources)
    if dry_run:
        return WorkReport(source_items=len(sources), preview=tuple(materials.splitlines()))

    drafts = await _ask(work, _render_prompt(work, materials, _existing(work, now)))
    source_ref = _source_ref([memory.id for memory in sources])
    derived = [
        draft.to_memory(
            persona_id=work.persona_id,
            memory_id=work.new_id(),
            occurred_at=now,
            created_at=now,
            source_ref=source_ref,
        )
        for draft in drafts
    ]
    downgraded = apply_consolidation(sources)

    with work.backend.transaction():
        work.memories.save_many(derived)
        work.memories.set_importance([(memory.id, memory.importance) for memory in downgraded])
        work.memories.mark_consolidated([memory.id for memory in sources], now)

    return WorkReport(
        source_items=len(sources),
        drafted=len(drafts),
        saved=len(derived),
        downgraded=len(downgraded),
    )

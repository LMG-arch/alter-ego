"""专项学习：一次学一格，外加回答「它为什么突然说起这个」。

这一层把三样东西接起来：库里的笔记（文件）、角色自己的进度（一个小文件）、
模型（一次便宜档的调用）。判断全在 `domain/study.py` 里，这里只管顺序与落盘。

**一次调用学一格。** 为什么不一次性让它把整个领域写完：那样就没有「时间」
了——学完的那一瞬间它什么都懂，之后每天说的话一模一样。而**一次一格**意味着
「它昨天还不知道这个」，那才是这个功能存在的理由。代价是学得慢，这是对的。

**为什么复用 ``vault`` 这个用途而不是新开一个。** ``[llm.routing]`` 加一个键
要动 `kernel/config.py`，而那个文件已经卡在 900 行上限上（见
`kernel/config_study.py` 的说明）。而这一次调用和知识库整理是同一形状的活：
便宜档、要结构化输出、产出是笔记。用户没有理由为两者选不同的模型。
真到了要给学习单独配模型的那天，顺手就把 `config.py` 里的一段挪出去了。

**写进库而不是另开一个目录。** 学到的专业知识和「读到的」「想到的」在
Obsidian 里应该能互相链接——那才是知识库，否则只是一个文件夹。

依据: docs/plans/2026-09-16-specialized-study.md § 3–5
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from alterego.domain.knowledge import Note, slugify
from alterego.domain.study import (
    DEFAULT_MIN_SCORE,
    DEFAULT_RECALL_LIMIT,
    STATE_FILENAME,
    Field,
    Progress,
    Recall,
    Topic,
    advance,
    field_hint,
    next_topics,
    parse_progress,
    parse_study_note,
    render_note_body,
    resolve_field,
    select,
    to_payload,
)
from alterego.domain.vault import FOLDER_INDEX, FOLDER_STUDY, TYPES, VaultIssue
from alterego.interfaces.repository import PersonaRecord
from alterego.kernel.errors import SimulationError
from alterego.llm import LLMGateway, PromptLibrary
from alterego.sim.vault import build, scan, write_note


__all__ = [
    "LearnReport",
    "ReadReport",
    "StudyWorkbench",
    "learn",
    "plan",
    "read",
    "recall",
    "resolve",
    "study_notes",
]


PURPOSE: Final[str] = "vault"
"""路由用的用途名。见模块开头的说明——复用，不新开。"""

_TEMPLATE: Final[str] = "study_topic"
"""学习用的提示词模板。"""

_TEMPERATURE: Final[float] = 0.6
"""比聊天低、比整理略高：要的是「像总结」，不是「像创作」。"""

_MAX_TOKENS: Final[int] = 900

_MAX_KNOWN: Final[int] = 30
"""提示词里最多列几篇已写过的笔记。列全了会把题目本身挤掉。"""

_BAD_REPLY: Final[str] = "模型没给出结构完整的一篇（要有 summary 和至少一条 points）"


@dataclass(frozen=True, slots=True)
class StudyWorkbench:
    """跑一次学习任务需要的一切。

    ``field`` / ``rounds`` / ``recall_limit`` / ``min_score`` 是配置的原样，
    故意**不在这里解释**：解释它们属于 :func:`resolve`——「配了什么」和
    「认出了哪个领域」是两件事，混在一起之后报错时说不清是谁的问题。
    """

    persona: PersonaRecord
    root: Path
    gateway: LLMGateway
    prompts: PromptLibrary
    logger: logging.Logger
    field: str = ""
    rounds: int = 1
    recall_limit: int = DEFAULT_RECALL_LIMIT
    min_score: float = DEFAULT_MIN_SCORE


# ── 报告 ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReadReport:
    """``status`` 看到的东西。不调模型、不写文件。"""

    root: Path
    field: Field | None = None
    hint: str = ""
    done: int = 0
    rounds_done: int = 0
    last_at: str = ""
    notes: tuple[Note, ...] = ()
    unindexed: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        """``60-专业`` 里实际有几篇。

        **以文件为准，不以进度为准**：用户可能在 Obsidian 里自己删了一篇，
        也可能自己写了一篇。进度说的是「学过什么」，文件说的是「库里有什么」，
        两个不对的时候，先把两个数都摆出来。
        """
        return len(self.notes)


@dataclass(frozen=True, slots=True)
class LearnReport:
    """``next`` 干了什么。"""

    field_label: str = ""
    learned: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    preview: tuple[Topic, ...] = ()
    skipped: str | None = None
    issues: tuple[VaultIssue, ...] = ()
    dry_run: bool = False


# ── 路径与小文件 ────────────────────────────────────────────


def _state_path(root: Path) -> Path:
    """进度文件的位置。

    放在 ``00-索引/`` 里和 ``.vault-state.json`` 做邻居：**隐藏状态集中在一处**，
    用户想备份、或者想删掉重来时不必满库找。以点开头，Obsidian 也不显示它。
    """
    return root / FOLDER_INDEX / STATE_FILENAME


def _load_progress(root: Path, *, field: Field) -> Progress:
    """读进度。文件不在、读不动、内容不是字典，都当成「没学过」。"""
    path = _state_path(root)
    payload: object = {}
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
    return parse_progress(payload if isinstance(payload, dict) else {}, field=field)


def _save_progress(root: Path, progress: Progress) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(to_payload(progress), ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(f"{text}\n", encoding="utf-8", newline="\n")


# ── 读 ──────────────────────────────────────────────────────


def study_notes(notes: Sequence[Note]) -> tuple[Note, ...]:
    """挑出 ``60-专业`` 里那几篇。

    :func:`recall` 只在调用方递进来的笔记里找。把范围收在**这一层**而不是
    写在提示词里，是为了让「它怎么突然说起这个」有确定的答案：
    它只会说起自己在 ``60-专业`` 里记过的东西。
    """
    return tuple(note for note in notes if note.folder == FOLDER_STUDY)


def resolve(work: StudyWorkbench) -> Field | None:
    """这一轮学什么。认不出来返回 `None`（不编一个）。"""
    return resolve_field(work.field, occupation=work.persona.occupation or "")


def _unindexed(root: Path, notes: Sequence[Note]) -> tuple[str, ...]:
    """在 ``60-专业`` 里、但索页码没提到的笔记。

    索引是派生的（``build`` 会重算），所以正常情况下这里永远是空的。
    不为空只有两种可能：用户手写的，或者上一次写到一半崩了。
    宁可在 ``status`` 里点出来，也不要让一篇笔记在库里当孤儿——
    孤儿在 Obsidian 里点不开：搜得到，接不上。
    """
    index = root / FOLDER_INDEX / f"{TYPES[FOLDER_STUDY]}.md"
    try:
        text = index.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = ""
    return tuple(note.path for note in notes if f"[[{note.stem}]]" not in text)


def read(work: StudyWorkbench, *, now: datetime) -> ReadReport:
    """看一眼现状。**不调模型、不写文件、不花钱。**"""
    field = resolve(work)
    notes = study_notes(scan(work.root, now=now, logger=work.logger))
    if field is None:
        return ReadReport(
            root=work.root,
            hint=field_hint(work.persona.occupation or ""),
            notes=notes,
            unindexed=_unindexed(work.root, notes),
        )
    progress = _load_progress(work.root, field=field)
    return ReadReport(
        root=work.root,
        field=field,
        done=len(progress.done),
        rounds_done=progress.rounds_done,
        last_at=progress.last_at,
        notes=notes,
        unindexed=_unindexed(work.root, notes),
    )


def plan(work: StudyWorkbench, *, rounds: int = 0) -> tuple[Topic, ...]:
    """接下来该学哪几格。纯读，不花钱。"""
    field = resolve(work)
    if field is None:
        return ()
    count = rounds if rounds > 0 else work.rounds
    progress = _load_progress(work.root, field=field)
    return next_topics(field, progress=progress, rounds=count)


def recall(
    work: StudyWorkbench,
    *,
    now: datetime,
    query: str,
    limit: int = 0,
    min_score: float = 0.0,
) -> Recall:
    """一句话 → 该翻出来的那几篇专业笔记。

    返回结构里带着分数和命中的词，所以「为什么是这几篇」不用猜。
    """
    return select(
        study_notes(scan(work.root, now=now, logger=work.logger)),
        query=query,
        limit=limit if limit > 0 else work.recall_limit,
        min_score=min_score if min_score > 0 else work.min_score,
    )


# ── 写 ──────────────────────────────────────────────────────


def _known(notes: Sequence[Note]) -> str:
    """提示词里的「我已经写过的」。用文件名当链接名——它本来就是链接名。"""
    if not notes:
        return "（还没写过。这是第一格。）"
    listed = list(notes)[-_MAX_KNOWN:]
    lines = [f"- {note.stem} · {note.title}" for note in listed]
    hidden = len(notes) - len(listed)
    if hidden > 0:
        lines.append(f"（还有 {hidden} 篇没列出来）")
    return "\n".join(lines)


def _note(*, field: Field, topic: Topic, body: str, now: datetime) -> Note:
    """主题 + 正文 → 一篇笔记。

    文件名由**标题**决定，所以同一个主题重跑两次落成同一个文件——
    幂等靠这个，而不是靠「记得自己写过什么」。
    """
    return Note(
        path=f"{FOLDER_STUDY}/{slugify(topic.title)}.md",
        title=topic.title,
        type=TYPES[FOLDER_STUDY],
        created=now,
        tags=(field.label, topic.aspect, f"第 {topic.round} 轮"),
        body=body,
    )


async def _ask(work: StudyWorkbench, *, field: Field, topic: Topic, known: str) -> str:
    """问一次模型，返回原文。

    调用本身失败就抛出去**不吞**：那多半是限流或没配密钥，继续问下去
    只会把同一个错问三遍。而「回复没法用」是另一回事——按题处理的。
    """
    prompt = work.prompts.render(
        _TEMPLATE,
        persona_name=work.persona.name,
        field=field.label,
        topic_title=topic.title,
        round=str(topic.round),
        known=known,
    )
    try:
        response = await work.gateway.complete(
            PURPOSE,
            prompt,
            temperature=_TEMPERATURE,
            max_tokens=_MAX_TOKENS,
            response_format="json",
            metadata={"persona_id": work.persona.id, "topic": topic.key},
        )
    except Exception as exc:
        raise SimulationError("学习时模型调用失败", purpose=PURPOSE, topic=topic.key) from exc
    return response.text


async def learn(
    work: StudyWorkbench,
    *,
    now: datetime,
    rounds: int = 0,
    dry_run: bool = False,
) -> LearnReport:
    """学 ``rounds`` 格，写进 ``60-专业``。

    ``dry_run`` **不调模型**，只把「这次打算学什么」摊开给人看——花不花钱
    应该先看得见（和 ``vault organize --dry-run`` 同一条理由）。

    一格失败**既不影响别的格，也不记进度**：它算没学过，下次还会来。
    把一次坏回复记成「学过」，是这个功能里最容易犯、也最难发现的错——
    它的表现是角色从此缺一块知识，而且再也补不上，因为它不会再回到那一格。
    """
    field = resolve(work)
    if field is None:
        return LearnReport(skipped=field_hint(work.persona.occupation or ""))

    count = rounds if rounds > 0 else work.rounds
    progress = _load_progress(work.root, field=field)
    topics = next_topics(field, progress=progress, rounds=count)
    if dry_run:
        return LearnReport(field_label=field.label, preview=topics, dry_run=True)

    existing = study_notes(scan(work.root, now=now, logger=work.logger))
    written: list[Note] = []
    failed: list[tuple[str, str]] = []
    done: list[Topic] = []

    for topic in topics:
        reply = await _ask(work, field=field, topic=topic, known=_known([*existing, *written]))
        draft = parse_study_note(reply)
        if draft is None:
            failed.append((topic.title, _BAD_REPLY))
            work.logger.warning("这一格没写出东西", extra={"topic": topic.key})
            continue
        note = _note(
            field=field,
            topic=topic,
            body=render_note_body(draft, field=field, topic=topic),
            now=now,
        )
        write_note(work.root, note)
        written.append(note)
        done.append(topic)

    if not done:
        return LearnReport(field_label=field.label, failed=tuple(failed), preview=topics)

    # 索引必须在笔记之后重建：新写的那篇要能被索页码提到。否则它会以
    # 「孤儿」的身份出现在下一份 `vault status` 里，看起来像我们写坏了库。
    report = build(work, now=now)
    _save_progress(work.root, advance(progress, field=field, topics=done, at=now.isoformat()))
    work.logger.info("学完了这一批", extra={"learned": len(done), "failed": len(failed)})
    return LearnReport(
        field_label=field.label,
        learned=tuple(note.path for note in written),
        failed=tuple(failed),
        preview=topics,
        issues=report.issues,
    )

"""``sim/study.py`` 的规矩：一次学一格、坏回复不算学过、只翻自己记过的。

这个文件里最要紧的三条断言是：

1. **一次调用学一格。** 一次学完就没有「昨天还不知道」了，那正是这个功能
   存在的理由。
2. **一格失败不记进度。** 把一次坏回复记成「学过」，表现是角色从此缺一块
   知识且再也补不上——这是这个功能里最难发现的错。
3. **召回只在自己记过的东西里找。** 「它怎么突然说起这个」必须能答。

依据: docs/plans/2026-09-16-specialized-study.md § 3–5
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from alterego.domain.knowledge import Note, slugify
from alterego.domain.study import STATE_FILENAME, Topic
from alterego.domain.vault import (
    FOLDER_INBOX,
    FOLDER_INDEX,
    FOLDER_STUDY,
    TYPES,
)
from alterego.interfaces.llm import LLMResponse
from alterego.interfaces.repository import PersonaRecord
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.errors import SimulationError
from alterego.kernel.logging import get_logger
from alterego.llm import PromptLibrary
from alterego.sim.study import (
    PURPOSE,
    StudyWorkbench,
    _known,
    learn,
    plan,
    read,
    recall,
    resolve,
    study_notes,
)


TZ = resolve_timezone("Asia/Shanghai")
T0 = datetime(2026, 9, 16, 21, 40, tzinfo=TZ)

PERSONA = PersonaRecord(id="p1", name="阿哲", occupation="算法工程师")
CLERK = PersonaRecord(id="p2", name="小满", occupation="会计")


# ── 假的模型 ────────────────────────────────────────────────


class StubGateway:
    """假网关。这里只关心「它回了什么」和「它被叫了几次」。

    ``replies`` 按顺序发，用完之后回落到 ``reply``：一次学多格时，
    每一格的回复都不一样，而大多数用例只关心「回了什么」。
    """

    def __init__(
        self,
        reply: str = "",
        *,
        replies: Sequence[str] = (),
        error: Exception | None = None,
    ) -> None:
        self.reply = reply
        self.replies = list(replies)
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def complete(self, purpose: str, prompt: str, **_: Any) -> LLMResponse:
        self.calls.append((purpose, prompt))
        if self.error is not None:
            raise self.error
        if self.replies:
            return LLMResponse(text=self.replies.pop(0), model="stub")
        return LLMResponse(text=self.reply, model="stub")

    @property
    def prompts(self) -> list[str]:
        return [prompt for _, prompt in self.calls]


def answer(
    summary: str = "降维是把高维压到低维。",
    *,
    points: Sequence[str] = ("先想清楚保留什么",),
    unsure: Sequence[str] = ("损失多少算够，我还没搞清",),
) -> str:
    """一段结构完整的回复。"""
    return json.dumps(
        {"summary": summary, "points": list(points), "unsure": list(unsure)},
        ensure_ascii=False,
    )


# ── 夹具 ────────────────────────────────────────────────────


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "阿哲的知识库"
    path.mkdir()
    return path


@pytest.fixture
def gateway() -> StubGateway:
    return StubGateway(answer())


def make(
    root: Path,
    gateway: StubGateway,
    *,
    persona: PersonaRecord = PERSONA,
    field: str = "",
    rounds: int = 1,
    **rest: Any,
) -> StudyWorkbench:
    return StudyWorkbench(
        persona=persona,
        root=root,
        gateway=gateway,  # type: ignore[arg-type]
        prompts=PromptLibrary(),
        logger=get_logger("test.study"),
        field=field,
        rounds=rounds,
        **rest,
    )


def state_file(root: Path) -> Path:
    return root / FOLDER_INDEX / STATE_FILENAME


def write_markdown(root: Path, *, folder: str, name: str, body: str = "") -> Path:
    path = root / folder / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {name}\n\n{body}\n", encoding="utf-8", newline="\n")
    return path


def topic_path(topic: Topic) -> str:
    return f"{FOLDER_STUDY}/{slugify(topic.title)}.md"


# ────────────────────────────────────────────────────────────
# 学什么
# ────────────────────────────────────────────────────────────


class TestResolve:
    def test_it_reads_the_occupation(self, root: Path, gateway: StubGateway) -> None:
        field = resolve(make(root, gateway))
        assert field is not None
        assert field.label == "数据与算法"
        assert field.source == "occupation"

    def test_configuration_wins(self, root: Path, gateway: StubGateway) -> None:
        field = resolve(make(root, gateway, field="计算机视觉"))
        assert field is not None
        assert field.label == "计算机视觉"
        assert field.source == "config"

    def test_an_unrecognisable_occupation_is_none(self, root: Path, gateway: StubGateway) -> None:
        """**不编一个领域出来。** 瞎猜的专业会写出一堆像模像样的空话。"""
        assert resolve(make(root, gateway, persona=PersonaRecord(id="p9", name="阿哲"))) is None


class TestPlan:
    def test_it_starts_from_the_beginning(self, root: Path, gateway: StubGateway) -> None:
        topics = plan(make(root, gateway, rounds=2))
        assert len(topics) == 2
        assert topics[0].key == "data/1/是什么"

    def test_the_rounds_argument_wins(self, root: Path, gateway: StubGateway) -> None:
        """``--rounds`` 是「这次多看几格」，压过配置里的默认值。"""
        assert len(plan(make(root, gateway, rounds=1), rounds=3)) == 3

    def test_it_skips_what_is_already_done(self, root: Path, gateway: StubGateway) -> None:
        state_file(root).parent.mkdir(parents=True, exist_ok=True)
        state_file(root).write_text(
            json.dumps({"field_key": "data", "done": ["data/1/是什么"]}), encoding="utf-8"
        )
        assert plan(make(root, gateway, rounds=1))[0].key == "data/1/怎么做"

    def test_it_uses_the_personas_own_field(self, root: Path, gateway: StubGateway) -> None:
        assert plan(make(root, gateway, persona=CLERK))[0].key.startswith("finance/")

    def test_no_field_no_plan(self, root: Path, gateway: StubGateway) -> None:
        assert plan(make(root, gateway, persona=PersonaRecord(id="p9", name="阿哲"))) == ()


# ────────────────────────────────────────────────────────────
# 看一眼
# ────────────────────────────────────────────────────────────


class TestRead:
    def test_it_does_not_call_the_model(self, root: Path, gateway: StubGateway) -> None:
        """看状态不该花钱——否则用户不敢敲第二次。"""
        read(make(root, gateway), now=T0)
        assert gateway.calls == []

    def test_it_does_not_write_anything(self, root: Path, gateway: StubGateway) -> None:
        read(make(root, gateway), now=T0)
        assert list(root.rglob("*")) == []

    def test_an_empty_vault_says_nothing_was_learned(
        self, root: Path, gateway: StubGateway
    ) -> None:
        report = read(make(root, gateway), now=T0)
        assert report.done == 0
        assert report.rounds_done == 0
        assert report.total == 0
        assert report.last_at == ""
        assert report.field is not None

    def test_it_only_counts_the_professional_folder(self, root: Path, gateway: StubGateway) -> None:
        write_markdown(root, folder=FOLDER_INBOX, name="随手记")
        write_markdown(root, folder=FOLDER_STUDY, name="卷积是什么")
        report = read(make(root, gateway), now=T0)
        assert report.total == 1
        assert report.notes[0].title == "卷积是什么"

    def test_an_unknown_field_still_reports_the_notes(
        self, root: Path, gateway: StubGateway
    ) -> None:
        write_markdown(root, folder=FOLDER_STUDY, name="会计是什么")
        report = read(make(root, gateway, persona=PersonaRecord(id="p9", name="阿哲")), now=T0)
        assert report.field is None
        assert report.hint
        assert report.total == 1

    def test_it_reports_a_note_the_index_never_mentions(
        self, root: Path, gateway: StubGateway
    ) -> None:
        """孤儿在 Obsidian 里点不开：搜得到，接不上——所以要点出来。"""
        write_markdown(root, folder=FOLDER_STUDY, name="手写的一篇")
        assert read(make(root, gateway), now=T0).unindexed == (f"{FOLDER_STUDY}/手写的一篇.md",)

    def test_a_note_inside_the_index_is_not_reported(
        self, root: Path, gateway: StubGateway
    ) -> None:
        index = root / FOLDER_INDEX / f"{TYPES[FOLDER_STUDY]}.md"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text("- [[卷积是什么]] · 卷积是什么\n", encoding="utf-8", newline="\n")
        write_markdown(root, folder=FOLDER_STUDY, name="卷积是什么")
        assert read(make(root, gateway), now=T0).unindexed == ()

    def test_it_reads_the_progress_back(self, root: Path, gateway: StubGateway) -> None:
        work = make(root, gateway)
        state_file(root).parent.mkdir(parents=True, exist_ok=True)
        state_file(root).write_text(
            json.dumps(
                {
                    "field_key": "data",
                    "done": ["data/1/是什么", "data/1/怎么做"],
                    "last_at": "2026-09-15T20:00:00",
                }
            ),
            encoding="utf-8",
        )
        again = read(work, now=T0)
        assert again.done == 2
        assert again.rounds_done == 0
        assert again.last_at == "2026-09-15T20:00:00"

    def test_a_corrupt_state_file_means_nothing_was_learned(
        self, root: Path, gateway: StubGateway
    ) -> None:
        state_file(root).parent.mkdir(parents=True, exist_ok=True)
        state_file(root).write_text("{ 这不是 json", encoding="utf-8")
        assert read(make(root, gateway), now=T0).done == 0

    def test_state_for_another_field_is_ignored(self, root: Path, gateway: StubGateway) -> None:
        """两个专业的主题混在一个进度里，「学到第几个了」就没意义了。"""
        state_file(root).parent.mkdir(parents=True, exist_ok=True)
        state_file(root).write_text(
            json.dumps({"field_key": "finance", "done": ["finance/1/是什么"]}),
            encoding="utf-8",
        )
        work = make(root, gateway)
        assert read(work, now=T0).done == 0
        assert plan(work)[0].key == "data/1/是什么"


# ────────────────────────────────────────────────────────────
# 学一格
# ────────────────────────────────────────────────────────────


class TestLearn:
    async def test_it_writes_one_note_per_topic(self, root: Path, gateway: StubGateway) -> None:
        work = make(root, gateway, rounds=1)
        first = plan(work)[0]
        report = await learn(work, now=T0)
        assert report.learned == (topic_path(first),)
        assert not report.failed
        assert not report.skipped

    async def test_it_asks_the_cheap_purpose(self, root: Path, gateway: StubGateway) -> None:
        """复用 ``vault`` 这个用途名，不为学习单开一个路由键。"""
        await learn(make(root, gateway), now=T0)
        assert {purpose for purpose, _ in gateway.calls} == {PURPOSE}

    async def test_the_note_lands_in_the_professional_folder(
        self, root: Path, gateway: StubGateway
    ) -> None:
        report = await learn(make(root, gateway), now=T0)
        written = root / report.learned[0]
        assert written.is_file()
        assert written.parent.name == FOLDER_STUDY

    async def test_the_body_carries_the_mechanism_sections(
        self, root: Path, gateway: StubGateway
    ) -> None:
        """「我还不确定的」那一节是机制不是格式，缺了就不算一篇。"""
        report = await learn(make(root, gateway), now=T0)
        text = (root / report.learned[0]).read_text(encoding="utf-8")
        assert "## 我还不确定的" in text
        assert "降维是把高维压到低维。" in text

    async def test_it_records_the_progress(self, root: Path, gateway: StubGateway) -> None:
        work = make(root, gateway)
        await learn(work, now=T0)
        payload = json.loads(state_file(root).read_text(encoding="utf-8"))
        assert payload["done"] == ["data/1/是什么"]
        assert payload["field_key"] == "data"
        assert payload["last_at"] == T0.isoformat()

    async def test_the_second_run_learns_the_next_topic(
        self, root: Path, gateway: StubGateway
    ) -> None:
        work = make(root, gateway)
        first = await learn(work, now=T0)
        second = await learn(work, now=T0)
        assert first.learned != second.learned
        assert read(work, now=T0).done == 2

    async def test_rerunning_the_same_topic_overwrites_it(
        self, root: Path, gateway: StubGateway
    ) -> None:
        """幂等靠**文件名由标题决定**，不靠「记得自己写过什么」。"""
        work = make(root, gateway)
        await learn(work, now=T0)
        state_file(root).unlink()
        await learn(work, now=T0)
        assert len(list((root / FOLDER_STUDY).glob("*.md"))) == 1

    async def test_it_rebuilds_the_index(self, root: Path, gateway: StubGateway) -> None:
        """索引是派生的；不重建的话新笔记会以孤儿身份出现在 status 里。"""
        work = make(root, gateway)
        report = await learn(work, now=T0)
        assert report.issues == ()
        assert read(work, now=T0).unindexed == ()

    async def test_it_tells_the_model_what_it_already_wrote(
        self, root: Path, gateway: StubGateway
    ) -> None:
        work = make(root, gateway)
        await learn(work, now=T0)
        await learn(work, now=T0)
        assert "数据与算法 · 是什么" in gateway.prompts[-1]

    async def test_the_first_topic_says_there_is_nothing_yet(
        self, root: Path, gateway: StubGateway
    ) -> None:
        await learn(make(root, gateway), now=T0)
        assert "还没写过" in gateway.prompts[0]

    async def test_a_broken_reply_is_a_failure_not_a_note(
        self, root: Path, gateway: StubGateway
    ) -> None:
        gateway.reply = "我看了看，觉得挺有意思的。"
        report = await learn(make(root, gateway), now=T0)
        assert report.learned == ()
        assert len(report.failed) == 1
        assert not list((root / FOLDER_STUDY).glob("*.md"))

    async def test_a_broken_reply_is_not_recorded_as_progress(
        self, root: Path, gateway: StubGateway
    ) -> None:
        """**这是这个功能里最难发现也最贵的错。** 记成学过，那一格就再也
        回不来了——它会缺一块知识，而且没有任何地方写着它缺。"""
        gateway.reply = "不合格的回复"
        work = make(root, gateway)
        await learn(work, now=T0)
        assert not state_file(root).exists()
        assert plan(work)[0].key == "data/1/是什么"

    async def test_one_bad_topic_does_not_stop_the_next(
        self, root: Path, gateway: StubGateway
    ) -> None:
        gateway.replies = ["不合格的回复", answer(summary="第二格写出来了。")]
        report = await learn(make(root, gateway, rounds=2), now=T0)
        assert len(report.learned) == 1
        assert len(report.failed) == 1
        assert report.failed[0][0].endswith("是什么")
        assert "第二格写出来了。" in (root / report.learned[0]).read_text(encoding="utf-8")

    async def test_only_the_good_topics_are_recorded(
        self, root: Path, gateway: StubGateway
    ) -> None:
        gateway.replies = ["坏的", answer()]
        work = make(root, gateway, rounds=2)
        await learn(work, now=T0)
        payload = json.loads(state_file(root).read_text(encoding="utf-8"))
        assert payload["done"] == ["data/1/怎么做"]

    async def test_it_comes_back_to_the_failed_topic(
        self, root: Path, gateway: StubGateway
    ) -> None:
        gateway.replies = ["坏的", answer()]
        work = make(root, gateway, rounds=2)
        await learn(work, now=T0)
        assert plan(work)[0].key == "data/1/是什么"

    async def test_a_call_failure_is_raised_not_swallowed(
        self, root: Path, gateway: StubGateway
    ) -> None:
        """多半是限流或没配密钥，继续问下去只会把同一个错问三遍。"""
        gateway.error = RuntimeError("429")
        with pytest.raises(SimulationError):
            await learn(make(root, gateway, rounds=3), now=T0)
        assert len(gateway.calls) == 1

    async def test_the_failure_carries_the_purpose(self, root: Path, gateway: StubGateway) -> None:
        gateway.error = RuntimeError("boom")
        with pytest.raises(SimulationError) as caught:
            await learn(make(root, gateway), now=T0)
        assert caught.value.context["purpose"] == PURPOSE

    async def test_a_dry_run_calls_nothing(self, root: Path, gateway: StubGateway) -> None:
        report = await learn(make(root, gateway, rounds=2), now=T0, dry_run=True)
        assert gateway.calls == []
        assert len(report.preview) == 2
        assert report.dry_run

    async def test_a_dry_run_writes_nothing(self, root: Path, gateway: StubGateway) -> None:
        await learn(make(root, gateway, rounds=2), now=T0, dry_run=True)
        assert list(root.rglob("*")) == []

    async def test_an_unknown_field_is_skipped_with_a_hint(
        self, root: Path, gateway: StubGateway
    ) -> None:
        report = await learn(
            make(root, gateway, persona=PersonaRecord(id="p9", name="阿哲")), now=T0
        )
        assert report.skipped is not None
        assert "[study] field" in report.skipped
        assert gateway.calls == []

    async def test_it_learns_the_personas_own_field(self, root: Path, gateway: StubGateway) -> None:
        report = await learn(make(root, gateway, persona=CLERK), now=T0)
        assert report.field_label == "财务与会计"
        assert report.learned[0].startswith(f"{FOLDER_STUDY}/财务与会计")

    async def test_it_honours_the_configured_rounds(self, root: Path, gateway: StubGateway) -> None:
        report = await learn(make(root, gateway, rounds=2), now=T0)
        assert len(report.learned) == 2
        assert len(gateway.calls) == 2

    async def test_nothing_to_learn_is_a_no_op(self, root: Path, gateway: StubGateway) -> None:
        report = await learn(make(root, gateway, rounds=0), now=T0)
        assert report.learned == ()
        assert not report.failed
        assert gateway.calls == []


# ────────────────────────────────────────────────────────────
# 翻出来用
# ────────────────────────────────────────────────────────────


class TestRecall:
    async def test_it_finds_what_it_just_wrote(self, root: Path, gateway: StubGateway) -> None:
        work = make(root, gateway)
        await learn(work, now=T0)
        found = recall(work, now=T0, query="今天数据与算法那块有点糊")
        assert found.hit
        assert "数据与算法" in found.matches[0].title

    def test_an_empty_vault_finds_nothing(self, root: Path, gateway: StubGateway) -> None:
        """空召回是正常状态，不是错误。"""
        assert not recall(make(root, gateway), now=T0, query="降维").hit

    def test_an_unrelated_message_finds_nothing(self, root: Path, gateway: StubGateway) -> None:
        write_markdown(root, folder=FOLDER_STUDY, name="降维是什么", body="把高维压到低维。")
        assert not recall(make(root, gateway), now=T0, query="周末去哪玩").hit

    def test_a_question_word_does_not_pull_a_note(self, root: Path, gateway: StubGateway) -> None:
        """问句里的虚词不是话题。

        第 1 轮的题面全都是 ``X · 是什么``，所以只要不过滤虚词，
        「今晚吃什么」就能靠一个 ``什么`` 把整个第一轮翻出来——
        那时候 ``min_score`` 那个门槛就不剩什么意义了。
        """
        write_markdown(root, folder=FOLDER_STUDY, name="降维是什么", body="把高维压到低维。")
        assert not recall(make(root, gateway), now=T0, query="今晚吃什么").hit

    def test_the_real_topic_still_lands(self, root: Path, gateway: StubGateway) -> None:
        """虚词被丢掉了，但真话题得留着。"""
        write_markdown(root, folder=FOLDER_STUDY, name="降维是什么", body="把高维压到低维。")
        assert recall(make(root, gateway), now=T0, query="今天这个降维怎么弄").hit

    def test_it_never_reaches_outside_the_professional_folder(
        self, root: Path, gateway: StubGateway
    ) -> None:
        """范围收在这一层，所以「它怎么突然说起这个」有确定的答案。"""
        write_markdown(root, folder=FOLDER_INBOX, name="降维随手记", body="降维。")
        found = recall(make(root, gateway), now=T0, query="降维")
        assert not found.hit
        assert found.considered == 0

    def test_considered_counts_the_whole_folder(self, root: Path, gateway: StubGateway) -> None:
        write_markdown(root, folder=FOLDER_STUDY, name="降维是什么", body="降维。")
        write_markdown(root, folder=FOLDER_STUDY, name="卷积是什么", body="卷积。")
        found = recall(make(root, gateway), now=T0, query="降维", limit=5)
        assert found.considered == 2

    def test_the_limit_comes_from_the_workbench(self, root: Path, gateway: StubGateway) -> None:
        for index in range(4):
            write_markdown(root, folder=FOLDER_STUDY, name=f"降维 第{index}篇", body="降维。")
        work = make(root, gateway, recall_limit=2)
        assert len(recall(work, now=T0, query="降维").matches) == 2

    def test_the_limit_argument_wins(self, root: Path, gateway: StubGateway) -> None:
        for index in range(4):
            write_markdown(root, folder=FOLDER_STUDY, name=f"降维 第{index}篇", body="降维。")
        work = make(root, gateway, recall_limit=1)
        assert len(recall(work, now=T0, query="降维", limit=3).matches) == 3

    def test_the_score_comes_from_the_workbench(self, root: Path, gateway: StubGateway) -> None:
        """门槛调高之后，连标题命中也进不来——那是用户自己拧的旋钮。"""
        write_markdown(root, folder=FOLDER_STUDY, name="降维是什么", body="降维。")
        work = make(root, gateway, min_score=99.0)
        assert not recall(work, now=T0, query="降维").hit

    def test_the_min_score_argument_wins(self, root: Path, gateway: StubGateway) -> None:
        write_markdown(root, folder=FOLDER_STUDY, name="降维是什么", body="降维。")
        work = make(root, gateway, min_score=0.0)
        assert recall(work, now=T0, query="降维", min_score=99.0).threshold == 99.0


# ────────────────────────────────────────────────────────────
# 小工具
# ────────────────────────────────────────────────────────────


class TestStudyNotes:
    def test_it_keeps_only_the_professional_folder(self) -> None:
        notes = (
            Note(path=f"{FOLDER_STUDY}/a.md", title="a", type="专业", created=T0),
            Note(path=f"{FOLDER_INBOX}/b.md", title="b", type="待整理", created=T0),
            Note(path=f"{FOLDER_INDEX}/c.md", title="c", type="索引", created=T0),
        )
        assert study_notes(notes) == (notes[0],)

    def test_an_empty_vault_has_no_study_notes(self) -> None:
        assert study_notes(()) == ()


class TestKnown:
    """提示词里那份「我已经写过的」清单。"""

    def test_the_first_time_it_says_so(self) -> None:
        assert "还没写过" in _known(())

    def test_it_lists_name_and_title(self) -> None:
        one = Note(
            path=f"{FOLDER_STUDY}/降维是什么.md", title="降维是什么", type="专业", created=T0
        )
        assert "- 降维是什么 · 降维是什么" in _known((one,))

    def test_a_long_history_is_capped(self) -> None:
        """列全了会把题目本身挤掉。"""
        notes = tuple(
            Note(
                path=f"{FOLDER_STUDY}/第{index}篇.md",
                title=f"第{index}篇",
                type="专业",
                created=T0,
            )
            for index in range(31)
        )
        text = _known(notes)
        assert "还有 1 篇没列出来" in text
        assert "第0篇" not in text
        assert "第30篇" in text

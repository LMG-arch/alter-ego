"""``sim/vault.py`` 的规矩：三步走，两块钱。

这个文件里最要紧的两条断言是：

1. **``sync`` 跑两遍，库逐字节不变。** 幂等不是「差不多」——同一份数据
   渲染两次必须产出同样的字节，否则每跑一次 git 就多一堆无意义的 diff。
2. **``organize`` 坏掉的时候，收集箱里的东西原样还在。** 一次整理失败
   最坏的结果是「没整理」，绝不能是「东西没了」。

所以每个测试都在 ``tmp_path`` 里造一个库，**从不碰真的知识库**。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from alterego.domain.memory import Memory
from alterego.domain.vault import (
    FOLDER_INBOX,
    FOLDER_INDEX,
    FOLDER_SCHEDULE,
    FOLDER_THOUGHTS,
    FOLDERS,
    STATE_FILENAME,
    validate,
)
from alterego.interfaces.llm import LLMResponse
from alterego.interfaces.repository import (
    ActivityRecord,
    PersonaRecord,
    ScheduleRecord,
    SourceRecord,
)
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.errors import SimulationError
from alterego.kernel.logging import get_logger
from alterego.llm import PromptLibrary
from alterego.sim.vault import (
    PURPOSE,
    VaultWorkbench,
    build,
    init_vault,
    organize,
    scan,
    sync,
)


TZ = resolve_timezone("Asia/Shanghai")
T0 = datetime(2026, 9, 16, 21, 40, tzinfo=TZ)
PERSONA = PersonaRecord(id="p1", name="阿哲", city="杭州")

DAY = date(2026, 9, 16)
SCHEDULE_DAY = f"{FOLDER_SCHEDULE}/{DAY:%Y-%m-%d}.md"


# ────────────────────────────────────────────────────────────
# 替身
# ────────────────────────────────────────────────────────────


class StubGateway:
    """假网关。

    真的那个要注册处、路由表、计费账本；这里只需要「它回了什么」和
    「它被叫了几次」——而后者正是这个文件里一半断言的主语。
    """

    def __init__(self, reply: str = "", *, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def complete(self, purpose: str, prompt: str, **_: Any) -> LLMResponse:
        self.calls.append((purpose, prompt))
        if self.error is not None:
            raise self.error
        return LLMResponse(text=self.reply, model="stub")


class _StubRepo:
    """只读仓储的最小实现。

    四个仓储被调用到的方法形状几乎一样：收一个 ``since``，回一批范围内的行。
    加一个基类是为了让每个测试只声明自己真正关心的那部分数据。
    """

    def __init__(self, rows: Sequence[Any] = ()) -> None:
        self.rows = list(rows)
        self.persona_ids: list[str] = []

    def _pick(
        self,
        persona_id: str,
        *,
        since: datetime,
        until: datetime | None,
        limit: int,
        key: str,
    ) -> list[Any]:
        self.persona_ids.append(persona_id)
        window = [row for row in self.rows if getattr(row, key) >= since]
        if until is not None:
            window = [row for row in window if getattr(row, key) <= until]
        return window[:limit]


class StubSchedules(_StubRepo):
    def list_range(
        self, persona_id: str, *, since: datetime, until: datetime, limit: int = 100
    ) -> list[Any]:
        return self._pick(persona_id, since=since, until=until, limit=limit, key="start_at")


class StubActivities(_StubRepo):
    def list_range(
        self, persona_id: str, *, since: datetime, until: datetime, limit: int = 100
    ) -> list[Any]:
        return self._pick(persona_id, since=since, until=until, limit=limit, key="started_at")


class StubSources(_StubRepo):
    def list_kept(self, persona_id: str, *, since: datetime, limit: int = 100) -> list[Any]:
        return self._pick(persona_id, since=since, until=None, limit=limit, key="fetched_at")


class StubMemories(_StubRepo):
    def list_recent(self, persona_id: str, *, since: datetime, limit: int = 100) -> list[Any]:
        return self._pick(persona_id, since=since, until=None, limit=limit, key="occurred_at")


@dataclass
class Store:
    """四个仓储加一个网关，凑成一次任务的输入。"""

    schedules: StubSchedules = field(default_factory=StubSchedules)
    activities: StubActivities = field(default_factory=StubActivities)
    sources: StubSources = field(default_factory=StubSources)
    memories: StubMemories = field(default_factory=StubMemories)
    gateway: StubGateway = field(default_factory=StubGateway)


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def work(tmp_path: Path, store: Store) -> VaultWorkbench:
    return VaultWorkbench(
        persona=PERSONA,
        user_name="你",
        root=tmp_path / "阿哲的知识库",
        gateway=store.gateway,  # type: ignore[arg-type]
        prompts=PromptLibrary(),
        schedules=store.schedules,  # type: ignore[arg-type]
        activities=store.activities,  # type: ignore[arg-type]
        sources=store.sources,  # type: ignore[arg-type]
        memories=store.memories,  # type: ignore[arg-type]
        logger=get_logger("test.vault"),
    )


# ────────────────────────────────────────────────────────────
# 造数据
# ────────────────────────────────────────────────────────────


def at(hour: int, minute: int = 0, *, day: int = 16) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ)


def schedule(
    activity: str = "写代码", *, start: datetime | None = None, **rest: Any
) -> ScheduleRecord:
    begin = start or at(20)
    fields: dict[str, Any] = {
        "id": f"s-{activity}",
        "day": begin.date(),
        "start_at": begin,
        "end_at": begin + timedelta(hours=2),
        "activity": activity,
    }
    fields.update(rest)
    return ScheduleRecord(**fields)


def activity(
    description: str = "改 bug", *, start: datetime | None = None, **rest: Any
) -> ActivityRecord:
    begin = start or at(21)
    fields: dict[str, Any] = {
        "id": f"a-{description}",
        "intent": "work",
        "description": description,
        "started_at": begin,
    }
    fields.update(rest)
    return ActivityRecord(**fields)


def source(title: str = "一篇讲睡眠的文章", **rest: Any) -> SourceRecord:
    fields: dict[str, Any] = {
        "id": "src-1",
        "url": "https://example.com/sleep",
        "fetched_at": at(19),
        "title": title,
        "summary": "讲了昼夜节律和咖啡因的半衰期。",
    }
    fields.update(rest)
    return SourceRecord(**fields)


def memory(content: str = "阿哲说过他怕麻烦别人。", **rest: Any) -> Memory:
    fields: dict[str, Any] = {
        "persona_id": PERSONA.id,
        "id": "mem-1",
        "kind": "episodic",
        "content": content,
        "summary": "阿哲怕麻烦别人",
        "occurred_at": at(18),
    }
    fields.update(rest)
    return Memory(**fields)


def snapshot(root: Path) -> dict[str, str]:
    """整个库的「逐字节」快照。幂等就靠它证明。"""
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def reply(*items: str) -> str:
    return "我看了看，这样放吧：\n\n[\n  " + ",\n  ".join(items) + "\n]\n"


# ────────────────────────────────────────────────────────────
# init
# ────────────────────────────────────────────────────────────


class TestInitVault:
    def test_it_creates_every_folder(self, work: VaultWorkbench) -> None:
        """骨架由代码保证——角色管分类，但目录得先在。"""
        init_vault(work, now=T0)
        for folder in FOLDERS:
            assert (work.root / folder).is_dir()

    def test_it_creates_the_root_index(self, work: VaultWorkbench) -> None:
        init_vault(work, now=T0)
        assert (work.root / FOLDER_INDEX / "阿哲的知识库.md").is_file()

    def test_the_root_index_says_who_it_belongs_to(self, work: VaultWorkbench) -> None:
        init_vault(work, now=T0)
        text = (work.root / FOLDER_INDEX / "阿哲的知识库.md").read_text(encoding="utf-8")
        assert "阿哲" in text

    def test_it_writes_the_obsidian_config(self, work: VaultWorkbench) -> None:
        """不写这个，用户打开库还得自己调一遍设置。"""
        init_vault(work, now=T0)
        assert (work.root / ".obsidian" / "app.json").is_file()
        assert (work.root / ".obsidian" / "core-plugins.json").is_file()

    def test_new_notes_land_in_the_inbox(self, work: VaultWorkbench) -> None:
        """用户在 Obsidian 里随手新建的笔记要落到收集箱里。

        让用户和角色共用一个入口，比教用户「请把东西放到 99-收集箱」实际。
        """
        import json

        init_vault(work, now=T0)
        app = json.loads((work.root / ".obsidian" / "app.json").read_text(encoding="utf-8"))
        assert app["newFileLocation"] == "folder"
        assert app["newFileFolderPath"] == FOLDER_INBOX

    def test_daily_notes_is_not_enabled(self, work: VaultWorkbench) -> None:
        """开了它 Obsidian 会在同一个目录里另建一套 ``YYYY-MM-DD.md``，
        和 ``sync`` 生成的日程页互相覆盖。"""
        import json

        init_vault(work, now=T0)
        plugins = json.loads(
            (work.root / ".obsidian" / "core-plugins.json").read_text(encoding="utf-8")
        )
        assert "daily-notes" not in plugins

    def test_it_reports_what_it_created(self, work: VaultWorkbench) -> None:
        """「建了 3 个文件」比「好了」有用——用户刚改过的话能一眼看出来。"""
        assert len(init_vault(work, now=T0)) > 0

    def test_running_it_twice_is_safe(self, work: VaultWorkbench) -> None:
        init_vault(work, now=T0)
        before = snapshot(work.root)
        init_vault(work, now=T0)
        assert snapshot(work.root) == before

    def test_it_does_not_overwrite_hand_edited_settings(self, work: VaultWorkbench) -> None:
        """用户调过字号、换过主题，那都是他的东西。"""
        init_vault(work, now=T0)
        app = work.root / ".obsidian" / "app.json"
        app.write_text('{"baseFontSize": 20}', encoding="utf-8", newline="\n")
        init_vault(work, now=T0)
        assert app.read_text(encoding="utf-8") == '{"baseFontSize": 20}'

    def test_the_empty_vault_passes_its_own_check(self, work: VaultWorkbench) -> None:
        """空库没有坏链——索引页链到的那几个目录索引页，init 之后都该在。"""
        init_vault(work, now=T0)
        issues = validate(scan(work.root, now=T0, logger=work.logger))
        assert [issue for issue in issues if issue.kind == "broken_link"] == []


# ────────────────────────────────────────────────────────────
# scan
# ────────────────────────────────────────────────────────────


class TestScan:
    def test_a_missing_root_is_an_empty_vault(self, work: VaultWorkbench) -> None:
        """``vault status`` 要能在一台还没建库的机器上跑。"""
        assert scan(work.root, now=T0) == ()

    def test_it_skips_dot_directories(self, work: VaultWorkbench) -> None:
        """``.obsidian/`` 里是 Obsidian 自己的东西，不是用户的笔记。"""
        init_vault(work, now=T0)
        assert not [note for note in scan(work.root, now=T0) if note.path.startswith(".")]

    def test_it_reads_a_hand_written_note(self, work: VaultWorkbench) -> None:
        (work.root / FOLDER_THOUGHTS).mkdir(parents=True)
        (work.root / FOLDER_THOUGHTS / "手写的.md").write_text(
            "# 手写的\n\n没有 frontmatter。", encoding="utf-8", newline="\n"
        )
        notes = scan(work.root, now=T0)
        assert [note.path for note in notes] == [f"{FOLDER_THOUGHTS}/手写的.md"]

    def test_a_note_without_frontmatter_still_gets_a_title(self, work: VaultWorkbench) -> None:
        """用户在 Obsidian 里新建的笔记没有 frontmatter，那也是合法的笔记。"""
        (work.root / FOLDER_THOUGHTS).mkdir(parents=True)
        (work.root / FOLDER_THOUGHTS / "手写的.md").write_text(
            "# 今天我有点累\n\n不想说话。", encoding="utf-8", newline="\n"
        )
        assert scan(work.root, now=T0)[0].title == "今天我有点累"

    def test_it_skips_a_file_it_cannot_read(self, work: VaultWorkbench) -> None:
        """GBK 文件、权限不对的空目录占位——它们不该让 status 整个跑不起来，
        那正是最需要看到输出的时候。"""
        (work.root / FOLDER_THOUGHTS).mkdir(parents=True)
        (work.root / FOLDER_THOUGHTS / "坏掉的.md").write_bytes(b"\xff\xfe\x00\x00\xff")
        assert scan(work.root, now=T0, logger=work.logger) == ()

    def test_one_bad_file_does_not_hide_the_good_ones(self, work: VaultWorkbench) -> None:
        (work.root / FOLDER_THOUGHTS).mkdir(parents=True)
        (work.root / FOLDER_THOUGHTS / "好.md").write_text("# 好", encoding="utf-8", newline="\n")
        (work.root / FOLDER_THOUGHTS / "坏.md").write_bytes(b"\xff\xfe\x00\x00\xff")
        assert [note.stem for note in scan(work.root, now=T0, logger=work.logger)] == ["好"]


# ────────────────────────────────────────────────────────────
# sync
# ────────────────────────────────────────────────────────────


class TestSync:
    def test_a_planned_day_gets_a_page(self, work: VaultWorkbench, store: Store) -> None:
        store.schedules.rows.append(schedule("写代码"))
        report = sync(work, now=T0)
        assert (work.root / SCHEDULE_DAY).is_file()
        assert report.days == 1

    def test_the_page_lives_in_the_schedule_folder_not_the_inbox(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        """日程的归属是确定的，让它去问模型「这算不算日程」纯属浪费。"""
        store.schedules.rows.append(schedule("写代码"))
        sync(work, now=T0)
        assert not list((work.root / FOLDER_INBOX).glob("*.md"))

    def test_the_page_shows_the_plan_and_what_actually_happened(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        """两者并排才有意义：「计划 20:00 读书，实际 21:30 才坐下」。"""
        store.schedules.rows.append(schedule("读书"))
        store.activities.rows.append(activity("刷了半小时手机", start=at(21, 30)))
        sync(work, now=T0)
        text = (work.root / SCHEDULE_DAY).read_text(encoding="utf-8")
        assert "读书" in text
        assert "刷了半小时手机" in text

    def test_it_carries_the_excuse_for_a_deviation(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        store.schedules.rows.append(schedule("读书", deviation_note="被一个电话叫走了"))
        sync(work, now=T0)
        assert "被一个电话叫走了" in (work.root / SCHEDULE_DAY).read_text(encoding="utf-8")

    def test_a_missing_location_never_leaves_an_empty_cell(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        """空单元格在 Obsidian 的表格里会塌成一格，整张表就错位了。"""
        store.schedules.rows.append(schedule("写代码"))
        sync(work, now=T0)
        text = (work.root / SCHEDULE_DAY).read_text(encoding="utf-8")
        assert "|  |" not in text
        assert "—" in text

    def test_an_inner_voice_gets_its_own_page(self, work: VaultWorkbench, store: Store) -> None:
        """独白比别的东西更值得单独放：它是「想说但没说」的那部分。"""
        store.activities.rows.append(activity("想找阿哲说话", inner_voice="算了，他可能在忙"))
        report = sync(work, now=T0)
        assert report.thoughts == 1
        assert len(list((work.root / FOLDER_INBOX).glob("*.md"))) == 1

    def test_the_day_page_quotes_the_inner_voice(self, work: VaultWorkbench, store: Store) -> None:
        """日程页写的是那句话本身，**不是一条链接**。

        独白单独成篇进了收集箱，而收集箱里的笔记会被 ``organize`` 改名。
        日程页引用的名字由另一步决定，放链接等于每整理一次就在日程里留
        几条坏链。
        """
        store.activities.rows.append(activity("想找阿哲说话", inner_voice="算了，他可能在忙"))
        sync(work, now=T0)
        text = (work.root / SCHEDULE_DAY).read_text(encoding="utf-8")
        assert "算了，他可能在忙" in text
        assert "[[" not in text

    def test_a_collected_source_goes_to_the_inbox(self, work: VaultWorkbench, store: Store) -> None:
        store.sources.rows.append(source())
        report = sync(work, now=T0)
        assert report.sources == 1
        assert len(list((work.root / FOLDER_INBOX).glob("*.md"))) == 1

    def test_a_source_keeps_its_url(self, work: VaultWorkbench, store: Store) -> None:
        """链接丢了这篇笔记就没用了。"""
        store.sources.rows.append(source())
        sync(work, now=T0)
        text = next((work.root / FOLDER_INBOX).glob("*.md")).read_text(encoding="utf-8")
        assert "https://example.com/sleep" in text

    def test_a_memory_goes_to_the_inbox(self, work: VaultWorkbench, store: Store) -> None:
        store.memories.rows.append(memory())
        report = sync(work, now=T0)
        assert report.memories == 1

    def test_a_memory_carries_its_kind_as_a_tag(self, work: VaultWorkbench, store: Store) -> None:
        """语义记忆和情绪记忆在 Obsidian 里该能筛出来。"""
        store.memories.rows.append(memory(kind="emotional"))
        sync(work, now=T0)
        text = next((work.root / FOLDER_INBOX).glob("*.md")).read_text(encoding="utf-8")
        assert "emotional" in text

    def test_an_empty_database_writes_nothing(self, work: VaultWorkbench) -> None:
        """一台刚 init 完的机器上跑 sync，不该报错也不该造空文件。"""
        report = sync(work, now=T0)
        assert report.days == 0
        assert report.written == 0

    def test_an_empty_day_still_passes_the_check(self, work: VaultWorkbench, store: Store) -> None:
        init_vault(work, now=T0)
        store.schedules.rows.append(schedule("写代码"))
        sync(work, now=T0)
        assert build(work, now=T0).issues == ()

    def test_it_asks_for_the_lookback_window(self, work: VaultWorkbench, store: Store) -> None:
        """读的是窗口内的事，不是全库的事。"""
        store.schedules.rows.append(schedule("很久以前", start=at(20, day=1)))
        store.schedules.rows.append(schedule("就在今天"))
        assert sync(work, now=T0, lookback_days=3).days == 1

    def test_only_the_window_is_read(self, work: VaultWorkbench, store: Store) -> None:
        """``--lookback-days`` 说了算，库里的老数据不该被翻出来。"""
        store.schedules.rows.append(schedule("很久以前", start=at(20, day=1)))
        assert sync(work, now=T0, lookback_days=3).days == 0

    def test_the_old_data_comes_back_with_a_bigger_window(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        store.schedules.rows.append(schedule("很久以前", start=at(20, day=1)))
        assert sync(work, now=T0, lookback_days=30).days == 1

    def test_the_persona_id_is_passed_through(self, work: VaultWorkbench, store: Store) -> None:
        """读的是**这个人**的事，不是全库的事。"""
        sync(work, now=T0)
        assert store.schedules.persona_ids == [PERSONA.id]

    def test_running_it_twice_changes_nothing(self, work: VaultWorkbench, store: Store) -> None:
        """幂等靠「文件名由数据决定」：同一条数据算出来的路径永远一样。"""
        init_vault(work, now=T0)
        store.schedules.rows.append(schedule("写代码"))
        store.activities.rows.append(activity("改 bug", inner_voice="其实我想休息"))
        store.sources.rows.append(source())

        sync(work, now=T0)
        first = snapshot(work.root)
        sync(work, now=T0)
        assert snapshot(work.root) == first

    def test_two_sources_with_the_same_title_do_not_collide(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        """只按标题命名的话，两条同名的链接会互相覆盖，用户不会知道少了一条。"""
        store.sources.rows.append(source("同一篇", id="aaa11111"))
        store.sources.rows.append(source("同一篇", id="bbb22222"))
        report = sync(work, now=T0)
        assert report.sources == 2
        assert len(list((work.root / FOLDER_INBOX).glob("*.md"))) == 2

    def test_the_pages_use_lf_line_endings(self, work: VaultWorkbench, store: Store) -> None:
        """仓库的 ``core.autocrlf=false``——写进 CRLF 的话每篇笔记在 git 里
        都成了「整个文件都变了」。"""
        store.schedules.rows.append(schedule("写代码"))
        sync(work, now=T0)
        assert b"\r\n" not in (work.root / SCHEDULE_DAY).read_bytes()

    def test_sync_never_calls_the_model(self, work: VaultWorkbench, store: Store) -> None:
        """这一步的意义就是「哪怕配额用光，它照样在长」。"""
        store.schedules.rows.append(schedule("写代码"))
        store.sources.rows.append(source())
        sync(work, now=T0)
        assert store.gateway.calls == []


# ────────────────────────────────────────────────────────────
# build
# ────────────────────────────────────────────────────────────


class TestBuild:
    def test_it_writes_the_root_index(self, work: VaultWorkbench, store: Store) -> None:
        store.schedules.rows.append(schedule("写代码"))
        sync(work, now=T0)
        build(work, now=T0)
        assert (work.root / FOLDER_INDEX / "阿哲的知识库.md").is_file(), sorted(
            path.relative_to(work.root).as_posix()
            for path in work.root.rglob("*")
            if path.is_file()
        )

    def test_it_writes_one_index_per_content_folder(self, work: VaultWorkbench) -> None:
        build(work, now=T0)
        assert (work.root / FOLDER_INDEX / "日程.md").is_file()
        assert (work.root / FOLDER_INDEX / "想法.md").is_file()

    def test_it_does_not_write_an_index_for_the_inbox(self, work: VaultWorkbench) -> None:
        """收集箱里的东西还没归位，进哪门子索引。"""
        build(work, now=T0)
        assert not (work.root / FOLDER_INDEX / "待整理.md").is_file()

    def test_it_lists_a_real_note(self, work: VaultWorkbench, store: Store) -> None:
        store.schedules.rows.append(schedule("写代码"))
        sync(work, now=T0)
        build(work, now=T0)
        text = (work.root / FOLDER_INDEX / "日程.md").read_text(encoding="utf-8")
        assert "[[2026-09-16]]" in text

    def test_it_rebuilds_rather_than_appends(self, work: VaultWorkbench, store: Store) -> None:
        """索引是**派生**的。手删一篇笔记，索引里那条也该跟着消失。"""
        store.schedules.rows.append(schedule("写代码"))
        sync(work, now=T0)
        build(work, now=T0)
        (work.root / SCHEDULE_DAY).unlink()
        build(work, now=T0)
        text = (work.root / FOLDER_INDEX / "日程.md").read_text(encoding="utf-8")
        assert "[[2026-09-16]]" not in text

    def test_it_rebuilds_the_count_too(self, work: VaultWorkbench, store: Store) -> None:
        store.schedules.rows.append(schedule("写代码"))
        sync(work, now=T0)
        build(work, now=T0)
        (work.root / SCHEDULE_DAY).unlink()
        build(work, now=T0)
        assert build(work, now=T0).scanned == 0

    def test_it_picks_up_a_hand_written_note(self, work: VaultWorkbench) -> None:
        """用户在 Obsidian 里新写一篇，下一次 build 就该出现在索引里。"""
        init_vault(work, now=T0)
        (work.root / FOLDER_THOUGHTS / "有点累.md").write_text(
            "# 有点累\n\n不想说话。", encoding="utf-8", newline="\n"
        )
        build(work, now=T0)
        text = (work.root / FOLDER_INDEX / "想法.md").read_text(encoding="utf-8")
        assert "[[有点累]]" in text

    def test_the_count_excludes_index_pages(self, work: VaultWorkbench, store: Store) -> None:
        """「共 1 篇」里不该算上索引页自己。"""
        store.schedules.rows.append(schedule("写代码"))
        sync(work, now=T0)
        report = build(work, now=T0)
        assert report.scanned == 1

    def test_a_clean_vault_reports_no_issues(self, work: VaultWorkbench, store: Store) -> None:
        store.schedules.rows.append(schedule("写代码"))
        sync(work, now=T0)
        assert build(work, now=T0).issues == ()

    def test_it_reports_issues_instead_of_refusing(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        """角色刚起的链接名可能指向一篇还没写的笔记，那不算错。"""
        init_vault(work, now=T0)
        (work.root / FOLDER_THOUGHTS / "想到一个新名字.md").write_text(
            "# 想到一个新名字\n\n这个叫 [[还没写的东西]]。", encoding="utf-8", newline="\n"
        )
        report = build(work, now=T0)
        assert "broken_link" in [issue.kind for issue in report.issues]

    def test_the_inbox_does_not_count_as_an_orphan(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        """还没归位的东西当然不在索引里。"""
        store.sources.rows.append(source())
        sync(work, now=T0)
        assert "orphan" not in [issue.kind for issue in build(work, now=T0).issues]

    def test_build_never_calls_the_model(self, work: VaultWorkbench, store: Store) -> None:
        """它写不出「共 87 篇」这种数。"""
        build(work, now=T0)
        assert store.gateway.calls == []

    def test_running_it_twice_changes_nothing(self, work: VaultWorkbench, store: Store) -> None:
        store.schedules.rows.append(schedule("写代码"))
        sync(work, now=T0)
        build(work, now=T0)
        first = snapshot(work.root)
        build(work, now=T0)
        assert snapshot(work.root) == first


# ────────────────────────────────────────────────────────────
# organize
# ────────────────────────────────────────────────────────────


class TestOrganize:
    async def test_an_empty_inbox_does_not_call_the_model(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        """没什么可整理的还去问一次，就是白花钱。"""
        report = await organize(work, now=T0)
        assert store.gateway.calls == []
        assert report.skipped is not None

    async def test_it_files_a_note(self, work: VaultWorkbench, store: Store) -> None:
        store.sources.rows.append(source("一篇讲睡眠的文章", id="aaa11111"))
        sync(work, now=T0)
        original = next((work.root / FOLDER_INBOX).glob("*.md"))
        store.gateway.reply = reply(
            f'{{"inbox": "{original.stem}", "folder": "30-读到的", "title": "昼夜节律"}}'
        )

        report = await organize(work, now=T0)

        assert report.filed == 1
        assert report.read == 1

    async def test_the_note_leaves_the_inbox(self, work: VaultWorkbench, store: Store) -> None:
        store.sources.rows.append(source("一篇讲睡眠的文章", id="aaa11111"))
        sync(work, now=T0)
        original = next((work.root / FOLDER_INBOX).glob("*.md"))
        store.gateway.reply = reply(
            f'{{"inbox": "{original.stem}", "folder": "30-读到的", "title": "昼夜节律"}}'
        )

        await organize(work, now=T0)

        assert not list((work.root / FOLDER_INBOX).glob("*.md"))
        assert (work.root / "30-读到的" / "昼夜节律.md").is_file()

    async def test_the_original_body_survives(self, work: VaultWorkbench, store: Store) -> None:
        """模型只负责「放哪、叫什么」，正文是它自己原来写下的。"""
        store.sources.rows.append(source("一篇讲睡眠的文章", id="aaa11111"))
        sync(work, now=T0)
        original = next((work.root / FOLDER_INBOX).glob("*.md"))
        store.gateway.reply = reply(
            f'{{"inbox": "{original.stem}", "folder": "30-读到的", "title": "昼夜节律"}}'
        )

        await organize(work, now=T0)

        text = (work.root / "30-读到的" / "昼夜节律.md").read_text(encoding="utf-8")
        assert "https://example.com/sleep" in text

    async def test_the_tags_are_written(self, work: VaultWorkbench, store: Store) -> None:
        store.sources.rows.append(source("一篇讲睡眠的文章", id="aaa11111"))
        sync(work, now=T0)
        original = next((work.root / FOLDER_INBOX).glob("*.md"))
        store.gateway.reply = reply(
            f'{{"inbox": "{original.stem}", "folder": "30-读到的", '
            f'"title": "昼夜节律", "tags": ["#睡眠"]}}'
        )

        await organize(work, now=T0)

        text = (work.root / "30-读到的" / "昼夜节律.md").read_text(encoding="utf-8")
        assert "睡眠" in text
        assert "#睡眠" not in text

    async def test_it_links_to_an_existing_note(self, work: VaultWorkbench, store: Store) -> None:
        init_vault(work, now=T0)
        (work.root / FOLDER_THOUGHTS / "怕麻烦别人.md").write_text(
            "# 怕麻烦别人\n\n不敢开口。", encoding="utf-8", newline="\n"
        )
        store.activities.rows.append(activity("想找阿哲说话", inner_voice="算了，他可能在忙"))
        sync(work, now=T0)
        original = next((work.root / FOLDER_INBOX).glob("*.md"))
        store.gateway.reply = reply(
            f'{{"inbox": "{original.stem}", "folder": "20-想法", '
            f'"title": "其实想找他", "links": ["怕麻烦别人.md"]}}'
        )

        await organize(work, now=T0)

        text = (work.root / FOLDER_THOUGHTS / "其实想找他.md").read_text(encoding="utf-8")
        assert "[[怕麻烦别人]]" in text

    async def test_the_index_is_rebuilt_afterwards(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        """新归位的笔记要进索引——否则用户根本找不到它。"""
        store.sources.rows.append(source("一篇讲睡眠的文章", id="aaa11111"))
        sync(work, now=T0)
        original = next((work.root / FOLDER_INBOX).glob("*.md"))
        store.gateway.reply = reply(
            f'{{"inbox": "{original.stem}", "folder": "30-读到的", "title": "昼夜节律"}}'
        )

        report = await organize(work, now=T0)

        assert (work.root / FOLDER_INDEX / "读到的.md").is_file()
        assert report.issues == ()

    async def test_it_records_that_it_ran(self, work: VaultWorkbench, store: Store) -> None:
        """进度只放在库里的一个文件里，不新增数据库表。"""
        store.sources.rows.append(source(id="aaa11111"))
        sync(work, now=T0)
        original = next((work.root / FOLDER_INBOX).glob("*.md"))
        store.gateway.reply = reply(
            f'{{"inbox": "{original.stem}", "folder": "30-读到的", "title": "昼夜节律"}}'
        )

        await organize(work, now=T0)

        assert (work.root / FOLDER_INDEX / STATE_FILENAME).is_file()

    async def test_a_broken_reply_leaves_everything_alone(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        """最坏的结果是「没整理」，绝不能是「东西没了」。"""
        store.sources.rows.append(source(id="aaa11111"))
        sync(work, now=T0)
        before = snapshot(work.root)
        store.gateway.reply = "我觉得都挺好的，就这样吧。"

        report = await organize(work, now=T0)

        assert report.filed == 0
        assert report.rejected[0][0] == "整批"
        assert snapshot(work.root) == before

    async def test_one_bad_decision_does_not_stop_the_others(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        """一条笔记标签起得不好，不值得让整次整理停下。"""
        store.sources.rows.append(source("第一篇", id="aaa11111"))
        store.sources.rows.append(source("第二篇", id="bbb22222"))
        sync(work, now=T0)
        stems = sorted(path.stem for path in (work.root / FOLDER_INBOX).glob("*.md"))
        store.gateway.reply = reply(
            f'{{"inbox": "{stems[0]}", "folder": "档案", "title": "放错了地方"}}',
            f'{{"inbox": "{stems[1]}", "folder": "30-读到的", "title": "放对了"}}',
        )

        report = await organize(work, now=T0)

        assert report.filed == 1
        assert len(report.rejected) == 1
        assert (work.root / "30-读到的" / "放对了.md").is_file()

    async def test_a_decision_naming_a_file_that_is_not_there_is_skipped(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        store.sources.rows.append(source(id="aaa11111"))
        sync(work, now=T0)
        store.gateway.reply = reply(
            '{"inbox": "根本没有这一篇", "folder": "30-读到的", "title": "x"}'
        )

        report = await organize(work, now=T0)

        assert report.filed == 0
        assert len(list((work.root / FOLDER_INBOX).glob("*.md"))) == 1

    async def test_a_rename_does_not_overwrite_an_existing_note(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        """覆盖会**悄悄毁掉**另一篇笔记，而用户在 Obsidian 里看不见这件事。"""
        init_vault(work, now=T0)
        (work.root / "30-读到的").mkdir(parents=True, exist_ok=True)
        (work.root / "30-读到的" / "昼夜节律.md").write_text(
            "# 昼夜节律\n\n早先写的那篇。", encoding="utf-8", newline="\n"
        )
        store.sources.rows.append(source(id="aaa11111"))
        sync(work, now=T0)
        original = next((work.root / FOLDER_INBOX).glob("*.md"))
        store.gateway.reply = reply(
            f'{{"inbox": "{original.stem}", "folder": "30-读到的", "title": "昼夜节律"}}'
        )

        await organize(work, now=T0)

        kept = (work.root / "30-读到的" / "昼夜节律.md").read_text(encoding="utf-8")
        assert "早先写的那篇" in kept
        assert (work.root / "30-读到的" / "昼夜节律-2.md").is_file()

    async def test_dry_run_does_not_call_the_model(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        """花不花钱应该先看得见。"""
        store.sources.rows.append(source(id="aaa11111"))
        sync(work, now=T0)

        report = await organize(work, now=T0, dry_run=True)

        assert store.gateway.calls == []
        assert report.preview
        assert report.filed == 0

    async def test_dry_run_writes_nothing(self, work: VaultWorkbench, store: Store) -> None:
        store.sources.rows.append(source(id="aaa11111"))
        sync(work, now=T0)
        before = snapshot(work.root)

        await organize(work, now=T0, dry_run=True)

        assert snapshot(work.root) == before

    async def test_the_prompt_goes_through_the_vault_purpose(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        """``[llm.routing] vault`` 决定这一步花谁的钱。"""
        store.sources.rows.append(source(id="aaa11111"))
        sync(work, now=T0)
        await organize(work, now=T0)
        assert store.gateway.calls[0][0] == PURPOSE

    async def test_the_prompt_contains_the_inbox_and_the_catalog(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        init_vault(work, now=T0)
        (work.root / FOLDER_THOUGHTS / "怕麻烦别人.md").write_text(
            "# 怕麻烦别人\n\n不敢开口。", encoding="utf-8", newline="\n"
        )
        store.sources.rows.append(source("一篇讲睡眠的文章", id="aaa11111"))
        sync(work, now=T0)

        await organize(work, now=T0)

        prompt = store.gateway.calls[0][1]
        assert "阿哲" in prompt
        assert "一篇讲睡眠的文章" in prompt
        assert "怕麻烦别人" in prompt

    async def test_it_does_not_render_a_placeholder_that_was_never_filled(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        """模板里多一个 ``{user_name}`` 而渲染方没传，会在**生产的第一次**
        调用上才炸。这里把它钉死在测试里。"""
        store.sources.rows.append(source(id="aaa11111"))
        sync(work, now=T0)

        await organize(work, now=T0)

        prompt = store.gateway.calls[0][1]
        assert not re.search(r"\{[a-z_]+\}", prompt)

    async def test_a_failed_call_raises_instead_of_losing_notes(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        store.sources.rows.append(source(id="aaa11111"))
        sync(work, now=T0)
        before = snapshot(work.root)
        store.gateway.error = RuntimeError("连接超时")

        with pytest.raises(SimulationError) as caught:
            await organize(work, now=T0)

        assert caught.value.context.get("purpose") == PURPOSE
        assert snapshot(work.root) == before

    async def test_running_it_twice_with_nothing_left_is_a_no_op(
        self, work: VaultWorkbench, store: Store
    ) -> None:
        store.sources.rows.append(source(id="aaa11111"))
        sync(work, now=T0)
        original = next((work.root / FOLDER_INBOX).glob("*.md"))
        store.gateway.reply = reply(
            f'{{"inbox": "{original.stem}", "folder": "30-读到的", "title": "昼夜节律"}}'
        )
        await organize(work, now=T0)
        calls_after_first = len(store.gateway.calls)

        second = await organize(work, now=T0)

        assert len(store.gateway.calls) == calls_after_first
        assert second.skipped is not None

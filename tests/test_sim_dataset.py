"""``alterego.sim.dataset`` 的测试。

这一层是**编排**，所以测的不是「一行 JSON 长什么样」（那是
``test_domain_dataset.py`` 的事），而是**磁盘上最后剩下的那个目录**：

1. **目录只有一个正确状态。** 跑完之后，目录内容必须**恰好**等于这次配置
   要求的那几份——多一份旧形状，谁按 ``*.jsonl`` 去微调就会把同一段内容
   学两遍，而说明页只描述了其中一种（见 ADR-0011）。
2. **空的一类不写文件。** 写出一堆 0 字节的 ``.jsonl`` 看起来像成功，
   训练框架读到只会报一个跟真正原因无关的错。
3. **``dry_run`` 一个字节都不落。** 预演的唯一价值就是「敢直接按」。
4. **``scan`` 不重新扫数据。** 它只读 ``manifest.json``：用户想知道的是
   「上次跑出来的是什么」，重新扫会算出「现在跑会得到什么」，那是
   ``preview`` 的活。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from alterego.domain.dataset import ActivityRow, MessageRow, TickRow
from alterego.interfaces.repository import PersonaRecord
from alterego.kernel.clock import resolve_timezone
from alterego.sim.dataset import (
    MANIFEST_NAME,
    README_NAME,
    DatasetWorkbench,
    build,
    file_name,
    planned_paths,
    preview,
    scan,
)


TZ = resolve_timezone("Asia/Shanghai")
NOW = datetime(2026, 9, 16, 8, 0, tzinfo=TZ)
LOGGER = logging.getLogger("test.dataset")

PERSONA = PersonaRecord(id="p1", name="林晚", occupation="算法工程师")


# ── 假的源仓储 ──────────────────────────────────────────────


class FakeSources:
    """内存里的只读源，行为对齐 ``SqliteDatasetSourceRepository``。

    刻意**不**重新排序、**不**重新筛时间：真实实现已经保证了这两件事，
    编排层要是偷偷补一遍，那边写错了就永远测不出来。所以这里存什么就返什么，
    顺带记下收到的参数，供「有没有真的按 since 取数」这类断言用。
    """

    def __init__(
        self,
        *,
        messages: Sequence[MessageRow] = (),
        ticks: Sequence[TickRow] = (),
        activities: Sequence[ActivityRow] = (),
        intents: dict[str, str] | None = None,
    ) -> None:
        self.messages = list(messages)
        self.ticks = list(ticks)
        self.activities = list(activities)
        self.intents = dict(intents or {})
        self.calls: list[str] = []
        self.since: datetime | None = None

    def list_conversation_messages(
        self, persona_id: str, *, since: datetime, limit: int = 20000
    ) -> list[MessageRow]:
        self.calls.append("messages")
        self.since = since
        return list(self.messages)

    def list_ticks(self, persona_id: str, *, since: datetime, limit: int = 5000) -> list[TickRow]:
        self.calls.append("ticks")
        self.since = since
        return list(self.ticks)

    def list_activities(
        self, persona_id: str, *, since: datetime, limit: int = 20000
    ) -> list[ActivityRow]:
        self.calls.append("activities")
        self.since = since
        return list(self.activities)

    def map_tick_intents(self, persona_id: str, *, since: datetime) -> dict[str, str]:
        self.calls.append("intents")
        return dict(self.intents)


def _work(root: Path, sources: FakeSources, **overrides: object) -> DatasetWorkbench:
    fields: dict[str, object] = {
        "persona": PERSONA,
        "user_name": "阿泽",
        "root": root,
        "sources": sources,
        "logger": LOGGER,
    }
    fields.update(overrides)
    return DatasetWorkbench(**fields)  # type: ignore[arg-type]


def _messages() -> list[MessageRow]:
    """一轮真对话，中间夹一个手机号——脱敏有东西可做。"""
    return [
        MessageRow(
            id="m1",
            conversation_id="c1",
            direction="inbound",
            content="在吗，我手机 13800138000 打不通了",
            content_type="text",
            created_at="2026-09-15T09:00:00+08:00",
        ),
        MessageRow(
            id="m2",
            conversation_id="c1",
            direction="outbound",
            content="我看看",
            content_type="text",
            created_at="2026-09-15T09:01:00+08:00",
        ),
    ]


def _tick() -> TickRow:
    """一条成功的 tick，字段够拼出一条推理样本。"""
    return TickRow(
        id="t1",
        virtual_time="2026-09-15T09:01:00+08:00",
        status="ok",
        percepts_json='["她说手机打不通"]',
        candidates_json='["回一句","先不管"]',
        chosen_intent="social/reply",
        motivation="她问我了",
        memories_json='["她桌上有个旧键盘"]',
    )


def _activity() -> ActivityRow:
    return ActivityRow(
        id="a1",
        intent="social/reply",
        category="social",
        description="回了她一句话",
        detail_json='{"chars": 12}',
        location="家",
        inner_voice="想让她安心",
        duration_minutes=1,
        tick_id="t1",
        started_at="2026-09-15T09:01:00+08:00",
    )


def _full(root: Path, **overrides: object) -> DatasetWorkbench:
    """三类源都有数据的工作台。"""
    sources = FakeSources(
        messages=_messages(),
        ticks=[_tick()],
        activities=[_activity()],
        intents={"t1": "先回她一句，问问是哪个号码"},
    )
    return _work(root, sources, **overrides)


def _jsonl(root: Path) -> set[str]:
    return {path.name for path in root.glob("*.jsonl")}


# ── 落盘 ────────────────────────────────────────────────────
def test_build_writes_one_file_per_dataset_plus_the_two_pages(tmp_path: Path) -> None:
    """三类都非空时，落三个数据集文件，再加说明页与记录。"""
    work = _full(tmp_path)
    report = build(work, now=NOW, days=30)

    assert len(report.files) == 3
    assert _jsonl(tmp_path) == {
        file_name("conversation", "chat"),
        file_name("reasoning", "chat"),
        file_name("tooluse", "chat"),
    }
    assert (tmp_path / MANIFEST_NAME).is_file()
    assert (tmp_path / README_NAME).is_file()


def test_build_is_reproducible_byte_for_byte(tmp_path: Path) -> None:
    """同一批输入跑两次，每个文件逐字节相同（P6）。

    时间戳之外的任何差异都会让 ``manifest.json`` 里的 sha256 变成废话，
    而那串 sha256 是「这份数据集还是不是上次那一份」唯一的依据。
    """
    first = _full(tmp_path / "a")
    build(first, now=NOW, days=30)
    digests = {path.name: path.read_bytes() for path in (tmp_path / "a").glob("*.jsonl")}

    second = _full(tmp_path / "b")
    build(second, now=NOW, days=30)
    assert {path.name: path.read_bytes() for path in (tmp_path / "b").glob("*.jsonl")} == digests


def test_build_writes_nothing_for_an_empty_dataset(tmp_path: Path) -> None:
    """空的那一类只出现在记录里，不落一个 0 字节的文件。"""
    sources = FakeSources(messages=_messages())
    report = build(_work(tmp_path, sources), now=NOW, days=30)

    assert {item.kind for item in report.files} == {"conversation"}
    assert _jsonl(tmp_path) == {file_name("conversation", "chat")}
    assert {kind for kind, _ in report.skipped} == {"reasoning", "tooluse"}


def test_build_reports_a_reason_for_every_skipped_dataset(tmp_path: Path) -> None:
    """每一类被跳过的原因都要说清，不能只留一个空字。"""
    report = build(_work(tmp_path, FakeSources()), now=NOW, days=30)
    assert len(report.skipped) == 3
    assert all(reason.strip() for _, reason in report.skipped)


def test_build_skips_a_dataset_whose_rows_produced_no_samples(tmp_path: Path) -> None:
    """取到行但拼不出样本，和一行都没取到，是两种不同的空。"""
    # 一条半成品 tick：状态成功，但没写下任何可用的字段。
    thin = TickRow(id="t1", virtual_time="2026-09-15T09:01:00+08:00", status="ok")
    sources = FakeSources(ticks=[thin])
    report = build(_work(tmp_path, sources), now=NOW, days=30)

    reason = dict(report.skipped)["reasoning"]
    assert "取到 1 行" in reason


def test_build_passes_the_lookback_window_down_to_the_sources(tmp_path: Path) -> None:
    """``days`` 要变成 ``now - days``，不能只在文案里提一句。"""
    sources = FakeSources()
    build(_work(tmp_path, sources), now=NOW, days=7)
    assert sources.since == NOW - timedelta(days=7)


# ── multi-format ────────────────────────────────────────────
def test_build_in_multiple_formats_writes_one_manifest_listing_both(tmp_path: Path) -> None:
    """两种形状同时产出时，说明页只有一份，且必须把两种都记上。

    只记最后一个，另一份文件就在文档里凭空消失了——文件在，但没人知道
    它是什么形状、能不能直接喂给训练框架。
    """
    report = build(_full(tmp_path), fmts=("chat", "sharegpt"), now=NOW, days=30)

    assert len(report.files) == 6
    assert len(_jsonl(tmp_path)) == 6
    assert report.fmts == ("chat", "sharegpt")

    manifest = json.loads((tmp_path / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["formats"] == ["chat", "sharegpt"]
    assert len(manifest["files"]) == 6

    readme = (tmp_path / README_NAME).read_text(encoding="utf-8")
    assert "chat" in readme
    assert "sharegpt" in readme


def test_build_sweeps_files_from_a_previous_run_in_another_format(tmp_path: Path) -> None:
    """换形状之后旧文件必须消失。

    不清的后果不是「多一点冗余」：两种形状并存时按 ``*.jsonl`` 整体读，
    同一段对话会被学两遍，而说明页只说了其中一种。
    """
    build(_full(tmp_path), now=NOW, days=30)
    assert file_name("conversation", "chat") in _jsonl(tmp_path)

    report = build(_full(tmp_path), fmts=("sharegpt",), now=NOW, days=30)

    assert _jsonl(tmp_path) == {
        file_name("conversation", "sharegpt"),
        file_name("reasoning", "sharegpt"),
        file_name("tooluse", "sharegpt"),
    }
    assert set(report.removed) == {
        file_name("conversation", "chat"),
        file_name("reasoning", "chat"),
        file_name("tooluse", "chat"),
    }


def test_build_keeps_unrelated_files_you_put_there(tmp_path: Path) -> None:
    """只清 ``.jsonl``。你自己丢进目录里的笔记不该被删。"""
    note = tmp_path / "我的笔记.md"
    note.write_text("别删我", encoding="utf-8")
    keep = tmp_path / "convert.py"
    keep.write_text("# 我的转换脚本\n", encoding="utf-8")

    build(_full(tmp_path), now=NOW, days=30)
    build(_full(tmp_path), fmts=("sharegpt",), now=NOW, days=30)

    assert note.read_text(encoding="utf-8") == "别删我"
    assert keep.is_file()


def test_build_does_not_sweep_when_it_wrote_nothing(tmp_path: Path) -> None:
    """这次一个文件都没写出来时，不许清目录。

    否则 ``build --days 1`` 落在空区间上跑一次，就会把一份本来好好的
    30 天数据集整个抹掉——一敲就中的陷阱，而且没人会觉得是自己干的。
    """
    build(_full(tmp_path), now=NOW, days=30)
    before = _jsonl(tmp_path)
    assert before

    report = build(_work(tmp_path, FakeSources()), now=NOW, days=1)

    assert report.removed == ()
    assert _jsonl(tmp_path) == before


def test_build_reports_files_it_could_not_sweep(tmp_path: Path, caplog, monkeypatch) -> None:
    """删不掉的旧文件要在日志里留痕，而不是静默留着。

    Windows 上文件可能正被别的程序占着。宁可在日志里喊一声，
    也不要让整次导出因为一个旧文件而失败。
    """
    build(_full(tmp_path), now=NOW, days=30)
    stuck = tmp_path / file_name("conversation", "chat")
    real_unlink = Path.unlink

    def refuse(self: Path, missing_ok: bool = False) -> None:
        if self == stuck:
            raise PermissionError(13, "文件正被占用")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse)
    caplog.set_level(logging.WARNING)
    report = build(_full(tmp_path), fmts=("sharegpt",), now=NOW, days=30)

    assert file_name("conversation", "chat") not in report.removed
    assert report.removed  # 其余两个还是清掉了
    assert any("删不掉" in record.getMessage() for record in caplog.records)
    assert stuck.is_file()  # 没删掉就真的还在，不是记录里少写了一个


# ── dry run ─────────────────────────────────────────────────
def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    """预演不落盘。这是 ``--dry-run`` 唯一的价值。"""
    report = build(_full(tmp_path), now=NOW, days=30, dry_run=True)

    assert report.dry_run is True
    assert not tmp_path.exists() or not any(tmp_path.iterdir())


def test_dry_run_still_reports_real_sizes_and_hashes(tmp_path: Path) -> None:
    """预演报的条数、字节、sha256 必须是**真**跑一遍会得到的那些。"""
    dry = build(_full(tmp_path / "dry"), now=NOW, days=30, dry_run=True)
    real = build(_full(tmp_path / "real"), now=NOW, days=30)

    assert [(item.samples, item.byte_count) for item in dry.files] == [
        (item.samples, item.byte_count) for item in real.files
    ]
    assert [item.sha256 for item in dry.files] == [item.sha256 for item in real.files]


def test_dry_run_says_what_it_would_delete(tmp_path: Path) -> None:
    """预演也要报「会删什么」：清理不可逆，不该只有真跑才看得见。"""
    build(_full(tmp_path), now=NOW, days=30)
    report = build(_full(tmp_path), fmts=("sharegpt",), now=NOW, days=30, dry_run=True)

    assert set(report.removed) == {
        file_name("conversation", "chat"),
        file_name("reasoning", "chat"),
        file_name("tooluse", "chat"),
    }
    # 说了会删，但一个都没真删。
    assert file_name("conversation", "chat") in _jsonl(tmp_path)


# ── scan ────────────────────────────────────────────────────
def test_scan_reads_back_what_build_wrote(tmp_path: Path) -> None:
    build(_full(tmp_path), now=NOW, days=30)
    status = scan(tmp_path, current_digest="", logger=LOGGER)

    assert status.exists is True
    assert len(status.files) == 3
    assert status.samples == 3
    assert status.error == ""


def test_scan_reports_a_missing_directory_without_raising(tmp_path: Path) -> None:
    """还没跑过不是错误。没跑过和跑坏了必须区分得开。"""
    status = scan(tmp_path / "没有这个目录", current_digest="x", logger=LOGGER)

    assert status.exists is False
    assert status.files == ()
    assert status.error == ""


def test_scan_marks_a_dataset_stale_when_the_rules_changed(tmp_path: Path) -> None:
    """脱敏规则一改就该重跑——这是「脱敏可回溯重做」的兑现机制。"""
    build(_full(tmp_path), now=NOW, days=30)

    built = scan(tmp_path, current_digest="", logger=LOGGER)
    assert built.redact_digest
    # 拿不到当前指纹（空串）时就比不了，此时按「该重跑」处理。
    assert built.stale is True
    assert scan(tmp_path, current_digest=built.redact_digest, logger=LOGGER).stale is False
    assert scan(tmp_path, current_digest="另一个指纹", logger=LOGGER).stale is True


def test_scan_treats_a_manifest_without_a_digest_as_stale(tmp_path: Path) -> None:
    """没有指纹记录的一律按「该重跑」处理。

    宁可多跑一次，也不要让一份可能没脱干净的数据集被当成合格品。
    """
    build(_full(tmp_path), now=NOW, days=30)
    assert scan(tmp_path, current_digest="anything", logger=LOGGER).stale is True


def test_scan_survives_a_corrupt_manifest(tmp_path: Path) -> None:
    """记录文件坏了要报出来，不能让 ``list`` 直接崩掉。"""
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / MANIFEST_NAME).write_text("{不是 JSON", encoding="utf-8")

    status = scan(tmp_path, current_digest="x", logger=LOGGER)

    assert status.error
    assert status.files == ()


def test_scan_does_not_reread_the_data_files(tmp_path: Path) -> None:
    """手改过 ``.jsonl`` 也要照实报——``scan`` 只认记录，不重新数。"""
    build(_full(tmp_path), now=NOW, days=30)
    before = scan(tmp_path, current_digest="", logger=LOGGER)
    target = tmp_path / file_name("conversation", "chat")
    target.write_text("", encoding="utf-8")

    after = scan(tmp_path, current_digest="", logger=LOGGER)
    assert [item.samples for item in after.files] == [item.samples for item in before.files]
    assert after.samples == before.samples


def test_scan_reports_files_that_the_manifest_never_mentions(tmp_path: Path) -> None:
    """目录里冒出来的 ``.jsonl`` 要报出来——那是会污染训练的隐患。"""
    build(_full(tmp_path), now=NOW, days=30)
    (tmp_path / "conversation.old.jsonl").write_text("{}\n", encoding="utf-8")

    status = scan(tmp_path, current_digest="", logger=LOGGER)
    assert status.unlisted == ("conversation.old.jsonl",)


def test_scan_on_a_clean_directory_reports_no_unlisted_files(tmp_path: Path) -> None:
    build(_full(tmp_path), now=NOW, days=30)
    assert scan(tmp_path, current_digest="", logger=LOGGER).unlisted == ()


def test_build_records_unlisted_files_in_the_manifest_and_readme(tmp_path: Path) -> None:
    """build 清不掉的（比如上一次自己什么都没写出来的情况下）要在两页里留痕。"""
    build(_full(tmp_path), now=NOW, days=30)
    stray = tmp_path / "conversation.old.jsonl"
    stray.write_text("{}\n", encoding="utf-8")

    # 先把这次要写的都写掉，然后手动再放一个进去——模拟「build 之后有人 dump 了一个」。
    build(_full(tmp_path), now=NOW, days=30)
    stray.write_text("{}\n", encoding="utf-8")
    build(_full(tmp_path), now=NOW, days=30)
    stray.write_text("{}\n", encoding="utf-8")
    status = scan(tmp_path, current_digest="", logger=LOGGER)

    assert status.unlisted == ("conversation.old.jsonl",)
    readme = (tmp_path / README_NAME).read_text(encoding="utf-8")
    # build 每次都把它清掉了，所以两页里没有它——上面那次 scan 看到的是
    # 「刚手动放进去」的状态。这条只在说明「清理确实生效」。
    assert "conversation.old.jsonl" not in readme


# ── preview ─────────────────────────────────────────────────
def test_preview_renders_samples_without_writing_anything(tmp_path: Path) -> None:
    """``show`` 是「现场跑一遍给我看」，不落任何文件。"""
    items = preview(_full(tmp_path), now=NOW, days=30, per_kind=1)

    assert {item.kind for item in items} == {"conversation", "reasoning", "tooluse"}
    assert all(item.shown for item in items)
    assert not any(tmp_path.iterdir())


def test_preview_respects_per_kind(tmp_path: Path) -> None:
    """``--limit`` 真的限制条数，不是摆设。"""
    items = preview(_full(tmp_path), now=NOW, days=30, per_kind=1)
    assert all(len(item.shown) <= 1 for item in items)


def test_preview_reports_the_total_not_just_what_it_shows(tmp_path: Path) -> None:
    """看一条也要知道一共多少条，否则判断不了值不值得导出。"""
    items = preview(_full(tmp_path), now=NOW, days=30, per_kind=1)
    conversation = next(item for item in items if item.kind == "conversation")
    assert conversation.total == 1
    assert len(conversation.shown) == 1


def test_preview_redacts_before_showing(tmp_path: Path) -> None:
    """屏幕上那份也必须是脱过的——否则脱敏就只是个事后步骤。

    终端里的记录会被复制、被截图。要给人看的东西，在给人看之前就得干净。
    """
    items = preview(_full(tmp_path), now=NOW, days=30, per_kind=1)
    rendered = json.dumps([item.shown for item in items], ensure_ascii=False)
    assert "13800138000" not in rendered
    assert "[手机号]" in rendered


def test_preview_is_empty_for_an_empty_window(tmp_path: Path) -> None:
    """窗口内没数据时，每类都是 0 条，而不是报错。"""
    items = preview(_work(tmp_path, FakeSources()), now=NOW, days=30)
    assert all(item.total == 0 and not item.shown for item in items)


# ── 纯函数 ──────────────────────────────────────────────────
def test_planned_paths_never_touches_the_disk(tmp_path: Path) -> None:
    """还没跑过也要能说出文件会落在哪。"""
    missing = tmp_path / "还没建"
    paths = planned_paths(missing, fmt="chat")
    assert paths == tuple(
        (kind, missing / file_name(kind, "chat"))
        for kind in ("conversation", "reasoning", "tooluse")
    )
    assert not missing.exists()


@pytest.mark.parametrize("fmt", ["chat", "sharegpt", "alpaca"])
def test_file_name_carries_both_kind_and_format(fmt: str) -> None:
    """形状进文件名而不是只进目录名——三种并存才好比较。"""
    name = file_name("conversation", fmt)  # type: ignore[arg-type]
    assert name.startswith("conversation.")
    assert fmt in name
    assert name.endswith(".jsonl")


def test_build_system_prompt_goes_into_every_format(tmp_path: Path) -> None:
    """``system_prompt`` 有值时三种形状都要带上，留空就不带。"""
    build(_full(tmp_path / "with", system_prompt="你是林晚"), now=NOW, days=30)
    build(_full(tmp_path / "without"), now=NOW, days=30)

    with_prompt = (tmp_path / "with" / file_name("conversation", "chat")).read_text(
        encoding="utf-8"
    )
    without = (tmp_path / "without" / file_name("conversation", "chat")).read_text(encoding="utf-8")

    assert "你是林晚" in with_prompt
    assert "你是林晚" not in without


def test_build_applies_the_configured_redact_terms(tmp_path: Path) -> None:
    """``redact_terms`` 里的自定义词真的会被摘掉。"""
    build(_full(tmp_path / "a", redact_terms=("13800138000",)), now=NOW, days=30)
    build(_full(tmp_path / "b"), now=NOW, days=30)

    extra = (tmp_path / "a" / file_name("conversation", "chat")).read_text(encoding="utf-8")
    plain = (tmp_path / "b" / file_name("conversation", "chat")).read_text(encoding="utf-8")

    assert "13800138000" not in extra
    assert "13800138000" not in plain  # 内置规则本来就会摘掉它


def test_build_records_the_redaction_counts(tmp_path: Path) -> None:
    """脱了几处要能报出来——这是用户判断「脱干净没有」唯一的反馈。"""
    report = build(_full(tmp_path), now=NOW, days=30)
    assert report.replaced >= 1
    assert dict(report.redactions).get("phone_cn") == 1


def test_manifest_records_the_digest_of_the_rules_that_were_used(tmp_path: Path) -> None:
    """写进记录里的指纹必须和 ``scan`` 比对的算法是同一个。"""
    report = build(_full(tmp_path), now=NOW, days=30)
    manifest = json.loads((tmp_path / MANIFEST_NAME).read_text(encoding="utf-8"))

    assert manifest["redact_digest"] == report.redact_digest
    assert scan(tmp_path, current_digest=report.redact_digest, logger=LOGGER).stale is False


def test_build_writes_utf8_so_chinese_stays_readable(tmp_path: Path) -> None:
    """中文原样落盘。转义成 ``\\u4f60`` 就没法一眼看出脱敏干不干净了。"""
    build(_full(tmp_path), now=NOW, days=30)
    raw = (tmp_path / file_name("conversation", "chat")).read_bytes()
    assert "我看看" in raw.decode("utf-8")
    assert b"\\u" not in raw
    assert (tmp_path / README_NAME).read_bytes().decode("utf-8").count("训练数据集") >= 1


def test_build_writes_lf_line_endings(tmp_path: Path) -> None:
    """Windows 上默认会写成 ``\\r\\n``，那份文件在 git 里 diff 就没用了。"""
    build(_full(tmp_path), now=NOW, days=30)
    raw = (tmp_path / file_name("conversation", "chat")).read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")


def test_build_leaves_no_temporary_files_behind(tmp_path: Path) -> None:
    """原子写用了 ``.tmp``，跑完不能留下它们。"""
    build(_full(tmp_path), now=NOW, days=30)
    assert not list(tmp_path.glob("*.tmp"))

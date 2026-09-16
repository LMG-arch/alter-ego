"""``alterego dataset`` 命令组的端到端测试。

覆盖**第五个组装根**里那些只在这里才成立的东西：参数解析、人设解析、
库路径推导、只读打开数据库、以及四条命令各自的输出。

两条只在这个文件里能测的规矩：

1. **一个模型都不调。** 所有动词都不建供应商——有 ``_explode`` 守着，
   谁哪天顺手加一次 LLM 调用就会红（见 ADR-0011）。
2. **库是只读打开的。** 导出不该顺手写一下「我导过了」这种记录：
   数据集是派生产物，库是事实来源，派生别回写事实。

依据: docs/plans/2026-09-16-training-datasets.md
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from alterego.cli import build_parser, main
from alterego.domain.dataset import FORMATS
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.config import Config
from alterego.storage.sqlite import SqliteStorageBackend


TZ = resolve_timezone("Asia/Shanghai")

#: 命令自己取墙上时间，素材用固定日期造的话窗口就永远对不上。
NOW = datetime.now(TZ)
YESTERDAY = (NOW - timedelta(days=1)).date()
ANCHOR = datetime(YESTERDAY.year, YESTERDAY.month, YESTERDAY.day, 12, 0, tzinfo=TZ)
STAMP = NOW.isoformat()


def _explode(_config: Config) -> object:
    """装了它，任何「其实不需要模型却去造供应商」的路径都会被逮住。"""
    raise AssertionError("这条命令不该去建供应商")


# ── 夹具 ────────────────────────────────────────────────────


def _make_config(tmp_path: Path) -> Config:
    """一份指向临时目录的配置，导出根也钉在临时目录里。"""
    return Config.load(
        path=None,
        env={},
        overrides={
            "core": {"data_dir": str(tmp_path)},
            "storage": {"backend": "sqlite"},
            "dataset": {"export_dir": str(tmp_path / "exports" / "datasets")},
        },
    )


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把配置和当前目录都挪进临时目录，免得碰到真的 ``exports/``。"""
    monkeypatch.setattr("alterego.cli_dataset._dataset_config", lambda: _make_config(tmp_path))
    monkeypatch.setattr("alterego.cli_dataset._providers", _explode, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alterego.db"


def dataset_root(tmp_path: Path, persona: str = "林晚") -> Path:
    return tmp_path / "exports" / "datasets" / persona


def _seed(
    path: Path,
    *,
    personas: Sequence[tuple[str, str]] = (("p1", "林晚"),),
    messages: int = 1,
    ticks: int = 0,
) -> None:
    """建库、迁移，然后塞进素材。"""
    with SqliteStorageBackend.open(path) as backend:
        backend.migrate()
        for persona_id, name in personas:
            backend.connection.execute(
                "INSERT INTO persona (id, name, persona_json, created_at, updated_at)"
                " VALUES (?, ?, '{}', ?, ?)",
                (persona_id, name, STAMP, STAMP),
            )
        if not personas:
            return
        owner = personas[0][0]

        if messages:
            backend.connection.execute(
                "INSERT INTO conversation (id, persona_id, counterpart_id, counterpart_kind,"
                " title, message_count, created_at)"
                " VALUES ('c1', ?, 'user', 'user', '和你的对话', ?, ?)",
                (owner, messages * 2, STAMP),
            )
        for index in range(messages):
            at = ANCHOR + timedelta(minutes=index * 2)
            backend.connection.execute(
                "INSERT INTO message (id, conversation_id, direction, sender_id, content,"
                " content_type, created_at) VALUES (?, 'c1', 'inbound', 'user', ?, 'text', ?)",
                (f"m{index}a", f"我手机 1380013800{index} 打不通了", at.isoformat()),
            )
            backend.connection.execute(
                "INSERT INTO message (id, conversation_id, direction, sender_id, content,"
                " content_type, created_at) VALUES (?, 'c1', 'outbound', ?, ?, 'text', ?)",
                (
                    f"m{index}b",
                    owner,
                    "我看看",
                    (at + timedelta(minutes=1)).isoformat(),
                ),
            )

        for index in range(ticks):
            at = ANCHOR + timedelta(minutes=10 + index * 10)
            backend.connection.execute(
                "INSERT INTO tick_log (id, persona_id, virtual_time, real_duration_ms, status,"
                " candidates_json, chosen_intent, motivation, percepts_json, created_at)"
                " VALUES (?, ?, ?, 10, 'ok', ?, 'social/reply', '她问我了', ?, ?)",
                (
                    f"t{index}",
                    owner,
                    at.isoformat(),
                    json.dumps([f"选项{index}"], ensure_ascii=False),
                    json.dumps([f"感知{index}"], ensure_ascii=False),
                    at.isoformat(),
                ),
            )


def _exports(tmp_path: Path) -> Path:
    return tmp_path / "exports"


def _snapshot(root: Path) -> dict[str, bytes]:
    """导出根下的每个文件按相对路径取内容。用来证明「什么都没动」。

    只看导出根，不看整个临时目录：库被打开一次就会多出
    ``-wal`` / ``-shm`` 两个旁支文件，那是 SQLite 的事，与导出无关。
    """
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _jsonl(root: Path) -> set[str]:
    return {path.name for path in root.glob("*.jsonl")} if root.is_dir() else set()


# ────────────────────────────────────────────────────────────
# 命令行
# ────────────────────────────────────────────────────────────


class TestCommandLine:
    def test_every_verb_is_registered(self) -> None:
        parser = build_parser()

        for verb in ("build", "list", "paths", "show"):
            args = parser.parse_args(["dataset", verb])
            assert args.handler.__name__ == f"cmd_dataset_{verb}"

    def test_the_subcommand_is_recorded(self) -> None:
        parser = build_parser()
        assert parser.parse_args(["dataset", "build"]).subcommand == "build"
        assert parser.parse_args(["dataset", "list"]).subcommand == "list"

    def test_the_group_shows_its_own_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        """只敲到 ``dataset`` 时要打这一层的帮助，而不是顶层的。"""
        assert main(["dataset"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("usage: alterego dataset ")
        assert "{build,list,paths,show}" in out
        assert "取数 → 脱敏" in out

    def test_the_top_level_lists_the_group(self, capsys: pytest.CaptureFixture[str]) -> None:
        """顶层帮助里要让 ``dataset`` 露面，不然没人会知道有这东西。"""
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--help"])
        assert "dataset" in capsys.readouterr().out

    @pytest.mark.parametrize("verb", ["build", "paths", "show"])
    def test_format_choices_are_enforced(self, verb: str) -> None:
        """形状名写错要当场报错，不能静默出空文件。"""
        for fmt in FORMATS:
            assert build_parser().parse_args(["dataset", verb, "--format", fmt]).fmt == fmt
        with pytest.raises(SystemExit) as caught:
            build_parser().parse_args(["dataset", verb, "--format", "yaml"])
        assert caught.value.code == 2

    def test_defaults_are_none_so_the_config_can_decide(self) -> None:
        """没给形状时留 ``None``：由 ``[dataset] formats`` 说了算，不是命令。"""
        assert build_parser().parse_args(["dataset", "build"]).fmt is None
        assert build_parser().parse_args(["dataset", "build"]).days == 0
        assert build_parser().parse_args(["dataset", "show"]).limit == 2


# ────────────────────────────────────────────────────────────
# 库与人设
# ────────────────────────────────────────────────────────────


class TestPicking:
    def test_a_missing_database_fails_with_a_hint(self, tmp_path: Path) -> None:
        assert main(["dataset", "list"]) == 2

    def test_an_empty_database_fails_with_a_hint(self, tmp_path: Path) -> None:
        _seed(db_path(tmp_path), personas=())
        assert main(["dataset", "list"]) == 2

    def test_one_persona_is_picked_automatically(self, tmp_path: Path) -> None:
        _seed(db_path(tmp_path))
        assert main(["dataset", "paths"]) == 0

    def test_an_unknown_name_fails(self, tmp_path: Path) -> None:
        _seed(db_path(tmp_path))
        assert main(["dataset", "paths", "--persona", "查无此人"]) == 2

    def test_several_personas_need_a_name(self, tmp_path: Path) -> None:
        _seed(db_path(tmp_path), personas=(("p1", "林晚"), ("p2", "阿泽")))
        assert main(["dataset", "paths"]) == 2
        assert main(["dataset", "paths", "--persona", "阿泽"]) == 0

    def test_an_explicit_out_directory_wins(self, tmp_path: Path) -> None:
        _seed(db_path(tmp_path))
        elsewhere = tmp_path / "别处"
        assert main(["dataset", "build", "--out", str(elsewhere)]) == 0
        assert (elsewhere / "manifest.json").is_file()

    def test_the_export_lands_under_the_configured_root(self, tmp_path: Path) -> None:
        _seed(db_path(tmp_path))
        assert main(["dataset", "build"]) == 0
        assert dataset_root(tmp_path).is_dir()


# ────────────────────────────────────────────────────────────
# build
# ────────────────────────────────────────────────────────────


class TestBuild:
    def test_build_writes_the_data_and_the_two_pages(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path(tmp_path), ticks=1)
        assert main(["dataset", "build"]) == 0

        root = dataset_root(tmp_path)
        assert _jsonl(root) == {"conversation.chat.jsonl", "reasoning.chat.jsonl"}
        assert (root / "manifest.json").is_file()
        assert (root / "README.md").is_file()

        out = capsys.readouterr().out
        assert "对话训练集" in out
        assert "说明页" in out

    def test_build_reports_a_dry_run_without_writing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path(tmp_path))
        assert main(["dataset", "build", "--dry-run"]) == 0

        assert _jsonl(dataset_root(tmp_path)) == set()
        assert "一个文件都没写" in capsys.readouterr().out

    def test_build_honours_an_explicit_format(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path(tmp_path))
        assert main(["dataset", "build", "--format", "alpaca"]) == 0

        assert _jsonl(dataset_root(tmp_path)) == {"conversation.alpaca.jsonl"}
        assert (
            (dataset_root(tmp_path) / "conversation.alpaca.jsonl")
            .read_text(encoding="utf-8")
            .startswith('{"instruction"')
        )
        assert "alpaca" in capsys.readouterr().out

    def test_build_says_what_it_removed_when_the_format_changed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """换形状之后旧文件被清掉，这件事必须在输出里看得见。"""
        _seed(db_path(tmp_path))
        main(["dataset", "build", "--format", "chat"])
        capsys.readouterr()

        assert main(["dataset", "build", "--format", "sharegpt"]) == 0
        out = capsys.readouterr().out
        assert "conversation.chat.jsonl" in out
        assert "删掉" in out
        assert _jsonl(dataset_root(tmp_path)) == {"conversation.sharegpt.jsonl"}

    def test_build_redacts_before_writing(self, tmp_path: Path) -> None:
        """落盘的那份必须是脱过的——这是整个子系统的存在理由。"""
        _seed(db_path(tmp_path))
        assert main(["dataset", "build"]) == 0
        raw = (dataset_root(tmp_path) / "conversation.chat.jsonl").read_text(encoding="utf-8")
        assert "13800138000" not in raw
        assert "[手机号]" in raw

    def test_build_counts_what_it_replaced(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path(tmp_path))
        assert main(["dataset", "build"]) == 0
        assert "脱敏" in capsys.readouterr().out

    def test_build_reports_every_empty_dataset_with_a_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """空的那一类要说清为什么空，不能只留一个零。"""
        _seed(db_path(tmp_path))
        assert main(["dataset", "build"]) == 0
        out = capsys.readouterr().out
        assert "工具调用训练集" in out
        assert "空" in out

    def test_build_takes_a_lookback_window(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path(tmp_path))
        assert main(["dataset", "build", "--days", "1"]) == 0
        assert "最近 1 天" in capsys.readouterr().out

    def test_build_never_touches_the_database(self, tmp_path: Path) -> None:
        """导出是只读的：派生不该回写事实来源。"""
        _seed(db_path(tmp_path))
        before = db_path(tmp_path).read_bytes()
        assert main(["dataset", "build"]) == 0
        assert db_path(tmp_path).read_bytes() == before

    def test_build_is_reproducible(self, tmp_path: Path) -> None:
        """跑两遍逐字节相同（时间戳之外）——sha256 才有意义。"""
        _seed(db_path(tmp_path))
        main(["dataset", "build"])
        first = (dataset_root(tmp_path) / "conversation.chat.jsonl").read_bytes()
        main(["dataset", "build"])
        assert (dataset_root(tmp_path) / "conversation.chat.jsonl").read_bytes() == first


# ────────────────────────────────────────────────────────────
# list
# ────────────────────────────────────────────────────────────


class TestList:
    def test_list_before_any_build_says_it_never_ran(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """还没跑过不是错误，但必须说清「先跑一次」。"""
        _seed(db_path(tmp_path))
        assert main(["dataset", "list"]) == 0
        assert "还没" in capsys.readouterr().out

    def test_list_shows_what_is_on_disk(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path(tmp_path))
        main(["dataset", "build"])
        capsys.readouterr()

        assert main(["dataset", "list"]) == 0
        out = capsys.readouterr().out
        assert "conversation.chat.jsonl" in out
        assert "是最新的" in out

    def test_list_prints_the_full_path_of_every_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """「保存路径也可以查看」——用户要的就是这个。"""
        _seed(db_path(tmp_path))
        main(["dataset", "build"])
        capsys.readouterr()

        main(["dataset", "list"])
        out = capsys.readouterr().out
        assert str(dataset_root(tmp_path) / "conversation.chat.jsonl") in out
        assert str(dataset_root(tmp_path) / "README.md") in out
        assert str(dataset_root(tmp_path) / "manifest.json") in out

    def test_list_warns_when_the_rules_changed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """脱敏规则改了就该说「该重跑」，而不是继续报「是最新的」。"""
        _seed(db_path(tmp_path))
        main(["dataset", "build"])
        capsys.readouterr()

        config = _make_config(tmp_path)
        changed = Config.load(
            path=None,
            env={},
            overrides={
                "core": {"data_dir": str(tmp_path)},
                "storage": {"backend": "sqlite"},
                "dataset": {
                    "export_dir": str(tmp_path / "exports" / "datasets"),
                    "redact_terms": ("某个只有这次才脱的词",),
                },
            },
        )
        assert config.dataset.redact_terms == ()

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr("alterego.cli_dataset._dataset_config", lambda: changed)
        try:
            assert main(["dataset", "list"]) == 0
        finally:
            monkeypatch.undo()
        assert "重跑" in capsys.readouterr().out

    def test_list_warns_about_files_the_manifest_does_not_mention(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """目录里多出来的 ``.jsonl`` 会污染训练，必须报出来。"""
        _seed(db_path(tmp_path))
        main(["dataset", "build"])
        capsys.readouterr()

        (dataset_root(tmp_path) / "conversation.old.jsonl").write_text("{}\n", encoding="utf-8")
        assert main(["dataset", "list"]) == 0
        out = capsys.readouterr().out
        assert "conversation.old.jsonl" in out
        assert "不在这份记录里" in out

    def test_list_never_writes_anything(self, tmp_path: Path) -> None:
        """看一眼不该改变磁盘状态。"""
        _seed(db_path(tmp_path))
        main(["dataset", "build"])
        before = _snapshot(_exports(tmp_path))
        main(["dataset", "list"])
        assert _snapshot(_exports(tmp_path)) == before


# ────────────────────────────────────────────────────────────
# paths
# ────────────────────────────────────────────────────────────


class TestPaths:
    def test_paths_answers_before_anything_was_built(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """还没跑过也要能问出「会落在哪」。"""
        _seed(db_path(tmp_path))
        assert main(["dataset", "paths"]) == 0

        out = capsys.readouterr().out
        assert str(dataset_root(tmp_path) / "conversation.chat.jsonl") in out
        assert not dataset_root(tmp_path).exists()

    def test_paths_follows_the_configured_formats(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch = pytest.MonkeyPatch()
        config = Config.load(
            path=None,
            env={},
            overrides={
                "core": {"data_dir": str(tmp_path)},
                "storage": {"backend": "sqlite"},
                "dataset": {
                    "export_dir": str(tmp_path / "exports" / "datasets"),
                    "formats": ["chat", "alpaca"],
                },
            },
        )
        monkeypatch.setattr("alterego.cli_dataset._dataset_config", lambda: config)
        _seed(db_path(tmp_path))
        try:
            assert main(["dataset", "paths"]) == 0
        finally:
            monkeypatch.undo()

        out = capsys.readouterr().out
        assert "conversation.chat.jsonl" in out
        assert "conversation.alpaca.jsonl" in out

    def test_paths_honours_an_explicit_format(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path(tmp_path))
        assert main(["dataset", "paths", "--format", "sharegpt"]) == 0
        out = capsys.readouterr().out
        assert "conversation.sharegpt.jsonl" in out
        assert "conversation.chat.jsonl" not in out

    def test_paths_says_which_ones_are_already_there(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """已经落过盘的那几份要标出来。"""
        _seed(db_path(tmp_path))
        main(["dataset", "build"])
        capsys.readouterr()

        assert main(["dataset", "paths"]) == 0
        out = capsys.readouterr().out
        assert "在" in out

    def test_paths_never_writes_anything(self, tmp_path: Path) -> None:
        _seed(db_path(tmp_path))
        before = _snapshot(_exports(tmp_path))
        main(["dataset", "paths"])
        assert _snapshot(_exports(tmp_path)) == before == {}


# ────────────────────────────────────────────────────────────
# show
# ────────────────────────────────────────────────────────────


class TestShow:
    def test_show_renders_samples_without_writing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``show`` 是现场跑一遍给你看，不是读磁盘上那一份。"""
        _seed(db_path(tmp_path), ticks=1)
        assert main(["dataset", "show"]) == 0

        out = capsys.readouterr().out
        assert "对话训练集" in out
        assert "messages" in out
        assert _jsonl(dataset_root(tmp_path)) == set()

    def test_show_works_before_any_build(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """想不想导出，是在**还没有数据**的时候决定的。"""
        _seed(db_path(tmp_path))
        assert main(["dataset", "show"]) == 0
        assert "messages" in capsys.readouterr().out

    def test_show_never_prints_a_raw_phone_number(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """屏幕上那份也要脱干净——终端里的记录会被复制、会被截图。"""
        _seed(db_path(tmp_path))
        assert main(["dataset", "show"]) == 0
        out = capsys.readouterr().out
        assert "13800138000" not in out
        assert "[手机号]" in out

    def test_show_honours_the_format(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path(tmp_path))
        assert main(["dataset", "show", "--format", "sharegpt"]) == 0
        out = capsys.readouterr().out
        assert "conversations" in out
        assert "messages" not in out

    def test_show_honours_the_limit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--limit 0`` 也是一条合法输入：只看总数。"""
        _seed(db_path(tmp_path), messages=3)
        assert main(["dataset", "show", "--limit", "0"]) == 0
        out = capsys.readouterr().out
        assert "共 1 条" in out

    def test_show_explains_an_empty_dataset_instead_of_printing_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """空的那一类要留一句说明，空面板会让人以为命令坏了。"""
        _seed(db_path(tmp_path))
        assert main(["dataset", "show"]) == 0
        assert "这一类是空的" in capsys.readouterr().out

    def test_show_never_touches_the_database(self, tmp_path: Path) -> None:
        _seed(db_path(tmp_path), ticks=1)
        before = db_path(tmp_path).read_bytes()
        main(["dataset", "show"])
        assert db_path(tmp_path).read_bytes() == before

    def test_show_is_reproducible(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """同样的库连看两次，输出一模一样——否则它就不是在报事实。"""
        _seed(db_path(tmp_path), ticks=1)
        main(["dataset", "show"])
        first = capsys.readouterr().out
        main(["dataset", "show"])
        assert capsys.readouterr().out == first

    def test_show_prints_the_source_and_the_lesson_of_each_dataset(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """看的人要知道这一类数据是「从哪来、教什么」。"""
        _seed(db_path(tmp_path), ticks=1)
        assert main(["dataset", "show"]) == 0
        out = capsys.readouterr().out
        assert "来自" in out
        assert "教它" in out

"""``alterego vault`` 命令组的端到端测试。

这个文件覆盖的是**第五个组装根**里那些只有在这里才成立的东西：
参数解析、人设解析、库路径推导、只读打开数据库、以及五条命令各自的输出。

真跑一次的路径（建库 → 同步 → 重建索引 → 看状态）都走一遍，
因为「手工敲一遍」是这个项目里第 11 次发现问题的办法——那就不该
只靠手工。能自动化的部分全放这里。

依据: docs/plans/2026-09-16-obsidian-vault.md § 8
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from alterego.cli import build_parser, main
from alterego.domain.vault import (
    CONTENT_FOLDERS,
    FOLDER_INBOX,
    FOLDER_INDEX,
    FOLDER_SCHEDULE,
    FOLDERS,
    TYPES,
)
from alterego.interfaces.common import HealthStatus
from alterego.interfaces.llm import LLMRequest, LLMResponse
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.config import Config
from alterego.storage.sqlite import SqliteStorageBackend


TZ = resolve_timezone("Asia/Shanghai")

#: 命令自己取的是墙上时间，素材若用固定日期造，窗口就永远对不上。
NOW = datetime.now(TZ)

#: 素材锂在**昨天中午**。选昨天是因为它一定落在 ``[now - 7 天, now]``
#: 这个窗口里，又不会因为测试恰好在凌晨跑、回头 7 天的素材落到了前一天，
#: 从而让「今天那一页」的断言时灵时不灵。
YESTERDAY = (NOW - timedelta(days=1)).date()
ANCHOR = datetime(YESTERDAY.year, YESTERDAY.month, YESTERDAY.day, 12, 0, tzinfo=TZ)

STAMP = NOW.isoformat()


# ── 假的模型 ────────────────────────────────────────────────


class FakeProvider:
    """只会回一段固定 JSON。``id`` 必须与 ``[llm.routing]`` 里的名字一致。"""

    id = "openai_compatible"
    tier = "cheap"
    models = ("m1",)

    def __init__(self, reply: str = "[]") -> None:
        self.reply = reply
        self.requests: list[LLMRequest] = []
        self.closed = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self.reply, model="m1", prompt_tokens=120, completion_tokens=30)

    async def aclose(self) -> None:
        self.closed = True

    def health_check(self) -> HealthStatus:
        return HealthStatus(ok=True, detail="fake")


def _explode(_config: Config) -> dict[str, FakeProvider]:
    """装了它，任何「其实不需要模型却去造供应商」的路径都会被逮住。"""
    raise AssertionError("这条命令不该去建供应商")


# ── 夹具 ────────────────────────────────────────────────────


def _make_config(tmp_path: Path, *, llm: bool = True) -> Config:
    """一份指向临时目录的配置。"""

    def build(*, with_llm: bool) -> Config:
        overrides: dict[str, object] = {
            "core": {"data_dir": str(tmp_path)},
            "storage": {"backend": "sqlite"},
        }
        if with_llm:
            overrides["llm"] = {
                "providers": {
                    "openai_compatible": {
                        "base_url": "https://example.test/v1",
                        "api_key_env": "KEY",
                        "model": "m1",
                    }
                },
                "routing": {"vault": "cheap", "cheap": "openai_compatible"},
            }
        return Config.load(path=None, env={}, overrides=overrides)

    return build(with_llm=llm)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把配置和当前目录都挪进临时目录，免得碰到真的 ``exports/``。"""
    monkeypatch.setattr("alterego.cli_vault._vault_config", lambda: _make_config(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alterego.db"


def vault_root(persona: str = "林晚") -> Path:
    return Path.cwd() / "exports" / f"{persona}的知识库"


def _seed(
    path: Path,
    *,
    personas: Sequence[tuple[str, str]] = (("p1", "林晚"),),
    schedules: int = 0,
    thoughts: int = 0,
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
            # 一条人设都没有时只建库：日程与行为日志都有外键，挂不上去。
            return
        owner = personas[0][0]

        for index in range(schedules):
            start = ANCHOR + timedelta(minutes=index * 60)
            backend.connection.execute(
                "INSERT INTO schedule_block"
                " (id, persona_id, day, start_at, end_at, activity, category,"
                "  location, source, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'work', '工位', 'template', ?)",
                (
                    f"sch-{index}",
                    owner,
                    start.date().isoformat(),
                    start.isoformat(),
                    (start + timedelta(minutes=50)).isoformat(),
                    f"第 {index} 件事",
                    STAMP,
                ),
            )

        for index in range(thoughts):
            started = ANCHOR + timedelta(minutes=index * 20)
            backend.connection.execute(
                "INSERT INTO activity_log"
                " (id, persona_id, intent, category, description, inner_voice, started_at)"
                " VALUES (?, ?, 'reflect_internal', 'internal', '坐在窗边发呆', ?, ?)",
                (f"act-{index}", owner, f"第 {index} 个念头", started.isoformat()),
            )


def _snapshot(root: Path) -> dict[str, bytes]:
    """库里的每一个文件，按相对路径取内容。用来证明「什么都没动」。"""
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _inbox_stems(root: Path) -> list[str]:
    return sorted(path.stem for path in (root / FOLDER_INBOX).glob("*.md"))


def _organize_reply(stems: Sequence[str], *, folder: str) -> str:
    return json.dumps(
        [
            {
                "inbox": stem,
                "folder": folder,
                "title": f"我自己起的名字 {index}",
                "tags": ["自己想的"],
            }
            for index, stem in enumerate(stems)
        ],
        ensure_ascii=False,
    )


def _use_fake(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    monkeypatch.setattr("alterego.cli_vault._providers", lambda _config: {provider.id: provider})


# ────────────────────────────────────────────────────────────
# 命令行
# ────────────────────────────────────────────────────────────


class TestCommandLine:
    def test_every_verb_is_registered(self) -> None:
        parser = build_parser()

        for verb in ("init", "sync", "organize", "build", "status"):
            assert parser.parse_args(["vault", verb]).handler.__name__ == f"cmd_vault_{verb}"

    def test_the_subcommand_is_recorded(self) -> None:
        parser = build_parser()

        assert parser.parse_args(["vault", "sync"]).subcommand == "sync"
        assert parser.parse_args(["vault", "status"]).subcommand == "status"

    def test_the_group_shows_its_own_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        """只敲到 ``vault`` 时要打这一层的帮助，而不是顶层的。"""
        assert main(["vault"]) == 0

        shown = capsys.readouterr().out
        for verb in ("init", "sync", "organize", "build", "status"):
            assert verb in shown

    def test_the_common_switches(self) -> None:
        parser = build_parser()

        bare = parser.parse_args(["vault", "init"])
        assert bare.vault is None
        assert bare.persona is None

        given = parser.parse_args(["vault", "init", "--vault", "D:/别的库", "--persona", "阿泽"])
        assert given.vault == "D:/别的库"
        assert given.persona == "阿泽"

    def test_every_verb_takes_the_common_switches(self) -> None:
        """漏一个的话，``vault status --vault X`` 会变成「unrecognized arguments」。"""
        parser = build_parser()

        for verb in ("init", "sync", "organize", "build", "status"):
            args = parser.parse_args(["vault", verb, "--vault", "D:/x", "--persona", "阿泽"])
            assert args.vault == "D:/x"
            assert args.persona == "阿泽"

    def test_lookback_defaults_to_seven(self) -> None:
        parser = build_parser()

        assert parser.parse_args(["vault", "sync"]).lookback_days == 7
        assert parser.parse_args(["vault", "sync", "--lookback-days", "3"]).lookback_days == 3

    def test_organize_is_not_dry_by_default(self) -> None:
        parser = build_parser()

        assert parser.parse_args(["vault", "organize"]).dry_run is False
        assert parser.parse_args(["vault", "organize", "--dry-run"]).dry_run is True


# ────────────────────────────────────────────────────────────
# 前置条件
# ────────────────────────────────────────────────────────────


class TestPreconditions:
    def test_it_says_so_when_there_is_no_database(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["vault", "init"]) == 2

        assert "数据库还不存在" in capsys.readouterr().err

    def test_no_persona_yet(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path, personas=())

        assert main(["vault", "init"]) == 2

        assert "还没有人设" in capsys.readouterr().err

    def test_several_personas_need_a_name(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """两个角色的库混在一起是没法修的，所以宁可不猜。"""
        _seed(db_path, personas=(("p1", "林晚"), ("p2", "阿泽")))

        assert main(["vault", "init"]) == 2

        err = capsys.readouterr().err
        assert "有多个人设" in err
        assert "林晚" in err
        assert "阿泽" in err

    def test_a_name_may_be_given(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path, personas=(("p1", "林晚"), ("p2", "阿泽")))

        assert main(["vault", "init", "--persona", "阿泽"]) == 0

        assert "阿泽" in capsys.readouterr().out
        assert vault_root("阿泽").is_dir()
        assert not vault_root("林晚").exists()

    def test_an_unknown_name(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path)

        assert main(["vault", "init", "--persona", "查无此人"]) == 2

        assert "没有叫这个名字的人设" in capsys.readouterr().err

    def test_the_error_is_not_printed_twice(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``_fail`` 自己打过一遍，``main()`` 不该再打一遍。"""
        _seed(db_path, personas=())

        main(["vault", "init"])

        assert capsys.readouterr().err.count("还没有人设") == 1


# ────────────────────────────────────────────────────────────
# init
# ────────────────────────────────────────────────────────────


class TestInit:
    def test_it_creates_the_skeleton(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)

        assert main(["vault", "init"]) == 0

        root = vault_root()
        for folder in FOLDERS:
            assert (root / folder).is_dir(), folder
        assert (root / ".obsidian" / "app.json").is_file()
        assert (root / ".obsidian" / "core-plugins.json").is_file()

    def test_new_files_land_in_the_inbox(self, db_path: Path) -> None:
        """在 Obsidian 里随手新建一篇，应该落在收集箱而不是库根目录。"""
        _seed(db_path)

        main(["vault", "init"])

        payload = json.loads((vault_root() / ".obsidian" / "app.json").read_text(encoding="utf-8"))
        assert payload["newFileLocation"] == "folder"
        assert payload["newFileFolderPath"] == FOLDER_INBOX

    def test_the_daily_notes_plugin_stays_off(self, db_path: Path) -> None:
        """开了 daily-notes 会往库根目录吐文件，绕开我们的目录约定。"""
        _seed(db_path)

        main(["vault", "init"])

        payload = json.loads(
            (vault_root() / ".obsidian" / "core-plugins.json").read_text(encoding="utf-8")
        )
        assert "daily-notes" not in payload

    def test_the_empty_vault_loads_without_broken_links(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """刚搭好的库不该报坏链——库首页链到五个目录页，那就得五页都在。"""
        _seed(db_path)
        main(["vault", "init"])
        capsys.readouterr()

        assert main(["vault", "status"]) == 0

        assert "问题      0 处" in capsys.readouterr().out

    def test_it_says_what_to_do_next(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)

        main(["vault", "init"])

        out = capsys.readouterr().out
        assert "林晚的知识库" in out
        assert "Obsidian" in out
        assert "vault sync" in out

    def test_an_explicit_location_wins(
        self, db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)
        elsewhere = tmp_path / "别处"

        assert main(["vault", "init", "--vault", str(elsewhere)]) == 0

        assert (elsewhere / FOLDER_INDEX).is_dir()
        assert not vault_root().exists()

    def test_it_never_touches_what_is_already_there(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """第一次 init 之后手改坏了想重来，不能把手写的东西抹掉。"""
        _seed(db_path)
        main(["vault", "init"])
        handwritten = vault_root() / FOLDER_INDEX / "我自己写的.md"
        handwritten.write_text("别动我", encoding="utf-8")
        capsys.readouterr()

        assert main(["vault", "init"]) == 0

        assert handwritten.read_text(encoding="utf-8") == "别动我"


# ────────────────────────────────────────────────────────────
# sync
# ────────────────────────────────────────────────────────────


class TestSync:
    def test_it_needs_no_provider(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """一台还没填密钥的机器，应该能先把自己的日程写成笔记。"""
        _seed(db_path, schedules=2)
        monkeypatch.setattr("alterego.cli_vault._providers", _explode)

        assert main(["vault", "sync"]) == 0

        assert "计费" not in capsys.readouterr().out

    def test_it_writes_a_day_page(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path, schedules=1)

        assert main(["vault", "sync"]) == 0

        page = vault_root() / FOLDER_SCHEDULE / f"{YESTERDAY:%Y-%m-%d}.md"
        assert page.is_file()
        assert "第 0 件事" in page.read_text(encoding="utf-8")

    def test_it_reports_the_counts(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path, schedules=2, thoughts=3)
        main(["vault", "init"])
        capsys.readouterr()

        assert main(["vault", "sync"]) == 0

        out = capsys.readouterr().out
        assert "日程      1 天" in out
        assert "想法      3 条" in out
        assert "写出" in out

    def test_it_builds_the_indexes(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """笔记一写下来索引就过时了，``sync`` 得顺手把它重建。

        不重建的话，用户打开 Obsidian 看到的是 ``init`` 时那几张空索引页，
        而旁边目录里明明躺着一堆笔记——那看起来像同步失败了。
        """
        _seed(db_path, schedules=1)

        assert main(["vault", "sync"]) == 0

        index = vault_root() / FOLDER_INDEX / f"{TYPES[FOLDER_SCHEDULE]}.md"
        assert index.is_file()
        assert f"{YESTERDAY:%Y-%m-%d}" in index.read_text(encoding="utf-8")

    def test_it_says_when_the_inbox_has_work_left(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path, schedules=1, thoughts=1)
        main(["vault", "init"])
        capsys.readouterr()

        main(["vault", "sync"])

        assert "vault organize" in capsys.readouterr().out

    def test_it_leaves_the_database_alone(self, db_path: Path) -> None:
        """知识库是库的**下游**：只读打开，一个字节都不写回去。"""
        _seed(db_path, schedules=2, thoughts=2)
        before = db_path.read_bytes()

        assert main(["vault", "sync"]) == 0

        assert db_path.read_bytes() == before


# ────────────────────────────────────────────────────────────
# build 与 status
# ────────────────────────────────────────────────────────────


class TestBuild:
    def test_it_rebuilds_the_index_pages(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path, schedules=1)
        main(["vault", "init"])
        main(["vault", "sync"])
        capsys.readouterr()

        assert main(["vault", "build"]) == 0

        out = capsys.readouterr().out
        assert "重建" in out
        assert "索引页" in out

    def test_it_notices_a_hand_deleted_note(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """索引是派生的，删一篇笔记之后重建一遍就该跟着消失。"""
        _seed(db_path, schedules=1, thoughts=2)
        main(["vault", "init"])
        main(["vault", "sync"])
        capsys.readouterr()
        assert len(_inbox_stems(vault_root())) == 2

        assert main(["vault", "build"]) == 0

        assert "问题      0 处" in capsys.readouterr().out

    def test_a_clean_vault_passes_the_check(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path, schedules=2, thoughts=2)
        for verb in ("init", "sync", "build"):
            main(["vault", verb])
        capsys.readouterr()

        main(["vault", "status"])

        assert "问题      0 处" in capsys.readouterr().out


class TestStatus:
    def test_before_the_vault_exists(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)

        assert main(["vault", "status"]) == 0

        out = capsys.readouterr().out
        assert "还没建" in out
        assert "vault init" in out

    def test_it_lists_the_folders(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path)
        main(["vault", "init"])
        capsys.readouterr()

        assert main(["vault", "status"]) == 0

        out = capsys.readouterr().out
        assert "林晚" in out
        for folder in CONTENT_FOLDERS:
            assert folder in out

    def test_it_writes_nothing(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path, schedules=1)
        main(["vault", "init"])
        main(["vault", "sync"])
        capsys.readouterr()
        before = _snapshot(vault_root())

        assert main(["vault", "status"]) == 0

        assert _snapshot(vault_root()) == before


# ────────────────────────────────────────────────────────────
# organize
# ────────────────────────────────────────────────────────────


class TestOrganize:
    def test_dry_run_needs_no_api_keys(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """想先看看收集箱里有什么，不该被迫先填密钥。"""
        _seed(db_path, schedules=1, thoughts=1)
        main(["vault", "init"])
        main(["vault", "sync"])
        capsys.readouterr()
        monkeypatch.setattr("alterego.cli_vault._providers", _explode)

        assert main(["vault", "organize", "--dry-run"]) == 0

        out = capsys.readouterr().out
        assert "收集箱" in out
        assert "这只是预演" in out

    def test_dry_run_changes_nothing(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path, schedules=1, thoughts=1)
        main(["vault", "init"])
        main(["vault", "sync"])
        capsys.readouterr()
        before = _snapshot(vault_root())

        main(["vault", "organize", "--dry-run"])

        assert _snapshot(vault_root()) == before

    def test_a_dry_run_is_not_billed(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path, schedules=1)
        main(["vault", "init"])
        main(["vault", "sync"])
        capsys.readouterr()

        main(["vault", "organize", "--dry-run"])

        assert "计费" not in capsys.readouterr().out

    def test_it_files_the_inbox_and_bills_once(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db_path, schedules=1, thoughts=1)
        main(["vault", "init"])
        main(["vault", "sync"])
        stems = _inbox_stems(vault_root())
        assert stems, "收集箱应该是空的——sync 没有写进任何东西？"
        provider = FakeProvider(_organize_reply(stems, folder=CONTENT_FOLDERS[1]))
        _use_fake(monkeypatch, provider)
        capsys.readouterr()

        assert main(["vault", "organize"]) == 0

        out = capsys.readouterr().out
        assert "归位" in out
        assert "计费" in out
        assert len(provider.requests) == 1
        assert provider.closed

    def test_it_pays_the_bill_in_the_ledger(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """账得真的记上。

        内容连接是**只读**的，把账本挂在它上面不会报错，只会在日志里留一句
        ``attempt to write a readonly database``，而命令照样打印「账记在
        llm_usage 表」——那比不记账还难查。
        """
        _seed(db_path, schedules=1, thoughts=1)
        main(["vault", "init"])
        main(["vault", "sync"])
        stems = _inbox_stems(vault_root())
        _use_fake(monkeypatch, FakeProvider(_organize_reply(stems, folder=CONTENT_FOLDERS[1])))
        capsys.readouterr()

        main(["vault", "organize"])

        with SqliteStorageBackend.open(db_path, read_only=True) as backend:
            purpose = backend.connection.scalar(
                "SELECT purpose FROM llm_usage ORDER BY created_at DESC LIMIT 1"
            )
            assert purpose == "vault"

    def test_it_empties_the_inbox(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db_path, schedules=1, thoughts=1)
        main(["vault", "init"])
        main(["vault", "sync"])
        stems = _inbox_stems(vault_root())
        _use_fake(monkeypatch, FakeProvider(_organize_reply(stems, folder=CONTENT_FOLDERS[1])))
        capsys.readouterr()

        main(["vault", "organize"])

        assert _inbox_stems(vault_root()) == []
        assert (vault_root() / CONTENT_FOLDERS[1] / "我自己起的名字 0.md").is_file()

    def test_the_indexes_are_rebuilt_afterwards(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db_path, schedules=1, thoughts=1)
        main(["vault", "init"])
        main(["vault", "sync"])
        stems = _inbox_stems(vault_root())
        _use_fake(monkeypatch, FakeProvider(_organize_reply(stems, folder=CONTENT_FOLDERS[1])))
        capsys.readouterr()

        main(["vault", "organize"])

        index = vault_root() / FOLDER_INDEX / f"{TYPES[CONTENT_FOLDERS[1]]}.md"
        assert "我自己起的名字 0" in index.read_text(encoding="utf-8")

    def test_a_broken_reply_leaves_everything_alone(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """模型说了一堆用不了的话，收集箱该原封不动——包括进度。"""
        _seed(db_path, schedules=1, thoughts=1)
        main(["vault", "init"])
        main(["vault", "sync"])
        _use_fake(monkeypatch, FakeProvider("我今天不太想整理。"))
        capsys.readouterr()
        before = _snapshot(vault_root())

        assert main(["vault", "organize"]) == 0

        out = capsys.readouterr().out
        assert "归位      0 篇" in out
        assert _snapshot(vault_root()) == before
        assert _inbox_stems(vault_root()) != []

    def test_a_partly_broken_reply_keeps_the_good_ones(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """一条笔记归类得不好，不该让整次整理停下。"""
        _seed(db_path, schedules=1, thoughts=1)
        main(["vault", "init"])
        main(["vault", "sync"])
        stems = _inbox_stems(vault_root())
        payload = json.dumps(
            [
                {"inbox": stems[0], "folder": "不存在的目录", "title": "随手起的"},
                {"inbox": stems[0], "folder": CONTENT_FOLDERS[1], "title": "我自己起的名字"},
            ],
            ensure_ascii=False,
        )
        _use_fake(monkeypatch, FakeProvider(payload))
        capsys.readouterr()

        assert main(["vault", "organize"]) == 0

        out = capsys.readouterr().out
        assert "归位      1 篇" in out
        assert "跳过      1 条" in out

    def test_nothing_in_the_inbox_is_not_an_error(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """收集箱空着是正常状态：不调模型，也不报错。"""
        _seed(db_path, schedules=1, thoughts=0)
        main(["vault", "init"])
        capsys.readouterr()

        assert main(["vault", "organize"]) == 0

        assert "跳过" in capsys.readouterr().out


# ────────────────────────────────────────────────────────────
# 供应商装配
# ────────────────────────────────────────────────────────────


class TestProviders:
    def test_no_provider_configured(
        self,
        db_path: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed(db_path, schedules=1)
        main(["vault", "init"])
        main(["vault", "sync"])
        monkeypatch.setattr(
            "alterego.cli_vault._vault_config", lambda: _make_config(tmp_path, llm=False)
        )
        capsys.readouterr()

        assert main(["vault", "organize"]) == 2

        assert "一个模型供应商都没有" in capsys.readouterr().err

    def test_a_provider_without_a_base_url(
        self,
        db_path: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed(db_path, schedules=1)
        main(["vault", "init"])
        main(["vault", "sync"])

        def config() -> Config:
            return Config.load(
                path=None,
                env={},
                overrides={
                    "core": {"data_dir": str(tmp_path)},
                    "storage": {"backend": "sqlite"},
                    "llm": {
                        "providers": {"openai_compatible": {"api_key_env": "KEY", "model": "m1"}},
                        "routing": {"vault": "cheap", "cheap": "openai_compatible"},
                    },
                },
            )

        monkeypatch.setattr("alterego.cli_vault._vault_config", config)
        capsys.readouterr()

        assert main(["vault", "organize"]) == 2
        assert "base_url" in capsys.readouterr().err

    def test_it_builds_every_provider_not_just_the_used_one(
        self,
        db_path: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``vault = "cheap"`` 指向谁只有把整张路由表装齐了才知道。"""
        from alterego.cli_vault import _providers

        _seed(db_path, schedules=1)
        config = Config.load(
            path=None,
            env={},
            overrides={
                "core": {"data_dir": str(tmp_path)},
                "storage": {"backend": "sqlite"},
                "llm": {
                    "providers": {
                        "a": {"base_url": "https://a.test/v1", "api_key_env": "KA", "model": "m"},
                        "b": {"base_url": "https://b.test/v1", "api_key_env": "KB", "model": "m"},
                    },
                    "routing": {"vault": "a"},
                },
            },
        )

        built = _providers(config)

        assert sorted(built) == ["a", "b"]

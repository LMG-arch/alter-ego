"""``alterego study`` 命令组的端到端测试。

这个文件覆盖的是**第六个组装根**里那些只有在这里才成立的东西：
参数解析、人设解析、库路径推导、只读打开数据库、以及四条命令各自的输出。

有两条断言值得单独记住，它们是「不花钱」这个承诺的守卫：

1. ``plan`` / ``recall`` / ``status`` **不需要任何密钥**——一台还没配模型的
   机器应该能先把课程表看清楚。
2. ``next --dry-run`` 既不需要密钥，也**一个文件都不写**。

依据: docs/plans/2026-09-16-specialized-study.md § 6–7
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from alterego.cli import build_parser, main
from alterego.interfaces.common import HealthStatus
from alterego.interfaces.llm import LLMRequest, LLMResponse
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.config import Config
from alterego.storage.sqlite import SqliteStorageBackend


TZ = resolve_timezone("Asia/Shanghai")
NOW = datetime.now(TZ)
STAMP = NOW.isoformat()

#: 昨天的中午。挑这个点是因为它一定落在命令自己算出来的窗口里，
#: 又不会因为测试恰好在凌晨跑而落到前一天。
YESTERDAY = NOW - timedelta(days=1)


#: 每个命令最少要给的参数。``recall`` 要一句话，别人不要。
NEEDED: dict[str, list[str]] = {"next": [], "plan": [], "recall": ["降维"], "status": []}


def reply(summary: str = "降维是把高维压到低维。") -> str:
    return json.dumps({"summary": summary, "points": ["先想清楚保留什么"]}, ensure_ascii=False)


# ── 假的模型 ────────────────────────────────────────────────


class FakeProvider:
    """只会回一段固定 JSON。``id`` 必须与 ``[llm.routing]`` 里的名字一致。"""

    id = "openai_compatible"
    tier = "cheap"
    models = ("m1",)

    def __init__(self, text: str = "") -> None:
        self.text = text or reply()
        self.requests: list[LLMRequest] = []
        self.closed = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self.text, model="m1", prompt_tokens=120, completion_tokens=30)

    async def aclose(self) -> None:
        self.closed = True

    def health_check(self) -> HealthStatus:
        return HealthStatus(ok=True, detail="fake")


class FailingProvider(FakeProvider):
    """每次调用都炸。

    模型调用失败必须变成一句人话加退出码 2：那是最常见的失败，
    而堆栈会把用户直接吓跑。
    """

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError("网络断了")


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
                # 失败用例里那个假供应商每次都抛，重试只会白等。
                "max_retries": 0,
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


def _use_fake(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    """``cli_study`` 自己持有 ``_providers`` 这个名字，所以要打它那一份。"""
    monkeypatch.setattr("alterego.cli_study._providers", lambda _config: {provider.id: provider})


def _seed(
    path: Path,
    *,
    personas: Sequence[tuple[str, str, str]] = (("p1", "林晚", "算法工程师"),),
) -> None:
    """建库、迁移、塞人设。``occupation`` 决定它该学什么。"""
    with SqliteStorageBackend.open(path) as backend:
        backend.migrate()
        for persona_id, name, occupation in personas:
            backend.connection.execute(
                "INSERT INTO persona (id, name, occupation, persona_json, created_at, updated_at)"
                " VALUES (?, ?, ?, '{}', ?, ?)",
                (persona_id, name, occupation, STAMP, STAMP),
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


def write_note(root: Path, name: str, *, body: str = "把高维压到低维。") -> Path:
    path = root / "60-专业" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {name}\n\n{body}\n", encoding="utf-8", newline="\n")
    return path


# ────────────────────────────────────────────────────────────
# 命令行
# ────────────────────────────────────────────────────────────


class TestCommandLine:
    def test_every_verb_is_registered(self) -> None:
        parser = build_parser()

        for verb, extra in NEEDED.items():
            args = parser.parse_args(["study", verb, *extra])
            assert args.handler.__name__ == f"cmd_study_{verb}"

    def test_the_subcommand_is_recorded(self) -> None:
        parser = build_parser()

        assert parser.parse_args(["study", "next"]).subcommand == "next"
        assert parser.parse_args(["study", "status"]).subcommand == "status"

    def test_it_is_a_top_level_group(self) -> None:
        parser = build_parser()

        assert parser.parse_args(["study", "plan"]).command == "study"

    def test_the_group_shows_its_own_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        """只敲到 ``study`` 时要打这一层的帮助，而不是顶层的。"""
        assert main(["study"]) == 0

        shown = capsys.readouterr().out
        for verb in ("next", "plan", "recall", "status"):
            assert verb in shown

    def test_the_common_switches(self) -> None:
        parser = build_parser()

        bare = parser.parse_args(["study", "plan"])
        assert bare.vault is None
        assert bare.persona is None

        given = parser.parse_args(["study", "plan", "--vault", "D:/别的库", "--persona", "阿泽"])
        assert given.vault == "D:/别的库"
        assert given.persona == "阿泽"

    def test_every_verb_takes_the_common_switches(self) -> None:
        """漏一个的话，``study status --vault X`` 会变成「unrecognized arguments」。"""
        parser = build_parser()

        for verb, extra in NEEDED.items():
            args = parser.parse_args(
                ["study", verb, *extra, "--vault", "D:/x", "--persona", "阿泽"]
            )
            assert args.vault == "D:/x"
            assert args.persona == "阿泽"

    @pytest.mark.parametrize("verb", ["next", "plan"])
    def test_rounds_defaults_to_the_config(self, verb: str) -> None:
        parser = build_parser()

        assert parser.parse_args(["study", verb]).rounds == 0
        assert parser.parse_args(["study", verb, "--rounds", "3"]).rounds == 3

    def test_next_is_not_dry_by_default(self) -> None:
        parser = build_parser()

        assert parser.parse_args(["study", "next"]).dry_run is False
        assert parser.parse_args(["study", "next", "--dry-run"]).dry_run is True

    def test_recall_takes_a_bare_sentence(self) -> None:
        parser = build_parser()

        assert parser.parse_args(["study", "recall", "今天在调降维"]).query == "今天在调降维"

    def test_recall_switches_default_to_the_config(self) -> None:
        parser = build_parser()

        bare = parser.parse_args(["study", "recall", "降维"])
        assert bare.limit == 0
        assert bare.min_score == 0.0

        given = parser.parse_args(["study", "recall", "降维", "--limit", "5", "--min-score", "3.5"])
        assert given.limit == 5
        assert given.min_score == 3.5


# ────────────────────────────────────────────────────────────
# 前置条件
# ────────────────────────────────────────────────────────────


class TestPreconditions:
    def test_it_says_so_when_there_is_no_database(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["study", "status"]) == 2

        assert "数据库还不存在" in capsys.readouterr().err

    def test_no_persona_yet(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path, personas=())

        assert main(["study", "status"]) == 2

        assert "还没有人设" in capsys.readouterr().err

    def test_several_personas_need_a_name(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path, personas=(("p1", "林晚", "算法工程师"), ("p2", "阿泽", "医生")))

        assert main(["study", "plan"]) == 2

        err = capsys.readouterr().err
        assert "有多个人设" in err
        assert "林晚" in err

    def test_a_name_may_be_given(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path, personas=(("p1", "林晚", "算法工程师"), ("p2", "阿泽", "医生")))

        assert main(["study", "plan", "--persona", "阿泽"]) == 0

        assert "财务" not in capsys.readouterr().out

    def test_an_unknown_name(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path)

        assert main(["study", "plan", "--persona", "查无此人"]) == 2

        assert "没有叫这个名字的人设" in capsys.readouterr().err


# ────────────────────────────────────────────────────────────
# status
# ────────────────────────────────────────────────────────────


class TestStatus:
    def test_it_costs_nothing(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``status`` 从不造供应商——装了炸弹也照样跑得完。"""
        _seed(db_path)
        monkeypatch.setattr("alterego.cli_study._providers", _explode)

        assert main(["study", "status"]) == 0

    def test_it_prints_the_essentials(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)

        assert main(["study", "status"]) == 0

        out = capsys.readouterr().out
        assert "人设" in out
        assert "库" in out
        assert "领域" in out
        assert "进度" in out
        assert "笔记" in out

    def test_it_names_the_field_it_recognised(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)

        main(["study", "status"])

        out = capsys.readouterr().out
        assert "数据与算法" in out
        assert "算法工程师" in out

    def test_it_says_so_when_it_cannot_tell(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """认不出来**不是错误**：明确地说「不知道该学什么」比瞎猜一个方向好。"""
        _seed(db_path, personas=(("p1", "林晚", ""),))

        assert main(["study", "status"]) == 0

        out = capsys.readouterr().out
        assert "还不知道" in out
        assert "[study] field" in out

    def test_a_student_is_asked_for_a_major(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path, personas=(("p1", "林晚", "研究生"),))

        main(["study", "status"])

        assert "你在学的那个专业" in capsys.readouterr().out

    def test_it_lists_the_notes(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path)
        write_note(vault_root(), "降维是什么")

        main(["study", "status"])

        out = capsys.readouterr().out
        assert "降维是什么" in out
        assert "1 篇" in out

    def test_it_points_out_a_note_the_index_never_mentions(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)
        write_note(vault_root(), "手写的一篇")

        main(["study", "status"])

        out = capsys.readouterr().out
        assert "没进索引" in out
        assert "vault build" in out

    def test_it_writes_nothing(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path)
        write_note(vault_root(), "降维是什么")
        capsys.readouterr()
        before = _snapshot(vault_root())

        assert main(["study", "status"]) == 0

        assert _snapshot(vault_root()) == before


# ────────────────────────────────────────────────────────────
# plan
# ────────────────────────────────────────────────────────────


class TestPlan:
    def test_it_costs_nothing(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """一台还没填密钥的机器应该能先把课程表看清楚。"""
        _seed(db_path)
        monkeypatch.setattr("alterego.cli_study._providers", _explode)

        assert main(["study", "plan"]) == 0

    def test_it_shows_one_topic_by_default(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``[study] rounds`` 说的是**一次学几格**，不是「几轮」。

        一轮是五个角度，一次只走一格——那样才有「昨天还不知道」。
        """
        _seed(db_path)

        assert main(["study", "plan"]) == 0

        out = capsys.readouterr().out
        assert "接下来 1 格：" in out
        assert "数据与算法 · 是什么" in out
        assert "怎么做" not in out

    def test_the_rounds_switch_asks_for_more(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)

        main(["study", "plan", "--rounds", "5"])

        out = capsys.readouterr().out
        assert "接下来 5 格：" in out
        assert "数据与算法 · 我还不服的" in out

    def test_it_runs_into_the_next_round(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """要满一轮以上时，题面要标出这是第二轮。"""
        _seed(db_path)

        main(["study", "plan", "--rounds", "10"])

        out = capsys.readouterr().out
        assert "接下来 10 格：" in out
        assert "（第 2 轮）" in out

    def test_it_says_the_topics_are_computed(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)

        main(["study", "plan"])

        out = capsys.readouterr().out
        assert "算出来的" in out
        assert "alterego study next" in out

    def test_an_unknown_field_shows_the_hint(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path, personas=(("p1", "林晚", ""),))

        assert main(["study", "plan"]) == 0

        out = capsys.readouterr().out
        assert "还不知道" in out
        assert "接下来" not in out

    def test_it_writes_nothing(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path)
        write_note(vault_root(), "降维是什么")
        capsys.readouterr()
        before = _snapshot(vault_root())

        assert main(["study", "plan"]) == 0

        assert _snapshot(vault_root()) == before


# ────────────────────────────────────────────────────────────
# recall
# ────────────────────────────────────────────────────────────


class TestRecall:
    def test_it_costs_nothing(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed(db_path)
        monkeypatch.setattr("alterego.cli_study._providers", _explode)

        assert main(["study", "recall", "今天在调降维"]) == 0

    def test_it_echoes_the_question(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)

        assert main(["study", "recall", "今天在调降维那一段"]) == 0

        out = capsys.readouterr().out
        assert "问题" in out
        assert "今天在调降维那一段" in out

    def test_an_empty_vault_is_not_an_error(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """空召回是**正常结果**，所以退出码是 0、话也说清楚。"""
        _seed(db_path)

        assert main(["study", "recall", "今晚吃什么"]) == 0

        out = capsys.readouterr().out
        assert "0 篇" in out
        assert "这是正常结果" in out

    def test_it_shows_what_would_be_used(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)
        write_note(vault_root(), "降维是什么")

        main(["study", "recall", "降维是什么"])

        out = capsys.readouterr().out
        assert "1 篇" in out
        assert "命中" in out
        assert "【我学过、可能和这次有关的】" in out

    def test_the_threshold_is_shown(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)
        write_note(vault_root(), "降维是什么")

        main(["study", "recall", "降维是什么", "--min-score", "3.5"])

        out = capsys.readouterr().out
        assert "3.5" in out

    def test_a_high_threshold_shuts_it_off(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)
        write_note(vault_root(), "降维是什么")

        main(["study", "recall", "降维是什么", "--min-score", "99"])

        assert "0 篇" in capsys.readouterr().out

    def test_the_limit_is_honoured(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path)
        for index in range(4):
            write_note(vault_root(), f"降维 第{index}篇")

        main(["study", "recall", "降维", "--limit", "2"])

        assert "2 篇" in capsys.readouterr().out

    def test_it_never_reaches_outside_the_professional_folder(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """范围收在这一层，「它怎么突然说起这个」才有确定的答案。"""
        _seed(db_path)
        inbox = vault_root() / "99-收集箱"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "降维随手记.md").write_text("# 降维随手记\n\n降维。\n", encoding="utf-8")

        main(["study", "recall", "降维"])

        out = capsys.readouterr().out
        assert "0 篇" in out
        assert "在 0 篇里找的" in out

    def test_it_writes_nothing(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path)
        write_note(vault_root(), "降维是什么")
        capsys.readouterr()
        before = _snapshot(vault_root())

        assert main(["study", "recall", "降维"]) == 0

        assert _snapshot(vault_root()) == before


# ────────────────────────────────────────────────────────────
# next
# ────────────────────────────────────────────────────────────


class TestNextDryRun:
    def test_needs_no_api_keys(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """想先看看它会学什么，不该被迫先填密钥。"""
        _seed(db_path)
        monkeypatch.setattr("alterego.cli_study._providers", _explode)

        assert main(["study", "next", "--dry-run"]) == 0

        out = capsys.readouterr().out
        assert "这次会学" in out
        assert "这只是预演" in out

    def test_it_is_not_billed(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path)

        main(["study", "next", "--dry-run"])

        assert "计费" not in capsys.readouterr().out

    def test_it_changes_nothing(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path)
        capsys.readouterr()
        before = _snapshot(vault_root())

        assert main(["study", "next", "--dry-run"]) == 0

        assert _snapshot(vault_root()) == before

    def test_it_lists_the_topics(self, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _seed(db_path)

        main(["study", "next", "--dry-run", "--rounds", "2"])

        out = capsys.readouterr().out
        assert "2 格" in out
        assert "数据与算法 · 怎么做" in out


class TestNextForReal:
    def test_it_writes_a_note(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db_path)
        provider = FakeProvider(reply("降维是把高维压到低维。"))
        _use_fake(monkeypatch, provider)

        assert main(["study", "next"]) == 0

        out = capsys.readouterr().out
        assert "学完" in out
        assert "1 格" in out
        assert "60-专业" in out
        assert len(list((vault_root() / "60-专业").glob("*.md"))) == 1

    def test_it_bills_once_and_closes_the_provider(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db_path)
        provider = FakeProvider()
        _use_fake(monkeypatch, provider)
        capsys.readouterr()

        assert main(["study", "next"]) == 0

        assert "计费" in capsys.readouterr().out
        assert len(provider.requests) == 1
        assert provider.closed

    def test_it_pays_the_bill_in_the_ledger(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """账得真的记上：内容连接是只读的，账本得挂在单独一条可写连接上。"""
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider())
        capsys.readouterr()

        main(["study", "next"])

        with SqliteStorageBackend.open(db_path, read_only=True) as backend:
            count = backend.connection.scalar(
                "SELECT COUNT(*) FROM llm_usage WHERE purpose = 'vault'"
            )
        assert count == 1

    def test_it_records_the_progress(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider())
        capsys.readouterr()

        main(["study", "next"])
        capsys.readouterr()

        main(["study", "status"])

        out = capsys.readouterr().out
        assert "1 格" in out
        assert "没进索引" not in out

    def test_it_learns_as_many_as_asked(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db_path)
        provider = FakeProvider()
        _use_fake(monkeypatch, provider)

        assert main(["study", "next", "--rounds", "2"]) == 0

        assert len(provider.requests) == 2
        assert len(list((vault_root() / "60-专业").glob("*.md"))) == 2

    def test_a_reply_nobody_can_use_is_reported(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """没写成不是错误，但必须说出来——**没记进度，下次还会来**。"""
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider("我看了看，觉得挺有意思的。"))

        assert main(["study", "next"]) == 0

        out = capsys.readouterr().out
        assert "没写成" in out
        assert "下次还会来" in out
        assert "0 格" in out

    def test_an_unknown_field_skips_without_a_model(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """认不出学什么时连供应商都不该造——那一步是要花钱的。"""
        _seed(db_path, personas=(("p1", "林晚", ""),))
        monkeypatch.setattr("alterego.cli_study._providers", _explode)

        assert main(["study", "next"]) == 0

        out = capsys.readouterr().out
        assert "跳过" in out
        assert "[study] field" in out

    def test_a_broken_model_call_is_reported(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """报错要是一句人话，退出码要能写进脚本。"""
        _seed(db_path)
        _use_fake(monkeypatch, FailingProvider())

        assert main(["study", "next"]) == 2

        assert "错误" in capsys.readouterr().err

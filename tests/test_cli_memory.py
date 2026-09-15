"""``alterego memory`` 的测试。

这组用例走的是**真实路径**：真配置、真 SQLite、真迁移、真提示词模板，
唯一被换掉的是模型本身。理由是这个命令做的那件事——读素材、渲染提示词、
解析、在一个事务里写两张表——每一步的正确性都来自「上一步的输出长什么样」，
把中间任何一环换成 mock，测的就成了一个不存在的程序。

模型是唯一可以假的：它是一段外部服务，行为不可控，而且这个命令对它的
要求只有「回一段 JSON 数组」。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from alterego.cli import build_parser, main
from alterego.domain.memory import Memory, MemoryKind
from alterego.interfaces.common import HealthStatus
from alterego.interfaces.llm import LLMRequest, LLMResponse
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.config import Config
from alterego.storage.sqlite import SqliteMemoryRepository, SqliteStorageBackend


TZ = resolve_timezone("Asia/Shanghai")

#: 行为日志与记忆的时间都锚在「真实现在」上——命令自己取的是墙上时间，
#: 素材若用固定日期造，窗口就永远对不上。
NOW = datetime.now(TZ)

STAMP = NOW.isoformat()

REPLY = json.dumps(
    [
        {
            "kind": "episodic",
            "content": "下午跟阿哲聊到搬家的事，他好像有点烦",
            "importance": 0.6,
            "entities": ["阿哲"],
        },
        {"kind": "semantic", "content": "写代码的时候反倒是最轻松的", "importance": 0.5},
    ],
    ensure_ascii=False,
)


# ── 假的模型 ────────────────────────────────────────────────


class FakeProvider:
    """只会回一段固定 JSON。``id`` 必须与 ``[llm.routing]`` 里的名字一致。"""

    id = "openai_compatible"
    tier = "cheap"
    models = ("m1",)

    def __init__(self, reply: str = REPLY) -> None:
        self.reply = reply
        self.requests: list[LLMRequest] = []
        self.closed = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            text=self.reply,
            model="m1",
            prompt_tokens=120,
            completion_tokens=30,
        )

    async def aclose(self) -> None:
        self.closed = True

    def health_check(self) -> HealthStatus:
        return HealthStatus(ok=True, detail="fake")


# ── 夹具 ────────────────────────────────────────────────────


def _make_config(tmp_path: Path, **llm_overrides: object) -> Config:
    """一份指向临时目录的配置。默认就带着一个可用的供应商与路由。"""
    overrides: dict[str, object] = {
        "core": {"data_dir": str(tmp_path)},
        "storage": {"backend": "sqlite"},
        "llm": {
            "providers": {
                "openai_compatible": {
                    "base_url": "https://example.test/v1",
                    "api_key_env": "KEY",
                    "model": "m1",
                }
            },
            "routing": {"memory": "cheap", "cheap": "openai_compatible"},
        },
    }
    if llm_overrides:
        overrides["llm"] = {**overrides["llm"], **llm_overrides}  # type: ignore[dict-item]
    return Config.load(path=None, env={}, overrides=overrides)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("alterego.cli_memory._memory_config", lambda: _make_config(tmp_path))
    return tmp_path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alterego.db"


def _seed(
    path: Path,
    *,
    personas: Sequence[tuple[str, str]] = (("p1", "林晚"),),
    activities: int = 5,
    episodic: int = 0,
    semantic: int = 0,
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
            # 一条人设都没有时只建库：行为日志有外键，挂不上去。
            return
        owner = personas[0][0]

        for index in range(activities):
            started = NOW - timedelta(hours=5) + timedelta(minutes=index * 10)
            backend.connection.execute(
                "INSERT INTO activity_log"
                " (id, persona_id, intent, category, description, duration_minutes,"
                "  started_at, ended_at)"
                " VALUES (?, ?, 'work', 'internal', ?, 25, ?, ?)",
                (
                    f"act-{index}",
                    owner,
                    f"第 {index} 件事",
                    started.isoformat(),
                    (started + timedelta(minutes=25)).isoformat(),
                ),
            )

        for index in range(episodic + semantic):
            kind: MemoryKind = "episodic" if index < episodic else "semantic"
            memory = Memory(
                id=f"mem-{index}",
                persona_id=owner,
                kind=kind,
                content=f"记着的一件事 {index}",
                summary=f"一件事 {index}",
                importance=0.6,
                occurred_at=NOW - timedelta(hours=3),
                created_at=NOW,
            )
            SqliteMemoryRepository(backend.connection).save(memory)


def _memory(db_path: Path) -> int:
    """跑一次 ``memory distill``，返回退出码。"""
    return main(["memory", "distill"])


def _use_fake(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    monkeypatch.setattr("alterego.cli_memory._providers", lambda config: {provider.id: provider})


# ────────────────────────────────────────────────────────────
# 命令行
# ────────────────────────────────────────────────────────────


class TestCommandLine:
    def test_the_group_is_registered(self) -> None:
        parser = build_parser()

        assert parser.parse_args(["memory", "distill"]).handler.__name__ == "cmd_memory_distill"
        assert parser.parse_args(["memory", "consolidate"]).handler.__name__ == (
            "cmd_memory_consolidate"
        )

    def test_both_commands_default_to_not_dry_running(self) -> None:
        parser = build_parser()

        assert parser.parse_args(["memory", "distill"]).dry_run is False
        assert parser.parse_args(["memory", "distill"]).persona is None
        assert parser.parse_args(["memory", "consolidate", "--persona", "林晚"]).persona == "林晚"

    def test_the_group_shows_its_own_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        """只敲到 ``memory`` 时要打这一层的帮助，而不是顶层的。"""
        assert main(["memory"]) == 0

        shown = capsys.readouterr().out
        assert "distill" in shown
        assert "consolidate" in shown


# ────────────────────────────────────────────────────────────
# 预演
# ────────────────────────────────────────────────────────────


class TestDryRun:
    def test_it_shows_the_materials(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)

        assert main(["memory", "distill", "--dry-run"]) == 0

        shown = capsys.readouterr().out
        assert "人设      林晚（p1）" in shown
        assert "素材      5 条" in shown
        assert "这只是预演：没有调用模型，也没有写任何东西。" in shown

    def test_it_does_not_need_a_configured_provider(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """「先看看会交给模型什么、再把密钥填上」是更自然的顺序。

        这条用例钉的是一个真出现过的 bug：``--dry-run`` 曾经硬要先装配供应商，
        于是一台还没配密钥的机器连预演都跑不了。
        """
        _seed(db_path)
        monkeypatch.setattr(
            "alterego.cli_memory._memory_config", lambda: _make_config(db_path.parent, providers={})
        )

        assert main(["memory", "distill", "--dry-run"]) == 0

        assert "这只是预演" in capsys.readouterr().out

    def test_it_leaves_the_database_alone(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)

        main(["memory", "distill", "--dry-run"])

        with SqliteStorageBackend.open(db_path, read_only=True) as backend:
            assert backend.connection.scalar("SELECT COUNT(*) FROM memory") == 0
            assert (
                backend.connection.scalar(
                    "SELECT COUNT(*) FROM activity_log WHERE distilled_at IS NULL"
                )
                == 5
            )

    def test_it_prints_no_billing_line(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """一次没花钱的调用不该出现在账单语境里。"""
        _seed(db_path)

        main(["memory", "distill", "--dry-run"])

        assert "计费" not in capsys.readouterr().out


# ────────────────────────────────────────────────────────────
# 真跑
# ────────────────────────────────────────────────────────────


class TestDistill:
    def test_it_writes_memories_and_marks_the_materials(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db_path)
        provider = FakeProvider()
        _use_fake(monkeypatch, provider)

        assert _memory(db_path) == 0

        shown = capsys.readouterr().out
        assert "生成      2 条" in shown
        assert "写入      2 条" in shown
        assert "计费      [llm.routing] memory" in shown

        with SqliteStorageBackend.open(db_path, read_only=True) as backend:
            rows = backend.connection.query("SELECT * FROM memory ORDER BY id")
            assert len(rows) == 2
            assert all(row["source"] == "consolidation" for row in rows)
            assert all(row["source_ref"] for row in rows)
            assert (
                backend.connection.scalar(
                    "SELECT COUNT(*) FROM activity_log WHERE distilled_at IS NULL"
                )
                == 0
            )

    def test_it_pays_the_bill_in_the_ledger(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider())

        _memory(db_path)

        with SqliteStorageBackend.open(db_path, read_only=True) as backend:
            row = backend.connection.query_one("SELECT * FROM llm_usage")
            assert row is not None
            assert row["purpose"] == "memory"
            assert row["provider_id"] == "openai_compatible"
            assert row["prompt_tokens"] == 120
            assert row["total_tokens"] == 150

    def test_it_closes_the_provider(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``httpx.AsyncClient`` 绑在事件循环上，进程退出时会留一串未关闭连接的告警。"""
        _seed(db_path)
        provider = FakeProvider()
        _use_fake(monkeypatch, provider)

        _memory(db_path)

        assert provider.closed

    def test_it_asks_for_json(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed(db_path)
        provider = FakeProvider()
        _use_fake(monkeypatch, provider)

        _memory(db_path)

        request = provider.requests[0]
        assert request.response_format == "json"
        assert "第 0 件事" in request.prompt
        assert "林晚" in request.prompt

    def test_it_says_so_when_there_is_nothing_to_remember(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """模型回空数组是一个**结论**，不是失败——素材照样标记为处理过。"""
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider("[]"))

        assert _memory(db_path) == 0

        assert "没有值得记住的事" in capsys.readouterr().out

        with SqliteStorageBackend.open(db_path, read_only=True) as backend:
            assert backend.connection.scalar("SELECT COUNT(*) FROM memory") == 0
            assert (
                backend.connection.scalar(
                    "SELECT COUNT(*) FROM activity_log WHERE distilled_at IS NULL"
                )
                == 0
            )

    def test_it_skips_without_calling_the_model_when_there_is_barely_anything(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """两条素材归纳不出东西，而一次注定无效的调用也要付 token 的钱。"""
        _seed(db_path, activities=2)
        provider = FakeProvider()
        _use_fake(monkeypatch, provider)

        assert _memory(db_path) == 0

        assert "跳过" in capsys.readouterr().out
        assert provider.requests == []

    def test_a_broken_reply_writes_nothing(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """模型答非所问时，素材必须留在原地——否则那批事就永远丢了。"""
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider("我觉得今天还不错。"))

        assert _memory(db_path) == 2

        assert "错误：" in capsys.readouterr().err

        with SqliteStorageBackend.open(db_path, read_only=True) as backend:
            assert backend.connection.scalar("SELECT COUNT(*) FROM memory") == 0
            assert (
                backend.connection.scalar(
                    "SELECT COUNT(*) FROM activity_log WHERE distilled_at IS NULL"
                )
                == 5
            )


class TestConsolidate:
    def test_it_downgrades_what_it_learned_from(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """归纳完要把原文降权：否则同一段经历每跑一次就重来一遍。"""
        _seed(db_path, activities=0, episodic=4)
        _use_fake(monkeypatch, FakeProvider())

        assert main(["memory", "consolidate"]) == 0

        shown = capsys.readouterr().out
        assert "降权      4 条（×0.6）" in shown

        with SqliteStorageBackend.open(db_path, read_only=True) as backend:
            # 四条源记忆全部巩固过（新写入的那两条另算——它们本来就没被归纳过）。
            assert (
                backend.connection.scalar(
                    "SELECT COUNT(*) FROM memory WHERE id LIKE 'mem-%' AND consolidated_at IS NULL"
                )
                == 0
            )
            assert (
                backend.connection.scalar(
                    "SELECT COUNT(*) FROM memory WHERE id LIKE 'mem-%' AND importance = 0.36"
                )
                == 4
            )

    def test_it_only_consolidates_episodic_memories(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """semantic 是已经归纳过的东西，再归纳一遍只会越来越抽象。"""
        _seed(db_path, activities=0, episodic=3, semantic=2)
        provider = FakeProvider()
        _use_fake(monkeypatch, provider)

        main(["memory", "consolidate"])

        # semantic 不参与归纳，但它仍然算「已有的记忆」（列的是摘要），
        # 会被列出来防止重复。
        assert "一件事 4" in provider.requests[0].prompt

        with SqliteStorageBackend.open(db_path, read_only=True) as backend:
            assert (
                backend.connection.scalar(
                    "SELECT COUNT(*) FROM memory WHERE id LIKE 'mem-%'"
                    " AND consolidated_at IS NOT NULL"
                )
                == 3
            )
            assert (
                backend.connection.scalar(
                    "SELECT COUNT(*) FROM memory WHERE kind = 'semantic' AND importance = 0.6"
                )
                == 2
            )

    def test_a_second_run_finds_nothing_left(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db_path, activities=0, episodic=4)
        provider = FakeProvider()
        _use_fake(monkeypatch, provider)

        main(["memory", "consolidate"])
        capsys.readouterr()
        main(["memory", "consolidate"])

        assert "跳过" in capsys.readouterr().out
        assert len(provider.requests) == 1


# ────────────────────────────────────────────────────────────
# 停下来的时候说什么
# ────────────────────────────────────────────────────────────


class TestFailures:
    def test_no_persona_yet(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db_path, personas=())
        _use_fake(monkeypatch, FakeProvider())

        assert _memory(db_path) == 2

        printed = capsys.readouterr().err
        assert "错误：数据库里还没有人设" in printed
        assert "alterego init" in printed

    def test_several_personas_need_a_name(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """挑错人是不可挽回的：写进去的记忆会挂在另一个人身上。"""
        _seed(db_path, personas=(("p1", "林晚"), ("p2", "阿泽")))
        _use_fake(monkeypatch, FakeProvider())

        assert _memory(db_path) == 2

        printed = capsys.readouterr().err
        assert "有多个人设" in printed
        assert "林晚, 阿泽" in printed

    def test_a_name_may_be_given(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db_path, personas=(("p1", "林晚"), ("p2", "阿泽")))
        _use_fake(monkeypatch, FakeProvider())

        assert main(["memory", "distill", "--persona", "阿泽"]) == 0

        assert "阿泽（p2）" in capsys.readouterr().out

    def test_no_provider_configured(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed(db_path)
        monkeypatch.setattr(
            "alterego.cli_memory._memory_config", lambda: _make_config(db_path.parent, providers={})
        )

        assert _memory(db_path) == 2

        printed = capsys.readouterr().err
        assert "错误：配置里一个模型供应商都没有" in printed
        assert "[llm.providers.openai_compatible]" in printed

    def test_a_provider_without_a_base_url(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """猜一个默认端点会打到别人那里，而那种失败长得像网络故障。"""
        _seed(db_path)
        monkeypatch.setattr(
            "alterego.cli_memory._memory_config",
            lambda: _make_config(db_path.parent, providers={"openai_compatible": {"model": "m1"}}),
        )

        assert _memory(db_path) == 2

        printed = capsys.readouterr().err
        assert "错误：模型供应商没有配置 base_url" in printed
        assert "base_url = " in printed

    def test_a_routing_that_points_nowhere(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """拼错的档位名不该悄悄降级——那样只会在月底看到账单时才发现。"""
        _seed(db_path)
        monkeypatch.setattr(
            "alterego.cli_memory._memory_config",
            lambda: _make_config(db_path.parent, routing={"memory": "nope"}),
        )

        assert _memory(db_path) == 2

        printed = capsys.readouterr().err
        assert "错误：LLM 用途指向了不存在的档位" in printed
        assert "strong, cheap" in printed

    def test_the_message_is_not_printed_twice(
        self, db_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """曾经把 ``str(exc)`` 打出来，于是同一句话打两遍、还带着 repr 的引号。"""
        _seed(db_path, personas=())

        _memory(db_path)

        printed = capsys.readouterr().err
        assert printed.count("数据库里还没有人设") == 1
        assert "hint=" not in printed

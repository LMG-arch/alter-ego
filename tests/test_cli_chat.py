"""``alterego chat`` 的测试。

这组用例走的是**真实路径**：真配置、真 SQLite、真迁移、真提示词模板、
真的 ``ConversationService``，唯一被换掉的是模型本身。理由与
``test_cli_memory.py`` 相同——这个命令的价值在于「用户说的那句话一定落库」
「它这一轮不回也是正常结果」这类**端到端**的保证，把中间环节换成 mock
之后，测的就成了一个不存在的程序。

模型是唯一可以假的：它是一段外部服务，而且这个命令对它的要求只有
「回一段人话」。

**stdin 也是假的**，而且是必须假的：pytest 会把 ``sys.stdin`` 接管过去，
直接调 ``input()`` 会抛 ``OSError``。所以 ``_read_line`` 是一个模块级函数
（``cli_chat`` 里解释了理由），这里把它换成逐行喂数据的替身。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest

from alterego.cli import build_parser, main
from alterego.interfaces.common import HealthStatus
from alterego.interfaces.llm import LLMRequest, LLMResponse
from alterego.kernel.clock import resolve_timezone
from alterego.kernel.config import Config
from alterego.storage.sqlite import SqliteStorageBackend


TZ = resolve_timezone("Asia/Shanghai")

#: 人设与情绪记录的时间锚在「真实现在」上——命令自己取墙上时间。
STAMP = datetime.now(TZ).isoformat()

HELLO = "在看代码呢，你说"


# ── 假的模型 ────────────────────────────────────────────────


class FakeProvider:
    """按顺序回几句话。``id`` 必须与 ``[llm.routing]`` 里指到的名字一致。

    多出来的调用（复读重写那一遍）一律回**最后一句**：重写只关心
    「第二次问了什么」，给它一个固定的答案比给它一个随机的好定位。
    """

    id = "openai_compatible"
    tier = "cheap"
    models = ("m1",)

    def __init__(self, replies: Sequence[str] = (HELLO,)) -> None:
        self.replies = list(replies)
        self.requests: list[LLMRequest] = []
        self.closed = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        index = min(len(self.requests), len(self.replies) - 1)
        self.requests.append(request)
        return LLMResponse(
            text=self.replies[index],
            model="m1",
            prompt_tokens=120,
            completion_tokens=30,
        )

    async def aclose(self) -> None:
        self.closed = True

    def health_check(self) -> HealthStatus:
        return HealthStatus(ok=True, detail="fake")


# ── 夹具 ────────────────────────────────────────────────────


def _make_config(tmp_path: Path, *, with_provider: bool = True) -> Config:
    """一份指向临时目录的配置。默认带着一个可用的供应商与路由。

    ``expression`` 是路由键而不是模板名——``alterego.cli_chat.PURPOSE`` 里
    解释了这两个名字为什么不一样。
    """
    llm: dict[str, object] = {"routing": {"expression": "cheap", "cheap": "openai_compatible"}}
    if with_provider:
        llm["providers"] = {
            "openai_compatible": {
                "base_url": "https://example.test/v1",
                "api_key_env": "KEY",
                "model": "m1",
            }
        }
    return Config.load(
        path=None,
        env={},
        overrides={
            "core": {"data_dir": str(tmp_path)},
            "storage": {"backend": "sqlite"},
            "llm": llm,
        },
    )


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("alterego.cli_chat._chat_config", lambda: _make_config(tmp_path))
    return tmp_path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "alterego.db"


def _seed(
    path: Path,
    *,
    personas: Sequence[tuple[str, str]] = (("p1", "林晚"),),
    fatigue: float = 0.0,
) -> None:
    """建库、迁移，然后塞进人设（以及可选的一条疲惫情绪）。"""
    with SqliteStorageBackend.open(path) as backend:
        backend.migrate()
        for persona_id, name in personas:
            backend.connection.execute(
                "INSERT INTO persona (id, name, persona_json, created_at, updated_at)"
                " VALUES (?, ?, '{}', ?, ?)",
                (persona_id, name, STAMP, STAMP),
            )

        if not personas or fatigue <= 0:
            return
        # 累到不想说话（EXHAUSTED_FATIGUE = 0.95）时它不会回话，
        # 这是 decide_reply 的短路规则之一，也是「不回不是错误」的开始。
        backend.connection.execute(
            "INSERT INTO emotion_log"
            " (id, persona_id, valence, arousal, fatigue, label, reason, recorded_at)"
            " VALUES ('e1', ?, 0.1, 0.3, ?, '疲惫', '测试', ?)",
            (personas[0][0], fatigue, STAMP),
        )


def _use_fake(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    monkeypatch.setattr("alterego.cli_chat._providers", lambda config: {provider.id: provider})


def _read_from(monkeypatch: pytest.MonkeyPatch, lines: Sequence[str]) -> list[str]:
    """把 ``_read_line`` 换成逐行喂数据。喂完再读会抛 ``EOFError``。"""
    remaining = list(lines)
    asked: list[str] = []

    def fake(prompt: str) -> str:
        asked.append(prompt)
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    monkeypatch.setattr("alterego.cli_chat._read_line", fake)
    return asked


def _messages(path: Path) -> list[tuple[str, str]]:
    """库里的消息，按**落库顺序**：``(direction, content)``。

    按 rowid 而不是 ``created_at``：那一列的精度是秒，连续对话里三句话完全
    可能同属一秒，排序就成了不确定的。而「用户那句先落库、回话后落库」本身
    就是要验的那条保证。
    """
    with SqliteStorageBackend.open(path, read_only=True) as backend:
        rows = backend.connection.query("SELECT direction, content FROM message ORDER BY rowid")
    return [(str(row["direction"]), str(row["content"])) for row in rows]


def _conversations(path: Path) -> list[tuple[str, str, str]]:
    """库里的会话：``(id, counterpart_id, counterpart_kind)``。"""
    with SqliteStorageBackend.open(path, read_only=True) as backend:
        rows = backend.connection.query(
            "SELECT id, counterpart_id, counterpart_kind FROM conversation ORDER BY id"
        )
    return [
        (str(row["id"]), str(row["counterpart_id"]), str(row["counterpart_kind"])) for row in rows
    ]


# ────────────────────────────────────────────────────────────
# 命令行
# ────────────────────────────────────────────────────────────


class TestCommandLine:
    def test_the_command_is_registered(self) -> None:
        parser = build_parser()

        assert parser.parse_args(["chat"]).handler.__name__ == "cmd_chat"

    def test_it_defaults_to_a_continuous_conversation(self) -> None:
        """不敲 ``--once`` 就是连着聊——这是这个命令的主用法。"""
        parser = build_parser()

        assert parser.parse_args(["chat"]).once is None
        assert parser.parse_args(["chat"]).show_prompt is False
        assert parser.parse_args(["chat"]).persona is None

    def test_once_and_show_prompt_are_parsed(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["chat", "--once", "在忙什么", "--show-prompt"])

        assert args.once == "在忙什么"
        assert args.show_prompt is True

    def test_a_blank_sentence_is_rejected_by_argparse(self) -> None:
        """空白是**命令写错了**，该由 argparse 打用法提示并退 2。"""
        parser = build_parser()

        with pytest.raises(SystemExit) as caught:
            parser.parse_args(["chat", "--once", "   "])

        assert caught.value.code == 2


# ────────────────────────────────────────────────────────────
# 单句
# ────────────────────────────────────────────────────────────


class TestOneShot:
    def test_the_header_comes_first(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider())

        assert main(["chat", "--once", "在忙什么"]) == 0

        shown = capsys.readouterr().out
        assert "人设      林晚（p1）" in shown
        assert "时刻      " in shown
        assert "[llm.routing] expression" in shown

    def test_the_user_message_is_stored_verbatim(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider())

        main(["chat", "--once", "帮我看看这个报错"])

        assert _messages(db_path)[0] == ("inbound", "帮我看看这个报错")

    def test_the_reply_is_stored_as_outbound(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider())

        main(["chat", "--once", "在忙什么"])

        assert _messages(db_path) == [("inbound", "在忙什么"), ("outbound", HELLO)]

    def test_the_conversation_is_the_user_one(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """会话 id 必须与 tick 引擎用的是同一个，否则两条路径会各聊各的。"""
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider())

        main(["chat", "--once", "在忙什么"])

        assert _conversations(db_path) == [("p1:user", "user", "user")]

    def test_the_sentence_and_the_reply_are_echoed(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider())

        main(["chat", "--once", "在忙什么"])

        shown = capsys.readouterr().out
        assert "你        在忙什么" in shown
        assert f"它        {HELLO}" in shown

    def test_the_pace_and_the_reason_are_shown(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """「它没秒回」这件事必须有能读的解释，否则用户只能猜它是不是坏了。"""
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider())

        main(["chat", "--once", "在忙什么"])

        shown = capsys.readouterr().out
        assert "节奏      " in shown
        assert "秒回" in shown or "正常" in shown
        assert "送出" in shown

    def test_the_prompt_can_be_shown(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)
        provider = FakeProvider()
        _use_fake(monkeypatch, provider)

        main(["chat", "--once", "在忙什么", "--show-prompt"])

        shown = capsys.readouterr().out
        assert "在忙什么" in shown
        # 打出来的提示词就是**发给模型的那一份**，不是重新渲染的一份。
        assert provider.requests[0].prompt in shown

    def test_the_prompt_is_hidden_by_default(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)
        provider = FakeProvider()
        _use_fake(monkeypatch, provider)

        main(["chat", "--once", "在忙什么"])

        assert provider.requests[0].prompt not in capsys.readouterr().out

    def test_the_provider_is_closed(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """不显式 aclose()，进程退出时会留下一串未关闭连接的告警。"""
        _seed(db_path)
        provider = FakeProvider()
        _use_fake(monkeypatch, provider)

        assert main(["chat", "--once", "在忙什么"]) == 0
        assert provider.closed is True


# ────────────────────────────────────────────────────────────
# 连续对话
# ────────────────────────────────────────────────────────────


class TestContinuous:
    def test_it_keeps_going_until_q(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider(["一", "二", "三"]))
        _read_from(monkeypatch, ["第一句", "第二句", "第三句", ":q"])

        assert main(["chat"]) == 0

        directions = [direction for direction, _ in _messages(db_path)]
        assert directions == ["inbound", "outbound"] * 3

    def test_eof_ends_the_loop(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Ctrl+Z / Ctrl+D 都只表示「不聊了」，不是错误。"""
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider())
        _read_from(monkeypatch, ["就一句"])

        assert main(["chat"]) == 0
        assert len(_messages(db_path)) == 2

    def test_a_blank_line_is_not_sent(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """只按回车不该变成「跟她说了一句空话」。"""
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider())
        _read_from(monkeypatch, ["", "  ", "在吗", ":q"])

        main(["chat"])

        assert _messages(db_path)[0] == ("inbound", "在吗")

    def test_every_turn_shares_one_conversation(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """每句新开一个事件循环会让第二句不再记得第一句。"""
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider(["一", "二", "三"]))
        _read_from(monkeypatch, ["第一句", "第二句", "第三句", ":q"])

        main(["chat"])

        assert len(_conversations(db_path)) == 1
        assert len(_messages(db_path)) == 6

    def test_two_lines_in_the_same_second_both_survive(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """库里按 ``message_id`` 去重，两句话落在同一秒里不能共用一个号。

        手工敲键盘很容易连着一秒说完两句，而 ``created_at`` 只精确到秒——
        这条用例存在是因为真出现过：三句只留下一句。
        """
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider(["一", "二", "三"]))
        _read_from(monkeypatch, ["甲", "乙", "丙", ":q"])

        main(["chat"])

        inbound = [content for direction, content in _messages(db_path) if direction == "inbound"]
        assert inbound == ["甲", "乙", "丙"]

    def test_the_turn_count_is_reported(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider(["一", "二"]))
        _read_from(monkeypatch, ["第一句", "第二句", ":q"])

        main(["chat"])

        assert "这次说了 2 句。" in capsys.readouterr().out

    def test_the_hint_is_shown_only_in_continuous_mode(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider())
        _read_from(monkeypatch, [":q"])

        main(["chat"])

        assert ":q 或 Ctrl+D 结束" in capsys.readouterr().out

    def test_later_turns_see_the_earlier_ones(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """第二句的提示词里必须出现第一句——同一个会话，同一份历史。"""
        _seed(db_path)
        provider = FakeProvider(["一", "二"])
        _use_fake(monkeypatch, provider)
        _read_from(monkeypatch, ["第一句说了这个", "第二句", ":q"])

        main(["chat"])

        assert len(provider.requests) == 2
        assert "第一句说了这个" in provider.requests[1].prompt


# ────────────────────────────────────────────────────────────
# 「这一轮不回」是结果，不是错误
# ────────────────────────────────────────────────────────────


class TestSilence:
    def test_a_tired_persona_stays_quiet(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path, fatigue=0.99)
        provider = FakeProvider()
        _use_fake(monkeypatch, provider)

        assert main(["chat", "--once", "在忙什么"]) == 0

        shown = capsys.readouterr().out
        assert "（这一轮不回）" in shown
        assert "不想说话" in shown

    def test_being_quiet_costs_nothing(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """被规则拦下时**一次模型调用都不该花**（P3：机制约束优于提示词祈祷）。"""
        _seed(db_path, fatigue=0.99)
        provider = FakeProvider()
        _use_fake(monkeypatch, provider)

        main(["chat", "--once", "在忙什么"])

        assert provider.requests == []

    def test_but_what_the_user_said_is_still_kept(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """入站先落库——它不回话，也不能把用户说过的话弄丢。"""
        _seed(db_path, fatigue=0.99)
        _use_fake(monkeypatch, FakeProvider())

        main(["chat", "--once", "在忙什么"])

        assert _messages(db_path) == [("inbound", "在忙什么")]


# ────────────────────────────────────────────────────────────
# 出错的时候
# ────────────────────────────────────────────────────────────


class TestFailures:
    def test_a_missing_provider_is_explained(
        self,
        db_path: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(
            "alterego.cli_chat._chat_config", lambda: _make_config(tmp_path, with_provider=False)
        )
        _seed(db_path)

        assert main(["chat", "--once", "在忙什么"]) == 2

        assert "配置里一个模型供应商都没有" in capsys.readouterr().err

    def test_a_missing_persona_is_explained(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path, personas=())
        _use_fake(monkeypatch, FakeProvider())

        assert main(["chat", "--once", "在忙什么"]) == 2

        assert "数据库里还没有人设" in capsys.readouterr().err

    def test_several_personas_ask_you_to_pick(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """随便挑一个等于随机改别人的数据，宁可不做。"""
        _seed(db_path, personas=(("p1", "林晚"), ("p2", "沈亦")))
        _use_fake(monkeypatch, FakeProvider())

        assert main(["chat", "--once", "在忙什么"]) == 2

        assert "有多个人设" in capsys.readouterr().err

    def test_persona_picks_by_name(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path, personas=(("p1", "林晚"), ("p2", "沈亦")))
        _use_fake(monkeypatch, FakeProvider())

        assert main(["chat", "--once", "在吗", "--persona", "沈亦"]) == 0

        assert _conversations(db_path) == [("p2:user", "user", "user")]
        assert "沈亦（p2）" in capsys.readouterr().out

    def test_an_unknown_persona_is_explained(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed(db_path)
        _use_fake(monkeypatch, FakeProvider())

        assert main(["chat", "--once", "在吗", "--persona", "查无此人"]) == 2

        assert "没有叫这个名字的人设" in capsys.readouterr().err

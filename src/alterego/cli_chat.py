"""``alterego chat`` —— 跟它说话。

**这是第四个组装根**，与 ``cli.py`` / ``cli_db.py`` / ``cli_memory.py`` /
``cli_study.py`` / ``cli_vault.py`` / ``cli_dataset.py`` 并列：只有这几个文件
知道「存储用的是 SQLite」「模型走的是哪个供应商」，其余代码一律只认
``StorageBackend`` / ``ConversationRepository`` / ``LLMGateway`` 这些协议。
``scripts/check_architecture.sh`` 第 3 组红线把这件事钉住了。

**为什么它不走 tick。** ``04-simulation-loop.md`` § 3.3 给 ``reply`` 意图在有
未读消息时的权重是 1.0，那是**主动行为的调度**；而人在这里打完字等着回话，
等不了一个 tick（``realtime`` 模式下 5 虚拟分钟 ≈ 真的 5 分钟）。
两条路径共用同一套纯函数（``domain.conversation.decide_reply`` 决定秒回 /
正常 / 忙完再回），差别只在**触发时机**：tick 决定「它想不想主动找你」，
这里决定「你说了话它怎么回」。

**这里不真的睡觉。** ``decide_reply`` 算出来的是「这一句该在几秒后送出」，
命令把它打出来、落进库，然后**立刻**返回——真的 ``asyncio.sleep`` 会让一次
对话要几分钟才结束，而那个延迟的真实含义是「它现在没空，等一下回」，
不是一个必须在这里等完的定时器（真延后由调度器负责，见
``04-simulation-loop.md`` § 5）。

依据: docs/design/12-calendar-and-conversation.md § 10–12、
docs/plans/2026-09-16-main-body.md § 4（批次 A）
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Final

from alterego.cli_db import _open_db, _require_sqlite, _resolve_persona
from alterego.cli_io import _RULE, _out, _pad
from alterego.cli_memory import _fail, _providers, _registry
from alterego.interfaces.repository import PersonaRecord
from alterego.kernel.clock import RealClock, resolve_timezone
from alterego.kernel.config import Config
from alterego.kernel.errors import AlterEgoError
from alterego.kernel.logging import get_logger
from alterego.llm import LLMGateway, PromptLibrary
from alterego.llm.providers import OpenAICompatibleProvider
from alterego.sim.conversation import ConversationService, InboundMessage, ReplyOutcome
from alterego.sim.engine import EnginePorts
from alterego.sim.persona_view import PersonaView
from alterego.storage.sqlite import (
    SqliteActivityRepository,
    SqliteBudgetRepository,
    SqliteConversationRepository,
    SqliteEmotionRepository,
    SqliteMemoryRepository,
    SqlitePersonaRepository,
    SqliteScheduleRepository,
    SqliteSocialPostRepository,
    SqliteStorageBackend,
    SqliteTickLogRepository,
    SqliteUsageRepository,
)


__all__ = ["add_chat_parser", "cmd_chat"]

_log = get_logger("cli.chat")

#: 回话走的是 ``[llm.routing]`` 里的哪一个用途。
#:
#: **是 ``expression`` 而不是 ``chat_reply``。** 后者是提示词**模板**的名字
#: （``prompts/chat_reply.md``），前者是路由表里的**用途**键。两者名字不一样，
#: 是因为同一个用途可以换模板（改文案不改路由），而同一个模板也可能被两个
#: 用途用上。把它们当成一件事会写出「改了 [llm.routing] 却还是老样子」。
PURPOSE: Final[str] = "expression"

#: 列标签占的**显示宽度**。与 ``cli_memory`` 的表头对齐（中文一个字占两列）。
_LABEL: Final[int] = 10

_PROMPT: Final[str] = "你  > "

#: 输入这几句就结束。带冒号前缀是为了不跟「对她说一句 exit」撞车。
_QUIT: Final[frozenset[str]] = frozenset({":q", ":quit", ":exit"})

_HINT: Final[str] = "直接打字就是跟它说话；只按回车不发出去，:q 或 Ctrl+D 结束。"

_MODE_LABELS: Final[dict[str, str]] = {
    "instant": "秒回",
    "normal": "正常",
    "delayed": "晚点回",
    "much_later": "忙完再回",
    "silent": "不回",
}
"""``ReplyMode`` 五个取值的中文说法。

``delayed`` 现在没有一条规则会产生它（``decide_reply`` 走的是 ``much_later``），
但仍然列出来：漏掉一个键的后果是界面上突然冒出一串英文标识符，
而那种场面只在某天新增一条规则时才会出现——那时没人会记得回来补这张表。
"""


# ── 配置与装配 ──────────────────────────────────────────────


def _chat_config() -> Config:
    """读配置。单独一个函数，是为了给测试一个能换掉的接口。"""
    return Config.load()


def _ports(backend: SqliteStorageBackend) -> EnginePorts:
    """引擎要的十个出口，一次装齐。

    十个仓储全部来自 ``storage/sqlite``。这是组装根本来就该干的事
    （红线第 3 组留的例外），而不是「顺手多写了几行」：少了任何一个，
    ``read_snapshot`` 都会在读快照时炸，而它读的是「它现在什么心情、
    在干嘛、有没有人在等」——缺一个就没法回答。
    """
    conn = backend.connection
    return EnginePorts(
        backend=backend,
        personas=SqlitePersonaRepository(conn),
        emotions=SqliteEmotionRepository(conn),
        schedules=SqliteScheduleRepository(conn),
        memories=SqliteMemoryRepository(conn),
        activities=SqliteActivityRepository(conn),
        conversations=SqliteConversationRepository(conn),
        budgets=SqliteBudgetRepository(conn),
        posts=SqliteSocialPostRepository(conn),
        tick_logs=SqliteTickLogRepository(conn),
    )


def _persona_view(
    config: Config, backend: SqliteStorageBackend, record: PersonaRecord
) -> PersonaView:
    """把「表里那几列」与「文档里那些自由字段」拼成一份视图。

    文档读不到是**正常情况**（人设文档还没生成过），这时它仍然能说话，
    只是语气、口头禅那些全是兜底值——``PersonaView.build`` 负责这件事，
    这里不重复一份兜底逻辑。
    """
    document = SqlitePersonaRepository(backend.connection).document(record.id)
    return PersonaView.build(record, user_name=config.core.user_name, document=document)


def _service(
    config: Config,
    backend: SqliteStorageBackend,
    providers: Mapping[str, OpenAICompatibleProvider],
    *,
    persona: PersonaRecord,
) -> ConversationService:
    """把一次对话要用的东西全装上。

    这里用 :class:`~alterego.kernel.clock.RealClock` 而**不是**虚拟时钟：
    手动敲命令时「现在」就是现在，而 ``sim/`` 里那些禁止取墙上时间的红线
    管的是推演循环（那里要用 ``ctx.clock.virtual_now()`` 才能被快进）——
    组装根取墙上时间是对的，理由同 ``cli_memory._now``。

    用量账本（``SqliteUsageRepository``）挂在这里而不是网关内部：网关只认识
    ``UsageSink`` 这个形状，它不知道账记在 SQLite 里（红线第 4 组）。
    """
    return ConversationService(
        persona=_persona_view(config, backend, persona),
        ports=_ports(backend),
        clock=RealClock(resolve_timezone(config.core.timezone)),
        prompts=PromptLibrary(),
        budget=config.disturb_budget,
        gateway=LLMGateway(
            _registry(providers),
            config.llm.routing,
            max_retries=config.llm.max_retries,
            usage_sink=SqliteUsageRepository(backend.connection, persona_id=persona.id),
            logger=_log,
        ),
        new_id=lambda: uuid.uuid4().hex,
    )


async def _drive(
    service: ConversationService,
    providers: Mapping[str, OpenAICompatibleProvider],
    *,
    once: str | None,
    show_prompt: bool,
) -> int:
    """跑对话，无论如何都把网络连接收掉（理由同 ``cli_memory._drive``）。"""
    try:
        if once is not None:
            await _turn(service, once, show_prompt=show_prompt)
            return 0
        return await _repl(service, show_prompt=show_prompt)
    finally:
        for provider in providers.values():
            await provider.aclose()


# ── 输出 ────────────────────────────────────────────────────


def _pace(outcome: ReplyOutcome) -> str:
    """这一句什么时候送出去、为什么。

    延迟是**算出来给用户看的**，不是等出来的。把 ``reason`` 一起打出来，
    是为了让「它没秒回」这件事有个能读的解释——否则用户只能猜它是不是坏了。
    """
    mode = _MODE_LABELS.get(outcome.decision.mode, outcome.decision.mode)
    seconds = outcome.delay_seconds
    when = "马上" if seconds < 1.0 else f"{seconds:.0f} 秒后"
    return f"{mode} · {when}送出 · {outcome.decision.reason}"


def _report(outcome: ReplyOutcome, *, said: str, show_prompt: bool) -> None:
    """把一轮对话摊开给用户看。"""
    _out(f"{_pad('你', _LABEL)}{said}")
    if outcome.replied:
        _out(f"{_pad('它', _LABEL)}{outcome.text}")
    else:
        # 「这一轮不回」是一个正常结果，不是一个错误（见 ReplyOutcome 的文档）。
        _out(f"{_pad('它', _LABEL)}（这一轮不回）")
    _out(f"{_pad('节奏', _LABEL)}{_pace(outcome)}")
    if outcome.repeated_from:
        _out(f"{_pad('重写', _LABEL)}它上一轮说过「{outcome.repeated_from}」，这一句换了个说法")
    if outcome.topic is not None and outcome.topic.open_topic:
        _out(f"{_pad('主动', _LABEL)}这一句它自己带了个新话题")
    if show_prompt:
        _out(_RULE)
        _out(outcome.prompt)
    _out("")


def _header(persona: PersonaRecord, now: datetime) -> None:
    """开场那三行。**先打再聊**——`--once` 与连续对话都要看得到它。"""
    _out(f"{_pad('人设', _LABEL)}{persona.name}（{persona.id}）")
    _out(f"{_pad('时刻', _LABEL)}{now:%Y-%m-%d %H:%M}")
    _out(f"{_pad('计费', _LABEL)}[llm.routing] {PURPOSE}（账记在 llm_usage 表）")
    _out(_RULE)


async def _turn(service: ConversationService, text: str, *, show_prompt: bool) -> ReplyOutcome:
    """说一句，看它怎么回。

    ``message_id`` 由命令行自己发：库里按 id 去重，而两句话完全可能落在
    同一秒里（连续对话就是按得快）。碰撞的代价是**第一句被当成重发丢掉**，
    所以这里宁可自己造一个不可能重的号，也不留给下层的降级规则。
    """
    inbound = InboundMessage(content=text, channel="cli", message_id=f"cli-{uuid.uuid4().hex}")
    outcome = await service.reply(inbound)
    _report(outcome, said=text, show_prompt=show_prompt)
    return outcome


def _read_line(prompt: str) -> str:
    """读一行输入。

    单独一个函数是为了给测试一个能换掉的接口（同 ``_chat_config`` 的道理）：
    ``input()`` 一旦写死在循环体里，「连着聊三句仍然共享同一个会话」这件事就没法测，
    而 pytest 会把 stdin 接过去、让直接调 ``input()`` 抛 ``OSError``。
    """
    return input(prompt)


async def _repl(service: ConversationService, *, show_prompt: bool) -> int:
    """连续对话，直到 EOF 或 ``:q``。

    **同一个事件循环、同一个 service、同一个会话 id。** 每句都新开一个
    ``asyncio.run`` 会让供应商的 ``httpx.AsyncClient`` 绑到一个已经关掉的循环上，
    而那样失败得很难看（一个跟「连接池属于别的循环」有关的怪错），
    且第二句就不再记得第一句了。
    """
    turns = 0
    while True:
        try:
            line = _read_line(_PROMPT)
        except (EOFError, KeyboardInterrupt):
            # Ctrl+Z / Ctrl+D / Ctrl+C 在这个位置都只表示「不聊了」，不是错误。
            _out("")
            break
        text = line.strip()
        if not text:
            continue
        if text in _QUIT:
            break
        await _turn(service, text, show_prompt=show_prompt)
        turns += 1
    _out(f"{_pad('结束', _LABEL)}这次说了 {turns} 句。")
    return 0


# ── 命令 ────────────────────────────────────────────────────


def _utterance(text: str) -> str:
    """``--once`` 的那句话。空白串在这里就拦下。

    用 argparse 的 ``type`` 而不是在 ``_run`` 里手写分支：空白参数是**命令
    写错了**，不是运行期故障；让 argparse 打用法提示并退 2，
    好过让它进到「跟她说了一句空话」那条路上。
    """
    if not text.strip():
        raise argparse.ArgumentTypeError("要说的话不能是空白")
    return text


def _run(args: argparse.Namespace) -> int:
    """装配 → 聊 → 报告。"""
    config = _chat_config()
    _require_sqlite(config)
    clock = RealClock(resolve_timezone(config.core.timezone))
    once: str | None = args.once

    try:
        # 与 `cli_memory` 不同，这里没有 `--dry-run`：不调模型的一句话
        # 什么都不是，它既不是预演也不是结果。要看提示词请用 `--show-prompt`。
        providers = _providers(config)
        with _open_db(config) as backend:
            persona = _resolve_persona(backend.connection, name=args.persona)
            service = _service(config, backend, providers, persona=persona)
            _header(persona, clock.virtual_now())
            if once is None:
                _out(_HINT)
            return asyncio.run(
                _drive(service, providers, once=once, show_prompt=bool(args.show_prompt))
            )
    except AlterEgoError as exc:
        return _fail(exc)


def cmd_chat(args: argparse.Namespace) -> int:
    """``alterego chat``。"""
    return _run(args)


def add_chat_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """把 ``chat`` 命令挂到顶层子命令上。

    **它没有子命令层**：``chat`` 本身就是一个动作。「说话」这件事没有什么
    可分的几类，硬造一层 ``alterego chat say`` 只会让人多敲一次回车。
    """
    chat_parser = commands.add_parser(
        "chat",
        help="说话：跟它聊一句，不敲 --once 就是连续对话",
    )
    chat_parser.add_argument(
        "--once",
        default=None,
        metavar="文本",
        type=_utterance,
        help="只说这一句就退出（适合管道与脚本；不给就是连续对话）",
    )
    chat_parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="把这一句用到的提示词摊开给你看",
    )
    chat_parser.add_argument(
        "--persona",
        default=None,
        metavar="NAME",
        help="跟哪个人设说话（默认：库里只有一个时自动选它）",
    )
    chat_parser.set_defaults(handler=cmd_chat)

"""``channels.web.sse`` 的测试：广播、丢帧、心跳、收摊。

这一层是 SSE 的**全部形状**（帧格式 / 队列 / 上限 / 心跳间隔），而它一行
FastAPI 都不依赖，所以可以纯用事件循环测透——不需要起服务器，也就不需要
一个「监听在随机端口上」的夹具。测试跑得快，失败时看到的是队列里到底
有几帧，而不是一条 HTTP 超时。

依据: docs/design/05-channels.md § 3.3
"""

from __future__ import annotations

import json

import pytest

from alterego.channels.web.sse import (
    CLOSE,
    KEEPALIVE,
    SSEBroker,
    Subscription,
    TooManySubscribers,
    sse_pack,
)


# ── 帧格式 ──────────────────────────────────────────────────


class TestFrameFormat:
    def test_a_frame_is_event_then_data_then_a_blank_line(self) -> None:
        """SSE 的帧必须用空行结束，否则浏览器不会把它交出去。"""
        frame = sse_pack("message", {"content": "在呢"})

        assert frame == 'event: message\ndata: {"content": "在呢"}\n\n'

    def test_chinese_is_not_escaped(self) -> None:
        """中文按原样出去：抓包看得懂，而且一个汉字 3 字节比 ``\\uXXXX`` 的 6 字节小。"""
        assert "在呢" in sse_pack("message", {"content": "在呢"})
        assert "\\u" not in sse_pack("message", {"content": "在呢"})

    def test_the_payload_is_one_json_object_on_one_line(self) -> None:
        """``data:`` 后面只能有一行——里面有换行的话浏览器会把后半截当成另一帧。"""
        frame = sse_pack("message", {"content": "第一行\n第二行"})
        data_line = frame.splitlines()[1]

        assert data_line.startswith("data: ")
        assert "\n" not in data_line
        assert json.loads(data_line[len("data: ") :])["content"] == "第一行\n第二行"

    def test_a_value_the_encoder_does_not_know_is_stringified(self) -> None:
        """``default=str``：日期之类的值不该让整帧发不出去。"""
        frame = sse_pack("x", {"when": object()})

        assert "object object" in frame

    def test_the_keepalive_is_a_comment_frame(self) -> None:
        """冒号开头是 SSE 注释，浏览器丢掉它，但代理看得见「连接还在动」。"""
        assert KEEPALIVE.startswith(":")
        assert KEEPALIVE.endswith("\n\n")


# ── 订阅 ────────────────────────────────────────────────────


class TestSubscribe:
    def test_subscribing_remembers_you(self) -> None:
        broker = SSEBroker()

        sub = broker.subscribe()

        assert isinstance(sub, Subscription)
        assert broker.subscribers == 1

    async def test_an_event_reaches_every_subscriber(self) -> None:
        broker = SSEBroker()
        subs = [broker.subscribe() for _ in range(3)]

        delivered = broker.broadcast("message", {"content": "早"})

        assert delivered == 3
        for sub in subs:
            assert await sub.next_frame(0.1) == sse_pack("message", {"content": "早"})

    def test_nobody_listening_is_not_an_error(self) -> None:
        """没人连着时 ``broadcast`` 返回 0。这个数在排障时是一条线索，不是错误。"""
        broker = SSEBroker()

        assert broker.broadcast("message", {"content": "早"}) == 0

    def test_the_connection_cap_is_enforced(self) -> None:
        """上限是配置项而不是「应该不会太多」：每个订阅者都是一个被服务器持有的队列。"""
        broker = SSEBroker(max_connections=2)
        broker.subscribe()
        broker.subscribe()

        with pytest.raises(TooManySubscribers) as caught:
            broker.subscribe()

        assert caught.value.context["limit"] == 2
        assert "sse_max_connections" in caught.value.context["hint"]

    def test_unsubscribing_frees_a_slot(self) -> None:
        broker = SSEBroker(max_connections=1)
        sub = broker.subscribe()

        assert broker.unsubscribe(sub) is True
        assert broker.subscribers == 0
        assert broker.subscribe() is not None

    def test_unsubscribing_twice_is_not_an_error(self) -> None:
        """断线、异常、超时三条路径都会走到退订，所以它必须幂等。"""
        broker = SSEBroker()
        sub = broker.subscribe()
        broker.unsubscribe(sub)

        assert broker.unsubscribe(sub) is False


# ── 丢帧 ────────────────────────────────────────────────────


class TestDropping:
    def test_a_full_queue_drops_the_new_frame_and_counts_it(self) -> None:
        """队列满意味着这个页面被切到后台、事件循环被节流，而它自己不知道。

        此时丢掉新帧比无限缓存（内存长在服务器上）或断开连接（切回来一片空白）
        都合理——消息本来就在库里，页面重新拉一次历史就补齐了。
        """
        broker = SSEBroker(queue_size=1)
        broker.subscribe()

        assert broker.broadcast("message", {"n": 1}) == 1
        assert broker.broadcast("message", {"n": 2}) == 0
        assert broker.dropped == 1

    def test_dropped_sums_across_subscribers(self) -> None:
        broker = SSEBroker(queue_size=1)
        first = broker.subscribe()
        broker.subscribe()

        broker.broadcast("message", {"n": 1})
        broker.broadcast("message", {"n": 2})

        # 两个订阅者各丢一帧（各自的第一帧都进了队列）。
        assert first.dropped == 1
        assert broker.dropped == 2


# ── 等待 ────────────────────────────────────────────────────


class TestNextFrame:
    async def test_a_timeout_returns_none_rather_than_raising(self) -> None:
        """超时不是错误：调用方据此发一条心跳，连接继续。"""
        sub = Subscription()

        assert await sub.next_frame(0.01) is None

    async def test_the_close_marker_is_its_own_frame(self) -> None:
        """``CLOSE`` 不是合法 SSE 帧，所以浏览器永远不会把它当成一条消息。"""
        broker = SSEBroker()
        sub = broker.subscribe()

        broker.close_all()

        assert await sub.next_frame(0.1) == CLOSE
        assert broker.subscribers == 0

    async def test_closing_a_full_queue_still_gets_the_marker_in(self) -> None:
        """队列满时挤掉一条旧帧也要把结束信号放进去——那条旧帧重新拉历史就回来了。"""
        broker = SSEBroker(queue_size=1)
        sub = broker.subscribe()
        broker.broadcast("message", {"n": 1})

        broker.close_all()

        assert await sub.next_frame(0.1) == CLOSE
        assert sub.dropped == 1

    async def test_pending_reports_what_is_still_queued(self) -> None:
        sub = Subscription()
        sub.offer("a")

        assert sub.pending == 1
        assert await sub.next_frame(0.1) == "a"
        assert sub.pending == 0


# ── 配置与日志 ──────────────────────────────────────────────


class TestWiring:
    def test_the_keepalive_interval_comes_from_config(self) -> None:
        """路由按这个数去等下一帧，所以它必须能从外面读出来。"""
        assert SSEBroker(keepalive_seconds=7.5).keepalive_seconds == 7.5
        assert SSEBroker(max_connections=3).max_connections == 3

    def test_a_logger_can_be_passed_in(self) -> None:
        import logging

        broker = SSEBroker(logger=logging.getLogger("test.web.sse"))
        broker.subscribe()

        assert broker.subscribers == 1


def test_the_package_does_not_need_fastapi_to_be_imported() -> None:
    """帧格式、订阅、丢帧必须能在**没装 fastapi** 的机器上被测透。

    这里直接读源码而不是查 ``sys.modules``：pytest 一个进程里跑完所有测试，
    别的测试 import 过 fastapi 之后，``sys.modules`` 里就有了，而那个断言
    永远为真（也就是永远不测任何东西）。
    """
    from pathlib import Path

    import alterego.channels.web.sse as module

    source = Path(module.__file__).read_text(encoding="utf-8")

    assert "fastapi" not in source

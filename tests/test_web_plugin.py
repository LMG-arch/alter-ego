"""``channels.web.plugin`` 的测试：浏览器渠道本身。

它是渠道、不是服务器，所以这里一行 HTTP 都不需要：出站推给 SSE 广播，
入站交给 ``on_receive`` 装上的处理器。两条都能用普通函数调出来验。

**「没人连着也算发送成功」是这里最重要的一条。** 消息本身已经落库（那是
引擎的事），渠道只负责「此刻在看的页面能不能立刻看到」。把「三个页面都关了」
判成失败，会让引擎去重试一件已经做完的工作。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from alterego.channels.web.plugin import (
    CHANNEL_ID,
    OUTBOUND_EVENT,
    WebChannel,
    outbound_payload,
)
from alterego.channels.web.sse import SSEBroker
from alterego.interfaces.channel import InboundMessage, OutboundMessage
from alterego.kernel.clock import resolve_timezone


TZ = resolve_timezone("Asia/Shanghai")
NOW = datetime(2026, 9, 16, 20, 30, tzinfo=TZ)


def _out(content: str = "在呢", **kwargs: Any) -> OutboundMessage:
    return OutboundMessage(kind="text", content=content, **kwargs)


def _in(content: str = "在忙什么") -> InboundMessage:
    return InboundMessage(
        channel_id=CHANNEL_ID,
        sender_id="user",
        sender_name="我",
        content=content,
        received_at=NOW,
    )


# ── 身份 ────────────────────────────────────────────────────


class TestIdentity:
    def test_the_id_is_web_not_the_plugin_id(self) -> None:
        """``Channel.id`` 是路由里那个名字，不是 ``channel.web``（插件 id）。

        两者现在长得像、含义完全不同：混用会让 ``[routing]`` 在换实现时立刻出错。
        """
        channel = WebChannel(SSEBroker())

        assert channel.id == CHANNEL_ID == "web"
        assert not channel.id.startswith("channel.")

    def test_it_is_bidirectional(self) -> None:
        """它有 ``"in"``：页面上的 ``POST`` 就是从这条渠道进来的。"""
        channel = WebChannel(SSEBroker())

        assert channel.direction == frozenset({"in", "out"})
        assert "text" in channel.capabilities


# ── 出站 ────────────────────────────────────────────────────


class TestSend:
    async def test_a_message_reaches_a_connected_page(self) -> None:
        broker = SSEBroker()
        sub = broker.subscribe()
        channel = WebChannel(broker)

        result = await channel.send(_out("早"))

        assert result.ok is True
        assert OUTBOUND_EVENT in (await sub.next_frame(0.1) or "")

    async def test_nobody_listening_is_still_a_success(self) -> None:
        """否则引擎会去重试一件已经做完的工作（消息早就落库了）。"""
        channel = WebChannel(SSEBroker())

        result = await channel.send(_out())

        assert result.ok is True
        assert channel.sent == 1

    async def test_sending_after_close_fails_but_is_not_retryable(self) -> None:
        """服务都要停了，重试没有意义——这是 ``retryable=False`` 的用法。"""
        channel = WebChannel(SSEBroker())
        await channel.aclose()

        result = await channel.send(_out())

        assert result.ok is False
        assert result.retryable is False
        assert result.error is not None

    async def test_the_count_goes_up(self) -> None:
        channel = WebChannel(SSEBroker())

        await channel.send(_out())
        await channel.send(_out())

        assert channel.sent == 2


class TestOutboundPayload:
    def test_only_fields_the_frontend_uses_go_out(self) -> None:
        """``metadata`` 装的是引擎内部的东西（渠道选项、重试次数），不该进浏览器。"""
        payload = outbound_payload(_out("在呢", metadata={"retries": 3}))

        assert payload["content"] == "在呢"
        assert payload["kind"] == "text"
        assert "metadata" not in payload

    def test_an_image_gives_a_file_name_not_a_path(self) -> None:
        """绝对路径会泄漏本机目录结构，而图片本身要走带鉴权的入口。"""
        payload = outbound_payload(_out("看这张", image_path=Path("/home/me/data/selfie/1.png")))

        assert payload["image"] == "1.png"
        assert "/home/me" not in str(payload)

    def test_a_message_without_an_image_has_no_image_key(self) -> None:
        assert "image" not in outbound_payload(_out())

    def test_the_json_is_plain_data(self) -> None:
        """摊平的结果要能直接交给 ``json.dumps``——``sse_pack`` 就是这么用的。"""
        payload = outbound_payload(_out("在呢", mention=("u1",), mention_all=True))

        assert json.loads(json.dumps(payload, ensure_ascii=False))["mention"] == ["u1"]
        assert payload["mention_all"] is True


# ── 入站 ────────────────────────────────────────────────────


class TestReceive:
    def test_a_message_without_a_handler_reports_nobody_took_it(self) -> None:
        """返回 ``False`` 时路由会答 503：那是**引擎没装配**，不是这条消息有问题。"""
        channel = WebChannel(SSEBroker())

        assert channel.deliver(_in()) is False
        assert channel.received == 1

    def test_a_handler_gets_the_message(self) -> None:
        channel = WebChannel(SSEBroker())
        seen: list[InboundMessage] = []
        channel.on_receive(seen.append)

        assert channel.deliver(_in("在吗")) is True
        assert [message.content for message in seen] == ["在吗"]

    def test_on_receive_does_not_raise_for_this_channel(self) -> None:
        """抛 ``NotImplementedError`` 那条约定是留给单向渠道的（webhook 收不了）。"""
        channel = WebChannel(SSEBroker())

        channel.on_receive(lambda message: None)

        assert channel.deliver(_in()) is True


# ── 生命周期与健康 ──────────────────────────────────────────


class TestLifecycle:
    async def test_closing_ends_the_connections(self) -> None:
        """服务都停了还让页面以为「已连接」，比直接断开更难查。"""
        broker = SSEBroker()
        sub = broker.subscribe()
        channel = WebChannel(broker)

        await channel.aclose()

        assert await sub.next_frame(0.1) == ""
        assert broker.subscribers == 0

    def test_health_reports_the_counts(self) -> None:
        channel = WebChannel(SSEBroker())
        channel.deliver(_in())

        status = channel.health_check()

        assert status.ok is True
        assert "1" in status.detail

    async def test_health_after_close_points_at_the_restart(self) -> None:
        """``hint`` 要说下一步做什么，「已关闭」本身不是一条可执行的提示。"""
        channel = WebChannel(SSEBroker())

        await channel.aclose()

        status = channel.health_check()
        assert status.ok is False
        assert status.hint is not None
        assert "serve" in status.hint


@pytest.mark.parametrize("content", ["", "很长的一段话" * 100])
def test_any_content_is_delivered_as_is(content: str) -> None:
    """渠道不判内容——长度与合法性是引擎和路由的事，管子只管递字节。"""
    channel = WebChannel(SSEBroker())
    seen: list[str] = []
    channel.on_receive(lambda message: seen.append(message.content))

    channel.deliver(_in(content))

    assert seen == [content]

"""渠道层：Agent 与外界之间的那条管子。

「来消息了」和「发消息出去」都从这里经过。
**本层不做表达决策**——说什么、什么时候说，由推演层决定；
渠道只负责把 `OutboundMessage` 送到正确的地方，并把送达结果如实回报。

⚠️ 企业微信群机器人与钉钉自定义机器人是**单向**渠道：只能发、不能收。
「能收」的只有 Web（见 ADR-0004）。渠道是否支持入站由
`ChannelCapabilities.inbound` 如实声明，不要假装支持。

依据: docs/design/05-channels.md、docs/adr/0004-im-channels-outbound-only-in-v1.md
"""

from __future__ import annotations


__all__: list[str] = []

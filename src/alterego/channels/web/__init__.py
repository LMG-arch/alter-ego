"""Web 接入层。

这一层负责**怎么把话送到浏览器**，不负责决定说什么——
说了什么由推演层决定（见 ``docs/design/04-simulation-loop.md``）。

形状（见 ``docs/design/05-channels.md`` § 3）：

- 一个 ``Channel`` 实现（``WebChannel``），把出站消息塞进 SSE 广播队列；
- 一个 FastAPI 应用，把入站消息转成 ``message.received`` 事件；
- ``static/`` 下的纯静态资源：无构建步骤，vanilla JS + SSE。

**为什么不用 WebSocket**：SSE 是单向的，正好匹配 v1 的「它主动说话、
你偶尔回一句」。断线自动重连也是浏览器送的，不用自己写心跳与状态机。
代价是入站要走普通 POST——但这反而更好调试，一个 ``curl`` 就能测。

**为什么没有前端构建步骤**：``npm install`` 会把「装好就能跑」变成一场考古。
这里只有一个 ``app.js`` 和几个页面，用原生 API 完全够。

依赖是可选 extra：``pip install alterego[web]``。没装的话本模块不参与加载。
"""

from __future__ import annotations


__all__: list[str] = []

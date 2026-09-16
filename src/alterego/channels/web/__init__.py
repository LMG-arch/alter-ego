"""Web 接入层。

这一层负责**怎么把话送到浏览器**，不负责决定说什么——
说了什么由推演层决定（见 ``docs/design/04-simulation-loop.md``）。

形状（见 ``docs/design/05-channels.md`` § 3）：

- 一个 ``Channel`` 实现（``WebChannel``），把出站消息塞进 SSE 广播队列；
- 一个 FastAPI 应用（``app.create_app``），把入站消息转成 ``message.received`` 事件；
- ``static/`` 下的纯静态资源：无构建步骤，vanilla JS + SSE。

**这个包不是通过插件机制进来的。** 包内没有内置插件的搜索路径
（``[project.entry-points."alterego.plugins"]`` 是空的；扫描目录由
``Config.plugins.search_paths`` 决定，默认只有仓库根的 ``plugins/``），
所以这里**没有也不该有 ``plugin.toml``**：它是装配根 ``cli_serve._deps``
直接注册到 ``ServiceRegistry`` 的，而且必须在 ``manager.load_all()`` 之前注册——
插件在 ``on_start`` 里可能就去取渠道，反过来拿到的是空登记表。

**为什么 ``deps.py`` 里一行 fastapi 都没有。** ``WebDeps`` 只是「这一层
要用的出口」的容器（配置、认证、广播、仓储、回话回调），路由以外的模块
（``views.py``、``sse.py``、``auth.py``）拿它当参数，于是它们的测试
不需要装 fastapi 也能跑。

**为什么不用 WebSocket**：SSE 是单向的，正好匹配 v1 的「它主动说话、
你偶尔回一句」。断线自动重连也是浏览器送的，不用自己写心跳与状态机。
代价是入站要走普通 POST——但这反而更好调试，一个 ``curl`` 就能测。

**为什么没有前端构建步骤**：``npm install`` 会把「装好就能跑」变成一场考古。
这里只有一个 ``app.js`` 和几个页面，用原生 API 完全够。

依赖是可选 extra：``pip install alterego[web]``。没装时 import 本模块不报错，
``create_app`` 会抛 :class:`~alterego.kernel.errors.ChannelError` 并点名
``alterego[web]``——报错出现在「要用它的那一刻」，而不是装包那一刻。
"""

from __future__ import annotations


__all__: list[str] = []

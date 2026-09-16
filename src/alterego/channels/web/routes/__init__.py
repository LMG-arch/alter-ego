"""Web API 的路由，按**页面**分组。

一个模块对应界面上一个（或一对紧邻的）页面，而不是按 HTTP 动词或数据表分组：
改「相册那一页」时该打开哪个文件，是看文件名就能答的问题。

路由模块**不** import 任何仓储的具体实现（``sqlite`` 一行都没有），
都从 :class:`~alterego.channels.web.deps.WebDeps` 里拿协议。这也是
``scripts/check_architecture.sh`` 第 3 组红线管的范围。
"""

from __future__ import annotations


__all__: list[str] = []

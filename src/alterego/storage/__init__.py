"""存储层：把状态与历史落盘，并把它们取回来。

本层**只做数据搬运**，不知道业务规则：它不判断「这条记忆该不该被召回」，
只负责按条件查出来。召回算法属于领域层。

数据库引擎由 `[storage] backend` 决定（默认 `sqlite`），
本层不假设具体引擎（设计原则 P1）。

依据: docs/design/03-data-model.md、docs/adr/0003-sqlite-as-sole-storage-backend.md
"""

from __future__ import annotations


__all__: list[str] = []

"""``[web]`` —— 本地 Web 界面的配置。

**为什么这个段不在 ``kernel/config.py`` 里。** 加上 ``auth_password`` 之后
那个文件涨到 914 行，而上限是 900（`scripts/check_architecture.sh` 的第 23 项）。
绕过去的办法当时有两个：把上限改成 950，或者把这个段搬出来。改上限更省事，
但那条红线的意义恰恰在于**它是硬的**——一旦允许「就这一次」，下一个 900 行
会变成 1200 行，而再下一个没人记得原来是多少。

所以照 ``kernel/config_study.py`` 的先例办：配置段的定义住在自己的模块里，
由 ``config.py`` import 进来挂到 :class:`~alterego.kernel.config.Config` 上，
并且继续从 ``alterego.kernel.config`` 转出去——外面那十几处
``from alterego.kernel.config import WebConfig`` 一行都不用改。

``_require_positive`` 这里是**有意重复**的一份，``config_study.py`` 也重复过。
公共版本住在 ``config.py`` 里，而 ``config.py`` 反过来要 import 本模块——
把它抽到第三个模块只为省下十二行，代价是「谁校验什么」要多跳一层才看得懂。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from alterego.kernel.errors import ConfigError


__all__ = ["WebConfig"]


@dataclass(frozen=True)
class WebConfig:
    """``[web]`` —— 本地 Web 界面（v1 唯一的入站渠道，ADR-0004）。"""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    auth: Literal["token", "password", "none"] = "token"
    auth_password: str = ""
    """``auth = "password"`` 时用来登录的密码。

    配置进 git，所以这里**只写引用**：``auth_password = "${ALTEREGO_WEB_PASSWORD}"``。
    直接写明文等于把家门钥匙提交进版本库——那是一次撤销不了的泄漏。
    改成别的值会发生什么：``auth = "password"`` 而这里是空的，服务起不来并会告诉你
    该设哪个环境变量；``auth`` 不是 ``password`` 时这个值被忽略，填了也不生效。
    """
    sse_keepalive_seconds: int = 20
    sse_max_connections: int = 20
    page_size: int = 50

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ConfigError("port 超出范围", port=self.port)
        _require_positive("sse_keepalive_seconds", self.sse_keepalive_seconds)
        _require_positive("sse_max_connections", self.sse_max_connections)
        _require_positive("page_size", self.page_size)
        if self.auth == "password" and not self.auth_password:
            # 校验放在这里而不是启动时：配置一旦加载就先问「这条配置自洽吗」，
            # 而不是等到用户打开页面才发现自己进不去。
            raise ConfigError(
                'auth = "password" 但没有密码',
                hint='把 auth_password 设为 "${ALTEREGO_WEB_PASSWORD}" 并在环境里设好它，'
                '或改用 auth = "token"（首次启动自动生成）',
            )
        if self.auth == "none" and self.host not in {"127.0.0.1", "localhost", "::1"}:
            # 监听 0.0.0.0 又不要认证 = 把内心日记暴露给整个局域网
            raise ConfigError(
                "非本机监听不能关闭认证",
                host=self.host,
                auth=self.auth,
                hint="把 auth 改为 token/password，或只监听 127.0.0.1",
            )


def _require_positive(name: str, value: float) -> None:
    """必须为正数。

    布尔值排除在外：``True`` 在 Python 里就是 ``1``，而 ``page_size = true``
    显然不是「一页 1 条」的意思。句子与 ``config.py`` 里那份逐字一致——
    同一个错误应该有同一种说法，哪怕报错来自两个模块。
    """
    if not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0:
        return
    raise ConfigError("配置项必须为正数", key=name, value=value)

"""Web 认证：token 与密码。

**两种凭证，两种存放位置，理由都是「它会不会进 git」。**

- **token**：首次启动时随机生成，写进 ``.alterego-token``（已经在 ``.gitignore``
  里）。它长得够随机，用户不需要记，所以没必要进配置文件。
- **密码**：用户自己想的值，只能在 ``alterego.toml`` 的 ``[web].auth_password``
  里写 ``"${ALTEREGO_WEB_PASSWORD}"``。配置文件是要提交的，明文密码等于把钥匙
  一起交上去——所以这里只接受引用，真正的值留在环境变量里。

**这一模块刻意不 import fastapi。** 「从请求里取凭证」是 Web 框架的形状，
「这个凭证对不对」是这个模块的形状。分开之后，认证逻辑可以在没有任何
HTTP 服务器的情况下被测透——而认证正是那种「测试里绕过去就等于没测」的东西。

依据: docs/design/05-channels.md § 7.2
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from alterego.kernel.config import WebConfig
from alterego.kernel.errors import WebAuthError
from alterego.kernel.logging import get_logger


__all__ = ["TOKEN_FILENAME", "AuthGate", "AuthMode", "ensure_token", "gate_for", "token_path"]

_log = get_logger("channels.web.auth")

TOKEN_FILENAME: Final[str] = ".alterego-token"
"""token 落盘的文件名，放在项目根（``data_dir`` 的上一级）。"""

_COOKIE: Final[str] = "alterego_token"
"""登录成功后下发的 Cookie 名。"""

AuthMode = Literal["token", "password", "none"]


def token_path(project_root: Path) -> Path:
    """token 文件的位置。

    放项目根而不是 ``data/``：``data/`` 是**会被备份、会被同步**的目录
    （数据库、相册都在里面），而 token 只是这台机器上的这把会话钥匙，
    丢了重新生成一个就好。
    """
    return project_root / TOKEN_FILENAME


def ensure_token(path: Path) -> str:
    """读现有的 token；没有就生成一个并落盘。

    ``token_urlsafe(32)`` 是 256 位随机数——这里不需要「更强的」算法，
    因为这个值不参与任何离线推导，只能硬猜。

    已知局限：权限位在 Windows 上基本不起作用（``chmod`` 只影响只读位）。
    所以 Windows 上的实际防线是「这台机器只有你一个人用」，
    而不是那行 ``chmod``。
    """
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        return existing
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - Windows 上 chmod 只动只读位
        _log.debug("无法修改 token 文件权限，跳过：%s", path)
    _log.info("已生成 Web 访问 token：%s", path)
    return token


@dataclass(frozen=True, slots=True)
class AuthGate:
    """一张凭证表。

    ``mode="none"`` 时 :meth:`check` 永远通过——配置层已经保证它只可能
    出现在监听本机的场景里（:class:`WebConfig` 的 ``__post_init__`` 管这件事），
    所以这里不需要再检查一遍，而**多写一遍的代价是两处判断会漂移**。
    """

    mode: AuthMode = "token"
    #: ``mode="token"`` 时的期望值。
    token: str = ""
    #: ``mode="password"`` 时的期望值。
    password: str = ""

    @property
    def required(self) -> bool:
        """是否真的需要凭证。"""
        return self.mode != "none"

    @property
    def cookie_name(self) -> str:
        return _COOKIE

    def check(self, presented: str | None) -> bool:
        """校验一个凭证。

        用 ``compare_digest`` 而不是 ``==``：``==`` 在第一个不同的字符处就返回，
        于是「猜对了前几个字符」会快一点点。这点差异在局域网里本来很难利用，
        但用常数时间比较的成本是**零**，就没有理由不用。
        """
        if not self.required:
            return True
        expected = self.token if self.mode == "token" else self.password
        if not presented or not expected:
            return False
        return hmac.compare_digest(presented, expected)

    def verify(self, presented: str | None) -> None:
        """校验并抛异常，失败时给出「该去哪儿拿凭证」而不是「密码错误」。"""
        if self.check(presented):
            return
        if self.mode == "none":
            return
        raise WebAuthError(
            "没有通过 Web 认证",
            mode=self.mode,
            hint=(
                "带上 ?token=… 或 Authorization: Bearer …"
                if self.mode == "token"
                else "用 [web].auth_password 的值；它只能来自环境变量"
            ),
        )


def gate_for(config: WebConfig, *, project_root: Path | None = None) -> AuthGate:
    """按配置造一张凭证表。

    ``project_root`` 只在 ``mode="token"`` 时会用到（要读写 token 文件）。
    显式传进来而不是从配置里推导：**组装根知道项目根在哪，这个函数不该猜**。
    """
    if config.auth == "none":
        return AuthGate(mode="none")
    if config.auth == "password":
        return AuthGate(mode="password", password=config.auth_password)
    if project_root is None:
        raise WebAuthError(
            "token 模式需要项目根目录",
            hint="由组装根把项目根传进来（token 要落在它下面）",
        )
    return AuthGate(mode="token", token=ensure_token(token_path(project_root)))

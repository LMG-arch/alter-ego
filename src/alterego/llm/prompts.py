"""提示词模板的装载与渲染。

模板是 `src/alterego/prompts/*.md`，占位符写成 ``{name}``。

**为什么不用 `str.format`**：这些模板里含 JSON 示例
（``{"kind": "episodic", "content": "…"}``）。`format` 会把那些花括号
当成占位符，轻则抛 `KeyError`，重则把示例吃掉一半——而吃掉的偏偏是
「要求模型输出什么形状」那一段，于是模型开始自由发挥。

所以这里只替换 ``{identifier}``：花括号里必须是一个合法标识符。
``{"kind"`` 不以字母开头，``{`` 后面紧跟 ``"``，都不会被匹配。

**两侧都校验**，这就是这个模块存在的理由：

- 模板里有占位符、调用方没给 → 报错（否则发出去的是带 ``{activities}`` 字面量的提示词，
  模型会一本正经地围绕这个占位符编内容）；
- 调用方给了模板里没有的值 → 报错（否则「改了模板忘了改调用方」会静默地
  继续传一堆没人用的上下文，直到有人发现提示词里少了一整段）。

两件事都必须在**发请求之前**失败：一次注定无效的调用也要付 token 的钱。

依据: docs/design/07-model-routing-and-media.md § 8
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from alterego.kernel.errors import PromptError


__all__ = [
    "PROMPTS_DIR",
    "PromptLibrary",
    "PromptTemplate",
    "placeholders",
    "render",
]


#: 随包分发的提示词目录。
#:
#: `llm/` 与 `prompts/` 是同级包，所以从本文件上跳两级。
#: 不用 `importlib.resources`：模板要能被插件作者直接打开读，
#: 而它就在源码树里——把它藏进一个只有 API 能碰到的地方没有任何好处。
PROMPTS_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "prompts"

#: ``{name}``：花括号里是合法标识符才算占位符。
#: ``re.ASCII`` 是必需的——不加的话 ``{记忆}`` 会被当成占位符。
_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"\{([a-z_][a-z0-9_]*)\}", re.ASCII)

#: 合法的模板名（取自文件名）。
_TEMPLATE_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_]+$", re.ASCII)

_SUFFIX: Final[str] = ".md"

_ENCODING: Final[str] = "utf-8"


def placeholders(template: str) -> tuple[str, ...]:
    """模板里出现的占位符，按首次出现的顺序去重。

    顺序有意义：报错时按模板里的自然顺序列出缺失项，
    比按字典序列出来更容易对上人眼看到的那段文字。
    """
    seen: dict[str, None] = {}
    for match in _PLACEHOLDER.finditer(template):
        seen.setdefault(match.group(1), None)
    return tuple(seen)


def render(template: str, values: Mapping[str, object], /, *, source: str = "<文本>") -> str:
    """把 ``{name}`` 替换成 `values` 里的值。

    Args:
        template: 模板原文。
        values: 占位符取值。值会被 `str()` 后插入。
        source: 出错信息里用来指认「哪个模板」的名字。

    Returns:
        替换后的文本。

    Raises:
        PromptError: 有占位符没给值，或给了模板里没有的值。
    """
    wanted = placeholders(template)
    missing = [name for name in wanted if name not in values]
    if missing:
        raise PromptError(
            "提示词模板缺少占位符取值",
            source=source,
            missing=", ".join(missing),
            hint="模板与调用方不一致。改了模板就要同步改渲染它的那段代码。",
        )

    unused = sorted(set(values) - set(wanted))
    if unused:
        raise PromptError(
            "提示词模板收到了它用不到的取值",
            source=source,
            unused=", ".join(unused),
            hint="传了模板里没有的键，通常意味着模板少了一段而调用方还在传。",
        )

    replacements = {name: str(values[name]) for name in wanted}
    # 用 lambda 而不是字符串：`re.sub` 的替换串会把反斜杠当转义，
    # 而值里出现 `\1` 或 `\g<0>` 是完全可能的（比如模型输出里带了路径）。
    return _PLACEHOLDER.sub(lambda match: replacements[match.group(1)], template)


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """一个已装载的提示词模板。

    Attributes:
        name: 模板名（文件名去扩展名），如 ``memory_consolidate``。
        text: 模板原文。
        path: 来源文件。手工构造的模板为 `None`。
    """

    name: str
    text: str
    path: Path | None = None

    @property
    def placeholders(self) -> tuple[str, ...]:
        """本模板需要的占位符。"""
        return placeholders(self.text)

    def render(self, **values: object) -> str:
        """渲染本模板。缺值、多值都抛 :class:`PromptError`。"""
        return render(self.text, values, source=self.name)


class PromptLibrary:
    """一个目录下的全部提示词模板。

    装载结果缓存在实例里：模板在运行期不会变，而「每次巩固都重读一遍磁盘」
    只会把「为什么不生效」变成「你是不是忘了重启」。

    缓存按实例隔离，所以测试可以拿一个临时目录建一个库，
    与随包的那份互不干扰。

    Args:
        directory: 模板目录，默认 :data:`PROMPTS_DIR`。
    """

    __slots__ = ("_cache", "_dir")

    def __init__(self, directory: Path | str = PROMPTS_DIR) -> None:
        self._dir = Path(directory)
        self._cache: dict[str, PromptTemplate] = {}

    @property
    def directory(self) -> Path:
        """模板目录。"""
        return self._dir

    def names(self) -> tuple[str, ...]:
        """目录里所有模板名，按字典序。

        目录不存在时返回空元组而不是抛错——「没有提示词目录」和
        「提示词目录是空的」在调用方眼里是同一件事，都属于配置问题，
        由 :meth:`get` 在真的要用某个模板时报出来。
        """
        if not self._dir.is_dir():
            return ()
        stems = (
            path.stem for path in self._dir.glob(f"*{_SUFFIX}") if _TEMPLATE_NAME.match(path.stem)
        )
        return tuple(sorted(stems))

    def get(self, name: str) -> PromptTemplate:
        """按名取模板并缓存。

        Raises:
            PromptError: 名字非法、文件不存在、文件为空。
        """
        cached = self._cache.get(name)
        if cached is not None:
            return cached

        if not _TEMPLATE_NAME.match(name):
            raise PromptError(
                "提示词模板名非法",
                name=name,
                hint="模板名只能是小写字母、数字与下划线。",
            )

        path = self._dir / f"{name}{_SUFFIX}"
        if not path.is_file():
            raise PromptError(
                "找不到提示词模板",
                name=name,
                prompts_dir=str(self._dir),
                available=", ".join(self.names()) or "（目录里没有模板）",
            )

        text = path.read_text(encoding=_ENCODING)
        if not text.strip():
            # 空模板不是「什么都不说」，它是一次什么都没问的付费调用。
            raise PromptError(
                "提示词模板是空的",
                name=name,
                path=str(path),
                hint="空模板会发出一段没有要求的提示词，模型只能自由发挥。",
            )

        template = PromptTemplate(name=name, text=text, path=path)
        self._cache[name] = template
        return template

    def render(self, name: str, /, **values: object) -> str:
        """装载并渲染。等价于 ``self.get(name).render(**values)``。"""
        return self.get(name).render(**values)

    def preload(self) -> tuple[PromptTemplate, ...]:
        """把整个目录装载进缓存并返回。

        启动时调一次，就能把「模板写坏了」暴露在**启动日志**里，
        而不是等它第一次被用到时才在用户面前炸开。
        """
        return tuple(self.get(name) for name in self.names())

    def clear(self) -> None:
        """清空缓存。

        给测试与「改完模板不想重启」的场景用；生产路径不该调它。
        """
        self._cache.clear()

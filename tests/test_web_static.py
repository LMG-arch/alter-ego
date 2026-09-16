"""Web 界面静态资源的测试：``hidden`` 属性必须真的能藏东西。

起因是一次真实事故。``index.html`` 用 ``hidden`` 属性藏起登录层，
而 ``style.css`` 里的 ``.gate { display: flex }`` 把它整条盖掉了——
``hidden`` 属性本身**没有任何样式**，它靠浏览器默认样式表里的
``display: none`` 生效，而默认样式表的优先级低于任何作者样式。
于是那个「藏起来」的登录层一直铺在整页上：谁打开界面都先撞上
一个没有密码可填的登录框，明明配置里写的是 ``auth = "none"``。

三千多个测试一个都没抓到它。接口层的用例走 httpx，从不碰 CSS，
而这是纯前端的事故。所以这里补的是接口测试做不到的那件事——
**把 HTML 与 CSS 两个文件对起来看**。

这也不是一个 CSS 引擎。它挡不住所有盖掉默认值的写法（内联 ``style``、
``:not([hidden])`` 之类），它挡的是已经真的发生过的那一条。

同一个理由后来也用在了 ``app.js`` 上：设置页的分组该怎么收起、收起之后
刷新一下还在不在，都是浏览器里的事，接口用例一个字也看不到。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


STATIC = Path(__file__).resolve().parent.parent / "src" / "alterego" / "channels" / "web" / "static"
HTML = STATIC / "index.html"
CSS = STATIC / "style.css"
JS = STATIC / "app.js"

#: 匹配一个属性里的 ``hidden``，但不匹配 ``data-hidden`` 这种带连字符的键。
_HIDDEN_ATTR = re.compile(r"(?<![-\w])hidden(?=[\s/>])")
_TAG = re.compile(r"<[^>]+>")
_CLASS_ATTR = re.compile(r"""class\s*=\s*["']([^"']*)["']""")
_GATE_TAG = re.compile(r"""<[^>]*\bid\s*=\s*["']gate["'][^>]*>""")

_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_DISPLAY = re.compile(r"display\s*:\s*([^;]+)")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _classes_with_hidden(html: str) -> set[str]:
    """HTML 里所有「带着 ``hidden`` 属性」的元素的 class。"""
    names: set[str] = set()
    for tag in _TAG.findall(html):
        if not _HIDDEN_ATTR.search(tag):
            continue
        found = _CLASS_ATTR.search(tag)
        if found:
            names.update(found.group(1).split())
    return names


def _rules(css: str) -> list[tuple[str, str]]:
    """把样式表切成 (选择器, 声明块)。

    朴素切分：``@media`` 的外层括号会让它的选择器粘到内层，但 ``[^{}]*``
    吃不进 ``{``，所以扫描会跳过 ``@media`` 那段、从里面的规则重新开始。
    对本次检查无害——最多多凑出几个不存在的选择器，不会漏掉类选择器。
    """
    stripped = _COMMENT.sub("", css)
    return [(" ".join(selector.split()), body) for selector, body in _RULE.findall(stripped)]


def _subjects(selector: str) -> list[str]:
    """选择器列表里每条选择器的**主体**，也就是真正会被套上样式的那个元素。

    区分主体是为了不把 ``.gate form`` 当成 ``.gate``——那条规则管的是表单，
    不是那个被藏起来的遮罩层本身。
    """
    out: list[str] = []
    for one in selector.split(","):
        parts = [part for part in re.split(r"[\s>+~]+", one.strip()) if part]
        if parts:
            out.append(parts[-1])
    return out


def _declared_display(css: str, class_name: str) -> str | None:
    """类选择器给 ``display`` 设的值；有多条时以最后一条为准（层叠里赢的那个）。"""
    pattern = re.compile(rf"\.{re.escape(class_name)}(?![\w-])")
    found: str | None = None
    for selector, body in _rules(css):
        if not any(pattern.search(subject) for subject in _subjects(selector)):
            continue
        match = _DISPLAY.search(body)
        if match:
            found = " ".join(match.group(1).split())
    return found


def _restores_hidden(css: str) -> bool:
    """样式表里有没有一条把 ``[hidden]`` 重新设成 ``display: none`` 的规则。"""
    for selector, body in _rules(css):
        if "[hidden]" not in selector:
            continue
        match = _DISPLAY.search(body)
        if match and " ".join(match.group(1).split()).startswith("none"):
            return True
    return False


def test_the_login_layer_starts_hidden_in_the_markup() -> None:
    """登录层在 HTML 里就带着 ``hidden``，不靠 JS 事后补。

    页面在 ``/api/health`` 回答之前并不知道要不要认证，所以「先亮出登录框、
    再撤掉」是必然会闪的一帧。藏在标记里就没有这一帧。
    """
    tags = _GATE_TAG.findall(_read(HTML))

    assert len(tags) == 1, f"{HTML.name} 里应当恰好有一个 id=gate 的元素，实际 {len(tags)} 个"
    assert _HIDDEN_ATTR.search(tags[0]), f"id=gate 的那个元素没有 hidden：{tags[0]}"


def test_no_author_rule_defeats_the_hidden_attribute() -> None:
    """只要一个元素既用 ``hidden`` 又被自己的类设了 ``display``，那条 ``hidden`` 就是装饰品。

    这正是登录层当初的样子：``class="gate" hidden`` 配上 ``.gate { display: flex }``。
    ``hidden`` 靠默认样式表赢不了作者样式，所以样式表里必须显式把默认行为写回来。
    """
    css = _read(CSS)
    defeated = [
        (name, display)
        for name in sorted(_classes_with_hidden(_read(HTML)))
        if (display := _declared_display(css, name)) and display != "none"
    ]

    if not defeated:
        pytest.skip("没有元素同时用 hidden 属性与 display 规则，这条检查此刻无事可做")

    assert _restores_hidden(css), (
        f"这些类带着 hidden 属性、又自己设了 display：{defeated}——"
        f"hidden 属性会完全失效。{CSS.name} 里必须有一条 "
        "`[hidden] { display: none }` 把浏览器默认行为写回来。"
    )


# ── 设置页的分组收起 ─────────────────────────────────────────────
#
# 设置页有十几个分组、九十多项。它们靠原生 ``<details>`` / ``<summary>``
# 收起，一行事件代码都不写；「用户收起过哪些组」记在 ``state.collapsed`` 里。
#
# 为什么要在这里查源码：收起行为是浏览器的，记不记得住是内存里的，两者
# 都逃得出 httpx 的视野。真实会发生的退化只有两种，而且都很安静——
# 有人把 ``<details>`` 换回 ``<h2>``（能看，但再也收不起来），
# 或者渲染时写死 ``open``（当时能收，一刷新又自己弹开）。这两种都能在
# 字符串层面认出来，所以这一条守的是它们。

_DETAILS_GROUP = re.compile(r"<details\s[^>]*class=\"group\"")


def test_settings_groups_collapse_through_native_details() -> None:
    """设置页的分组必须是原生 ``<details>``，并且带着 ``<summary>``。

    换回 ``<h2>`` 的那一刻，这一页就再也没有「收起」这个功能了——
    而且没有任何报错，只是点不动。
    """
    js = _read(JS)
    groups = _DETAILS_GROUP.findall(js)

    assert groups, f'{JS.name} 里没有用 `class="group"` 的 <details>，设置页的分组收不起来了'
    assert "<summary>" in js, (
        f"{JS.name} 里的 <details> 没配 <summary>——没有把手，展开了也就没法再收回去"
    )


def test_the_collapsed_state_is_remembered_not_hardcoded() -> None:
    """分组展开与否要读 ``state.collapsed``，且把用户的每次开合记回去。

    ``render()`` 会把整个面板重画一遍，所以刷新、切页签回来都算一次重画。
    渲染时写死 ``open``、或者只监听不记录，用户都会遇到「刚收起的分组
    自己弹开」——那一刻他什么都没做，只会以为界面坏了。
    """
    js = _read(JS)

    assert "state.collapsed.has(" in js, (
        f"{JS.name} 渲染分组时没有读 state.collapsed——收起来的分组会在下一次重画时自己弹开"
    )
    assert re.search(r"state\.collapsed\.(add|delete)\(", js), (
        f"{JS.name} 里没人往 state.collapsed 里记东西——只读不写的话，那个集合永远是空的"
    )


# ── 模型端点：在页面上加/改 ──────────────────────────────────────
#
# 这一页原本的 ``llm.providers`` 那一行是一句灰字：「这一项是一整段表，请直接
# 改配置文件。」那个死胡同本身就是事故——设置页有九十多项，用户会以为「能改的都
# 能改」，于是把「改不了」也读成「还没做」，最后去改那个没用的地方。
#
# 这两条守的是它退回去的路：接口不调（页面又只剩一句灰字），以及密钥行被当成
# 普通行渲染（值此时是 ``{"set": true}``，页面上会出现一个填着 ``[object Object]``
# 的输入框，用户一保存就把 ``{set:true}`` 写进配置文件）。

_PROVIDER_EDITOR = "/api/settings/providers"


def test_the_settings_page_edits_model_endpoints_through_the_api() -> None:
    js = _read(JS)

    assert _PROVIDER_EDITOR in js, (
        f"{JS.name} 没有调 {_PROVIDER_EDITOR}——设置页的模型端点又会变回一句灰字"
    )
    assert "data-provider-add" in js and "data-provider-save" in js, (
        f"{JS.name} 里没有「加一个端点」与「保存这个端点」的把手，页面只能看，不能改"
    )


def test_a_secret_row_is_checked_before_the_not_editable_branch() -> None:
    """密钥行的判定必须排在「不是标量」那条判定**前面**。

    密钥行的 ``editable`` 也是 ``false``（值压根没传上来），所以顺序反了的症状是：
    用户看到「这一行是嵌套的表，请在文件里改：{"set":true}」——既没说他有个密钥，
    又顺手把接口的内部形状显示给他看。
    """
    js = _read(JS)
    secret_at = js.index('field.kind === "secret"')
    nested_at = js.index("这一行是嵌套的表")

    assert secret_at < nested_at, (
        f"{JS.name} 里密钥行落在了「不是标量」那条分支后面，密钥行会被当成嵌套的表显示"
    )

# 桌面窗口

一个全局快捷键，把 Web 界面叫成一个没有地址栏的桌面窗口，再按一下收回去。

```bash
# config/alterego.toml
[plugins]
enabled = ["capability.desktop_window"]

alterego serve          # 界面起来之后快捷键才生效
# 按 ctrl+alt+a  → 窗口出现
# 再按一下       → 窗口收起来（不关）
```

## 它是什么

`alterego serve` 起来之后按快捷键，桌面上多一个窗口，里面就是那份 Web 界面——
聊天、时间线、状态、设置、插件，一个不少。再按一下收起来，窗口**不关**，
所以下一次是「秒开」而不是重新加载一遍页面。

置顶默认开着，所以它不会被别的窗口盖住。

## 为什么是浏览器窗口，不是自己画一个

因为这样**「所有功能」就真的是所有功能**。

窗口里跑的是 `alterego.channels.web` 那一套页面，也就是你在浏览器里看的同一份：
同样的路由、同样的数据、同样的卡片、同样的 SSE 流式回复。自己画一个界面意味着
把这些页面再实现一遍，于是从「一个真源」变成「两个必须同时改的地方」——
而它们开始分叉的那一刻，用户看到的是「网页上有、窗口里没有」，却没有任何报错。

具体做法是 `msedge --app=<url>`（或 `chrome`）：`--app=` 去掉地址栏和标签页，
剩下的就是一个干净的应用窗口。它用的是**自己的一份浏览数据目录**
（`<cache_dir>/browser-profile`），不碰你日常那个浏览器。

## 为什么快捷键要等界面起来

不是「等一等更稳」，是**必须**。

`alterego plugins doctor` 也会走一遍 `manager.load_all()`——它和 `alterego serve`
加载的是同一批插件。如果占快捷键这件事写在 `on_start` 里，那么你只是想看一眼
插件健不健康，键盘上的 `ctrl+alt+a` 就被永久占掉了，而 `doctor` 打印的
是一切正常。

所以武装挂在一条广播上：`cli_serve` 在 uvicorn **真的把端口绑上之后**才发
`serve.listening`，载荷里带 `url`。收到才占键。`--no-web` 和
`[web] enabled = false` 两种情况下连广播都没有，插件就静静地待着，一个键都不占。

顺带解决了另一件事：地址也只有这条路进来。端口住在 `[web]` 里，读它的是
`cli_serve`；插件自己抄一份默认值，等到你用 `alterego serve --port 9000`
的那天就会开出一个连不上的窗口——一个什么都没说错的失败。

## 配置

都是「改了只影响这个插件自己」的旋钮，所以在插件清单里：

```toml
[plugins.config."capability.desktop_window"]
hotkey = "ctrl+alt+a"     # 召唤/收起
width = 1180              # 窗口宽度
height = 800              # 窗口高度
position = "auto"         # "auto" 或 "1024,80"（允许负数，副屏在主屏左边时就是负的）
always_on_top = true      # 固定在最上层
browser = ""              # 留空 = 自动找 Edge 或 Chrome
```

看它们现在是多少、改了会怎样：

```bash
alterego plugins config capability.desktop_window
```

**这里没有端口、host、认证方式** —— 那不是漏了，是故意的。那些值住在 `[web]`
里，读它们的是 `cli_serve`，这个插件只是「知道有这回事」。抄一份过来就是
一份设置的两个真源（[`docs/guide/plugin-development.md`](../../docs/guide/plugin-development.md) § 2.4.1）。

改 `width` / `height` / `always_on_top` 会直接作用在已经开着的那个窗口上；
改 `hotkey` 会把监听器拆了重建（`RegisterHotKey` 没有「改一下」的办法）。

## 快捷键写法

`修饰键+修饰键+主键`，至少要有一个修饰键。

| 部分 | 认得的写法 |
| --- | --- |
| 修饰键 | `ctrl`（`control`）、`alt`、`shift`、`win`（`super` / `meta` / `cmd`） |
| 主键 | `a`–`z`、`0`–`9`、`f1`–`f24`、`space` `tab` `enter` `escape` `backspace` `delete` `insert` `home` `end` `pageup` `pagedown` `left` `up` `right` `down` |

大小写与顺序都不讲究：`alt+ctrl+A` 和 `ctrl+alt+a` 是同一个（日志里只会
出现规范化的那一种写法）。

**一个裸键会被拒绝**，这是故意的：`hotkey = "a"` 会让 `a` 在所有程序里都打不出来，
而用户通常是在打开某个编辑器之后才发现。写错了在加载时就会报出来，
不用等到第一次按快捷键才发现「它没反应」。

## 它只在 Windows 上占快捷键

`ctypes` 调 `user32.RegisterHotKey` + `GetMessageW` 是零依赖的做法（设计原则 P5），
而它是 Win32 专有的。Linux 上得走 `XGrabKey`、macOS 上得走 `CGEventTap`
（还要辅助功能授权），那不是这个插件现在能诚实承诺的事。

所以在别的系统上：

- 插件**照常加载**，不抛异常，不占任何键；
- `alterego plugins doctor` 会把它报成不健康，并说明原因。

```bash
alterego plugins doctor
```

不假装支持，比假装了再让人去猜要好。

## 排错

| 症状 | 多半是 |
| --- | --- |
| 按快捷键完全没反应 | `alterego serve` 没在跑，或者跑的时候带了 `--no-web` / `[web] enabled = false`。先看 `alterego plugins doctor` 那句话 |
| 快捷键没反应，`doctor` 说「Windows 没有把 … 给出来」 | 这个组合被别的程序占了（输入法、截图工具、显卡驱动面板最常见）。换一个 |
| `doctor` 说「没找到 Edge 或 Chrome」 | 把 `browser` 指向浏览器的可执行文件 |
| 窗口开了但内容是「无法访问此网站」 | 服务停了。窗口不会自己发现这件事，关掉它重开 |
| 点右上角的叉之后快捷键没反应了 | 正常的：那个窗口关了。再按一下就是重新开一个（要等几秒） |
| 改了配置没生效 | `alterego plugins config capability.desktop_window` 看它是多少；插件配置是热加载的，不用重启 |

## 它注册了什么

一个 `capability`，名字是 `desktop_window`：

```python
registry.get(Capability, name="desktop_window")
```

它的 `intent_types` 是**空集合**，所以推演循环永远不会挑中它——在用户桌面上
开一个窗口是**用户**的动作，不是它能自己决定的事。这条由机制挡住（P3），
不靠一句「请不要随便弹窗口」的提示词。

任何拿着登记表的代码都可以 `await cap.execute(intent, ctx)` 让它把窗口叫出来。

## 相关

- 插件契约：[`docs/guide/plugin-development.md`](../../docs/guide/plugin-development.md)
- 渠道与界面：[`docs/design/05-channels.md`](../../docs/design/05-channels.md)
- 代码：`plugin.py`（生命周期与开关逻辑）、`win32.py`（快捷键与窗口，全是系统调用）

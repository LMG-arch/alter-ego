# 13 · 接口一致性审计

> **这篇文档怎么用**：它不是设计，是**一次审计的结论**。
> 当你发现「代码写的」和「文档写的」对不上时，先来这里查——很可能已经查过一遍了，
> 结论与依据都在下面。文档里没有的，再自己查，然后**补到这里**。
>
> 与它配套的可执行版本是 [`tests/test_interfaces_consistency.py`](../../tests/test_interfaces_consistency.py)：
> 凡是能写成断言的差异，都已经写成断言；剩下的只能靠人看。

| | |
| --- | --- |
| 审计日期 | 2026-09-16（含当日补审） |
| 审计基线 | `f6aa2b1`（v0.1.2，Web 界面落地之后） |
| 审计范围 | `interfaces/`、`kernel/plugin.py`、`kernel/manifest.py`、`kernel/loader.py`、`kernel/manager.py`、`kernel/context.py`、`plugins/*/plugin.toml`，对照 `docs/design/02-plugin-api.md`、`docs/DESIGN.md` § 6、`docs/design/06-roadmap.md`、`docs/design/01-architecture.md` § 2.2 |
| 结论 | **23 条差异**：代码侧已修 **8** 条、文档侧已改 **12** 条、判定「都不错」**2** 条、已知缺口 **3** 条。**待办 0 条** |

---

## 1. 审计方法

四步，可以原样复现。**顺序很重要**——先读代码，后读文档。反过来会让文档里的措辞
变成你读代码时的滤镜，你会不自觉地把代码「读成」文档的样子。

### 第一步：从代码里抽接口面

```
src/alterego/interfaces/*.py          # 跨层契约
src/alterego/kernel/plugin.py         # 插件只看这一个门面
src/alterego/kernel/manifest.py       # plugin.toml 的 schema
```

对每个模块，列出**模块级定义**（不是 import 进来的名字）：

```bash
python -c "
import ast,sys,pathlib
for p in sorted(pathlib.Path('src/alterego/interfaces').glob('*.py')):
    t=ast.parse(p.read_text('utf-8'))
    names=[n.name for n in t.body if isinstance(n,ast.ClassDef)]
    names+=[x.id for n in t.body if isinstance(n,ast.Assign) for x in n.targets if isinstance(x,ast.Name)]
    print(p.name, sorted(names))
"
```

「定义了什么」与「借来了什么」必须分开：插件读到的是前者，而 `__all__` 该覆盖的也是前者。

### 第二步：读文档，逐条记下它承诺的名字

```bash
grep -n 'provide_\|ctx\.\|api_version\|kind = ' docs/design/02-plugin-api.md
```

### 第三步：**对文档里的每个 API 名字 grep 一次，数命中数**

这是全部方法里最快、最能出结论的一步。**零命中就是结论**。

```bash
grep -rn 'provide_channel\|provide_capability\|provide_llm' src/
```

上面那条命令的结果是：**1 条命中**，而且是 `sim/intents.py:188` 的一句 docstring。
于是 § 7 那张 12 行的扩展点表，一行都不成立。

### 第四步：把能断言的写成断言

判据：**如果一个聪明人可能在不知情的情况下把它改回去，它就该有测试。**
比如「`__all__` 覆盖全部公开名字」——人手加一个类时很容易忘；
再比如「`_KNOWN_MANIFEST_KEYS` 等于数据类字段减去 `path`/`source`」——手抄一份清单必然漂。

反过来，**ruff 已经管的事不要写测试**。排序（`RUF022`）、未使用变量（`F841`）
都有 lint 权威，再写一遍只会制造「lint 说对、测试说错」的假红。

---

## 2. 差异总表

判定分三档，含义是固定的：

| 判定 | 含义 | 处理 |
| --- | --- | --- |
| **代码胜** | 代码是对的，文档写错了 | 改文档 |
| **文档胜** | 文档是对的，代码漏了 | 改代码 **＋ 补回归测试** |
| **两边都错** | 都指向一个不存在的东西 | 重写这一节 |

| # | 差异 | 判定 | 处理 | 依据 |
| --- | --- | --- | --- | --- |
| 1 | `PluginKind` 代码里 6 类，文档写 8 类 | 文档胜 | ✅ 代码加 `image` / `source` | `02-plugin-api.md` § 2；`DESIGN.md` § 6.1 |
| 2 | § 7 的 12 个 `provide_*` 扩展点全仓库只 1 条命中（且是 docstring） | 代码胜 | § 7 重写为 `ctx.registry.register(...)` | `grep -rn provide_channel src/` → 0 |
| 3 | § 3.1 清单示例写 `api_version = "1"`（字符串） | 代码胜 | 改示例为裸整数；注明字符串形式也接受 | `manifest._parse_api_version` |
| 4 | `DisturbBudgetConfig` 文档草稿 6 字段；代码 7 字段（多 `pending_topic_ttl_hours`），同段 `SimulationConfig` 也少一个 `max_consecutive_tick_failures` | 代码胜 | ✅ 改文档 | `kernel/config.py` |
| 5 | `channels/web/__init__.py` 的 89 行 docstring 描述了一个不存在的模块结构 | 代码胜 | ✅ 已重写（批次 C） | — |
| 6 | `interfaces/channel.py` 的 `__all__` 只导 7 个名字里的 4 个 | 文档胜 | ✅ 补齐并转发 | — |
| 7 | `Stage.run(ctx)` 的 `TickContext` 只在 `TYPE_CHECKING` 下导入 | **都不错** | 保留，补一段「为什么」 | 见 § 4.2 |
| 8 | `on_tick_pre` / `on_tick_post` 的参数类型是 `Any` | **都不错** | 保留，补一段「为什么」 | 架构红线第 4 组 |
| 9 | `cli.py` 的 `metavar` 与实际子命令不符 | 文档胜 | ✅ 已修（含 `serve`） | — |
| 10 | `PluginStatus.DISCOVERED` / `VALIDATED` / `STOPPED` **从没被赋值过** | 文档胜 | ✅ 三处补上赋值 | — |
| 11 | `DESIGN.md` § 6.1 八类表格与 § 2 重复，且把 `capability` 的扩展点写成 `provide_tool()` | 代码胜 | ✅ 表格改成指向 § 2 的三条结论 | — |
| 12 | `starlette.testclient` 在 `filterwarnings = ["error"]` 下不可用 | 环境事实 | 记录，见 § 6.1 | — |
| 13 | `logging` 全局状态在测试间泄漏 | 缺陷 | ✅ `tests/conftest.py` 加了 autouse fixture | — |
| 14 | `plugin_state` 表 + `flush_state()` 都在，但**没有一个组装根接线** | 缺口 | 记录，见 § 5.2 | — |
| 15 | `interfaces/image.py`、`interfaces/source.py` 不存在，但 `media_asset` / `source_*` 表已存在 | 缺口 | 清单层面先接受这两种 `kind`，见 § 5.3 | — |
| 16 | `06-roadmap.md` 写 `SourceProvider`，`02-plugin-api.md` § 2.3 写 `SearchProvider` / `FeedReader` / `PageFetcher` | 代码胜 | 统一到 § 2.3 的三个名字 | — |
| 17 | § 9.1 说导入机制是 `importlib.util.spec_from_file_location` | 代码胜 | 改为「`_install_package` ＋ `importlib.import_module`」 | `loader.py` |
| 18 | § 3.2 的 `object` 类型带 `schema` 子键，但代码**明确拒绝** `object.schema` | 代码胜 | 改文档，去掉 `schema` | `_parse_config_table` |
| 19 | § 5 的 `PluginContext` 草稿类型写成 `dict` / `EventBus` / `ServiceRegistry`，且只列 4 个便捷方法 | 代码胜 | 改成 `Mapping` / `OwnedBus` / `OwnedRegistry` ＋ 5 个方法 | `kernel/context.py` |
| 20 | `PluginState` 文档说 6 个方法，代码有 9 个成员 | 代码胜 | 改文档 | `kernel/context.py` |
| 21 | `_manifest_from_entry_point` 接受模块级 `MANIFEST` 映射——**文档只字未提** | 缺口 | 补文档（第三方插件的有用逃生口） | `loader.py` |
| 22 | `plugin.toml` 拼错的顶层键被**静默忽略** | 缺陷 | ✅ 加 `_KNOWN_MANIFEST_KEYS` ＋ 报错 | 见 § 3.2 |
| 23 | `plugin.toml` 里 `[plugin]` 之外**整张顶层表**被静默忽略（漏写 `plugin.` 前缀的 `[config]` 是最常见写法） | 缺陷 | ✅ 加顶层键校验 ＋ 2 个回归测试 | 见 § 3.5 |

**23 条的分账**：

- **代码侧已修 8 条**：#1 #5 #6 #9 #10 #13 #22 #23；
- **文档侧已改 12 条**：#2 #3 #4 #11 #16 #17 #18 #19 #20 #21（＋ #5 与 #9 的文档部分已在批次 C 完成）；
- **判定「都不错、只需补一段为什么」2 条**：#7 #8（见 § 4）；
- **已知缺口与环境事实 3 条**：#12 #14 #15（见 § 5 与 § 6）。

**只剩 0 条待改**——本文档记录的所有差异都已落地。
后续再发现新差异时，请**在这张表末尾追加**，不要重排编号：
编号是被 § 3 与 § 4 的小节标题引用的。

---

## 3. 已落地的代码侧修正

### 3.1 补齐 `interfaces/channel.py` 的导出（#6）

`__all__` 原本只列 4 个名字，漏掉了 `Direction`、`ChannelCapability`、`MessageKind`。
它们不是内部细节——是 `Channel` 协议三个字段的类型：

| 名字 | 用在哪 |
| --- | --- |
| `Direction` | `Channel.direction` |
| `ChannelCapability` | `Channel.capabilities` |
| `MessageKind` | `OutboundMessage.kind` |

插件实现渠道时要**用它们标注自己的类属性**，所以它们是契约的一部分。
漏在 `__all__` 外面，等于逼插件作者去翻源码才能写对类型注解。

同时把 `interfaces/__init__.py` 改为**全部转发**：六个子模块的公开名字一个不漏
（44 个，channels 7 + common 1 + llm 6 + repository 20 + simulation 9 + storage 1）。
「部分转发」比「完全不转发」更坏——`from alterego.interfaces import PersonaRecord`
报 `ImportError` 的人会以为是自己写错了，而正确的结论只是「这个包没导它」。
要么全导，要么一个都不导。

### 3.2 拒绝拼错的清单键（#22）

`plugin.toml` 里写错的顶层键原本被静默忽略。最典型的后果：

```toml
enabled_by_default = false   # 想要的是这个
enabledByDefault = false     # 实际写成这个 —— 插件照样默认开启
```

一个只想写「默认别开」的作者，得到一个默认开着的插件。这类**「配错了反而更开放」**
的降级最难发现，因为没人会去查一个「本来就没配」的东西。

修法是**从数据类推导允许键**，不手抄：

```python
_KNOWN_MANIFEST_KEYS: frozenset[str] = frozenset(
    f.name for f in dataclasses.fields(PluginManifest) if f.name not in {"path", "source"}
)
```

去掉 `path` / `source`：它们由发现过程填（本地目录 / entry point），不是插件作者写的。
推导放在**类定义之后**——需要字段已经存在。加字段时它自动跟着变，不可能漂。

### 3.3 补齐三个从未被赋值的状态（#10）

`PluginStatus` 有 8 个成员，枚举、CLI 的状态标签表、`info` 的输出格式全都写好了，
但 `DISCOVERED` / `VALIDATED` / `STOPPED` **从来没有被赋值过**。

| 状态 | 现在在哪赋值 | 少了它会怎样 |
| --- | --- | --- |
| `DISCOVERED` | `discover()` | `alterego plugins info <没启用的插件>` 和「插件不存在」输出一样——空 |
| `VALIDATED` | `load_all()` 解析拓扑序之后 | 分不清「校验没过」和「还没轮到它」 |
| `STOPPED` | `_teardown()` 里 `on_stop` 之后、`on_unload` 之前 | 「服务已经摘了但插件以为自己在跑」无从定位 |

`STOPPED` 的位置是刻意的：它是 `Loaded` 与 `Unloaded` 之间**唯一可观察的中间态**——
插件已经停了，但它注册的服务与事件订阅**还在**（那两行才撤）。写成两个状态而不是三个，
就等于放弃诊断这一类问题。

### 3.4 清单层面先接受 `image` / `source`（#1、#15）

`PluginKind` 从 6 类扩到 8 类，与 `02-plugin-api.md` § 2 逐字一致。
两种新 `kind` 的接口（`interfaces/image.py`）要到 v0.2.0 / v0.3.0 才落地，
但**清单层面今天就接受**，三条理由：

1. `kind` 今天是**纯元数据**——内核只用它显示，以及措辞一句「entry 指错地方了」的提示
   （`loader._entry_hint`）。内核**不校验**「声明 channel 就必须注册 Channel」，
   今天不校验，以后也不该用一个半吊子的校验器代替（没校验可查，错校验难查）。
2. 一份照设计文档写出来的 `plugin.toml` 不该在解析阶段被拒。拒绝它会让
   「文档说要写 `kind = "image"`」和「内核说这不是合法 kind」同时为真，而插件作者无从判断该信哪一个。
3. 加两个字符串的成本是零：没有任何分支读这两个值。

> ⚠️ **「能通过解析」不等于「今天能用」。** 那张表在
> [`docs/guide/plugin-development.md`](../guide/plugin-development.md) § 1.2。

### 3.5 拒绝 `[plugin]` 之外的顶层表（#23）

#22 拦住了拼错的**键**，但没有拦住多出来的**表**。
`kernel/loader.py` 的 `load_manifest` 只取 `data["plugin"]`，
顶层剩下的东西**一个都没碰**——于是下面这份清单会安静地加载成功：

```toml
[plugin]
id = "tool.webhook"
# …

[config]                       # ✗ 少了 plugin. 前缀
webhook_url = { type = "string", required = true }
```

插件起来了，`ctx.config` 却是空的：作者以为配了 1 个必填字段，
内核看到的是一份没有 `config` 段的清单。而 `[plugin.config.*]` 的
「必填字段缺失会报错」这条保护**也一并失效了**——因为清单里根本没有这个字段。

> 这个 bug 的**唯一实例是文档自己的示例**（`02-plugin-api.md` § 3.1）。
> 三个真实插件与所有测试夹具写的都是 `[plugin.config.*]`。
> 也就是说：读文档照抄的人会掉进去，读代码的人不会。

修法与 #22 同源——**不猜、不宽容**（P2）：

```python
stray = sorted(set(data) - {_MANIFEST_TABLE})
if stray:
    raise PluginManifestError(
        "插件清单里有 [plugin] 之外的顶层键",
        manifest=str(path),
        unknown=stray,
        hint="配置字段要写在 [plugin.config.<字段名>] 里……",
    )
```

> **为什么不做成「宽容处理」**：把顶层 `[config]` 自动当成 `[plugin.config]`
> 看起来更友好，但它会把「作者写错了」变成「内核替他猜对了」，
> 而猜错的代价是静默——下一次他写 `[settigns]` 就不一定猜得中了。
> 同一个错误必须在同一个地方报出来。

回归测试：`tests/test_kernel_loader.py` 的
`test_a_stray_top_level_table_is_rejected` 与
`test_a_stray_top_level_scalar_is_rejected`，分别覆盖「多一张表」
与「多一个标量键」。

---

## 4. 判定为「都不错」的两条

这两条看起来像缺陷，其实都是**上一条红线的必然结果**。写清楚是为了阻止下一个人「顺手修一下」。

### 4.1 为什么 `on_tick_pre` / `on_tick_post` 的参数是 `Any`

```python
def on_tick_pre(self, ctx: Any) -> None: ...
def on_tick_post(self, ctx: Any) -> None: ...
```

不是偷懒。`TickContext` 属于 `sim/`，而架构红线第 4 组禁止 `kernel/` 导入
`domain` / `sim` / `storage` / `llm` / `channels`。内核一旦为了类型标注认识 `sim`，
「内核无知」（P1）就只剩一句口号了。

代价是插件作者拿不到 IDE 补全。补偿办法是把 `TickContext` 的真实形状写进
插件开发文档，并说明「这两个钩子的入参是 `sim.TickContext`，类型标注只能写 `Any`」。

### 4.2 为什么 `Stage.run(ctx)` 的 `TickContext` 在 `TYPE_CHECKING` 下导入

```python
if TYPE_CHECKING:  # pragma: no cover - 只为类型检查器存在
    from alterego.sim.context import TickContext
```

`interfaces/simulation.py` 的 `Stage` 协议要吃 `sim` 的类型，而 `sim` 又要 import
`interfaces`——直接 import 就是一个循环。`TYPE_CHECKING` ＋ 字符串注解是标准的破环手法：
运行时零依赖，类型检查器照样认。

**保持现状。** 去掉它会让 `import alterego.interfaces` 触发 `import alterego.sim`，
而 `interfaces` 是被所有层引用的最底层，它一旦牵扯 `sim`，
「接入层 → 推演层 → 领域层 → 内核层」这条单向依赖当场破。

---

## 5. 已知缺口（不是差异，是缺功能）

下面这些**代码与文档不矛盾**——文档也没承诺它们。记录下来是为了让「为什么点不动」
有一个可查的答案，而不是每次重新怀疑一遍。

### 5.1 仓储接口缺口

Web 界面（批次 C）落地时，有四处功能因为**接口层没有对应方法**而无法实现。
不是路由忘了写，是 `interfaces/repository.py` 里没有那个方法：

| 缺口 | 被谁挡住 | 现象 |
| --- | --- | --- |
| `EmotionRepository` 只有 `latest` / `append`，没有区间查询 | `/api/emotion` 画不出曲线 | 只能显示当前情绪一个点 |
| `TickLogRepository` 只有 `append`，没有读取 | 「内心」页显示不了候选意图 | 页面显示当前情绪，没有「它在想什么」 |
| `SocialPostRepository` 只有 `append` / `list_recent` | `POST /api/feed/{id}/comment` 返回 **503** | 点赞可以（写进 metadata），评论不行 |
| `MemoryRepository` 没有 `search` / `stats` | 记忆页不能检索、不能看统计 | `MemoryStats` 是死代码 |

**修法**：在 `interfaces/repository.py` 加方法 → 在 `storage/sqlite/` 实现 →
再回来接路由。三层一起动，所以它是一次独立的批次，不是一致性审计能顺带做完的。

### 5.2 `plugin_state` 没有接线（审计表 #14）

```
migrations/001_initial.sql    plugin_state / plugin_meta 两张表 ✅
kernel/context.py             PluginState：9 个成员，JSON 可序列化校验 ✅
kernel/manager.py             __init__ 收 state_loader/state_sink ✅
                              flush_state()：批量落盘 ✅
组装根（8 个 cli_*.py）        没有一个人传这两个参数 ❌
storage/sqlite/               没有 PluginStateRepository ❌
```

后果具体而隐蔽：插件今天**可以**正常记账、也能读回自己刚写的东西，
但**进程重启后拿不回来**——没有 `sink` 时 `flush()` 直接丢弃挂起的写入。
`flush_state()` 更是一行静态无操作，且**无人调用**（`grep -rn flush_state src/` 只有它的定义）。

已经在代码里把这件事说出来了：`PluginState` 与 `PluginManager.flush_state()`
的 docstring 都写明了现状，免得插件作者在踩坑之后才发现。

**判定：接线是独立批次。** 它要动存储层（新仓储）＋ 8 个组装根 ＋ 覆盖率，
混进一致性审计会让这次提交没法审。

### 5.3 `image` / `source` 接口未落地（#15）

见 § 3.4。`media_asset` / `source_feed` / `source_item` 表已随迁移
`002_media.sql` / `003_sources.sql` 建好，但 `interfaces/image.py` 与
`interfaces/source.py` 要到阶段 L（v0.2.0）/ M（v0.3.0）才写。

> **表可以先于功能存在。** `plugin_state`、`media_asset`、`source_item` 都是这个情况。
> 这不是债，是刻意的：迁移一旦发布就不能改，先建好能避免「加一个字段要写一次迁移」。

### 5.4 Web 界面 10/13 页

`06-roadmap.md` 的 M4 要求 13 个页面，v0.1.2 交付 10 个。
缺的三个是**关系网 / 相册 / 日志**，分别等 `domain/relationship.py` 的聚合视图、
阶段 L 的 `media_asset`、阶段 I 的日志查询——**缺的不是页面而是数据**。

另外 `static/app.js` 还没读 `has_more`，所以列表翻页是单页的。

---

## 6. 测试基建的两条发现

### 6.1 `starlette.testclient` 在本仓库不可用

`pyproject.toml` 里 `filterwarnings = ["error"]` 是**承重墙**——它把
「依赖库发了 DeprecationWarning」当成失败，这样升级依赖时会当场知道。
但它同时排除了 `TestClient`：它会触发一个警告，于是每个 HTTP 测试都会红。

**结论：HTTP 测试一律用 `httpx.ASGITransport` ＋ `httpx.AsyncClient`。**
先例是 `tests/test_web_routes.py`（62 个测试）。中间件的 `scope` / `receive` / `send`
直接手写 ASGI 三元组更合适，先例是 `tests/test_web_app_middleware.py`。

不要为了让 `TestClient` 能用而给 `filterwarnings` 加例外——那等于把承重墙凿一个洞。

### 6.2 `logging` 全局状态会跨测试泄漏

`logging.getLogger(...)` 拿到的是**进程级单例**，插件用 `PropagateHandler` 时
很容易让上一个测试的 handler 活到下一个测试，表现为「单独跑绿、一起跑红」。

修法在 `tests/conftest.py`：一个 autouse 的 `_restore_logging` fixture，
每个测试前记录 root logger 的 handler 列表，测试后还原。

---

## 7. 可执行的对照物

`tests/test_interfaces_consistency.py`（26 个测试，`@pytest.mark.architecture`）
是本文档里**能写成断言的**那一部分：

| 测试 | 守住什么 |
| --- | --- |
| `test_module_all_covers_every_public_name[6]` | 每个 `interfaces/*.py` 的 `__all__` 覆盖它定义的每个公开名字 |
| `test_every_exported_name_resolves[6]` | `__all__` 里每个名字真的存在（不是拼错的字符串） |
| `test_module_all_has_no_duplicates[6]` | `__all__` 里没有重复名字 |
| `test_package_all_is_the_union_of_submodule_alls` | 包的 `__all__` 恰好等于六个子模块 `__all__` 的并集 |
| `test_interfaces_submodule_list_is_complete` | 新增 `interfaces/*.py` 忘了登记时红 |
| `test_plugin_facade_exports_resolve` | `kernel/plugin.py` 门面导出的名字都能取到 |
| `test_plugin_kinds_match_the_documented_eight` | `Literal` 与 `_KINDS` **同步**（只改一个等于没改——mypy 管不到运行时的 TOML） |
| `test_config_value_types_match_the_documented_eight` | 同上，配置值类型 |
| `test_manifest_known_keys_are_derived_from_the_dataclass` | 手抄清单写回去时立刻红 |
| `test_unknown_manifest_key_is_rejected` | `enabledByDefault = false` 这类拼错当场报错 |

清单的**表级**校验在 `tests/test_kernel_loader.py`（§ 3.5 的回归测试）：

| 测试 | 守住什么 |
| --- | --- |
| `test_a_stray_top_level_table_is_rejected` | 漏写 `plugin.` 前缀的顶层 `[config]` 报错，且报错里点名 `config`、提示怎么写 |
| `test_a_stray_top_level_scalar_is_rejected` | 顶层多出标量键同样报错 |

配套的运行时断言在 `tests/test_kernel_manager.py::TestStatusSteps`（4 个测试）：
状态机上每个成员都真的被落到 `_status`。

**刻意不写的两条**：`__all__` 是否按字母序、是否有未使用的 import。
前者是 `ruff` 的 `RUF022`，后者是 `F401`——它们才是权威。
再写一遍只会制造「lint 说对、测试说错」的假红，然后教人 `noqa` 掉错误的那个。

---

## 8. 复现命令

```bash
# 接口一致性（26 个测试）
python -m pytest tests/test_interfaces_consistency.py -q

# 状态机（4 个测试）
python -m pytest tests/test_kernel_manager.py -k StatusSteps -q

# 清单校验（含 § 3.5 的两个回归测试）
python -m pytest tests/test_kernel_loader.py -q

# 架构红线（23 项，含分层依赖方向）
bash scripts/check_architecture.sh --verbose

# 「数命中数」这个动作本身
grep -rn 'provide_channel\|provide_capability\|provide_llm\|provide_tool' src/
```

---

## 变更记录

| 日期 | 版本 | 变更 | 作者 |
| --- | --- | --- | --- |
| 2026-09-16 | v0.1.2 | 首次审计：22 条差异，6 条代码侧修正 + 4 个回归测试文件 | LMG-arch |
| 2026-09-16 | v0.1.2 | 补审：新增 #23（顶层表被静默忽略，代码侧修）；文档侧 12 条全部改完（含 `02-plugin-api.md` 9 节、`DESIGN.md` § 6.1/§ 6.2、`01-architecture.md` § 2.2、`06-roadmap.md` § 2.2）；配套新增 `docs/guide/plugin-development.md` | LMG-arch |

# 插件开发指南

> **这篇文档是写插件的唯一依据。** 它只写**今天能跑起来的东西**——
> 每一条命令都跑过，每一个字段都在代码里存在。设计意图（为什么这样设计、
> 以后会变成什么样）去看 [`docs/design/02-plugin-api.md`](../design/02-plugin-api.md)；
> 两者矛盾时以本文档为准，并且**把矛盾报出来**（那说明有一边过时了）。
>
> 配套的可执行对照物：`tests/test_interfaces_consistency.py`（26 个测试）
> 与 `plugins/example_plugin/`（可直接跑的最小插件）。

| | |
| --- | --- |
| 适用版本 | v0.1.2（插件 API 版本 `1`） |
| 插件放哪 | `plugins/<目录名>/plugin.toml` ＋ `plugin.py` |
| 只能 import | `alterego.kernel.plugin`（内核门面）、`alterego.interfaces.*`（跨层契约） |
| 调试命令 | `alterego plugins list` / `doctor` / `info <id>` / `reload <id>` |

---

## 1. 五分钟写一个

### 1.1 复制示例

```bash
cp -r plugins/example_plugin plugins/my_plugin
```

然后改三处：

| 文件 | 改什么 |
| --- | --- |
| `plugin.toml` | `id`、`name`、`entry` 的类名 |
| `plugin.py` | 类名、`id` 类属性（**必须与清单里的 `id` 一致**） |
| `plugin.toml` | `enabled_by_default = false` 保持不动（见 § 2.6） |

### 1.2 今日可用的 `kind`

`kind` 声明这个插件是什么。八种里**六种今天能跑**：

| kind | 能跑吗 | 你要实现的接口 | 从哪 import |
| --- | --- | --- | --- |
| `capability` | ✅ | `Capability` | `alterego.interfaces.simulation` |
| `tool` | ✅ | `Tool` | `alterego.interfaces.simulation` |
| `stage` | ✅ | `Stage` | `alterego.interfaces.simulation` |
| `channel` | ✅ | `Channel` | `alterego.interfaces.channel` |
| `llm` | ✅ | 在 `ctx.registry` 上注册 `LLMProvider` | `alterego.interfaces.llm` |
| `storage` | ✅ | 在 `ctx.registry` 上注册 `StorageBackend` | `alterego.interfaces.storage` |
| `image` | ⚠️ **只有清单能过** | 接口 `interfaces/image.py` 在 v0.2.0 | — |
| `source` | ⚠️ **只有清单能过** | 接口 `interfaces/source.py` 在 v0.3.0 | — |

`image` / `source` 今天**能通过清单校验、但内核不会用它做任何事**——因为 `kind`
目前是纯元数据，内核只用它显示和措辞提示，不拿它做分支。写进去不会报错，
但也没有任何效果。见 [`13-interface-consistency.md`](../design/13-interface-consistency.md) § 3.4。

### 1.3 让它跑起来

```bash
# 1. 看它被发现了吗（不导入任何插件代码）
alterego plugins list

# 2. 真装一遍，看健康状态
alterego plugins doctor

# 3. 看它的清单与配置现状
alterego plugins info my_plugin_name
```

如果 `list` 说「一个都没启用」，去 `config/alterego.toml` 加：

```toml
[plugins]
enabled = ["capability.my_plugin"]
```

`enabled` 里**没有**它时，只有清单里 `enabled_by_default = true` 的插件会加载。
随包的三个示例都是 `false`——**示例不该在你没要求的时候自己跑起来**，
你自己的插件也应该保持 `false`。

---

## 2. `plugin.toml` 字段表

**顶层只允许下面 18 个键。写错一个键名会当场报错，不会被静默忽略。**
（`path` 与 `source` 由发现过程自动填，不要自己写。）

### 2.1 必填（5 个）

| 键 | 类型 | 规则 | 例子 |
| --- | --- | --- | --- |
| `id` | 字符串 | `^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$`——**恰好一个点**，全小写 | `capability.my_plugin` |
| `version` | 字符串 | SemVer：`^数字.数字.数字`，可带 `-beta.1` 后缀 | `0.1.0` |
| `api_version` | **整数** | 与内核的兼容窗口是 `0 <= 内核 - 插件 <= 1`（内核今天是 `1`），所以只能写 `0` 或 `1` | `1` |
| `kind` | 字符串 | § 1.2 的八种之一 | `capability` |
| `entry` | 字符串 | `模块路径:类名`，模块路径**相对插件根目录**，用点分隔 | `plugin:MyPlugin` |

```toml
[plugin]
id = "capability.my_plugin"
version = "0.1.0"
api_version = 1                 # 裸整数，不要写 "1"（字符串也接受，但整数才是规范形态）
kind = "capability"
entry = "plugin:MyPlugin"       # 代码在 <插件目录>/plugin.py 的 MyPlugin 类
```

> `entry` 指到不存在的模块或类时，报错会顺着 `kind` 给一句更具体的提示——
> 所以 `kind` 填对不只是文档的事。

### 2.2 展示（6 个，都可选）

| 键 | 类型 | 说明 |
| --- | --- | --- |
| `name` | 字符串 | 给人看的名字，`alterego plugins info` 里显示它；省略就显示 `id` |
| `description` | 字符串 | 一句话说清它做什么 |
| `authors` | 字符串数组 | `["你的名字"]` |
| `license` | 字符串 | `"MIT"` |
| `homepage` | 字符串 | 仓库或文档 URL |
| `tags` | 字符串数组 | 便于检索，`["channel", "wecom"]` |

### 2.3 行为（4 个，都有默认值）

| 键 | 类型 | 默认 | 效果 |
| --- | --- | --- | --- |
| `enabled_by_default` | 布尔 | `true` | `false` 表示「只有用户在 `[plugins] enabled` 里点名才加载」 |
| `priority` | 整数 | `0` | 同一个接口注册了多个实现时，**大的先被取到** |
| `auto_reload` | 布尔 | `true` | 本地插件的文件改动后自动重载（需要 `[plugins] auto_reload = true`） |
| `requires` / `provides` / `optional` | 字符串数组 | `[]` | 见 § 7 |

### 2.4 配置：`[plugin.config.<字段名>]`

每个配置项一张子表，键就是配置项名字：

```toml
[plugin.config.greeting]
type = "string"
default = "你好"
description = "问候语的开头。"     # 会在 plugins info 里打出来，请写清楚
min_length = 1
max_length = 40
```

`type` 是八种之一，**每种能配的约束不同**：

| `type` | 可用约束 | 例子 |
| --- | --- | --- |
| `string` | `min_length` `max_length` `pattern` `choices` | `default = "你好"` |
| `integer` | `min` `max` `choices` | `default = 1` |
| `number` | `min` `max` `choices` | `default = 0.5` |
| `boolean` | —— | `default = true` |
| `array` | `item_type` `min_items` `max_items` | `default = ["a"]`, `item_type = "string"` |
| `object` | —— | 值必须是表；**不支持嵌套的 `schema` 子键** |
| `duration` | —— | `default = "30s"` / `"5m"` / `"2h"` / `"1d"` |
| `path` | `must_exist` | `default = ""`，`must_exist = true` 表示启动时目录必须已经存在 |

所有类型都还接受这 5 个通用键：

| 键 | 作用 |
| --- | --- |
| `required` | `true` 表示**必须有值**；与 `default` 同时写会当场报错 |
| `default` | 用户没配、环境变量也没有时用它 |
| `description` | 中文说明，`plugins info` 会显示 |
| `secret` | `true` 时日志、CLI、Web 里都显示成 `***` |
| `env` | 环境变量名，**该变量优先于配置文件**（见 § 2.5） |

> ⚠️ **`object` 不支持 `schema`。** 写 `schema = {...}` 会报「v1 尚不支持嵌套的
> object.schema」。嵌套结构请拆成多个平铺字段，或用 `type = "string"` 存 JSON 字符串。
> （这与 [`02-plugin-api.md`](../design/02-plugin-api.md) § 3.2 的旧描述不同，以这里为准。）

### 2.5 配置值的来源与优先级

```
环境变量（只有声明了 env 的字段才读）  >  用户配置文件  >  清单 default
```

只有**显式声明了 `env`** 的字段才会去读环境变量。这是刻意的（P2 显式优于隐式）：
如果自动把所有大写环境变量都当成配置，那 `DEBUG=1` 之类的普通环境变量
会莫名其妙地改动插件行为，而且没有任何地方记录这件事。

```toml
[plugin.config.api_token]
type = "string"
secret = true
env = "MY_PLUGIN_TOKEN"     # 有这个环境变量时，它赢过配置文件
description = "服务端要求的访问令牌。"
```

读取时用 `ctx.config`（已校验、已填默认值、已解析环境变量）：

```python
self._token = str(ctx.config["api_token"])
```

想拿配置之外的东西用 `ctx.config_value(key, default)`——它在缺失时**不报错**。

**用户在哪填这些值？** 在 `config/alterego.toml` 里按**插件 id** 分块：

```toml
[plugins.config."channel.dingtalk_webhook"]
webhook_url = "https://oapi.dingtalk.com/robot/send?access_token=..."
```

`kernel/config.py` 的 `_normalize()` 会把 `[plugins.<插件 id>]` 这种「以用户起的
名字为键」的表折叠进 `plugins.config`，所以下面两种写法等价
（`templates/alterego.toml` 用的是后者，因为 `config.` 前缀能提醒人
「这是插件配置，不是内核配置」）：

```toml
[plugins."channel.dingtalk_webhook"]           # 折叠进 plugins.config
[plugins.config."channel.dingtalk_webhook"]    # 直接命中已声明的容器字段
```

运行时由 `PluginManager` 取 `config.plugins.config_for(manifest.id)`，把它交给
`resolve_config()`，得到的就是 `ctx.config`。

### 2.6 为什么默认别开

```toml
enabled_by_default = false
```

一个插件在别人机器上「因为是示例所以默认跑起来」是最容易被抱怨的事：
它会发请求、写文件、花 token，而用户从没要求过。
`false` 表示「我装在这里，但等你点名」。

### 2.7 ⚠️ 拼错的键会报错，不会被忽略

```toml
enabledByDefault = false   # ✗ 报错：清单里有无法识别的键
```

这条检查是**故意**做严的。静默忽略一个键，等于允许插件带着一份
「你以为配了、其实没配」的清单跑起来——上面这个例子里，
作者只想写「默认别开」，实际却得到一个默认开着的插件。
这类**「配错了反而更开放」**的降级最难发现，所以宁可当场报错。

同样的检查也覆盖 `[plugin.config.*]` 里的每个键（`requierd`、`defualt` 都会报错）。

---

## 3. 九个钩子

```python
from typing import Any

from alterego.kernel.plugin import Plugin, PluginContext


class MyPlugin(Plugin):
    id: str = "capability.my_plugin"     # 必须与清单里的 id 一致

    def on_load(self, ctx: PluginContext) -> None: ...
    def on_start(self) -> None: ...
    def on_stop(self) -> None: ...
    def on_unload(self) -> None: ...
    def on_config_changed(self, new_config: dict[str, Any]) -> None: ...
    def on_tick_pre(self, ctx: Any) -> None: ...
    def on_tick_post(self, ctx: Any) -> None: ...
    def on_event(self, event: Any) -> None: ...
    def health(self) -> HealthStatus: ...
```

**全部是同步的**（`async def` 不会被 await——内核按同步函数调用它们），
**没有一个带 `@abstractmethod`**：按需覆盖你关心的一两个就够了，
强制实现九个空方法只会制造样板代码。

| 钩子 | 什么时候被调 | 必须做的 / 注意的 |
| --- | --- | --- |
| `on_load(ctx)` | 装进来时，**一次性** | 在这里注册服务、订阅事件、读配置、存 `self._ctx = ctx` |
| `on_start()` | 所有依赖都 load 完之后 | 可以在这里建连接、开线程 |
| `on_stop()` | 停机与热重载时 | **必须幂等**（可能被调多次）。用了 `async def` 或其他原因没做到幂等，重载两次就会炸 |
| `on_unload()` | `on_stop` 之后 | 清自己申请的资源；服务与订阅由内核代撤 |
| `on_config_changed(new)` | 用户改了配置或热重载 | 重新读 `new_config`。不实现的话配置要等重启才生效 |
| `on_tick_pre(ctx)` / `on_tick_post(ctx)` | 每个 tick 前后 | 入参是 `sim.TickContext`，但类型只能标 `Any`——见下 |
| `on_event(event)` | 你订阅过的主题 | 只订阅你一定要处理的主题 |
| `health()` | `alterego plugins doctor` | 默认返回「正常」；出错时返回 `HealthStatus(ok=False, detail=..., hint=...)` |

### 3.1 为什么 `on_tick_pre` 的参数是 `Any`

不是偷懒。`TickContext` 属于 `sim/`，而架构红线禁止 `kernel/` 导入 `sim/`——
内核一旦为了类型标注认识 `sim`，「内核无知」（P1）就只剩一句口号了。

补偿办法：**这两个钩子的第一个参数就是推演层的 `TickContext`**，
能用到的字段见 [`04-simulation-loop.md`](../design/04-simulation-loop.md)。
写的时候标 `ctx: Any` 并在上面加一行注释说明来源。

### 3.2 参数名不要加下划线前缀

```python
def on_tick_post(self, ctx: Any) -> None: ...    # ✅
def on_tick_post(self, _ctx: Any) -> None: ...   # ✗
```

不用到的参数写 `_ctx` 会让「协议一致性检查」认不出这是同一个方法。
`plugins/*/plugin.py` 已经豁免了「未使用参数」的 lint，所以放心写原名。

### 3.3 只订阅你一定要处理的主题

订阅了就必须处理：一个什么都不做的事件订阅会在每个 tick 上被调用一次，
变成纯粹的噪声与性能损耗。记住每个 tick 都写一次盘是最常见的性能坑——
攒够一批再写（见 § 8）。

---

## 4. `PluginContext` 能用什么

`on_load(ctx)` 拿到的 `ctx` 是你和内核之间的全部接口。

### 4.1 十一个字段

| 字段 | 类型 | 用来做什么 |
| --- | --- | --- |
| `ctx.plugin_id` | `str` | 你自己的 id |
| `ctx.manifest` | `PluginManifest` | 清单的完整内容 |
| `ctx.config` | `Mapping[str, Any]` | 已校验、已填默认值、已解析环境变量的配置。**用 `ctx.config["键"]` 取，缺键会 KeyError** |
| `ctx.logger` | `logging.Logger` | 打日志。**用这个，不要自己建 handler**（脱敏与轮转由内核负责） |
| `ctx.bus` | `OwnedBus` | 发布/订阅事件。归属自动记在你名下 |
| `ctx.registry` | `OwnedRegistry` | 注册你提供的服务 |
| `ctx.clock` | `Clock` | 虚拟时钟。**取时间只能用它**（`datetime.now()` 在 `sim/`、`domain/` 里是红线） |
| `ctx.scheduler` | `Scheduler` | 定时任务 |
| `ctx.state` | `PluginState` | 键值对状态（⚠️ 今天不跨重启，见 § 8） |
| `ctx.paths` | `PluginPaths` | 五个目录：`data_dir` `cache_dir` `config_dir` `plugin_dir` `alterego_dir` |
| `ctx.rng` | `random.Random` | 随机数。**用它，不要 `import random`**——种子要可复现 |

### 4.2 五个便捷方法

```python
ctx.get_service(Capability, name="example")          # 取不到就抛
ctx.get_optional_service(Channel, name="channel.web") # 取不到返回 None
ctx.publish("my_plugin.happened", {"n": 1})           # 发事件；source 自动是你
ctx.now()                                             # = ctx.clock.now()
ctx.config_value("repeat", 1)                          # 带默认值的配置读取
```

**没有 `ctx.provide()`。** 注册只有一条路：

```python
ctx.registry.register(Capability, self, name="example")
```

不要自己传 `owner=`——归属由 `OwnedRegistry` 自动填成你的插件 id
（[ADR-0007](../adr/0007-auto-owning-plugin-context-views.md)）。
自己填只会填错，而填错的表现是「插件卸载后服务还在」。

### 4.3 ⚠️ 两个 `ctx` 不一样，`llm()` 只在其中一个上

这是最容易混的一处，所以单列：

| | `kernel.PluginContext` | `sim.TickContext` |
| --- | --- | --- |
| 从哪来 | `on_load(ctx)` 的参数；自己存成 `self._ctx` | `Stage.run(ctx)` / `Capability.execute(intent, ctx)` / `on_tick_pre(ctx)` / `on_tick_post(ctx)` 的参数 |
| 活多久 | 插件活多久 | **一个 tick**（引擎每个 tick 重建） |
| 有 `llm()` 吗 | ❌ **没有** | ✅ 有 |
| 时间 | `ctx.now()`（方法） | `ctx.virtual_now`（字段） |
| `state` | `PluginState`：你自己的键值对 | `StateSnapshot`：人格/情绪/日程快照，**tick 内只读** |
| `paths` / `registry` / `config` | ✅ 有 | ❌ 没有（去 `self._ctx` 上拿） |

**最容易踩的一条**：两个 `ctx` 都有叫 `state` 的属性，但**完全不是一回事**。
`TickContext.state` 是「世界在看什么」（怎么改见 `04-simulation-loop.md` 的
`StageResult.changes`），`PluginContext.state` 是「你自己想记住什么」。
在阶段里写 `ctx.state.set(...)` 会写到快照对象上去，而它没有 `set`。

所以要调模型，只能在**推演过程中**：

```python
async def execute(self, intent: Any, ctx: Any) -> CapabilityResult:
    text = await ctx.llm("reply", prompt)     # ← 这里的 ctx 是 TickContext
    return CapabilityResult(ok=True, summary=text)
```

`ctx.llm(purpose, prompt, *, tier=None, system=None, temperature=0.8, max_tokens=1024, json_schema=None)`
三次副作用都在它内部发生，所以**必须用它、不要自己建 HTTP 客户端**：

1. 调用被记进 `ctx.llm_calls`（写进 `tick_log`，Web 上能看到）；
2. 用量（token）从返回的用量里读到，成本统计靠它；
3. `correlation_id` 传给模型网关，于是「一次 tick 花了多少钱」能串成一条线。

> `purpose` 必须是 `[llm.routing]` 里**已经存在**的用途名——写错会当场报错，
> 而不是悄悄走默认档（静默回落会让「我明明配了便宜档怎么这么贵」变成玄学）。
> 可用用途见 [`07-model-routing-and-media.md`](../design/07-model-routing-and-media.md)。

在 `on_load` 里就想调模型是不行的——那时还没有 tick，也没有 `TickContext`。
真要开插件自己的模型调用，请把它做成一个 `Capability` 或 `Stage`。

### 4.4 目录：用 `ctx.paths`，不要拼字符串

```python
cache = ctx.paths.cache_dir / "my_plugin"
cache.mkdir(parents=True, exist_ok=True)
```

`ctx.paths.ensure_dirs()` 会把五个目录都建好。**不要**写 `Path("data")` 或
`os.path.expanduser("~/.alterego")`——那些路径由用户配置决定，
硬编码会写到别的用户意想不到的地方。

---

## 5. 六种「我提供什么」

只有一条注册路径：`ctx.registry.register(接口, 实例, name=..., priority=...)`。
`name` 是**同一接口下的实例名**，别的插件靠它取你——不是靠 `import` 你的类
（那会破坏插件隔离）。

```python
from alterego.interfaces.simulation import Capability, CapabilityResult


class MyCapability(Plugin):
    id: str = "capability.my_plugin"
    intent_types: frozenset[str] = frozenset({"post_moment"})   # 我能执行哪些意图

    def on_load(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        ctx.registry.register(Capability, self, name="my_plugin")

    async def execute(self, intent: Any, ctx: Any) -> CapabilityResult:
        return CapabilityResult(ok=True, summary="做了一件事", artifacts={"k": "v"})
```

| 你提供 | 注册的接口 | import 自 | `name` 的约定 |
| --- | --- | --- | --- |
| 执行意图的能力 | `Capability` | `interfaces.simulation` | 短名，如 `"my_plugin"` |
| 给 LLM 用的工具 | `Tool` | `interfaces.simulation` | 工具名，见 `Tool.name` |
| 推演流水线的一阶段 | `Stage` | `interfaces.simulation` | `Stage.name` |
| 一个渠道 | `Channel` | `interfaces.channel` | **就是你的 `id`**，如 `"channel.web"` |
| 一个 LLM 供应商 | `LLMProvider` | `interfaces.llm` | 供应商短名 |
| 一个存储后端 | `StorageBackend` | `interfaces.storage` | 后端短名 |

> `CapabilityResult.summary` 会写进 `activity_log` 并在 Web 上展示——**说人话**。
> `"处理完成"` 没有信息量，`"说了一句「你好！」"` 有。

### 5.1 同名冲突

同一个 `(接口, name)` 注册两次会**抛异常**，不会被覆盖。
两个插件争同一个名字时，先加载的那个赢（拓扑序决定谁先）——所以
`alterego plugins doctor` 报错时，先看 `priority` 和依赖关系。

`priority` 只在**同一个接口有多个不同 name** 时起作用：`get_service` 取优先级最高的那个。

---

## 6. 实现一个渠道（如果你要写 `kind = "channel"`）

渠道比能力复杂，因为它有方向。三条硬约束：

1. **v1 只做出站**（[ADR-0004](../adr/0004-im-channels-outbound-only-in-v1.md)）。
   `direction = frozenset({"in", "out"})` 里的 `"in"` 今天用不上。
2. `capabilities` 要**如实声明**。声明了 `"markdown"` 但发送时不带格式，
   上游会按 markdown 排版，用户看到一堆 `**`。
3. `send()` 要返回 `SendResult`，**失败要如实报 `ok=False`**。
   把失败吞掉返回成功，会让打扰预算记为「已送达」，它就会以为你收到了。

```python
from alterego.interfaces.channel import (
    Channel,
    ChannelCapability,
    Direction,
    OutboundMessage,
    SendResult,
)


class MyChannel(Plugin):
    id: str = "channel.my_channel"
    direction: Direction = frozenset({"out"})                      # 类型在这里用得上
    capabilities: frozenset[ChannelCapability] = frozenset({"text"})

    def on_load(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        ctx.registry.register(Channel, self, name=self.id)

    async def send(self, message: OutboundMessage) -> SendResult:
        ...
```

> ⚠️ **`channels/` 目录下的内置渠道不能靠 `plugin.toml` 被发现。**
> 包内没有内置插件的搜索路径，`channels/web/` 是由组装根
> （`cli_serve`）**在 `manager.load_all()` 之前手动注册**的。
> 第三方渠道请放 `plugins/<目录>/`，或做成 pip 包用 entry point（见 § 9.2）。

---

## 7. 依赖另一个插件

```toml
[plugin]
requires = ["storage.my_backend >= 0.1.0"]   # 缺了就不加载我
optional = ["capability.obsidian_vault"]     # 缺了也能加载
provides = ["capability.my_plugin"]          # 我提供什么（可被别人依赖）
```

依赖声明的语法：

| 写法 | 含义 |
| --- | --- |
| `"tool.other"` | 只要有这个插件就行，不管版本 |
| `"tool.other >= 0.1.0"` | 大于等于 |
| `"capability.example == 0.1.0"` | 精确版本 |
| `"storage.backend ~= 0.1.0"` | 兼容版本：`>= 0.1.0` 且 `< 0.2.0` |

比较符只有四个：`>=` `<=` `==` `~=`。漏了比较符而写成 `"storage.x 0.1.0"`
会**报错**，不会被当成「无约束」默默放过。

### 7.1 内核会替你做三件事

1. **拓扑排序**：保证被依赖的先加载。你不用管顺序。
2. **顺带启用**：你 `requires` 的插件如果没在 `[plugins] enabled` 里，
   内核会把它一起启用（`doctor` 会告诉你哪些是顺带装的）。
3. **`optional` 缺失就跳过**，不影响你加载。代码里用
   `ctx.get_optional_service(...)` 拿它，拿不到就降级：

```python
vault = ctx.get_optional_service(Capability, name="obsidian_vault")
if vault is None:
    ctx.logger.info("没装 obsidian_vault，跳过导出")
    return
```

### 7.2 退出码 3

依赖成环、或 `requires` 的插件根本不存在时，`alterego plugins doctor`
以 **3** 退出（`EXIT_DEPENDENCY_ERROR`）。脚本靠这个区分
「配置写错了」（其他非零码）和「插件之间接不起来」。

### 7.3 插件之间不要互相 import

```python
from alterego_plugins.capability.other import OtherCapability   # ✗ 红线
```

别的插件可能在另一个用户目录里、甚至没被安装。跨插件协作只有一条路：
**通过接口注册表按名字找**。想找的能力没注册，就用 `optional` 降级。

---

## 8. 状态：`ctx.state`

```python
def on_load(self, ctx: PluginContext) -> None:
    self._ctx = ctx
    self._count = int(ctx.state.get("count", 0))

def on_event(self, event: Any) -> None:
    self._count += 1
    self._ctx.state.set("count", self._count)     # 只记账，不落盘
```

- `set()` 的值**必须 JSON 可序列化**，否则当场抛 `PluginError`。
- `set()` **不落盘**：一个 tick 里改十几次状态，逐次写库没有必要。
  批量落盘由内核在 tick 结束时做。
- 支持 `get` / `set` / `delete` / `update` / `keys` / `clear` / `snapshot`。

> ⚠️ **今天这条链没有接线（已知缺口）。** `plugin_state` 表、
> `PluginState`、`flush_state()` 都写好了，但**没有一个组装根**把
> `state_loader` / `state_sink` 传给管理器，`storage/sqlite/` 里也没有对应的仓储。
> 后果具体而隐蔽：**`ctx.state` 今天能正常读写，但进程重启后拿不回来**——
> 没有 sink 时挂起的写入会被直接丢弃。
>
> 所以跨重启要留下的东西，请放**数据库**或 `ctx.paths.config_dir` 下的用户配置，
> 不要放这里。跟踪项：[`13-interface-consistency.md`](../design/13-interface-consistency.md) § 5.2。

---

## 9. 加载来源与热重载

### 9.1 本地插件（推荐）

```
plugins/my_plugin/
├── plugin.toml
└── plugin.py
```

搜索路径由 `[plugins] search_paths` 决定。**默认只有 `plugins/` 一处**——
`kernel/loader.py` 里的 `DEFAULT_SEARCH_PATHS` 虽然写着 `plugins/` 与
`~/.alterego/plugins` 两个，但 `PluginManager` 每次扫描都会把
`Config.plugins.search_paths` 显式传进去，所以**以配置为准**。
要加第二个目录，就在用户配置里写：

```toml
[plugins]
search_paths = ["plugins", "~/.alterego/plugins"]
```

**每个插件一个目录**，`plugin.toml` 直接放在目录里（不是它的子目录）。

> ⚠️ **目录里有 `.py` 就必须同时有 `__init__.py`。**
> 这是架构检查的第 23 项（`scripts/check_architecture.sh`）：任何含 `.py` 的目录
> 都必须有 `__init__.py`，插件目录也算在内。
> 它的实际影响是「插件内部的 `import helpers` 能不能找到自己的邻居」——
> 缺了它报错会发生在 import 别的模块的时候，很难查。

### 9.2 pip 包（entry point）

在包自己的 `pyproject.toml` 里声明：

```toml
[project.entry-points."alterego.plugins"]
"capability.my_plugin" = "my_package.plugin:MyPlugin"
```

**entry point 的写法与本地插件不同**：`entry` 写在**包自己的** `pyproject.toml`
里（`id = 模块路径:类名`），此时不需要 `plugin.toml`——但需要模块级
`MANIFEST` 映射：

```python
MANIFEST = {
    "id": "capability.my_plugin",
    "version": "0.1.0",
    "api_version": 1,
    "kind": "capability",
    "entry": "my_package.plugin:MyPlugin",
}
```

⚠️ **entry point 插件不能热重载。** `reload` 只支持本地插件（要重装包本身才行）。

### 9.3 热重载

- 只对**本地**插件生效；需要 `[plugins] auto_reload = true`。
- 监视的是 `.py` 与 `.toml` 的 **mtime**，1 秒轮询一次（不用 `watchdog`，P5）。
- 手动触发：`alterego plugins reload capability.my_plugin`。
- **重载失败不恢复旧实例**：旧实例可能已经半死，把它请回来只会让问题更难查。
  修好代码再跑一次。
- 重载期间 `on_stop` 会被调用——这就是「必须幂等」的原因。

---

## 10. 出错会怎样（错误隔离与熔断）

内核在**七个位置**都套了隔离：`on_load`、`on_start`、`on_stop`、
`on_unload`、`on_config_changed`、`on_tick_*`、`on_event`。

- 默认 `[plugins] isolate_failures = true`：某个插件抛异常**不会**带崩整个系统，
  只会让它自己记一次失败。
- 同一个插件连续失败 `circuit_breaker_threshold`（默认 **5**）次后**熔断**——
  它被停用，钩子不再被调用，事件总线上会发一条 `plugin.circuit_opened`。
- 熔断状态活在**内存**里（进程重启就没了）。`alterego plugins reset <id>`
  在一次性进程里几乎总是回答「它本来就没被熔断」，这句话是真的。

**写插件时的含义**：钩子里的异常你不需要自己 try/except 到底——
但要保证**抛出去之后你的对象还能被安全地 `on_stop`**，
否则熔断发生时清理会失败。

---

## 11. 测试你的插件

三个构件：一个临时插件目录、一个最小 `Config`、一个 `PluginManager`。

```python
from alterego.kernel.config import Config


def make_config(plugins_dir, **plugin_kwargs):
    """一个只改了插件搜索路径的最小配置。"""
    return Config(...)          # 照抄 tests/test_kernel_manager.py 的 make_config


def test_my_plugin_loads(tmp_path):
    root = tmp_path / "plugins"
    install(root, "capability.my_plugin", MY_BODY)      # 写 plugin.toml + plugin.py
    pm = make_manager(tmp_path, clock=clock, bus=bus, registry=registry)

    report = pm.load_all()

    assert report.ok
    assert pm.status_of("capability.my_plugin") is PluginStatus.STARTED
```

`tests/test_kernel_manager.py` 里有四个可以直接抄的 helper：
`install()`（写一个插件目录）、`make_config()`、`make_manager()`、`collect_events()`。

### 11.1 值得写的四类测试

| 测什么 | 怎么写 |
| --- | --- |
| 装得上 | `report.ok` 为真，`status_of(id) is PluginStatus.STARTED` |
| 服务注册了 | `registry.get(Capability, name="my_plugin")` 能取到 |
| 钩子被调了 | `collect_events(bus)` 收 `tick.completed`，断言你的副作用 |
| 状态机走对了 | 见 `tests/test_kernel_manager.py::TestStatusSteps` 的四个测试 |

### 11.2 两个容易踩的坑

- **`filterwarnings = ["error"]`**：任何库发一条 DeprecationWarning 都会让测试红。
  这不是 bug，是有意为之（升级依赖时会当场发现）。
- **`logging` 是进程级单例**：`tests/conftest.py` 有一个 autouse 的
  `_restore_logging` fixture 负责还原，你自己起的 logger 不用管它。

---

## 12. 常见错误速查

| 症状 | 原因 | 怎么办 |
| --- | --- | --- |
| 表里没有你的插件 | 目录结构错了 / 没有 `plugin.toml` | 一个插件一个目录，`plugin.toml` 直接放进去；跑 `alterego plugins list` 看「清单读不出来的目录」那一节 |
| 「发现到了但一个都没启用」 | `enabled_by_default = false` 且没写进 `enabled` | `list` 会直接给你可抄的那一行 |
| 「清单里有无法识别的键」 | 顶层键拼错（`enabledByDefault`） | 对照 § 2；拼错会被报出来，不要改成能过就删 |
| 「配置字段声明里有未知键」 | `[plugin.config.x]` 里的键拼错 | 对照 § 2.4，`requierd` / `defualt` 是最常见的 |
| 「v1 尚不支持嵌套的 object.schema」 | `type = "object"` 还写了 `schema` | 拆成平铺字段，或 `type = "string"` 存 JSON |
| 「依赖声明格式非法」 | 漏了比较符：`"storage.x 0.1.0"` | 写 `">= 0.1.0"`，或不写版本 |
| 「插件依赖的版本不满足」 | 要求太严 | 放宽 `requires`，或用 `~=` |
| 「API 版本不兼容」 | `api_version` 不是 `0` / `1` | 内核今天是 `1`，兼容窗口是 `0 <= 内核 - 插件 <= 1` |
| 装上了但什么都没发生 | `kind` 与实现的接口不匹配 | `kind` 现在只是元数据，内核**不校验**它——它填错不会有报错，只会让人误会。对照 § 1.2 |
| 「插件状态必须是 JSON 可序列化的」 | `ctx.state.set()` 收到了 `datetime` 之类的对象 | 转成 ISO 字符串再存 |
| 重载之后行为没变 | pip 装的插件不能热重载 | 重装包；或改用本地插件 |
| 用户改了配置没生效 | 没实现 `on_config_changed` | 实现它，或告诉用户需要重启 |

---

## 13. 插件不能做的事（红线）

这些有**自动化检查**，违规会在 CI 里红：

| 不能 | 为什么 | 改成 |
| --- | --- | --- |
| `import random` 后用全局随机 | 不可复现 | `ctx.rng` |
| `datetime.now()` | 虚拟时钟才是它的「现在」 | `ctx.now()` / `ctx.clock.now()` |
| 自己建 `logging.FileHandler` | 脱敏与轮转由内核统一负责 | `ctx.logger` |
| 直接 `httpx.AsyncClient(...)` 调 LLM | 模型路由、成本统计、重试、`correlation_id` 都在内核里 | `TickContext.llm(purpose, prompt)`——**只在 `sim.TickContext` 上有**，见 § 4.3 |
| import 另一个插件 | 它可能不在，破坏隔离 | `ctx.get_service(...)` |
| 在 `on_load` 里阻塞等网络 | `on_load` 是同步的，会卡住整个启动 | 放 `on_start`，或起后台任务 |
| 往 `plugin.toml` 写不认识的键 | 静默失效比报错更危险 | 对照 § 2 |

`format` / `lint` / `type` 三件事由仓库统一配置（`ruff format` / `ruff check` /
`mypy`），提交前跑一次即可——插件目录 `plugins/` 也在检查范围内。

---

## 14. 调试命令一览

```bash
alterego plugins list                    # 发现到了什么、哪些会被加载（不导入插件代码）
alterego plugins doctor                  # 真装一遍，报加载结果与健康状态（失败退出码 1，依赖问题 3）
alterego plugins info capability.x       # 一个插件的清单与配置现状（不导入它的代码）
alterego plugins reload capability.x     # 卸掉再装一次（只支持本地插件）
alterego plugins reset capability.x      # 解除熔断（内存里的状态，重启就没了）

alterego config explain <配置键>          # 「它是什么、改了会怎样、现在是多少」
```

`list` 与 `info` **不会导入你的代码**——所以它们在你写坏插件时照样能用。
这是排查时最重要的性质：出错时你仍然有一个能看清单的工具。

---

## 15. 交付清单

写完之后逐条过一遍：

- [ ] `plugin.toml` 的 `id` 与 `plugin.py` 的 `id` 类属性**完全一致**
- [ ] `api_version = 1` 是**整数**
- [ ] `kind` 是 § 1.2 里能跑的那六种之一
- [ ] `entry` 指向的模块路径**相对插件根目录**
- [ ] `enabled_by_default = false`（除非你确实想让它默认跑）
- [ ] 每个 `[plugin.config.*]` 都有 `description`
- [ ] 密钥类字段标了 `secret = true`
- [ ] `on_stop` 幂等
- [ ] 订阅的事件都有实际处理逻辑
- [ ] 跨重启要留的东西**没有**放在 `ctx.state`（见 § 8）
- [ ] 目录里有 `__init__.py`
- [ ] `alterego plugins doctor` 全绿
- [ ] 有测试覆盖「装得上 / 注册了 / 钩子被调了」

---

## 变更记录

| 日期 | 版本 | 变更 | 作者 |
| --- | --- | --- | --- |
| 2026-09-16 | v0.1.2 | 首版。依据是一次完整的接口一致性审计（`13-interface-consistency.md`）：所有命令、字段、类型均取自当时代码 | LMG-arch |

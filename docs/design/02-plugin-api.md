# 02 · 插件 API 规范

> 上级文档：[DESIGN.md](../DESIGN.md) · 版本 v0.1.0 · **api_version = 1**
> 本文档面向**插件开发者**。项目的一切可变部分都通过插件实现——LLM、存储、渠道、行为、推演阶段。

---

## 目录

1. [插件哲学](#1-插件哲学)
2. [插件类型（kind）](#2-插件类型kind)
3. [插件清单 plugin.toml](#3-插件清单-plugintoml)
4. [生命周期](#4-生命周期)
5. [PluginContext](#5-plugincontext)
6. [能力接口定义](#6-能力接口定义)
7. [扩展点（Hook）](#7-扩展点hook)
8. [依赖管理](#8-依赖管理)
9. [加载来源](#9-加载来源)
10. [热重载](#10-热重载)
11. [错误隔离](#11-错误隔离)
12. [插件状态持久化](#12-插件状态持久化)
13. [完整示例：钉钉渠道插件](#13-完整示例钉钉渠道插件)
14. [完整示例：自定义推演阶段](#14-完整示例自定义推演阶段)
15. [完整示例：新增意图类型](#15-完整示例新增意图类型)
16. [开发 Checklist](#16-开发-checklist)

---

## 1. 插件哲学

### 1.1 三条原则

| 原则 | 含义 |
| --- | --- |
| **内核无知** | 内核不认识任何具体插件，只认识接口。插件出错不影响内核 |
| **声明式契约** | 插件在 `plugin.toml` 中声明「我提供什么能力、我依赖什么」，内核据此编排 |
| **零内核修改** | 新增功能 = 新增插件目录。如果必须改内核，说明抽象有误 |

### 1.2 什么该做成插件

| 场景 | 是否插件 |
| --- | --- |
| 接入新的 LLM 供应商 | ✅ `kind = "llm"` |
| 换用 Postgres / MongoDB 存储 | ✅ `kind = "storage"` |
| 增加 Telegram / QQ 渠道 | ✅ `kind = "channel"` |
| 增加「拍照发动态」能力 | ✅ `kind = "capability"` |
| 在推演循环中插入自定义阶段 | ✅ `kind = "stage"` |
| 给 LLM 增加可调用工具 | ✅ `kind = "tool"` |
| 修改核心人格数据结构 | ❌ 这是内核/领域层的职责，需走 ADR |

### 1.3 版本兼容

`plugin.toml` 中的 `api_version` 声明插件面向的插件 API 版本。

| 插件 api_version | 内核 api_version | 结果 |
| --- | --- | --- |
| `1` | `1` | ✅ 兼容 |
| `1` | `2` | ✅ 兼容（内核保证向后兼容 1 个大版本） |
| `2` | `1` | ❌ 不兼容，插件标记 `Failed`，提示内核版本过低 |
| `1` | `3` | ❌ 不兼容，超出兼容窗口 |

**内核承诺**：`api_version` 的 `MAJOR` 递增即代表不兼容变更。这对应 SemVer 中项目版本的 `MAJOR`。

---

## 2. 插件类型（kind）

| kind | 必须实现 | 典型文件 | 内置实现 |
| --- | --- | --- | --- |
| `llm` | `on_load` 中注册 `LLMProvider` | `plugin.py` | `llm.openai_compatible` |
| `storage` | `on_load` 中注册 `StorageBackend` 及 `*Repository` | `plugin.py`, `repo/` | `storage.sqlite` |
| `channel` | 实现 `Channel` 协议并注册 | `plugin.py` | `channel.file`、`channel.web`、`channel.wecom_webhook`、`channel.dingtalk_webhook` |
| `capability` | 实现 `Capability` 协议并注册 | `plugin.py` | `capability.activity`、`capability.post`、`capability.chat` |
| `stage` | 实现 `Stage` 协议并注册到 pipeline | `plugin.py` | `stage.sense/reflect/intention/act/express/persist` |
| `tool` | 实现 `Tool` 协议并注册 | `plugin.py` | `tool.time_query`、`tool.web_search`（可选） |

一个插件**只能声明一个 kind**。若需要提供多种能力，拆成多个插件（内聚性更好，也便于单独启停）。

---

## 3. 插件清单 plugin.toml

### 3.1 完整字段参考

```toml
[plugin]
# ── 必填 ──────────────────────────────────────────────
id          = "channel.dingtalk_webhook"   # 全局唯一。约定 "<kind>.<name>"，全小写+下划线
version     = "0.1.0"                       # 插件自身版本，SemVer
api_version = "1"                           # 面向的插件 API 主版本
kind        = "channel"                     # llm|storage|channel|capability|stage|tool
entry       = "plugin:DingtalkWebhookChannel"  # "<模块>:<类名>"，模块路径相对于插件根目录

# ── 选填（推荐填写） ──────────────────────────────────
name        = "钉钉自定义机器人"              # 人类可读名称（缺省用 id）
description = "通过钉钉自定义机器人 Webhook 推送消息，支持 Markdown 与 @ 提醒"
authors     = ["LMG-arch"]
license     = "MIT"
homepage    = "https://github.com/LMG-arch/alter-ego"
tags        = ["im", "china", "webhook", "outbound"]

# ── 依赖与提供 ────────────────────────────────────────
requires    = ["storage.sqlite >= 0.1.0"]   # 依赖的插件 id + 版本约束
provides    = ["channel"]                   # 提供的接口名（用于依赖解析）
optional    = ["llm.openai_compatible"]     # 可选依赖：缺失时插件仍需能加载

# ── 运行控制 ──────────────────────────────────────────
enabled_by_default = true
priority           = 10                     # 同接口多实现时的选中优先级，数值大者优先
auto_reload        = true                   # 是否支持文件变化热重载（仅本地插件有效）

# ── 配置 Schema ───────────────────────────────────────
[config]
# 每个字段：type / required / default / description / secret / env / choices / min / max
webhook_url = { type = "string", required = true,  secret = true,
                env = "DINGTALK_WEBHOOK",
                description = "钉钉机器人 Webhook 地址" }
secret      = { type = "string", required = true,  secret = true,
                env = "DINGTALK_SECRET",
                description = "加签密钥，以 SEC 开头" }
at_mobiles  = { type = "array",  default = [],
                description = "需要 @ 的手机号列表" }
at_all      = { type = "boolean", default = false }
timeout_sec = { type = "integer", default = 10, min = 1, max = 60 }
max_retry   = { type = "integer", default = 3,  min = 0, max = 5 }
```

### 3.2 配置字段类型

| type | 映射的 Python 类型 | 额外约束键 |
| --- | --- | --- |
| `string` | `str` | `pattern`, `min_length`, `max_length` |
| `integer` | `int` | `min`, `max` |
| `number` | `float` | `min`, `max` |
| `boolean` | `bool` | — |
| `array` | `list` | `item_type`, `min_items`, `max_items` |
| `object` | `dict` | `schema`（嵌套字段定义） |
| `duration` | `timedelta` | 支持 `"30s"`, `"5m"`, `"2h"`, `"1d"` |
| `path` | `Path` | `must_exist` |

### 3.3 特殊配置键

| 键 | 说明 |
| --- | --- |
| `secret = true` | 该字段在任何日志输出、CLI 展示、Web 界面中**永远脱敏**为 `***` |
| `env = "VAR"` | 从环境变量读取。若用户 config 中也提供了值，**环境变量优先**（便于容器部署） |
| `required = true` | 缺失则插件加载失败，错误信息会明确指出缺少哪个配置项 |
| `default` | 缺省值。与 `required = true` 互斥 |

### 3.4 清单校验

`PluginLoader` 在加载前校验：

1. `id` 符合 `^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$`
2. `kind` 在允许枚举内
3. `api_version` 与内核兼容
4. `entry` 格式为 `模块:类名`，且模块可导入、类可解析
5. `config` 中每个字段的 `type` 合法，且 `required` 与 `default` 不同时存在
6. `requires` 中每个 id 在已发现插件集合中（或可通过 pip 安装）

校验失败 → 该插件标记 `Failed` 并给出精确到字段的错误信息：

```
✗ 插件 channel.dingtalk_webhook 加载失败
  plugin.toml:14  config.webhook_url
  错误: required = true 的字段缺少值
  修复: 在 config/alterego.toml 的 [plugins.channel.dingtalk_webhook] 中设置，
        或设置环境变量 DINGTALK_WEBHOOK
```

---

## 4. 生命周期

### 4.1 基类

```python
# alterego/interfaces.py（内核提供，插件只需 import）

class Plugin(ABC):
    """所有插件的基类"""

    manifest: PluginManifest        # 由 PluginManager 在实例化后注入

    def on_load(self, ctx: "PluginContext") -> None:
        """加载阶段。在此注册能力、订阅事件、读取配置。
        此时不要做网络请求或长时间操作。
        此阶段抛异常 → 插件标记 Failed，其余插件继续启动。"""

    def on_start(self) -> None:
        """启动阶段。依赖此插件的所有插件均已 load 完成。
        可以在此启动后台任务、建立连接。"""

    def on_stop(self) -> None:
        """停止阶段（逆拓扑序）。释放连接、停止后台任务。
        必须幂等，可能被调用多次。"""

    def on_unload(self) -> None:
        """卸载阶段。清理注册表引用。"""

    def on_config_changed(self, new_config: dict) -> None:
        """配置热更新回调（可选实现）。"""

    def on_tick_pre(self, ctx: "TickContext") -> None:
        """每个 tick 开始时调用（可选实现）。"""

    def on_tick_post(self, ctx: "TickContext") -> None:
        """每个 tick 结束时调用（可选实现）。"""

    def on_event(self, event: "Event") -> None:
        """收到订阅的事件（可选实现）。通常直接用 ctx.bus.subscribe。"""

    def health(self) -> "HealthStatus":
        """健康检查（可选实现）。alterego plugins doctor 会调用。"""
```

### 4.2 状态机

```mermaid
stateDiagram-v2
    [*] --> Discovered: loader 扫描 plugins/ 与 entry_points
    Discovered --> Validated: 校验 plugin.toml 通过
    Discovered --> Failed: 清单非法 / api_version 不兼容

    Validated --> Loading: 依赖就绪
    Loading --> Loaded: import + 实例化 + on_load(ctx) 成功
    Loading --> Failed: import 失败 / on_load 抛异常

    Loaded --> Started: 依赖全部 Started 后 on_start()
    Started --> Stopped: on_stop() 成功
    Started --> Failed: 运行时未捕获异常

    Stopped --> Loading: reload()（仅本地插件且 auto_reload=true）
    Stopped --> Unloaded: on_unload()
    Unloaded --> [*]

    Failed --> [*]: 需手动 plugins enable 后重启
```

### 4.3 时序

```mermaid
sequenceDiagram
    autonumber
    participant PM as PluginManager
    participant P as Plugin 实例
    participant Ctx as PluginContext
    participant Reg as ServiceRegistry
    participant Bus as EventBus

    Note over PM: ── 加载阶段（按拓扑序）──
    PM->>P: 实例化（无参构造）
    PM->>P: 注入 manifest
    PM->>Ctx: 构造 PluginContext(plugin_id, config, ...)
    PM->>P: on_load(ctx)
    P->>Ctx: ctx.logger.info("加载中")
    P->>Reg: ctx.registry.register(Channel, self, name=..., priority=...)
    P->>Bus: ctx.bus.subscribe("message.received", self.on_event)
    P->>Ctx: ctx.state.set("initialized_at", now)

    Note over PM: ── 启动阶段（按拓扑序）──
    PM->>P: on_start()
    P->>P: 启动后台任务 / 验证连通性

    Note over PM: ── 运行阶段 ──
    loop 每个 tick
        PM->>P: on_tick_pre(ctx)
        PM->>P: on_event(event)  ← 订阅的事件
        PM->>P: on_tick_post(ctx)
    end

    Note over PM: ── 停止阶段（逆拓扑序）──
    PM->>P: on_stop()
    P->>P: 取消后台任务 / 关闭连接
    PM->>P: on_unload()
    P->>Reg: ctx.registry.unregister(...)
```

---

## 5. PluginContext

`PluginContext` 是插件访问系统的**唯一入口**。插件不得直接 `import` 内核的其他模块或全局单例。

```python
@dataclass(frozen=True)
class PluginContext:
    # ── 身份 ──
    plugin_id: str
    manifest: PluginManifest

    # ── 配置 ──
    config: dict[str, Any]
    """已校验、已填充默认值、已解析 ${ENV} 的插件配置字典。
    密钥字段也在其中（插件自己需要真实值）。"""

    # ── 基础设施 ──
    logger: logging.Logger
    """已绑定插件 id 的 logger，输出自动带 plugin_id 字段"""

    bus: EventBus
    """事件总线：订阅与发布"""

    registry: ServiceRegistry
    """能力注册表：注册自己提供的实现，获取自己依赖的实现"""

    clock: Clock
    """时钟：所有时间判断必须用它，保证倍速仿真正确"""

    scheduler: Scheduler
    """调度器：注册周期任务"""

    state: PluginState
    """插件持久化 KV 存储（底层为 plugin_state 表）"""

    paths: PluginPaths
    """路径工具：data_dir / config_dir / plugin_dir / cache_dir"""

    rng: random.Random
    """插件专属随机源（由全局 seed 派生），保证可复现"""

    # ── 便捷方法 ──
    def get_service(self, interface: type[T], name: str | None = None) -> T: ...
    def get_optional_service(self, interface: type[T], name: str | None = None) -> T | None: ...
    def publish(self, topic: str, payload: dict) -> None: ...
    def now(self) -> datetime: ...      # == clock.virtual_now()
```

### 5.1 `PluginPaths`

```python
@dataclass(frozen=True)
class PluginPaths:
    data_dir: Path      # data/plugins/<plugin_id>/（自动创建，可写）
    cache_dir: Path     # data/plugins/<plugin_id>/cache/（可清理）
    config_dir: Path    # config/
    plugin_dir: Path    # 插件自身所在目录（只读）
    alterego_dir: Path  # 项目根
```

### 5.2 `PluginState`

```python
class PluginState:
    def get(self, key: str, default: Any = None) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...      # 值必须 JSON 可序列化
    def delete(self, key: str) -> None: ...
    def keys(self) -> list[str]: ...
    def clear(self) -> None: ...
    def update(self, mapping: dict[str, Any]) -> None: ...  # 批量，单事务
```

> **实现提示**：`set()` 不会立即写盘。`PluginManager` 在每个 tick 结束时批量 flush，减少 SQLite 写入次数。

---

## 6. 能力接口定义

接口定义在 `alterego/interfaces.py`（内核提供），插件实现并注册。

### 6.1 `LLMProvider`

```python
@dataclass(frozen=True)
class LLMRequest:
    prompt: str
    system: str | None = None
    temperature: float = 0.8
    max_tokens: int = 1024
    stop: tuple[str, ...] = ()
    response_format: Literal["text", "json"] = "text"
    json_schema: dict | None = None
    timeout_sec: float = 60.0
    metadata: dict[str, Any] = field(default_factory=dict)   # tier / purpose，用于计量

@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    latency_ms: int
    raw: dict | None = None

class LLMProvider(Protocol):
    id: str
    tier: Literal["strong", "cheap", "custom"]
    models: tuple[str, ...]

    async def complete(self, req: LLMRequest) -> LLMResponse: ...
    async def aclose(self) -> None: ...
    def health_check(self) -> "HealthStatus": ...
```

### 6.2 `Channel`

```python
@dataclass(frozen=True)
class OutboundMessage:
    kind: Literal["text", "markdown", "image", "card"]
    content: str
    title: str | None = None
    image_path: Path | None = None
    mention: tuple[str, ...] = ()          # 用户标识，语义由渠道解释
    mention_all: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class InboundMessage:
    channel_id: str
    sender_id: str
    sender_name: str
    content: str
    received_at: datetime
    raw: dict | None = None

@dataclass(frozen=True)
class SendResult:
    ok: bool
    error: str | None = None
    retryable: bool = False
    message_id: str | None = None

class Channel(Protocol):
    id: str
    direction: frozenset[Literal["in", "out"]]
    capabilities: frozenset[str]        # "text" | "markdown" | "image" | "mention" | "card" | "long_text"

    async def send(self, msg: OutboundMessage) -> SendResult: ...
    def on_receive(self, handler: Callable[[InboundMessage], None]) -> None: ...
    async def aclose(self) -> None: ...
    def health_check(self) -> "HealthStatus": ...
```

### 6.3 `StorageBackend`

```python
class StorageBackend(Protocol):
    def migrate(self) -> str: ...                       # 返回应用后的 schema_version
    def transaction(self) -> ContextManager[None]: ...
    def flush(self) -> None: ...
    def checkpoint(self) -> None: ...
    def close(self) -> None: ...

    # 各 Repository 通过 registry 单独获取
```

### 6.4 `Capability`

```python
class Capability(Protocol):
    id: str
    intent_types: frozenset[str]        # 该能力可以执行哪些意图，如 {"post_moment"}

    async def execute(self, intent: "Intent", ctx: "TickContext") -> "CapabilityResult": ...

@dataclass
class CapabilityResult:
    ok: bool
    summary: str                        # 一句话描述做了什么，写入 activity_log
    artifacts: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
```

### 6.5 `Stage`

```python
class Stage(Protocol):
    name: str
    order: int                          # 决定在流水线中的位置
    depends_on: tuple[str, ...]         # 依赖的其他阶段名
    enabled: bool

    async def run(self, ctx: "TickContext") -> "StageResult": ...
```

### 6.6 `Tool`

```python
class Tool(Protocol):
    name: str                           # LLM function calling 用的名字
    description: str                    # 给 LLM 看的说明
    parameters: dict                    # JSON Schema

    async def invoke(self, **kwargs: Any) -> Any: ...
```

### 6.7 `EmbeddingProvider`（可选）

```python
class EmbeddingProvider(Protocol):
    id: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...
```

---

## 7. 扩展点（Hook）

内核在固定位置调用插件。这是「不改内核就能扩展行为」的机制。

| 扩展点 | 触发时机 | 可注册实现 | 典型用途 |
| --- | --- | --- | --- |
| `provide_llm` | `on_load` 中注册 | `LLMProvider` | 接入新模型供应商 |
| `provide_storage` | `on_load` 中注册 | `StorageBackend` + Repository | 换数据库 |
| `provide_channel` | `on_load` 中注册 | `Channel` | 接入新消息渠道 |
| `provide_capability` | `on_load` 中注册 | `Capability` | 新增行为类型 |
| `provide_tool` | `on_load` 中注册 | `Tool` | 给 LLM 新工具 |
| `provide_embedding` | `on_load` 中注册 | `EmbeddingProvider` | 语义检索 |
| `pipeline` | `on_load` 中注册 | `Stage` | 插入推演阶段 |
| `intent_catalog` | `on_load` 中注册 | `IntentType` | 新增意图类型 |
| `prompt_source` | `on_load` 中注册 | `PromptSource` | 从数据库/远程加载提示词 |
| `on_tick_pre` | 每 tick 开始 | 插件方法 | 采集指标、状态快照 |
| `on_tick_post` | 每 tick 结束 | 插件方法 | 上报、清理 |
| `on_event` | 事件发布时 | 订阅回调 | 响应系统事件 |

### 7.1 注册示例

```python
def on_load(self, ctx: PluginContext) -> None:
    # 注册能力
    ctx.registry.register(Channel, self, name=self.id, priority=self.config.get("priority", 0))

    # 注册推演阶段
    ctx.registry.register(Stage, MyCustomStage(ctx), name="stage.mood_weather")

    # 注册意图类型
    ctx.registry.register(IntentType, TakePhotoIntent(), name="intent.take_photo")

    # 订阅事件
    ctx.bus.subscribe("post.created", self._on_post_created, priority=10)

    # 注册定时任务
    ctx.scheduler.every(timedelta(hours=1), self._hourly, name=f"{self.id}.hourly")
```

### 7.2 完整扩展点契约

```python
class PromptSource(Protocol):
    """允许从数据库、远程服务等加载提示词模板"""
    id: str
    priority: int
    def load(self, name: str) -> str | None: ...
    def list_names(self) -> list[str]: ...

@dataclass(frozen=True)
class IntentType:
    """意图类型定义"""
    name: str                           # "reach_out"
    description: str                    # 给 LLM 看的说明
    category: Literal["internal", "social", "outbound"]
    default_weight: float
    requires_capability: str | None     # 需要哪个 capability 执行
    parameters_schema: dict             # LLM 输出该意图时必须提供的参数 schema
    outbound: bool                      # 是否会产生对用户可见的内容
    budget_kind: str | None             # "message" | "post" | None，对应打扰预算类别
```

---

## 8. 依赖管理

### 8.1 声明

```toml
requires = ["storage.sqlite >= 0.1.0", "llm.openai_compatible"]
optional = ["channel.web"]
```

支持版本约束：`>=`、`<=`、`==`、`~=`（兼容版本）、无约束。

### 8.2 解析流程

```mermaid
flowchart TD
    A["收集所有 manifest"] --> B{"enabled 列表中的<br/>插件都存在？"}
    B -->|否| B1["报错退出<br/>提示 plugins doctor"]
    B -->|是| C["构建依赖图"]
    C --> D{"存在环？"}
    D -->|是| D1["列印环上的插件<br/>退出码 3"]
    D -->|否| E["拓扑排序"]
    E --> F{"requires 的目标<br/>已启用？"}
    F -->|否| F1["自动启用依赖<br/>记录警告日志"]
    F -->|是| G
    F1 --> G["按拓扑序加载"]
    G --> H{"optional 的目标<br/>存在？"}
    H -->|是| I["加载"]
    H -->|否| J["跳过，插件需自行处理缺失"]
    I --> K["按拓扑序启动"]
    J --> K
```

### 8.3 依赖缺失的降级

`optional` 中的依赖缺失时，插件**必须仍能加载并运行**，只是功能降级：

```python
def on_load(self, ctx: PluginContext) -> None:
    web = ctx.get_optional_service(Channel, name="channel.web")
    if web is None:
        ctx.logger.warning("channel.web 不可用，长文本将截断发送")
        self.supports_long_text = False
    else:
        self.supports_long_text = True
```

---

## 9. 加载来源

### 9.1 本地 drop-in

扫描 `config.plugins.search_paths`（默认 `["plugins", "~/.alterego/plugins"]`）下的每个一级子目录，要求包含 `plugin.toml`。

```
plugins/
├── my_channel/
│   ├── plugin.toml       ← 必需
│   ├── plugin.py         ← entry 指向它
│   └── helpers.py        ← 可选，同目录可互相 import
```

**导入机制**：用 `importlib.util.spec_from_file_location` 以 `alterego_plugins.<plugin_id>` 为模块名加载，避免污染全局命名空间，也避免与 pip 包名冲突。

### 9.2 pip 分发（entry_points）

```toml
# 第三方插件的 pyproject.toml
[project.entry-points."alterego.plugins"]
dingtalk = "alterego_dingtalk.plugin:DingtalkWebhookChannel"
```

通过 `importlib.metadata.entry_points(group="alterego.plugins")` 发现。

### 9.3 优先级与冲突

| 情况 | 行为 |
| --- | --- |
| 同一 `id` 在本地目录和 entry_points 都存在 | **本地优先**（便于覆盖调试），记录警告 |
| 本地两个 search_path 都有同一 `id` | 按 `search_paths` 顺序，前者优先 |
| 两个插件提供同一接口且 priority 相同 | 内核不自动选择，需用户在 config 中显式指定 `default_provider` |

---

## 10. 热重载

### 10.1 触发条件

- 插件位于本地 drop-in 目录（entry_points 插件不支持热重载）
- `auto_reload = true`
- 文件 mtime 变化（插件目录下任意 `.py` 或 `.toml`）

### 10.2 实现

无 `watchdog` 依赖，用一个后台线程**每 1 秒轮询**插件目录的 mtime（对少量插件而言开销可忽略）。

```mermaid
sequenceDiagram
    autonumber
    participant W as Watcher 线程
    participant PM as PluginManager
    participant P as 旧实例
    participant N as 新实例
    participant Reg as ServiceRegistry
    participant Bus as EventBus

    W->>W: 检测到 plugins/my_channel/plugin.py mtime 变化
    W->>PM: reload("my_channel")
    PM->>PM: 获取 _reload_lock（暂停 tick 调度）
    PM->>Bus: publish("plugin.reloading", {id})

    PM->>P: on_stop()
    P->>P: 取消后台任务
    PM->>P: on_unload()
    PM->>Reg: 移除该插件注册的所有实现
    PM->>Bus: 移除该插件的订阅

    PM->>PM: 从 sys.modules 移除 alterego_plugins.my_channel
    PM->>PM: invalidate importlib caches

    PM->>N: 重新 import + 实例化 + on_load(ctx)
    PM->>N: on_start()
    PM->>Reg: 重新注册

    PM->>PM: 释放 _reload_lock
    PM->>Bus: publish("plugin.reloaded", {id, ok})
```

### 10.3 关键约束

| 约束 | 说明 |
| --- | --- |
| **重载失败回滚** | 新实例 `on_load` 失败 → 保留卸载状态，标记 `Failed`，发出告警；**不恢复旧实例**（状态可能已不一致） |
| **状态不丢失** | `ctx.state` 的数据在 `on_stop` 前已 flush 到库，重载后重新读取 |
| **重载期间暂停 tick** | 保证不出现「一半旧插件一半新插件」的中间态 |
| **手动触发** | `alterego plugins reload <id>` |

---

## 11. 错误隔离

### 11.1 隔离级别

| 阶段 | 异常处理 |
| --- | --- |
| 清单校验 | 无副作用，直接标记 `Failed` |
| `import` 模块 | 捕获 `ImportError`/`SyntaxError`，标记 `Failed`，**其余插件继续加载** |
| 实例化 | 同上 |
| `on_load` | 卸载已注册的实现（回滚），标记 `Failed`，继续 |
| `on_start` | 标记 `Failed`，尝试 `on_stop` 清理，继续 |
| 运行时 `on_event` | 记录 + 发 `plugin.failed`，**继续派发给其他订阅者** |
| 运行时 `Stage.run` | 该阶段失败 → 标记 tick 为 `partial`，跳过后续依赖它的阶段 |
| 运行时渠道 `send` | 记录失败，重试（若 `retryable`），不影响其他渠道 |

### 11.2 熔断

连续失败超过阈值（默认 5 次）的插件自动进入**熔断状态**，不再被调用，直到：

- `alterego plugins reset <id>` 手动重置
- 或进程重启

发出 `plugin.circuit_opened` 事件，Web 后台红色告警。

### 11.3 失败信息质量

失败信息必须包含**可操作的修复建议**：

```
✗ 插件 channel.dingtalk_webhook 运行时错误（第 3 次）
  错误: httpx.ConnectError: 无法连接到 oapi.dingtalk.com

  可能原因:
    1. 网络不可达或需要代理
    2. Webhook URL 已失效（钉钉机器人被删除或重置）

  建议操作:
    · 检查网络: curl -I https://oapi.dingtalk.com
    · 在钉钉群重新创建机器人并更新 DINGTALK_WEBHOOK
    · 如需代理: 在 config/alterego.toml 设置 [network] proxy = "http://..."

  该插件已连续失败 3 次，达到 5 次后将自动熔断。
```

---

## 12. 插件状态持久化

### 12.1 表结构

```sql
CREATE TABLE plugin_state (
    plugin_id   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,          -- JSON 序列化
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (plugin_id, key)
);
```

### 12.2 使用

```python
# 简单值
ctx.state.set("last_post_at", ctx.now().isoformat())
last_post = ctx.state.get("last_post_at")

# 复杂结构（自动 JSON 序列化）
ctx.state.set("retry_queue", [{"id": "m1", "attempts": 2, "next_at": "..."}])
queue = ctx.state.get("retry_queue", [])

# 批量（单事务）
ctx.state.update({"a": 1, "b": 2})
```

### 12.3 约定

| 约定 | 说明 |
| --- | --- |
| 值必须 JSON 可序列化 | 不支持的对象请自行转换 |
| 不要存大量数据 | 这是 KV 状态存储，不是数据仓库。大数据请用 `ctx.paths.data_dir` 下的文件 |
| 不要存密钥 | 密钥通过 config 注入，不落库 |
| 写入延迟 | `set()` 在内存中立即生效，磁盘写入在每个 tick 结束时批量 flush（可用 `ctx.state.flush()` 强制） |
| 卸载清理 | `alterego plugins uninstall <id>` 会询问是否一并删除其状态数据 |

---

## 13. 完整示例：钉钉渠道插件

展示一个**完整可运行**的渠道插件，涵盖配置、加签、重试、健康检查。

### 13.1 目录结构

```
plugins/dingtalk/
├── plugin.toml
└── plugin.py
```

### 13.2 `plugin.toml`

```toml
[plugin]
id          = "channel.dingtalk_webhook"
version     = "0.1.0"
api_version = "1"
kind        = "channel"
entry       = "plugin:DingtalkWebhookChannel"
name        = "钉钉自定义机器人"
description = "通过钉钉自定义机器人 Webhook 推送消息（单向出站），支持 Markdown 与 @ 提醒"
authors     = ["LMG-arch"]
license     = "MIT"
tags        = ["im", "china", "webhook", "outbound"]
provides    = ["channel"]
priority    = 10

[config]
webhook_url = { type = "string",  required = true, secret = true, env = "DINGTALK_WEBHOOK",
                description = "钉钉机器人 Webhook 地址" }
secret      = { type = "string",  required = true, secret = true, env = "DINGTALK_SECRET",
                description = "加签密钥，以 SEC 开头" }
at_mobiles  = { type = "array",   default = [],  description = "需要 @ 的手机号列表" }
at_all      = { type = "boolean", default = false }
timeout_sec = { type = "integer", default = 10, min = 1, max = 60 }
max_retry   = { type = "integer", default = 3,  min = 0, max = 5 }
enabled_for = { type = "array",   default = ["post", "message"],
                choices = ["post", "message"],
                description = "哪些内容类型推送于此渠道" }
```

### 13.3 `plugin.py`

```python
"""钉钉自定义机器人渠道插件。

单向出站渠道：只能推送消息，无法接收用户回复。
双向对话请使用 channel.web（v1）或 channel.wecom_app（v2）。

钉钉自定义机器人安全设置需选择「加签」，签名算法：
    string_to_sign = f"{timestamp}\n{secret}"
    sign = urlsafe_b64encode(hmac_sha256(secret, string_to_sign))
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import timedelta
from typing import Any
from urllib.parse import quote_plus

import httpx

from alterego.interfaces import (
    Channel, HealthStatus, OutboundMessage, SendResult,
)
from alterego.kernel.plugin import Plugin, PluginContext


class DingtalkWebhookChannel(Plugin, Channel):
    """钉钉自定义机器人渠道"""

    id = "channel.dingtalk_webhook"
    direction = frozenset({"out"})              # 只出站
    capabilities = frozenset({"text", "markdown", "mention"})

    # ── 生命周期 ────────────────────────────────────────

    def on_load(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        cfg = ctx.config

        self._webhook_url: str = cfg["webhook_url"]
        self._secret: str = cfg["secret"]
        self._at_mobiles: list[str] = cfg["at_mobiles"]
        self._at_all: bool = cfg["at_all"]
        self._max_retry: int = cfg["max_retry"]
        self._enabled_for: set[str] = set(cfg["enabled_for"])

        self._client = httpx.AsyncClient(
            timeout=cfg["timeout_sec"],
            headers={"Content-Type": "application/json"},
        )

        # 失败计数用于熔断
        self._consecutive_failures: int = 0
        self._circuit_open: bool = False

        ctx.registry.register(Channel, self, name=self.id, priority=self.manifest.priority)

        # 订阅需要推送的事件
        if "post" in self._enabled_for:
            ctx.bus.subscribe("post.created", self._on_post_created, priority=10)
        if "message" in self._enabled_for:
            ctx.bus.subscribe("message.created", self._on_message_created, priority=10)

        ctx.logger.info(
            "钉钉渠道已加载",
            extra={"enabled_for": sorted(self._enabled_for), "at_count": len(self._at_mobiles)},
        )

    def on_start(self) -> None:
        # 启动时不做连通性探测（避免打扰群），仅记录
        self._ctx.logger.info("钉钉渠道已就绪")

    def on_stop(self) -> None:
        # 同步方法中无法 await，交由 aclose
        pass

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── 事件处理 ────────────────────────────────────────

    def _on_post_created(self, event) -> None:
        content = event.payload.get("content", "")
        if not content:
            return
        self._ctx.scheduler.at(
            self._ctx.now(),
            lambda: self._send_safe(OutboundMessage(kind="markdown", content=content, title="动态")),
            name=f"{self.id}.post.{event.event_id}",
        )

    def _on_message_created(self, event) -> None:
        # 只推送 Agent 主动发出的消息，不推送用户的
        if event.payload.get("direction") != "outbound":
            return
        content = event.payload.get("content", "")
        if not content:
            return
        self._ctx.scheduler.at(
            self._ctx.now(),
            lambda: self._send_safe(OutboundMessage(
                kind="text", content=content,
                mention=tuple(self._at_mobiles), mention_all=self._at_all,
            )),
            name=f"{self.id}.msg.{event.event_id}",
        )

    async def _send_safe(self, msg: OutboundMessage) -> None:
        """调度器回调：内部消化异常，避免污染调度器"""
        result = await self.send(msg)
        if not result.ok:
            self._ctx.logger.warning("钉钉推送失败", extra={"error": result.error})

    # ── Channel 实现 ────────────────────────────────────

    async def send(self, msg: OutboundMessage) -> SendResult:
        if self._circuit_open:
            return SendResult(ok=False, error="渠道已熔断，等待重置", retryable=False)

        body = self._build_body(msg)

        last_error: str | None = None
        for attempt in range(self._max_retry + 1):
            try:
                resp = await self._client.post(self._signed_url(), json=body)
                data = resp.json()

                if data.get("errcode") == 0:
                    self._consecutive_failures = 0
                    return SendResult(ok=True, message_id=f"dt_{int(time.time()*1000)}")

                errcode = data.get("errcode")
                errmsg = data.get("errmsg", "未知错误")
                last_error = f"errcode={errcode} errmsg={errmsg}"

                # 310000 = 加签校验失败，不可重试
                if errcode == 310000:
                    return SendResult(ok=False, error=last_error, retryable=False)

                # 限流可重试
                if errcode in (130101, 410100):
                    self._ctx.logger.warning(f"钉钉限流，第 {attempt+1} 次重试")
                    await self._sleep_backoff(attempt)
                    continue

                return SendResult(ok=False, error=last_error, retryable=False)

            except httpx.TimeoutException:
                last_error = "请求超时"
                await self._sleep_backoff(attempt)
            except httpx.HTTPError as exc:
                last_error = f"网络错误: {exc}"
                await self._sleep_backoff(attempt)

        self._record_failure(last_error)
        return SendResult(ok=False, error=last_error, retryable=True)

    def on_receive(self, handler) -> None:
        # 单向渠道，不支持接收
        raise NotImplementedError("钉钉自定义机器人不支持接收消息")

    def health_check(self) -> HealthStatus:
        if self._circuit_open:
            return HealthStatus(ok=False, detail="已熔断", hint="运行 alterego plugins reset channel.dingtalk_webhook")
        if self._consecutive_failures > 0:
            return HealthStatus(
                ok=False,
                detail=f"连续失败 {self._consecutive_failures} 次",
                hint="检查网络与 Webhook 是否有效",
            )
        return HealthStatus(ok=True, detail="正常")

    # ── 内部工具 ────────────────────────────────────────

    def _signed_url(self) -> str:
        """钉钉加签：timestamp 与 sign 作为 query 参数附加"""
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self._secret}"
        digest = hmac.new(
            self._secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = quote_plus(base64.b64encode(digest).decode("utf-8"))
        sep = "&" if "?" in self._webhook_url else "?"
        return f"{self._webhook_url}{sep}timestamp={timestamp}&sign={sign}"

    def _build_body(self, msg: OutboundMessage) -> dict[str, Any]:
        at = {"atMobiles": list(msg.mention or self._at_mobiles),
              "isAtAll": msg.mention_all or self._at_all}

        if msg.kind == "markdown":
            title = msg.title or "AlterEgo"
            return {"msgtype": "markdown",
                    "markdown": {"title": title, "text": msg.content},
                    "at": at}

        # 默认 text
        return {"msgtype": "text", "text": {"content": msg.content}, "at": at}

    async def _sleep_backoff(self, attempt: int) -> None:
        import asyncio
        await asyncio.sleep(min(2 ** attempt * 0.5, 8.0))

    def _record_failure(self, error: str | None) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= 5:
            self._circuit_open = True
            self._ctx.publish("plugin.circuit_opened",
                              {"plugin_id": self.id, "error": error})
            self._ctx.logger.error(
                "钉钉渠道已熔断（连续失败 5 次）",
                extra={"error": error, "hint": "alterego plugins reset channel.dingtalk_webhook"},
            )
```

### 13.4 本示例演示的要点

| 要点 | 位置 |
| --- | --- |
| 配置读取与校验 | `on_load` 开头 |
| 注册到 registry | `ctx.registry.register(Channel, ...)` |
| 订阅事件 | `ctx.bus.subscribe("post.created", ...)` |
| 用调度器异步执行 IO | `ctx.scheduler.at(...)` |
| 指数退避重试 | `send()` 中的 `for attempt` 循环 |
| 可重试 vs 不可重试错误 | `retryable` 标志 |
| 熔断机制 | `_record_failure` / `_circuit_open` |
| 健康检查 | `health_check` |
| 加签算法 | `_signed_url` |
| 资源清理 | `aclose` |
| 结构化日志 | 所有 `ctx.logger.*` 带 `extra` |
| 单向渠道明确拒绝入站 | `on_receive` 抛 `NotImplementedError` |

---

## 14. 完整示例：自定义推演阶段

在推演流水线中插入一个「天气影响情绪」的阶段。

```toml
# plugins/weather_mood/plugin.toml
[plugin]
id          = "stage.weather_mood"
version     = "0.1.0"
api_version = "1"
kind        = "stage"
entry       = "plugin:WeatherMoodStage"
name        = "天气影响心情"
description = "根据天气调整情绪效价，阴雨天更容易低落"
provides    = ["stage"]
requires    = ["storage.sqlite"]

[config]
api_url     = { type = "string", default = "", description = "天气 API 地址，留空则用内置随机模拟" }
city        = { type = "string", default = "杭州" }
rain_effect = { type = "number", default = -0.12, min = -1.0, max = 0.0,
                description = "下雨对效价的影响幅度" }
```

```python
# plugins/weather_mood/plugin.py
"""天气影响情绪阶段。

插入位置：order = 25，位于 Sense(10) 之后、Reflect(30) 之前，
这样 Reflect 阶段能读取到被天气修正后的情绪。
"""
from __future__ import annotations

import random
from datetime import datetime

from alterego.interfaces import HealthStatus, Stage, StageResult, TickContext
from alterego.kernel.plugin import Plugin, PluginContext

WEATHERS = [
    ("晴", +0.08), ("多云", 0.0), ("阴", -0.03),
    ("小雨", "rain"), ("中雨", "rain"), ("大雨", "rain"),
]


class WeatherMoodStage(Plugin, Stage):
    name = "weather_mood"
    order = 25                                  # Sense=10 → 本阶段=25 → Reflect=30
    depends_on = ("sense",)
    enabled = True

    def on_load(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._rain_effect: float = ctx.config["rain_effect"]
        self._city: str = ctx.config["city"]

        # 持久化当前天气，避免同一 tick 内多次查询
        self._today: str | None = None
        self._weather: str = "多云"

        ctx.registry.register(Stage, self, name=self.name)
        ctx.logger.info("天气情绪阶段已加载", extra={"city": self._city})

    async def run(self, ctx: TickContext) -> StageResult:
        now = ctx.virtual_now
        day_key = now.strftime("%Y-%m-%d")

        # 每天只决定一次天气；同一天内保持稳定
        if self._today != day_key:
            rng = random.Random(f"{ctx.rng.random()}:{day_key}")
            self._weather, self._effect = rng.choice(
                [(w, e if isinstance(e, float) else None) for w, e in WEATHERS]
            )
            self._today = day_key
            self._ctx.state.set("today_weather", {"date": day_key, "weather": self._weather})
            ctx.notes.append(f"今日天气：{self._weather}")

        effect = self._rain_effect if self._effect is None else self._effect

        if effect != 0.0:
            emo = ctx.state.emotion
            new_valence = max(-1.0, min(1.0, emo.valence + effect))
            ctx.state = ctx.state.evolve(
                emotion=emo.evolve(valence=new_valence, updated_at=now)
            )
            ctx.notes.append(
                f"天气「{self._weather}」使效价 {emo.valence:+.3f} → {new_valence:+.3f}"
            )

        return StageResult(
            ok=True,
            changes={"weather": self._weather, "valence_delta": effect},
        )

    def health(self) -> HealthStatus:
        return HealthStatus(ok=True, detail=f"当前天气 {self._weather}")


# 说明：
# - 通过 ctx.registry.register(Stage, ...) 自动加入流水线，无需修改引擎
# - order=25 决定它插在 Sense 与 Reflect 之间
# - depends_on 保证 Sense 已执行
# - 修改情绪写入 ctx.state，后续阶段可见
# - ctx.notes 的内容会写入 tick_log，alterego why 可展示
```

**引擎侧的发现逻辑**（已有，无需修改）：

```python
# sim/engine.py 中已有代码
stages = [s for _, s in sorted(
    ctx.registry.get_all(Stage), key=lambda kv: kv[1].order
) if s.enabled]
```

---

## 15. 完整示例：新增意图类型

给 Agent 增加「拍照发动态」的意图。

```toml
# plugins/photo_intent/plugin.toml
[plugin]
id          = "capability.photo_intent"
version     = "0.1.0"
api_version = "1"
kind        = "capability"
entry       = "plugin:PhotoIntentCapability"
name        = "拍照发动态"
description = "让 Agent 能够拍摄照片并发布带图动态"
provides    = ["capability", "intent_catalog"]
requires    = ["channel.web"]

[config]
image_generator = { type = "string", default = "none",
                    choices = ["none", "placeholder", "stable_diffusion"],
                    description = "图像生成方式，none 则只发文字" }
```

```python
# plugins/photo_intent/plugin.py
from __future__ import annotations

from dataclasses import dataclass

from alterego.interfaces import (
    Capability, CapabilityResult, HealthStatus, Intent, IntentType,
    TickContext, Tool,
)
from alterego.kernel.plugin import Plugin, PluginContext


@dataclass(frozen=True)
class TakePhotoIntent:
    """意图类型定义：拍照"""


class PhotoIntentCapability(Plugin, Capability):
    id = "capability.photo_intent"
    intent_types = frozenset({"take_photo"})

    def on_load(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._mode: str = ctx.config["image_generator"]

        # 1) 注册意图类型 → LLM 会在决策时看到这个选项
        ctx.registry.register(IntentType, IntentType(
            name="take_photo",
            description="拍一张照片并配文发布，适合遇到好看的风景、好玩的场景、值得记录的瞬间",
            category="outbound",
            default_weight=0.08,
            requires_capability=self.id,
            parameters_schema={
                "type": "object",
                "properties": {
                    "subject":   {"type": "string", "description": "拍摄对象"},
                    "caption":   {"type": "string", "description": "配文，第一人称"},
                    "location":  {"type": "string", "description": "拍摄地点"},
                    "mood":      {"type": "string", "description": "拍摄时的心情"},
                },
                "required": ["subject", "caption"],
            },
            outbound=True,
            budget_kind="post",                 # 计入每日动态配额
        ), name="intent.take_photo")

        # 2) 注册执行能力
        ctx.registry.register(Capability, self, name=self.id)

        # 3) 给 LLM 一个可调用工具（可选，让 Agent 能主动"查询相机状态"）
        ctx.registry.register(Tool, CameraStatusTool(), name="tool.camera_status")

        ctx.logger.info("拍照意图已注册", extra={"mode": self._mode})

    async def execute(self, intent: Intent, ctx: TickContext) -> CapabilityResult:
        subject = intent.params.get("subject", "")
        caption = intent.params.get("caption", "")
        location = intent.params.get("location")

        image_path = None
        if self._mode != "none":
            image_path = await self._generate_image(subject, ctx)

        # 交给 post 能力统一发布（复用现有渠道分发逻辑）
        post_cap = ctx_registry_get_post_capability(self._ctx, ctx)
        result = await post_cap.execute(
            Intent(name="post_moment",
                   params={"content": caption, "image_path": image_path,
                           "location": location},
                   motivation=intent.motivation),
            ctx,
        )

        return CapabilityResult(
            ok=result.ok,
            summary=f"拍下了「{subject}」并发布" + (f"，地点：{location}" if location else ""),
            artifacts={"image_path": str(image_path) if image_path else None},
        )

    async def _generate_image(self, subject: str, ctx: TickContext):
        from pathlib import Path
        out_dir = self._ctx.paths.data_dir / "images"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{ctx.tick_id}.png"

        if self._mode == "placeholder":
            _write_placeholder_png(path, subject)
            return path

        # stable_diffusion: 通过其他插件提供的 ImageGenerator 接口
        gen = self._ctx.get_optional_service(ImageGenerator)
        if gen is None:
            self._ctx.logger.warning("ImageGenerator 不可用，降级为占位图")
            _write_placeholder_png(path, subject)
            return path

        await gen.generate(prompt=f"照片：{subject}", out_path=path)
        return path

    def health(self) -> HealthStatus:
        return HealthStatus(ok=True, detail=f"图像模式 {self._mode}")


class CameraStatusTool:
    name = "check_camera"
    description = "查询「手机」相机是否可用，以及电量情况"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def invoke(self) -> dict:
        return {"available": True, "battery": 0.72}
```

**新增一个意图需要做的三件事**（全部在插件内完成，不碰内核）：

1. 注册 `IntentType` → LLM 决策时能看到这个选项
2. 注册 `Capability` → 意图被选中后有人执行
3. （可选）注册 `Tool` → 让 LLM 能查询相关状态

另外需要在 `prompts/intention.md` 中**不需要**做任何修改——意图列表由 `intent_catalog` 动态渲染进提示词。

---

## 16. 开发 Checklist

### 16.1 新建插件

- [ ] 在 `plugins/<name>/` 下创建目录
- [ ] 编写 `plugin.toml`：`id` 符合命名规范、`api_version = "1"`、`kind` 正确、`entry` 可解析
- [ ] `config` 中每个字段都有 `type` 与 `description`；密钥字段标 `secret = true` 并配 `env`
- [ ] `requires` 只列出真正必需的依赖；能可选的放 `optional`
- [ ] 实现 `Plugin` 子类，在 `on_load` 中注册能力
- [ ] 所有时间判断使用 `ctx.now()` / `ctx.clock`，**不要用 `datetime.now()`**
- [ ] 所有随机性使用 `ctx.rng` 或 tick 的 `ctx.rng`，**不要用全局 `random`**
- [ ] 所有日志使用 `ctx.logger`，**不要用 `print`**
- [ ] 所有配置从 `ctx.config` 读取，**不要自己读环境变量或文件**
- [ ] `on_stop` 幂等，释放所有资源（HTTP 客户端、文件句柄、后台任务）
- [ ] 实现 `health_check()`，给出可操作的 `hint`
- [ ] 网络请求有超时；区分可重试与不可重试错误
- [ ] 大量 IO 用 `ctx.scheduler` 异步执行，不要阻塞 tick

### 16.2 测试

- [ ] 单元测试：`on_load` 能正确注册能力（用 `FakeRegistry`）
- [ ] 单元测试：核心逻辑用 `FrozenClock` + 固定 seed 验证确定性
- [ ] 集成测试：能被 `PluginManager` 正常加载、启动、停止、重载
- [ ] 异常测试：依赖缺失时的降级行为
- [ ] 异常测试：`on_load` 抛异常时不影响其他插件

测试辅助工具（内核提供）：

```python
from alterego.testing import (
    FakeRegistry, FakeBus, FrozenClock, FakeLLM, FakeChannel,
    make_context, load_plugin_for_test,
)

def test_dingtalk_signing():
    plugin, ctx = load_plugin_for_test(
        "plugins/dingtalk",
        config={"webhook_url": "https://example.com/hook", "secret": "SECtest"},
    )
    url = plugin._signed_url()
    assert "timestamp=" in url and "sign=" in url
```

### 16.3 文档

- [ ] 插件目录下有 `README.md`，说明用途、配置项、使用示例
- [ ] 若插件引入了新的用户可见行为 → 更新 `CHANGELOG.md`
- [ ] 若插件定义了新的公开接口 → 更新 `docs/design/02-plugin-api.md`
- [ ] 若插件带来重要架构变化 → 新增 `docs/adr/NNNN-*.md`

### 16.4 常见错误

| 错误 | 后果 | 正确做法 |
| --- | --- | --- |
| 用 `datetime.now()` | 倍速仿真下时间错乱 | `ctx.now()` |
| 用全局 `random` | 结果不可复现，测试不稳定 | `ctx.rng` |
| 在 `on_load` 中发网络请求 | 启动变慢，离线环境下启动失败 | 放 `on_start` 或用 `ctx.scheduler` |
| 在 `on_event` 中做耗时同步 IO | 阻塞事件总线的其他订阅者 | 用 `ctx.scheduler` 异步执行 |
| 直接 `import` 另一个插件 | 破坏插件隔离，卸载时崩溃 | 通过 `ctx.registry` 获取接口 |
| 在 `on_load` 中忘记注册能力 | 静默失效，难以排查 | 参考本 Checklist |
| `on_stop` 中抛异常 | 影响逆拓扑序的后续停止 | 内部 try/except，只记录日志 |
| 存密钥到 `ctx.state` | 明文落库 | 只用 `ctx.config` |

---

## 变更记录

| 日期 | 版本 | 变更 | 作者 |
| --- | --- | --- | --- |
| 2026-09-15 | v0.1.0 | 初版，api_version = 1 | LMG-arch |

# ADR-0007：`PluginContext.bus` / `.registry` 使用带归属的视图

- **状态**：已接受
- **日期**：2026-09-15
- **决策者**：LMG-arch
- **关联**：[ADR-0006](0006-ship-implementation-choices-as-data.md)、`docs/design/02-plugin-api.md` § 5 / § 10.3 / § 11.1

---

## 背景

`docs/design/02-plugin-api.md` 向插件作者承诺了两件事：

1. **§ 11.1 隔离级别表**：「`on_load` 抛异常 → 卸载已注册的实现（回滚），标记 Failed，继续」
2. **§ 10.3 关键约束**：「热重载时旧实例的状态与订阅不会遗留」

这两条承诺的**实现基础**都是同一件事：内核必须知道「哪个服务是哪个插件注册的」、
「哪个订阅是哪个插件订的」，才能在插件失败或重载时精确清理它留下的痕迹。

内核为此提供了参数：

```python
registry.register(Channel, self, name="dingtalk", owner="channel.dingtalk")
bus.subscribe("message.received", self.on_event, owner="channel.dingtalk")
```

**问题在于设计文档自己给的示例代码没传 `owner=`。** § 7.1、§ 13.3 里插件作者的
标准写法是：

```python
ctx.registry.register(Channel, self, name="dingtalk")     # 没有 owner
ctx.bus.subscribe("message.received", self.on_event)       # 没有 owner
```

于是上面两条承诺都退化成了「祈祷」：作者照文档写，内核就清理不掉任何东西。
失败的具体形态是——

- 插件 `on_load` 中途抛异常 → 已注册的实现留在注册表里，指向一个**半初始化的对象**。
  它不会被调用（因为插件状态是 Failed），但 `registry.get(Channel)` 会返回它，
  于是**其他插件拿到一个坏掉的对象**，报错信息还完全指不到真正的源头。
- 插件热重载 → 旧实例的订阅仍在总线上。旧实例持有旧代码、旧连接、旧锁；
  新实例也订阅了同一个主题。**同一条消息被处理两次**，而且其中一次来自已被判死的代码。

这类问题不会在启动时暴露——它们只在「改一次插件再等一会儿」之后才显形，
而那时的现象（「消息偶尔重复」「渠道时好时坏」）与原因之间隔着好几层。

---

## 决策

**`PluginContext.registry` 与 `PluginContext.bus` 的运行期类型改为
`OwnedRegistry` / `OwnedBus`——自动把 `owner` 预填成当前插件 id 的薄代理。**

```python
class OwnedRegistry:
    """``ctx.registry`` 的运行期真实类型：把 ``owner`` 预填成插件 id 的视图。"""

    def register(self, interface, instance, *, name, priority=0, owner=None) -> None:
        self._registry.register(
            interface, instance, name=name, priority=priority, owner=owner or self._owner
        )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)          # 别让 copy/pickle 的探针一路问到里面
        return getattr(self._registry, name)     # 其余方法原样转发
```

`OwnedBus` 同理，只代理 `subscribe`。

**插件作者什么都不用改。** 文档里的写法（不传 `owner=`）就是正确写法，
而且是**唯一**的写法——忘不掉，因为压根不用记。

---

## 备选方案

| 方案 | 评价 |
| --- | --- |
| **A. 保持现状，把文档示例补上 `owner=`** | ❌ 靠提示词祈祷。文档有 5 处示例，将来还会加；漏一处就是一个只在热重载后才显形的 bug。违反 P3。 |
| **B. 内核在 `on_load` 前后比对注册表快照，事后差集清理** | ❌ 快照比对无法区分「插件 A 注册的」与「插件 A 覆盖了插件 B 的」。而且 `on_load` 是同步的，快照成本要每次加载都付。 |
| **C. 把 `owner` 做成必填参数** | ❌ 每个插件都要手写自己的 id，等于把「写对字符串」变成契约的一部分。写错了同样静默。 |
| **D. 用线程局部变量记录「当前正在加载哪个插件」** | ❌ 全局隐式状态，与 P2（显式优于隐式）冲突；异步回调里会串台。 |
| **E. 带归属的视图（本决策）** | ✅ 归属信息在所有权的**边界**上（即 `PluginContext` 的构造处）一次性确定，越往里越无法出错。 |

---

## 代价

1. **多了一层间接。** `ctx.registry` 不再是 `ServiceRegistry` 实例，`isinstance` 检查
   会失败；调试时栈里多一帧。已用 `__getattr__` 全量转发把影响压到最小。
2. **类型标注与运行期类型不一致。** `PluginContext.registry` 声明为 `ServiceRegistry`
   （这是插件作者该知道的类型），实际是 `OwnedRegistry`。好处是插件侧代码不受影响，
   代价是 `mypy` 看不出这层差异——`__post_init__` 里用 `isinstance` 做了运行期保证。
3. **同一个 `PluginContext` 会被包两次的风险。** 热重载路径上可能重复构造，
   因此在 `__post_init__` 里检查 `isinstance(..., OwnedRegistry)`，已包过就跳过。

---

## 后续约束

1. **`PluginContext.registry` / `.bus` 的赋值必须经过 `__post_init__`。** 不要用
   `object.__new__` 之类的旁路构造它，否则归属会丢。
2. **插件代码不得直接 import `ServiceRegistry` / `EventBus` 来做全局注册。**
   所有注册都走 `ctx`。这一条由 `docs/design/02-plugin-api.md` § 5 的
   「`PluginContext` 是插件访问系统的唯一入口」保证。
3. **新增任何「插件在系统里留下痕迹」的能力（订阅、注册、定时任务、临时文件）时，
   都要问同一个问题：插件失败/重载时，内核怎么找到并清掉它？**
   如果答案是「靠作者记得」，那就还没做完——按本 ADR 的思路做成机制。
4. 已注册的实现与订阅的清理靠 `unregister_owner(plugin_id)` / `unsubscribe_owner(plugin_id)`，
   两者的返回值为被清理的条目数，`PluginManager` 在 `on_load` 失败与热重载时各调一次。

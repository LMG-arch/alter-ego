# ADR-0002 · 使用 Python 3.11+ 作为实现语言

- **状态**：已接受
- **日期**：2026-09-15
- **决策者**：LMG-arch
- **相关**：[ADR-0001](0001-record-architecture-decisions.md)、[`docs/DESIGN.md`](../DESIGN.md) § 3 设计原则
- **影响范围**：全体

---

## 背景

项目的需求中有一条是**硬性约束**，其他需求都必须在它之下权衡：

> 「进行插件式开发，后续好添加功能」

与之相关的是用户的另一句话：

> 「选择加载快，内存占用少，代码简单的语言」

这句话的字面指向是 Go 或 Rust。**但这两个语言都无法满足"插件式开发"这条硬性约束**，而且用户环境是 Windows。这不是"口感偏好"问题，是可行性问题。

具体约束条件：

| 约束 | 说明 |
| --- | --- |
| **C1 插件式扩展** | 必须能"丢一个文件夹进去就生效"，不重新编译、不重启 |
| **C2 热重载** | 插件更新后无需重启进程（长期运行的 Agent，重启会丢失推演连续性）|
| **C3 Windows 支持** | 开发与运行环境是 Windows（本次会话环境）|
| **C4 单人本地运行** | 不是服务器场景，单进程、低并发 |
| **C5 启动快、内存少** | 用户明确提出的期望 |
| **C6 代码简单** | 用户明确提出的期望 |

C1+C2+C3 三者同时满足的方案集合非常小。

---

## 决策

使用 **Python 3.11+** 实现整个项目。

**用"依赖最小化"而不是"换语言"来满足 C5、C6。** 具体手段：

1. **必需依赖只有两个**：`pydantic`（结构化校验）、`httpx`（异步 HTTP）。其余全部标准库。
2. **TOML 解析用内置 `tomllib`**（3.11 加入）——这是一个刻意的版本下限理由，避免引入 `tomli`。
3. **CLI 用内置 `argparse`**，不用 `click` / `typer`。
4. **并发用内置 `asyncio`**，不用多进程/多线程框架。
5. **热重载用 1 秒轮询文件的 mtime**，不引入 `watchdog`。
6. **Web 用 FastAPI + uvicorn 作为可选依赖**（`pip install "alterego[web]"`），无前端构建步骤。
7. **前端用原生 JS + SSE**，不用 React/Vue，不引入 npm。

目标指标：**启动 ≤ 1 秒，常驻内存 ≤ 150 MB**。

---

## 考虑过的方案

| 方案 | C1 插件 | C2 热重载 | C3 Win | C5 启动 | 是否采用 |
| --- | --- | --- | --- | --- | --- |
| **Python 3.11+** | ✅ `importlib` 动态导入 | ✅ 天然支持 | ✅ | ~0.6s / ~80MB | ✅ |
| Go | ❌ `plugin` 包 | ❌ | ❌ **官方不支持 Windows** | ~0.02s / ~15MB | ❌ |
| Rust | ❌ 需 `dlopen` + ABI 对齐 | ❌ 无类型安全的热重载 | ⚠️ 可行但复杂 | ~0.01s / ~8MB | ❌ |
| Node.js / TypeScript | ✅ 好 | ✅ 好 | ✅ | ~0.15s / ~60MB | ❌ |
| Java / Kotlin | ✅ OSGi 那套 | ⚠️ 复杂 | ✅ | ~1.5s / ~300MB | ❌ |
| C# / .NET | ✅ AssemblyLoadContext | ⚠️ 复杂 | ✅ | ~0.3s / ~100MB | ❌ |

### 为什么否掉 Go（最接近的竞争者）

Go 在 C5、C6 上是明确赢家——编译后单文件、启动 20 毫秒、常驻内存 15MB、语言简单。如果需求里没有 C1，Go 是显然的选择。

**但 Go 的 `plugin` 包在 Windows 上不可用。** 官方文档明确说明（`plugin` 包仅在 Linux、FreeBSD、macOS 上以 `-buildmode=plugin` 支持），Windows 上编译会直接报错。

替代方案是 Go 1.8+ 的 `plugin` 之外的做法：
- **外挂子进程 + RPC**（如 HashiCorp 的 go-plugin）：进程间通信，插件崩溃不影响主进程。但**热重载需要重启子进程并重建连接**，状态迁移复杂；且每个插件一个进程，内存开销反而上升。
- **嵌入脚本引擎**（Lua / Starlark）：能满足 C1、C2，但插件作者要写另一种语言，C6"代码简单"的目标被打折，而且整个插件 API 要翻译成脚本 API。

两条路都偏离了"丢一个文件夹进去就生效"这个简单心智模型。

### 为什么否掉 Rust

Rust 在 C5 上最强，但：

- 动态加载需要 `libloading` + 手工维护 ABI 兼容，插件必须用**完全相同的 Rust 版本和编译器标志**编译——这对插件开发者的要求极不友好，与 C6 冲突。
- 热重载没有安全方案：Rust 的 RAII 与所有权模型使得"卸载旧代码、加载新代码"无法在保持安全的前提下做到。社区方案（如 `hot-lib-reloader`）都限制在"开发时"而非生产可用。
- 编译时间长，违反了"简单"的直觉。

### 为什么否掉 Node.js

Node 的插件机制（`require` + `require.cache` 删除）在 C1、C2 上几乎和 Python 一样好，且启动与内存略优于 Python。

否掉的原因：

1. **类型系统的天花板。** 项目有大量结构化数据（20 张表的记录、复杂的 `TickContext`、插件接口）。TypeScript 的类型在**运行时不存在**，仍需额外的运行时校验（zod / io-ts），相当于把 Python 的 pydantic 换成另一个依赖，没有净收益。
2. **Python 的领域契合度更高。** 记忆检索、情绪衰减、文本分词、数据分析是 Python 的强项，而这些都是本项目的核心工作。Node 生态在这块偏弱。
3. **用户已有 Python 经验。** 从用户 memory 中的历史项目（`work_diary.database` 用的是 `sqlite3`、`report_generator` 用的是 `python-docx`）可以确认。
4. **启动与内存的差距不足以弥补上述劣势。** 0.15s/60MB vs 0.6s/80MB——对于"本地单人长期运行"的场景，这个差异可以忽略。

### 为什么否掉 Java / C#

两者的插件机制（OSGi、AssemblyLoadContext）都能满足 C1、C2，但：

- **C5 明确失败。** JVM 冷启动 1.5 秒起步，常驻内存 300MB 起。这直接违反用户提出的要求。
- **C6 失败。** 为了做一个"模拟人生活"的小工具引入 JVM 或 .NET 运行时，与"代码简单"背道而驰。

### 为什么 Python 可以接受

Python 在 C5 上是所有候选中**较差的**（启动最慢、内存最大），但差距在可接受范围内：

| 指标 | Python 3.11 | Go | 差距 | 对本地单人的影响 |
| --- | --- | --- | --- | --- |
| 冷启动 | ~0.6s | ~0.02s | 30x | 用户敲 `alterego status` 时要等半秒，可感知但不烦人 |
| 常驻内存 | ~80MB | ~15MB | 5x | 占现代机器内存的 0.3%，可忽略 |
| 首次全量导入 | ~1.2s | — | — | 只在 `serve` 启动时发生一次 |

**关键判断**：C5 的目标是"加载快、内存少"，而**实际的比较基准不是 Go，是"一个 Electron 应用"或"一个 IDE"**。80MB 在这个语境下完全合格。相反，如果为了省 65MB 而放弃"插件式开发"这个核心需求，是本末倒置。

---

## 后果

### 正面

- **C1 完美满足。** `importlib.util.spec_from_file_location` 加载 `plugins/<name>/__init__.py`，`sys.modules` 删除即可卸载。一个文件夹 = 一个插件。
- **C2 有成熟做法。** 热重载路径：`on_stop` → 清 `sys.modules` → 重新导入 → `on_load`。Python 的模块系统天然支持这一流程，无需 ABI 技巧。
- **C3 无阻碍。** Python 在 Windows 上是一等公民。
- **C4 契合。** `asyncio` 单进程模型适合"周期性 tick + 少量 IO"的负载。
- **C6 有真实保障。** 通过依赖最小化（必需依赖仅 2 个）+ 不使用元编程魔法 + 严格类型注解（mypy `strict`），代码保持了可读性。
- **生态契合。** `sqlite3` 内置、`jieba` 处理中文分词、`httpx` 处理异步 HTTP——核心需求都有成熟方案。
- **用户已有经验。** 降低上手成本。

### 负面

- **C5 打了折扣。** 启动 0.6s 而不是 0.02s，内存 80MB 而不是 15MB。这是为 C1 付出的代价，**是明确接受的权衡**。
- **性能上限低。** 如果未来要做大规模并发（多用户 SaaS、多 Agent 并行推演），Python 会成为瓶颈。但这是明确非目标（见 `DESIGN.md` § 15）。
- **GIL 限制。** 真正的 CPU 并行不可用。当前设计不依赖它（`asyncio` + `to_thread` 足够），但需要留意"不要在 `domain/` 里写重 CPU 循环"。
- **`plugin` 状态管理靠自觉。** Python 没有 Rust 那样的编译期保证，插件写了坏代码能崩掉整个进程——需要用错误隔离 + 熔断器（见 `02-plugin-api.md` § 11）在运行时兜住。
- **打包分发稍麻烦。** 用户需要 Python 环境。缓解：文档里给清晰的 `venv` 步骤，未来可考虑 `pipx` / `uv` 一键安装。

### 需要关注

**什么时候应该重新审视这个决策：**

1. 如果启动时间**超过 2 秒**（例如依赖膨胀导致），需要立刻回头查依赖表。
2. 如果常驻内存**超过 300 MB**（例如 `tick_log` 的 `state_snapshot_json` 被大量缓存在内存里），需要查内存泄漏。
3. 如果决定支持多 Agent 并行推演且性能成为瓶颈。
4. 如果 Python 的插件生态出现更好的方案（例如 `pyo3` 相关的成熟替代品）。

**重新审视的流程**：写一个新 ADR 说明触发条件与迁移方案，然后按 `CONTRIBUTING.md` 的流程执行。不要直接改。

---

## 对设计原则的影响

| 原则 | 影响 |
| --- | --- |
| P1 内核无知 | **支持**。Python 的鸭子类型 + `Protocol` 使"内核只定义协议"非常自然，无需泛型体操 |
| P2 显式优于隐式 | **支持**。通过禁用元编程、启用 mypy `strict`、显式依赖注入来对抗 Python 的动态性 |
| P3 机制约束优于提示词祈祷 | 无影响 |
| P4 可插拔优于可配置 | **支持**。动态导入是"零内核修改扩展"的技术基础 |
| P5 标准库优先 | **强支持**。这是选 Python 的核心理由之一——`sqlite3` / `tomllib` / `argparse` / `asyncio` / `logging` 全部内置 |
| P6 可复现 | 需要额外努力。Python 的字典顺序、`random` 全局状态、版本差异都可能破坏可复现性。故在 `sim/` 中**禁止全局 `random`**（用 `ctx.rng`）与**禁止真实时钟**（用 `ctx.clock`），并由 `scripts/check_architecture.sh` 强制 |
| P7 文档与代码同生共死 | 无影响 |

---

## 需要同步的文档

- [x] `docs/DESIGN.md` —— § 中已说明"Python 3.11+"与依赖最小化策略
- [x] `docs/design/01-architecture.md` —— 内核的 `Clock` / `EventBus` 等设计基于 `asyncio`
- [x] `docs/design/02-plugin-api.md` —— 加载机制基于 `importlib`，热重载基于 `sys.modules`
- [x] `docs/design/06-roadmap.md` —— 成本与工作量估算基于 Python 生态
- [x] `CONTRIBUTING.md` —— 「新增依赖」章节记录了"必需依赖只有两个"的约束
- [x] `pyproject.toml` —— `requires-python = ">=3.11"`，依赖列表已最小化
- [x] `.gitignore` —— 包含 Python 相关规则
- [x] `CHANGELOG.md` —— 已在 `[0.1.0]` 的「架构」小节记录

---

## 验证方式

| 指标 | 目标 | 验证命令 / 方式 |
| --- | --- | --- |
| Python 版本下限 | 3.11+ | `pyproject.toml` 中 `requires-python = ">=3.11"`；CI 矩阵含 3.11 |
| 必需依赖数量 | **2** | `python -c "import tomllib;print(len(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))"` 应输出 2 |
| 冷启动时间 | ≤ 1.0s | `python -X importtime -c "import alterego" 2>&1 \| tail -1` |
| CLI 响应时间 | ≤ 1.5s | `Measure-Command { alterego status }`（Windows）|
| 常驻内存 | ≤ 150 MB | `alterego serve` 跑 1 小时后测 RSS |
| 无元编程滥用 | 无 `__getattr__` / `exec` / `eval` 于 `src/` | `grep -rEn "exec\(|eval\(|__getattr__" src/alterego/` |
| 类型完整性 | mypy strict 通过 | `mypy src/alterego` |

**回归防护**：在 CI 中加入"依赖数量检查"与"启动时间检查"，超过阈值直接失败。这样如果未来有人随手加了依赖，会立刻被发现。

```yaml
# .github/workflows/ci.yml 中可加入
- name: 依赖数量守门
  run: |
    n=$(python -c "import tomllib;print(len(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))")
    if [ "$n" -gt 5 ]; then
      echo "::error::必需依赖增加到 $n 个（上限 5）。新增必需依赖需要写 ADR。"
      exit 1
    fi
```

---

## 备注

**这个 ADR 最重要的部分是"背景"一节的约束表。** 如果只读结论"用 Python"，会误以为这是一个偏好问题，从而可能被"Python 太慢了，换 Go 吧"推翻。读了约束表才明白：**换语言会破坏 C1/C2/C3，而这三个是硬性需求；被牺牲的 C5 在"本地单人"语境下影响可忽略。**

这也是为什么这个 ADR 值得写得比其他几个长——它是全项目唯一一个"结论与表面直觉相反"的决策。

参考：

- [Go `plugin` 包文档](https://pkg.go.dev/plugin)（说明 Windows 不支持）
- [Python `importlib` 文档](https://docs.python.org/3/library/importlib.html)
- [PEP 680 · `tomllib`](https://peps.python.org/pep-0680/)（3.11 引入标准库 TOML 解析）

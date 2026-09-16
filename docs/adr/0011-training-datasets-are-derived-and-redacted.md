# ADR-0011 · 训练数据集是**派生产物**，脱敏**不可关闭**

- **状态**：已接受
- **实施状态**：已实现（`alterego dataset`、`domain/redact.py`、`domain/dataset.py`、`sim/dataset.py`）
- **日期**：2026-09-16
- **决策者**：LMG-arch
- **相关**：[ADR-0006](0006-ship-implementation-choices-as-data.md)、
  [ADR-0009](0009-fetched-content-is-untrusted.md)、
  [`../design/11-optimization-roadmap.md`](../design/11-optimization-roadmap.md)
- **影响范围**：`domain/redact.py`、`domain/dataset.py`、`sim/dataset.py`、`cli_dataset.py`、
  `interfaces/repository.py`、`storage/sqlite/repositories.py`、`kernel/config.py`、
  `templates/alterego.toml`、`scripts/check_architecture.sh`

---

## 背景

需求是：

> 对话内容自动脱敏整理为对话训练集……思考推理过程保存为思考推理训练集……
> 工具调用过程也进行整理保存为训练集，后续进行本地小模型的微调训练。

这句话里藏着三个必须先回答的问题，而其中两个与**已有的书面决定**正面冲突。

### 冲突一：`11-optimization-roadmap.md` 把「微调」列为非目标

`docs/DESIGN.md` § 非目标 与 `11-optimization-roadmap.md` § 8 都写着：

| 方向 | 判断 |
| --- | --- |
| 自主训练/微调人设模型 | 门槛（显存）与收益不匹配；LoRA 只作为可选插件 |

`docs/DESIGN.md` 的非目标表里还有一行 `自研 LLM 微调 | 用现成 API 即可`。

**但这两句话说的是「项目自己训练模型」，不是「项目产出数据集」。**
把训练集导出成 JSONL 是纯文本处理，零新依赖；而 `transformers` / `peft` / `torch`
一旦进来，`P5 标准库优先` 与「必需依赖只有两个」就同时失效。

**这条分界线必须在代码里落地，而不只是写在文档里。**

### 冲突二：「脱敏」在本仓库里已经是一个**被占用的词**

全仓库 grep `脱敏|redact` 有 77 处命中，**全部指密钥脱敏**：
`kernel/logging.py` 的 `SecretFilter`、`kernel/config.py` 的 `to_dict(redact=True)`、
`PluginManifest.redacted_config()`。它们解决的是「别把 API key 写进日志/贴进 issue」。

训练数据要脱的是**隐私**，是另一件事：邮箱、手机号、证件号、绝对路径、
以及用户自己的显示名。两套规则的服务对象完全不同，不能共用一个开关。

### 约束

1. **不能有第二真源。** 数据集如果能被手工编辑，就会与库漂移。
2. **脱敏必须可回溯重做。** 规则漏了一条，历史数据也得能重新脱一遍。
3. **脱敏不能靠提示词。** P3：机制约束优于提示词祈祷。
4. **不能引入新依赖**（P5）。JSONL、正则、文件写入全在标准库里。
5. **不能有枚举式的格式名污染内核。** `kernel/` 红线禁止出现 `openai` 等厂商名
   （`scripts/check_architecture.sh` 第 1 组）。

## 决策

### 一、数据集是**派生产物**，不新建任何表

`message` / `tick_log` / `activity_log` 是**真源**，训练集是它们的下游，
和生产代码里的 `vault`（Obsidian 知识库）是同一个形状：

```
库里的表  ──build──▶  exports/datasets/<角色名>/*.jsonl
   ▲                              │
   └──── 手工编辑这里 ────────────┘  ✗ 不许
```

**理由是可回溯性，不是洁癖。** 假设三个月后有人发现「手机号没脱干净」：

- 若数据集是实时追加写的 → 旧的泄漏永远留在旧文件里，除非写迁移脚本重扫
- 若数据集是派生的 → 修一条正则，重跑 `alterego dataset build`，全部历史重新脱一遍

这决定了整套实现：**没有 `dataset_sample` 表，没有水位，没有去重**。
`--since` 只是一个过滤参数，`build` 全量重跑是幂等的。

### 二、脱敏在 `domain/` 里，是纯函数，且**没有关闭开关**

`domain/redact.py` 导出 `redact(text, *, user_name="", extra_terms=()) -> str`：

- 纯函数、无 IO、无配置读取（`domain/` 的红线本来就禁止这些）
- **规则表是模块级常量**，顺序固定 → 同一输入永远同一输出（P6）
- 没有 `enabled` 参数，没有配置项能关掉它

**为什么不留开关**：脱敏一旦能被关掉，早晚有人为了「数据好看一点」关掉它，
而数据集是要拿出去（上传到训练服务、分享给别人）的。
**不提供这个旋钮，比提供了一个默认关闭的旋钮更安全。**

用户唯一能定制的是 `[dataset] redact_terms = [...]`——**往上加**，不能往下减。

### 三、虚构角色的名字**不脱**，只脱用户自己的身份

这一点和直觉相反，所以写清楚：

| 内容 | 脱不脱 | 为什么 |
| --- | --- | --- |
| `core.user_name`（用户显示名） | **脱**，替换为「你」 | 用户可能填真名 |
| 角色自己的名字、NPC 名字 | **不脱** | 是虚构的；脱掉会让模型学不会「以这个名字自称」 |
| 邮箱 / 手机 / 证件 / 银行卡 | 脱 | 真实身份 |
| API key / token / 密码 | 脱 | 与 `SecretFilter` 同一动机，但入口不同 |
| 绝对路径（`C:\Users\xxx\...`） | 脱 | **本项目的真实情况：路径里带用户名** |
| 内网 IP / 带凭据的 URL | 脱 | 真实基础设施 |

**「脱得越干净越好」是错的。**一个把所有人名都换成 `[人名]` 的数据集，
训出来的模型不会自称、不会称呼任何人。脱敏的目标是**去掉可识别的真实身份**，
不是去掉专有名词。

### 四、格式名用**形状**命名，不用厂商命名

`[dataset] formats` 的取值是 `Literal["chat", "sharegpt", "alpaca"]`：

| 值 | 产出形状 |
| --- | --- |
| `chat` | `{"messages": [{"role": ..., "content": ...}]}`（最通用） |
| `sharegpt` | `{"conversations": [{"from": "human", "value": ...}]}` |
| `alpaca` | `{"instruction": ..., "input": ..., "output": ...}` |

**不叫 `openai` 有两个原因**：一是 `kernel/` 红线禁止厂商名；二是
`{"messages": [...]}` 早已是跨厂商的通用形状（ChatML 家族），
用厂商名命名反而把用户误导成「这是某家的私有格式」。

### 五、项目**只产出数据集**，训练在项目外

`alterego dataset build` 的终点是磁盘上的文件 + 一份 `README.md`。
本 ADR 明确记录：

- **不引入** `torch` / `transformers` / `peft` / `datasets`
- **不提供** `alterego dataset train`
- 想微调的人拿 `.jsonl` 去用任何训练框架，本项目的责任到文件为止

这是对 `DESIGN.md` 与 `11-optimization-roadmap.md` 非目标的**精确化**，
不是推翻：非目标说的是「不做训练」，本 ADR 说的是「做数据集，那是训练的上游」。

## 后果

### 好的

- 脱敏规则改了可以**重跑修正全部历史**，这是安全属性，不是便利
- 零新依赖，`exports/` 已在 `.gitignore` 里
- 数据集的三个来源里有两个（`tick_log` / `activity_log`）**今天还没有写入者**，
  所以 `build` 会如实报「0 条」并说明上游还没实现——不假装，也不先生成假数据
- `dataset show` 从库里实时渲染，不读已写出的文件 → 改了规则立刻能看见效果

### 坏的 / 要接受的

- **每次改脱敏规则都要重跑 `build`，用户得自己记得。** 缓解办法：
  `manifest.json` 里记 `redact_digest`（规则表的哈希），
  `dataset list` 发现磁盘上的 digest 与当前代码不一致时会提示「规则变了，建议重跑」。
- **正则是确定性的，会漏。** 它挡不住「我住在某某小区」这种自然语言里的身份信息。
  这一点写进 `README.md`，**不假装脱敏是完备的**。
- **工具调用数据集今天必定是 0 条。** `tick_log` / `activity_log` 都没有写入者
  （`sim/` 主体尚未实现）。数据集的定义已经定好，等 `sim/` 落地自然有数据。

## 备选方案

| 方案 | 为什么不选 |
| --- | --- |
| A · 实时追加写 JSONL（插件订阅事件） | 产生第二真源；脱敏规则改了无法回溯修正历史 |
| B · 新建 `dataset_sample` 表存样本 | 同上，且要写迁移；派生物不值得进库 |
| C · 让 LLM 做脱敏（提示词「请去掉个人信息」） | 违反 P3；不可测（无法断言某个模式一定被替换）；每条都要花钱 |
| D · 引入 `datasets` / `transformers` 直接产出 Arrow | 违反 P5；且把「产出数据」和「训练」焊死，训练框架换了就得改代码 |
| E · 给脱敏加 `enabled` 配置项 | 见决策二：提供了关掉它的办法，早晚有人关 |
| F · 格式名用 `openai` / `deepseek` | 违反 `kernel/` 红线；且把通用形状误标成厂商私有 |

## 依据

- `docs/DESIGN.md` § 非目标（`自研 LLM 微调 | 用现成 API 即可`）
- `docs/design/11-optimization-roadmap.md` § 8（`自主训练/微调人设模型 | 门槛与收益不匹配`）
- `docs/design/03-data-model.md` § 7（仓储不含业务逻辑）
- `docs/design/01-architecture.md` § 1.1（依赖矩阵）
- `AGENTS.md` § 2（P1 内核无知 / P3 机制约束 / P5 标准库优先 / P6 可复现）
- `docs/plans/2026-09-16-training-datasets.md`（落地细节）

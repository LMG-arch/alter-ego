# 计划 · 训练数据集管线（`alterego dataset`）

> 决策记录见 [ADR-0011](../adr/0011-training-datasets-are-derived-and-redacted.md)。
> 本文件是落地细节与**结果**。

---

## 1. 原话与读法

> 写一个插件，对话内容自动脱敏整理为对话训练集，后面可以使用这个对话训练集进行微调本地模型，
> 思考推理过程保存为思考推理训练集，后面进行本地模型微调，微调一个适配这个项目的模型，
> 并且工具调用过程也进行整理保存为训练集，后续进行本地小模型的微调训练，
> 这些数据可以在数据页面查看，保存路径也可以查看

拆成五条可验证要求：

| # | 要求 | 本批次怎么满足 |
| --- | --- | --- |
| R1 | 对话 + 思考推理 + 工具调用 **三类** 训练集 | `conversation` / `reasoning` / `tooluse` |
| R2 | **自动脱敏** | `domain/redact.py`，构建时逐字段过一遍 |
| R3 | 能拿去**微调本地模型** | 三种通用 JSONL 形状，`manifest.json` + `README.md` 说明怎么用 |
| R4 | 数据**可以在页面查看** | `alterego dataset list / show`（Web 层是零实现，见 § 7） |
| R5 | **保存路径也可以查看** | `alterego dataset paths` + `list` 每行末列出路径 |

**「自动」的含义**：不是「后台实时写」，而是「不用手写一条规则去指定脱什么」——
规则表是内置的、固定的，用户不需要配置任何东西就能跑。

---

## 2. 三类数据集的定义

### 2.1 `conversation` —— 对话训练集

源：`message` 表（按 `conversation` 分组）。

```
conversation.counterpart_kind = 'user'  的那一条会话
  ↓ 按 created_at 正序、id 稳定次序
inbound  → role = "user"
outbound → role = "assistant"
```

三条规则：

1. **相邻同向消息合并**成一条。真人会连发三条，拆成三个 `user` 轮
   训出来的模型会以为「一次只说一句话」。
2. **至少一对** `(user, assistant)` 才算一个样本。只有 inbound 的会话
   （用户发了它还没回）导不出东西。
3. `content_type != 'text'` 的消息（图片）**跳过内容但保留占位**——
   样本里写 `[图片]`，否则对话会显得它凭空跳过了一轮。

### 2.2 `reasoning` —— 思考推理训练集

源：`tick_log`。

| 段 | 取自 | 进样本的字段 |
| --- | --- | --- |
| 看到什么 | `percepts_json` + `state_snapshot_json` | `input` |
| 想到了什么 | `candidates_json` + `suppressed_json` + `memories_json` + `notes_json` | `reasoning_content` |
| 决定了什么 | `chosen_intent` + `motivation` + `trigger_note` | `output` |

**只取 `status = 'ok'` 的 tick**。`partial` / `failed` / `interrupted` 的轨迹
不是「它怎么想的」，是「它怎么没想成的」——喂给模型会教它犯错。

### 2.3 `tooluse` —— 工具调用训练集

源：`activity_log`（主）+ `tick_log`（补上下文）。

| 段 | 取自 |
| --- | --- |
| 请求 | `tick_log.chosen_intent`（它想干什么） |
| 调用 | `activity_log.intent` + `category` |
| 参数 | `activity_log.detail_json` |
| 结果 | `activity_log.description` |
| 内心 | `activity_log.inner_voice`（含被预算拦下的那些） |

> ⚠️ **今天这个数据集必定是 0 条。** `activity_log` 与 `tick_log` 都还没有写入者
> （`sim/` 主体未实现）。`build` 会如实报 0 并打印一句解释，**不生成假数据**。

---

## 3. 脱敏

`domain/redact.py`：纯函数、有序规则表、无开关。

| # | 规则 | 替换为 |
| --- | --- | --- |
| 1 | `url_credentials` —— `scheme://user:pass@` | `[凭据]` |
| 2 | `api_key` —— `sk-` / `ghp_` / `AKIA` / `Bearer` / `key=…` | `[密钥]` |
| 3 | `email` | `[邮箱]` |
| 4 | `id_card_cn` —— 18 位 | `[证件号]` |
| 5 | `phone_cn` —— `1[3-9]` 开头 11 位 | `[手机号]` |
| 6 | `ipv4` | `[IP]` |
| 7 | `win_path` —— `C:\Users\…` | `[路径]` |
| 8 | `posix_home` —— `/home/…`、`/Users/…` | `[路径]` |
| 9 | `user_name` —— `core.user_name` 的字面值 | `[我]` |
| 10 | `extra_terms` —— 用户自定义字面量 | `[自定义]` |

**顺序有意义**：`url_credentials` 必须在 `email` 之前
（`https://a@b.com:pw@host` 里嵌着一个邮箱），
`id_card_cn` 必须在 `phone_cn` 之前（18 位里含 11 位数字子串）。

**幂等性**：占位符本身不含任何规则能匹配的形状。
`[手机号]` 不会被 `phone_cn` 二次命中。测试钉住 `redact(redact(x)) == redact(x)`。

---

## 4. 输出

```
exports/datasets/<角色名>/
  conversation.chat.jsonl
  reasoning.chat.jsonl
  tooluse.chat.jsonl
  manifest.json     ← 机器可读：条数、字节、sha256、时刻、脱敏规则摘要
  README.md         ← 人可读：从哪来、脱了什么、怎么拿去微调、已知局限
```

`README.md` 就是「数据页面」的当前形态，可以整个丢进 Obsidian 看。

**`manifest.json` 的 `redact_digest`** 是规则表的 sha256 前 12 位。
`dataset list` 拿它和当前代码比，不一致就提示「脱敏规则变了，建议重跑 build」——
这是对「脱敏必须可回溯重做」那条决策的**兑现机制**，不是装饰。

---

## 5. CLI

```
alterego dataset build [--format F]... [--since TIME] [--out DIR] [--persona NAME] [--dry-run]
alterego dataset list  [--persona NAME]
alterego dataset paths [--persona NAME]
alterego dataset show  NAME [--limit N] [--format F] [--persona NAME]
```

`show` **从库里实时渲染**，不读磁盘上已有的文件。
理由：改了脱敏规则后，`show` 立刻反映新规则；读文件则要看旧的。

---

## 6. 插件

`plugins/dataset_exporter/`（id `capability.dataset_exporter`）。

**极薄，和 `obsidian_vault` 同一个形状**：

- `PluginContext` 没有 `llm()`（那是 v0.2.0），所以真活全在 `sim/dataset.py`
- **不订阅任何事件**——导出是一次性动作，不该由 tick 频率驱动
- `intent_types = frozenset()`（空的，故意的）
- `enabled_by_default = false`

---

## 7. 本批次**不做**的（写清楚，免得下一轮当成 bug）

| 不做 | 为什么 |
| --- | --- |
| Web 数据页面 | `src/alterego/channels/web/` 只有 docstring 与空 `static/`，**零 HTTP server 实现**。建它是独立一大块 |
| `alterego dataset train` | 见 ADR-0011 决策五：不做训练，只产出数据 |
| 工具调用数据集的真实内容 | 上游 `sim/` 未实现，今天必然 0 条 |
| 用 LLM 二次审脱敏结果 | 违反 P3（不可测）且每条花钱 |

---

## 8. 验收

```bash
# 三类数据集都能产出，且文件里有内容
alterego dataset build

# 脱敏真的生效
alterego dataset show conversation --limit 1

# 路径可查
alterego dataset paths
```

自动化验收见 `tests/test_domain_redact.py` 的
`test_no_rule_leaks_the_original_value`（断言 PII 原值不出现在输出里）。

---

## 9. 落地时与原计划的偏差

计划是计划，写代码时总会撞上计划里没写到的东西。**这一节是账**，下一轮读这份文件
时不用重新推一遍：

| 原计划 | 实际 | 为什么 |
| --- | --- | --- |
| `build --since TIME` | `build --days N` | 窗口长度是配置项（`[dataset] lookback_days`），命令参数只做**覆盖**；再来一个绝对时间是第二个真源，两个对不上时没人知道听谁的 |
| `show NAME`（位置参数） | `show [--format F]` | 四个子命令共用 `_add_common` 的 `--persona` / `--out`。再加一个位置参数 `NAME`，它会和 `--persona` 长得像两件事、其实是同一件事 |
| 没有 | `_sweep()`：清掉目录里上一次留下的旧形状 | 真跑一次才发现：`formats` 改过之后，`*.chat.jsonl` 和 `*.sharegpt.jsonl` 会同时躺在目录里，而 `README.md` 只描述一种形状。任何按 `*.jsonl` 整体训练的脚本会把每段对话学两遍 |
| 没有 | `_unlisted()`：点出不在 `manifest.json` 里的 `.jsonl` | 用户自己丢进去的文件、上一次「一条样本都没拼成」时跳过的文件，都不在记录里。不在记录里就没人知道它是按哪套规则脱的 |
| `render_readme` 里写死一段示例 | `_EXAMPLES[primary]`，按当次的主形状选 | 三种形状的包裹键完全不同。写死 `{"messages": ...}`，选了 `sharegpt` 的人照它写的解析器一条都读不出来 |
| 有 `REASONING_CLOSE` 常量 | 删掉了 | 只有开标签有意义（模型不需要被提醒「思考结束」），留一个常量等于邀请代码去生成不配对的标签 |
| 三类数据集各写一个 `.jsonl` | **空的那一类不写文件**，落进 `skipped` 并说清是三种「哪一种空」 | 一个 0 字节的 `.jsonl` 让人第一反应是「文件坏了」 |
| `kernel/config.py` 里写一遍形状名单 | 写 `_DATASET_FORMATS`，并由 `tests/test_kernel_config.py` 的一条断言钉住它 == `domain.dataset.FORMATS` | 内核不能 import domain（第 4 组红线），所以名单必须重复一遍。**重复可以，漂移不行**——断言把两边绑死 |


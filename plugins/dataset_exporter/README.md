# 训练数据集导出

把角色说过的话、想过的事、调过的工具整理成可以直接拿去微调的 JSONL。

## 为什么需要它

一个跑了几周的实例，库里会有几千条消息、几万条推演记录。这些数据
是**你自己的**：没有版权、没有隐私第三方、分布正好是你想要的那个角色。
拿它微调一个本地小模型，比拿通用语料微调出来的更像「这个人」。

但它不能原样导出——里面会有你的手机号、路径、邮箱、IP。所以这个
子系统先脱敏再落盘，而且脱了什么、脱了几处，都写在旁边那份
`manifest.json` 里（见 [ADR-0011](../../docs/adr/0011-training-datasets-are-derived-and-redacted.md)）。

## 装

把 `plugins/dataset_exporter` 拷到插件搜索路径下（默认是项目里的
`plugins/` 或 `~/.alterego/plugins/`），然后在 `config/alterego.toml` 里加：

```toml
[plugins]
enabled = ["capability.dataset_exporter"]
```

它是 `capability` 插件，`enabled_by_default = false` —— 不主动往别人实例里塞东西。

## 配置

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `export_dir` | string | `""` | 导出根目录。**留空**表示交给 `alterego dataset` 按角色名决定（`exports/datasets/<角色名>`） |
| `formats` | string[] | `[]` | 要出哪些形状：`chat` / `sharegpt` / `alpaca`。留空表示按 `[dataset] formats` |
| `redact_terms` | string[] | `[]` | 内置八条规则之外，还要额外脱掉的词 |

三项改完都立即生效（`auto_reload = true`），不用重启。

`export_dir` 是 `string` 而不是 `path`，因为「留空」和「填了 `.`」
必须是两件事：写成 `path` 的话 `""` 会被规范化成 `.`，配置里的空
和配置里的当前目录就没法区分了。

## 用

这个插件**自己不导出**。导出由命令驱动，因为落盘、取数、脱敏分别
要 `StorageBackend`、`ScheduleRepository` 和 `sim/dataset.py`，
那三样插件都拿不到（第 3 组红线）。

```bash
alterego dataset paths        # 会落在哪几个文件（还没跑过也能问）
alterego dataset show         # 现场渲染几条出来看，不写文件
alterego dataset build        # 取数 → 脱敏 → 写 .jsonl + manifest.json + README.md
alterego dataset build --dry-run     # 只算不写
alterego dataset list         # 磁盘上那一批什么样、该不该重跑
```

第一次用：`show` 看一眼 → `build` → `list` 确认。

## 导出什么

三类，都靠 `manifest.json` 里的 `kind` 区分：

| 文件 | 教模型什么 | 从哪来 |
| --- | --- | --- |
| `conversation.<形状>.jsonl` | 这个角色怎么说话 | `message` 表里和用户的往返 |
| `reasoning.<形状>.jsonl` | 这个角色怎么想 | `tick_log` 里的感知、候选、选中意图、动机 |
| `tooluse.<形状>.jsonl` | 这个角色什么时候调工具 | `activity_log` 的 `intent`，配上那一次 tick 的选中意图 |

**空的那一类不会写成空文件。** `build` 会说清是哪一种空：
「库里这个时间段还没有对应的记录」、「取到 N 行但一行都没拼成」、
或者「上游还没落地」。0 字节的 `.jsonl` 会让人以为文件坏了。

## 换形状会清旧的

`formats` 从 `["chat"]` 改成 `["sharegpt"]` 之后，上一次留下的
`*.chat.jsonl` 会被删掉，`build` 的输出里看得见删了哪几个。

不这样做的话，目录里会同时躺着两种形状，而任何按 `*.jsonl` 整体
训练的脚本都会**把每段对话学两遍**——同时 `README.md` 只描述了其中一种。

如果你是自己往这个目录里放了 `.jsonl`，它不在 `manifest.json` 里，
`list` 会把它报出来：「有不在这份记录里的文件」。**不要**用整个目录
喂给训练脚本，先看 `list`。

## 脱敏

八条内置规则，任何时候都生效，没开关：

| 规则 | 换成 |
| --- | --- |
| 网址里的账号密码 | `[凭据]` |
| API 密钥、Token | `[密钥]` |
| 邮箱 | `[邮箱]` |
| 身份证号 | `[证件号]` |
| 手机号 | `[手机号]` |
| IPv4 地址 | `[IP]` |
| Windows 路径 | `[路径]` |
| `/home/xxx` 这类路径 | `[路径]` |

另外角色名（`[dataset] user_name` / `core.user_name`）会被换成
`[我]`，`redact_terms` 里的自定义词会被换成 `[自定义]`。

`manifest.json` 里的 `redact_digest` 是当时那套规则的指纹。
`list` 发现指纹和现在算出来的不一样，就会说「该重跑」——
改了规则却没重导，磁盘上那份就是按**旧规则**脱的。

**替换是单向的。** 原文里的 `[手机号]` 和脱敏生成的 `[手机号]`
长得一样，导出之后分不出来。防的是「顺手把日志发出去」，
不是「有人拿着导出文件反推」。真要把数据给出去，先确认一遍。

## 落点

```
exports/datasets/<角色名>/
├── conversation.<形状>.jsonl
├── reasoning.<形状>.jsonl
├── tooluse.<形状>.jsonl
├── manifest.json     机器看的：条数、字节数、sha256、脱敏摘要、规则指纹
└── README.md         人看的：怎么用、脱了什么、已知局限
```

`README.md` 是**生成**的，别手改——下次 `build` 会覆盖。
`export_dir` 是相对路径时，相对的是**跑命令时的工作目录**
（和 `alterego vault` 一致）。

## 已知局限

- **没有工具调用的结构化记录。** 库里的 `activity_log` 没有「调了哪个
  工具、参数是什么、返回什么」这几列，所以 `tooluse` 教的是
  「什么样的情形该选哪个意图」，不是「怎么填参数」。等推演引擎开始
  写工具调用日志，这一类才有真正的调用轨迹。
- **`reasoning` 依赖 `tick_log`。** 推演引擎还没写 `tick_log` 之前，
  这一类必然是空的，输出里会明说「上游还没落地」。
- **推理链是拼出来的，不是模型原本想的。** 「思考过程」是从
  感知、候选、选中意图这些**决策记录**还原的，不是当时那段
  真实的思考文本。它能教「这个角色会怎么权衡」，教不了「它的措辞」。
- **不含图片和语音。** `content_type` 不是纯文本的消息会被跳过。

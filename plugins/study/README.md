# 专项学习

让角色按自己的人设，一次一格地把它本行的东西学下来，写进知识库的「60-专业」。

## 为什么需要它

人设里只有一句 `occupation`（「产品经理」），而一个产品经理该懂的东西不是
一句话能装下的。这个子系统把它展开成一张**有序、固定**的课程表：一个领域
× 五个切入面 = 五格，每学一次消耗一格，顺序固定所以可复现——同样的进度
跑两次，学的是同一个主题。

学完的东西落在知识库里，于是「它怎么突然说起这个」永远答得出来：
它只会说起自己在 `60-专业` 里记过的东西（见
[ADR-0012](../../docs/adr/0012-specialized-study-is-a-curriculum-not-a-prompt.md)）。

## 装

把 `plugins/study` 拷到插件搜索路径下（默认是项目里的 `plugins/`），
然后在 `config/alterego.toml` 里加：

```toml
[plugins]
enabled = ["capability.study"]
```

它是 `capability` 插件，`enabled_by_default = false` —— 不主动往别人实例里塞东西。

## 配置

**这个插件没有配置项，是有意留白的，不是没写完。**

唯一会改变学习行为的四个值全在 `[study]` 段里，`alterego study` 直接读那里：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `field` | `""` | 学什么专业。**留空表示按人设的 `occupation` 认**；认不出来就不学，也不编一个方向 |
| `rounds` | `1` | 一次学几格 |
| `recall_limit` | `3` | 聊天时最多翻出几篇专业笔记 |
| `min_score` | `2.0` | 够多少分才算「这次聊到专业了」 |

`templates/alterego.toml` 里这一段带注释列好了，改它不用重启。

为什么不在插件里再抄一份：那就是一份设置的**两个真源**——改插件这份不会改
命令的行为，改命令那份又不会改插件说的话，两边都「看起来对」，而其中一个是
谎话。同一个理由，`cli_study.py` 已经为「库在哪」拒绝过一次。

所以 `alterego plugins config capability.study` 会说「清单里没有声明任何配置项」，
那是它的真实状态。

## 用

这个插件**自己不学**。学习由命令驱动：它要读人设（得有已装好的存储连接）、
要按知识库的目录白名单落笔（那段规则在领域层）、还要在命令行上按需调一次模型。
插件只被允许 import `alterego.interfaces.*` 与 `alterego.kernel.plugin`（见
[`docs/guide/plugin-development.md`](../../docs/guide/plugin-development.md) § 11.1），
前两样它够不着；`ctx.llm()` 虽然存在，但只在一次 tick 里有（`on_tick_pre` /
`on_tick_post`），而 `alterego study next` 是人在命令行上喊的一次，不是 tick。

```bash
alterego study status         # 学到哪了、库里有什么          （不花钱）
alterego study plan           # 接下来会学哪几格              （不花钱）
alterego study plan --rounds 5
alterego study recall "这条 SQL 怎么优化"   # 看它会翻出哪几篇（不花钱）
alterego study next           # 学几格，写进 60-专业          （会调用模型）
alterego study next --rounds 3
alterego study next --dry-run # 只说这次会学什么，不调模型、不写文件
```

第一次用：`status` 看方向认出来没有 → `plan` 看课程表 → `recall` 试一句话 →
`next --dry-run` → `next`。

四个命令都接受 `--vault DIR` 和 `--persona NAME`，和 `alterego vault` 完全一样
（它们共用同一个知识库；「库在哪」有两个真源时，两条命令会往不同的目录里写）。

## 它学什么

一个领域展开成五格，**顺序即学习顺序**：

| 格 | 学的是 |
| --- | --- |
| 是什么 | 定义和边界 |
| 怎么做 | 具体动作 |
| 容易踩的坑 | 反例 |
| 和什么容易混 | 区分 |
| 我还不服的 | 它自己的判断 |

「我还不服的」是刻意留的：只会讲定义的人不算懂，这一格逼它留一条自己的看法，
而这条看法是它以后会不会主动提这个话题的关键。

方向由人设的 `occupation` 认（14 个领域的词表）。**认不出来时它不学**——
「研究生」「学生」这种看不出专业的，命令会直接说「不知道该学什么」
并告诉你把 `[study] field` 填上。学错方向的代价是它写出一堆像模像样的空话，
而没人会想到去检查。

## 学到的东西存在哪

```
exports/<角色名>的知识库/
├── 60-专业/
│   ├── 计算机软件 · 是什么.md
│   └── 计算机软件 · 怎么做.md     文件名由主题决定，同一个主题重跑两次落成同一个文件
└── 00-索引/
    ├── 专业.md                    索引（`alterego vault build` 会重算）
    └── .study-state.json          进度：哪个领域、学完哪几格、上次什么时候
```

`.study-state.json` 以点开头，Obsidian 不显示它。**进度以这个文件为准**，
不是以条数推断出来的——删一篇笔记不等于没学过，改一篇也不等于学了两格。

## 已知局限

- **召回只做完了机制那半。** 「给一句话 → 该翻哪几篇笔记」是纯函数，
  `alterego study recall` 就是它的证据；但「每轮对话自动塞进上下文」这一半
  还没接上——对话循环还没落地。所以今天它是「能让它学会」，还不是
  「它讲得出来」。
- **一次 `next` 只写一格，不写复习。** 没有间隔重复、没有遗忘曲线，
  同一格不会自己回来。重跑 `next` 不会重复写（文件名由主题决定）。
- **学的东西不会自动更新。** `60-专业` 里的笔记是生成物，改它们请直接改，
  但 `next` 只往后学，不会回头修订旧的那几篇。

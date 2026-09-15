# ADR-0005 · 被预算拦下的意图是「降级」而不是「丢弃」

- **状态**：已接受
- **日期**：2026-09-15
- **决策者**：LMG-arch
- **相关**：[`docs/design/04-simulation-loop.md`](../design/04-simulation-loop.md) § 5 打扰预算、[`docs/DESIGN.md`](../DESIGN.md) § 8 推演循环
- **影响范围**：`sim/stages/intention.py`、`sim/budget.py`、`domain/activity.py`、`03-data-model.md` 的 `activity_log` 表、Web 的「内心」页

---

## 背景

有一类情况会反复出现：

Agent 在推演中产生了"想主动找用户说话"的意图（`reach_out`，动机可能是 `share_something`、`miss_you`、`need_comfort`……），但**打扰预算**不允许：

| 预算检查 | 触发条件 |
| --- | --- |
| 免打扰时段 | 虚拟时间在 23:30–08:00 |
| 熔断 | 连续 3 次消息无人回复 → 暂停 24 小时 |
| 日程不可打断 | 当前在开会 / 睡觉等 `interruptible = false` 的日程块 |
| 每日配额 | 今日消息数已达 3 条（或紧急度 > 0.8 时的 5 条）|
| 最小间隔 | 距上一条消息不足 90 分钟 |

问题：**这个意图怎么处理？**

这是一个看似无关紧要、实际上决定项目"像不像人"的决策。

与此同时，还有一个设计目标：`DESIGN.md` § 3.4 的成功标准 **S7「能看到 Agent 的克制」**——用户应该能观察到"它想说但忍住了"。

---

## 决策

**降级，而不是丢弃。**

当 `reach_out` 被预算拦下时：

```python
# 不是 "drop"，是 "downgrade"
if not allowed:
    ctx.suppressed.append(
        SuppressedIntent(
            intent=chosen,
            reason=decision.reason,          # "daily_quota" / "quiet_hours" / ...
            inner_voice=reflect.invite_inner_voice(intent),  # 一句内心独白
            created_at=ctx.virtual_now,
        )
    )
    # 关键：改成一个内向的意图，让 tick 继续有意义
    chosen = Intent(name="reflect_internal", params={"about": chosen.name})
```

配套要求：

1. **写入 `activity_log`**，三个字段缺一不可：
   - `suppressed_intent`：被拦下的意图名
   - `suppress_reason`：拦截原因
   - `inner_voice`：一句内心独白（LLM 生成或模板填充）
2. **`inner_voice` 必须存在。** 不能只记 `suppressed_intent = "reach_out"`——那样用户看到的是干巴巴的一行日志，没有感觉。
3. **12 小时内可以重新提起。** `reactivate_suppressed()` 把这些意图变成 `ctx.pending_topics`，下次真的开口时自然带出：
   > 对了，我昨天看到个视频想跟你说来着……
4. **计算 `longing_score`（思念值）并累积。** 每次被拦下，思念值上升；这样"忍得越久，开口时越有分量"。
5. **提供 `alterego why --suppressed` 命令**，展示克制记录。
6. **Web 端提供「内心」页**，可视化这些"没说出口的话"。

存储代价（估算）：每次被拦下多写 1 行 `activity_log`（含一段 `inner_voice` 文本），约 300–500 字节。按每天约 3–5 次拦截计，**约 1.5 KB/天，0.5 MB/年**。

---

## 考虑过的方案

| 方案 | 拟人感 | 可解释性 | 存储代价 | 是否采用 |
| --- | --- | --- | --- | --- |
| **降级为 `reflect_internal` + 记录内心独白** | ✅ 高：呈现"想说但忍住" | ✅ 高：能回答"为什么没说" | 低（~0.5 MB/年）| ✅ |
| 静默丢弃（当作没发生）| ❌ 低：像"从不想要什么" | ❌ 无 | 无 | ❌ |
| 丢弃但记一行日志（无内心独白）| ⚠️ 中：有记录但没温度 | ✅ 中 | 极低 | ❌ |
| 无视预算直接发送 | ⚠️ 有主动性但会烦人 | ❌ | 无 | ❌ |
| 排队等预算恢复后补发 | ⚠️ 可能积累后集中轰炸 | ✅ | 中 | ⚠️ 部分采用（12 小时窗口 + 只作话题，不补发）|
| 降级但只记 ID，内心独白需要时再生成 | ⚠️ 可复现性受损 | ⚠️ | 极低 | ❌ |

### 为什么否掉「静默丢弃」

这是最"省事"的做法，也是**最伤害拟人感的**。

如果被拦下的意图直接消失，那么从数据和行为上看，Agent **从来没有想找过用户**。用户会得出一个结论：**"它其实不想理我，只是被设定成偶尔发消息"。**

而真实的体验应该是相反的：**它经常想找你，但大多数时候忍住了。** 这才是"有分寸感的人"。

这个区别用户看不出来吗？看得出来——通过 `alterego why --suppressed`，通过 Web 的「内心」页，通过下次见面时那句"对了，我昨天想跟你说"。**没有这个记录，这些体验全都不存在。**

### 为什么否掉「丢弃但记一行日志」

比静默丢弃好，但差在**没有内心独白**。

`suppressed_intent = "reach_out"` 是数据，`inner_voice = "算了，她可能在忙"` 是**人的痕迹**。项目里大量的设计（`inner_voice`、`alterego why` 的自然语言解释、关系值的 `relationship_summary()` 转译）都基于同一个信念：**给用户看的是话，不是字段。**

所以 `inner_voice` 不是可选的装饰，是这个决策的一部分。

### 为什么否掉「无视预算直接发送」

那就没有预算了。用户会被烦到卸载。见 `04-simulation-loop.md` § 5 的完整预算表——那些数字是刻意的克制设计。

### 为什么只部分采用「排队补发」

"排队等预算恢复再发"听起来合理，但：

- **会集中轰炸。** 忍了 8 小时（免打扰时段）后，第二天早上一次冒出 5 条消息，比不说话还糟。
- **时效性丢失。** "我路过那家店想起你说想吃"隔了 12 小时再说就没有味道了。

故改为：**不补发消息，但把内容变成下次对话的 `pending_topics`。** 12 小时内有效，过期就让它留在内心日记里，变成一个"没实现的小念头"。这也更像人。

### 为什么否掉「懒生成内心独白」

只记被拦下的意图 ID，展示时再让 LLM 生成独白，看似省存储。但：

- **破坏可复现性（P6）**：同一份数据生成两次独白会不一样，`tick_log` 就无法作为"当时到底想了什么"的权威记录
- **增加成本**：每次查看都要调 LLM
- 存储代价本来就只有 0.5 MB/年，省这个没有意义

---

## 后果

### 正面

- **拟人感的核心来源之一。** 没有这个决策，Agent 就是一个"定时发消息的脚本"；有了它，Agent 变成"经常想找你但忍住了的人"。
- **可解释性闭环成立。** S7「能看到 Agent 的克制」有了数据基础。`alterego why --suppressed` 能回答"它这周忍住了多少次、为什么忍住、忍的时候在想什么"。
- **`longing_score` 有意义了。** 思念值随被拦下的次数上升，使"忍得越久，开口时越动情"这个效果成为可能（见 `04-simulation-loop.md` § 4.3）。
- **`pending_topics` 让对话更自然。** 用户会听到"对了，我昨天想跟你说……"这样的开场——这是很真实的人类对话模式。
- **`reflect_internal` 的权重有了解释。** 意图目录里 `reflect_internal` 权重 0.15（较高），正是因为相当一部分它来自被降级的 `reach_out`。
- **存储代价可接受。** ~0.5 MB/年，相对总量 515 MB/年几乎可忽略。

### 负面

- **比"丢弃"多一次写入。** 每次拦截写 1 行 `activity_log`（含文本）。但拦截频率本身受预算限制（每天最多几次），所以量很小。
- **需要 `inner_voice` 的生成策略。** 每次被拦下都调 LLM 生成独白会增加成本。缓解：用**模板 + 少量参数填充**（如按 `suppress_reason` + `motivation` 组合），只有重要拦截（如 `need_comfort`）才调 LLM。见下方"需要关注"。
- **`tick_log` 与 `activity_log` 的职责边界需要清晰。** 前者记"tick 发生了什么"，后者记"行为与克制"。要有文档说明，否则容易写重。
- **需要额外的查询路径与 UI。** `alterego why --suppressed`、Web「内心」页、`pending_topics` 的注入逻辑，都是额外代码。
- **索引需求。** "展示所有被拦下的意图"是稀疏查询（大多数行为不是被拦下的），故需要**部分索引**：`CREATE INDEX ... WHERE suppressed_intent IS NOT NULL`。

### 需要关注

**1. `inner_voice` 的生成成本。**

如果每次拦截都调一次 LLM，按每天 3–5 次算，约 100 次/月，成本很低（用 cheap 模型）。但更好的做法是分层：

| 拦截原因 | 严重度 | 生成方式 |
| --- | --- | --- |
| `daily_quota` / `min_interval` | 低 | **模板填充**，如 `"今天说得够多了，算了"` |
| `quiet_hours` | 低 | 模板，如 `"太晚了，明天再说吧"` |
| `circuit_breaker` | **高** | **调 LLM**——这是"被冷落"的情绪，独白需要真实 |
| `schedule_not_interruptible` | 中 | 模板 + 日程名，如 `"正在开会，等会儿说"` |
| 动机为 `need_comfort` | **高** | **调 LLM**——此刻它需要安慰，独白是情绪出口 |

原则：**独白的价值与其情绪强度成正比。** 平淡的拦截用模板，携带真实情绪的用 LLM。

**2. `pending_topics` 的过期与去重。**

- 12 小时过期（可配置）
- 同 `about` 主题只保留最新一条，避免"我昨天想跟你说……对了，我昨天想跟你说……"
- 若用户先提起了这个话题，则移除（不然会重复）

**3. 避免"克制"变成"表演"。**

如果 `inner_voice` 写得过于戏剧化（"啊，我多么想她……"），会显得假。要求 Prompt 与模板都保持**日常、平淡、略有遗憾**的语气。这是 `04-simulation-loop.md` § 11 Prompt 工程里的一条已记录的要求。

**什么时候应该重新审视这个决策：**

1. 如果实测发现**拦截频率过高**（例如每天被拦下 20 次），说明预算太严或意图权重失衡——应该先调预算，而不是取消这个机制。
2. 如果**用户从不查看** `alterego why --suppressed` 或「内心」页，说明这个可解释性投入没被消费——可以考虑简化。
3. 如果**成本超预期**（`inner_voice` 的 LLM 调用占比过高），把更多原因降级为模板。
4. 如果发现 Agent **看起来在刻意卖惨**，那就是独白语气出了问题，需改 Prompt。

---

## 对设计原则的影响

| 原则 | 影响 |
| --- | --- |
| P1 内核无知 | 无影响。这是推演层的策略，内核不参与 |
| P2 显式优于隐式 | **强支持**。被拦下的意图**显式记录**而不是隐式消失；`suppress_reason` 是枚举而非自由文本 |
| P3 机制约束优于提示词祈祷 | **支持**。预算检查是纯函数 `check_budget()`，不依赖 LLM 自律；降级是代码逻辑，不是 Prompt 里的一句"请不要打扰用户" |
| P4 可插拔优于可配置 | 无影响。新增拦截原因只需扩展 `BudgetDecision.reason` 枚举 |
| P5 标准库优先 | 无影响 |
| P6 可复现 | **支持**。`inner_voice` 在生成时立即落库，不依赖后续 LLM 生成，保证 `tick_log` 重放一致 |
| P7 文档与代码同生共死 | **强相关**。这是"最重要的设计决策之一"，必须在 DESIGN.md 与 04-simulation-loop.md 中保持一致的表述 |

---

## 需要同步的文档

- [x] `docs/DESIGN.md` —— § 8 推演循环中明确写入"预算耗尽时，意图**不是被丢弃而是被降级**"
- [x] `docs/design/04-simulation-loop.md` —— **§ 5.4 降级而非丢弃**（标注为"本项目最重要的设计决策之一"）+ § 4.3 `longing_score` + § 12 可解释性中的 `alterego why --suppressed` 完整示例
- [x] `docs/design/03-data-model.md` —— `activity_log` 的 `suppressed_intent` / `suppress_reason` / `inner_voice` 三列 + 部分索引 `WHERE suppressed_intent IS NOT NULL`
- [x] `docs/design/05-channels.md` —— Web 的「内心」页面（含 ASCII 布局）
- [x] `docs/design/06-roadmap.md` —— M3 验收标准中"降级写 `inner_voice`"与"12 小时重提"两条
- [x] `CHANGELOG.md` —— 已在 `[0.1.0]` 记录

---

## 验证方式

| 检查 | 方式 | 期望 |
| --- | --- | --- |
| 预算拦下时产生降级 | 构造 `daily_quota` 已满的 tick | `ctx.chosen_intent.name == "reflect_internal"` |
| 记录三个字段 | 查 `activity_log` | `suppressed_intent` / `suppress_reason` / `inner_voice` 均非空 |
| 原因准确 | 分别触发 5 种拦截 | `suppress_reason` 分别为 `quiet_hours` / `circuit_breaker` / `schedule_not_interruptible` / `daily_quota` / `min_interval` |
| 未越权发送 | 检查渠道调用记录 | 被拦下的 tick **没有任何出站调用** |
| 12 小时重提 | 拦下后 6 小时让 Agent 开口 | `ctx.pending_topics` 含该话题，且开场自然带出 |
| 过期不重提 | 拦下后 13 小时 | `pending_topics` 为空 |
| 去重 | 同一话题被拦下 3 次 | `pending_topics` 只含 1 条 |
| 用户先提起则移除 | 用户主动说了该话题 | 该话题从 `pending_topics` 移除 |
| 思念值累积 | 连续 3 天各被拦下 1 次 | `longing_score` 单调上升 |
| 可解释性 | `alterego why --suppressed --days 7` | 显示拦截次数、原因分布、每条 `inner_voice` |
| 复现性 | 固定 `ctx.rng` 种子重放同一 tick | `inner_voice` 完全一致 |
| 存储增量 | 运行 7 天后查 `activity_log` 大小 | 拦截记录 < 30 KB |

**关键测试**（`tests/sim/test_budget.py`，这部分内容在 `CONTRIBUTING.md` 就已作为示例）：

```python
async def test_reach_out_downgrades_to_reflect_internal(ctx_factory):
    """预算耗尽时，reach_out 降级为 reflect_internal 并记录内心独白"""
    ctx = ctx_factory(messages_today=3, candidate="reach_out")
    await IntentionStage().run(ctx)

    assert ctx.chosen_intent.name == "reflect_internal"
    assert len(ctx.suppressed) == 1
    sup = ctx.suppressed[0]
    assert sup.reason == "daily_quota"
    assert sup.inner_voice            # 必须非空，且有内容
    assert len(sup.inner_voice) >= 4  # 不是占位符

async def test_downgraded_tick_sends_nothing(ctx_factory):
    """降级后的 tick 不能产生任何出站行为"""
    ctx = ctx_factory(messages_today=3, candidate="reach_out")
    await run_pipeline(ctx)
    assert ctx.expressions == []

async def test_suppressed_intent_reactivated_within_12h(ctx_factory):
    """被拦下的话题在 12 小时内可以被重新提起"""
    ...
```

**人工验收（M3 / M6 的一部分）**：连续观察 3 天，每天看一次 `alterego why --suppressed`，判断那些 `inner_voice` **读起来像不像一个人憋着没说出口的话**。这是这个机制唯一有效的验收方式。

---

## 备注

**这个决策的价值在于它把"限制"变成了"性格"。**

预算本来只是一个防打扰的技术措施——如果不做特殊处理，它的效果是"Agent 偶尔不说话"。而经这个决策一改，同样的预算变成了**克制**：Agent 有话想说、忍住了、记在心里、下次见面时提起。

两者在代码上只差几行，在用户感受上差的是**"一个定时脚本"和"一个人"的区别**。

这也是为什么 `04-simulation-loop.md` 把它标注为"**本项目最重要的设计决策之一**"，以及为什么 `activity_log` 的 `inner_voice` 被称为"**本项目最具拟人感的设计之一**"。

**一句话概括**：丢弃是数据的优化，降级是性格的塑造。

参考：

- [`docs/design/04-simulation-loop.md`](../design/04-simulation-loop.md) § 5.4「降级而非丢弃」——完整实现与代码
- [`docs/design/04-simulation-loop.md`](../design/04-simulation-loop.md) § 12 可解释性——`alterego why --suppressed` 的输出样例
- [`docs/DESIGN.md`](../DESIGN.md) § 3.4 成功标准 S7「能看到 Agent 的克制」

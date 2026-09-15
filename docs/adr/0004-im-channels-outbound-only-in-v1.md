# ADR-0004 · v1 的 IM 渠道仅支持单向出站

- **状态**：已接受
- **日期**：2026-09-15
- **决策者**：LMG-arch
- **相关**：[`docs/design/05-channels.md`](../design/05-channels.md)、[`docs/DESIGN.md`](../DESIGN.md) § 11 外部渠道
- **影响范围**：`channels/`、v0.1.0 的功能范围、`06-roadmap.md` 的版本规划

---

## 背景

用户希望 Agent「可以主动发消息给用户」。最自然的需求落点是"能在手机上收到它的消息"，而这意味着接入 IM。

调研后发现一个**关键事实**：

> **企业微信「群机器人」和钉钉「自定义机器人」都是单向渠道——只能发，不能收。**

原因（技术细节见 `05-channels.md` § 1）：

| 平台 | 群机器人 / 自定义机器人的机制 | 能收吗 |
| --- | --- | --- |
| 企业微信 | 你创建一个 Webhook URL，向它 POST 消息，消息出现在群里 | ❌ 没有回调地址。机器人在群里**也不响应 @**，那是「应用消息」的能力 |
| 钉钉 | 同上，Webhook + 加签 | ❌ 用户在群里 @机器人不会被投递到你的服务 |

**能双向的只有「应用」形态**（企业微信自建应用 / 钉钉企业内部应用），而它要求：

| 要求 | 说明 |
| --- | --- |
| **企业主体** | 需要企业认证，个人开发者无法创建 |
| **公网可达的回调地址** | 需要服务器 + 域名（或内网穿透），且域名要备案（国内） |
| **复杂的配置** | `corp_id` / `agent_id` / `corp_secret`、可信域名、`Token`、`EncodingAESKey` 等 |
| **消息加解密** | 需实现 AES-256-CBC + PKCS7 + SHA1 签名校验 |
| **审核** | 部分能力需要平台审核 |

对"本地单人运行的开源工具"来说，这套要求是不合理的门槛。

同时项目本身还有一个约束：**v1 要尽快跑起来**（`06-roadmap.md` § 1.3「先跑起来再完美」，因为拟人感只能通过长期运行验证）。

---

## 决策

**v1（v0.1.0）的 IM 渠道只做单向出站。双向 IM 推迟到 v0.3.0。**

具体切分：

| 渠道 | 方向 | 版本 | 说明 |
| --- | --- | --- | --- |
| `web` | **双向** | v0.1.0 | **v1 唯一的入站渠道。** 本地浏览器，FastAPI + SSE |
| `console` | 双向 | v0.1.0 | 终端交互，调试用 |
| `file` | 双向 | v0.1.0 | 写文件出站 + 轮询 `inbox/` 入站。离线兜底 |
| `dingtalk_webhook` | **仅出站** | v0.1.0 | 主动消息推送到手机 |
| `wecom_webhook` | **仅出站** | v0.1.0 | 同上 |
| `channel.wecom_app` | 双向 | v0.3.0 | 应用消息，需企业主体 + 公网回调 |
| `channel.dingtalk_app` | 双向 | v0.3.0 | 企业内部应用 |
| `channel.telegram` | 双向 | v0.2.0 | **无需企业主体**，长轮询即可双向——性价比最高的双向方案 |

**为了实现这个切分，`Channel` 协议必须显式声明方向：**

```python
class Channel(Protocol):
    @property
    def direction(self) -> frozenset[Literal["in", "out"]]:
        """本渠道支持的方向。单向出站渠道返回 frozenset({"out"})"""

    @property
    def capabilities(self) -> frozenset[str]:
        """支持的消息能力，如 {"text", "markdown", "image", "mention"}"""

    async def send(self, msg: OutboundMessage) -> SendResult: ...

    async def on_receive(self, msg: InboundMessage) -> None:
        """单向渠道必须抛 NotImplementedError"""
```

并且：

1. **`[routing]` 配置中第一个 `direction` 含 `"in"` 的渠道被视为主渠道。**
2. **所有渠道必须声明 `direction`，缺失则 manifest 校验失败。**
3. **`alterego channels doctor` 必须明确显示方向**，例如 `方向: 仅出站`。
4. **启动输出、README、Web 状态页都要说明单向限制**，避免用户以为可以回复。

---

## 考虑过的方案

| 方案 | 能满足"手机收到消息" | 配置门槛 | v1 可行性 | 是否采用 |
| --- | --- | --- | --- | --- |
| **IM 仅出站 + Web 入站** | ✅ 能收到（只是不能直接回）| 极低：只需一个 Webhook URL | ✅ | ✅ |
| 只做双向应用消息（企业微信/钉钉）| ✅ | **极高**：企业主体 + 公网回调 + 加解密 | ❌ 个人开发者的门槛过高 | ❌ |
| 只做 Telegram | ✅ | 低 | ⚠️ 国内用户访问受限 | ❌（v0.2.0 补上）|
| 自建内网穿透给 IM 回调 | ✅ | 高：需服务器 + 域名备案 | ❌ 超出"本地工具"定位 | ❌ |
| 完全不接 IM，只用 Web | ✅（需打开浏览器）| 无 | ✅ | ⚠️ 但丢失"主动找到你"的核心体验 |
| 邮件（SMTP/IMAP）| ✅ | 中：需邮箱密码 + IMAP 轮询 | ⚠️ 可行但体验差 | 可作社区插件 |

### 为什么否掉"只做双向应用消息"

这是唯一能提供完整双向体验的方案，但它把项目从"`pip install` 就能跑"变成"先注册企业、再买服务器、再备案域名、再配加解密"。

对于一个**本地单人**的开源工具，这个门槛会劝退绝大多数用户，而收益只是"能在同一个 App 里回复"——但用户本来就可以打开 Web 界面回复。

### 为什么仍然要保留 IM 出站（而不是只用 Web）

"主动找到你"是本项目的核心体验。如果只能通过 Web 查看，那用户必须**主动打开界面**才能知道 Agent 说了什么——这削弱了"它有自己的生活、它会想起你"的感觉。

单向出站解决的是最关键的那一半：**它能主动找到你。** 用户收到一条钉钉消息"今天路过那家店，想起你说想吃"——这个体验已经成立了，即使回复要去 Web。

### 为什么否掉"完全不接 IM"

那就丢掉了核心体验。用户的要求明确包含"可以主动发消息给用户"。

### 为什么 Telegram 放在 v0.2.0 而不是 v0.1.0

Telegram 是**唯一无需企业主体就能双向**的方案（Bot API + 长轮询），性价比很高。但它：

- 国内用户访问需要代理，覆盖面有限
- v0.1.0 应该聚焦"能活着"这条主线（`06-roadmap.md` § 1.2），渠道属于 M5，排在 M3（"能活着并主动说话"）之后
- 出站能力由钉钉/企业微信 webhook 已经覆盖

故 v0.2.0 补上，成本很低（约 0.5 人日）。

---

## 后果

### 正面

- **v1 门槛降到最低。** 用户只需要在钉钉/企业微信群里点几下拿到一个 Webhook URL，填进配置即可。**不需要企业主体、不需要服务器、不需要域名、不需要加解密。**
- **开发量显著减少。** 双向 IM 需要实现 `WeComCrypto`（AES-256-CBC + PKCS7 + SHA1 签名）、回调服务器、防重放、IP 白名单、5 秒内 ACK 等。约 3–4 人日的额外工作被推迟。
- **安全面更小。** 单向出站不需要暴露任何入站端点，不需要处理来自公网的请求，不需要考虑签名伪造与重放攻击。
- **不阻塞主线。** M5（渠道）不阻塞 M3（能活着）与 M4（能交互），三者可以按 `06-roadmap.md` 的顺序推进。
- **`direction` 成为显式契约。** 这个字段本身是好设计：内核与路由层能据此判断"这个渠道能不能作为主渠道"，不需要硬编码渠道名字。

### 负面

- **用户在 IM 里回复不会到达 Agent。** 这是最主要的不便。缓解：
  - 启动输出、`channels doctor`、Web 状态页、README **四处**明确说明
  - Web 界面作为唯一入站渠道，体验做完整（含移动端响应式）
  - 消息内容里不带"回复我"之类的引导，避免误导
- **首次使用的困惑风险很高。** 很多用户会本能地在钉钉里回复，然后发现没有响应。这是本 ADR 记录的**最大风险**。
- **"双向"这个卖点在 v1 缺失。** 与商业 AI 伴侣产品的体验有差距。
- **推迟不是取消，但可能被无限推迟。** v0.3.0 的双向 IM 需要企业主体——如果始终没有，可能永远做不成。Telegram（v0.2.0）是无企业主体时的替代路径。

### 需要关注

**这是本 ADR 里最重要的一节。** 单向渠道的最大风险不是技术，是**用户预期管理**。

必须做好的四件事：

1. **启动时明确显示**：
   ```
   ┌─────────────────────────────────────────────┐
   │  已加载渠道                                  │
   │    web              双向  ← 在这里与它对话    │
   │    dingtalk_webhook 仅出站（它能把消息推给你， │
   │                     但不能收你的回复）        │
   └─────────────────────────────────────────────┘
   ```

2. **`alterego channels doctor` 打印方向说明**，并对单向渠道给出提示：
   ```
   dingtalk_webhook
     方向: 仅出站
     ⚠️ 该渠道只能发送。你的回复不会被接收。
        请在 Web 界面回复：http://127.0.0.1:8765
   ```

3. **Web 状态页显示渠道方向**，单向渠道旁标注提示。

4. **README 的快速开始章节**明确说明，不要藏在文档深处。

**什么时候应该重新审视这个决策：**

1. **如果很多用户反馈"在钉钉回复没反应"** → 说明预期管理没做好，优先改文档与提示文案，而不是急着做双向。
2. **如果用户明确表示有企业主体和公网服务器** → 可以提前做 `channel.wecom_app`（作为插件，不改内核）。
3. **如果 Telegram 在国内的可用性改善**，或用户群体主要在海外 → 把 `channel.telegram` 提前到 v0.1.0。
4. **如果决定放弃"本地工具"定位**，转向云端服务 → 整个决策需要重写。但那与 `DESIGN.md` § 15 非目标冲突。

---

## 对设计原则的影响

| 原则 | 影响 |
| --- | --- |
| P1 内核无知 | **支持**。内核只知道 `Channel` 协议，不知道渠道是单向还是双向；`direction` 是协议属性，不是 `if channel == "dingtalk"` |
| P2 显式优于隐式 | **强支持**。`direction` 必须显式声明且校验，避免"以为能收却收不到"的隐式失败 |
| P3 机制约束优于提示词祈祷 | **支持**。`on_receive` 对单向渠道**抛异常**而不是静默忽略，这样路由层不会误以为投递成功 |
| P4 可插拔优于可配置 | **强支持**。双向 IM 不做成内核的 `if-else`，而是做成 `channel.wecom_app` 插件。v0.3.0 只需新增插件文件，不改内核 |
| P5 标准库优先 | **支持**。单向出站只需要 `httpx`（已在必需依赖中）。双向 IM 的加解密需要 `pycryptodome`——放成可选依赖 `[wecom]` |
| P6 可复现 | 无影响 |
| P7 文档与代码同生共死 | **强相关**。这个决策必须在 DESIGN.md、05-channels.md、README、启动输出四处保持一致，否则用户会被误导 |

---

## 需要同步的文档

- [x] `docs/DESIGN.md` —— § 11 外部渠道，说明了单向限制与 v1 的渠道布局
- [x] `docs/design/05-channels.md` —— **§ 1 核心约束：单向 vs 双向**，含 ⚠️ 警告块、对比图、渠道能力矩阵、v2 规划
- [x] `docs/design/02-plugin-api.md` —— `Channel` 接口的 `direction` 属性与 `on_receive` 抛异常约定
- [x] `docs/design/06-roadmap.md` —— § 2.1 版本规划（`channel.wecom_app` → v0.3.0，`channel.telegram` → v0.2.0）
- [x] `README.md` —— 快速开始与特性说明中标注单向限制
- [x] `CHANGELOG.md` —— 已在 `[0.1.0]` 记录
- [ ] `.github/ISSUE_TEMPLATE/bug_report.yml` —— 已在「启用的渠道」中列出各渠道，无需说明方向

---

## 验证方式

| 检查 | 方式 | 期望 |
| --- | --- | --- |
| 所有渠道声明方向 | 启动时遍历 registry 中的 `Channel` 实现 | 每个都有 `direction`，且非空 |
| 单向渠道不谎报入站 | 调用单向渠道的 `on_receive` | 抛 `NotImplementedError` |
| 主渠道选取正确 | 配置 `channels = ["dingtalk_webhook", "web"]` | 主渠道为 `web`（第一个含 `"in"` 的）|
| 路由不向单向渠道投递入站消息 | 模拟用户回复 | 不产生错误，因为根本没有入站路径 |
| 免打扰时段不出站 | 在 23:30–08:00 触发 `reach_out` | IM 推送被丢弃（`is_quiet_hours()` 过滤）|
| 诊断输出含方向 | `alterego channels doctor` | 每条渠道后显示 `方向: 双向` / `方向: 仅出站`，单向渠道附提示 |
| 启动输出含方向 | `alterego serve` | 渠道列表中标注方向 |
| 文档四处一致 | 人工检查 | README / DESIGN.md / 05-channels.md / 启动输出 口径一致 |

**关键测试**：

```python
def test_unidirectional_channel_rejects_inbound():
    """单向渠道必须明确拒绝入站，不能静默忽略"""
    ch = DingtalkWebhookChannel(config=FakeConfig())
    assert ch.direction == frozenset({"out"})
    with pytest.raises(NotImplementedError):
        asyncio.run(ch.on_receive(make_inbound("你好")))

def test_primary_channel_is_bidirectional():
    """主渠道必须是双向的"""
    router = ChannelRouter(channels=[dingtalk, web])
    assert router.primary.direction >= {"in"}
    assert router.primary.name == "web"

def test_doctor_shows_direction(capsys):
    doctor()
    out = capsys.readouterr().out
    assert "仅出站" in out
    assert "回复不会被接收" in out
```

**人工验收（M5 的一部分）**：让一个没读过文档的人配置钉钉渠道，观察他是否会尝试在钉钉里回复。如果会，说明提示文案不够醒目。

---

## 备注

**这个决策的本质是"接受一半的功能，换取十分之一的门槛"。**

完整的双向体验需要：企业主体 + 公网服务器 + 域名备案 + 加解密实现 + 审核。这套要求面向的是"企业服务"，而 AlterEgo 是一个**本地单人的开源工具**。用企业级门槛换取"能在同一个 App 里回复"，性价比太低。

而且单向出站已经能满足核心体验："**它有自己的生活，并且能主动找到你**"。回复路径换成打开浏览器，损失的体验有限。

**推迟而非取消。** v0.2.0 的 Telegram 是无需企业主体的双向方案；v0.3.0 的 `wecom_app` / `dingtalk_app` 留给有条件的用户。三条路径都在 `06-roadmap.md` 的插件路线图中。

**最需要持续关注的是预期管理。** 技术上的单向限制没法绕过，但可以通过文案让用户一早知道。四处提示（README / 启动输出 / channels doctor / Web 状态页）中，**启动输出最关键**——那是用户第一次看到渠道列表的地方。

参考：

- [企业微信群机器人文档](https://developer.work.weixin.qq.com/document/path/91770)（注意：只描述了发送，没有接收回调）
- [钉钉自定义机器人文档](https://open.dingtalk.com/document/orgapp/custom-robot-access)（同上）
- [企业微信接收消息与事件](https://developer.work.weixin.qq.com/document/path/90238)（需要"自建应用"，非群机器人）
- [`docs/design/05-channels.md`](../design/05-channels.md) —— 完整的渠道设计与 v2 规划

/* alterego 的 Web 界面。
 *
 * 四个约定，都是为了让这一页在「它今天怎么样了」这个场景下能直接用：
 *
 * 1. **不引任何前端框架、没有构建步骤。** 这一页要在用户自己机器上离线
 *    跑起来，一个需要 npm install 的界面等于在第一步就断了。
 * 2. **所有从数据库来的文本都过 esc()。** 内容是模型写的，里出现 <b>
 *    不算稀奇，而直接拼进 innerHTML 就是一次 XSS。这条没有例外。
 * 3. **SSE 连不上就退化成轮询。** 会话推送是唯一「实时」的需求；中间有
 *    代理、或者浏览器不支持 EventSource 时，10 秒问一次也够用，而页面
 *    不该因此白屏。
 * 4. **错误原样显示服务端那句话。** 服务端的 detail 里写着 what/hint，
 *    它们比「请求失败」有用得多。
 */

"use strict";

const state = {
  tab: "overview",
  messages: [],
  stream: null,
  pollTimer: null,
  retries: 0,
  health: null,
  authed: false,
  // 设置页里被用户收起的那些分组。`render()` 会整个重画面板，
  // 刷新一下、或者切到别的页签再回来，都算一次重画——不记下来的话，
  // 用户刚收起的分组会在他什么都没做的时候自己弹开。
  collapsed: new Set(),
};

const $ = (id) => document.getElementById(id);
const view = () => $("view");

/* ── 工具 ─────────────────────────────────────────────── */

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function attr(value) {
  return esc(value);
}

function when(iso) {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return esc(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ` +
         `${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

function card(inner) {
  return `<div class="card">${inner}</div>`;
}

function table(headers, rows) {
  if (!rows.length) return `<p class="muted">没有内容。</p>`;
  const head = headers.map((h) => `<th>${esc(h)}</th>`).join("");
  const body = rows
    .map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`)
    .join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function grouped(value) {
  // token 是七位数起步的，不分千位读不出数量级。它只影响显示，
  // 所以拿不到数字时把原值原样吐回去，而不是显示一个 NaN。
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString("zh-CN") : String(value ?? "");
}

/* ── 请求 ─────────────────────────────────────────────── */

class ApiError extends Error {
  constructor(status, detail) {
    const what = detail && detail.what ? detail.what
      : detail && detail.message ? detail.message
      : `HTTP ${status}`;
    super(what);
    this.status = status;
    this.detail = detail || {};
  }
}

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });

  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    // 服务端偶尔会回一段非 JSON（比如一个未捕获的异常），这时把原文
    // 露出来比吞掉它有用——那句话里通常有 Python 的报错位置。
    payload = { detail: { what: text.slice(0, 400) } };
  }

  if (!response.ok) {
    const detail = payload && payload.detail !== undefined ? payload.detail : payload;
    if (response.status === 401) showGate(true);
    throw new ApiError(response.status, typeof detail === "string" ? { what: detail } : detail);
  }
  return payload;
}

function fail(error) {
  if (error instanceof ApiError && error.status === 401) return "";
  const hint = error.detail && error.detail.hint ? error.detail.hint : "";
  return card(
    `<p><span class="tag bad">没读到</span>${esc(error.message)}</p>` +
    (hint ? `<p class="muted">${esc(hint)}</p>` : "")
  );
}

/* ── 认证 ─────────────────────────────────────────────── */

function showGate(visible) {
  $("gate").hidden = !visible;
}

async function unlock(credential) {
  // 服务端用 Set-Cookie 落地，所以这里只需要发出这一次请求。
  await api("/api/auth", { method: "POST", body: { token: credential } });
  showGate(false);
  render();
}

function tokenFromUrl() {
  const token = new URLSearchParams(location.search).get("token");
  if (!token) return null;
  // 从地址栏里抹掉它：留在地址栏就会被进历史、被截图、被复制出去。
  history.replaceState(null, "", location.pathname);
  return token;
}

/* ── 各页 ─────────────────────────────────────────────── */

const views = {};

views.overview = async function () {
  const status = await api("/api/status");
  const budget = await api("/api/budget");
  const persona = status.persona || {};
  const emotion = status.emotion || {};
  const channel = status.channel || {};
  const usage = budget.usage || {};

  const head =
    `<div class="spread"><strong>${esc(persona.name || "还没有人设")}</strong>` +
    `<span class="muted">${esc(persona.occupation || "")}${persona.city ? " · " + esc(persona.city) : ""}</span></div>`;

  const emo = emotion.label
    ? `<p>心情 <strong>${esc(emotion.label)}</strong> ` +
      `<span class="muted">愉悦 ${esc(emotion.valence)} · 亢奋 ${esc(emotion.arousal)} · 疲惫 ${esc(emotion.fatigue)}</span></p>`
    : `<p class="muted">还没有情绪记录。</p>`;

  const channelLine =
    `<p>${channel.ok ? '<span class="tag ok">通</span>' : '<span class="tag bad">不通</span>'}` +
    `${esc(channel.id || "")} <span class="muted">${esc(channel.detail || "")}</span></p>` +
    (channel.hint ? `<p class="muted">${esc(channel.hint)}</p>` : "");

  const activity = (status.recent_activity || [])
    .map((item) => `<li>${esc(when(item.at))} · ${esc(item.description || item.intent)}</li>`)
    .join("");

  return (
    card(head + emo) +
    card(channelLine) +
    card(
      `<div class="spread"><span>未读</span><span class="num">${esc(status.unread)}</span></div>` +
      `<div class="spread"><span>今天发出去的消息</span><span class="num">${esc(usage.messages_sent)}</span></div>` +
      `<div class="spread"><span>今天发出去的动态</span><span class="num">${esc(usage.posts_sent)}</span></div>` +
      `<div class="spread"><span>被预算拦下的消息 / 动态</span><span class="num">${esc(usage.messages_suppressed)} / ${esc(usage.posts_suppressed)}</span></div>` +
      (usage.circuit_until
        ? `<p class="muted">它把自己静音到了 ${esc(when(usage.circuit_until))}——连续没人回它，这是代码硬约束，不是状态。</p>`
        : "")
    ) +
    `<h2>最近做的事</h2>` +
    (activity ? `<ul class="mono-list">${activity}</ul>` : `<p class="muted">还没有记录。</p>`)
  );
};

views.chat = async function () {
  const data = await api("/api/chat/history?limit=40");
  state.messages = data.messages || [];
  return (
    `<div id="chat-log">${chatLog()}</div>` +
    `<div class="composer">` +
    `<textarea id="say" rows="1" placeholder="跟它说句话…（Ctrl/⌘ + Enter 发送）"></textarea>` +
    `<button id="send" type="button">发送</button>` +
    `</div>` +
    `<p class="muted" id="chat-note">` +
    (state.stream ? "已接上推送。" : "推送没接上，退化成 10 秒问一次。") +
    `</p>`
  );
};

function chatLog() {
  if (!state.messages.length) return `<p class="muted">还没有聊过。</p>`;
  return state.messages.map(bubble).join("");
}

function bubble(message) {
  // 方向的原值是 inbound / outbound（消息表里的写法，与 /api/stats 数的
  // 是同一套），不是 Channel.direction 那个 in/out。两个都在项目里存在，
  // 在这里认错一个的后果是「自己说的话全显示成它说的」。
  const mine = message.direction === "inbound";
  const who = mine ? "你" : "它";
  // initiative 是布尔：这一句是不是它自己想说的（不是回你）。说明在 motivation 里。
  const why = message.motivation || message.trigger_note || "主动";
  const note = !mine && message.initiative ? ` <span class="tag">${esc(why)}</span>` : "";
  return (
    `<div class="bubble ${mine ? "me" : ""}">` +
    `<div class="who">${who}${note} · ${esc(when(message.at))}</div>` +
    `<div class="body">${esc(message.content)}</div>` +
    `</div>`
  );
}

views.feed = async function () {
  const data = await api("/api/feed?limit=30");
  const items = data.items || [];
  if (!items.length) return `<p class="muted">它还没发过动态。</p>`;
  return items
    .map((post) => {
      const mood = post.mood && post.mood.label
        ? `<span class="tag">${esc(post.mood.label)}</span>`
        : "";
      const place = post.location ? ` · ${esc(post.location)}` : "";
      return card(
        `<div class="muted">${esc(when(post.at))}${place} ${mood}</div>` +
        `<p>${esc(post.content)}</p>` +
        (post.motivation ? `<p class="muted">因为：${esc(post.motivation)}</p>` : "") +
        `<div class="row">` +
        `<button class="ghost" type="button" data-like="${attr(post.id)}">赞 ${esc(post.like_count)}</button>` +
        `<span class="muted">${esc(post.comment_count)} 条评论</span>` +
        `</div>`
      );
    })
    .join("");
};

views.timeline = async function () {
  const data = await api("/api/feed/timeline?days=3&limit=80");
  const items = data.items || [];
  if (!items.length) return `<p class="muted">这三天什么都没发生。</p>`;
  return (
    `<p class="muted">${esc(when(data.since))} 起，共 ${items.length} 条</p>` +
    `<ul class="mono-list">` +
    items
      .map((item) => {
        const text = item.kind === "post" ? item.content : (item.description || item.intent);
        const tag = item.kind === "post" ? "动态" : "行为";
        return `<li><span class="tag">${tag}</span> ${esc(when(item.at))}<br>${esc(text)}</li>`;
      })
      .join("") +
    `</ul>`
  );
};

views.thoughts = async function () {
  const data = await api("/api/thoughts?limit=60&days=7");
  const items = data.items || [];
  if (!items.length) return `<p class="muted">这一周没有留下内心独白。</p>`;
  return items
    .map((item) =>
      card(
        `<div class="muted">${esc(when(item.at))} · ${esc(item.category || "")}</div>` +
        `<p>${esc(item.inner_voice)}</p>` +
        `<p class="muted">做的是：${esc(item.description || item.intent)}</p>`
      )
    )
    .join("");
};

views.memory = async function () {
  const kind = new URLSearchParams(location.search).get("kind") || "";
  const query = kind ? `&kind=${encodeURIComponent(kind)}` : "";
  const data = await api(`/api/memory?limit=60${query}`);
  const items = data.items || [];
  const picker =
    `<select id="kind">` +
    ["", "episodic", "semantic", "emotional"]
      .map((value) => {
        const label = value === "" ? "全部" : value;
        return `<option value="${attr(value)}"${value === kind ? " selected" : ""}>${esc(label)}</option>`;
      })
      .join("") +
    `</select>`;
  if (!items.length) return picker + `<p class="muted">没有符合条件的记忆。</p>`;
  return (
    picker +
    items
      .map((memory) =>
        card(
          `<div class="muted">${esc(when(memory.at))} · ${esc(memory.kind)} · ` +
          `重要 ${esc(memory.importance)} · 强度 ${esc(memory.strength)}</div>` +
          `<p>${esc(memory.content)}</p>` +
          (memory.tags && memory.tags.length
            ? `<p>${memory.tags.map((tag) => `<span class="tag">${esc(tag)}</span>`).join("")}</p>`
            : "")
        )
      )
      .join("")
  );
};

/* token 消耗那一块的窗口与分组。

这两组取值是**白名单**，不是「随便传个值」。服务端那一边 ``days`` 有 1..365
的约束、``group`` 是三个字面量之一，传错都答 422；而 422 会让整页变成一张
「没读到」的卡片——用户只是动了一下地址栏，不该因此丢掉整页。所以先在这里
滤一遍，滤不掉的落回默认值；滤得掉但服务端还不认识的（比如以后新加的分组）
仍然由服务端说了算。 */
const TOKEN_DAYS = ["1", "7", "30"];
const TOKEN_GROUPS = ["purpose", "model", "day"];
const TOKEN_GROUP_LABELS = { purpose: "用途", model: "模型", day: "日期" };

views.stats = async function () {
  const params = new URLSearchParams(location.search);
  const days = TOKEN_DAYS.includes(params.get("days")) ? params.get("days") : TOKEN_DAYS[0];
  const group = TOKEN_GROUPS.includes(params.get("group")) ? params.get("group") : TOKEN_GROUPS[0];

  const data = await api("/api/stats?days=30");
  // token 那一块**单独 catch**：它答 503（没记账本）时，上面那六个数是从别的
  // 仓储读出来的、一个都没少。让整页一起变成「没读到」，等于把还能看的数
  // 一起扔掉——而这一页的用法是「一眼看它过得怎么样」，不是只看 token。
  const usage = await api(`/api/stats/tokens?days=${days}&group=${group}`).catch((error) => error);

  const kinds = Object.entries(data.memory_kinds || {})
    .map(([kind, count]) => `${esc(kind)} ${esc(count)}`)
    .join(" · ");
  return (
    card(`<p class="muted">窗口：最近 ${esc(data.window_days)} 天（${esc(when(data.since))} 起）</p>`) +
    card(table(["", "条数"], [
      ["动态", `<span class="num">${esc(data.posts)}</span>`],
      ["行为", `<span class="num">${esc(data.activities)}</span>`],
      ["你发出的消息", `<span class="num">${esc(data.messages_in)}</span>`],
      ["它发出的消息", `<span class="num">${esc(data.messages_out)}</span>`],
      ["新记忆", `<span class="num">${esc(data.memories)}</span>`],
      ["读到的东西", `<span class="num">${esc(data.sources)}</span>`],
    ])) +
    (kinds ? `<p class="muted">记忆分类：${kinds}</p>` : "") +
    (data.truncated
      ? `<p class="muted">这次统计只扫了前若干条记录，数字是下限而不是全量。</p>`
      : "") +
    `<p class="muted">这些都是窗口内的数，不是总量——仓储层没有 count()，` +
    `所以「一共多少」目前答不出来。</p>` +
    `<h2>token 消耗</h2>` +
    tokenBlock(usage, days, group)
  );
};

function tokenBlock(usage, days, group) {
  if (usage instanceof Error) return fail(usage);

  const pickers =
    `<select data-tokens="days">` +
    TOKEN_DAYS.map(
      (value) =>
        `<option value="${attr(value)}"${value === days ? " selected" : ""}>近 ${esc(value)} 天</option>`
    ).join("") +
    `</select>` +
    `<select data-tokens="group">` +
    TOKEN_GROUPS.map(
      (value) =>
        `<option value="${attr(value)}"${value === group ? " selected" : ""}>按${esc(TOKEN_GROUP_LABELS[value])}</option>`
    ).join("") +
    `</select>`;

  const totals = usage.totals || {};
  const rows = (usage.groups || []).map((item) => [
    // 模型名可能是空的：某次调用没定下模型就失败了。空着的那一栏要说明
    // 是空，而不是让一格空白看起来像渲染错位。
    item.key ? esc(item.key) : `<span class="muted">未记</span>`,
    `<span class="num">${esc(grouped(item.calls))}</span>`,
    `<span class="num">${esc(grouped(item.prompt_tokens))}</span>`,
    `<span class="num">${esc(grouped(item.completion_tokens))}</span>`,
    `<span class="num">${esc(grouped(item.total_tokens))}</span>`,
    item.failed ? `<span class="tag bad">${esc(item.failed)}</span>` : `<span class="muted">—</span>`,
  ]);
  if (rows.length) {
    rows.push([
      `<strong>合计</strong>`,
      `<span class="num">${esc(grouped(totals.calls))}</span>`,
      `<span class="num">${esc(grouped(totals.prompt_tokens))}</span>`,
      `<span class="num">${esc(grouped(totals.completion_tokens))}</span>`,
      `<span class="num">${esc(grouped(totals.total_tokens))}</span>`,
      `<span class="num">${esc(grouped(totals.failed))}</span>`,
    ]);
  }

  return card(
    `<p class="muted">账本里${esc(when(usage.since))}之后的每一次模型调用一行。</p>` +
    pickers +
    (rows.length
      ? table(
          [TOKEN_GROUP_LABELS[group], "次数", "输入", "输出", "合计", "失败"],
          rows
        )
      : `<p class="muted">这段时间没有调用记录。</p>`) +
    (usage.truncated
      ? `<p class="muted">分组太多，上面只列了前几组，合计仍按全部算。</p>`
      : "") +
    `<p class="muted">失败的调用也算在里面：重试三次才成的那一次，钱是照花的。` +
    `「失败」单独一列，所以「贵」和「一直在重试」能分开看。</p>` +
    `<p class="muted">金额：—（还没有价目表，账本里记的是 0；` +
    `把那个 0 画成「0 元」会让人以为它不花钱）。</p>`
  );
}

views.sources = async function () {
  const data = await api("/api/sources?limit=40");
  const items = data.items || [];
  if (!items.length) return `<p class="muted">最近 180 天没有读进什么东西。</p>`;
  return items
    .map((source) =>
      card(
        `<div class="muted">${esc(when(source.at))} · ${esc(source.kind || "")} ${esc(source.lang || "")}</div>` +
        `<p><a href="${attr(source.url)}" target="_blank" rel="noopener noreferrer">${esc(source.title || source.url)}</a></p>` +
        (source.summary ? `<p>${esc(source.summary)}</p>` : "")
      )
    )
    .join("");
};

views.plugins = async function () {
  const data = await api("/api/plugins");
  const items = data.items || [];
  const rows = items.map((plugin) => {
    const health = plugin.health
      ? plugin.health.ok
        ? `<span class="tag ok">正常</span>`
        : `<span class="tag bad">异常</span>`
      : `<span class="tag">未知</span>`;
    const state = plugin.loaded
      ? `<span class="tag ok">已加载</span>`
      : plugin.enabled
        ? `<span class="tag warn">已启用未加载</span>`
        : `<span class="tag">未启用</span>`;
    const detail = plugin.health && plugin.health.detail ? plugin.health.detail : "";
    const hint = plugin.health && plugin.health.hint ? `<br><span class="muted">${esc(plugin.health.hint)}</span>` : "";
    return [
      `${esc(plugin.name)}<br><span class="key">${esc(plugin.id)}</span>`,
      `${esc(plugin.kind)} ${esc(plugin.version)}`,
      state + (plugin.compatible ? "" : ` <span class="tag bad">API 版本不合</span>`),
      health + (detail ? `<br><span class="muted">${esc(detail)}</span>` : "") + hint,
      plugin.loaded
        ? `<button class="ghost" type="button" data-reload="${attr(plugin.id)}">重载</button>`
        : "",
    ];
  });

  const failed = (data.failed || [])
    .map((item) => `<li>${esc(item.path)}<br><span class="muted">${esc(item.error)}</span></li>`)
    .join("");

  return (
    `<p class="muted">这些状态来自 serve 这个进程本身——它持有的就是插件真正在跑的那台管理器，` +
    `所以重载下一次 tick 就会生效。</p>` +
    (items.length ? table(["插件", "类型", "状态", "健康", ""], rows) : `<p class="muted">一个插件都没发现。</p>`) +
    (failed
      ? `<h2>清单读不出来</h2>` +
        `<p class="muted">这些目录里的 plugin.toml 没能读出来，所以它们不在上面的表里。</p>` +
        `<ul class="mono-list">${failed}</ul>`
      : "") +
    `<h2>它会在这些目录里找</h2>` +
    `<ul class="mono-list">${(data.search_paths || []).map((p) => `<li><code>${esc(p)}</code></li>`).join("")}</ul>`
  );
};

views.settings = async function () {
  const [schema, current, providers] = await Promise.all([
    api("/api/settings/schema"),
    api("/api/settings"),
    api("/api/settings/providers"),
  ]);
  const values = current.values || {};
  const groups = new Map();
  for (const setting of schema.settings) {
    const group = setting.group || "其他";
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(setting);
  }

  // 一段表里的键，有多少已经被单独列成设置了。用来把「这一项是一整段表，请直接改
  // 配置文件」这句话换掉——[llm.routing] 的九行本来就在下面各自成行，还这么说
  // 就是让用户去改一个其实不用改的文件。
  const keys = new Set(schema.settings.map((setting) => setting.key));
  const ctx = {
    providers,
    expanded: (key) => [...keys].some((other) => other.startsWith(`${key}.`)),
  };

  const head =
    `<p class="muted">配置文件：${current.source ? `<code>${esc(current.source)}</code>` : "内置默认值（还没跑过 alterego init，这一页改不了任何东西）"}</p>`;

  // 分组用原生 `<details>` 收起：浏览器自带展开/收起、键盘可操作、
  // 不写一行事件代码，也不影响「没有 JS 时至少看得见内容」。
  // 默认展开——把 96 项藏在 12 次点击后面，对「只想抄一下某个键名」的人
  // 是净损失；这里要的是「能收起」，不是「默认收起」。
  const sections = [...groups.entries()]
    .map(([group, settings]) => {
      const rows = settings.map((setting) => settingRow(setting, values[setting.key], ctx));
      const open = state.collapsed.has(group) ? "" : " open";
      return (
        `<details class="group" data-group="${attr(group)}"${open}>` +
        `<summary>${esc(group)}<span class="count">${settings.length} 项</span></summary>` +
        `<div class="card">${rows.join("")}</div></details>`
      );
    })
    .join("");

  const unknown = (current.unannotated || []).length
    ? `<h2>未标注</h2><p class="muted">这些键在配置文件里，但内核不认识它们——写它们的多半是某个插件。它们没有被忽略，只是没有说明可显示。</p>` +
      `<ul class="mono-list">${current.unannotated
        .map((item) => `<li><code>${esc(item.key)}</code> = ${esc(JSON.stringify(item.value))}</li>`)
        .join("")}</ul>`
    : "";

  return head + sections + unknown;
};

function settingRow(setting, value, ctx) {
  const key = attr(setting.key);
  const secret = setting.kind === "secret";
  const mapping = setting.kind === "mapping";
  const set = secret && value && value.set;

  let control;
  if (secret) {
    control = `<span class="tag ${set ? "ok" : ""}">${set ? "已设置" : "未设置"}</span>` +
      `<span class="muted">密钥不从这里改——把它写成 \${环境变量名}，改环境变量。</span>`;
  } else if (ctx && ctx.providers && setting.key === ctx.providers.key) {
    // 这一段的内层键由 provider 的插件解释（P4），没有 schema 可查，所以它走一套
    // 自己的读写口：类型从文件里现有的那一行猜，猜不出来的老实显示成灰字。
    control = providerTable(ctx.providers);
  } else if (mapping) {
    control = ctx && ctx.expanded(setting.key)
      ? `<span class="muted">这一节的每一行在下面单独列出了，改那几行就行。</span>`
      : `<span class="muted">这一项是一整段表，请直接改配置文件。</span>`;
  } else if (setting.kind === "enum") {
    const options = (setting.choices || [])
      .map((choice) => {
        const chosen = String(value) === String(choice.value) ? " selected" : "";
        const title = choice.consequence ? ` title="${attr(choice.consequence)}"` : "";
        return `<option value="${attr(choice.value)}"${chosen}${title}>${esc(choice.label)}</option>`;
      })
      .join("");
    control = `<select data-key="${key}" data-kind="enum">${options}</select>`;
  } else if (setting.kind === "bool") {
    const chosen = value === true ? " checked" : "";
    control = `<input type="checkbox" data-key="${key}" data-kind="bool"${chosen}>`;
  } else if (setting.kind === "int" || setting.kind === "float") {
    const step = setting.kind === "float" ? "0.1" : "1";
    control = `<input type="number" step="${step}" data-key="${key}" data-kind="${attr(setting.kind)}" value="${attr(value ?? "")}">`;
  } else if (setting.kind === "list") {
    const text = Array.isArray(value) ? value.join(", ") : (value ?? "");
    control = `<input type="text" data-key="${key}" data-kind="list" value="${attr(text)}" placeholder="逗号分隔">`;
  } else {
    control = `<input type="text" data-key="${key}" data-kind="${attr(setting.kind)}" value="${attr(value ?? "")}">`;
  }

  const meta = [
    setting.requires_restart ? `<span class="tag warn">要重启</span>` : "",
    setting.danger ? `<span class="tag bad">危险</span>` : "",
    setting.advanced ? `<span class="tag">进阶</span>` : "",
    setting.minimum !== null && setting.minimum !== undefined
      ? `<span class="tag">${esc(setting.minimum)} ~ ${esc(setting.maximum ?? "∞")}${esc(setting.unit || "")}</span>`
      : "",
    setting.depends_on ? `<span class="tag">依赖 ${esc(setting.depends_on)}</span>` : "",
  ].join("");

  const disabled = secret || mapping;
  const editor = Boolean(ctx && ctx.providers && setting.key === ctx.providers.key);
  const ctl = editor
    ? control
    : `<div class="ctl">${control}` +
      (disabled ? "" : `<button type="button" data-save="${key}">保存</button>`) +
      `</div>`;
  return (
    `<div class="setting">` +
    `<div class="spread"><span class="name">${esc(setting.label)}</span><span class="key">${esc(setting.key)}</span></div>` +
    (setting.description ? `<div class="effect">${esc(setting.description)}</div>` : "") +
    (setting.effect ? `<div class="effect">改了会怎样：${esc(setting.effect)}</div>` : "") +
    (meta ? `<div>${meta}</div>` : "") +
    ctl +
    `</div>`
  );
}

/* ── 模型端点（[llm.providers.*]）──────────────────────────
 *
 * 这一节和别处不一样：内层键由那个 provider 的插件解释（P4），内核没有它们的
 * schema，所以类型（字符串/数字/开关/数组）是从**文件里现有的那一行**猜出来的；
 * 猜不出来（嵌套的表）就老实显示成灰字，而不是画一个文本框——画了文本框，用户
 * 改完保存，写进去的是一个字符串，然后 provider 在运行时报一个和「刚才改的那
 * 一下」看起来毫无关系的错。
 *
 * 字段名同理：补全列表里是**你自己配置里出现过的**字段名，不是一份内核声称
 * 「provider 该有哪些键」的清单——那种清单一定会和插件漂移，然后页面开始教用户
 * 写一个插件根本不读的键。
 *
 * 名字看起来是密钥的那几行（api_key / token / secret）只显示「设了没有」，值一个
 * 字都不回。provider 插件的正常用法就是 api_key_env 指向一个环境变量，而万一有人
 * 把密钥直接写在文件里，这一页也不该把它送到浏览器上。
 *
 * 这里没有删除：删掉一个还被 [llm.routing] 指着的端点，下次 serve 直接起不来，
 * 而文本补丁删行删段没法回滚（只留一份 .bak，连着撤两步就撤不回去了）。
 * 要删就在文件里删，那里顺手改 [llm.routing]，本来也是同一件事。
 */

function providerTable(data) {
  const cards = (data.providers || []).map((item) => providerCard(item, data.writable)).join("");
  const empty = (data.providers || []).length
    ? ""
    : `<p class="muted">配置文件里一个端点都没有。</p>`;
  const known = (data.known_fields || [])
    .map((name) => `<option value="${attr(name)}"></option>`)
    .join("");
  return (
    `<div class="providers" data-providers>` +
    `<p class="muted">每个端点对应配置里的一段 <code>[llm.providers.名字]</code>。` +
    `<code>api_key_env</code> 填的是环境变量的名字，密钥本身不会进这个页面。</p>` +
    empty +
    cards +
    (data.writable ? providerNew() : "") +
    `<datalist id="provider-fields">${known}</datalist>` +
    `</div>`
  );
}

function providerCard(item, writable) {
  const head =
    `<div class="spread"><span class="name">${esc(item.name)}</span>` +
    `<span class="key">[llm.providers.${esc(item.name)}]</span></div>`;
  if (!item.editable) {
    // 值不是一张表（手写的 ``providers.x = "..."``）。照样画出来，否则用户会以为
    // 页面把他的配置弄丢了。
    return (
      `<div class="provider">` + head +
      `<div class="effect">这一行不是一张表，改不了它：<code>${esc(JSON.stringify(item.value))}</code></div>` +
      `</div>`
    );
  }
  const rows = (item.fields || []).map(providerField).join("");
  const save = writable
    ? `<div class="ctl"><button type="button" data-provider-save="${attr(item.name)}">保存这个端点</button></div>`
    : "";
  return `<div class="provider" data-provider="${attr(item.name)}">` + head + rows + save + `</div>`;
}

function providerField(field) {
  const label = `<span class="label">${esc(field.name)}</span>`;
  if (field.kind === "secret") {
    // 名字里带 api_key / token / secret 的行只回「设了没有」。
    // 回显它就是把这个密钥送到浏览器上，和 /api/settings 的规矩对不上。
    const state = field.value && field.value.set ? "已设置" : "没有设置";
    return (
      `<div class="field">` + label +
      `<span class="muted">${state}，值不经过这个页面 · ` +
      `密钥放环境变量，文件里只写变量名（通常是 <code>api_key_env</code>）</span>` +
      `</div>`
    );
  }
  if (!field.editable) {
    return (
      `<div class="field">` + label +
      `<span class="muted">这一行是嵌套的表，请在文件里改：<code>${esc(JSON.stringify(field.value))}</code></span>` +
      `</div>`
    );
  }
  const text = Array.isArray(field.value) ? field.value.join(", ") : (field.value ?? "");
  return (
    `<div class="field">` + label +
    `<input type="text" data-field="${attr(field.name)}" value="${attr(text)}" spellcheck="false">` +
    `</div>`
  );
}

function providerNew() {
  return (
    `<details class="provider new" data-provider-new>` +
    `<summary>加一个端点</summary>` +
    `<div class="field"><span class="label">名字</span>` +
    `<input type="text" data-new-name placeholder="deepseek" spellcheck="false"></div>` +
    `<div data-new-rows>${providerNewRow()}</div>` +
    `<div class="ctl"><button type="button" data-add-row>再加一行</button>` +
    `<button type="button" data-provider-add>创建</button></div>` +
    `</details>`
  );
}

function providerNewRow() {
  return (
    `<div class="field" data-new-row>` +
    `<input type="text" data-new-key list="provider-fields" placeholder="字段名" spellcheck="false">` +
    `<input type="text" data-new-value placeholder="值" spellcheck="false">` +
    `</div>`
  );
}

/* ── 渲染与事件 ───────────────────────────────────────── */

async function render() {
  const panel = view();
  panel.innerHTML = `<p class="muted">正在载入…</p>`;
  try {
    panel.innerHTML = await views[state.tab]();
  } catch (error) {
    panel.innerHTML = fail(error);
  }
  if (state.tab === "chat") {
    hookChat();
    connectStream();
    for (const box of panel.querySelectorAll("textarea")) {
      box.focus();
    }
  }
  if (state.tab === "plugins") hookPlugins();
  if (state.tab === "feed") hookFeed();
  if (state.tab === "settings") hookSettings();
  if (state.tab === "memory") hookKind();
  if (state.tab === "stats") hookStats();
}

function hookFeed() {
  for (const button of view().querySelectorAll("[data-like]")) {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await api(`/api/feed/${encodeURIComponent(button.dataset.like)}/like`, { method: "POST" });
        await render();
      } catch (error) {
        button.disabled = false;
        button.title = error.message;
      }
    });
  }
}

function hookPlugins() {
  for (const button of view().querySelectorAll("[data-reload]")) {
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "重载中…";
      try {
        const result = await api(`/api/plugins/${encodeURIComponent(button.dataset.reload)}/reload`, {
          method: "POST",
        });
        button.textContent = result.ok ? "成功" : "失败";
      } catch (error) {
        button.textContent = "失败";
        button.title = error.message;
      }
      await render();
    });
  }
}

function hookKind() {
  const picker = $("kind");
  if (!picker) return;
  picker.addEventListener("change", () => {
    const query = picker.value ? `?kind=${encodeURIComponent(picker.value)}` : "";
    history.replaceState(null, "", location.pathname + query);
    render();
  });
}

function hookStats() {
  // 两个选择器都只做一件事：把选中的值写进地址栏，然后重画。
  // 写进地址栏而不是只存在内存里，是因为刷新一下就该还是刚才看的那一屏——
  // 「按模型看」看到一半按 F5 弹回默认值，会让人以为刚才那个是假的。
  for (const picker of view().querySelectorAll("[data-tokens]")) {
    picker.addEventListener("change", () => {
      const params = new URLSearchParams(location.search);
      params.set(picker.dataset.tokens, picker.value);
      history.replaceState(null, "", `${location.pathname}?${params.toString()}`);
      render();
    });
  }
}

function hookSettings() {
  for (const group of view().querySelectorAll("details.group")) {
    group.addEventListener("toggle", () => {
      group.open ? state.collapsed.delete(group.dataset.group) : state.collapsed.add(group.dataset.group);
    });
  }
  for (const button of view().querySelectorAll("[data-save]")) {
    button.addEventListener("click", async () => {
      const key = button.dataset.save;
      const field = view().querySelector(`[data-key="${CSS.escape(key)}"]`);
      if (!field) return;
      const value = field.type === "checkbox" ? (field.checked ? "true" : "false") : field.value;
      const before = button.textContent;
      button.disabled = true;
      button.textContent = "保存中…";
      try {
        const result = await api("/api/settings", { method: "POST", body: { key, value } });
        button.textContent = "已保存";
        if (result.requires_restart && (result.restart_needed || []).length) {
          button.textContent = "已保存 · 待重启";
          button.title = "这一项要重启 alterego serve 才会生效。";
        }
      } catch (error) {
        button.textContent = before;
        button.title = error.message + (error.detail && error.detail.hint ? `\n${error.detail.hint}` : "");
        alert(`${error.message}${error.detail && error.detail.hint ? "\n\n" + error.detail.hint : ""}`);
      } finally {
        button.disabled = false;
      }
    });
  }
  hookProviders();
}

function hookProviders() {
  const table = view().querySelector("[data-providers]");
  if (!table) return;
  for (const button of table.querySelectorAll("[data-add-row]")) {
    button.addEventListener("click", () => {
      const rows = button.closest("[data-provider-new]").querySelector("[data-new-rows]");
      rows.insertAdjacentHTML("beforeend", providerNewRow());
      const added = rows.lastElementChild.querySelector("input");
      if (added) added.focus();
    });
  }
  for (const button of table.querySelectorAll("[data-provider-add]")) {
    button.addEventListener("click", () => postProvider(button, newProviderBody(button), "创建中…"));
  }
  for (const button of table.querySelectorAll("[data-provider-save]")) {
    button.addEventListener("click", () => postProvider(button, cardBody(button), "保存中…"));
  }
}

/** 一张已存在的端点卡片：表单里每一行都写回去。 */
function cardBody(button) {
  const fields = {};
  for (const input of button.closest(".provider").querySelectorAll("[data-field]")) {
    fields[input.dataset.field] = input.value;
  }
  return { name: button.dataset.providerSave, fields };
}

/** 「加一个端点」表单：键名空着的行当它不存在。 */
function newProviderBody(button) {
  const form = button.closest("[data-provider-new]");
  const fields = {};
  for (const row of form.querySelectorAll("[data-new-row]")) {
    // 键名都没填的行是「点了一下『再加一行』但没用上」，不是「我要写一个叫空字符串
    // 的字段」。键名填了而值是空的，就是**明确地**写一个空字符串——api_key_env
    // 为空本来就是「这个端点不校验密钥」的意思。
    const name = row.querySelector("[data-new-key]").value.trim();
    if (name) fields[name] = row.querySelector("[data-new-value]").value;
  }
  return { name: form.querySelector("[data-new-name]").value.trim(), fields };
}

async function postProvider(button, body, busy) {
  const before = button.textContent;
  button.disabled = true;
  button.textContent = busy;
  try {
    const result = await api("/api/settings/providers", { method: "POST", body });
    if ((result.added || []).length) {
      // 新加的行值得单独说一句：这一段里的键由 provider 自己解释，名字拼错了
      // 不会有任何提示，它只是读不到那一行。
      alert(
        `已写进 ${result.source}。\n\n` +
          `新加的行：${result.added.join("、")}\n` +
          `这一节里的键由 provider 自己解释，名字写错不会有提示——它只是读不到。`,
      );
    }
    button.textContent = (result.restart_needed || []).length ? "已保存 · 待重启" : "已保存";
    await render();
  } catch (error) {
    button.textContent = before;
    button.disabled = false;
    alert(`${error.message}${error.detail && error.detail.hint ? `\n\n${error.detail.hint}` : ""}`);
  }
}

function hookChat() {
  const box = $("say");
  if (!box) return;
  box.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      say();
    }
  });
  $("send").addEventListener("click", say);
}

async function say() {
  const box = $("say");
  const text = box.value.trim();
  if (!text) return;
  box.disabled = true;
  try {
    const result = await api("/api/chat/send", { method: "POST", body: { text } });
    box.value = "";
    // 只信服务端重画的那一份。它这一轮说的话在响应回来之前就已经落库了，
    // refreshChat 拉回来的历史里就有它——再 push 一次会让同一句话出现两遍。
    // result 唯一的用处是「它为什么没说」，那是历史里没有的信息。
    await refreshChat();
    explain(result);
  } catch (error) {
    alert(error.message + (error.detail && error.detail.hint ? `\n\n${error.detail.hint}` : ""));
  } finally {
    box.disabled = false;
    box.focus();
  }
}

function explain(result) {
  const line = $("chat-note");
  if (!line) return;
  if (result.reply) {
    line.textContent = state.stream ? "已接上推送。" : "推送没接上，退化成 10 秒问一次。";
    return;
  }
  // 「它决定不说」与「它说失败了」在历史里长得一模一样（都只有你那一句），
  // 区别只在 reason 与 notes 里。不显示出来的话，人只能猜它是不是坏了。
  const why = result.decision && result.decision.reason ? `（${result.decision.reason}）` : "";
  const notes = (result.notes || []).join("；");
  line.textContent = `这一轮它没回${why}${notes ? `：${notes}` : ""}`;
}

async function refreshChat() {
  const data = await api("/api/chat/history?limit=40");
  state.messages = data.messages || [];
  const log = $("chat-log");
  if (!log) return;
  log.innerHTML = chatLog();
  log.lastElementChild?.scrollIntoView({ block: "end" });
}

function connectStream() {
  if (state.stream) return;
  if (!("EventSource" in window)) {
    startPolling();
    return;
  }
  const stream = new EventSource("/api/chat/stream");
  stream.addEventListener("message", (event) => {
    state.retries = 0;
    stopPolling();
    let payload = null;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return;
    }
    state.messages.push(payload);
    const log = $("chat-log");
    if (log) {
      log.insertAdjacentHTML("beforeend", bubble(payload));
      log.lastElementChild.scrollIntoView({ block: "end" });
    }
  });
  stream.addEventListener("open", () => {
    state.retries = 0;
    stopPolling();
  });
  stream.addEventListener("error", () => {
    // EventSource 自己会重连。连不上太多次就当作这条路走不通，
    // 改用轮询——中间有代理时 SSE 常常是被无声掐断的。
    state.retries += 1;
    if (state.retries >= 3) {
      stream.close();
      state.stream = null;
      startPolling();
    }
  });
  state.stream = stream;
}

function startPolling() {
  if (state.pollTimer || state.tab !== "chat") return;
  const note = $("chat-note");
  if (note) note.textContent = "推送没接上，每 10 秒问一次。";
  state.pollTimer = setInterval(() => {
    refreshChat().catch(() => {});
  }, 10000);
}

function stopPolling() {
  if (!state.pollTimer) return;
  clearInterval(state.pollTimer);
  state.pollTimer = null;
  const note = $("chat-note");
  if (note) note.textContent = "已接上推送。";
}

/* ── 启动 ─────────────────────────────────────────────── */

async function refreshHealth() {
  const pill = $("pill");
  try {
    const health = await api("/api/health");
    state.health = health;
    const channel = health.channel || {};
    pill.textContent = channel.ok ? "在跑" : "渠道不通";
    pill.className = `pill ${channel.ok ? "ok" : "bad"}`;
    pill.title = `${channel.id || ""} ${channel.detail || ""} ${channel.readers ?? ""} 个连接`;
    $("where").textContent = health.persona_id ? `人设 ${health.persona_id}` : "还没有人设";
    // 认证开着、而这一次又没带对凭据——那就把门摆出来。
    const required = Boolean(health.auth && health.auth.required);
    showGate(required && !state.authed);
  } catch (error) {
    pill.textContent = "连不上";
    pill.className = "pill bad";
    pill.title = error.message;
  }
}

function select(tab) {
  state.tab = tab;
  for (const button of document.querySelectorAll("#tabs button")) {
    button.classList.toggle("on", button.dataset.tab === tab);
  }
  if (tab !== "chat") stopPolling();
  render();
}

async function boot() {
  for (const button of document.querySelectorAll("#tabs button")) {
    button.addEventListener("click", () => select(button.dataset.tab));
  }
  $("refresh").addEventListener("click", () => {
    refreshHealth();
    render();
  });
  $("pill").addEventListener("click", refreshHealth);
  $("gate-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const credential = $("gate-token").value.trim();
    if (!credential) return;
    try {
      state.authed = true;
      await unlock(credential);
    } catch (error) {
      state.authed = false;
      $("gate-token").value = "";
      alert(error.message);
    }
  });

  const token = tokenFromUrl();
  if (token) {
    try {
      state.authed = true;
      await unlock(token);
    } catch {
      state.authed = false;
      showGate(true);
    }
  }

  await refreshHealth();
  await render();
  setInterval(refreshHealth, 30000);
}

boot();

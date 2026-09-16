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

views.stats = async function () {
  const data = await api("/api/stats?days=30");
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
    `所以「一共多少」目前答不出来。</p>`
  );
};

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
  const [schema, current] = await Promise.all([api("/api/settings/schema"), api("/api/settings")]);
  const values = current.values || {};
  const groups = new Map();
  for (const setting of schema.settings) {
    const group = setting.group || "其他";
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(setting);
  }

  const head =
    `<p class="muted">配置文件：${current.source ? `<code>${esc(current.source)}</code>` : "内置默认值（还没跑过 alterego init，这一页改不了任何东西）"}</p>`;

  const sections = [...groups.entries()]
    .map(([group, settings]) => {
      const rows = settings.map((setting) => settingRow(setting, values[setting.key]));
      return `<h2>${esc(group)}</h2><div class="card">${rows.join("")}</div>`;
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

function settingRow(setting, value) {
  const key = attr(setting.key);
  const secret = setting.kind === "secret";
  const mapping = setting.kind === "mapping";
  const set = secret && value && value.set;

  let control;
  if (secret) {
    control = `<span class="tag ${set ? "ok" : ""}">${set ? "已设置" : "未设置"}</span>` +
      `<span class="muted">密钥不从这里改——把它写成 \${环境变量名}，改环境变量。</span>`;
  } else if (mapping) {
    control = `<span class="muted">这一项是一整段表，请直接改配置文件。</span>`;
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
  return (
    `<div class="setting">` +
    `<div class="spread"><span class="name">${esc(setting.label)}</span><span class="key">${esc(setting.key)}</span></div>` +
    (setting.description ? `<div class="effect">${esc(setting.description)}</div>` : "") +
    (setting.effect ? `<div class="effect">改了会怎样：${esc(setting.effect)}</div>` : "") +
    (meta ? `<div>${meta}</div>` : "") +
    `<div class="ctl">${control}` +
    (disabled ? "" : `<button type="button" data-save="${key}">保存</button>`) +
    `</div></div>`
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

function hookSettings() {
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

# 03 · 数据模型与存储

> 上级文档：[DESIGN.md](../DESIGN.md) · 版本 v0.1.0 · schema_version = 1
> 本文档描述完整的 ER 模型、DDL、索引策略、检索算法与迁移机制。

---

## 目录

1. [设计决策](#1-设计决策)
2. [ER 总览](#2-er-总览)
3. [完整 DDL](#3-完整-ddl)
4. [表字段说明](#4-表字段说明)
5. [索引策略](#5-索引策略)
6. [记忆检索算法](#6-记忆检索算法)
7. [Repository 接口](#7-repository-接口)
8. [迁移机制](#8-迁移机制)
9. [备份与导出](#9-备份与导出)
10. [容量估算](#10-容量估算)

---

## 1. 设计决策

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 数据库 | SQLite 3.35+ | 零运维、单文件、Python 内置驱动、支持 FTS5 |
| 日志模式 | WAL | 支持 Web 线程与 Tick 线程并发读写 |
| 复杂结构 | JSON 字符串列 | 人格、世界设定是深度嵌套结构，拆表得不偿失；SQLite 有 `json_extract` 可查询 |
| 时间存储 | ISO 8601 TEXT | 可读、可排序、SQLite 日期函数直接支持；统一存 **UTC+08:00 带时区** |
| 主键 | TEXT (uuid4 hex) | 便于导出合并；避免 AUTOINCREMENT 的跨库冲突 |
| 字段命名 | snake_case | SQL 惯例 |
| 布尔 | INTEGER 0/1 | SQLite 无原生布尔 |
| 数组 | JSON 字符串 | 同上 |
| 向量 | ❌ v1 不支持 | 零依赖优先；作为插件额外建表 |

### 1.1 时间表示规范

所有时间字段统一为 **ISO 8601 带时区偏移** 的字符串：

```
2026-09-15T14:32:11+08:00
```

**为什么带时区**：Agent 的作息依赖当地时间。存储时不带时区会在跨时区或夏令时场景下出错。

**索引友好性**：ISO 8601 的字典序与时间序一致，`ORDER BY occurred_at DESC` 可直接用索引。

### 1.2 JSON 列的使用规范

标记为 `JSON` 的列，内容为 JSON 对象/数组的字符串。约定：

- 始终是**对象或数组**，不是裸标量（`{}`/`[]` 而非 `null`）
- 空值用 `'{}'` / `'[]'` 而非 `NULL`（避免 `json.loads(NULL)` 报错）
- 提供 `CHECK (json_valid(列名))` 约束保证写入合法

---

## 2. ER 总览

```mermaid
erDiagram
    persona ||--o{ persona_version : "演化历史"
    persona ||--|| world : "共享 id"
    world ||--o{ npc : "包含"

    persona ||--o{ emotion_log : "情绪轨迹"
    persona ||--o{ memory : "记忆"
    memory ||--|| memory_fts : "全文索引"
    persona ||--o{ schedule_block : "日程"
    persona ||--o{ activity_log : "行为"
    persona ||--o{ social_post : "动态"
    social_post ||--o{ post_interaction : "互动"
    persona ||--o{ relationship : "关系状态"
    npc ||--o| relationship : "对应关系"

    persona ||--o{ conversation : "参与"
    conversation ||--o{ message : "包含"

    persona ||--o{ tick_log : "推演记录"
    persona ||--o{ llm_usage : "模型消耗"
    plugin_state }o--|| plugin_meta : "属于"
    persona ||--o{ event_log : "事件流"

    persona ||--o{ media_asset : "图片资产"
    persona ||--o{ media_usage : "生图消耗"
    media_asset ||--o| social_post : "被发布为"
    persona ||--o{ source_query : "检索"
    source_query ||--o{ source_item : "抓到"
    source_item ||--o| memory : "沉淀为"
    plugin_meta ||--o{ source_feed : "RSS 订阅"

    persona {
        text id PK
        text name
        text persona_json
        text created_at
    }
    memory {
        text id PK
        text persona_id FK
        text kind
        text content
        real importance
        real strength
        text occurred_at
    }
    social_post {
        text id PK
        text persona_id FK
        text content
        text posted_at
    }
    message {
        text id PK
        text conversation_id FK
        text direction
        text content
        text created_at
    }
```

---

## 3. 完整 DDL

> **表共 26 张**（21–26 为 v0.2.0/v0.3.0 新增，见 `migrations/002_media.sql`、`003_sources.sql`、`004_observability.sql`），
> 另有 2 个视图（`v_cost_daily` / `v_trace`）。
> `log_entry` 与 `schema_version` 不挂在 persona 上（前者是全局运行日志，后者是迁移元数据）。
> **每张可观测表都必须有 `correlation_id` 列**，同一次推演内共享同一个值——
> 这是「从一条错误跳回那次推演」的实现基础，见 [09-observability.md § 1.2](09-observability.md#12-核心洞察一切都要能串起来)。

```sql
-- ============================================================
-- AlterEgo 数据库 Schema
-- schema_version: 1（迁移 002/003 后为 3）
-- 约定：所有时间字段为 ISO 8601 带时区字符串
-- ============================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;

-- ────────────────────────────────────────────────────────────
-- 迁移版本
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    description TEXT NOT NULL
);

-- ────────────────────────────────────────────────────────────
-- 1. persona · 人格主表（当前生效版本）
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS persona (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    age             INTEGER,
    gender          TEXT,
    city            TEXT,
    occupation      TEXT,
    -- 完整人格数据（含性格/表达/偏好/背景/情绪基线）
    persona_json    TEXT NOT NULL CHECK (json_valid(persona_json)),
    -- 当前人格版本号，对应 persona_version.version
    current_version INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- ────────────────────────────────────────────────────────────
-- 2. persona_version · 人格版本历史（支持回滚与演化追踪）
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS persona_version (
    id          TEXT PRIMARY KEY,
    persona_id  TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    persona_json TEXT NOT NULL CHECK (json_valid(persona_json)),
    change_type TEXT NOT NULL,          -- init | manual_edit | llm_refine | evolution
    change_note TEXT,                   -- 变更说明（如"更毒舌一点"）
    diff_json   TEXT CHECK (diff_json IS NULL OR json_valid(diff_json)),
    created_at  TEXT NOT NULL,
    UNIQUE (persona_id, version)
);

CREATE INDEX IF NOT EXISTS idx_persona_version_pid
    ON persona_version(persona_id, version DESC);

-- ────────────────────────────────────────────────────────────
-- 3. world · 世界设定
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS world (
    id              TEXT PRIMARY KEY,
    persona_id      TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    setting         TEXT NOT NULL,      -- 时代/城市/行业/社会背景（长文本）
    locations_json  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(locations_json)),
    events_json     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(events_json)),
    weather_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(weather_json)),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_world_persona ON world(persona_id);

-- ────────────────────────────────────────────────────────────
-- 4. npc · NPC 档案（Agent 社交圈）
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS npc (
    id              TEXT PRIMARY KEY,
    world_id        TEXT NOT NULL REFERENCES world(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    relation        TEXT NOT NULL,      -- family | friend | close_friend | colleague | acquaintance | rival
    profile_json    TEXT NOT NULL CHECK (json_valid(profile_json)),
    -- 简化人格：性格标签、说话风格、口头禅
    emotion_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(emotion_json)),
    active          INTEGER NOT NULL DEFAULT 1,   -- 是否参与推演
    npc_tick_interval_minutes INTEGER NOT NULL DEFAULT 30,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_npc_world ON npc(world_id, active);

-- ────────────────────────────────────────────────────────────
-- 5. relationship · 关系状态
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS relationship (
    id                  TEXT PRIMARY KEY,
    persona_id          TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    target_id           TEXT NOT NULL,      -- 'user' 或 npc.id
    target_name         TEXT NOT NULL,
    target_kind         TEXT NOT NULL,      -- user | npc
    relation_type       TEXT NOT NULL,
    affinity            REAL NOT NULL DEFAULT 0,     -- -100 ~ 100
    familiarity         REAL NOT NULL DEFAULT 0,     -- 0 ~ 100
    trust               REAL NOT NULL DEFAULT 50,    -- 0 ~ 100
    tension             REAL NOT NULL DEFAULT 0,     -- 0 ~ 100
    notes               TEXT NOT NULL DEFAULT '',    -- LLM 生成的印象笔记
    interaction_count   INTEGER NOT NULL DEFAULT 0,
    last_contact_at     TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (persona_id, target_id)
);

CREATE INDEX IF NOT EXISTS idx_relationship_persona
    ON relationship(persona_id, affinity DESC);

-- ────────────────────────────────────────────────────────────
-- 6. emotion_log · 情绪时间序列
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS emotion_log (
    id          TEXT PRIMARY KEY,
    persona_id  TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    valence     REAL NOT NULL CHECK (valence >= -1.0 AND valence <= 1.0),
    arousal     REAL NOT NULL CHECK (arousal >= 0.0 AND arousal <= 1.0),
    fatigue     REAL NOT NULL DEFAULT 0 CHECK (fatigue >= 0.0 AND fatigue <= 1.0),
    label       TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',    -- 变化原因（可解释性）
    causes_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(causes_json)),
    tick_id     TEXT,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_emotion_persona_time
    ON emotion_log(persona_id, recorded_at DESC);

-- ────────────────────────────────────────────────────────────
-- 7. memory · 记忆
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory (
    id                  TEXT PRIMARY KEY,
    persona_id          TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,      -- episodic | semantic | emotional
    content             TEXT NOT NULL,      -- 完整内容
    summary             TEXT NOT NULL,      -- 一句话摘要，注入提示词用
    importance          REAL NOT NULL DEFAULT 0.5 CHECK (importance >= 0 AND importance <= 1),
    strength            REAL NOT NULL DEFAULT 1.0 CHECK (strength >= 0),
    valence             REAL NOT NULL DEFAULT 0 CHECK (valence >= -1 AND valence <= 1),
    entities_json       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(entities_json)),
    tags_json           TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags_json)),
    source              TEXT NOT NULL DEFAULT 'tick',   -- tick | conversation | consolidation | manual | npc
    source_ref          TEXT,               -- 来源 id（tick_id / message_id / memory_id）
    occurred_at         TEXT NOT NULL,
    last_recalled_at    TEXT,
    recall_count        INTEGER NOT NULL DEFAULT 0,
    forgotten           INTEGER NOT NULL DEFAULT 0,     -- 是否已标记遗忘（不删除，仅降权）
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_persona_time
    ON memory(persona_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_strength
    ON memory(persona_id, strength DESC) WHERE forgotten = 0;
CREATE INDEX IF NOT EXISTS idx_memory_kind
    ON memory(persona_id, kind, occurred_at DESC);

-- ────────────────────────────────────────────────────────────
-- 8. memory_fts · 记忆全文索引（FTS5）
-- ────────────────────────────────────────────────────────────
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    memory_id UNINDEXED,
    content,
    summary,
    tags,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- 保持 FTS 与主表同步的触发器
CREATE TRIGGER IF NOT EXISTS trg_memory_ai
AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(memory_id, content, summary, tags)
    VALUES (new.id, new.content, new.summary, new.tags_json);
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_au
AFTER UPDATE OF content, summary, tags_json ON memory BEGIN
    DELETE FROM memory_fts WHERE memory_id = old.id;
    INSERT INTO memory_fts(memory_id, content, summary, tags)
    VALUES (new.id, new.content, new.summary, new.tags_json);
END;

CREATE TRIGGER IF NOT EXISTS trg_memory_ad
AFTER DELETE ON memory BEGIN
    DELETE FROM memory_fts WHERE memory_id = old.id;
END;

-- ────────────────────────────────────────────────────────────
-- 9. conversation · 会话
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS conversation (
    id              TEXT PRIMARY KEY,
    persona_id      TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    counterpart_id  TEXT NOT NULL,          -- 'user' 或 npc.id
    counterpart_kind TEXT NOT NULL,         -- user | npc
    title           TEXT,
    message_count   INTEGER NOT NULL DEFAULT 0,
    last_message_at TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (persona_id, counterpart_id)
);

CREATE INDEX IF NOT EXISTS idx_conversation_persona
    ON conversation(persona_id, last_message_at DESC);

-- ────────────────────────────────────────────────────────────
-- 10. message · 消息
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS message (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
    direction       TEXT NOT NULL,          -- inbound（用户→Agent）| outbound（Agent→用户）
    sender_id       TEXT NOT NULL,          -- 'user' / persona.id / npc.id
    content         TEXT NOT NULL,
    content_type    TEXT NOT NULL DEFAULT 'text',  -- text | markdown | image
    -- 主动消息的动机（仅 outbound 且为主动发起时填写）
    initiative      INTEGER NOT NULL DEFAULT 0,    -- 是否主动发起（非回复）
    motivation      TEXT,                   -- share_something | miss_you | need_comfort | ...
    trigger_note    TEXT,                   -- 触发原因描述
    -- 分发状态
    delivered_channels_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(delivered_channels_json)),
    read_at         TEXT,
    replied_at      TEXT,
    tick_id         TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_message_conv_time
    ON message(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_message_unread
    ON message(conversation_id, direction, read_at) WHERE read_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_message_initiative
    ON message(direction, initiative, created_at DESC);

-- ────────────────────────────────────────────────────────────
-- 11. social_post · 动态（朋友圈）
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS social_post (
    id              TEXT PRIMARY KEY,
    persona_id      TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    content         TEXT NOT NULL,
    image_paths_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(image_paths_json)),
    location        TEXT,
    mood_label      TEXT,
    mood_valence    REAL,
    mood_arousal    REAL,
    -- 生成溯源
    intent_motivation TEXT,
    trigger_note    TEXT,
    activity_ref    TEXT,                   -- 关联的 activity_log.id
    -- 统计
    like_count      INTEGER NOT NULL DEFAULT 0,
    comment_count   INTEGER NOT NULL DEFAULT 0,
    visible         INTEGER NOT NULL DEFAULT 1,
    tick_id         TEXT,
    posted_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_post_persona_time
    ON social_post(persona_id, posted_at DESC);

-- ────────────────────────────────────────────────────────────
-- 12. post_interaction · 动态互动
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS post_interaction (
    id          TEXT PRIMARY KEY,
    post_id     TEXT NOT NULL REFERENCES social_post(id) ON DELETE CASCADE,
    actor_id    TEXT NOT NULL,          -- 'user' 或 npc.id
    actor_kind  TEXT NOT NULL,          -- user | npc
    actor_name  TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- like | comment
    content     TEXT,                   -- 评论内容
    -- 该互动对 Agent 产生的影响
    emotion_impact REAL NOT NULL DEFAULT 0,
    affinity_impact REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_post_interaction_post
    ON post_interaction(post_id, created_at);

-- ────────────────────────────────────────────────────────────
-- 13. schedule_block · 日程块
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schedule_block (
    id              TEXT PRIMARY KEY,
    persona_id      TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    day             TEXT NOT NULL,          -- YYYY-MM-DD（虚拟时间当地日期）
    start_at        TEXT NOT NULL,
    end_at          TEXT NOT NULL,
    activity        TEXT NOT NULL,
    category        TEXT NOT NULL,          -- sleep|work|meal|commute|leisure|social|chore|other
    location        TEXT,
    interruptible   INTEGER NOT NULL DEFAULT 1,
    source          TEXT NOT NULL DEFAULT 'template',  -- template|llm|manual
    actual_start_at TEXT,                   -- 实际执行时间（可能与计划不同）
    actual_end_at   TEXT,
    deviation_note  TEXT,                   -- 偏离原因
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_schedule_persona_day
    ON schedule_block(persona_id, day, start_at);

-- ────────────────────────────────────────────────────────────
-- 14. activity_log · 行为日志
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS activity_log (
    id              TEXT PRIMARY KEY,
    persona_id      TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    actor_kind      TEXT NOT NULL DEFAULT 'persona',  -- persona | npc
    actor_id        TEXT,
    intent          TEXT NOT NULL,
    category        TEXT NOT NULL,          -- internal | social | outbound
    description     TEXT NOT NULL,          -- 一句话描述做了什么
    detail_json     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
    location        TEXT,
    -- 是否产生对外内容
    outbound        INTEGER NOT NULL DEFAULT 0,
    outbound_ref    TEXT,                   -- post_id 或 message_id
    -- 预算拦截记录：意图被降级时，原始意图记在此
    suppressed_intent TEXT,
    suppress_reason TEXT,
    -- 内心独白（含被拦截的"想说但没说"）
    inner_voice     TEXT,
    duration_minutes INTEGER,
    tick_id         TEXT,
    started_at      TEXT NOT NULL,
    ended_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_activity_persona_time
    ON activity_log(persona_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_intent
    ON activity_log(persona_id, intent, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_suppressed
    ON activity_log(persona_id, started_at DESC) WHERE suppressed_intent IS NOT NULL;

-- ────────────────────────────────────────────────────────────
-- 15. tick_log · 推演日志（可解释性的核心）
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tick_log (
    id              TEXT PRIMARY KEY,       -- tick_id
    persona_id      TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    virtual_time    TEXT NOT NULL,
    real_duration_ms INTEGER NOT NULL,
    status          TEXT NOT NULL,          -- ok | partial | failed | interrupted | skipped
    -- 决策溯源
    state_snapshot_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(state_snapshot_json)),
    percepts_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(percepts_json)),
    candidates_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(candidates_json)),
    chosen_intent   TEXT,
    motivation      TEXT,
    trigger_note    TEXT,
    suppressed_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(suppressed_json)),
    memories_json   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(memories_json)),
    notes_json      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(notes_json)),
    stage_results_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(stage_results_json)),
    llm_calls       INTEGER NOT NULL DEFAULT 0,
    llm_tokens      INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tick_persona_time
    ON tick_log(persona_id, virtual_time DESC);
CREATE INDEX IF NOT EXISTS idx_tick_status
    ON tick_log(status, virtual_time DESC);

-- ────────────────────────────────────────────────────────────
-- 16. llm_usage · LLM 调用计量
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_usage (
    id                  TEXT PRIMARY KEY,
    persona_id          TEXT REFERENCES persona(id) ON DELETE SET NULL,
    tick_id             TEXT,
    purpose             TEXT NOT NULL,      -- decision|expression|reflection|emotion|memory|npc|persona|image_prompt|research_query|research_summarize
    tier                TEXT NOT NULL,      -- strong | cheap | custom
    provider_id         TEXT NOT NULL,
    model               TEXT NOT NULL,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    latency_ms          INTEGER NOT NULL DEFAULT 0,
    cost_usd            REAL NOT NULL DEFAULT 0,
    cache_hit           INTEGER NOT NULL DEFAULT 0,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    success             INTEGER NOT NULL DEFAULT 1,
    error               TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_time
    ON llm_usage(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_usage_purpose
    ON llm_usage(purpose, created_at DESC);

-- ────────────────────────────────────────────────────────────
-- 17. plugin_state · 插件 KV 状态
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS plugin_state (
    plugin_id   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL CHECK (json_valid(value)),
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (plugin_id, key)
);

-- ────────────────────────────────────────────────────────────
-- 18. plugin_meta · 插件元信息（用于 plugins doctor）
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS plugin_meta (
    plugin_id       TEXT PRIMARY KEY,
    version         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    source          TEXT NOT NULL,          -- local | entry_point
    source_path     TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_loaded_at  TEXT,
    last_error      TEXT,
    failure_count   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- ────────────────────────────────────────────────────────────
-- 19. event_log · 事件流（Web 回放与调试）
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS event_log (
    id              TEXT PRIMARY KEY,
    topic           TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    source          TEXT NOT NULL,
    correlation_id  TEXT,
    occurred_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_topic_time
    ON event_log(topic, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_correlation
    ON event_log(correlation_id);

-- 没有 tick_id 列，这是故意的：correlation_id 已经唯一标识一次推演（一个 tick 一个），
-- 再存一列 tick_id 等于把同一个值写两遍——两份就得同步，而同步就会漂移。
-- 代价是 v_trace 里这一支的 tick_id 只能是 NULL，见 § 3.3。

-- ────────────────────────────────────────────────────────────
-- 20. budget_usage · 打扰预算每日用量
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS budget_usage (
    persona_id          TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    day                 TEXT NOT NULL,      -- YYYY-MM-DD 虚拟时间当地日期
    messages_sent       INTEGER NOT NULL DEFAULT 0,
    posts_sent          INTEGER NOT NULL DEFAULT 0,
    messages_suppressed INTEGER NOT NULL DEFAULT 0,
    posts_suppressed    INTEGER NOT NULL DEFAULT 0,
    consecutive_no_reply INTEGER NOT NULL DEFAULT 0,
    circuit_until       TEXT,               -- 熔断截止时间（连续未回复触发）
    last_message_at     TEXT,
    last_post_at        TEXT,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (persona_id, day)
);

-- ────────────────────────────────────────────────────────────
-- 初始数据：真实迁移文件里**没有这段**——版本号由迁移器在同一个事务里写入。
-- 文件自己写就会出现「有的文件写了、有的忘了」，而忘掉的那次
-- 会留下「表建好了、版本号没涨」的状态。见 § 8.3。
```

> **本节（§ 3 及 § 3.1–§ 3.3）的 DDL 是设计参考，不是可执行文件。**
> 真实文件在 `src/alterego/storage/sqlite/migrations/`，与这里的差别只有三处，
> 全部是有意选择而非抄漏：**没有 `PRAGMA` 头、没有 `IF NOT EXISTS`、没有 `BEGIN`/`COMMIT`**。
> 原因见 § 8.3。

### 3.1 v0.2.0 新增：图片资产与生图计量

```sql
-- 迁移文件：migrations/002_media.sql

-- ────────────────────────────────────────────────────────
-- 21. media_asset · 图片资产
-- role='canonical' 的行是**角色形象一致性的唯一真源**（ADR-0008）
-- ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS media_asset (
    id                  TEXT PRIMARY KEY,
    persona_id          TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    -- canonical = 定妆照（全库唯一且仅一张）
    -- selfie    = 角色自拍（多为人物图，受一致性约束）
    -- scenery   = 风景/物品/抽象图（无人物，不要求参考图）
    -- user_upload = 用户手动放入相册的图
    role                TEXT NOT NULL CHECK (role IN ('canonical','selfie','scenery','user_upload')),
    file_path           TEXT NOT NULL,          -- 相对 data/media/ 的路径
    mime_type           TEXT NOT NULL DEFAULT 'image/png',
    width               INTEGER NOT NULL DEFAULT 0,
    height              INTEGER NOT NULL DEFAULT 0,
    bytes               INTEGER NOT NULL DEFAULT 0,
    -- 生成溯源：没有这几列，定妆照就无法复现，一致性链条当场断掉
    provider_id         TEXT,
    model               TEXT,
    prompt              TEXT,                   -- 实际发出的提示词（含骨架与四槽位展开后的完整文本）
    negative_prompt     TEXT,
    seed                INTEGER,
    reference_asset_id  TEXT REFERENCES media_asset(id) ON DELETE SET NULL,
    appearance_brief    TEXT,                   -- 仅 canonical：人工可编辑的外貌描述
    -- 四槽位原值，供重新生成同一构图时复用
    outfit              TEXT,
    scene               TEXT,
    mood                TEXT,
    lighting            TEXT,
    -- 可见性：未发布的图也必须可见（ADR-0008 的诚实要求）
    visibility          TEXT NOT NULL DEFAULT 'private'
                        CHECK (visibility IN ('private','posted','discarded')),
    cost_usd            REAL NOT NULL DEFAULT 0,
    tick_id             TEXT,
    correlation_id      TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- 定妆照全库唯一（部分唯一索引——只约束 role='canonical' 的行）
CREATE UNIQUE INDEX IF NOT EXISTS idx_media_canonical
    ON media_asset(persona_id) WHERE role = 'canonical';
CREATE INDEX IF NOT EXISTS idx_media_time ON media_asset(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_role ON media_asset(persona_id, role, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_visibility ON media_asset(visibility, created_at DESC);

-- ────────────────────────────────────────────────────────
-- 22. media_usage · 生图消耗明细
-- 为什么不塞进 llm_usage：那张表的语义是 **token 计量**，
-- 生图既无 prompt_tokens 也无 completion_tokens，混进去会让两边的聚合都变脏
-- ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS media_usage (
    id                  TEXT PRIMARY KEY,
    persona_id          TEXT REFERENCES persona(id) ON DELETE SET NULL,
    asset_id            TEXT REFERENCES media_asset(id) ON DELETE SET NULL,
    tick_id             TEXT,
    purpose             TEXT NOT NULL,          -- selfie|scenery|canonical|regenerate
    provider_id         TEXT NOT NULL,
    model               TEXT NOT NULL,
    image_count         INTEGER NOT NULL DEFAULT 1,
    width               INTEGER NOT NULL DEFAULT 0,
    height              INTEGER NOT NULL DEFAULT 0,
    used_reference      INTEGER NOT NULL DEFAULT 0,
    latency_ms          INTEGER NOT NULL DEFAULT 0,
    cost_usd            REAL NOT NULL DEFAULT 0,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    success             INTEGER NOT NULL DEFAULT 1,
    error               TEXT,
    correlation_id      TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_usage_time ON media_usage(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_usage_purpose ON media_usage(purpose, created_at DESC);
```

### 3.2 v0.3.0 新增：外部信息来源

```sql
-- 迁移文件：migrations/003_sources.sql

-- ────────────────────────────────────────────────────────
-- 23. source_item · 检索到的外部条目
-- 不存网页正文：版权、体积、检索质量（见 ADR-0009）
-- ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS source_item (
    id                  TEXT PRIMARY KEY,
    query_id            TEXT REFERENCES source_query(id) ON DELETE CASCADE,
    persona_id          TEXT REFERENCES persona(id) ON DELETE SET NULL,
    url                 TEXT NOT NULL,
    url_hash            TEXT NOT NULL,          -- normalize 后取 SHA-256 前 16 字节，用于去重
    title               TEXT NOT NULL DEFAULT '',
    summary             TEXT NOT NULL DEFAULT '',
    lang                TEXT,
    published_at        TEXT,
    fetched_at          TEXT NOT NULL,
    source_kind         TEXT NOT NULL DEFAULT 'search'   -- search|feed|direct
                        CHECK (source_kind IN ('search','feed','direct')),
    feed_id             TEXT REFERENCES source_feed(id) ON DELETE SET NULL,
    content_chars       INTEGER NOT NULL DEFAULT 0,
    dropped             INTEGER NOT NULL DEFAULT 0,
    dropped_reason      TEXT,                   -- injection|too_long|duplicate|blocked
    memory_id           TEXT REFERENCES memory(id) ON DELETE SET NULL,
    interest_delta      REAL NOT NULL DEFAULT 0,
    correlation_id      TEXT,
    created_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_item_hash ON source_item(url_hash);
CREATE INDEX IF NOT EXISTS idx_source_item_time ON source_item(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_item_dropped ON source_item(dropped, fetched_at DESC);

-- ────────────────────────────────────────────────────────
-- 24. source_feed · RSS 订阅源
-- ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS source_feed (
    id                  TEXT PRIMARY KEY,
    persona_id          TEXT REFERENCES persona(id) ON DELETE CASCADE,
    url                 TEXT NOT NULL,
    title               TEXT NOT NULL DEFAULT '',
    category            TEXT,
    etag                TEXT,                   -- 304 时不消耗流量也不消耗 LLM
    last_modified       TEXT,
    last_checked_at     TEXT,
    last_success_at     TEXT,
    failure_count       INTEGER NOT NULL DEFAULT 0,
    enabled             INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_feed_url ON source_feed(persona_id, url);

-- ────────────────────────────────────────────────────────
-- 25. source_query · 一次检索
-- 存 emotion_label 是为了能按情绪回顾「它最近在关心什么」
-- ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS source_query (
    id                  TEXT PRIMARY KEY,
    persona_id          TEXT REFERENCES persona(id) ON DELETE SET NULL,
    tick_id             TEXT,
    query_text          TEXT NOT NULL,
    query_kind          TEXT NOT NULL DEFAULT 'search'
                        CHECK (query_kind IN ('search','feed')),
    interest_key        TEXT,                   -- 命中的兴趣项 key
    emotion_label       TEXT,                   -- 当时的情绪标签
    result_count        INTEGER NOT NULL DEFAULT 0,
    kept_count          INTEGER NOT NULL DEFAULT 0,
    dropped_count       INTEGER NOT NULL DEFAULT 0,
    degraded            INTEGER NOT NULL DEFAULT 0,   -- 是否发生了降级
    degrade_reason      TEXT,
    cost_usd            REAL NOT NULL DEFAULT 0,
    correlation_id      TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_query_time ON source_query(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_query_interest ON source_query(interest_key, created_at DESC);
```

### 3.3 v0.3.0 新增：结构化日志

```sql
-- 迁移文件：migrations/004_observability.sql

-- ────────────────────────────────────────────────────────
-- 26. log_entry · 结构化运行日志
-- 只落 WARNING 及以上（全量日志在 logs/ 下的文件里）
-- ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS log_entry (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    level               TEXT NOT NULL,          -- WARNING|ERROR|CRITICAL
    logger              TEXT NOT NULL DEFAULT '',
    message             TEXT NOT NULL,
    module              TEXT,
    function            TEXT,
    line                INTEGER,
    plugin_id           TEXT,
    tick_id             TEXT,
    correlation_id      TEXT,
    exc_type            TEXT,
    exc_text            TEXT,                   -- 含堆栈；**不落库就无法排查**（这是与 event_log 的关键区别）
    extra_json          TEXT CHECK(extra_json IS NULL OR json_valid(extra_json)),
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_time ON log_entry(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_log_level_time ON log_entry(level, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_log_correlation ON log_entry(correlation_id);

-- 给已有可观测表补 correlation_id。
-- SQLite 没有 ADD COLUMN IF NOT EXISTS，但这里**不需要**「先查 PRAGMA table_info
-- 再决定要不要执行」：整个迁移跑在一个事务里，而它只会被跑一次（§ 8.3）。
ALTER TABLE tick_log     ADD COLUMN correlation_id TEXT;
ALTER TABLE activity_log ADD COLUMN correlation_id TEXT;
ALTER TABLE llm_usage    ADD COLUMN correlation_id TEXT;

CREATE INDEX IF NOT EXISTS idx_tick_log_correlation ON tick_log(correlation_id);
CREATE INDEX IF NOT EXISTS idx_activity_log_correlation ON activity_log(correlation_id);
CREATE INDEX IF NOT EXISTS idx_llm_usage_correlation ON llm_usage(correlation_id);

-- ────────────────────────────────────────────────────────
-- 视图（不是表）：成本合并与链路追踪
-- 为什么用视图而不是汇总表：汇总表要么有两个写入点（必然漂移），
-- 要么定期重算（预算检查会读到过期数据）
-- ────────────────────────────────────────────────────────
CREATE VIEW IF NOT EXISTS v_cost_daily AS
    SELECT persona_id, substr(created_at, 1, 10) AS day,
           purpose, 'llm'   AS cost_kind, cost_usd, 1 AS call_count
      FROM llm_usage WHERE success = 1
    UNION ALL
    SELECT persona_id, substr(created_at, 1, 10) AS day,
           purpose, 'media' AS cost_kind, cost_usd, 1 AS call_count
      FROM media_usage WHERE success = 1;

-- **第一路取 tick_log 而不是 event_log**：event_log 默认关闭（§ 5.3），
-- 只有它是锚点时，时间线在默认配置下会一条 tick 记录都没有，
-- 而 tick 恰恰是整条链的起点。event_log 因此降为第二路。
-- 第二路的 tick_id 是 NULL——event_log 没这列，也不该有。
CREATE VIEW IF NOT EXISTS v_trace AS
    SELECT correlation_id, 'tick'   AS step, '推演' AS label, created_at AS at,
           id AS tick_id, status AS detail, NULL AS payload_json
      FROM tick_log WHERE correlation_id IS NOT NULL
    UNION ALL
    SELECT correlation_id, 'event', '事件', occurred_at, NULL, topic, payload_json
      FROM event_log WHERE correlation_id IS NOT NULL
    UNION ALL
    SELECT correlation_id, 'llm', '模型调用', created_at, tick_id,
           model || ' · ' || purpose || ' · ' || total_tokens || ' tok', NULL
      FROM llm_usage WHERE correlation_id IS NOT NULL
    UNION ALL
    SELECT correlation_id, 'media', '生图', created_at, tick_id,
           model || ' · ' || purpose, NULL
      FROM media_usage WHERE correlation_id IS NOT NULL
    UNION ALL
    SELECT correlation_id, 'source', '检索', created_at, tick_id,
           query_text || ' · 保留 ' || kept_count, NULL
      FROM source_query WHERE correlation_id IS NOT NULL
    UNION ALL
    SELECT correlation_id, 'log', level, created_at, tick_id,
           message || COALESCE(' · ' || exc_text, ''), NULL
      FROM log_entry WHERE correlation_id IS NOT NULL;
```

> **上面这些 DDL 是设计参考，不是可执行文件。**
> 真实迁移文件（`src/alterego/storage/sqlite/migrations/*.sql`）里
> **没有** `PRAGMA`、**没有** `IF NOT EXISTS`、**没有** `BEGIN`/`COMMIT`、
> **没有** `INSERT INTO schema_version`——四件事全部由迁移器统一负责，
> 原因与后果见 § 8.3。「迁移文件为什么与文档不一样」这个问题的答案永远是
> 「因为不加那四句是一次有意的选择」，而不是「抄漏了」。

> **`event_log` 与 `log_entry` 为什么不合并**：`event_log` 是**可回放**的结构化事件流
> （它的 payload 能被重新投回总线），而 `log_entry` 含第三方库的输出与堆栈，**不可回放**。
> 两者受众也不同。`event_log` 保持默认关闭。

---

## 4. 表字段说明

### 4.1 需重点说明的字段

#### `memory.importance` 与 `memory.strength` 的区别

| 字段 | 含义 | 是否变化 |
| --- | --- | --- |
| `importance` | 写入时评定的**固有重要度**（0~1）。由 LLM 或规则评定，之后**不再改变** | ❌ 不变 |
| `strength` | **当前可召回强度**。随时间指数衰减，被回忆时回升 | ✅ 每 6 虚拟小时重算 |

分离两者的原因：一条记忆「很重要」（importance=0.9），但太久没想起来会「暂时想不起来」（strength 降到 0.2）。如果合并为一个字段，就无法表达「重要但不记得」这种真实状态。

#### `activity_log.suppressed_intent` / `suppress_reason`

当打扰预算拦截了一个 `reach_out` 意图时：

```
intent            = 'reflect_internal'      ← 实际执行（降级后）
suppressed_intent = 'reach_out'             ← 原本想做的
suppress_reason   = '当前日程不可打断（工作时段）'
inner_voice       = '刚看到那个独立游戏的视频，好想跟他说一声，算了他在上班'
```

这让 Web 的「内心」页面能展示 Agent「想说但没说出口的话」——这是本项目**最具拟人感的设计之一**。

#### `message.initiative` 与 `motivation`

| `initiative` | `motivation` | 含义 |
| --- | --- | --- |
| 0 | NULL | 回复用户消息 |
| 1 | `share_something` | 主动分享 |
| 1 | `miss_you` | 想念 |
| 1 | `need_comfort` | 求安慰 |
| 1 | `ask_question` | 求助 |
| 1 | `follow_up` | 跟进之前的话题 |
| 1 | `just_bored` | 无聊 |

统计动机分布是调优 Agent 拟人度的重要依据：如果 90% 都是 `just_bored`，说明人设需要调整。

#### `schedule_block.actual_start_at` vs `start_at`

计划与实际分离，用于计算「作息规律度」。长期偏离（如总是熬夜）可以作为 LLM 反思的输入：「最近一周有 5 天都是凌晨 2 点才睡，有点担心」。

#### `tick_log.state_snapshot_json`

保存 tick 开始时的关键状态片段（情绪、当前日程、关系摘要）。这让 `alterego why <tick_id>` 能完整还原当时的决策上下文，即使后续状态已变化。

### 4.2 枚举值汇总

| 表.字段 | 允许值 |
| --- | --- |
| `memory.kind` | `episodic`, `semantic`, `emotional` |
| `memory.source` | `tick`, `conversation`, `consolidation`, `manual`, `npc` |
| `relationship.relation_type` | `user`, `family`, `friend`, `close_friend`, `colleague`, `acquaintance`, `rival` |
| `npc.relation` | 同上（除 `user`） |
| `message.direction` | `inbound`, `outbound` |
| `message.motivation` | `share_something`, `miss_you`, `need_comfort`, `ask_question`, `follow_up`, `just_bored` |
| `social_post` 无枚举 | `mood_label` 为自由文本 |
| `schedule_block.category` | `sleep`, `work`, `meal`, `commute`, `leisure`, `social`, `chore`, `other` |
| `activity_log.category` | `internal`, `social`, `outbound` |
| `tick_log.status` | `ok`, `partial`, `failed`, `interrupted`, `skipped` |
| `llm_usage.purpose` | `decision`, `expression`, `reflection`, `emotion`, `memory`, `npc`, `persona`, `image_prompt`, `research_query`, `research_summarize`（权威清单在 [`07-model-routing-and-media.md § 2.4`](07-model-routing-and-media.md#24-用途purpose清单)） |
| `llm_usage.tier` | `strong`, `cheap`, `custom` |
| `post_interaction.kind` | `like`, `comment` |
| `persona_version.change_type` | `init`, `manual_edit`, `llm_refine`, `evolution` |

**节日不在这张表里，也不建表。** 日型（`workday` / `weekend` / `holiday` / `makeup_workday`）
与节日阶段（`none` / `anticipating` / `during` / `aftermath`）都是 `domain/calendar.py` 的
运行期枚举，**不落库**：它们是「给定日期 + 随包数据」的纯函数结果，落库只会多一份可能与
`holidays/*.toml` 不一致的副本。世界级事件仍然走 `world.events_json`（它装的是季节、社会热点
这类没有提前量的东西）；节日的区别与理由见 [12-calendar-and-conversation.md](12-calendar-and-conversation.md) § 2。

同理，`schedule_block.category` **本批没有增加 `festival`**：日程生成器还没写，
现在加进去会同时改 domain 的 Literal、`001_initial.sql` 的 CHECK 与上表三处，
而那时还没有任何代码会写入这个值。取舍与触发条件见 [12](12-calendar-and-conversation.md) § 15.1。

**生日也不建表。** 它和节日共用同一套运行期表示（`Holiday(kind="personal")`），
所以落库会带来同样的不一致风险，而它比节日更没必要——生日的**权威副本只有一个**，
就是用户手写的那份 `data/birthdays.toml`。建表就等于把「用户自己的文件」
变成一个需要同步的缓存，而缓存和真源对不上时，谁说了算会立刻变成一个新问题。

具体地，`relationship` 表**不加 `birthday` 列**。理由有三条：

1. 生日对**三种主体**都存在（`self` / `user` / `npc`），而 `relationship` 只描述 NPC。
   把 `self` 和 `user` 的生日塞进 NPC 表，或者为它们各开一张表，都不如一份文件干净。
2. `relationship` 不保证每个 NPC 都有一行。「记得生日但还没建档」是常态，
   而 `birthday` 列会让这种状态变得无法表示（要么强制先建档，要么允许 `NULL` 两重含义）。
3. 生日是用户手写的**事实**，不是推演出来的**状态**。这个仓库里推演产生的量
   （`affinity` / `familiarity` / `trust` / `tension`）才进 `relationship`。

判定依据很简单：**这份数据是「它想出来的」还是「你告诉它的」？** 后者不进库。
实现见 [12](12-calendar-and-conversation.md) § 17.3。

---

## 5. 索引策略

### 5.1 已建索引的设计意图

| 索引 | 支撑的查询 |
| --- | --- |
| `idx_memory_persona_time` | Web 记忆页按时间浏览 |
| `idx_memory_strength`（部分索引 `forgotten=0`） | 检索时按强度过滤，部分索引避免扫描已遗忘记忆 |
| `idx_message_unread`（部分索引 `read_at IS NULL`） | Sense 阶段拉取未读消息——高频查询，部分索引显著减小体积 |
| `idx_activity_suppressed`（部分索引） | Web「内心」页只查被拦截的意图 |
| `idx_tick_status` | 启动时查找 `interrupted` 状态的 tick |
| `idx_llm_usage_purpose` | `alterego stats` 按用途聚合成本 |
| `idx_event_correlation` | 按 correlation_id 还原一次完整 tick 的事件流 |
| `idx_tick_log_correlation` / `idx_activity_log_correlation` | 链路视图 `v_trace` 的两条支路（tick、activity） |
| `idx_llm_usage_correlation` | 同上，llm 支路 |
| `idx_log_correlation` | 按 correlation_id 一次性打开某次推演的全部日志 |

### 5.2 未建索引的取舍

| 字段 | 为何不建索引 |
| --- | --- |
| `memory.tags_json` | JSON 数组无法直接索引。需要标签检索时用 FTS5 的 `tags` 列 |
| `social_post.content` | 动态不需要全文检索（数量少，直接扫描） |
| `plugin_state.value` | KV 查询总是带 `plugin_id + key` 主键 |

### 5.3 写入放大控制

| 表 | 预估日增行数 | 控制策略 |
| --- | --- | --- |
| `tick_log` | 288（5 分钟粒度） | 保留 90 天后归档到 `tick_log_archive` 或直接删除 |
| `emotion_log` | 288 | 同上 |
| `event_log` | ~1500 | 默认关闭（`ALTEREGO_EVENT_LOG=1` 才写）；保留 7 天 |
| `activity_log` | ~288 | 保留 180 天 |
| `llm_usage` | ~400 | 永久保留（用于成本分析），行小 |
| `memory` | ~20 | 永久保留 |

**清理任务**：`alterego retention --apply` 按上述策略清理；`data_dir` 下的 `retention.toml` 可配置。

---

## 6. 记忆检索算法

### 6.1 完整流程

```mermaid
flowchart TD
    A["查询输入<br/>关键词 + 可选情绪上下文"] --> B["FTS5 全文检索<br/>BM25 排序<br/>limit=50"]
    B --> C{"EmbeddingProvider<br/>插件可用？"}
    C -->|是| D["向量检索 Top-30<br/>合并去重"]
    C -->|否| E["跳过"]
    D --> F["候选集"]
    E --> F
    F --> G["加权重排<br/>score = 相关性 × 重要度<br/>× 新近度 × 情绪一致度"]
    G --> H["过滤 strength < 阈值"]
    H --> I["取 Top-K (默认 8)"]
    I --> J["更新 recall_count<br/>与 last_recalled_at"]
```

### 6.2 排序公式

$$
\text{score} = w_r \cdot R + w_i \cdot I + w_n \cdot N + w_e \cdot E
$$

其中：

| 符号 | 含义 | 计算方式 | 默认权重 |
| --- | --- | --- | --- |
| $R$ | 相关性 | FTS5 BM25 分数归一化到 [0,1]；若来自向量检索则为余弦相似度 | $w_r = 0.40$ |
| $I$ | 重要度 | `memory.importance` | $w_i = 0.25$ |
| $N$ | 新近度 | $e^{-\lambda_n \cdot \text{days}}$，$\lambda_n = 0.03$ | $w_n = 0.20$ |
| $E$ | 情绪一致度 | $1 - \frac{|\text{memory.valence} - \text{current.valence}|}{2}$ | $w_e = 0.15$ |

**情绪一致度（$E$）的作用**：心情好的时候更容易想起开心的事，心情差的时候更容易想起不开心的事。这是心理学上的「心境一致性记忆」，让 Agent 的记忆检索有情绪色彩，而非机械匹配。

**强度门槛**：$I$ 项实际使用 $\text{importance} \times \text{strength\_at}(now)$，即同时考虑固有重要度与当前可召回度。

### 6.3 SQL 实现

```sql
-- 第 1 步：FTS5 召回
SELECT m.id, m.kind, m.content, m.summary, m.importance, m.strength,
       m.valence, m.entities_json, m.tags_json, m.occurred_at,
       m.recall_count, m.last_recalled_at,
       bm25(memory_fts, 1.0, 2.0, 1.5) AS bm25_score
FROM memory_fts
JOIN memory m ON m.id = memory_fts.memory_id
WHERE memory_fts MATCH :query
  AND m.persona_id = :persona_id
  AND m.forgotten = 0
ORDER BY bm25_score
LIMIT 50;
```

> `bm25(memory_fts, 1.0, 2.0, 1.5)` 中的权重分别对应 `content`、`summary`、`tags` 列。`summary` 权重最高（2.0），因为摘要是最精炼的语义表达。

```sql
-- 第 2 步：在 Python 中重排（领域层纯函数 rank_memories）
-- 不在 SQL 里算的原因：公式需要当前情绪状态等运行时参数，
-- 且要便于单元测试与调整权重
```

重排函数与权重都是领域层模型（`domain/memory.py`）：

```python
@dataclass(frozen=True, slots=True)
class RetrievalWeights:
    relevance: float = 0.40
    importance: float = 0.25
    recency: float = 0.20
    mood: float = 0.15

def rank_memories(
    candidates: Sequence[MemoryCandidate],
    now: datetime,
    *,
    query_valence: float | None = None,
    weights: RetrievalWeights = DEFAULT_RETRIEVAL_WEIGHTS,
    limit: int | None = DEFAULT_TOP_K,
    min_strength: float = FADE_THRESHOLD,
) -> list[MemorySearchHit]: ...
```

- `MemoryCandidate` = `(memory, relevance)`，`relevance` 是上一步交给领域层的 $R$ 项。
- `query_valence` 为 `None` 时 $E$ 项一律取 1.0（没有情绪上下文，就不让它扰动排序）。
- `min_strength` 先于 `limit` 生效：先丢掉低于阈值的，再取 Top-K。
- 同分时按 `memory.id` 排序，保证同一输入两次调用结果一致（P6 可复现）。

### 6.4 中文分词

SQLite FTS5 内置 `unicode61` 分词器对中文按**单字**切分，效果一般。两个改进选项：

| 方案 | 效果 | 代价 |
| --- | --- | --- |
| **默认**：`unicode61` 单字切分 | 能召回，但精度一般（"火锅" 会匹配含"火"或"锅"的记忆） | 零依赖 |
| **推荐**：`plugins/tokenizer_jieba/` 自定义 FTS5 分词器 | 精度显著提升 | 需安装 `jieba` + 编译扩展 |

**v1 折中方案**：写入 `memory_fts.content` 时，用 Python 侧的 `jieba`（若可用）预分词，用空格连接后存入；查询时同样处理。这不需要编译 SQLite 扩展：

```python
def preprocess_for_fts(text: str) -> str:
    try:
        import jieba
        return " ".join(jieba.cut_for_search(text))
    except ImportError:
        return text          # 降级：原样存储，FTS5 按单字切分
```

**索引一致性注意**：预处理必须**写入时与查询时使用同一函数**，否则匹配失败。`MemoryRepository` 统一封装这个逻辑。

### 6.5 记忆巩固

每 6 虚拟小时执行一次，把零散的 `episodic` 记忆压缩成 `semantic` 记忆：

```python
async def consolidate(repo: MemoryRepository, llm: LLMProvider, now: datetime) -> list[Memory]:
    """取最近 6 小时未巩固的 episodic 记忆 → LLM 归纳 → 写入 semantic 记忆"""

    recent = repo.list_recent(kind="episodic", since=now - timedelta(hours=6),
                              not_consolidated=True)
    if len(recent) < 3:
        return []       # 太少不值得归纳

    prompt = render("memory_consolidate", memories=recent)
    result = await llm.complete(LLMRequest(prompt=prompt, response_format="json"))
    # 期望输出: [{"summary": "...", "importance": 0.7, "tags": [...]}]

    created = []
    for item in json.loads(result.text)["memories"]:
        mem = Memory(
            kind="semantic",
            content=item["summary"],
            summary=item["summary"],
            importance=item["importance"],
            source="consolidation",
            source_ref=",".join(m.id for m in recent),
            entities=tuple(item.get("entities", [])),
            occurred_at=now,
            ...
        )
        repo.save(mem)
        created.append(mem)

    repo.mark_consolidated([m.id for m in recent])
    return created
```

**副作用**：巩固后原始 episodic 记忆的 `strength` 会被降低（信息已上提），模拟「细节模糊了但记住了大概」。

---

## 6.9 领域模型的归属

`§ 7` 的 Repository 契约引用了 20 多个领域类型。它们**不在存储层定义**，归属规则如下：

| 东西 | 住哪 | 为什么 |
| --- | --- | --- |
| 表镜像（一行 ↔ 一个对象） | `domain/` | 它是数据模型本身，不是存储实现细节；换后端不该换类型 |
| 业务规则与纯函数（`strength_at` / `rank_memories` / `update_emotion`） | `domain/` | 无 IO、无全局，可直接单测 |
| SQL、事务、JSON 序列化、往返映射 | `storage/` | 换 `sqlite` 为 `postgres` 时只有这一层要重写 |

**已经实现（本批次）**：`domain/schedule.py` / `domain/emotion.py` / `domain/memory.py`。

### 6.9.1 只有查询才需要的类型

有两个类型不对应任何表，它们是**查询结果**的形状，因此也住在 `domain/memory.py`：

```python
@dataclass(frozen=True, slots=True)
class MemorySearchHit:
    memory: Memory
    score: float          # 四项加权总和
    relevance: float      # R
    importance: float     # I（已乘上 strength_at）
    recency: float        # N
    mood: float           # E
    strength: float       # strength_at(memory, now) 的快照
```

保留分解而不只给一个 `score`，是因为「为什么排到前面」是调试检索质量时唯一看得见的东西——
`alterego memory search --explain` 直接把这几列打出来。

```python
@dataclass(frozen=True, slots=True)
class MemoryStats:
    total: int = 0
    active: int = 0
    forgotten: int = 0
    by_kind: Mapping[str, int] = field(default_factory=dict)
    avg_importance: float = 0.0
    avg_strength: float = 0.0
    oldest_at: datetime | None = None
    newest_at: datetime | None = None
```

`stats(persona_id)` 没有 `now` 参数，所以 `avg_strength` 用的是 `memory.strength`
这一列的**快照值**，不是现算的——这正是那个列必须存在的原因。

---

## 7. Repository 接口

存储层的 Repository 是**接口**，具体实现由 storage 插件提供。

```python
class PersonaRepository(Protocol):
    def get(self, persona_id: str) -> Persona | None: ...
    def save(self, persona: Persona) -> None: ...
    def list_versions(self, persona_id: str) -> list[PersonaVersion]: ...
    def rollback(self, persona_id: str, version: int) -> Persona: ...

class WorldRepository(Protocol):
    def get_by_persona(self, persona_id: str) -> World | None: ...
    def save(self, world: World) -> None: ...
    def list_npcs(self, world_id: str, active_only: bool = True) -> list[NPC]: ...
    def save_npc(self, npc: NPC) -> None: ...

class EmotionRepository(Protocol):
    def append(self, entry: EmotionEntry) -> None: ...
    def latest(self, persona_id: str) -> EmotionEntry | None: ...
    def history(self, persona_id: str, since: datetime, until: datetime | None = None) -> list[EmotionEntry]: ...

class MemoryRepository(Protocol):
    def save(self, memory: Memory) -> None: ...
    def save_many(self, memories: list[Memory]) -> None: ...
    def get(self, memory_id: str) -> Memory | None: ...
    def search_fts(self, query: str, persona_id: str, limit: int = 50) -> list[MemorySearchHit]: ...
    def list_recent(self, persona_id: str, since: datetime, *, kind: str | None = None,
                    not_consolidated: bool = False, limit: int = 100) -> list[Memory]: ...
    def update_strength(self, updates: list[tuple[str, float]]) -> None: ...
    def mark_recalled(self, memory_ids: list[str], at: datetime) -> None: ...
    def mark_forgotten(self, memory_ids: list[str]) -> None: ...
    def mark_consolidated(self, memory_ids: list[str]) -> None: ...
    def stats(self, persona_id: str) -> MemoryStats: ...

class RelationshipRepository(Protocol):
    def get(self, persona_id: str, target_id: str) -> Relationship | None: ...
    def get_all(self, persona_id: str) -> list[Relationship]: ...
    def upsert(self, rel: Relationship) -> None: ...
    def touch_contact(self, persona_id: str, target_id: str, at: datetime) -> None: ...

class ScheduleRepository(Protocol):
    def save_day(self, persona_id: str, day: date, blocks: list[ScheduleBlock]) -> None: ...
    def get_day(self, persona_id: str, day: date) -> list[ScheduleBlock]: ...
    def get_current(self, persona_id: str, at: datetime) -> ScheduleBlock | None: ...
    def update_actual(self, block_id: str, actual_start: datetime,
                      actual_end: datetime | None, note: str | None) -> None: ...

class ActivityRepository(Protocol):
    def append(self, activity: Activity) -> None: ...
    def list_range(self, persona_id: str, start: datetime, end: datetime,
                   *, outbound_only: bool = False) -> list[Activity]: ...
    def suppressed_only(self, persona_id: str, limit: int = 50) -> list[Activity]: ...

class PostRepository(Protocol):
    def save(self, post: SocialPost) -> None: ...
    def list_feed(self, persona_id: str, limit: int = 50, offset: int = 0) -> list[SocialPost]: ...
    def add_interaction(self, interaction: PostInteraction) -> None: ...
    def list_interactions(self, post_id: str) -> list[PostInteraction]: ...
    def count_today(self, persona_id: str, day: date) -> int: ...

class ConversationRepository(Protocol):
    def get_or_create(self, persona_id: str, counterpart_id: str,
                      counterpart_kind: str) -> Conversation: ...
    def append_message(self, message: Message) -> None: ...
    def list_messages(self, conversation_id: str, limit: int = 100,
                      before: datetime | None = None) -> list[Message]: ...
    def unread(self, persona_id: str) -> list[Message]: ...
    def mark_read(self, message_ids: list[str], at: datetime) -> None: ...

class TickRepository(Protocol):
    def save(self, tick: TickLog) -> None: ...
    def get(self, tick_id: str) -> TickLog | None: ...
    def latest(self, persona_id: str, limit: int = 1) -> list[TickLog]: ...
    def find_interrupted(self, persona_id: str) -> list[TickLog]: ...
    def mark_interrupted(self, tick_ids: list[str]) -> None: ...

class UsageRepository(Protocol):
    def record(self, usage: LLMUsage) -> None: ...
    def summary(self, since: datetime, until: datetime | None = None) -> UsageSummary: ...
    def daily_series(self, days: int = 30) -> list[UsageDaily]: ...

class BudgetRepository(Protocol):
    def get_today(self, persona_id: str, day: date) -> BudgetUsage: ...
    def increment(self, persona_id: str, day: date, *, kind: Literal["message", "post"],
                  suppressed: bool = False) -> None: ...
    def reset_no_reply(self, persona_id: str, day: date) -> None: ...
    def bump_no_reply(self, persona_id: str, day: date) -> int: ...
    def set_circuit(self, persona_id: str, until: datetime | None) -> None: ...
```

**约定**：

- 所有方法可能抛 `StorageError`
- 所有写操作在调用方的 `with storage.transaction():` 内执行时会被合并为单事务
- Repository 实现不得包含业务逻辑（如「重要度如何计算」属于领域层）

---

## 8. 迁移机制

### 8.1 文件组织

```
src/alterego/storage/sqlite/migrations/
├── 001_initial.sql          # 表 1–20：人格、世界、记忆、会话、推演、事件流、预算
├── 002_media.sql            # 表 21–22：图片资产与生图计量
├── 003_sources.sql          # 表 23–25：外部信息来源
└── 004_observability.sql    # 表 26：结构化日志 + 3 处补列 + 2 个视图
```

命名规范：`NNN_description.sql`，`NNN` 为三位数字，**严格递增且不许跳号**。
跳号会让「哪些版本存在」变成需要猜测的事，因此发现阶段会直接报错。

> **`003_sources.sql` 里的建表顺序与我们的编号不同**（先 `source_feed`、
> 再 `source_query`、最后 `source_item`）：SQLite 在 `CREATE TABLE` 那一刻
> 就会校验外键目标是否存在，而 `source_item` 引用 `source_query(id)`。
> **外键不能指向未来。** 编号只在文件名和 `schema_version` 里递增，
> 与文件内部的语句顺序无关。

### 8.2 迁移应用流程

```mermaid
flowchart TD
    A["存储后端 open()"] --> B["读 PRAGMA user_version<br/>（当前版本）"]
    B --> C["发现并解析 migrations/*.sql<br/>零容忍：文件名、头部四键、跳号"]
    C --> D["plan()：pending = 版本号 > 当前的迁移"]
    D --> E{"有破坏性迁移？"}
    E -->|是且没有备份目录| F["硬失败：不给退路就别改"]
    E -->|是且有备份目录| G["VACUUM INTO 备份"]
    E -->|否| H["按序逐个应用"]
    G --> H
    H --> I["一个迁移 = 一个脚本 = 一个事务：<br/>BEGIN → DDL → 记 schema_version<br/>→ PRAGMA user_version → COMMIT"]
    I --> J{"整个脚本成功？"}
    J -->|否| K["ROLLBACK：该迁移全部撤销<br/>版本号不动；已应用的迁移保持已应用"]
    J -->|是| H
```

**版本号的真源是 `PRAGMA user_version`，不是 `schema_version` 表。**
两者分工不同：`user_version` 是一个整数，能在不建表、不开事务的情况下读到
（空库也能读，值是 0），因此适合做**判依据**；`schema_version` 表是**审计记录**，
回答「第 3 版是什么时候、被谁、以什么名义应用的」。
如果反过来拿 `schema_version` 做判依据，那么「库是新库还是被清空过」
就变成了一个需要猜的问题。

### 8.3 迁移文件规范

一个迁移文件 = 一个版本 = **一次事务**。文件只管 DDL，五件事由迁移器统一负责：

| 文件里**不许**出现 | 为什么 |
| --- | --- |
| `PRAGMA ...` | 连接参数由连接层统一设置（§ 1）。文件里写 `PRAGMA journal_mode = WAL` 会直接报错——WAL 只能在事务外切换，而文件正在事务里 |
| `INSERT INTO` / `UPDATE` / `DELETE FROM schema_version` | 版本记账由迁移器在**同一个事务里**写。见下 |
| `PRAGMA user_version = N` | 同上：版本号与 DDL 必须在同一个事务里推进 |
| `BEGIN` / `COMMIT` / `ROLLBACK` / `END TRANSACTION` | 事务边界由迁移器负责；文件自己写就不再是一个事务，而这正是「失败就完整回滚」的前提 |
| `IF NOT EXISTS` | 见下 |

文件头是元数据，四个键**全部必填**：

```sql
-- migration: 002
-- description: 新增图片资产与生图计量两张表
-- destructive: false
-- reversible: true
```

`destructive` 与 `reversible` **没有默认值**：忘填会让迁移在发现阶段就被拒绝，
而不是被默认当成「安全」。一个字段如果默认值总是猜对，它就不该有默认值。

`destructive: true` 的迁移（删表、删列、重建表）在应用前必须能拿到退路：
迁移器要求一个**已授权的备份目录**，没有就硬失败。注意这与配置项
`storage.backup_before_destructive_migration = false` 的效果是同一件事：
关掉它意味着「不给退路」，而不意味着「那就别备份了」——失败落在「迁移没跑成」
而不是「数据没了」，前者重跑一次就好，后者不可逆。

#### 幂等靠版本号，不靠 `IF NOT EXISTS`

`CREATE TABLE IF NOT EXISTS` 看上去更稳，实际更危险：它把「同一个迁移被跑了两次」
——一件真事故——变成一次静默的空操作，然后版本号照样涨上去。
**一个不可能失败的检查比没有检查更糟**（`AGENTS.md` 第 4 条）。

真正的幂等由两件事保证：

1. **版本号**：`plan()` 只挑版本号大于当前 `user_version` 的文件；
2. **原子性**：一个迁移里任何一条语句失败，整个事务回滚，版本号不动。

`ALTER TABLE ... ADD COLUMN` 因此**不需要**「先查 `PRAGMA table_info` 再决定要不要执行」
——它只在版本号没到的时候跑，而且只会跑一次。多一个预检就多一处「预检写错了
于是真话被跳过」的时机。

### 8.4 版本兼容检查

**要求哪个版本不是一个写死的常量，而是从迁移目录算出来的**：

```python
required = max(item.version for item in discover_migrations())   # 当前 = 4
```

理由：写死的常量会在「有人加了迁移但忘了改常量」时静默失效——新库建到 v4，
检查却以为只需要 v3。推导出来的值不可能与迁移目录不一致。

检查是**双向**的：

| 情况 | 结果 |
| --- | --- |
| `version == 0` | **合法**。空库就是 v0，`db migrate` 会建出全部结构 |
| `0 < version < MIN_COMPATIBLE_VERSION` | `StorageError`，`hint` 指向 `alterego db migrate` |
| `version > required` | `StorageError`，`hint` 指向「升级 alterego」 |
| 其余 | 通过 |

报错必须带上**库文件路径**：用户手里往往同时有几个库（开发用的、测试用的、
从别处拷来的），只说「版本不对」等于让他们自己猜是哪一个。

> **为什么要拒绝「库比程序新」。** 一份 v5 的库用 v4 的程序打开，大部分
> `SELECT` 会正常返回，但新增的列对代码不存在——于是「读到一半发现缺字段」
> 变成「读到了一份看起来正常、实际不完整的数据」。这类错误会在很久以后以
> 「数据怎么少了一块」的形式暴露，而那时已经无从追溯。**宁可启动失败。**

### 8.5 命令

```bash
alterego db status               # 显示当前版本与待执行迁移（只读，不建库）
alterego db migrate              # 应用所有待执行迁移
alterego db migrate --dry-run    # 只显示将要执行的内容
alterego db backup               # 手动备份
alterego db restore <file>       # 从备份恢复（这是唯一的「回滚」方式）
```

**已实现**（`alterego db` 命令组，见 [05](05-channels.md) § 8.2 的样例输出）。
几条不在字面上但影响使用的约定：

- **`status` 与 `migrate --dry-run` 不建库**。库不存在时它们打开一个内存库，
  而不是把真路径建出来——「查一下」不该变成「建了一个空库」。
- **`migrate --dry-run` 也不备份**。备份的时机是「真的要改数据库之前」，
  预演连这个前提都不存在。
- **`restore` 先把现在这份留一手再覆盖**，顺序是「留一手 → 校验备份 → 覆盖」。
  任何一步失败都必须在「现在这份还在」的状态下退出：一次失败的恢复
  不该把两份都弄没。
- **退出码**：命令自身的问题（库还不在、备份读不出来）是 `2`；
  迁移失败是 `4`（见 [01](01-architecture.md) § 6.3）。

> **没有 `db rollback --to N`，这是有意的。** 反向迁移的正确性几乎从不会被测试
> ——真正的回滚发生在生产事故里，而那是最不适合第一次运行某段代码的时机。
> 迁移器的选择是**只往前**：版本号低于当前就直接拒绝，退路是一条 `VACUUM INTO`
> 出来的完整备份，恢复它就是完整的回滚。
>
> 迁移文件头里的 `reversible` 因此目前只是**声明性元数据**：它记下「这个变更
> 原则上可逆」，供 `db status` 展示与将来的实现参考，但它不是任何一条代码路径的开关。

---

## 9. 备份与导出

### 9.1 备份

```bash
alterego db backup                          # → data/backups/alterego-20260915-143211.db
alterego db backup --dest /path/to/backup
```

使用 SQLite 的 `VACUUM INTO`（在线安全备份，无需停止进程）：

```sql
VACUUM INTO 'data/backups/alterego-20260915-143211.db';
```

**备份不覆盖已有的备份。** 目标文件名已存在时往后加序号（`x.db` → `x-2.db`），
实际的路径会打印出来。自动生成的名字只精确到秒，一秒内跑两次完全可能，
而 `VACUUM INTO` 又拒绝写一个已存在的文件——早先的做法是「先删掉再写」，
于是第二次备份把第一次的成果删掉了。**备份是退路，退路不能互相抵消。**

### 9.2 导出

```bash
alterego export --format json --out backup.json
alterego export --format jsonl --since 2026-09-01
alterego export --include-usage --include-tick-log
```

JSON 导出格式：

```json
{
  "format_version": 1,
  "schema_version": 1,
  "exported_at": "2026-09-15T14:32:11+08:00",
  "persona": { "...": "..." },
  "world": { "...": "..." },
  "npcs": [ "..." ],
  "relationships": [ "..." ],
  "memories": [ "..." ],
  "posts": [ "..." ],
  "conversations": [ "..." ],
  "schedule": [ "..." ],
  "activities": [ "..." ],
  "emotion_log": [ "..." ]
}
```

**导出用途**：
- 迁移到另一台机器
- 分享人格设定（可 `--anonymize` 脱敏用户对话内容）
- 数据分析（导出为 CSV 后外部处理）

### 9.3 导入

```bash
alterego import backup.json              # 合并到现有数据
alterego import backup.json --replace    # 清空后导入
```

导入时校验 `schema_version`，不兼容则尝试自动迁移。

---

## 10. 容量估算

以 5 分钟 tick 粒度、1 个主角 + 8 个 NPC、运行 1 年为例：

| 表 | 行数/年 | 平均行大小 | 数据量 |
| --- | --- | --- | --- |
| `tick_log` | 105,120 | ~2 KB | ~210 MB |
| `emotion_log` | 105,120 | ~200 B | ~21 MB |
| `activity_log` | 105,120 | ~500 B | ~53 MB |
| `event_log` | 关闭则不占 | — | 0 |
| `llm_usage` | 146,000 | ~200 B | ~29 MB |
| `media_usage` | ~1,200 | ~200 B | ~0.3 MB |
| `log_entry` | ~11,000 | ~1 KB | ~11 MB |
| `source_item` | ~3,000 | ~700 B | ~2 MB |
| `source_query` | ~500 | ~300 B | ~0.2 MB |
| `memory` | 7,300 | ~600 B | ~4.4 MB |
| `message` | 5,000 | ~400 B | ~2 MB |
| `social_post` | 1,460 | ~600 B | ~0.9 MB |
| 其余小表 | — | — | ~5 MB |
| **合计** | | | **~339 MB/年** |

**图片文件不计入本表**（存在文件系统而非数据库）：1024×1024 PNG 约 1.2 MB / WebP 约 180 KB，
每天 3–8 张 → **~180–550 MB/年**。若只保留 WebP，约 200 MB/年。

**优化建议**：

1. `tick_log` 占 62%（约 210 MB / 339 MB）。若不需要完整决策溯源，可设 `[retention] tick_log_detail = "summary"`，只保留 intent + 耗时，体积降至 1/20
2. 图片是**唯一无上限增长**的部分。`[retention] media_keep_days` 与 `media_private_keep_days` 应默认开启
3. 定期 `VACUUM` 回收空间（`alterego db vacuum`）
4. 归档策略：`alterego db archive --before 2026-06-01` 把老数据移到独立文件

**关键约束**：SQLite 在单库 < 10 GB 时性能优良。上述规模即使运行 10 年也在安全范围。

---

## 11. 数据一致性硬要求

| 要求 | 理由 | 强制方式 |
| --- | --- | --- |
| 所有可观测表必须有 `correlation_id` | 没有它就无法把一次推演的全部痕迹串起来 | 迁移脚本 + 新增表时的 checklist |
| 同一次推演内 `correlation_id` 必须相同 | 否则「链路追踪」是假的 | `Event.correlation_id` 已存在，各表写入时统一取用 |
| `media_asset` 中 `role='canonical'` 每个 persona 恰有一行 | 定妆照是形象一致性的单点真源（ADR-0008） | 部分唯一索引 `idx_media_canonical` |
| `source_item.url_hash` 全局唯一 | 防止同一文章反复进记忆、重复消耗 LLM | UNIQUE 索引 + 写入前 `normalize_url()` |
| `llm_usage` / `media_usage` 只记计量，不记业务语义 | 语义混入计量表会让两边聚合都变脏 | 评审约定；统计一律走 `v_cost_daily` |

---

## 变更记录

| 日期 | 版本 | 变更 | 作者 |
| --- | --- | --- | --- |
| 2026-09-15 | v0.1.0 | 初版，schema_version = 1 | LMG-arch |
| 2026-09-15 | v0.2.0 | 新增表 21–26（`media_asset` / `media_usage` / `source_item` / `source_feed` / `source_query` / `log_entry`）；新增视图 `v_cost_daily` / `v_trace`；`tick_log`/`activity_log`/`llm_usage` 补 `correlation_id`；新增 § 11 数据一致性硬要求；容量估算 325→339 MB/年 | LMG-arch |

-- migration: 001
-- description: 建立 v0.1.0 的基础表、记忆全文索引与触发器
-- destructive: false
-- reversible: true
--
-- ============================================================
-- 写这个文件的四条约束（依据 docs/design/03-data-model.md § 8.3）
-- ============================================================
--
-- 1. 不放 PRAGMA。journal_mode / synchronous / busy_timeout /
--    foreign_keys / temp_store 由存储连接的 PragmaSettings 统一设置，
--    而且 journal_mode 必须在任何事务之前设好——迁移是在事务里跑的，
--    这里再设一次不但晚，还会在只读打开时莫名失败。
--
-- 2. 不写 schema_version，也不写 PRAGMA user_version。这两笔账由迁移器在
--    **同一个事务里**记。让文件自己记账，就总有一个文件会忘——
--    忘掉的那次会留下「表建好了、版本号没涨」的状态，
--    而下次启动会再跑一遍这个文件。
--
-- 3. 不写 BEGIN / COMMIT。事务边界属于迁移器；文件自带 COMMIT 会把
--    「原子应用」变成一句口号，中途失败就留下半截结构。
--
-- 4. 不写 IF NOT EXISTS。迁移由版本号保证只跑一次，
--    而 IF NOT EXISTS 恰好会把「同一个迁移被跑了两次」这件真事故
--    变成一个静默的空操作，然后版本号照样涨上去——
--    一个不可能失败的检查比没有检查更糟。
--
-- ============================================================

-- ────────────────────────────────────────────────────────────
-- 迁移版本
-- ────────────────────────────────────────────────────────────
CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL,
    description TEXT NOT NULL
);

-- ────────────────────────────────────────────────────────────
-- 1. persona · 人格主表（当前生效版本）
-- ────────────────────────────────────────────────────────────
CREATE TABLE persona (
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
CREATE TABLE persona_version (
    id           TEXT PRIMARY KEY,
    persona_id   TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    persona_json TEXT NOT NULL CHECK (json_valid(persona_json)),
    change_type  TEXT NOT NULL,          -- init | manual_edit | llm_refine | evolution
    change_note  TEXT,                   -- 变更说明（如「更毒舌一点」）
    diff_json    TEXT CHECK (diff_json IS NULL OR json_valid(diff_json)),
    created_at   TEXT NOT NULL,
    UNIQUE (persona_id, version)
);

CREATE INDEX idx_persona_version_pid
    ON persona_version(persona_id, version DESC);

-- ────────────────────────────────────────────────────────────
-- 3. world · 世界设定
-- ────────────────────────────────────────────────────────────
CREATE TABLE world (
    id             TEXT PRIMARY KEY,
    persona_id     TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    setting        TEXT NOT NULL,      -- 时代/城市/行业/社会背景（长文本）
    locations_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(locations_json)),
    events_json    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(events_json)),
    weather_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(weather_json)),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_world_persona ON world(persona_id);

-- ────────────────────────────────────────────────────────────
-- 4. npc · NPC 档案（Agent 社交圈）
-- ────────────────────────────────────────────────────────────
CREATE TABLE npc (
    id             TEXT PRIMARY KEY,
    world_id       TEXT NOT NULL REFERENCES world(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    -- family | friend | close_friend | colleague | acquaintance | rival
    relation       TEXT NOT NULL,
    profile_json   TEXT NOT NULL CHECK (json_valid(profile_json)),
    -- 简化人格：性格标签、说话风格、口头禅
    emotion_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(emotion_json)),
    active         INTEGER NOT NULL DEFAULT 1,   -- 是否参与推演
    npc_tick_interval_minutes INTEGER NOT NULL DEFAULT 30,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX idx_npc_world ON npc(world_id, active);

-- ────────────────────────────────────────────────────────────
-- 5. relationship · 关系状态
-- ────────────────────────────────────────────────────────────
CREATE TABLE relationship (
    id                TEXT PRIMARY KEY,
    persona_id        TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    target_id         TEXT NOT NULL,      -- 'user' 或 npc.id
    target_name       TEXT NOT NULL,
    target_kind       TEXT NOT NULL,      -- user | npc
    relation_type     TEXT NOT NULL,
    affinity          REAL NOT NULL DEFAULT 0,     -- -100 ~ 100
    familiarity       REAL NOT NULL DEFAULT 0,     -- 0 ~ 100
    trust             REAL NOT NULL DEFAULT 50,    -- 0 ~ 100
    tension           REAL NOT NULL DEFAULT 0,     -- 0 ~ 100
    notes             TEXT NOT NULL DEFAULT '',    -- LLM 生成的印象笔记
    interaction_count INTEGER NOT NULL DEFAULT 0,
    last_contact_at   TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (persona_id, target_id)
);

CREATE INDEX idx_relationship_persona
    ON relationship(persona_id, affinity DESC);

-- ────────────────────────────────────────────────────────────
-- 6. emotion_log · 情绪时间序列
-- ────────────────────────────────────────────────────────────
CREATE TABLE emotion_log (
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

CREATE INDEX idx_emotion_persona_time
    ON emotion_log(persona_id, recorded_at DESC);

-- ────────────────────────────────────────────────────────────
-- 7. memory · 记忆
-- ────────────────────────────────────────────────────────────
CREATE TABLE memory (
    id               TEXT PRIMARY KEY,
    persona_id       TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    kind             TEXT NOT NULL,      -- episodic | semantic | emotional
    content          TEXT NOT NULL,      -- 完整内容
    summary          TEXT NOT NULL,      -- 一句话摘要，注入提示词用
    importance       REAL NOT NULL DEFAULT 0.5 CHECK (importance >= 0 AND importance <= 1),
    strength         REAL NOT NULL DEFAULT 1.0 CHECK (strength >= 0),
    valence          REAL NOT NULL DEFAULT 0 CHECK (valence >= -1 AND valence <= 1),
    entities_json    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(entities_json)),
    tags_json        TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags_json)),
    -- tick | conversation | consolidation | manual | npc
    source           TEXT NOT NULL DEFAULT 'tick',
    source_ref       TEXT,               -- 来源 id（tick_id / message_id / memory_id）
    occurred_at      TEXT NOT NULL,
    last_recalled_at TEXT,
    recall_count     INTEGER NOT NULL DEFAULT 0,
    forgotten        INTEGER NOT NULL DEFAULT 0,   -- 已遗忘（不删除，仅降权）
    created_at       TEXT NOT NULL
);

CREATE INDEX idx_memory_persona_time
    ON memory(persona_id, occurred_at DESC);
CREATE INDEX idx_memory_strength
    ON memory(persona_id, strength DESC) WHERE forgotten = 0;
CREATE INDEX idx_memory_kind
    ON memory(persona_id, kind, occurred_at DESC);

-- ────────────────────────────────────────────────────────────
-- 8. memory_fts · 记忆全文索引（FTS5）
--
-- 外部内容表（contentless 的反面）：它自己存一份正文，
-- 所以这里不需要 `content='memory'` 的触发器维护协议。
-- 分词器固定 unicode61 + remove_diacritics 2：
-- 中文由 jieba 在**写入前**切好空格，查询时同样切——
-- 换分词器等于让已建索引全部作废，所以它是一个需要迁移的决定。
-- ────────────────────────────────────────────────────────────
CREATE VIRTUAL TABLE memory_fts USING fts5(
    memory_id UNINDEXED,
    content,
    summary,
    tags,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- 保持 FTS 与主表同步的触发器
CREATE TRIGGER trg_memory_ai
AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(memory_id, content, summary, tags)
    VALUES (new.id, new.content, new.summary, new.tags_json);
END;

CREATE TRIGGER trg_memory_au
AFTER UPDATE OF content, summary, tags_json ON memory BEGIN
    DELETE FROM memory_fts WHERE memory_id = old.id;
    INSERT INTO memory_fts(memory_id, content, summary, tags)
    VALUES (new.id, new.content, new.summary, new.tags_json);
END;

CREATE TRIGGER trg_memory_ad
AFTER DELETE ON memory BEGIN
    DELETE FROM memory_fts WHERE memory_id = old.id;
END;

-- ────────────────────────────────────────────────────────────
-- 9. conversation · 会话
-- ────────────────────────────────────────────────────────────
CREATE TABLE conversation (
    id               TEXT PRIMARY KEY,
    persona_id       TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    counterpart_id   TEXT NOT NULL,          -- 'user' 或 npc.id
    counterpart_kind TEXT NOT NULL,          -- user | npc
    title            TEXT,
    message_count    INTEGER NOT NULL DEFAULT 0,
    last_message_at  TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE (persona_id, counterpart_id)
);

CREATE INDEX idx_conversation_persona
    ON conversation(persona_id, last_message_at DESC);

-- ────────────────────────────────────────────────────────────
-- 10. message · 消息
-- ────────────────────────────────────────────────────────────
CREATE TABLE message (
    id               TEXT PRIMARY KEY,
    conversation_id  TEXT NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
    -- inbound（用户→Agent）| outbound（Agent→用户）
    direction        TEXT NOT NULL,
    sender_id        TEXT NOT NULL,          -- 'user' / persona.id / npc.id
    content          TEXT NOT NULL,
    content_type     TEXT NOT NULL DEFAULT 'text',  -- text | markdown | image
    -- 主动消息的动机（仅 outbound 且为主动发起时填写）
    initiative       INTEGER NOT NULL DEFAULT 0,    -- 是否主动发起（非回复）
    motivation       TEXT,                   -- share_something | miss_you | ...
    trigger_note     TEXT,                   -- 触发原因描述
    -- 分发状态
    delivered_channels_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(delivered_channels_json)),
    read_at          TEXT,
    replied_at       TEXT,
    tick_id          TEXT,
    created_at       TEXT NOT NULL
);

CREATE INDEX idx_message_conv_time
    ON message(conversation_id, created_at DESC);
CREATE INDEX idx_message_unread
    ON message(conversation_id, direction, read_at) WHERE read_at IS NULL;
CREATE INDEX idx_message_initiative
    ON message(direction, initiative, created_at DESC);

-- ────────────────────────────────────────────────────────────
-- 11. social_post · 动态（朋友圈）
-- ────────────────────────────────────────────────────────────
CREATE TABLE social_post (
    id                TEXT PRIMARY KEY,
    persona_id        TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    content           TEXT NOT NULL,
    image_paths_json  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(image_paths_json)),
    location          TEXT,
    mood_label        TEXT,
    mood_valence      REAL,
    mood_arousal      REAL,
    -- 生成溯源
    intent_motivation TEXT,
    trigger_note      TEXT,
    activity_ref      TEXT,                   -- 关联的 activity_log.id
    -- 统计
    like_count        INTEGER NOT NULL DEFAULT 0,
    comment_count     INTEGER NOT NULL DEFAULT 0,
    visible           INTEGER NOT NULL DEFAULT 1,
    tick_id           TEXT,
    posted_at         TEXT NOT NULL
);

CREATE INDEX idx_post_persona_time
    ON social_post(persona_id, posted_at DESC);

-- ────────────────────────────────────────────────────────────
-- 12. post_interaction · 动态互动
-- ────────────────────────────────────────────────────────────
CREATE TABLE post_interaction (
    id              TEXT PRIMARY KEY,
    post_id         TEXT NOT NULL REFERENCES social_post(id) ON DELETE CASCADE,
    actor_id        TEXT NOT NULL,          -- 'user' 或 npc.id
    actor_kind      TEXT NOT NULL,          -- user | npc
    actor_name      TEXT NOT NULL,
    kind            TEXT NOT NULL,          -- like | comment
    content         TEXT,                   -- 评论内容
    -- 该互动对 Agent 产生的影响
    emotion_impact  REAL NOT NULL DEFAULT 0,
    affinity_impact REAL NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE INDEX idx_post_interaction_post
    ON post_interaction(post_id, created_at);

-- ────────────────────────────────────────────────────────────
-- 13. schedule_block · 日程块
-- ────────────────────────────────────────────────────────────
CREATE TABLE schedule_block (
    id              TEXT PRIMARY KEY,
    persona_id      TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    day             TEXT NOT NULL,          -- YYYY-MM-DD（虚拟时间当地日期）
    start_at        TEXT NOT NULL,
    end_at          TEXT NOT NULL,
    activity        TEXT NOT NULL,
    -- sleep|work|meal|commute|leisure|social|chore|other
    category        TEXT NOT NULL,
    location        TEXT,
    interruptible   INTEGER NOT NULL DEFAULT 1,
    source          TEXT NOT NULL DEFAULT 'template',  -- template|llm|manual
    actual_start_at TEXT,                   -- 实际执行时间（可能与计划不同）
    actual_end_at   TEXT,
    deviation_note  TEXT,                   -- 偏离原因
    created_at      TEXT NOT NULL
);

CREATE INDEX idx_schedule_persona_day
    ON schedule_block(persona_id, day, start_at);

-- ────────────────────────────────────────────────────────────
-- 14. activity_log · 行为日志
-- ────────────────────────────────────────────────────────────
CREATE TABLE activity_log (
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
    suppress_reason   TEXT,
    -- 内心独白（含被拦截的「想说但没说」）
    inner_voice     TEXT,
    duration_minutes INTEGER,
    tick_id         TEXT,
    started_at      TEXT NOT NULL,
    ended_at        TEXT
);

CREATE INDEX idx_activity_persona_time
    ON activity_log(persona_id, started_at DESC);
CREATE INDEX idx_activity_intent
    ON activity_log(persona_id, intent, started_at DESC);
CREATE INDEX idx_activity_suppressed
    ON activity_log(persona_id, started_at DESC) WHERE suppressed_intent IS NOT NULL;

-- ────────────────────────────────────────────────────────────
-- 15. tick_log · 推演日志（可解释性的核心）
-- ────────────────────────────────────────────────────────────
CREATE TABLE tick_log (
    id                  TEXT PRIMARY KEY,   -- tick_id
    persona_id          TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    virtual_time        TEXT NOT NULL,
    real_duration_ms    INTEGER NOT NULL,
    -- ok | partial | failed | interrupted | skipped
    status              TEXT NOT NULL,
    -- 决策溯源
    state_snapshot_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(state_snapshot_json)),
    percepts_json       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(percepts_json)),
    candidates_json     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(candidates_json)),
    chosen_intent       TEXT,
    motivation          TEXT,
    trigger_note        TEXT,
    suppressed_json     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(suppressed_json)),
    memories_json       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(memories_json)),
    notes_json          TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(notes_json)),
    stage_results_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(stage_results_json)),
    llm_calls           INTEGER NOT NULL DEFAULT 0,
    llm_tokens          INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX idx_tick_persona_time
    ON tick_log(persona_id, virtual_time DESC);
CREATE INDEX idx_tick_status
    ON tick_log(status, virtual_time DESC);

-- ────────────────────────────────────────────────────────────
-- 16. llm_usage · LLM 调用计量
--
-- purpose 的取值见 docs/design/07-model-routing-and-media.md § 2.4：
-- intention|emotion|memory|post|chat|reach_out|npc|persona_gen
--   |image_prompt|research_query|research_summarize|reaction
--
-- tier 记的是**路由档位**（strong / cheap / custom），
-- 不是模型名；模型名在 model 列。分开记才能在换模型后回答
-- 「换了之后便宜了多少」。
-- ────────────────────────────────────────────────────────────
CREATE TABLE llm_usage (
    id                TEXT PRIMARY KEY,
    persona_id        TEXT REFERENCES persona(id) ON DELETE SET NULL,
    tick_id           TEXT,
    purpose           TEXT NOT NULL,
    tier              TEXT NOT NULL,
    provider_id       TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0,
    cache_hit         INTEGER NOT NULL DEFAULT 0,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    success           INTEGER NOT NULL DEFAULT 1,
    error             TEXT,
    created_at        TEXT NOT NULL
);

CREATE INDEX idx_llm_usage_time
    ON llm_usage(created_at DESC);
CREATE INDEX idx_llm_usage_purpose
    ON llm_usage(purpose, created_at DESC);

-- ────────────────────────────────────────────────────────────
-- 17. plugin_state · 插件 KV 状态
-- ────────────────────────────────────────────────────────────
CREATE TABLE plugin_state (
    plugin_id  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL CHECK (json_valid(value)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (plugin_id, key)
);

-- ────────────────────────────────────────────────────────────
-- 18. plugin_meta · 插件元信息（用于 plugins doctor）
-- ────────────────────────────────────────────────────────────
CREATE TABLE plugin_meta (
    plugin_id     TEXT PRIMARY KEY,
    version       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    source        TEXT NOT NULL,          -- local | entry_point
    source_path   TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_loaded_at TEXT,
    last_error    TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- ────────────────────────────────────────────────────────────
-- 19. event_log · 事件流（Web 回放与调试）
-- ────────────────────────────────────────────────────────────
CREATE TABLE event_log (
    id             TEXT PRIMARY KEY,
    topic          TEXT NOT NULL,
    payload_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    source         TEXT NOT NULL,
    correlation_id TEXT,
    occurred_at    TEXT NOT NULL
);

CREATE INDEX idx_event_topic_time
    ON event_log(topic, occurred_at DESC);
CREATE INDEX idx_event_correlation
    ON event_log(correlation_id);

-- ────────────────────────────────────────────────────────────
-- 20. budget_usage · 打扰预算每日用量
--
-- 主键是 (persona_id, day)：一天一行，用 UPDATE 累加。
-- 「连续未回复」也放在这里，因为熔断判定只需要这一个数，
-- 单独建表会让检查预算时多一次查询。
-- ────────────────────────────────────────────────────────────
CREATE TABLE budget_usage (
    persona_id           TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    day                  TEXT NOT NULL,      -- YYYY-MM-DD 虚拟时间当地日期
    messages_sent        INTEGER NOT NULL DEFAULT 0,
    posts_sent           INTEGER NOT NULL DEFAULT 0,
    messages_suppressed  INTEGER NOT NULL DEFAULT 0,
    posts_suppressed     INTEGER NOT NULL DEFAULT 0,
    consecutive_no_reply INTEGER NOT NULL DEFAULT 0,
    circuit_until        TEXT,               -- 熔断截止时间（连续未回复触发）
    last_message_at      TEXT,
    last_post_at         TEXT,
    updated_at           TEXT NOT NULL,
    PRIMARY KEY (persona_id, day)
);

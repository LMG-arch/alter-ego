-- migration: 003
-- description: 新增外部信息来源（检索条目、RSS 订阅源、检索记录）
-- destructive: false
-- reversible: true
--
-- v0.3.0（ADR-0009 抓取到的内容是**不可信输入**）。
--
-- 本文件只放 DDL：不放 PRAGMA、不写 schema_version、不写
-- PRAGMA user_version、不写 BEGIN/COMMIT、不写 IF NOT EXISTS。
-- 原因见 001_initial.sql 的文件头。
--
-- 建表顺序与设计文档里的编号顺序**不同**，这是有意的：
-- source_item 同时引用 source_query 和 source_feed，
-- 而 SQLite 建表时就会检查外键目标是否存在（外键不能指向未来）。
-- 所以先建两张被引用的表，最后建 source_item。
--
-- ============================================================

-- ────────────────────────────────────────────────────────
-- 24. source_feed · RSS 订阅源
--
-- etag / last_modified 是省钱的关键：命中 304 时既不消耗流量，
-- 也不消耗 LLM——不用为「什么都没变」付一次摘要的钱。
-- ────────────────────────────────────────────────────────
CREATE TABLE source_feed (
    id              TEXT PRIMARY KEY,
    persona_id      TEXT REFERENCES persona(id) ON DELETE CASCADE,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    category        TEXT,
    etag            TEXT,
    last_modified   TEXT,
    last_checked_at TEXT,
    last_success_at TEXT,
    failure_count   INTEGER NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_source_feed_url ON source_feed(persona_id, url);

-- ────────────────────────────────────────────────────────
-- 25. source_query · 一次检索
--
-- 存 emotion_label 是为了能按情绪回顾「它最近在关心什么」——
-- 只留 URL 和标题的话，这份记录就只是日志，不是它的经历。
-- ────────────────────────────────────────────────────────
CREATE TABLE source_query (
    id             TEXT PRIMARY KEY,
    persona_id     TEXT REFERENCES persona(id) ON DELETE SET NULL,
    tick_id        TEXT,
    query_text     TEXT NOT NULL,
    query_kind     TEXT NOT NULL DEFAULT 'search'
                   CHECK (query_kind IN ('search','feed')),
    interest_key   TEXT,                   -- 命中的兴趣项 key
    emotion_label  TEXT,                   -- 当时的情绪标签
    result_count   INTEGER NOT NULL DEFAULT 0,
    kept_count     INTEGER NOT NULL DEFAULT 0,
    dropped_count  INTEGER NOT NULL DEFAULT 0,
    degraded       INTEGER NOT NULL DEFAULT 0,   -- 是否发生了降级
    degrade_reason TEXT,
    cost_usd       REAL NOT NULL DEFAULT 0,
    correlation_id TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX idx_source_query_time ON source_query(created_at DESC);
CREATE INDEX idx_source_query_interest ON source_query(interest_key, created_at DESC);

-- ────────────────────────────────────────────────────────
-- 23. source_item · 检索到的外部条目
--
-- 不存网页正文：版权、体积、检索质量三重原因（见 ADR-0009）。
-- 正文只在一次推演内活着，用完即弃；留下的只有摘要——
-- 而摘要本身也是**不可信输入**，注入检测在入库前完成，
-- 被拦下的条目照样留一行（dropped=1 + dropped_reason），
-- 因为「它差点被喂进去什么」正是排查提示词注入的唯一线索。
-- ────────────────────────────────────────────────────────
CREATE TABLE source_item (
    id             TEXT PRIMARY KEY,
    query_id       TEXT REFERENCES source_query(id) ON DELETE CASCADE,
    persona_id     TEXT REFERENCES persona(id) ON DELETE SET NULL,
    url            TEXT NOT NULL,
    url_hash       TEXT NOT NULL,          -- normalize 后取 SHA-256 前 16 字节，用于去重
    title          TEXT NOT NULL DEFAULT '',
    summary        TEXT NOT NULL DEFAULT '',
    lang           TEXT,
    published_at   TEXT,
    fetched_at     TEXT NOT NULL,
    source_kind    TEXT NOT NULL DEFAULT 'search'
                   CHECK (source_kind IN ('search','feed','direct')),
    feed_id        TEXT REFERENCES source_feed(id) ON DELETE SET NULL,
    content_chars  INTEGER NOT NULL DEFAULT 0,
    dropped        INTEGER NOT NULL DEFAULT 0,
    dropped_reason TEXT,                   -- injection|too_long|duplicate|blocked
    memory_id      TEXT REFERENCES memory(id) ON DELETE SET NULL,
    interest_delta REAL NOT NULL DEFAULT 0,
    correlation_id TEXT,
    created_at     TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_source_item_hash ON source_item(url_hash);
CREATE INDEX idx_source_item_time ON source_item(fetched_at DESC);
CREATE INDEX idx_source_item_dropped ON source_item(dropped, fetched_at DESC);

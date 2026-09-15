-- migration: 004
-- description: 新增结构化运行日志、补齐 correlation_id 与两个视图
-- destructive: false
-- reversible: true
--
-- v0.3.0 可观测性（docs/design/09-observability.md）。
--
-- 本文件只放 DDL：不放 PRAGMA、不写 schema_version、不写
-- PRAGMA user_version、不写 BEGIN/COMMIT、不写 IF NOT EXISTS。
-- 原因见 001_initial.sql 的文件头。
--
-- 这里有三条 ALTER TABLE ADD COLUMN。SQLite 没有
-- ADD COLUMN IF NOT EXISTS，所以这三条**只有靠「迁移只跑一次」才安全**——
-- 版本号与整个迁移在同一个事务里推进，跑过一次就再也不会跑第二次。
-- 这也正是文件里不写 IF NOT EXISTS 的理由的延伸：
-- 一半语句靠版本号保证、另一半靠 IF NOT EXISTS 兜底，
-- 会让人看不出到底哪一层在保证幂等。
--
-- ============================================================

-- ────────────────────────────────────────────────────────
-- 26. log_entry · 结构化运行日志
--
-- 只落 WARNING 及以上（全量日志在 logs/ 下的文件里）。
-- 与 event_log 的分工：event_log 是可回放的事件流（payload 能重新投回总线），
-- log_entry 含第三方库输出与堆栈，不可回放；受众也不同。
-- exc_text 存堆栈是有意的——**不落库就无法排查**，
-- 而「出问题那一刻的堆栈」往往在轮转后的文件里已经找不到了。
-- ────────────────────────────────────────────────────────
CREATE TABLE log_entry (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    level          TEXT NOT NULL,          -- WARNING|ERROR|CRITICAL
    logger         TEXT NOT NULL DEFAULT '',
    message        TEXT NOT NULL,
    module         TEXT,
    function       TEXT,
    line           INTEGER,
    plugin_id      TEXT,
    tick_id        TEXT,
    correlation_id TEXT,
    exc_type       TEXT,
    exc_text       TEXT,                   -- 含堆栈
    extra_json     TEXT CHECK (extra_json IS NULL OR json_valid(extra_json)),
    created_at     TEXT NOT NULL
);

CREATE INDEX idx_log_time ON log_entry(created_at DESC);
CREATE INDEX idx_log_level_time ON log_entry(level, created_at DESC);
CREATE INDEX idx_log_correlation ON log_entry(correlation_id);

-- ────────────────────────────────────────────────────────
-- 给已有可观测表补 correlation_id
--
-- 这是「从一条错误跳回那次推演」的实现基础：
-- 同一次 tick 内产生的所有行共享同一个 correlation_id，
-- 于是排查时不需要靠时间戳猜关联。
-- ────────────────────────────────────────────────────────
ALTER TABLE tick_log     ADD COLUMN correlation_id TEXT;
ALTER TABLE activity_log ADD COLUMN correlation_id TEXT;
ALTER TABLE llm_usage    ADD COLUMN correlation_id TEXT;

CREATE INDEX idx_tick_log_correlation ON tick_log(correlation_id);
CREATE INDEX idx_activity_log_correlation ON activity_log(correlation_id);

-- ────────────────────────────────────────────────────────
-- 视图（不是表）：成本合并与链路追踪
--
-- 为什么用视图而不是汇总表：汇总表要么有两个写入点（必然漂移，
-- 因为没人记得同时更新两处），要么定期重算（于是预算检查会读到过期数据，
-- 而预算检查的全部意义就是「现在花超了吗」）。
-- 视图每次读的都是真实数据，代价只是一次全表扫。
-- ────────────────────────────────────────────────────────
CREATE VIEW v_cost_daily AS
    SELECT persona_id, substr(created_at, 1, 10) AS day,
           purpose, 'llm'   AS cost_kind, cost_usd, 1 AS call_count
      FROM llm_usage WHERE success = 1
    UNION ALL
    SELECT persona_id, substr(created_at, 1, 10) AS day,
           purpose, 'media' AS cost_kind, cost_usd, 1 AS call_count
      FROM media_usage WHERE success = 1;

-- 一次推演的完整时间线。列的含义：
--   correlation_id  分组键
--   step            来源类型，页面据此上色/加图标
--   label           给人看的名字
--   at              真实时间，只有它能让六路来源排在同一条时间轴上
--   tick_id         跳转回推演详情（event/log 两类没有，见下）
--   detail          一行摘要
--   payload_json    只有 event_log 有，用于「重放这一步」
--
-- **第一路取 tick_log 而不是 event_log**：event_log 默认关闭
-- （`06-roadmap.md § 5.4` 估算 0 行/天），只有它是锚点时，
-- 时间线在默认配置下会一条 tick 记录都没有，而 tick 恰恰是整条链的起点。
-- event_log 因此降为第二路，只在用户打开事件回放时出现。
--
-- **event_log 那一路的 tick_id 是 NULL**：event_log 没有 tick_id 列。
-- 不给它加一列的原因是它的 correlation_id 已经唯一标识一次推演，
-- 再加一列 tick_id 等于把同一个值写两遍——两份就得同步，同步就会漂移。
-- ────────────────────────────────────────────────────────
CREATE VIEW v_trace AS
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

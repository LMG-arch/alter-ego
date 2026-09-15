-- migration: 005
-- description: 记忆巩固标记与活动流梳理标记，各带一个部分索引
-- destructive: false
-- reversible: true
--
-- 依据 docs/design/03-data-model.md § 6.5 与
-- docs/plans/2026-09-15-memory-consolidation.md § 3。
--
-- 本文件只放 DDL：不放 PRAGMA、不写 schema_version、不写
-- PRAGMA user_version、不写 BEGIN/COMMIT、不写 IF NOT EXISTS。
-- 原因见 001_initial.sql 的文件头。
--
-- 「跑过一次」这件事由迁移器用版本号保证，所以下面两条
-- ALTER TABLE ADD COLUMN 不需要（也没有）IF NOT EXISTS 兜底。
--
-- ════════════════════════════════════════════════════════════
-- 为什么是「标记」而不是「记忆类型」
-- ════════════════════════════════════════════════════════════
--
-- 归纳出的 semantic 记忆与它的来源 episodic 记忆是**两条独立的行**，
-- 靠 source / source_ref 单向关联（semantic → 来源）。
-- 于是「这条被归纳过了吗」必须自己记一笔，否则下一次巩固会把
-- 同一批 episodic 再归纳一遍，产出内容几乎重复的记忆——
-- 而重复的记忆会让检索结果里出现三条一模一样的摘要。
--
-- 同理，activity_log 需要「这条活动被提炼过了吗」。
-- 没有它，每跑一次 distill 都会把同一周的活动重新变成新记忆。
--
-- ════════════════════════════════════════════════════════════
-- 为什么是部分索引
-- ════════════════════════════════════════════════════════════
--
-- 生产查询只有两条，且永远带 WHERE … IS NULL：
--
--     SELECT … FROM memory
--      WHERE persona_id = ? AND kind = 'episodic' AND consolidated_at IS NULL
--      ORDER BY occurred_at DESC;
--
--     SELECT … FROM activity_log
--      WHERE persona_id = ? AND distilled_at IS NULL
--      ORDER BY started_at DESC;
--
-- 部分索引的键顺序与这两条查询一一对应，索引体也只含「还没处理的那批」——
-- 随着数据沉淀，它不但不长大，反而变小。
--
-- ⚠️ 注意 memory 表的 FTS 触发器是 AFTER UPDATE OF content, summary, tags_json，
-- 所以下面这两列被 UPDATE 时不会触发 memory_fts 的删+插。
-- 这是有意的：巩固只改记账信息，正文没变。

-- ────────────────────────────────────────────────────────────
-- memory · 已巩固标记
--
-- NULL = 还没被归纳过。非 NULL 是那次归纳发生的虚拟时间（ISO 8601）。
-- 存时间而不是存布尔值：0/1 回答不了「上周到底跑没跑」。
-- ────────────────────────────────────────────────────────────
ALTER TABLE memory ADD COLUMN consolidated_at TEXT;

CREATE INDEX idx_memory_unconsolidated
    ON memory(persona_id, kind, occurred_at DESC)
    WHERE consolidated_at IS NULL;

-- ────────────────────────────────────────────────────────────
-- activity_log · 已梳理标记
--
-- NULL = 还没被提炼成记忆。非 NULL 是那次提炼发生的虚拟时间。
--
-- 为什么不做成「活动直接变记忆」的外键：提炼是一对多且**有损**的
-- （十条活动可能只留下一条记忆，也可能一条都不留），
-- 存一个反向指针反而会暗示一种并不存在的对应关系。
-- ────────────────────────────────────────────────────────────
ALTER TABLE activity_log ADD COLUMN distilled_at TEXT;

CREATE INDEX idx_activity_undistilled
    ON activity_log(persona_id, started_at DESC)
    WHERE distilled_at IS NULL;

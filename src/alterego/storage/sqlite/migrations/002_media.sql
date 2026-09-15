-- migration: 002
-- description: 新增图片资产与生图计量
-- destructive: false
-- reversible: true
--
-- v0.2.0（ADR-0008 角色形象一致性）。
--
-- 本文件只放 DDL：不放 PRAGMA、不写 schema_version、不写
-- PRAGMA user_version、不写 BEGIN/COMMIT、不写 IF NOT EXISTS。
-- 原因见 001_initial.sql 的文件头。
--
-- ============================================================

-- ────────────────────────────────────────────────────────
-- 21. media_asset · 图片资产
--
-- role='canonical' 的行是**角色形象一致性的唯一真源**：
-- 每次生成人物图都要把它作为参考图发出去，所以它必须全库唯一
-- （见下面的部分唯一索引），而且「未发布的图也可见」——
-- 一致性出问题时，用户要能看到模型到底画成了什么样。
-- ────────────────────────────────────────────────────────
CREATE TABLE media_asset (
    id                 TEXT PRIMARY KEY,
    persona_id         TEXT NOT NULL REFERENCES persona(id) ON DELETE CASCADE,
    -- canonical   = 定妆照（全库唯一且仅一张）
    -- selfie      = 角色自拍（多为人物图，受一致性约束）
    -- scenery     = 风景/物品/抽象图（无人物，不要求参考图）
    -- user_upload = 用户手动放入相册的图
    role               TEXT NOT NULL
                       CHECK (role IN ('canonical','selfie','scenery','user_upload')),
    file_path          TEXT NOT NULL,          -- 相对 data/media/ 的路径
    mime_type          TEXT NOT NULL DEFAULT 'image/png',
    width              INTEGER NOT NULL DEFAULT 0,
    height             INTEGER NOT NULL DEFAULT 0,
    bytes              INTEGER NOT NULL DEFAULT 0,
    -- 生成溯源：没有这几列，定妆照就无法复现，一致性链条当场断掉
    provider_id        TEXT,
    model              TEXT,
    prompt             TEXT,       -- 实际发出的提示词（骨架 + 四槽位展开后的完整文本）
    negative_prompt    TEXT,
    seed               INTEGER,
    reference_asset_id TEXT REFERENCES media_asset(id) ON DELETE SET NULL,
    appearance_brief   TEXT,                   -- 仅 canonical：人工可编辑的外貌描述
    -- 四槽位原值，供重新生成同一构图时复用
    outfit             TEXT,
    scene              TEXT,
    mood               TEXT,
    lighting           TEXT,
    -- 可见性：未发布的图也必须可见（ADR-0008 的诚实要求）
    visibility         TEXT NOT NULL DEFAULT 'private'
                       CHECK (visibility IN ('private','posted','discarded')),
    cost_usd           REAL NOT NULL DEFAULT 0,
    tick_id            TEXT,
    correlation_id     TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

-- 定妆照全库唯一（部分唯一索引——只约束 role='canonical' 的行）。
-- 这里不能用「表级 UNIQUE(persona_id, role)」代替：那会限制
-- 同一个角色只能有一张自拍。
CREATE UNIQUE INDEX idx_media_canonical
    ON media_asset(persona_id) WHERE role = 'canonical';
CREATE INDEX idx_media_time ON media_asset(created_at DESC);
CREATE INDEX idx_media_role ON media_asset(persona_id, role, created_at DESC);
CREATE INDEX idx_media_visibility ON media_asset(visibility, created_at DESC);

-- ────────────────────────────────────────────────────────
-- 22. media_usage · 生图消耗明细
--
-- 为什么不塞进 llm_usage：那张表的语义是 **token 计量**，
-- 生图既无 prompt_tokens 也无 completion_tokens，
-- 混进去会让两边的聚合都变脏（平均值被零拉低、调用次数被高估）。
-- ────────────────────────────────────────────────────────
CREATE TABLE media_usage (
    id             TEXT PRIMARY KEY,
    persona_id     TEXT REFERENCES persona(id) ON DELETE SET NULL,
    asset_id       TEXT REFERENCES media_asset(id) ON DELETE SET NULL,
    tick_id        TEXT,
    purpose        TEXT NOT NULL,          -- selfie|scenery|canonical|regenerate
    provider_id    TEXT NOT NULL,
    model          TEXT NOT NULL,
    image_count    INTEGER NOT NULL DEFAULT 1,
    width          INTEGER NOT NULL DEFAULT 0,
    height         INTEGER NOT NULL DEFAULT 0,
    used_reference INTEGER NOT NULL DEFAULT 0,
    latency_ms     INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL NOT NULL DEFAULT 0,
    retry_count    INTEGER NOT NULL DEFAULT 0,
    success        INTEGER NOT NULL DEFAULT 1,
    error          TEXT,
    correlation_id TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX idx_media_usage_time ON media_usage(created_at DESC);
CREATE INDEX idx_media_usage_purpose ON media_usage(purpose, created_at DESC);

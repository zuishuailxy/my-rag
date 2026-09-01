-- ============================================================================
-- PostgreSQL schema 真相源
--
-- 本目录的 *.sql 是表结构的唯一权威定义（首次启动容器时由
-- docker-entrypoint-initdb.d 按文件名字母序执行：init.sql → 02_pgvector.sql）。
--
-- 修改约定：
--   1. 加字段 / 改类型 / 加索引 —— 先改这里的 DDL，再同步 backend/app/models/*.py
--   2. 应用启动的 Base.metadata.create_all 只做 CREATE TABLE IF NOT EXISTS 兜底，
--      不会 ALTER 已有表，因此「只改 ORM 模型」在新环境会静默不生效
--   3. 对已有库的变更需另写 ALTER 语句（幂等，可重复执行）
-- ============================================================================

-- 文档元数据表
CREATE TABLE IF NOT EXISTS kh_document (
    id BIGINT PRIMARY KEY,
    title VARCHAR NOT NULL,
    content_id VARCHAR NOT NULL UNIQUE,
    summary VARCHAR,
    category_id BIGINT,
    team_id BIGINT,
    author_id BIGINT,
    cover_image VARCHAR,
    tags VARCHAR,
    status SMALLINT NOT NULL DEFAULT 0,
    remark VARCHAR,
    view_count INT NOT NULL DEFAULT 0,
    like_count INT NOT NULL DEFAULT 0,
    comment_count INT NOT NULL DEFAULT 0,
    favourite_count INT NOT NULL DEFAULT 0,
    word_count INT NOT NULL DEFAULT 0,
    publish_time TIMESTAMP,
    is_public BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    create_by BIGINT,
    update_by BIGINT,
    deleted BOOLEAN NOT NULL DEFAULT false
);

-- 文档发布审核记录
-- 一次「提交审核」一行；approve/reject 后 review_result 非空，不再出现在待办列表
-- 状态流转：Draft(0) → PendingReview(3) → Published(1) → Archived(2)；驳回返回 Draft
-- 刻意不加外键：审核记录是留痕，文档删除后历史仍应可查
CREATE TABLE IF NOT EXISTS kh_document_review (
    id BIGINT PRIMARY KEY,                          -- 审核记录 ID（雪花）
    document_id BIGINT NOT NULL,                    -- 被审文档 ID → kh_document.id
    reviewer_id BIGINT,                             -- 审核人 ID；待审时为 NULL
    reviewer_name VARCHAR,                          -- 审核人姓名
    review_result SMALLINT,                         -- NULL=待审 1=通过 2=驳回
    review_comment VARCHAR,                         -- 审核意见（驳回必填）
    before_status SMALLINT NOT NULL,                -- 提审前文档 status（0 草稿 / 1 已发布）
    reviewed_at TIMESTAMP,                          -- 审核完成时间
    created_at TIMESTAMP NOT NULL DEFAULT NOW()     -- 提交审核时间
);

-- 按文档查审核历史
CREATE INDEX IF NOT EXISTS idx_kh_document_review_document_id
    ON kh_document_review(document_id);

-- 待办列表：仅 review_result IS NULL 的行
CREATE INDEX IF NOT EXISTS idx_kh_document_review_pending
    ON kh_document_review(review_result) WHERE review_result IS NULL;

-- ============================================================================
-- 用户与角色（第一期：简化注册，注册后立即可登录）
-- ============================================================================

CREATE TABLE IF NOT EXISTS kh_user (
    id BIGINT PRIMARY KEY,                          -- 用户 ID（雪花）
    username VARCHAR(50) NOT NULL,                  -- 登录用户名
    password VARCHAR(255) NOT NULL,                 -- 密码（bcrypt 哈希）
    email VARCHAR(100),                             -- 邮箱（可选）
    real_name VARCHAR(50),                          -- 真实姓名 / 显示名
    avatar VARCHAR(500),                            -- 头像 URL
    status SMALLINT NOT NULL DEFAULT 1,             -- 0 禁用 1 启用
    last_login_at TIMESTAMP,                        -- 最后登录时间
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),    -- 创建时间
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),    -- 更新时间
    deleted BOOLEAN NOT NULL DEFAULT false          -- 软删除标记
);

-- 未删除用户名唯一：软删账号不占用用户名，允许同名重新注册
CREATE UNIQUE INDEX IF NOT EXISTS uk_kh_user_username
    ON kh_user(username) WHERE deleted = false;

CREATE TABLE IF NOT EXISTS kh_role (
    id BIGINT PRIMARY KEY,                          -- 角色 ID（雪花）
    role_name VARCHAR(50) NOT NULL,                 -- 角色名称（展示用）
    role_code VARCHAR(50) NOT NULL UNIQUE,          -- 角色编码，如 ROLE_ADMIN / ROLE_REVIEWER / ROLE_USER
    description VARCHAR(200),                       -- 角色描述
    status SMALLINT NOT NULL DEFAULT 1              -- 0 禁用 1 启用
);

CREATE TABLE IF NOT EXISTS kh_user_role (
    id BIGINT PRIMARY KEY,                          -- 关联 ID（雪花）
    user_id BIGINT NOT NULL REFERENCES kh_user(id), -- 用户 ID
    role_id BIGINT NOT NULL REFERENCES kh_role(id), -- 角色 ID
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),    -- 分配时间
    CONSTRAINT uk_kh_user_role_user_role UNIQUE (user_id, role_id)  -- 同一用户不重复授予同一角色
);

CREATE INDEX IF NOT EXISTS idx_kh_user_role_user_id ON kh_user_role(user_id);

-- 预置角色（固定雪花 ID，便于种子数据与代码引用）
INSERT INTO kh_role (id, role_name, role_code, description) VALUES
    (2000000000000000001, '管理员', 'ROLE_ADMIN', '系统管理'),
    (2000000000000000002, '审核员', 'ROLE_REVIEWER', '文档审核'),
    (2000000000000000003, '普通用户', 'ROLE_USER', '默认角色')
ON CONFLICT (id) DO NOTHING;

-- 测试账号（密码均为 123456，bcrypt cost=10）
INSERT INTO kh_user (id, username, password, email, real_name, status) VALUES
    (1000000000000000001, 'admin', '$2a$10$N.zmdr9k7uOCQb376NoUnuTJ8iAt6Z5EHsM8lE9lBOsl7iKTVKIUi', 'admin@company.com', '系统管理员', 1),
    (1000000000000000002, 'reviewer', '$2a$10$N.zmdr9k7uOCQb376NoUnuTJ8iAt6Z5EHsM8lE9lBOsl7iKTVKIUi', 'reviewer@company.com', '审核员张三', 1),
    (1000000000000000003, 'user', '$2a$10$N.zmdr9k7uOCQb376NoUnuTJ8iAt6Z5EHsM8lE9lBOsl7iKTVKIUi', 'user@company.com', '普通用户李四', 1)
ON CONFLICT (id) DO NOTHING;

INSERT INTO kh_user_role (id, user_id, role_id) VALUES
    (3000000000000000001, 1000000000000000001, 2000000000000000001),  -- admin    → 管理员
    (3000000000000000002, 1000000000000000001, 2000000000000000002),  -- admin    → 审核员
    (3000000000000000003, 1000000000000000002, 2000000000000000002),  -- reviewer → 审核员
    (3000000000000000004, 1000000000000000003, 2000000000000000003)   -- user     → 普通用户
ON CONFLICT (id) DO NOTHING;

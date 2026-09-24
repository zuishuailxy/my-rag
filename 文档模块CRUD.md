# 文档模块 CRUD 实现说明

- 日期：2026-08-27
- 范围：文档（Document）（TypeORM + Mongoose）到 FastAPI（SQLAlchemy 2.0 + motor）的完整移植
- 状态：已通过真实数据库集成测试（docker 内 Postgres + MongoDB）

---

## 1. 背景与架构

，移植后保持一致：

| 存储 | 表 / 集合 | 内容 | 客户端 |
|---|---|---|---|
| PostgreSQL | `kh_document` | 文档元数据（标题、分类、状态、计数、时间戳等） | SQLAlchemy 2.0 + asyncpg |
| MongoDB | `document_content` | Markdown 正文（正文、摘要、版本号） | motor（pymongo 异步版） |

两库一对一关联：

```
kh_document.content_id ←→ document_content._id (ObjectId)
kh_document.id ←→ document_content.documentId（雪花 ID）
```

创建顺序：**生成雪花 ID → 先写 Mongo（拿 ObjectId）→ 再写 Postgres**；Postgres 失败时回滚物理删除已写入的 Mongo 正文，避免脏数据。

## 2. 文件清单（今日新增 / 修改）

| 文件 | 说明 |
|---|---|
| `backend/app/models/base.py` | SQLAlchemy `DeclarativeBase` 公共基类 |
| `backend/app/models/document.py` | `DocumentEntity`（kh_document）+ `DocumentStatus` 枚举 |
| `backend/app/models/document_content.py` | `DocumentContent`（Mongo 文档，Pydantic 模型） |
| `backend/app/common/snowflake.py` | 雪花 ID 生成器（线程安全） |
| `backend/app/db/session.py` | Postgres 异步引擎 / 会话工厂 / FastAPI 依赖 |
| `backend/app/db/mongo.py` | motor 客户端单例 + 集合获取 |
| `backend/app/services/document.py` | `DocumentService` 业务层 |
| `backend/app/schemas/document.py` | `DocumentCreate` / `DocumentQuery` / `DocumentUpdate` |
| `backend/app/api/v1/document.py` | 文档路由（Controller 层） |
| `backend/app/api/v1/__init__.py` | 路由装配（Module 层等价物） |
| `backend/app/core/config.py` | 新增 MongoDB 连接配置 |
| `backend/app/main.py` | lifespan：退出时关闭连接池 / Mongo 客户端 |
| `backend/scripts/smoke_document_service.py` | Service 层集成冒烟测试 |
| `backend/scripts/smoke_document_api.py` | HTTP 层 API 冒烟测试 |
| `backend/pyproject.toml` / `uv.lock` | 新增依赖：`motor`、`pymongo`、`greenlet`、`httpx`(dev) |
| `.env.example` | 新增 MongoDB 环境变量模板 |

## 3. 数据模型

### 3.1 状态枚举（双端共用一份）

`backend/app/models/document.py` 中定义，DTO 与实体共用：

| 枚举 | 值 | 含义 |
|---|---|---|
| `DRAFT` | 0 | 草稿 |
| `PUBLISHED` | 1 | 已发布 |
| `ARCHIVED` | 2 | 已归档（不参与知识检索） |

### 3.2 kh_document（Postgres）

与 `init-scripts/postgresql/init.sql` 逐列一致（已验证 DDL）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | BIGINT PK（非自增） | 雪花 ID，应用层生成 |
| `title` | VARCHAR NOT NULL | 标题 |
| `content_id` | VARCHAR UNIQUE | Mongo ObjectId 字符串 |
| `summary` | VARCHAR NULL | 摘要 |
| `category_id` / `team_id` / `author_id` | BIGINT NULL | 分类 / 团队 / 作者 |
| `cover_image` / `tags` / `remark` | VARCHAR NULL | 封面 / 标签 / 备注 |
| `status` | SMALLINT DEFAULT 0 | 文档状态 |
| `view_count` / `like_count` / `comment_count` / `favourite_count` / `word_count` | INT DEFAULT 0 | 计数（收藏数为英式拼写 favourite） |
| `publish_time` | TIMESTAMP NULL | 发布时间 |
| `is_public` | BOOLEAN DEFAULT false | 是否公开 |
| `created_at` / `updated_at` | TIMESTAMP DEFAULT now() | 创建 / 更新时间（update 自动刷新） |
| `create_by` / `update_by` | BIGINT NULL | 创建人 / 更新人 |
| `deleted` | BOOLEAN DEFAULT false | 逻辑删除 |

> 大整数统一为 Python `int`（任意精度）；API 输出时再转回字符串。

### 3.3 document_content（MongoDB）

`backend/app/models/document_content.py`，Pydantic 模型（snake_case 属性 + camelCase 别名对齐库内字段）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `_id` | ObjectId（自动生成） | 对应 kh_document.content_id |
| `documentId` | string 必填（库内唯一索引） | 对应 kh_document.id |
| `content` | string 默认 '' | Markdown 正文 |
| `contentLength` | int 默认 0 | 正文字符数 |
| `contentSummary` | string 默认 '' | 正文摘要 / 预览 |
| `version` | int 默认 1 | 版本号（正文变更 +1） |
| `deleted` | bool 默认 false | 逻辑删除 |
| `createdAt` / `updatedAt` | datetime | 对应 Mongoose `timestamps: true` |

## 4. DTO（Pydantic，对应 class-validator DTO）

| DTO |
|---|---|---|
| `DocumentCreate` | `CreateDocumentDto` | `title`/`content` 必填；其余可选；`status` 用统一枚举；ID 字段为字符串 + camelCase 别名 |
| `DocumentQuery` | `QueryDocumentDto` | `title` 模糊、分类/团队/作者/状态筛选；`page` 默认 1（ge=1）；`pageSize` 默认 20（1–100） |
| `DocumentUpdate` | `UpdateDocumentDto` = `PartialType(OmitType(Create, ['createBy']))` | 全部字段可选；去掉 `createBy`；新增 `updateBy` |

接口层接受 camelCase（`categoryId`/`pageSize`…）；`populate_by_name=True` 同时兼容 snake_case。

## 5. Service 业务逻辑（`DocumentService`）

### create

1. `next_snowflake_id()` 生成 id；`count_words()` 统计字数；未传 summary 时用 `build_content_summary()` 从正文生成预览
2. 写 Mongo（`_id` 自动生成 ObjectId）→ 得到 `content_id`
3. 写 Postgres 元数据（`content_id`、状态、`publish_time`（创建即发布时写入）、`create_by`/`update_by`）
4. Postgres 异常 → 回滚删除 Mongo 正文 → 重新抛出

### find_all

- 默认排除 `deleted = true`
- 条件：标题 `ILIKE %kw%`（不区分大小写）、分类 / 团队 / 作者精确匹配、状态匹配
- `created_at DESC` 排序，`(page-1)*pageSize` 偏移分页
- 返回 `{ items, total, page, pageSize }`（仅元数据，不含正文）

### find_one

- 按 id 查未软删文档，不存在 → 404
- 默认附带正文：`find_one({_id: content_id, deleted: false})`，缺失时正文为空串

### update（局部更新）

- 传了 `content`：Mongo `$set`（content / contentLength / contentSummary / updatedAt）+ `$inc version: 1`；`matchedCount = 0` → 400；重算 `word_count`
- 只传 `summary`：同步 Mongo `contentSummary`（version 不变）
- 元数据字段有传才覆盖（title / categoryId / teamId / authorId / coverImage / tags / remark / isPublic / updateBy）
- 状态从非发布 → 发布时首次写入 `publish_time`

### remove

双侧软删除：Postgres `deleted = true` + Mongo `deleted = true`（不物理删正文），返回 `{ id, deleted: true }`。

### 工具方法

- `count_words(content)`：CJK 汉字每字 1 + 拉丁文本按空白分词每词 1
- `build_content_summary(content, max_len=200)`：压缩连续空白后截断，超出追加 `...`

## 6. API 接口（前缀 `/api/v1`）

| 方法 | 路径 | 说明 | 成功状态码 |
|---|---|---|---|
| POST | `/api/v1/documents` | 创建文档 | 201 |
| GET | `/api/v1/documents` | 分页列表（query 参数见下） | 200 |
| GET | `/api/v1/documents/{id}` | 详情（含正文） | 200 |
| PATCH | `/api/v1/documents/{id}` | 局部更新 | 200 |
| DELETE | `/api/v1/documents/{id}` | 软删除 | 200 |

列表接口 query 参数：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `title` | string | - | 标题模糊 |
| `categoryId` / `teamId` / `authorId` | string | - | 精确筛选（ID 字符串） |
| `status` | int | - | 0 / 1 / 2 |
| `page` | int | 1 | 页码，≥1 |
| `pageSize` | int | 20 | 每页条数，1–100 |

### 请求 / 响应示例

创建：

```bash
curl -X POST http://localhost:8000/api/v1/documents \
 -H 'Content-Type: application/json' \
 -d '{
 "title": "部署指南",
 "content": "# 部署\n1. docker compose up -d\n2. 初始化数据库",
 "categoryId": "100",
 "authorId": "200",
 "status": 1,
 "createBy": "300"
 }'
```

```json
{
 "id": "748087797847429120",
 "title": "部署指南",
 "contentId": "66f2a1b2c3d4e5f6a7b8c9d0",
 "summary": null,
 "categoryId": "100",
 "teamId": null,
 "authorId": "200",
 "coverImage": null,
 "tags": null,
 "status": 1,
 "remark": null,
 "viewCount": 0,
 "likeCount": 0,
 "commentCount": 0,
 "favouriteCount": 0,
 "wordCount": 15,
 "publishTime": "2026-08-27T08:00:47.523536",
 "isPublic": false,
 "createdAt": "2026-08-27T08:00:47.523536",
 "updatedAt": "2026-08-27T08:00:47.523536",
 "createBy": "300",
 "updateBy": "300",
 "deleted": false,
 "content": "# 部署\n1. docker compose up -d\n2. 初始化数据库"
}
```

更新正文（版本号自动 +1）：

```bash
curl -X PATCH http://localhost:8000/api/v1/documents/748087797847429120 \
 -H 'Content-Type: application/json' \
 -d '{"content": "新的正文内容", "updateBy": "800"}'
```

## 7. 错误处理

| 场景 | 状态码 |
|---|---|
| 文档不存在 / 已软删 / id 非法或超出 bigint 范围 | 404 |
| 更新时 Mongo 正文缺失（matchedCount=0） | 400 |
| 筛选 ID 非法（如 `categoryId=abc` 或超 int64） | 400 |
| 参数校验失败（如 `pageSize=200`、`page=0`、`status=5`） | 422 |

## 8. 测试

两个冒烟脚本，均需 docker 的 postgres + mongodb 运行中，测试数据自动清理：

```bash
cd backend
PYTHONPATH=. .venv/bin/python scripts/smoke_document_service.py # Service 层
PYTHONPATH=. .venv/bin/python scripts/smoke_document_api.py # HTTP 层
```

覆盖点：

- Service 层：创建（字数 / 自动摘要 / 草稿无发布时间）→ 详情 → 列表筛选分页 → 更新正文（version 1→2、发布写 publishTime）→ 只改摘要（version 不变）→ 软删除 → 404
- API 层：201 / 200 / 404 / 422 全链路，camelCase 参数，lifespan 启停

## 9. 过程中发现并修复的问题

| 问题 | 现象 | 修复 |
|---|---|---|
| `favourite_count` 拼写不一致 | 模型 / 运行中 dev 库（旧 volume）为美式 `favorite_count`；dev 库执行 `ALTER TABLE ... RENAME COLUMN`；模型改回 |
| fastapi 0.110.1 不支持 query 参数模型 | `Annotated[Model, Query()]` 直接 AssertionError（0.115 才支持） | GET 端点显式声明 Query 参数，再组装 `DocumentQuery` |
| 超出 int64 的 id 导致 500 | Python `int()` 任意精度不抛错，超界发生在 asyncpg 绑定参数时 | `_parse_bigint()` 显式范围检查：路径 id → 404，筛选 id → 400 |
| 缺少 `greenlet` | SQLAlchemy 异步会话报 `No module named 'greenlet'` | `uv add greenlet`（`sqlalchemy[asyncio]` extra 的必需依赖） |
| Mongoose `timestamps: true` 无对应魔法 | Mongo 文档无自动 createdAt/updatedAt | Service 层统一 `_utcnow()` 手动维护 |

##

1. **Mongo 时间戳**：Mongoose 自动维护 `createdAt/updatedAt`，Python 侧在写库 helper 中显式赋值，保持语义一致。
2. **DTO 与实体的枚举统一**：`DocumentStatus` 只有一份（IntEnum，0/1/2），DTO 从模型导入，"字符串枚举 vs 数值枚举"的分裂。
3. **大整数序列化**：响应中所有 bigint 字段输出为字符串，键名 camelCase。
4. **`DocumentUpdate` 为显式定义**（Pydantic 无 `PartialType`），新增字段时需与 `DocumentCreate` 保持同步，文件内有注释提醒。
5. **dev 库与 `init.sql` 的一致性**：本次已统一 `favourite_count` 拼写；后续如再遇到新旧 volume 差异，以 `init-scripts/postgresql/init.sql` 为准，并对 dev 库执行对应 DDL。

## 11. 后续建议

- 用 Pydantic `DocumentResponse` + `response_model` 替代裸 dict 返回，完善 OpenAPI 文档
- 引入 Alembic 管理 Postgres 迁移（架构.md 已规划）
- 按原型图继续分类 / 团队 / 用户模块，复用 `get_db_session` / `get_document_service` 模式
- 两个冒烟脚本可演进为 pytest 测试套件（项目已配置 `[tool.pytest.ini_options]`）

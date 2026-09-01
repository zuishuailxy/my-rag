# my-rag 项目长期约定

TS(NestJS) → Python(FastAPI) 重写项目，逐模块对照 TS 源码核验，差异需文档化。

## 数据库 schema 管理
- **真相源：`init-scripts/postgresql/*.sql`**（字母序执行）。启动的 `create_all` 只建不 ALTER。
- 改字段/索引：先改 init.sql 的 DDL，再同步 `app/models/*.py`。只改 ORM 在新环境静默不生效。
- 验证：psql 在 `BEGIN/ROLLBACK` 内跑 SQL 验证语法且不留痕。`text()` 不支持多语句（需 `exec_driver_sql`）。

## 环境变量两层化
- 外层 `.env`：容器凭据/存储路径（docker-compose 用 `${VAR:-默认值}`）；`backend/.env`：应用配置（pydantic-settings 只读这一份）。改密码两处同步。

## 迁移对齐原则
- 常量名、消息 schema、命名与 TS 完全一致；分发逻辑在编排器而非消费者。
- 日志：`logging.getLogger("ClassName")`，`[LEVEL] [ClassName] 中文消息：key=value`。
- 向量存 PG（pgvector `doc_chunk`），ES 只做全文检索（`kh_document`）不存 dense_vector；IK 中文分词。
- 有意偏离 TS：RAG 删除时机 `chunk → embed → delete → index`（先建后删）。

## 文档状态与审核
- 状态 `0 Draft/1 Published/2 Archived/3 PendingReview`，判定集中在 `app/models/document_status.py`。
- 审核结果 `NULL=待审/1=通过/2=驳回`；`ReviewResult` 是 IntEnum 且**不含 NULL 成员**，待审由 `review_result IS NULL` 表达（partial index 依赖它）。
- `DOCUMENT_REQUIRE_APPROVAL` 严格等于 `"false"` 才免审（保真 TS 字符串判定）。
- 发布分叉：PendingReview 单独拦「文档审核中」→ 需审且 Draft/Published 走 `submit_for_review`（不建索引）→ 否则 `direct_publish`。**Archived 即使需审也走 direct_publish 直接上架**（TS 同款）。
- PATCH 不允许改 status（唯一例外 Published→Draft）。索引同步：下架清索引；仍 Published 改稿时仅免审模式重建；软删仅当 Published 才清索引。
- **审核人永远从 JWT 取**：`ReviewDecision` 只有 `reviewComment`（对齐 TS 注释「审核人信息从 JWT 取，不在 body 传」），`approve_review/reject_review` 的 `reviewer_id`/`reviewer_name` **必填**，由路由层传 `user.user_id` 与 `realName ?? username`。请求体夹带 reviewerId 会被 Pydantic `extra=ignore` 忽略 → 伪造不了审核身份。
- `DocumentService.create/update/upload_and_create_document` 末位可接收 `actor: AuthUser | None`：create 的 authorId/createBy、update 的 updateBy 在未显式传时回退 `actor.userId`。
- **create 的 updateBy 恒取 `actor.userId`**（不跟随 createBy）：TS 原文 `updateBy: dto.createBy ?? actor?.userId` 经用户确认为笔误（应为 `dto.updateBy ?? actor?.userId`），而 `CreateDocumentDto` **没有 updateBy 字段**（TS 里写 dto.updateBy 编译不过），该表达式退化为 `actor?.userId` → 不新增客户端可写字段，避免创建时伪造「最后修改人」。副作用（刻意）：导入传 createBy=原作者时，updateBy 记导入操作者。

## 路由层约定
- 状态码对齐 NestJS：`POST` → 201，其余 200。
- 含字面量前缀的路由（`/documents/reviews/tasks`）必须注册在 `/{doc_id}` 之前。
- 审核类接口返回「文档」而非「审核任务」，前端要 taskId 先调 `GET /{id}/reviews/current`。
- 实体→响应的 bigint 转字符串统一走 `services/document.py:document_to_dict()`。
- 冒烟脚本用「裸 FastAPI app（不挂 lifespan）+ `httpx.ASGITransport`」；**不要用 TestClient**（独立线程跑事件循环，与 asyncpg 冲突）。

## bigint ID 解析约定
- 唯一真相源 `app/common/bigint.py`，禁止在 service 写本地副本：
  - `parse_bigint(v)` 宽松→None，**只给路径参数用**，调用方抛 404。
  - `require_bigint(v, field)` 严格→400「Invalid <field>: <value>」，**请求体与查询参数一律用它**。
- 空串：请求体=清空字段（→None）；查询参数=不筛选（真值判断，勿用 `is not None`）。
- **校验必须前置**到任何副作用之前，否则出现「Mongo 已改而 PG 未提交」的两侧不一致。
- 刻意优于 TS：TS 只 `@IsString()`，非数字串撞 PG 22P02 变 500；Python 统一 400。别写裸 `int(dto.xxx)`。

## Swagger / DTO 展示约定
- **Swagger 的 Example Value 会把每个 property 填占位值**（string→`"string"`），与 required 无关；必填只看 OpenAPI 的 `required` 数组。
- 写操作 DTO 配 `json_schema_extra={"example": {...}}` **值全部合法**的示例（已加：`DocumentUpdate`、`ReviewDecision`、三个 auth DTO；未加：`DocumentCreate`/`UploadParse`）。陷阱字段在 description 写明限制。

## Neo4j / KG 图约定
- 图模型：`(KnowledgeDocument)-[:HAS_CHUNK]->(DocumentChunk)-[:MENTIONS]->(KnowledgeEntity)`，`(Entity)-[:RELATED_TO]->(Entity)`。
- **三个唯一性约束是正确性硬前提**（无约束时 MERGE 不原子，并发建图会重复，曾观测同一 docId 4 份）：`knowledge_document_id`/`document_chunk_chunk_id`/`knowledge_entity_name`。由 `GraphBuildService.start()` 用 `IF NOT EXISTS` 幂等建，失败只 WARN。
- **所有写操作走 `_run_write()`**：① 必须 `consume()`，否则报错被静默吞；② 内置 `TransientError` 重试（3 次退避）。建约束的 session 设 `notifications_min_severity=WARNING`（否则刷三屏 INFO）。
- 排查：`docker exec knowledge_hub_neo4j cypher-shell -u neo4j -p 12345678 --format plain "..."`；全清 `MATCH (n) DETACH DELETE n`。
- 沙箱：`ps` 不可用、RabbitMQ 15672 未映射。用户常并行跑自己的 uvicorn，清图后可能被写回，动手前采样两次确认。

## RBAC / 认证
**schema（真相源同在 init.sql）**：三表 `kh_user`/`kh_role`/`kh_user_role`；ORM 已与 TS 实体逐字段核验一致。
- 种子固定 ID：角色 `2000000000000000001~3`，用户 `1000000000000000001~3`（admin/reviewer/user）。**admin=ADMIN+REVIEWER，reviewer=REVIEWER，user=USER**。密码均为 `123456`（哈希 `$2a$10$N.zmdr9k7...`）。
- `uk_kh_user_username` 是 **partial unique（`WHERE deleted=false`）**——软删不占用户名，应用层判重要带 `deleted`。
- `kh_user_role` 两个 FK **无 CASCADE**：删用户**先删 kh_user_role 再删 kh_user**。用 `ON CONFLICT (user_id, role_id) DO NOTHING` 幂等授权。
- `kh_role` 无 `created_at/updated_at/deleted`（原文设计）。

**库选型**：bcrypt 5.0 直接用 + PyJWT 2.13 + email-validator（`EmailStr` 需要）。
- bcrypt 5.0：兼容老 `$2a$`、新哈希 `$2b$`、**密码 > 72 字节抛 ValueError**（4.x 静默截断）。
- PyJWT：密钥 < 32 字节告警。开发默认已改为 `dev-secret-change-me-please-use-32-bytes-random`（47 字节，刻意偏离 TS 的 20 字节默认值，已注释文档化）消除告警；生产必须配 JWT_SECRET 强随机密钥。

**代码结构**：`schemas/auth.py`（DTO + `AuthUser`/`LoginResult`）、`services/user.py`、`services/auth.py`（双令牌 HS256，payload `{sub,username,type,exp}`）、`common/roles.py`（`RoleCode` StrEnum）。
- 注册默认 `status=1` 立即可登录 + 自动授 USER 角色；refresh token **不落库无吊销**（与 TS 一致）。
- 刻意优于 TS：username/realName `max_length=50`（防 PG 22001）；password `max_length=72` 粗筛（**字符数非字节数**，服务层再按 UTF-8 字节校验：注册超限 400、登录超限 401）。

## 鉴权守卫（route_class 方案）
- 实现在 `app/api/auth_route.py`：`AuthRoute(APIRoute)` + `@public()`/`@roles(...)` + `auth_router()` 工厂 + `check_route_classes()` 启动自检。
- **不用中间件**：`BaseHTTPMiddleware` 运行在**路由匹配之前**，scope 只有 path（实测 `has_route=False`），读不到 `@Public()` 标记，只能退化成硬编码路径白名单（与路由定义分离易漂移）。route_class 是唯一能在「路由匹配后、依赖解析前」读 endpoint 元数据的扩展点，等价 NestJS `Reflector`。
- **默认全拦是刻意 fail-safe**：未标 `@public()` 的路由一律需登录，忘记标记 = 被保护。
- ⚠️ **FastAPI 0.110 的 `include_router` 不继承父 router 的 `route_class`**（内部 `route_class_override=type(route)` 只保留子路由自己的）→ 每个子 router 必须写 `auth_router()`；漏写整组失去保护，靠 `check_route_classes()` WARNING 兜底。
- `app/api/deps.py:get_current_user` 等价 `@CurrentUser()`，从 `request.state.user` 读不重复验签查库。**无 `require_roles` 依赖**（避免两套机制）。
- 文案对齐 TS guard：401「未登录或 token 已失效」、401「无效的 access token」、403「权限不足」。`build_auth_user` 抛的 404「用户不存在」/401「账户已禁用」**原样透出**（TS `throw err`，token 有效但用户已软删 → 404 而非 401）。
- 守卫跑在依赖解析前，拿不到 `Depends(get_db_session)`，需自建 session。
- 覆盖：`/`、`health`、auth 的 register/login/refresh 公开；`auth/me` 需登录；`auth/reviewer-ids` 需 ADMIN|REVIEWER；**documents 全部接口需登录**，其中 4 个审核接口额外 `@roles(REVIEWER, ADMIN)`：`GET /reviews/tasks`、`GET /reviews/tasks/pending-count`、`POST /reviews/tasks/{taskId}/approve|reject`。角色守卫跑在业务逻辑前（403 早于「该审核任务已处理」400）。
- `scripts/smoke_auth.py` 的 `bearer_headers()` 直接签 admin token（不走登录，解决 client 与 token 的鸡生蛋），历史脚本在构造 client 时挂 `headers=bearer_headers()` 一处搞定。已导出 `SEED_ADMIN_ID/REVIEWER_ID/USER_ID` 三个常量，403 用例用 `user`（仅 ROLE_USER）验证，httpx 单次请求的 `headers=` 会覆盖 client 默认头。

## 依赖管理约定
- **`.venv/bin` 里没有 pip**，venv 由 `uv` 创建 → 用 `/Users/magicyoung/.local/bin/uv add <pkg>`。
- **Dockerfile 走 `pip install -r requirements.txt`，不走 uv.lock** → `uv add` 后必须手工补 requirements.txt。
- 装包前快照三个文件到 /tmp，装后 diff + `uv lock --check` 确认没顶掉其他版本。
- ~~已知漂移（待修）~~ **已修复（2026-09-01）**：requirements.txt 四处对齐 pyproject——`neo4j>=6.3.0`（原 5.28，6.x API 在 5.x 运行时炸）、`boto3>=1.43.81`、`pymupdf>=1.28.2`、`aiohttp>=3.14.3`。对齐方向 = requirements 跟随 pyproject（本地冒烟在 pyproject 版本上验证）。**改依赖时两个文件要同步改**。

## 验证标准
真实依赖的 E2E 烟测 + `mypy app/` 全仓 0 issue。跑含 publish/approve 的脚本加 `RABBITMQ_ENABLED=false`（不触发 KG 消费者调 LLM）。
- 运行：`cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false uv run python scripts/xxx.py`。
- **冒烟脚本不得污染种子数据**：登录会写 `last_login_at`，改账号后必须恢复（含清 `last_login_at`）。
- `smoke_publish_document`/`smoke_index_pgvector` 自带免审夹具（`settings.DOCUMENT_REQUIRE_APPROVAL="false"`），它们测发布/索引链路而非审核。
- 断言别依赖脆弱前提（如「种子账号从未登录」）。

## 踩坑记录
- `with_structured_output(..., name=...)` 会把 name 透传给 `completions.create()` → TypeError。
- `deepseek-v4-flash` 默认 thinking 模式不支持强制 tool_choice → `ChatOpenAI(extra_body={"thinking": {"type": "disabled"}})`。
- SQLAlchemy `echo=True` 会在启动时把 `sqlalchemy.engine.Engine` 置 INFO → 压日志要在 import 后设 `engine.echo = False`。
- `app.get()` 不接受 `route_class` 参数（只有 `APIRouter` 支持）→ 根路由要单独建 router。

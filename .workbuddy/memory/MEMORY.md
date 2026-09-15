# my-rag 项目长期约定

TS(NestJS) → Python(FastAPI) 重写，逐文件对照 TS 源码核验；刻意差异必须注释文档化。实现细节见 .workbuddy/memory/ 每日日志。

## 环境 / Schema
- 真相源 `init-scripts/postgresql/*.sql`，⚠️ 在**仓库根**（不在 backend/ 下；在 backend grep 权限种子会误判「没有种子」）。改字段 = init.sql → `app/models/*.py` → 存量库手动 ALTER 三处同步。
- env 两层：外层 `.env` 容器凭据（compose `${VAR:-}`）；`backend/.env` 应用配置（pydantic-settings 只读它，未声明 key 静默忽略）。
- 依赖：venv 无 pip 用 `uv add`；Dockerfile 走 requirements.txt → 与 pyproject 必须同步改。

## 运行 / 验证标准
- `cd /abs/backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/xxx.py`（cwd 会漂移，必须带 cd）。
- 标准 = 真实依赖 E2E 冒烟 + `mypy app/` 0 issue；冒烟用「裸 FastAPI app（不挂 lifespan）+ `httpx.ASGITransport`」，**不要用 TestClient**（与 asyncpg 冲突）。
- 冒烟不得污染种子数据；断言不依赖脆弱前提（总行数、基址）；基址一律读 settings；临时用户清理先子表后主表，Redis 激活键 **token/user 两侧都要删**。

## 通用约定
- 状态码：写操作显式 201（auth/users/role/document/ai sessions）；**查询类 POST 刻意 200**（/search、/search/hybrid、/rag/search、/ai/chat，TS `@Post()` 默认 201，差异已注释文档化）。含字面量前缀的路由须注册在 `/{doc_id}` 之前。
- bigint 真相源 `app/common/bigint.py`：`parse_bigint` 给路径参数（404）、`require_bigint` 给 body/query（400）；空串 body=清空字段、query=不筛选；校验必须前置到任何副作用之前。实体→响应 bigint 一律转字符串。
- 文档状态 0Draft/1Published/2Archived/3PendingReview（判定集中 `app/models/document_status.py`）；审核结果 NULL=待审；`DOCUMENT_REQUIRE_APPROVAL` 严格 =="false" 才免审；PATCH 不许改 status（唯一例外 Published→Draft）；**Archived 即使需审也 direct_publish**。
- 审核人从 JWT 取（body 夹带 reviewerId 被 extra=ignore 丢弃）；create 的 updateBy **恒取 actor.userId**。

## 鉴权守卫（route_class 方案）
- `app/api/auth_route.py`：`AuthRoute` + `@public()`/`@roles(...)` + `auth_router()` 工厂 + `check_route_classes()` 启动自检。**默认全拦是刻意 fail-safe**。
- ⚠️ FastAPI `include_router` 不继承父 router 的 route_class → **每个子 router 必须写 `auth_router()`**，漏写整组失去保护。
- 三层 = ①AuthRoute 默认全拦+@public / ②@roles / ③require_permission。**①②在 handler 内先跑，③在 `original_route_handler` 之后才跑** → ②永远先于③拦截，故同时挂 `@roles(ADMIN)` 的 system:* 端点第三层被遮蔽（TS 同款）；`require_permission(*codes)` 是 **OR 语义**；document 16 端点是第三层真正生效的样板。
- 审计路由：路由级依赖里 `params.Depends` 属性是 **`dependency`** 不是 `call`；`check_route_classes` 挂在 lifespan，冒烟脚本跑不到。

## 检索可见性（2026-09-15，四条链路：chunk / 文档级 ES / KG / 文档列表与详情）
- 模型：公开 ∪ 本人 ∪ 所在团队，命中任一可读；ADMIN/REVIEWER 不受限。判定集中 `app/common/document_access.py`；ES 侧 `es_visibility_filter` 两索引共用，字段常量分开：`CHUNK_VISIBILITY_FIELDS`（snake_case）/ `DOC_VISIBILITY_FIELDS`（camelCase）；Neo4j 侧 `neo4j_access_params`（产出 `$unrestricted/$accessUserId/$accessTeamIds`）+ `neo4j_document_access_where(alias)`，KG 节点属性是 camelCase。
- **架构差异**：本项目向量在 PG doc_chunk（TS 在 ES）→ 向量通道靠 **JOIN kh_document** 实时过滤（改权限立即生效）；ES 关键词通道靠 chunk 冗余列 + 可见性 filter（相关性 must / 可见性 filter 不打分）；Neo4j 靠文档节点属性 + 沿 HAS_CHUNK/MENTIONS 回溯所属文档。
- ⚠️ **scope=None 一律 = 不过滤**（对齐 TS `scope?`）：`hybrid_search`/`search_documents`/`neo4j_access_params` 同此语义 → **API 端点必须显式传 scope，漏传即放开权限**。检索入口 `retrieval.retrieve(query, top_k, user)`，无 user → `empty_scope()` 仅公开。
- `AuthUser.team_ids` 由 `team_service.list_user_team_ids`（成员 ∪ 负责人）注入。
- ⚠️ **kh_document 的 `teamId` 必须显式声明 keyword**（动态映射会建成 text，`terms` 过滤按分词匹配 → 团队可见性静默失效）；存量由 `ensure_visibility_mapping()` 增量补（main lifespan + 回填脚本都调）。
- ⚠️ **Neo4j 的 `authorId`/`teamId` 一律存字符串**（`graph_build._opt_str`）：雪花 ID，且 scope 侧本就是 str，类型不一致 → `d.teamId IN $accessTeamIds` 静默不命中（表现为团队可见的文档查不到，且不报错）。
- **文档列表 / 详情是 PG 实时判定**（第四链路）：`document.find_all(session, query, scope)` 把可见性拼成 SQL 条件（`author_id=我 OR (status=已发布 AND (is_public OR team_id IN 我的团队))`，与筛选条件 AND）；「作者」分支**不限 status**（自己草稿/待审/归档要可见）。`find_one(..., scope)` 不可读抛 403「无权查看该文档」。无索引、无延迟，改权限立即生效。
- 改可见性必须**三处同时**同步（`document._sync_pipeline_after_update` 的 `visibility_changed` 分支）：ES kh_chunk + ES kh_document + Neo4j 文档节点；漏任一 = 「公开转私有仍被检索到 / 仍出现在图谱」。（列表/详情链路不受影响，走 PG。）
- 字段映射单一事实源 `pipeline/types.py:to_search_index_doc`；`/search` 的 item 含 `teamId`/`isPublic`，改 items 映射时 schema 同步加字段。
- 回填：ES 侧 `scripts/reindex_all_chunks.py`（⚠️ 不挂 lifespan，必须显式 `await es_service.start()`，否则 ES 写入被静默跳过）；KG 侧 `scripts/backfill_kg_visibility.py`（只刷三个属性、不重跑抽取、零 token，`--dry-run` 可预览）。
- 冒烟 `scripts/smoke_document_visibility.py`（25 断言，列表+详情，PG 实时链路）、`scripts/smoke_retrieval_visibility.py`（18 断言）、`scripts/smoke_graph_visibility.py`（30 断言，含写入侧 `build_for_document` 与 `update_visibility` 即时生效）。未覆盖：前端展示。

## 已闭环模块
- RBAC：三表 kh_user/kh_role/kh_user_role；种子角色 `2000000000000000001~3`、用户 `1000000000000000001~3`（admin=ADMIN+REVIEWER/reviewer/user，密码 123456）；权限 ID `40000000000000000xx`（search=3, document:list=11, create=12, edit=13, delete=14, review=15）；`kh_role_permission` 共 **11 行** —— **加权限层前必须先补种子，否则普通用户全线 403**（存量用 `seed_role_permission_matrix.py` 幂等补）；`uk_kh_user_username` 是 partial unique（WHERE deleted=false）；kh_user_role 两 FK 无 CASCADE → 删用户先删子表。
- role：每端点 @roles(ADMIN)、`/roles/list` 注册在 `/{role_id}` 前、delete 是硬删除、有关联用户 400、roleCode 查重 409、非法 id 统一 404。
- team：同码不查重 201、create 不校验 leader/parent 存在、delete 软删+成员硬删、page 排序 created_at **DESC**、tree 公开（@Public）。
- auth：bcrypt 5.0（>72 字节抛错；注册 400/登录 401/reset 400）、PyJWT、JWT_SECRET 默认 47 字节（刻意偏离 TS，生产必须配强随机）；注册 status=1 立即可登录；`REQUIRE_EMAIL_VERIFICATION=true` 且带邮箱 → status=0 发激活邮件；`password:reset:code:{email小写}` TTL 600s；**Redis 客户端必须 `decode_responses=True`**；logout 需登录、verify-email 的 token query 必填（刻意优于 TS）；未激活登录报「账户已禁用」（TS 死分支保真）。
- search/graph：`/search`、`/search/hybrid`、graph 全 **4 端点**（overview/search/nodes/edges）均挂 `require_permission("search")` —— TS graph.controller.ts 是**类级** @RequirePermission(search)（2026-09-07 同步收紧，此前「仅登录」的对齐已作废）；Neo4j driver 6.x 用原生 `int` 传 limit。

## 踩坑速查
- redis-py 默认返回 bytes；EmailStr 拒收 `.test` 等保留域（冒烟邮箱用 example.com）。
- `with_structured_output(..., name=...)` TypeError；deepseek-v4-flash 需 `extra_body={"thinking":{"type":"disabled"}}`。
- SQLAlchemy `echo=True` 压日志：import 后 `engine.echo = False`；`app.get()` 不接受 route_class。
- **`onupdate=func.now()` 列：commit 后再访问属性 → MissingGreenlet，必须 `await session.refresh(实体)`**；**TS `@UpdateDateColumn` → Python 必须同时写 `server_default=func.now()` + `onupdate=func.now()`**（init.sql 无触发器，`now()` 只在 INSERT 生效）。样板：document.py / user.py / doc_chunk.py / team.py。
- **PG `now()` = 事务开始时间** → 同一事务内多次 UPDATE，`updated_at` 不前进；测 onupdate 必须开两个 session。
- 冒烟脚本 main + cleanup 必须同一 asyncio.run 事件循环（engine 连接池绑定首个 loop）。
- elasticsearch-py 8.x `search()` 关键字是 **`source=`** / **`from_=`**（不是 `_source=`，否则 mypy 报 Unexpected keyword argument）。
- 冒烟里 `delete()/sa_delete()` **必须 `.commit()`**，否则回滚留残留行。
- ⚠️ **同一文件在一条消息里发多个 Edit，常只有部分（甚至只有最后一条）落盘** → 「一条消息一个 Edit + 立即 grep 复验」。
- macOS grep 不支持 `\|`，一律 `grep -E`（`\|` 匹配 0 且 exit 1，会让 `&&` 链静默中断）。
- 沙箱偶发 `Sensitive content protection failed`（拦截 `python -c "import ..."`），py_compile / mypy / 冒烟正常 → 抖动，非代码问题。

## 待办
- ⚠️ **写权限未移植**：TS `document-access.ts` 的 `canWriteDocument`（ADMIN 或作者本人）+ `DocumentService.assertWritable` 在 Python 侧**完全不存在** → 只要持有 `document:edit`/`document:delete`（ROLE_USER 种子就有）就能改/删**他人**文档，update/publish/archive/save-draft/remove 全无归属校验。已上报用户待拍板（勿把审核链路一起加进去：`approve_review` 自行置 Published + `_safe_publish`，不经 `direct_publish`，由 `@roles(REVIEWER/ADMIN)` + `document:review` 把关；`direct_publish` 唯一调用方是 `publish()`，只带 `actor` 回填 `updateBy`）。
- `document-review.service.ts` 的 actor 透传待逐文件核验；`POST /documents/{id}/index` 是 Python 独有端点，维持仅登录、不发明权限码。
- 前端检索 / 图谱结果展示未核（后端已按 scope 过滤，前端无需再过滤）。
- KG 写入侧与查询侧可见性均已核（2026-09-15）；再有历史图谱数据用 `scripts/backfill_kg_visibility.py` 刷属性。

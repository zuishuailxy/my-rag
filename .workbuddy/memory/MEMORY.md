# my-rag 项目长期约定

TS(NestJS) → Python(FastAPI) 重写，逐文件对照 TS 源码核验（贴源后转写，不靠 DTO 推断）；刻意差异必须注释文档化。细节见 .workbuddy/memory/ 每日日志。

## 环境 / Schema
- 真相源 `init-scripts/postgresql/*.sql`，⚠️ 在**仓库根**（不在 backend/ 下）。改字段 = init.sql → `app/models/*.py` → 存量库手动 ALTER，三处同步。
- env 两层：外层 `.env` 给 compose（`${VAR:-}`）；`backend/.env` 给应用（pydantic-settings 只读它，未声明 key 静默忽略）。
- 依赖：venv 无 pip 用 `uv add`；Dockerfile 走 requirements.txt → 与 pyproject 同步改。
- Docker Desktop 退出后 `open -a Docker` 重启，容器自动恢复；⚠️ **ES/Neo4j 需等就绪**（端口先通但 `_cluster/health` 无响应 / 分片 red→yellow 才可用），早跑冒烟会误报「客户端不可用」。

## 运行 / 验证
- `cd /abs/backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/xxx.py`（cwd 会漂移，必须带 cd）。
- 标准 = 真实依赖 E2E 冒烟 + `mypy app/` 0 issue。冒烟用「裸 app（不挂 lifespan）+ `httpx.ASGITransport`」，**不要 TestClient**（与 asyncpg 冲突）。
- 冒烟不得污染种子数据；别依赖脆弱前提（总行数、基址）；基址读 settings；临时用户清理先子表后主表，Redis 激活键 **token/user 两侧都删**；`scripts/` 不在标准范围（既有 mypy 报错可忽略，别新增）。

## 通用约定
- 状态码：写操作显式 201；**查询类 POST 刻意 200**（/search、/search/hybrid、/rag/search、/ai/chat；TS `@Post()` 默认 201，差异已注释）。含字面量前缀的路由注册在 `/{doc_id}` 之前。
- bigint 真相源 `app/common/bigint.py`：`parse_bigint` 路径参数(404) / `require_bigint` body+query(400)；空串 body=清空字段、query=不筛选；校验前置到副作用之前。实体→响应 bigint 转字符串。
- 状态 0Draft/1Published/2Archived/3PendingReview，判定集中 `app/models/document_status.py`（与 TS 零缺口）；审核结果 NULL=待审；`DOCUMENT_REQUIRE_APPROVAL` 严格 =="false" 才免审；PATCH 不许改 status（唯一例外 Published→Draft）；**Archived 即使需审也 direct_publish**；审核人从 JWT 取（body 夹带 reviewerId 被 extra=ignore 丢弃）。

## 鉴权守卫（route_class）
- `app/api/auth_route.py`：`AuthRoute` + `@public()`/`@roles(...)` + `auth_router()` + `check_route_classes()` 自检。**默认全拦是刻意 fail-safe**。
- ⚠️ `include_router` 不继承父 router 的 route_class → **每个子 router 必须写 `auth_router()`**，漏写整组失去保护。
- 三层 = ①默认全拦+@public / ②@roles / ③require_permission；**①②在 handler 内先跑，③在 `original_route_handler` 之后** → ②永远先于③，同挂 `@roles(ADMIN)` 的 system:* 端点第三层被遮蔽（TS 同款）。`require_permission(*codes)` 是 **OR 语义**；document 16 端点是第三层生效样板。审计路由依赖取 `params.Depends` 的 **`dependency`** 属性（不是 `call`）；`check_route_classes` 挂 lifespan，冒烟跑不到。

## 可见性（读侧四链路）
- 模型：公开 ∪ 本人 ∪ 所在团队，命中任一可读；ADMIN/REVIEWER 不受限。判定集中 `app/common/document_access.py`。ES `es_visibility_filter` 两索引共用，字段常量分开：`CHUNK_VISIBILITY_FIELDS`(snake_case)/`DOC_VISIBILITY_FIELDS`(camelCase)；Neo4j `neo4j_access_params`（产 `$unrestricted/$accessUserId/$accessTeamIds`）+ `neo4j_document_access_where(alias)`，KG 节点属性 camelCase。
- **架构差异**：向量在 PG doc_chunk（TS 在 ES）→ 向量通道靠 **JOIN kh_document** 实时过滤；ES 关键词通道靠 chunk 冗余列 + 可见性 filter（相关性 must / 可见性 filter 不打分）；Neo4j 靠文档节点属性 + 沿 HAS_CHUNK/MENTIONS 回溯所属文档。
- ⚠️ **`scope=None` 一律 = 不过滤**（对齐 TS `scope?`）：`hybrid_search`/`search_documents`/`neo4j_access_params` 同此语义 → **API 端点必须显式传 scope，漏传即放开权限**。`retrieval.retrieve(query, top_k, user)`，无 user → `empty_scope()` 仅公开。`AuthUser.team_ids` 由 `team_service.list_user_team_ids`（成员 ∪ 负责人）注入；⚠️ 该函数必须**再按 `kh_team.deleted=false` 过滤**（TS `listAccessibleTeamIds` 第 3 步）——成员记录可能仍指向已软删团队，不过滤 = 已删团队继续授予文档可见性（2026-09-15 修复，`smoke_team_api` 25/26 锁死）。
- ⚠️ **kh_document 的 `teamId` 必须显式 keyword**：动态映射会建成 text → `terms` 按分词匹配 → 团队可见性静默失效；存量由 `ensure_visibility_mapping()` 补（main lifespan + 回填脚本都调）。
- ⚠️ **Neo4j `authorId`/`teamId` 一律存字符串**（`_opt_str`）：雪花 ID + scope 侧本就是 str，类型不一致 → `d.teamId IN $accessTeamIds` 静默不命中（团队可见文档查不到且不报错）。
- **列表/详情是 PG 实时判定**：`find_all(session, query, scope)` 拼 SQL（`author_id=我 OR (status=已发布 AND (is_public OR team_id IN 我的团队))`，与筛选 AND）；「作者」分支**不限 status**（自己草稿/待审/归档可见）。`find_one(..., scope)` 不可读抛 403「无权查看该文档」。无索引无延迟。
- 改可见性必须**三处同时**同步（`_sync_pipeline_after_update` 的 `visibility_changed` 分支）：ES kh_chunk + ES kh_document + Neo4j 文档节点；漏任一 = 「公开转私有仍被检索到 / 仍出现在图谱」。
- `search_index.update_visibility` 用**单条 `client.update`**；⚠️ 勿退回 `update_by_query`——后者在「文档没进索引」时 `updated=0` 静默返回 True，把缺口伪装成成功。单条 update 抛 `document_missing_exception` → WARN + False（`smoke_search_index` 2b/2c/2d 锁死）。
- 映射单一事实源 `pipeline/types.py:to_search_index_doc`；`/search` item 含 `teamId`/`isPublic`，改 items 映射时同步加 schema 字段。回填：ES `reindex_all_chunks.py`（⚠️ 不挂 lifespan，必须显式 `await es_service.start()`，否则写入被静默跳过）；KG `backfill_kg_visibility.py`（只刷属性、零 token，`--dry-run`）。
- 冒烟：`smoke_document_visibility`(25) / `smoke_retrieval_visibility`(18) / `smoke_graph_visibility`(30)。前端展示未覆盖。

## 写权限（2026-09-15 完成）
- `can_write_document(doc, user: AuthUser)`：**只认 ADMIN + 作者本人，REVIEWER 不在白名单**（读侧 `unrestricted` 把 REVIEWER 一并放开、分辨不出 ADMIN，故签名收 **`AuthUser`** 而非 scope）。
- `_assert_writable` → 403「无权修改该文档」，在 update / remove / publish / archive / save_as_draft 五处「取实体之后、状态校验之前」调用（与 TS `assertWritable` 同位 → 草稿归档拿 403 而非「只有已发布可以归档」400）。五处 `actor` **必传**，mypy 强制所有调用点过校验。
- `submit_for_review(session, doc_id, actor: AuthUser | None = None)`：403「无权提交该文档审核」，排在 `can_submit_review` **之前**。⚠️ **TS 这里 actor 可选**（`if (actor && …)` 短路），勿收紧；端点 `POST /{id}/reviews/submit` 必须取用户，否则校验恒被跳过。
- `direct_publish(actor?)` 可选（TS 同）；**approve/reject 无写权校验是刻意的**（TS 也没有，由 `@roles(REVIEWER,ADMIN)` + `document:review` 把关，勿误加）。
- `create` / `upload_and_create_document` 的 actor **必传**；`create_by`、`author_id`、`update_by` **恒取 actor.user_id**（TS `createBy: actor.userId`），DTO 同名字段被忽略 → 客户端不能伪造归属/审计人。`update` 同样不读 `dto.author_id`/`dto.update_by`；`visibility_changed` 只比 `is_public`/`team_id`。
- ⚠️ 收紧这几处会连累冒烟：`smoke_document_api`（createBy/updateBy 断言）、`smoke_document_bigint_validation`（原拿 authorId/createBy/updateBy 当 bigint 用例，已换仍生效字段）、`smoke_document_service`。写权新断言：`smoke_document_publish` D 段(8) + `smoke_document_review` 第 11 段(5)。

## 已闭环模块（逐项核验过程见每日日志）
- RBAC 种子：角色 `2000000000000000001~3`、用户 `1000000000000000001~3`（admin=ADMIN+REVIEWER / reviewer / user，密码 123456），权限 ID `40000000000000000xx`；`kh_role_permission` **11 行**——**加权限层前必须先补种子，否则普通用户全线 403**（补法：`seed_role_permission_matrix.py` 幂等刷）；`uk_kh_user_username` 是 partial unique（WHERE deleted=false）；kh_user_role 两 FK 无 CASCADE → 删用户先删子表。
- 守卫口径：管理端点 = `@roles(ADMIN)` + `require_permission("system:xxx")`；`/search`、`/search/hybrid`、graph 4 端点挂 `require_permission("search")`（TS graph 是**类级**，09-07 收紧）；document 16 端点权限码已逐条核对 `document.controller.ts`（审核 4 接口=Roles(REVIEWER,ADMIN)+p:review，其余写接口=p:edit/p:delete），`POST /documents/{id}/index` 是 Python 独有端点、仅登录。
- 例外 / 保真点：**`GET /teams/mine` 登录即可**（TS 无 @Roles/无权限码）；**`GET /teams/tree` 是 ADMIN + system:team**（09-15 订正：早期误判「TS 标了 @Public」做成公开）；team 同码不查重 201、create 不校验 leader/parent 存在、delete 软删+成员硬删、page 排序 created_at **DESC**；role 列表路由在 `/{id}` 前、delete 硬删、有用户 400、查重 409；auth `JWT_SECRET` 默认 47 字节（刻意偏离 TS，生产须配强随机）、未激活登录报「账户已禁用」（TS 死分支保真）、**Redis 客户端必须 `decode_responses=True`**；Neo4j driver 6.x 用原生 `int` 传 limit。

## 踩坑速查
- redis-py 默认 bytes；EmailStr 拒收 `.test`（冒烟用 example.com）。
- `with_structured_output(..., name=...)` TypeError；deepseek-v4-flash 需 `extra_body={"thinking":{"type":"disabled"}}`。
- **`onupdate=func.now()` 列 commit 后再访问属性 → MissingGreenlet，必须 `await session.refresh(实体)`**；TS `@UpdateDateColumn` → Python 必须同写 `server_default`+`onupdate`（init.sql 无触发器）。
- **PG `now()` = 事务开始时间** → 同事务多次 UPDATE `updated_at` 不前进；测 onupdate 开两个 session。
- 冒烟 main + cleanup 必须同一 asyncio.run 事件循环（engine 连接池绑首个 loop）；`delete()/sa_delete()` **必须 `.commit()`**，否则回滚留残留行。
- ⚠️ 冒烟断言别用**全局总行数**（`kh_team==2`、`len(kh_role)==3`、计数式「种子还原」）：库里有 09-03 手工测试残留（团队「前端开发组」×2、`ROLE_TEST`、xiao 的成员关联），必误报。口径 = 「按固定 ID 断言种子行仍在 + 本次临时行已清空」。
- elasticsearch-py 8.x `search()` 关键字是 **`source=`/`from_=`**（不是 `_source=`，否则 mypy 报错）；`app.get()` 不接受 route_class。
- ⚠️ **同一文件一条消息发多个 Edit，常只落最后一条** → 「一条消息一个 Edit + 立即 grep 复验」。
- macOS grep 不支持 `\|`，一律 `grep -E`（否则匹配 0 且 exit 1，静默中断 `&&` 链）。
- git：同文件混语义用 `git apply --cached` 按 hunk 拆（比「临时还原文件」安全）；拆前 `git diff --cached` 核验，拆后 `git show :<path> > /tmp/x.py && py_compile` 验索引态语法。

## 待办
- 前端检索 / 图谱 / 列表展示未调（后端已按 scope 过滤，前端无需再过滤）。
- 历史图谱数据用 `scripts/backfill_kg_visibility.py` 刷可见性属性。
- 未入库：仓库根 `简历素材-my-rag.md`（待用户确认）。

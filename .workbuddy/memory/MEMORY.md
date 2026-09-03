# my-rag 项目长期约定

TS(NestJS) → Python(FastAPI) 重写，逐文件对照 TS 源码核验；刻意差异必须注释文档化。历史细节见 .workbuddy/memory/ 每日日志。

## Schema / 环境
- 真相源 `init-scripts/postgresql/*.sql`；改字段/索引 = init.sql → `app/models/*.py` → 已有库手动 ALTER 三处同步。
- env 两层：外层 `.env` 容器凭据（compose `${VAR:-}`）；`backend/.env` 应用配置（pydantic-settings 只读它）。pydantic-settings 对未声明 key 静默忽略。
- 依赖：venv 无 pip，用 `uv add`；Dockerfile 走 requirements.txt → pyproject 与 requirements 必须同步改。

## 路由 / 校验
- 状态码对齐 NestJS：POST→201 其余 200；含字面量前缀的路由须注册在 `/{doc_id}` 之前。
- bigint 唯一真相源 `app/common/bigint.py`：`parse_bigint` 只给路径参数（404）；`require_bigint` 给请求体/查询参数（400）。空串：body=清空字段、query=不筛选（真值判断）。校验必须前置到任何副作用之前。
- 写操作 DTO 配 `json_schema_extra` **值全部合法**的示例；Swagger 自动给每个 property 填占位值，required 只看 OpenAPI required 数组。
- 实体→响应 bigint 转字符串统一 `services/document.py:document_to_dict()`。

## 文档状态与审核
- 状态 0Draft/1Published/2Archived/3PendingReview，判定集中 `app/models/document_status.py`；审核结果 NULL=待审（`ReviewResult` IntEnum 不含 NULL）。`DOCUMENT_REQUIRE_APPROVAL` 严格=="false" 才免审。
- 发布分叉：PendingReview 单拦；需审且 Draft/Published 走 submit_for_review；**Archived 即使需审也 direct_publish**（TS 同款）。PATCH 不许改 status（唯一例外 Published→Draft）。
- **审核人从 JWT 取**：`ReviewDecision` 只有 reviewComment；body 夹带 reviewerId 被 Pydantic extra=ignore 忽略。
- create 的 updateBy **恒取 actor.userId**（TS `dto.createBy ?? actor?.userId` 系笔误，用户拍板不加字段）；actor 未显式传时 createBy/updateBy 回退 actor.userId。

## RBAC / 邮箱验证
- 三表 kh_user/kh_role/kh_user_role；种子 ID：角色 `2000000000000000001~3`、用户 `1000000000000000001~3`（admin=ADMIN+REVIEWER/reviewer/user），密码 123456。`uk_kh_user_username` 是 partial unique（WHERE deleted=false）。kh_user_role 两个 FK 无 CASCADE → 删用户先删子表。
- bcrypt 5.0（>72 字节抛 ValueError；服务层按 UTF-8 字节校验：注册 400/登录 401/reset 400）+ PyJWT 2.13 + email-validator；JWT_SECRET 默认 47 字节（刻意偏离 TS，生产必须配强随机）。
- 注册默认 status=1 立即可登录 + 授 USER 角色；`REQUIRE_EMAIL_VERIFICATION=true` 且带邮箱 → status=0+email_verified=0 发激活邮件（失败补偿删 token 抛 400）；激活幂等双字段恢复。基建：`services/email.py`（aiosmtplib；multipart 取内容必须 `iter_parts()`）、`email_activation.py`（双 key 令牌）、`password_reset.py`（`password:reset:code:{email小写}`，TTL 600s/冷却 60s，verify 只比对不删，删除由调用方负责）。**Redis 客户端必须 `decode_responses=True`**。
- TS 保真 quirk：未激活登录报「账户已禁用」（validateCredentials 的 status 检查先于 email_verified，死分支保留）。
- auth 路由已全接线（2026-09-02）：register/login/refresh/verify-email/password/reset/send-code/password/reset 均 @Public，logout **需登录**（TS 未标 @Public）；verify-email 的 token query 必填（缺失 422，刻意优于 TS）。

## role 模块（2026-09-02 完成）
- `services/role.py` + `api/v1/role.py` + `schemas/role.py`：TS 类级 @Roles(ADMIN) → 每端点 @roles(ADMIN)；`/roles/list` 必须注册在 `/{role_id}` 前；delete 是**硬删除**（kh_role 无 deleted 列），有关联用户 400「角色仍有关联用户，无法删除」（count 不筛 deleted，TS 同款）；roleCode 查重 409；路径 id 经 parse_bigint，非法值统一 404「角色不存在」。响应分 `RoleBriefResponse`（无 status，list/create）/`RoleDetailResponse`（有 status，get/update），对齐 TS 各方法内联对象。

## 权限种子矩阵（kh_permission / kh_role_permission）
- 权限 ID 段 `40000000000000000xx`；关键码：search=3, document:list=11, create=12, edit=13, delete=14, review=15。角色 ID：ADMIN=…001 / REVIEWER=…002 / USER=…003。
- `kh_role_permission` 完整 **11 行**（2026-09-03 由 8 行补齐）：USER 增 edit(13)/delete(14)、REVIEWER 增 search(3)。**加权限层前必须先补种子，否则普通用户全线 403**。
- 存量库同步用 `scripts/seed_role_permission_matrix.py`（幂等 ON CONFLICT DO NOTHING），已对 dev 库跑过 8→11。
- ⚠️ `init-scripts/` 在**仓库根**，不在 `backend/` 下 —— 在 backend 里 grep 权限种子会得到空结果并误判「没有种子」。

## search / graph 查询模块（2026-09-03）
- `POST /api/v1/search`（ES 全文 + 高亮）挂 `require_permission("search")`；种子里 USER 已有 search 权限码。
- `GET /api/v1/graph/{search,nodes,edges}`（Neo4j）**只需登录、不挂权限码** —— TS graph.controller.ts 未标注 @RequirePermission，忠实保真。
- graph 服务 driver 为 None 或异常时统一返回空列表；Neo4j driver 6.x 用原生 `int` 传 limit，`await result.data()` 取 list[dict]。

## 鉴权守卫（route_class 方案）
- `app/api/auth_route.py`：`AuthRoute` + `@public()`/`@roles(...)` + `auth_router()` 工厂 + `check_route_classes()` 启动自检。不用中间件（路由匹配前读不到 endpoint 元数据）。**默认全拦是刻意 fail-safe**。
- ⚠️ FastAPI `include_router` 不继承父 router 的 route_class → **每个子 router 必须写 `auth_router()`**，漏写整组失去保护。
- `deps.py:get_current_user` 等价 @CurrentUser()；404「用户不存在」/401「账户已禁用」原样透出；documents 4 个审核接口 `@roles(REVIEWER, ADMIN)`（403 早于业务 400）。
- 三层守卫 = JwtAuthGuard(AuthRoute 默认全拦+@public) / RolesGuard(@roles) / PermissionsGuard(require_permission 依赖)。**执行顺序：①②在 AuthRoute handler 内顺序执行，③属依赖系统，在 `original_route_handler` 之后才跑** → ②永远先于③拦截。
- ⚠️ **管理类端点第三层被第二层遮蔽**：挂 require_permission 的 12 条 system:* 端点同时有 `@roles(ADMIN)`，非管理员在②就 403、管理员自动继承 ADMIN_OPERATION_PERMISSIONS 必然放行，故③不独立拒绝（实测直授 system:user 给 ROLE_USER 仍 403）。**TS 同款设计，忠实移植**。若要让非管理员凭权限码访问，必须先摘掉 `@roles(ADMIN)`。
- **document 16 端点（2026-09-03）是第三层真正生效的样板**：不带 `@roles(ADMIN)`，纯凭权限码放行 → USER 凭 document:edit/delete 通过、REVIEWER 缺 document:create 被 403。`require_permission(*codes)` 是 **OR 语义**（任一即通过），故双码端点 reviews/current、reviews/history 只要有 document:list 即可访问。
- 审计路由守卫：`route.dependencies`（路由级依赖）里 `params.Depends` 的属性是 **`dependency`** 不是 `call`；`check_route_classes` 挂在 lifespan，冒烟脚本不挂 lifespan → 自检跑不到。

## 验证标准
- 真实依赖 E2E 冒烟 + `mypy app/` 0 issue；冒烟用「裸 FastAPI app（不挂 lifespan）+ `httpx.ASGITransport`」，**不要用 TestClient**（与 asyncpg 冲突）。
- 运行：`cd /abs/backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/xxx.py`（Bash cwd 会漂移，必须带 cd）。
- 冒烟不得污染种子数据（登录写 last_login_at，改账号须恢复）；断言不依赖脆弱前提（总行数、基址等）；基址一律读 settings；临时用户清理先子表后主表，Redis 激活键 **token/user 两侧都要删**。

## 踩坑速查
- redis-py 默认返回 bytes；EmailStr 拒收 `.test` 等保留域（冒烟邮箱用 example.com）。
- `with_structured_output(..., name=...)` TypeError；deepseek-v4-flash 需 `extra_body={"thinking":{"type":"disabled"}}`。
- SQLAlchemy `echo=True` 压日志：import 后 `engine.echo = False`；`app.get()` 不接受 route_class（只有 APIRouter 支持）。
- **服务端 `onupdate=func.now()` 列：UPDATE 后 commit 再访问属性 → MissingGreenlet（asyncpg）；commit 后必须 `await session.refresh(实体)`**。expire_on_commit=False 免不了 flush 期 server-generated 过期。
- **TS `@UpdateDateColumn` → Python 必须写 `server_default=func.now(), onupdate=func.now()` 两个**；只写 server_default 则 UPDATE 不刷新（init.sql 无触发器，`now()` 只在 INSERT 生效）。带 updated_at 的正确样板：document.py / user.py / doc_chunk.py / team.py。
- **PG `now()` = 事务开始时间**：同一事务内多次 UPDATE，`updated_at` 不会前进；跨事务才会变。测 onupdate 必须开两个 session，否则误判为「没生效」。
- 冒烟脚本 main + cleanup 必须同一 asyncio.run 事件循环（engine 连接池绑定首个 loop）。
- elasticsearch-py 8.x `search()` 关键字是 **`source=`**（不是 `_source=`，否则 mypy 报 Unexpected keyword argument）；`from_=` 同理。
- 冒烟里 `delete()/sa_delete()` **必须 `.commit()`**，否则回滚留残留行。
- ⚠️ **同一文件在一条消息里发多个 Edit，常只有部分（甚至只有最后一条）真正落盘**。改多处必须「一条消息一个 Edit + 立即 grep 复验」，尤其 import 行漏落会导致整文件 NameError。

## 剩余待办
- RBAC 全链路接口已闭环（permission / role / user 三类授权接口齐备）：user 管理端 = 11 端点（含 GET/PUT `/{id}/permissions`）。
- 真实 SMTP 已验 + alice 测试账号已处置（用户 2026-09-02 确认，不再列为待办）。
- **team 模块**：已完成全量移植（2026-09-03）——kh_team / kh_team_member 实体核验一致 + `schemas/team.py`（请求+响应）+ `services/team.py` 10 法 + `api/v1/teams.py` 9 端点 + smoke_team_api 27 步。关键保真：同码不查重 201、create 不校验 leader/parent 存在、delete 软删+成员硬删、addMembers 幂等逐个插、page 排序 created_at **DESC**、tree 公开（@Public 无 @roles）、POST members 请求体是裸数组。
- permission.py 的 updated_at 已补 onupdate（2026-09-03，推断修复并在注释注明回退条件）；team.py 同步补齐。
- **document 模块已闭环**（2026-09-03）：service 层 authorId 恒取 actor、publish/direct_publish/archive/save_as_draft/remove 五法回填 update_by；16 端点权限码齐；`smoke_document_permission.py` 锁定行为。`POST /{id}/index` 是 Python 独有端点（TS 无），维持仅登录、不发明权限码。
- **graph / search 查询侧已完成**；KG 写入侧（图谱构建 pipeline）与 document-review.service.ts 的 actor 透传仍待逐文件核验。

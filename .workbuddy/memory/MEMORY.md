# my-rag 项目长期约定

TS(NestJS) → Python(FastAPI) 重写：逐文件对照 TS 源码核验（贴源后转写，不靠 DTO 推断）；刻意差异必须注释文档化。细节见同目录每日日志，本文件只放**跨会话硬约束**。

## 配置 / 环境
- 配置三处：`config.py` 声明字段+类型+默认值（`extra="ignore"` → **未声明 key 静默忽略**）；`backend/.env` = 运行实际值（**gitignore，用户维护**）；`backend/.env.example` = 入库模板（新增配置必须同步）。
- ✅ `env_file` 已是**绝对路径**（`Path(__file__).resolve().parents[2]/".env"`），不依赖 cwd。09-18 修前从仓库根启动会整份静默不生效，只暴露为「发邮件失败 / 联网搜索空结果」这类功能降级。仓库根无 .env；`.dockerignore` 排除 .env（容器靠环境变量注入）；`scripts/` 里的 `cd backend` 仍需要（`PYTHONPATH=.` 依赖 cwd）。
- Schema 真相源 `init-scripts/postgresql/*.sql`（⚠️ 仓库根）：改字段 = init.sql → `app/models/*.py` → 存量库手动 ALTER。依赖用 `uv add`（venv 无 pip），requirements.txt 与 pyproject 同步。
- ⚠️ **ES/Neo4j 需等就绪**（端口通但 health 无响应 / 分片 red 未转 yellow）；Docker 退出后 `open -a Docker`。
- ⚠️ **「`logger.info` 全都看不到」= 日志没初始化**：唯一初始化点是 `app/core/logging.py` 的 `setup_logging()`（由 `main.py` 顶层调用）。不调用则 root 无 handler + level=WARNING → **INFO 静默丢弃**、WARN+ 靠 `logging.lastResort` 裸输出（无时间戳/级别/logger 名）。uvicorn 默认 `LOGGING_CONFIG` **不配置 root**（只管 uvicorn/uvicorn.error/uvicorn.access 三兄弟）、FastAPI 也不管。级别由 `LOG_LEVEL` 控制（拼错回退 INFO、不报错）；格式 `%(asctime)s %(levelname)-5s [%(name)s] %(message)s`——`[name]` 就是 `[ClassName]`（靠 `getLogger("XxxService")` 提供），**消息文本里不要再手写前缀**（会双份）。`--reload` 主进程前几行仍是 uvicorn 原生格式（打在我们 import 之前，固有时序）。
- ⚠️ **SQL 日志改级别压不住，只能从引擎侧关**：SQLAlchemy `echo` 为真时 `InstanceLogger.log()` 直接取 `_echo_map[echo]`（True→INFO）判定、并调 `logger._log()`，**绕过 `isEnabledFor()`** → 降噪名单里放 `sqlalchemy.engine` 是**假开关**（已移除）。新增 `DB_ECHO`（默认 false，**刻意与 `DEBUG` 解耦**——后者只管 /docs）控制 `session.py` 的 `echo=`；另实测 echo=True 时 SQLAlchemy 自挂 stdout handler，与 root **双份输出**。
- ⚠️ 本机 `HTTP_PROXY=127.0.0.1:53304` 无 NO_PROXY + vite 只监听 IPv6 `[::1]:5173` → `127.0.0.1:5173` 必得 **502（假象）**，用 `localhost` 或 unset 代理。

## 运行 / 验证
- `cd /abs/backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/xxx.py`（cwd 会漂移，必须带 cd）。
- 标准 = 真实依赖 E2E 冒烟 + `mypy app/` 0 issue。冒烟用「裸 app + `httpx.ASGITransport`」，**不要 TestClient**（与 asyncpg 冲突）；不污染种子；别依赖脆弱前提（总行数、基址）；临时用户先删子表后主表、Redis 激活键 **token/user 两侧都删**。
- 新增/改动断言必须做**回退法证伪**（改坏实现看断言是否真报错），否则常是假绿/假红（09-18 两次实证）。
- **GET 探针判路由**：404 = 路由不存在，**405** = 存在但方法不符（不触发 LLM / 不写数据）。权威路由表 = `GET /openapi.json`。
- 自签 JWT 复现任意种子身份：`jwt.encode({"sub","username","type":"access","exp"}, settings.JWT_SECRET, "HS256")`。

## 通用约定
- 同文件承载多主题要拆提交时：若该文件 diff 的 **hunk 边界与主题对齐**（写代码时顺手把新增块各自成段即可做到），用「解析 diff → 按 hunk 分组生成 patch → 逐个 `git apply --cached`」；先 `--check` 再 apply，最后 `diff --cached --stat` 复核文件清单。比手写 patch / `hash-object` blob 中间态可靠（09-18 `config.py` 与 `.env.example` 各 4 主题实证）。
- 状态码：写操作显式 201；**查询类 POST 刻意 200**（/search、/search/hybrid、/rag/search、/ai/chat）。含字面量前缀的路由注册在 `/{doc_id}` 之前。
- bigint 真相源 `app/common/bigint.py`：`parse_bigint` 路径参数(404) / `require_bigint` body+query(400)；空串 body=清空、query=不筛选；校验前置于副作用；实体→响应转字符串。
- 文档状态 0Draft/1Published/2Archived/3PendingReview 集中 `app/models/document_status.py`；审核结果 NULL=待审；`DOCUMENT_REQUIRE_APPROVAL` 严格 =="false" 才免审；PATCH 不许改 status（唯一例外 Published→Draft）；**Archived 即使需审也 direct_publish**；审核人从 JWT 取。
- ⚠️ **前端拼 URL 必须用 `client.ts` 的 `API_BASE`（已 export）**：vite proxy 只转发、**无 rewrite**，硬编码漏 `/v1` → 404。

## 鉴权守卫（route_class）
- `app/api/auth_route.py`：`AuthRoute` + `@public()`/`@roles(...)` + `auth_router()` + `check_route_classes()`；**默认全拦是刻意 fail-safe**。⚠️ `include_router` 不继承父 router 的 route_class → **每个子 router 必须写 `auth_router()`**，漏写整组失去保护。
- 三层 = ①默认全拦+@public / ②@roles / ③require_permission；**①②在 handler 内先跑、③在 `original_route_handler` 之后** → ②永远先于③，同挂 `@roles(ADMIN)` 的 system:* 端点第三层被遮蔽（TS 同款）。`require_permission(*codes)` = **OR 语义**。审计路由依赖取 `params.Depends` 的 **`dependency`**（非 `call`）。

## 可见性 / 写权限（**完整细节见仓库根 `文档可见性与写权限.md`**）
- 模型：公开 ∪ 本人 ∪ 所在团队任一命中可读；ADMIN/REVIEWER 不受限；判定集中 `app/common/document_access.py`（`can_read_document` / `can_write_document`）。**写权只认 ADMIN + 作者**（REVIEWER 不在白名单；approve/reject 走 `document:review`，刻意不校验写权）。六条读链路 = 列表/详情(PG 实时 SQL) + chunk 混合(ES filter / PG JOIN) + 文档级全文(ES) + 图谱(Neo4j)。
- ⚠️ **`scope=None` = 不过滤**（`find_all` / `find_one` / `hybrid_search` / `neo4j_access_params` / graph 四方法同语义；**`search_documents` 是唯一例外**，缺省兜底 `empty_scope()` = 仅公开）→ 端点必须显式传 `access_from_user(user)`；`empty_scope()` 与「不过滤」语义相反。`AuthUser.team_ids` 注入后必须**再按 `kh_team.deleted=false` 过滤**，否则已软删团队继续授权。
- ⚠️ 改可见性必须三处同时同步（ES kh_chunk + kh_document + Neo4j），**不得被 content 分支 return 吞掉**：短路条件须与 TS 同形 `is_published and content_changed and not is_require_approval()` 在同一 if 内（挪进 if 体 → 需审模式下静默丢弃、实测越权）。
- ⚠️ 两处同名 `update_visibility` 勿「统一」：`es_index` 用 `update_by_query` + 容忍 `index_not_found`；`search_index` 必须单条 `client.update`（`updated=0` 静默返回 True）。索引侧陷阱：ES `teamId` 必须 keyword（text → terms 走分词 → 静默失效）；Neo4j `authorId`/`teamId` 一律 `_opt_str`。
- ⚠️ 写权校验 `_assert_writable` 在 update/remove/publish/archive/save_as_draft 五处「取实体后、状态校验前」；`submit_for_review(actor=None)` 可选**勿收紧**但端点必传（漏传 = 该路径不设防）；`create`/`upload` 的 `author_id`/`create_by`/`update_by` **恒取 actor.user_id**，DTO 同名字段被忽略。

## 已闭环模块要点
- RBAC 种子：角色 `2000000000000000001~3`、用户 `1000000000000000001~3`（admin=ADMIN+REVIEWER / reviewer / user，密码 123456）；`kh_role_permission` **11 行**——**加权限层前必须先补种子，否则普通用户全线 403**（`seed_role_permission_matrix.py` 幂等刷）；`kh_user_role` 无 CASCADE → 删用户先删子表。
- 守卫口径：管理端点 = `@roles(ADMIN)` + `require_permission("system:xxx")`；`/search`、`/search/hybrid`、graph 4 端点挂 `require_permission("search")`。保真例外：**`GET /teams/mine` 登录即可**、**`GET /teams/tree` 是 ADMIN + system:team**；**Redis 必须 `decode_responses=True`**；Neo4j driver 6.x 传原生 `int` limit。

## 对话记忆（09-18；短期 + 长期均已实现，ai_stream 已接入）
- **短期**（Redis 热窗口）`services/chat_short_memory.py` + `chat_memory.py`：key `kh:chat:{uid}:{sid}:messages`、TTL 86400、窗口 20 条、**只存 human/ai 原文**；失败只 WARN，`try_load` 返回 None = 「请回落数据库」。⚠️ `save([])` 后 `try_load` 返回 `[]` **不是** None，别当 miss 回填。⚠️ 冒烟必须先 `ping()` 探活：save 吞异常 → Redis 没起时静默失败 → `is None` 断言**假绿**。
- **回填链路**（TS `loadWorkingHistory`）：`try_load` →（`is not None`）→ miss 才 `chat_session_service.list_recent_messages(db, uid, sid, window_size)` → `db_rows_to_messages` → `save`。该方法**只查库不写缓存**；⚠️ 必须 `created_at DESC, id DESC` + `LIMIT max(limit,1)` 再 `reverse()` 成正序——写成 ASC+limit 就是「取最旧 N 条」（`smoke_ai_sessions` 第 14 段回退法证伪）。
- **删会话**三处一起动：PG（先删消息再删会话）→ 短期 `clear` → 长期 `clear_session`；只删库不清缓存 → 已删会话内容在 TTL 内继续被当「历史记忆」召回。
- **长期**（Mem0 **托管平台**）`services/chat_long_memory.py`；未配 `MEM0_API_KEY` 整体跳过。⚠️ **两个 LLM 分工**：分类器（判 write_user/write_session）在**本进程**用 deepseek；**抽取在云侧、SDK 不能指定模型**（要换须改 OSS 自托管）。
  ⚠️⚠️ **v3 实体 ID 三方法传法各不相同**（SDK 类型注释是错的，以实测为准）：`search` **必须** `filters={'user_id':…,'run_id':…}`（顶层被 SDK `ENTITY_PARAMS` raise）；`add`/`delete_all` **必须**顶层 `user_id=`/`run_id=`（传 `filters={...}` SDK 不拦但服务端 400；`delete_all` 走 query string，dict 被 `str()` 塞进 URL）。
  ⚠️⚠️ **`delete_all(user_id, run_id)` 忽略 run_id、清空整个 user 记忆**（实测 4/1/1 → 0/0/0，120s 不复原）→ TS 的 `deleteAll({userId,runId})` 做 clearSession = 「删一个会话抹掉该用户全部长期记忆」。本项目 `clear_session` 改为 `get_all(filters={user_id,run_id})` 列 id → 逐条 `delete(id)`。
  ⚠️ `add()` 是**异步**抽取（`status=PENDING`，实测 6~9s 后才可检索）→ 冒烟必须轮询。结果取 `res["results"][]["memory"]`；客户端必须 `AsyncMemoryClient`（同步版阻塞事件循环）。
  ⚠️ **pyproject 必须保留 `[[tool.mypy.overrides]] module=["mem0","mem0.*"] follow_imports="skip"`**，否则 mypy 跟随 mem0 依赖图 → 本机 OOM 被杀 exit 137。

## 踩坑速查
- redis-py 默认 bytes；EmailStr 拒收 `.test`（冒烟用 example.com）。
- `with_structured_output(..., name=...)` TypeError；deepseek-v4-flash 需 `extra_body={"thinking":{"type":"disabled"}}`。
- **`onupdate=func.now()` 列 commit 后再读属性 → MissingGreenlet，必须 `await session.refresh(实体)`**；**PG `now()` = 事务开始时间**（同事务多次 UPDATE `updated_at` 不前进）。
- 冒烟 main + cleanup 同一 asyncio.run 事件循环（engine 池绑首个 loop）；`delete()/sa_delete()` **必须 `.commit()`**。
- ⚠️ 冒烟断言别用**全局总行数**（库里有 09-03 手工残留）→ 口径 = 「按固定 ID 断言种子行仍在 + 本次临时行已清空」。
- elasticsearch-py 8.x `search()` 关键字是 **`source=`/`from_=`**；`app.get()` 不接受 route_class。
- ⚠️ **一条消息里对同一文件发多个 Edit 常只落最后一条** → 一个 Edit + 立即 grep 复验。
- macOS grep 不支持 `\|`，一律 `grep -E`（否则匹配 0 且 exit 1，静默中断 `&&` 链）。

## 待办
- 库里 09-03 手工残留（重复「前端开发组」+ xiao 成员关联、`ROLE_TEST`）待拍板。
- 前端检索 / 图谱 / 列表展示未调；`/teams/mine` 前端未接入。
- rerank 降级路径（`if reranked:` 不成立时整段跳过 `RAG_MIN_SCORE`）无告警，待定。

# 简历素材：企业级 RAG 知识库与 Agent 问答系统

> 依据：仓库真实代码统计（2026-09-11 核对），非架构文档规划。
> 凡标 ⚠️ 项为**未实现**，简历上不要写。

---

## 一、客观事实（面试背这些数字）

| 项 | 真实值 | 来源 |
| --- | --- | --- |
| 周期 | 2026-08-27 → 2026-09-08（约 2 周，持续迭代中） | git log |
| 提交 | 41 commits，单人 | git log |
| 后端代码 | 13,837 行 Python（app/），含脚本 20,521 行 | wc -l |
| 前端代码 | 4,404 行 TS/TSX，13 个页面 | wc -l |
| REST 端点 | 82 个（document 17 / users 13 / permissions 10 / teams 10 / auth 9 / ai 8 / role 8 / graph 4 / search 2 / health 1） | api/v1 |
| 数据表 | 13 张 PG 表 + pgvector 扩展 | init.sql |
| 基础设施 | Docker Compose 编排 10 服务：PG、MongoDB、mongo-express、RabbitMQ、ES、Kibana、RustFS、Neo4j、Redis、RedisInsight | docker-compose.yml |
| E2E 冒烟脚本 | 34 个，跑真实依赖（PG/ES/Neo4j/RabbitMQ/Redis） | scripts/smoke_*.py |
| 静态检查 | mypy 0 issue | 项目约定 |

**关键参数（必背）**

| 参数 | 值 |
| --- | --- |
| Embedding | `qwen3.7-text-embedding`，1024 维，batch=10（DashScope 兼容口） |
| Reranker | `qwen3-rerank`（DashScope text-rerank 专用端点） |
| 混合检索 | 双路各召回 20 条 → RRF 融合（常数 C=60）→ 全候选精排 → minScore 过滤 → 截 topK |
| 分块 | 512 token / overlap 64（langchain-text-splitters） |
| LLM | `deepseek-v4-flash`（OpenAI 兼容口，关闭 thinking） |
| Agent | `langchain.agents.create_agent`，stream_mode `["messages","updates"]` |

---

## 二、一句话定位（三档，按投递岗位选）

- **保守**：企业级 RAG 知识库系统 —— 多格式文档解析、混合检索、知识图谱与流式智能问答。
- **标准（推荐）**：面向企业知识管理场景的 Agentic RAG 系统，覆盖「多源文档解析 → 三路索引 → 混合检索 → Agent 问答」全链路。
- **进取**：企业级多模态 RAG 知识库与 Agent 问答平台 ⚠️（"多模态"目前未实现，仅在图片/音频/视频解析落地后才可用）

---

## 三、技术栈一行（按需裁剪）

**投 Agent / AI 后端岗：**
`Python · FastAPI · LangChain · LangGraph · PGVector · Elasticsearch · Neo4j · RabbitMQ · Redis · RustFS(S3) · Docker`

**投全栈岗（保留前端优势）：**
`React 19 · TypeScript · Vite · Python · FastAPI · LangChain · LangGraph · PostgreSQL/PGVector · Elasticsearch · Neo4j · RabbitMQ · Redis · Docker Compose`

---

## 四、简历正文（可直接粘贴，按优先级排序）

> 建议用前 4 条；岗位偏 AI/Agent 用到第 5 条；全栈岗保留第 6 条。

**1｜架构设计与全栈落地**
面向企业知识管理场景，独立设计并实现「多源文档解析 → 三路索引 → 混合检索 → Agent 问答」全链路 RAG 系统；后端 FastAPI + LangChain/LangGraph，前端 React 19 + TypeScript，Docker Compose 一键编排 PostgreSQL/PGVector、Elasticsearch、Neo4j、RabbitMQ、Redis、RustFS 等 10 个基础设施服务，共 82 个 REST 端点、约 1.8 万行代码。

**2｜混合检索与答案溯源**
实现 Elasticsearch 关键词（BM25）与 PGVector 向量双路召回，RRF 融合（C=60）后交由 DashScope `qwen3-rerank` 精排，支持 minScore 阈值过滤；结合 ES 词高亮与回答中 `[n]` 引用编号解析，将答案回溯到具体文档分片，生成可点击的引用卡片，解决 RAG「黑箱生成」问题。

**3｜Agent 问答与流式输出**
基于 LangGraph 构建 ReAct Agent，挂载知识库检索与联网搜索（Tavily）两类工具，由模型自主决定是否检索外源；因 Python 侧无可用适配器（版本冲突 / 过滤 thinking 块），自研 AI SDK UI Message Stream 协议编码器输出 SSE 流，支持 reasoning 思考过程、data-* 自定义进度事件与引用来源，前端实时渲染。

**4｜知识图谱构建与多跳查询**
用 LLM 结构化输出（function calling，规避 DeepSeek 不支持 json_schema 严格模式）从文档分片抽取实体与关系，写入 Neo4j 构建企业知识图谱（10 类实体 / 12 类关系 + 兜底关系），提供全景图与邻域多跳查询接口，支持图谱可视化探索。

**5｜异步文档解析流水线**
RabbitMQ（aio-pika）异步消费解析任务，PDF / Word / PPT / Excel / 文本统一归一为 Markdown，解析产物与内嵌资源托管至 RustFS（S3 兼容）；分片后「一写三索引」——同一份 chunk 并行写入 PGVector 向量库、ES 全文索引、Neo4j 图谱，避免检索侧能力割裂。

**6｜权限体系与工程质量**
实现三层鉴权守卫（JWT 认证 → 角色守卫 → 权限码守卫）覆盖 82 个端点，配合 RBAC 权限矩阵、文档审核发布流程（草稿/待审/发布/归档）与邮箱激活、密码重置；编写 34 个真实依赖 E2E 冒烟脚本 + mypy 静态检查（0 issue）保障重构行为对齐。

---

## 五、⚠️ 真实边界：这些现在**不能写**

架构文档（架构.md / 需求.md）是**规划**，代码没跟上。以下 6 项写了会被追问穿：

| 规划里有 | 代码现状 | 处理 |
| --- | --- | --- |
| Mem0 长期记忆、分层记忆 | 未实现（无依赖、无服务） | 简历删掉；面试问"记忆怎么做"答"当前为会话历史落库 + 滑动窗口，长期记忆是下一步" |
| LangFuse 全链路观测 | 未实现 | 删掉 |
| 多模态：图片 OCR / 音频 ASR / 视频理解 | 未实现，仅 5 种文本类解析器 | 删掉"多模态"，改"多格式文档" |
| 以图搜图 / 跨模态检索 | 未实现 | 删掉 |
| RAG 链路检索后权限过滤 | 权限只在文档业务接口层落地，retrieval/search/ai_stream **无权限过滤** | 简历删掉"检索后鉴权"；问到答"文档接口层已完整落地，RAG 链路过滤在 TODO" |
| Celery + Redis 队列 | 实际是 **RabbitMQ + aio-pika** | 改写 |

另外两点口径要注意：
- **前端是 React 19**，不是架构文档写的 Vue 3。
- **这是参照既有 NestJS/TS 实现做的 Python 重写**，不是从零原创设计。被问"架构是你设计的吗"要如实说：整体架构参照既有实现，Python 侧全部重写 + Agent 链路与流式协议为我独立实现。

---

## 六、面试深挖防御（准备这 6 个）

**Q1：为什么向量库和全文索引分开，不直接用 ES 做向量？**
答：关键词召回保精确匹配（型号、编号、专有名词），向量召回保语义泛化，两路互补；RRF 融合不依赖两路分数可比（只用排名），比加权求和更稳。

**Q2：Reranker 为什么对全部融合候选精排，不先截 topK？**
答：先截断会把 reranker 能捞回来的低排名相关块提前丢掉。项目里是「RRF 全候选 → rerank → minScore 过滤 → 截 topK」，过滤在精排之后。

**Q3：为什么自研 SSE 编码器？**
答：三个候选都不行 —— `ai-sdk-stream-python` 要求 fastapi>=0.128（项目锁 0.110，升级爆炸半径大）；`langchain-vercel-ai-sdk-adapter` 是 alpha 且会过滤 thinking 块（丢 DeepSeek reasoning_content），也不支持 data-* 自定义事件。所以按协议逐帧手写，终止帧 `data: [DONE]`，响应头 `x-vercel-ai-ui-message-stream: v1`。

**Q4：chunk 怎么切？512 这个值怎么定的？**
答：512 token / 64 overlap，用 langchain-text-splitters。512 是「单块能承载完整语义」与「embedding 输入长度限制」的折中；overlap 防跨块语义断裂。可补一句：没做实验调参，是工程默认值 —— 诚实比编数据强。

**Q5：怎么保证答案不胡说？**
答：三层 —— System Prompt 强制"资料不足就说不知道、不许编造"；输出强制标 `[n]` 编号；后端用正则抽编号，只把**实际被引用**的分片作为 sources 落库返回（未标注即空列表），前端渲染引用卡片。

**Q6：重写时怎么保证行为一致？**
答：逐文件对照 TS 源码（不是照 DTO 猜），业务规则、常量、日志文案、状态码逐项对齐；刻意差异写进代码注释标注；每个模块配真实依赖 E2E 冒烟（34 个），mypy 0 issue 才继续。

---

## 七、待你确认的 3 件事

1. **项目归属表述** —— 简历是写「主导重写」还是「独立开发」？代码仓库只有你一人提交，但存在既有 TS 实现。我倾向写「独立将既有 NestJS 实现重写为 Python/FastAPI 架构，并补齐 Agent 链路与前端」—— 可防御且体现执行力。
2. **项目时间** —— 2026.08-2026.09 只有 2 周，简历上要不要写成「2026.08 - 至今」体现持续投入？（如果后续还会做多模态，建议写至今）
3. **是否要补一块再投** —— 目前最大缺口是「RAG 链路权限过滤」和「可观测/效果评估」，这两块是 AI 岗高频考点，补上 1-2 天就能让简历从"搭过 RAG"升级到"懂生产 RAG"。要不要先补再投？

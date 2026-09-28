"""
应用配置：使用 pydantic-settings 从环境变量加载
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

# .env 定位：backend/.env
# （本文件在 backend/app/core/config.py → parents[2] 即 backend/）
# ⚠️ 刻意不用相对的 `env_file=".env"`：pydantic-settings 对相对路径是**按 cwd 解析**的，
# 于是会出现「从仓库根启动 → 整份 .env 不生效、全部悄悄回落默认值、且不报错」——2026-09-18
# 实测：仓库根启动时 MAIL_USER=''、TAVILY_API_KEY 为空（backend/.env 其实存在且值齐全）。
# 改成基于 __file__ 的绝对路径后，无论从哪个目录启动都稳定读到 backend/.env。
# 容器内：Dockerfile WORKDIR=/app 且代码复制到 /app/app/... → 解析为 /app/.env，行为一致。
# 注：文件不存在时 pydantic-settings 仍按「静默忽略」处理（沿用原行为，不额外报错）。
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """应用配置类"""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- JWT 认证----
# 'dev-secret-change-me' 仅 20 字节，PyJWT 对 <32 字节
    # 的 HS256 密钥会发 InsecureKeyLengthWarning（RFC 7518 要求 ≥32 字节）。此处把开发
    # 默认值加长到 47 字节以消除告警；生产环境必须通过 JWT_SECRET 配置强随机密钥。
    JWT_SECRET: str = "dev-secret-change-me-please-use-32-bytes-random"
    # 过期时长格式：<数字><s|m|h|d>，如 2h / 7d / 30m
    JWT_ACCESS_EXPIRES: str = "2h"
    JWT_REFRESH_EXPIRES: str = "7d"

    # ---- 应用基础 ----
    APP_NAME: str = "企业智能知识库系统"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"

    # ---- 日志 ----
    # 业务 logger 的输出级别。，恒 INFO），
    # 这里加开关是为了排查时不必改代码。**与 DEBUG 刻意解耦**：DEBUG 管的是
    # /docs 与 /redoc 是否开放，两者职责不同，不该互相牵动。
    # 取值：DEBUG / INFO / WARNING / ERROR / CRITICAL（大小写不敏感）。
    # 拼错不报错，回退 INFO —— 日志级别写错不该让服务起不来（见 app/core/logging.py）。
    LOG_LEVEL: str = "INFO"

    # ---- 服务器 ----
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # ---- 文档发布审核----
    # 字符串而非 bool，(..., 'true') !== 'false'` 完全一致的判定：
    # 只有严格等于 "false" 才免审，其余任何值（含 TRUE/0/空串）都视为开启审核。
    DOCUMENT_REQUIRE_APPROVAL: str = "true"

    # ---- 数据库 (PostgreSQL + PGVector) ----
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "user"
    POSTGRES_PASSWORD: str = "123456"
    POSTGRES_DB: str = "knowledge_hub"
    # 是否把每条 SQL 打到日志（对应 SQLAlchemy `create_async_engine(echo=...)`）。
    # ⚠️ 默认 **False**，且**刻意与 DEBUG 解耦**：DEBUG 管的是 /docs 与 /redoc 是否开放
    # （默认 True），原先把 echo 挂在 DEBUG 上 ⇒ 开发环境每条 SQL（含会话分页、权限查询）
    # 都会刷屏，而它并不提供任何不该在日志里出现的信息。
    # ⚠️ 另一个不直观的点：**日志级别压不住 echo**。SQLAlchemy 的 `InstanceLogger.log()`
    # 在 echo 为真时直接取 `_echo_map[echo]`（True→INFO）作为判定级别，**完全不读
    # logging 的 effective level**，再直接调 `logger._log()` 绕过 `Logger.isEnabledFor()`。
    # 所以无论把 `sqlalchemy` / `sqlalchemy.engine` 调到 WARNING 还是 CRITICAL 都无效
    # （实测；`app/core/logging.py` 的降噪名单因此不再收录该 logger）—— 要关只能从这里关。
    DB_ECHO: bool = False

    @property
    def DATABASE_URL(self) -> str:
        """异步 SQLAlchemy 连接串"""
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # ---- MongoDB（文档正文，motor 异步客户端）----
    MONGO_HOST: str = "127.0.0.1"
    MONGO_PORT: int = 27017
    MONGO_USER: str = "knowledge_hub_user"
    MONGO_PASSWORD: str = "knowledge_hub_password"
    MONGO_DB: str = "knowledge_hub"
    MONGO_AUTH_SOURCE: str = "knowledge_hub"

    @property
    def MONGO_URI(self) -> str:
        """MongoDB 连接串（应用账号，authSource 为用户所在库）"""
        return (
            f"mongodb://{self.MONGO_USER}:{self.MONGO_PASSWORD}"
            f"@{self.MONGO_HOST}:{self.MONGO_PORT}/{self.MONGO_DB}"
            f"?authSource={self.MONGO_AUTH_SOURCE}"
        )

    # ---- Redis----
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    # 空串视为未设密码
    REDIS_PASSWORD: str = ""
    REDIS_DB: int = 0

    # ---- 邮箱验证----
    # 默认 false：注册后立即可登录；true 时需邮件激活
    REQUIRE_EMAIL_VERIFICATION: bool = False
    # 激活链接拼接用的基址（('APP_PUBLIC_URL', 'http://localhost:3000')`）。
# （链接 /auth/verify-email 是前端页面路由）；无前端阶段
    # 直接指向后端 API 时必须含全局前缀 /api/v1（如 http://localhost:8000/api/v1），
    # 否则邮件里的激活链接会 404
    APP_PUBLIC_URL: str = "http://localhost:3000"

    # ---- SMTP 邮件----
    MAIL_HOST: str = "smtp.qq.com"
    MAIL_PORT: int = 587
    # true=隐式 TLS（通常 465）；false=STARTTLS（通常 587）——与 nodemailer 的 secure 语义一致
    MAIL_SECURE: bool = False
    MAIL_USER: str = ""
    MAIL_PASS: str = ""
    # 发件人
    MAIL_FROM: str = "Knowledge Hub <no-reply@example.com>"

    # ---- RustFS（S3 兼容对象存储，替代 MinIO）----
    RUSTFS_ENABLED: bool = True
    # 本地开发走宿主机映射端口 9000；容器内由 compose 覆盖为 http://rustfs:9000
    RUSTFS_ENDPOINT: str = "http://127.0.0.1:9000"
    RUSTFS_ACCESS_KEY: str = "rustfsadmin"
    RUSTFS_SECRET_KEY: str = "rustfsadmin"
    RUSTFS_REGION: str = "us-east-1"
    RUSTFS_BUCKET: str = "knowledge-hub"
    # 对外可访问的基址（如经 nginx/CDN），缺省用 RUSTFS_ENDPOINT
    RUSTFS_PUBLIC_URL: str | None = None

    # ---- CORS ----
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://localhost:8080"]

    # ---- Embedding----
    EMBEDDING_PROVIDER: str = (
        "local" # local=确定性伪向量(开发/测试) | openai=OpenAI 兼容 API
    )
# EMBEDDING_API_KEY → DASHSCOPE_API_KEY → OPENAI_API_KEY
    EMBEDDING_API_KEY: str | None = None
    DASHSCOPE_API_KEY: str | None = None
    OPENAI_API_KEY: str | None = None
    # DashScope 百炼 OpenAI 兼容端点
    EMBEDDING_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # 模型 ID 以百炼控制台为准（text-embedding-v3/v4 或 qwen3-embedding-*；写错会 400）
    EMBEDDING_MODEL: str = "qwen3.7-text-embedding-flash"
    EMBEDDING_DIMENSION: int = 1024
    # DashScope text-embedding-v3 单次最多 10 条；超出会 400 InvalidParameter（代码内钳制）
    EMBEDDING_BATCH_SIZE: int = 10

    # ---- Reranker----
    # 字符串而非 bool，(..., 'true') !== 'false'` 判定一致：
    # 仅严格等于 "false" 才关闭重排。
    RAG_RERANK_ENABLED: str = "true"
    # API Key fallback 链：RERANK_API_KEY → DASHSCOPE_API_KEY → OPENAI_API_KEY
    RERANK_API_KEY: str | None = None
    # 模型 ID 以百炼控制台为准（qwen3.7-text-rerank 等；写错会 400）
    RAG_RERANK_MODEL: str = "qwen3.7-text-rerank"
    # DashScope 原生（非 OpenAI 兼容）端点前缀，重排走专用 /api/v1/services/rerank/...
    RERANK_BASE_URL: str = "https://dashscope.aliyuncs.com"

    @property
    def RERANK_ENDPOINT(self) -> str:
        """text-rerank 专用端点（去掉尾部斜杠再拼路径）"""
        return (
            f"{self.RERANK_BASE_URL.rstrip('/')}"
            f"/api/v1/services/rerank/text-rerank/text-rerank"
        )

    # ---- 混合检索----
    # 多路召回每路的候选数 / 默认返回条数（RRF 前召回规模）
    RAG_HYBRID_TOP_K: int = 20
    # RRF 常数（与 ES RRF 默认一致）：score = Σ 1/(C + rank)
    RAG_RRF_C: int = 60
    # rerank relevance_score 下限（0–1），低于该值的候选不进入最终结果；<=0 表示不过滤。
    # ⚠️ 这个阈值**跟 reranker 模型绑定**，换模型必须重新校准（否则要么误杀、要么漏过）：
    # · gte-rerank-v2：分档偏低但离散——相关 0.13~0.47、无关 ~0.006，0.1 能分开；
    #   该模型仅免费额度可用，额度耗尽即降级 RRF。
    # · qwen3.7-text-rerank（当前）：分档高且分离度好——相关 ~0.9、无关 ~0.001。
    #   55 条评测集实测：<0.55 有负例漏过；0.55~0.84 recall@5=1.0 且负例全部过滤；
    #   ≥0.85 开始误杀合法命中（recall 0.98）。取区间中点 0.7（两侧各留 ~0.15 余量）。
    # 历史教训：0.4 会把 gte 的**全部**合法命中过滤掉 → AI 回答「召回不到相关资料」，
    # 而文档搜索（ES kh_document，不走 rerank）却能找到。
    RAG_MIN_SCORE: float = 0.7

    # ---- RabbitMQ（发布后异步管线：RAG / KG / ES）----
    RABBITMQ_ENABLED: bool = True
    # 本地开发走宿主机映射端口 5672；容器内由 compose 覆盖为 amqp://rabbitmq:5672
    RABBITMQ_URL: str = "amqp://guest:guest@localhost:5672"
    RABBITMQ_CONNECT_TIMEOUT_MS: int = 15000

    # ---- RAG 分块----
    RAG_CHUNK_SIZE: int = 512
    RAG_CHUNK_OVERLAP: int = 64

    # ---- Elasticsearch（全文/关键词检索；向量存 PG，不写 dense_vector）----
    ELASTICSEARCH_ENABLED: bool = True
    # 本地开发走宿主机映射端口 9200；容器内部署时覆盖为 http://es:9200
    ELASTICSEARCH_NODE: str = "http://localhost:9200"

    # ---- KG 抽取----
    # API Key fallback 链：OPENAI_API_KEY → LLM_API_KEY → DASHSCOPE_API_KEY
    LLM_API_KEY: str | None = None
    # DeepSeek OpenAI 兼容端点
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    # 模型：deepseek-v4-flash（DeepSeek-V4-Flash）
    LLM_MODEL: str = "deepseek-v4-flash"
    # 单 chunk 最多实体 / 关系数，防止图爆炸。
    # ⚠️ 实测 12/15 对中文密集文档偏小：01-travel-expense-policy 两块都**正好抽满 12**，
    # 「私车公用申请表」这类实体被挤掉 → 后续图谱对话检索按实体名匹配就查不到。
    # 放宽到 20/20（仍有限，防止图无限膨胀）。改完需重跑建图脚本才生效（旧图是旧上限抽的）。
    KG_MAX_ENTITIES: int = 20
    KG_MAX_RELATIONS: int = 20
    # 单次抽取 LLM 超时（毫秒）
    KG_LLM_TIMEOUT_MS: int = 60000

    # ---- AI 对话----
    # 大模型 DeepSeek，复用上面的 LLM_BASE_URL / LLM_MODEL；逐个可独立覆盖。
    # OPENAI_API_KEY 在上方 API Key fallback 链区声明（EMBEDDING_API_KEY → DASHSCOPE_API_KEY → OPENAI_API_KEY），
    # 此处复用同一字段，勿重复声明（pydantic 第二次赋值会静默覆盖）。
    OPENAI_BASE_URL: str | None = None
    # 模型覆盖：MODEL_NAME → LLM_MODEL（默认 deepseek-v4-flash）
    MODEL_NAME: str | None = None
    # LLM 单次作答超时
    AI_CHAT_TIMEOUT_MS: int = 60000
    # 流式对话思考开关：严格 == "false" 才关，
    # 默认开——deepseek-v4-flash 开思考后 reasoning_content 随流式 delta 返回
    LLM_ENABLE_THINKING: str = "true"

    # ---- 多模态图像识别（图片 OCR；Qwen-VL via DashScope 兼容口）----
    # 图片文件（jpg/png/webp/bmp/gif）走视觉模型 OCR → Markdown，再分块入索引。
    # Key fallback：VISION_API_KEY → DASHSCOPE_API_KEY → OPENAI_API_KEY。
    VISION_API_KEY: str | None = None
    # baseURL 留空用 DashScope 官方兼容口
    VISION_BASE_URL: str | None = None
    # 模型 ID 以百炼控制台为准：qwen-vl-max（通用）/ qwen-vl-ocr（专用 OCR）
    VISION_MODEL: str = "qwen-vl-max"
    # 单张图片识别超时（毫秒）
    VISION_TIMEOUT_MS: int = 60000

    # ---- 检索改写----
    # 单次改写 LLM 超时（毫秒，(config.get('AI_QUERY_REWRITE_TIMEOUT_MS', 15000))`）。
    # 改写是增强环节：超时/失败都回退原问题，绝不阻断检索。
# (...)` 会把写错的环境变量变成 NaN 一路传到 ChatOpenAI；
    # 这里声明为 int，非法值在启动时即报错（同 chat_short_memory 的处理）。
    AI_QUERY_REWRITE_TIMEOUT_MS: int = 15000

    # ---- TypeSafe 结构化判定----
    # TypeSafe 是判定式 SaaS（system_one → Noul/Choice/Score 类型化结果），只用来做
    # 检索前的 5 路意图分类；查询改写仍是生成式 LLM（路线 A：TypeSafe 管决策、LLM 管改写）。
    # 未配置则意图识别回退 LLM 的 need_retrieve 推导（见 chat_query_rewrite.py）。
    TYPESAFE_API_KEY: str | None = None

    # ---- LangSmith（LangChain / LangGraph 链路追踪）----
    # ⚠️ 这里声明只是为了让 pydantic 认识这四个 key（extra="ignore" 下未声明的会被静默丢弃）。
    # **真正让追踪生效的是文件末尾的 _export_langsmith_env()**：LangSmith SDK 只读
    # `os.environ`，不读 pydantic Settings。pydantic-settings 只把 .env 读进 Settings
    # 对象，**不会写回 os.environ** —— 所以只填 .env 而不导出时，
    # `tracing_is_enabled()` 恒为 False，Web 上一条记录都没有。
    # 开关用字符串，与项目其他开关一致：仅严格 == "true" 才开启（SDK 同款判定）。
    LANGSMITH_TRACING: str = "false"
    LANGSMITH_API_KEY: str | None = None
    LANGSMITH_PROJECT: str = "default"
    # 留空用官方 SaaS（https://api.smith.langchain.com）；自建网关才需覆盖
    LANGSMITH_ENDPOINT: str | None = None
    # 联网搜索
    TAVILY_API_KEY: str | None = None

    # ---- 对话短期记忆----
    # 用途：最近若干条 human/ai 原文放 Redis，避免每轮问答都回 Postgres 拉历史；
    # 只存原文，不含检索资料 / 思考 / Mem0 系统消息（由 chat_memory.is_working_message 过滤）。
    # 窗口 key 为 `{PREFIX}:{userId}:{sessionId}:messages`，miss 时由调用方从库回填。
# (config.get(..., 86400))`，环境变量写错会得到 NaN 并一路传到 Redis；
    # 这里声明为 int，非法值在启动时即报错。
    CHAT_SHORT_MEMORY_TTL_SECONDS: int = 86400
    CHAT_SHORT_MEMORY_MAX_MESSAGES: int = 20
    CHAT_SHORT_MEMORY_KEY_PREFIX: str = "kh:chat"

    # ---- 会话滚动摘要（滑出热窗口的旧历史 → 增量折叠成一段摘要）----
    # 存储：Redis 热缓存（`{PREFIX}:{uid}:{sid}:summary`，TTL 复用上方）
    # + Postgres `kh_ai_session_summary`（双写，Redis 优先 / DB 兜底）。
    # 摘要目标字数：热窗口 20 条 ≈ 1500~2500 token，摘要占窗口 ~15% 比较合理；
    # 因为是**增量折叠**（旧摘要 + 新滑出 → 新摘要），prompt 里限死字数防止无限增长。
    # 超限会硬截断兜底（见 chat_summary.py）。
    CHAT_SUMMARY_MAX_CHARS: int = 400

    # ---- 对话长期记忆----
    # 未配置 MEM0_API_KEY 时整个长期记忆模块跳过（构造时 WARN，各方法直接返回空/不写）。
    # MEM0_HOST 留空则用 SDK 默认 https://api.mem0.ai（自建网关时才需覆盖）。
    # 每层各取多少条（(config.get('MEM0_TOP_K', 5))`）。
    # ⚠️ 记忆的「抽取」在 Mem0 云侧完成，SDK 不暴露模型参数，只能用 custom_instructions
    # 约束输出；本进程只跑「分类器」（判断写 user 层 / session 层 / 都不写），走下方
    # OPENAI_API_KEY → LLM_API_KEY → DASHSCOPE_API_KEY 链，本项目即 deepseek-v4-flash。
    MEM0_API_KEY: str | None = None
    MEM0_HOST: str | None = None
    MEM0_TOP_K: int = 5

    # ---- Neo4j（KG 知识图谱存储；与 docker-compose 中 neo4j 服务一致）----
    NEO4J_ENABLED: bool = True
    # 本地开发走宿主机映射端口 7687；容器内部署时覆盖为 bolt://neo4j:7687
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    # 与 docker-compose NEO4J_AUTH=neo4j/12345678 保持一致
    NEO4J_PASSWORD: str = "12345678"


@lru_cache
def get_settings() -> Settings:
    """获取配置单例"""
    return Settings()


settings = get_settings()


def _export_langsmith_env() -> None:
    """把 LangSmith 配置导出到 os.environ（SDK 只读环境变量）

    ⚠️ 这是「在 .env 填了 key 但 Web 看不到记录」的根因修复。
    LangSmith SDK 的 `tracing_is_enabled()` / Client 都走 `os.environ`；
    pydantic-settings 不会把 .env 写回进程环境，必须显式导出。

    用 `setdefault`：外部真实环境变量（docker/CI/命令行 export）优先于 .env，
    与 pydantic-settings 的优先级一致。空值不导出，避免 SDK 拿到空串 key。
    """
    for key in ("LANGSMITH_TRACING", "LANGSMITH_PROJECT", "LANGSMITH_ENDPOINT"):
        value = getattr(settings, key)
        if value:
            os.environ.setdefault(key, str(value))
    if settings.LANGSMITH_API_KEY:
        os.environ.setdefault("LANGSMITH_API_KEY", settings.LANGSMITH_API_KEY)


# 在 import 期即导出（config 被所有服务模块引用，早于任何 langsmith 运行时取值）
_export_langsmith_env()

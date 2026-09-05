"""
应用配置：使用 pydantic-settings 从环境变量加载
"""

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置类"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- JWT 认证（对应 TS auth.module.ts JwtModule + AuthService）----
    # ⚠️ 刻意偏离 TS：TS 开发默认 'dev-secret-change-me' 仅 20 字节，PyJWT 对 <32 字节
    # 的 HS256 密钥会发 InsecureKeyLengthWarning（RFC 7518 要求 ≥32 字节）。此处把开发
    # 默认值加长到 47 字节以消除告警；生产环境必须通过 JWT_SECRET 配置强随机密钥。
    JWT_SECRET: str = "dev-secret-change-me-please-use-32-bytes-random"
    # 过期时长格式与 NestJS @nestjs/jwt 一致：<数字><s|m|h|d>，如 2h / 7d / 30m
    JWT_ACCESS_EXPIRES: str = "2h"
    JWT_REFRESH_EXPIRES: str = "7d"

    # ---- 应用基础 ----
    APP_NAME: str = "企业智能知识库系统"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"

    # ---- 服务器 ----
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # ---- 文档发布审核（对应 TS DOCUMENT_REQUIRE_APPROVAL）----
    # 字符串而非 bool，保持与 TS `config.get(..., 'true') !== 'false'` 完全一致的判定：
    # 只有严格等于 "false" 才免审，其余任何值（含 TRUE/0/空串）都视为开启审核。
    DOCUMENT_REQUIRE_APPROVAL: str = "true"

    # ---- 数据库 (PostgreSQL + PGVector) ----
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "user"
    POSTGRES_PASSWORD: str = "123456"
    POSTGRES_DB: str = "knowledge_hub"

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

    # ---- Redis（密码重置验证码等；对应 TS RedisModule 全局单例）----
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    # 空串视为未设密码（compose 里 redis:7-alpine 无密码；TS `password || undefined` 同义）
    REDIS_PASSWORD: str = ""
    REDIS_DB: int = 0

    # ---- 邮箱验证（注册激活 / 密码重置；对应 TS REQUIRE_EMAIL_VERIFICATION）----
    # 默认 false：注册后立即可登录；true 时需邮件激活
    REQUIRE_EMAIL_VERIFICATION: bool = False
    # 激活链接拼接用的基址（TS `config.get('APP_PUBLIC_URL', 'http://localhost:3000')`）。
    # TS 原设计指向前端（链接 /auth/verify-email 是前端页面路由）；无前端阶段
    # 直接指向后端 API 时必须含全局前缀 /api/v1（如 http://localhost:8000/api/v1），
    # 否则邮件里的激活链接会 404
    APP_PUBLIC_URL: str = "http://localhost:3000"

    # ---- SMTP 邮件（aiosmtplib；对应 TS @nestjs-modules/mailer + nodemailer）----
    MAIL_HOST: str = "smtp.qq.com"
    MAIL_PORT: int = 587
    # true=隐式 TLS（通常 465）；false=STARTTLS（通常 587）——与 nodemailer 的 secure 语义一致
    MAIL_SECURE: bool = False
    MAIL_USER: str = ""
    MAIL_PASS: str = ""
    # 发件人（支持 "显示名 <addr@domain>" 格式，与 TS mail.from 一致）
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

    # ---- Embedding（发布索引：文本向量，对齐架构 1024 维；对应 TS EmbeddingService）----
    EMBEDDING_PROVIDER: str = (
        "local"  # local=确定性伪向量(开发/测试) | openai=OpenAI 兼容 API
    )
    # API Key 按 TS 的 fallback 链：EMBEDDING_API_KEY → DASHSCOPE_API_KEY → OPENAI_API_KEY
    EMBEDDING_API_KEY: str | None = None
    DASHSCOPE_API_KEY: str | None = None
    OPENAI_API_KEY: str | None = None
    # DashScope 百炼 OpenAI 兼容端点
    EMBEDDING_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # 模型 ID 以百炼控制台为准（text-embedding-v3/v4 或 qwen3-embedding-*；写错会 400）
    EMBEDDING_MODEL: str = "qwen3.7-text-embedding"
    EMBEDDING_DIMENSION: int = 1024
    # DashScope text-embedding-v3 单次最多 10 条；超出会 400 InvalidParameter（代码内钳制）
    EMBEDDING_BATCH_SIZE: int = 10

    # ---- Reranker（重排：DashScope text-rerank；对应 TS RerankerService）----
    # 字符串而非 bool，与 TS `config.get(..., 'true') !== 'false'` 判定一致：
    # 仅严格等于 "false" 才关闭重排。
    RAG_RERANK_ENABLED: str = "true"
    # API Key fallback 链与 TS 一致：RERANK_API_KEY → DASHSCOPE_API_KEY → OPENAI_API_KEY
    RERANK_API_KEY: str | None = None
    # 模型 ID 以百炼控制台为准（qwen3-rerank 等；写错会 400）
    RAG_RERANK_MODEL: str = "qwen3-rerank"
    # DashScope 原生（非 OpenAI 兼容）端点前缀，重排走专用 /api/v1/services/rerank/...
    RERANK_BASE_URL: str = "https://dashscope.aliyuncs.com"

    @property
    def RERANK_ENDPOINT(self) -> str:
        """text-rerank 专用端点（去掉尾部斜杠再拼路径）"""
        return (
            f"{self.RERANK_BASE_URL.rstrip('/')}"
            f"/api/v1/services/rerank/text-rerank/text-rerank"
        )

    # ---- 混合检索（对应 TS HybridRetrievalService：RAG_HYBRID_TOP_K / RAG_RRF_C）----
    # 多路召回每路的候选数 / 默认返回条数（RRF 前召回规模）
    RAG_HYBRID_TOP_K: int = 20
    # RRF 常数（与 ES RRF 默认一致）：score = Σ 1/(C + rank)
    RAG_RRF_C: int = 60

    # ---- RabbitMQ（发布后异步管线：RAG / KG / ES）----
    RABBITMQ_ENABLED: bool = True
    # 本地开发走宿主机映射端口 5672；容器内由 compose 覆盖为 amqp://rabbitmq:5672
    RABBITMQ_URL: str = "amqp://guest:guest@localhost:5672"
    RABBITMQ_CONNECT_TIMEOUT_MS: int = 15000

    # ---- RAG 分块（对应 TS RAG_CHUNK_SIZE/OVERLAP，token 数）----
    RAG_CHUNK_SIZE: int = 512
    RAG_CHUNK_OVERLAP: int = 64

    # ---- Elasticsearch（全文/关键词检索；向量存 PG，不写 dense_vector）----
    ELASTICSEARCH_ENABLED: bool = True
    # 本地开发走宿主机映射端口 9200；容器内部署时覆盖为 http://es:9200
    ELASTICSEARCH_NODE: str = "http://localhost:9200"

    # ---- KG 抽取（LLM 实体/关系抽取，DeepSeek OpenAI 兼容端点；对应 TS ExtractionService）----
    # API Key fallback 链与 TS 一致：OPENAI_API_KEY → LLM_API_KEY → DASHSCOPE_API_KEY
    LLM_API_KEY: str | None = None
    # DeepSeek OpenAI 兼容端点
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    # 模型：deepseek-v4-flash（DeepSeek-V4-Flash）
    LLM_MODEL: str = "deepseek-v4-flash"
    # 单 chunk 最多实体 / 关系数，防止图爆炸
    KG_MAX_ENTITIES: int = 12
    KG_MAX_RELATIONS: int = 15
    # 单次抽取 LLM 超时（毫秒）
    KG_LLM_TIMEOUT_MS: int = 60000

    # ---- AI 对话（RAG 问答：混合检索 → LLM 作答；对应 TS AiChatService）----
    # 大模型 DeepSeek，复用上面的 LLM_BASE_URL / LLM_MODEL；逐个可独立覆盖。
    OPENAI_API_KEY: str | None = None
    OPENAI_BASE_URL: str | None = None
    # 模型覆盖：MODEL_NAME → LLM_MODEL（默认 deepseek-v4-flash）
    MODEL_NAME: str | None = None
    # LLM 单次作答超时（毫秒，对应 TS AI_CHAT_TIMEOUT_MS）
    AI_CHAT_TIMEOUT_MS: int = 60000

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

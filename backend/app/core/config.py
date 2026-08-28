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

    # ---- 应用基础 ----
    APP_NAME: str = "企业智能知识库系统"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"

    # ---- 服务器 ----
    HOST: str = "0.0.0.0"
    PORT: int = 8000

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

    # ---- Embedding（发布索引：文本向量，对齐架构 1024 维）----
    EMBEDDING_PROVIDER: str = "local"  # local=确定性伪向量(开发/测试) | openai=OpenAI 兼容 API
    EMBEDDING_BASE_URL: str | None = None
    EMBEDDING_API_KEY: str | None = None
    EMBEDDING_MODEL: str = "text-embedding-v3"
    EMBEDDING_DIM: int = 1024


@lru_cache
def get_settings() -> Settings:
    """获取配置单例"""
    return Settings()


settings = get_settings()

"""
FastAPI 应用入口
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import models  # noqa: F401  确保所有 ORM 模型注册到 Base.metadata
from app.api.v1 import api_router
from app.core.config import settings
from app.db.mongo import close_mongo_client
from app.db.session import close_db_engine, engine
from app.models.base import Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时幂等建表，退出时关闭 Postgres 连接池与 Mongo 客户端"""
    # 幂等建表（CREATE TABLE IF NOT EXISTS 语义）：
    # 不依赖 init.sql 是否执行过，任何环境启动都能确保表存在
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await close_db_engine()
    await close_mongo_client()


def create_app() -> FastAPI:
    """创建应用实例"""
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="企业智能知识库系统 - 后端 API",
        docs_url="/docs" if settings.DEBUG else None,
        redoc_url="/redoc" if settings.DEBUG else None,
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 路由
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    @app.get("/")
    async def root():
        return {
            "service": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "docs": "/docs" if settings.DEBUG else "disabled",
        }

    return app


app = create_app()

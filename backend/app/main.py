"""
FastAPI 应用入口
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import models  # noqa: F401  确保所有 ORM 模型注册到 Base.metadata
from app.api.v1 import api_router
from app.core.config import settings
from app.db.es import es_service
from app.db.mongo import close_mongo_client
from app.db.session import close_db_engine, engine
from app.models.base import Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时幂等建表 + 连接 MQ/ES，退出时关闭各资源"""
    from app.mq.consumer import register_consumers
    from app.mq.rabbitmq import rabbitmq_service

    # 幂等建表（CREATE TABLE IF NOT EXISTS 语义）：
    # 不依赖 init.sql 是否执行过，任何环境启动都能确保表存在
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # ES：健康检查，不可用只降级不阻断启动（索引写入时跳过）
    await es_service.start()
    # MQ：先注册消费者再连接；连接失败抛错 → 应用启动失败（与 TS onModuleInit 一致）
    register_consumers()
    await rabbitmq_service.start()
    yield
    await close_db_engine()
    await close_mongo_client()
    await es_service.close()
    await rabbitmq_service.close()


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

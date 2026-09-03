"""
FastAPI 应用入口
"""

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import models  # noqa: F401  确保所有 ORM 模型注册到 Base.metadata
from app.api.auth_route import auth_router, check_route_classes, public
from app.api.v1 import api_router
from app.core.config import settings
from app.db.es import es_service
from app.db.mongo import close_mongo_client
from app.db.redis import redis_service
from app.db.session import close_db_engine, engine
from app.models.base import Base
from app.services.graph_build import graph_build_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时幂等建表 + 连接 MQ/ES/Neo4j，退出时关闭各资源"""
    from app.mq.consumer import register_consumers
    from app.mq.rabbitmq import rabbitmq_service

    # 建表兜底（CREATE TABLE IF NOT EXISTS 语义——只建不 ALTER）：
    # schema 真相源是 init-scripts/postgresql/*.sql。加字段 / 改类型请先改那里的 DDL，
    # 再同步 ORM 模型；这里只补表是否存在，不会演进已有表结构。
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # ES：健康检查，不可用只降级不阻断启动（索引写入时跳过）
    await es_service.start()
    # Neo4j：连通性验证，不可用只降级不阻断启动（KG 写入时跳过）
    await graph_build_service.start()
    # Redis：验证码等短时存储；连接失败只告警不阻断启动（后续命令自动重连）
    await redis_service.start()
    # MQ：先注册消费者再连接；连接失败抛错 → 应用启动失败（与 TS onModuleInit 一致）
    register_consumers()
    await rabbitmq_service.start()
    # 鉴权自检：告警「route_class 非 AuthRoute 因而不受保护」的路由（漏写会静默裸奔）
    check_route_classes(app)
    yield
    await close_db_engine()
    await close_mongo_client()
    await es_service.close()
    await redis_service.close()
    await graph_build_service.close()
    await rabbitmq_service.close()


def _inject_bearer_security_scheme(app: FastAPI) -> None:
    """往 OpenAPI 的 components.securitySchemes 注入 HTTPBearer

    鉴权由 AuthRoute 在依赖系统之外完成，FastAPI 不会自动注册 security scheme，
    不注入的话 Swagger 里受保护接口引用的 scheme 会悬空（锁图标不显示）。
    """
    original_openapi = app.openapi

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema  # type: ignore[return-value]
        schema = original_openapi()
        schemes = schema.setdefault("components", {}).setdefault(
            "securitySchemes", {}
        )
        schemes.setdefault("HTTPBearer", {"type": "http", "scheme": "bearer"})
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


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

    # 受保护路由的 OpenAPI 里写了 security: [{HTTPBearer: []}]（见 AuthRoute.__init__），
    # 这里补上对应的 scheme 定义，Swagger 才会显示锁图标并启用 Authorize 按钮。
    _inject_bearer_security_scheme(app)

    # 根路由：FastAPI 的 app.get() 不支持 route_class 参数（只有 APIRouter 支持），
    # 所以单独建一个 router 挂上去，保证它同样走 AuthRoute 的自检口径。
    root_router = auth_router()

    @root_router.get("/")
    @public()
    async def root():
        return {
            "service": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "docs": "/docs" if settings.DEBUG else "disabled",
        }

    app.include_router(root_router)

    return app


app = create_app()

"""
API v1 路由（对应 NestJS 各业务 Module 的路由装配）
"""

from fastapi import APIRouter

from app.api.v1 import document, health

api_router = APIRouter()

api_router.include_router(health.router, prefix="/health", tags=["健康检查"])
api_router.include_router(document.router, prefix="/documents", tags=["文档"])

"""
API v1 路由（对应 NestJS 各业务 Module 的路由装配）

鉴权：所有子 router 用 auth_router()（= APIRouter(route_class=AuthRoute)），
默认**全部需要登录**，公开接口在 endpoint 上标 @public()。
见 app/api/auth_route.py 的说明。
"""

from app.api.auth_route import auth_router
from app.api.v1 import auth, document, health, permissions, role, teams, users

api_router = auth_router()

api_router.include_router(health.router, prefix="/health", tags=["健康检查"])
api_router.include_router(auth.router, prefix="/auth", tags=["认证"])
api_router.include_router(document.router, prefix="/documents", tags=["文档"])
api_router.include_router(role.router, prefix="/roles", tags=["角色管理"])
api_router.include_router(users.router, prefix="/users", tags=["用户管理"])
api_router.include_router(permissions.router, prefix="/permissions", tags=["权限管理"])
api_router.include_router(teams.router, prefix="/teams", tags=["团队管理"])

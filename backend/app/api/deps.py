"""
当前登录用户依赖

真正的拦截在 app/api/auth_route.py 的 AuthRoute（等价 JwtAuthGuard + RolesGuard）：
那里完成验签、查库、角色校验，并把结果写入 request.state.user。

本模块只负责把已解析的用户取出来给路由函数用 —— 从 request.state 读，**不重复验签、
不重复查库**。未走守卫时（如裸 APIRouter 或单测里直接构造的 app）回退为自行解析，
保证行为一致。

角色校验统一用 app/api/auth_route.py 的 @roles(...) 装饰器（等价 @Roles + RolesGuard），
不再提供 require_roles 依赖 —— 两套机制并存会让「这个接口到底受什么保护」变得难判断。
"""

from fastapi import Request

from app.api.auth_route import authenticate_request
from app.schemas.auth import AuthUser

__all__ = ["get_current_user"]


async def get_current_user(request: Request) -> AuthUser:
    """当前登录用户

    用于需要 userId 的路由（如 `GET /auth/me`）。路由必须挂在 AuthRoute 上，
    否则守卫不会执行，这里会回退为自行解析。
    """
    user = getattr(request.state, "user", None)
    if isinstance(user, AuthUser):
        return user
    return await authenticate_request(request)

"""细粒度权限守卫依赖（对应 TS @RequirePermission + PermissionsGuard）

TS 用 SetMetadata(PERMISSIONS_KEY, codes) + PermissionsGuard 读取并放行：用户拥有
codes 中任一 permission_code 即通过。ADMIN 角色由 getUserPermissionCodes 自动追加
ADMIN_OPERATION_PERMISSIONS，因此**此处无需单独 blanket bypass 管理员**（与 TS 行为一致：
管理员仅自动获得列表内的操作权限码，而非任意权限码）。

AuthRoute 已在进入路由前完成登录校验并把当前用户写入 request.state.user（含 roles），
本依赖直接复用，不再重复验签。

用法（与 @roles(ADMIN) 组合，等价 TS 类级 @Roles(ADMIN) + 方法级 @RequirePermission）：
    @router.get("/list", dependencies=[Depends(require_permission("system:permission"))])
    @roles(RoleCode.ADMIN)
"""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.schemas.auth import AuthUser
from app.services.permission import permission_service

_FORBIDDEN = "权限不足"


def require_permission(*codes: str) -> Callable[..., Coroutine[Any, Any, None]]:
    """要求当前用户拥有 codes 中至少一个权限码（对应 @RequirePermission(...)）

    返回 FastAPI 依赖可包裹的协程；在路由上用
    `dependencies=[Depends(require_permission("..."))]` 挂载。
    """

    if not codes:
        raise ValueError("require_permission() 至少需要传入一个权限码")

    async def checker(
        request: Request,
        session: AsyncSession = Depends(get_db_session),
    ) -> None:
        user = getattr(request.state, "user", None)
        if not isinstance(user, AuthUser):
            # 理论上 AuthRoute 已保证登录；兜底 401 防止守卫被误用到公开路由
            raise HTTPException(status_code=401, detail="未登录或 token 已失效")
        granted = await permission_service.get_user_permission_codes(session, user.user_id)
        if not (set(codes) & set(granted)):
            raise HTTPException(status_code=403, detail=_FORBIDDEN)

    return checker

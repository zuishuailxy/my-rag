"""细粒度权限守卫依赖

(PERMISSIONS_KEY, codes) + PermissionsGuard 读取并放行：用户拥有
codes 中任一 permission_code 即通过。ADMIN 角色由 getUserPermissionCodes 自动追加
ADMIN_OPERATION_PERMISSIONS，因此**此处无需单独 blanket bypass 管理员**。

AuthRoute 已在进入路由前完成登录校验并把当前用户写入 request.state.user（含 roles），
本依赖直接复用，不再重复验签。

用法（与 @roles(ADMIN) 组合) + 方法级 @RequirePermission）：
    @router.get("/list", dependencies=[Depends(require_permission("system:permission"))])
    @roles(RoleCode.ADMIN)
"""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException, Request

from app.db.session import async_session_factory
from app.schemas.auth import AuthUser
from app.services.permission import permission_service

_FORBIDDEN = "权限不足"


def require_permission(*codes: str) -> Callable[..., Coroutine[Any, Any, None]]:
    """要求当前用户拥有 codes 中至少一个权限码（对应 @RequirePermission(）

    返回 FastAPI 依赖可包裹的协程；在路由上用
    `dependencies=[Depends(require_permission("..."))]` 挂载。
    """

    if not codes:
        raise ValueError("require_permission() 至少需要传入一个权限码")

    async def checker(request: Request) -> None:
        user = getattr(request.state, "user", None)
        if not isinstance(user, AuthUser):
            # 理论上 AuthRoute 已保证登录；兑底 401 防止守卫被误用到公开路由
            raise HTTPException(status_code=401, detail="未登录或 token 已失效")

        # ⚠️ 刻意用**短生命周期会话**，而不是 `Depends(get_db_session)`。
        # FastAPI 的生成器依赖会活到**响应结束**（流式就是整个流），所以用
        # `Depends(get_db_session)` 时：权限查询一跑，会话/事务/连接就被占住整条链路
        # （包括后面几秒的检索 + LLM 生成）。
        #
        # 压测实测：并发 40 时峰值 40 条连接里有 **20 条 `idle in transaction`**，
        # 它们的最后一条 SQL 正是这里的权限查询 —— 连接在「开着事务空等 LLM」。
        # 权限检查只需要一次查询，查完就还连接。
        async with async_session_factory() as session:
            granted = await permission_service.get_user_permission_codes(
                session, user.user_id
            )
        if not (set(codes) & set(granted)):
            raise HTTPException(status_code=403, detail=_FORBIDDEN)

    return checker

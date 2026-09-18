"""
全局鉴权守卫（对应 TS JwtAuthGuard + JwtStrategy + RolesGuard + @Public / @Roles）

实现方式：**自定义 APIRoute（route_class）**，不是中间件。

为什么不用中间件
────────────────
BaseHTTPMiddleware（含 @app.middleware("http")）是纯 ASGI 中间件，运行在**路由匹配之前**，
那一刻 scope 里只有 path，没有 route / endpoint（已实测：has_route=False），因此读不到
@Public() 打在 endpoint 函数上的标记，只能退化成硬编码路径白名单。白名单与路由定义分离在
两处，新增公开路由忘了同步 → 接口被自己的守卫拦成 401，而路由文件里看不出任何异常。

route_class 是唯一能在「路由匹配之后、依赖解析之前」读到 endpoint 元数据的扩展点，
语义等价于 NestJS 的 `Reflector.getAllAndOverride(key, [handler, class])`。

用法
────
    from app.api.auth_route import AuthRoute, public, roles

    router = APIRouter(route_class=AuthRoute)

    @router.post("/login")
    @public()                                    # 免登录
    async def login(...): ...

    @router.get("/reviewer-ids")
    @roles(RoleCode.ADMIN, RoleCode.REVIEWER)    # 需登录 + 角色之一
    async def reviewer_ids(...): ...

默认行为：**未标 @public() 的路由一律需要登录** —— 这是刻意的 fail-safe 设计，
忘记标记 = 被保护，不会裸奔；反过来（默认公开、逐个标记需登录）漏一个就是永久漏洞。

⚠️ FastAPI 0.110 的 include_router 用 `route_class_override=type(route)` 保留**子路由自己**
的 route class，**不会**继承父 router 的 route_class。所以每个子 router 都必须显式写
`APIRouter(route_class=AuthRoute)`，漏写的 router 会整组失去保护 —— 由启动时调用的
`check_route_classes()` 兜底告警。

与 TS 的对应关系
────────────────
  @Public()                      → @public()
  @Roles(...) + RolesGuard       → @roles(...)
  JwtAuthGuard（APP_GUARD 全局） → AuthRoute.get_route_handler（默认拦截）
  JwtStrategy.validate           → authenticate_request()（验签 + type=access + 查库）
  @CurrentUser()                 → Depends(get_current_user)（复用 request.state.user）

文案与 TS 逐条对齐：
  401「未登录或 token 已失效」  ← JwtAuthGuard.handleRequest（err || !user）
  401「无效的 access token」    ← JwtStrategy.validate（payload.type !== 'access'）
  403「权限不足」               ← RolesGuard（角色未命中 / 用户无角色）

注意：build_auth_user 抛出的 404「用户不存在」/ 401「账户已禁用」**原样透出**，不在这里
兜成 401 —— 对应 TS handleRequest 里的 `throw err`，即「token 有效但用户已软删」在两侧
都是 404 而非 401。

另一个语义细节：@public() 与 @roles() 同时使用时以 @roles() 为准（仍需登录）。
TS 里这种组合会因为 request.user 不存在而恒定 403，属调用方错误，此处不复制该行为。
"""

import logging
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import jwt
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.db.session import async_session_factory
from app.schemas.auth import AuthUser
from app.services.auth import auth_service

logger = logging.getLogger("AuthRoute")

# endpoint 上挂载的元数据键（等价 NestJS SetMetadata 的 key）
_IS_PUBLIC_KEY = "_auth_is_public"
_ROLES_KEY = "_auth_required_roles"

# 错误文案（与 TS guard 源码逐条对齐，改这里前先看 src/auth/*.guard.ts）
_NOT_AUTHENTICATED = "未登录或 token 已失效"
_INVALID_ACCESS_TOKEN = "无效的 access token"
_FORBIDDEN = "权限不足"

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}

_F = TypeVar("_F", bound=Callable[..., Any])

# auto_error=False：关掉 FastAPI 默认的 403「Not authenticated」，
# 缺 token / token 非法统一由下面抛 401（NestJS guard 行为）
_bearer = HTTPBearer(auto_error=False)


def auth_router(**kwargs: Any) -> APIRouter:
    """创建默认挂载 AuthRoute 的 APIRouter

    等价于 `APIRouter(route_class=AuthRoute, **kwargs)`，但不会漏写 route_class ——
    漏写的后果是该 router 下**所有路由失去保护**，且从路由文件看不出来（见 check_route_classes）。
    """
    kwargs.setdefault("route_class", AuthRoute)
    return APIRouter(**kwargs)


def public() -> Callable[[_F], _F]:
    """标记接口无需登录（等价 @Public()）"""

    def decorator(func: _F) -> _F:
        setattr(func, _IS_PUBLIC_KEY, True)
        return func

    return decorator


def roles(*role_codes: str) -> Callable[[_F], _F]:
    """要求用户拥有指定角色之一（等价 @Roles(...) + RolesGuard）；不满足 403

    隐含要求登录。与 @public() 同时使用时以本装饰器为准。
    """
    if not role_codes:
        raise ValueError("roles() 至少需要传入一个角色编码")

    def decorator(func: _F) -> _F:
        setattr(func, _ROLES_KEY, tuple(role_codes))
        return func

    return decorator


def _unauthorized(detail: str) -> HTTPException:
    """构造带 WWW-Authenticate 头的 401（与 NestJS 的 Bearer 挑战一致）"""
    return HTTPException(
        status_code=401, detail=detail, headers=_UNAUTHORIZED_HEADERS
    )


async def authenticate_request(request: Request) -> AuthUser:
    """解析并校验 Bearer access token（等价 JwtStrategy.validate）

    顺序与 TS 一致：验签 + 验 exp → 校验 type=access → 按 sub 查库组装。
    build_auth_user 抛出的 404「用户不存在」/ 401「账户已禁用」原样透出。
    """
    credentials: HTTPAuthorizationCredentials | None = await _bearer(request)
    if credentials is None:
        raise _unauthorized(_NOT_AUTHENTICATED)
    try:
        payload = jwt.decode(
            credentials.credentials, settings.JWT_SECRET, algorithms=["HS256"]
        )
    except jwt.PyJWTError:
        # 过期 / 签名不符 / 格式非法统一文案，不泄露具体原因（TS catch-all 同款）
        raise _unauthorized(_NOT_AUTHENTICATED) from None
    # refresh token 不能当 access token 用（TS JwtStrategy 只认 type='access'）
    if payload.get("type") != "access":
        raise _unauthorized(_INVALID_ACCESS_TOKEN)
    sub = payload.get("sub")
    if not isinstance(sub, str):
        # 刻意优于 TS：TS 会拿 undefined 去查 bigint 主键 → PG 22P02 → 500
        raise _unauthorized(_NOT_AUTHENTICATED)

    # 守卫跑在依赖解析之前，拿不到 Depends(get_db_session) 的 session，需自建。
    # 解析结果写入 request.state.user，由 get_current_user 依赖复用，避免二次查库。
    async with async_session_factory() as session:
        return await auth_service.build_auth_user(session, sub)


class AuthRoute(APIRoute):
    """带全局鉴权的路由类（等价 NestJS 的 JwtAuthGuard + RolesGuard）"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not bool(getattr(self.endpoint, _IS_PUBLIC_KEY, False)):
            # 仅供 OpenAPI 展示（Swagger 的锁图标 / Authorize 按钮）。
            # 真正的鉴权在 get_route_handler 里做，不走依赖系统，因此不会重复验签；
            # securitySchemes 里的 HTTPBearer 由 main.create_app 注入。
            self.openapi_extra = {
                **(self.openapi_extra or {}),
                "security": [{_bearer.scheme_name: []}],
            }

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_route_handler = super().get_route_handler()
        is_public = bool(getattr(self.endpoint, _IS_PUBLIC_KEY, False))
        required_roles: tuple[str, ...] = tuple(
            getattr(self.endpoint, _ROLES_KEY, ())
        )

        if is_public and not required_roles:
            return original_route_handler

        async def auth_route_handler(request: Request) -> Response:
            user = await authenticate_request(request)
            # 供 Depends(get_current_user) 复用（等价 @CurrentUser() 的 request.user）
            request.state.user = user
            if required_roles and not set(required_roles) & set(user.roles):
                raise HTTPException(status_code=403, detail=_FORBIDDEN)
            return await original_route_handler(request)

        return auth_route_handler


def check_route_classes(app: FastAPI) -> None:
    """启动自检：告警「未使用 AuthRoute 因而完全不受保护」的路由

    FastAPI 0.110 的 include_router 不继承父 router 的 route_class，漏写
    `APIRouter(route_class=AuthRoute)` 的 router 会整组失去保护，且从路由文件看不出异常。
    这里在启动时兜底。
    """
    total = 0
    unprotected: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        total += 1
        if not isinstance(route, AuthRoute):
            methods = ",".join(sorted(route.methods))
            unprotected.append(f"{methods} {route.path}")

    if unprotected:
        logger.warning(
            "[AuthRoute] 以下路由未启用鉴权守卫（route_class 非 AuthRoute）：routes=%s",
            unprotected,
        )
    else:
        logger.info("鉴权守卫已覆盖全部 APIRoute：count=%d", total)


__all__ = [
    "AuthRoute",
    "auth_router",
    "authenticate_request",
    "check_route_classes",
    "public",
    "roles",
]

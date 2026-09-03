"""用户管理接口（对应 NestJS UserController）

- PUT    /users/me                  当前用户更新资料（需登录）
- GET    /users/me/stats            当前用户统计（需登录）
- PUT    /users/password/change     修改密码（需登录）
- GET    /users/page                用户分页列表（仅 ADMIN）
- POST   /users                     创建用户（仅 ADMIN，201）
- GET    /users/{user_id}           用户详情（仅 ADMIN）
- PUT    /users/{user_id}           管理员更新用户（仅 ADMIN）
- DELETE /users/{user_id}           软删除用户（仅 ADMIN）
- PUT    /users/{user_id}/password/reset  重置密码（仅 ADMIN）
- GET    /users/{user_id}/roles     用户角色码（仅 ADMIN）
- PUT    /users/{user_id}/roles     全量替换角色（仅 ADMIN）
- GET    /users/{user_id}/permissions       用户权限码（仅 ADMIN + system:user）
- PUT    /users/{user_id}/permissions       全量替换用户直接授权（仅 ADMIN + system:user）

路由顺序：/me、/me/stats、/password/change、/page 是字面量段，必须注册在
/users/{user_id} 之前（否则被动态路径吞掉），与 role/document 路由约定一致；
POST /users 与 /{user_id} 组方法不同（POST vs GET/PUT/DELETE）本无冲突，
仍按约定放在动态段之前。

状态码：NestJS @Post 默认 201 其余 200（本组仅 createUser 是 POST → 201）。
鉴权：TS 逐方法标 @Roles(ADMIN) → 对应端点 @roles(RoleCode.ADMIN)；
me / me/stats / password/change 只需登录（TS 未标 @Roles）。

createUser 响应体：TS 控制器拿 service 返回的 userId 再调 getUserVO 组装
（两次服务调用），Python 同款先 create 后查 VO，不合并为单次调用。
"""

from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth_route import auth_router, roles
from app.api.deps import get_current_user
from app.api.permission_guard import require_permission
from app.common.roles import RoleCode
from app.db.session import get_db_session
from app.schemas.auth import AuthUser
from app.schemas.permission import AssignPermissionIdsRequest
from app.schemas.user import (
    AssignRolesRequest,
    ChangePasswordRequest,
    CreateUserRequest,
    ProfileUpdateRequest,
    ResetPasswordRequest,
    UpdateUserRequest,
    UserPermissionCodesResponse,
    UserPermissionIdsResponse,
    UserRoleCodesResponse,
    UserPageQuery,
    UserPageResponse,
    UserStatsResponse,
    UserVOResponse,
)
from app.services.permission import permission_service
from app.services.user import user_service

router = auth_router()

_DB = Annotated[AsyncSession, Depends(get_db_session)]
_ME = Annotated[AuthUser, Depends(get_current_user)]


def _vo(data: dict[str, object]) -> UserVOResponse:
    """服务层 VO dict → 响应模型（camelCase 键直接喂给 alias 校验）"""
    return UserVOResponse.model_validate(data)


@router.put("/me", response_model=UserVOResponse)
async def update_me(user: _ME, dto: ProfileUpdateRequest, session: _DB) -> UserVOResponse:
    """当前用户更新资料（需登录；部分更新，未传字段不改动）"""
    return _vo(await user_service.update_profile(session, user.user_id, dto))


@router.get("/me/stats", response_model=UserStatsResponse)
async def get_my_stats(user: _ME, session: _DB) -> UserStatsResponse:
    """当前用户统计（文档数 / 浏览 / 点赞 / 评论量合计，均筛未删除）"""
    return UserStatsResponse.model_validate(
        await user_service.get_user_statistics(session, user.user_id)
    )


@router.put("/password/change")
async def change_password(
    user: _ME, dto: ChangePasswordRequest, session: _DB
) -> dict[str, str]:
    """修改密码（需登录；原密码错误 → 400「原密码错误」）"""
    await user_service.change_password(
        session, user.user_id, dto.old_password, dto.new_password
    )
    return {"message": "密码修改成功"}


@router.get("/page", response_model=UserPageResponse)
@roles(RoleCode.ADMIN)
async def page_users(
    session: _DB,
    keyword: Annotated[str | None, Query(description="用户名/姓名/邮箱 模糊匹配")] = None,
    role_code: Annotated[
        str | None, Query(alias="roleCode", description="按角色编码筛选")
    ] = None,
    status: Annotated[int | None, Query(description="状态：0 禁用 1 启用")] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[
        int, Query(alias="pageSize", ge=1, le=100, description="每页条数")
    ] = 20,
) -> UserPageResponse:
    """用户分页列表（仅 ADMIN；keyword 模糊 / status / roleCode 筛选，created_at 降序）"""
    query = UserPageQuery(
        keyword=keyword,
        role_code=role_code,
        status=status,
        page=page,
        page_size=page_size,
    )
    data = await user_service.page_users(session, query)
    return UserPageResponse.model_validate(data)


@router.post("", response_model=UserVOResponse, status_code=201)
@roles(RoleCode.ADMIN)
async def create_user(
    dto: CreateUserRequest, session: _DB
) -> UserVOResponse:
    """创建用户（仅 ADMIN；201；用户名重复 409 / 角色不存在 404）

    与 TS 控制器同款两步：service 只返回 userId，再查一次 getUserVO 组装响应。
    """
    user_id = await user_service.create_user(session, dto)
    return _vo(await user_service.get_user_vo(session, user_id))


@router.get("/{user_id}", response_model=UserVOResponse)
@roles(RoleCode.ADMIN)
async def get_user(user_id: str, session: _DB) -> UserVOResponse:
    """用户详情（仅 ADMIN；不存在 / id 非法 → 404「用户不存在」）"""
    return _vo(await user_service.get_user_vo(session, user_id))


@router.put("/{user_id}", response_model=UserVOResponse)
@roles(RoleCode.ADMIN)
async def update_user(
    user_id: str, dto: UpdateUserRequest, session: _DB
) -> UserVOResponse:
    """管理员更新用户（仅 ADMIN；部分更新；status 0/1 均接受与 TS 一致）"""
    return _vo(await user_service.update_user(session, user_id, dto))


@router.delete("/{user_id}")
@roles(RoleCode.ADMIN)
async def delete_user(user_id: str, session: _DB) -> dict[str, str]:
    """删除用户（仅 ADMIN；软删除 deleted=true，角色关联行保留与 TS 一致）"""
    await user_service.delete_user(session, user_id)
    return {"message": "删除成功"}


@router.put("/{user_id}/password/reset")
@roles(RoleCode.ADMIN)
async def reset_password(
    user_id: str, dto: ResetPasswordRequest, session: _DB
) -> dict[str, str]:
    """重置密码（仅 ADMIN；不校验原密码）"""
    await user_service.reset_password(session, user_id, dto.new_password)
    return {"message": "密码重置成功"}


@router.get("/{user_id}/roles", response_model=UserRoleCodesResponse)
@roles(RoleCode.ADMIN)
async def get_user_roles(user_id: str, session: _DB) -> UserRoleCodesResponse:
    """用户角色码（仅 ADMIN；用户不存在 → 404）"""
    role_codes = await user_service.get_user_role_codes(session, user_id)
    return UserRoleCodesResponse(userId=user_id, roleCodes=role_codes)


@router.put("/{user_id}/roles", response_model=UserRoleCodesResponse)
@roles(RoleCode.ADMIN)
async def assign_roles(
    user_id: str, dto: AssignRolesRequest, session: _DB
) -> UserRoleCodesResponse:
    """全量替换用户角色（仅 ADMIN；任一角色码不存在/禁用 → 404「角色不存在: ...」）"""
    role_codes = await user_service.replace_roles(session, user_id, dto.role_codes)
    return UserRoleCodesResponse(userId=user_id, roleCodes=role_codes)


@router.get(
    "/{user_id}/permissions",
    response_model=UserPermissionCodesResponse,
    dependencies=[Depends(require_permission("system:user"))],
)
@roles(RoleCode.ADMIN)
async def get_user_permissions(
    user_id: str, session: _DB
) -> UserPermissionCodesResponse:
    """用户权限码（仅 ADMIN + system:user）

    与 TS getUserPermissions 同款：先 findByIdOrThrow 校验用户存在（404），
    再查合并权限码（直接 + 角色）与直接授权 ID 列表。
    """
    await user_service.find_by_id_or_throw(session, user_id)
    permission_codes = await permission_service.get_user_permission_codes(session, user_id)
    direct_ids = await permission_service.get_user_direct_permission_ids(session, user_id)
    return UserPermissionCodesResponse(
        userId=user_id,
        permissionCodes=permission_codes,
        directPermissionIds=direct_ids,
    )


@router.put(
    "/{user_id}/permissions",
    response_model=UserPermissionIdsResponse,
    dependencies=[Depends(require_permission("system:user"))],
)
@roles(RoleCode.ADMIN)
async def assign_user_permissions(
    user_id: str, dto: AssignPermissionIdsRequest, session: _DB
) -> UserPermissionIdsResponse:
    """全量替换用户直接授权（仅 ADMIN + system:user；先删后插，空数组即清空）

    与 TS assignUserPermissions 同款：先 findByIdOrThrow 校验用户存在（404），
    再全量替换 UserPermission；permissionIds 含非法/已禁用权限 → 404。
    """
    await user_service.find_by_id_or_throw(session, user_id)
    permission_ids = await permission_service.assign_user_permissions(
        session, user_id, dto
    )
    return UserPermissionIdsResponse(userId=user_id, permissionIds=permission_ids)

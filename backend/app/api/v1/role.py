"""角色管理接口（对应 NestJS RoleController）

- GET    /roles/list        启用角色列表（不含 status）
- GET    /roles/{role_id}   角色详情（含 status）
- POST   /roles             创建角色（201）
- PUT    /roles/{role_id}   更新角色
- DELETE /roles/{role_id}   删除角色（硬删除；仍有关联用户 400）
- GET    /roles/{role_id}/permissions   角色已授权权限 ID 列表
- PUT    /roles/{role_id}/permissions   全量替换角色权限

鉴权：TS 在 **Controller 类级**标 @Roles(RoleCode.ADMIN) → 本 router 全部接口
@roles(RoleCode.ADMIN)（需登录 + ADMIN 角色，403 早于业务 400/404）。
/permissions 两个端点额外叠 @RequirePermission('system:role') 细粒度权限守卫
（dependencies=[Depends(require_permission(...))]），与 TS 双层守卫一致。

路由顺序：`/roles/list` 是字面量段，必须注册在 `/roles/{role_id}` 之前
（否则会被动态路径吞掉）；`/{role_id}/permissions` 是带静态后缀的二级段，
不与 `/{role_id}` 冲突，但注册在 `/{role_id}` 之后更符合阅读顺序，与
document 路由的同款约定一致。

状态码：NestJS @Post 默认 201，其余 200。
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth_route import auth_router, roles
from app.api.permission_guard import require_permission
from app.common.roles import RoleCode
from app.db.session import get_db_session
from app.models.role import Role
from app.schemas.permission import AssignPermissionIdsRequest
from app.schemas.role import (
    RoleBriefResponse,
    RoleCreateRequest,
    RoleDetailResponse,
    RolePermissionIdsResponse,
    RoleUpdateRequest,
)
from app.services.permission import permission_service
from app.services.role import role_service

router = auth_router()

_DB = Annotated[AsyncSession, Depends(get_db_session)]


def _brief(role: Role) -> RoleBriefResponse:
    """实体 → 简要响应（bigint 转字符串；list/create 不回 status，对齐 TS 内联对象）"""
    return RoleBriefResponse(
        id=str(role.id),
        role_name=role.role_name,
        role_code=role.role_code,
        description=role.description,
    )


def _detail(role: Role) -> RoleDetailResponse:
    """实体 → 完整响应（get/update 多回 status）"""
    return RoleDetailResponse(
        id=str(role.id),
        role_name=role.role_name,
        role_code=role.role_code,
        description=role.description,
        status=role.status,
    )


@router.get("/list", response_model=list[RoleBriefResponse])
@roles(RoleCode.ADMIN)
async def list_roles(session: _DB) -> list[RoleBriefResponse]:
    """启用角色列表（仅 ADMIN；按 roleName 升序，不含已禁用角色）"""
    roles_ = await role_service.list_all(session)
    return [_brief(r) for r in roles_]


@router.get("/{role_id}", response_model=RoleDetailResponse)
@roles(RoleCode.ADMIN)
async def get_role(role_id: str, session: _DB) -> RoleDetailResponse:
    """角色详情（仅 ADMIN；不存在 / id 非法 → 404「角色不存在」）"""
    return _detail(await role_service.get_by_id(session, role_id))


@router.post("", status_code=201, response_model=RoleBriefResponse)
@roles(RoleCode.ADMIN)
async def create_role(dto: RoleCreateRequest, session: _DB) -> RoleBriefResponse:
    """创建角色（仅 ADMIN；roleCode 重复 → 409「角色编码已存在」，初始 status=1）"""
    return _brief(await role_service.create(session, dto))


@router.put("/{role_id}", response_model=RoleDetailResponse)
@roles(RoleCode.ADMIN)
async def update_role(
    role_id: str, dto: RoleUpdateRequest, session: _DB
) -> RoleDetailResponse:
    """更新角色（仅 ADMIN；部分更新，未传字段不改动）"""
    return _detail(await role_service.update(session, role_id, dto))


@router.delete("/{role_id}")
@roles(RoleCode.ADMIN)
async def delete_role(role_id: str, session: _DB) -> dict[str, str]:
    """删除角色（仅 ADMIN；硬删除，仍有关联用户 → 400）"""
    await role_service.delete(session, role_id)
    return {"message": "删除成功"}


@router.get(
    "/{role_id}/permissions",
    response_model=RolePermissionIdsResponse,
    dependencies=[Depends(require_permission("system:role"))],
)
@roles(RoleCode.ADMIN)
async def get_role_permissions(
    role_id: str, session: _DB
) -> RolePermissionIdsResponse:
    """角色已授权权限 ID 列表（仅 ADMIN + system:role；role 不存在 → 404）"""
    permission_ids = await permission_service.get_role_permission_ids(session, role_id)
    return RolePermissionIdsResponse(role_id=role_id, permission_ids=permission_ids)


@router.put(
    "/{role_id}/permissions",
    response_model=RolePermissionIdsResponse,
    dependencies=[Depends(require_permission("system:role"))],
)
@roles(RoleCode.ADMIN)
async def assign_role_permissions(
    role_id: str, dto: AssignPermissionIdsRequest, session: _DB
) -> RolePermissionIdsResponse:
    """全量替换角色权限（仅 ADMIN + system:role；role/权限不存在 → 404）"""
    permission_ids = await permission_service.assign_role_permissions(
        session, role_id, dto
    )
    return RolePermissionIdsResponse(role_id=role_id, permission_ids=permission_ids)

"""权限管理路由

双层守卫（) + 方法级 @RequirePermission）：
  - 类级 @Roles(ADMIN) → 每个端点 @roles(RoleCode.ADMIN)
  - 方法级 @RequirePermission(code) → 对应端点加
    dependencies=[Depends(require_permission(code))]

路由顺序：/list、/tree、/page、/:id/children 是字面量或特殊段，必须注册在
/:permission_id 动态段之前（与 role / document 路由约定一致，否则被动态路径吞掉）。

状态码：POST create → 201；其余 200。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth_route import auth_router, roles
from app.api.permission_guard import require_permission
from app.common.roles import RoleCode
from app.db.session import get_db_session
from app.models.permission import Permission
from app.schemas.permission import (
    CreatePermissionRequest,
    PermissionPageResponse,
    PermissionTreeResponse,
    PermissionVOResponse,
    QueryPermissionRequest,
    UpdatePermissionRequest,
)
from app.services.permission import permission_service

router = auth_router()

_DB = Annotated[AsyncSession, Depends(get_db_session)]


def _vo(p: Permission) -> PermissionVOResponse:
    """实体 → 响应 VO"""
    return PermissionVOResponse(
        id=str(p.id),
        parent_id=str(p.parent_id),
        permission_name=p.permission_name,
        permission_code=p.permission_code,
        permission_type=p.permission_type,
        menu_url=p.menu_url,
        api_url=p.api_url,
        method=p.method,
        icon=p.icon,
        sort=p.sort,
        status=p.status,
    )


@router.get(
    "/list",
    response_model=list[PermissionVOResponse],
    dependencies=[Depends(require_permission("system:permission"))],
)
@roles(RoleCode.ADMIN)
async def list_permissions(session: _DB) -> list[PermissionVOResponse]:
    """启用权限列表（仅 ADMIN + system:permission）"""
    perms = await permission_service.list_all(session)
    return [_vo(p) for p in perms]


@router.get(
    "/tree",
    response_model=list[PermissionTreeResponse],
    dependencies=[Depends(require_permission("system:permission"))],
)
@roles(RoleCode.ADMIN)
async def permission_tree(session: _DB) -> list[PermissionTreeResponse]:
    """权限树（根 parentId='0' 递归展开 children）"""
    perms = await permission_service.list_all(session)
    children_map: dict[str, list[Permission]] = {}
    for p in perms:
        children_map.setdefault(str(p.parent_id), []).append(p)

    def build(parent_id: str) -> list[PermissionTreeResponse]:
        nodes: list[PermissionTreeResponse] = []
        for p in children_map.get(parent_id, []):
            node = PermissionTreeResponse(
                **_vo(p).model_dump(), children=build(str(p.id))
            )
            nodes.append(node)
        return nodes

    return build("0")


@router.get(
    "/page",
    response_model=PermissionPageResponse,
    dependencies=[Depends(require_permission("system:permission"))],
)
@roles(RoleCode.ADMIN)
async def page_permissions(
    session: _DB,
    keyword: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
) -> PermissionPageResponse:
    """权限分页（keyword 模糊匹配 permission_name / permission_code）"""
    query = QueryPermissionRequest(keyword=keyword, page=page, page_size=page_size)
    result = await permission_service.page(session, query)
    return PermissionPageResponse(
        items=[_vo(p) for p in result["items"]],
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


@router.get(
    "/{permission_id}/children",
    response_model=list[PermissionVOResponse],
    dependencies=[Depends(require_permission("system:permission"))],
)
@roles(RoleCode.ADMIN)
async def children_permissions(
    session: _DB, permission_id: str
) -> list[PermissionVOResponse]:
    """某父权限下的子权限列表"""
    perms = await permission_service.get_children(session, permission_id)
    return [_vo(p) for p in perms]


@router.get(
    "/{permission_id}",
    response_model=PermissionVOResponse,
    dependencies=[Depends(require_permission("system:permission"))],
)
@roles(RoleCode.ADMIN)
async def get_permission(
    session: _DB, permission_id: str
) -> PermissionVOResponse:
    """权限详情（非法 / 不存在 / 已删 → 404）"""
    perm = await permission_service.get_by_id(session, permission_id)
    return _vo(perm)


@router.post(
    "",
    response_model=PermissionVOResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("system:permission:create"))],
)
@roles(RoleCode.ADMIN)
async def create_permission(
    session: _DB, dto: CreatePermissionRequest
) -> PermissionVOResponse:
    """创建权限（permission_code 查重 → 409）"""
    perm = await permission_service.create(session, dto)
    return _vo(perm)


@router.put(
    "/{permission_id}",
    response_model=PermissionVOResponse,
    dependencies=[Depends(require_permission("system:permission:edit"))],
)
@roles(RoleCode.ADMIN)
async def update_permission(
    session: _DB, permission_id: str, dto: UpdatePermissionRequest
) -> PermissionVOResponse:
    """更新权限（父不能是自己 → 400；编码查重 → 409）"""
    perm = await permission_service.update(session, permission_id, dto)
    return _vo(perm)


@router.delete(
    "/{permission_id}",
    dependencies=[Depends(require_permission("system:permission:delete"))],
)
@roles(RoleCode.ADMIN)
async def delete_permission(
    session: _DB, permission_id: str
) -> dict:
    """软删权限（有子权限 / 关联角色或用户 → 400）"""
    await permission_service.delete(session, permission_id)
    return {"message": "删除成功"}

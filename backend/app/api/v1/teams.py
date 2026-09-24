"""团队管理路由

双层守卫（) + 方法级 @RequirePermission('system:team')）：
  - 类级 @Roles(ADMIN) → 每个端点 @roles(RoleCode.ADMIN)
  - 方法级 @RequirePermission(code) → dependencies=[Depends(require_permission(code))]

唯一例外：（注释「当前用户所在
团队（含担任负责人的），登录即可」）→ Python 只靠 auth_router 的默认登录拦截，
不加 @roles(ADMIN)、也不挂 system:team。

⚠️ 2026-09-15 订正：()」，故 Python 只标
@public()。，/tree **是** @Roles(RoleCode.ADMIN) +
@RequirePermission('system:team')，且整个 controller 未 import Public 装饰器
（不存在被 @Public 放开的端点）。

路由顺序：/page、/mine、/tree 是字面量段，必须注册在 /{team_id} 动态段之前。

状态码：POST create / addMembers → 201；其余 200。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth_route import auth_router, roles
from app.api.deps import get_current_user
from app.api.permission_guard import require_permission
from app.common.roles import RoleCode
from app.db.session import get_db_session
from app.models.team import Team
from app.schemas.auth import AuthUser
from app.schemas.team import (
    CreateTeamRequest,
    QueryTeamRequest,
    TeamDetailResponse,
    TeamMemberResponse,
    TeamPageResponse,
    TeamResponse,
    TeamTreeResponse,
    UpdateTeamRequest,
)
from app.services.team import team_service

router = auth_router()

_DB = Annotated[AsyncSession, Depends(get_db_session)]

_CurrentUser = Annotated[AuthUser, Depends(get_current_user)]

_TEAM_PERM = [Depends(require_permission("system:team"))]


def _vo(t: Team) -> TeamResponse:
    """实体 → 响应 VO"""
    return TeamResponse(
        id=str(t.id),
        team_name=t.team_name,
        team_code=t.team_code,
        description=t.description,
        leader_id=str(t.leader_id) if t.leader_id is not None else None,
        parent_id=str(t.parent_id),
        sort=t.sort,
        status=t.status,
        created_at=t.created_at,
        updated_at=t.updated_at,
        deleted=t.deleted,
    )


@router.post(
    "",
    response_model=TeamResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=_TEAM_PERM,
)
@roles(RoleCode.ADMIN)
async def create_team(session: _DB, dto: CreateTeamRequest) -> TeamResponse:
    """创建团队（仅 ADMIN + system:team；parentId 缺省根，不校验 leader/parent 存在）"""
    team = await team_service.create(session, dto)
    return _vo(team)


@router.get(
    "/page",
    response_model=TeamPageResponse,
    dependencies=_TEAM_PERM,
)
@roles(RoleCode.ADMIN)
async def page_teams(
    session: _DB,
    keyword: Annotated[str | None, Query()] = None,
    status_filter: Annotated[int | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
) -> TeamPageResponse:
    """团队分页（keyword 模糊匹配团队名 / 编码；按 sort ASC、created_at DESC 排序）"""
    query = QueryTeamRequest(
        keyword=keyword, status=status_filter, page=page, page_size=page_size
    )
    result = await team_service.page(session, query)
    return TeamPageResponse(
        items=[_vo(t) for t in result["items"]],
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


@router.get("/mine", response_model=list[TeamResponse])
async def list_my_teams(
    session: _DB,
    current_user: _CurrentUser,
) -> list[TeamResponse]:
    """当前用户所在团队（含担任负责人的），登录即可)`：**不挂**
    @roles(ADMIN) 与 system:team，
    只靠 auth_router 的默认登录拦截。结果按 sort ASC / created_at ASC 排序。
    """
    teams = await team_service.list_mine(session, current_user.user_id)
    return [_vo(t) for t in teams]


@router.get(
    "/tree",
    response_model=list[TeamTreeResponse],
    dependencies=_TEAM_PERM,
)
@roles(RoleCode.ADMIN)
async def team_tree(
    session: _DB,
    root_only: Annotated[str | None, Query(alias="rootOnly")] = None,
) -> list[TeamTreeResponse]:
    """团队树（ADMIN + system:team) + @RequirePermission('system:team')）

    rootOnly=true 只返回根节点。
    """
    nodes = await team_service.get_tree(session, root_only == "true")
    # rootOnly 节点无 children 键 → 响应模型补默认空数组
    return [TeamTreeResponse(**node) for node in nodes]


@router.get(
    "/{team_id}",
    response_model=TeamDetailResponse,
    dependencies=_TEAM_PERM,
)
@roles(RoleCode.ADMIN)
async def get_team(session: _DB, team_id: str) -> TeamDetailResponse:
    """团队详情（含 memberCount；非法 / 不存在 / 已删 → 404）"""
    team, member_count = await team_service.get_detail(session, team_id)
    return TeamDetailResponse(**_vo(team).model_dump(), member_count=member_count)


@router.put(
    "/{team_id}",
    response_model=TeamResponse,
    dependencies=_TEAM_PERM,
)
@roles(RoleCode.ADMIN)
async def update_team(
    session: _DB, team_id: str, dto: UpdateTeamRequest
) -> TeamResponse:
    """更新团队（父团队不能是自己 → 400）"""
    team = await team_service.update(session, team_id, dto)
    return _vo(team)


@router.delete(
    "/{team_id}",
    dependencies=_TEAM_PERM,
)
@roles(RoleCode.ADMIN)
async def delete_team(session: _DB, team_id: str) -> dict:
    """删除团队（有未删子团队 → 400；软删团队并硬删成员关联）"""
    await team_service.delete(session, team_id)
    return {"message": "删除成功"}


@router.post(
    "/{team_id}/members",
    status_code=status.HTTP_201_CREATED,
    dependencies=_TEAM_PERM,
)
@roles(RoleCode.ADMIN)
async def add_team_members(
    session: _DB, team_id: str, user_ids: list[str]
) -> bool:
    """批量添加成员（请求体是字符串数组本身；任一用户不存在/已删 → 404）"""
    return await team_service.add_members(session, team_id, user_ids)


@router.delete(
    "/{team_id}/members",
    dependencies=_TEAM_PERM,
)
@roles(RoleCode.ADMIN)
async def remove_team_members(
    session: _DB, team_id: str, user_ids: list[str]
) -> bool:
    """批量移除成员（请求体是字符串数组本身；未关联的 ID 静默跳过）"""
    return await team_service.remove_members(session, team_id, user_ids)


@router.get(
    "/{team_id}/members",
    response_model=list[TeamMemberResponse],
    dependencies=_TEAM_PERM,
)
@roles(RoleCode.ADMIN)
async def list_team_members(
    session: _DB, team_id: str
) -> list[TeamMemberResponse]:
    """成员列表（join 未删用户：userId / memberRole / username / realName）"""
    rows = await team_service.list_members(session, team_id)
    return [TeamMemberResponse(**row) for row in rows]

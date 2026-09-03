"""团队管理服务（对应 TS src/team/team.service.ts）

与 TS 的仓库/工具对应：
  teamRepo   → Team        (app/models/team.py)
  memberRepo → TeamMember  (app/models/team_member.py)
  userRepo   → User        (app/models/user.py)
  nextSnowflakeId → app.common.snowflake.next_snowflake_id

关键保真点（逐条对齐 TS，便于后续反查）：
  - create 默认值：parentId 缺省 '0'（根）、sort 0、status 1、其余可空字段 None；
    **不校验 leaderId / parentId 是否存在**（TS create 同样不查，悬空引用如实保留）。
  - update：parentId 传自身 ID → 400「父团队不能是自己」；其余字段未传不改动。
  - delete：有未删子团队 → 400「存在子团队，无法删除」；软删 team 后**硬删**成员关联
    （TS memberRepo.delete 同款）。TS 是 save + delete 两步各自生效，Python 合并为
    单事务提交（语义一致，原子性更强，刻意差异）。
  - getTree：只取 status=1 且未删，按 sort ASC / created_at ASC 排序；
    rootOnly=true 返回根节点列表（TS 不带 children 键，Python 响应模型统一补
    children: []，语义一致）。
  - addMembers：先校验团队存在，再校验用户全部存在且未删（缺 → 404「部分用户不存在」）；
    已存在的关联跳过（幂等），新建 memberRole='member'。TS 逐个 findOne + save
    （N 次查询），Python 同款逐个查（保真，不合并批量）。
  - removeMembers：In(userIds) 批量删；空数组时 TS TypeORM In([]) 不删任何行，
    SQLAlchemy in_([]) 同样生成恒假条件，行为一致。
  - listMembers：inner join 未删用户，返回 userId / memberRole / username / realName。
"""

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.bigint import parse_bigint
from app.common.snowflake import next_snowflake_id
from app.models.team import Team
from app.models.team_member import MEMBER_ROLE_MEMBER, TeamMember
from app.models.user import User
from app.schemas.team import (
    CreateTeamRequest,
    QueryTeamRequest,
    UpdateTeamRequest,
)


class TeamService:
    """团队 CRUD + 成员管理（路由层 @roles(ADMIN) + @require_permission 组合保护）"""

    # ---------------- 基础查询 ----------------

    async def find_by_id_or_throw(self, session: AsyncSession, team_id: str) -> Team:
        """按 ID 查未删团队（对应 TS findByIdOrThrow）；非法/不存在/已删 → 404「团队不存在」"""
        pk = parse_bigint(team_id)
        if pk is None:
            raise HTTPException(status_code=404, detail="团队不存在")
        team = (
            await session.scalars(
                select(Team).where(Team.id == pk, Team.deleted == False)  # noqa: E712
            )
        ).first()
        if not team:
            raise HTTPException(status_code=404, detail="团队不存在")
        return team

    async def get_detail(self, session: AsyncSession, team_id: str) -> tuple[Team, int]:
        """团队详情 + 成员数（对应 TS getDetail 返回 { ...team, memberCount }）"""
        team = await self.find_by_id_or_throw(session, team_id)
        member_count = (
            await session.scalar(
                select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team.id)
            )
        ) or 0
        return team, int(member_count)

    async def page(self, session: AsyncSession, query: QueryTeamRequest) -> dict:
        """分页 + keyword 模糊（对应 TS page）；keyword 去空格后 ILIKE 团队名 / 编码两列"""
        page = query.page or 1
        page_size = query.page_size or 20
        conditions = [Team.deleted == False]  # noqa: E712
        if query.keyword and query.keyword.strip():
            kw = f"%{query.keyword.strip()}%"
            conditions.append((Team.team_name.ilike(kw)) | (Team.team_code.ilike(kw)))
        if query.status is not None:
            conditions.append(Team.status == query.status)
        total = (
            await session.scalar(
                select(func.count()).select_from(Team).where(*conditions)
            )
        ) or 0
        items = list(
            (
                await session.scalars(
                    select(Team)
                    .where(*conditions)
                    # TS：orderBy sort ASC, created_at DESC（与 permission 的 ASC 不同，保真）
                    .order_by(Team.sort.asc(), Team.created_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
        )
        return {"items": items, "total": int(total), "page": page, "page_size": page_size}

    async def get_tree(self, session: AsyncSession, root_only: bool = False) -> list[dict]:
        """团队树（对应 TS getTree）；只含启用团队，根 parent_id=0 递归展开 children

        rootOnly=True 时返回根节点本身（TS 不带 children 键）；此处节点字典不含
        children 键，由响应模型统一补默认空数组。
        """
        teams = list(
            (
                await session.scalars(
                    select(Team)
                    .where(Team.deleted == False, Team.status == 1)  # noqa: E712
                    .order_by(Team.sort.asc(), Team.created_at.asc())
                )
            ).all()
        )
        parent_groups: dict[str, list[Team]] = {}
        for t in teams:
            parent_groups.setdefault(str(t.parent_id), []).append(t)

        def build(parent_id: str) -> list[dict]:
            nodes: list[dict] = []
            for t in parent_groups.get(parent_id, []):
                node = self._to_dict(t)
                node["children"] = build(str(t.id))
                nodes.append(node)
            return nodes

        roots = parent_groups.get("0", [])
        if root_only:
            return [self._to_dict(t) for t in roots]
        return build("0")

    # ---------------- 写操作 ----------------

    async def create(self, session: AsyncSession, dto: CreateTeamRequest) -> Team:
        """创建团队（对应 TS create）；parentId 缺省根 '0'，不校验 leader/parent 存在"""
        team = Team(
            id=next_snowflake_id(),
            team_name=dto.team_name,
            team_code=dto.team_code,
            description=dto.description,
            leader_id=parse_bigint(dto.leader_id) if dto.leader_id is not None else None,
            parent_id=parse_bigint(dto.parent_id) if dto.parent_id is not None else 0,
            sort=dto.sort if dto.sort is not None else 0,
            status=dto.status if dto.status is not None else 1,
        )
        session.add(team)
        await session.commit()
        await session.refresh(team)
        return team

    async def update(
        self, session: AsyncSession, team_id: str, dto: UpdateTeamRequest
    ) -> Team:
        """更新团队（对应 TS update）；全字段可选，未传不改

        刻意差异：TS 对显式传 null 的可空字段会写入 NULL（IsOptional 放行 null），
        Python 按 None=未传不改动处理；清空字段请传空字符串 ""（与 TS 空串写入一致）。
        """
        team = await self.find_by_id_or_throw(session, team_id)
        if dto.team_name is not None:
            team.team_name = dto.team_name
        if dto.team_code is not None:
            team.team_code = dto.team_code
        if dto.description is not None:
            team.description = dto.description
        if dto.leader_id is not None:
            team.leader_id = parse_bigint(dto.leader_id)
        if dto.parent_id is not None:
            new_parent = parse_bigint(dto.parent_id)
            if new_parent is None:
                raise HTTPException(status_code=400, detail="父团队 ID 非法")
            if new_parent == team.id:
                raise HTTPException(status_code=400, detail="父团队不能是自己")
            team.parent_id = new_parent
        if dto.sort is not None:
            team.sort = dto.sort
        if dto.status is not None:
            team.status = dto.status
        await session.commit()
        await session.refresh(team)
        return team

    async def delete(self, session: AsyncSession, team_id: str) -> None:
        """删除团队（对应 TS delete）；有未删子团队 → 400，软删后硬删成员关联"""
        team = await self.find_by_id_or_throw(session, team_id)
        child_count = (
            await session.scalar(
                select(func.count())
                .select_from(Team)
                .where(Team.parent_id == team.id, Team.deleted == False)  # noqa: E712
            )
        ) or 0
        if child_count > 0:
            raise HTTPException(status_code=400, detail="存在子团队，无法删除")
        team.deleted = True
        # TS 分两步（save → delete）；Python 合并单事务提交，语义一致
        await session.execute(delete(TeamMember).where(TeamMember.team_id == team.id))
        await session.commit()

    # ---------------- 成员管理 ----------------

    async def add_members(
        self, session: AsyncSession, team_id: str, user_ids: list[str]
    ) -> bool:
        """批量添加成员（对应 TS addMembers）；用户缺一 → 404「部分用户不存在」"""
        team = await self.find_by_id_or_throw(session, team_id)
        int_ids = [parse_bigint(u) for u in user_ids]
        found: list[int] = []
        if all(i is not None for i in int_ids) and user_ids:
            found = list(
                (
                    await session.scalars(
                        select(User.id).where(
                            User.id.in_([i for i in int_ids if i is not None]),
                            User.deleted == False,  # noqa: E712
                        )
                    )
                ).all()
            )
        if len(found) != len(user_ids):
            raise HTTPException(status_code=404, detail="部分用户不存在")
        # TS 逐个 findOne + save（已存在跳过）；Python 同款逐个查，保真不合并
        for uid in int_ids:
            assert uid is not None
            exists = (
                await session.scalars(
                    select(TeamMember.id).where(
                        TeamMember.team_id == team.id, TeamMember.user_id == uid
                    )
                )
            ).first()
            if exists:
                continue
            session.add(
                TeamMember(
                    id=next_snowflake_id(),
                    team_id=team.id,
                    user_id=uid,
                    member_role=MEMBER_ROLE_MEMBER,
                )
            )
        await session.commit()
        return True

    async def remove_members(
        self, session: AsyncSession, team_id: str, user_ids: list[str]
    ) -> bool:
        """批量移除成员（对应 TS removeMembers）；空数组 / 未关联的 ID 均静默跳过"""
        team = await self.find_by_id_or_throw(session, team_id)
        int_ids = [parse_bigint(u) for u in user_ids]
        if any(i is None for i in int_ids):
            # 非法 ID 不会命中任何行，与 TS In() 查不到即无操作一致
            int_ids = [i for i in int_ids if i is not None]
        if int_ids:
            await session.execute(
                delete(TeamMember).where(
                    TeamMember.team_id == team.id, TeamMember.user_id.in_(int_ids)
                )
            )
        await session.commit()
        return True

    async def list_members(self, session: AsyncSession, team_id: str) -> list[dict]:
        """成员列表（对应 TS listMembers）；inner join 未删用户，返回 4 字段 raw 行"""
        team = await self.find_by_id_or_throw(session, team_id)
        rows = (
            await session.execute(
                select(TeamMember.user_id, TeamMember.member_role, User.username, User.real_name)
                .join(User, User.id == TeamMember.user_id)
                .where(TeamMember.team_id == team.id, User.deleted == False)  # noqa: E712
            )
        ).all()
        return [
            {
                "user_id": str(r.user_id),
                "member_role": r.member_role,
                "username": r.username,
                "real_name": r.real_name,
            }
            for r in rows
        ]

    # ---------------- 内部辅助 ----------------

    @staticmethod
    def _to_dict(team: Team) -> dict:
        """实体 → 字典（bigint 转字符串；camelCase 由响应模型别名处理）"""
        return {
            "id": str(team.id),
            "team_name": team.team_name,
            "team_code": team.team_code,
            "description": team.description,
            "leader_id": str(team.leader_id) if team.leader_id is not None else None,
            "parent_id": str(team.parent_id),
            "sort": team.sort,
            "status": team.status,
            "created_at": team.created_at,
            "updated_at": team.updated_at,
            "deleted": team.deleted,
        }


# 全局单例（无状态）
team_service = TeamService()

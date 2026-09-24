"""角色管理服务

- kh_role 无 deleted 列→ delete 是**硬删除**。
- getById 对任意非法/不存在的 id 统一 404「角色不存在」。
- 服务层无状态、不持会话，session 由路由层传入（同 UserService 惯例）。

：
  roleRepo → Role 模型（app/models/role.py）
  userRoleRepo → UserRole 模型（app/models/user_role.py）
  nextSnowflakeId → app.common.snowflake.next_snowflake_id
"""

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.bigint import parse_bigint
from app.common.snowflake import next_snowflake_id
from app.models.role import ROLE_STATUS_ENABLED, Role
from app.models.user_role import UserRole
from app.schemas.role import RoleCreateRequest, RoleUpdateRequest


class RoleService:
    """角色 CRUD（路由层全部 @roles(ADMIN)）"""

    async def list_all(self, session: AsyncSession) -> list[Role]:
        """全部**启用**角色，按 roleName 升序"""
        stmt = (
            select(Role)
            .where(Role.status == ROLE_STATUS_ENABLED)
            .order_by(Role.role_name.asc())
        )
        return list((await session.scalars(stmt)).all())

    async def get_by_id(self, session: AsyncSession, role_id: str) -> Role:
        """按 ID 查角色；不存在 / id 非法 → 404「角色不存在」"""
        pk = parse_bigint(role_id)
        if pk is None:
            raise HTTPException(status_code=404, detail="角色不存在")
        role = (
            await session.scalars(select(Role).where(Role.id == pk))
        ).first()
        if not role:
            raise HTTPException(status_code=404, detail="角色不存在")
        return role

    async def create(
        self, session: AsyncSession, dto: RoleCreateRequest
    ) -> Role:
        """创建角色；roleCode 查重 → 409「角色编码已存在」。

        依赖数据库唯一约束兜底并发。
        """
        exists = (
            await session.scalars(
                select(Role.id).where(Role.role_code == dto.role_code)
            )
        ).first()
        if exists:
            raise HTTPException(status_code=409, detail="角色编码已存在")

        role = Role(
            id=next_snowflake_id(),
            role_name=dto.role_name,
            role_code=dto.role_code,
            description=dto.description,
            status=ROLE_STATUS_ENABLED,
        )
        session.add(role)
        await session.commit()
        await session.refresh(role)
        return role

    async def update(
        self, session: AsyncSession, role_id: str, dto: RoleUpdateRequest
    ) -> Role:
        """部分更新角色；未传字段不改动"""
        role = await self.get_by_id(session, role_id)
        if dto.role_name is not None:
            role.role_name = dto.role_name
        if dto.description is not None:
            role.description = dto.description
        if dto.status is not None:
            role.status = dto.status
        await session.commit()
        await session.refresh(role)
        return role

    async def delete(self, session: AsyncSession, role_id: str) -> None:
        """删除角色

        仍有关联用户（kh_user_role 存在引用）→ 400「角色仍有关联用户，无法删除」。
，此处保持一致。
        """
        role = await self.get_by_id(session, role_id)
        pk = role.id
        bound = (
            await session.scalars(
                select(func.count()).select_from(UserRole).where(UserRole.role_id == pk)
            )
        ).one()
        if bound > 0:
            raise HTTPException(status_code=400, detail="角色仍有关联用户，无法删除")

        await session.delete(role)
        await session.commit()


# 全局单例（无状态）
role_service = RoleService()

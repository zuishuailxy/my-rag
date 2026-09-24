"""权限管理服务

：
  permRepo → Permission (app/models/permission.py)
  rolePermRepo → RolePermission (app/models/role_permission.py)
  userPermRepo → UserPermission (app/models/user_permission.py)
  userRoleRepo → UserRole (app/models/user_role.py)
  roleRepo → Role (app/models/role.py)
  nextSnowflakeId → app.common.snowflake.next_snowflake_id

关键保真点：
  - 软删：delete 只置 deleted=true。
    注意 RolePermission / UserPermission 的授权是**硬删**（repo.delete 同款）。
  - 父不能是自己：update 传 parentId==自身 id → 400「父权限不能是自己」。
  - 删除前置校验：有子权限 400 / 有关联角色或用户 400（均 BadRequestException）。
  - getUserPermissionCodes：合并「直接权限 + 角色权限」；用户拥有任一 ADMIN_ROLES
    角色时追加 ADMIN_OPERATION_PERMISSIONS。
"""

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.bigint import parse_bigint
from app.common.permissions import ADMIN_OPERATION_PERMISSIONS, ADMIN_ROLES
from app.common.snowflake import next_snowflake_id
from app.models.permission import Permission
from app.models.role import Role
from app.models.role_permission import RolePermission
from app.models.user_permission import UserPermission
from app.models.user_role import UserRole
from app.schemas.permission import (
    AssignPermissionIdsRequest,
    CreatePermissionRequest,
    QueryPermissionRequest,
    UpdatePermissionRequest,
)


class PermissionService:
    """权限 CRUD + 角色/用户授权（路由层 @roles(ADMIN) + @require_permission 组合保护）"""

    # ---------------- 基础查询 ----------------

    async def list_all(self, session: AsyncSession) -> list[Permission]:
        """启用且未删的权限，按 sort、created_at 升序"""
        stmt = (
            select(Permission)
            .where(Permission.deleted == False, Permission.status == 1)
            .order_by(Permission.sort.asc(), Permission.created_at.asc())
        )
        return list((await session.scalars(stmt)).all())

    async def get_by_id(self, session: AsyncSession, permission_id: str) -> Permission:
        """按 ID 查权限；非法/不存在/已删 → 404「权限不存在」"""
        pk = parse_bigint(permission_id)
        if pk is None:
            raise HTTPException(status_code=404, detail="权限不存在")
        perm = (
            await session.scalars(
                select(Permission).where(Permission.id == pk, Permission.deleted == False)
            )
        ).first()
        if not perm:
            raise HTTPException(status_code=404, detail="权限不存在")
        return perm

    async def get_children(self, session: AsyncSession, parent_id: str) -> list[Permission]:
        """查某父权限下的子权限"""
        pid = parse_bigint(parent_id)
        if pid is None:
            # 非法父 ID → 视为无子节点，
            return []
        stmt = (
            select(Permission)
            .where(
                Permission.parent_id == pid,
                Permission.deleted == False,
                Permission.status == 1,
            )
            .order_by(Permission.sort.asc())
        )
        return list((await session.scalars(stmt)).all())

    async def page(self, session: AsyncSession, query: QueryPermissionRequest) -> dict:
        """分页 + keyword 模糊；keyword 去空格后 ILIKE 两列"""
        page = query.page or 1
        page_size = query.page_size or 20
        conditions = [Permission.deleted == False]
        if query.keyword and query.keyword.strip():
            kw = f"%{query.keyword.strip()}%"
            conditions.append(
                (Permission.permission_name.ilike(kw))
                | (Permission.permission_code.ilike(kw))
            )
        total = (
            await session.scalar(
                select(func.count()).select_from(Permission).where(*conditions)
            )
        ) or 0
        items = list(
            (
                await session.scalars(
                    select(Permission)
                    .where(*conditions)
                    .order_by(Permission.sort.asc(), Permission.created_at.asc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
        )
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    async def get_tree(self, session: AsyncSession) -> list[dict]:
        """组装权限树；根节点 parentId='0' 递归展开 children"""
        perms = await self.list_all(session)
        parent_groups: dict[str, list[Permission]] = {}
        for p in perms:
            parent_groups.setdefault(str(p.parent_id), []).append(p)

        def build(parent_id: str) -> list[dict]:
            nodes: list[dict] = []
            for p in parent_groups.get(parent_id, []):
                node = self._to_dict(p)
                node["children"] = build(str(p.id))
                nodes.append(node)
            return nodes

        return build("0")

    # ---------------- 写操作 ----------------

    async def create(self, session: AsyncSession, dto: CreatePermissionRequest) -> Permission:
        """创建权限"""
        exists = (
            await session.scalars(
                select(Permission.id).where(
                    Permission.permission_code == dto.permission_code,
                    Permission.deleted == False,
                )
            )
        ).first()
        if exists:
            raise HTTPException(status_code=409, detail="权限编码已存在")
        perm = Permission(
            id=next_snowflake_id(),
            parent_id=parse_bigint(dto.parent_id) or 0,
            permission_name=dto.permission_name,
            permission_code=dto.permission_code,
            permission_type=dto.permission_type,
            menu_url=dto.menu_url,
            api_url=dto.api_url,
            method=dto.method,
            icon=dto.icon,
            sort=dto.sort if dto.sort is not None else 0,
            status=dto.status if dto.status is not None else 1,
        )
        session.add(perm)
        await session.commit()
        await session.refresh(perm)
        return perm

    async def update(
        self,
        session: AsyncSession,
        permission_id: str,
        dto: UpdatePermissionRequest,
    ) -> Permission:
        """更新权限；全字段可选，未传不改"""
        perm = await self.get_by_id(session, permission_id)
        if dto.permission_code is not None and dto.permission_code != perm.permission_code:
            exists = (
                await session.scalars(
                    select(Permission.id).where(
                        Permission.permission_code == dto.permission_code,
                        Permission.deleted == False,
                    )
                )
            ).first()
            if exists:
                raise HTTPException(status_code=409, detail="权限编码已存在")
            perm.permission_code = dto.permission_code
        if dto.permission_name is not None:
            perm.permission_name = dto.permission_name
        if dto.permission_type is not None:
            perm.permission_type = dto.permission_type
        if dto.parent_id is not None:
            new_parent = parse_bigint(dto.parent_id)
            if new_parent is None:
                raise HTTPException(status_code=400, detail="父权限 ID 非法")
            if new_parent == perm.id:
                raise HTTPException(status_code=400, detail="父权限不能是自己")
            perm.parent_id = new_parent
        if dto.menu_url is not None:
            perm.menu_url = dto.menu_url
        if dto.api_url is not None:
            perm.api_url = dto.api_url
        if dto.method is not None:
            perm.method = dto.method
        if dto.icon is not None:
            perm.icon = dto.icon
        if dto.sort is not None:
            perm.sort = dto.sort
        if dto.status is not None:
            perm.status = dto.status
        await session.commit()
        await session.refresh(perm)
        return perm

    async def delete(self, session: AsyncSession, permission_id: str) -> None:
        """软删权限；前置校验子权限 / 关联角色或用户"""
        perm = await self.get_by_id(session, permission_id)
        child_count = (
            await session.scalar(
                select(func.count())
                .select_from(Permission)
                .where(Permission.parent_id == perm.id, Permission.deleted == False)
            )
        ) or 0
        if child_count > 0:
            raise HTTPException(status_code=400, detail="存在子权限，无法删除")
        role_bound = (
            await session.scalar(
                select(func.count())
                .select_from(RolePermission)
                .where(RolePermission.permission_id == perm.id)
            )
        ) or 0
        user_bound = (
            await session.scalar(
                select(func.count())
                .select_from(UserPermission)
                .where(UserPermission.permission_id == perm.id)
            )
        ) or 0
        if role_bound > 0 or user_bound > 0:
            raise HTTPException(status_code=400, detail="权限仍有关联角色或用户，无法删除")
        perm.deleted = True
        await session.commit()

    # ---------------- 角色 / 用户授权（供 role / user 管理端调用） ----------------

    async def get_role_permission_ids(
        self, session: AsyncSession, role_id: str
    ) -> list[str]:
        """角色已授予的权限 ID 列表"""
        await self._ensure_role_exists(session, role_id)
        rows = (
            await session.scalars(
                select(RolePermission.permission_id).where(
                    RolePermission.role_id == parse_bigint(role_id)
                )
            )
        ).all()
        return [str(r) for r in rows]

    async def assign_role_permissions(
        self, session: AsyncSession, role_id: str, dto: AssignPermissionIdsRequest
    ) -> list[str]:
        """全量替换角色权限；先删后插"""
        await self._ensure_role_exists(session, role_id)
        await self._validate_permission_ids(session, dto.permission_ids)
        rid = parse_bigint(role_id)
        await session.execute(
            delete(RolePermission).where(RolePermission.role_id == rid)
        )
        for pid in dto.permission_ids:
            session.add(
                RolePermission(
                    id=next_snowflake_id(),
                    role_id=rid,
                    permission_id=parse_bigint(pid),
                )
            )
        await session.commit()
        return dto.permission_ids

    async def get_user_direct_permission_ids(
        self, session: AsyncSession, user_id: str
    ) -> list[str]:
        """用户直接授权（非经角色）的权限 ID 列表"""
        rows = (
            await session.scalars(
                select(UserPermission.permission_id).where(
                    UserPermission.user_id == parse_bigint(user_id)
                )
            )
        ).all()
        return [str(r) for r in rows]

    async def assign_user_permissions(
        self, session: AsyncSession, user_id: str, dto: AssignPermissionIdsRequest
    ) -> list[str]:
        """全量替换用户直接权限；先删后插"""
        await self._validate_permission_ids(session, dto.permission_ids)
        uid = parse_bigint(user_id)
        await session.execute(
            delete(UserPermission).where(UserPermission.user_id == uid)
        )
        for pid in dto.permission_ids:
            session.add(
                UserPermission(
                    id=next_snowflake_id(),
                    user_id=uid,
                    permission_id=parse_bigint(pid),
                )
            )
        await session.commit()
        return dto.permission_ids

    # ---------------- 合并权限码（守卫核心） ----------------

    async def get_user_permission_codes(
        self, session: AsyncSession, user_id: str
    ) -> list[str]:
        """合并用户权限码集合

        组成：直接权限 + 角色权限（都要求 perm.status=1、perm.deleted=false）；
        角色拥有任一 ADMIN_ROLES 时追加 ADMIN_OPERATION_PERMISSIONS。
        """
        uid = parse_bigint(user_id)
        if uid is None:
            return []

        direct_rows = (
            await session.execute(
                select(Permission.permission_code)
                .join(UserPermission, UserPermission.permission_id == Permission.id)
                .where(
                    UserPermission.user_id == uid,
                    Permission.status == 1,
                    Permission.deleted == False,
                )
            )
        ).scalars().all()

        via_role_rows = (
            await session.execute(
                select(Permission.permission_code)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .join(UserRole, UserRole.role_id == RolePermission.role_id)
                .where(
                    UserRole.user_id == uid,
                    Permission.status == 1,
                    Permission.deleted == False,
                )
            )
        ).scalars().all()

        codes: set[str] = set(direct_rows)
        codes.update(via_role_rows)

        # 角色编码（仅启用的角色）判定管理员身份
        role_code_rows = (
            await session.execute(
                select(Role.role_code)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(UserRole.user_id == uid, Role.status == 1)
            )
        ).scalars().all()
        if any(rc in ADMIN_ROLES for rc in role_code_rows):
            codes.update(ADMIN_OPERATION_PERMISSIONS)

        return list(codes)

    # ---------------- 内部辅助 ----------------

    async def _ensure_role_exists(self, session: AsyncSession, role_id: str) -> Role:
        """角色必须存在（任何状态），否则 404「角色不存在」"""
        pk = parse_bigint(role_id)
        if pk is None:
            raise HTTPException(status_code=404, detail="角色不存在")
        role = (await session.scalars(select(Role).where(Role.id == pk))).first()
        if not role:
            raise HTTPException(status_code=404, detail="角色不存在")
        return role

    async def _validate_permission_ids(
        self, session: AsyncSession, ids: list[str]
    ) -> None:
        """校验权限 ID 全部存在且启用

        空列表直接放行；含非法 ID 或指向已停用的权限 → 404「部分权限不存在或已禁用」。
        """
        if not ids:
            return
        int_ids = [parse_bigint(i) for i in ids]
        if any(x is None for x in int_ids):
            raise HTTPException(status_code=400, detail="权限 ID 非法")
        found = (
            await session.scalars(
                select(Permission.id).where(
                    Permission.id.in_(int_ids),
                    Permission.deleted == False,
                    Permission.status == 1,
                )
            )
        ).all()
        if len(found) != len(int_ids):
            raise HTTPException(status_code=404, detail="部分权限不存在或已禁用")

    @staticmethod
    def _to_dict(perm: Permission) -> dict:
        """实体 → 字典（camelCase 由响应模型别名处理；此处用 snake 原值）"""
        return {
            "id": str(perm.id),
            "parent_id": str(perm.parent_id),
            "permission_name": perm.permission_name,
            "permission_code": perm.permission_code,
            "permission_type": perm.permission_type,
            "menu_url": perm.menu_url,
            "api_url": perm.api_url,
            "method": perm.method,
            "icon": perm.icon,
            "sort": perm.sort,
            "status": perm.status,
        }


# 全局单例（无状态）
permission_service = PermissionService()

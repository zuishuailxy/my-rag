"""
用户服务（对应 TS src/user/user.service.ts）

方法与 TS 一一对应，形参顺序保持一致；服务层无状态、不持会话，
`session: AsyncSession` 由路由层通过 `Depends(get_db_session)` 传入。

TS 侧 id 经 bigintTransformer 是 string，Python 服务层同样收 `str`，
内部用 parse_bigint / require_bigint 转 int（见 app/common/bigint.py 的语义说明）。

刻意偏离 TS 两处（均为防 500，TS 侧会静默截断或抛 5xx）：
  1. register 前置校验密码 UTF-8 字节数 ≤ 72：bcryptjs 对超长密码**静默截断**到 72 字节
     （用户以为设了长密码，实际只有前 72 字节生效），Python bcrypt 5.0 直接抛 ValueError；
     此处显式 400，语义更安全。DTO 层的 max_length=72 是字符数粗筛，字节校验在这里兜底。
  2. validate_credentials 的 compare 包 try/except：超 72 字节的密码在 bcrypt 5.0 下抛
     ValueError，此处按「用户名或密码错误」返回 401（超长密码不可能匹配任何账号），
     而不是让 500 冒出来。

并发说明：register 的「查重 → 插入」与 TS 一样存在竞态（两请求同时注册同名会都通过
exists 检查），最终由 partial unique index uk_kh_user_username 兜底——TS 侧撞索引是 500，
Python 侧同样是 500，此缺陷两侧一致，未做收敛。
"""

import bcrypt
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.common.bigint import parse_bigint, require_bigint
from app.common.roles import RoleCode
from app.common.snowflake import next_snowflake_id
from app.models.role import Role, ROLE_STATUS_ENABLED
from app.models.user import USER_STATUS_DISABLED, USER_STATUS_ENABLED, User
from app.models.user_role import UserRole
from app.models.document import DocumentEntity
from app.schemas.auth import AuthUser, BCRYPT_MAX_PASSWORD_BYTES, RegisterRequest
from app.schemas.user import (
    CreateUserRequest,
    ProfileUpdateRequest,
    UpdateUserRequest,
    UserPageQuery,
)

from app.services.permission import permission_service

# bcrypt 工作因子（TS bcryptjs.hash(password, 10) 的 10）
BCRYPT_ROUNDS = 10


def _utcnow() -> datetime:
    """UTC 当前时间（naive，适配 TIMESTAMP WITHOUT TIME ZONE 列，同 document 服务）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash_password(password: str) -> str:
    """明文 → bcrypt 哈希（$2b$ 前缀；bcryptjs 生成 $2a$，两者互相可验证）"""
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    ).decode("ascii")


class UserService:
    """用户服务"""

    # ---------------------------------------------------------------- query

    async def find_by_username(self, session: AsyncSession, username: str) -> User | None:
        """按用户名查未删除用户（对应 TS findByUsername）"""
        stmt = select(User).where(User.username == username, User.deleted.is_(False))
        return (await session.scalars(stmt)).first()

    async def find_by_email(self, session: AsyncSession, email: str) -> User | None:
        """按邮箱查未删除用户（对应 TS findByEmail）；不做小写归一（TS 同款，
        Redis 侧验证码 key 才 lower()，两处口径不同是 TS 原设计）"""
        stmt = select(User).where(User.email == email, User.deleted.is_(False))
        return (await session.scalars(stmt)).first()

    async def find_by_id_or_throw(self, session: AsyncSession, user_id: str) -> User:
        """按 ID 查未删除用户，不存在抛 404「用户不存在」（对应 TS findByIdOrThrow）"""
        pk = parse_bigint(user_id)
        if pk is not None:
            stmt = select(User).where(User.id == pk, User.deleted.is_(False))
            user = (await session.scalars(stmt)).first()
            if user is not None:
                return user
        raise HTTPException(status_code=404, detail="用户不存在")

    async def get_role_codes(self, session: AsyncSession, user_id: str) -> list[str]:
        """用户的全部**启用**角色编码（对应 TS getRoleCodes）

        只 JOIN kh_role 且过滤 r.status = 1，与 TS 一致；用户没有角色时返回空列表。
        """
        pk = parse_bigint(user_id)
        if pk is None:
            # 理论上只会被内部调用（JWT 里的 userId 已验签），非法值按「无角色」处理，
            # 不复制 TS 侧 22P02 → 500 的缺陷路径
            return []
        stmt = (
            select(Role.role_code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == pk, Role.status == ROLE_STATUS_ENABLED)
        )
        return list((await session.scalars(stmt)).all())

    def to_auth_user(
        self, user: User, roles: list[str], permissions: list[str]
    ) -> AuthUser:
        """实体 + 角色编码 + 权限码 → AuthUser（对应 TS toAuthUser；不查库、不异步）

        permissions 由调用方查好传入（TS 同构：buildAuthUser / validateCredentials
        先调 permissionService.getUserPermissionCodes 再传入）。
        """
        return AuthUser(
            user_id=str(user.id),
            username=user.username,
            real_name=user.real_name,
            email=user.email,
            avatar=user.avatar,
            roles=roles,
            permissions=permissions,
        )

    async def build_auth_user(self, session: AsyncSession, user_id: str) -> AuthUser:
        """按 ID 组装当前登录用户（对应 TS buildAuthUser；JWT 校验后的依赖用）

        与 TS 一致：status 校验后依次查角色码 + 合并权限码（PermissionService
        getUserPermissionCodes），登录 / refresh / me / 每请求鉴权注入都走这里。
        """
        user = await self.find_by_id_or_throw(session, user_id)
        if user.status != USER_STATUS_ENABLED:
            raise HTTPException(status_code=401, detail="账户已禁用")
        roles = await self.get_role_codes(session, user_id)
        permissions = await permission_service.get_user_permission_codes(
            session, user_id
        )
        return self.to_auth_user(user, roles, permissions)

    # ---------------------------------------------------------------- auth

    async def validate_credentials(
        self, session: AsyncSession, username: str, password: str
    ) -> AuthUser:
        """用户名 + 密码校验（对应 TS validateCredentials）

        注意与 TS 相同的防枚举顺序：用户不存在与密码错误返回**同一句**提示，
        不泄露「该用户名已注册」；账户禁用则明确提示。
        """
        user = await self.find_by_username(session, username)
        if not user:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        if user.status != USER_STATUS_ENABLED:
            raise HTTPException(status_code=401, detail="账户已禁用")
        if user.email_verified == 0:
            raise HTTPException(status_code=401, detail="账户未激活，请先验证邮箱")
        try:
            ok = bcrypt.checkpw(
                password.encode("utf-8"), user.password.encode("utf-8")
            )
        except ValueError:
            # bcrypt 5.0：密码 > 72 字节抛 ValueError（超长密码不可能匹配任何账号），
            # 按密码错误处理而不是 500
            ok = False
        if not ok:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        roles = await self.get_role_codes(session, str(user.id))
        permissions = await permission_service.get_user_permission_codes(
            session, str(user.id)
        )
        return self.to_auth_user(user, roles, permissions)

    # ---------------------------------------------------------------- write

    async def register(
        self,
        session: AsyncSession,
        input_: RegisterRequest,
        require_email_verification: bool = False,
    ) -> dict[str, object]:
        """注册（对应 TS register）：查重 → 建用户 → 授 USER 角色

        返回 { userId, emailVerificationRequired }（TS 返回体两键恒在，
        emailVerificationRequired 是 boolean 而非可选键）。

        邮箱验证分叉：needVerify = require_email_verification 且传了 email 时，
        建用户 status=0 + email_verified=0（激活后 activate_email 双双置 1）；
        注意未激活用户登录会在 validateCredentials 先命中 status 检查报
        「账户已禁用」——email_verified 检查在其后，按现注册流程不可达
        （TS 同款顺序，刻意保留以保真）。

        事务边界与 TS 相同：建用户与授角色是**两次独立提交**（TS 是两次独立 save），
        极端情况下可能出现「用户已建但授角色失败」，与 TS 行为一致，不额外包事务。
        """
        if await self.find_by_username(session, input_.username):
            raise HTTPException(status_code=409, detail="用户名已存在")
        # 邮箱查重在用户名之后（TS 同款顺序）；只查未删除用户，软删用户的邮箱可复用
        if input_.email:
            if await self.find_by_email(session, str(input_.email)):
                raise HTTPException(status_code=409, detail="邮箱已被使用")
        # 刻意优于 TS：TS bcryptjs 静默截断超长密码，这里显式 400（见模块 docstring）
        if len(input_.password.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
            raise HTTPException(status_code=400, detail="密码过长（最多 72 字节）")

        user_id = next_snowflake_id()
        # TS needVerify = input.requireEmailVerification && !!input.email：
        # 开了验证但没填邮箱 → 仍走免验证路径（立即可登录）
        need_verify = require_email_verification and input_.email is not None
        session.add(
            User(
                id=user_id,
                username=input_.username,
                password=_hash_password(input_.password),
                email=str(input_.email) if input_.email else None,
                real_name=input_.real_name,
                status=USER_STATUS_DISABLED if need_verify else USER_STATUS_ENABLED,
                email_verified=0 if need_verify else 1,
            )
        )
        await session.commit()
        await self.assign_role(session, str(user_id), RoleCode.USER)
        return {
            "userId": str(user_id),
            "emailVerificationRequired": need_verify,
        }

    async def assign_role(
        self, session: AsyncSession, user_id: str, role_code: str
    ) -> None:
        """授予角色（对应 TS assignRole）；角色不存在抛 404，已持有则幂等跳过"""
        user_pk = require_bigint(user_id, "userId")
        if user_pk is None:  # 仅当 user_id 为 None / 空串（非法值已被 require_bigint 抛 400）
            raise HTTPException(status_code=400, detail="Invalid userId: 空")
        role = (
            await session.scalars(select(Role).where(Role.role_code == role_code))
        ).first()
        if not role:
            raise HTTPException(status_code=404, detail=f"角色 {role_code} 不存在")

        exists = (
            await session.scalars(
                select(UserRole.id).where(
                    UserRole.user_id == user_pk, UserRole.role_id == role.id
                )
            )
        ).first()
        if exists:
            return

        session.add(
            UserRole(
                id=next_snowflake_id(),
                user_id=user_pk,
                role_id=role.id,
            )
        )
        await session.commit()

    async def touch_last_login(self, session: AsyncSession, user_id: str) -> None:
        """更新最后登录时间（对应 TS touchLastLogin；updated_at 由 onupdate 同步刷新）"""
        pk = require_bigint(user_id, "userId")
        await session.execute(
            update(User).where(User.id == pk).values(last_login_at=_utcnow())
        )
        await session.commit()

    async def get_user_ids_by_role_code(
        self, session: AsyncSession, role_code: str
    ) -> list[str]:
        """某角色下全部**启用且未删除**的用户 ID（对应 TS getUserIdsByRoleCode）"""
        stmt = (
            select(User.id)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, UserRole.role_id == Role.id)
            .where(
                Role.role_code == role_code,
                User.deleted.is_(False),
                User.status == USER_STATUS_ENABLED,
            )
        )
        return [str(pk) for pk in (await session.scalars(stmt)).all()]

    async def activate_email(self, session: AsyncSession, user_id: str) -> str:
        """邮箱激活（对应 TS activateEmail）；返回提示语给 verifyEmail 接口透出

        已激活幂等返回（不重复写库）；未激活则 email_verified 与 status **双双置 1**
        （注册分叉时两者一起置 0，激活对称恢复），单次提交。
        """
        user = await self.find_by_id_or_throw(session, user_id)
        if user.email_verified == 1:
            return "账户已激活，请直接登录"
        user.email_verified = 1
        user.status = USER_STATUS_ENABLED
        await session.commit()
        return "账户激活成功，请登录"

    async def reset_password_by_email(
        self, session: AsyncSession, email: str, new_password: str
    ) -> None:
        """按邮箱重置密码（对应 TS resetPasswordByEmail）；验证码校验在 auth service"""
        user = await self.find_by_email(session, email)
        if not user:
            raise HTTPException(status_code=404, detail="该邮箱未注册")
        # 刻意优于 TS：与 register 同款字节校验，防 bcrypt 5.0 对超长密码抛 ValueError 变 500
        if len(new_password.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
            raise HTTPException(status_code=400, detail="密码过长（最多 72 字节）")
        user.password = _hash_password(new_password)
        await session.commit()

    # ------------------------------------------------------- 管理端（UserController）

    def _iso(self, value: datetime | None) -> str | None:
        """时间 → isoformat（document_to_dict 同款口径：naive UTC，无时区后缀）"""
        return value.isoformat() if value is not None else None

    async def to_user_vo(self, session: AsyncSession, user: User) -> dict[str, object]:
        """实体 + 角色码 → UserVO dict（对应 TS toUserVO；键名 camelCase）"""
        role_codes = await self.get_role_codes(session, str(user.id))
        return {
            "id": str(user.id),
            "username": user.username,
            "email": user.email,
            "realName": user.real_name,
            "avatar": user.avatar,
            "status": user.status,
            "lastLoginAt": self._iso(user.last_login_at),
            "createdAt": self._iso(user.created_at),
            "updatedAt": self._iso(user.updated_at),
            "roleCodes": role_codes,
        }

    async def get_user_vo(self, session: AsyncSession, user_id: str) -> dict[str, object]:
        """用户详情 VO（对应 TS getUserVO；不存在 → 404「用户不存在」）"""
        user = await self.find_by_id_or_throw(session, user_id)
        return await self.to_user_vo(session, user)

    async def create_user(self, session: AsyncSession, dto: CreateUserRequest) -> str:
        """管理员创建用户（对应 TS createUser）；返回新用户 ID，VO 由控制器二次查询

        与 TS 一致的行为要点：
        - **只查重 username**，不查 email（createUser 与 register 不同，邮箱重复
          两侧都是脏数据，与 update 同款口径）；
        - emailVerified 恒 1（管理员创建视为已验证，不走邮箱激活分叉）；
        - status 取 dto.status ?? 1（?? 只对 None 生效，传 0 照样落 0）；
        - roleCodes 未传或传空数组都回退 [ROLE_USER]（TS `?.length ? :` 语义）；
        - 授角色**复用 replaceRoles**（TS 同款调用），角色不存在 → 404
          「角色不存在: ...」；replaceRoles 先删后插，对新用户是无害空删；
        - **非事务边界**：用户先落库提交，replaceRoles 失败时用户仍留存
          （无角色），与 TS 两次独立 save 的行为一致，不额外包事务。

        刻意优于 TS：密码 > 72 字节显式 400（TS bcryptjs 静默截断，见模块 docstring）。
        """
        if await self.find_by_username(session, dto.username):
            raise HTTPException(status_code=409, detail="用户名已存在")
        if len(dto.password.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
            raise HTTPException(status_code=400, detail="密码过长（最多 72 字节）")

        user_id = next_snowflake_id()
        session.add(
            User(
                id=user_id,
                username=dto.username,
                password=_hash_password(dto.password),
                email=str(dto.email) if dto.email else None,
                real_name=dto.real_name,
                avatar=dto.avatar,
                status=dto.status if dto.status is not None else USER_STATUS_ENABLED,
                email_verified=1,
            )
        )
        await session.commit()
        # TS: dto.roleCodes?.length ? dto.roleCodes : [RoleCode.USER] —— 空数组同回退
        role_codes: list[str] = (
            dto.role_codes if dto.role_codes else [RoleCode.USER.value]
        )
        await self.replace_roles(session, str(user_id), role_codes)
        return str(user_id)

    async def update_user(
        self, session: AsyncSession, user_id: str, dto: UpdateUserRequest
    ) -> dict[str, object]:
        """管理员更新用户（对应 TS updateUser；部分更新，未传字段不改动）

        与 TS 一致：email 不做唯一性查重；status 无取值约束。
        """
        user = await self.find_by_id_or_throw(session, user_id)
        if dto.email is not None:
            user.email = str(dto.email)
        if dto.real_name is not None:
            user.real_name = dto.real_name
        if dto.avatar is not None:
            user.avatar = dto.avatar
        if dto.status is not None:
            user.status = dto.status
        await session.commit()
        # updated_at 是服务端 onupdate=func.now()，flush 后属性过期等回读；
        # 不 refresh 直接访问会触发同步 IO（asyncpg 下 MissingGreenlet），显式异步回读
        await session.refresh(user)
        return await self.to_user_vo(session, user)

    async def delete_user(self, session: AsyncSession, user_id: str) -> None:
        """软删除用户（对应 TS deleteUser：deleted=true，不做物理删除）

        注意：kh_user_role 的关联行**不清**（TS 同款，两个 FK 也无 CASCADE）；
        邮箱/用户名随 partial unique 索引（WHERE deleted=false）自动释放。
        """
        user = await self.find_by_id_or_throw(session, user_id)
        user.deleted = True
        await session.commit()

    async def page_users(
        self, session: AsyncSession, query: UserPageQuery
    ) -> dict[str, object]:
        """用户分页列表（对应 TS pageUsers）

        筛选：keyword 模糊匹配 username/real_name/email（trim 后为空则不筛）；
        status / roleCode 等值过滤。排序 created_at DESC。
        total 与 TS getManyAndCount 同口径：带相同筛选条件计数（roleCode 走 JOIN，
        一个角色编码只对应一行 kh_role，JOIN 不会放大计数）。
        """
        conditions: list[ColumnElement[bool]] = [User.deleted.is_(False)]
        keyword = (query.keyword or "").strip()
        if keyword:
            kw = f"%{keyword}%"
            conditions.append(
                User.username.ilike(kw)
                | User.real_name.ilike(kw)
                | User.email.ilike(kw)
            )
        if query.status is not None:
            conditions.append(User.status == query.status)

        role_joined = query.role_code is not None and query.role_code != ""
        if role_joined:
            conditions.append(
                User.id.in_(
                    select(UserRole.user_id)
                    .join(Role, Role.id == UserRole.role_id)
                    .where(Role.role_code == query.role_code)
                )
            )

        where = and_(*conditions)
        total = (
            await session.scalars(
                select(func.count()).select_from(User).where(where)
            )
        ).one()
        rows = (
            await session.scalars(
                select(User)
                .where(where)
                .order_by(User.created_at.desc())
                .offset((query.page - 1) * query.page_size)
                .limit(query.page_size)
            )
        ).all()
        items = [await self.to_user_vo(session, u) for u in rows]
        return {
            "items": items,
            "total": total,
            "page": query.page,
            "pageSize": query.page_size,
        }

    async def get_user_role_codes(
        self, session: AsyncSession, user_id: str
    ) -> list[str]:
        """用户角色码（对应 TS getUserRoleCodes；先 404 校验再查角色）"""
        await self.find_by_id_or_throw(session, user_id)
        return await self.get_role_codes(session, user_id)

    async def replace_roles(
        self, session: AsyncSession, user_id: str, role_codes: list[str]
    ) -> list[str]:
        """全量替换用户角色（对应 TS replaceRoles）

        与 TS 相同的校验口径：按 roleCode 查**启用**角色，数量不等即 404
        「角色不存在: ...」——注意保真 quirk：roleCodes 含重复项时（如 ['A','A']）
        In() 只命中 1 行，长度校验同样不等，但 missing 过滤后为空 → 报
        「角色不存在: 」（冒号后空），两侧一致不收敛。
        """
        await self.find_by_id_or_throw(session, user_id)

        roles = (
            await session.scalars(
                select(Role).where(
                    Role.role_code.in_(role_codes), Role.status == ROLE_STATUS_ENABLED
                )
            )
        ).all()
        if len(roles) != len(role_codes):
            found = {r.role_code for r in roles}
            missing = [c for c in role_codes if c not in found]
            raise HTTPException(
                status_code=404, detail=f"角色不存在: {', '.join(missing)}"
            )

        await session.execute(
            delete(UserRole).where(UserRole.user_id == parse_bigint(user_id))
        )
        for role in roles:
            session.add(
                UserRole(id=next_snowflake_id(), user_id=int(user_id), role_id=role.id)
            )
        await session.commit()
        return role_codes

    async def update_profile(
        self, session: AsyncSession, user_id: str, dto: ProfileUpdateRequest
    ) -> dict[str, object]:
        """当前用户更新自己资料（对应 TS updateProfile；不含 status）"""
        user = await self.find_by_id_or_throw(session, user_id)
        if dto.email is not None:
            user.email = str(dto.email)
        if dto.real_name is not None:
            user.real_name = dto.real_name
        if dto.avatar is not None:
            user.avatar = dto.avatar
        await session.commit()
        # 同 update_user：服务端 onupdate 的时间列需异步回读后再组装 VO
        await session.refresh(user)
        return await self.to_user_vo(session, user)

    async def change_password(
        self, session: AsyncSession, user_id: str, old_password: str, new_password: str
    ) -> None:
        """修改密码（对应 TS changePassword；原密码错误 → 400「原密码错误」）"""
        user = await self.find_by_id_or_throw(session, user_id)
        try:
            ok = bcrypt.checkpw(
                old_password.encode("utf-8"), user.password.encode("utf-8")
            )
        except ValueError:
            ok = False  # 原密码超 72 字节不可能匹配（同 validate_credentials 口径）
        if not ok:
            raise HTTPException(status_code=400, detail="原密码错误")
        # 刻意优于 TS：bcrypt 5.0 对超长密码抛 ValueError 变 500，这里显式 400
        if len(new_password.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
            raise HTTPException(status_code=400, detail="密码过长（最多 72 字节）")
        user.password = _hash_password(new_password)
        await session.commit()

    async def reset_password(
        self, session: AsyncSession, user_id: str, new_password: str
    ) -> None:
        """管理员重置密码（对应 TS resetPassword；不校验原密码）"""
        user = await self.find_by_id_or_throw(session, user_id)
        # 刻意优于 TS：同款字节校验
        if len(new_password.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
            raise HTTPException(status_code=400, detail="密码过长（最多 72 字节）")
        user.password = _hash_password(new_password)
        await session.commit()

    async def get_user_statistics(
        self, session: AsyncSession, user_id: str
    ) -> dict[str, int]:
        """用户统计（对应 TS getUserStatistics：文档数 + 浏览/点赞/评论量合计，均筛 deleted=false）

        SUM 走 COALESCE(..., 0)，无文档时全 0（TS 同款）。
        """
        pk = parse_bigint(user_id)
        if pk is None:  # find_by_id_or_throw 先行，正常不可达
            raise HTTPException(status_code=404, detail="用户不存在")
        await self.find_by_id_or_throw(session, user_id)
        document_count = (
            await session.scalars(
                select(func.count())
                .select_from(DocumentEntity)
                .where(
                    DocumentEntity.author_id == pk, DocumentEntity.deleted.is_(False)
                )
            )
        ).one()
        raw = (
            await session.execute(
                select(
                    func.coalesce(func.sum(DocumentEntity.view_count), 0).label("view_count"),
                    func.coalesce(func.sum(DocumentEntity.like_count), 0).label("like_count"),
                    func.coalesce(func.sum(DocumentEntity.comment_count), 0).label("comment_count"),
                ).where(
                    DocumentEntity.author_id == pk, DocumentEntity.deleted.is_(False)
                )
            )
        ).one()
        return {
            "documentCount": int(document_count),
            "viewCount": int(raw.view_count),
            "likeCount": int(raw.like_count),
            "commentCount": int(raw.comment_count),
        }


# 全局单例（无状态）
user_service = UserService()

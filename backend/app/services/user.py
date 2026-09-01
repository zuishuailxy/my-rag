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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.bigint import parse_bigint, require_bigint
from app.common.roles import RoleCode
from app.common.snowflake import next_snowflake_id
from app.models.role import Role, ROLE_STATUS_ENABLED
from app.models.user import User, USER_STATUS_ENABLED
from app.models.user_role import UserRole
from app.schemas.auth import AuthUser, BCRYPT_MAX_PASSWORD_BYTES, RegisterRequest

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

    def to_auth_user(self, user: User, roles: list[str]) -> AuthUser:
        """实体 + 角色编码 → AuthUser（对应 TS toAuthUser；不查库、不异步）"""
        return AuthUser(
            user_id=str(user.id),
            username=user.username,
            real_name=user.real_name,
            email=user.email,
            avatar=user.avatar,
            roles=roles,
        )

    async def build_auth_user(self, session: AsyncSession, user_id: str) -> AuthUser:
        """按 ID 组装当前登录用户（对应 TS buildAuthUser；JWT 校验后的依赖用）"""
        user = await self.find_by_id_or_throw(session, user_id)
        if user.status != USER_STATUS_ENABLED:
            raise HTTPException(status_code=401, detail="账户已禁用")
        roles = await self.get_role_codes(session, user_id)
        return self.to_auth_user(user, roles)

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
        return self.to_auth_user(user, roles)

    # ---------------------------------------------------------------- write

    async def register(
        self, session: AsyncSession, input_: RegisterRequest
    ) -> dict[str, str]:
        """注册（对应 TS register）：查重 → 建用户 → 授 USER 角色，返回 { userId }

        简化注册：status=1 立即可登录，注册即授予 USER 角色（与 TS 一致）。
        事务边界与 TS 相同：建用户与授角色是**两次独立提交**（TS 是两次独立 save），
        极端情况下可能出现「用户已建但授角色失败」，与 TS 行为一致，不额外包事务。
        """
        if await self.find_by_username(session, input_.username):
            raise HTTPException(status_code=409, detail="用户名已存在")
        # 刻意优于 TS：TS bcryptjs 静默截断超长密码，这里显式 400（见模块 docstring）
        if len(input_.password.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
            raise HTTPException(status_code=400, detail="密码过长（最多 72 字节）")

        user_id = next_snowflake_id()
        session.add(
            User(
                id=user_id,
                username=input_.username,
                password=_hash_password(input_.password),
                email=str(input_.email) if input_.email else None,
                real_name=input_.real_name,
                status=USER_STATUS_ENABLED,
            )
        )
        await session.commit()
        await self.assign_role(session, str(user_id), RoleCode.USER)
        return {"userId": str(user_id)}

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


# 全局单例（无状态）
user_service = UserService()

"""RBAC ORM 模型 冒烟验证

验证 models/user.py / role.py / user_role.py 与真实 PG（init.sql 建出的库）的映射完全一致，
以及三个数据库层约束在 ORM 通道下同样生效：

  1. 种子数据经 ORM 可读，字段映射正确（含 nullable / 默认值 / 长度）
  2. uk_kh_user_username 是 partial unique —— 软删后同名可再注册
  3. uk_kh_user_role_user_role —— 同一用户重复授同一角色被拒
  4. kh_user_role 外键无级联 —— 删仍有角色的用户被拒；先解绑再删则成功

刻意不改生产数据：所有破坏性用例都在事务内执行并回滚，只有只读断言走真实提交。

不调 LLM、不发 MQ 消息，不消耗 token。

前置条件：docker 的 postgres 运行中，且已执行过 init-scripts/postgresql/init.sql。

用法：
    cd backend && PYTHONPATH=. .venv/bin/python scripts/smoke_rbac_models.py
"""
import asyncio
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.session import async_session_factory
from app.models import Role, User, UserRole

logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("SmokeRbac")

# 种子数据（init.sql 固定雪花 ID，本脚本依赖其存在）
SEED_ADMIN_ID = 1000000000000000001
SEED_REVIEWER_ID = 1000000000000000002
SEED_USER_ID = 1000000000000000003
SEED_ROLE_ADMIN_ID = 2000000000000000001
SEED_ROLE_REVIEWER_ID = 2000000000000000002
SEED_ROLE_USER_ID = 2000000000000000003

step = 0


def ok(msg: str) -> None:
    print(f" ✓ {msg}")


async def main() -> None:
    global step

    async with async_session_factory() as session:
        # ---------- 1) 种子数据经 ORM 可读 ----------
        step += 1
        print(f"{step}) 种子用户经 ORM 可读且字段映射正确")
        admin = (
            await session.execute(select(User).where(User.id == SEED_ADMIN_ID))
        ).scalar_one_or_none()
        assert admin is not None, "admin 种子用户不存在，请先执行 init.sql"
        assert admin.username == "admin"
        assert admin.real_name == "系统管理员"
        assert admin.email == "admin@company.com"
        assert admin.status == 1
        assert admin.deleted is False
        # 不能断言 last_login_at is None：任何一次登录（含 smoke_auth_guard 的登录冒烟）
        # 都会经 touch_last_login 写入它。这里只校验它是 None 或 datetime 的映射正确性。
        assert admin.last_login_at is None or isinstance(admin.last_login_at, datetime)
        assert admin.avatar is None
        # password 必须是 bcrypt 哈希而不是明文
        assert admin.password.startswith("$2a$"), f"密码不是 bcrypt 哈希：{admin.password!r}"
        assert admin.created_at is not None and admin.updated_at is not None
        ok(f"admin: id={admin.id} username={admin.username} status={admin.status}")

        # ---------- 2) 种子角色可读 ----------
        step += 1
        print(f"{step}) 种子角色经 ORM 可读")
        # 只断言 3 个种子角色按固定 ID 存在、编码正确、status=1，**不断言总数与顺序** ——
        # 库里可能有手工建的测试角色（如 2026-09-03 的 ROLE_TEST「测试」），
        # 总计数和「按 id 排序后正好是三个」都是脆弱前提（与第 3 步同一口径）。
        roles = (await session.execute(select(Role).order_by(Role.id))).scalars().all()
        by_id = {r.id: r for r in roles}
        seed_roles = {
            SEED_ROLE_ADMIN_ID: "ROLE_ADMIN",
            SEED_ROLE_REVIEWER_ID: "ROLE_REVIEWER",
            SEED_ROLE_USER_ID: "ROLE_USER",
        }
        missing = [rid for rid in seed_roles if rid not in by_id]
        assert not missing, f"种子角色缺失：{missing}，库里角色共 {len(roles)} 个"
        for rid, code in seed_roles.items():
            assert by_id[rid].role_code == code, (
                f"{rid} 期望 {code}，实际 {by_id[rid].role_code}"
            )
            assert by_id[rid].status == 1, f"{code} status 应为 1"
        ok(f"种子角色：{[by_id[rid].role_code for rid in seed_roles]}")

        # ---------- 3) 用户-角色关联可读 ----------
        step += 1
        print(f"{step}) 用户-角色关联经 ORM 可读")
        links = (
            await session.execute(select(UserRole).order_by(UserRole.id))
        ).scalars().all()
        # 只断言 4 条种子关联存在（按固定 ID 逐条找），不断言总数 ——
        # 库里可能有手工注册的真实账号（如 alice），总计数是脆弱前提
        seed_link_ids = {3000000000000000001, 3000000000000000002, 3000000000000000003, 3000000000000000004}
        link_ids = {link.id for link in links}
        missing = seed_link_ids - link_ids
        assert not missing, f"种子关联缺失：{missing}，实际关联 {len(links)} 条"
        admin_roles = sorted(link.role_id for link in links if link.user_id == SEED_ADMIN_ID)
        assert admin_roles == [SEED_ROLE_ADMIN_ID, SEED_ROLE_REVIEWER_ID], admin_roles
        ok(f"admin 持角色 {admin_roles}（管理员+审核员）")

        # ---------- 4) partial unique：软删后同名可再注册 ----------
        step += 1
        print(f"{step}) uk_kh_user_username 为 partial unique（软删后同名可再注册）")
        # 未软删时重名 → 拒绝
        try:
            async with session.begin_nested():
                session.add(
                    User(id=9000000000000000001, username="admin", password="x")
                )
                await session.flush()
        except IntegrityError as err:
            assert "uk_kh_user_username" in str(err), str(err)
            ok("重名（未删除）被拒 → uk_kh_user_username")
        else:
            raise AssertionError("重名用户竟然插入成功，partial unique 未生效")

        # 软删后重名 → 放行
        async with session.begin_nested():
            tmp = User(id=9000000000000000002, username="smoke-dup", password="x")
            session.add(tmp)
            await session.flush()
            tmp.deleted = True
            await session.flush()
            session.add(User(id=9000000000000000003, username="smoke-dup", password="x"))
            await session.flush()
            ok("软删后同名再注册 → 放行")
        await session.rollback() # 丢弃本步全部写入

    # ---------- 5) 重复授权被拒 ----------
    step += 1
    print(f"{step}) uk_kh_user_role_user_role 阻止重复授予同一角色")
    async with async_session_factory() as session:
        try:
            async with session.begin_nested():
                session.add(
                    UserRole(
                        id=9000000000000000004,
                        user_id=SEED_ADMIN_ID,
                        role_id=SEED_ROLE_ADMIN_ID,
                    )
                )
                await session.flush()
        except IntegrityError as err:
            assert "uk_kh_user_role_user_role" in str(err), str(err)
            ok("重复授权被拒 → uk_kh_user_role_user_role")
        else:
            raise AssertionError("重复授权竟然成功，唯一约束未生效")
        await session.rollback()

    # ---------- 6) 外键无级联：删仍有角色的用户被拒 ----------
    step += 1
    print(f"{step}) 外键无级联：删仍有角色的用户被拒")
    async with async_session_factory() as session:
        user = (
            await session.execute(select(User).where(User.id == SEED_ADMIN_ID))
        ).scalar_one()
        try:
            async with session.begin_nested():
                await session.delete(user)
                await session.flush()
        except IntegrityError as err:
            assert "kh_user_role_user_id_fkey" in str(err), str(err)
            ok("删用户被拒 → kh_user_role_user_id_fkey")
        else:
            raise AssertionError("有角色关联的用户竟被删除，外键未生效")
        await session.rollback()

    # ---------- 7) 正确删除顺序：先解绑再删 ----------
    step += 1
    print(f"{step}) 正确删除顺序：先删 kh_user_role，再删 kh_user")
    async with async_session_factory() as session:
        victim_id = 9000000000000000005
        try:
            async with session.begin_nested():
                session.add(User(id=victim_id, username="smoke-victim", password="x"))
                await session.flush()
                session.add(
                    UserRole(
                        id=9000000000000000006,
                        user_id=victim_id,
                        role_id=SEED_ROLE_REVIEWER_ID,
                    )
                )
                await session.flush()

                # 顺序①：先解绑角色
                link = (
                    await session.execute(
                        select(UserRole).where(UserRole.user_id == victim_id)
                    )
                ).scalars().all()
                assert len(link) == 1
                await session.delete(link[0])
                await session.flush()
                # 顺序②：再删用户
                victim = (
                    await session.execute(select(User).where(User.id == victim_id))
                ).scalar_one()
                await session.delete(victim)
                await session.flush()
                ok("先解绑再删用户 → 成功")
        finally:
            await session.rollback()

    # ---------- 8) 数据未被污染 ----------
    step += 1
    print(f"{step}) 冒烟结束后数据未被污染")
    async with async_session_factory() as session:
        # 只断言种子数据原样存在（按固定 ID 逐条找），不断言总行数 ——
        # 库里可能有手工注册的真实账号（如 alice），总计数是脆弱前提
        for uid, uname in (
            (SEED_ADMIN_ID, "admin"),
            (SEED_REVIEWER_ID, "reviewer"),
            (SEED_USER_ID, "user"),
        ):
            u = (
                await session.execute(
                    select(User).where(
                        User.id == uid, User.status == 1, User.deleted == False
                    )
                )
            ).scalar_one()
            assert u.username == uname, f"种子账号 {uid} 异常：{u.username!r}"
        roles = (await session.execute(select(Role))).scalars().all()
        # 同样不断言 kh_role 总行数：库里有 2026-09-03 手工建的 ROLE_TEST「测试」，
        # 只校验 3 个种子角色仍在且字段未被冒烟用例改动（破坏性用例都在事务内回滚）
        role_by_id = {r.id: r for r in roles}
        for rid, code in (
            (SEED_ROLE_ADMIN_ID, "ROLE_ADMIN"),
            (SEED_ROLE_REVIEWER_ID, "ROLE_REVIEWER"),
            (SEED_ROLE_USER_ID, "ROLE_USER"),
        ):
            r = role_by_id.get(rid)
            assert r is not None, f"种子角色 {rid} 丢失"
            assert r.role_code == code and r.status == 1, f"种子角色 {code} 被改动：{r.role_code}/{r.status}"
        seed_link_ids = {3000000000000000001, 3000000000000000002, 3000000000000000003, 3000000000000000004}
        links = (await session.execute(select(UserRole))).scalars().all()
        missing = seed_link_ids - {link.id for link in links}
        assert not missing, f"种子关联缺失：{missing}"
        ok(f"种子数据原样（kh_user_role 共 {len(links)} 行，含非种子账号关联属正常）")

    print(f"\n全部通过（{step} 步）")


if __name__ == "__main__":
    asyncio.run(main())

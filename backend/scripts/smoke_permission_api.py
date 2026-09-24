"""权限模块管理端 E2E 冒烟

覆盖 PermissionController 已接线端点（list / tree / page / children / :id / POST / PUT / DELETE）：
  401 未登录 → 403 普通用户 → list 非空 → tree 含嵌套 → page 分页 →
  创建 201 → 编码查重 409 → 详情 200 → 父不能是自己 400 → 部分更新 200 →
  children 空 → 建子权限 201 → children 含子 → 删父(有子) 400 → 删子 200 →
  删父 200 → 假 id 404。

另含一处直接服务层断言（无对应 HTTP 端点）：admin 经 get_user_permission_codes
自动获得 ADMIN_OPERATION_PERMISSIONS（含 system:permission）。

种子数据只读（17 个 kh_permission 为 init.sql 预置）；临时权限结束硬删（先子后父）。
运行：cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_permission_api.py
"""

import asyncio
import secrets
import time

import httpx
from fastapi import FastAPI
from sqlalchemy import text

from app.api.v1 import api_router
from app.core.config import settings
from app.db.session import async_session_factory, engine
from app.services.permission import permission_service
from smoke_auth import SEED_ADMIN_ID, SEED_USER_ID, bearer_headers

logging_import = __import__("logging")
logging_import.basicConfig(
    level=logging_import.INFO, format="%(levelname)s [%(name)s] %(message)s"
)
engine.echo = False

app = FastAPI()
app.include_router(api_router, prefix=settings.API_V1_PREFIX)
API = settings.API_V1_PREFIX

RUN_TAG = f"{int(time.time())}{secrets.token_hex(3)}"
PERM_CODE = f"smoke:perm:{RUN_TAG}"
PERM_NAME = f"冒烟权限{RUN_TAG}"
CHILD_CODE = f"smoke:perm:child:{RUN_TAG}"

_results: list[str] = []
CREATED_IDS: list[int] = []


def check(step: object, ok: bool, detail: str) -> None:
    mark = "✅" if ok else "❌"
    _results.append(f"{mark} {step:>4} {detail}")


async def cleanup() -> None:
    """硬删临时权限（绕过软删；先子后父无所谓，这里整体 IN 删除）"""
    if not CREATED_IDS:
        return
    async with async_session_factory() as session:
        await session.execute(
            text("DELETE FROM kh_permission WHERE id = ANY(:ids)"),
            {"ids": CREATED_IDS},
        )
        await session.commit()
        print(f"🧹 已删除临时权限 {len(CREATED_IDS)} 条")


async def main() -> None:
    # 普通用户 token（ROLE_USER，非管理员，无 system:permission）
    user_headers = bearer_headers(SEED_USER_ID, "user")
    AUTH = bearer_headers()

    # 1. 未登录 401（匿名客户端，确认守卫默认拒绝）
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://smoke"
    ) as anon:
        r = await anon.get(f"{API}/permissions/list")
    check(1, r.status_code == 401, f"无 token GET /permissions/list → {r.status_code}")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://smoke",
        headers=AUTH,
    ) as client:
        # 2. 普通用户 403
        r = await client.get(f"{API}/permissions/list", headers=user_headers)
        check(2, r.status_code == 403, f"USER 角色 GET /permissions/list → {r.status_code}")

        # 3. admin list 非空（17 个种子）
        r = await client.get(f"{API}/permissions/list")
        check(3, r.status_code == 200 and len(r.json()) >= 17,
              f"ADMIN 角色 list → {r.status_code} 共 {len(r.json())} 条")

        # 4. tree 含嵌套（document 中心下应有子节点）
        r = await client.get(f"{API}/permissions/tree")
        assert r.status_code == 200, r.text
        has_children = any(len(node.get("children", [])) > 0 for node in r.json())
        check(4, has_children, f"tree 含嵌套子节点 → {has_children}（根节点 {len(r.json())} 个）")

        # 5. page 分页
        r = await client.get(f"{API}/permissions/page", params={"page": 1, "pageSize": 5})
        body = r.json()
        check(5, r.status_code == 200 and body["total"] >= 17 and len(body["items"]) == 5,
              f"page → total={body['total']} 本页 {len(body['items'])} 条")

        # 6. page keyword 模糊
        r = await client.get(f"{API}/permissions/page", params={"keyword": "document", "pageSize": 100})
        codes = [i["permissionCode"] for i in r.json()["items"]]
        check(6, all("document" in c for c in codes) and len(codes) > 0,
              f"keyword=document 命中 {len(codes)} 条（如 {codes[:3]}）")

        # 7. 创建 201（parentId 根 '0'）
        r = await client.post(f"{API}/permissions", json={
            "permissionName": PERM_NAME, "permissionCode": PERM_CODE,
            "permissionType": 2, "parentId": "0",
        })
        check(7, r.status_code == 201, f"POST /permissions → {r.status_code}")
        perm_id = r.json().get("id") if r.status_code == 201 else None
        if perm_id:
            CREATED_IDS.append(int(perm_id))

        # 8. 编码查重 409
        r = await client.post(f"{API}/permissions", json={
            "permissionName": "重复", "permissionCode": PERM_CODE, "permissionType": 2,
        })
        check(8, r.status_code == 409, f"重复 permissionCode → {r.status_code}（{r.json().get('detail')}）")

        # 9. 种子编码 also 409（document:create 已存在）
        r = await client.post(f"{API}/permissions", json={
            "permissionName": "x", "permissionCode": "document:create", "permissionType": 2,
        })
        check(9, r.status_code == 409, f"占用种子编码 → {r.status_code}")

        # 10. 详情 200
        if perm_id:
            r = await client.get(f"{API}/permissions/{perm_id}")
            check(10, r.status_code == 200 and r.json().get("permissionCode") == PERM_CODE,
                  f"GET /permissions/{{id}} → {r.status_code}")

        # 11. 父不能是自己 400
        if perm_id:
            r = await client.put(f"{API}/permissions/{perm_id}", json={"parentId": perm_id})
            check(11, r.status_code == 400, f"parentId==自身 → {r.status_code}（{r.json().get('detail')}）")

        # 12. 部分更新 200（改名 + 禁用）
        if perm_id:
            r = await client.put(f"{API}/permissions/{perm_id}", json={"permissionName": PERM_NAME + "(改)", "status": 0})
            check(12, r.status_code == 200 and r.json().get("status") == 0,
                  f"PUT 更新 → {r.status_code} status={r.json().get('status')}")
            # 还原为启用，避免后面 children/list 行为异常（仅冒烟内部）
            await client.put(f"{API}/permissions/{perm_id}", json={"status": 1})

        # 13. children 当前为空
        if perm_id:
            r = await client.get(f"{API}/permissions/{perm_id}/children")
            check(13, r.status_code == 200 and len(r.json()) == 0,
                  f"children（无子）→ {r.status_code} 共 {len(r.json())} 条")

        # 14. 建子权限 201
        r = await client.post(f"{API}/permissions", json={
            "permissionName": "子权限", "permissionCode": CHILD_CODE,
            "permissionType": 2, "parentId": perm_id,
        })
        check(14, r.status_code == 201, f"POST 子权限 → {r.status_code}")
        child_id = r.json().get("id") if r.status_code == 201 else None
        if child_id:
            CREATED_IDS.append(int(child_id))

        # 15. children 含子
        if perm_id and child_id:
            r = await client.get(f"{API}/permissions/{perm_id}/children")
            check(15, r.status_code == 200 and len(r.json()) == 1,
                  f"children（有子）→ {r.status_code} 共 {len(r.json())} 条")

        # 16. 删父（有子）400
        if perm_id:
            r = await client.delete(f"{API}/permissions/{perm_id}")
            check(16, r.status_code == 400, f"删父(有子) → {r.status_code}（{r.json().get('detail')}）")

        # 17. 删子 200
        if child_id:
            r = await client.delete(f"{API}/permissions/{child_id}")
            check(17, r.status_code == 200 and r.json().get("message") == "删除成功",
                  f"DELETE 子 → {r.status_code}")

        # 18. 删父 200
        if perm_id:
            r = await client.delete(f"{API}/permissions/{perm_id}")
            check(18, r.status_code == 200, f"DELETE 父 → {r.status_code}")
            # ⚠️ 不要 remove：API delete 只是软删（deleted=true），行仍在库里；
            # 必须留给 cleanup 物理删，否则每轮残留 2 行（历史教训：曾积累 8 行）

        # 19. 假 id 404
        r = await client.get(f"{API}/permissions/9999999999999999999")
        check(19, r.status_code == 404, f"假 id 详情 → {r.status_code}")

    # 20. 直接服务层：admin 自动获得 ADMIN_OPERATION_PERMISSIONS
    async with async_session_factory() as session:
        codes = await permission_service.get_user_permission_codes(session, SEED_ADMIN_ID)
        check(20, "system:permission" in codes and "document:create" in codes,
              f"admin 权限码含 system:permission/system:permission:create → "
              f"{'system:permission' in codes and 'document:create' in codes}")
        # 普通用户无管理员权限
        user_codes = await permission_service.get_user_permission_codes(session, SEED_USER_ID)
        check(21, "system:permission" not in user_codes,
              f"user 权限码不含 system:permission → {'system:permission' not in user_codes}")


if __name__ == "__main__":
    async def _run() -> None:
        await main()
        # 同一事件循环内清理，避免引擎连接池跨 loop 报错
        await cleanup()

    asyncio.run(_run())
    print("\n".join(_results))
    failed = [r for r in _results if r.startswith("❌")]
    if failed:
        raise SystemExit(f"\n{len(failed)} 个断言失败")
    print(f"\n全部 {len(_results)} 步通过 ✅")

"""
冒烟脚本共用：为受保护接口生成 Bearer 请求头

鉴权守卫上线后（见 app/api/auth_route.py），除 @public() 的接口外全部需要登录，
历史冒烟脚本因此需要在每个请求上带 Authorization 头。最省事的接法是**给 client 挂默认
请求头**（httpx.Client / TestClient 都支持），一处改动覆盖全部请求：

    from smoke_auth import bearer_headers

    with TestClient(app, headers=bearer_headers()) as client:   # 同步脚本
    async with httpx.AsyncClient(..., headers=bearer_headers()) as client:  # 异步脚本

这里**直接签发 JWT 而不调登录接口**：登录需要先有 client，而 client 又需要先有 token，
会形成死循环；直接签发也让上面这种单点改造成立。登录链路本身由 smoke_auth_guard.py 覆盖。

用的是 init.sql 预置的管理员 admin（ROLE_ADMIN + ROLE_REVIEWER），
ID 1000000000000000001 来自种子数据。

⚠️ 注意：守卫会拿 sub 去查库，所以这个 ID 对应的用户必须在库里存在
（种子账号被删 / 被禁用的话，冒烟脚本会拿到 404 / 401 而非 200）。
"""

from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import settings

# init.sql 种子数据：三个预置账号（ID / username 与种子一致）
#   admin    ROLE_ADMIN + ROLE_REVIEWER  → 可通过 @roles(REVIEWER, ADMIN) 的审核接口
#   reviewer ROLE_REVIEWER               → 同上
#   user     ROLE_USER                   → 审核接口应 403
SEED_ADMIN_ID = "1000000000000000001"
SEED_ADMIN_USERNAME = "admin"
SEED_REVIEWER_ID = "1000000000000000002"
SEED_REVIEWER_USERNAME = "reviewer"
SEED_USER_ID = "1000000000000000003"
SEED_USER_USERNAME = "user"

_TOKEN_TTL_HOURS = 2


def make_access_token(
    user_id: str = SEED_ADMIN_ID,
    username: str = SEED_ADMIN_USERNAME,
) -> str:
    """签发 access token，payload 与 AuthService._sign_token 一致

    结构：{sub, username, type='access', exp}；type 必须是 access，
    否则守卫按「无效的 access token」401 拒绝。
    """
    payload = {
        "sub": user_id,
        "username": username,
        "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(hours=_TOKEN_TTL_HOURS),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


def bearer_headers(
    user_id: str = SEED_ADMIN_ID,
    username: str = SEED_ADMIN_USERNAME,
) -> dict[str, str]:
    """构造 Authorization 请求头（可直接喂给 client 的 headers 参数）"""
    return {"Authorization": f"Bearer {make_access_token(user_id, username)}"}

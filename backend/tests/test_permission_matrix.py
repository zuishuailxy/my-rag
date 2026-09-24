"""RBAC 权限码聚合（mock PG 会话，不打真实库）

验证 get_user_permission_codes：直接权限 + 角色权限合并去重；拥有 ADMIN 角色时
自动追加管理员操作权限；非数字 user_id 直接返回 []。
"""
import pytest

from app.api.permission_guard import require_permission
from app.common.permissions import ADMIN_OPERATION_PERMISSIONS, ADMIN_ROLES
from app.common.roles import RoleCode
from app.services.permission import permission_service


class _FakeScalars:
    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def all(self) -> list[str]:
        return self._rows


class _FakeResult:
    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class _QueuedSession:
    """按调用顺序返回预置结果：direct → via_role → role_code"""

    def __init__(self, results: list[list[str]]) -> None:
        self._results = list(results)

    async def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return _FakeResult(self._results.pop(0))


async def test_admin_role_appends_operation_permissions() -> None:
    session = _QueuedSession([["document:list"], [], [RoleCode.ADMIN.value]])
    codes = set(await permission_service.get_user_permission_codes(session, "1001"))
    assert "document:list" in codes
    assert set(ADMIN_OPERATION_PERMISSIONS) <= codes, "ADMIN 应自动获得全部操作权限码"


async def test_direct_and_role_permissions_merge_dedup() -> None:
    session = _QueuedSession(
        [["search", "document:list"], ["search", "document:create"], [RoleCode.USER.value]]
    )
    codes = set(await permission_service.get_user_permission_codes(session, "1001"))
    assert codes == {"search", "document:list", "document:create"}
    assert not (set(ADMIN_OPERATION_PERMISSIONS) <= codes), "普通用户不应有管理员权限"


async def test_non_numeric_user_id_returns_empty() -> None:
    codes = await permission_service.get_user_permission_codes(_QueuedSession([]), "abc")
    assert codes == []


def test_require_permission_needs_at_least_one_code() -> None:
    with pytest.raises(ValueError):
        require_permission()


def test_role_constants() -> None:
    assert ADMIN_ROLES == (RoleCode.ADMIN.value,)
    assert RoleCode.ADMIN == "ROLE_ADMIN", "StrEnum 应可直接与字符串比较"
    assert "search" in ADMIN_OPERATION_PERMISSIONS

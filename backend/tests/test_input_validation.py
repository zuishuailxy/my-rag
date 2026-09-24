"""入参校验：超长字段应被 Pydantic 拦下（HTTP 422），而不是撞库变 500。"""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.schemas.auth import BCRYPT_MAX_PASSWORD_BYTES, LoginRequest, RegisterRequest


def test_login_username_too_long_rejected() -> None:
    with pytest.raises(ValidationError):
        LoginRequest(username="x" * 51, password="secret")


def test_login_password_over_bcrypt_limit_rejected() -> None:
    with pytest.raises(ValidationError):
        LoginRequest(username="u", password="x" * (BCRYPT_MAX_PASSWORD_BYTES + 1))


def test_register_real_name_too_long_rejected() -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(username="u", password="secret1", realName="x" * 51)


def test_http_returns_422_not_500_for_overlong_input() -> None:
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/login", json={"username": "x" * 51, "password": "secret"}
    )
    assert resp.status_code == 422, "超长入参应是校验错误（422），不应落到 PG 变 500"
    assert resp.status_code != 500

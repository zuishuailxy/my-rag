"""数据库「依赖不可用」的识别（纯逻辑，不连库）

这条判定的意义：把「依赖暂时抖动」与「代码有 bug」分开 ——
前者返 503（可重试），后者必须继续按 500 暴露。
**误判的代价是双向的**：
  - 漏判 → 前端只看到不透明的 500，不知道该不该重试；
  - 过判 → SQL 写错被伪装成「依赖抖动」，真 bug 被掩盖（所以这条测试最重要）。
"""
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.exc import TimeoutError as SAPoolTimeoutError

from app.common.db_errors import is_db_unavailable

_ORIG = RuntimeError("driver error")


def test_connectivity_types_are_detected() -> None:
    assert is_db_unavailable(OperationalError("SELECT 1", {}, _ORIG)) is True
    assert is_db_unavailable(InterfaceError("SELECT 1", {}, _ORIG)) is True
    assert is_db_unavailable(SAPoolTimeoutError("QueuePool limit reached")) is True


def test_builtin_connection_errors_are_detected() -> None:
    """实测（停掉 PG 后请求）：异常从鉴权守卫里冒出时是**原生内置异常**，
    不是 SQLAlchemyError —— 只注册后者会漏掉，依旧返回 500。

    这一条是本次修复的直接依据，别删。
    """
    assert is_db_unavailable(ConnectionRefusedError(61, "Connection refused")) is True
    assert is_db_unavailable(ConnectionResetError(54, "reset by peer")) is True
    assert is_db_unavailable(TimeoutError("connection timed out")) is True


def test_connection_invalidated_flag_is_detected() -> None:
    """连接被服务端掐断时 SQLAlchemy 会打这个标记，哪怕类型是普通 DBAPIError"""
    exc = DBAPIError("SELECT 1", {}, _ORIG, connection_invalidated=True)
    assert is_db_unavailable(exc) is True


def test_message_markers_are_detected() -> None:
    """不同驱动/版本把同类问题包装成不同异常类型，用消息特征兜一层"""
    for message in (
        "the database system is in recovery mode",
        "connection refused",
        "server closed the connection unexpectedly",
        "queuepool limit of size 20 overflow 10 reached",
    ):
        assert is_db_unavailable(DBAPIError(message, {}, _ORIG)) is True, message


def test_sql_bug_is_not_treated_as_unavailable() -> None:
    """**最关键的一条**：SQL/约束错误是代码 bug，不能伪装成「依赖不可用」"""
    assert is_db_unavailable(ProgrammingError("SELECT bad syntax", {}, _ORIG)) is False
    assert is_db_unavailable(IntegrityError("duplicate key", {}, _ORIG)) is False


def test_unrelated_exception_is_not_detected() -> None:
    assert is_db_unavailable(ValueError("boom")) is False
    assert is_db_unavailable(KeyError("missing")) is False

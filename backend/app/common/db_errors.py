"""数据库「依赖不可用」的识别

**为什么需要单独做这件事**
    依赖暂时不可用（PG crash recovery / 连接被掐 / 连接池打满）时，
    原先会直接冒成 **HTTP 500 Internal Server Error**。后果是：
      - 前端只能一直转圈，最后弹出「Internal Server Error」；
      - 用户**看不出这是「暂时不可用、可以重试」还是「代码坏了」**；
      - 监控上也无法把「依赖抖动」与「真 bug」区分开。

    所以这里把这类错误识别出来，交给全局异常处理器返回 **503 + Retry-After**。

**为什么不用「捕获所有 SQLAlchemyError」**
    SQL 写错（ProgrammingError）也是 SQLAlchemyError，但它是**代码 bug**，
    返回 503 会把 bug 伪装成「依赖抖动」，掩盖真问题。
    所以只对「连接性」错误降级，其余仍按 500 暴露出来。
"""
from __future__ import annotations

from sqlalchemy.exc import (
    DBAPIError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
)

try:  # SQLAlchemy 的 TimeoutError 与内置同名，显式起别名避免混淆
    from sqlalchemy.exc import TimeoutError as SAPoolTimeoutError
except ImportError:  # pragma: no cover - 版本兼容兜底
    SAPoolTimeoutError = None  # type: ignore[assignment,misc]

# 连接性错误的显式类型（SQLAlchemy 会把 asyncpg 的异常翻译到这几个）
_CONNECTIVITY_TYPES: tuple[type[BaseException], ...] = tuple(
    t for t in (OperationalError, InterfaceError, SAPoolTimeoutError) if t is not None
)

# ⚠️ 不是所有连接失败都会被 SQLAlchemy 包一层。实测（停掉 PG 后请求）：
# 异常从鉴权守卫里冒出来时是**原生 `ConnectionRefusedError`**（内置类型），
# 不是 SQLAlchemyError —— 只注册 SQLAlchemyError 的处理会漏掉它，仍然返回 500。
_NETWORK_TYPES: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError)

# 全局异常处理器需要为这些类型都注册（见 app/main.py）。
# 类型标成 type[Exception]（而不是 BaseException）：Starlette 的
# add_exception_handler 只接受 Exception 子类。
HANDLED_ERROR_TYPES: tuple[type[Exception], ...] = (
    SQLAlchemyError,
    ConnectionError,
    TimeoutError,
)

# 类型判断之外的兜底：不同驱动/版本把同一类问题包装成不同类型，
# 用消息特征兜一层（全部小写比较）。
_MARKERS = (
    "recovery mode",
    "cannot connect now",
    "connection refused",
    "connection was closed",
    "connection is closed",
    "server closed the connection",
    "connection reset",
    "connection timed out",
    "the database system is starting up",
    "the database system is shutting down",
    "queuepool limit",  # 连接池打满（小写化后比较）
    "too many clients",
)


def is_db_unavailable(exc: BaseException) -> bool:
    """异常是否表示「数据库暂时不可用」（而非 SQL/逻辑错误）

    判定顺序：显式类型 → `connection_invalidated` 标记 → 消息特征。
    """
    if isinstance(exc, _CONNECTIVITY_TYPES + _NETWORK_TYPES):
        return True

    # 连接被服务端掐断时，SQLAlchemy 会打这个标记（哪怕异常类型是 DBAPIError）
    if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
        return True

    if isinstance(exc, SQLAlchemyError):
        message = str(exc).lower()
        return any(marker.lower() in message for marker in _MARKERS)

    return False

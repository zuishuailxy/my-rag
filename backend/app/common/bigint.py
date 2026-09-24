"""
bigint（雪花 ID / 外键 ID）解析工具

背景：，
经 bigintTransformer 在 DTO 与响应中以字符串表示（避免 JS Number 精度丢失）。
Python 侧同样在 DTO 用 `str`，落库前需要转 `int`。

两个函数语义不同，按调用场景选择：

- `parse_bigint`：宽松解析，非法 / 超界返回 None。用于**路径参数**——调用方通常
  再抛 404（「文档不存在」），而不是 400，因为路径里的非法 ID 本质就是资源不存在。
- `require_bigint`：严格校验，非法 / 超界抛 HTTP 400，错误信息带字段名。用于
  **请求体 / 查询参数**里的 ID 字段——客户端传错了要明确告知，不能静默丢弃，
  更不能把非数字串一路送到 PG 由驱动报错变成 500。
"""

from fastapi import HTTPException

# PostgreSQL BIGINT（int8）上限；超出会在 INSERT/UPDATE 时被 PG 拒绝
INT64_MAX = 9223372036854775807


def parse_bigint(value: str | None) -> int | None:
    """字符串 → bigint；空 / 非法 / 超出 int64 范围返回 None（宽松，供路径参数用）"""
    if value is None or value == "":
        return None
    try:
        pk = int(value)
    except ValueError:
        return None
    if pk < 0 or pk > INT64_MAX:
        return None
    return pk


def require_bigint(value: str | None, field: str) -> int | None:
    """字符串 → bigint；空值返回 None，非法 / 超界抛 400（严格，供请求体字段用）

    Args:
        value: DTO 中的字符串 ID，None / 空串视为「未提供」
        field: 出错时回显的字段名，用 camelCase 与请求体保持一致（如 categoryId）
    """
    if value is None or value == "":
        return None
    try:
        pk = int(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {field}: {value}")
    if pk < 0 or pk > INT64_MAX:
        raise HTTPException(status_code=400, detail=f"Invalid {field}: {value}")
    return pk

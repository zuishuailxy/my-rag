"""
Pydantic 请求/响应模型（DTO）
"""

from app.schemas.document import (
    DocumentCreate,
    DocumentListResponse,
    DocumentMetadata,
    DocumentQuery,
    DocumentRemoveResponse,
    DocumentResponse,
    DocumentStatus,
    DocumentUpdate,
    IndexResult,
    UploadParseDto,
    UploadParseResult,
)

__all__ = [
    "DocumentCreate",
    "DocumentQuery",
    "DocumentStatus",
    "DocumentUpdate",
    "DocumentMetadata",
    "DocumentResponse",
    "DocumentListResponse",
    "DocumentRemoveResponse",
    "IndexResult",
    "UploadParseDto",
    "UploadParseResult",
]

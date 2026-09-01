"""
Pydantic 请求/响应模型（DTO）
"""

from app.schemas.auth import (
    AuthUser,
    LoginRequest,
    LoginResult,
    RefreshTokenRequest,
    RegisterRequest,
    RegisterResult,
)
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
from app.schemas.document_review import (
    ReviewDecision,
    ReviewTaskItem,
    ReviewTaskListResponse,
    ReviewTaskQuery,
)

__all__ = [
    "LoginRequest",
    "RegisterRequest",
    "RefreshTokenRequest",
    "AuthUser",
    "LoginResult",
    "RegisterResult",
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
    "ReviewTaskQuery",
    "ReviewDecision",
    "ReviewTaskItem",
    "ReviewTaskListResponse",
]

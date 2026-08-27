"""
文档接口（对应 NestJS DocumentController）

- POST   /documents           创建文档
- GET    /documents           分页查询列表（QueryDocumentDto）
- GET    /documents/{id}      查询详情（附带正文）
- PATCH  /documents/{id}      更新文档（UpdateDocumentDto）
- DELETE /documents/{id}      软删除
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.models.document import DocumentStatus
from app.schemas.document import (
    DocumentCreate,
    DocumentListResponse,
    DocumentQuery,
    DocumentRemoveResponse,
    DocumentResponse,
    DocumentUpdate,
)
from app.services.document import DocumentService

router = APIRouter()


def get_document_service() -> DocumentService:
    """文档服务（无状态，可直接复用）"""
    return DocumentService()


@router.post("", summary="创建文档", status_code=201, response_model=DocumentResponse)
async def create_document(
    dto: DocumentCreate,
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    """创建文档：写 Mongo 正文 + Postgres 元数据"""
    return DocumentResponse.model_validate(await service.create(session, dto))


@router.get("", summary="分页查询文档列表", response_model=DocumentListResponse)
async def find_all_documents(
    title: str | None = Query(default=None, description="标题（模糊）"),
    category_id: str | None = Query(
        default=None, alias="categoryId", description="分类 ID"
    ),
    team_id: str | None = Query(default=None, alias="teamId", description="团队 ID"),
    author_id: str | None = Query(
        default=None, alias="authorId", description="作者 ID"
    ),
    status: DocumentStatus | None = Query(
        default=None, description="状态：0 草稿 / 1 已发布 / 2 已归档"
    ),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(
        default=20, ge=1, le=100, alias="pageSize", description="每页条数"
    ),
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> DocumentListResponse:
    """按标题模糊 / 分类 / 团队 / 作者 / 状态筛选，created_at 倒序分页"""
    query = DocumentQuery(
        title=title,
        category_id=category_id,
        team_id=team_id,
        author_id=author_id,
        status=status,
        page=page,
        page_size=page_size,
    )
    return DocumentListResponse.model_validate(await service.find_all(session, query))


@router.get("/{doc_id}", summary="查询文档详情", response_model=DocumentResponse)
async def find_one_document(
    doc_id: str,
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    """查询文档详情，附带 Mongo 正文"""
    return DocumentResponse.model_validate(await service.find_one(session, doc_id))


@router.patch("/{doc_id}", summary="更新文档", response_model=DocumentResponse)
async def update_document(
    doc_id: str,
    dto: DocumentUpdate,
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    """局部更新：正文 / 摘要 / 元数据字段均可选"""
    return DocumentResponse.model_validate(await service.update(session, doc_id, dto))


@router.delete("/{doc_id}", summary="软删除文档", response_model=DocumentRemoveResponse)
async def remove_document(
    doc_id: str,
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> DocumentRemoveResponse:
    """软删除：Postgres 与 Mongo 双侧 deleted=true"""
    return DocumentRemoveResponse.model_validate(await service.remove(session, doc_id))

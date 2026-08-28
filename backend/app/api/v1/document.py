"""
文档接口（对应 NestJS DocumentController）

- POST   /documents              创建文档
- POST   /documents/upload/parse 上传并解析文件，创建草稿
- GET    /documents              分页查询列表（QueryDocumentDto）
- GET    /documents/{id}         查询详情（附带正文）
- PATCH  /documents/{id}         更新文档（UpdateDocumentDto）
- DELETE /documents/{id}         软删除
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
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
    IndexResult,
    UploadParseDto,
    UploadParseResult,
)
from app.services.document import DocumentService

router = APIRouter()

# 上传大小上限（对应 TS Multer fileSize: 50MB）
MAX_UPLOAD_SIZE = 50 * 1024 * 1024


def get_document_service() -> DocumentService:
    """文档服务（无状态，可直接复用）"""
    return DocumentService()


@router.post(
    "/upload/parse",
    summary="上传并解析文件",
    status_code=201,
    response_model=UploadParseResult,
)
async def upload_and_parse(
    file: UploadFile = File(..., description="文件（form-data 字段名: file）"),
    category_id: str | None = Form(default=None, alias="categoryId"),
    team_id: str | None = Form(default=None, alias="teamId"),
    author_id: str | None = Form(default=None, alias="authorId"),
    tags: str | None = Form(default=None, description="标签（逗号分隔）"),
    remark: str | None = Form(default=None, description="备注"),
    create_by: str | None = Form(default=None, alias="createBy"),
    is_public: bool | None = Form(default=None, alias="isPublic"),
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> UploadParseResult:
    """上传文件并解析为 Markdown，创建草稿文档"""
    if not file or not file.filename:
        raise HTTPException(
            status_code=400, detail="请上传文件（form-data 字段名: file）"
        )

    data = await file.read()
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="文件大小超过 50MB 限制")

    meta = UploadParseDto(
        category_id=category_id,
        team_id=team_id,
        author_id=author_id,
        tags=tags,
        remark=remark,
        create_by=create_by,
        is_public=is_public,
    )
    return UploadParseResult.model_validate(
        await service.upload_and_create_document(
            session, data, file.filename, file.content_type, meta
        )
    )


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


@router.post(
    "/{doc_id}/index",
    summary="索引文档（RAG：分块+向量入 PGVector）",
    response_model=IndexResult,
)
async def index_document(
    doc_id: str,
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> IndexResult:
    """发布后索引：拉正文 → 分块 → Embedding → 写入 doc_chunk（幂等重建）

    当前为手动/同步触发；接入异步任务队列后由「文档发布」事件驱动。
    """
    return IndexResult.model_validate(await service.index_document(session, doc_id))


@router.put(
    "/{doc_id}/publish",
    summary="发布文档",
    response_model=DocumentResponse,
)
async def publish_document(
    doc_id: str,
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    """直接发布文档（无审核）；发布后 MQ 异步触发 RAG / KG / ES 索引"""
    return DocumentResponse.model_validate(await service.publish(session, doc_id))


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

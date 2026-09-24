"""
文档接口

- POST /documents 创建文档
- POST /documents/upload/parse 上传并解析文件，创建草稿
- GET /documents 分页查询列表（QueryDocumentDto）
- GET /documents/{id} 查询详情（附带正文）
- PATCH /documents/{id} 更新文档（UpdateDocumentDto）
- DELETE /documents/{id} 软删除
- PUT /documents/{id}/publish 发布（需审核时进待审，免审直接上架）
- PUT /documents/{id}/archive 归档（已发布 → 已归档，清索引）
- PUT /documents/{id}/save-draft 保存草稿（已发布 → 草稿，清索引）
- GET /documents/reviews/tasks 审核任务列表（QueryReviewTasksDto）
- GET /documents/reviews/tasks/pending-count 待审数量
- POST /documents/{id}/reviews/submit 单独提交审核
- GET /documents/{id}/reviews/current 当前待审记录（无则 null）
- GET /documents/{id}/reviews/history 该文档全部审核历史
- POST /documents/reviews/tasks/{taskId}/approve 审核通过
- POST /documents/reviews/tasks/{taskId}/reject 审核驳回

路由顺序：reviews/tasks 等含字面量前缀的路由注册在 /{doc_id} 之前，
避免被路径参数吃掉（FastAPI 按注册顺序匹配）。

状态码：（其余 200），故新建 / 提交 / 审核三个 POST 均为 201。

鉴权（对应）：
- 本 router 用 auth_router() → **全部接口都需要 Bearer token**
- 逐接口权限码：
  POST /upload/parse、POST / → document:create
  GET / → document:list
  GET /{id} → document:list
  PUT /{id}/publish、archive、save-draft→ document:edit
  PATCH /{id} → document:edit
  DELETE /{id} → document:delete
  POST /{id}/reviews/submit → document:edit
  GET /{id}/reviews/current、history → document:list + document:review
  审核 4 接口（见下）额外 document:review
  ADMIN 经 ADMIN_OPERATION_PERMISSIONS 自动拥有上述全部权限码。
- 4 个审核接口额外 @roles(RoleCode.REVIEWER, RoleCode.ADMIN)：
  GET /reviews/tasks、GET /reviews/tasks/pending-count、
  POST /reviews/tasks/{taskId}/approve、POST /reviews/tasks/{taskId}/reject
  这 4 个接口同时挂 @roles 与 require_permission(document:review)。
- 需要用户身份的接口用 Depends(get_current_user)（等价 @CurrentUser()）：
  create / upload-parse 回填 authorId、createBy；update 回填 updateBy；
  approve / reject 取审核人 userId 与 `realName ?? username`。
  审核人**不从请求体取**。

可见性：
- GET /documents 普通用户只看「自己写的 ∪ 已发布且公开 ∪ 已发布且所在团队」
- GET /documents/{id} 同上判权，读不到抛 403
两者都传 access_from_user(current_user)；ADMIN / REVIEWER 不受限。
⚠️ 服务层的 scope=None 表示不过滤，端点漏传即等于放开可见性。
"""

from fastapi import Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth_route import auth_router, roles
from app.api.deps import get_current_user
from app.api.permission_guard import require_permission
from app.common.document_access import access_from_user
from app.common.roles import RoleCode
from app.db.session import get_db_session
from app.schemas.auth import AuthUser
from app.models.document import DocumentEntity, DocumentStatus
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
from app.schemas.document_review import (
    ReviewDecision,
    ReviewTaskItem,
    ReviewTaskListResponse,
    ReviewTaskQuery,
    ReviewTaskStatus,
)
from app.services.document import DocumentService, document_to_dict
from app.services.document_review import DocumentReviewService

router = auth_router()

# 上传大小上限
MAX_UPLOAD_SIZE = 50 * 1024 * 1024


def get_document_service() -> DocumentService:
    """文档服务（无状态，可直接复用）"""
    return DocumentService()


def get_document_review_service() -> DocumentReviewService:
    """文档审核服务（无状态，可直接复用）"""
    return DocumentReviewService()


def _doc_response(entity: DocumentEntity) -> DocumentResponse:
    """ORM 实体 → 文档响应

    审核服务返回的是 DocumentEntity，这里复用 document_to_dict 做 bigint → 字符串转换；
    content 在 DocumentResponse 中可空，故不带正文也可通过校验。
    """
    return DocumentResponse.model_validate(document_to_dict(entity))


def _reviewer_name(user: AuthUser) -> str:
    """审核人姓名：`realName ?? username`

    username 是必填项，因此结果一定是非空字符串。
    """
    return user.real_name or user.username


@router.post(
    "/upload/parse",
    summary="上传并解析文件",
    status_code=201,
    response_model=UploadParseResult,
    dependencies=[Depends(require_permission("document:create"))],
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
    current_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> UploadParseResult:
    """上传文件并解析为 Markdown，创建草稿文档（作者信息取当前登录用户）"""
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
            session, data, file.filename, file.content_type, meta, current_user
        )
    )


@router.post(
    "",
    summary="创建文档",
    status_code=201,
    response_model=DocumentResponse,
    dependencies=[Depends(require_permission("document:create"))],
)
async def create_document(
    dto: DocumentCreate,
    current_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    """创建文档：写 Mongo 正文 + Postgres 元数据

    authorId / createBy / updateBy 一律取当前登录用户；
    请求体即使带 authorId / createBy 也会被服务层忽略 —— 无法伪造作者 / 创建人。
    """
    return DocumentResponse.model_validate(
        await service.create(session, dto, current_user)
    )


@router.get(
    "/reviews/tasks",
    summary="审核任务列表",
    response_model=ReviewTaskListResponse,
    dependencies=[Depends(require_permission("document:review"))],
)
@roles(RoleCode.REVIEWER, RoleCode.ADMIN)
async def list_review_tasks(
    status: ReviewTaskStatus
    | None = Query(
        default=None, description="筛选：pending 待办 / approved 已通过 / rejected 已驳回"
    ),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(
        default=20, ge=1, le=100, alias="pageSize", description="每页条数"
    ),
    session: AsyncSession = Depends(get_db_session),
    service: DocumentReviewService = Depends(get_document_review_service),
) -> ReviewTaskListResponse:
    """审核员工作台：待办 / 已通过 / 已驳回，按 created_at 倒序分页（默认只列待审）"""
    return await service.list_tasks(
        session, ReviewTaskQuery(status=status, page=page, page_size=page_size)
    )


@router.get(
    "/reviews/tasks/pending-count",
    summary="待审核数量",
    dependencies=[Depends(require_permission("document:review"))],
)
@roles(RoleCode.REVIEWER, RoleCode.ADMIN)
async def pending_review_count(
    session: AsyncSession = Depends(get_db_session),
    service: DocumentReviewService = Depends(get_document_review_service),
) -> int:
    """待审任务总数（导航角标等）；直接返回数字"""
    return await service.get_pending_count(session)


@router.get(
    "",
    summary="分页查询文档列表",
    response_model=DocumentListResponse,
    dependencies=[Depends(require_permission("document:list"))],
)
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
        default=None, description="状态：0 草稿 / 1 已发布 / 2 已归档 / 3 待审核"
    ),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(
        default=20, ge=1, le=100, alias="pageSize", description="每页条数"
    ),
    current_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> DocumentListResponse:
    """按标题模糊 / 分类 / 团队 / 作者 / 状态筛选，created_at 倒序分页

    普通用户只看得到「自己写的 ∪ 已发布且公开 ∪ 已发布且所在团队」
    （)` 里的 accessFromUser 分支）；
    ADMIN / REVIEWER 不受限。可见性条件与上方筛选条件是 AND 关系。
    """
    query = DocumentQuery(
        title=title,
        category_id=category_id,
        team_id=team_id,
        author_id=author_id,
        status=status,
        page=page,
        page_size=page_size,
    )
    return DocumentListResponse.model_validate(
        await service.find_all(session, query, access_from_user(current_user))
    )


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
    dependencies=[Depends(require_permission("document:edit"))],
)
async def publish_document(
    doc_id: str,
    current_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    """发布文档

    - 审核开启（DOCUMENT_REQUIRE_APPROVAL≠false）：草稿 / 已发布 → 待审核，不建索引
    - 免审：草稿 / 已发布 / 已归档 → 已发布，MQ 异步触发 RAG / Search / KG 索引
    """
    return DocumentResponse.model_validate(
        await service.publish(session, doc_id, current_user)
    )


@router.put(
    "/{doc_id}/archive",
    summary="归档文档",
    response_model=DocumentResponse,
    dependencies=[Depends(require_permission("document:edit"))],
)
async def archive_document(
    doc_id: str,
    current_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    """归档：仅已发布可归档，状态置 Archived 并清理 RAG / Search / KG 索引"""
    return DocumentResponse.model_validate(
        await service.archive(session, doc_id, current_user)
    )


@router.put(
    "/{doc_id}/save-draft",
    summary="保存为草稿",
    response_model=DocumentResponse,
    dependencies=[Depends(require_permission("document:edit"))],
)
async def save_document_as_draft(
    doc_id: str,
    current_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    """保存草稿：仅已发布可下架，状态置 Draft 并清理 RAG / Search / KG 索引"""
    return DocumentResponse.model_validate(
        await service.save_as_draft(session, doc_id, current_user)
    )


@router.post(
    "/{doc_id}/reviews/submit",
    summary="提交审核",
    status_code=201,
    response_model=DocumentResponse,
    dependencies=[Depends(require_permission("document:edit"))],
)
async def submit_review(
    doc_id: str,
    current_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    service: DocumentReviewService = Depends(get_document_review_service),
) -> DocumentResponse:
    """单独提交审核（也可由 publish 在需审核时内部调用）

    草稿 / 已发布 → 待审核；来自已发布时会先清索引，审核期间不可检索。
    写权校验在服务层，
    故必须传 current_user —— 漏传等于这条写路径不设防。
    """
    return _doc_response(
        await service.submit_for_review(session, doc_id, current_user)
    )


@router.get(
    "/{doc_id}/reviews/current",
    summary="当前待审记录",
    response_model=ReviewTaskItem | None,
    dependencies=[
        Depends(require_permission("document:list", "document:review"))
    ],
)
async def get_current_review(
    doc_id: str,
    session: AsyncSession = Depends(get_db_session),
    service: DocumentReviewService = Depends(get_document_review_service),
) -> ReviewTaskItem | None:
    """该文档当前待审任务（review_result IS NULL），无则返回 null"""
    return await service.get_current_review(session, doc_id)


@router.get(
    "/{doc_id}/reviews/history",
    summary="审核历史",
    response_model=list[ReviewTaskItem],
    dependencies=[
        Depends(require_permission("document:list", "document:review"))
    ],
)
async def get_review_history(
    doc_id: str,
    session: AsyncSession = Depends(get_db_session),
    service: DocumentReviewService = Depends(get_document_review_service),
) -> list[ReviewTaskItem]:
    """该文档全部审核记录（含已通过、已驳回），按 created_at 倒序"""
    return await service.get_review_history(session, doc_id)


@router.post(
    "/reviews/tasks/{task_id}/approve",
    summary="审核通过",
    status_code=201,
    response_model=DocumentResponse,
    dependencies=[Depends(require_permission("document:review"))],
)
@roles(RoleCode.REVIEWER, RoleCode.ADMIN)
async def approve_review(
    task_id: str,
    dto: ReviewDecision,
    current_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    service: DocumentReviewService = Depends(get_document_review_service),
) -> DocumentResponse:
    """审核通过：文档置 Published + 重建 RAG / Search / KG 索引

    审核人取当前登录用户，
    不从请求体取 —— 请求体只有 reviewComment。
    """
    return _doc_response(
        await service.approve_review(
            session,
            task_id,
            current_user.user_id,
            _reviewer_name(current_user),
            dto.review_comment,
        )
    )


@router.post(
    "/reviews/tasks/{task_id}/reject",
    summary="审核驳回",
    status_code=201,
    response_model=DocumentResponse,
    dependencies=[Depends(require_permission("document:review"))],
)
@roles(RoleCode.REVIEWER, RoleCode.ADMIN)
async def reject_review(
    task_id: str,
    dto: ReviewDecision,
    current_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    service: DocumentReviewService = Depends(get_document_review_service),
) -> DocumentResponse:
    """审核驳回：文档回 Draft，作者可修改后再次提审

    驳回意见必填由服务层校验（400「驳回意见不能为空」）；''`
    兜空串后交给服务层判空，此处 review_comment 为 None 时等价。
    审核人同 approve，取当前登录用户。
    """
    return _doc_response(
        await service.reject_review(
            session,
            task_id,
            dto.review_comment,
            current_user.user_id,
            _reviewer_name(current_user),
        )
    )


@router.get(
    "/{doc_id}",
    summary="查询文档详情",
    response_model=DocumentResponse,
    dependencies=[Depends(require_permission("document:list"))],
)
async def find_one_document(
    doc_id: str,
    current_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    """查询文档详情，附带 Mongo 正文

    读不到（他人未发布 / 非公开且非本团队）抛 403「无权查看该文档」
    （)` 的 canReadDocument 检查）。
    """
    return DocumentResponse.model_validate(
        await service.find_one(session, doc_id, scope=access_from_user(current_user))
    )


@router.patch(
    "/{doc_id}",
    summary="更新文档",
    response_model=DocumentResponse,
    dependencies=[Depends(require_permission("document:edit"))],
)
async def update_document(
    doc_id: str,
    dto: DocumentUpdate,
    current_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> DocumentResponse:
    """局部更新：正文 / 摘要 / 元数据字段均可选

    updateBy 未显式传时取当前登录用户（(actor?.userId)`）。
    """
    return DocumentResponse.model_validate(
        await service.update(session, doc_id, dto, current_user)
    )


@router.delete(
    "/{doc_id}",
    summary="软删除文档",
    response_model=DocumentRemoveResponse,
    dependencies=[Depends(require_permission("document:delete"))],
)
async def remove_document(
    doc_id: str,
    current_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    service: DocumentService = Depends(get_document_service),
) -> DocumentRemoveResponse:
    """软删除：Postgres 与 Mongo 双侧 deleted=true"""
    return DocumentRemoveResponse.model_validate(
        await service.remove(session, doc_id, current_user)
    )

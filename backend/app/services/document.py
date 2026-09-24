"""
文档服务（对应 TypeORM DocumentService）

- 元数据：PostgreSQL（kh_document，SQLAlchemy 异步会话）
- 正文：MongoDB（document_content，motor）
- 关联：content_id ↔ Mongo _id，documentId ↔ 文档 id
"""
import logging
import re
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import HTTPException
from sqlalchemy import ColumnElement, and_, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.bigint import parse_bigint, require_bigint
from app.common.document_access import (
    DocumentAccessScope,
    can_read_document,
    can_write_document,
)
from app.common.snowflake import next_snowflake_id
from app.db.mongo import get_document_content_collection
from app.models.document import DocumentEntity, DocumentStatus
from app.models.document_content import DocumentContent
from app.models.document_status import (
    can_archive,
    can_edit_content,
    can_publish_from,
)
from app.schemas.auth import AuthUser
from app.schemas.document import (
    DocumentCreate,
    DocumentQuery,
    DocumentUpdate,
    UploadParseDto,
)
from app.services.document_review import document_review_service
from app.services.es_index import es_chunk_index_service
from app.services.file_parser import file_parser_service
from app.services.graph_build import graph_build_service
from app.services.pipeline_publisher import pipeline_publisher
from app.services.rustfs import rustfs_service
from app.services.search_index import search_index_service
from app.utils.markdown import (
    decode_upload_filename,
    get_extension,
    title_from_filename,
)

logger = logging.getLogger("DocumentService")


def _utcnow() -> datetime:
    """UTC 当前时间（naive，适配 TIMESTAMP WITHOUT TIME ZONE 列）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_status(value: int | DocumentStatus | None) -> DocumentStatus:
    """列值（int）→ DocumentStatus；未知值原样按 int 构造（IntEnum 允许）"""
    if isinstance(value, DocumentStatus):
        return value
    return DocumentStatus(int(value) if value is not None else 0)


def document_to_dict(entity: DocumentEntity) -> dict:
    """实体 → 响应 dict

    放在模块级是为了给路由层复用：审核服务返回的是 DocumentEntity（不带正文），
    路由需要同一套 bigint → 字符串的转换后再交给 DocumentResponse 校验。
    """

    def s(v: int | None) -> str | None:
        return str(v) if v is not None else None

    def dt(v: datetime | None) -> str | None:
        return v.isoformat() if v is not None else None

    return {
        "id": str(entity.id),
        "title": entity.title,
        "contentId": entity.content_id,
        "summary": entity.summary,
        "categoryId": s(entity.category_id),
        "teamId": s(entity.team_id),
        "authorId": s(entity.author_id),
        "coverImage": entity.cover_image,
        "tags": entity.tags,
        "status": int(entity.status) if entity.status is not None else None,
        "remark": entity.remark,
        "viewCount": entity.view_count,
        "likeCount": entity.like_count,
        "commentCount": entity.comment_count,
        "favouriteCount": entity.favourite_count,
        "wordCount": entity.word_count,
        "publishTime": dt(entity.publish_time),
        "isPublic": entity.is_public,
        "createdAt": dt(entity.created_at),
        "updatedAt": dt(entity.updated_at),
        "createBy": s(entity.create_by),
        "updateBy": s(entity.update_by),
        "deleted": entity.deleted,
    }


class DocumentService:
    """文档服务"""

    # ---------------------------------------------------------------- create

    async def create(
        self,
        session: AsyncSession,
        dto: DocumentCreate,
        actor: AuthUser,
    ) -> dict:
        """创建文档：生成雪花 ID → 写 Mongo 正文（拿 ObjectId）→ 写 Postgres 元数据

        actor 为当前登录用户（(dto, user)` 的 @CurrentUser()，**必传**）：

        authorId / createBy / updateBy 三者**恒为 actor.userId**，一律不读 DTO
。CreateDocumentDto 里虽有 authorId / createBy 字段，
        但服务层忽略 → 客户端无法在创建时伪造作者、创建人或「最后修改人」。
        ⚠️ 刻意差异说明：，
        经核对为笔误（CreateDocumentDto 无 updateBy 字段，写 `dto.updateBy` 编译不过），
        实际效果等价 `actor.userId`，此处按效果对齐。
        副作用（刻意为之）：导入场景显式传 createBy=原作者时，createBy / updateBy
        记的都是实际执行导入的操作者，不会被复制成原作者。

        Postgres 写入失败时回滚删除已写入的 Mongo 正文，避免脏数据。
        """
        requested_status = (
            dto.status if dto.status is not None else DocumentStatus.DRAFT
        )
        # 创建时不允许直接设为 Archived / PendingReview
        if requested_status not in (
            DocumentStatus.DRAFT,
            DocumentStatus.PUBLISHED,
        ):
            raise HTTPException(status_code=400, detail="创建文档仅允许草稿或已发布状态")
        # DOCUMENT_REQUIRE_APPROVAL=true 时必须先草稿，再通过 publish / 提交审核发布
        if (
            requested_status == DocumentStatus.PUBLISHED
            and document_review_service.is_require_approval()
        ):
            raise HTTPException(
                status_code=400, detail="开启审核时请先创建草稿，再提交发布/审核"
            )

        doc_id = next_snowflake_id()
        status = requested_status
        word_count = self.count_words(dto.content)
        # 未传 summary 时，从正文截取预览作为 contentSummary
        content_summary = (
            dto.summary
            if dto.summary is not None
            else self.build_content_summary(dto.content)
        )
        # ID 字段先校验再写 Mongo：非法值（如 Swagger 默认的 "string"）直接 400，
        # 避免先写正文再回滚删除，也避免非数字串落到 PG 报驱动级错误变成 500
        #
        # DTO 未传 authorId / createBy 时回退当前登录用户。
        # update_by 单独取 actor。
        actor_id = actor.user_id
        # createBy / updateBy 均恒取当前登录用户—— 不读 dto.createBy，
        # 否则客户端可在创建时伪造「创建人」。
        create_by = require_bigint(actor_id, "createBy")
        update_by = require_bigint(actor_id, "updateBy")
        category_id = require_bigint(dto.category_id, "categoryId")
        team_id = require_bigint(dto.team_id, "teamId")
        # `authorId: actor.userId`，不读 dto.authorId
        # （避免客户端伪造作者）。actor 必传，故 actor_id 恒非空。
        author_id = require_bigint(actor_id, "authorId")
        now = _utcnow()

        # 先写 Mongo，_id 由驱动自动生成 ObjectId
        content_doc = DocumentContent(
            document_id=str(doc_id),
            content=dto.content,
            content_length=len(dto.content),
            content_summary=content_summary,
            version=1,
            deleted=False,
            created_at=now,
            updated_at=now,
        )
        coll = get_document_content_collection()
        result = await coll.insert_one(content_doc.model_dump(by_alias=True))
        # ObjectId 转字符串，存入 Postgres content_id
        content_id = str(result.inserted_id)

        try:
            entity = DocumentEntity(
                id=doc_id,
                title=dto.title,
                content_id=content_id,
                summary=dto.summary,
                category_id=category_id,
                team_id=team_id,
                author_id=author_id,
                cover_image=dto.cover_image,
                tags=dto.tags,
                status=status,
                remark=dto.remark,
                is_public=dto.is_public if dto.is_public is not None else False,
                word_count=word_count,
                # 创建即发布时，记录发布时间
                publish_time=now if status == DocumentStatus.PUBLISHED else None,
                create_by=create_by,
                update_by=update_by,
                deleted=False,
            )
            session.add(entity)
            await session.commit()
            await session.refresh(entity)

            # 仅 Published 才建索引。需审时创建即 Published 已在上方拒绝，
            # 能走到这里的 Published 一定是免审；草稿不投 MQ。
            if status == DocumentStatus.PUBLISHED:
                await self._safe_publish(entity)

            return {**document_to_dict(entity), "content": dto.content}
        except Exception:
            # Postgres 失败：物理删除刚写入的 Mongo 正文
            await coll.delete_one({"_id": result.inserted_id})
            raise

    # -------------------------------------------------------------- find_all

    async def find_all(
        self,
        session: AsyncSession,
        query: DocumentQuery,
        scope: DocumentAccessScope | None = None,
    ) -> dict:
        """分页查询文档列表（仅元数据，不含正文）

        支持按标题模糊、分类 / 团队 / 作者 / 状态筛选；默认排除已软删记录。

        可见性（)` 分支）：
        普通用户只能看到「自己写的 ∪ 已发布且公开 ∪ 已发布且所在团队」：
          - 不受限（ADMIN / REVIEWER）：不加任何可见性条件，与原先行为一致
          - 有团队：author_id = 我
                    OR (status = 已发布 AND (is_public = true OR team_id ∈ 我的团队))
          - 无团队：author_id = 我 OR (status = 已发布 AND is_public = true)
        「自己写的」分支**不限制 status** —— 自己的草稿 / 待审 / 已归档都要能在列表里
        看到；他人的未发布文档一律不可见。

        ⚠️ scope=None 表示**不加可见性过滤**（与 search_documents(scope=None) /
        hybrid_search(scope=None) / KG 查询侧 scope=None 同一约定）。因此生产链路
        （api/v1/document.py 的 GET /documents）**必须**传 access_from_user(user)，
        漏传即等于把全库文档列表暴露给任何登录用户；脚本 / 内部调用才可省略。
        """
        page = query.page
        page_size = query.page_size

        # 显式标注 ColumnElement[bool]：is_()/ilike()/== 等返回类型不同，统一用公共父类
        conditions: list[ColumnElement[bool]] = [DocumentEntity.deleted.is_(False)]

        # —— 可见性过滤（必须排在用户筛选条件之前，两者是 AND 关系）——
        if scope is not None and not scope.unrestricted:
            # JWT 里的 userId / teamIds 一定是合法雪花 ID；这里仍用宽松解析，
            # 非法值只影响「自己 / 团队」分支的命中（不抛 400，因为不是客户端输入）
            me = parse_bigint(scope.user_id)
            team_ids = [
                pk
                for pk in (parse_bigint(team_id) for team_id in scope.team_ids)
                if pk is not None
            ]
            # 公开 ∪ 所在团队；无团队时退化为只判公开
            visible_to_others = (
                or_(
                    DocumentEntity.is_public.is_(True),
                    DocumentEntity.team_id.in_(team_ids),
                )
                if team_ids
                else DocumentEntity.is_public.is_(True)
            )
            conditions.append(
                or_(
                    # 自己的文档：不限状态；userId 非法（None）时该分支恒假
                    DocumentEntity.author_id == me if me is not None else false(),
                    # 他人的文档：必须已发布，且公开或属于我的团队
                    and_(
                        DocumentEntity.status == DocumentStatus.PUBLISHED,
                        visible_to_others,
                    ),
                )
            )

        if query.title:
            conditions.append(DocumentEntity.title.ilike(f"%{query.title}%"))
        # 用真值判断（与 title 一致、(categoryId)` 一致）：
        # ?categoryId= 空串视为未筛选，而非筛选 category_id IS NULL；
        # 非空但非法（如 "string" / 超 int64）由 require_bigint 抛 400
        if query.category_id:
            conditions.append(
                DocumentEntity.category_id
                == require_bigint(query.category_id, "categoryId")
            )
        if query.team_id:
            conditions.append(
                DocumentEntity.team_id == require_bigint(query.team_id, "teamId")
            )
        if query.author_id:
            conditions.append(
                DocumentEntity.author_id == require_bigint(query.author_id, "authorId")
            )
        if query.status is not None:
            conditions.append(DocumentEntity.status == query.status)

        total = await session.scalar(
            select(func.count()).select_from(DocumentEntity).where(*conditions)
        )
        stmt = (
            select(DocumentEntity)
            .where(*conditions)
            .order_by(DocumentEntity.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = (await session.scalars(stmt)).all()

        return {
            "items": [document_to_dict(item) for item in items],
            "total": total or 0,
            "page": page,
            "pageSize": page_size,
        }

    # --------------------------------------------------------------- find_one

    async def find_one(
        self,
        session: AsyncSession,
        doc_id: str,
        with_content: bool = True,
        scope: DocumentAccessScope | None = None,
    ) -> dict:
        """查询文档详情（默认附带 Mongo 正文）

        可见性：scope 非空时按 can_read_document 判权，读不到抛 403「无权查看该文档」
        （)` 里的 `if (user && !canReadDocument(...))`）。

        scope=None 表示不判权 ——，仅供脚本 /
        内部调用；HTTP 端点（GET /documents/{id}）**必须**传 access_from_user(user)，
        否则列表过滤形同虚设（拿到 id 就能直读他人未发布文档）。
        """
        entity = await self._get_active(session, doc_id)
        if scope is not None and not can_read_document(entity, scope):
            raise HTTPException(status_code=403, detail="无权查看该文档")
        if not with_content:
            return document_to_dict(entity)
        return {
            **document_to_dict(entity),
            "content": await self._get_content(entity.content_id),
        }

    # ----------------------------------------------------------------- update

    async def update(
        self,
        session: AsyncSession,
        doc_id: str,
        dto: DocumentUpdate,
        actor: AuthUser,
    ) -> dict:
        """更新文档

        - 有 content：同步更新 Mongo 正文，并递增 version
        - 仅改 summary：同步更新 Mongo contentSummary
        - 其余字段只更新 Postgres 元数据
        - 审核中（PendingReview）不可改正文 / 标题
        - PATCH 不允许随意改 status；仅兼容 Published→Draft
        - 提交后按状态变化同步 RAG / Search / KG

        actor 为当前登录用户（(id, dto, user)`）：`updateBy` 恒记 actor.userId、
        **不读 dto.updateBy**；`author_id` **不可改**。
        两者都为了让客户端无法伪造归属 / 审计人（DTO 字段仍在，服务层忽略）。

        写权：ADMIN 或作者本人，否则 403「无权修改该文档」
        （)`，位置在取到实体之后、状态校验之前）。
        """
        entity = await self._get_active(session, doc_id)
        self._assert_writable(entity, actor)
        coll = get_document_content_collection()
        old_status = _as_status(entity.status)
        # 可见性变更检测用
        old_visibility = (entity.is_public, entity.team_id)

        # —— 状态与编辑权限（待审核中不可改标题 / 正文）——
        if old_status == DocumentStatus.PENDING_REVIEW:
            if dto.content is not None or dto.title is not None:
                raise HTTPException(status_code=400, detail="审核中的文档不可编辑")
        elif not can_edit_content(old_status):
            raise HTTPException(status_code=400, detail="当前文档状态不允许编辑")

        # —— ID 字段先校验 ——
        # 位置刻意排在「改 entity / 写 Mongo」之前：非法 ID（如 Swagger 默认的
        # "string"、或超 int64 的数字串）在这里就 400，避免出现「正文已更新、PG 元数据
        # 未提交」的两侧不一致，也避免非数字串落到 PG 由驱动报错变成 500。
        # 空串保持「清空该字段」语义（与改动前一致）。
        new_category_id = require_bigint(dto.category_id, "categoryId")
        new_team_id = require_bigint(dto.team_id, "teamId")
        # ⚠️ 刻意差异：、另手动加 updateBy，
        # 但 `update` **两个都不读**（恒 `doc.updateBy = actor.userId`）。此处同样不读 ——
        # 否则客户端可借 PATCH 改文档归属、伪造「最后修改人」。DTO 字段保留，
        # 只是服务层忽略。

        # PATCH 不允许随意改 status；仅兼容 Published→Draft，其余走专用接口
        if dto.status is not None and dto.status != old_status:
            if (
                dto.status == DocumentStatus.DRAFT
                and old_status == DocumentStatus.PUBLISHED
            ):
                entity.status = DocumentStatus.DRAFT
            else:
                raise HTTPException(
                    status_code=400,
                    detail="请使用 publish / archive / save-draft / 审核接口变更文档状态",
                )

        content_changed = False
        new_content: str | None = None

        # —— 正文变更 ——
        if dto.content is not None:
            content_changed = True
            new_content = dto.content
            content_summary = (
                dto.summary
                if dto.summary is not None
                else self.build_content_summary(dto.content)
            )
            result = await coll.update_one(
                {"_id": ObjectId(entity.content_id), "deleted": False},
                {
                    "$set": {
                        "content": dto.content,
                        "contentLength": len(dto.content),
                        "contentSummary": content_summary,
                        "updatedAt": _utcnow(),
                    },
                    "$inc": {"version": 1}, # 版本号 +1
                },
            )
            if result.matched_count == 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Document content {entity.content_id} not found",
                )
            entity.word_count = self.count_words(dto.content)
        elif dto.summary is not None:
            # 只改摘要时，同步 Mongo 侧预览字段
            await coll.update_one(
                {"_id": ObjectId(entity.content_id), "deleted": False},
                {"$set": {"contentSummary": dto.summary, "updatedAt": _utcnow()}},
            )

        # —— 元数据字段（有传才覆盖）——
        if dto.title is not None:
            entity.title = dto.title
        if dto.summary is not None:
            entity.summary = dto.summary
        if dto.category_id is not None:
            entity.category_id = new_category_id
        if dto.team_id is not None:
            entity.team_id = new_team_id
        if dto.cover_image is not None:
            entity.cover_image = dto.cover_image
        if dto.tags is not None:
            entity.tags = dto.tags
        if dto.remark is not None:
            entity.remark = dto.remark
        if dto.is_public is not None:
            entity.is_public = dto.is_public
        # 无条件记当前登录用户
        entity.update_by = require_bigint(actor.user_id, "updateBy")

        await session.commit()
        await session.refresh(entity)

        # 本次已带新正文则直接返回；否则再查一次 Mongo
        final_content = (
            new_content
            if new_content is not None
            else await self._get_content(entity.content_id)
        )

        # 已发布文档改内容 / 下架时同步 RAG / Search / KG
        # （需审核模式下已发布改稿不立即重建索引，等再次发布 / 审核通过）
        await self._sync_pipeline_after_update(
            entity,
            old_status,
            _as_status(entity.status),
            content_changed,
            visibility_changed=old_visibility != (entity.is_public, entity.team_id),
        )

        return {**document_to_dict(entity), "content": final_content}

    # ----------------------------------------------------------------- remove

    async def remove(self, session: AsyncSession, doc_id: str, actor: AuthUser) -> dict:
        """软删除文档：Postgres、Mongo 两侧都将 deleted 置为 true（不物理删正文），
        并异步清理 ES 搜索索引与向量块（MQ 投递失败只记日志）

        写权：ADMIN 或作者本人，否则 403（)`）。
        """
        entity = await self._get_active(session, doc_id)
        self._assert_writable(entity, actor)

        if _as_status(entity.status) == DocumentStatus.PUBLISHED:
            # 仅已发布需要清索引；草稿 / 待审 / 归档删除时不投递 unpublish
            await self._safe_unpublish(doc_id)

        entity.deleted = True
        # `doc.updateBy = actor.userId;`（actor 必传，无 ?. 可选链）
        entity.update_by = require_bigint(actor.user_id, "updateBy")
        await session.commit()
        await get_document_content_collection().update_one(
            {"_id": ObjectId(entity.content_id)},
            {"$set": {"deleted": True, "updatedAt": _utcnow()}},
        )
        return {"id": doc_id, "deleted": True}

    # ------------------------------------------------------ upload & create

    async def upload_and_create_document(
        self,
        session: AsyncSession,
        file_data: bytes,
        filename: str,
        content_type: str | None,
        meta: UploadParseDto,
        actor: AuthUser,
    ) -> dict:
        """上传并解析文件 → 创建草稿文档

        流程：校验格式 → 解析为 Markdown（PDF 图片走 RustFS）→ 原文件存 RustFS →
        创建草稿（DocumentStatus.DRAFT）→ 返回摘要。

        actor 透传给 create（(file, meta, user)` 同款），
        用于回填 authorId / createBy。
        """
        if not file_data:
            raise HTTPException(status_code=400, detail="文件不能为空")

        original_filename = decode_upload_filename(filename)
        extension = get_extension(original_filename)

        if not file_parser_service.is_supported(extension):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"不支持的文件格式: {extension}，支持的格式: "
                    f"{file_parser_service.supported_list()}"
                ),
            )

        logger.info(
            "上传并解析文件：name=%s, size=%d, ext=%s",
            original_filename,
            len(file_data),
            extension,
        )

        # PDF 图片上传器（RustFS；未启用时解析器自动降级为纯文本）
        uploader = None
        if rustfs_service.is_enabled():
            async def _upload_pdf_image(
                data: bytes, name: str, ctype: str
            ) -> str:
                return await rustfs_service.upload_bytes(
                    data, file_name=name, content_type=ctype, prefix="pdf-images"
                )

            uploader = _upload_pdf_image

        # 解析
        try:
            parsed_content = await file_parser_service.parse(
                original_filename, file_data, uploader=uploader
            )
        except HTTPException:
            raise
        except Exception as err:
            logger.error("文件解析失败：name=%s, error=%s", original_filename, err)
            raise HTTPException(status_code=400, detail=f"文件解析失败: {err}")

        # 原文件上传 RustFS
        file_url: str | None = None
        if rustfs_service.is_enabled():
            try:
                file_url = await rustfs_service.upload_bytes(
                    file_data,
                    file_name=original_filename,
                    content_type=content_type or "application/octet-stream",
                    prefix="documents",
                )
            except Exception as err:
                logger.error("原文件上传 RustFS 失败：%s", err)
                raise HTTPException(status_code=400, detail=f"原文件上传失败: {err}")
        else:
            logger.warning("RustFS 未启用，跳过原文件上传")

        title = title_from_filename(original_filename)

        created = await self.create(
            session,
            DocumentCreate(
                title=title,
                content=parsed_content,
                category_id=meta.category_id,
                team_id=meta.team_id,
                tags=meta.tags,
                remark=meta.remark,
                is_public=meta.is_public,
                status=DocumentStatus.DRAFT,
                # 刻意不传 meta.authorId / meta.createBy：
                # 也没传这两个（CreateDocumentDto 有此字段但 create 一律忽略），
                # 作者 / 创建人恒取当前登录用户。
            ),
            actor,
        )

        preview_len = min(200, len(parsed_content))
        result = {
            "documentId": created["id"],
            "title": title,
            "fileUrl": file_url,
            "fileSize": len(file_data),
            "fileExtension": extension,
            "contentLength": len(parsed_content),
            "contentPreview": parsed_content[:preview_len],
            "status": DocumentStatus.DRAFT,
        }

        logger.info(
            "文件解析并创建文档成功：documentId=%s, title=%s, ext=%s, chars=%d, fileUrl=%s",
            created["id"],
            title,
            extension,
            len(parsed_content),
            file_url,
        )
        return result

    # --------------------------------------------------------------- indexing

    async def index_document(self, session: AsyncSession, doc_id: str) -> dict:
        """发布后索引（RAG 消费者）：分块 → 嵌入 → 写入 PGVector doc_chunk"""
        pk = parse_bigint(doc_id)
        if pk is None:
            raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
        from app.services.indexing import index_document_vectors

        try:
            chunk_count = await index_document_vectors(session, pk)
        except ValueError as err:
            raise HTTPException(status_code=404, detail=str(err))
        return {"documentId": doc_id, "chunkCount": chunk_count}

    # --------------------------------------------------------------- publish

    async def publish(self, session: AsyncSession, doc_id: str, actor: AuthUser) -> dict:
        """发布文档

        - 需审核：Draft / Published → PendingReview（不建索引）
        - 免审：Draft / Published / Archived → Published + 建索引

        写权：ADMIN 或作者本人，否则 403（)`）。
        """
        logger.info("发布文档：documentId=%s", doc_id)

        entity = await self._get_active(session, doc_id)
        self._assert_writable(entity, actor)
        status = _as_status(entity.status)

        if not can_publish_from(status):
            raise HTTPException(status_code=400, detail="当前文档状态不允许发布")

        if status == DocumentStatus.PENDING_REVIEW:
            raise HTTPException(status_code=400, detail="文档审核中，请等待审核结果")

        if document_review_service.is_require_approval() and status in (
            DocumentStatus.DRAFT,
            DocumentStatus.PUBLISHED,
        ):
            # 草稿或已发布：进入待审，不建索引；
            # 来自 Published 时 submit_for_review 内部会清旧索引
            # actor 透传（(id, actor)`）：
            # 写权已由本方法开头的 _assert_writable 拦过，此处再判一次无害且保真
            saved = await document_review_service.submit_for_review(
                session, doc_id, actor
            )
            content = await self._get_content(saved.content_id)
            return {**document_to_dict(saved), "content": content}

        return await self.direct_publish(session, doc_id, actor)

    async def direct_publish(
        self, session: AsyncSession, doc_id: str, actor: AuthUser | None = None
    ) -> dict:
        """免审直接发布（Draft / Published / Archived / PendingReview → Published）

        也供 DocumentReviewService.approve_review 间接使用
        （审核通过后 status→Published，索引由审核服务自行投递）。
        """
        entity = await self._get_active(session, doc_id)

        if not can_publish_from(_as_status(entity.status)):
            raise HTTPException(status_code=400, detail="当前文档状态不允许发布")

        entity.status = DocumentStatus.PUBLISHED
        entity.publish_time = _utcnow()
        if actor is not None and actor.user_id:
            # `if (actor?.userId) doc.updateBy = actor.userId;`
            entity.update_by = require_bigint(actor.user_id, "updateBy")
        await session.commit()
        await session.refresh(entity)

        content = await self._get_content(entity.content_id)
        await self._safe_publish(entity)

        logger.info("文档发布成功：documentId=%s", doc_id)
        return {**document_to_dict(entity), "content": content}

    # ---------------------------------------------------------------- archive

    async def archive(
        self, session: AsyncSession, doc_id: str, actor: AuthUser
    ) -> dict:
        """归档：Published → Archived，清索引

        写权：ADMIN 或作者本人，否则 403（)`）。
        """
        entity = await self._get_active(session, doc_id)
        self._assert_writable(entity, actor)

        if not can_archive(_as_status(entity.status)):
            raise HTTPException(status_code=400, detail="只有已发布文档可以归档")

        entity.status = DocumentStatus.ARCHIVED
        # `doc.updateBy = actor.userId;`（actor 必传）
        entity.update_by = require_bigint(actor.user_id, "updateBy")
        await session.commit()
        await session.refresh(entity)
        await self._safe_unpublish(doc_id)

        logger.info("文档已归档：documentId=%s", doc_id)
        return document_to_dict(entity)

    # ------------------------------------------------------------ save as draft

    async def save_as_draft(
        self, session: AsyncSession, doc_id: str, actor: AuthUser
    ) -> dict:
        """已发布 → 草稿（保存草稿），清索引

        写权：ADMIN 或作者本人，否则 403（)`）。
        """
        entity = await self._get_active(session, doc_id)
        self._assert_writable(entity, actor)

        if _as_status(entity.status) != DocumentStatus.PUBLISHED:
            raise HTTPException(status_code=400, detail="只有已发布文档可以保存为草稿")

        entity.status = DocumentStatus.DRAFT
        # `doc.updateBy = actor.userId;`（actor 必传）
        entity.update_by = require_bigint(actor.user_id, "updateBy")
        await session.commit()
        await session.refresh(entity)
        await self._safe_unpublish(doc_id)

        logger.info("文档已保存为草稿：documentId=%s", doc_id)
        return document_to_dict(entity)

    # ---------------------------------------------------------------- helpers

    async def _sync_pipeline_after_update(
        self,
        doc: DocumentEntity,
        old_status: DocumentStatus,
        new_status: DocumentStatus,
        content_changed: bool,
        visibility_changed: bool = False,
    ) -> None:
        """更新后根据状态变化同步索引

        - Published → 非 Published：清索引
        - 仍为 Published 且正文变了：**免审**时重建索引；**需审**时等再次发布 / 审核通过
          ⚠️ 该分支只在免审时短路 return —— 需审时不返回，流程继续往下走
        - 仍为 Published 且可见性变了（is_public / team_id）：
          就地更新三处索引的可见性字段 —— chunk（ES kh_chunk）、文档级
          （ES kh_document）、图谱文档节点（Neo4j KnowledgeDocument），
          不必重新分块 / 重新嵌入 / 重新拉全文 / 重新抽实体建图
。

        两个分支可**同时命中**：免审下重建索引本身就带上了新可见性（本分支再刷一次幂等），
        需审下不重建索引，可见性完全靠本分支兜住 —— 故本分支不得被上面的 return 吞掉。

        为什么必须处理可见性变更：检索可见性靠索引上的 is_public/team_id/author_id
        过滤，文档由公开转私有后若不同步，未授权用户仍能通过 chunk 混合检索、
        文档级全文检索（POST /search）或图谱四个端点（/graph/*）命中其内容 /
        标题摘要 / 提及实体。
        （PG 向量侧按 JOIN kh_document 实时取，不受此影响，但仍一并更新保持两侧一致。）
        """
        was_published = old_status == DocumentStatus.PUBLISHED
        is_published = new_status == DocumentStatus.PUBLISHED

        if was_published and not is_published:
            await self._safe_unpublish(doc.id)
            return

        # ()` 写在
        # if 条件**里面**（`isPublished && contentChanged && !isRequireApproval()`），
        # 所以需审模式下这个分支整体不成立，流程继续往下走可见性同步。
        # 曾经把 `not is_require_approval()` 挪进 if 体内 —— 那等于「需审也 return」，
        # 需审模式下可见性变更被整个丢弃（2026-09-16 实测越权：PG 已私有化，
        # ES kh_chunk / kh_document / Neo4j 三处仍留「公开 + 原团队」旧值，任何用户可搜到）。
        # 判断的语义是「要不要重建索引」，不是「要不要继续走这个函数」。
        if (
            is_published
            and content_changed
            and not document_review_service.is_require_approval()
        ):
            await self._safe_publish(doc)
            return

        if is_published and visibility_changed:
            # 三套索引的字段风格不同（chunk 用 snake_case、document 用 camelCase、
            # Neo4j 文档节点用 authorId/isPublic/teamId），各自有 update_visibility
            # 实现；任一失败只记日志，不阻断文档更新。
            #
            # 刻意差异：
            # 三处调用，本项目沿用「document.py 直接调具体服务」的既有结构（编排器
            # 只负责 MQ 消费侧的建索引），此处顺序 await 三处，语义等价。
            # 外层 try) }
            # catch { warn }`：三处实现内部都已自行降级（不抛），这里是兜底 ——
            # 将来某处改成抛错，也不该把整个 PATCH 打成 500。
            try:
                await es_chunk_index_service.update_visibility(
                    doc.id, doc.is_public, doc.team_id, doc.author_id
                )
                await search_index_service.update_visibility(
                    doc.id, doc.is_public, doc.team_id, doc.author_id
                )
                await graph_build_service.update_visibility(
                    doc.id, doc.is_public, doc.team_id, doc.author_id
                )
            except Exception as err: # noqa: BLE001
                logger.warning(
                    "可见性同步失败（不影响文档保存）：documentId=%s, %s", doc.id, err
                )

    async def _safe_publish(self, doc: DocumentEntity) -> None:
        """投递 MQ：RAG 分块向量 + 全文搜索 + KG 建图（失败不回滚文档状态）

        消息只带 documentId，正文由消费者按 ID 拉库，此处不再传 content。
        """
        try:
            await pipeline_publisher.after_publish(doc.id)
        except Exception as err:
            logger.warning(
                "索引投递失败（不影响文档状态）：documentId=%s, error=%s",
                doc.id,
                err,
            )

    async def _safe_unpublish(self, document_id: str | int) -> None:
        """投递 MQ：删除该文档在 ES / Neo4j 等侧的索引数据"""
        try:
            await pipeline_publisher.after_unpublish(document_id)
        except Exception as err:
            logger.warning(
                "索引清理投递失败：documentId=%s, error=%s", document_id, err
            )

    async def _get_active(self, session: AsyncSession, doc_id: str) -> DocumentEntity:
        """按 id 查询未软删文档，不存在（或 id 非法 / 超出 bigint 范围）则抛 404"""
        pk = parse_bigint(doc_id)
        if pk is None:
            raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
        stmt = select(DocumentEntity).where(
            DocumentEntity.id == pk, DocumentEntity.deleted.is_(False)
        )
        entity = await session.scalar(stmt)
        if entity is None:
            raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
        return entity

    @staticmethod
    def _assert_writable(doc: DocumentEntity, actor: AuthUser) -> None:
        """写权校验：只有 ADMIN 或作者本人可改

        权限码（require_permission("document:edit")）只回答「能不能用这类接口」，
        不回答「能不能动**这一篇**」。缺了本校验，任何持 document:edit / document:delete
        的角色（种子里的 ROLE_USER 就同时持有两者）都能改删他人文档。

        REVIEWER 不在白名单：审核员走 reviews/tasks 的 approve / reject 处置他人文档，
        不走 update / publish / archive / save-draft / remove。

        越权抛 403「无权修改该文档」。
        """
        if not can_write_document(doc, actor):
            raise HTTPException(status_code=403, detail="无权修改该文档")

    async def _get_content(self, content_id: str) -> str:
        """按 content_id 拉取未删除的正文（缺失或无效 ObjectId 时返回空串）"""
        try:
            oid = ObjectId(content_id)
        except Exception:
            return ""
        doc = await get_document_content_collection().find_one(
            {"_id": oid, "deleted": False}
        )
        return doc.get("content", "") if doc else ""

    @staticmethod
    def build_content_summary(content: str, max_len: int = 200) -> str:
        """从正文截取预览摘要：压缩连续空白后截断到 maxLen，超出则追加省略号"""
        trimmed = re.sub(r"\s+", " ", content.strip())
        return trimmed if len(trimmed) <= max_len else f"{trimmed[:max_len]}..."

    @staticmethod
    def count_words(content: str) -> int:
        """统计正文字数（中英混合）

        - 中日韩汉字（U+4E00–U+9FFF）：每个字符计 1 字
        - 英文等拉丁文本：按空白分词，每个单词计 1 字
        """
        trimmed = content.strip()
        if not trimmed:
            return 0
        cjk = len(re.findall(r"[\u4e00-\u9fff]", trimmed))
        # 汉字替换为空格避免与英文粘连，再按空白分词
        latin = len(re.sub(r"[\u4e00-\u9fff]", " ", trimmed).strip().split())
        return cjk + latin


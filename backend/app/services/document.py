"""
文档服务（对应 TypeORM DocumentService）

- 元数据：PostgreSQL（kh_document，SQLAlchemy 异步会话）
- 正文：MongoDB（document_content，motor）
- 关联：content_id ↔ Mongo _id，documentId ↔ 文档 id
"""
import re
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import HTTPException
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.snowflake import next_snowflake_id
from app.db.mongo import get_document_content_collection
from app.models.document import DocumentEntity, DocumentStatus
from app.models.document_content import DocumentContent
from app.schemas.document import DocumentCreate, DocumentQuery, DocumentUpdate


def _utcnow() -> datetime:
    """UTC 当前时间（naive，适配 TIMESTAMP WITHOUT TIME ZONE 列）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_bigint(value: str | None) -> int | None:
    """DTO 的字符串 ID → BIGINT（雪花 ID 在 Python 侧为 int）"""
    if value is None or value == "":
        return None
    return int(value)


# PostgreSQL BIGINT（int8）上限
INT64_MAX = 9223372036854775807


def _parse_bigint(value: str | None) -> int | None:
    """字符串 → bigint；空 / 非法 / 超出 int64 范围返回 None"""
    if value is None or value == "":
        return None
    try:
        pk = int(value)
    except ValueError:
        return None
    if pk < 0 or pk > INT64_MAX:
        return None
    return pk


class DocumentService:
    """文档服务"""

    # ---------------------------------------------------------------- create

    async def create(self, session: AsyncSession, dto: DocumentCreate) -> dict:
        """创建文档：生成雪花 ID → 写 Mongo 正文（拿 ObjectId）→ 写 Postgres 元数据

        Postgres 写入失败时回滚删除已写入的 Mongo 正文，避免脏数据。
        """
        doc_id = next_snowflake_id()
        status = dto.status if dto.status is not None else DocumentStatus.DRAFT
        word_count = self.count_words(dto.content)
        # 未传 summary 时，从正文截取预览作为 contentSummary
        content_summary = (
            dto.summary
            if dto.summary is not None
            else self.build_content_summary(dto.content)
        )
        create_by = _to_bigint(dto.create_by)
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
                category_id=_to_bigint(dto.category_id),
                team_id=_to_bigint(dto.team_id),
                author_id=_to_bigint(dto.author_id),
                cover_image=dto.cover_image,
                tags=dto.tags,
                status=status,
                remark=dto.remark,
                is_public=dto.is_public if dto.is_public is not None else False,
                word_count=word_count,
                # 创建即发布时，记录发布时间
                publish_time=now if status == DocumentStatus.PUBLISHED else None,
                create_by=create_by,
                update_by=create_by,
                deleted=False,
            )
            session.add(entity)
            await session.commit()
            await session.refresh(entity)
        except Exception:
            # Postgres 失败：物理删除刚写入的 Mongo 正文
            await coll.delete_one({"_id": result.inserted_id})
            raise

        return {**self._to_dict(entity), "content": dto.content}

    # -------------------------------------------------------------- find_all

    async def find_all(self, session: AsyncSession, query: DocumentQuery) -> dict:
        """分页查询文档列表（仅元数据，不含正文）

        支持按标题模糊、分类 / 团队 / 作者 / 状态筛选；默认排除已软删记录。
        """
        page = query.page
        page_size = query.page_size

        # 显式标注 ColumnElement[bool]：is_()/ilike()/== 等返回类型不同，统一用公共父类
        conditions: list[ColumnElement[bool]] = [DocumentEntity.deleted.is_(False)]
        if query.title:
            conditions.append(DocumentEntity.title.ilike(f"%{query.title}%"))
        if query.category_id is not None:
            cid = _parse_bigint(query.category_id)
            if cid is None:
                raise HTTPException(
                    status_code=400, detail=f"Invalid categoryId: {query.category_id}"
                )
            conditions.append(DocumentEntity.category_id == cid)
        if query.team_id is not None:
            tid = _parse_bigint(query.team_id)
            if tid is None:
                raise HTTPException(
                    status_code=400, detail=f"Invalid teamId: {query.team_id}"
                )
            conditions.append(DocumentEntity.team_id == tid)
        if query.author_id is not None:
            aid = _parse_bigint(query.author_id)
            if aid is None:
                raise HTTPException(
                    status_code=400, detail=f"Invalid authorId: {query.author_id}"
                )
            conditions.append(DocumentEntity.author_id == aid)
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
            "items": [self._to_dict(item) for item in items],
            "total": total or 0,
            "page": page,
            "pageSize": page_size,
        }

    # --------------------------------------------------------------- find_one

    async def find_one(
        self, session: AsyncSession, doc_id: str, with_content: bool = True
    ) -> dict:
        """查询文档详情（默认附带 Mongo 正文）"""
        entity = await self._get_active(session, doc_id)
        if not with_content:
            return self._to_dict(entity)
        return {
            **self._to_dict(entity),
            "content": await self._get_content(entity.content_id),
        }

    # ----------------------------------------------------------------- update

    async def update(
        self, session: AsyncSession, doc_id: str, dto: DocumentUpdate
    ) -> dict:
        """更新文档

        - 有 content：同步更新 Mongo 正文，并递增 version
        - 仅改 summary：同步更新 Mongo contentSummary
        - 其余字段只更新 Postgres 元数据
        - 首次变为「已发布」时写入 publishTime
        """
        entity = await self._get_active(session, doc_id)
        coll = get_document_content_collection()

        # —— 正文变更 ——
        if dto.content is not None:
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
                    "$inc": {"version": 1},  # 版本号 +1
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
            entity.category_id = _to_bigint(dto.category_id)
        if dto.team_id is not None:
            entity.team_id = _to_bigint(dto.team_id)
        if dto.author_id is not None:
            entity.author_id = _to_bigint(dto.author_id)
        if dto.cover_image is not None:
            entity.cover_image = dto.cover_image
        if dto.tags is not None:
            entity.tags = dto.tags
        if dto.remark is not None:
            entity.remark = dto.remark
        if dto.is_public is not None:
            entity.is_public = dto.is_public
        if dto.update_by is not None:
            entity.update_by = _to_bigint(dto.update_by)

        # 状态从非发布 → 发布时，记录发布时间
        if dto.status is not None:
            if (
                dto.status == DocumentStatus.PUBLISHED
                and entity.status != DocumentStatus.PUBLISHED
            ):
                entity.publish_time = _utcnow()
            entity.status = dto.status

        await session.commit()
        await session.refresh(entity)

        # 本次已带新正文则直接返回；否则再查一次 Mongo
        if dto.content is not None:
            return {**self._to_dict(entity), "content": dto.content}
        return {
            **self._to_dict(entity),
            "content": await self._get_content(entity.content_id),
        }

    # ----------------------------------------------------------------- remove

    async def remove(self, session: AsyncSession, doc_id: str) -> dict:
        """软删除文档：Postgres、Mongo 两侧都将 deleted 置为 true（不物理删正文）"""
        entity = await self._get_active(session, doc_id)

        entity.deleted = True
        await session.commit()
        await get_document_content_collection().update_one(
            {"_id": ObjectId(entity.content_id)},
            {"$set": {"deleted": True, "updatedAt": _utcnow()}},
        )
        return {"id": doc_id, "deleted": True}

    # ---------------------------------------------------------------- helpers

    async def _get_active(self, session: AsyncSession, doc_id: str) -> DocumentEntity:
        """按 id 查询未软删文档，不存在（或 id 非法 / 超出 bigint 范围）则抛 404"""
        pk = _parse_bigint(doc_id)
        if pk is None:
            raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
        stmt = select(DocumentEntity).where(
            DocumentEntity.id == pk, DocumentEntity.deleted.is_(False)
        )
        entity = await session.scalar(stmt)
        if entity is None:
            raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")
        return entity

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

    @staticmethod
    def _to_dict(entity: DocumentEntity) -> dict:
        """实体 → 响应 dict（bigint 转字符串与 TS bigintTransformer 一致，键名 camelCase）"""

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

"""
RustFS 文件存储服务（S3 兼容，对应 NestJS RustfsService / StorageModule）

- 客户端：boto3（同步 SDK），阻塞调用通过 asyncio.to_thread 放入线程池，不阻塞事件循环
- 上传返回 URL：{public_base}/{bucket}/{key}
- bucket 不存在时自动创建（幂等，容忍并发创建）
"""
import asyncio
import logging
import re
import uuid
from datetime import datetime

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from fastapi import HTTPException

from app.core.config import settings

logger = logging.getLogger(__name__)


class RustfsService:
    """RustFS 文件存储（S3 兼容）"""

    def __init__(self) -> None:
        self._enabled = settings.RUSTFS_ENABLED
        self._bucket = settings.RUSTFS_BUCKET
        self._public_base = (
            settings.RUSTFS_PUBLIC_URL or settings.RUSTFS_ENDPOINT
        ).rstrip("/")
        # boto3 客户端构造不做网络 IO，可安全地在初始化时创建
        self._client = (
            boto3.client(
                "s3",
                endpoint_url=settings.RUSTFS_ENDPOINT,
                region_name=settings.RUSTFS_REGION,
                aws_access_key_id=settings.RUSTFS_ACCESS_KEY,
                aws_secret_access_key=settings.RUSTFS_SECRET_KEY,
                config=Config(
                    s3={"addressing_style": "path"},
                    signature_version="s3v4",
                ),
            )
            if self._enabled
            else None
        )

        if not self._enabled:
            logger.warning("RustFS 已禁用（RUSTFS_ENABLED=false），文件上传将跳过")
        else:
            logger.info(
                "RustFS 已配置: endpoint=%s, bucket=%s, public=%s",
                settings.RUSTFS_ENDPOINT,
                self._bucket,
                self._public_base,
            )

    def is_enabled(self) -> bool:
        """RustFS 是否可用"""
        return self._enabled and self._client is not None

    async def upload_bytes(
        self,
        data: bytes | bytearray | memoryview,
        file_name: str,
        content_type: str,
        prefix: str = "documents",
    ) -> str:
        """上传字节，返回可访问 URL：{public_base}/{bucket}/{key}

        key 结构：{prefix}/{YYYY}/{MM}/{DD}/{安全文件名}-{uuid}{扩展名}
        """
        if not self.is_enabled() or self._client is None:
            raise HTTPException(
                status_code=503,
                detail="RustFS 未启用或未配置，无法上传文件",
            )

        body = bytes(data)
        await self._ensure_bucket()

        clean_prefix = prefix.strip("/")
        ext = ext_from_name(file_name) or guess_ext(content_type)
        safe_base = sanitize_base_name(file_name)
        key = (
            f"{clean_prefix}/{format_date_path()}/"
            f"{safe_base}-{uuid.uuid4().hex}{ext}"
        )

        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            ContentLength=len(body),
        )

        url = f"{self._public_base}/{self._bucket}/{key}"
        logger.info(
            "RustFS 上传成功: key=%s, size=%d, url=%s", key, len(body), url
        )
        return url

    async def _ensure_bucket(self) -> None:
        """确保 bucket 存在（不存在则创建；并发创建时容忍已存在）"""
        if self._client is None:
            return
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self._bucket)
        except ClientError:
            # bucket 不存在则创建
            try:
                await asyncio.to_thread(
                    self._client.create_bucket, Bucket=self._bucket
                )
                logger.info("RustFS bucket 已创建: %s", self._bucket)
            except ClientError as err:
                # 并发创建时可能已存在
                code = err.response.get("Error", {}).get("Code", "")
                if code not in (
                    "BucketAlreadyOwnedByYou",
                    "BucketAlreadyExists",
                ) and "already exists" not in str(err):
                    raise


def format_date_path() -> str:
    """对象 key 的日期目录：YYYY/MM/DD"""
    now = datetime.now()
    return f"{now.year}/{now.month:02d}/{now.day:02d}"


def sanitize_base_name(file_name: str) -> str:
    """去扩展名并清洗为安全文件名基座（保留中文，最长 64 字符）"""
    base = re.sub(r"\.[^.]+$", "", file_name) or "file"
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", base)
    return cleaned[:64]


def ext_from_name(file_name: str) -> str:
    """从文件名取扩展名（含点），无扩展名返回空串"""
    match = re.search(r"(\.[^.]+)$", file_name)
    return match.group(1) if match else ""


def guess_ext(content_type: str) -> str:
    """按 MIME 猜测扩展名（无扩展名时的兜底）"""
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
    }.get(content_type, "")


# 全局单例（对应 NestJS @Injectable + 模块 provider）
rustfs_service = RustfsService()


def get_rustfs_service() -> RustfsService:
    """FastAPI 依赖：RustFS 存储服务"""
    return rustfs_service

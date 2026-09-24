"""
文件解析服务

按扩展名分发到各 parser：
- pdf：可选注入图片上传回调（RustFS `pdf-images/` 前缀；未启用则仅输出文本/表格）
- xlsx：openpyxl 优先，失败降级 OOXML 文本提取
- pptx / docx / txt / md：直接调用对应 parser
- jpg / png / webp / bmp / gif：视觉模型 OCR → Markdown（需配视觉模型 Key，否则 400）

解析结果为空或格式不支持时抛 400（HTTPException 对应 BadRequestException）。
"""
import io
import logging
import re
import time
import zipfile
from collections.abc import Awaitable, Callable
from html import unescape

from fastapi import HTTPException

from app.utils.markdown import get_extension
from app.utils.parsers.docx import parse_docx
from app.utils.parsers.image import IMAGE_EXTENSIONS, is_vision_configured, parse_image
from app.utils.parsers.pdf import parse_pdf
from app.utils.parsers.pptx import parse_pptx
from app.utils.parsers.text import parse_plain_text
from app.utils.parsers.xlsx import parse_xlsx

logger = logging.getLogger(__name__)

# 支持解析的文件扩展名（图片走视觉模型 OCR，见 parsers/image.py）
SUPPORTED_EXTENSIONS = ["pdf", "docx", "xlsx", "pptx", "txt", "md", *IMAGE_EXTENSIONS]


class FileParserService:
    """文件 → Markdown 解析服务"""

    def is_supported(self, extension: str) -> bool:
        """是否支持（大小写不敏感）"""
        return extension.lower() in SUPPORTED_EXTENSIONS

    def supported_list(self) -> str:
        """逗号分隔的支持格式列表，用于错误提示"""
        return ", ".join(SUPPORTED_EXTENSIONS)

    async def parse(
        self,
        filename: str,
        buffer: bytes,
        uploader: Callable[[bytes, str, str], Awaitable[str]] | None = None,
    ) -> str:
        """将上传文件解析为 Markdown 字符串

        uploader 仅 PDF 图片提取时使用（由调用方注入，如 RustFS）。
        """
        extension = get_extension(filename)
        if not self.is_supported(extension):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"不支持的文件格式: {extension or '(无扩展名)'}，"
                    f"支持的格式: {self.supported_list()}"
                ),
            )
        if not buffer:
            raise HTTPException(status_code=400, detail="文件内容为空，无法解析")

        start = time.perf_counter()
        if extension == "docx":
            result = parse_docx(buffer)
        elif extension == "pdf":
            result = await parse_pdf(buffer, upload_image=uploader)
        elif extension == "pptx":
            result = parse_pptx(buffer)
        elif extension == "xlsx":
            result = self._parse_xlsx_with_fallback(buffer)
        elif extension in ("txt", "md"):
            result = parse_plain_text(buffer)
        elif extension in IMAGE_EXTENSIONS:
            # 图片：视觉模型 OCR → Markdown；未配置视觉模型则明确 400（不是 500）
            if not is_vision_configured():
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "图片解析需要视觉模型（配置 DASHSCOPE_API_KEY 或 VISION_API_KEY / "
                        "VISION_MODEL）；也可直接上传 PDF/Word 等文本类文档"
                    ),
                )
            result = await parse_image(buffer, extension)
        else:
            # 理论不可达（is_supported 已校验）
            raise HTTPException(status_code=400, detail=f"不支持的文件格式: {extension}")

        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "文件解析完成: name=%s, format=%s, chars=%d, elapsed=%.0fms",
            filename,
            extension,
            len(result),
            elapsed_ms,
        )

        if not result.strip():
            raise HTTPException(
                status_code=400,
                detail="文件解析结果为空，请确认文件包含可提取的文本内容",
            )
        return result

    def _parse_xlsx_with_fallback(self, buffer: bytes) -> str:
        """XLSX：openpyxl 优先（结构化表格 Markdown）；失败降级 OOXML 文本提取"""
        try:
            start = time.perf_counter()
            result = parse_xlsx(buffer)
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "XLSX(openpyxl) 解析成功: chars=%d, elapsed=%.0fms",
                len(result),
                elapsed_ms,
            )
            return result
        except Exception as err:
            logger.warning("XLSX(openpyxl) 解析失败，降级 OOXML 文本提取: %s", err)
            return extract_xlsx_text(buffer)


def extract_xlsx_text(buffer: bytes) -> str:
    """XLSX 降级路径：直接从 OOXML 提取文本（对应 officeparser 的通用提取）

    只读共享字符串表与工作表 XML 中的 <t> 文本，实体反转义后按行拼接。
    """
    texts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(buffer)) as zf:
        for name in zf.namelist():
            if name == "xl/sharedStrings.xml" or re.match(
                r"xl/worksheets/sheet\d+\.xml$", name
            ):
                content = zf.read(name).decode("utf-8", errors="replace")
                for m in re.finditer(r"<t[^>]*>(.*?)</t>", content, re.S):
                    texts.append(unescape(m.group(1)))
    return "\n".join(t for t in texts if t.strip())


# 全局单例
file_parser_service = FileParserService()

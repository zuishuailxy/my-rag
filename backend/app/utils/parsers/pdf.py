"""
PDF 解析为 Markdown（对应 TS utils/parsers/pdf.ts，底层 pdf-parse → PyMuPDF）

整体流程：
1. 按页提取文本（page.get_text）；
2. 若提供 upload_image，则按页提取图片 → 过滤小图 → 上传 → 记下 URL；
3. 按页拼装：先文本，再该页图片的 `![](url)`；
4. 尝试提取表格（page.find_tables）；若正文里尚无 Markdown 表格，则追加到文末「检测到的表格」章节；
5. 无论成败，finally 关闭文档释放底层资源。

图片提取失败不会中断解析，会降级为仅文本；单张图片上传失败则跳过该张。
"""
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pymupdf

from app.utils.markdown import clean_markdown, to_markdown_table

logger = logging.getLogger(__name__)

# 图片上传回调：bytes → URL（对应 TS ImageUploader，由调用方注入，如 RustFS）
ImageUploader = Callable[[bytes, str, str], Awaitable[str]]


async def parse_pdf(
    buffer: bytes,
    upload_image: ImageUploader | None = None,
    image_threshold: int = 50,
) -> str:
    """将 PDF 解析为 Markdown"""
    doc = pymupdf.open(stream=buffer, filetype="pdf")
    threshold = image_threshold

    try:
        # ---------- 1. 文本：按页取出 ----------
        page_texts: list[tuple[int, str]] = []  # (页码, 文本)
        for page in doc.pages():
            page_texts.append(_page_number_and_text(page))

        # pageNumber → 该页已上传图片的 URL 列表（保持提取顺序）
        page_image_urls: dict[int, list[str]] = {}

        # ---------- 2. 图片（可选）：提取 → 过滤小图 → 上传 ----------
        if upload_image is not None:
            try:
                total_uploaded = 0
                for page in doc.pages():
                    page_number = page.number if page.number is not None else 0
                    urls: list[str] = []
                    img_idx = 0
                    seen_xrefs: set[int] = set()
                    for info in page.get_images(full=True):
                        xref = info[0]
                        # 同一图片可能被多次引用，按 xref 去重
                        if xref in seen_xrefs:
                            continue
                        seen_xrefs.add(xref)

                        try:
                            extracted = doc.extract_image(xref)
                        except Exception:
                            continue
                        width, height = extracted["width"], extracted["height"]
                        image_bytes: bytes = extracted["image"]
                        # 二次过滤：跳过宽或高小于阈值的图片（多为装饰图标/噪点）
                        if (
                            (width > 0 and width < threshold)
                            or (height > 0 and height < threshold)
                        ):
                            continue
                        if not image_bytes:
                            continue

                        # 根据文件头嗅探 MIME，决定扩展名（webp 也按 png 扩展名上传，contentType 仍为真实类型）
                        content_type = sniff_image_content_type(image_bytes)
                        ext = "jpg" if content_type == "image/jpeg" else "png"
                        file_name = f"pdf_img_p{page_number}_{img_idx}.{ext}"
                        img_idx += 1
                        try:
                            url = await upload_image(image_bytes, file_name, content_type)
                            urls.append(url)
                            total_uploaded += 1
                        except Exception as err:
                            # 单张失败不影响同页其他图片与整份文档
                            logger.warning(
                                "PDF 图片上传失败: page=%s, name=%s, err=%s",
                                page_number,
                                file_name,
                                err,
                            )
                    if urls:
                        page_image_urls[page_number] = urls

                if total_uploaded > 0:
                    logger.info("PDF 图片提取完成: %s 张", total_uploaded)
            except Exception as err:
                # 整批图片提取失败：降级为纯文本，不抛错
                logger.warning("PDF 图片提取失败，继续仅文本: %s", err)

        # ---------- 3. 按页拼装 Markdown ----------
        parts: list[str] = []
        if page_texts:
            for page_number, text in page_texts:
                text = text.strip()
                if text:
                    parts.append(text)
                urls = page_image_urls.get(page_number, [])
                for url in urls:
                    parts.append(f"![]({url})")
                # 该页有内容时末尾加空串，join 后形成段落间距
                if text or urls:
                    parts.append("")
        else:
            # 无分页信息时（空文档等）：退回全文，图片统一追加在后
            for urls in page_image_urls.values():
                for url in urls:
                    parts.append(f"![]({url})")

        markdown = clean_markdown("\n\n".join(parts))

        # ---------- 4. 表格（尽力而为）----------
        # 仅当正文中尚未出现 Markdown 表头分隔行（| ---）时才追加，避免与正文重复
        if "| ---" not in markdown:
            try:
                table_parts: list[str] = []
                table_idx = 0
                for page in doc.pages():
                    for table in page.find_tables().tables:
                        rows = normalize_pdf_table(table.extract())
                        if rows:
                            table_idx += 1
                            table_parts.append(
                                f"### 表格 {table_idx}\n\n{to_markdown_table(rows)}"
                            )
                if table_parts:
                    markdown = clean_markdown(
                        f"{markdown}\n\n## 检测到的表格\n\n" + "\n".join(table_parts)
                    )
            except Exception:
                # 表格提取失败不影响主结果
                logger.warning("PDF 表格提取失败，已跳过", exc_info=True)

        return markdown
    finally:
        doc.close()


def _page_number_and_text(page: pymupdf.Page) -> tuple[int, str]:
    """收窄 PyMuPDF 的桩类型：page.number 恒为 int，get_text() 无参调用恒返回 str

    官方类型签名较宽（int | None / str | list | dict），这里做运行时兜底并收窄，
    避免下游解包类型报错。
    """
    number = page.number if page.number is not None else 0
    raw = page.get_text()
    text = raw if isinstance(raw, str) else ""
    return number, text


def sniff_image_content_type(data: bytes) -> str:
    """通过魔数（文件头字节）判断图片 MIME；识别失败默认 image/png"""
    # JPEG: FF D8 FF
    if len(data) >= 3 and data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF:
        return "image/jpeg"
    # PNG: 89 50 4E 47（即 \x89PNG）
    if (
        len(data) >= 8
        and data[0] == 0x89
        and data[1] == 0x50
        and data[2] == 0x4E
        and data[3] == 0x47
    ):
        return "image/png"
    # WebP: 以 RIFF 开头（完整格式还含 WEBP，这里只做粗判）
    if (
        len(data) >= 4
        and data[0] == 0x52
        and data[1] == 0x49
        and data[2] == 0x46
        and data[3] == 0x46
    ):
        return "image/webp"
    return "image/png"


def normalize_pdf_table(raw: Any) -> list[list[str]]:
    """将表格提取结果统一成二维字符串数组（对应 TS normalizePdfTable）

    兼容形态：
    - list[list[str]]：已是行列结构；
    - 嵌套数组：逐项递归再扁平合并；
    - {"rows": ...} / {"data": ...}：取字段后继续递归。
    无法识别时返回空数组。
    """
    if raw is None:
        return []

    if isinstance(raw, list):
        if len(raw) == 0:
            return []
        # 首元素仍是数组 → 视为「行 → 单元格」
        if isinstance(raw[0], list):
            return [[str(cell or "").strip() for cell in row] for row in raw]
        # 否则当作「多个表/多块」拼接
        merged: list[list[str]] = []
        for item in raw:
            merged.extend(normalize_pdf_table(item))
        return merged

    if isinstance(raw, dict):
        if raw.get("rows"):
            return normalize_pdf_table(raw["rows"])
        if raw.get("data"):
            return normalize_pdf_table(raw["data"])

    return []

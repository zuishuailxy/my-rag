"""
PPTX 解析为 Markdown

优先走自研 ZIP/XML 路径（按幻灯片输出 `## 幻灯片 N` + 标题/正文/表格）；
解压或提取失败时降级到 python-pptx。

注意：（异步）+ officeparser（异步），Python 侧 zipfile / python-pptx
均为同步，因此 parse_pptx 直接返回 str。
"""
import io
import logging
import re
import zipfile
from typing import Pattern

from app.utils.markdown import clean_markdown, to_markdown_table

logger = logging.getLogger(__name__)

_SLIDE_RE = re.compile(r"^ppt/slides/slide\d+\.xml$", re.I)
_SLIDE_NUM_RE = re.compile(r"slide(\d+)\.xml$", re.I)


def parse_pptx(buffer: bytes) -> str:
    """将 PPTX 解析为 Markdown（ZIP 路径失败时降级 python-pptx）"""
    try:
        return _parse_pptx_with_zip(buffer)
    except Exception:
        logger.warning("PPTX ZIP 路径失败，降级 python-pptx 路径", exc_info=True)
        return _parse_pptx_with_python_pptx(buffer)


def _parse_pptx_with_zip(buffer: bytes) -> str:
    """自研路径：解压 PPTX（OOXML），按 slideN.xml 顺序提取。"""
    with zipfile.ZipFile(io.BytesIO(buffer)) as zf:
        slide_paths = sorted(
            (p for p in zf.namelist() if _SLIDE_RE.match(p)),
            key=slide_number,
        )
        if not slide_paths:
            raise ValueError("未找到 PPTX slide")

        parts: list[str] = []
        for i, path in enumerate(slide_paths):
            xml = zf.read(path).decode("utf-8", errors="replace")
            parts.append(f"## 幻灯片 {i + 1}\n")

            # 表格（先于正文，正文里剔除表格区域避免重复）
            for table in extract_tables(xml):
                parts.append(to_markdown_table(table))
            body_xml = re.sub(r"<a:tbl.*?</a:tbl>", "", xml, flags=re.S)

            # 标题占位符（ctrTitle / title）→ ### 标题
            title_texts = extract_placeholder_texts(
                body_xml, re.compile(r"ctrTitle|title", re.I)
            )
            used = {t.strip() for t in title_texts if t.strip()}
            for t in title_texts:
                text = t.strip()
                if text:
                    parts.append(f"### {text}\n")

            # 正文段落（已在标题中出现过的文本跳过，避免重复）
            body_lines = [
                t.strip()
                for t in extract_paragraph_texts(body_xml)
                if t.strip() and t.strip() not in used
            ]
            if body_lines:
                parts.append("\n".join(body_lines) + "\n\n")

    result = clean_markdown("\n".join(parts))
    if not result:
        raise ValueError("PPTX 提取结果为空")
    return result


def _parse_pptx_with_python_pptx(buffer: bytes) -> str:
    """降级路径：python-pptx 按幻灯片提取文本与表格"""
    from pptx import Presentation

    prs = Presentation(io.BytesIO(buffer))
    parts: list[str] = []
    for i, slide in enumerate(prs.slides, start=1):
        parts.append(f"## 幻灯片 {i}\n")
        for shape in slide.shapes:
            if shape.has_table:
                table = shape.table
                rows = [
                    [cell.text.strip() for cell in row.cells] for row in table.rows
                ]
                parts.append(to_markdown_table(rows))
            elif shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if not text:
                    continue
                parts.append(f"### {text}" if _is_title_shape(shape) else text)
        parts.append("")

    result = clean_markdown("\n".join(parts))
    if not result:
        raise ValueError("PPTX 提取结果为空")
    return result


def _is_title_shape(shape) -> bool:
    """是否标题占位符（title / ctrTitle / subtitle）"""
    try:
        from pptx.enum.shapes import PP_PLACEHOLDER

        return (
            shape.is_placeholder
            and shape.placeholder_format.type
            in (
                PP_PLACEHOLDER.TITLE,
                PP_PLACEHOLDER.CENTER_TITLE,
                PP_PLACEHOLDER.SUBTITLE,
            )
        )
    except Exception:
        return False


def slide_number(path: str) -> int:
    """从路径 `ppt/slides/slide12.xml` 解析页码，供排序"""
    m = _SLIDE_NUM_RE.search(path)
    return int(m.group(1)) if m else 0


def extract_paragraph_texts(xml: str) -> list[str]:
    """按段落提取文本：同一 `<a:p>` 内的多个 `<a:t>` run 合并，`<a:br/>` 转换行"""
    paragraphs: list[str] = []
    for block in re.findall(r"<a:p[\s>].*?</a:p>", xml, re.S):
        text = extract_runs_text(block)
        if text.strip():
            paragraphs.append(text)
    return paragraphs


def extract_placeholder_texts(xml: str, type_pattern: Pattern[str]) -> list[str]:
    """从指定占位符类型的 shape 中提取文本（按段落合并）"""
    texts: list[str] = []
    # 按 shape 切开，再看 <p:ph type="...">
    for shape in re.split(r"<p:sp[\s>]", xml)[1:]:
        ph = re.search(r'<p:ph[^>]*\btype="([^"]+)"', shape, re.I)
        if not ph or not type_pattern.search(ph.group(1)):
            continue
        joined = "\n".join(extract_paragraph_texts(shape)).strip()
        if joined:
            texts.append(joined)
    return texts


def extract_tables(xml: str) -> list[list[list[str]]]:
    """从 slide XML 中提取所有 `<a:tbl>`，归一为（多表 → 行 → 单元格）"""
    tables: list[list[list[str]]] = []
    for block in re.findall(r"<a:tbl.*?</a:tbl>", xml, re.S):
        rows: list[list[str]] = []
        for tr in re.findall(r"<a:tr.*?</a:tr>", block, re.S):
            cells: list[str] = []
            for tc in re.findall(r"<a:tc.*?</a:tc>", tr, re.S):
                # 单元格内多段落用空格拼成一格
                cells.append(" ".join(extract_paragraph_texts(tc)).strip())
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def extract_runs_text(xml: str) -> str:
    """收集块内文本 run，并把显式换行 `<a:br/>` 转为 `\n`"""
    result: list[str] = []
    for m in re.finditer(r"<a:br\s*/>|<a:t(?:\s[^>]*)?>(.*?)</a:t>", xml, re.S):
        if m.group(0).startswith("<a:br"):
            result.append("\n")
        else:
            result.append(decode_xml(m.group(1)))
    return "".join(result)


def decode_xml(text: str) -> str:
    """OOXML 文本实体反转义"""
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )

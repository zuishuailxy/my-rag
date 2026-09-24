"""
DOCX 解析为 Markdown

整体流程：
1. mammoth（官方 Python 移植版）把 DOCX 转为 HTML（保留标题 / 列表 / 表格等结构）；
2. markdownify（turndown 的 Python 移植版）把 HTML 转为 Markdown（ATX 标题 / 围栏代码块 / GFM 表格）；
3. clean_markdown 做换行与空白规范化。

styleMap 同时覆盖英文与中文 Word 内置样式名，避免中文版 Word 标题丢失层级。
"""
import io
import re

import mammoth
from markdownify import markdownify as html_to_markdown

from app.utils.markdown import clean_markdown

# 英文与中文 Word 内置样式名 → 标题层级
_STYLE_MAP = [
    # 英文样式
    "p[style-name='Title'] => h1:fresh",
    "p[style-name='Subtitle'] => h2:fresh",
    "p[style-name='Heading 1'] => h1:fresh",
    "p[style-name='Heading 2'] => h2:fresh",
    "p[style-name='Heading 3'] => h3:fresh",
    "p[style-name='Heading 4'] => h4:fresh",
    # 中文 Word 内置「标题 N」
    "p[style-name='标题 1'] => h1:fresh",
    "p[style-name='标题 2'] => h2:fresh",
    "p[style-name='标题 3'] => h3:fresh",
    "p[style-name='标题 4'] => h4:fresh",
]


def parse_docx(buffer: bytes) -> str:
    """将 DOCX 解析为 Markdown"""
    # mammoth 接收文件对象，字节包一层 BytesIO；Python 版 style_map 为换行分隔字符串
    result = mammoth.convert_to_html(
        io.BytesIO(buffer), style_map="\n".join(_STYLE_MAP)
    )
    html = result.value

    # markdownify：ATX 标题（#）、'-' 无序列表、GFM 表格
    md = html_to_markdown(
        _prepare_tables(html),
        heading_style="ATX",
        bullets="-",
    )
    return clean_markdown(md)


def _prepare_tables(html: str) -> str:
    """修复 markdownify 对 Word 表格的处理

    mammoth 产出的单元格是 <td><p>…</p></td>，markdownify 会弄丢首行内容：
    - 去掉单元格内的 <p> 包裹；
    - 首行 td → th（GFM 表头语义，与 markdownify 将首行视为表头的行为一致）。
    """
    def fix_table(tbl: str) -> str:
        tbl = re.sub(
            r"<(td|th)[^>]*>\s*<p[^>]*>(.*?)</p>\s*</\1>",
            r"<\1>\2</\1>",
            tbl,
            flags=re.S,
        )
        first_tr_end = tbl.find("</tr>")
        if first_tr_end == -1:
            return tbl
        head, rest = tbl[:first_tr_end], tbl[first_tr_end:]
        head = re.sub(r"<td([^>]*)>", r"<th\1>", head).replace("</td>", "</th>")
        return head + rest

    return re.sub(
        r"<table>.*?</table>", lambda m: fix_table(m.group(0)), html, flags=re.S
    )

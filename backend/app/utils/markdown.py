"""
Markdown 工具
"""
import re

# cleanMarkdown 的 \n{4,} → \n\n\n 预编译
_RE_CRLF = re.compile(r"\r\n")
_RE_NEWLINES4 = re.compile(r"\n{4,}")


def clean_markdown(text: str) -> str:
    """规范化 Markdown：统一换行，压缩连续空行，去首尾空白"""
    if not text:
        return ""
    return _RE_NEWLINES4.sub("\n\n\n", _RE_CRLF.sub("\n", text)).strip()


def escape_table_cell(value: str) -> str:
    """转义表格单元格中的 `|`，并把换行压成空格，避免破坏表结构"""
    return re.sub(r"\|", r"\\|", re.sub(r"\r?\n", " ", value)).strip()


def to_markdown_table(rows: list[list[str]]) -> str:
    """二维数组 → Markdown 表格（首行作表头，第二行输出 `| --- |` 分隔）

    列数按所有行的最大列对齐，缺省单元格视为空串。
    """
    if not rows:
        return ""

    max_cols = max(len(row) for row in rows)
    if max_cols == 0:
        return ""

    lines: list[str] = []
    for r, row in enumerate(rows):
        cells = [
            escape_table_cell(row[c] if c < len(row) else "") for c in range(max_cols)
        ]
        lines.append(f"| {' | '.join(cells)} |")
        if r == 0:
            lines.append(f"| {' | '.join(['---'] * max_cols)} |")
    return "\n".join(lines) + "\n\n"


def get_extension(filename: str | None = None) -> str:
    """取文件扩展名（小写，不含点）；无扩展名时返回空串"""
    if not filename or "." not in filename:
        return ""
    return filename[filename.rindex(".") + 1 :].lower()


def decode_upload_filename(filename: str | None = None) -> str:
    """修复 multipart 文件名的 mojibake（Latin-1 → UTF-8 还原）

    Multer/busboy 常把 multipart 文件名的 UTF-8 字节按 Latin-1 解码，
    导致中文变成「ç³è®º…」。按 Latin-1 取回原始字节再按 UTF-8 还原。
    解码失败（出现替换字符或编码越界）时保留原值。
    """
    if not filename:
        return ""
    try:
        decoded = filename.encode("latin-1").decode("utf-8")
        # 解码失败会出现替换字符，此时保留原值
        if "\ufffd" in decoded:
            return filename
        return decoded
    except (UnicodeEncodeError, UnicodeDecodeError):
        return filename


def title_from_filename(filename: str | None = None) -> str:
    """去掉扩展名作为文档标题；空文件名时返回「未命名文档」"""
    if not filename:
        return "未命名文档"
    idx = filename.rfind(".")
    return filename[:idx] if idx > 0 else filename

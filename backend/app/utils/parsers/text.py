"""
TXT / MD 解析为文本

不做结构转换：按 UTF-8 读出，后续由调用方直接当作 Markdown/纯文本使用。
无效字节序列用替换字符（U+FFFD）替代，与 JS Buffer.toString('utf8') 行为一致。
"""


def parse_plain_text(buffer: bytes) -> str:
    """将 TXT / MD 字节解析为 UTF-8 文本"""
    return buffer.decode("utf-8", errors="replace")

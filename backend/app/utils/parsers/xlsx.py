"""
XLSX 解析为 Markdown（对应 TS utils/parsers 的 parseXlsx，exceljs → openpyxl）

整体流程：
1. openpyxl 加载工作簿（data_only=True：公式取缓存结果，对应 ExcelJS 的 result）；
2. 每个 Sheet → `## SheetName` + Markdown 表格（首行作表头）；
3. 单元格值经 cell_to_string 统一成字符串；
4. 列数按整表最大列对齐后交给 to_markdown_table。

空 Sheet 仍保留标题与空行，避免丢 sheet 名信息。
注意：TS 中 exceljs 失败时由调用方降级 officeparser；Python 侧无此降级（openpyxl
已足够健壮），如需要可自行加 try/except。
"""
import datetime as _dt
import io
from typing import Any

from app.utils.markdown import clean_markdown, to_markdown_table


def parse_xlsx(buffer: bytes) -> str:
    """将 XLSX 解析为 Markdown"""
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(buffer), data_only=True)
    parts: list[str] = []
    try:
        for ws in workbook.worksheets:
            parts.append(f"## {ws.title}\n")

            rows: list[list[str]] = []
            max_cols = 0
            for row in ws.iter_rows():
                # 对齐 TS row.actualCellCount：最后一个非空单元格的列号
                last = 0
                values: list[str] = []
                for cell in row:
                    s = cell_to_string(cell.value)
                    values.append(s)
                    if cell.value is not None:
                        last = len(values)
                if last == 0:
                    continue  # 整行为空，跳过（对应 eachRow includeEmpty: false）
                max_cols = max(max_cols, last)
                rows.append(values[:last])

            if max_cols == 0 or not rows:
                parts.append("\n")
                continue

            # 各行列数可能不等，右侧补空串再出表
            normalized = [r + [""] * (max_cols - len(r)) for r in rows]
            parts.append(to_markdown_table(normalized))
    finally:
        workbook.close()

    return clean_markdown("\n".join(parts))


def cell_to_string(value: Any) -> str:
    """将单元格值转为展示用字符串（对应 TS cellToString）

    openpyxl 已把富文本 / 超链接 / 公式结果统一为原生值（data_only=True），
    因此只需处理基础类型；无法识别的对象退回 str(value)。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # JS String(整数浮点) 不带小数：1.0 → "1"
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, _dt.datetime):
        # 对齐 TS Date.toISOString 的毫秒精度（无时区后缀）
        return value.isoformat(timespec="milliseconds")
    if isinstance(value, _dt.time):
        return value.isoformat()
    return str(value)

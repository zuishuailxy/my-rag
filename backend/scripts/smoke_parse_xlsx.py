"""
XLSX 解析冒烟测试（自包含：用 openpyxl 构造测试工作簿）

验证点：
- 每个 Sheet → `## SheetName` + GFM 表格（首行表头）；
- 单元格类型：整数 / 整数浮点(100.0→"100") / 小数 / 布尔 / 日期；
- 列对齐（稀疏行补空）；
- 空 Sheet 保留标题与空行；
- cell_to_string 单元测试。

用法：
    cd backend && PYTHONPATH=. .venv/bin/python scripts/smoke_parse_xlsx.py
"""
import datetime
import io

from openpyxl import Workbook

from app.utils.parsers.xlsx import cell_to_string, parse_xlsx


def build_test_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "数据"
    ws.append(["指标", "数值", "备注"])
    ws.append(["A", 100, True])
    ws.append(["B", 1.5, "小数"])
    ws.append(["C", 100.0, None])          # 整数浮点 → "100"；稀疏行：末列空
    ws.append(["D", datetime.datetime(2026, 1, 2, 3, 4, 5), "日期"])
    wb.create_sheet("空表")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def main() -> None:
    # 1) cell_to_string 单元测试
    assert cell_to_string(None) == ""
    assert cell_to_string("abc") == "abc"
    assert cell_to_string(True) == "True"
    assert cell_to_string(100) == "100"
    assert cell_to_string(1.5) == "1.5"
    assert cell_to_string(100.0) == "100"                      # 整数浮点去 .0
    assert cell_to_string(datetime.datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05.000"
    assert cell_to_string(object()) == str(object())
    print("1) cell_to_string OK")

    # 2) 整体解析
    md = parse_xlsx(build_test_xlsx())
    print("--- 解析结果 ---")
    print(md)
    print("----------------")
    assert md.startswith("## 数据"), "Sheet 标题缺失"
    assert "## 空表" in md, "空 Sheet 标题缺失"
    assert "| 指标 | 数值 | 备注 |" in md, "表头丢失"
    assert "| --- | --- | --- |" in md, "表头分隔行丢失"
    assert "| A | 100 | True |" in md, "整数/布尔单元格异常"
    assert "| B | 1.5 | 小数 |" in md, "小数单元格异常"
    assert "| C | 100 |  |" in md, "整数浮点或稀疏行对齐异常"
    assert "| D | 2026-01-02T03:04:05.000 | 日期 |" in md, "日期单元格异常"
    # 空表后应有空行分隔（空 sheet 保留标题与空行）
    assert "## 空表" in md and md.rstrip().endswith("## 空表")

    print("\nXLSX 解析冒烟测试全部通过 ✅")


if __name__ == "__main__":
    main()

"""
DOCX 解析冒烟测试（自包含：用 zipfile 构造最小 DOCX，无需 python-docx）

验证点：
- 英文 Heading 1 / 中文「标题 1」样式 → # 标题
- 普通段落保留
- 表格 → GFM 管道表格
- 列表 → '-' 无序列表
- clean_markdown 规范化

用法：
    cd backend && .venv/bin/python scripts/smoke_parse_docx.py
"""
import io
import zipfile

from app.utils.parsers.docx import parse_docx

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""

STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="Heading 1"/><w:basedOn w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="ChineseHeading1"><w:name w:val="标题 1"/><w:basedOn w:val="Normal"/></w:style>
</w:styles>
"""

DOCUMENT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>English Heading</w:t></w:r></w:p>
    <w:p><w:pPr><w:pStyle w:val="ChineseHeading1"/></w:pPr><w:r><w:t>中文标题一</w:t></w:r></w:p>
    <w:p><w:r><w:t>这是一个普通段落，包含</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>加粗</w:t></w:r><w:r><w:t>内容。</w:t></w:r></w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>列A</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>列B</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:p><w:r><w:t>1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>2</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:p><w:r><w:t>- 列表项一</w:t></w:r></w:p>
    <w:p><w:r><w:t>- 列表项二</w:t></w:r></w:p>
  </w:body>
</w:document>
"""


def build_test_docx() -> bytes:
    """构造最小可解析 DOCX"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/_rels/document.xml.rels", DOC_RELS)
        zf.writestr("word/styles.xml", STYLES)
        zf.writestr("word/document.xml", DOCUMENT)
    return buf.getvalue()


def main() -> None:
    md = parse_docx(build_test_docx())
    print("--- 解析结果 ---")
    print(md)
    print("----------------")

    # 断言
    assert md.startswith("# English Heading"), "英文 Heading 1 未转 # 标题"
    assert "# 中文标题一" in md, "中文「标题 1」未转 # 标题"
    assert "普通段落" in md and "**加粗**" in md, "段落/加粗丢失"
    assert "| 列A | 列B |" in md and "| --- | --- |" in md, "表格未转 GFM"
    assert "- 列表项一" in md, "列表未保留"
    assert "\n\n" in md and not md.endswith("\n\n"), "空行规范化异常"
    print("\nDOCX 解析冒烟测试全部通过 ✅")


if __name__ == "__main__":
    main()

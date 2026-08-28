"""
PDF 解析冒烟测试（自包含：用 PyMuPDF 构造测试 PDF，假上传器验证图片提取/过滤/表格）

验证点：
- 分页文本保留；
- 图片：大于阈值的上传、小于阈值的跳过、按页插入 ![](url)；
- 表格：正文无表时追加「检测到的表格」章节；
- 不传 upload_image 时降级为纯文本。

用法：
    cd backend && PYTHONPATH=. .venv/bin/python scripts/smoke_parse_pdf.py
"""
import asyncio

import pymupdf

from app.utils.parsers.pdf import parse_pdf, sniff_image_content_type

THRESHOLD = 50


def build_test_pdf() -> bytes:
    """构造测试 PDF：第 1 页文本；第 2 页小图(30px) + 大图(120px) + 表格网格"""
    doc = pymupdf.open()

    # 第 1 页：标题 + 段落
    page1 = doc.new_page()
    page1.insert_text((72, 72), "PDF 解析测试", fontsize=24, fontname="china-s")
    page1.insert_text(
        (72, 120), "这是第一页的正文内容，用于验证文本提取。", fontsize=12, fontname="china-s"
    )

    # 第 2 页
    page2 = doc.new_page()
    # 小图（30x30，低于阈值 50 → 应被跳过）
    small = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 30, 30), False)
    small.clear_with(0)
    page2.insert_image(pymupdf.Rect(50, 50, 80, 80), pixmap=small)
    # 大图（120x120 → 应被上传）
    big = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 120), False)
    big.clear_with(0)
    page2.insert_image(pymupdf.Rect(120, 50, 240, 170), pixmap=big)
    # 表格网格（2 行 2 列）
    for x in (50, 150, 250):
        page2.draw_line(pymupdf.Point(x, 400), pymupdf.Point(x, 500), width=1)
    for y in (400, 450, 500):
        page2.draw_line(pymupdf.Point(50, y), pymupdf.Point(250, y), width=1)
    page2.insert_text((60, 425), "表头A", fontname="china-s")
    page2.insert_text((160, 425), "表头B", fontname="china-s")
    page2.insert_text((60, 475), "1")
    page2.insert_text((160, 475), "2")

    data = doc.tobytes()
    doc.close()
    return data


async def main() -> None:
    pdf = build_test_pdf()

    # 假上传器：记录调用并返回 URL
    uploaded: list[tuple[bytes, str, str]] = []

    async def fake_upload(data: bytes, name: str, content_type: str) -> str:
        uploaded.append((data, name, content_type))
        return f"https://fake.example/{name}"

    # 1) 带图片上传的完整解析
    md = await parse_pdf(pdf, upload_image=fake_upload, image_threshold=THRESHOLD)
    print("--- 完整解析结果 ---")
    print(md)
    print("--------------------")
    assert "PDF 解析测试" in md, "第 1 页标题丢失"
    assert "第一页的正文内容" in md, "第 1 页正文丢失"

    # 2) 图片：只上传了大图（1 张），小图被阈值过滤
    print("上传记录:", [(n, t) for _, n, t in uploaded])
    assert len(uploaded) == 1, f"应只上传 1 张大图，实际 {len(uploaded)} 张"
    name, content_type = uploaded[0][1], uploaded[0][2]
    assert name.startswith("pdf_img_p1_0."), name
    assert content_type == "image/png", content_type
    assert f"![]({f'https://fake.example/{name}'})" in md, "图片 URL 未写入 Markdown"
    assert "pdf_img_p" in md and "30" not in md.split("pdf_img_p")[0][-3:], "小图不应出现"

    # 3) 表格章节
    assert "## 检测到的表格" in md, "缺少表格章节"
    assert "### 表格 1" in md, "缺少表格标题"
    assert "| 表头A | 表头B |" in md, "表头丢失"
    assert "| --- | --- |" in md, "表头分隔行丢失"

    # 4) 不传 upload_image：纯文本降级
    md_text = await parse_pdf(pdf, image_threshold=THRESHOLD)
    assert "PDF 解析测试" in md_text
    assert "![](" not in md_text, "无上传器时不应出现图片 Markdown"

    # 5) 魔数嗅探
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 20
    webp = b"RIFF" + b"\x00" * 20
    assert sniff_image_content_type(png) == "image/png"
    assert sniff_image_content_type(jpg) == "image/jpeg"
    assert sniff_image_content_type(webp) == "image/webp"
    assert sniff_image_content_type(b"unknown") == "image/png"

    print("\nPDF 解析冒烟测试全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())

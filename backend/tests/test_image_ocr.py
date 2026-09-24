"""图片 OCR（多模态）：解析分发、多模态消息构造、未配置视觉模型时的降级。

通过 monkeypatch 注入假视觉模型，隔离网络。
"""
from typing import Any

import pytest
from fastapi import HTTPException

import app.services.file_parser as fp_mod
from app.services.file_parser import file_parser_service
from app.utils.parsers import image as image_mod


class _FakeResponse:
    def __init__(self, content: Any) -> None:
        self.content = content


class _FakeVisionLLM:
    def __init__(self, content: Any = "识别出的文字") -> None:
        self._content = content
        self.messages: list[Any] | None = None

    async def ainvoke(self, messages: list[Any]) -> _FakeResponse:
        self.messages = messages
        return _FakeResponse(self._content)


def test_image_extensions_supported() -> None:
    for ext in ("png", "jpg", "jpeg", "webp", "bmp", "gif"):
        assert file_parser_service.is_supported(ext), f"{ext} 应被支持"
    assert file_parser_service.is_supported("PNG"), "扩展名大小写不敏感"
    assert "png" in file_parser_service.supported_list()


async def test_parse_image_builds_multimodal_message(monkeypatch) -> None:
    fake = _FakeVisionLLM("图片里的文字")
    monkeypatch.setattr(image_mod, "_build_vision_llm", lambda: fake)

    out = await image_mod.parse_image(b"\x89PNG\r\n", "png")
    assert out == "图片里的文字"
    assert fake.messages is not None
    blocks = fake.messages[-1].content
    assert any(
        isinstance(b, dict) and b.get("type") == "image_url" for b in blocks
    ), "应带 image_url 多模态块"
    url = next(b["image_url"]["url"] for b in blocks if b.get("type") == "image_url")
    assert url.startswith("data:image/png;base64,"), "应编码为 data URL"


async def test_parse_image_content_blocks_list(monkeypatch) -> None:
    # 视觉模型返回块列表时也应取到文本
    fake = _FakeVisionLLM([{"type": "text", "text": "块文本"}])
    monkeypatch.setattr(image_mod, "_build_vision_llm", lambda: fake)
    assert await image_mod.parse_image(b"x", "jpg") == "块文本"


async def test_parse_image_without_key_raises(monkeypatch) -> None:
    monkeypatch.setattr(image_mod, "_build_vision_llm", lambda: None)
    with pytest.raises(RuntimeError):
        await image_mod.parse_image(b"x", "png")


async def test_file_parser_image_requires_vision(monkeypatch) -> None:
    monkeypatch.setattr(fp_mod, "is_vision_configured", lambda: False)
    with pytest.raises(HTTPException) as exc:
        await file_parser_service.parse("a.png", b"\x89PNG")
    assert exc.value.status_code == 400, "未配置视觉模型应是 400，不是 500"


async def test_file_parser_dispatches_image(monkeypatch) -> None:
    monkeypatch.setattr(fp_mod, "is_vision_configured", lambda: True)

    async def fake_parse(buffer: bytes, extension: str) -> str:
        return "# 图片转写"

    monkeypatch.setattr(fp_mod, "parse_image", fake_parse)
    assert await file_parser_service.parse("a.png", b"\x89PNG") == "# 图片转写"

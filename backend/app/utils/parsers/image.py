"""图片 OCR / 图像理解（多模态）

把 JPG / PNG 等图片送 **视觉模型**（Qwen-VL via DashScope OpenAI 兼容口），产出
Markdown 文本（正文 + 图像描述），供后续分块 / 三路索引 —— 这是「多模态文档」的
第一块拼图，让图片类知识也能被检索、问答。

Key / baseURL / model fallback 链：
- Key：VISION_API_KEY → DASHSCOPE_API_KEY → OPENAI_API_KEY
- baseURL：VISION_BASE_URL → DashScope 兼容端点
- model：VISION_MODEL（默认 qwen-vl-max；专用 OCR 可换 qwen-vl-ocr）

未配置 Key 时 `parse_image` 抛 RuntimeError，由 `FileParserService` 决定降级为 400。
"""
import base64
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.core.config import settings
from app.services.llm_budget import usage_callbacks

logger = logging.getLogger("ImageParser")

_DASHSCOPE_COMPATIBLE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 扩展名 → MIME（决定 data URL 前缀，也是「支持哪些图片格式」的唯一事实源）
IMAGE_MIME: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "gif": "image/gif",
}

IMAGE_EXTENSIONS: tuple[str, ...] = tuple(IMAGE_MIME)

_OCR_SYSTEM = (
    "你是文档图像识别助手。把图片中的文字尽量完整、准确地转写为 Markdown："
    "保留标题层级与表格结构；图内没有文字时，用一句中文描述图像内容。"
    "只输出转写/描述结果，不要额外解释。"
)


def is_vision_configured() -> bool:
    """是否配置了视觉模型 Key（决定图片解析走 OCR 还是直接 400）"""
    return bool(_vision_api_key())


def _vision_api_key() -> str | None:
    return (
        settings.VISION_API_KEY
        or settings.DASHSCOPE_API_KEY
        or settings.OPENAI_API_KEY
    )


def _build_vision_llm() -> ChatOpenAI | None:
    """构建视觉模型客户端；未配置 Key 返回 None"""
    api_key = _vision_api_key()
    if not api_key:
        return None
    timeout_ms = settings.VISION_TIMEOUT_MS
    return ChatOpenAI(
        api_key=SecretStr(api_key),
        model=settings.VISION_MODEL,
        base_url=settings.VISION_BASE_URL or _DASHSCOPE_COMPATIBLE,
        temperature=0,
        timeout=timeout_ms / 1000 if timeout_ms > 0 else 60.0,
        max_retries=0,
        # 用量/成本统计：挂一次即覆盖该 LLM 的所有调用点
        callbacks=usage_callbacks(),
        # 走 Chat Completions 兼容层，不要切 OpenAI Responses API
        use_responses_api=False,
    )


def _content_to_text(content: Any) -> str:
    """模型返回可能是 str 或块列表，统一取文本"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content or "")


async def parse_image(buffer: bytes, extension: str) -> str:
    """图片 → Markdown 文本（OCR + 描述）

    未配置 Key、格式不支持、调用失败均抛异常，由调用方决定 400 / 降级。
    """
    llm = _build_vision_llm()
    if llm is None:
        raise RuntimeError(
            "未配置视觉模型 Key（VISION_API_KEY / DASHSCOPE_API_KEY / OPENAI_API_KEY）"
        )
    mime = IMAGE_MIME.get(extension.lower())
    if mime is None:
        raise RuntimeError(f"不支持的图片格式: {extension or '(无扩展名)'}")

    data_url = f"data:{mime};base64,{base64.b64encode(buffer).decode('ascii')}"
    message = HumanMessage(
        content=[
            {"type": "text", "text": "请转写图片中的文字；无文字则描述图像内容。"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
    )
    response = await llm.ainvoke([SystemMessage(_OCR_SYSTEM), message])
    text = _content_to_text(response.content).strip()
    logger.info(
        "图片 OCR 完成：ext=%s, bytes=%d, chars=%d", extension, len(buffer), len(text)
    )
    return text

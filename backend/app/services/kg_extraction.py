"""
实体 / 关系抽取服务

供 KG 建图使用：从每个 chunk 抽「实体 + 关系」，再写入 Neo4j。

<p>ChatOpenAI.with_structured_output(method="function_calling")：
DeepSeek 走 OpenAI 兼容 Chat Completions，不支持 json_schema 严格模式，
用函数调用方式拿到结构化结果。</p>

<p>配置（config.py）：OPENAI_API_KEY / LLM_API_KEY / DASHSCOPE_API_KEY、
LLM_BASE_URL（默认 DeepSeek）、LLM_MODEL（默认 deepseek-v4-flash）、
KG_MAX_ENTITIES / KG_MAX_RELATIONS / KG_LLM_TIMEOUT_MS</p>
"""
import logging
import time

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.core.config import settings
from app.pipeline.types import ExtractedEntity, ExtractedRelation, ExtractionResult
from app.services.kg_extraction_schema import (
    KgExtractionLlmOutput,
    build_extraction_system_prompt,
    normalize_entity_type,
    normalize_relation_type,
)

logger = logging.getLogger("ExtractionService")

# user 消息里正文截断长度，防超上下文（)）
_MAX_CONTENT_CHARS = 4000
_LLM_TEMPERATURE = 0.1


class ExtractionService:
    """实体 / 关系抽取服务"""

    def __init__(self) -> None:
        # 单 chunk 最多实体 / 关系数，防止图爆炸
        self.max_entities = settings.KG_MAX_ENTITIES
        self.max_relations = settings.KG_MAX_RELATIONS
        # 未配置 API Key 时保持 None，extract() 调用时抛错
        self._structured = _build_structured_llm()

    async def extract(
        self,
        content: str,
        heading: str | None,
        document_title: str,
    ) -> ExtractionResult:
        """对单个 chunk 做抽取

        :param content: chunk 正文
        :param heading: 所属章节标题（给 LLM 当上下文）
        :param document_title: 文档标题
        """
        if not content or not content.strip():
            return ExtractionResult(entities=[], relations=[])

        return await self._extract_by_llm(content, heading, document_title)

    async def _extract_by_llm(
        self,
        content: str,
        heading: str | None,
        document_title: str,
    ) -> ExtractionResult:
        """LLM 抽取：system 约束规则，user 塞标题+正文（截断 4000 字防超上下文）"""
        if self._structured is None:
            raise RuntimeError(
                "KG 抽取未配置 API Key（OPENAI_API_KEY / LLM_API_KEY / DASHSCOPE_API_KEY）"
            )

        system = build_extraction_system_prompt(self.max_entities, self.max_relations)
        user = (
            f"文档标题: {document_title}\n"
            f"章节: {heading or '无'}\n\n"
            f"内容:\n{content[:_MAX_CONTENT_CHARS]}"
        )

        started = time.monotonic()
        parsed: KgExtractionLlmOutput = await self._structured.ainvoke(
            [SystemMessage(content=system), HumanMessage(content=user)]
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "KG 抽取完成：title=%s, elapsed=%dms, chars=%d, entities=%d",
            document_title,
            elapsed_ms,
            len(content),
            len(parsed.entities),
        )

        return self._to_extraction_result(parsed)

    def _to_extraction_result(self, parsed: KgExtractionLlmOutput) -> ExtractionResult:
        """截断数量、规范化类型、丢掉挂空实体的关系"""
        entity_names: set[str] = set()
        entities: list[ExtractedEntity] = []
        for e in parsed.entities[: self.max_entities]:
            name = (e.name or "").strip()
            if not name:
                continue
            entity_names.add(name)
            entities.append(
                ExtractedEntity(
                    name=name,
                    type=normalize_entity_type(e.type),
                    description=(e.description or "").strip(),
                    aliases=[a.strip() for a in (e.aliases or []) if a.strip()],
                )
            )

        relations: list[ExtractedRelation] = []
        for r in parsed.relations[: self.max_relations]:
            source = (r.source or "").strip()
            target = (r.target or "").strip()
            if (
                not source
                or not target
                or source not in entity_names
                or target not in entity_names
            ):
                continue
            relations.append(
                ExtractedRelation(
                    source=source,
                    target=target,
                    relation=normalize_relation_type(r.relation or r.type),
                    weight=r.weight if r.weight is not None else 0.5,
                )
            )

        return ExtractionResult(entities=entities, relations=relations)


def _build_structured_llm():
    """构建结构化 LLM"""
    api_key = (
        settings.OPENAI_API_KEY or settings.LLM_API_KEY or settings.DASHSCOPE_API_KEY
    )
    if not api_key:
        return None

    timeout_ms = settings.KG_LLM_TIMEOUT_MS
    timeout_s = timeout_ms / 1000 if timeout_ms > 0 else 60.0

    llm = ChatOpenAI(
        api_key=SecretStr(api_key),
        model=settings.LLM_MODEL,
        temperature=_LLM_TEMPERATURE,
        timeout=timeout_s,
        max_retries=0,
        # DeepSeek 走 Chat Completions 兼容层，不要切 OpenAI Responses API
        use_responses_api=False,
        base_url=settings.LLM_BASE_URL,
        # deepseek-v4-flash 默认 thinking 模式，不支持强制 tool_choice
        #（报错 "Thinking mode does not support this tool_choice"），
        # 结构化输出（function_calling）必须关闭 thinking
        extra_body={"thinking": {"type": "disabled"}},
    )

    return llm.with_structured_output(
        KgExtractionLlmOutput,
        method="function_calling",
        # 注意：不传 name——
        # langchain-openai 会把 name 透传给 completions.create() 导致 TypeError，
        # 工具名由 SDK 内部按 schema 生成，不影响结构化结果。
    )


# 全局单例
extraction_service = ExtractionService()

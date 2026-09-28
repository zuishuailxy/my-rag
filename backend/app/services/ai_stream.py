"""AI 流式对话服务

Agentic RAG 链路：会话（touchTitle / 新建）→ 取历史（Redis 热窗口，miss 回填库）
→ 滚动摘要 → 意图识别（TypeSafe classify → data-intent）→ 长期记忆检索
→ 按意图现建 agent 并挂工具（retrieve_knowledge / rewrite_query / web_search）
→ agent 循环（模型驱动：检索 → 评估 → 不足则改写再查，最多两次 → 仍不足才联网）
→ UI Message Stream（AI SDK v6 SSE）→ 落库（appendTurn + 短期/长期记忆）
→ 后台滚动摘要折叠。

工具内用 LangGraph `get_stream_writer()` 推 `data-eval` / `data-sources` /
`source-document` / `data-status`（custom 流）；SSE 生成器把 custom 帧原样转发。
内部评估/改写 LLM 调用带 `kh-internal` tag 且清空 callbacks，
消费端再按 metadata.tags 过滤，避免内部结构化 JSON 混进回答正文。

短期记忆（Redis 热窗口）：`load_working_history` 先 `tryLoad`，miss 才查库回填；
长期记忆（Mem0）：本轮 `search` 出 user/session 两层，拼成 SystemMessage 前置进
messages；落库尾部 `remember_turn` 异步写回。
滚动摘要（ChatSessionSummaryService）：把滑出热窗口的旧历史增量折叠；作答时作为
SystemMessage 前置，落库后后台 fold，不阻塞回答。

🔴 刻意差异：
- **协议编码器自研**（UiMessageStreamWriter）：
  toUIMessageStream，Python 无合适替代（候选包或要求高版本 fastapi、或丢 reasoning、
  或不支持 data-* / 流合并）。
- **agent 流用 stream_mode=["messages","updates","custom"]**：
  ["messages","tools"]；Python 的 tools 等价物是 updates 取 ToolMessage，custom 是
  工具内推 data-* 的通道。
：改由 ChatSessionSummaryService 做
  **跨请求持久化**的滚动摘要（中间件只在 agent state 内、每轮重算、不持久）。
- **mapReasoningStream 不需要**：DeepSeek reasoning_content 直接落在
  AIMessageChunk.additional_kwargs，这里直接映射 reasoning-* parts。

落库语义：sources 只存回答中实际引用 [n] 的条目，未标注存空列表。
"""

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import (
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.tools import tool
from langchain_deepseek import ChatDeepSeek
from langgraph.config import get_stream_writer
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.document_access import access_from_user, empty_scope
from app.core.config import settings
from app.services.llm_budget import usage_callbacks
from app.pipeline.types import ChunkHit
from app.schemas.ai import ChatStreamRequest, CreateSessionRequest
from app.schemas.auth import AuthUser
from app.services.agentic_retrieve import GradedRetrieve, retrieve_and_grade
from app.services.chat_long_memory import chat_long_memory_service
from app.services.chat_memory import db_rows_to_messages
from app.services.chat_query_rewrite import (
    KH_INTERNAL_TAG,
    ChatRoutePlan,
    chat_query_rewrite_service,
)
from app.services.chat_session import chat_session_service, title_from_question
from app.services.chat_short_memory import chat_short_memory_service
from app.services.chat_summary import chat_session_summary_service
from app.services.graph_build import format_graph_context, graph_build_service
from app.services.llm_budget import end_turn, start_turn
from app.services.retrieval import hybrid_retrieval_service
from app.services.web_search import web_search_service

logger = logging.getLogger("AiStreamService")

# 溯源摘录长度
_EXCERPT_LEN = 200
# 上下文里每条资料正文截断（)）
_CONTEXT_SNIPPET_LEN = 800
# 匹配回答中引用编号 [n]
_CITATION_RE = re.compile(r"\[(\d+)\]")

# SSE 终止帧（AI SDK useChat / DefaultChatTransport 识别用）
_SSE_DONE = "data: [DONE]\n\n"

# 长期记忆写入是 fire-and-forget。
# ⚠️ 必须持强引用：asyncio.create_task 生成的 Task 若无引用会被 GC 回收，
# 记忆写入会静默丢失（异常也被吞掉只 WARN，很难发现）。
_background_tasks: set[asyncio.Task[None]] = set()


def _spawn(coro: Any) -> None:
    """启动后台任务并持强引用（见 _background_tasks 注释）"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _noop_stream_writer(_event: dict[str, Any]) -> None:
    return None


def _stream_writer() -> Any:
    """取 LangGraph custom 流写端（工具内推 data-* 事件用）。

    在图内（正常请求路径）返回真实 writer；不在图内（如冒烟直接 invoke 工具）时
    `get_stream_writer()` 会抛 KeyError，这里退化为 no-op —— 事件是过程条增强，
    推不出去不应影响检索与作答。
    """
    try:
        return get_stream_writer()
    except Exception: # noqa: BLE001
        return _noop_stream_writer


class UiMessageStreamWriter:
    """UI Message Stream（AI SDK v6 协议）SSE 编码器

    正文（text-*）与思考（reasoning-*）自动开合：首个 delta 前发 start。text_parts 只收集正文。
    """

    def __init__(self) -> None:
        self._seq = 0
        self._text_id: str | None = None
        self._reasoning_id: str | None = None
        self._tool_input_started: set[str] = set()
        self._tool_input_sent: set[str] = set()
        self.text_parts: list[str] = []

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    @staticmethod
    def write(event: dict[str, Any]) -> str:
        return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    # ---- 显式事件----

    def start(self) -> str:
        return self.write({"type": "start"})

    def finish(self) -> str:
        return self.write({"type": "finish"})

    def error(self, error_text: str) -> str:
        return self.write({"type": "error", "errorText": error_text})

    def data(self, name: str, data: Any) -> str:
        """自定义 data-* part"""
        return self.write({"type": f"data-{name}", "data": data})

    def source_document(self, source_id: str, media_type: str, title: str) -> str:
        return self.write(
            {"type": "source-document", "sourceId": source_id, "mediaType": media_type, "title": title}
        )

    # ---- 自动开合的正文 / 思考 ----

    def text_delta(self, delta: str) -> str:
        events = ""
        if self._text_id is None:
            # 正文开始时收口尚未结束的思考 part（保持 part 互不嵌套）
            if self._reasoning_id is not None:
                events += self.write({"type": "reasoning-end", "id": self._reasoning_id})
                self._reasoning_id = None
            self._text_id = self._next_id("text")
            events += self.write({"type": "text-start", "id": self._text_id})
        self.text_parts.append(delta)
        return events + self.write({"type": "text-delta", "id": self._text_id, "delta": delta})

    def text_end(self) -> str:
        if self._text_id is None:
            return ""
        event = self.write({"type": "text-end", "id": self._text_id})
        self._text_id = None
        return event

    def reasoning_delta(self, delta: str) -> str:
        events = ""
        if self._reasoning_id is None:
            self._reasoning_id = self._next_id("reasoning")
            events += self.write({"type": "reasoning-start", "id": self._reasoning_id})
        return events + self.write(
            {"type": "reasoning-delta", "id": self._reasoning_id, "delta": delta}
        )

    def reasoning_end(self) -> str:
        if self._reasoning_id is None:
            return ""
        event = self.write({"type": "reasoning-end", "id": self._reasoning_id})
        self._reasoning_id = None
        return event

    # ---- 工具事件----

    def tool_input_start(self, tool_call_id: str, tool_name: str) -> str:
        self._tool_input_started.add(tool_call_id)
        return self.write(
            {"type": "tool-input-start", "toolCallId": tool_call_id, "toolName": tool_name}
        )

    def tool_input_available(self, tool_call_id: str, tool_name: str, tool_input: Any) -> str:
        self._tool_input_sent.add(tool_call_id)
        return self.write(
            {
                "type": "tool-input-available",
                "toolCallId": tool_call_id,
                "toolName": tool_name,
                "input": tool_input,
            }
        )

    def tool_output_available(self, tool_call_id: str, output: Any) -> str:
        return self.write({"type": "tool-output-available", "toolCallId": tool_call_id, "output": output})


# ---- 工具参数 schema----

class _WebSearchInput(BaseModel):
    """web_search 工具参数"""

    query: str = Field(min_length=1, description="搜索关键词")
    count: int = Field(default=5, ge=1, le=10, description="条数，默认 5")


class _RetrieveKnowledgeInput(BaseModel):
    """retrieve_knowledge 工具参数"""

    query: str = Field(min_length=1, description="适合检索的关键词或短句")
    top_k: int | None = Field(default=None, ge=1, le=10, description="条数，默认与请求一致")


class _RewriteQueryInput(BaseModel):
    """rewrite_query 工具参数"""

    focus: str | None = Field(default=None, description="希望改写时强调的缺口，可空")


class _GraphSearchInput(BaseModel):
    """retrieve_graph 工具参数"""

    query: str = Field(
        min_length=1,
        description="短实体名，多个用空格或 / 分隔（如「发票 / 报销」），不要整句",
    )


@tool("web_search", description=(
    "联网搜索（Tavily）。知识库循环结束后仍不足、或本轮只需公开时效信息时再调用。"
    "不要用网页替代已切题的知识库资料。"
), args_schema=_WebSearchInput)
async def _web_search(query: str, count: int = 5) -> dict[str, Any]:
    result = await web_search_service.search(query, count)
    out: dict[str, Any] = {
        "query": result.query,
        "items": [
            {"title": h.title, "url": h.url, "snippet": h.snippet, "siteName": h.siteName}
            for h in result.items
        ],
    }
    if result.error:
        out["error"] = result.error
    return out


def _build_agent_llm() -> ChatDeepSeek | None:
    """构建 agent 用 LLM；未配置 API Key 返回 None"""
    api_key = settings.OPENAI_API_KEY or settings.LLM_API_KEY or settings.DASHSCOPE_API_KEY
    if not api_key:
        return None

    base_url = settings.OPENAI_BASE_URL or settings.LLM_BASE_URL
    model = settings.MODEL_NAME or settings.LLM_MODEL
    timeout_ms = settings.AI_CHAT_TIMEOUT_MS
    timeout_s = timeout_ms / 1000 if timeout_ms > 0 else 60.0
# 'false' 时 enable_thinking；DeepSeek 用 thinking.type
    thinking_enabled = settings.LLM_ENABLE_THINKING != "false"

    # 刻意差异：用 ChatDeepSeek 而非 ChatOpenAI。langchain-openai 1.6.0 明确不抽取
    # reasoning_content（模块注释要求用 provider-specific subclass），实测流式思考
    # 全部丢失；ChatDeepSeek 实测把 reasoning_content 透传进 additional_kwargs。
    # 非流式 ai_chat.py 仍用 ChatOpenAI（thinking 关闭，无此需求）。
    return ChatDeepSeek(
        api_key=SecretStr(api_key),
        model=model,
        base_url=base_url,
        temperature=0.2,
        timeout=timeout_s,
        max_retries=0,
        # 用量/成本统计：挂一次即覆盖该 LLM 的所有调用点
        callbacks=usage_callbacks(),
        stream_usage=False,
        extra_body={
            "thinking": {"type": "enabled" if thinking_enabled else "disabled"},
            # 知识库 + 图谱需要同一步并行调用，显式打开并行 tool call
            "parallel_tool_calls": True,
        },
    )


def system_for_plan(plan: ChatRoutePlan) -> str:
    """按本轮意图写 agent 系统提示，规定 Agentic RAG 循环。"""
    prompt = (
        "你是企业知识库助手，必须按 Agentic RAG 循环作答，不要跳步。"
        "结合对话历史和记忆里的用户背景，但制度/流程以本轮检索资料为准。"
        f"本轮意图：{plan['label']}。"
    )
    if plan["allow_retrieve"] and plan["allow_graph"]:
        graph_hint = " / ".join(plan["graph_queries"]) or "从问题抽出的短实体名"
        prompt += (
            f"知识库建议检索词「{plan['query']}」，图谱建议检索词「{graph_hint}」。"
            "第一步必须在同一次回复里并行调用 retrieve_knowledge 和 retrieve_graph，两个都要调："
            "不要先查库再决定是否查图，不要只调其中一个，也不要等一个返回再调另一个。"
            "retrieve_graph 的 query 必须是短实体名（空格或 / 分隔），不要整句。"
            "两个工具的检索词/实体都只针对【当前问题】：记忆与历史只用于理解用户背景，"
            "不要把记忆或上文里其它话题的实体拿去检索。"
            "循环：① 并行检索；② 若知识库 eval.ok 则用文档 context 作答并标 [n]，"
            "图谱只看实体关系、不能当制度原文；"
            "③ 若 insufficient，先 rewrite_query，再用新词再次 retrieve_knowledge"
            "（知识库最多两次）；图谱词变了才再调 retrieve_graph；④ 两次后仍不足再考虑联网。"
            "eval.ok 后不要再检索、不要改写。不要第三次 retrieve_knowledge。"
            "图谱为空不要编造关系。不要用记忆替代文档。"
        )
    elif plan["allow_retrieve"]:
        prompt += (
            f"建议检索词「{plan['query']}」，可改写成更准的 query。"
            "循环：① retrieve_knowledge；② 若 eval.ok 则用 context 作答并标 [n]；"
            "③ 若 insufficient，先 rewrite_query，再用返回的新词再次 retrieve_knowledge"
            "（知识库最多两次）；④ 两次后仍不足再考虑联网。"
            "eval.ok 后不要再检索、不要改写。不要第三次 retrieve_knowledge。不要用记忆替代文档。"
        )
    else:
        prompt += "不要调用 retrieve_knowledge 或 rewrite_query。"
    if not plan["allow_graph"]:
        prompt += "不要调用 retrieve_graph。"
    if plan["intent"] == "chitchat":
        prompt += "这是闲聊，直接回应，不要调用任何工具。"
    if plan["intent"] == "profile":
        prompt += "这是个人偏好或身份问题，根据记忆回答，不要检索知识库。"
    if plan["intent"] == "kb":
        prompt += "两次检索后仍 insufficient：明确说知识库没有相关内容，禁止编造制度，不要联网。"
    if plan["intent"] == "web":
        prompt += "用 web_search 查公开信息，用标题+链接说明，不要编造。"
    if plan["intent"] == "kb_then_web":
        prompt += (
            "先走完知识库循环；两次后仍 insufficient 再 web_search。"
            "联网结果用标题+链接说明，不要把网页写成内部制度。"
        )
    if not plan["allow_web"]:
        prompt += "不要调用 web_search。"
    prompt += "资料不够就明确说不知道。"
    return prompt


def _pack_retrieve_result(
    hits: list[ChunkHit],
    query: str,
    persist_sources: list[dict[str, Any]],
    stream_writer: Any,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """命中写入 persist_sources 并推 data-sources；context 给模型，excerpt 给前端。），
    每次推**全量** data-sources（前端覆盖），逐条发 source-document。
    """
    offset = len(persist_sources)
    sources = [
        {
            "index": offset + i + 1,
            "documentId": hit.documentId,
            "documentTitle": hit.documentTitle,
            "heading": hit.heading,
            "excerpt": excerpt(hit.content),
            "score": hit.score,
        }
        for i, hit in enumerate(hits)
    ]
    persist_sources.extend(sources)
    items = [
        {
            "index": s["index"],
            "documentId": s["documentId"],
            "documentTitle": s["documentTitle"],
            "heading": s["heading"],
        }
        for s in sources
    ]
    # 只推 data-sources（全量快照）+ source-document；
    # 不再单独推 data-retrieve —— query/items 由前端 retrieve_knowledge 工具卡展示。
    stream_writer({"type": "data-sources", "data": list(persist_sources)})
    for src in sources:
        stream_writer(
            {
                "type": "source-document",
                "sourceId": str(src["documentId"]),
                "mediaType": "text/markdown",
                "title": f"[{src['index']}] {src['documentTitle']}",
            }
        )
    context = "\n\n".join(
        f"[{sources[i]['index']}] {hit.documentTitle}"
        + (f" / {hit.heading}" if hit.heading else "")
        + "\n"
        + (
            hit.content[:_CONTEXT_SNIPPET_LEN] + "..."
            if len(hit.content) > _CONTEXT_SNIPPET_LEN
            else hit.content
        )
        for i, hit in enumerate(hits)
    )
    result: dict[str, Any] = {
        "query": query,
        "items": [
            {**item, "excerpt": s["excerpt"]}
            for item, s in zip(items, sources, strict=True)
        ],
        "context": context,
    }
    if extra:
        result.update(extra)
    return result


def _chunk_text(chunk: AIMessageChunk) -> str:
    """取 AIMessageChunk 的正文增量（content 可能是 str 或块列表）"""
    content = chunk.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _iter_tool_messages(payload: Any) -> list[ToolMessage]:
    """从 updates 流事件里取 ToolMessage"""
    found: list[ToolMessage] = []
    if isinstance(payload, dict):
        for node_out in payload.values():
            if isinstance(node_out, dict):
                for msg in node_out.get("messages") or []:
                    if isinstance(msg, ToolMessage):
                        found.append(msg)
    return found


def last_user_text(messages: list[Any] | None) -> str:
    """取最后一条 user 消息的文本"""
    if not messages:
        return ""
    for msg in reversed(messages):
        if (msg.role or "") != "user":
            continue
        texts = [p.text for p in (msg.parts or []) if p.type == "text" and p.text]
        return "".join(texts).strip()
    return ""


def excerpt(content: str) -> str:
    """摘要：压缩空白 + 超长截断"""
    text = re.sub(r"\s+", " ", content).strip()
    if len(text) <= _EXCERPT_LEN:
        return text
    return f"{text[:_EXCERPT_LEN]}..."


class AiStreamService:
    """AI 流式对话：会话编排 → 意图路由 → agentic 检索循环 → UI Message Stream → 落库"""

    def __init__(self) -> None:
        # 只建 LLM，agent/工具**每轮现建**（闭包持本轮 retrieve_count / last_retrieve /
        # persist_sources）。
        self._llm = _build_agent_llm()

    def create_turn_agent(
        self,
        user: AuthUser,
        default_top_k: int,
        persist_sources: list[dict[str, Any]],
        plan: ChatRoutePlan,
        question: str,
    ) -> Any | None:
        """按本轮意图挂工具现建 agent

        检索、改写、再检索由 agent 循环调度，每个工具只做一步。工具内的可变状态
        （retrieve_count / last_retrieve）与 persist_sources 由闭包捕获，天然按轮隔离。
        """
        if self._llm is None:
            return None
        retrieval = hybrid_retrieval_service
        rewrite = chat_query_rewrite_service
        graph = graph_build_service
        tools: list[Any] = []
        turn_state: dict[str, Any] = {"retrieve_count": 0, "last_retrieve": None}

        if plan["allow_retrieve"]:
            @tool(
                "retrieve_knowledge",
                args_schema=_RetrieveKnowledgeInput,
                description=(
                    "检索企业知识库一次（仅当前用户可见文档）并评估是否切题。不会自动再查。"
                    "首次必须与 retrieve_graph 在同一次并行调用。"
                    "eval.ok 为 false 时先 rewrite_query，再用新词再调本工具。本轮最多两次。"
                ),
            )
            async def retrieve_knowledge(
                query: str, top_k: int | None = None
            ) -> dict[str, Any]:
                if turn_state["retrieve_count"] >= 2:
                    return {
                        "query": query,
                        "items": [],
                        "insufficient": True,
                        "error": "本轮知识库最多检索两次，请根据已有评估作答或联网",
                    }
                turn_state["retrieve_count"] += 1
                last: GradedRetrieve | None = turn_state["last_retrieve"]
                result = await retrieve_and_grade(
                    question=question,
                    query=query,
                    top_k=top_k or default_top_k,
                    user=user,
                    retrieval=retrieval,
                    rewrite=rewrite,
                    retried=turn_state["retrieve_count"] > 1,
                    previous_query=last.used_query if last else None,
                )
                turn_state["last_retrieve"] = result
                stream_writer = _stream_writer()
                stream_writer({"type": "data-eval", "data": result.eval.to_payload()})
                if result.eval.reason == "error":
                    logger.warning("RAG 检索失败：%s", result.eval.text)
                    return {
                        "query": result.used_query,
                        "items": [],
                        "error": result.eval.text,
                        "insufficient": True,
                        "eval": result.eval.to_payload(),
                    }
                if not result.eval.ok:
                    return _pack_retrieve_result(
                        [],
                        result.used_query,
                        persist_sources,
                        stream_writer,
                        {"insufficient": True, "eval": result.eval.to_payload()},
                    )
                return _pack_retrieve_result(
                    result.hits,
                    result.used_query,
                    persist_sources,
                    stream_writer,
                    {"eval": result.eval.to_payload()},
                )

            @tool(
                "rewrite_query",
                args_schema=_RewriteQueryInput,
                description=(
                    "根据上次检索评估改写检索词。仅在 retrieve_knowledge 返回 insufficient 后调用，"
                    "再把返回的 query 交给 retrieve_knowledge。"
                ),
            )
            async def rewrite_query(focus: str | None = None) -> dict[str, Any]:
                last: GradedRetrieve | None = turn_state["last_retrieve"]
                if last is None:
                    return {"error": "请先调用 retrieve_knowledge"}
                if last.eval.ok:
                    return {"error": "当前资料已切题，无需改写", "query": last.used_query}
                _stream_writer()(
                    {"type": "data-status", "data": {"stage": "rewrite", "text": "正在改写检索词…"}}
                )
                retry_query = await rewrite.rewrite_after_retrieve(
                    question, last.used_query, last.grade, last.raw_hits
                )
                if not retry_query:
                    return {
                        "error": "无法改写出与上次不同的检索词",
                        "previousQuery": last.used_query,
                        "reason": last.eval.text,
                    }
                return {
                    "query": retry_query,
                    "previousQuery": last.used_query,
                    "reason": last.eval.text,
                    "focus": focus,
                }

            tools.extend([retrieve_knowledge, rewrite_query])

        if plan["allow_graph"]:

            @tool(
                "retrieve_graph",
                args_schema=_GraphSearchInput,
                description=(
                    "检索知识图谱实体与关系（仅当前用户可见文档抽取出的图）。"
                    "入参必须是短实体名，多个用空格或 / 分隔，例如「发票 / 报销」，不要整句。"
                    "只从【当前用户问题】抽实体，不要用【用户长期记忆】/【当前会话记忆】"
                    "或历史对话里其它话题的实体。"
                    "首次检索知识库时必须与 retrieve_knowledge 在同一次并行调用。"
                    "图谱只说明实体关系，不能当制度原文。仅当还要查其他实体名时再单独再调。"
                ),
            )
            async def retrieve_graph(query: str) -> dict[str, Any]:
                keywords = [
                    *plan["graph_queries"],
                    *([query.strip()] if query.strip() else []),
                ]
                logger.info(
                    "图谱检索(工具)：question=%r plan=%s input=%r → kws=%s",
                    question[:80],
                    "/".join(plan["graph_queries"]) or "-",
                    query[:60],
                    "/".join(keywords) or "-",
                )
                scope = access_from_user(user) if user is not None else empty_scope()
                hit = await graph.retrieve_for_chat(keywords, limit=8, scope=scope)
                insufficient = not hit["entities"] and not hit["relations"]
                logger.info(
                    "图谱检索(工具)结果：entities=%d relations=%d documents=%d insufficient=%s",
                    len(hit["entities"]),
                    len(hit["relations"]),
                    len(hit["documents"]),
                    insufficient,
                )
                _stream_writer()({"type": "data-graph", "data": hit})
                return {
                    "query": hit["query"],
                    "entities": hit["entities"],
                    "relations": hit["relations"],
                    "documents": hit["documents"],
                    "context": format_graph_context(hit),
                    "insufficient": insufficient,
                }

            tools.append(retrieve_graph)

        if plan["allow_web"]:
            tools.append(_web_search)

        logger.info(
            "本轮工具挂载：intent=%s query=%r graph=%s tools=%s",
            plan["intent"],
            plan["query"][:60],
            "/".join(plan["graph_queries"]) or "-",
            [t.name for t in tools],
        )
        # 单次 invoke 最多 8 次模型调用：避免循环打爆；
        # 超限正常结束而非抛错。工具内部的评估/改写 LLM 调用已用 kh-internal 隔离、不计入。
        return create_agent(
            model=self._llm,
            tools=tools,
            system_prompt=system_for_plan(plan),
            middleware=[ModelCallLimitMiddleware(run_limit=8, exit_behavior="end")],
        )

    async def stream_chat(
        self, db: AsyncSession, dto: ChatStreamRequest, user: AuthUser
    ) -> AsyncIterator[str]:
        """SSE 事件流生成器"""
        writer = UiMessageStreamWriter()
        question = last_user_text(dto.messages)
        top_k = dto.topK or 5
        persist_session_id: str | None = dto.sessionId
        persist_sources: list[dict[str, Any]] = []
        persist_history: list[BaseMessage] = []

        yield writer.start()

        if not question:
            # 空问题不建会话不落库
            yield writer.text_delta("请输入问题。")
            yield writer.text_end()
            yield writer.finish()
            yield _SSE_DONE
            return

        # 会话：有 id 走 touchTitle（默认标题时用首问覆盖），否则新建
        if dto.sessionId:
            sess = await chat_session_service.touch_title(
                db, user.user_id, dto.sessionId, question
            )
        else:
            sess = await chat_session_service.create(
                db, user.user_id, CreateSessionRequest(title=title_from_question(question))
            )
        persist_session_id = str(sess.id)
        yield writer.data("session", {"sessionId": persist_session_id})

        # 历史：先读 Redis 热窗口，miss 才查库回填
        history = await self.load_working_history(db, user.user_id, persist_session_id)
        persist_history = history

        # 滚动摘要：热窗口之外的旧历史（Redis + Postgres 双写，Redis 优先）
        summary_state = await chat_session_summary_service.try_load(
            user.user_id, persist_session_id
        )

        # 意图路由（TypeSafe）在 agent 之前完成：工具列表按 plan 挂载，前端立刻能画意图卡
        yield writer.data("status", {"stage": "intent", "text": "正在识别意图…"})
        plan = await chat_query_rewrite_service.classify(question, history)
        yield writer.data(
            "intent",
            {
                "intent": plan["intent"],
                "label": plan["label"],
                "query": plan["query"],
                "graphQueries": plan["graph_queries"],
                "allowRetrieve": plan["allow_retrieve"],
                "allowGraph": plan["allow_graph"],
                "allowWeb": plan["allow_web"],
            },
        )

        # 长期记忆检索
        mem_hits = await chat_long_memory_service.search(
            user.user_id, persist_session_id, question
        )

        agent = self.create_turn_agent(user, top_k, persist_sources, plan, question)
        if agent is None:
            yield writer.error("未配置 LLM Key，无法生成回答")
            yield writer.finish()
            yield _SSE_DONE
            return

        # 会话摘要（早前对话）与长期记忆各拼一条 SystemMessage 前置；
        # 顺序：先摘要（对话上下文），后 Mem0（用户背景）。空则跳过。
        summary_msg = chat_session_summary_service.build_system_message(summary_state)
        memory_msg = chat_long_memory_service.build_system_message(mem_hits)
        human_parts = []
        if plan["allow_retrieve"]:
            human_parts.append(f"建议检索词（可按问题改写后再检索）：{plan['query']}")
        human_parts.append(f"用户问题：{question}")
        messages: list[BaseMessage] = [
            *([summary_msg] if summary_msg else []),
            *([memory_msg] if memory_msg else []),
            *history,
            HumanMessage("\n\n".join(human_parts)),
        ]

        # 用量统计：handler 已挂在各 LLM 的构造器上（见 llm_budget.usage_callbacks），
        # LangChain 会把 callbacks 透传给工具内部的子调用（改写 / 切题判定），
        # 所以这里只需把「本轮」的 ContextVar 划出来。
        usage, usage_token = start_turn()
        try:
            # custom 流 = 工具内 get_stream_writer 推来的 data-*；messages = 模型 token/思考；
            # updates = 工具结果。
            agent_stream = agent.astream(
                {"messages": messages},
                stream_mode=["messages", "updates", "custom"],
            )
            async for mode, payload in agent_stream:
                if mode == "custom":
                    # 工具推来的 UI 事件（data-eval / data-sources / source-document / data-status）
                    if isinstance(payload, dict):
                        yield UiMessageStreamWriter.write(payload)
                    continue
                if mode == "messages":
                    chunk, meta = payload
                    # 内部评估/改写 LLM 的 token 不能进正文
                    if KH_INTERNAL_TAG in ((meta or {}).get("tags") or []):
                        continue
                    if not isinstance(chunk, AIMessageChunk):
                        continue
                    reasoning = chunk.additional_kwargs.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        yield writer.reasoning_delta(reasoning)
                    text = _chunk_text(chunk)
                    if text:
                        yield writer.text_delta(text)
                    for tc in chunk.tool_call_chunks or []:
                        tc_id = tc.get("id")
                        tc_name = tc.get("name")
                        if tc_id and tc_name and tc_id not in writer._tool_input_started:
                            yield writer.tool_input_start(str(tc_id), str(tc_name))
                    for call in chunk.tool_calls or []:
                        call_id = call.get("id")
                        if call_id and call_id not in writer._tool_input_sent:
                            yield writer.tool_input_available(
                                str(call_id),
                                str(call.get("name") or ""),
                                call.get("args") or {},
                            )
                else: # updates：工具结果
                    for msg in _iter_tool_messages(payload):
                        if msg.tool_call_id in writer._tool_input_sent:
                            yield writer.tool_output_available(
                                msg.tool_call_id, msg.content
                            )
            # 收口未闭合的 part（流正常结束时 adapter 由 sendFinish 收口，这里显式做）
            yield writer.reasoning_end()
            yield writer.text_end()
        except Exception as exc: # noqa: BLE001
            logger.warning("LangChain 流失败：%s", exc)
        finally:
            end_turn(usage_token)

        # 成本可见性：只有真实 token 数（取不到就不猜），金额标注为估算
        logger.info(
            "本轮 LLM 用量：sessionId=%s, %s",
            persist_session_id,
            usage.as_log_fields(),
        )

        yield writer.finish()
        yield _SSE_DONE

        # onFinish：落库
        answer = "".join(writer.text_parts).strip()
        used = {int(m.group(1)) for m in _CITATION_RE.finditer(answer)}
        cited = [s for s in persist_sources if s["index"] in used] if used else []
        if not question or not persist_session_id:
            return
        final_answer = answer or "未能生成回答。"
        try:
            await chat_session_service.append_turn(
                db, user.user_id, persist_session_id, question, final_answer, cited
            )
            # 短期记忆回写：history + 本轮问答（appendTurn 内部再截断到窗口）
            await chat_short_memory_service.append_turn(
                user.user_id, persist_session_id, persist_history, question, final_answer
            )
        except Exception as exc: # noqa: BLE001
            logger.warning("流式对话落库失败：%s", exc)
        # 长期记忆写入不 await：分类器是一次额外 LLM 调用，
        # 不能让用户等它；remember_turn 内部已吞异常只 WARN。
        _spawn(
            chat_long_memory_service.remember_turn(
                user.user_id, persist_session_id, question, final_answer
            )
        )
        # 滚动摘要：把滑出热窗口的旧消息增量折叠进摘要（fire-and-forget，
        # 与摘要读取同为「下一轮生效」；服务内部有每会话锁防重复折叠）。
        _spawn(
            chat_session_summary_service.fold(
                user.user_id,
                persist_session_id,
                chat_short_memory_service.window_size,
            )
        )

    async def load_working_history(
        self, db: AsyncSession, user_id: str, session_id: str
    ) -> list[BaseMessage]:
        """取本轮要用的历史：Redis 热窗口优先，miss 才查库回填

        返回的 history 会前置进 agent 的 messages，并在落库时原样交给短期记忆
        追加本轮问答（否则只写本轮、丢掉窗口里已有的历史）。
        """
        cached = await chat_short_memory_service.try_load(user_id, session_id)
        # ⚠️ 必须是 `is not None` 而不是真值判断：save([]) 后 try_load 返回 []，
        # 那是「命中但窗口为空」而不是 miss，用 `if cached:` 会白白回填一次库。
        if cached is not None:
            return cached
        rows = await chat_session_service.list_recent_messages(
            db, user_id, session_id, chat_short_memory_service.window_size
        )
        history = db_rows_to_messages(rows)
        if history:
            await chat_short_memory_service.save(user_id, session_id, history)
            logger.info(
                "短期记忆从库回填：sessionId=%s, n=%d", session_id, len(history)
            )
        return history


# 全局单例
ai_stream_service = AiStreamService()

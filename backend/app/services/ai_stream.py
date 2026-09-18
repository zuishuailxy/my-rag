"""AI 流式对话服务（对应 TS ai/ai-stream.service.ts，逐段转写）

链路：会话（touchTitle / 新建）→ 取历史（Redis 热窗口，miss 回填库）→ 追问改写
（检索改写器）→ 混合检索 ∥ 长期记忆检索 → data-* 进度事件 → agent 流式作答
→ UI Message Stream（AI SDK v6 SSE 协议）→ 落库（appendTurn + 短期/长期记忆）。

短期记忆（Redis 热窗口）：`load_working_history` 先 `tryLoad`，miss 才查库回填；
长期记忆（Mem0）：本轮 `search` 出 user/session 两层，拼成 SystemMessage 前置进
messages；落库尾部 `remember_turn` 异步写回（不 await，对齐 TS 的 fire-and-forget）。
追问改写（ChatQueryRewriteService）：结合历史把「那谁审批？」改写成独立查询，
并判定本轮是否需要检索；`need_retrieve=false`（寒暄）时跳过检索直接作答。

🔴 刻意差异一：协议编码器自研（本文件 UiMessageStreamWriter）。
TS 用 `ai` 包的 createUIMessageStream/pipeUIMessageStreamToResponse，Python 无对应物。
候选包均不合适：
- ai-sdk-stream-python：要求 fastapi>=0.128.8，项目锁 fastapi==0.110.1，
  为辅助包升级 Web 框架爆炸半径过大；
- langchain-vercel-ai-sdk-adapter：alpha 且**明确过滤 thinking 块**（会丢
  DeepSeek reasoning_content），也不支持 data-* 自定义事件与流合并。
事件序列逐条对齐 TS writer 调用（含 data-session / data-status / data-retrieve /
data-sources / source-document），SSE 帧格式 `data: {json}\\n\\n`，
终止帧 `data: [DONE]\\n\\n`，响应头 `x-vercel-ai-ui-message-stream: v1`。

🔴 刻意差异二：不需要 TS 的 mapReasoningStream。那是为了喂 @ai-sdk/langchain
适配器（只认 additional_kwargs.reasoning.summary）；DeepSeek（OpenAI 兼容口）
的思考 delta 直接落在 AIMessageChunk.additional_kwargs["reasoning_content"]，
此处直接映射协议的 reasoning-start/delta/end parts。

🔴 刻意差异三：agent 流用 langgraph 的 stream_mode=["messages", "updates"]。
TS 用 streamMode=["messages", "tools"]（LangChain JS 的 createAgent 支持
"tools" 模式），Python 端等价物是 updates 模式里取 ToolMessage。

落库语义（对齐 TS onFinish）：sources 只存回答中实际引用 [n] 的条目，
未标注时存**空列表**（与非流式 to_cited_sources 的「回退全部召回」不同）。
"""

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
)
from langchain_core.messages import (
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.tools import tool
from langchain_deepseek import ChatDeepSeek
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.pipeline.types import ChunkHit
from app.schemas.ai import ChatStreamRequest, CreateSessionRequest
from app.schemas.auth import AuthUser
from app.services.chat_long_memory import chat_long_memory_service
from app.services.chat_memory import db_rows_to_messages
from app.services.chat_query_rewrite import chat_query_rewrite_service
from app.services.chat_session import chat_session_service, title_from_question
from app.services.chat_short_memory import chat_short_memory_service
from app.services.retrieval import hybrid_retrieval_service
from app.services.web_search import web_search_service

logger = logging.getLogger("AiStreamService")

# 溯源摘录长度（对齐 TS EXCERPT_LEN）
_EXCERPT_LEN = 200
# 上下文里每条资料正文截断（对齐 TS slice(0, 800)）
_CONTEXT_SNIPPET_LEN = 800
# 匹配回答中引用编号 [n]
_CITATION_RE = re.compile(r"\[(\d+)\]")

_SYSTEM_PROMPT = (
    "你是企业知识库助手。优先根据「检索到的资料」回答。"
    "结合对话历史和记忆里的用户背景，但制度/流程以本轮资料为准，不要用记忆替代文档。"
    "没有检索资料且是寒暄时，直接回应，不必调用 web_search。"
    "资料不足、需要时效性或外部公开信息时，调用 web_search。"
    "依据资料的陈述句末标 [n]，与资料编号一致。"
    "联网结果用标题+链接说明，不要编造。资料不够就明确说不知道。"
)

# 摘要中间件提示词（与 TS summaryPrompt 逐字一致，{messages} 由中间件填充）
_SUMMARY_PROMPT = (
    "用中文简洁总结对话：话题、已确认结论、待办。不要写入知识库条文。\n\n"
    "待摘要的对话：\n{messages}\n\n摘要："
)
# 历史达到 12 条时触发摘要，保留最近 6 条（对齐 TS trigger/keep）
_SUMMARY_TRIGGER_MESSAGES = 12
_SUMMARY_KEEP_MESSAGES = 6

# SSE 终止帧（AI SDK useChat / DefaultChatTransport 识别用）
_SSE_DONE = "data: [DONE]\n\n"

# 长期记忆写入是 fire-and-forget（对齐 TS 不 await 的 rememberTurn）。
# ⚠️ 必须持强引用：asyncio.create_task 生成的 Task 若无引用会被 GC 回收，
# 记忆写入会静默丢失（异常也被吞掉只 WARN，很难发现）。
_background_tasks: set[asyncio.Task[None]] = set()


def _spawn(coro: Any) -> None:
    """启动后台任务并持强引用（见 _background_tasks 注释）"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


class UiMessageStreamWriter:
    """UI Message Stream（AI SDK v6 协议）SSE 编码器

    正文（text-*）与思考（reasoning-*）自动开合：首个 delta 前发 start，
    收口时发 end——等价 TS 适配包内部做的协议状态机。text_parts 只收集正文，
    对应 TS onFinish 里「取 responseMessage 的 text parts 拼回答」。
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

    # ---- 显式事件（逐条对齐 TS writer.write 调用）----

    def start(self) -> str:
        return self.write({"type": "start"})

    def finish(self) -> str:
        return self.write({"type": "finish"})

    def error(self, error_text: str) -> str:
        return self.write({"type": "error", "errorText": error_text})

    def data(self, name: str, data: Any) -> str:
        """自定义 data-* part（TS: data-session / data-status / data-retrieve / data-sources）"""
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

    # ---- 工具事件（对应 TS 适配包转出的 tool-* 事件）----

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


class _WebSearchInput(BaseModel):
    """web_search 工具参数（对齐 TS zod schema）"""

    query: str = Field(min_length=1, description="搜索关键词")
    count: int = Field(default=5, ge=1, le=10, description="条数，默认 5")


@tool("web_search", description=(
    "联网搜索（Tavily）。知识库不足、需要最新公开信息或外部资料时再调用。"
    "不要用它替代知识库已有内容。"
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


def _build_agent() -> Any | None:
    """构建 agent（对应 TS 构造函数）；未配置 API Key 返回 None"""
    api_key = settings.OPENAI_API_KEY or settings.LLM_API_KEY or settings.DASHSCOPE_API_KEY
    if not api_key:
        return None

    base_url = settings.OPENAI_BASE_URL or settings.LLM_BASE_URL
    model = settings.MODEL_NAME or settings.LLM_MODEL
    timeout_ms = settings.AI_CHAT_TIMEOUT_MS
    timeout_s = timeout_ms / 1000 if timeout_ms > 0 else 60.0
    # TS: LLM_ENABLE_THINKING !== 'false' 时 enable_thinking；DeepSeek 用 thinking.type
    thinking_enabled = settings.LLM_ENABLE_THINKING != "false"

    # 刻意差异：用 ChatDeepSeek 而非 ChatOpenAI。langchain-openai 1.6.0 明确不抽取
    # reasoning_content（模块注释要求用 provider-specific subclass），实测流式思考
    # 全部丢失；ChatDeepSeek 实测把 reasoning_content 透传进 additional_kwargs。
    # 非流式 ai_chat.py 仍用 ChatOpenAI（thinking 关闭，无此需求）。
    llm = ChatDeepSeek(
        api_key=SecretStr(api_key),
        model=model,
        base_url=base_url,
        temperature=0.2,
        timeout=timeout_s,
        max_retries=0,
        stream_usage=False,
        extra_body={"thinking": {"type": "enabled" if thinking_enabled else "disabled"}},
    )
    # 摘要用 LLM（对齐 TS compactLlm：temperature 0）。
    # ⚠️ 刻意差异：TS 的 compactLlm 没关 thinking（DeepSeek 默认开），这里按 TS 注释
    # 「摘要/分类不要开思考」的意图显式关闭——思考对摘要无益，还拉长耗时与成本。
    compact_llm = ChatDeepSeek(
        api_key=SecretStr(api_key),
        model=model,
        base_url=base_url,
        temperature=0,
        timeout=timeout_s,
        max_retries=0,
        stream_usage=False,
        extra_body={"thinking": {"type": "disabled"}},
    )
    # 中间件列表显式标注为 list[Any]：SummarizationMiddleware 与 ModelCallLimitMiddleware
    # 的状态泛型不同，直接内联进 create_agent 会让 mypy 按首元素推断列表类型而报错。
    middleware: list[Any] = [
        # 历史过长时把早期对话压成摘要、保留最近 6 条（对齐 TS summarizationMiddleware）。
        # Python 侧 trigger / keep 必须是元组形式（字典 `{messages: 12}` 会解包报错）。
        SummarizationMiddleware(
            model=compact_llm,
            trigger=("messages", _SUMMARY_TRIGGER_MESSAGES),
            keep=("messages", _SUMMARY_KEEP_MESSAGES),
            summary_prompt=_SUMMARY_PROMPT,
        ),
        # 单次 invoke 最多 4 次模型调用（对齐 TS modelCallLimitMiddleware）：
        # 避免 web_search 循环打爆；超限正常结束而非抛错
        ModelCallLimitMiddleware(run_limit=4, exit_behavior="end"),
    ]
    return create_agent(
        model=llm,
        tools=[_web_search],
        system_prompt=_SYSTEM_PROMPT,
        middleware=middleware,
    )


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
    """从 updates 流事件里取 ToolMessage（对应 TS 'tools' 流模式）"""
    found: list[ToolMessage] = []
    if isinstance(payload, dict):
        for node_out in payload.values():
            if isinstance(node_out, dict):
                for msg in node_out.get("messages") or []:
                    if isinstance(msg, ToolMessage):
                        found.append(msg)
    return found


def last_user_text(messages: list[Any] | None) -> str:
    """取最后一条 user 消息的文本（对齐 TS lastUserText）"""
    if not messages:
        return ""
    for msg in reversed(messages):
        if (msg.role or "") != "user":
            continue
        texts = [p.text for p in (msg.parts or []) if p.type == "text" and p.text]
        return "".join(texts).strip()
    return ""


def excerpt(content: str) -> str:
    """摘要：压缩空白 + 超长截断（对齐 TS excerpt）"""
    text = re.sub(r"\s+", " ", content).strip()
    if len(text) <= _EXCERPT_LEN:
        return text
    return f"{text[:_EXCERPT_LEN]}..."


class AiStreamService:
    """AI 流式对话：会话编排 → 检索 → agent 流 → UI Message Stream → 落库"""

    def __init__(self) -> None:
        self._agent = _build_agent()

    async def stream_chat(
        self, db: AsyncSession, dto: ChatStreamRequest, user: AuthUser
    ) -> AsyncIterator[str]:
        """SSE 事件流生成器（对应 TS streamChat + createUIMessageStream execute）"""
        writer = UiMessageStreamWriter()
        question = last_user_text(dto.messages)
        top_k = dto.topK or 5
        persist_session_id: str | None = dto.sessionId
        persist_sources: list[dict[str, Any]] = []
        persist_history: list[BaseMessage] = []

        yield writer.start()

        if not question:
            # 空问题不建会话不落库（对齐 TS 早退分支）
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

        # 追问改写：有历史才有改写价值（对齐 TS `if (history.length)`）
        if history:
            yield writer.data("status", {"stage": "rewrite", "text": "正在理解问题…"})
        plan = await chat_query_rewrite_service.rewrite(question, history)
        if plan["need_retrieve"]:
            yield writer.data("status", {"stage": "retrieve", "text": "正在检索知识库…"})

        # 检索与长期记忆并行（对齐 TS Promise.all）；need_retrieve=false 时跳过检索
        async def _retrieve_if_needed() -> list[ChunkHit]:
            if not plan["need_retrieve"]:
                return []
            try:
                rows = await hybrid_retrieval_service.retrieve(plan["query"], top_k, user)
                return [ChunkHit.from_row(row) for row in rows]
            except Exception as exc:  # noqa: BLE001 —— 检索失败不阻断作答（对齐 TS catch）
                logger.warning("RAG 检索失败：%s", exc)
                return []

        hits, mem_hits = await asyncio.gather(
            _retrieve_if_needed(),
            chat_long_memory_service.search(
                user.user_id, persist_session_id, plan["query"]
            ),
        )

        sources = [
            {
                "index": i,
                "documentId": hit.documentId,
                "documentTitle": hit.documentTitle,
                "heading": hit.heading,
                "excerpt": excerpt(hit.content),
                "score": hit.score,
            }
            for i, hit in enumerate(hits, start=1)
        ]
        persist_sources = sources
        yield writer.data(
            "retrieve",
            {
                # 对齐 TS：上报的是改写后的检索 query，不是原始追问
                "query": plan["query"],
                "items": [
                    {
                        "index": s["index"],
                        "documentId": s["documentId"],
                        "documentTitle": s["documentTitle"],
                        "heading": s["heading"],
                    }
                    for s in sources
                ],
            },
        )
        yield writer.data("sources", sources)
        for s in sources:
            yield writer.source_document(
                str(s["documentId"]), "text/markdown", f"[{s['index']}] {s['documentTitle']}"
            )

        if self._agent is None:
            yield writer.error("未配置 LLM Key，无法生成回答")
            yield writer.finish()
            yield _SSE_DONE
            return

        if hits:
            prompt = f"检索到的资料：\n{self.build_context(hits)}\n\n用户问题：{question}"
        elif plan["need_retrieve"]:
            prompt = f"知识库没有召回到相关内容。\n\n用户问题：{question}"
        else:
            # 寒暄：不检索，直接作答（对齐 TS）
            prompt = f"用户问题：{question}"

        # 长期记忆拼成一条 SystemMessage 前置；两层皆空时为 None，不注入（对齐 TS）
        memory_msg = chat_long_memory_service.build_system_message(mem_hits)
        messages: list[BaseMessage] = [
            *([memory_msg] if memory_msg else []),
            *history,
            HumanMessage(prompt),
        ]

        try:
            agent_stream = self._agent.astream(
                {"messages": messages},
                stream_mode=["messages", "updates"],
            )
            async for mode, payload in agent_stream:
                if mode == "messages":
                    chunk, _meta = payload
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
                else:  # updates：工具结果（对应 TS 'tools' 流模式）
                    for msg in _iter_tool_messages(payload):
                        if msg.tool_call_id in writer._tool_input_sent:
                            yield writer.tool_output_available(
                                msg.tool_call_id, msg.content
                            )
            # 收口未闭合的 part（流正常结束时 adapter 由 sendFinish 收口，这里显式做）
            yield writer.reasoning_end()
            yield writer.text_end()
        except Exception as exc:  # noqa: BLE001
            logger.warning("LangChain 流失败：%s", exc)

        yield writer.finish()
        yield _SSE_DONE

        # onFinish：落库（对齐 TS——sources 只存实际引用的 [n]，未标注存空列表）
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("流式对话落库失败：%s", exc)
        # 长期记忆写入不 await（对齐 TS 的 fire-and-forget）：分类器是一次额外 LLM 调用，
        # 不能让用户等它；remember_turn 内部已吞异常只 WARN。
        _spawn(
            chat_long_memory_service.remember_turn(
                user.user_id, persist_session_id, question, final_answer
            )
        )

    async def load_working_history(
        self, db: AsyncSession, user_id: str, session_id: str
    ) -> list[BaseMessage]:
        """取本轮要用的历史：Redis 热窗口优先，miss 才查库回填（对应 TS loadWorkingHistory）

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

    @staticmethod
    def build_context(hits: list[ChunkHit]) -> str:
        """把召回块拼成 LLM 上下文（对齐 TS buildContext）"""
        parts = []
        for i, src in enumerate(hits, start=1):
            heading = f" / {src.heading}" if src.heading else ""
            snippet = (
                src.content[:_CONTEXT_SNIPPET_LEN] + "..."
                if len(src.content) > _CONTEXT_SNIPPET_LEN
                else src.content
            )
            parts.append(f"[{i}] {src.documentTitle}{heading}\n{snippet}")
        return "\n\n".join(parts)


# 全局单例
ai_stream_service = AiStreamService()

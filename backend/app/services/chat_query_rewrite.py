"""检索前把追问改写成独立短查询（对应 TS ai/chat-query-rewrite.service.ts，逐段转写）

角色：多轮对话里用户常问「那谁审批？」「这个怎么办？」这类依赖上文的追问，
直接拿去混合检索会召回一堆无关制度。本服务先让 LLM 结合最近几轮把追问改写成
一条可独立检索的短查询，同时判断这一轮到底要不要查知识库（寒暄、致谢直接跳过检索）。

回退语义（与 TS 一致）：未配置 LLM Key、没有历史、上下文为空、LLM 调用失败，
任一情况下都返回 `{query: 原问题, need_retrieve: True}` —— 改写是增强，不是必需环节，
任何失败都不能让用户问不出结果。

刻意差异（相对 TS）：
- **结构化输出改用 `method="function_calling"` 且关闭 thinking**：TS 用
  `withStructuredOutput(rewriteSchema)` 走所选模型（qwen-plus）默认的 jsonSchema 通道；
  本项目 LLM 是 **deepseek-v4-flash**，不支持 json_schema 严格模式、也不接受带 name 的
  tool_choice（详见 kg_extraction.py / chat_long_memory.py 同类注释）。故显式指定
  function_calling + `extra_body={"thinking": {"type": "disabled"}}`。
- **`RetrieveQueryPlan` 的 `needRetrieve` → `need_retrieve`**：纯进程内类型，
  无跨语言契约（不像 ChunkHit 要对齐 search 输出的 dict 键），按 Python 习惯 snake_case。
  注意 LLM 结构化输出的字段名仍是 `standalone_query` / `need_retrieve`，与 TS zod 逐字一致。
- TS `Number(config.get('AI_QUERY_REWRITE_TIMEOUT_MS', 15000))` → config.py 声明为 int，
  非法值启动即报错（同 chat_short_memory 的处理）。
- 调用用 `ainvoke`（TS `invoke`）：本服务在流式问答的请求路径上，不阻塞事件循环。

未实现（TS 侧也没有，勿擅自加）：检索结果缓存、按 query 去重的改写缓存、
need_retrieve=false 时的追问兜底话术（由调用方决定）。
"""

import logging
from collections.abc import Sequence
from typing import Any, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, SecretStr

from app.core.config import settings
from app.services.chat_memory import compact_rewrite_context

logger = logging.getLogger("ChatQueryRewriteService")

__all__ = [
    "REWRITE_PROMPT",
    "ChatQueryRewriteService",
    "RetrieveQueryPlan",
    "RewriteResult",
    "chat_query_rewrite_service",
]

# 改写温度固定 0（对齐 TS `temperature: 0`）：改写要可复现，不要自由发挥
_REWRITE_TEMPERATURE = 0.0


class RewriteResult(BaseModel):
    """结构化改写结果（对应 TS zod `rewriteSchema`，字段名与描述逐字一致）"""

    standalone_query: str = Field(
        description="可独立检索的一句中文短查询，消解指代和省略，不含寒暄"
    )
    need_retrieve: bool = Field(
        description="是否需要查知识库。寒暄、致谢、与制度无关的闲聊为 false"
    )


# 改写系统提示词（与 TS REWRITE_PROMPT 逐字一致）
REWRITE_PROMPT = (
    "你是企业知识库的检索改写器。根据对话把当前问题改写成一条可独立检索的短查询。\n"
    "\n"
    "## 要求\n"
    "- 消解「这个 / 那个 / 谁负责 / 怎么办」等指代，补全省略的主题\n"
    "- 只保留检索需要的实体、事项、动作，一句中文，尽量不超过 40 字\n"
    "- 不要编造上文没出现的专有名词、条款号、系统名\n"
    "- 不要复述助手给出的制度条文、时限、流程细节\n"
    "- 当前问题已经完整、不依赖上文时，standalone_query 用原问题略作精炼即可\n"
    "- 寒暄、致谢、与知识库无关 → need_retrieve=false，standalone_query 仍给原问题\n"
    "- 输出不要解释"
)


class RetrieveQueryPlan(TypedDict):
    """改写后的检索方案（对应 TS `type RetrieveQueryPlan`）

    query          —— 实际送去检索的查询（改写成功用 LLM 结果，否则原问题）
    need_retrieve  —— TS `needRetrieve`：false 时调用方应跳过检索直接作答
    """

    query: str
    need_retrieve: bool


def _build_rewriter():
    """构建结构化改写器；未配置 API Key 返回 None（对齐 TS 构造函数早退）

    Key / baseURL / model 的 fallback 链与 ai_stream / chat_long_memory 一致：
    OPENAI_API_KEY → LLM_API_KEY → DASHSCOPE_API_KEY；
    OPENAI_BASE_URL → LLM_BASE_URL → DashScope 兼容端点；
    MODEL_NAME → LLM_MODEL → qwen-plus（后两个为 TS 字面量，本项目走 DeepSeek）。

    刻意不写返回类型注解——`with_structured_output` 的返回类型在 langchain 侧
    是个联合体，注解反而要 cast（与 kg_extraction / chat_long_memory 同款处理）。
    """
    api_key = (
        settings.OPENAI_API_KEY or settings.LLM_API_KEY or settings.DASHSCOPE_API_KEY
    )
    if not api_key:
        return None

    base_url = (
        settings.OPENAI_BASE_URL
        or settings.LLM_BASE_URL
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    model = settings.MODEL_NAME or settings.LLM_MODEL or "qwen-plus"
    timeout_ms = settings.AI_QUERY_REWRITE_TIMEOUT_MS
    timeout_s = timeout_ms / 1000 if timeout_ms > 0 else 15.0

    llm = ChatOpenAI(
        api_key=SecretStr(api_key),
        model=model,
        temperature=_REWRITE_TEMPERATURE,
        timeout=timeout_s,
        max_retries=0,
        # DeepSeek 走 Chat Completions 兼容层，不要切 OpenAI Responses API
        use_responses_api=False,
        base_url=base_url,
        # deepseek-v4-flash 默认 thinking 模式，不支持强制 tool_choice
        #（"Thinking mode does not support this tool_choice"），结构化输出必须关闭
        extra_body={"thinking": {"type": "disabled"}},
    )

    return llm.with_structured_output(
        RewriteResult,
        method="function_calling",
        # 不传 name：langchain-openai 会把 name 透传给 completions.create() 导致 TypeError
    )


class ChatQueryRewriteService:
    """检索前把追问改写成独立短查询。未配置 LLM 或改写失败时回退原文。"""

    def __init__(self) -> None:
        # 对应 TS 构造函数里的 `config.get(...)`：缺省值写在 config.py，此处直接读
        self._rewriter: Any | None = _build_rewriter()

    async def rewrite(
        self,
        question: str,
        history: Sequence[BaseMessage],
    ) -> RetrieveQueryPlan:
        """把当前问题改写成独立检索查询（对应 TS rewrite）

        没有历史或未配置 LLM 时不值得改写，直接回退原文；
        有历史但 `compact_rewrite_context` 压缩后为空（全空白消息）同样回退。
        LLM 调用失败只 WARN —— 改写失败不能阻断对话。
        """
        fallback: RetrieveQueryPlan = {"query": question, "need_retrieve": True}
        if not history or self._rewriter is None:
            return fallback

        context = compact_rewrite_context(history)
        if not context:
            return fallback

        try:
            result: RewriteResult = await self._rewriter.ainvoke(
                [
                    SystemMessage(REWRITE_PROMPT),
                    HumanMessage(f"对话：\n{context}\n\n当前问题：{question}"),
                ]
            )
            # TS `result.standalone_query.trim() || question`：模型给出空白时回退原文
            query = result.standalone_query.strip() or question
            logger.info(
                f"检索改写：needRetrieve={result.need_retrieve} query={query[:80]}"
            )
            return {"query": query, "need_retrieve": result.need_retrieve}
        except Exception as err:  # noqa: BLE001
            logger.warning(f"检索改写失败，使用原问题：{err}")
            return fallback


# 全局单例（对应 TS @Injectable 的单例语义）
chat_query_rewrite_service = ChatQueryRewriteService()

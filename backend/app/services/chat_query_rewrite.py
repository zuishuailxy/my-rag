"""检索前把追问改写成独立短查询 + 意图路由（对应 TS ai/chat-query-rewrite.service.ts）

角色：多轮对话里用户常问「那谁审批？」「这个怎么办？」这类依赖上文的追问，
直接拿去混合检索会召回一堆无关制度。本服务做三件事：
  1. **查询改写 + 图谱短实体名**：只有依赖上文的追问才调一次 LLM，同时产出
     「可独立检索的短查询 standalone_query」与「图谱用的短实体名 graph_queries」；
     自足问题不调（图谱词由模型在 retrieve_graph 入参里现抽）。是否改写先用 TypeSafe
     判「自足 / 依赖上文 / 无需检索」（TypeSafe 判别式，生成不了实体名）；
  2. **意图路由**：判断本轮意图（chitchat / profile / kb / web / kb_then_web），
     据此给出工具开关（allow_retrieve / allow_web）——这是 agentic RAG 的「决策层」；
  3. **切题判定 + 不足改写**：检索后判断召回文档与问题是否切题（TypeSafe 的两路
     Choice，未配 TypeSafe 时回退 LLM 结构化判定）；不切题/空命中时用 LLM 换一种
     问法改写检索词（`rewrite_after_retrieve`），失败则回退原问题。

分工（路线 A：TypeSafe 管决策、LLM 管改写）：
  - **意图识别走 TypeSafe**（结构化判定 SaaS，`system_one(state=..., questions=...)`
    返回类型化结果，非 OpenAI 兼容 LLM）。只用一个 `Choice` 原语做 5 路分类。
  - **查询改写仍走生成式 LLM**（deepseek-v4-flash）：TypeSafe 的 Noul/Choice/Score
    原语是判别式的，生成不了自由文本，所以 `standalone_query` 不能交给它。
  - 回退：TypeSafe 未安装 / 未配 key / 调用失败 → 用 LLM 的 `need_retrieve`
    推导意图（True→kb，False→chitchat），再不行落到 kb。

回退语义（与 TS 一致）：未配置 LLM Key、没有历史、上下文为空、任一调用失败，
都回退 `{query: 原问题, need_retrieve: True}` / `intent=kb` —— 改写是增强，不是必需
环节，任何失败都不能让用户问不出结果。

刻意差异（相对 TS）：
- **结构化输出改用 `method="function_calling"` 且关闭 thinking**：TS 用
  `withStructuredOutput(rewriteSchema)` 走所选模型（qwen-plus）默认的 jsonSchema 通道；
  本项目 LLM 是 **deepseek-v4-flash**，不支持 json_schema 严格模式、也不接受带 name 的
  tool_choice（详见 kg_extraction.py / chat_long_memory.py 同类注释）。故显式指定
  function_calling + `extra_body={"thinking": {"type": "disabled"}}`。
- **TypeSafe 依赖是本次新增**：SDK 未进 requirements，`_load_typesafe()` 延迟导入，
  失败只 WARN 并回退 LLM，不影响模块加载。
- **切题判定在本项目优先 TypeSafe（TS 用 LLM `gradeSchema`）**：TS 的 `gradeHits` /
  `rewriteAfterRetrieve` 都走 `withStructuredOutput`；本项目把切题判定换成 TypeSafe
  两路 Choice（自己加的优化），LLM 仅作回退；改写仍是 LLM（`retryRewriteSchema`）。
  对调用方的 `HitGrade` 契约一致。
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
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, SecretStr

from app.core.config import settings
from app.pipeline.types import ChunkHit
from app.services.chat_memory import compact_rewrite_context

logger = logging.getLogger("ChatQueryRewriteService")

# 内部 LLM 调用的隔离配置（对应 TS invokeIsolated）
# 工具里再 invoke 评估/改写模型时，默认会继承 agent 的 callbacks，内部结构化 JSON 会进
# messages 流、变成回答正文。显式传空 callbacks 摘掉 StreamMessagesHandler
#（实测 ensure_config 对 callbacks 是**覆盖**而非合并），tags 作消费端兜底过滤。
# 见 chat_query_rewrite.py / ai_stream.py 的 kh-internal 过滤。
KH_INTERNAL_TAG = "kh-internal"
_INTERNAL_CALL_CONFIG: dict[str, Any] = {
    "callbacks": [],
    "tags": [KH_INTERNAL_TAG],
    "metadata": {"khInternal": True},
}

__all__ = [
    "CHAT_INTENTS",
    "ChatIntent",
    "ChatRoutePlan",
    "INTENT_LABEL",
    "RELEVANCE_CHOICES",
    "RELEVANCE_PROMPT",
    "RETRY_REWRITE_PROMPT",
    "REWRITE_GATE_CHOICES",
    "REWRITE_PROMPT",
    "KH_INTERNAL_TAG",
    "ChatQueryRewriteService",
    "HitGrade",
    "RelevanceResult",
    "RetrieveQueryPlan",
    "RetryRewriteResult",
    "RewriteResult",
    "chat_query_rewrite_service",
]

# 改写温度固定 0（对齐 TS `temperature: 0`）：改写要可复现，不要自由发挥
_REWRITE_TEMPERATURE = 0.0

# 命中资料正文送入评估/改写的截断长度。
# ⚠️ **刻意偏离 TS summarizeHits 的 180**：中文 chunk 约 700~1000 字，关键句常在中后段
#（实测「私车公用」在 759 字 chunk 的第 644 字），截 180 会让评估器只看到 chunk 开头的
# 「差旅管理办法」标题，误判不切题 → 合法命中被丢 → stream 说「知识库无内容」。
# 放到能覆盖整块，评估/改写才看得到真正相关的内容。
_RELEVANCE_SNIPPET_LEN = 1200

# 本轮路由意图（对应 TS CHAT_INTENTS 字面量联合）
CHAT_INTENTS = ("chitchat", "profile", "kb", "web", "kb_then_web")
ChatIntent = Literal["chitchat", "profile", "kb", "web", "kb_then_web"]

# 意图在过程条上的中文名（对应 TS INTENT_LABEL）
INTENT_LABEL: dict[ChatIntent, str] = {
    "chitchat": "闲聊",
    "profile": "个人偏好",
    "kb": "知识库",
    "web": "联网搜索",
    "kb_then_web": "知识库，不足则联网",
}

# TypeSafe 意图识别的 Choice 指令（判别式，只分类、不生成自由文本）
_INTENT_INSTRUCTIONS = (
    "判断用户本轮问题的意图。"
    "chitchat=寒暄闲聊；profile=只改用户自己的名字/偏好；"
    "kb=公司制度、流程、岗位、内部系统、文档；"
    "web=只要最新公开信息；"
    "kb_then_web=像内部问题但也可能要外部时效。"
)

# TypeSafe 切题判定的两路 Choice（判别式：切题 / 不切题）
RELEVANCE_CHOICES = ("relevant", "irrelevant")

# TypeSafe 切题判定的 Choice 指令
_RELEVANCE_INSTRUCTIONS = (
    "判断「检索到的资料」是否与「用户问题」切题。"
    "relevant=资料主题与问题一致，且包含能回答问题的关键信息；"
    "irrelevant=资料讲的是别的事、答非所问，或与问题无关。"
)

# TypeSafe「本轮问题是否需要改写 / 检索」的三路 Choice
REWRITE_GATE_CHOICES = ("standalone", "needs_context", "no_retrieve")

# TypeSafe 改写门控指令（廉价判别：自足 / 依赖上文 / 无需检索）
_REWRITE_GATE_INSTRUCTIONS = (
    "判断用户本轮问题该怎么处理。"
    "standalone=问题已完整，脱离对话历史也能独立检索，"
    "不含「这个/那个/那谁/它」等指代或省略；"
    "needs_context=问题依赖上文，含指代或省略，"
    "必须结合对话历史才能确定检索什么；"
    "no_retrieve=寒暄、致谢、与知识库无关，无需检索。"
)


class RewriteResult(BaseModel):
    """结构化改写结果（对应 TS zod `rewriteSchema`，字段名与描述逐字一致）"""

    standalone_query: str = Field(
        description="可独立检索的一句中文短查询，消解指代和省略，不含寒暄"
    )
    need_retrieve: bool = Field(
        description="是否需要查知识库。寒暄、致谢、与制度无关的闲聊为 false"
    )
    graph_queries: list[str] = Field(
        default_factory=list,
        description=(
            "图谱短实体名：2~6 个、每个 2~4 字，必须拆开（发票如何报销→发票、报销）；"
            "寒暄/偏好/纯联网为空数组"
        ),
    )


class RelevanceResult(BaseModel):
    """检索后切题判定结果（判断召回文档与问题是否相关，kb_then_web 的决策点）"""

    relevant: bool = Field(
        description=(
            "召回的文档是否切题、能否支撑回答当前问题。"
            "切题为 true，答非所问/无关为 false"
        )
    )
    reason: str = Field(
        description="一句中文说明判定理由（切题与否的依据）"
    )


class RetryRewriteResult(BaseModel):
    """不足后改写检索词（对应 TS zod `retryRewriteSchema`：字段名 query）"""

    query: str = Field(
        description="与上次不同的一句中文检索词，不超过 40 字，针对用户原问题"
    )


HitGradeReason = Literal["relevant", "irrelevant", "empty"]


@dataclass
class HitGrade:
    """检索结果切题评估（对应 TS `HitGrade`，供 agentic-retrieve 用）

    ok     —— 是否可当制度依据（不是「有没有条数」）
    reason —— relevant / irrelevant / empty
    text   —— 给过程条看的一句中文
    """

    ok: bool
    reason: HitGradeReason
    text: str


# 改写 + 图谱短实体名系统提示词（对齐 TS ROUTE_PROMPT 的 standalone_query / graph_queries 部分）
REWRITE_PROMPT = (
    "你是企业知识库的检索改写器。根据对话把当前问题改写成一条可独立检索的短查询，"
    "并抽出图谱要用的短实体名。\n"
    "\n"
    "## standalone_query\n"
    "- 消解「这个 / 那个 / 谁负责 / 怎么办」等指代，补全省略的主题\n"
    "- 只保留检索需要的实体、事项、动作，一句中文，尽量不超过 40 字\n"
    "- 不要编造上文没出现的专有名词、条款号、系统名\n"
    "- 不要复述助手给出的制度条文、时限、流程细节\n"
    "- 当前问题已经完整、不依赖上文时，standalone_query 用原问题略作精炼即可\n"
    "- 寒暄、致谢、与知识库无关 → need_retrieve=false，standalone_query 仍给原问题\n"
    "\n"
    "## graph_queries（图谱短实体名）\n"
    "- **只从当前问题的主题抽**：可结合上文消解「这个/那个/它」的指代，"
    "但**不要带入上文其它话题的实体**（上一轮问私车公用、本轮问出差申请，就只给「出差申请」相关）\n"
    "- 每个词 2～4 个字的实体短名（人/岗/制度对象），不要整句、不要问号\n"
    "- 必须拆开：发票如何报销 → 发票、报销；发票报销流程和要求 → 发票、报销\n"
    "- 禁止：流程、要求、办法、如何、怎么、什么、是什么\n"
    "- 寒暄、偏好、纯联网 → 空数组\n"
    "- 输出不要解释"
)


# 切题评估提示词（TypeSafe 不可用时的 LLM 回退，对应 TS GRADE_PROMPT）
RELEVANCE_PROMPT = (
    "你是企业知识库的检索评估器。判断命中资料能否回答用户问题。\n"
    "\n"
    "- 切题：同一主题、同一制度/岗位/流程，能支撑作答（不必覆盖每个细节）\n"
    "- 不切题：只是词沾边（如问加班餐补却命中差旅报销）、或完全另一件事\n"
    "- 只根据给定标题和摘录判断，不要假设库里还有别的文档\n"
    "- 输出不要解释字段以外的内容"
)


# 不足后换一种问法再查（对应 TS RETRY_REWRITE_PROMPT，empty / irrelevant 共用）
RETRY_REWRITE_PROMPT = (
    "你是企业知识库的检索改写器。上次检索不足，请换一种问法再查。\n"
    "\n"
    "- 紧扣用户原问题，不要跑题\n"
    "- 不要重复上次检索词\n"
    "- 可换同义、补全制度/岗位/补贴类型等核心实体\n"
    "- 不要编造条款号、专有名词\n"
    "- 一句中文，尽量不超过 40 字"
)


class RetrieveQueryPlan(TypedDict):
    """改写后的检索方案（对应 TS `type RetrieveQueryPlan`）

    query          —— 实际送去检索的查询（改写成功用 LLM 结果，否则原问题）
    need_retrieve  —— TS `needRetrieve`：false 时调用方应跳过检索直接作答
    """

    query: str
    need_retrieve: bool


class ChatRoutePlan(TypedDict):
    """本轮意图 + 工具开关（对应 TS `type ChatRoutePlan`）

    intent         —— 5 路意图
    label          —— 意图中文名（过程条展示）
    query          —— 改写后的检索词（给 RAG / 联网用）
    graph_queries  —— 图谱用的短实体名（整句对不上图节点）
    allow_retrieve —— kb / kb_then_web 才挂 retrieve_knowledge
    allow_graph    —— 与 allow_retrieve 相同：查知识库的轮次才查图谱
    allow_web      —— web / kb_then_web 才挂 web_search
    """

    intent: ChatIntent
    label: str
    query: str
    graph_queries: list[str]
    allow_retrieve: bool
    allow_graph: bool
    allow_web: bool


def _build_llm() -> ChatOpenAI | None:
    """构建 DeepSeek 兼容 LLM（改写 / 切题判定共用）；未配置 API Key 返回 None

    Key / baseURL / model 的 fallback 链与 ai_stream / chat_long_memory 一致：
    OPENAI_API_KEY → LLM_API_KEY → DASHSCOPE_API_KEY；
    OPENAI_BASE_URL → LLM_BASE_URL → DashScope 兼容端点；
    MODEL_NAME → LLM_MODEL → qwen-plus（后两个为 TS 字面量，本项目走 DeepSeek）。
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

    return ChatOpenAI(
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


def _build_rewriter():
    """构建结构化改写器（RewriteResult）；未配置 API Key 返回 None

    刻意不写返回类型注解——`with_structured_output` 的返回类型在 langchain 侧
    是个联合体，注解反而要 cast（与 kg_extraction / chat_long_memory 同款处理）。
    """
    llm = _build_llm()
    if llm is None:
        return None
    return llm.with_structured_output(
        RewriteResult,
        method="function_calling",
        # 不传 name：langchain-openai 会把 name 透传给 completions.create() 导致 TypeError
    )


def _build_relevance_judge():
    """构建切题判定器（RelevanceResult）；未配置 API Key 返回 None（同 _build_rewriter）"""
    llm = _build_llm()
    if llm is None:
        return None
    return llm.with_structured_output(
        RelevanceResult,
        method="function_calling",
        # 不传 name：同上，langchain-openai 会把 name 透传导致 TypeError
    )


def _build_retry_writer():
    """构建不足后改写器（RetryRewriteResult）；未配置 API Key 返回 None（同 _build_rewriter）"""
    llm = _build_llm()
    if llm is None:
        return None
    return llm.with_structured_output(
        RetryRewriteResult,
        method="function_calling",
        # 不传 name：同上，langchain-openai 会把 name 透传导致 TypeError
    )


def summarize_hits(hits: Sequence[ChunkHit], limit: int = 5) -> str:
    """把命中资料拼成摘要（对应 TS summarizeHits：序号 + 标题/章节 + 分数 + 摘要）

    截断长度用 `_RELEVANCE_SNIPPET_LEN`（刻意比 TS 长，见该常量注释）。
    """
    lines = []
    for i, hit in enumerate(hits[:limit], start=1):
        heading = f" / {hit.heading}" if hit.heading else ""
        snippet = re.sub(r"\s+", " ", hit.content).strip()[:_RELEVANCE_SNIPPET_LEN]
        score = f"{hit.score:.2f}" if math.isfinite(hit.score) else "-"
        lines.append(f"{i}. {hit.documentTitle}{heading}（分={score}）\n{snippet}")
    return "\n".join(lines)


def distinct_query(next_query: str, previous: str) -> str:
    """归一空白后返回新查询；为空或与上次相同时返回空串（对应 TS distinctQuery）"""
    query = re.sub(r"\s+", " ", next_query).strip()
    if not query:
        return ""
    if query == re.sub(r"\s+", " ", previous).strip():
        return ""
    return query


def _load_typesafe() -> tuple[Any, Any] | None:
    """延迟导入 typesafe_sdk；未配 key 或未安装返回 None

    返回 (AsyncTypeSafeClient, Choice)，供 `_route_intent` 构造与调用。
    TypeSafe 是硬外部依赖（未进 requirements），必须容错：import 失败只 WARN，
    意图识别回退 LLM，不影响本模块加载。
    """
    if not settings.TYPESAFE_API_KEY:
        return None
    try:
        from typesafe_sdk import AsyncTypeSafeClient, Choice  # noqa: PLC0415
    except ImportError:
        logger.warning("typesafe_sdk 未安装，意图识别回退 LLM")
        return None
    return AsyncTypeSafeClient, Choice


class ChatQueryRewriteService:
    """查询改写（LLM）+ 意图路由（TypeSafe）。任一环节失败都回退原文，不阻断对话。"""

    def __init__(self) -> None:
        # 对应 TS 构造函数里的 `config.get(...)`：缺省值写在 config.py，此处直接读
        self._rewriter: Any | None = _build_rewriter()
        self._relevance_judge: Any | None = _build_relevance_judge()
        self._retry_writer: Any | None = _build_retry_writer()
        loaded = _load_typesafe()
        self._typesafe_client_cls: Any | None = loaded[0] if loaded else None
        self._typesafe_choice_cls: Any | None = loaded[1] if loaded else None

    async def classify(
        self,
        question: str,
        history: Sequence[BaseMessage],
    ) -> ChatRoutePlan:
        """意图路由 + 查询改写（agentic RAG 入口，对应 TS classify）

        分工（路线 A）：
          - TypeSafe 判 intent（5 路 Choice）；失败/未配置时用 LLM 的
            need_retrieve 推导（True→kb，False→chitchat），再不行落到 kb；
          - LLM 改写 standalone_query（生成式，TypeSafe 做不了自由文本）。
        与 rewrite() 的区别：返回完整 ChatRoutePlan（intent + 工具开关），
        rewrite() 只返回 {query, need_retrieve} 供现有流式链路用。
        """
        context = compact_rewrite_context(history)

        # 1. 意图：优先 TypeSafe，None 表示不可用（后面用 LLM 推导兜底）
        intent = await self._route_intent(question, context)

        # 2. 只有依赖上文的追问（gate=needs_context / 门控不可用）才调一次 LLM，
        #    同时产出 standalone_query + graph_queries。
        #    自足问题不调：query 用原问题，图谱短实体名交给模型在 retrieve_graph
        #    入参里现抽（retrieve_graph 工具照常挂载，见 ai_stream.create_turn_agent）。
        query = question
        graph_queries: list[str] = []
        llm_need_retrieve: bool | None = None
        if self._rewriter is not None:
            gate = (
                await self._route_rewrite_gate(question) if context else "standalone"
            )
            if gate == "no_retrieve":
                llm_need_retrieve = False
            elif gate != "standalone":
                result = await self._invoke_rewriter(question, context)
                if result is not None:
                    query = result.standalone_query.strip() or question
                    llm_need_retrieve = result.need_retrieve
                    graph_queries = result.graph_queries

        if intent is None:
            intent = "kb" if llm_need_retrieve is not False else "chitchat"
            logger.info(
                "意图识别回退(LLM need_retrieve=%s)：intent=%s",
                llm_need_retrieve,
                intent,
            )

        plan = self._to_plan(intent, query, graph_queries)
        logger.info(
            "意图路由：question=%r → intent=%s query=%r graph=%s "
            "allow_retrieve=%s allow_graph=%s allow_web=%s",
            question[:80],
            plan["intent"],
            plan["query"][:80],
            "/".join(plan["graph_queries"]) or "-",
            plan["allow_retrieve"],
            plan["allow_graph"],
            plan["allow_web"],
        )
        return plan

    async def rewrite(
        self,
        question: str,
        history: Sequence[BaseMessage],
    ) -> RetrieveQueryPlan:
        """把当前问题改写成独立检索查询（对应 TS rewrite）

        ⚠️ 与 TS 一致：`rewrite` 只是 `classify` 的**薄封装**，只取「检索词 + 要不要检索」，
        给非流式 /ai/chat 用；流式链路直接用 `classify()`（还要 intent / 工具开关）。
        改写逻辑（TypeSafe 门控 + LLM 消解指代）全部在 `classify()` 里，只此一处，避免漂移。
        """
        plan = await self.classify(question, history)
        return {"query": plan["query"], "need_retrieve": plan["allow_retrieve"]}

    async def check_relevance(
        self,
        question: str,
        hits: Sequence[ChunkHit],
    ) -> RelevanceResult | None:
        """判断召回的文档与问题是否切题（kb_then_web 的决策点）

        优先用 TypeSafe（jev）的两路 Choice —— 判定是判别式任务，正是 Choice 的用武
        之地，且比生成式 LLM 便宜快；TypeSafe 未配 / 未装 / 调用失败时回退 LLM 结构化
        判定。无召回或两条路都不可用时返回 None —— 调用方按「切题」处理：判定是增强
        环节，任何失败都不能阻断作答。
        """
        if not hits:
            return None
        context = summarize_hits(hits)
        if not context:
            return None

        # 1. 优先 TypeSafe（判别式判定）
        result = await self._route_relevance(question, context)
        if result is not None:
            return result

        # 2. 回退 LLM 结构化判定
        return await self._judge_relevance_llm(question, context)

    async def _route_relevance(
        self, question: str, context: str
    ) -> RelevanceResult | None:
        """TypeSafe 切题判定；SDK 未装 / 未配 key / 调用失败均返回 None

        与 `_route_intent` 同一套调用方式，只是原语换成两路 Choice；顺带用
        confidence 记录到 reason，便于观测判定把握。
        """
        if self._typesafe_client_cls is None or self._typesafe_choice_cls is None:
            return None
        try:
            async with self._make_typesafe_client() as client:
                response = await client.system_one(
                    state={"question": question, "documents": context},
                    questions={
                        "relevance": self._typesafe_choice_cls(
                            instructions=_RELEVANCE_INSTRUCTIONS,
                            criteria={name: None for name in RELEVANCE_CHOICES},
                        ),
                    },
                )
            answer = response.choices["relevance"]
        except Exception as err:  # noqa: BLE001
            logger.warning("TypeSafe 切题判定失败，回退 LLM：%s", err)
            return None

        if answer.choice not in RELEVANCE_CHOICES:
            return None
        relevant = answer.choice == "relevant"
        result = RelevanceResult(
            relevant=relevant,
            reason="资料与问题相关" if relevant else "资料与问题不相关",
        )
        logger.info(
            "切题判定(TypeSafe)：question=%r → relevant=%s confidence=%.2f",
            question[:80],
            relevant,
            answer.confidence,
        )
        return result

    async def _judge_relevance_llm(
        self, question: str, context: str
    ) -> RelevanceResult | None:
        """LLM 结构化切题判定（TypeSafe 不可用时的回退）；调用失败返回 None"""
        if self._relevance_judge is None:
            return None
        try:
            result = await self._relevance_judge.ainvoke(
                [
                    SystemMessage(RELEVANCE_PROMPT),
                    HumanMessage(f"用户问题：{question}\n\n命中资料：\n{context}"),
                ],
                config=_INTERNAL_CALL_CONFIG,
            )
        except Exception as err:  # noqa: BLE001
            logger.warning("切题判定(LLM)失败，按切题处理：%s", err)
            return None
        logger.info(
            "切题判定(LLM)：question=%r → relevant=%s reason=%s",
            question[:80],
            result.relevant,
            (result.reason or "")[:80],
        )
        return result

    async def grade_hits(
        self, question: str, hits: Sequence[ChunkHit]
    ) -> HitGrade:
        """检索结果切题评估（对应 TS gradeHits，返回 HitGrade 给 agentic-retrieve）

        空召回 → empty；否则走 TypeSafe/LLM 判定；判定不可用（未配/失败）时按切题
        放行（ok=True）—— 判定是增强，不阻断作答。
        """
        if not hits:
            return HitGrade(ok=False, reason="empty", text="未检索到资料")
        result = await self.check_relevance(question, hits)
        if result is None:
            return HitGrade(
                ok=True, reason="relevant", text="有召回（评估不可用，按切题放行）"
            )
        text = (result.reason or "").strip()
        if result.relevant:
            return HitGrade(ok=True, reason="relevant", text=text or "资料切题")
        return HitGrade(ok=False, reason="irrelevant", text=text or "资料不切题")

    async def rewrite_after_retrieve(
        self,
        question: str,
        previous_query: str,
        grade: HitGrade,
        hits: Sequence[ChunkHit],
    ) -> str:
        """首轮空 / 不切题后改写检索词（对应 TS rewriteAfterRetrieve）

        返回新检索词；失败或与上次相同则回退用户原问题；原问题也与上次相同时返回
        空串（表示无法再查）—— 与 TS 一致，**永不返回 None**。
        """
        fallback = distinct_query(question, previous_query)
        if self._retry_writer is None:
            return fallback
        hit_block = (
            f"\n上次命中（不切题）：\n{summarize_hits(hits, 3)}"
            if hits
            else "\n上次无命中。"
        )
        try:
            result = await self._retry_writer.ainvoke(
                [
                    SystemMessage(RETRY_REWRITE_PROMPT),
                    HumanMessage(
                        f"用户问题：{question}\n上次检索词：{previous_query}\n"
                        f"不足原因：{grade.text}{hit_block}"
                    ),
                ],
                config=_INTERNAL_CALL_CONFIG,
            )
        except Exception as err:  # noqa: BLE001
            logger.warning("检索改写失败，回退原问题：%s", err)
            return fallback
        query = distinct_query(result.query, previous_query)
        if not query:
            return fallback
        logger.info("检索改写：%s → %s", previous_query[:40], query[:40])
        return query

    async def _invoke_rewriter(
        self, question: str, context: str
    ) -> RewriteResult | None:
        """调 LLM 做查询改写；未配置 LLM 或调用失败返回 None（调用方回退原问题）"""
        if self._rewriter is None:
            return None
        try:
            result = await self._rewriter.ainvoke(
                [
                    SystemMessage(REWRITE_PROMPT),
                    HumanMessage(f"对话：\n{context}\n\n当前问题：{question}"),
                ]
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(f"检索改写失败，使用原问题：{err}")
            return None
        logger.info(
            "改写(LLM)：question=%r → standalone_query=%r need_retrieve=%s",
            question[:80],
            result.standalone_query[:80],
            result.need_retrieve,
        )
        return result

    async def _route_intent(
        self, question: str, context: str
    ) -> ChatIntent | None:
        """TypeSafe 意图识别；SDK 未装 / 未配 key / 调用失败均返回 None

        TypeSafe 是结构化判定 SaaS：`system_one(state=..., questions=...)` 返回
        类型化结果，与 OpenAI 兼容 LLM 是两套协议（无 baseURL / tool calling）。
        这里只用它的 Choice 原语做 5 路分类；自由文本改写仍走生成式 LLM。
        """
        if self._typesafe_client_cls is None or self._typesafe_choice_cls is None:
            return None

        state: dict[str, str] = {"question": question}
        if context:
            state["history"] = context

        try:
            async with self._make_typesafe_client() as client:
                response = await client.system_one(
                    state=state,
                    questions={
                        "intent": self._typesafe_choice_cls(
                            instructions=_INTENT_INSTRUCTIONS,
                            criteria={name: None for name in CHAT_INTENTS},
                        ),
                    },
                )
            answer = response.choices["intent"]
        except Exception as err:  # noqa: BLE001
            logger.warning("TypeSafe 意图识别失败，回退 LLM：%s", err)
            return None

        # 模型返回不在枚举内（SDK 异常 / 加了新意图）时同样视作不可用
        if answer.choice not in CHAT_INTENTS:
            logger.warning(
                "TypeSafe 意图识别返回未知意图：question=%r → %r",
                question[:80],
                answer.choice,
            )
            return None
        logger.info(
            "意图识别(TypeSafe)：question=%r → intent=%s confidence=%.2f probabilities=%s",
            question[:80],
            answer.choice,
            answer.confidence,
            answer.probabilities,
        )
        return answer.choice

    async def _route_rewrite_gate(self, question: str) -> str | None:
        """TypeSafe 判「本轮是否需要改写 / 检索」；不可用 / 失败返回 None

        返回 "standalone"=自足，直接用原问题；"needs_context"=依赖上文，需 LLM 消解
        指代；"no_retrieve"=无需检索（寒暄等）。None 表示判不了，调用方走 LLM 兜底
        （即保守地当作需要改写）。
        """
        if self._typesafe_client_cls is None or self._typesafe_choice_cls is None:
            return None
        try:
            async with self._make_typesafe_client() as client:
                response = await client.system_one(
                    state={"question": question},
                    questions={
                        "rewrite_gate": self._typesafe_choice_cls(
                            instructions=_REWRITE_GATE_INSTRUCTIONS,
                            criteria={name: None for name in REWRITE_GATE_CHOICES},
                        ),
                    },
                )
            answer = response.choices["rewrite_gate"]
        except Exception as err:  # noqa: BLE001
            logger.warning("TypeSafe 改写门控失败，回退 LLM 改写：%s", err)
            return None

        if answer.choice not in REWRITE_GATE_CHOICES:
            logger.warning(
                "TypeSafe 改写门控返回未知选项：question=%r → %r",
                question[:80],
                answer.choice,
            )
            return None
        logger.info(
            "改写门控(TypeSafe)：question=%r → %s confidence=%.2f",
            question[:80],
            answer.choice,
            answer.confidence,
        )
        return answer.choice

    def _make_typesafe_client(self) -> Any:
        """构造 TypeSafe 客户端（构造签名以 SDK 官方为准）

        优先显式传 api_key（本项目惯例）；个别 SDK 版本只认环境变量时会抛
        TypeError，此时退回无参构造（靠进程环境变量 TYPESAFE_API_KEY）。

        ⚠️ 必须显式判 None：`_typesafe_client_cls` 的注解是 `Any | None`，
        `Optional[Any]` **不可调用**（mypy 报 `"None" not callable`）。三个调用点
        虽都在外面判过 `is None`，但那不收窄本方法里的属性类型；在这里判一次既不
        改变行为（判过 None 的调用点永远走不到 raise），也让「直接调本方法」的路径
        得到明确错误而不是 TypeError。
        """
        cls = self._typesafe_client_cls
        if cls is None:
            raise RuntimeError("TypeSafe 客户端不可用（未安装 SDK 或未配置 key）")
        try:
            return cls(api_key=settings.TYPESAFE_API_KEY)
        except TypeError:
            return cls()

    @staticmethod
    def _to_plan(
        intent: ChatIntent, query: str, graph_queries: list[str] | None = None
    ) -> ChatRoutePlan:
        """意图 → 工具开关（对应 TS toPlan）

        allow_retrieve / allow_graph：kb / kb_then_web 才挂 retrieve_knowledge /
        retrieve_graph；allow_web：web / kb_then_web 才挂 web_search。
        graph_queries 仅 kb 轮次保留（其余意图一律清空）。
        识别失败时 classify 已落到 kb，避免内部问题漏检。
        """
        kb = intent in ("kb", "kb_then_web")
        return {
            "intent": intent,
            "label": INTENT_LABEL[intent],
            "query": query,
            "graph_queries": (
                [q.strip() for q in (graph_queries or []) if q.strip()] if kb else []
            ),
            "allow_retrieve": kb,
            "allow_graph": kb,
            "allow_web": intent in ("web", "kb_then_web"),
        }


# 全局单例（对应 TS @Injectable 的单例语义）
chat_query_rewrite_service = ChatQueryRewriteService()

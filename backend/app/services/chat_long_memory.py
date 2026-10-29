"""对话长期记忆

角色：跨会话的「用户长期记忆」+ 仅本会话的「会话记忆」。记忆**只用于改写问题、
补上下文，不作制度事实来源** —— 拼出的 SystemMessage 末尾明确要求以本轮检索资料为准。

启用条件：配置 MEM0_API_KEY。未配置时 `enabled=False`，所有方法直接返回空/跳过。

### 两道工序用的模型不同（这是本模块最容易误解的一点）
1. **分类器**（本进程内跑）：判断本轮该写 user 层、session 层、两层、还是都不写。
   两种实现，由 `MEMORY_CLASSIFIER` 选择：
   - `jev`（推荐，配 TYPESAFE_API_KEY 时 `auto` 即走它）：TypeSafe **Noul × 2**，
     每层给独立的 P(是)，可分层设阈值（user 严、session 松）；**不产自由文本**，
     理由由概率拼出。
   - `llm`：ChatOpenAI + `with_structured_output`，走与 ai_chat 相同的 fallback 链
     （OPENAI_API_KEY → LLM_API_KEY → DASHSCOPE_API_KEY、OPENAI_BASE_URL → LLM_BASE_URL、
     MODEL_NAME → LLM_MODEL），本项目里即 **deepseek-flash**。
   - `shadow`：两边都跑、只记日志，实际按 LLM 结果写入（校准 Jev 阈值用）。
   为什么用两个 Noul 而不是一个 4 路 Choice：user / session 是两个**独立维度**
   （跨会话事实 vs 本轮上下文），Noul 保留每层独立概率、可分层设阈值、扩展第三层只需
   再加一个 Noul；「都不写」= 两个都不过阈值，天然覆盖。
2. **记忆抽取**（Mem0 云侧跑）：`client.add()` 由平台完成事实抽取，**SDK 不暴露模型参数**，
   只能通过 `custom_instructions` 约束输出。⚠️ 也就是说「抽取」用的**不是** deepseek —— 这是
   选托管方案的固有代价。若要让抽取也走自有模型，必须改 Mem0 OSS
   自托管（`Memory.from_config({"llm": ..., "embedder": ..., "vector_store": ...})`），
，需另行决策。

### 刻意差异
- Python 的同步版是阻塞 HTTP —— 本服务位于流式问答的请求路径上，直接用同步版会卡住事件循环。
  异步版语义对齐且不阻塞。
- **实体 ID 的传法三个方法各不相同（必须按方法区分，不能统一）**：mem0ai 2.0.20 对应 v3
  云 API，三个端点对实体 ID 的位置要求不一致，且**SDK 的类型注释是错的**（`AddMemoryOptions` /
  `DeleteAllMemoryOptions` 的 docstring 都写「identity fields must be passed inside the filters
  dict」，实测服务端会直接 400）。实测定论（2026-09-18，真实 API key 逐条跑过）：
  - `search(q, filters={'user_id': ...}, top_k=n)` —— **必须放 filters**；放顶层会被 SDK 的
    `ENTITY_PARAMS` 白名单校验直接 `raise ValueError`（客户端拦，不发出请求）。
  - `add(msgs, user_id=..., run_id=..., custom_instructions=...)` —— **必须放顶层**；
    传 `filters={'user_id': ...}` 时 SDK 不拦，但服务端 400
    「At least one entity ID is required (user_id, agent_id, app_id, or run_id).」
  - `delete_all(user_id=..., run_id=...)` —— **必须放顶层**。它还额外有个更隐蔽的坑：
    `delete_all` 走的是 DELETE + **query string**，`_prepare_params` 只做 None 过滤不做 JSON
    序列化，dict 会被 httpx 用 `str()` 塞进 URL（`filters={'user_id': +'990...'}`，单引号），
    服务端连 JSON 都解析不出来 → 400「At least one filter ... is required」。
(msgs, { userId, runId })` / `deleteAll({ userId, runId })` 在这里必须改成
  顶层关键字参数。
（见该方法 docstring 的实测数据）：当前云 API 的
  `delete_all(user_id, run_id)` 会**忽略 run_id、清空整个 user 记忆**，等于「删一个会话抹掉
  该用户全部长期记忆」。这里改为 `get_all(filters={user_id, run_id})` 列出 id 后逐条
  `delete(id)`——run_id 过滤与逐条删除都实测有效。
- **`add()` 是异步抽取，写完不能立刻读到**：返回体是 `{'event_id':..., 'status':'PENDING'}`，
  云侧后台才做事实抽取与向量化。实测同一进程内立即 `search` 命中 0 条，轮询到第 3 次
  （约 6~9 秒）才出现。这不是 bug 而是平台特性——对生产无影响（记忆是给**后续轮次**用的），
  但意味着：① 冒烟必须轮询等待，不能断言「写完立刻可检索」；② 任何「本轮写入本轮立即读回」
  的设计都不成立。**`custom_instructions` 约束抽取口径**，且**必须按层区分**：
   user 层只抽身份/岗位/长期偏好/持久约束，session 层只抽本轮任务/待办/临时约定
   （见 `_EXTRACT_USER_INSTRUCTIONS` / `_EXTRACT_SESSION_INSTRUCTIONS`）——分类器只决定
   「写哪层」，抽取 LLM 并不知道自己在写哪层，不分开写就会重复抽取 + 跨层污染。
- **session 层过滤写法**：[{user_id}, {run_id}] }`，此处用等价的
  多键平铺 `{'user_id': ..., 'run_id': ...}`（mem0 的 filters 多键即隐式 AND；OSS 侧
  `memory/main.py` 文档也以 `{"AND": [f1, f2]}` 表述同一语义）。
- **响应是 dict 而非对象**：[].memory` → Python `res["results"][]["memory"]`，
  取值前逐层兜底（平台返回结构变动时退化为空列表而不是抛异常）。
- **结构化输出用 `method="function_calling"` 且不传 `name`**：DeepSeek 不支持 json_schema
  严格模式，也不接受带 name 的 tool_choice（详见 kg_extraction.py 的同类注释）。
- **分类器用 `ainvoke`**：本模块整体是 async 路径，不阻塞事件循环。
- `MEM0_TOP_K` / 实体 ID 一律 `str()` 归一：雪花 ID 在本项目里 int / str 两种形态并存，
  不归一会让「同一个用户」在 Mem0 侧裂成两个身份。

未实现：记忆的增删改查 API、按 team 维度隔离、记忆过期策略、
分类器结果落库审计。
"""

import asyncio
import logging
import time
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from mem0 import AsyncMemoryClient
from mem0.exceptions import RateLimitError
from pydantic import BaseModel, Field, SecretStr

from app.core.config import settings
from app.services.llm_budget import usage_callbacks

logger = logging.getLogger("ChatLongMemoryService")

__all__ = [
    "ChatLongMemoryService",
    "LongMemoryHits",
    "MemoryClassifyResult",
    "chat_long_memory_service",
]

# 分类温度固定 0：分类判定要可复现
_CLASSIFIER_TEMPERATURE = 0.0

# 撞到 429 后的熔断窗口（秒）。**刻意不对齐响应里的 quota_reset**：
# 那是「到 2026-11-24 之前永久关闭」，换 key 或提额后也不会恢复，只能重启进程。
# 固定窗口的代价是配额仍耗尽时每窗口多付一次探测往返，换来的是自愈。
_QUOTA_COOLDOWN_S = 600.0


class MemoryClassifyResult(BaseModel):
    """记忆分类结果"""

    write_user: bool = Field(
        description=(
            "写入用户层：换会话仍应保留的身份、岗位、回答偏好、长期约束。"
            "不含本轮任务、不含知识库条文。"
        )
    )
    write_session: bool = Field(
        description="写入会话层：仅当前会话的任务、进度、待办、临时约定。"
    )
    reason: str = Field(description="分类理由，一句话")


# 分类器系统提示词
CLASSIFIER_PROMPT = (
    "你是企业知识库助手的记忆分类器。判断本轮是否有「新事实」要写入 Mem0。\n"
    "\n"
    "## user 层（跨会话）\n"
    "- 用户身份、岗位、所在团队自称\n"
    "- 长期偏好：答短一点、只要本团队制度、技术回答带示例\n"
    "- 持久约束：过敏、语言、称呼\n"
    "\n"
    "## session 层（仅当前会话）\n"
    "- 正在排查的问题、本次要写的文档、已确认的下一步\n"
    "- 用户说「这次」「本轮」的工作上下文\n"
    "\n"
    "## 均不写入\n"
    "- 寒暄、致谢、纯确认\n"
    "- 助手根据知识库/检索资料说出的制度、流程、负责人、系统名（那是文档事实，不是用户记忆）\n"
    "- 联网搜索结果、引用编号 [n]\n"
    "- 无信息增量的复述\n"
    "\n"
    "## 原则\n"
    "1. 知识库内容永远不要写成 user 记忆\n"
    "2. 「这次先看差旅制度第三节」→ session，不要标成 user\n"
    "3. user 与 session 可同时为 true\n"
    "4. 一次性提问且未产生需跨轮记住的约定 → 均为 false"
)

# 抽取侧约束（**按层区分**）
#
# 为什么必须分两层写：分类器只决定「写哪层」，而 `client.add()` 的抽取在 Mem0 云侧完成——
# 两次 add 若传同一份 instructions，抽取 LLM 无从知道自己在写 user 层还是 session 层，
# 于是「都写」时同一段文本会被两层抽出重复事实，甚至跨层污染（本轮任务进 user 层、
# 长期身份进 session 层）。提示词里的分层规则只作用于分类，管不到抽取，必须在这里再写一遍。
_EXTRACT_COMMON = (
    "只用用户说的话抽取记忆，一句完整中文。"
    "不要保存制度条文、流程、时限、负责人、系统名、引用编号。"
    "不要译成英文。"
)

# user 层：跨会话仍然成立的事实
_EXTRACT_USER_INSTRUCTIONS = (
    _EXTRACT_COMMON + "只抽身份、岗位、所在团队、长期偏好、持久约束；"
    "不要抽本轮任务、待办、临时约定。"
)

# session 层：仅当前会话有效的工作上下文
_EXTRACT_SESSION_INSTRUCTIONS = (
    _EXTRACT_COMMON + "只抽本轮任务、进度、待办、临时约定；"
    "不要抽身份、岗位、长期偏好。"
)

# 会话层清理（clear_session）的分页参数：get_all 每页条数 + 页数上限兜底。
# 见 clear_session 的注释——这里刻意不走 delete_all，必须先把 id 列出来。
_CLEAR_PAGE_SIZE = 100
_CLEAR_MAX_PAGES = 20


class LongMemoryHits(TypedDict):
    """一次检索拿到的两层记忆"""

    user: list[str]
    session: list[str]


def _pick_memories(response: Any) -> list[str]:
    """从 Mem0 响应里取出 memory 文本列表) => m.memory).filter((t): t is string => Boolean(t))`。
    逐层 isinstance 兜底：平台响应结构变动时退化为空列表，不把异常抛给调用方。
    """
    if not isinstance(response, dict):
        return []
    results = response.get("results")
    if not isinstance(results, list):
        return []
    picked: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        text = item.get("memory")
        if text:
            picked.append(str(text))
    return picked


def _build_classifier():
    """构建记忆分类器；未配置 API Key 返回 None

    Key / baseURL / model 的 fallback 链与 ai_chat._build_llm 完全一致，
    刻意不写返回类型注解——`with_structured_output` 的返回类型在 langchain 侧
    是个联合体，注解反而要 cast（与 kg_extraction._build_structured_llm 同款处理）。
    """
    api_key = (
        settings.OPENAI_API_KEY or settings.LLM_API_KEY or settings.DASHSCOPE_API_KEY
    )
    if not api_key:
        return None

    base_url = settings.OPENAI_BASE_URL or settings.LLM_BASE_URL
    model = settings.MODEL_NAME or settings.LLM_MODEL
    timeout_ms = settings.AI_CHAT_TIMEOUT_MS
    timeout_s = timeout_ms / 1000 if timeout_ms > 0 else 60.0

    llm = ChatOpenAI(
        api_key=SecretStr(api_key),
        model=model,
        temperature=_CLASSIFIER_TEMPERATURE,
        timeout=timeout_s,
        max_retries=0,
        # 用量/成本统计：挂一次即覆盖该 LLM 的所有调用点
        callbacks=usage_callbacks(),
        # DeepSeek 走 Chat Completions 兼容层，不要切 OpenAI Responses API
        use_responses_api=False,
        base_url=base_url,
        # deepseek-flash 默认 thinking 模式，不支持强制 tool_choice
        # （"Thinking mode does not support this tool_choice"），结构化输出必须关掉
        extra_body={"thinking": {"type": "disabled"}},
    )

    return llm.with_structured_output(
        MemoryClassifyResult,
        method="function_calling",
        # 不传 name：langchain-openai 会把 name 透传给 completions.create() 导致 TypeError
    )


# ---------------------------------------------------------------------------
# TypeSafe Jev 分类器（Noul × 2）
#
# 与 chat_query_rewrite.py 的意图识别同源：延迟导入 typesafe_sdk、失败一律回退 LLM。
# 为什么用两个 Noul 而不是一个 4 路 Choice：
#   1. user / session 是**两个独立维度**（跨会话事实 vs 本轮上下文），不是互斥层级；
#   2. Noul 给每层独立的 P(是)，可分层设阈值（user 严、session 松）——这是引入 Jev 的
#      主要收益，Choice 的 4 路概率会互相分摊；
#   3. 扩展第三层（如 team 层）只需再加一个 Noul。
# 「都不写」= 两个 Noul 都不过阈值，天然覆盖，不需要单独选项。
#
# Noul 的 instructions 要「表述成高值=是」；criteria.true/false 描述两端含义。
# ⚠️ NoulAnswer 只有 `noul`（P(是)），没有 confidence 字段。
# ---------------------------------------------------------------------------

_MEM_JEV_USER_INSTRUCTIONS = (
    "本轮「用户消息」包含应当跨会话记住的新事实：用户身份、岗位、所在团队、"
    "长期偏好或持久约束。"
)
_MEM_JEV_USER_CRITERIA = {
    "true": "明确出现身份、岗位、团队、长期偏好或持久约束",
    "false": "寒暄致谢、复述知识库内容，或只有本轮临时任务",
}

_MEM_JEV_SESSION_INSTRUCTIONS = (
    "本轮「用户消息」包含仅当前会话需要的任务、进度、待办或临时约定"
    "（如「这次先看…」「本轮要写…」）。"
)
_MEM_JEV_SESSION_CRITERIA = {
    "true": "出现本轮任务、进度、待办或临时约定",
    "false": "没有需要在本会话内延续的工作上下文",
}


class JevMemoryClassifier:
    """TypeSafe Jev 记忆分类器：Noul × 2 → 两个 bool（含概率）

    与 LLM 分类器的差别：Jev 不产出自由文本，`reason` 由两层概率拼出（比一句理由
    更利于观测与调阈值）。
    """

    def __init__(self, client_cls: Any, noul_cls: Any) -> None:
        self._client_cls = client_cls
        self._noul_cls = noul_cls

    def _make_client(self) -> Any:
        """构造客户端：优先显式 api_key，个别 SDK 版本只认环境变量时退回无参构造

        与 chat_query_rewrite._make_typesafe_client 同款处理。
        """
        try:
            return self._client_cls(api_key=settings.TYPESAFE_API_KEY)
        except TypeError:
            return self._client_cls()

    async def classify(self, question: str, answer: str) -> MemoryClassifyResult:
        """判定两层是否该写；异常向上抛，由调用方决定回退策略"""
        # 助手回复只作判断上下文：state 里分开标注，避免被当成用户事实
        state = {"user_message": question, "assistant_reply": answer[:300]}
        async with self._make_client() as client:
            response = await client.system_one(
                state=state,
                questions={
                    "write_user": self._noul_cls(
                        instructions=_MEM_JEV_USER_INSTRUCTIONS,
                        criteria=_MEM_JEV_USER_CRITERIA,
                    ),
                    "write_session": self._noul_cls(
                        instructions=_MEM_JEV_SESSION_INSTRUCTIONS,
                        criteria=_MEM_JEV_SESSION_CRITERIA,
                    ),
                },
            )
        p_user = float(response.nouls["write_user"].noul)
        p_session = float(response.nouls["write_session"].noul)
        write_user = p_user >= settings.MEMORY_JEV_USER_THRESHOLD
        write_session = p_session >= settings.MEMORY_JEV_SESSION_THRESHOLD
        logger.info(
            "记忆分类(Jev)：p_user=%.2f→%s p_session=%.2f→%s (阈值 %.2f/%.2f)",
            p_user,
            write_user,
            p_session,
            write_session,
            settings.MEMORY_JEV_USER_THRESHOLD,
            settings.MEMORY_JEV_SESSION_THRESHOLD,
        )
        return MemoryClassifyResult(
            write_user=write_user,
            write_session=write_session,
            reason=f"jev p_user={p_user:.2f} p_session={p_session:.2f}",
        )


def _build_jev_classifier() -> JevMemoryClassifier | None:
    """构建 Jev 分类器；未配 key / SDK 未装返回 None（与 chat_query_rewrite 同款容错）"""
    if not settings.TYPESAFE_API_KEY:
        return None
    try:
        from typesafe_sdk import AsyncTypeSafeClient, Noul  # noqa: PLC0415
    except ImportError:
        logger.warning("typesafe_sdk 未安装，记忆分类回退 LLM")
        return None
    return JevMemoryClassifier(AsyncTypeSafeClient, Noul)


def _search_timeout_s(timeout_ms: int) -> float | None:
    """检索预算：毫秒 → 秒；≤0 视为不限（返回 None）

    单独抽成纯函数：`ChatLongMemoryService.__init__` 要建 Mem0 客户端（约 1.3s），
    为测一个换算去构造整个服务不值得。
    """
    return timeout_ms / 1000 if timeout_ms > 0 else None


def _is_quota_error(err: BaseException) -> bool:
    """是不是「这个 key 没额度了」（HTTP 429）

    实测形态（2026-10-29，真 key）：`mem0.exceptions.RateLimitError`，它**没有**
    `status_code` / `response` 属性，只有 `message`、`error_code`（= `"HTTP_429"`）。

    所以按**类型**判，兜底看 `error_code`；**不做 message 子串匹配** ——
    用户的检索词里带个 "429" 就会把普通失败当成配额耗尽，然后静默关掉记忆 10 分钟。
    """
    return isinstance(err, RateLimitError) or getattr(err, "error_code", "") == "HTTP_429"


def _task_is_quota(task: asyncio.Task[Any]) -> bool:
    """已完成的任务是不是撞了 429（未完成 / 已取消都不算）"""
    if task.cancelled():
        return False
    err = task.exception()
    return err is not None and _is_quota_error(err)


def _task_outcome(task: asyncio.Task[Any]) -> Any:
    """把任务归一成「结果或异常」，交给 `_layer_payload` 做单层降级"""
    if task.cancelled():
        return asyncio.CancelledError()
    err = task.exception()
    return err if err is not None else task.result()


def _layer_payload(raw: Any, layer: str) -> dict[str, Any]:
    """把 `_task_outcome` 归一出的一项变成 payload（结果或异常都走这里）

    两层并行之后，「一层挂了」与「两层都挂了」必须能分开 —— 否则日志只剩
    「没有记忆」，定位时连是哪层超时都看不出来。

    只降级 `Exception`；`CancelledError` 必须上抛，不能把「请求被取消」
    伪装成「这轮没有记忆」。
    """
    if isinstance(raw, BaseException):
        if isinstance(raw, asyncio.CancelledError):
            raise raw
        logger.warning("Mem0 %s 层检索失败：%s", layer, raw)
        return {"results": []}
    return raw if isinstance(raw, dict) else {"results": []}


class ChatLongMemoryService:
    """对话长期记忆（Mem0）。未配置 MEM0_API_KEY 时全部跳过。

    记忆只用于改写问题和补上下文，不作制度事实来源。
    """

    def __init__(self) -> None:
        # )：默认值写在 config.py，此处直接读
        self._top_k = settings.MEM0_TOP_K
        # 检索整体预算（秒）。≤0 视为不限 —— 与「配置成 0 = 立刻全超时」相比，
        # 后者会让记忆功能静默失效，排查成本高。
        self._timeout_s: float | None = _search_timeout_s(
            settings.MEM0_SEARCH_TIMEOUT_MS
        )
        # 配额熔断状态：窗口截止时刻（monotonic），以及窗口内被跳过、尚未上报的次数。
        # 放实例上而不是模块全局：服务本身是单例，测试里换实例互不影响。
        self._quota_blocked_until = 0.0
        self._quota_suppressed = 0
        # 预算用尽后仍在路上的请求（持强引用，见 `_watch_late_quota`）
        self._inflight: set[asyncio.Task[Any]] = set()

        api_key = settings.MEM0_API_KEY or ""
        host = settings.MEM0_HOST or ""
        self._client: AsyncMemoryClient | None = None
        if api_key:
            # host 仅在配置时传入（(host ? { host } : {})`），
            # 否则由 SDK 用默认 https://api.mem0.ai
            if host:
                self._client = AsyncMemoryClient(api_key=api_key, host=host)
            else:
                self._client = AsyncMemoryClient(api_key=api_key)
        else:
            logger.warning("未配置 MEM0_API_KEY，跳过长期记忆")

        # （`if (!apiKey || !this.client) return;`）
        self._classifier = _build_classifier() if self._client else None
        # Jev 分类器（Noul×2）；未配 TYPESAFE_API_KEY / SDK 未装时为 None
        self._jev = _build_jev_classifier() if self._client else None

    @property
    def enabled(self) -> bool:
        """)`：只有配了 MEM0_API_KEY 才为真"""
        return self._client is not None

    async def search(
        self,
        user_id: str,
        session_id: str | None,
        query: str,
    ) -> LongMemoryHits:
        """并行检索两层记忆

        user 层按 user_id 过滤；session 层额外叠加 run_id（= sessionId）。
        未配置 sessionId 时不查会话层。

        **两层同时发出**：它们只共享 query，一层的结果不参与另一层的过滤条件。
        串行时 1500ms 预算要覆盖两次往返（实测 span p50 1038ms），并行后只覆盖一次。

        整体受 `MEM0_SEARCH_TIMEOUT_MS` 约束（两层共用同一 deadline，不是每层一份）。
        单层超时 / 异常只降级该层；整体超时按无记忆处理 —— 记忆不可用不能阻断对话，
        而且它坐在 TTFT 关键路径上，慢就等于用户盯着空白屏幕。

        **配额熔断**：记忆配额耗尽时 Mem0 回 429，而「没额度」不会自己好。撞到一次就
        关窗 `_QUOTA_COOLDOWN_S`，窗口内直接返回空、不碰网络 —— 否则每一轮对话都要为
        「确定查不到」白付一次往返（实测单次 300~1500ms），而且它看起来像「记忆很慢」。
        """
        empty: LongMemoryHits = {"user": [], "session": []}
        if self._client is None:
            return empty
        # 熔断窗口内：0 次网络请求。这里只累加计数不打日志，否则每轮刷一条 WARN。
        if time.monotonic() < self._quota_blocked_until:
            self._quota_suppressed += 1
            return empty
        # ⚠️ search 是三个方法里**唯一**要求实体 ID 放 filters 的（放顶层会被 SDK 的
        # ENTITY_PARAMS 白名单校验直接 raise）。add / delete_all 恰恰相反，走顶层。
        # 详见模块注释「实体 ID 的传法三个方法各不相同」——不要图省事统一。
        coros = [
            self._client.search(
                query,
                filters={"user_id": str(user_id)},
                top_k=self._top_k,
            )
        ]
        if session_id:
            coros.append(
                self._client.search(
                    query,
                    filters={"user_id": str(user_id), "run_id": str(session_id)},
                    top_k=self._top_k,
                )
            )
        tasks = [asyncio.create_task(c) for c in coros]
        try:
            # 用 wait 而不是 `asyncio.timeout` + `gather`：后者超时时会把请求**取消**，
            # 而 429 的往返实测 300~1500ms、冷启动握手更久，**经常比预算还慢** ——
            # 取消了就永远看不见「没额度」这个事实，熔断能否生效会变成
            # 「429 是否刚好比预算快」的运气问题（实测 4 次只撞上 1 次）。
            # wait 不取消未完成任务，把它们留给 `_watch_late_quota` 收尾。
            # timeout=None（未配预算）= 一直等。
            done, pending = await asyncio.wait(tasks, timeout=self._timeout_s)
        except BaseException:
            # 调用方取消（如 ai_stream 里 classify 抛错 → mem_task.cancel()）：
            # 保持原语义，连在途请求一起取消，不留后台请求。
            for task in tasks:
                task.cancel()
            raise

        # 配额是账号级的：一层 429 另一层必然也 429，没必要拼半份记忆，
        # 更不能按层打 WARN —— 那会把「没额度」写成「user 层检索失败」，
        # 运维按检索故障去查，永远查不到真因。
        # ⚠️ 不能写成 `any(_task_is_quota(t) for t in done)`：生成器在第一个 True 处
        # 短路，剩下的 task 异常就没人取回，asyncio 会在关闭循环时打
        # 「Task exception was never retrieved」。逐个走完，顺带把所有异常都取回。
        quota_hit = False
        for task in done:
            if _task_is_quota(task):
                quota_hit = True
        if quota_hit:
            self._trip_quota_breaker(user_id)
            return empty

        if pending:
            # 本轮按超时放弃，但请求不取消：跑完若发现是 429，这笔账记给**下一轮**。
            for task in pending:
                self._watch_late_quota(task, user_id)
            logger.warning(
                "Mem0 检索超时（预算 %ss），本轮按无记忆处理：userId=%s, sessionId=%s",
                self._timeout_s,
                user_id,
                session_id,
            )
            return empty

        # 按下标认层：tasks 保持入参顺序，而 done 是 set，不能直接拿来配对
        raw = [_task_outcome(task) for task in tasks]
        return {
            "user": _pick_memories(_layer_payload(raw[0], "user")),
            "session": (
                _pick_memories(_layer_payload(raw[1], "session")) if session_id else []
            ),
        }

    def _watch_late_quota(self, task: asyncio.Task[Any], user_id: str) -> None:
        """等一个「本轮已经不等了」的请求跑完，只为看出它是不是 429

        必须持强引用：没有 await 的 task 只被事件循环弱引用，可能在完成前被回收，
        报「Task was destroyed but it is pending」。
        """
        self._inflight.add(task)
        task.add_done_callback(lambda done: self._on_late_done(done, user_id))

    def _on_late_done(self, task: asyncio.Task[Any], user_id: str) -> None:
        self._inflight.discard(task)
        if task.cancelled():
            return
        err = task.exception()
        if err is not None and _is_quota_error(err):
            self._trip_quota_breaker(user_id)

    def _trip_quota_breaker(self, user_id: str) -> None:
        """开窗 + 打**一条** WARN

        窗口内不再打日志，只在下次开窗时把「上一窗口跳过了多少次」一起报出来 ——
        这个数字是熔断到底省了多少，比每轮刷一行有用。
        """
        now = time.monotonic()
        already_open = now < self._quota_blocked_until
        self._quota_blocked_until = now + _QUOTA_COOLDOWN_S
        if already_open:
            # 窗口本来就开着：同一轮的另一层、或一个迟到的请求刚报过。
            # 只顺延窗口，不重复打日志 —— 「打一条」是需求，不是口号。
            return
        skipped = self._quota_suppressed
        self._quota_suppressed = 0
        logger.warning(
            "Mem0 检索配额耗尽（429）：%d 分钟内不再发起检索，本轮按无记忆处理。"
            "请更换 MEM0_API_KEY，或在 Mem0 后台接入自有 LLM key 提高额度"
            "（上一窗口内已跳过 %d 次检索，userId=%s）",
            int(_QUOTA_COOLDOWN_S // 60),
            skipped,
            user_id,
        )

    def build_system_message(self, hits: LongMemoryHits) -> SystemMessage | None:
        """把两层记忆拼成一条 SystemMessage；两层都空返回 None

        末尾强制加免责句：记忆只作背景，制度/流程以本轮检索资料为准。
        """
        blocks: list[str] = []
        if hits["user"]:
            lines = "\n".join(f"- {line}" for line in hits["user"])
            blocks.append(f"【用户长期记忆】\n{lines}")
        if hits["session"]:
            lines = "\n".join(f"- {line}" for line in hits["session"])
            blocks.append(f"【当前会话记忆】\n{lines}")
        if not blocks:
            return None
        return SystemMessage(
            "\n\n".join(blocks)
            + "\n\n以上仅作背景，制度/流程以本轮检索资料为准，不要用记忆替代文档。"
        )

    async def remember_turn(
        self,
        user_id: str,
        session_id: str,
        question: str,
        answer: str,
    ) -> None:
        """一轮问答结束后分类并写入记忆

        分类器（Jev / LLM，见 `_classify`）决定写 user 层 / session 层 / 两层 / 都不写。
        **只把用户说的话交给 Mem0 抽取**（extractFrom 只有 question），助手回答仅用于让
        分类器判断上下文。
        两层各自带**分层抽取指令**：否则同一段文本会被抽出重复事实并跨层污染。
        写入失败只 WARN——记忆丢一条不影响本轮对话。
        """
        if self._client is None:
            return
        result = await self._classify(question, answer)
        if result is None:
            return
        extract_from = [{"role": "user", "content": question}]
        try:
            written: list[str] = []
            if result.write_user:
                # ⚠️ 实体 ID 走顶层（不能包 filters），见模块注释「实体 ID 的传法」
                await self._client.add(
                    extract_from,
                    user_id=str(user_id),
                    custom_instructions=_EXTRACT_USER_INSTRUCTIONS,
                )
                written.append("user")
            if result.write_session:
                await self._client.add(
                    extract_from,
                    user_id=str(user_id),
                    run_id=str(session_id),
                    custom_instructions=_EXTRACT_SESSION_INSTRUCTIONS,
                )
                written.append("session")
            logger.info(
                "Mem0 分类：%s；写入=%s", result.reason, ",".join(written) or "无"
            )
        except Exception as err:  # noqa: BLE001
            logger.warning("Mem0 写入失败：%s", err)

    # ---------------------------------------------------------------- 分类分派

    async def _classify_llm(
        self, question: str, answer: str
    ) -> MemoryClassifyResult | None:
        """LLM 分类（ChatOpenAI + with_structured_output）；不可用返回 None"""
        if self._classifier is None:
            return None
        result: MemoryClassifyResult = await self._classifier.ainvoke(
            [
                SystemMessage(CLASSIFIER_PROMPT),
                HumanMessage(
                    f"用户：{question}\n助手（仅供判断，不要当作用户事实）：{answer[:300]}"
                ),
            ]
        )
        return result

    async def _classify(
        self, question: str, answer: str
    ) -> MemoryClassifyResult | None:
        """按 MEMORY_CLASSIFIER 分派记忆分类；两者都不可用返回 None（本轮不写）

        - auto：优先 Jev，失败回退 LLM（记忆是增强项，不因分类器故障断链）
        - jev：只用 Jev，失败即放弃本轮
        - llm：只用 LLM
        - shadow：两边都跑、只记日志，**按 LLM 结果写入**（校准 Jev 阈值用，切换前先跑这个）
        """
        mode = settings.MEMORY_CLASSIFIER
        if mode == "shadow":
            return await self._classify_shadow(question, answer)
        if mode == "llm":
            return await self._classify_llm(question, answer)
        if self._jev is not None:
            try:
                return await self._jev.classify(question, answer)
            except Exception as err:  # noqa: BLE001
                logger.warning("Jev 记忆分类失败：%s", err)
                if mode == "jev":
                    return None
        elif mode == "jev":
            logger.warning(
                "MEMORY_CLASSIFIER=jev 但未配置 TYPESAFE_API_KEY，本轮不写记忆"
            )
            return None
        return await self._classify_llm(question, answer)

    async def _classify_shadow(
        self, question: str, answer: str
    ) -> MemoryClassifyResult | None:
        """影子模式：Jev 与 LLM 都跑，比较判定是否一致，**按 LLM 结果写入**

        目的：拿真实流量看 Jev 在中文上的判定质量与概率分布，再决定阈值/是否切换。
        Jev 侧概率由 `JevMemoryClassifier.classify` 的 INFO 日志输出。
        """
        llm_result = await self._classify_llm(question, answer)
        if self._jev is None:
            return llm_result
        try:
            jev_result = await self._jev.classify(question, answer)
        except Exception as err:  # noqa: BLE001
            logger.warning("记忆分类 shadow：Jev 调用失败 %s", err)
            return llm_result
        agree = (
            llm_result is not None
            and llm_result.write_user == jev_result.write_user
            and llm_result.write_session == jev_result.write_session
        )
        logger.info(
            "记忆分类 shadow：llm=user:%s/session:%s | jev=user:%s/session:%s | 一致=%s",
            llm_result.write_user if llm_result else None,
            llm_result.write_session if llm_result else None,
            jev_result.write_user,
            jev_result.write_session,
            agree,
        )
        return llm_result

    async def clear_session(self, user_id: str, session_id: str) -> None:
        """删除该会话的会话层记忆

                只删 session 层（run_id 维度），不动 user 层——用户偏好跨会话保留。
                删除失败只 WARN。

        ({userId, runId})`（此处即
                `delete_all(user_id=..., run_id=...)`）。实测（2026-09-18，真实 key）该方法
                **完全忽略 run_id，直接清空该 user_id 下的全部记忆**：
                  删除前 user 层 4 条 / 本会话 1 条 / 其他会话 1 条
                  delete_all(user_id=UID, run_id=SID) 之后 → 0 / 0 / 0

                全部长期记忆」——用户偏好会被静默清空，且因为只剩一行 WARN 而完全不会被发现。
                故这里改为「先按 filters 列出该会话的记忆 id，再逐条 delete」：
                  - `get_all(filters={'user_id':…, 'run_id':…})` 的 run_id 过滤**是有效的**
                    （实测同 user 下 4 条 → 命中 1 条），
                  - `delete(memory_id)` 精确只删目标（实测 4 → 3）。
                代价是 N 次请求（会话层记忆通常只有个位数，可接受）；若将来单会话记忆量级变大，
                需改成批量删除接口（`batch_delete`）。
        """
        client = self._client
        if client is None:
            return
        try:
            memory_ids = await self._session_memory_ids(client, user_id, session_id)
            for memory_id in memory_ids:
                await client.delete(memory_id)
            if memory_ids:
                logger.info(
                    "Mem0 会话层已清理：userId=%s, sessionId=%s, 删除 %d 条",
                    user_id,
                    session_id,
                    len(memory_ids),
                )
        except Exception as err:  # noqa: BLE001
            logger.warning("Mem0 会话层清理失败：%s", err)

    async def _session_memory_ids(
        self, client: AsyncMemoryClient, user_id: str, session_id: str
    ) -> list[str]:
        """列出某会话的全部会话层记忆 id

        `get_all` 走 POST + JSON body（与 delete_all 的 query string 不同），
        `filters` 能正常序列化、run_id 过滤有效。page / page_size 由 SDK 放进 query
        string，其余字段放 body。
        """
        ids: list[str] = []
        page = 1
        while page <= _CLEAR_MAX_PAGES:
            resp = await client.get_all(
                filters={"user_id": str(user_id), "run_id": str(session_id)},
                page=page,
                page_size=_CLEAR_PAGE_SIZE,
            )
            rows = resp.get("results") or []
            ids.extend(str(row["id"]) for row in rows if row.get("id"))
            # 不足一页即到底（平台分页语义）；同时有页数上限兜底，避免异常响应导致死循环
            if len(rows) < _CLEAR_PAGE_SIZE:
                break
            page += 1
        return ids


# 全局单例
chat_long_memory_service = ChatLongMemoryService()

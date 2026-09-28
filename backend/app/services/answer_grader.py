"""
生成侧评测（LLM-as-judge）

检索指标（recall / hit@1 / MRR）只回答「资料有没有被找到」，回答不了
「回答本身好不好」—— 检索全对但模型编造、乱标引用，检索指标一样满分。
本模块补上 P0 缺的两个生成侧指标：

  1. **faithfulness（忠实度）**：回答里的断言是否都能被检索资料支撑
  2. **citation_accuracy（引用准确率）**：回答里的 `[n]` 是否指向真正支持该句的资料

另有两个**确定性**指标（不花 LLM 调用，见 `fact_coverage` / `extract_citations`）：
要点覆盖率、引用行为（是否给出了引用）。

分工：本模块只判分；跑批、汇总、LangSmith 上报在 `scripts/eval_generation.py`。
判分失败（未配 Key / 网络 / 结构化解析失败）返回 None —— fail-open，
不把「判分失败」算成「回答错误」（与 chat_query_rewrite 的切题判定同款约定）。
"""
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, SecretStr

from app.core.config import settings
from app.services.llm_budget import usage_callbacks

logger = logging.getLogger("AnswerGrader")

# 匹配回答中的引用编号 [n]
_CITATION_RE = re.compile(r"\[(\d+)\]")
# 判分要稳定复现，温度取 0
_TEMPERATURE = 0.0

_JUDGE_SYSTEM = (
    "你是 RAG 回答质量评审员。输入包含「检索到的资料」「用户问题」「模型回答」。"
    "请完成两件事：\n"
    "1. 把模型回答拆成若干条断言，逐条判断能否被资料**直接**支撑；"
    "资料里找不到依据的、自行发挥的，计入 unsupported_claims；有依据的计入 supported_claims。\n"
    "2. 检查回答里的引用编号 [n] 是否落位。"
    "⚠️ 编号口径：资料按 [1]、[2]… 编号，**一个编号对应一整天资料原文**，"
    "这条原文可能很长、还可能包含多个小节或一张表格 —— 只要断言在该编号对应的"
    "**整段原文里任意位置**能找到依据，这条引用就是对的。"
    "切勿因为「这句话在原文里属于哪一小节」或「编号标记出现在句末」而判错。"
    "只有当断言在其标注编号对应的整段原文里**完全找不到依据**、"
    "或编号超出资料条数时，才写进 citation_issues。\n"
    "只依据给定资料判断，不要用你自己的知识补全。"
)


class AnswerGrade(BaseModel):
    """LLM 判分结果（结构化输出）"""

    supported_claims: list[str] = Field(
        description="能被检索资料直接支撑的断言（摘录回答原句）"
    )
    unsupported_claims: list[str] = Field(
        description="资料无法支撑／模型自行发挥的断言；没有则空列表"
    )
    citation_issues: list[str] = Field(
        description="引用编号与资料不符的问题描述（含编号越界）；没有问题则空列表"
    )
    reason: str = Field(description="一句中文总评")


def extract_citations(answer: str) -> list[int]:
    """抽取回答里的引用编号 [n]（去重、保序）"""
    seen: list[int] = []
    for match in _CITATION_RE.finditer(answer):
        number = int(match.group(1))
        if number not in seen:
            seen.append(number)
    return seen


def _normalize(text: str) -> str:
    """去空白 + 转小写，用于要点匹配（避免「600 元」和「600元」判成不同）"""
    return re.sub(r"\s+", "", text).lower()


def fact_coverage(answer: str, facts: list[str]) -> float:
    """要点覆盖率：facts 中有多少条出现在回答里（子串匹配）

    facts 为空（如「不可回答」用例）→ 1.0，不参与扣分。
    ⚠️ 这是便宜的近似：只判「提没提到」，判不了「说得对不对」——
    后者交给 LLM 判分的 faithfulness。
    """
    if not facts:
        return 1.0
    text = _normalize(answer)
    hit = sum(1 for fact in facts if _normalize(fact) in text)
    return hit / len(facts)


def faithfulness_score(grade: AnswerGrade) -> float:
    """忠实度 = 被支撑断言数 / 总断言数；一条断言都没提取到时按 1.0（无话即无幻觉）"""
    total = len(grade.supported_claims) + len(grade.unsupported_claims)
    if total == 0:
        return 1.0
    return len(grade.supported_claims) / total


def citation_ok(grade: AnswerGrade) -> bool:
    """引用是否全部正确落位"""
    return not grade.citation_issues


class AnswerGrader:
    """LLM 判分器；未配置 Key 时 `is_enabled()` 为 False，`grade()` 返回 None"""

    def __init__(self) -> None:
        self._judge = _build_judge()

    def is_enabled(self) -> bool:
        return self._judge is not None

    async def grade(
        self, question: str, answer: str, contexts: list[str]
    ) -> AnswerGrade | None:
        """对一条问答判分；失败返回 None（调用方按 fail-open 处理）"""
        if self._judge is None:
            logger.warning("未配置 LLM Key，跳过生成侧判分")
            return None

        material = "\n\n".join(
            f"[{index}] {text}" for index, text in enumerate(contexts, start=1)
        )
        try:
            return await self._judge.ainvoke(
                [
                    SystemMessage(content=_JUDGE_SYSTEM),
                    HumanMessage(
                        content=(
                            f"检索到的资料：\n{material}\n\n"
                            f"用户问题：{question}\n\n"
                            f"模型回答：\n{answer}"
                        )
                    ),
                ]
            )
        except Exception as err:  # noqa: BLE001 — 判分失败不该让评测中断
            logger.warning("生成侧判分失败（跳过该题）：%s", err)
            return None


def _build_judge():
    """构建结构化判分器（AnswerGrade）；未配置 API Key 返回 None

    刻意不写返回类型注解：`with_structured_output` 的返回类型在 langchain 侧
    是个联合体，注解反而要 cast（与 chat_query_rewrite / kg_extraction 同款处理）。
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
    timeout_ms = settings.AI_EVAL_TIMEOUT_MS
    timeout_s = timeout_ms / 1000 if timeout_ms > 0 else 60.0

    llm = ChatOpenAI(
        api_key=SecretStr(api_key),
        model=model,
        temperature=_TEMPERATURE,
        timeout=timeout_s,
        max_retries=0,
        # 用量/成本统计：挂一次即覆盖该 LLM 的所有调用点
        callbacks=usage_callbacks(),
        # DeepSeek 走 Chat Completions 兼容层，不要切 OpenAI Responses API
        use_responses_api=False,
        base_url=base_url,
        # deepseek-v4-flash 默认 thinking 模式，结构化输出必须关闭
        extra_body={"thinking": {"type": "disabled"}},
    )
    return llm.with_structured_output(
        AnswerGrade,
        method="function_calling",
        # 不传 name：langchain-openai 会把 name 透传给 completions.create() 导致 TypeError
    )


# 全局单例
answer_grader = AnswerGrader()

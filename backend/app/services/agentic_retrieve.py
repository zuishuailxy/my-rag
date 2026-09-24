"""检索 + 切题评估编排（对应 TS ai/agentic-retrieve.ts）

把「检索一次 → 判切题 → 不切题则改写再查一次」的纠错环封装成可复用函数：

  - `retrieve_and_grade`：单次检索 + 切题评估。Agent loop 每次工具调用走这里
    （agentic RAG：由模型决定何时检索），返回原始命中 / 切题命中 / 评估。
  - `retrieve_until_relevant`：查一次 → 评估 → 不足则改写再查一次。给非流式
    `/ai/chat` 用（无 agent 时的等价环）。

评估结果 `RetrieveEval` 是推给前端过程条 `data-eval` 的：`ok` 表示「资料切题」，
不是「有条数」。只有切题时 `hits` 才非空（给模型引用）；不切题时 `hits` 为空，
`raw_hits` 仍保留给改写器。

⚠️ 与 TS 的差异（迁移代价，刻意记录）：
- TS 的 `retrieval.retrieve()` 直接返回 `ChunkHit[]`；本项目的
  `HybridRetrievalService.retrieve()` 返回 camelCase dict，故此处用
  `ChunkHit.from_row` 归一（与 ai_stream / ai_chat 现有做法一致）。
- TS 用回调 `onEval` / `onRewrite` 推进过程条。Python 的异步生成器**无法在回调里
  yield**，所以流式链路（ai_stream.py）不调 `retrieve_until_relevant`，而是用
  `retrieve_and_grade` + `to_eval` 手工内联、逐次 yield `data-eval`；本模块的
  `retrieve_until_relevant` 主要给非流式 ai_chat 用。
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from app.pipeline.types import ChunkHit
from app.schemas.auth import AuthUser
from app.services.chat_query_rewrite import ChatQueryRewriteService, HitGrade
from app.services.retrieval import HybridRetrievalService

logger = logging.getLogger("AgenticRetrieve")

# 推给前端 data-eval；ok 表示资料切题，不是「有条数」（对应 TS RetrieveEvalReason）
RetrieveEvalReason = Literal[
    "ok",  # 首轮检索切题
    "retried_ok",  # 改写后再查切题
    "empty",  # 首轮无命中
    "retried_empty",  # 改写后再查仍无命中
    "irrelevant",  # 首轮有命中但不切题
    "retried_irrelevant",  # 改写后再查仍不切题
    "error",  # 检索接口失败
]


@dataclass
class RetrieveEval:
    """一次检索的评估（对应 TS RetrieveEval，推给前端 data-eval）"""

    ok: bool
    reason: RetrieveEvalReason
    text: str
    retried: bool
    query: str
    retry_query: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """转成前端契约（camelCase：`retryQuery`，对齐 TS）"""
        return {
            "ok": self.ok,
            "reason": self.reason,
            "text": self.text,
            "retried": self.retried,
            "query": self.query,
            "retryQuery": self.retry_query,
        }


@dataclass
class GradedRetrieve:
    """单次检索 + 评估结果（对应 TS GradedRetrieve）"""

    # 原始命中，给改写器用；不切题时不要当制度依据
    raw_hits: list[ChunkHit]
    # 切题才有，给模型引用
    hits: list[ChunkHit]
    # 切题判断原文（HitGrade）
    grade: HitGrade
    # 推给前端过程条
    eval: RetrieveEval
    # 本轮实际用来检索的词
    used_query: str


@dataclass
class RetrieveOutcome:
    """`retrieve_until_relevant` 的返回（对应 TS `{ hits, eval, usedQuery }`）"""

    hits: list[ChunkHit]
    eval: RetrieveEval
    used_query: str


# 无命中时的评估占位（对应 TS EMPTY_GRADE）
EMPTY_GRADE = HitGrade(ok=False, reason="empty", text="未检索到资料")


async def retrieve_and_grade(
    *,
    question: str,
    query: str,
    top_k: int,
    user: AuthUser | None,
    retrieval: HybridRetrievalService,
    rewrite: ChatQueryRewriteService,
    retried: bool = False,
    previous_query: str | None = None,
) -> GradedRetrieve:
    """单次检索 + 切题评估（对应 TS retrieveAndGrade）

    `question` 是用户原问题（评估切题时对照这个）；`query` 是本轮检索词。
    `retried` 标记是否已是改写后的第二次检索；`previous_query` 是首轮检索词，
    只用来填 eval.query（前端过程条要展示「首轮词 → 改写词」）。
    """
    used_query = query.strip() or question
    retried = bool(retried)
    display_query = (previous_query or used_query) if retried else used_query
    retry_query = used_query if retried else None

    try:
        rows = await retrieval.retrieve(used_query, top_k, user)
        raw_hits = [ChunkHit.from_row(row) for row in rows]
        grade = await rewrite.grade_hits(question, raw_hits)
        if raw_hits and not grade.ok:
            # 方便定位「检索得到但被判不切题」的静默丢弃：把被判掉的命中列出来
            logger.info(
                "检索有命中但判不切题：query=%r raw=%d reason=%s top=%s",
                used_query[:80],
                len(raw_hits),
                grade.reason,
                [(h.documentTitle[:24], round(h.score, 3)) for h in raw_hits[:3]],
            )
        return GradedRetrieve(
            raw_hits=raw_hits,
            hits=raw_hits if grade.ok else [],
            grade=grade,
            eval=to_eval(grade, display_query, retried, retry_query),
            used_query=used_query,
        )
    except Exception as error:  # noqa: BLE001 —— 检索失败不抛，收敛成 error 评估
        detail = str(error)
        logger.warning("检索失败：query=%r err=%s", used_query[:80], detail)
        return GradedRetrieve(
            raw_hits=[],
            hits=[],
            grade=HitGrade(ok=False, reason="empty", text=f"检索失败：{detail}"),
            eval=RetrieveEval(
                ok=False,
                reason="error",
                text="改写后再次检索失败" if retried else "检索失败",
                retried=retried,
                query=display_query,
                retry_query=retry_query,
            ),
            used_query=used_query,
        )


async def retrieve_until_relevant(
    *,
    question: str,
    query: str,
    top_k: int,
    user: AuthUser | None,
    retrieval: HybridRetrievalService,
    rewrite: ChatQueryRewriteService,
    on_eval: Callable[[RetrieveEval], None] | None = None,
    on_rewrite: Callable[[str], None] | None = None,
) -> RetrieveOutcome:
    """查一次 → 评估 → 不足则改写再查一次（对应 TS retrieveUntilRelevant）

    无 agent 时的等价环，给非流式 /ai/chat 用。
    """
    first = await retrieve_and_grade(
        question=question,
        query=query,
        top_k=top_k,
        user=user,
        retrieval=retrieval,
        rewrite=rewrite,
    )
    if on_eval is not None:
        on_eval(first.eval)
    # 切题或接口失败都不再重试（error 重试也没意义）
    if first.eval.ok or first.eval.reason == "error":
        return RetrieveOutcome(first.hits, first.eval, first.used_query)

    # rewrite_after_retrieve 永不返回 None：失败会回退用户原问题；返回空串表示
    # 原问题与上次检索词相同、无法再查（与 TS 一致）。
    retry_query = await rewrite.rewrite_after_retrieve(
        question, first.used_query, first.grade, first.raw_hits
    )
    if not retry_query:
        return RetrieveOutcome([], first.eval, first.used_query)
    if on_rewrite is not None:
        on_rewrite(retry_query)

    second = await retrieve_and_grade(
        question=question,
        query=retry_query,
        top_k=top_k,
        user=user,
        retrieval=retrieval,
        rewrite=rewrite,
        retried=True,
        previous_query=first.used_query,
    )
    if on_eval is not None:
        on_eval(second.eval)
    return RetrieveOutcome(second.hits, second.eval, second.used_query)


def to_eval(
    grade: HitGrade,
    query: str,
    retried: bool,
    retry_query: str | None = None,
) -> RetrieveEval:
    """HitGrade → 前端 RetrieveEval（对应 TS toEval）"""
    if grade.reason == "relevant":
        return RetrieveEval(
            ok=True,
            reason="retried_ok" if retried else "ok",
            text=f"已改写再查，{grade.text}" if retried else grade.text,
            retried=retried,
            query=query,
            retry_query=retry_query,
        )
    if grade.reason == "empty":
        return RetrieveEval(
            ok=False,
            reason="retried_empty" if retried else "empty",
            text="改写后仍无结果" if retried else grade.text,
            retried=retried,
            query=query,
            retry_query=retry_query,
        )
    return RetrieveEval(
        ok=False,
        reason="retried_irrelevant" if retried else "irrelevant",
        text=f"改写后仍不切题：{grade.text}" if retried else grade.text,
        retried=retried,
        query=query,
        retry_query=retry_query,
    )

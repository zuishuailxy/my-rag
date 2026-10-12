"""Agentic RAG 冒烟

覆盖（**不依赖 ES/PG/Redis 网络**；工具逻辑用假 retrieval/rewrite/graph 注入）：
1. system_for_plan：5 路意图的循环指令 / 工具禁用语都在（含 retrieve_graph 并行提示）
2. packRetrieveResult（_pack_retrieve_result）：多次检索的**全局连续编号**、
   data-sources 全量推送、source-document 逐条、context 带正确 [n]
3. create_turn_agent：按 plan 挂工具（chitchat/profile 无工具、kb 挂检索+改写+图谱、
   web 挂 web_search、kb_then_web 挂四个）
4. retrieve_knowledge 工具：全局编号累加、最多两次（第三次直接 insufficient）、
   eval 随返回值透传
5. 隔离配置：_INTERNAL_CALL_CONFIG 清空 callbacks + kh-internal tag
6. retrieve_graph 工具 + 图谱纯函数：split/format/merge、retrieve_for_chat 注入
   （keywords = plan.graph_queries + 工具入参）

运行：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_agentic_rag.py

前置：LLM Key **非必需**（无则用 dummy 模型只建图，不会发起调用）。
"""

import asyncio
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables.config import ensure_config, var_child_runnable_config
from langchain_deepseek import ChatDeepSeek
from pydantic import SecretStr

import app.services.ai_stream as ai_stream_mod
from app.core.logging import setup_logging
from app.pipeline.types import ChunkHit
from app.services.ai_stream import (
    _pack_retrieve_result,
    ai_stream_service,
    system_for_plan,
)
from app.services.chat_query_rewrite import (
    INTENT_LABEL,
    KH_INTERNAL_TAG,
    _INTERNAL_CALL_CONFIG,
)
from app.services.chat_query_rewrite import HitGrade
from app.services.graph_build import (
    format_graph_context,
    format_graph_system_text,
    merge_graph_hits,
    split_graph_keywords,
)


class _CollectWriter:
    """假 stream writer：收集工具推来的 UI 事件"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, event: dict) -> None:
        self.events.append(event)


class _FakeRetrieval:
    """假混合检索：返回 camelCase 行，并记录调用"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def retrieve(self, query: str, top_k: int, user: Any = None) -> list[dict]:
        self.calls.append((query, top_k))
        return [
            {
                "chunkId": f"c-{query}",
                "documentId": f"d-{query}",
                "documentTitle": f"文档-{query}",
                "content": f"正文-{query}",
                "heading": "章节",
                "score": 0.9,
            }
        ]


class _FakeRewrite:
    """假改写服务：首轮不切题、之后切题；rewrite_after_retrieve 给固定新词"""

    def __init__(self) -> None:
        self.rewrite_calls: list[str] = []
        self.grade_calls = 0

    async def grade_hits(self, question: str, hits: list[ChunkHit]) -> HitGrade:
        self.grade_calls += 1
        if self.grade_calls == 1:
            return HitGrade(ok=False, reason="irrelevant", text="资料与问题不相关")
        return HitGrade(ok=True, reason="relevant", text="资料与问题相关")

    async def rewrite_after_retrieve(
        self, question: str, previous_query: str, grade: HitGrade, hits: list[ChunkHit]
    ) -> str:
        self.rewrite_calls.append(previous_query)
        return f"{previous_query}-改写"


class _FakeGraph:
    """假图谱服务：固定返回一条关系 + 一篇来源文档"""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, int]] = []

    async def retrieve_for_chat(
        self, keyword: Any, limit: int = 8, scope: Any = None
    ) -> dict:
        self.calls.append((keyword, limit))
        return {
            "query": " / ".join(keyword) if isinstance(keyword, list) else str(keyword),
            "entities": [{"name": "差旅", "type": "CONCEPT", "description": None}],
            "relations": [{"source": "差旅", "relation": "需要", "target": "审批"}],
            "documents": [{"documentId": "d1", "title": "差旅管理办法"}],
        }


def _agent_tools(agent: Any) -> dict[str, Any]:
    node = getattr(agent, "nodes", {}).get("tools")
    bound = getattr(node, "bound", node)
    return getattr(bound, "tools_by_name", {})


def _plan(intent: str, query: str = "差旅审批流程") -> dict[str, Any]:
    kb = intent in ("kb", "kb_then_web")
    return {
        "intent": intent,
        "label": INTENT_LABEL[intent],
        "query": query,
        "graph_queries": ["差旅", "审批"] if kb else [],
        "allow_retrieve": kb,
        "allow_graph": kb,
        "allow_web": intent in ("web", "kb_then_web"),
    }


def _hit(doc: str, heading: str | None = "章节") -> ChunkHit:
    return ChunkHit(
        chunkId=f"c-{doc}",
        documentId=f"d-{doc}",
        documentTitle=f"文档-{doc}",
        content=f"正文-{doc}",
        heading=heading,
        score=0.9,
    )


async def main() -> None:
    # 启用统一日志（否则 root 默认 WARNING，工具挂载/图谱检索的 INFO 看不到）
    setup_logging()

    # 1) system_for_plan
    p_kb = system_for_plan(_plan("kb"))
    assert "最多两次" in p_kb and "不要调用 web_search" in p_kb, p_kb
    p_web = system_for_plan(_plan("web"))
    assert "web_search" in p_web and "不要调用 retrieve_knowledge" in p_web, p_web
    p_kbw = system_for_plan(_plan("kb_then_web"))
    assert "两次后仍 insufficient 再 web_search" in p_kbw, p_kbw
    p_chat = system_for_plan(_plan("chitchat"))
    assert "不要调用任何工具" in p_chat, p_chat
    p_profile = system_for_plan(_plan("profile"))
    assert "不要检索知识库" in p_profile, p_profile
    # kb（allowRetrieve && allowGraph）走「并行首次调用」分支；web 走禁用语
    assert "并行调用" in p_kb and "图谱建议检索词「差旅 / 审批」" in p_kb, p_kb
    assert "并行调用" not in p_web and "不要调用 retrieve_graph" in p_web, p_web
    print(" 1) system_for_plan ✓ 5 路意图指令/禁用语齐全（含 retrieve_graph 并行）")

    # 2) _pack_retrieve_result：全局编号 + 事件 + context
    persist: list[dict] = []
    w1 = _CollectWriter()
    r1 = _pack_retrieve_result([_hit("A")], "q1", persist, w1, {"eval": {"ok": True}})
    assert [s["index"] for s in persist] == [1], persist
    assert r1["items"][0]["index"] == 1 and "[1]" in r1["context"], r1
    w2 = _CollectWriter()
    r2 = _pack_retrieve_result([_hit("B"), _hit("C")], "q2", persist, w2)
    assert [s["index"] for s in persist] == [1, 2, 3], persist
    assert [s["index"] for s in r2["items"]] == [2, 3], r2
    assert "[2]" in r2["context"] and "[3]" in r2["context"], r2["context"]
    assert w2.events[0]["type"] == "data-sources" and len(w2.events[0]["data"]) == 3, w2.events
    assert [e["type"] for e in w2.events[1:]] == [
        "source-document",
        "source-document",
    ], w2.events
    assert w2.events[1]["title"] == "[2] 文档-B", w2.events[1]
    print(" 2) _pack_retrieve_result ✓ 全局编号 1→2,3、events、context [n]")

    # 3) create_turn_agent 按 plan 挂工具
    saved_llm = ai_stream_service._llm
    if saved_llm is None:
        # 无 Key 时用 dummy 只建图（不会发起调用），测完还原
        ai_stream_service._llm = ChatDeepSeek(
            api_key=SecretStr("dummy"), model="deepseek-flash"
        )
    try:
        mounts = {
            "chitchat": set(),
            "profile": set(),
            "kb": {"retrieve_knowledge", "rewrite_query", "retrieve_graph"},
            "web": {"web_search"},
            "kb_then_web": {
                "retrieve_knowledge",
                "rewrite_query",
                "retrieve_graph",
                "web_search",
            },
        }
        for intent, expected in mounts.items():
            agent = ai_stream_service.create_turn_agent(None, 5, [], _plan(intent), "问题")
            got = set(_agent_tools(agent).keys())
            assert got == expected, f"{intent}: 期望 {expected}，实际 {got}"
        print(" 3) create_turn_agent ✓ 5 路意图工具挂载正确")
    finally:
        ai_stream_service._llm = saved_llm

    # 4) retrieve_knowledge 工具：补丁模块单例后再建图，直接 invoke 工具
    fake_retr = _FakeRetrieval()
    fake_rewr = _FakeRewrite()
    saved_retr = ai_stream_mod.hybrid_retrieval_service
    saved_rewr = ai_stream_mod.chat_query_rewrite_service
    saved_llm = ai_stream_service._llm
    ai_stream_mod.hybrid_retrieval_service = fake_retr
    ai_stream_mod.chat_query_rewrite_service = fake_rewr
    if ai_stream_service._llm is None:
        ai_stream_service._llm = ChatDeepSeek(
            api_key=SecretStr("dummy"), model="deepseek-flash"
        )
    try:
        persist2: list[dict] = []
        agent = ai_stream_service.create_turn_agent(
            None, 5, persist2, _plan("kb"), "差旅谁审批？"
        )
        tools = _agent_tools(agent)
        retrieve_tool = tools["retrieve_knowledge"]
        rewrite_tool = tools["rewrite_query"]

        # 第一次：不切题 → insufficient，不产出 sources
        res1 = await retrieve_tool.ainvoke({"query": "差旅审批"})
        assert res1["insufficient"] is True and res1["items"] == [] and persist2 == [], res1
        # 改写 → 新词
        rw = await rewrite_tool.ainvoke({})
        assert rw["query"] == "差旅审批-改写" and rw["previousQuery"] == "差旅审批", rw
        assert fake_rewr.rewrite_calls == ["差旅审批"], fake_rewr.rewrite_calls
        # 第二次：切题 → 产出 sources，编号 [1]
        res2 = await retrieve_tool.ainvoke({"query": rw["query"]})
        assert res2.get("insufficient") is None, res2
        assert res2["items"][0]["index"] == 1 and "[1]" in res2["context"], res2
        assert [s["index"] for s in persist2] == [1], persist2
        # 第三次：直接拦下，不再检索
        res3 = await retrieve_tool.ainvoke({"query": "第三次"})
        assert res3["insufficient"] is True and "最多检索两次" in res3.get("error", ""), res3
        # top_k 未传时应回退请求默认值 5（防「工具参数遮蔽外层默认值」回归）
        assert fake_retr.calls == [("差旅审批", 5), ("差旅审批-改写", 5)], fake_retr.calls
        print(" 4) retrieve_knowledge ✓ 首轮 insufficient、改写再查切题、最多两次、top_k 回退默认")
    finally:
        ai_stream_mod.hybrid_retrieval_service = saved_retr
        ai_stream_mod.chat_query_rewrite_service = saved_rewr
        ai_stream_service._llm = saved_llm

    # 5) 隔离配置：空 callbacks 能摘掉继承的回调；tag 用于兜底过滤
    assert _INTERNAL_CALL_CONFIG["callbacks"] == [], _INTERNAL_CALL_CONFIG
    assert _INTERNAL_CALL_CONFIG["tags"] == [KH_INTERNAL_TAG], _INTERNAL_CALL_CONFIG

    class _H(BaseCallbackHandler):
        pass

    tok = var_child_runnable_config.set({"callbacks": [_H()], "tags": ["agent-run"]})
    try:
        cfg = ensure_config(_INTERNAL_CALL_CONFIG)
        assert cfg["callbacks"] == [], "内部调用的 callbacks 应被清空（摘掉 StreamMessagesHandler）"
        assert KH_INTERNAL_TAG in cfg["tags"], "应带 kh-internal tag 供兜底过滤"
    finally:
        var_child_runnable_config.reset(tok)
    print(" 5) 隔离配置 ✓ 空 callbacks + kh-internal tag")

    # 6) retrieve_graph 工具 + 图谱纯函数
    # 6a) 纯函数（split / format / merge）
    assert split_graph_keywords("发票 报销/差旅") == ["发票", "报销", "差旅"]
    hit_g = {
        "query": "差旅",
        "entities": [{"name": "差旅", "type": "CONCEPT", "description": None}],
        "relations": [{"source": "差旅", "relation": "需要", "target": "审批"}],
        "documents": [{"documentId": "d1", "title": "差旅管理办法"}],
    }
    assert "- 差旅 → 审批（需要）" in format_graph_context(hit_g), hit_g
    assert "来源文档：差旅管理办法" in format_graph_context(hit_g), hit_g
    assert (
        format_graph_context(
            {"query": "x", "entities": [], "relations": [], "documents": []}
        )
        == ""
    ), "空命中应返回空串"
    assert "【本轮知识图谱】" in format_graph_system_text(hit_g), hit_g
    merged = merge_graph_hits(
        hit_g,
        {
            "query": "审批",
            "entities": [{"name": "审批", "type": None, "description": None}],
            "relations": [{"source": "差旅", "relation": "需要", "target": "审批"}],
            "documents": [],
        },
    )
    assert merged["query"] == "差旅 / 审批", merged["query"]
    assert [e["name"] for e in merged["entities"]] == ["差旅", "审批"], merged
    assert len(merged["relations"]) == 1, merged

    # 6b) 工具：注入假图谱，按 kb 建 agent 后直接 invoke
    fake_graph = _FakeGraph()
    saved_graph = ai_stream_mod.graph_build_service
    saved_llm = ai_stream_service._llm
    ai_stream_mod.graph_build_service = fake_graph
    if ai_stream_service._llm is None:
        ai_stream_service._llm = ChatDeepSeek(
            api_key=SecretStr("dummy"), model="deepseek-flash"
        )
    try:
        agent = ai_stream_service.create_turn_agent(None, 5, [], _plan("kb"), "差旅谁审批？")
        tool = _agent_tools(agent)["retrieve_graph"]
        res = await tool.ainvoke({"query": "发票"})
        assert res["relations"][0]["target"] == "审批", res
        assert "差旅 → 审批" in res["context"], res["context"]
        assert res["insufficient"] is False, res
        # keywords = plan.graph_queries（差旅/审批）+ 工具入参（发票）
        assert fake_graph.calls == [(["差旅", "审批", "发票"], 8)], fake_graph.calls
        print(" 6) retrieve_graph ✓ 纯函数 + 工具（plan 词 + 入参、关系/来源/context）")
    finally:
        ai_stream_mod.graph_build_service = saved_graph
        ai_stream_service._llm = saved_llm

    print("\nAgentic RAG 冒烟测试全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())

"""AI 流式对话 E2E 冒烟（真实 ES 检索 + 真实 DeepSeek 流式 + 真实 PG 落库）

裸 FastAPI app + ASGITransport，SSE 逐帧解析，断言 UI Message Stream 协议：
  1. 未登录 → 401
  2. 响应头 x-vercel-ai-ui-message-stream: v1
  3. 事件顺序：start → data-session → data-status → data-intent →
     [data-eval → data-sources → (source-document)*] →
     [tool-*/reasoning-*/text-*] → finish → [DONE]
     （中括号段为 agentic 检索循环，意图不允许检索或模型未调用工具时可不出现）
  4. 正文非空（text-delta 聚合）
  5. 落库：assistant 消息内容 == 流式回答；二轮对话同会话消息数 2 → 4
  6. 临时会话 DELETE 清理零残留

运行：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_ai_stream.py

前置：ES / PG / DeepSeek API Key 就绪（连接读 backend/.env）。
"""
import asyncio
import json
import sys

import httpx

from app.db.session import engine
from app.main import app
from scripts.smoke_auth import (
    SEED_USER_ID,
    SEED_USER_USERNAME,
    bearer_headers,
)

BASE = "http://testserver/api/v1"

_results: list[str] = []


def check(step: str, ok: bool, detail: str) -> None:
    mark = "✅" if ok else "❌"
    _results.append(f"{mark} {step}: {detail}")
    if not ok:
        raise AssertionError(f"{step} 失败：{detail}")


def _parse_sse(lines: list[str]) -> list[dict]:
    """data: {json} 帧 → 事件列表（丢弃 [DONE] 与空行）"""
    events = []
    for line in lines:
        line = line.rstrip("\n")
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        events.append(json.loads(payload))
    return events


def _types_of(events: list[dict]) -> list[str]:
    return [e.get("type", "?") for e in events]


async def _stream_chat(client: httpx.AsyncClient, headers: dict, body: dict) -> tuple[list[str], list[dict]]:
    lines: list[str] = []
    async with client.stream("POST", f"{BASE}/ai/chat/stream", json=body, headers=headers) as resp:
        assert resp.status_code == 200, f"期望 200，实际 {resp.status_code}"
        proto_header = resp.headers.get("x-vercel-ai-ui-message-stream")
        check("协议头", proto_header == "v1", f"x-vercel-ai-ui-message-stream={proto_header}")
        async for line in resp.aiter_lines():
            lines.append(line)
    return lines, _parse_sse(lines)


async def main() -> None:
    print("=== AI 流式对话冒烟 ===")
    engine.echo = False

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=180) as client:
        user_headers = bearer_headers(SEED_USER_ID, SEED_USER_USERNAME)

        # 1) 未登录 → 401
        r = await client.post(f"{BASE}/ai/chat/stream", json={"messages": []})
        check("未登录", r.status_code == 401, f"期望 401，实际 {r.status_code}")

        # 2) 第一轮流式对话（无 sessionId → 新建会话）
        q1 = "用一句话介绍知识库的用途"
        lines, events = await _stream_chat(
            client, user_headers, {"messages": [{"role": "user", "parts": [{"type": "text", "text": q1}]}]}
        )
        types = _types_of(events)
        check("start 首帧", bool(types) and types[0] == "start", f"首帧={types[0] if types else '无'}")
        check("finish 收口", types[-1] == "finish", f"末帧={types[-1]}, DONE={'data: [DONE]' in [l.strip() for l in lines]}")

        def _idx(t: str) -> int:
            return types.index(t) if t in types else -1

        check(
            "data 事件顺序",
            0 < _idx("data-session") < _idx("data-status") < _idx("data-intent"),
            f"session={_idx('data-session')}, status={_idx('data-status')}, "
            f"intent={_idx('data-intent')}",
        )
        # 检索系事件：仅当意图允许检索且模型调用了工具时才出现（agentic 循环由模型驱动）
        if _idx("data-sources") != -1:
            check(
                "检索事件先于来源",
                _idx("data-eval") < _idx("data-sources"),
                f"eval={_idx('data-eval')}, sources={_idx('data-sources')}",
            )
        else:
            print("   （本轮无检索事件：意图未允许检索或模型未调用工具）")
        session_id = next(
            (e["data"]["sessionId"] for e in events if e.get("type") == "data-session"), None
        )
        check("sessionId 返回", bool(session_id), f"sessionId={session_id}")
        answer = "".join(
            e["delta"] for e in events if e.get("type") == "text-delta" and e.get("delta")
        )
        check("正文非空", len(answer.strip()) > 0, f"answerLength={len(answer.strip())}")
        n_reasoning = sum(
            len(e.get("delta") or "") for e in events if e.get("type") == "reasoning-delta"
        )
        print(f"   思考量: {n_reasoning} 字符（0 表示本轮模型未输出思考，不阻断）")
        n_source_docs = sum(1 for t in types if t == "source-document")
        print(f"   source-document: {n_source_docs} 条（0 表示知识库无召回）")

        # 3) 落库：2 条消息，assistant 内容 == 流式回答
        r = await client.get(f"{BASE}/ai/sessions/{session_id}/messages", headers=user_headers)
        rows = r.json()
        check(
            "落库消息成对",
            r.status_code == 200 and [m["role"] for m in rows] == ["user", "assistant"],
            f"roles={[m['role'] for m in rows]}",
        )
        check(
            "assistant 内容一致",
            rows[1]["content"].strip() == answer.strip(),
            f"db={rows[1]['content'][:40]!r}... stream={answer[:40]!r}...",
        )
        check(
            "sources 引用过滤形态",
            rows[1]["sources"] is None or isinstance(rows[1]["sources"], list),
            f"sources 类型={type(rows[1]['sources']).__name__}",
        )

        # 4) 第二轮（带 sessionId → touch_title，标题已非默认不再覆盖）
        q2 = "再补充一点"
        _, events2 = await _stream_chat(
            client,
            user_headers,
            {
                "sessionId": session_id,
                "messages": [{"role": "user", "parts": [{"type": "text", "text": q2}]}],
            },
        )
        session_id2 = next(
            (e["data"]["sessionId"] for e in events2 if e.get("type") == "data-session"), None
        )
        check("二轮同会话", session_id2 == session_id, f"{session_id2} vs {session_id}")
        r = await client.get(f"{BASE}/ai/sessions/{session_id}/messages", headers=user_headers)
        rows2 = r.json()
        check(
            "二轮消息 4 条",
            len(rows2) == 4 and rows2[2]["role"] == "user" and rows2[2]["content"] == q2,
            f"count={len(rows2)}, roles={[m['role'] for m in rows2]}",
        )

        # 5) 清理临时会话
        r = await client.delete(f"{BASE}/ai/sessions/{session_id}", headers=user_headers)
        check("清理会话", r.status_code == 200, f"期望 200，实际 {r.status_code}")
        r = await client.get(f"{BASE}/ai/sessions/{session_id}/messages", headers=user_headers)
        check("清理后 404", r.status_code == 404, f"期望 404，实际 {r.status_code}")

    print("\nAI 流式对话冒烟全部通过 ✅")


async def run() -> None:
    try:
        await main()
        print("\n".join(_results))
    except Exception:
        print("冒烟测试失败", file=sys.stderr)
        raise
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())

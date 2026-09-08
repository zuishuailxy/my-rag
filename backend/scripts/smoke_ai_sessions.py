"""AI 会话接口 + 问答落库 E2E 冒烟（真实 PostgreSQL，裸 FastAPI app + ASGITransport）

覆盖：
  1. 未登录 → 401
  2. POST /ai/sessions：显式标题 201 / 缺省标题「新对话」201
  3. GET /ai/sessions：分页返回 + pageSize/total；page=0 → 422
  4. PATCH /ai/sessions/{id}：改名（trim）；跨用户 404；非法 id 404
  5. GET /ai/sessions/{id}/messages：空列表；跨用户 404
  6. DELETE /ai/sessions/{id}：200 已删除，删后再查 404
  7. chat_session_service.append_turn（服务层直测）：
     无 sessionId 新建（标题取首问）/ 默认标题被首问覆盖 / 消息成对落库
     / sources JSONB 往返 / 标题不变时 updated_at 仍前进

运行：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_ai_sessions.py

前置：docker 的 postgres 已就绪（连接读 backend/.env）。
"""
import asyncio
import sys
import uuid

import httpx
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from app.db.session import async_session_factory, engine
from app.main import app
from app.services.chat_session import chat_session_service
from scripts.smoke_auth import (
    SEED_ADMIN_ID,
    SEED_ADMIN_USERNAME,
    SEED_USER_ID,
    SEED_USER_USERNAME,
    bearer_headers,
)

BASE = "http://testserver/api/v1"

_TAG = f"SmokeSess_{uuid.uuid4().hex[:8]}"
_SESSION_IDS: list[int] = []  # 所有新建会话 id，finally 兜底清理

_results: list[str] = []


def check(step: str, ok: bool, detail: str) -> None:
    mark = "✅" if ok else "❌"
    _results.append(f"{mark} {step}: {detail}")
    if not ok:
        raise AssertionError(f"{step} 失败：{detail}")


async def _cleanup() -> None:
    """按记录 id 兜底清理（先消息后会话），不依赖标题匹配"""
    if not _SESSION_IDS:
        return
    async with async_session_factory() as session:
        await session.execute(
            sa_delete(_AiMessage).where(_AiMessage.session_id.in_(_SESSION_IDS))
        )
        await session.execute(
            sa_delete(_AiSession).where(_AiSession.id.in_(_SESSION_IDS))
        )
        await session.commit()


async def _count_by_prefix() -> int:
    from sqlalchemy import func

    async with async_session_factory() as session:
        stmt = (
            select(func.count())
            .select_from(_AiSession)
            .where(_AiSession.title.like(f"%{_TAG}%"))
        )
        return int(await session.scalar(stmt) or 0)


async def main() -> None:
    print("=== AI 会话接口冒烟 ===")
    engine.echo = False

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        user_headers = bearer_headers(SEED_USER_ID, SEED_USER_USERNAME)
        admin_headers = bearer_headers(SEED_ADMIN_ID, SEED_ADMIN_USERNAME)

        # 1) 未登录 → 401
        r = await client.get(f"{BASE}/ai/sessions")
        check("未登录", r.status_code == 401, f"期望 401，实际 {r.status_code}")

        # 2) 新建：显式标题
        title_a = f"{_TAG} 甲"
        r = await client.post(
            f"{BASE}/ai/sessions", json={"title": title_a}, headers=user_headers
        )
        check("新建会话 201", r.status_code == 201, f"期望 201，实际 {r.status_code}")
        a = r.json()
        check(
            "新建响应字段",
            isinstance(a.get("id"), str)
            and a.get("userId") == SEED_USER_ID
            and a.get("title") == title_a,
            f"id={a.get('id')}, userId={a.get('userId')}, title={a.get('title')}",
        )
        _SESSION_IDS.append(int(a["id"]))

        # 3) 新建：缺省标题 → 「新对话」
        r = await client.post(f"{BASE}/ai/sessions", json={}, headers=user_headers)
        check("缺省标题 201", r.status_code == 201, f"期望 201，实际 {r.status_code}")
        b = r.json()
        check(
            "默认标题",
            b.get("title") == "新对话",
            f"title={b.get('title')}",
        )
        _SESSION_IDS.append(int(b["id"]))

        # 4) 分页：命中两个新会话 + pageSize 透传
        r = await client.get(
            f"{BASE}/ai/sessions?page=1&pageSize=10", headers=user_headers
        )
        check("分页 200", r.status_code == 200, f"期望 200，实际 {r.status_code}")
        page = r.json()
        got_ids = {item["id"] for item in page["items"]}
        check(
            "分页命中",
            a["id"] in got_ids and b["id"] in got_ids,
            f"total={page['total']}, pageSize={page['pageSize']}, items={len(page['items'])}",
        )
        check(
            "分页字段回显",
            page["page"] == 1 and page["pageSize"] == 10,
            f"page={page['page']}, pageSize={page['pageSize']}",
        )

        # 5) page=0 → 422
        r = await client.get(f"{BASE}/ai/sessions?page=0", headers=user_headers)
        check("page=0 越界", r.status_code == 422, f"期望 422，实际 {r.status_code}")

        # 6) 改名：trim 生效
        r = await client.patch(
            f"{BASE}/ai/sessions/{a['id']}",
            json={"title": f"  {_TAG} 改名  "},
            headers=user_headers,
        )
        check("改名 200", r.status_code == 200, f"期望 200，实际 {r.status_code}")
        check(
            "改名 trim",
            r.json().get("title") == f"{_TAG} 改名",
            f"title={r.json().get('title')!r}",
        )

        # 7) 消息列表：空
        r = await client.get(
            f"{BASE}/ai/sessions/{a['id']}/messages", headers=user_headers
        )
        check("空消息列表", r.status_code == 200 and r.json() == [], f"实际 {r.json()}")

        # 8) 跨用户：admin 访问 user 的会话 → 404
        r = await client.get(
            f"{BASE}/ai/sessions/{a['id']}/messages", headers=admin_headers
        )
        check("跨用户 404", r.status_code == 404, f"期望 404，实际 {r.status_code}")

        # 9) 非法 id → 404
        r = await client.get(
            f"{BASE}/ai/sessions/not-a-number/messages", headers=user_headers
        )
        check("非法 id 404", r.status_code == 404, f"期望 404，实际 {r.status_code}")

        # 10) 删除：200 + 删后再查 404
        r = await client.delete(f"{BASE}/ai/sessions/{b['id']}", headers=user_headers)
        check("删除 200", r.status_code == 200, f"期望 200，实际 {r.status_code}")
        check(
            "删除消息",
            r.json().get("message") == "已删除",
            f"message={r.json().get('message')}",
        )
        _SESSION_IDS.remove(int(b["id"]))
        r = await client.get(
            f"{BASE}/ai/sessions/{b['id']}/messages", headers=user_headers
        )
        check("删后查询 404", r.status_code == 404, f"期望 404，实际 {r.status_code}")

    # ---- 服务层 append_turn 直测（不调 LLM，验证落库路径）----
    print("=== append_turn 落库直测 ===")
    async with async_session_factory() as db:
        # 11) 无 sessionId：新建 + 标题取首问（短问题，不截断）
        q1 = f"{_TAG} 首问？"
        sess1 = await chat_session_service.append_turn(
            db, SEED_USER_ID, None, q1, "答一", []
        )
        _SESSION_IDS.append(sess1.id)
        check(
            "首问建标题",
            sess1.title == q1,
            f"title={sess1.title!r}",
        )
        rows = await chat_session_service.list_messages(db, SEED_USER_ID, str(sess1.id))
        check(
            "消息成对升序",
            [m.role for m in rows] == ["user", "assistant"],
            f"roles={[m.role for m in rows]}",
        )
        check(
            "空 sources 落 NULL",
            rows[1].sources is None,
            f"sources={rows[1].sources}",
        )

        # 12) 默认标题会话：append_turn 后标题被首问覆盖
        sess2 = await chat_session_service.create(
            db, SEED_USER_ID, _CreateSession(title=None)
        )
        _SESSION_IDS.append(sess2.id)
        q2 = f"{_TAG} 覆盖标题？"
        await chat_session_service.append_turn(
            db, SEED_USER_ID, str(sess2.id), q2, "答二", None
        )
        await db.refresh(sess2)
        check(
            "默认标题被覆盖",
            sess2.title == q2,
            f"title={sess2.title!r}",
        )

        # 13) 标题非默认：标题保持不变，updated_at 前进；sources JSONB 往返
        before = sess2.updated_at
        src = [
            {
                "index": 1,
                "documentId": "999",
                "documentTitle": f"{_TAG} 文档",
                "heading": None,
                "excerpt": "摘录",
                "score": 0.9,
            }
        ]
        sess2 = await chat_session_service.append_turn(
            db, SEED_USER_ID, str(sess2.id), f"{_TAG} 二问？", "答三", src
        )
        check("标题保持", sess2.title == q2, f"title={sess2.title!r}")
        check(
            "updated_at 前进",
            sess2.updated_at > before,
            f"{before} -> {sess2.updated_at}",
        )
        rows = await chat_session_service.list_messages(db, SEED_USER_ID, str(sess2.id))
        last = rows[-1]
        check(
            "sources JSONB 往返",
            last.sources is not None
            and last.sources[0]["documentTitle"] == f"{_TAG} 文档",
            f"sources={last.sources}",
        )
        check(
            "消息总数 4",
            len(rows) == 4,
            f"count={len(rows)}",
        )

    print("\nAI 会话接口冒烟全部通过 ✅")


# 延迟导入别名（保持 import 区整洁，同时避免与 schemas 混淆）
from app.models.ai_session import AiMessage as _AiMessage  # noqa: E402
from app.models.ai_session import AiSession as _AiSession  # noqa: E402
from app.schemas.ai import CreateSessionRequest as _CreateSession  # noqa: E402


async def run() -> None:
    try:
        await main()
        print("\n".join(_results))
    except Exception:
        print("冒烟测试失败", file=sys.stderr)
        raise
    finally:
        await _cleanup()
        # 清理完成后按标题前缀兜底复查（零残留）
        leftover = await _count_by_prefix()
        if leftover:
            print(f"❌ 零残留: 清理后仍残留 {leftover} 条 {_TAG} 会话", file=sys.stderr)
            sys.exit(1)
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())

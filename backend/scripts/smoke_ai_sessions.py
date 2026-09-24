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
  8. chat_session_service.list_recent_messages（服务层直测）：
     取最近 N 条且**时间正序**（不是最旧 N 条）/ limit<=0 兜底为 1 条（不退化成
     不限量）/ limit 超总数返回全部 / 跨用户 404 / 非法 id 404
  9. 回填链路：短期记忆 miss → list_recent_messages 查库 → db_rows_to_messages
     → save → 再命中原内容。⚠️ 本步需要 Redis，未就绪时打印 SKIP 且**不算通过**
     （本脚本的主前置仍只有 postgres，不因它改变可运行条件）

运行：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_ai_sessions.py

前置：docker 的 postgres 已就绪（连接读 backend/.env）；第 9 步额外需要 redis。
"""
import asyncio
import sys
import uuid

import httpx
from fastapi import HTTPException
from langchain_core.messages import HumanMessage
from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from app.db.redis import redis_service
from app.db.session import async_session_factory, engine
from app.main import app
from app.services.chat_memory import db_rows_to_messages
from app.services.chat_session import chat_session_service
from app.services.chat_short_memory import chat_short_memory_service
from scripts.smoke_auth import (
    SEED_ADMIN_ID,
    SEED_ADMIN_USERNAME,
    SEED_USER_ID,
    SEED_USER_USERNAME,
    bearer_headers,
)

BASE = "http://testserver/api/v1"

_TAG = f"SmokeSess_{uuid.uuid4().hex[:8]}"
_SESSION_IDS: list[int] = [] # 所有新建会话 id，finally 兜底清理
# 写过 Redis 热窗口的会话 id（第 9 步回填用）：断言中途失败也要清，
# 否则 key 会活满 TTL（86400s），下次跑冒烟时「回填前 miss」断言会假红
_MEM_SESSION_IDS: list[str] = []

_results: list[str] = []


def check(step: str, ok: bool, detail: str) -> None:
    mark = "✅" if ok else "❌"
    _results.append(f"{mark} {step}: {detail}")
    if not ok:
        raise AssertionError(f"{step} 失败：{detail}")


async def _cleanup() -> None:
    """按记录 id 兜底清理（先消息后会话），不依赖标题匹配；顺带清 Redis 热窗口"""
    # Redis 不可用时 clear 内部只 WARN，不阻塞收尾
    for sid in _MEM_SESSION_IDS:
        await chat_short_memory_service.clear(SEED_USER_ID, sid)
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


async def _redis_ready() -> bool:
    """Redis 探活。默认在 main 开始时调一次，供两处 Redis 相关断言共用。

    ⚠️ 必须显式 ping：ChatShortMemoryService 的 save / clear 都「吞异常只 WARN」，
    Redis 没起时 save 静默失败 → try_load 返回 None，「删除清热窗口」断言会**假绿**。
    不可用时相关步骤打印 SKIP 且不计入通过 —— 本脚本主前置仍只有 postgres，
    不因新增步骤抬高门槛。
    """
    await redis_service.start()
    client = redis_service.client
    if client is None:
        return False
    try:
        await client.ping()
        return True
    except Exception: # noqa: BLE001
        return False


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
    # Redis 探活（一次性）：供「删除清热窗口」与「回填链路」两步共用。
    # 不可用时这两步 SKIP 且不算通过——PG 才是本脚本的硬前置。
    redis_ok = await _redis_ready()
    if not redis_ok:
        print(" (Redis 不可用：删除清缓存 / 回填链路两步将 SKIP，不计入通过)")

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
            json={"title": f" {_TAG} 改名 "},
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
        # 10a) 删除前先往该会话的 Redis 热窗口塞一条，用于验证「删除会清缓存」
        # 
        if redis_ok:
            await chat_short_memory_service.save(
                SEED_USER_ID, b["id"], [HumanMessage(f"{_TAG} 待清理的热窗口")]
            )
            check(
                "删除前热窗口存在",
                await chat_short_memory_service.try_load(SEED_USER_ID, b["id"]) is not None,
                "save 后应能命中",
            )
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

        # 10b) 删除会话同时清掉 Redis 热窗口：只删库不清缓存的话，已被删除的会话
        # 内容会在 TTL 内继续被当作「历史记忆」召回（表现为删了还在影响回答）
        if not redis_ok:
            print(" SKIP: Redis 未连接，「删除清热窗口」未验证（不算通过）")
        else:
            check(
                "删除清热窗口",
                await chat_short_memory_service.try_load(SEED_USER_ID, b["id"]) is None,
                "删除后窗口应 miss",
            )

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

    # ---- 服务层 list_recent_messages 边界直测----
    print("=== list_recent_messages 边界直测 ===")
    async with async_session_factory() as db:
        sess3 = await chat_session_service.create(
            db, SEED_USER_ID, _CreateSession(title=f"{_TAG} 近期")
        )
        _SESSION_IDS.append(sess3.id)
        sid3 = str(sess3.id)
        # 3 轮问答 → 6 条消息，id 递增：问1 答1 问2 答2 问3 答3
        for i in (1, 2, 3):
            await chat_session_service.append_turn(
                db, SEED_USER_ID, sid3, f"{_TAG} 问{i}", f"答{i}", None
            )
        full = await chat_session_service.list_messages(db, SEED_USER_ID, sid3)
        check("6 条消息就绪", len(full) == 6, f"count={len(full)}")

        # 14) 取最近 3 条：必须是「最新的三条」且按时间正序返回
        recent = await chat_session_service.list_recent_messages(
            db, SEED_USER_ID, sid3, 3
        )
        check(
            "最近 3 条为最新且正序",
            [m.id for m in recent] == [m.id for m in full[-3:]],
            f"recent={[m.id for m in recent]} 期望={[m.id for m in full[-3:]]}",
        )
        check(
            "最近 3 条内容",
            [m.content for m in recent] == ["答2", f"{_TAG} 问3", "答3"],
            f"contents={[m.content for m in recent]}",
        )

        # 15) limit 边界：<=0 兜底 1 条（)，
        # 绝不退化成「不限量」）；超过总数时返回全部
        for bad_limit in (0, -7):
            one = await chat_session_service.list_recent_messages(
                db, SEED_USER_ID, sid3, bad_limit
            )
            check(
                f"limit={bad_limit} 兜底 1 条",
                len(one) == 1 and one[0].id == full[-1].id,
                f"count={len(one)}, ids={[m.id for m in one]}",
            )
        many = await chat_session_service.list_recent_messages(
            db, SEED_USER_ID, sid3, 100
        )
        check(
            "limit 超总数返回全部",
            [m.id for m in many] == [m.id for m in full],
            f"count={len(many)}",
        )

        # 16) 越权 / 非法 id：与 list_messages 同口径 404
        for label, uid, target in (
            ("跨用户", SEED_ADMIN_ID, sid3),
            ("非法 id", SEED_USER_ID, "not-a-number"),
        ):
            try:
                await chat_session_service.list_recent_messages(db, uid, target, 5)
                check(f"{label} 404", False, "未抛 404")
            except HTTPException as err:
                check(f"{label} 404", err.status_code == 404, f"实际 {err.status_code}")

    # ---- 回填链路（需要 Redis；未就绪 SKIP 且不算通过）----
    print("=== 回填链路（miss → 查库 → 回填）===")
    await _backfill_roundtrip(redis_ok)

    print("\nAI 会话接口冒烟全部通过 ✅")


# 延迟导入别名（保持 import 区整洁，同时避免与 schemas 混淆）
from app.models.ai_session import AiMessage as _AiMessage # noqa: E402
from app.models.ai_session import AiSession as _AiSession # noqa: E402
from app.schemas.ai import CreateSessionRequest as _CreateSession # noqa: E402


async def _backfill_roundtrip(redis_ok: bool) -> None:
    """Redis 热窗口 miss → 查库 → 回填 的完整链路

    这是「回填」的唯一权威路径，调用方（ai_stream / ai_chat）在 try_load 拿到
    None 后应当：list_recent_messages → db_rows_to_messages → short_memory.save。
    本步把整条链路真跑一遍，确认「回填后能命中、内容保真、角色分类正确」。

    Redis 未就绪（redis_ok=False）时打印 SKIP 并 return —— **不算通过**，
    也不让本冒烟整体失败（主前置只有 postgres）。
    """
    if not redis_ok:
        print(" SKIP: Redis 未连接，本步未验证（不算通过）")
        return

    async with async_session_factory() as db:
        sess = await chat_session_service.create(
            db, SEED_USER_ID, _CreateSession(title=f"{_TAG} 回填")
        )
        _SESSION_IDS.append(sess.id)
        sid = str(sess.id)
        _MEM_SESSION_IDS.append(sid)
        for i in (1, 2, 3):
            await chat_session_service.append_turn(
                db, SEED_USER_ID, sid, f"{_TAG} 回填问{i}", f"回填答{i}", None
            )

        # 先确认真的是 miss（不是 miss 就不会走回填分支，本步失去意义）
        await chat_short_memory_service.clear(SEED_USER_ID, sid)
        check(
            "回填前 miss",
            await chat_short_memory_service.try_load(SEED_USER_ID, sid) is None,
            "应返回 None（语义 = 请回填数据库）",
        )

        # 回填：查库 → 转 LangChain 消息 → 写窗口
        rows = await chat_session_service.list_recent_messages(
            db, SEED_USER_ID, sid, chat_short_memory_service.window_size
        )
        msgs = db_rows_to_messages(rows)
        check("查库转换 6 条", len(msgs) == 6, f"count={len(msgs)}")
        check(
            "角色类型交替",
            [type(m).__name__ for m in msgs] == ["HumanMessage", "AIMessage"] * 3,
            f"types={[type(m).__name__ for m in msgs]}",
        )
        await chat_short_memory_service.save(SEED_USER_ID, sid, msgs)

        loaded = await chat_short_memory_service.try_load(SEED_USER_ID, sid)
        check(
            "回填后命中且内容保真",
            loaded is not None and [m.content for m in loaded] == [m.content for m in msgs],
            f"loaded={None if loaded is None else [m.content for m in loaded]}",
        )


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

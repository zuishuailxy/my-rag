"""对话短期记忆（Redis 热窗口）冒烟测试（真实 Redis）

验证点：
1. 未命中：key 不存在（或已 clear）时 tryLoad 返回 None —— 语义是「请回落数据库」
2. 往返：save 后 tryLoad 拿回同类型同内容的消息（human / ai 各归其类）
3. 窗口截断：写入超过 maxMessages 时只保留**最近** N 条（)`）
4. 有效消息过滤：system / tool 不进窗口，只存 human/ai 原文
5. appendTurn：history + 本轮问答整体回写，末两条顺序正确
6. TTL：写入带过期（0 < ttl <= CHAT_SHORT_MEMORY_TTL_SECONDS）
7. key 形态：`{PREFIX}:{userId}:{sessionId}:messages`
8. 空数组保真：save([]) 后 tryLoad 返回 **[]**（不是 None），
   调用方不能把 [] 当 miss，否则会白白回填一次
9. 脏数据容错：值是非法 JSON / 不是数组时返回 None 且不抛（失败只 WARN）
10. clear：删除后回到未命中
11. 纯函数：db_rows_to_messages 的 role / 空正文过滤；compact_rewrite_context 的
    窗口与截断；message_text 的 str / 块列表 / None 三种 content 形态

⚠️ 本模块的 save / clear 都「吞异常只 WARN」（缓存不可用不能影响对话），
所以冒烟第 0 步必须**显式 ping 确认 Redis 真的通** —— 否则 save 静默失败、
try_load 返回 None，断言会假绿。

运行：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_chat_short_memory.py

前置：docker 的 redis 已就绪（连接读 backend/.env）。

不污染任何持久层：只用一个临时 user/session 组合的 key，结束前 clear。
"""
import asyncio
import json
import sys

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.core.config import settings
from app.db.redis import redis_service
from app.models.ai_session import AiMessage
from app.services.chat_memory import (
    compact_rewrite_context,
    db_rows_to_messages,
    is_working_message,
    message_text,
)
from app.services.chat_short_memory import chat_short_memory_service

# 临时身份（不用种子用户，避免与真实会话窗口撞 key）
USER_ID = "990000000000000901"
SESSION_ID = "990000000000000902"
# 期望 key：()` 同形
EXPECTED_KEY = (
    f"{settings.CHAT_SHORT_MEMORY_KEY_PREFIX}:{USER_ID}:{SESSION_ID}:messages"
)


async def cleanup() -> None:
    """清掉本次用的 key（幂等；Redis 不可用时不阻塞收尾）"""
    try:
        await chat_short_memory_service.clear(USER_ID, SESSION_ID)
    except Exception as err: # noqa: BLE001
        print(f" (cleanup 跳过：{err})", file=sys.stderr)


async def main() -> None:
    # 0) 前置：Redis 必须真的可用（否则后续 save 会静默失败 → 假绿）
    await redis_service.start()
    client = redis_service.client
    assert client is not None, "Redis 未连接（redis_service.start() 失败）"
    await client.ping() # 抛错即中断，不继续
    print(" 0) Redis 连接 ✓ 已确认可用（save 吞异常，必须显式探活）")

    await chat_short_memory_service.clear(USER_ID, SESSION_ID)

    # 1) 未命中 → None
    assert await chat_short_memory_service.try_load(USER_ID, SESSION_ID) is None, (
        "key 不存在时应返回 None（语义 = 请回填数据库），而不是空列表"
    )
    print(" 1) tryLoad 未命中 ✓ 返回 None")

    # 2) 往返：类型与内容都不丢
    window = [HumanMessage("第一问"), AIMessage("第一答"), HumanMessage("第二问")]
    await chat_short_memory_service.save(USER_ID, SESSION_ID, window)
    loaded = await chat_short_memory_service.try_load(USER_ID, SESSION_ID)
    assert loaded is not None, "save 后应能读回"
    assert [type(m).__name__ for m in loaded] == [
        "HumanMessage",
        "AIMessage",
        "HumanMessage",
    ], f"消息类型应保真，实际 {[type(m).__name__ for m in loaded]}"
    assert [m.content for m in loaded] == ["第一问", "第一答", "第二问"], (
        f"消息内容应保真，实际 {[m.content for m in loaded]}"
    )
    print(" 2) save → tryLoad 往返 ✓ 3 条，类型与内容保真")

    # 3) 窗口截断：只留最近 N 条
    n = chat_short_memory_service.window_size
    assert n == settings.CHAT_SHORT_MEMORY_MAX_MESSAGES, "windowSize 应等于配置值"
    many = [
        HumanMessage(f"问题{i}") if i % 2 == 0 else AIMessage(f"回答{i}")
        for i in range(n + 5)
    ]
    await chat_short_memory_service.save(USER_ID, SESSION_ID, many)
    trimmed = await chat_short_memory_service.try_load(USER_ID, SESSION_ID)
    assert trimmed is not None
    assert len(trimmed) == n, f"窗口应截断到 {n} 条，实际 {len(trimmed)}"
    # 保留的必须是**最后** N 条（不是最早的 N 条）
    assert [m.content for m in trimmed] == [m.content for m in many[-n:]], (
        "窗口截断应保留最近 N 条（)）"
    )
    print(f" 3) 窗口截断 ✓ 写入 {len(many)} 条 → 只留最近 {n} 条")

    # 4) 有效消息过滤：system / tool 不入窗口
    mixed = [
        SystemMessage("Mem0 系统提示，不该进窗口"),
        HumanMessage("用户问"),
        ToolMessage(content="检索工具返回的原始片段", tool_call_id="t1"),
        AIMessage("助手答"),
        SystemMessage("又一条系统消息"),
    ]
    await chat_short_memory_service.save(USER_ID, SESSION_ID, mixed)
    only_working = await chat_short_memory_service.try_load(USER_ID, SESSION_ID)
    assert only_working is not None
    assert [m.content for m in only_working] == ["用户问", "助手答"], (
        f"只应保留 human/ai，实际 {[m.content for m in only_working]}"
    )
    print(" 4) 有效消息过滤 ✓ system / tool 未进窗口")

    # 5) appendTurn：history + 本轮问答
    history = [HumanMessage("历史问"), AIMessage("历史答")]
    await chat_short_memory_service.append_turn(
        USER_ID, SESSION_ID, history, "本轮问", "本轮答"
    )
    after = await chat_short_memory_service.try_load(USER_ID, SESSION_ID)
    assert after is not None
    assert [m.content for m in after] == ["历史问", "历史答", "本轮问", "本轮答"], (
        f"appendTurn 顺序应为 history → 问 → 答，实际 {[m.content for m in after]}"
    )
    assert isinstance(after[-2], HumanMessage) and isinstance(after[-1], AIMessage), (
        "末两条应分别是 HumanMessage / AIMessage"
    )
    print(" 5) appendTurn ✓ history + 本轮问答，末两条为 问 / 答")

    # 6) TTL：必须带过期（否则热窗口永不释放）
    ttl = int(await client.ttl(EXPECTED_KEY))
    assert 0 < ttl <= settings.CHAT_SHORT_MEMORY_TTL_SECONDS, (
        f"TTL 应在 (0, {settings.CHAT_SHORT_MEMORY_TTL_SECONDS}]，实际 {ttl}"
    )
    print(f" 6) TTL ✓ 剩余 {ttl}s（上限 {settings.CHAT_SHORT_MEMORY_TTL_SECONDS}）")

    # 7) key 形态
    assert await client.exists(EXPECTED_KEY) == 1, (
        f"key 形态应为 {EXPECTED_KEY}（): prefix:userId:sessionId:messages）"
    )
    print(f" 7) key 形态 ✓ {EXPECTED_KEY}")

    # 8) 空数组保真：返回 [] 而非 None（调用方不能把 [] 当 miss）
    await chat_short_memory_service.save(USER_ID, SESSION_ID, [])
    empty = await chat_short_memory_service.try_load(USER_ID, SESSION_ID)
    assert empty == [], (
        f"save([]) 后应返回 []，实际 {empty!r}"
    )
    print(" 8) 空数组语义 ✓ 返回 []（不是 None，勿当 miss 回填）")

    # 9) 脏数据容错：非法 JSON / 非数组 → None 且不抛
    await client.set(EXPECTED_KEY, "{不是合法 JSON", ex=60)
    assert await chat_short_memory_service.try_load(USER_ID, SESSION_ID) is None, (
        "非法 JSON 应降级为 None（WARN + 走数据库），不能抛给调用方"
    )
    await client.set(EXPECTED_KEY, json.dumps({"type": "human"}, ensure_ascii=False), ex=60)
    assert await chat_short_memory_service.try_load(USER_ID, SESSION_ID) is None, (
        "值不是数组时应返回 None（)）"
    )
    print(" 9) 脏数据容错 ✓ 非法 JSON / 非数组 → None，不抛")

    # 10) clear
    await chat_short_memory_service.clear(USER_ID, SESSION_ID)
    assert await chat_short_memory_service.try_load(USER_ID, SESSION_ID) is None, (
        "clear 后应回到未命中"
    )
    assert await client.exists(EXPECTED_KEY) == 0, "clear 后 key 应被删除"
    print(" 10) clear ✓ key 已删除")

    # 11a) db_rows_to_messages：role / 空正文过滤
    rows = [
        AiMessage(id=1, session_id=1, role="user", content=" 库里的问 "),
        AiMessage(id=2, session_id=1, role="assistant", content="库里的答"),
        AiMessage(id=3, session_id=1, role="user", content=" "),
        AiMessage(id=4, session_id=1, role="system", content="忽略我"),
    ]
    from_rows = db_rows_to_messages(rows)
    assert [type(m).__name__ for m in from_rows] == ["HumanMessage", "AIMessage"], (
        f"只取 user/assistant 且正文非空，实际 {[type(m).__name__ for m in from_rows]}"
    )
    assert from_rows[0].content == "库里的问", "正文应 trim"
    assert all(is_working_message(m) for m in from_rows), (
        "回填出的消息必须全部通过 is_working_message（两条来源同一口径）"
    )
    print(" 11) dbRowsToMessages ✓ role / 空正文过滤，正文已 trim")

    # 11b) compact_rewrite_context：只看最近 4 条 + 截断长度
    long_history = [
        HumanMessage("H" * 500),
        AIMessage("A" * 500),
        HumanMessage("早" * 10),
        AIMessage("早答" * 10),
        HumanMessage("最近问"),
        AIMessage("最近答"),
    ]
    ctx = compact_rewrite_context(long_history)
    lines = ctx.split("\n")
    assert len(lines) == 4, f"只回看最近 4 条，实际 {len(lines)} 行"
    assert lines[-2] == "用户：最近问" and lines[-1] == "助手：最近答", (
        f"格式应为 `用户：…` / `助手：…`，实际末两行 {lines[-2:]!r}"
    )
    clipped = compact_rewrite_context([HumanMessage("长" * 500), AIMessage("答" * 500)])
    user_line, assistant_line = clipped.split("\n")
    assert len(user_line) == len("用户：") + 200, "用户侧应截断到 200 字"
    assert len(assistant_line) == len("助手：") + 120, "助手侧应截断到 120 字（避免原文污染 query）"
    assert compact_rewrite_context([]) == "", "空历史返回空串"
    print(" 12) compactRewriteContext ✓ 最近 4 条 / 用户 200 字 / 助手 120 字")

    # 11c) message_text：三种 content 形态
    assert message_text(HumanMessage(" 纯文本 ")) == "纯文本"
    assert message_text(AIMessage(content=[{"type": "text", "text": "块一"}, {"type": "text", "text": "块二"}])) == "块一" + "块二"
    assert message_text(AIMessage(content=[{"type": "text", "text": None}])) == "", (
        "块的 text 为 None 时应得空串"
    )
    assert message_text(AIMessage(content="")) == ""
    print(" 13) messageText ✓ str / 块列表 / None 三种形态")

    await cleanup()
    print("\n对话短期记忆（Redis 热窗口）冒烟测试全部通过 ✅")


async def run() -> None:
    """main + cleanup 同一事件循环（redis 客户端绑定首个 loop，不能二次 run）"""
    try:
        await main()
    except Exception:
        print("冒烟测试失败，执行清理", file=sys.stderr)
        await cleanup()
        raise
    finally:
        await redis_service.close()


if __name__ == "__main__":
    asyncio.run(run())

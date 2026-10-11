"""对话长期记忆（Mem0 托管平台）冒烟测试

验证点：
1. 降级路径：未配置 MEM0_API_KEY 时 enabled=False、search 返回空的 user/session 两层、
   remember_turn / clear_session 静默返回不抛 —— 「记忆不可用不能阻断对话」
2. _pick_memories：平台响应的 5 种形态（正常 / 缺 results / results 为 None / 非 dict /
   条目非 dict 或 memory 为空）都不炸，空串被过滤（)`）
3. build_system_message：两层皆空返回 None（调用方据此跳过注入）
4. build_system_message：拼接格式（【用户长期记忆】/【当前会话记忆】+ `- ` 条目 + 免责句）
5. 分类器：**真实调用 LLM**（本项目 = deepseek-v4-flash），三条样例的 write_user /
   write_session 判定 —— 验证 CLASSIFIER_PROMPT + `function_calling` 结构化输出可用
6. 真实 Mem0 链路（**仅当配置了 MEM0_API_KEY 时**）：remember_turn 写 user 层 → 轮询
   检索命中 → 直接写一条 session 层 → clear_session → 轮询确认会话层清空**且 user 层
   未被误伤**。未配置时明确打印 SKIP —— ⚠️ **SKIP 不算通过**，别当绿灯读。
   ⚠️ 本步必须轮询：Mem0 云侧的 add / delete_all 都是异步的（add 返回 status=PENDING、
   delete_all 返回「Delete in progress」），「写完立刻读」「删完立刻断言为空」都不成立。

⚠️ 与短期记忆冒烟同一个教训：本模块所有失败路径都是「WARN + 返回空」，若不做前提断言，
「配了 key 但调用失败」会被误读成「没配 key 的正常降级」而假绿。所以第 1 步先断言
`enabled` 与 `MEM0_API_KEY` 是否配置**一致**，再分别走两条分支。
第 5 步是 3 次真实 LLM 调用（prompt 很短），会消耗少量额度。

运行：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false .venv/bin/python scripts/smoke_chat_long_memory.py

前置：LLM_API_KEY 已配置（第 5 步需要）；配了 MEM0_API_KEY 才会跑第 6 步。
不污染持久层：Mem0 侧只用临时 user / session id，结束前清理干净。
"""

import asyncio
import sys
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import settings
from app.services.chat_long_memory import (
    CLASSIFIER_PROMPT,
    _build_classifier, # noqa: PLC2701 冒烟需要直接验证分类器（不经过 client 门禁）
    _build_jev_classifier, # noqa: PLC2701
    _pick_memories, # noqa: PLC2701
    chat_long_memory_service,
)

# 临时身份（不用种子用户，避免与真实记忆撞 user_id）
USER_ID = "990000000000000911"
SESSION_ID = "990000000000000912"


async def cleanup() -> None:
    """清掉本次可能写入的记忆（幂等；失败不阻塞收尾）

    注意：service 只提供 clear_session（清 session 层）；user 层要直接经 client 删除，
    否则冒烟会在 Mem0 云上留下测试记忆。
    ⚠️ `delete_all` 的实体 ID 必须走**顶层关键字参数**：走 `filters={...}` 时它是
    DELETE + query string，dict 会被 httpx 用 `str()` 塞进 URL（单引号），服务端解析
    不出 JSON → 400「At least one filter ... is required」，也就是**一条都没删**却只看到
    一行 WARN。这条是实测踩出来的，见 services/chat_long_memory.py 模块注释。
    """
    client = chat_long_memory_service._client # noqa: SLF001
    if client is None:
        return
    try:
        await client.delete_all(user_id=USER_ID)
        print(" (cleanup: 已提交删除本次临时 user 层的全部记忆)", file=sys.stderr)
    except Exception as err: # noqa: BLE001
        print(f" (cleanup 跳过：{err})", file=sys.stderr)


async def _poll_until(label: str, call, ok, tries: int = 12, delay: float = 3.0):
    """轮询直到 `ok(结果)` 为真，返回最后一次结果（含轮询次数）

    ⚠️ 为什么必须轮询：Mem0 云侧的 add / delete_all 都是**异步**的 ——
    add 立刻返回 `{'status': 'PENDING'}`，后台才做事实抽取与向量化；delete_all 返回
    「Delete in progress. This may take some time.」。实测 add 后立即检索命中 0 条，
    轮询到第 3 次（约 6~9 秒）才出现。所以「写完立刻断言命中」「删完立刻断言为空」
    在云侧都不成立，直接断言必然假红（或反过来掩盖真实失败）。
    """
    result = None
    for attempt in range(1, tries + 1):
        result = await call()
        if ok(result):
            if attempt > 1:
                print(f" ({label}：第 {attempt} 次轮询命中，共等约 {(attempt - 1) * delay:.0f}s)")
            return result
        if attempt < tries:
            await asyncio.sleep(delay)
    return result


async def main() -> None:
    has_key = bool(settings.MEM0_API_KEY)
    print(
        f" · 配置：MEM0_API_KEY={'已配置' if has_key else '未配置'}、"
        f"MEM0_TOP_K={settings.MEM0_TOP_K}、"
        f"MEM0_HOST={settings.MEM0_HOST or '(默认 https://api.mem0.ai)'}"
    )

    # 1) 前提断言 + 降级路径
    assert chat_long_memory_service.enabled is has_key, (
        "enabled 必须与「是否配置 MEM0_API_KEY」一致，否则后面的降级断言会假绿"
    )
    if has_key:
        print(" 1) 已配置 MEM0_API_KEY → 降级断言跳过（第 6 步走真实链路）")
    else:
        hits = await chat_long_memory_service.search(USER_ID, SESSION_ID, "任意查询")
        assert hits == {"user": [], "session": []}, f"降级应返回两层空列表，实际 {hits}"
        # 不抛即通过（记忆不可用不能阻断对话）
        await chat_long_memory_service.remember_turn(USER_ID, SESSION_ID, "问", "答")
        await chat_long_memory_service.clear_session(USER_ID, SESSION_ID)
        print(" 1) 未配置 key → enabled=False、search 空、写/清静默跳过 ✓")

    # 2) _pick_memories：五种响应形态
    pick_cases: list[tuple[Any, list[str]]] = [
        ({"results": [{"memory": "记住A"}, {"memory": "记住B"}]}, ["记住A", "记住B"]),
        ({}, []),
        ({"results": None}, []),
        ("不是 dict", []),
        (
            {
                "results": [
                    {"memory": ""},
                    "字符串条目",
                    {"no_memory": 1},
                    {"memory": "唯一有效"},
                ]
            },
            ["唯一有效"],
        ),
    ]
    for raw, expected in pick_cases:
        got = _pick_memories(raw)
        assert got == expected, f"输入 {raw!r} 应得 {expected}，实际 {got}"
    print(" 2) _pick_memories ✓ 5 种响应形态都不炸，空串被过滤")

    # 3) 两层皆空 → None
    assert chat_long_memory_service.build_system_message({"user": [], "session": []}) is None, (
        "两层都空时应返回 None（调用方据此跳过注入）"
    )
    print(" 3) build_system_message 空输入 ✓ 返回 None")

    # 4) 拼接格式
    only_user = chat_long_memory_service.build_system_message(
        {"user": ["偏好简洁", "后端技术组"], "session": []}
    )
    assert only_user is not None
    user_text = only_user.content
    assert isinstance(user_text, str)
    assert "【用户长期记忆】" in user_text and "【当前会话记忆】" not in user_text, (
        "只有 user 层时不应出现会话记忆标题"
    )
    assert "- 偏好简洁" in user_text and "- 后端技术组" in user_text, "条目应以 `- ` 开头"
    assert "不要用记忆替代文档" in user_text, "必须带免责句（记忆不是制度事实来源）"

    both = chat_long_memory_service.build_system_message(
        {"user": ["偏好简洁"], "session": ["本轮在补差旅制度"]}
    )
    assert both is not None
    both_text = both.content
    assert isinstance(both_text, str)
    assert both_text.index("【用户长期记忆】") < both_text.index("【当前会话记忆】"), (
        "user 层应排在 session 层之前"
    )
    print(" 4) build_system_message ✓ 标题 / `- ` 条目 / 免责句 / user 在前")

    # 5) 分类器：真实 LLM 调用
    classifier = _build_classifier()
    assert classifier is not None, (
        "分类器应可构建（需要 OPENAI_API_KEY / LLM_API_KEY / DASHSCOPE_API_KEY 之一）"
    )
    samples: list[tuple[str, str]] = [
        ("你好", "你好，有什么可以帮你的？"),
        ("我是后端技术组的，以后回答短一点，别贴大段代码", "好的，我记下了。"),
        ("这次先把差旅制度第三节补完", "好的，我们从第三节开始。"),
    ]
    results: list[tuple[str, bool, bool, str]] = []
    for question, answer in samples:
        parsed = await classifier.ainvoke(
            [
                SystemMessage(CLASSIFIER_PROMPT),
                HumanMessage(
                    f"用户：{question}\n助手（仅供判断，不要当作用户事实）：{answer[:300]}"
                ),
            ]
        )
        results.append((question, bool(parsed.write_user), bool(parsed.write_session), parsed.reason))

    for question, w_user, w_session, reason in results:
        print(f" · 「{question}」→ user={w_user} session={w_session}｜{reason}")

    greet = results[0]
    assert not greet[1] and not greet[2], (
        f"寒暄不应写入任何层，实际 user={greet[1]} session={greet[2]}"
    )
    prefer = results[1]
    assert prefer[1], (
        f"长期偏好（「以后回答短一点」）应写 user 层，实际 user={prefer[1]}｜{prefer[3]}"
    )
    print(" 5) 分类器真实调用 ✓ 寒暄不写、长期偏好写 user 层（结构化输出可用）")

    # 5b) Jev 分类器（TypeSafe Noul×2，需 TYPESAFE_API_KEY）
    #
    # 与 5) 同一批样例、同一套期望：Jev 若与 LLM 判定不一致，说明阈值/中文精度有问题。
    # NoulAnswer 无 confidence，只有一个 P(是)，阈值见 MEMORY_JEV_*_THRESHOLD。
    jev = _build_jev_classifier()
    if jev is None:
        print(
            " 5b) Jev 分类器 ⋯ SKIP（未配置 TYPESAFE_API_KEY 或未装 typesafe_sdk）"
            "⚠️ SKIP 不算通过"
        )
    else:
        jev_results: list[tuple[str, bool, bool, str]] = []
        for question, answer in samples:
            parsed = await jev.classify(question, answer)
            jev_results.append(
                (question, bool(parsed.write_user), bool(parsed.write_session), parsed.reason)
            )
        for question, w_user, w_session, reason in jev_results:
            print(f" · 「{question}」→ user={w_user} session={w_session}｜{reason}")

        # 与 LLM 分类器逐条比对（判定不一致只告警不失败：两者都是增强项，
        # 但真实流量下持续偏离说明阈值需要重校准——用 MEMORY_CLASSIFIER=shadow 观测）
        for (q, l_user, l_session, _), (_, j_user, j_session, _) in zip(results, jev_results):
            if (l_user, l_session) != (j_user, j_session):
                print(
                    f" ⚠️ 判定不一致「{q}」：LLM=({l_user},{l_session}) Jev=({j_user},{j_session})"
                )

        j_greet = jev_results[0]
        assert not j_greet[1] and not j_greet[2], (
            f"寒暄不应写入任何层，实际 user={j_greet[1]} session={j_greet[2]}｜{j_greet[3]}"
        )
        j_prefer = jev_results[1]
        assert j_prefer[1], (
            f"长期偏好应写 user 层，实际 user={j_prefer[1]}｜{j_prefer[3]}"
        )
        j_task = jev_results[2]
        assert j_task[2], (
            f"本轮任务应写 session 层，实际 session={j_task[2]}｜{j_task[3]}"
        )
        print(" 5b) Jev 分类器真实调用 ✓ 寒暄不写 / 偏好写 user / 本轮任务写 session")

    # 6) 真实 Mem0 链路（需 MEM0_API_KEY）
    if not has_key:
        print(
            " 6) 真实 Mem0 链路 ⋯ SKIP（未配置 MEM0_API_KEY）"
            "⚠️ SKIP 不算通过，配置 key 后必须重跑"
        )
    else:
        client = chat_long_memory_service._client # noqa: SLF001
        assert client is not None
        await cleanup() # 先清干净，保证「search 落空」这一步可信
        await asyncio.sleep(5) # 删除同样是异步的，等它落地再断言「清理后为空」
        hits_before = await chat_long_memory_service.search(USER_ID, SESSION_ID, "偏好")
        if hits_before != {"user": [], "session": []}:
            # 检索失败也会返回空，所以这里只能作参考，不作断言
            print(f" ⚠️ 清理后仍能搜到记忆（前次删除可能未落地）：{hits_before}")

        # 6a) 真实写入路径：分类器 → add（user 层）→ 轮询等异步抽取完成
        await chat_long_memory_service.remember_turn(
            USER_ID, SESSION_ID, "我是后端技术组的，回答短一点", "好的。"
        )
        after = await _poll_until(
            "user 层检索到记忆",
            lambda: chat_long_memory_service.search(
                USER_ID, SESSION_ID, "我是什么组的？回答有什么偏好？"
            ),
            lambda hits: bool(hits["user"]),
        )
        assert after and after["user"], (
            "写入后 user 层应检索到记忆（已轮询约 36s）。若仍为空，检查："
            "① 分类器是否判 write_user；② add 是否 400（实体 ID 必须走顶层 user_id，"
            "不能包 filters）；③ 云侧抽取是否异常缓慢"
        )

        # 6b) 直接写一条 session 层记忆（不经分类器），让「清会话层」这条断言有实际约束力：
        # 否则若分类器把本轮判成 user 层，session 层本来就是空的，
        # clear_session 后断言"为空"会**假绿**（什么都没删也会过）
        await client.add(
            [{"role": "user", "content": f"这次要看的文档编号是 42（会话 {SESSION_ID}）"}],
            user_id=USER_ID,
            run_id=SESSION_ID,
        )
        with_session = await _poll_until(
            "session 层检索到记忆",
            lambda: chat_long_memory_service.search(USER_ID, SESSION_ID, "这次要看的文档编号"),
            lambda hits: bool(hits["session"]),
        )
        assert with_session and with_session["session"], (
            "session 层应能检索到刚写入的记忆（验证 add 的 run_id 维度可用）"
        )

        # 6c) clear_session 只清会话层，**不能动 user 层**（用户偏好跨会话保留）
        await chat_long_memory_service.clear_session(USER_ID, SESSION_ID)
        cleared = await _poll_until(
            "会话层清空",
            lambda: chat_long_memory_service.search(USER_ID, SESSION_ID, "这次要看的文档编号"),
            lambda hits: not hits["session"],
        )
        assert cleared is not None and not cleared["session"], (
            f"clear_session 后会话层应为空（已轮询约 36s），实际 {cleared and cleared['session']}"
        )
        # ⚠️ 判「user 层未受影响」必须用**与 user 层记忆语义相关**的 query：search 是
        # 语义检索 + top_k，拿会话层的 query 去查 user 层本来就会落空，那样断言是假红
        survived = await chat_long_memory_service.search(
            USER_ID, SESSION_ID, "我是什么组的？回答有什么偏好？"
        )
        assert survived["user"], (
            "clear_session 不应影响 user 层——用户偏好必须跨会话保留，"
            f"实际 user 层 {survived['user']}（若确实被删，说明 delete_all 的 run_id "
            "过滤没有隔离，需改用 memory_id 逐条删除）"
        )
        print(
            f" 6) 真实 Mem0 链路 ✓ user 层 {len(after['user'])} 条；"
            "会话层写入→清除已闭环；清会话层未误伤 user 层"
        )

    await cleanup()
    print("\n对话长期记忆（Mem0）冒烟测试通过 ✅" + ("（第 6 步为 SKIP，未覆盖真实链路）" if not has_key else ""))


async def run() -> None:
    try:
        await main()
    except Exception:
        print("冒烟测试失败，执行清理", file=sys.stderr)
        await cleanup()
        raise


if __name__ == "__main__":
    asyncio.run(run())

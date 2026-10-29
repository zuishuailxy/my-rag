"""并发压测：N 路并发对话，测**基础设施层**的行为与瓶颈

**为什么 mock 掉 LLM / Embedding**
    压测要测的是「我的系统在并发下的行为」，不是第三方 API 的抖动。
    把 LLM、Embedding 换成本地 mock 后：外部变量归零、不花钱、可复现、能进 CI。
    真实 API 的延迟方差反而会掩盖连接池这类问题。

**测什么**（都是「面试官会问、但项目原本答不上」的东西）
    - 端到端延迟 p50 / p95 / max、流式首字节 TTFT
    - 错误率
    - **PG 连接池压力**：运行期间采样 `pg_stat_activity` 的峰值连接数
      （应用侧池上限 = pool_size 20 + max_overflow 10 = 30，PG 服务端 100 —— 瓶颈在应用侧）
      采样按 `application_name = loadtest-app` **只统计被测实例**：同库的开发实例 /
      psql / GUI 一律排除，否则峰值连接数不可复算（旧版就栽在这：三个文档三个数）

**用法**
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false \\
        .venv/bin/python scripts/load_test.py --levels 5,10,20,40

脚本会自己：起 mock 服务 → 起一个用 mock 的 uvicorn（独立端口）→ 压测 → 采样 → 清理 → 关停。
不会碰你正在跑的 8000 端口那个实例。
"""
import argparse
import asyncio
import json
import logging
import os
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

sys.path.insert(0, ".")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db.session import async_session_factory, engine  # noqa: E402
from smoke_auth import make_access_token  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

QUESTION_MARKER = "[loadtest] 差旅住宿标准是多少"
# 每轮压测用独立 sessionId，避开「每会话每日轮次」限制；每用户限制在开跑前清掉
QUOTA_KEY_PREFIX = "kh:llm:turns"
# 被测实例的连接标识（与 _start_app 注入的 DB_APPLICATION_NAME 必须一致）。
# 采样器**只统计这个标识的连接**，所以同库的其他客户端（开发实例 / psql / GUI）
# 不会被算进峰值连接数；本进程自己的连接也因标识不同而自动排除。
LOADTEST_APP_NAME = "loadtest-app"


# --------------------------------------------------------------------- mock LLM


async def _read_one_embedding() -> list[float]:
    """从 PG 取一条真实 chunk 向量返回给 mock

    这样 mock 出来的查询向量与库里向量**高度相似** → 能穿过检索阈值 → 走到生成阶段，
    压测才覆盖完整链路（否则会一直走「没有相关内容」的短路）。
    """
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text("SELECT embedding FROM doc_chunk WHERE embedding IS NOT NULL LIMIT 1")
            )
        ).first()
        if not row:
            return [0.0] * 1024
        raw = row[0]
        # ⚠️ pgvector 经 asyncpg 读回来是**字符串** "[-0.008,...]"（形如 JSON）。
        # 直接 list(raw) 会把字符串拆成单个字符 → 拼出的向量参数是垃圾 →
        # PG 报 InvalidTextRepresentationError。必须显式解析。
        if isinstance(raw, str):
            return list(json.loads(raw))
        return list(raw)


class _MockLLMHandler(BaseHTTPRequestHandler):
    """OpenAI 兼容 mock：/v1/embeddings + /v1/chat/completions（含 SSE 流式）"""

    embedding: list[float] = []
    # 人为延迟：真实 LLM 有延迟，纯 0 延迟会让「并发」退化成瞬时完成，测不出排队
    llm_delay_s: float = 0.0

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/embeddings"):
            self._embeddings(body)
        elif self.path.endswith("/chat/completions"):
            if body.get("stream"):
                self._chat_stream(body)
            else:
                self._chat(body)
        else:
            self.send_error(404)

    # -- embeddings --

    def _embeddings(self, body: dict) -> None:
        items = body.get("input") or [""]
        if isinstance(items, str):
            items = [items]
        self._json(
            {
                "object": "list",
                "data": [
                    {"object": "embedding", "index": i, "embedding": self.embedding}
                    for i in range(len(items))
                ],
                "model": body.get("model", "mock-embedding"),
                "usage": {"prompt_tokens": 8, "total_tokens": 8},
            }
        )

    # -- chat（非流式）--

    def _chat(self, body: dict) -> None:
        if self.llm_delay_s:
            time.sleep(self.llm_delay_s)
        self._json(
            {
                "id": "mock",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model", "mock"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "住宿费上限 600 元/晚 [1]。"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 900, "completion_tokens": 40, "total_tokens": 940},
            }
        )

    # -- chat（SSE 流式）--

    def _chat_stream(self, body: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def chunk(delta: dict, finish: str | None = None) -> None:
            payload = {
                "id": "mock",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body.get("model", "mock"),
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()

        chunk({"role": "assistant", "content": ""})
        for piece in ("住宿", "费上限", " 600 元/晚", " [1]。"):
            if self.llm_delay_s:
                time.sleep(self.llm_delay_s / 5)
            chunk({"content": piece})
        chunk({}, "stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _json(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:  # noqa: ARG002
        """压测中屏蔽访问日志（否则日志本身就是瓶颈）"""


# ----------------------------------------------------------------- 测试用服务


def _start_mock(port: int, llm_delay_s: float, embedding: list[float]) -> ThreadingHTTPServer:
    _MockLLMHandler.embedding = embedding
    _MockLLMHandler.llm_delay_s = llm_delay_s
    # ThreadingHTTPServer：mock 必须能并发，否则它自己成了瓶颈
    server = ThreadingHTTPServer(("127.0.0.1", port), _MockLLMHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _start_app(port: int, mock_port: int, log_path: str) -> subprocess.Popen:
    """起一个用 mock 的 uvicorn（不动你 8000 上那个实例）

    服务端 stderr 存文件而不是丢弃：**失败时必须能看到真实异常**，
    否则只能从 HTTP 500 反推，那不叫定位、叫猜。
    """
    env = {
        **os.environ,
        "OPENAI_BASE_URL": f"http://127.0.0.1:{mock_port}/v1",
        "EMBEDDING_BASE_URL": f"http://127.0.0.1:{mock_port}/v1",
        "OPENAI_API_KEY": "mock",
        "EMBEDDING_API_KEY": "mock",
        # 压测不需要第三方 rerank（会 403/超时，给结果引入方差）；关掉后走 RRF + 向量兜底
        "RAG_RERANK_ENABLED": "false",
        # 压测流量不该污染 Langfuse 看板
        "LANGFUSE_TRACING": "false",
        "RABBITMQ_ENABLED": "false",
        # 给被测实例的连接打标，采样器据此只统计它自己（见 LOADTEST_APP_NAME）
        "DB_APPLICATION_NAME": LOADTEST_APP_NAME,
    }
    log = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — 子进程持有，退出时关闭
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )


async def _wait_ready(url: str, timeout_s: float = 90) -> bool:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{url}/docs", timeout=3)
                if resp.status_code < 500:
                    return True
            except Exception:  # noqa: BLE001 — 启动中，继续等
                pass
            await asyncio.sleep(1)
    return False


# ------------------------------------------------------------------- 采集与压测


class PgConnectionSampler:
    """后台采样 PG 连接：峰值数 + 峰值时刻的**状态分布**

    只看数量不足以定位：若大量连接处于 `idle in transaction`，说明它们正被
    「开着事务等 LLM」空占 —— 那才是吞吐上限的真因，而不仅是池容量不够。
    """

    def __init__(self) -> None:
        self.samples: list[int] = []
        self.states_at_peak: dict[str, int] = {}
        self.holders_at_peak: list[tuple[str, int]] = []
        self._stop = False

    async def run(self) -> None:
        # ⚠️ **每次采样都新签出一条连接，且用 AUTOCOMMIT** —— 绝不能复用一个长事务会话。
        # 这里踩过一个把整条结论作废的坑：复用单一会话时，`pg_stat_activity` 的可见行集
        # **冻结在事务开始时的快照**上。实测（A/B）：同一时刻新连接看到 15 条 app 连接，
        # 长事务会话里始终只看到 1 条 —— 于是峰值连接数恒为 1（或任意首帧值），指标失效。
        # 文档里 16 / 31 / 32 / 40 四个数对不上的根因就在这里。
        # 旧版复用一个会话，是想避免「采样器自己制造 idle in transaction 把指标算高」；
        # 现在采样器的连接因 `application_name` 不同已被直接排除，那个顾虑不存在了，
        # 所以「一条连接一次采样」既准确又不污染。
        while not self._stop:
            try:
                async with engine.connect() as conn:
                    conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
                    rows = (
                        await conn.execute(
                            text(
                                "SELECT coalesce(state, 'null') AS st, count(*) AS n "
                                "FROM pg_stat_activity "
                                "WHERE application_name = :app_name "
                                "GROUP BY 1"
                            ),
                            {"app_name": LOADTEST_APP_NAME},
                        )
                    ).all()
                    total = sum(int(n) for _, n in rows)
                    self.samples.append(total)
                    # 保留峰值时刻的快照，而不是最后一次的快照
                    if total >= (max(self.samples) if self.samples else 0):
                        self.states_at_peak = {str(st): int(n) for st, n in rows}
                        # 关键：空占的连接「上一条 SQL 是什么」= 谁开的会话
                        holders = (
                            await conn.execute(
                                text(
                                    "SELECT left(regexp_replace(coalesce(query, ''), '\\s+', ' ', 'g'), 90) AS q, "
                                    "count(*) AS n FROM pg_stat_activity "
                                    "WHERE application_name = :app_name "
                                    "AND state = 'idle in transaction' GROUP BY 1 ORDER BY 2 DESC LIMIT 4"
                                ),
                                {"app_name": LOADTEST_APP_NAME},
                            )
                        ).all()
                        self.holders_at_peak = [(str(q), int(n)) for q, n in holders]
            except Exception:  # noqa: BLE001 — 采样失败不影响压测
                pass
            await asyncio.sleep(0.2)

    def stop(self) -> None:
        self._stop = True


async def _one_stream(client: httpx.AsyncClient, url: str, headers: dict, payload: dict) -> dict:
    """发一路流式请求，记首帧 / 首个内容 token / 总时长

    ⚠️ **首帧不是 TTFT**：SSE 的 `start` 帧在业务逻辑开始之前就发出去了，
    测它只会得到「连接建好要多久」，对优化毫无信息量（旧版就把它叫 TTFT）。
    真正的首 token 取第一个带 delta 的 `text-delta` / `reasoning-delta` 帧。
    """
    start = time.perf_counter()
    first_frame: float | None = None
    ttft: float | None = None
    status = 0
    try:
        async with client.stream("POST", url, headers=headers, json=payload, timeout=180) as resp:
            status = resp.status_code
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                now = time.perf_counter() - start
                if first_frame is None:
                    first_frame = now
                if ttft is not None:
                    continue
                body = line[5:].strip()
                if not body or body == "[DONE]":
                    continue
                try:
                    event = json.loads(body)
                except json.JSONDecodeError:
                    continue
                if event.get("type") in ("text-delta", "reasoning-delta"):
                    ttft = now
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "status": status, "error": str(err)[:120], "total": time.perf_counter() - start}
    return {
        "ok": status == 200,
        "status": status,
        "first_frame": first_frame,
        "ttft": ttft,
        "total": time.perf_counter() - start,
    }


async def _one_chat(client: httpx.AsyncClient, url: str, headers: dict, payload: dict) -> dict:
    """发一路非流式请求（必然走完整检索 + 生成链路）"""
    start = time.perf_counter()
    try:
        resp = await client.post(url, headers=headers, json=payload, timeout=180)
    except Exception as err:  # noqa: BLE001
        return {"ok": False, "status": 0, "error": str(err)[:120], "total": time.perf_counter() - start}
    return {
        "ok": resp.status_code == 200,
        "status": resp.status_code,
        "error": "" if resp.status_code == 200 else resp.text[:120],
        "total": time.perf_counter() - start,
    }


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(p / 100 * len(ordered) + 0.5)) - 1)
    return ordered[max(0, idx)]


def _payload(endpoint: str) -> dict:
    """两种端点的请求体形状不同：`/chat` 收 content，`/chat/stream` 收 useChat 的 messages

    （旧版无论哪个端点都发 content，stream 分支一直是 422 —— 也就是说
    历史上的「TTFT」从来没真正测到过，看到的只是非流式端点的总时长。）
    """
    if endpoint == "stream":
        return {
            "messages": [
                {
                    "role": "user",
                    "parts": [{"type": "text", "text": QUESTION_MARKER}],
                }
            ],
            "topK": 5,
            "sessionId": None,
        }
    return {"content": QUESTION_MARKER, "topK": 5, "sessionId": None}


async def _run_level(
    level: int,
    base_url: str,
    endpoint: str,
    token: str,
    sampler: PgConnectionSampler,
    rounds: int = 1,
) -> dict:
    """跑一档并发。`rounds` 轮样本合并统计 —— 样本太少时 p95 会退化成 max
    （n=20 时 p95 取到排序后第 20 个，就是最大值），多轮才能让高分位数有意义。"""
    url = f"{base_url}/api/v1/ai/chat" + ("/stream" if endpoint == "stream" else "")
    headers = {"Authorization": f"Bearer {token}"}
    runner = _one_stream if endpoint == "stream" else _one_chat
    payload = _payload(endpoint)

    results: list[dict] = []
    wall_total = 0.0
    for _ in range(max(1, rounds)):
        # 每轮清配额，保证各轮在同一起跑线（可重复跑）
        await _reset_quota()
        async with httpx.AsyncClient() as client:
            started = time.perf_counter()
            tasks = [
                runner(client, url, headers, payload)
                for _ in range(level)
            ]
            results.extend(await asyncio.gather(*tasks))
            wall_total += time.perf_counter() - started

    totals = [r["total"] for r in results]
    ttfts = [r["ttft"] for r in results if r.get("ttft")]
    frames = [r["first_frame"] for r in results if r.get("first_frame")]
    errors = [r for r in results if not r["ok"]]
    return {
        "level": level,
        "rounds": max(1, rounds),
        "samples": len(results),
        "wall": wall_total,
        "ok": len(results) - len(errors),
        "errors": len(errors),
        "error_sample": (
            (errors[0].get("error") or f"HTTP {errors[0].get('status')}") if errors else ""
        ),
        "p50": statistics.median(totals) if totals else 0.0,
        "p95": _pct(totals, 95),
        "max": max(totals) if totals else 0.0,
        "ttft_p50": statistics.median(ttfts) if ttfts else None,
        "ttft_p95": _pct(ttfts, 95) if ttfts else None,
        "first_frame_p50": statistics.median(frames) if frames else None,
        "rps": len(results) / wall_total if wall_total else 0.0,
        "peak_conn": max(sampler.samples) if sampler.samples else 0,
        # 原始样本：落盘后 p99 / 任意分位都能重算，不必重跑
        "totals": totals,
        "ttfts": ttfts,
    }


def _print_server_errors(log_path: str, limit: int = 6) -> None:
    """从服务端日志里滤出真正有用的行（异常类型 / 连接池告警）"""
    keys = ("Error", "error", "Timeout", "pool", "Exception", "pool_size")
    try:
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            lines = [ln.strip() for ln in handle if any(k in ln for k in keys)]
    except OSError:
        print("  （读不到服务端日志）")
        return
    for line in lines[-limit:]:
        print(f"  {line[:200]}")


async def _reset_quota() -> None:
    """清掉压测用户的配额计数，保证每一档都在同一起跑线（可重复跑）"""
    from datetime import datetime, timezone

    from app.db.redis import redis_service

    if redis_service.client is None:
        return
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    keys = [k async for k in redis_service.client.scan_iter(match=f"{QUOTA_KEY_PREFIX}:*:{day}")]
    if keys:
        await redis_service.client.delete(*keys)


async def _cleanup_sessions() -> int:
    """删掉压测产生的会话与消息（标题带标记）"""
    async with async_session_factory() as session:
        ids = [
            row[0]
            for row in (
                await session.execute(
                    text("SELECT id FROM kh_ai_session WHERE title LIKE :m"),
                    {"m": f"%{QUESTION_MARKER}%"},
                )
            ).all()
        ]
        if not ids:
            return 0
        await session.execute(
            text("DELETE FROM kh_ai_message WHERE session_id = ANY(:ids)"), {"ids": ids}
        )
        await session.execute(
            text("DELETE FROM kh_ai_session WHERE id = ANY(:ids)"), {"ids": ids}
        )
        await session.commit()
    return len(ids)


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _write_results(
    path: str,
    args: argparse.Namespace,
    rows: list[dict],
    pool_capacity: int,
    worst_peak: int,
    started_at: str,
) -> None:
    """落盘：**per-request 原始耗时数组** + 环境元信息

    只存聚合值（p50/p95/max）等于把分位数写死：事后想补 p99、想换算法、
    想和另一次结果合并统计，都得整轮重跑。存原始数组则随便重算。
    """
    payload = {
        "meta": {
            "started_at": started_at,
            "ended_at": _now_iso(),
            "commit": _git_commit(),
            "endpoint": args.endpoint,
            "levels": args.levels,
            "rounds": args.rounds,
            "llm_delay_s": args.llm_delay,
            "pool_capacity": pool_capacity,
            "peak_conn_observed": worst_peak,
            "definitions": {
                "ttft": "请求发出 → 首个 text-delta / reasoning-delta 帧（不是 SSE 首帧）",
                "total": "请求发出 → 响应流结束",
                "p95": "最近秩法；样本数 n 时取 round(0.95*n+0.5) 位，n≤20 会退化到 max",
            },
        },
        "rows": rows,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------------------ main


async def main(args: argparse.Namespace) -> int:
    from app.db.redis import redis_service

    started_at = _now_iso()
    await redis_service.start()
    mock = _start_mock(args.mock_port, args.llm_delay, await _read_one_embedding())
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".load_test_server.log")
    app_proc = _start_app(args.port, args.mock_port, log_path)
    base_url = f"http://127.0.0.1:{args.port}"

    print(f"mock LLM : http://127.0.0.1:{args.mock_port}/v1（人为延迟 {args.llm_delay}s）")
    print(f"测试实例 : {base_url}（独立于你 8000 上那个）")
    if not await _wait_ready(base_url):
        app_proc.terminate()
        mock.shutdown()
        raise SystemExit("❌ 测试实例启动超时")

    token = make_access_token()
    sampler = PgConnectionSampler()
    sampler_task = asyncio.create_task(sampler.run())

    print(f"\n{'并发':>4} {'样本':>4} {'成功':>4} {'失败':>4} {'p50(s)':>8} {'p95(s)':>8} {'max(s)':>8} "
          f"{'RPS':>6} {'峰值连接':>8}")
    rows: list[dict] = []
    try:
        for level in args.levels:
            row = await _run_level(
                level, base_url, args.endpoint, token, sampler, args.rounds
            )
            rows.append(row)
            ttft = (
                f"  首token p50={row['ttft_p50']:.2f}s p95={row['ttft_p95']:.2f}s"
                if row["ttft_p50"]
                else ""
            )
            print(
                f"{row['level']:>4} {row['samples']:>4} {row['ok']:>4} {row['errors']:>4} "
                f"{row['p50']:>8.2f} {row['p95']:>8.2f} {row['max']:>8.2f} "
                f"{row['rps']:>6.2f} {row['peak_conn']:>8}{ttft}"
            )
            if row["error_sample"]:
                print(f"      ⚠️ 错误样例：{row['error_sample']}")
    finally:
        sampler.stop()
        await sampler_task

    if any(row["errors"] for row in rows):
        print("\n=== 服务端异常（关键字过滤）===")
        _print_server_errors(log_path)

    print("\n=== 结论 ===")
    worst = max(row["peak_conn"] for row in rows) if rows else 0
    # 读**实际**生效的池配置（不要写死数字：改了配置却忘了改提示，结论就会骗人）
    pool = engine.pool
    capacity = int(pool.size()) + int(getattr(pool, "_max_overflow", 0))
    print(
        f"  应用侧连接池：pool_size={pool.size()} + max_overflow="
        f"{getattr(pool, '_max_overflow', 0)} = {capacity}（实测含采样连接位）"
    )
    print(f"  实测峰值活跃连接 = {worst}")

    if sampler.states_at_peak:
        breakdown = ", ".join(
            f"{k}={v}"
            for k, v in sorted(sampler.states_at_peak.items(), key=lambda kv: -kv[1])
        )
        print(f"  峰值时刻连接状态：{breakdown}")
        idle_tx = sampler.states_at_peak.get("idle in transaction", 0)
        if idle_tx:
            print(
                f"  ⚠️ 其中 {idle_tx} 条为 `idle in transaction` —— 连接被「开着事务等外部调用」空占，"
                "这是吞吐上限的关键项，单纯加池只能缓解"
            )
            if sampler.holders_at_peak:
                print("  空占连接的最后一条 SQL（= 谁开的会话）：")
                for query_text, count in sampler.holders_at_peak:
                    print(f"    ×{count:>3}  {query_text}")

    if any(row["errors"] for row in rows):
        print("  ⚠️ 出现失败请求 —— 说明并发已超出当前配置的处理能力（详见上方错误样例）")
    else:
        print("  ✅ 未出现失败请求")

    if args.out:
        _write_results(args.out, args, rows, capacity, worst, started_at)
        print(f"  原始样本已落盘：{args.out}")

    removed = await _cleanup_sessions()
    print(f"  清理压测会话：{removed} 条")

    app_proc.terminate()
    try:
        app_proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        app_proc.kill()
    mock.shutdown()
    await redis_service.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="并发压测（mock 掉 LLM/Embedding）")
    parser.add_argument("--levels", default="5,10,20,40", help="并发档位，逗号分隔")
    parser.add_argument("--endpoint", choices=["chat", "stream"], default="chat")
    parser.add_argument("--port", type=int, default=8099, help="测试实例端口")
    parser.add_argument("--mock-port", type=int, default=18095)
    parser.add_argument(
        "--llm-delay",
        type=float,
        default=0.05,
        help="mock LLM 每段延迟（秒）；纯 0 延迟测不出排队",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="每档重复轮数；样本数 = 并发 × 轮数，p95 在样本 <40 时没有意义",
    )
    parser.add_argument(
        "--out",
        default="",
        help="结果落盘路径（JSON，含 per-request 原始耗时数组）；留空只打印",
    )
    parsed = parser.parse_args()
    parsed.levels = [int(x) for x in parsed.levels.split(",") if x.strip()]
    raise SystemExit(asyncio.run(main(parsed)))
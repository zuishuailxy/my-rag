"""校验文档里写的数字与源码 / 评测集算出来的一致 —— 防「文档过期」

**为什么需要它**
    项目里栽过两次，都是「人工核对不可靠」导致的：
      · 同一组连接数在三个文档里是 16 / 31 / 32 / 40（根因是压测采样器口径，
        见 `scripts/load_test.py` 的 `PgConnectionSampler.run`）
      · README 写「难题 = 21 条」，而评测集里实际是 **51** 条
    这类错误的共同点是**没人会去核对**：文档写错不影响任何测试，
    直到面试 / 评审被追问才暴露。所以把「文档声称的数字」与
    「从源码 / 评测集算出来的数字」绑进 CI。

**设计**
    `FACTS`  —— 每个数字**只有一个**权威来源（评测集 / 源码 / 配置）
    `CLAIMS` —— 文档里哪一处声称了哪个数字
    数字不一致 → 失败；**模式未命中 → 同样失败**（文档被改写后必须同步本脚本，
    否则校验会静默失效，那比没有校验更糟）。

**用法**
    cd backend && PYTHONPATH=. .venv/bin/python scripts/check_doc_numbers.py
    # 额外校验需要连库的规模数字（默认跳过，CI 里不连库）
    cd backend && PYTHONPATH=. .venv/bin/python scripts/check_doc_numbers.py --with-db
    # 校验「反向断言」模式本身（改动 FORBIDDEN 后必须跑）
    cd backend && PYTHONPATH=. .venv/bin/python scripts/check_doc_numbers.py --self-test
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent

# --------------------------------------------------------------------- 权威数字


def _eval_facts() -> dict[str, int]:
    """评测集：一切指标的分母都来自这三份 jsonl，不来自文档"""

    def load(name: str) -> list[dict]:
        path = BACKEND / "eval" / name
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    rag = load("rag_eval.jsonl")
    gen = load("gen_eval.jsonl")
    graph = load("graph_eval.jsonl")

    single = [c for c in rag if c.get("expect_documents") and not c.get("multi_hop")]
    multi = [c for c in rag if c.get("multi_hop")]
    negative = [c for c in rag if not c.get("expect_documents")]
    # 「难题」= 近义改写难例：带 difficulty=hard 且不属于多跳。
    # ⚠️ 这是全项目唯一口径，README / 简历 / 面试稿都必须用它。
    hard = [c for c in rag if c.get("difficulty") == "hard" and not c.get("multi_hop")]

    unanswerable = [c for c in gen if not c.get("expect_documents")]
    return {
        "rag_total": len(rag),
        "rag_single": len(single),
        "rag_multi": len(multi),
        "rag_negative": len(negative),
        "rag_hard": len(hard),
        "rag_single_plus_negative": len(single) + len(negative),
        "gen_total": len(gen),
        "gen_unanswerable": len(unanswerable),
        "graph_total": len(graph),
        "eval_total": len(rag) + len(gen) + len(graph),
        # README 首页的对外口径：检索 + 生成 = 206（图谱 19 条另计）
        "rag_plus_gen": len(rag) + len(gen),
    }


def _code_facts() -> dict[str, int]:
    """源码规模：以 disk 上的 app/ 目录为准（不含 tests / scripts / .venv）"""
    files = [
        p
        for p in (BACKEND / "app").rglob("*.py")
        if "__pycache__" not in p.parts and p.is_file()
    ]
    lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in files)

    frontend_files = [
        p for p in (REPO / "frontend" / "src").rglob("*") if p.suffix in {".ts", ".tsx"} and p.is_file()
    ]
    frontend_lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in frontend_files)
    pages = [p for p in frontend_files if "pages" in p.parts]

    return {
        "backend_files": len(files),
        "backend_lines": lines,
        "frontend_lines": frontend_lines,
        "frontend_pages": len(pages),
        "smoke_scripts": len(list((BACKEND / "scripts").glob("smoke_*.py"))),
        # 排除本脚本自身：否则它一进仓库就会把文档里的脚本数顶掉一个，
        # 「加校验」反而变成「改坏文档」。
        "scripts_total": len(
            [p for p in (BACKEND / "scripts").glob("*.py") if p.name != Path(__file__).name]
        ),
    }


# 实测值：无法从源码推导，只能人工维护（复跑命令见 README 的并发章节）。
# 本脚本在这里的作用是**保证它在所有文档里一致** —— 历史上「峰值连接」
# 曾在 16 / 31 / 32 / 40 四处分叉，就是因为没有任何机制强制它们相等。
MEASURED_FACTS: dict[str, int] = {
    # 修复前（池 5+10=15）：并发 20 时 5/20 失败，p50 从 2.5s 跳到 31s（≈ pool_timeout）
    "pre_fix_fail_at_20": 5,
    "pre_fix_peak_conn": 15,
    "pre_fix_p50_s": 31,
    # 修复后（池 20+10=30）：10/20/40/60/100 全部零失败，峰值连接跑满整池 30
    "post_fix_peak_conn": 30,
}

# 已被证伪、出现即失败的说法。它们曾经在文档里以**不同措辞**多次回流
# （「31 → 2」「从 31 降到 2」「峰值连接只要 2 条」），靠人工 grep 一定会漏，
# 所以把「反向断言」也做进校验：光检查数字写对了不够，还要防止错的说法回来。
#
# ⚠️ 这几条模式本身也被验证过：拿 git 历史里**真实出现过的**句子跑一遍，
#    漏掉 6 处（详见 `_BAD_SAMPLES` 与 `--self-test`）。所以模式要写成
#    「贴住 峰值 / 连接 / 条 这些语境词」，而不是只匹配数字对 ——
#    README 里「解释这个错误」的那句本身就含 `31 → 2`，裸匹配数字对会误伤它。
FORBIDDEN: tuple[tuple[str, str], ...] = (
    (r"峰值 ?(?:DB |数据库)?连接[^。\n]{0,10}从 ?31", "「峰值连接从 31 …」是采样器长事务快照冻结的产物，不是测量值"),
    (r"峰值 ?(?:DB |数据库)?连接[^。\n]{0,10}31\s*(?:→|->|➔)\s*\*{0,2}2(?!\d)", "「峰值连接 31 → 2」同上"),
    (r"峰值 ?(?:DB |数据库)?连接[^。\n]{0,8}2(?!\d)", "「峰值连接 2」「峰值连接只要 2 条」同上"),
    (r"峰值 ?DB 连接 \*\*31", "「峰值 DB 连接 **31」同上"),
    (r"峰值从 ?\*{0,2}2(?!\d)", "「峰值从 2 变成 31」是旧读数（加池只是把池买大，不是修好）"),
    (r"峰值[^。\n]{0,10}2 条", "「…峰值也只要 2 条」同上"),
    (r"3[12] 条连接", "「31/32 条连接」是长事务快照冻结前的旧读数；真实值是整池（15 或 30）"),
    (r"\*\*峰值连接\*\* ?\| ?16", "旧表格「峰值连接 16/31/2」：16 是采样器首帧值，不是测量值"),
)

# **自纠语境豁免**：文档里主动承认「这个数字是假的、我推翻过」是加分项，
# 恰恰不该被拦。误伤过一次 —— 面试问答集里「我推翻过一次自己的数字：旧文档
# 「峰值连接 31→2」是采样器…导致的假值」被 3 条模式同时命中，而这句话恰恰
# 是整份材料里最可信的一段。所以先看整行有没有自纠词，有则整行豁免。
_SELF_CORRECTION = re.compile(
    r"推翻|作废|假值|不是测量值|不是真的|已废弃|旧文档|曾经|写错|更正|订正|误判|旧读数|已删除"
)


def _is_self_correction(line: str) -> bool:
    """该行是否在「主动承认并纠正某个旧说法」"""
    return bool(_SELF_CORRECTION.search(line))

# 反向断言的自我校验样本：**全部逐字取自 git 历史**（前 8 条）与当前正确文档
# （后 6 条，其中第 1 条专门用来卡「误伤」——它含 `31 → 2` 却是在**解释**这个错误）。
# 只跑 `--self-test`，不进 CI 主流程。
_BAD_SAMPLES: tuple[str, ...] = (
    "修法：权限守卫改短会话 + 后台摘要把 LLM 移出会话块 → **60/100 并发零失败，峰值连接 31→2**。",
    "| 6 | 并发压测 | 峰值连接 31→2；根因是 **FastAPI 生成器依赖把连接占着等 LLM**，不是池太小 |",
    "④ **单进程 + 连接池**——我压测过 100 并发（峰值连接只要 2 条），但那是单 worker",
    "**31 条连接里 20 条在「开着事务空等」，真正跑查询的只有 1 条。**",
    "| **峰值连接** | 16 | 31 | **2** |",
    "> 峰值从 2 变成 31 却仍在 40 并发崩；修掉持有问题后，100 并发峰值也只要 **2 条**。",
    "| **并发能力** | **100 并发零失败**；峰值 DB 连接 **31 → 2**（根因是 FastAPI 生成器依赖把连接占着等 LLM，不是池太小） |",
    "结果：**并发 20→40→60→100 零失败，峰值 DB 连接从 31 降到 2**。",
)
_GOOD_SAMPLES: tuple[str, ...] = (
    "> 于是「峰值连接」恒为 1（或任意首帧值），**旧文档里的 `31 → 2` 就是这么来的、不是测量值**。",
    "峰值时刻：15 条连接（= 整池）全部被占用",
    "| 配置 | 池 | 并发 20 | 并发 40 | 并发 100 | 峰值连接 |",
    "| 修复后（默认） | 30 | 20/0，p50 2.8s | 40/0，p50 2.1s | 100/0，p50 9.9s | 30 |",
    "  实测峰值活跃连接 = 15",
    "> 池**保持 15 不动**，只修掉「连接跨外部调用持有」，100 并发就零失败",
    # ↓ 主动承认旧数字是假的 —— 反向断言必须放行（真实误伤案例，逐字取自面试问答集）
    "- **你的数字可靠吗？**→ **我推翻过一次自己的数字。** 旧文档\"峰值连接 31→2\"是采样器复用长事务会话导致的假值（行集冻结在事务开始快照），**整条结论作废**，改成每次新签 AUTOCOMMIT 连接后重测。**测量方法错了，测得再认真也是错的。**",
    "于是旧文档里那个\"31 → 2\"就是这么来的——**它不是测量值，是首帧快照值**。**我的\"峰值连接从 31 降到 2\"这个结论是假的**",
)


def _self_test() -> int:
    """校验反向断言本身：必须拦下的句子全拦下、必须放行的句子不误伤。

    为什么值得单独一个入口：正则的漏与误伤**看不出来** —— 原版 3 条模式看着挺全，
    实测漏掉 8 条真实样本里的 6 条。没有这层，`check_forbidden` 会给出一份
    「全绿」的假保证，比没有校验更糟。
    """
    failed = 0
    for sample in _BAD_SAMPLES:
        # 自纠语境豁免对 BAD 样本同样生效 —— 否则「承认旧数字是假的」这句会被
        # 自己判成"该拦"，测试就变成了自欺欺人。
        if _is_self_correction(sample):
            failed += 1
            print(f"  ❌ 样本写错：它含自纠语境（不该出现在「必须拦下」组）：{sample[:60]}")
            continue
        if not any(re.search(pat, sample) for pat, _ in FORBIDDEN):
            failed += 1
            print(f"  ❌ 漏过（该拦没拦）：{sample}")
    for sample in _GOOD_SAMPLES:
        if _is_self_correction(sample):
            # 靠豁免放行也算通过，但明确打出来，避免误以为是被模式放行的
            print(f"  ↩️  靠自纠豁免放行（应如此）：{sample[:60]}")
            continue
        hit = next((pat for pat, _ in FORBIDDEN if re.search(pat, sample)), None)
        if hit:
            failed += 1
            print(f"  ❌ 误伤（不该拦）：{sample}\n      命中模式 {hit!r}")
    total = len(_BAD_SAMPLES) + len(_GOOD_SAMPLES)
    print(f"反向断言自测：{total - failed}/{total} 通过")
    return 1 if failed else 0


def _endpoint_count() -> int:
    """端点数：以 FastAPI 实际注册的路由表为准（= 面试官问的「多少个接口」）

    不用 grep 装饰器：那会把 `@router.get` 这类字符串也算进来，
    且无法区分「注册了」与「写在注释里」。
    """
    sys.path.insert(0, str(BACKEND))
    from fastapi.routing import APIRoute  # noqa: PLC0415

    from app.main import app  # noqa: PLC0415

    return len([r for r in app.routes if isinstance(r, APIRoute)])


def _unit_test_count() -> int:
    """单测数：以 pytest **实际收集**为准，而不是数源码里的 `def test_`

    ⚠️ 教训来自姊妹项目：那里 `test_config.py` 源码里 20 个 `def test_`，
    因为一个 `@pytest.mark.parametrize` 实际收集出 25 个用例 —— 两个口径
    都能自圆其说，于是文档和「实测」对不上。所以这里只认收集结果，
    并在收集失败时直接报错，绝不退回静态计数。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=BACKEND,
        env={**os.environ, "RABBITMQ_ENABLED": "false"},
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"(\d+) tests? collected", proc.stdout)
    if not match:
        raise RuntimeError(f"pytest --collect-only 没给出用例数，输出尾部：\n{proc.stdout[-500:]}")
    return int(match.group(1))


def _config_facts() -> dict[str, int]:
    """连接池数字：读 settings，不写死（写死就会在改配置后骗人）"""
    sys.path.insert(0, str(BACKEND))
    from app.core.config import settings  # noqa: PLC0415

    return {
        "db_pool_size": settings.DB_POOL_SIZE,
        "db_max_overflow": settings.DB_MAX_OVERFLOW,
        "db_pool_capacity": settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW,
    }


def _db_facts() -> dict[str, int]:
    """需要连库的规模数字（--with-db 才查）

    ⚠️ 「174 篇文档」的口径是**有 chunk 的文档数**（= 真正进了索引的），
    不是 `kh_document` 的行数 —— 后者含已删除与未发布的，是 180 行。
    口径写在文档里必须一致，否则每次核对都会「发现」一个假差异。
    """
    import asyncio  # noqa: PLC0415

    from sqlalchemy import text  # noqa: PLC0415

    sys.path.insert(0, str(BACKEND))
    from app.db.session import async_session_factory  # noqa: PLC0415

    async def query() -> dict[str, int]:
        async with async_session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT count(*) AS chunks, count(DISTINCT document_id) AS docs FROM doc_chunk"
                    )
                )
            ).one()
        return {"chunk_total": int(row[0]), "indexed_docs": int(row[1])}

    return asyncio.run(query())


# --------------------------------------------------------------------- 文档声称


@dataclass(frozen=True)
class Claim:
    path: str  # 相对仓库根
    pattern: str  # 必须含一个捕获组 = 文档声称的数字；**文件内所有命中都会被核对**
    fact: str
    note: str = ""
    # 允许的相对偏差。**只给「行数」这类天然浮动的规模数字用**：
    # 改一行注释它就变，如果要求精确相等，CI 会在每次正常改代码时误报，
    # 然后就没人看这个校验了。计数类（用例数 / 评测集条数 / 端点数）必须精确相等。
    tolerance: float = 0.0


# 行数容差：1%。不能给更松 —— 姊妹项目那次「文档过期一个 commit」是
# 180/6072 = 2.96%，3% 的容差会把它放过（实测踩到）。
LINE_TOLERANCE = 0.01


CLAIMS: tuple[Claim, ...] = (
    # ---- README：评测集构成（指标的分母，最容易过期）----
    Claim("README.md", r"\*\*(\d+) 条评测集\*\*", "rag_plus_gen", note="检索 + 生成；图谱另计"),
    Claim(
        "README.md",
        r"检索 (\d+) = \d+ 单跳 \+ 16 多跳 \+ 12 负例",
        "rag_total",
    ),
    Claim("README.md", r"其中 \*\*(\d+) 条难例\*\*", "rag_hard"),
    Claim("README.md", r"难题 = (\d+) 条近义改写题", "rag_hard"),
    Claim("README.md", r"从 12 条扩到 (\d+) 条", "gen_total"),
    # ---- README：规模 ----
    Claim("README.md", r"知识库共 \*\*(\d+) 篇文档 / 467 块\*\*", "indexed_docs", note="需 --with-db"),
    # ---- 应答稿 / 问答集：对外数字必须一致 ----
    # ⚠️ 简历类文件（简历-项目一-修订版.md / 简历素材汇总.md）在公开发布前已撤下，
    #    它们的 CLAIMS 条目同步移除；文件若重新入库，记得把条目加回来。
    Claim("docs/interview/面试应答稿.md", r"后端 (\d+) 文件", "backend_files"),
    # ⚠️ 拆成两条、两个数字都用分组捕获：原来写成「(\d+) 端点 · 60 脚本」，
    #    把脚本数当成了字面量 —— 每加一个脚本就得改本脚本，否则门禁红。
    Claim("docs/interview/面试应答稿.md", r"(\d+) 端点 · \d+ 脚本", "endpoints"),
    Claim("docs/interview/面试应答稿.md", r"\d+ 端点 · (\d+) 脚本", "scripts_total"),
    # 面试实战问答集：逐题累积的现场作答稿，里面单列了一张「可报数字」表。
    # 它最容易和简历 / README 分叉（实测：端点数曾写 78+3、脚本数曾写 62），
    # 而这张表恰好是面试现场照着念的那张 —— 说出去的数字必须只有一套口径。
    Claim("docs/interview/面试实战问答集.md", r"\| REST 端点 \| \*\*(\d+) 个\*\*", "endpoints"),
    Claim("docs/interview/面试实战问答集.md", r"工程脚本\s+\| `backend/scripts/` (\d+) 个", "scripts_total"),
    # ---- 压测实测值：只保证「所有文档说的是同一个数」，不保证它是最新测量 ----
    Claim("README.md", r"并发 20 时 (\d+)/20 失败", "pre_fix_fail_at_20"),
    Claim("README.md", r"\| 修复前（`Depends` 守卫） \| 15 \|.*\| (\d+) \|", "pre_fix_peak_conn"),
    Claim("README.md", r"\| 修复后（默认） \| 30 \|.*\| (\d+) \|", "post_fix_peak_conn"),
    # p50 的**起始值**写死在模式里（2.5s）：旧文档写的 0.5s 是更早机器状态的读数，
    # 一旦回流就「模式未命中」→ 失败。捕获的是跳变后的 31s。
    Claim("todo.md", r"p50 2\.5s→(\d+)s", "pre_fix_p50_s"),
    Claim("项目导读.md", r"p50 从 2\.5s 跳到 (\d+)s", "pre_fix_p50_s"),
    Claim("docs/interview/面试题库-AgenticRAG.md", r"p50 从 2\.5 秒跳到 (\d+) 秒", "pre_fix_p50_s"),
    Claim("docs/interview/面试应答稿.md", r"p50 从 2\.5 秒直接跳到 (\d+) 秒", "pre_fix_p50_s"),
)


def _load_lines(rel: str) -> list[str]:
    return (REPO / rel).read_text(encoding="utf-8").splitlines()


# 反向断言扫描的目录白名单：**只看项目文档，不扫数据与依赖**
_DOC_SCAN_DIRS = ("docs",)
_DOC_SCAN_FILES = ("README.md", "项目导读.md", "todo.md")
# 明确排除：评测语料是**被检索的数据**（内容里出现"峰值连接"等词属于正常），
# 以及 node_modules / .venv 等依赖目录。
_DOC_SCAN_SKIP = ("node_modules", ".venv", "volumes", ".git", "eval")


def _iter_docs() -> list[Path]:
    """待扫的项目文档（递归，含 docs/ 子目录）

    刻意不扫 `backend/eval/`：那里的 md 是喂给检索的语料，不是项目文档，
    用它们做反向断言会误报。
    """
    out: list[Path] = [REPO / name for name in _DOC_SCAN_FILES if (REPO / name).exists()]
    for d in _DOC_SCAN_DIRS:
        out.extend(sorted((REPO / d).rglob("*.md")))
    return [
        p
        for p in out
        if p.is_file() and not any(s in p.parts for s in _DOC_SCAN_SKIP)
    ]


def check_forbidden(failures: list[str]) -> int:
    """扫全仓 markdown：已证伪的说法出现即失败。

    为什么需要这层：错误结论会以**新措辞**回流 —— 「31 → 2」被清掉后，
    「从 31 降到 2」「峰值连接 2」还留在另外两份文档里，人工 grep 漏了三次。
    所以要反向断言，而不是只检查「数字写对了」。

    ⚠️ 必须**递归**扫，不能用 `REPO.glob("*.md")`：文档挪进 `docs/` 后，
    只扫根目录会从13 份掉到 4 份，反向断言等于静默失效 —— 而静默失效比
    没有校验更糟（本脚本的设计前提之一）。
    """
    hits = 0
    exempted = 0
    for path in sorted(_iter_docs()):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _is_self_correction(line):
                exempted += 1
                continue
            for pattern, reason in FORBIDDEN:
                if re.search(pattern, line):
                    hits += 1
                    failures.append(
                        f"{path.name}:{lineno}  出现已证伪的说法：{line.strip()[:70]}\n"
                        f"    → {reason}"
                    )
    if exempted:
        print(f"  ↩️  {exempted} 行含自纠语境（承认并纠正旧说法），已豁免反向断言")
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="校验文档数字与源码/评测集一致")
    parser.add_argument("--with-db", action="store_true", help="额外校验需要连库的规模数字")
    parser.add_argument("--list", action="store_true", help="只打印算出来的权威数字")
    parser.add_argument("--self-test", action="store_true", help="只校验反向断言模式本身（该拦的拦下、不该拦的放行）")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()

    facts: dict[str, int] = {}
    facts.update(_eval_facts())
    facts.update(_code_facts())
    facts.update(_config_facts())
    facts.update(MEASURED_FACTS)
    try:
        facts["endpoints"] = _endpoint_count()
    except Exception as err:  # noqa: BLE001
        print(f"❌ 端点统计失败（无法导入 app）：{err}")
        return 2
    try:
        facts["unit_tests"] = _unit_test_count()
    except Exception as err:  # noqa: BLE001
        print(f"❌ 单测统计失败：{err}")
        return 2

    db_needed = {c.fact for c in CLAIMS if c.fact in {"indexed_docs", "chunk_total"}}
    if args.with_db and db_needed:
        try:
            facts.update(_db_facts())
        except Exception as err:  # noqa: BLE001
            print(f"⚠️  --with-db 失败，跳过连库数字：{err}")

    if args.list:
        print("权威数字（来自评测集 / 源码 / 配置）：")
        for key in sorted(facts):
            print(f"  {key:26} {facts[key]}")
        return 0

    # 按文档聚合，报错时能直接定位到行
    lines_cache: dict[str, list[str]] = {}
    failures: list[str] = []
    checked = 0
    # fact → 全文所有声称位置，用于**跨写法 / 跨文档**的一致性核对
    seen: dict[str, list[tuple[str, int, int]]] = {}
    print("文档数字校验（权威来源：评测集 / 源码 / 配置）\n")

    for claim in CLAIMS:
        if claim.fact not in facts:
            print(f"  ⏭  {claim.path}: 「{claim.fact}」需要 --with-db，本次跳过")
            continue
        # 文件可能已被删除（求职材料常在发布前撤下）。这里报可读错误而不是让
        # _load_lines 抛 FileNotFoundError —— 后者看不出是「文件没了」还是「脚本坏了」。
        if not (REPO / claim.path).exists():
            failures.append(
                f"{claim.path}: 文件不存在，但门禁仍在核对它的事实「{claim.fact}」\n"
                f"    → 若该文档已撤下，请连同这里的 CLAIMS 条目一起删除；"
                f"若只是改名/移位，请更新 claim.path"
            )
            continue
        if claim.path not in lines_cache:
            lines_cache[claim.path] = _load_lines(claim.path)
        expected = facts[claim.fact]
        # 该文件内**所有**命中都核对：只查第一处会漏掉「同一个文件里两个数打架」
        hits = [
            (lineno, int(m.group(1).replace(",", "")))
            for lineno, line in enumerate(lines_cache[claim.path], start=1)
            if (m := re.search(claim.pattern, line))
        ]
        if not hits:
            failures.append(
                f"{claim.path}: 模式未命中 {claim.pattern!r}\n"
                f"    → 文档可能被改写了。请同步本脚本的模式，或恢复该数字。"
            )
            continue
        for lineno, claimed in hits:
            checked += 1
            seen.setdefault(claim.fact, []).append((claim.path, lineno, claimed))
            drift = abs(claimed - expected) / expected if expected else 0.0
            if drift <= claim.tolerance:
                suffix = f"（实际 {expected}，容差内）" if claimed != expected else ""
                print(f"  ✅ {claim.path}:{lineno:<4} {claim.fact:24} = {claimed}{suffix}")
            else:
                print(f"  ❌ {claim.path}:{lineno:<4} {claim.fact:24} 文档={claimed}  实际={expected}")
                failures.append(f"{claim.path}:{lineno}  文档写 {claimed}，实际 {expected}")

    # 同一个事实在不同文档/不同写法里必须给出同一个数。
    # 这条专门拦「措辞不重叠但指的是同一个数字」的分叉 —— 本项目的
    # 「峰值连接 16 / 31 / 32 / 40」正是这么来的。
    for fact, entries in sorted(seen.items()):
        distinct = {value for _, _, value in entries}
        if len(distinct) > 1:
            detail = "、".join(f"{path}:{ln}={v}" for path, ln, v in sorted(entries))
            failures.append(f"「{fact}」在不同文档/写法里说法不一致：{detail}")

    checked += check_forbidden(failures)

    print(f"\n共核对 {checked} 处数字，{len(failures)} 处不一致")
    if failures:
        print("\n不一致明细：")
        for item in failures:
            print(f"  · {item}")
        return 1
    print("✅ 文档数字与源码 / 评测集一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

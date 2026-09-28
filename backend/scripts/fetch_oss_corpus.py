"""抓取公开开源文档，作为**真实**评测语料

**为什么不自己编文档**
    评测集的说服力取决于语料是否真实。合成语料能测「逻辑对不对」，
    但经不起面试里的那句追问：「这 14 篇是不是你自己写的？」

**选源：`jaywcjlove/linux-command`**（MIT 许可，37k stars，621 篇中文命令手册）
    - 中文、真实、体裁统一（都是技术手册），与企业知识库语境相符；
    - **天然成簇、互为强干扰**：`ps`/`top`/`kill`/`pkill`、`tar`/`zip`/`7z`……
      近义文档正是把 `recall` / `hit@1` 拉开差距的必要条件；
    - MIT 许可，可自由使用。

⚠️ **抓取结果不入库**（体积 + 不让仓库混入第三方内容）：目录已 gitignore，
   由本脚本按需拉取。目录内会写一份 `SOURCE.md` 说明来源与许可。

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false \\
        .venv/bin/python scripts/fetch_oss_corpus.py --limit 160
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, ".")

import httpx  # noqa: E402

REPO = "jaywcjlove/linux-command"
LICENSE = "MIT"
# ⚠️ 用 jsDelivr 而不是 GitHub API：后者未认证限额只有 60 次/小时，跑几次探测就没了
#（实测直接 403 rate limit exceeded）。jsDelivr 同时提供文件清单与内容分发，无此限制。
LIST_API = f"https://data.jsdelivr.com/v1/packages/gh/{REPO}@master?structure=flat"
RAW_BASE = f"https://cdn.jsdelivr.net/gh/{REPO}@master/command"
_COMMAND_PREFIX = "/command/"
OUT_DIR = Path(__file__).resolve().parent.parent / "eval" / "corpus_oss"

# 过滤掉「太短的桩」与「超长怪物」：桩文档没有检索价值，超长文档会挤占 top-k
MIN_BYTES = 400
MAX_BYTES = 20000
_CONCURRENCY = 8


def _list_files(client: httpx.Client) -> list[dict]:
    """取 command/ 目录清单（按文件名排序，保证可复现）"""
    resp = client.get(LIST_API, timeout=30)
    resp.raise_for_status()
    items: list[dict] = []
    for entry in resp.json().get("files", []):
        path = entry.get("name", "")
        if not (path.startswith(_COMMAND_PREFIX) and path.endswith(".md")):
            continue
        name = path[len(_COMMAND_PREFIX) :]
        size = int(entry.get("size") or 0)
        # 清单不带 size 时不做长度过滤（宁可多抓，不要静默丢样本）
        if size and not (MIN_BYTES <= size <= MAX_BYTES):
            continue
        items.append({"name": name, "size": size})
    return sorted(items, key=lambda it: it["name"])


def _normalize(name: str, text: str) -> str:
    """原文首行是命令名、紧跟一行 `===`；统一成 `# 标题` + 正文"""
    lines = text.splitlines()
    body_start = 0
    for index, line in enumerate(lines[:5]):
        stripped = line.strip()
        if stripped and set(stripped) <= {"="}:
            body_start = index + 1
            break
    body = "\n".join(lines[body_start:]).strip()
    return f"# {name}\n\n{body}\n"


async def _download(
    client: httpx.AsyncClient, semaphore: asyncio.Semaphore, item: dict
) -> tuple[str, bool]:
    stem = item["name"][:-3]
    target = OUT_DIR / f"{stem}.md"
    if target.exists():
        return stem, False
    async with semaphore:
        try:
            resp = await client.get(f"{RAW_BASE}/{item['name']}", timeout=30)
            resp.raise_for_status()
        except Exception as err:  # noqa: BLE001 — 单篇失败不阻塞整批
            print(f"  ⚠️ 跳过 {item['name']}：{err}")
            return stem, False
    target.write_text(_normalize(stem, resp.text), encoding="utf-8")
    return stem, True


def _write_source_note(count: int) -> None:
    (OUT_DIR / "SOURCE.md").write_text(
        f"""# 语料来源

- 仓库：https://github.com/{REPO}
- 许可：{LICENSE}
- 内容：Linux 命令中文手册（每篇一个命令）
- 本次抓取：{count} 篇

抓取脚本：`backend/scripts/fetch_oss_corpus.py`

> 本目录**不入版本库**：体积较大，且不应让项目仓库混入第三方内容。
> 需要评测语料时运行上述脚本按需拉取。
""",
        encoding="utf-8",
    )


async def main(args: argparse.Namespace) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers={"User-Agent": "my-rag-eval/1.0"}) as client:
        items = _list_files(client)
    chosen = items[: args.limit]
    print(
        f"源：{REPO}（{LICENSE}）\n"
        f"可选 {len(items)} 篇（已过滤 <{MIN_BYTES}B 的桩与 >{MAX_BYTES}B 的超长文档）"
        f"→ 取前 {len(chosen)} 篇"
    )

    semaphore = asyncio.Semaphore(_CONCURRENCY)
    async with httpx.AsyncClient(headers={"User-Agent": "my-rag-eval/1.0"}) as client:
        results = await asyncio.gather(
            *[_download(client, semaphore, item) for item in chosen]
        )

    fresh = sum(1 for _, new in results if new)
    existing = len(results) - fresh
    _write_source_note(len(list(OUT_DIR.glob("*.md"))))
    print(f"完成：新增 {fresh} 篇，已存在 {existing} 篇 → {OUT_DIR}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="抓取公开开源中文文档作为评测语料")
    parser.add_argument("--limit", type=int, default=160, help="抓取篇数上限")
    parsed = parser.parse_args()
    raise SystemExit(asyncio.run(main(parsed)))

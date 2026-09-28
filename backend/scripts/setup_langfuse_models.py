"""向自托管 Langfuse 注册本项目用到的模型价格（幂等）

**为什么需要这一步**：Langfuse 只认识它内置价格表里的模型（gpt-4o / claude 之类）。
本项目用的是 `deepseek-v4-flash`、`qwen-vl-max` 这类别名 —— 匹配不到价格时，
Langfuse 会照常记录 token 用量，但 `cost_details` 是**空的**，成本看板恒为 0。
（实测确认：`usage_details={'input':1234,...}` 而 `cost_details={}`。）

**计价口径**：Langfuse 的 `inputPrice` / `outputPrice` 是**单 token 价**，
不是「每百万 token」。内置 babbage-002 是 `4e-07`，即 $0.40 / 1M tokens，可作参照。
本脚本把 config 里的「元 / 百万 token」换算成单 token 价提交。

⚠️ 数值单位是**元**（与 config 一致）。Langfuse UI 里的货币符号是纯展示，
不要被 `$` 误导 —— 口径以 config 的 `AI_PRICE_*_PER_MTOK` 为准。

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false \\
        .venv/bin/python scripts/setup_langfuse_models.py
"""
import base64
import json
import sys
import urllib.error
import urllib.request

sys.path.insert(0, ".")

from app.core.config import settings  # noqa: E402

# 本项目实际会出现在 trace 里的模型别名（与 config 默认值对齐）
_MODEL_NAMES = [
    settings.MODEL_NAME or settings.LLM_MODEL or "deepseek-v4-flash",
    "qwen-plus",  # chat_query_rewrite 等处的兜底模型
    settings.VISION_MODEL,  # 图像 OCR
]


def _request(method: str, path: str, payload: dict | None = None) -> tuple[int, str]:
    url = f"{settings.LANGFUSE_HOST.rstrip('/')}{path}"
    token = base64.b64encode(
        f"{settings.LANGFUSE_PUBLIC_KEY}:{settings.LANGFUSE_SECRET_KEY}".encode()
    ).decode()
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


def _prices() -> tuple[float, float]:
    """元/百万 token → 单 token 价（Langfuse 口径）"""
    return (
        settings.AI_PRICE_INPUT_PER_MTOK / 1_000_000,
        settings.AI_PRICE_OUTPUT_PER_MTOK / 1_000_000,
    )


def _existing_names() -> set[str]:
    status, body = _request("GET", "/api/public/models?limit=100")
    if status != 200:
        raise SystemExit(f"读取模型列表失败：HTTP {status} {body[:200]}")
    return {m["modelName"] for m in json.loads(body).get("data", [])}


def main() -> int:
    if not (settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY):
        raise SystemExit("未配置 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY")

    input_price, output_price = _prices()
    print(
        f"单价（元 / 百万 token）：输入 {settings.AI_PRICE_INPUT_PER_MTOK}、"
        f"输出 {settings.AI_PRICE_OUTPUT_PER_MTOK}"
        f" → 单 token：{input_price:.3e} / {output_price:.3e}"
    )

    existing = _existing_names()
    for name in _MODEL_NAMES:
        if name in existing:
            print(f"  跳过（已存在）：{name}")
            continue
        status, body = _request(
            "POST",
            "/api/public/models",
            {
                "modelName": name,
                "matchPattern": f"(?i)^({name})$",
                "unit": "TOKENS",
                "inputPrice": input_price,
                "outputPrice": output_price,
            },
        )
        if status in (200, 201):
            print(f"  已注册：{name}")
        else:
            print(f"  ⚠️ 注册失败 {name}：HTTP {status} {body[:160]}")

    print("\n完成。重新跑一次对话后，cost_details 就会带上金额。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""向自托管 Langfuse 注册 / 修复本项目用到的模型价格（幂等）

**为什么需要这一步**：Langfuse 只认识它内置价格表里的模型（gpt-4o / claude 之类）。
本项目用的是 `deepseek-flash`、`qwen-vl-max` 这类模型 —— 匹配不到价格时，Langfuse
会照常记录 token 用量，但 `cost_details` 是**空的**，成本看板恒为 0。
（实测确认：`usage_details={'input':1234,...}` 而 `cost_details={}`。）

**两个必须踩对的坑（都是实测出来的）**：

1. **模型名必须用规范名 `deepseek-flash`**：`deepseek-v4-flash` 是 DeepSeek 已退役的
   别名（仍可用、按 Flash 价计费），但 **API 响应回显的是规范名**，而 Langfuse 是拿
   上报的模型名去匹配价格的 —— 用别名注册，实际入库的名字是 `deepseek-flash`，
   匹配落空 → 成本恒为 0。这里注册时把**规范名与旧别名都放进 matchPattern**，两个
   名字都能匹配（老代码 / 老 trace 也覆盖）。
2. **思考 token 要单独定价**：Langfuse 会把输出拆成 `output`（正文）与
   `output_reasoning`（思考），只登记 `input`/`output` 时**思考 token 不计费** →
   成本系统性偏低（实测一次调用：Python 按含思考算 0.001496 元，Langfuse 只按正文
   算 0.000384，差 3.9 倍）。DeepSeek 的计费口径是「输入 + 输出 token」，思考算输出，
   所以 `output_reasoning` 取与 `output` **同价**（与 Langfuse 内置 o3 / gpt-5 的
   定义一致）。

**计价口径**：Langfuse 的 `prices` 是**单 token 价**，不是「每百万 token」。
本脚本把 config 里的「元 / 百万 token」换算成单 token 价提交。

⚠️ 数值单位是**元**（与 config 一致）。Langfuse UI 里的货币符号是纯展示，
不要被 `$` 误导 —— 口径以 config 的 `AI_PRICE_*_PER_MTOK` 为准。

⚠️ **写 `output_reasoning` 必须走 `pricingTiers` 入参**：顶层 `prices` 字段会被 API
静默忽略（实测：顶层传 prices 建出来的模型只有 input/output 两项），只有
`pricingTiers: [{name, isDefault, priority, conditions, prices: {usage_type: price}}]`
才会真正落库。

⚠️ **Langfuse 公开 API 不支持更新模型**（PUT/PATCH 都是 405），所以「修复」的实现是
**删除后重建**。重建不影响历史 trace 上已算出的成本。

用法：
    cd backend && PYTHONPATH=. RABBITMQ_ENABLED=false \\
        .venv/bin/python scripts/setup_langfuse_models.py
"""
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, ".")

from app.core.config import settings  # noqa: E402

# 本项目实际会出现在 trace 里的模型名（与 config 默认值对齐）
_MODEL_NAMES = [
    settings.MODEL_NAME or settings.LLM_MODEL or "deepseek-flash",
    "qwen-plus",  # chat_query_rewrite 等处的兜底模型
    settings.VISION_MODEL,  # 图像 OCR
]

# 同一模型的历史别名：注册进 matchPattern，老 trace / 老配置也能匹配上价格
_LEGACY_ALIASES: dict[str, list[str]] = {
    "deepseek-flash": ["deepseek-v4-flash"],
}


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


def _match_pattern(name: str) -> str:
    """规范名 + 历史别名都匹配（Langfuse 用正则匹配上报的模型名）"""
    names = [name, *_LEGACY_ALIASES.get(name, [])]
    return rf"(?i)^({'|'.join(names)})$"


def _prices() -> tuple[float, float]:
    """元/百万 token → 单 token 价（Langfuse 口径）"""
    return (
        settings.AI_PRICE_INPUT_PER_MTOK / 1_000_000,
        settings.AI_PRICE_OUTPUT_PER_MTOK / 1_000_000,
    )


def _desired_prices() -> dict[str, dict[str, float]]:
    """按 usage_type 的期望定价：input / output / output_reasoning（思考同输出价）"""
    input_price, output_price = _prices()
    return {
        "input": {"price": input_price},
        "output": {"price": output_price},
        "output_reasoning": {"price": output_price},
    }


def _existing() -> dict[str, dict]:
    """已注册模型：name → 模型对象（含 id / matchPattern / prices / isLangfuseManaged）

    ⚠️ 必须翻页：Langfuse 内置模型有数百个，`limit=100` 一页装不下项目自己的模型。
    """
    models: dict[str, dict] = {}
    page = 1
    while True:
        status, body = _request("GET", f"/api/public/models?limit=100&page={page}")
        if status != 200:
            raise SystemExit(f"读取模型列表失败：HTTP {status} {body[:200]}")
        data = json.loads(body).get("data", [])
        for m in data:
            models[m["modelName"]] = m
        if len(data) < 100:
            return models
        page += 1


def _needs_repair(model: dict, pattern: str, prices: dict) -> str | None:
    """返回需要修复的原因；无需修复返回 None"""
    if model.get("isLangfuseManaged"):
        return None  # 内置模型不动
    if model.get("matchPattern") != pattern:
        return f"匹配模式不同（现有 {model.get('matchPattern')}）"
    have = model.get("prices") or {}
    missing = [t for t in prices if t not in have]
    if missing:
        return f"缺少 usage_type 定价：{','.join(missing)}"
    return None


def main() -> int:
    if not (settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY):
        raise SystemExit("未配置 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY")

    input_price, output_price = _prices()
    prices = _desired_prices()
    print(
        f"单价（元 / 百万 token）：输入 {settings.AI_PRICE_INPUT_PER_MTOK}、"
        f"输出 {settings.AI_PRICE_OUTPUT_PER_MTOK}"
        f" → 单 token：{input_price:.3e} / {output_price:.3e}"
        f"（另有 output_reasoning 同输出价）"
    )

    existing = _existing()
    for name in _MODEL_NAMES:
        pattern = _match_pattern(name)
        current = existing.get(name)
        # 只对**主模型**做「修复」（删除重建）：它的名字/思考定价会直接影响成本口径；
        # qwen 等旁路模型不产生思考 token，缺 output_reasoning 定价无害，
        # 没必要为一处无害的差异去删除重建（删了重建失败反而会让成本归零）。
        is_primary = name == _MODEL_NAMES[0]

        if current is not None:
            reason = _needs_repair(current, pattern, prices) if is_primary else None
            if reason is None:
                print(f"  跳过（已存在）：{name}")
                continue
            # 公开 API 无更新端点（PUT/PATCH → 405），只能删除后重建
            mid = urllib.parse.quote(current["id"], safe="")
            status, body = _request("DELETE", f"/api/public/models/{mid}")
            if status not in (200, 204):
                print(f"  ⚠️ 删除旧定义失败 {name}：HTTP {status} {body[:160]}")
                continue
            print(f"  已删除旧定义（{reason}）：{name}")

        status, body = _request(
            "POST",
            "/api/public/models",
            {
                "modelName": name,
                "matchPattern": pattern,
                "unit": "TOKENS",
                # ⚠️ 顶级 prices / outputPrice 写不进 output_reasoning（实测被忽略），
                # 必须走 pricingTiers 才落库
                "pricingTiers": [
                    {
                        "name": "Standard",
                        "isDefault": True,
                        "priority": 0,
                        "conditions": [],
                        "prices": {t: v["price"] for t, v in prices.items()},
                    }
                ],
            },
        )
        if status in (200, 201):
            print(f"  已注册：{name}（匹配 {pattern}，定价 {sorted(prices)}）")
        else:
            print(f"  ⚠️ 注册失败 {name}：HTTP {status} {body[:200]}")

    # 清理历史别名模型行：新定义的正则已覆盖旧别名，两行并存会导致同一模型名匹配到
    # 两个定价定义（歧义）。只删「名字恰好是某模型的历史别名」的行。
    aliases = {a for names in _LEGACY_ALIASES.values() for a in names}
    for name, model in existing.items():
        if name in aliases:
            mid = urllib.parse.quote(model["id"], safe="")
            status, _ = _request("DELETE", f"/api/public/models/{mid}")
            print(f"  已清理历史别名模型行：{name}（HTTP {status}）")

    print("\n完成。重新跑一次对话后，cost_details 就会带上金额（含思考 token）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

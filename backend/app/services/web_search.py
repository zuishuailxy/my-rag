"""联网搜索服务（对应 TS ai/web-search.service.ts）

⚠️ 刻意迁移：TS 原版用 Bocha（api.bochaai.com），本项目改用 Tavily
（api.tavily.com），对外接口形状保持一致（WebSearchHit / WebSearchResult），
供 ai_stream 的 web_search 工具调用。字段映射：

- title  ← results[].title
- url    ← results[].url
- snippet ← results[].content（Tavily 无 summary/snippet 之分，content 即摘要）
- siteName ← Tavily 不返回，取 URL hostname 兜底（前端展示用）

Tavily 无 Bocha 的 freshness=noLimit 等价参数（默认全网检索），省略；
失败不抛错——返回 items=[] + error 文案，由上层决定是否调用工具失败。
"""

import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from app.core.config import settings

logger = logging.getLogger("WebSearchService")

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"


@dataclass
class WebSearchHit:
    """单条搜索命中（对应 TS WebSearchHit）"""

    title: str
    url: str
    snippet: str
    siteName: str | None = None


@dataclass
class WebSearchResult:
    """搜索结果（对应 TS WebSearchResult；error 非 None 表示本次未命中原因）"""

    query: str
    items: list[WebSearchHit] = field(default_factory=list)
    error: str | None = None


def _site_name(url: str) -> str | None:
    """从 URL 提取 hostname 作为 siteName（Tavily 不返回该字段，刻意兜底）"""
    try:
        host = urlparse(url).hostname
        return host or None
    except ValueError:
        return None


class WebSearchService:
    """Tavily 联网搜索：知识库不足时供 agent 的 web_search 工具调用"""

    async def search(self, query: str, count: int = 5) -> WebSearchResult:
        """检索并返回结构化命中；任何失败都收敛为 error 文案，不抛错"""
        api_key = settings.TAVILY_API_KEY
        if not api_key:
            return WebSearchResult(
                query=query, items=[], error="未配置 TAVILY_API_KEY，无法联网搜索"
            )

        body = {
            "query": query,
            # 对齐 TS 的 Math.min(Math.max(count, 1), 10)
            "max_results": min(max(count, 1), 10),
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    _TAVILY_SEARCH_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
        except httpx.HTTPError as exc:
            logger.warning("Tavily 搜索请求异常：query=%s, %s", query, exc)
            return WebSearchResult(query=query, items=[], error=f"搜索请求异常：{exc}")

        if resp.status_code != 200:
            detail = resp.text[:120]
            logger.warning("Tavily 搜索失败：status=%d", resp.status_code)
            return WebSearchResult(
                query=query, items=[], error=f"搜索失败（{resp.status_code}）{detail}"
            )

        try:
            data = resp.json()
        except ValueError:
            return WebSearchResult(query=query, items=[], error="搜索接口返回非 JSON")

        results = data.get("results") or []
        items = [
            WebSearchHit(
                title=str(page["title"]),
                url=str(page["url"]),
                snippet=str(page.get("content") or "").strip(),
                siteName=_site_name(str(page["url"])),
            )
            for page in results
            if page.get("url") and page.get("title")
        ]
        return WebSearchResult(query=query, items=items)


# 全局单例
web_search_service = WebSearchService()

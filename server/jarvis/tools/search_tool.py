"""Web search and page reading.

Three backends, tried in order, all free:
  1. SearXNG — self-hosted metasearch, no key, no quota. Best option; the compose
     file ships one alongside Jarvis.
  2. Brave Search API — free tier, needs a key, used only if SearXNG is absent.
  3. DuckDuckGo HTML endpoint — no key at all, but brittle. Last-resort fallback.
"""

from __future__ import annotations

import logging
import re

import httpx

from ..config import get_settings
from .base import ToolResult, registry

log = logging.getLogger(__name__)


async def _searxng(query: str, limit: int) -> list[dict]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        resp = await client.get(
            f"{settings.searxng_url.rstrip('/')}/search",
            params={"q": query, "format": "json", "safesearch": 0},
            headers={"User-Agent": settings.user_agent},
        )
        resp.raise_for_status()
        data = resp.json()
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": (r.get("content") or "")[:400],
            "engine": r.get("engine", ""),
        }
        for r in data.get("results", [])[:limit]
    ]


async def _brave(query: str, limit: int) -> list[dict]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(limit, 20)},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": settings.brave_api_key,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": re.sub(r"<[^>]+>", "", r.get("description", ""))[:400],
            "engine": "brave",
        }
        for r in (data.get("web", {}).get("results") or [])[:limit]
    ]


async def _duckduckgo(query: str, limit: int) -> list[dict]:
    from bs4 import BeautifulSoup

    settings = get_settings()
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        resp = await client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": settings.user_agent},
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

    results = []
    for node in soup.select(".result")[: limit * 2]:
        link = node.select_one(".result__a")
        snippet = node.select_one(".result__snippet")
        if not link:
            continue
        results.append(
            {
                "title": link.get_text(" ").strip(),
                "url": link.get("href", ""),
                "snippet": snippet.get_text(" ").strip()[:400] if snippet else "",
                "engine": "duckduckgo",
            }
        )
        if len(results) >= limit:
            break
    return results


@registry.tool(
    name="web_search",
    description=(
        "Search the web. Use whenever the user asks about current events, facts you are not "
        "certain of, product/price lookups, or anything after your training cutoff. Returns "
        "titles, URLs and snippets — call web_read on a result to get the full page text."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "limit": {"type": "integer", "description": "Max results. Default 8."},
        },
        "required": ["query"],
    },
    tags=["search"],
)
async def web_search(query: str, limit: int = 8):
    settings = get_settings()
    limit = max(1, min(limit, 20))
    attempts: list[tuple[str, object]] = []
    if settings.searxng_url:
        attempts.append(("searxng", _searxng))
    if settings.brave_api_key:
        attempts.append(("brave", _brave))
    attempts.append(("duckduckgo", _duckduckgo))

    errors = []
    for name, fn in attempts:
        try:
            results = await fn(query, limit)
            if results:
                return ToolResult.success(
                    {"query": query, "backend": name, "results": results},
                    display={"type": "search", "query": query, "results": results},
                )
            errors.append(f"{name}: no results")
        except Exception as exc:
            log.warning("Search backend %s failed: %s", name, exc)
            errors.append(f"{name}: {exc}")

    return ToolResult.fail(
        "Every search backend failed (" + "; ".join(errors) + "). "
        "Setting SEARXNG_URL to a self-hosted SearXNG makes this reliable."
    )


@registry.tool(
    name="web_read",
    description=(
        "Fetch a URL and return its readable text. Use after web_search when a snippet isn't "
        "enough, or when the user gives you a link and asks what it says. Skips paywalled "
        "content — it reads only what is publicly served."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full URL including https://."},
            "max_chars": {"type": "integer", "description": "Truncate at this length. Default 6000."},
        },
        "required": ["url"],
    },
    tags=["search"],
)
async def web_read(url: str, max_chars: int = 6000):
    from bs4 import BeautifulSoup

    settings = get_settings()
    if not url.startswith(("http://", "https://")):
        return ToolResult.fail("URL must start with http:// or https://")

    try:
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, headers={"User-Agent": settings.user_agent}
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        return ToolResult.fail(f"Could not fetch that page: {exc}")

    if "html" not in resp.headers.get("content-type", "").lower():
        return ToolResult.success({"url": url, "text": resp.text[:max_chars]})

    soup = BeautifulSoup(resp.text, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()
    main = soup.select_one("article, main, [role=main]") or soup.body or soup
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n")).strip()
    title = soup.title.get_text().strip() if soup.title else ""

    return ToolResult.success(
        {
            "url": url,
            "title": title,
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
        }
    )

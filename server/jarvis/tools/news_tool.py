"""News headlines.

On the WSJ: the Journal publishes free public RSS feeds carrying headlines and
standfirst summaries for every section, and that is what this tool reads. It does
*not* log into your WSJ account or scrape the paid article text — automating a
paywall login is against the Journal's terms of use and would put your
subscription at risk. Headlines and summaries come through here; when you want a
full piece, Jarvis hands you the link and you open it signed in.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import get_settings
from .base import ToolResult, registry

log = logging.getLogger(__name__)

WSJ_FEEDS = {
    "top": "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
    "world": "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
    "markets": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
    "business": "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",
    "tech": "https://feeds.content.dowjones.io/public/rss/RSSWSJD",
    "opinion": "https://feeds.content.dowjones.io/public/rss/RSSOpinion",
    "lifestyle": "https://feeds.content.dowjones.io/public/rss/RSSLifestyle",
}

OTHER_FEEDS = {
    "reuters_business": "https://feeds.reuters.com/reuters/businessNews",
    "ap_top": "https://rsshub.app/apnews/topics/apf-topnews",
    "bbc_world": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "hacker_news": "https://hnrss.org/frontpage",
}


async def _fetch_feed(url: str, limit: int) -> list[dict]:
    import feedparser

    settings = get_settings()
    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True, headers={"User-Agent": settings.user_agent}
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        raw = resp.content

    parsed = await asyncio.to_thread(feedparser.parse, raw)
    items = []
    for entry in parsed.entries[:limit]:
        summary = (getattr(entry, "summary", "") or "").strip()
        if "<" in summary:
            from bs4 import BeautifulSoup

            summary = BeautifulSoup(summary, "lxml").get_text(" ").strip()
        items.append(
            {
                "title": (getattr(entry, "title", "") or "").strip(),
                "summary": summary[:500],
                "link": getattr(entry, "link", ""),
                "published": getattr(entry, "published", "") or getattr(entry, "updated", ""),
            }
        )
    return [i for i in items if i["title"]]


@registry.tool(
    name="wsj_headlines",
    description=(
        "Fetch current Wall Street Journal headlines with summaries from the WSJ's public "
        "feeds. Covers top news, markets, business, tech, opinion and lifestyle. Returns "
        "headline + summary + link; full article text is behind the WSJ paywall, so give the "
        "user the link to read the whole piece in their subscription."
    ),
    parameters={
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "enum": list(WSJ_FEEDS),
                "description": "Which WSJ section. Default 'top'.",
            },
            "limit": {"type": "integer", "description": "Max headlines. Default 12."},
        },
    },
    tags=["news"],
)
async def wsj_headlines(section: str = "top", limit: int = 12):
    url = WSJ_FEEDS.get(section.lower())
    if not url:
        return ToolResult.fail(f"Unknown section. Choose from: {', '.join(WSJ_FEEDS)}")
    try:
        items = await _fetch_feed(url, min(limit, 30))
    except httpx.HTTPError as exc:
        return ToolResult.fail(f"Could not reach the WSJ feed: {exc}")
    return ToolResult.success(
        {
            "source": "The Wall Street Journal",
            "section": section,
            "count": len(items),
            "articles": items,
            "note": "Summaries are from WSJ's public feed; open the link to read in full.",
        },
        display={"type": "news", "source": "WSJ", "articles": items},
    )


@registry.tool(
    name="news_headlines",
    description=(
        "Fetch headlines from a non-WSJ source (Reuters, AP, BBC, Hacker News) or any RSS "
        "feed URL. Use when the user wants a second source or a topic WSJ doesn't cover."
    ),
    parameters={
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": f"One of {', '.join(OTHER_FEEDS)}, or a full RSS feed URL.",
            },
            "limit": {"type": "integer", "description": "Max headlines. Default 12."},
        },
        "required": ["source"],
    },
    tags=["news"],
)
async def news_headlines(source: str, limit: int = 12):
    url = OTHER_FEEDS.get(source.lower(), source if source.startswith("http") else "")
    if not url:
        return ToolResult.fail(f"Unknown source. Known: {', '.join(OTHER_FEEDS)}, or pass a feed URL.")
    try:
        items = await _fetch_feed(url, min(limit, 30))
    except httpx.HTTPError as exc:
        return ToolResult.fail(f"Could not reach that feed: {exc}")
    return ToolResult.success(
        {"source": source, "count": len(items), "articles": items},
        display={"type": "news", "source": source, "articles": items},
    )

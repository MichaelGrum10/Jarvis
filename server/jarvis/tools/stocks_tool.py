"""Market data via yfinance — free, no API key, no rate-limit plan to manage."""

from __future__ import annotations

import asyncio
import logging

from .. import cache
from .base import ToolResult, registry

log = logging.getLogger(__name__)


def _yf():
    import yfinance  # imported lazily: it's slow and pulls in pandas

    return yfinance


def _num(value, digits: int = 2):
    try:
        if value is None:
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _quote_sync(symbol: str) -> dict:
    ticker = _yf().Ticker(symbol)
    info = {}
    try:
        info = ticker.get_info() or {}
    except Exception as exc:
        log.warning("get_info failed for %s: %s", symbol, exc)

    price = info.get("currentPrice") or info.get("regularMarketPrice")
    prev = info.get("previousClose") or info.get("regularMarketPreviousClose")

    # get_info is flaky for some tickers; fall back to the price history, which isn't.
    if price is None:
        try:
            hist = ticker.history(period="2d")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
                if prev is None and len(hist) > 1:
                    prev = float(hist["Close"].iloc[-2])
        except Exception as exc:
            log.warning("history fallback failed for %s: %s", symbol, exc)

    if price is None:
        raise ValueError(f"No market data for '{symbol}'. Check the ticker symbol.")

    change = _num(price - prev) if prev else None
    change_pct = _num((price - prev) / prev * 100) if prev else None

    return {
        "symbol": symbol.upper(),
        "name": info.get("shortName") or info.get("longName") or symbol.upper(),
        "price": _num(price),
        "currency": info.get("currency", "USD"),
        "previous_close": _num(prev),
        "change": change,
        "change_percent": change_pct,
        "day_high": _num(info.get("dayHigh")),
        "day_low": _num(info.get("dayLow")),
        "open": _num(info.get("open")),
        "volume": info.get("volume"),
        "market_cap": info.get("marketCap"),
        "pe_ratio": _num(info.get("trailingPE")),
        "forward_pe": _num(info.get("forwardPE")),
        "dividend_yield": _num(info.get("dividendYield"), 4),
        "fifty_two_week_high": _num(info.get("fiftyTwoWeekHigh")),
        "fifty_two_week_low": _num(info.get("fiftyTwoWeekLow")),
        "analyst_target": _num(info.get("targetMeanPrice")),
        "recommendation": info.get("recommendationKey"),
        "sector": info.get("sector"),
        "exchange": info.get("fullExchangeName") or info.get("exchange"),
    }


@registry.tool(
    name="stock_quote",
    description=(
        "Get live-ish market data for one or more tickers: price, day move, 52-week range, "
        "P/E, market cap, analyst target. Use for any question about a stock, ETF, index "
        "(^GSPC, ^DJI, ^IXIC), currency pair (EURUSD=X) or crypto (BTC-USD)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ticker symbols, e.g. ['AAPL','MSFT','^GSPC'].",
            }
        },
        "required": ["symbols"],
    },
    tags=["finance"],
)
async def stock_quote(symbols: list[str]):
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",") if s.strip()]
    symbols = [s.strip().upper() for s in symbols][:12]
    if not symbols:
        return ToolResult.fail("No ticker symbols given.")

    cached = cache.get("stocks", ",".join(sorted(symbols)))
    if cached is not None:
        return ToolResult.success(cached, display={"type": "stocks", "quotes": cached.get("quotes", [])})

    results, errors = [], []
    settled = await asyncio.gather(
        *(asyncio.to_thread(_quote_sync, s) for s in symbols), return_exceptions=True
    )
    for symbol, outcome in zip(symbols, settled, strict=True):
        if isinstance(outcome, Exception):
            errors.append({"symbol": symbol, "error": str(outcome)})
        else:
            results.append(outcome)

    if not results:
        return ToolResult.fail("; ".join(e["error"] for e in errors) or "No data returned.")

    payload = {"quotes": results, "errors": errors}
    # Only a clean result is cached. Storing a partial one would serve the same
    # missing ticker for the next minute without ever retrying it.
    if not errors:
        cache.put("stocks", ",".join(sorted(symbols)), payload)
    return ToolResult.success(payload, display={"type": "stocks", "quotes": results})


@registry.tool(
    name="stock_history",
    description=(
        "Get historical price performance for a ticker over a period, with the start/end "
        "price and total return. Use for 'how has X done this year' style questions."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker symbol."},
            "period": {
                "type": "string",
                "enum": ["5d", "1mo", "3mo", "6mo", "ytd", "1y", "2y", "5y", "max"],
                "description": "Look-back window. Default 1mo.",
            },
        },
        "required": ["symbol"],
    },
    tags=["finance"],
)
async def stock_history(symbol: str, period: str = "1mo"):
    def run() -> dict:
        hist = _yf().Ticker(symbol).history(period=period)
        if hist.empty:
            raise ValueError(f"No history for '{symbol}' over {period}.")
        first, last = float(hist["Close"].iloc[0]), float(hist["Close"].iloc[-1])
        return {
            "symbol": symbol.upper(),
            "period": period,
            "start_date": str(hist.index[0].date()),
            "end_date": str(hist.index[-1].date()),
            "start_price": _num(first),
            "end_price": _num(last),
            "change_percent": _num((last - first) / first * 100),
            "period_high": _num(float(hist["High"].max())),
            "period_low": _num(float(hist["Low"].min())),
            "closes": [
                {"date": str(idx.date()), "close": _num(float(val))}
                for idx, val in list(hist["Close"].items())[-60:]
            ],
        }

    data = await asyncio.to_thread(run)
    return ToolResult.success(data, display={"type": "stock_chart", **data})


@registry.tool(
    name="stock_news",
    description="Recent news headlines for a specific ticker, from Yahoo Finance.",
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker symbol."},
            "limit": {"type": "integer", "description": "Max headlines. Default 8."},
        },
        "required": ["symbol"],
    },
    tags=["finance", "news"],
)
async def stock_news(symbol: str, limit: int = 8):
    def run() -> list[dict]:
        raw = _yf().Ticker(symbol).news or []
        items = []
        for entry in raw[:limit]:
            content = entry.get("content", entry)
            items.append(
                {
                    "title": content.get("title") or entry.get("title", ""),
                    "publisher": (content.get("provider") or {}).get("displayName")
                    or entry.get("publisher", ""),
                    "link": (content.get("canonicalUrl") or {}).get("url") or entry.get("link", ""),
                    "published": content.get("pubDate") or entry.get("providerPublishTime"),
                }
            )
        return [i for i in items if i["title"]]

    items = await asyncio.to_thread(run)
    return ToolResult.success(
        {"symbol": symbol.upper(), "articles": items},
        display={"type": "news", "articles": items},
    )

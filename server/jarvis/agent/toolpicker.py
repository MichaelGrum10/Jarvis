"""Choosing which tools to offer the model for a given turn.

Sending all ~30 schemas every turn costs about 3,700 tokens before the user has
said a word, and on a free tier metered per minute that is most of the budget.
It also makes answers worse: a model choosing between 30 options picks wrong more
often than one choosing between six.

So tools are grouped into domains and only relevant domains are offered. Matching
is deliberately generous — the cost of omitting a needed tool (the model cannot
do the job at all) is far worse than the cost of including an unneeded one (a few
hundred wasted tokens). When a request is ambiguous, more domains are included,
not fewer.
"""

from __future__ import annotations

import re

# Words that put a domain in scope. Chosen for how people actually phrase things,
# not for tidiness — "what's on today" is a calendar question with no calendar
# word in it, so bare temporal phrases have to pull the calendar in too.
DOMAIN_HINTS: dict[str, tuple[str, ...]] = {
    "calendar": (
        "calendar", "schedule", "meeting", "appointment", "event", "booking", "book",
        "busy", "free", "today", "tomorrow", "tonight", "week", "monday", "tuesday",
        "wednesday", "thursday", "friday", "saturday", "sunday", "diary", "agenda",
        "reschedule", "cancel", "when am i", "what's on", "whats on", "haircut",
        "dentist", "doctor", "lunch", "dinner", "flight", "remind",
        # Amendments to something already booked. These rarely name the calendar
        # at all — "make it 4 instead" is a calendar write with no calendar word
        # in it — and without the tools in scope the model can only apologise.
        "move it", "move the", "change it", "change the", "make it", "push it",
        "push the", "instead", "delete it", "remove it", "call it off",
        "earlier", "later", "shift it",
    ),
    "mail": (
        "mail", "email", "inbox", "message from", "unread", "sender", "reply",
        "forward", "subject", "attachment", "spam", "newsletter", "wrote", "sent me",
    ),
    "messages": (
        "message", "text", "imessage", "sms", "texted", "wrote", "reply", "said",
        "chat", "conversation", "whatsapp", "group",
    ),
    "finance": (
        "stock", "share", "price", "market", "ticker", "portfolio", "invest",
        "nasdaq", "s&p", "dow", "crypto", "bitcoin", "eth", "earnings", "dividend",
        "aapl", "tsla", "nvda", "msft", "up", "down", "chart", "graph",
    ),
    "news": (
        "news", "headline", "wsj", "journal", "article", "happening", "story",
        "reported", "press", "briefing", "world", "politics",
    ),
    "search": (
        "search", "look up", "google", "find out", "what is", "who is", "how do",
        "how to", "why", "explain", "research", "website", "link", "read this",
        "according to", "latest",
        # Opening a specific piece. "read it to me" after a headline list carries
        # no search word at all, and browse_page lives in this domain.
        "open it", "open the", "read it", "read the", "full article", "whole thing",
        "expand", "what does it say", "paywall", "article", "http",
    ),
    "places": (
        "near", "nearby", "around here", "close by", "restaurant", "cafe", "coffee",
        "bar", "shop", "store", "pharmacy", "gym", "barber", "haircut", "petrol",
        "gas", "parking", "hotel", "directions", "where", "address", "walk",
    ),
    "location": ("where am i", "my location", "here", "nearby", "around here"),
    "device": (
        "open", "launch", "play", "start", "app", "spotify", "music", "maps",
        "shortcut", "run", "turn on", "turn off", "lights", "home",
    ),
    "memory": (
        "remember", "forget", "know about me", "my preference", "i like", "i prefer",
        "i always", "note that", "keep in mind", "my name", "my wife", "my husband",
    ),
    # The vault is a different thing from memory: memory is a handful of facts
    # about how the user likes things done, notes are everything they have ever
    # written down. "Remember that…" is deliberately in both — the model picks.
    "notes": (
        "note", "notes", "my notes", "wrote down", "write down", "remember that",
        "my vault", "obsidian", "did i write", "what do i know about", "capture",
        "jot", "second brain", "knowledge", "galaxy", "according to my",
    ),
}

# Always offered: cheap, tiny, and needed constantly. current_time in particular
# has to be present on every turn — without it the model reasons about "today"
# from its training data and confidently gets the date wrong.
ALWAYS = ("current_time", "memory_search", "system_status")

# A request that matches nothing specific still needs to be able to do something
# useful, so fall back to the domains that answer most general questions.
# "notes" earns its place here rather than only on a keyword: a question that
# matched nothing specific — "what's the deal with the Punic Wars" — is exactly
# the case where the user's own writing should be consulted before the model's
# training. It costs one schema, and only when a vault is configured.
FALLBACK_DOMAINS = ("calendar", "mail", "search", "memory", "notes")

MAX_TOOLS = 16


def _domain_of(tool) -> str:
    return tool.tags[0] if tool.tags else "misc"


def relevant_domains(message: str) -> set[str]:
    text = message.lower()
    hits = {
        domain
        for domain, hints in DOMAIN_HINTS.items()
        if any(hint in text for hint in hints)
    }

    # A ticker-shaped token means finance even without a finance word: "how's
    # NVDA doing" has no keyword in it otherwise.
    if re.search(r"\b[A-Z]{2,5}\b", message) and not hits:
        hits.add("finance")

    return hits or set(FALLBACK_DOMAINS)


def select_tools(message: str, tools: list, *, max_tools: int = MAX_TOOLS) -> list:
    """Pick the tools worth offering for this message.

    `tools` is the already credential-filtered list, so anything unconfigured has
    been removed before we get here.
    """
    domains = relevant_domains(message)

    chosen: list = []
    seen: set[str] = set()

    for tool in tools:
        if tool.name in ALWAYS:
            chosen.append(tool)
            seen.add(tool.name)

    for tool in tools:
        if tool.name in seen:
            continue
        if _domain_of(tool) in domains:
            chosen.append(tool)
            seen.add(tool.name)

    # Writing tools travel with their readers: asking to "cancel my 3pm" matches
    # the calendar domain, and the delete tool has to come along or the model can
    # find the event and then be unable to act on it.
    if len(chosen) > max_tools:
        chosen = chosen[:max_tools]

    return chosen


def describe_selection(message: str, tools: list, chosen: list) -> str:
    """One-line summary for logs — makes a wrong selection easy to spot."""
    return (
        f"tools {len(chosen)}/{len(tools)} "
        f"domains={sorted(relevant_domains(message))} "
        f"names={[t.name for t in chosen]}"
    )

"""Self-diagnosis: check every credential and integration, on your own machine.

    docker compose exec jarvis python -m jarvis.doctor
    # or, outside docker:
    python -m jarvis.doctor

Prints a pass/fail line per integration with the specific fix for each failure.
Secrets are masked in all output, so it is safe to copy the results into a chat
or an issue when you want help — no credential ever appears.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys

from .config import get_settings

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)

PASS, FAIL, WARN = f"{GREEN}✓{RESET}", f"{RED}✗{RESET}", f"{YELLOW}!{RESET}"


def mask(secret: str) -> str:
    """Show enough to identify which credential it is, never enough to use it."""
    if not secret:
        return "(not set)"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}…{secret[-2:]} ({len(secret)} chars)"


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def ok(self, label: str, detail: str = "") -> None:
        print(f"  {PASS} {label}" + (f" {DIM}— {detail}{RESET}" if detail else ""))

    def bad(self, label: str, problem: str, fix: str = "") -> None:
        self.failures += 1
        print(f"  {FAIL} {label} {DIM}— {problem}{RESET}")
        if fix:
            for line in fix.strip().splitlines():
                print(f"      {YELLOW}→{RESET} {line.strip()}")

    def warn(self, label: str, problem: str, fix: str = "") -> None:
        self.warnings += 1
        print(f"  {WARN} {label} {DIM}— {problem}{RESET}")
        if fix:
            for line in fix.strip().splitlines():
                print(f"      {YELLOW}→{RESET} {line.strip()}")


def _looks_like_app_password(value: str) -> bool:
    """Apple's app-specific passwords are always four lowercase quads."""
    import re

    return bool(re.fullmatch(r"[a-z]{4}-[a-z]{4}-[a-z]{4}-[a-z]{4}", value or ""))


def header(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}")


async def check_groq(report: Report) -> None:
    header("Groq (chat + voice transcription)")
    settings = get_settings()

    if not settings.groq_api_key:
        report.bad(
            "API key", "GROQ_API_KEY is not set",
            "Get a free key at https://console.groq.com/keys\n"
            "Add it to .env, then: docker compose up -d",
        )
        return
    if not settings.groq_api_key.startswith("gsk_"):
        report.warn("API key format", f"{mask(settings.groq_api_key)} doesn't start with gsk_")

    import httpx

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{settings.groq_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                json={
                    "model": settings.groq_model,
                    "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                    "max_tokens": 5,
                },
            )
    except httpx.HTTPError as exc:
        report.bad("Connection", str(exc), "Check the server has outbound internet access.")
        return

    if response.status_code == 401:
        report.bad(
            "Authentication", "Groq rejected the key",
            "The key is wrong, revoked, or has a stray space.\n"
            "Regenerate at https://console.groq.com/keys and re-run scripts/setup.sh",
        )
    elif response.status_code == 429:
        report.warn("Rate limit", "key is valid but currently throttled — resets hourly")
    elif response.status_code >= 400:
        report.bad("API", f"HTTP {response.status_code}: {response.text[:160]}")
    else:
        reply = response.json()["choices"][0]["message"]["content"].strip()
        report.ok("Chat model", f"{settings.groq_model} replied {reply!r}")
        report.ok("Key", mask(settings.groq_api_key))

    await _check_pool(report)


def classify_models(
    endpoints, by_provider: dict[str, dict]
) -> tuple[list[str], list[str]]:
    """Split endpoints into 'the key genuinely can't reach this' and 'unproven'.

    Each endpoint is judged only against its own provider's catalogue, and only
    when that catalogue was readable at all. Merging providers lets Groq's list
    answer for Gemini; treating an unreadable list as an empty one reports every
    model on that provider as missing. Both send you to fix a model name that was
    never the problem — the usual cause is the key, one level up.
    """
    from .llm.client import normalise_model_id

    missing, unverified = [], []
    for endpoint in endpoints:
        catalogue = by_provider.get(endpoint.base_url)
        if catalogue is None:
            unverified.append(endpoint.label)
        elif normalise_model_id(endpoint.model) not in {
            normalise_model_id(m) for m in catalogue
        }:
            missing.append(f"{endpoint.label} ({endpoint.model})")
    return sorted(missing), sorted(unverified)


async def _check_pool(report: Report) -> None:
    """Show the endpoint pool and what the configured keys can actually reach.

    Model names and rate limits change without notice, so this reads them from
    the provider rather than asserting anything from memory.
    """
    from .llm.client import get_llm

    client = get_llm()
    total = len(client.pool)
    if total <= 1:
        report.warn(
            "Capacity", "a single endpoint — one busy minute stops everything",
            "Add a second provider (free tiers at cerebras.ai or openrouter.ai)\n"
            "See docs/model-capacity.md",
        )
    else:
        report.ok("Capacity", f"{total} endpoints with failover")
    for entry in client.pool.status():
        if entry.get("retired"):
            report.bad(
                f"  {entry['label']}", f"retired — {entry['retired']}",
                "Remove it from GROQ_MODEL_LADDER in .env; it will never recover.",
            )
            continue
        if entry["available"]:
            state = "ready"
        else:
            because = entry.get("cooling_because") or "earlier failure"
            state = f"cooling {entry['cooling_for']}s after {because}"
        report.ok(f"  {entry['label']}", f"key {entry['key']}, {state}")

    try:
        by_provider = await client.list_models_by_provider()
    except Exception as exc:
        report.warn("Model list", f"could not fetch: {type(exc).__name__}")
        return

    if not by_provider:
        report.warn("Model list", "no provider returned a catalogue")
        return

    missing, unverified = classify_models(client.pool.endpoints, by_provider)
    if missing:
        report.bad(
            "Configured models", f"not available to your key: {', '.join(missing)}",
            "Pick a replacement from that provider's list below, then:\n"
            "bash scripts/setkey.sh GROQ_MODEL the-model-id   (or GEMINI_MODEL, CEREBRAS_MODEL)\n"
            "docker compose up -d",
        )
    if unverified:
        report.warn(
            "Model list", f"could not be read for: {', '.join(unverified)}",
            "Usually a rejected key. The model names themselves are unproven either way.",
        )

    # Listed per provider rather than merged, for the same reason as above: what
    # you can switch a Gemini endpoint to is whatever Gemini offers, and a
    # combined list makes Groq's models look like candidates for it.
    configured = {e.model for e in client.pool.endpoints}
    for base_url, catalogue in sorted(by_provider.items()):
        host = base_url.split("//")[-1].split("/")[0]
        chat_models = sorted(
            m for m in catalogue
            if not any(skip in m.lower() for skip in ("whisper", "tts", "guard", "embed"))
        )
        report.ok(host, f"{len(chat_models)} models usable for chat")
        for model_id in chat_models[:12]:
            marker = " *" if model_id in configured else ""
            print(f"      {DIM}{model_id}{marker}{RESET}")
        if len(chat_models) > 12:
            print(f"      {DIM}… and {len(chat_models) - 12} more{RESET}")
    print(f"      {DIM}* currently in your ladder{RESET}")


async def check_mail(report: Report) -> None:
    header("iCloud Mail (IMAP)")
    settings = get_settings()

    missing = settings.missing_for("mail")
    if missing:
        report.warn(
            "Not configured", f"missing {', '.join(missing)}",
            "Email tools stay hidden until this is set. See docs/setup.md step 3.",
        )
        return

    password = settings.icloud_app_password
    if not _looks_like_app_password(password):
        report.warn(
            "Password format", f"{mask(password)} isn't the xxxx-xxxx-xxxx-xxxx shape",
            "iCloud only accepts an APP-SPECIFIC password here, not your Apple ID password.\n"
            "Generate one at https://account.apple.com → Sign-In and Security",
        )

    def probe() -> tuple[bool, str, int]:
        import imaplib

        try:
            conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=25)
        except (TimeoutError, OSError) as exc:
            return False, f"cannot reach {settings.imap_host}: {exc}", 0
        try:
            conn.login(settings.icloud_email, password)
        except imaplib.IMAP4.error as exc:
            return False, f"login rejected: {exc}", 0
        try:
            status, data = conn.select("INBOX", readonly=True)
            count = int(data[0]) if status == "OK" and data and data[0] else 0
            return True, "", count
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    ok, problem, count = await asyncio.to_thread(probe)
    if ok:
        report.ok("Login", f"{settings.icloud_email}")
        report.ok("INBOX", f"{count} messages visible")
    else:
        report.bad(
            "Login", problem,
            "Use an app-specific password from https://account.apple.com,\n"
            "not your Apple ID password. Revoke and regenerate if unsure.",
        )


async def check_calendar(report: Report) -> None:
    header("iCloud Calendar (CalDAV)")
    settings = get_settings()

    missing = settings.missing_for("calendar")
    if missing:
        report.warn("Not configured", f"missing {', '.join(missing)}")
        return

    from .integrations.apple_calendar import CalendarError, get_calendar

    try:
        names = await get_calendar().list_calendars()
    except CalendarError as exc:
        report.bad("Connection", str(exc))
        return
    except Exception as exc:
        report.bad(
            "Connection", f"{type(exc).__name__}: {exc}",
            "Confirm the same app-specific password works for Mail above.",
        )
        return

    if not names:
        report.warn(
            "Calendars", "connected, but no writable calendars found",
            "iCloud hides calendars not owned by the authenticated account.",
        )
        return

    report.ok("Login", settings.caldav_user)
    report.ok("Calendars", ", ".join(names[:6]) + ("…" if len(names) > 6 else ""))

    now = dt.datetime.now(dt.UTC)
    try:
        events = await get_calendar().events_between(now, now + dt.timedelta(days=7))
        report.ok("Read test", f"{len(events)} events in the next 7 days")
    except Exception as exc:
        report.warn("Read test", f"{type(exc).__name__}: {exc}")


async def check_messages(report: Report) -> None:
    header("Messages bridge (iMessage)")
    settings = get_settings()

    if settings.missing_for("messages"):
        report.warn(
            "Not configured", "BRIDGE_TOKEN is not set",
            "Only needed if you have a Mac to mirror iMessage. See docs/messages-bridge.md",
        )
        return

    from sqlalchemy import func, select

    from .db import BridgeHeartbeat, ChatMessage, init_db, session_scope, utcnow

    await init_db()
    async with session_scope() as session:
        beat = (await session.execute(select(BridgeHeartbeat).limit(1))).scalar_one_or_none()
        total = (await session.execute(select(func.count(ChatMessage.id)))).scalar() or 0

    if beat is None:
        report.warn(
            "Bridge", "has never connected",
            "On your Mac: python3 bridge/jarvis_bridge.py\n"
            "Needs Full Disk Access — see docs/messages-bridge.md",
        )
        return

    minutes = (utcnow() - beat.last_seen).total_seconds() / 60
    if minutes > settings.bridge_stale_minutes:
        report.warn(
            "Bridge", f"last seen {int(minutes)} minutes ago on {beat.hostname or 'unknown host'}",
            "The Mac may be asleep or the bridge stopped.\n"
            "Check: launchctl list | grep jarvis",
        )
    else:
        report.ok("Bridge", f"{beat.hostname}, {int(minutes)}m ago")
    report.ok("Mirrored messages", str(total))


async def check_outbound(report: Report) -> None:
    header("Outbound APIs (no credentials needed)")
    import httpx

    targets = [
        ("Yahoo Finance", "https://query2.finance.yahoo.com/v8/finance/chart/AAPL"),
        ("WSJ feed", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
        ("OpenStreetMap", "https://nominatim.openstreetmap.org/status"),
    ]
    settings = get_settings()

    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True, headers={"User-Agent": settings.user_agent}
    ) as client:
        for name, url in targets:
            try:
                response = await client.get(url)
                if response.status_code < 400:
                    report.ok(name, f"HTTP {response.status_code}")
                else:
                    report.warn(name, f"HTTP {response.status_code}")
            except httpx.HTTPError as exc:
                report.warn(name, f"unreachable: {type(exc).__name__}")

    if settings.searxng_url:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{settings.searxng_url.rstrip('/')}/search",
                    params={"q": "test", "format": "json"},
                )
            if response.status_code < 400:
                report.ok("SearXNG", settings.searxng_url)
            else:
                report.warn(
                    "SearXNG", f"HTTP {response.status_code}",
                    "Ensure 'json' is listed under search.formats in searxng/settings.yml",
                )
        except httpx.HTTPError as exc:
            report.warn("SearXNG", f"unreachable: {exc}")
    else:
        report.warn("SearXNG", "not configured — falling back to DuckDuckGo scraping")


def check_config(report: Report) -> None:
    header("Configuration")
    settings = get_settings()

    if settings.missing_for("auth"):
        report.bad(
            "Auth", f"missing {', '.join(settings.missing_for('auth'))}",
            "Run: bash scripts/setup.sh",
        )
    else:
        report.ok("Auth secret", mask(settings.auth_secret))
        report.ok("Access password", mask(settings.access_password))

    report.ok("Owner", settings.owner_name or "(not set)")
    report.ok("Timezone", settings.timezone)
    report.ok("Database", str(settings.db_path))

    # Which provider keys are actually loaded. Grepping .env only proves a line
    # exists — an empty value looks identical and does nothing.
    configured, blank = [], []
    for name in ("groq", "gemini", "cerebras", "openrouter", "together",
                 "github_models", "mistral", "nvidia", "huggingface", "custom"):
        value = getattr(settings, f"{name}_api_key", "")
        if value.strip():
            configured.append(name)
        elif value:
            blank.append(name)
    report.ok("Provider keys", ", ".join(configured) or "none")
    if blank:
        report.warn(
            "Empty keys", f"set but blank: {', '.join(blank)}",
            "A blank value is the same as unset. Re-run scripts/setkey.sh with the key.",
        )

    if settings.autonomy_enabled:
        report.warn("Autonomy", "ENABLED — Jarvis may modify its own source")
    else:
        report.ok("Autonomy", "disabled")

    # Reported from the running process, not from .env. Editing .env without
    # `docker compose up -d` is the usual reason a setting looks on and isn't:
    # the file says one thing and the container is still running the old value.
    mode = settings.improve_mode
    if mode == "off":
        report.warn(
            "Self-improvement", "off — no fixes will be proposed",
            "bash scripts/setkey.sh IMPROVE_MODE propose && docker compose up -d",
        )
    elif mode == "propose":
        report.ok(
            "Self-improvement",
            f"propose — every {settings.improve_interval_hours}h, on a branch for you to merge",
        )
    elif mode == "apply":
        report.warn(
            "Self-improvement",
            f"apply — merges its own changes every {settings.improve_interval_hours}h when tests pass",
        )
    else:
        report.bad(
            "Self-improvement", f"unknown mode {mode!r}",
            "IMPROVE_MODE must be one of: off, propose, apply",
        )

    # Setting the mode is only one of three things this needs. With the others
    # missing the loop still starts and still fails every cycle, in a log nobody
    # is watching — so report every blocker, not just the first.
    from .agent.selfimprove import blockers

    for blocker in blockers(settings):
        if "IMPROVE_MODE" in blocker["fix"]:
            continue  # already reported just above
        report.bad(f"  {blocker['what']}", "self-improvement can't run", blocker["fix"])


def check_browser(report: Report) -> None:
    header("Browser reader")
    import datetime as when

    from .integrations.browser import session_summary

    settings = get_settings()
    if not settings.browser_enabled:
        report.ok("Browser", "off — pages are read over plain HTTP")
        return

    try:
        import playwright  # noqa: F401
    except ImportError:
        report.bad(
            "Playwright", "enabled, but not installed in this image",
            "The image was built before this was turned on. Rebuild with:\n"
            "bash scripts/browser.sh on",
        )
        return
    report.ok("Playwright", "installed")

    summary = session_summary()
    if not summary["present"]:
        report.warn(
            "Session", "none stored — subscriber-only pages will show a teaser",
            "Import one: docs/browser.md",
        )
        return

    expires = summary["expires"]
    detail = f"{summary['cookies']} cookies for {', '.join(summary['domains'][:4])}"
    if expires is None:
        report.ok("Session", f"{detail} (session cookies, no fixed expiry)")
        return

    days = (expires - when.datetime.now(when.UTC).timestamp()) / 86400
    if days <= 0:
        report.bad("Session", f"{detail} — expired", "Re-import: docs/browser.md")
    elif days < 3:
        report.warn("Session", f"{detail} — expires in {days:.1f} days")
    else:
        report.ok("Session", f"{detail}, {days:.0f} days left")


async def check_voice(report: Report) -> None:
    header("Voice identity")
    from sqlalchemy import select

    from .db import SpeakerProfile, session_scope

    settings = get_settings()

    async with session_scope() as session:
        profile = (
            await session.execute(select(SpeakerProfile).limit(1))
        ).scalar_one_or_none()

    enrolled = profile is not None and profile.sample_count > 0
    if enrolled:
        report.ok(
            "Enrolled voice",
            f"{profile.sample_count} samples, consistency {profile.cohesion:.2f}",
        )
    else:
        report.warn(
            "Enrolled voice", "nothing enrolled",
            "Open the HUD (☰ → Open HUD) and tap 'Enrol voice'. Three samples minimum.",
        )

    # The pairing is what matters: enforcement without a profile locks nobody
    # out, and a profile without enforcement guards nothing. Either alone reads
    # as configured while doing nothing.
    if settings.require_voice_match and not enrolled:
        report.bad(
            "Voice matching", "required, but no voice is enrolled",
            "Every voice request will be refused until you enrol.",
        )
    elif settings.require_voice_match:
        report.ok("Voice matching", f"enforced at threshold {settings.voice_match_threshold}")
    elif enrolled:
        report.warn(
            "Voice matching", "enrolled but not enforced — anyone's voice is answered",
            "bash scripts/setkey.sh REQUIRE_VOICE_MATCH true && docker compose up -d",
        )
    else:
        report.ok("Voice matching", "off")

    report.ok("Wake word", f"{settings.wake_word!r}" + ("" if settings.require_wake_word else " (optional)"))


async def main() -> int:
    print(f"{BOLD}Jarvis doctor{RESET}")
    print(f"{DIM}Secrets are masked below — output is safe to share.{RESET}")

    report = Report()
    check_config(report)
    await check_groq(report)
    await check_mail(report)
    await check_calendar(report)
    await check_messages(report)
    await check_voice(report)
    check_browser(report)
    await check_outbound(report)

    print()
    if report.failures:
        print(f"{RED}{report.failures} problem(s) need fixing{RESET}", end="")
        print(f", {report.warnings} warning(s)" if report.warnings else "")
        return 1
    if report.warnings:
        print(f"{YELLOW}All required checks passed, {report.warnings} optional item(s) unconfigured{RESET}")
        return 0
    print(f"{GREEN}Everything checks out.{RESET}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)

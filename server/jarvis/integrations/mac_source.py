"""Calendar and mail read from the Mac, in the shapes the tools already expect.

The Mac agent can see things this server cannot: Apple Mail and Calendar as they
actually are on the machine, rather than as iCloud's CalDAV and IMAP endpoints
describe them. This adapter puts that behind the same `CalEvent` and
`MailSummary` objects the existing integrations return, so the tools gain a
source rather than a second code path.

It is a *source*, never the only one. The Mac is shut for most of the day, and a
calendar tool that fails whenever the lid is closed is worse than one that
quietly uses iCloud instead. Everything here reports whether it can serve a
request before it tries, and the tools fall back when it cannot.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..agentlink import AgentUnavailable, get_link
from .apple_calendar import CalEvent
from .apple_mail import MailSummary

log = logging.getLogger(__name__)

# The Mac window is expressed in minutes either side of now. Beyond a couple of
# months, enumerating every event in every calendar over AppleScript costs more
# than the answer is worth — CalDAV queries a range server-side and is the
# better tool for "what am I doing in November".
MAX_WINDOW_DAYS = 62

# Clock skew between two real machines is seconds, but a boundary event exactly
# on the hour should not depend on that. Ask for slightly more than needed and
# filter precisely here.
EDGE_MINUTES = 5


class MacSource:
    """Reads from Mail.app and Calendar.app via the companion agent."""

    @property
    def available(self) -> bool:
        return get_link().connected

    # ---- calendar ---------------------------------------------------------

    def can_serve_range(self, start: dt.datetime, end: dt.datetime) -> bool:
        """Whether the Mac can answer this range at all.

        Returning False here is how a wrong answer is avoided: the alternative
        is a plausible-looking empty list for a range the Mac was never asked
        about, which reads exactly like a free afternoon.
        """
        if not self.available:
            return False
        return (end - start) <= dt.timedelta(days=MAX_WINDOW_DAYS)

    async def events_between(
        self, start: dt.datetime, end: dt.datetime, calendar: str = ""
    ) -> list[CalEvent]:
        now = dt.datetime.now(dt.timezone.utc)
        start_offset = int((start - now).total_seconds() // 60) - EDGE_MINUTES
        end_offset = int((end - now).total_seconds() // 60) + EDGE_MINUTES

        reply = await get_link().call(
            "calendar.list_events",
            {"start_offset": start_offset, "end_offset": end_offset},
        )
        if not reply.get("ok"):
            raise AgentUnavailable(reply.get("error") or "The Mac refused that.")

        events = []
        for row in reply.get("data", {}).get("events", []):
            when = _parse(row.get("start"))
            # The widened window is for clock skew, not for returning extra
            # events. Filtering here keeps the range the caller asked for.
            if when is None or not (start <= when < end):
                continue
            if calendar and row.get("calendar", "").lower() != calendar.lower():
                continue
            events.append(
                CalEvent(
                    uid=row.get("uid", ""),
                    summary=row.get("summary", ""),
                    start=row.get("start", ""),
                    end=row.get("end", ""),
                    all_day=bool(row.get("all_day")),
                    location=row.get("location", ""),
                    description="",
                    calendar=row.get("calendar", ""),
                )
            )
        return events

    # ---- mail -------------------------------------------------------------

    async def recent(self, limit: int = 25) -> list[MailSummary]:
        reply = await get_link().call("mail.list_recent", {"limit": min(limit, 50)})
        if not reply.get("ok"):
            raise AgentUnavailable(reply.get("error") or "The Mac refused that.")
        return [_summary(row) for row in reply.get("data", {}).get("messages", [])]

    async def search(self, query: str, limit: int = 20) -> list[MailSummary]:
        reply = await get_link().call(
            "mail.search", {"query": query, "limit": min(limit, 50)}
        )
        if not reply.get("ok"):
            raise AgentUnavailable(reply.get("error") or "The Mac refused that.")
        return [_summary(row) for row in reply.get("data", {}).get("messages", [])]


def _summary(row: dict) -> MailSummary:
    return MailSummary(
        uid=row.get("uid", ""),
        subject=row.get("subject", ""),
        sender=row.get("sender", ""),
        sender_email=row.get("sender_email", ""),
        date=row.get("date", ""),
        # Neither is available over AppleScript without a per-message round trip
        # that would blow the time budget. Left empty and False rather than
        # guessed — the importance score weights attachments, so a guess would
        # quietly reorder the inbox.
        snippet="",
        unread=bool(row.get("unread")),
        has_attachments=False,
        mailbox=row.get("mailbox", "INBOX"),
    )


def _parse(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    # The agent sends an offset, but a naive value would compare-explode against
    # the aware bounds, so pin it to UTC rather than raising.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


_source: MacSource | None = None


def get_mac() -> MacSource:
    global _source
    if _source is None:
        _source = MacSource()
    return _source


# --------------------------------------------------------- choosing the source
#
# One place, because three callers need the same decision: the calendar tool,
# the mail tools, and the HUD. The HUD used to go straight to iCloud, which
# meant it reported "not configured" with a connected Mac sitting right there.


async def read_events(start: dt.datetime, end: dt.datetime, calendar: str = ""):
    """Events for a range, from whichever source can answer. Returns (events, source).

    The Mac first when it is awake: it sees every calendar the machine has,
    including local ones iCloud never receives, and answers without a round trip
    to Apple. iCloud otherwise, because the laptop is shut most of the day and a
    schedule readable only when the lid is open is not a schedule.

    A failure on the Mac side falls through rather than propagating. The failure
    modes are all transient — asleep mid-call, a permission not yet granted,
    Calendar busy syncing — and none of them is a reason to tell someone their
    day is empty.
    """
    from .apple_calendar import CalendarError, get_calendar

    mac = get_mac()
    mac_error = ""
    if mac.can_serve_range(start, end):
        try:
            return await mac.events_between(start, end, calendar), "Mac"
        except AgentUnavailable as exc:
            mac_error = str(exc)
            log.info("Mac calendar unavailable, falling back to iCloud: %s", exc)
        except Exception as exc:                 # noqa: BLE001
            mac_error = str(exc)
            log.warning("Mac calendar failed, falling back to iCloud", exc_info=True)

    try:
        return await get_calendar().events_between(start, end, calendar), "iCloud"
    except CalendarError as exc:
        raise CalendarError(_both_failed(mac, mac_error, exc)) from None


async def read_recent_mail(days: int, limit: int, unread_only: bool, mailbox: str):
    """Recent mail, from whichever source can answer. Returns (messages, source)."""
    from .apple_mail import MailError, get_mail

    mac = get_mac()
    mac_error = ""
    # Mail.app's AppleScript reaches the unified inbox. Any other mailbox, and
    # IMAP is the only thing that can serve the request at all.
    if mac.available and mailbox.upper() == "INBOX":
        try:
            items = await mac.recent(limit=min(limit, 50))
            if unread_only:
                items = [i for i in items if i.unread]
            if days:
                cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
                items = [i for i in items if _after(i.date, cutoff)]
            return items, "Mac"
        except AgentUnavailable as exc:
            mac_error = str(exc)
            log.info("Mac mail unavailable, falling back to iCloud: %s", exc)
        except Exception as exc:                 # noqa: BLE001
            mac_error = str(exc)
            log.warning("Mac mail failed, falling back to iCloud", exc_info=True)

    try:
        return await get_mail().recent(
            limit=min(limit, 60), mailbox=mailbox, unread_only=unread_only, days=days
        ), "iCloud"
    except MailError as exc:
        raise MailError(_both_failed(mac, mac_error, exc)) from None


async def search_mail(query: str, limit: int, mailbox: str):
    """Search, from whichever source can answer. Returns (messages, source)."""
    from .apple_mail import MailError, get_mail

    mac = get_mac()
    mac_error = ""
    if mac.available and mailbox.upper() == "INBOX":
        try:
            return await mac.search(query, limit=limit), "Mac"
        except AgentUnavailable as exc:
            mac_error = str(exc)
            log.info("Mac mail search unavailable, falling back to iCloud: %s", exc)
        except Exception as exc:                 # noqa: BLE001
            mac_error = str(exc)
            log.warning("Mac mail search failed, falling back to iCloud", exc_info=True)

    try:
        return await get_mail().search(query, limit=limit, mailbox=mailbox), "iCloud"
    except MailError as exc:
        raise MailError(_both_failed(mac, mac_error, exc)) from None


def _both_failed(mac: MacSource, mac_error: str, icloud_error: Exception) -> str:
    """One message naming both failures, because the fix is whichever is nearer.

    The likeliest real failure is Automation being refused on the Mac with no
    iCloud credentials behind it. Reporting only iCloud's complaint sends
    someone off to set CALDAV_USERNAME when the actual fix is a checkbox in
    System Settings — and they would have no reason to suspect otherwise.
    """
    from ..config import get_settings

    if mac_error:
        return f"Your Mac could not answer ({mac_error}), and iCloud did not either: {icloud_error}"
    if get_settings().agent_secret and not mac.available:
        return f"Your Mac is not connected, and iCloud did not answer either: {icloud_error}"
    return str(icloud_error)


def _after(stamp: str, cutoff: dt.datetime) -> bool:
    """Keep anything that cannot be dated. Dropping a message because its
    timestamp would not parse hides mail; keeping it shows one too many."""
    when = _parse(stamp)
    return True if when is None else when >= cutoff

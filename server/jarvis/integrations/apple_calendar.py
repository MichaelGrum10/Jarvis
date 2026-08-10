"""iCloud Calendar over CalDAV.

Apple has no public calendar API, but iCloud speaks standard CalDAV at
caldav.icloud.com using an app-specific password. That gives full read/create/
delete against the same calendars your iPhone shows.

The `caldav` library is synchronous, so every call runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from dataclasses import asdict, dataclass
from zoneinfo import ZoneInfo

import caldav
from caldav.elements import dav
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

# Per-calendar ceiling. Apple is usually fast, but a shared or subscribed
# calendar can hang, and one of those must not take the whole request with it.
CALENDAR_SEARCH_TIMEOUT = 12.0


@dataclass
class CalEvent:
    uid: str
    summary: str
    start: str
    end: str
    all_day: bool
    location: str = ""
    description: str = ""
    calendar: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class CalendarError(RuntimeError):
    pass


class AppleCalendar:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._principal = None
        self._lock = asyncio.Lock()

    # ---- connection -------------------------------------------------------

    def _connect(self):
        if self._principal is not None:
            return self._principal
        if not self.settings.caldav_user or not self.settings.caldav_pass:
            raise CalendarError(
                "iCloud calendar is not configured. Set CALDAV_USERNAME/CALDAV_PASSWORD "
                "(or ICLOUD_EMAIL/ICLOUD_APP_PASSWORD) to an app-specific password."
            )
        client = caldav.DAVClient(
            url=self.settings.caldav_url,
            username=self.settings.caldav_user,
            password=self.settings.caldav_pass,
        )
        self._principal = client.principal()
        return self._principal

    def _calendars(self) -> list:
        cals = self._principal_calendars()
        if not cals:
            raise CalendarError("No writable calendars found on this iCloud account.")
        return cals

    def _principal_calendars(self) -> list:
        principal = self._connect()
        out = []
        for cal in principal.calendars():
            try:
                # iCloud exposes read-only subscribed calendars too; keep only VEVENT ones.
                comps = cal.get_supported_components()
                if comps and "VEVENT" not in comps:
                    continue
            except Exception:
                pass
            out.append(cal)
        return out

    def _pick(self, name: str = ""):
        wanted = (name or self.settings.default_calendar).strip().lower()
        cals = self._calendars()
        if wanted:
            for cal in cals:
                if self._name_of(cal).lower() == wanted:
                    return cal
            for cal in cals:
                if wanted in self._name_of(cal).lower():
                    return cal
            raise CalendarError(
                f"No calendar named '{name}'. Available: {', '.join(self._name_of(c) for c in cals)}"
            )
        return cals[0]

    @staticmethod
    def _name_of(cal) -> str:
        try:
            return str(cal.get_properties([dav.DisplayName()])[dav.DisplayName().tag] or cal.name)
        except Exception:
            return str(getattr(cal, "name", "") or "Calendar")

    # ---- public async API -------------------------------------------------

    async def list_calendars(self) -> list[str]:
        return await asyncio.to_thread(
            lambda: [self._name_of(c) for c in self._calendars()]
        )

    async def events_between(
        self, start: dt.datetime, end: dt.datetime, calendar: str = ""
    ) -> list[CalEvent]:
        """Search every calendar at once rather than one after another.

        An iCloud account routinely carries five or more calendars, and each
        search is its own HTTP round-trip to Apple. Sequentially that is five
        round-trips added to a turn that is already waiting on a rate-limited
        model — which is most of why reading the calendar felt slow, and why it
        sometimes outlived the request altogether.
        """
        cals = await asyncio.to_thread(
            lambda: [self._pick(calendar)] if calendar else self._calendars()
        )

        async def search(cal):
            name = self._name_of(cal)
            try:
                results = await asyncio.wait_for(
                    asyncio.to_thread(
                        cal.search, start=start, end=end, event=True, expand=True
                    ),
                    timeout=CALENDAR_SEARCH_TIMEOUT,
                )
            except TimeoutError:
                # One slow calendar must not cost the whole answer. Shared and
                # subscribed calendars are the usual offenders.
                log.warning("Calendar %s timed out after %ss", name, CALENDAR_SEARCH_TIMEOUT)
                return []
            except Exception as exc:
                log.warning("Search failed on calendar %s: %s", name, exc)
                return []
            return [p for p in (self._parse(i, name) for i in results) if p]

        found: list[CalEvent] = []
        for chunk in await asyncio.gather(*(search(c) for c in cals)):
            found.extend(chunk)
        found.sort(key=lambda e: e.start)
        return found

    def _parse(self, item, calendar_name: str) -> CalEvent | None:
        try:
            ical = ICalendar.from_ical(item.data)
        except Exception:
            return None
        for comp in ical.walk("VEVENT"):
            dtstart = comp.get("DTSTART")
            dtend = comp.get("DTEND") or dtstart
            if dtstart is None:
                continue
            start_val = dtstart.dt
            end_val = dtend.dt if dtend is not None else start_val
            all_day = not isinstance(start_val, dt.datetime)
            return CalEvent(
                uid=str(comp.get("UID", "")),
                summary=str(comp.get("SUMMARY", "(no title)")),
                start=self._iso(start_val),
                end=self._iso(end_val),
                all_day=all_day,
                location=str(comp.get("LOCATION", "") or ""),
                description=str(comp.get("DESCRIPTION", "") or ""),
                calendar=calendar_name,
            )
        return None

    def _iso(self, value) -> str:
        if isinstance(value, dt.datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=ZoneInfo(self.settings.timezone))
            return value.isoformat()
        return value.isoformat()

    async def create_event(
        self,
        summary: str,
        start: dt.datetime,
        end: dt.datetime,
        *,
        location: str = "",
        description: str = "",
        calendar: str = "",
        alarm_minutes: int | None = None,
    ) -> CalEvent:
        return await asyncio.to_thread(
            self._create_event, summary, start, end, location, description, calendar, alarm_minutes
        )

    def _create_event(
        self, summary, start, end, location, description, calendar, alarm_minutes
    ) -> CalEvent:
        cal = self._pick(calendar)
        tz = ZoneInfo(self.settings.timezone)
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        if end.tzinfo is None:
            end = end.replace(tzinfo=tz)

        uid = f"{uuid.uuid4()}@jarvis"
        ical = ICalendar()
        ical.add("prodid", "-//Jarvis//EN")
        ical.add("version", "2.0")
        event = IEvent()
        event.add("uid", uid)
        event.add("dtstamp", dt.datetime.now(dt.UTC))
        # Written in UTC rather than with a TZID. A zoneinfo-aware datetime makes
        # icalendar emit `DTSTART;TZID=America/New_York:...`, but it does not also
        # emit the matching VTIMEZONE component — and RFC 5545 requires that any
        # referenced TZID be defined in the same VCALENDAR. iCloud enforces this
        # and rejects the event outright, which is why creation failed while
        # reading worked fine. UTC needs no VTIMEZONE and is unambiguous; Apple's
        # clients render it in local time regardless.
        event.add("dtstart", start.astimezone(dt.UTC))
        event.add("dtend", end.astimezone(dt.UTC))
        event.add("summary", summary)
        if location:
            event.add("location", location)
        if description:
            event.add("description", description)
        if alarm_minutes:
            from icalendar import Alarm

            alarm = Alarm()
            alarm.add("action", "DISPLAY")
            alarm.add("description", summary)
            alarm.add("trigger", dt.timedelta(minutes=-abs(alarm_minutes)))
            event.add_component(alarm)
        ical.add_component(event)

        payload = ical.to_ical().decode()
        try:
            cal.save_event(payload)
        except Exception as exc:
            # iCloud accounts usually carry calendars that accept VEVENT but
            # refuse writes — Birthdays, Siri Suggestions, subscribed holiday
            # feeds. If the caller didn't name one and the default turned out to
            # be read-only, try the others before giving up.
            if calendar:
                raise CalendarError(
                    f"Could not write to calendar '{self._name_of(cal)}': {exc}"
                ) from exc

            tried = [self._name_of(cal)]
            for alternative in self._calendars():
                name = self._name_of(alternative)
                if name in tried:
                    continue
                try:
                    alternative.save_event(payload)
                    cal = alternative
                    break
                except Exception:
                    tried.append(name)
            else:
                raise CalendarError(
                    "None of your calendars accepted a new event "
                    f"({', '.join(tried)}). They may all be read-only or shared "
                    "to you rather than owned by this account. Set "
                    "DEFAULT_CALENDAR in .env to one you own."
                ) from exc

        return CalEvent(
            uid=uid,
            summary=summary,
            start=start.isoformat(),
            end=end.isoformat(),
            all_day=False,
            location=location,
            description=description,
            calendar=self._name_of(cal),
        )

    async def update_event(
        self,
        uid: str,
        *,
        summary: str | None = None,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
        location: str | None = None,
        description: str | None = None,
        calendar: str = "",
    ) -> CalEvent:
        return await asyncio.to_thread(
            self._update_event, uid, summary, start, end, location, description, calendar
        )

    def _update_event(
        self, uid, summary, start, end, location, description, calendar
    ) -> CalEvent:
        """Edit an existing event in place, leaving unnamed fields alone.

        Editing rather than delete-and-recreate keeps the uid stable, so
        invitees, alarms and the user's own phone see a changed event instead of
        a cancellation followed by an unfamiliar new one.
        """
        found = self._locate(uid, calendar)
        if found is None:
            raise CalendarError(f"No event with uid {uid} found.")
        cal, stored = found

        component = self._vevent_of(stored)
        if component is None:
            raise CalendarError(f"Event {uid} has no VEVENT to edit.")

        tz = ZoneInfo(self.settings.timezone)
        if summary is not None:
            component["summary"] = summary
        for field_name, value in (("dtstart", start), ("dtend", end)):
            if value is None:
                continue
            if value.tzinfo is None:
                value = value.replace(tzinfo=tz)
            # UTC for the same reason as creation: a TZID with no matching
            # VTIMEZONE is invalid iCalendar and iCloud rejects the whole event.
            component.pop(field_name.upper(), None)
            component.add(field_name, value.astimezone(dt.UTC))
        if location is not None:
            component["location"] = location
        if description is not None:
            component["description"] = description

        # Bumping the sequence is what tells clients this is a revision rather
        # than a conflicting copy of the same event.
        component["sequence"] = int(component.get("sequence", 0)) + 1
        component.pop("DTSTAMP", None)
        component.add("dtstamp", dt.datetime.now(dt.UTC))

        try:
            stored.save()
        except Exception as exc:
            raise CalendarError(f"Could not save changes to '{uid}': {exc}") from exc

        return CalEvent(
            uid=uid,
            summary=str(component.get("SUMMARY", "")),
            start=self._iso(component.get("DTSTART").dt),
            end=self._iso(component.get("DTEND").dt) if component.get("DTEND") else "",
            all_day=False,
            location=str(component.get("LOCATION", "")),
            description=str(component.get("DESCRIPTION", "")),
            calendar=self._name_of(cal),
        )

    def _locate(self, uid: str, calendar: str = ""):
        """Find (calendar, stored event) for a uid, searching every calendar."""
        cals = [self._pick(calendar)] if calendar else self._calendars()
        for cal in cals:
            try:
                event = cal.event_by_uid(uid)
            except Exception:
                continue
            if event is not None:
                return cal, event
        return None

    @staticmethod
    def _vevent_of(stored):
        for component in stored.icalendar_instance.walk("VEVENT"):
            return component
        return None

    async def delete_event(self, uid: str, calendar: str = "") -> bool:
        return await asyncio.to_thread(self._delete_event, uid, calendar)

    def _delete_event(self, uid: str, calendar: str) -> bool:
        cals = [self._pick(calendar)] if calendar else self._calendars()
        for cal in cals:
            try:
                event = cal.event_by_uid(uid)
            except Exception:
                continue
            if event is not None:
                event.delete()
                return True
        raise CalendarError(f"No event with uid {uid} found.")


_calendar: AppleCalendar | None = None


def get_calendar() -> AppleCalendar:
    global _calendar
    if _calendar is None:
        _calendar = AppleCalendar()
    return _calendar

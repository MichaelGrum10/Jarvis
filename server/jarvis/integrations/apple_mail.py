"""iCloud Mail over IMAP (read) and SMTP (send).

Apple exposes iCloud Mail through bog-standard IMAP with an app-specific
password — no scraping, no private API. We fetch headers first and only pull
full bodies when the user asks to expand a specific message, which keeps the
common "summarise my inbox" path fast.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import email
import email.policy
import imaplib
import logging
import re
import smtplib
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

_WS = re.compile(r"[ \t]*\r?\n[ \t]*")
_MULTISPACE = re.compile(r"\s{2,}")


class MailError(RuntimeError):
    pass


@dataclass
class MailSummary:
    uid: str
    subject: str
    sender: str
    sender_email: str
    date: str
    snippet: str
    unread: bool
    has_attachments: bool
    mailbox: str = "INBOX"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MailFull(MailSummary):
    body: str = ""
    to: str = ""
    cc: str = ""


class AppleMail:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _require(self) -> None:
        if not self.settings.icloud_email or not self.settings.icloud_app_password:
            raise MailError(
                "iCloud Mail is not configured. Set ICLOUD_EMAIL and ICLOUD_APP_PASSWORD "
                "(an app-specific password from appleid.apple.com)."
            )

    def _connect(self) -> imaplib.IMAP4_SSL:
        self._require()
        conn = imaplib.IMAP4_SSL(self.settings.imap_host, self.settings.imap_port)
        try:
            conn.login(self.settings.icloud_email, self.settings.icloud_app_password)
        except imaplib.IMAP4.error as exc:
            raise MailError(
                "iCloud rejected the login. Confirm you used an app-specific password."
            ) from exc
        return conn

    # ---- reading ----------------------------------------------------------

    async def recent(
        self, limit: int = 25, mailbox: str = "INBOX", unread_only: bool = False, days: int = 3
    ) -> list[MailSummary]:
        return await asyncio.to_thread(self._recent, limit, mailbox, unread_only, days)

    def _recent(self, limit: int, mailbox: str, unread_only: bool, days: int) -> list[MailSummary]:
        conn = self._connect()
        try:
            status, _ = conn.select(f'"{mailbox}"', readonly=True)
            if status != "OK":
                raise MailError(f"Cannot open mailbox '{mailbox}'.")

            since = (dt.date.today() - dt.timedelta(days=max(days, 1))).strftime("%d-%b-%Y")
            criteria = ["SINCE", since]
            if unread_only:
                criteria.append("UNSEEN")
            status, data = conn.uid("SEARCH", None, *criteria)
            if status != "OK":
                return []
            uids = (data[0] or b"").split()[-limit:]
            if not uids:
                return []

            # BODY.PEEK avoids marking things read just because Jarvis looked.
            fetch_expr = "(FLAGS BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
            status, raw = conn.uid("FETCH", b",".join(uids), fetch_expr)
            if status != "OK":
                return []
            return list(reversed(self._parse_fetch(raw, mailbox)))
        finally:
            self._logout(conn)

    def _parse_fetch(self, raw: list, mailbox: str) -> list[MailSummary]:
        out: list[MailSummary] = []
        for chunk in raw:
            if not isinstance(chunk, tuple) or len(chunk) < 2:
                continue
            meta = chunk[0].decode(errors="replace")
            uid_match = re.search(r"UID (\d+)", meta)
            if not uid_match:
                continue
            headers = email.message_from_bytes(chunk[1], policy=email.policy.default)
            sender_name, sender_email = self._split_sender(str(headers.get("From", "")))
            out.append(
                MailSummary(
                    uid=uid_match.group(1),
                    subject=self._clean(str(headers.get("Subject", "(no subject)"))),
                    sender=sender_name,
                    sender_email=sender_email,
                    date=self._date(str(headers.get("Date", ""))),
                    snippet="",
                    unread="\\Seen" not in meta,
                    has_attachments='"attachment"' in meta.lower(),
                    mailbox=mailbox,
                )
            )
        return out

    async def full(self, uid: str, mailbox: str = "INBOX", max_chars: int = 8000) -> MailFull:
        return await asyncio.to_thread(self._full, uid, mailbox, max_chars)

    def _full(self, uid: str, mailbox: str, max_chars: int) -> MailFull:
        conn = self._connect()
        try:
            conn.select(f'"{mailbox}"', readonly=True)
            status, raw = conn.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
            if status != "OK" or not raw or not isinstance(raw[0], tuple):
                raise MailError(f"Message {uid} not found in {mailbox}.")
            flags = raw[0][0].decode(errors="replace")
            msg = email.message_from_bytes(raw[0][1], policy=email.policy.default)
            sender_name, sender_email = self._split_sender(str(msg.get("From", "")))
            body, attachments = self._extract_body(msg)
            return MailFull(
                uid=uid,
                subject=self._clean(str(msg.get("Subject", "(no subject)"))),
                sender=sender_name,
                sender_email=sender_email,
                date=self._date(str(msg.get("Date", ""))),
                snippet=body[:200],
                unread="\\Seen" not in flags,
                has_attachments=bool(attachments),
                mailbox=mailbox,
                body=body[:max_chars],
                to=self._clean(str(msg.get("To", ""))),
                cc=self._clean(str(msg.get("Cc", ""))),
            )
        finally:
            self._logout(conn)

    async def search(self, query: str, limit: int = 20, mailbox: str = "INBOX") -> list[MailSummary]:
        return await asyncio.to_thread(self._search, query, limit, mailbox)

    def _search(self, query: str, limit: int, mailbox: str) -> list[MailSummary]:
        conn = self._connect()
        try:
            conn.select(f'"{mailbox}"', readonly=True)
            # TEXT searches headers + body server-side; much cheaper than downloading.
            status, data = conn.uid("SEARCH", None, "TEXT", f'"{query}"')
            if status != "OK":
                return []
            uids = (data[0] or b"").split()[-limit:]
            if not uids:
                return []
            status, raw = conn.uid(
                "FETCH",
                b",".join(uids),
                "(FLAGS BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])",
            )
            return list(reversed(self._parse_fetch(raw, mailbox))) if status == "OK" else []
        finally:
            self._logout(conn)

    def _extract_body(self, msg: EmailMessage) -> tuple[str, list[str]]:
        attachments: list[str] = []
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                disp = str(part.get("Content-Disposition", ""))
                if "attachment" in disp:
                    attachments.append(part.get_filename() or "attachment")
                    continue
                if part.get_content_type() == "text/plain" and not body:
                    body = self._decode(part)
            if not body:
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        body = self._html_to_text(self._decode(part))
                        break
        else:
            body = self._decode(msg)
            if msg.get_content_type() == "text/html":
                body = self._html_to_text(body)
        return self._clean(body), attachments

    @staticmethod
    def _decode(part) -> str:
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                return ""
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
        except Exception:
            return ""

    @staticmethod
    def _html_to_text(html: str) -> str:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        return soup.get_text("\n")

    @staticmethod
    def _split_sender(value: str) -> tuple[str, str]:
        name, addr = email.utils.parseaddr(value)
        return (name or addr or "unknown").strip(), (addr or "").strip()

    @staticmethod
    def _date(value: str) -> str:
        try:
            return parsedate_to_datetime(value).isoformat()
        except (TypeError, ValueError):
            return value

    @staticmethod
    def _clean(text: str) -> str:
        text = _WS.sub(" ", text or "")
        return _MULTISPACE.sub(" ", text).strip()

    @staticmethod
    def _logout(conn: imaplib.IMAP4_SSL) -> None:
        try:
            conn.logout()
        except Exception:
            pass

    # ---- sending ----------------------------------------------------------

    async def send(self, to: str, subject: str, body: str, cc: str = "") -> bool:
        return await asyncio.to_thread(self._send, to, subject, body, cc)

    def _send(self, to: str, subject: str, body: str, cc: str) -> bool:
        self._require()
        msg = EmailMessage()
        msg["From"] = self.settings.icloud_email
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port) as server:
            server.starttls()
            server.login(self.settings.icloud_email, self.settings.icloud_app_password)
            server.send_message(msg)
        return True


_mail: AppleMail | None = None


def get_mail() -> AppleMail:
    global _mail
    if _mail is None:
        _mail = AppleMail()
    return _mail

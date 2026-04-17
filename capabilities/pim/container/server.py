"""
Selu PIM iCloud Capability Server

gRPC server implementing email (IMAP/SMTP) and calendar (CalDAV) tools
for iCloud accounts.  Follows the selu.capability.v1.Capability service
interface.
"""

import concurrent.futures
import email
import email.header
import email.mime.application
import email.mime.multipart
import email.mime.text
import email.utils
import imaplib
import json
import logging
import os
import signal
import smtplib
import sys
import threading
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from uuid import uuid4

import grpc
import capability_pb2
import capability_pb2_grpc

# CalDAV imports
import caldav
import icalendar

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("pim")

# ── Default iCloud server configuration ──────────────────────────────────────

DEFAULT_IMAP_SERVER = "imap.mail.me.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_SERVER = "smtp.mail.me.com"
DEFAULT_SMTP_PORT = 587
DEFAULT_CALDAV_URL = "https://caldav.icloud.com"
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

# ── IMAP connection manager ──────────────────────────────────────────────────


class ImapManager:
    """Manages a reusable IMAP connection with automatic reconnection."""

    def __init__(self) -> None:
        self._conn: Optional[imaplib.IMAP4_SSL] = None
        self._lock = threading.Lock()
        self._server: str = DEFAULT_IMAP_SERVER
        self._port: int = DEFAULT_IMAP_PORT
        self._email: str = ""
        self._password: str = ""

    def configure(self, server: str, port: int, email_addr: str, password: str) -> None:
        with self._lock:
            changed = (
                self._server != server
                or self._port != port
                or self._email != email_addr
                or self._password != password
            )
            self._server = server
            self._port = port
            self._email = email_addr
            self._password = password
            if changed and self._conn is not None:
                log.info("IMAP: configuration changed, resetting connection")
                self._close_locked()

    def _connect(self) -> imaplib.IMAP4_SSL:
        """Create a fresh IMAP connection."""
        log.info("IMAP: connecting to %s:%d as %s", self._server, self._port, self._email)
        conn = imaplib.IMAP4_SSL(self._server, self._port)
        conn.login(self._email, self._password)
        log.info("IMAP: connected")
        return conn

    def _close_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def _ensure_connection_locked(self) -> imaplib.IMAP4_SSL:
        if self._conn is not None:
            try:
                self._conn.noop()
                return self._conn
            except Exception as e:
                log.warning("IMAP: existing connection stale (%s), reconnecting", e)
                self._close_locked()

        self._conn = self._connect()
        return self._conn

    def get_connection(self) -> imaplib.IMAP4_SSL:
        """Get a working IMAP connection, reconnecting if needed."""
        with self._lock:
            return self._ensure_connection_locked()

    def run(self, operation_name: str, fn: Callable[[imaplib.IMAP4_SSL], Any]) -> Any:
        """Run an IMAP operation under lock with one reconnect+retry on abort."""
        with self._lock:
            for attempt in range(2):
                conn = self._ensure_connection_locked()
                try:
                    return fn(conn)
                except imaplib.IMAP4.abort as e:
                    log.warning(
                        "IMAP: %s aborted (%s), retry %d/1",
                        operation_name,
                        e,
                        attempt + 1,
                    )
                    self._close_locked()
                    if attempt == 1:
                        raise
                except OSError as e:
                    log.warning(
                        "IMAP: %s socket error (%s), retry %d/1",
                        operation_name,
                        e,
                        attempt + 1,
                    )
                    self._close_locked()
                    if attempt == 1:
                        raise
            raise RuntimeError(f"IMAP operation failed after retry: {operation_name}")

    def close(self) -> None:
        with self._lock:
            self._close_locked()


# ── Helper functions ─────────────────────────────────────────────────────────


def decode_header_value(raw: str) -> str:
    """Decode RFC 2047 encoded header values.

    Uses make_header() + str() to correctly reassemble multi-part
    encoded-words without inserting spurious spaces at encoding
    boundaries.  This matters for languages like German where compound
    words (e.g. "anwendungsspezifisches") may be split across encoded
    parts by the sending MTA.
    """
    if raw is None:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(raw)))
    except Exception:
        # Fallback: manual decode (handles truly malformed headers)
        try:
            parts = email.header.decode_header(raw)
            decoded = []
            for part, charset in parts:
                if isinstance(part, bytes):
                    decoded.append(part.decode(charset or "utf-8", errors="replace"))
                else:
                    decoded.append(part)
            return "".join(decoded)
        except Exception:
            return raw


def parse_date(date_str: str) -> Optional[datetime]:
    """Parse an email date header into a datetime."""
    if not date_str:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        return parsed
    except Exception:
        return None


def imap_date_str(dt: datetime) -> str:
    """Format a datetime for IMAP SINCE/BEFORE criteria."""
    return dt.strftime("%d-%b-%Y")


def get_email_body(msg: email.message.Message) -> tuple[str, str]:
    """Extract plain text and HTML body from an email message."""
    text_body = ""
    html_body = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in content_disposition:
                continue
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    text_body = payload.decode(charset, errors="replace")
            elif content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html_body = payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            content = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_body = content
            else:
                text_body = content

    return text_body, html_body


def get_attachments(msg: email.message.Message) -> list[dict]:
    """Extract attachment metadata from an email message."""
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in content_disposition:
                filename = part.get_filename()
                if filename:
                    filename = decode_header_value(filename)
                size = len(part.get_payload(decode=True) or b"")
                attachments.append({
                    "filename": filename or "unnamed",
                    "content_type": part.get_content_type(),
                    "size_bytes": size,
                })
    return attachments


def email_summary(uid: str, msg: email.message.Message) -> dict:
    """Build a concise email summary dict."""
    text_body, _ = get_email_body(msg)
    snippet = text_body[:200].strip().replace("\n", " ") if text_body else ""
    date = parse_date(msg.get("Date", ""))

    return {
        "id": uid,
        "from": decode_header_value(msg.get("From", "")),
        "to": decode_header_value(msg.get("To", "")),
        "subject": decode_header_value(msg.get("Subject", "")),
        "date": date.isoformat() if date else "",
        "snippet": snippet,
    }


# ── Tool handlers ────────────────────────────────────────────────────────────


def handle_check_email(args: dict, imap: ImapManager) -> str:
    """Fetch emails from a mailbox folder."""
    folder = args.get("folder", "INBOX")
    limit = args.get("limit", 20)
    since_date = args.get("since_date")
    unseen_only = args.get("unseen_only", False)

    log.info("check_email: folder=%s limit=%d since=%s unseen_only=%s",
             folder, limit, since_date, unseen_only)

    try:
        def _run(conn: imaplib.IMAP4_SSL) -> str:
            status, _ = conn.select(folder, readonly=True)
            if status != "OK":
                return json.dumps({"error": f"Could not open folder '{folder}'"})

            # Build IMAP search criteria
            criteria = []
            if unseen_only:
                criteria.append("UNSEEN")
            if since_date:
                try:
                    dt = datetime.fromisoformat(since_date.replace("Z", "+00:00"))
                    criteria.append(f'SINCE {imap_date_str(dt)}')
                except ValueError:
                    pass

            search_str = " ".join(criteria) if criteria else "ALL"
            status, data = conn.search(None, search_str)
            if status != "OK":
                return json.dumps({"error": "Search failed"})
            if data[0] is None:
                raise imaplib.IMAP4.abort("search returned None data")

            msg_ids = data[0].split()
            if not msg_ids:
                return json.dumps({"emails": [], "total": 0})

            # Take the most recent emails (last N)
            msg_ids = msg_ids[-limit:]
            msg_ids.reverse()  # newest first

            emails = []
            for mid in msg_ids:
                uid_status, uid_data = conn.fetch(mid, "(UID RFC822.HEADER FLAGS)")
                if uid_status != "OK" or not uid_data or uid_data[0] is None:
                    continue

                # Extract UID
                uid = mid.decode()
                for item in uid_data:
                    if isinstance(item, tuple) and b"UID" in item[0]:
                        uid_part = item[0].decode()
                        uid_start = uid_part.find("UID ")
                        if uid_start >= 0:
                            uid_str = uid_part[uid_start + 4:].split()[0].rstrip(")")
                            uid = uid_str
                        break

                # Parse headers
                header_data = None
                for item in uid_data:
                    if isinstance(item, tuple) and len(item) > 1:
                        header_data = item[1]
                        break
                if header_data is None:
                    continue

                msg = email.message_from_bytes(header_data)
                summary = email_summary(uid, msg)

                # Check flags for read status
                for item in uid_data:
                    if isinstance(item, bytes) and b"FLAGS" in item:
                        summary["is_read"] = b"\\Seen" in item
                        break

                # Filter by since_date more precisely (IMAP SINCE is date-only)
                if since_date:
                    try:
                        filter_dt = datetime.fromisoformat(since_date.replace("Z", "+00:00"))
                        email_dt = parse_date(msg.get("Date", ""))
                        if email_dt and email_dt.astimezone(timezone.utc) < filter_dt:
                            continue
                    except ValueError:
                        pass

                emails.append(summary)

            log.info("check_email: returning %d emails", len(emails))
            return json.dumps({"emails": emails, "total": len(emails)})

        return imap.run("check_email", _run)
    except Exception as e:
        log.error("check_email failed: %s\n%s", e, traceback.format_exc())
        return json.dumps({"error": f"Failed to check email: {str(e)}"})


def handle_get_email(args: dict, imap: ImapManager) -> str:
    """Read the full content of a specific email."""
    email_id = args.get("email_id")
    if not email_id:
        return json.dumps({"error": "email_id is required"})

    log.info("get_email: id=%s", email_id)

    try:
        def _run(conn: imaplib.IMAP4_SSL) -> str:
            conn.select("INBOX", readonly=True)

            def extract_raw_email(fetch_data: list) -> Optional[bytes]:
                """Handle both tuple-based and bytes-only IMAP FETCH payload shapes."""
                for i, item in enumerate(fetch_data):
                    if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
                        return item[1]
                    if isinstance(item, bytes):
                        has_literal_marker = (
                            b"RFC822" in item
                            or b"BODY[]" in item
                            or b"BODY[TEXT]" in item
                            or b"BODY.PEEK[]" in item
                        )
                        if has_literal_marker and b"{" in item and i + 1 < len(fetch_data):
                            next_item = fetch_data[i + 1]
                            if isinstance(next_item, bytes):
                                return next_item
                return None

            fetch_attempts = [
                ("uid_fetch_rfc822", lambda: conn.uid("FETCH", email_id, "(RFC822)")),
                ("uid_fetch_body_peek", lambda: conn.uid("FETCH", email_id, "(BODY.PEEK[])")),
            ]

            raw_email = None
            last_data = None
            for attempt_name, fetch_fn in fetch_attempts:
                status, data = fetch_fn()
                last_data = data
                if status != "OK" or not data or data[0] is None:
                    continue
                raw_email = extract_raw_email(data)
                if raw_email is not None:
                    break
                log.warning(
                    "get_email: %s returned unparseable structure for id=%s",
                    attempt_name,
                    email_id,
                )

            if raw_email is None:
                # Fallback: resolve UID -> message sequence number, then FETCH by sequence.
                seq_status, seq_data = conn.uid("SEARCH", None, f"UID {email_id}")
                if seq_status == "OK" and seq_data and seq_data[0]:
                    seq_ids = seq_data[0].split()
                    if seq_ids:
                        seq = seq_ids[-1]
                        status, data = conn.fetch(seq, "(RFC822)")
                        last_data = data
                        if status == "OK" and data and data[0] is not None:
                            raw_email = extract_raw_email(data)

            if raw_email is None:
                log.error("get_email: unexpected IMAP response structure for id=%s: %s",
                          email_id, [(type(item).__name__, item[:100] if isinstance(item, bytes) else item) for item in (last_data or [])])
                return json.dumps({"error": "Could not parse email data"})

            msg = email.message_from_bytes(raw_email)
            text_body, html_body = get_email_body(msg)
            attachments = get_attachments(msg)
            date = parse_date(msg.get("Date", ""))
            message_id = msg.get("Message-ID", "")

            result = {
                "id": email_id,
                "from": decode_header_value(msg.get("From", "")),
                "to": decode_header_value(msg.get("To", "")),
                "cc": decode_header_value(msg.get("Cc", "")),
                "subject": decode_header_value(msg.get("Subject", "")),
                "date": date.isoformat() if date else "",
                "message_id": message_id,
                "body_text": text_body,
                "body_html": html_body[:2000] if html_body else "",
                "attachments": attachments,
            }
            log.info("get_email: success, subject=%s", result["subject"][:60])
            return json.dumps(result)

        return imap.run("get_email", _run)
    except Exception as e:
        log.error("get_email failed: %s\n%s", e, traceback.format_exc())
        return json.dumps({"error": f"Failed to read email: {str(e)}"})


def handle_send_email(args: dict, config: dict, input_artifacts: dict[str, dict]) -> str:
    """Send an email via SMTP."""
    to_addr = args.get("to")
    subject = args.get("subject")
    body = args.get("body")
    cc = args.get("cc")
    reply_to_id = args.get("reply_to_email_id")
    attachments = args.get("attachments") or []

    if not to_addr or not subject or not body:
        return json.dumps({"error": "to, subject, and body are required"})

    log.info("send_email: to=%s subject=%s", to_addr, subject[:60] if subject else "")

    email_addr = config.get("EMAIL_ADDRESS", "")
    password = config.get("APP_PASSWORD", "")
    smtp_server = config.get("SMTP_SERVER", DEFAULT_SMTP_SERVER)
    smtp_port = int(config.get("SMTP_PORT", str(DEFAULT_SMTP_PORT)))

    try:
        msg = email.mime.multipart.MIMEMultipart()
        msg["From"] = email_addr
        msg["To"] = to_addr
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        if reply_to_id:
            msg["In-Reply-To"] = reply_to_id
            msg["References"] = reply_to_id

        msg.attach(email.mime.text.MIMEText(body, "plain", "utf-8"))

        attached = []
        for idx, attachment in enumerate(attachments):
            if not isinstance(attachment, dict):
                return json.dumps({"error": f"attachments[{idx}] must be an object"})

            capability_artifact_id = attachment.get("capability_artifact_id")
            if not isinstance(capability_artifact_id, str) or not capability_artifact_id.strip():
                return json.dumps({
                    "error": f"attachments[{idx}] is missing capability_artifact_id",
                })
            artifact = input_artifacts.pop(capability_artifact_id, None)
            if artifact is None:
                return json.dumps({
                    "error": (
                        f"attachments[{idx}] references unknown capability_artifact_id "
                        f"'{capability_artifact_id}'"
                    ),
                })

            filename = attachment.get("filename") or artifact.get("filename") or f"attachment-{idx + 1}.bin"
            mime_type = attachment.get("mime_type") or artifact.get("mime_type") or "application/octet-stream"
            payload = artifact.get("data") or b""

            if len(payload) > MAX_ATTACHMENT_BYTES:
                return json.dumps({
                    "error": (
                        f"attachments[{idx}] exceeds size limit "
                        f"({MAX_ATTACHMENT_BYTES} bytes)"
                    ),
                })

            main_type, _, sub_type = mime_type.partition("/")
            if main_type != "application" or not sub_type:
                main_type = "application"
                sub_type = "octet-stream"
            part = email.mime.application.MIMEApplication(payload, _subtype=sub_type)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)
            attached.append({"filename": filename, "mime_type": mime_type, "size_bytes": len(payload)})

        # Build recipient list
        recipients = [addr.strip() for addr in to_addr.split(",")]
        if cc:
            recipients.extend([addr.strip() for addr in cc.split(",")])

        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(email_addr, password)
            server.send_message(msg, from_addr=email_addr, to_addrs=recipients)

        log.info("send_email: sent to %s", to_addr)
        return json.dumps({
            "ok": True,
            "to": to_addr,
            "subject": subject,
            "attachments": attached,
        })

    except Exception as e:
        log.error("send_email failed: %s\n%s", e, traceback.format_exc())
        return json.dumps({"error": f"Failed to send email: {str(e)}"})


def handle_search_emails(args: dict, imap: ImapManager) -> str:
    """Search emails using IMAP SEARCH criteria."""
    query = args.get("query")
    from_addr = args.get("from_addr")
    subject_filter = args.get("subject")
    since_date = args.get("since_date")
    before_date = args.get("before_date")
    folder = args.get("folder", "INBOX")
    limit = args.get("limit", 20)

    log.info("search_emails: query=%s from=%s subject=%s folder=%s",
             query, from_addr, subject_filter, folder)

    try:
        def _run(conn: imaplib.IMAP4_SSL) -> str:
            status, _ = conn.select(folder, readonly=True)
            if status != "OK":
                return json.dumps({"error": f"Could not open folder '{folder}'"})

            # Build search criteria
            criteria = []
            if query:
                criteria.append(f'TEXT "{query}"')
            if from_addr:
                criteria.append(f'FROM "{from_addr}"')
            if subject_filter:
                criteria.append(f'SUBJECT "{subject_filter}"')
            if since_date:
                try:
                    dt = datetime.fromisoformat(since_date.replace("Z", "+00:00"))
                    criteria.append(f'SINCE {imap_date_str(dt)}')
                except ValueError:
                    pass
            if before_date:
                try:
                    dt = datetime.fromisoformat(before_date.replace("Z", "+00:00"))
                    criteria.append(f'BEFORE {imap_date_str(dt)}')
                except ValueError:
                    pass

            search_str = " ".join(criteria) if criteria else "ALL"
            status, data = conn.search(None, search_str)
            if status != "OK":
                return json.dumps({"error": "Search failed"})
            if data[0] is None:
                raise imaplib.IMAP4.abort("search returned None data")

            msg_ids = data[0].split()
            if not msg_ids:
                return json.dumps({"emails": [], "total": 0})

            msg_ids = msg_ids[-limit:]
            msg_ids.reverse()

            emails = []
            for mid in msg_ids:
                uid_status, uid_data = conn.fetch(mid, "(UID RFC822.HEADER)")
                if uid_status != "OK" or not uid_data or uid_data[0] is None:
                    continue

                uid = mid.decode()
                for item in uid_data:
                    if isinstance(item, tuple) and b"UID" in item[0]:
                        uid_part = item[0].decode()
                        uid_start = uid_part.find("UID ")
                        if uid_start >= 0:
                            uid_str = uid_part[uid_start + 4:].split()[0].rstrip(")")
                            uid = uid_str
                        break

                header_data = None
                for item in uid_data:
                    if isinstance(item, tuple) and len(item) > 1:
                        header_data = item[1]
                        break
                if header_data is None:
                    continue

                msg = email.message_from_bytes(header_data)
                emails.append(email_summary(uid, msg))

            return json.dumps({"emails": emails, "total": len(emails)})

        return imap.run("search_emails", _run)
    except Exception as e:
        log.error("search_emails failed: %s\n%s", e, traceback.format_exc())
        return json.dumps({"error": f"Failed to search emails: {str(e)}"})


def handle_list_calendars(args: dict, config: dict) -> str:
    """List available CalDAV calendars with their names and URLs."""
    caldav_url = config.get("CALDAV_URL", DEFAULT_CALDAV_URL)
    email_addr = config.get("EMAIL_ADDRESS", "")
    password = config.get("APP_PASSWORD", "")

    log.info("list_calendars: url=%s user=%s", caldav_url, email_addr)

    try:
        client = caldav.DAVClient(
            url=caldav_url,
            username=email_addr,
            password=password,
        )
        principal = client.principal()
        calendars = principal.calendars()
        log.info("list_calendars: found %d calendars", len(calendars))

        result = []
        for cal in calendars:
            result.append({
                "name": cal.name or "",
                "url": str(cal.url),
            })

        return json.dumps({"calendars": result, "total": len(result)})

    except Exception as e:
        log.error("list_calendars failed: %s\n%s", e, traceback.format_exc())
        return json.dumps({"error": f"Failed to list calendars: {str(e)}"})


def handle_check_calendar(args: dict, config: dict) -> str:
    """Get upcoming calendar events via CalDAV."""
    days_ahead = args.get("days_ahead", 7)
    calendar_name = args.get("calendar_name")

    caldav_url = config.get("CALDAV_URL", DEFAULT_CALDAV_URL)
    email_addr = config.get("EMAIL_ADDRESS", "")
    password = config.get("APP_PASSWORD", "")

    log.info("check_calendar: url=%s user=%s days_ahead=%d calendar=%s",
             caldav_url, email_addr, days_ahead, calendar_name)

    try:
        client = caldav.DAVClient(
            url=caldav_url,
            username=email_addr,
            password=password,
        )
        log.debug("check_calendar: connecting to CalDAV principal")
        principal = client.principal()
        calendars = principal.calendars()
        log.info("check_calendar: found %d calendars: %s",
                 len(calendars), [c.name for c in calendars])

        if not calendars:
            return json.dumps({"events": [], "message": "No calendars found"})

        # Filter by calendar name if specified
        if calendar_name:
            calendars = [c for c in calendars if c.name and calendar_name.lower() in c.name.lower()]
            if not calendars:
                return json.dumps({"error": f"Calendar '{calendar_name}' not found"})

        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days_ahead)

        events = []
        for cal in calendars:
            try:
                log.debug("check_calendar: searching %s from %s to %s",
                          cal.name, now.isoformat(), end.isoformat())
                results = cal.date_search(start=now, end=end, expand=True)
                log.debug("check_calendar: %s returned %d events", cal.name, len(results))
            except Exception as e:
                log.warning("check_calendar: date_search failed for %s: %s\n%s",
                            cal.name, e, traceback.format_exc())
                continue

            for event in results:
                try:
                    ical = icalendar.Calendar.from_ical(event.data)
                    for component in ical.walk():
                        if component.name == "VEVENT":
                            dtstart = component.get("DTSTART")
                            dtend = component.get("DTEND")
                            summary = str(component.get("SUMMARY", ""))
                            location = str(component.get("LOCATION", ""))
                            description = str(component.get("DESCRIPTION", ""))

                            start_dt = dtstart.dt if dtstart else None
                            end_dt = dtend.dt if dtend else None

                            # Determine if all-day event
                            all_day = not isinstance(start_dt, datetime)

                            event_data = {
                                "summary": summary,
                                "start": start_dt.isoformat() if start_dt else "",
                                "end": end_dt.isoformat() if end_dt else "",
                                "location": location,
                                "description": description[:500] if description else "",
                                "calendar": cal.name or "Unknown",
                                "all_day": all_day,
                            }
                            events.append(event_data)
                except Exception as e:
                    log.warning("check_calendar: failed to parse event: %s", e)
                    continue

        # Sort by start time
        events.sort(key=lambda e: e.get("start", ""))

        log.info("check_calendar: returning %d events", len(events))
        return json.dumps({"events": events, "total": len(events)})

    except Exception as e:
        log.error("check_calendar failed: %s\n%s", e, traceback.format_exc())
        return json.dumps({"error": f"Failed to check calendar: {str(e)}"})


def handle_create_calendar_event(args: dict, config: dict) -> str:
    """Create a new calendar event via CalDAV."""
    summary = args.get("summary")
    start = args.get("start")
    end = args.get("end")
    location = args.get("location", "")
    description = args.get("description", "")
    calendar_name = args.get("calendar_name")
    all_day = args.get("all_day", False)

    if not summary or not start or not end or not calendar_name:
        return json.dumps({"error": "summary, start, end, and calendar_name are required. "
                                     "Use list_calendars to discover available calendar names."})

    caldav_url = config.get("CALDAV_URL", DEFAULT_CALDAV_URL)
    email_addr = config.get("EMAIL_ADDRESS", "")
    password = config.get("APP_PASSWORD", "")

    log.info("create_calendar_event: url=%s user=%s summary=%s start=%s end=%s all_day=%s calendar=%s",
             caldav_url, email_addr, summary, start, end, all_day, calendar_name)

    try:
        client = caldav.DAVClient(
            url=caldav_url,
            username=email_addr,
            password=password,
        )
        log.debug("create_calendar_event: connecting to CalDAV principal")
        principal = client.principal()
        calendars = principal.calendars()
        log.info("create_calendar_event: found %d calendars: %s",
                 len(calendars), [c.name for c in calendars])

        if not calendars:
            return json.dumps({"error": "No calendars found"})

        # Select target calendar (calendar_name is required)
        # Prefer exact name match over substring match
        exact = [c for c in calendars if c.name and c.name.lower() == calendar_name.lower()]
        partial = [c for c in calendars if c.name and calendar_name.lower() in c.name.lower()
                   and c.name.lower() != calendar_name.lower()]
        if exact:
            if len(exact) > 1:
                log.warning("create_calendar_event: %d calendars with exact name '%s', "
                            "using first (url=%s). Others: %s",
                            len(exact), calendar_name, exact[0].url,
                            [str(c.url) for c in exact[1:]])
            target_cal = exact[0]
        elif partial:
            log.info("create_calendar_event: no exact match for '%s', "
                     "using partial match '%s'", calendar_name, partial[0].name)
            target_cal = partial[0]
        else:
            available = [c.name for c in calendars]
            return json.dumps({"error": f"Calendar '{calendar_name}' not found. "
                                         f"Available calendars: {available}"})

        log.info("create_calendar_event: target calendar=%s url=%s",
                 target_cal.name, target_cal.url)

        # Build iCalendar event
        cal = icalendar.Calendar()
        cal.add("prodid", "-//Selu PIM//EN")
        cal.add("version", "2.0")
        cal.add("calscale", "GREGORIAN")

        event = icalendar.Event()
        event.add("summary", summary)

        if all_day:
            # User-facing end date is inclusive; iCalendar DTEND is exclusive.
            start_date = datetime.fromisoformat(start.replace("Z", "+00:00")).date()
            end_date_inclusive = datetime.fromisoformat(end.replace("Z", "+00:00")).date()
            if end_date_inclusive < start_date:
                return json.dumps({"error": "For all-day events, end date must be on or after start date"})
            end_date_exclusive = end_date_inclusive + timedelta(days=1)
            event.add("dtstart", start_date)
            event.add("dtend", end_date_exclusive)
        else:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if end_dt < start_dt:
                return json.dumps({"error": "End datetime must be on or after start datetime"})
            # Keep naive datetimes as floating local times to avoid UTC shifts.
            event.add("dtstart", start_dt)
            event.add("dtend", end_dt)

        if location:
            event.add("location", location)
        if description:
            event.add("description", description)

        event.add("dtstamp", datetime.now(timezone.utc))
        import uuid as _uuid
        event.add("uid", str(_uuid.uuid4()))

        cal.add_component(event)

        ical_data = cal.to_ical().decode("utf-8")
        log.debug("create_calendar_event: iCalendar data:\n%s", ical_data)

        # Save to CalDAV
        log.info("create_calendar_event: saving event to calendar=%s url=%s",
                 target_cal.name, target_cal.url)
        try:
            result = target_cal.save_event(ical_data)
            log.info("create_calendar_event: save_event returned url=%s",
                     getattr(result, 'url', 'N/A'))
        except Exception as save_err:
            # Extract HTTP details if available (caldav library DAVError)
            err_detail = str(save_err)
            for attr in ('reason', 'errmsg'):
                val = getattr(save_err, attr, None)
                if val:
                    err_detail = f"{err_detail} [{attr}={val}]"
            resp = getattr(save_err, 'response', None)
            if resp is not None:
                err_detail = (f"{err_detail} (HTTP {getattr(resp, 'status', '?')} "
                              f"{getattr(resp, 'reason', '')})")
                log.error("create_calendar_event: server response body: %.500s",
                          getattr(resp, 'text', getattr(resp, 'raw', '')))
            log.error("create_calendar_event: save_event failed for calendar=%s: %s\n%s",
                      target_cal.name, err_detail, traceback.format_exc())
            return json.dumps({"error": f"Failed to save event to '{target_cal.name}': {err_detail}"})

        log.info("create_calendar_event: success")
        return json.dumps({
            "ok": True,
            "summary": summary,
            "start": start,
            "end": end,
            "calendar": target_cal.name or "Default",
        })

    except Exception as e:
        log.error("create_calendar_event failed: %s\n%s", e, traceback.format_exc())
        return json.dumps({"error": f"Failed to create event: {str(e)}"})


# ── Tool dispatch ────────────────────────────────────────────────────────────

# Mapping from tool name to primary parameter (for malformed args fallback)
TOOL_PRIMARY_PARAM: dict[str, str] = {
    "check_email": "folder",
    "get_email": "email_id",
    "search_emails": "query",
    "check_calendar": "days_ahead",
    "create_calendar_event": "summary",
}


# ── gRPC Servicer ────────────────────────────────────────────────────────────


class PimCapabilityServicer(capability_pb2_grpc.CapabilityServicer):
    """gRPC server implementing the Selu Capability interface for PIM."""

    def __init__(self) -> None:
        self.imap = ImapManager()
        self._input_artifacts: dict[str, dict] = {}

    def Healthcheck(self, request, context):
        return capability_pb2.HealthResponse(ready=True)

    def Invoke(self, request, context):
        tool_name = request.tool_name
        cap_id = request.capability_id or "pim"

        log.info("Invoke: tool=%s capability=%s", tool_name, cap_id)

        # Parse arguments
        try:
            args = json.loads(request.args_json) if request.args_json else {}
        except json.JSONDecodeError:
            # Handle bare string args (Bedrock streaming parser edge case)
            raw = request.args_json.decode("utf-8", errors="replace") if isinstance(request.args_json, bytes) else str(request.args_json)
            primary = TOOL_PRIMARY_PARAM.get(tool_name)
            if primary:
                args = {primary: raw}
            else:
                args = {}
            log.warning("Invoke: malformed JSON args for %s, wrapped as: %s", tool_name, args)

        if isinstance(args, str):
            primary = TOOL_PRIMARY_PARAM.get(tool_name)
            if primary:
                args = {primary: args}
            else:
                args = {}

        log.debug("Invoke: args=%s", json.dumps(args, default=str)[:500])

        # Parse credentials from config_json
        config: dict[str, str] = {}
        try:
            if request.config_json:
                config = json.loads(request.config_json)
        except json.JSONDecodeError:
            log.warning("Invoke: failed to parse config_json")

        # Log which config keys are present (not values — those are secrets)
        log.debug("Invoke: config keys=%s", list(config.keys()))

        # Inject credentials as temporary env vars for consistency,
        # but also pass config dict directly to handlers that need it.
        env_backup: dict[str, Optional[str]] = {}
        for key, value in config.items():
            env_backup[key] = os.environ.get(key)
            os.environ[key] = value

        try:
            # Configure IMAP with credentials
            email_addr = config.get("EMAIL_ADDRESS", "")
            password = config.get("APP_PASSWORD", "")
            imap_server = config.get("IMAP_SERVER", DEFAULT_IMAP_SERVER)
            imap_port = int(config.get("IMAP_PORT", str(DEFAULT_IMAP_PORT)))

            if email_addr and password:
                self.imap.configure(imap_server, imap_port, email_addr, password)

            # Dispatch to handler
            if tool_name == "check_email":
                result = handle_check_email(args, self.imap)
            elif tool_name == "get_email":
                result = handle_get_email(args, self.imap)
            elif tool_name == "send_email":
                result = handle_send_email(args, config, self._input_artifacts)
            elif tool_name == "search_emails":
                result = handle_search_emails(args, self.imap)
            elif tool_name == "list_calendars":
                result = handle_list_calendars(args, config)
            elif tool_name == "check_calendar":
                result = handle_check_calendar(args, config)
            elif tool_name == "create_calendar_event":
                result = handle_create_calendar_event(args, config)
            else:
                log.warning("Invoke: unknown tool %s", tool_name)
                result = json.dumps({"error": f"Unknown tool: {tool_name}"})

            log.debug("Invoke: result=%s", result[:500] if len(result) > 500 else result)

            return capability_pb2.InvokeResponse(
                result_json=result.encode("utf-8")
            )

        except Exception as e:
            log.error("Invoke: unhandled exception in %s: %s\n%s",
                      tool_name, e, traceback.format_exc())
            return capability_pb2.InvokeResponse(
                error=f"Tool '{tool_name}' failed: {str(e)}"
            )
        finally:
            # Restore environment
            for key, original in env_backup.items():
                if original is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = original

    def StreamInvoke(self, request, context):
        """Wrap Invoke as a single-chunk stream (PIM tools are synchronous)."""
        response = self.Invoke(request, context)
        if response.error:
            yield capability_pb2.InvokeChunk(error=response.error, done=True)
        else:
            yield capability_pb2.InvokeChunk(data=response.result_json, done=True)

    def UploadInputArtifact(self, request_iterator, context):
        data = bytearray()
        filename = ""
        mime_type = ""
        for chunk in request_iterator:
            if not filename and chunk.filename:
                filename = chunk.filename
            if not mime_type and chunk.mime_type:
                mime_type = chunk.mime_type
            if chunk.data:
                data.extend(chunk.data)
                if len(data) > MAX_ATTACHMENT_BYTES:
                    return capability_pb2.UploadInputArtifactResponse(
                        error=(
                            "Input artifact exceeds size limit "
                            f"({MAX_ATTACHMENT_BYTES} bytes)"
                        )
                    )

        capability_artifact_id = str(uuid4())
        self._input_artifacts[capability_artifact_id] = {
            "filename": filename or "attachment.bin",
            "mime_type": mime_type or "application/octet-stream",
            "data": bytes(data),
        }
        return capability_pb2.UploadInputArtifactResponse(
            capability_artifact_id=capability_artifact_id,
            size_bytes=len(data),
        )

    def DownloadOutputArtifact(self, request, context):
        yield capability_pb2.ArtifactChunk(
            error="DownloadOutputArtifact is not supported by pim capability",
            done=True,
        )


# ── Server entry point ───────────────────────────────────────────────────────


def serve() -> None:
    port = os.environ.get("PORT", "50051")
    server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=4))
    servicer = PimCapabilityServicer()
    capability_pb2_grpc.add_CapabilityServicer_to_server(servicer, server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    print(f"PIM capability server listening on port {port}", flush=True)

    # Graceful shutdown
    stop_event = threading.Event()

    def shutdown(signum, frame):
        print(f"Received signal {signum}, shutting down...", flush=True)
        stop_event.set()
        server.stop(grace=5)
        servicer.imap.close()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    stop_event.wait()


if __name__ == "__main__":
    serve()

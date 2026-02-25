from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

CAIRO_TZ = ZoneInfo("Africa/Cairo")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
]


@dataclass
class EmailMessage:
    message_id:   str
    thread_id:    str
    sender:       str
    sender_email: str
    sender_name:  str
    subject:      str
    body_plain:   str
    received_at:  str


@dataclass
class ActionResult:
    message_id:        str
    action_taken:      str
    reply_sent:        bool = False
    meeting_created:   bool = False
    meet_url:          str  = ""
    calendar_event_id: str  = ""
    sheets_row:        int  = 0
    slack_notified:    bool = False
    error:             Optional[str] = None


def _get_google_credentials(credentials_path: str, token_path: str) -> Credentials:
    creds: Optional[Credentials] = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return creds


def _extract_email_address(raw: str) -> str:
    match = re.search(r"<([^>]+)>", raw)
    return match.group(1).strip() if match else raw.strip()


def _extract_display_name(raw: str) -> str:
    match = re.match(r"^(.+?)\s*<", raw)
    if match:
        return match.group(1).strip().strip('"')
    return _extract_email_address(raw).split("@")[0].capitalize()


def _now_cairo() -> datetime:
    return datetime.now(tz=CAIRO_TZ)


def _iso_now() -> str:
    return _now_cairo().isoformat()


class EmailResponder:
    def __init__(
        self,
        credentials_path: Optional[str] = None,
        token_path:        Optional[str] = None,
        sheet_id:          Optional[str] = None,
        sheet_tab:         Optional[str] = None,
        calendar_id:       Optional[str] = None,
        slack_webhook:     Optional[str] = None,
        slack_channel:     Optional[str] = None,
    ):
        self.credentials_path = credentials_path or os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
        self.token_path       = token_path        or os.getenv("GOOGLE_TOKEN_PATH",       "token.json")
        self.sheet_id         = sheet_id          or os.getenv("AUDIT_SHEET_ID",          "")
        self.sheet_tab        = sheet_tab         or os.getenv("AUDIT_SHEET_TAB",         "email_audit_log")
        self.calendar_id      = calendar_id       or os.getenv("CALENDAR_ID",             "primary")
        self.slack_webhook    = slack_webhook     or os.getenv("SLACK_WEBHOOK_URL",       "")
        self.slack_channel    = slack_channel     or os.getenv("SLACK_NOTIFY_CHANNEL",    "#email-automation")
        self.processed_label  = os.getenv("GMAIL_PROCESSED_LABEL", "")
        self.review_label     = os.getenv("GMAIL_REVIEW_LABEL",    "")
        self._creds:    Optional[Credentials] = None
        self._gmail:    Optional[object]      = None
        self._calendar: Optional[object]      = None
        self._sheets:   Optional[object]      = None

    def _get_creds(self) -> Credentials:
        if self._creds is None or not self._creds.valid:
            self._creds = _get_google_credentials(self.credentials_path, self.token_path)
        return self._creds

    def _gmail_client(self):
        if self._gmail is None:
            self._gmail = build("gmail", "v1", credentials=self._get_creds())
        return self._gmail

    def _calendar_client(self):
        if self._calendar is None:
            self._calendar = build("calendar", "v3", credentials=self._get_creds())
        return self._calendar

    def _sheets_client(self):
        if self._sheets is None:
            self._sheets = build("sheets", "v4", credentials=self._get_creds())
        return self._sheets

    def send_reply(self, email: EmailMessage, reply_body: str, subject_prefix: str = "Re: ") -> bool:
        subject = email.subject if email.subject.lower().startswith("re:") else f"{subject_prefix}{email.subject}"
        msg = MIMEMultipart("alternative")
        msg["To"]          = email.sender
        msg["Subject"]     = subject
        msg["In-Reply-To"] = email.message_id
        msg["References"]  = email.message_id
        msg.attach(MIMEText(reply_body, "plain"))
        raw_bytes = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        try:
            sent = (
                self._gmail_client()
                .users().messages()
                .send(userId="me", body={"raw": raw_bytes, "threadId": email.thread_id})
                .execute()
            )
            logger.info("Reply sent | to=%s gmail_id=%s", email.sender_email, sent.get("id"))
            if self.processed_label:
                self._apply_gmail_label(email.message_id, self.processed_label)
            return True
        except HttpError as exc:
            logger.error("Failed to send reply to %s: %s", email.sender_email, exc)
            return False

    def create_meeting(
        self,
        email:            EmailMessage,
        meeting_title:    Optional[str] = None,
        duration_minutes: int = 45,
        days_from_now:    int = 2,
        start_hour:       int = 14,
    ) -> tuple[bool, str, str]:
        title    = meeting_title or f"Meeting: {email.subject[:60]}"
        start_dt = _now_cairo().replace(hour=start_hour, minute=0, second=0, microsecond=0) + timedelta(days=days_from_now)
        end_dt   = start_dt + timedelta(minutes=duration_minutes)
        event_body = {
            "summary": title,
            "description": (
                f"Scheduled via AI Email Concierge.\n\n"
                f"From: {email.sender}\nSubject: {email.subject}\nReceived: {email.received_at}"
            ),
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "Africa/Cairo"},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "Africa/Cairo"},
            "attendees": [{"email": email.sender_email}],
            "conferenceData": {
                "createRequest": {
                    "requestId": f"concierge-{email.message_id[:16]}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "email",  "minutes": 1440},
                    {"method": "popup",  "minutes": 15},
                ],
            },
        }
        try:
            event = (
                self._calendar_client()
                .events()
                .insert(calendarId=self.calendar_id, body=event_body,
                        conferenceDataVersion=1, sendUpdates="all")
                .execute()
            )
            meet_url = event.get("hangoutLink", "")
            event_id = event.get("id", "")
            logger.info("Calendar event created | title=%r meet=%s", title, meet_url)
            return True, meet_url, event_id
        except HttpError as exc:
            logger.error("Failed to create calendar event: %s", exc)
            return False, "", ""

    def log_to_sheets(
        self,
        email:             EmailMessage,
        intent_label:      str,
        confidence_score:  float,
        action_taken:      str,
        meet_url:          str  = "",
        review_pending:    bool = False,
        calendar_event_id: str  = "",
    ) -> int:
        if not self.sheet_id:
            logger.warning("AUDIT_SHEET_ID not configured — skipping Sheets log")
            return 0
        row = [
            _iso_now(),
            email.sender_email,
            email.subject,
            intent_label,
            round(confidence_score, 4),
            action_taken,
            meet_url,
            "TRUE" if review_pending else "FALSE",
            email.message_id,
            calendar_event_id,
        ]
        try:
            result = (
                self._sheets_client()
                .spreadsheets().values()
                .append(
                    spreadsheetId=self.sheet_id,
                    range=f"{self.sheet_tab}!A:J",
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    body={"values": [row]},
                )
                .execute()
            )
            updated_range = result.get("updates", {}).get("updatedRange", "")
            logger.info("Audit row written | range=%s", updated_range)
            row_match = re.search(r":J(\d+)", updated_range)
            return int(row_match.group(1)) if row_match else 0
        except HttpError as exc:
            logger.error("Failed to write audit row: %s", exc)
            return 0

    def ensure_sheet_headers(self) -> None:
        headers = [
            "processed_at", "sender_address", "email_subject", "intent_label",
            "confidence_score", "action_dispatched", "meet_url",
            "review_pending", "gmail_message_id", "calendar_event_id",
        ]
        try:
            existing = (
                self._sheets_client()
                .spreadsheets().values()
                .get(spreadsheetId=self.sheet_id, range=f"{self.sheet_tab}!A1:J1")
                .execute()
            )
            if existing.get("values"):
                return
            self._sheets_client().spreadsheets().values().update(
                spreadsheetId=self.sheet_id,
                range=f"{self.sheet_tab}!A1",
                valueInputOption="USER_ENTERED",
                body={"values": [headers]},
            ).execute()
            logger.info("Sheet headers written.")
        except HttpError as exc:
            logger.error("Failed to write sheet headers: %s", exc)

    def notify_slack(
        self,
        email:             EmailMessage,
        intent_label:      str,
        confidence_score:  float,
        action_taken:      str,
        routing_decision:  str,
        meet_url:          str  = "",
        review_pending:    bool = False,
        latency_ms:        int  = 0,
    ) -> bool:
        if not self.slack_webhook:
            return False
        status_emoji = {
            "AUTO_ACT":    "✅",
            "ACT_FLAGGED": "⚠️",
            "MANUAL_ONLY": "🔍",
            "SILENT_LOG":  "🔕",
        }.get(routing_decision, "📧")
        review_tag = " `REVIEW FLAGGED`" if review_pending else ""
        meet_line  = f"\n>  📹 *Meet:* {meet_url}" if meet_url else ""
        text = (
            f"{status_emoji} *Email Concierge — {action_taken}*{review_tag}\n"
            f">  *From:* {email.sender_email}\n"
            f">  *Subject:* {email.subject}\n"
            f">  *Intent:* `{intent_label}` — confidence `{confidence_score:.0%}`\n"
            f">  *Routing:* `{routing_decision}`"
            f"{meet_line}\n"
            f">  *Latency:* {latency_ms}ms"
        )
        try:
            resp = requests.post(
                self.slack_webhook,
                json={"channel": self.slack_channel, "text": text, "unfurl_links": False},
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            logger.warning("Slack notification failed: %s", exc)
            return False

    def _apply_gmail_label(self, message_id: str, label_name: str) -> None:
        try:
            label_id = self._get_or_create_label(label_name)
            if label_id:
                self._gmail_client().users().messages().modify(
                    userId="me", id=message_id, body={"addLabelIds": [label_id]}
                ).execute()
        except HttpError as exc:
            logger.warning("Could not apply Gmail label %r: %s", label_name, exc)

    def _get_or_create_label(self, label_name: str) -> Optional[str]:
        try:
            all_labels = self._gmail_client().users().labels().list(userId="me").execute()
            for lbl in all_labels.get("labels", []):
                if lbl["name"] == label_name:
                    return lbl["id"]
            created = self._gmail_client().users().labels().create(
                userId="me", body={"name": label_name}
            ).execute()
            return created["id"]
        except HttpError as exc:
            logger.error("Label lookup/create failed for %r: %s", label_name, exc)
            return None

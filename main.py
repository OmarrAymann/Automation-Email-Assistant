from __future__ import annotations

import argparse
import base64
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Generator, Optional

import requests
from dotenv import load_dotenv
from googleapiclient.errors import HttpError

from email_classifier import EmailClassifier, IntentLabel, RoutingDecision
from email_responder import (
    CAIRO_TZ,
    EmailMessage,
    EmailResponder,
    _extract_display_name,
    _extract_email_address,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  |  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("concierge.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("concierge.main")
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
logging.getLogger("googleapiclient.discovery").setLevel(logging.WARNING)

POLL_INTERVAL_SECONDS  = int(os.getenv("POLL_INTERVAL_SECONDS",  "120"))
INITIAL_LOOKBACK_HOURS = int(os.getenv("INITIAL_LOOKBACK_HOURS", "24"))
MAX_EMAILS_PER_CYCLE   = int(os.getenv("MAX_EMAILS_PER_CYCLE",   "20"))
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")


def _load_template(filename: str) -> str:
    path = os.path.join(TEMPLATE_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if not ln.startswith("#")]
        return "\n".join(lines).strip()
    except FileNotFoundError:
        return ""


def _render_template(template: str, variables: dict) -> str:
    for key, value in variables.items():
        template = template.replace(f"{{{{{key}}}}}", str(value))
    return template


def _fetch_unread_emails(
    gmail_client,
    lookback_hours: int = INITIAL_LOOKBACK_HOURS,
    max_results:    int = MAX_EMAILS_PER_CYCLE,
) -> Generator[EmailMessage, None, None]:
    after_ts = int((datetime.utcnow() - timedelta(hours=lookback_hours)).timestamp())
    query    = f"is:unread in:inbox after:{after_ts}"
    try:
        response = (
            gmail_client.users().messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
    except HttpError as exc:
        logger.error("Gmail list failed: %s", exc)
        return

    messages = response.get("messages", [])
    if not messages:
        logger.debug("No unread emails in the last %dh.", lookback_hours)
        return

    logger.info("Found %d unread email(s).", len(messages))
    for stub in messages:
        try:
            full = (
                gmail_client.users().messages()
                .get(userId="me", id=stub["id"], format="full")
                .execute()
            )
            email = _parse_gmail_message(full)
            if email:
                yield email
        except HttpError as exc:
            logger.error("Failed to fetch message %s: %s", stub["id"], exc)


def _parse_gmail_message(raw: dict) -> Optional[EmailMessage]:
    try:
        headers = {h["name"].lower(): h["value"] for h in raw.get("payload", {}).get("headers", [])}
        sender_raw = headers.get("from", "unknown@unknown.com")
        return EmailMessage(
            message_id   = headers.get("message-id", raw["id"]),
            thread_id    = raw.get("threadId", ""),
            sender       = sender_raw,
            sender_email = _extract_email_address(sender_raw),
            sender_name  = _extract_display_name(sender_raw),
            subject      = headers.get("subject", "(no subject)"),
            body_plain   = _extract_plain_text(raw.get("payload", {})),
            received_at  = headers.get("date", ""),
        )
    except Exception as exc:
        logger.error("Failed to parse message %s: %s", raw.get("id", "?"), exc)
        return None


def _extract_plain_text(payload: dict) -> str:
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")
    if mime_type == "text/plain" and body_data:
        return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        text = _extract_plain_text(part)
        if text:
            return text
    if body_data:
        return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
    return ""


def _mark_as_read(gmail_client, message_id: str) -> None:
    try:
        gmail_client.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
    except HttpError as exc:
        logger.warning("Could not mark message as read: %s", exc)


def _build_inquiry_reply(email: EmailMessage, inquiry_summary: str) -> str:
    template = _load_template("standard_inquiry.md")
    if not template:
        return (
            f"Hi {email.sender_name},\n\n"
            f"Thank you for your message regarding: {inquiry_summary}\n\n"
            f"We'll follow up with more detail shortly.\n\nBest regards,\nAI Email Concierge"
        )
    return _render_template(template, {
        "sender_first_name": email.sender_name.split()[0],
        "original_subject":  email.subject,
        "inquiry_summary":   inquiry_summary,
        "response_body":     f"Thank you for your enquiry about: {inquiry_summary}\n\nWe'll follow up shortly.",
        "agent_name":        "The Team",
        "agent_title":       "AI Email Concierge",
    })


def _build_meeting_reply(email: EmailMessage, meet_url: str, event_id: str) -> str:
    template = _load_template("schedule_meeting.md")
    if not template:
        return (
            f"Hi {email.sender_name},\n\n"
            f"Your meeting has been scheduled. Join via Google Meet: {meet_url}\n\n"
            f"Best regards,\nAI Email Concierge"
        )
    from datetime import datetime, timedelta
    start_dt = datetime.now(tz=CAIRO_TZ).replace(hour=14, minute=0, second=0, microsecond=0) + timedelta(days=2)
    return _render_template(template, {
        "sender_first_name":     email.sender_name.split()[0],
        "original_subject":      email.subject,
        "meeting_title":         f"Meeting: {email.subject[:50]}",
        "meeting_date":          start_dt.strftime("%A, %d %B %Y"),
        "meeting_time":          start_dt.strftime("%I:%M %p Cairo Time"),
        "meeting_duration_mins": "45",
        "meet_url":              meet_url,
        "calendar_event_id":     event_id,
        "agent_name":            "The Team",
        "agent_title":           "AI Email Concierge",
    })


def process_email(
    email:       EmailMessage,
    classifier:  EmailClassifier,
    responder:   EmailResponder,
    gmail_client,
) -> None:
    logger.info("Processing: %r from %s", email.subject, email.sender_email)

    result = classifier.classify(
        sender=email.sender, subject=email.subject,
        body=email.body_plain, received_at=email.received_at,
    )

    action_taken     = "LOGGED_ONLY"
    meet_url         = ""
    event_id         = ""

    if result.routing_decision == RoutingDecision.MANUAL_ONLY:
        action_taken = "MANUAL_REVIEW"
        if os.getenv("GMAIL_REVIEW_LABEL"):
            responder._apply_gmail_label(email.message_id, os.getenv("GMAIL_REVIEW_LABEL"))

    elif result.routing_decision == RoutingDecision.SILENT_LOG:
        action_taken = "LOGGED_ONLY"

    elif result.should_act:
        if result.is_meeting_request:
            ok, meet_url, event_id = responder.create_meeting(email)
            if ok:
                responder.send_reply(email, _build_meeting_reply(email, meet_url, event_id))
                action_taken = "MEETING_CREATED"
            else:
                action_taken = "MANUAL_REVIEW"
        elif result.should_reply:
            ok = responder.send_reply(email, _build_inquiry_reply(email, result.inquiry_summary))
            action_taken = "REPLY_SENT" if ok else "MANUAL_REVIEW"

    _mark_as_read(gmail_client, email.message_id)

    responder.log_to_sheets(
        email=email,
        intent_label=result.intent_label.value,
        confidence_score=result.confidence_score,
        action_taken=action_taken,
        meet_url=meet_url,
        review_pending=result.review_pending,
        calendar_event_id=event_id,
    )
    responder.notify_slack(
        email=email,
        intent_label=result.intent_label.value,
        confidence_score=result.confidence_score,
        action_taken=action_taken,
        routing_decision=result.routing_decision.value,
        meet_url=meet_url,
        review_pending=result.review_pending,
        latency_ms=result.latency_ms,
    )

    logger.info("Done | action=%-15s intent=%-22s confidence=%.2f",
                action_taken, result.intent_label, result.confidence_score)


def run_pipeline(classifier: EmailClassifier, responder: EmailResponder, once: bool = False) -> None:
    logger.info("AI Email Concierge started | interval=%ds model=%s",
                POLL_INTERVAL_SECONDS, os.getenv("OLLAMA_MODEL", "llama3.2:1b"))

    lookback = INITIAL_LOOKBACK_HOURS
    while True:
        logger.info("Polling cycle started (lookback: %dh)", lookback)
        try:
            gmail = responder._gmail_client()
            count = 0
            for email in _fetch_unread_emails(gmail, lookback_hours=lookback):
                try:
                    process_email(email, classifier, responder, gmail)
                    count += 1
                except Exception as exc:
                    logger.exception("Error processing %s: %s", email.message_id, exc)
            logger.info("Cycle complete | processed=%d", count)
        except Exception as exc:
            logger.exception("Polling cycle failed: %s", exc)

        lookback = max(1, POLL_INTERVAL_SECONDS // 3600 + 1)

        if once:
            break
        logger.info("Sleeping %ds...", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


def run_health_check(classifier: EmailClassifier, responder: EmailResponder) -> bool:
    checks: dict[str, bool] = {}

    checks["Ollama LLM"] = classifier.health_check()

    try:
        responder._get_creds()
        checks["Google OAuth"] = True
    except Exception as exc:
        logger.error("Google OAuth: %s", exc)
        checks["Google OAuth"] = False

    try:
        responder._gmail_client().users().getProfile(userId="me").execute()
        checks["Gmail API"] = True
    except Exception as exc:
        logger.error("Gmail API: %s", exc)
        checks["Gmail API"] = False

    sheet_id = os.getenv("AUDIT_SHEET_ID", "")
    if sheet_id:
        try:
            responder._sheets_client().spreadsheets().get(spreadsheetId=sheet_id).execute()
            checks["Google Sheets"] = True
        except Exception as exc:
            logger.error("Sheets API: %s", exc)
            checks["Google Sheets"] = False
    else:
        checks["Google Sheets"] = False

    webhook = os.getenv("SLACK_WEBHOOK_URL", "")
    if webhook:
        try:
            r = requests.post(webhook, json={"text": "health check ping"}, timeout=5)
            checks["Slack Webhook"] = r.status_code == 200
        except Exception as exc:
            logger.error("Slack: %s", exc)
            checks["Slack Webhook"] = False
    else:
        checks["Slack Webhook"] = False

    print("\nHealth Check")
    print("─" * 40)
    all_ok = True
    for name, ok in checks.items():
        print(f"  {'✅' if ok else '❌'}  {name}")
        if not ok:
            all_ok = False
    print()
    return all_ok


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Email Concierge")
    parser.add_argument("--once",           action="store_true", help="Single polling pass then exit")
    parser.add_argument("--health",         action="store_true", help="Run dependency checks then exit")
    parser.add_argument("--setup-headers",  action="store_true", dest="setup_headers",
                        help="Write Sheet column headers then exit")
    args = parser.parse_args()

    classifier = EmailClassifier()
    responder  = EmailResponder()

    if args.health:
        sys.exit(0 if run_health_check(classifier, responder) else 1)

    if args.setup_headers:
        responder.ensure_sheet_headers()
        sys.exit(0)

    run_pipeline(classifier, responder, once=args.once)


if __name__ == "__main__":
    main()

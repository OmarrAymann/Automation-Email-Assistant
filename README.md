# AI Email Concierge

A self-hosted email automation pipeline. Reads incoming Gmail, classifies intent with a local Ollama LLM, sends replies, creates Google Calendar events with Meet links, logs everything to Google Sheets, and posts traces to Slack.

No cloud AI. No external API keys. Runs entirely on your infrastructure.

---

## Architecture

```
Gmail (unread)
      │
      ▼
email_classifier.py   ← Ollama / Llama 3.2:1b
      │
  intent + confidence
      │
      ▼
    main.py  (router)
      │
  ┌───┴───────────────┐
  ▼                   ▼
Reply flow       Meeting flow
  │                   │
Gmail send    Google Calendar + Meet
  │                   │
  └────────┬──────────┘
           ▼
  email_responder.py
           │
    Google Sheets log
           │
    Slack notification
```

---

## Files

| File | Responsibility |
|---|---|
| `main.py` | Entry point, polling loop, routing logic |
| `email_classifier.py` | Ollama classification, confidence thresholds, routing decisions |
| `email_responder.py` | Gmail replies, Calendar events, Sheets logging, Slack notifications |

---

## Intent Labels

| Label | Description | Action |
|---|---|---|
| `INQUIRY_STANDARD` | General questions, info requests | Reply sent |
| `SUPPORT_ESCALATION` | Help requests, bug reports, complaints | Reply sent |
| `CALENDAR_REQUEST` | Meeting or call scheduling requests | Calendar event + Meet link + reply |
| `JUNK_FILTERED` | Spam, promotions, phishing | Logged only |
| `AWAY_NOTICE` | Out-of-office, bounce notifications | Logged only |

---

## Confidence Routing

| Score | Decision |
|---|---|
| `≥ 0.85` | `AUTO_ACT` — immediate automated action |
| `0.70 – 0.84` | `ACT_FLAGGED` — action taken, `review_pending = TRUE` in Sheets |
| `< 0.70` | `MANUAL_ONLY` — no action, routed to manual review queue |
| `JUNK_FILTERED` / `AWAY_NOTICE` | `SILENT_LOG` — logged only, no reply ever sent |

---

## Requirements

- Python 3.11+
- Ollama running locally (`ollama serve`)
- Llama 3.2:1b pulled (`ollama pull llama3.2:1b`)
- Google Workspace account (Gmail, Calendar, Sheets APIs enabled)
- Slack workspace with an incoming webhook

### Install dependencies

```bash
pip install \
  google-auth \
  google-auth-oauthlib \
  google-auth-httplib2 \
  google-api-python-client \
  requests \
  python-dotenv
```

---

## Setup

**1. Clone and configure environment**

```bash
cp .env.example .env
```

Fill in all values in `.env` — sheet ID, calendar ID, Slack webhook, and OAuth paths.

**2. Enable Google APIs**

In [Google Cloud Console](https://console.cloud.google.com):
- Enable Gmail API, Google Calendar API, Google Sheets API
- Create OAuth 2.0 credentials (Desktop app type)
- Download `credentials.json` to the project root

**3. Write Sheet headers**

```bash
python main.py --setup-headers
```

This runs once. On first execution it will open a browser for Google OAuth consent and cache the token.

**4. Run health checks**

```bash
python main.py --health
```

Expected output:
```
Health Check
────────────────────────────────────────
  ✅  Ollama LLM
  ✅  Google OAuth
  ✅  Gmail API
  ✅  Google Sheets
  ✅  Slack Webhook
```

**5. Start the pipeline**

```bash
python main.py          # continuous loop (polls every 120s by default)
python main.py --once   # single pass then exit (useful for cron)
```

---

## Environment Variables

```env
# n8n / runtime
POLL_INTERVAL_SECONDS=120
INITIAL_LOOKBACK_HOURS=24
MAX_EMAILS_PER_CYCLE=20

# Ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=llama3.2:1b

# Confidence thresholds
CONFIDENCE_THRESHOLD_AUTO=0.85
CONFIDENCE_THRESHOLD_REVIEW=0.70

# Google OAuth
GOOGLE_CREDENTIALS_PATH=credentials.json
GOOGLE_TOKEN_PATH=token.json

# Google Sheets
AUDIT_SHEET_ID=your_sheet_id_here
AUDIT_SHEET_TAB=email_audit_log

# Google Calendar
CALENDAR_ID=primary

# Slack
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
SLACK_NOTIFY_CHANNEL=#email-automation

# Gmail labels (optional)
GMAIL_PROCESSED_LABEL=concierge/processed
GMAIL_REVIEW_LABEL=concierge/needs-review
```

---

## Audit Log Schema

Every processed email writes one row to Google Sheets:

| Column | Key | Example |
|---|---|---|
| Timestamp | `processed_at` | `2025-03-12T14:00:00+02:00` |
| Sender | `sender_address` | `youssef@example.com` |
| Subject | `email_subject` | `Quick question about pricing` |
| Intent | `intent_label` | `INQUIRY_STANDARD` |
| Confidence | `confidence_score` | `0.92` |
| Action | `action_dispatched` | `REPLY_SENT` |
| Meet link | `meet_url` | `https://meet.google.com/...` |
| Review flag | `review_pending` | `FALSE` |
| Gmail ID | `gmail_message_id` | `<msg-id@mail.gmail.com>` |
| Event ID | `calendar_event_id` | `abc123xyz` |

---

## Project Structure

```
ai-email-concierge/
├── main.py
├── email_classifier.py
├── email_responder.py
├── templates/
│   ├── standard_inquiry.md
│   └── schedule_meeting.md
├── .env.example
├── credentials.json       ← from Google Cloud Console (not committed)
├── token.json             ← auto-generated on first run (not committed)
└── concierge.log          ← auto-generated at runtime
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| LLM runtime | Ollama |
| Model | Llama 3.2:1b |
| Email | Gmail API |
| Scheduling | Google Calendar API + Google Meet |
| Audit log | Google Sheets API |
| Notifications | Slack Incoming Webhooks |
| Timezone | `Africa/Cairo` |

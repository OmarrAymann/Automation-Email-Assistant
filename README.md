# Email Assistant 

A self-hosted email automation pipeline. Reads incoming Gmail, classifies intent with a local Ollama LLM, sends replies, creates Google Calendar events with Meet links, logs everything to Google Sheets, and posts traces to Slack.

---

## Architecture

```
Gmail (unread)
      │
      ▼
email_classifier.py   ← Llama 3.2:1b
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

```


---


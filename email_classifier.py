from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class IntentLabel(str, Enum):
    INQUIRY_STANDARD   = "INQUIRY_STANDARD"
    SUPPORT_ESCALATION = "SUPPORT_ESCALATION"
    CALENDAR_REQUEST   = "CALENDAR_REQUEST"
    JUNK_FILTERED      = "JUNK_FILTERED"
    AWAY_NOTICE        = "AWAY_NOTICE"
    UNRESOLVED         = "UNRESOLVED"


SILENT_INTENTS = {IntentLabel.JUNK_FILTERED, IntentLabel.AWAY_NOTICE}


class RoutingDecision(str, Enum):
    AUTO_ACT    = "AUTO_ACT"
    ACT_FLAGGED = "ACT_FLAGGED"
    MANUAL_ONLY = "MANUAL_ONLY"
    SILENT_LOG  = "SILENT_LOG"


@dataclass
class ClassificationResult:
    intent_label:         IntentLabel
    confidence_score:     float
    reasoning:            str
    inquiry_summary:      str
    suggested_reply_tone: str
    routing_decision:     RoutingDecision
    review_pending:       bool
    raw_response:         str = field(repr=False, default="")
    latency_ms:           int = 0

    @property
    def should_act(self) -> bool:
        return self.routing_decision in (RoutingDecision.AUTO_ACT, RoutingDecision.ACT_FLAGGED)

    @property
    def should_reply(self) -> bool:
        return self.should_act and self.intent_label not in SILENT_INTENTS

    @property
    def is_meeting_request(self) -> bool:
        return self.intent_label == IntentLabel.CALENDAR_REQUEST and self.should_act


SYSTEM_PROMPT = """You are an email classification engine embedded in an automated workflow.
Your sole function is to analyse incoming emails and return a structured JSON object.
You do not generate conversational replies. You do not ask clarifying questions.
You return JSON only — no markdown, no code fences, no extra text.

Return exactly this JSON structure:
{
  "intent_label": "<INTENT>",
  "confidence_score": <float 0.0-1.0>,
  "reasoning": "<1-2 sentences>",
  "inquiry_summary": "<1 sentence summarising what the sender wants>",
  "suggested_reply_tone": "<formal|friendly|neutral>"
}

VALID INTENT LABELS (use exactly as written):
  INQUIRY_STANDARD    — General questions, info requests, introductions, pre-sales queries
  SUPPORT_ESCALATION  — Help requests, bug reports, complaints, issues needing resolution
  CALENDAR_REQUEST    — Any request to schedule a meeting, call, or demo
  JUNK_FILTERED       — Spam, unsolicited promotions, newsletters, phishing
  AWAY_NOTICE         — Automated out-of-office replies, delivery failure notices, bounces

CONFIDENCE SCORE GUIDANCE:
  1.0 = Unambiguous. Only one label could possibly apply.
  0.9 = Highly confident. Email strongly signals this intent.
  0.8 = Confident. Minor ambiguity but label is clearly correct.
  0.7 = Plausible but another label could also fit.
  < 0.7 = Uncertain. Flag for human review.

RULES:
  - Return ONLY valid JSON. No preamble. No explanation. No markdown code fences.
  - If the email body is empty, classify as JUNK_FILTERED with confidence 0.95.
  - If it is an automated system message, classify as AWAY_NOTICE.
  - For CALENDAR_REQUEST look for: schedule, book a time, set up a call, when are you free,
    can we meet, let's connect, arrange a demo, availability."""


def _build_user_message(sender: str, subject: str, body: str, received_at: str) -> str:
    max_body_chars = 2000
    truncated = body[:max_body_chars] + ("…[truncated]" if len(body) > max_body_chars else "")
    return (
        f"<email>\n"
        f"  <from>{sender}</from>\n"
        f"  <subject>{subject}</subject>\n"
        f"  <date>{received_at}</date>\n"
        f"  <body>\n{truncated}\n  </body>\n"
        f"</email>\n\n"
        f"Classify the email above and return the JSON object."
    )


def _parse_json_from_response(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in model response: {raw[:200]!r}")
    return json.loads(match.group())


def _determine_routing(
    intent: IntentLabel,
    score: float,
    threshold_auto: float,
    threshold_review: float,
) -> tuple[RoutingDecision, bool]:
    if intent in SILENT_INTENTS:
        return RoutingDecision.SILENT_LOG, False
    if score >= threshold_auto:
        return RoutingDecision.AUTO_ACT, False
    if score >= threshold_review:
        return RoutingDecision.ACT_FLAGGED, True
    return RoutingDecision.MANUAL_ONLY, True


class EmailClassifier:
    def __init__(
        self,
        base_url:         Optional[str]   = None,
        model:            Optional[str]   = None,
        threshold_auto:   Optional[float] = None,
        threshold_review: Optional[float] = None,
        timeout_seconds:  int = 60,
        max_retries:      int = 3,
    ):
        self.base_url         = (base_url or os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.model            = model or os.getenv("OLLAMA_MODEL", "llama3.2:1b")
        self.threshold_auto   = threshold_auto   or float(os.getenv("CONFIDENCE_THRESHOLD_AUTO",   "0.85"))
        self.threshold_review = threshold_review or float(os.getenv("CONFIDENCE_THRESHOLD_REVIEW", "0.70"))
        self.timeout          = timeout_seconds
        self.max_retries      = max_retries
        self._endpoint        = f"{self.base_url}/api/chat"
        logger.info("EmailClassifier ready | model=%s auto=%.2f review=%.2f",
                    self.model, self.threshold_auto, self.threshold_review)

    def classify(self, sender: str, subject: str, body: str, received_at: str = "") -> ClassificationResult:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": _build_user_message(sender, subject, body, received_at)},
            ],
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 256},
        }
        raw, latency_ms = self._call_ollama(payload)
        return self._build_result(raw, latency_ms)

    def health_check(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            models = [m.get("name", "") for m in resp.json().get("models", [])]
            available = any(self.model in m for m in models)
            if not available:
                logger.warning("Model %s not found. Available: %s", self.model, models)
            return available
        except Exception as exc:
            logger.error("Ollama health check failed: %s", exc)
            return False

    def _call_ollama(self, payload: dict) -> tuple[str, int]:
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                t0 = time.monotonic()
                resp = requests.post(self._endpoint, json=payload, timeout=self.timeout)
                latency_ms = int((time.monotonic() - t0) * 1000)
                resp.raise_for_status()
                raw = resp.json()["message"]["content"]
                return raw, latency_ms
            except (requests.RequestException, KeyError) as exc:
                last_exc = exc
                wait = 2 ** attempt
                logger.warning("Ollama attempt %d/%d failed — retry in %ds", attempt, self.max_retries, wait)
                if attempt < self.max_retries:
                    time.sleep(wait)
        raise requests.RequestException(f"Ollama unreachable after {self.max_retries} attempts") from last_exc

    def _build_result(self, raw: str, latency_ms: int) -> ClassificationResult:
        try:
            data = _parse_json_from_response(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            logger.error("Failed to parse Ollama response: %s", exc)
            return ClassificationResult(
                intent_label=IntentLabel.UNRESOLVED,
                confidence_score=0.0,
                reasoning="Classification parsing failed.",
                inquiry_summary="Unable to summarise.",
                suggested_reply_tone="neutral",
                routing_decision=RoutingDecision.MANUAL_ONLY,
                review_pending=True,
                raw_response=raw,
                latency_ms=latency_ms,
            )

        raw_label = data.get("intent_label", "UNRESOLVED").strip().upper()
        try:
            intent = IntentLabel(raw_label)
        except ValueError:
            logger.warning("Unknown intent label %r — defaulting to UNRESOLVED", raw_label)
            intent = IntentLabel.UNRESOLVED

        score = max(0.0, min(1.0, float(data.get("confidence_score", 0.0))))
        routing, review_flag = _determine_routing(intent, score, self.threshold_auto, self.threshold_review)

        result = ClassificationResult(
            intent_label=intent,
            confidence_score=score,
            reasoning=data.get("reasoning", ""),
            inquiry_summary=data.get("inquiry_summary", ""),
            suggested_reply_tone=data.get("suggested_reply_tone", "neutral"),
            routing_decision=routing,
            review_pending=review_flag,
            raw_response=raw,
            latency_ms=latency_ms,
        )
        logger.info("Classified | intent=%-20s confidence=%.2f routing=%-12s latency=%dms",
                    result.intent_label, result.confidence_score, result.routing_decision, latency_ms)
        return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s | %(name)s | %(message)s")
    clf = EmailClassifier()
    if not clf.health_check():
        print("Ollama is not reachable. Run: ollama serve")
    else:
        r = clf.classify(
            sender="youssef@example.com",
            subject="Can we set up a quick call this week?",
            body="Hi, I'd love to schedule a 30-minute call. Free Thursday afternoon? — Youssef",
            received_at="2025-03-12T10:30:00+02:00",
        )
        print(f"Intent:     {r.intent_label}")
        print(f"Confidence: {r.confidence_score:.2f}")
        print(f"Routing:    {r.routing_decision}")
        print(f"Summary:    {r.inquiry_summary}")
        print(f"Latency:    {r.latency_ms}ms")

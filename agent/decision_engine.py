"""Layered reply-or-ignore pipeline: decide whether the agent may auto-reply to a message.

Layers run cheapest first and stop at the first "ignore":
    1. Hard rules   — no LLM: own messages, non-person chats, unknown senders.
    2. Signal rules — no LLM: media-only, forwarded, empty, one-word acks.
    3. Intent check — one Gemini call; anything unexpected fails CLOSED (ignore).
       A message asking Vaideesh to commit to something (plan, invite, call, time) may
       still be drafted, but its reason starts with APPROVAL_PREFIX: approve before sending.
Every decision is appended as one JSON line to logs/decision_log.jsonl.
"""

import json
import logging
import os
import unicodedata
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from google.genai import types

from agent.gemini import MODEL, get_client
from config.constants import ONE_WORD_ACKS


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_PATH = LOG_DIR / "decision_log.jsonl"
PERSONA_PATH = PROJECT_ROOT / "persona" / "persona.json"

INTENT_MODEL = MODEL
SAFE = "safe_to_auto_reply"
NEEDS_HUMAN = "needs_human_money_or_serious"
NEEDS_APPROVAL = "needs_approval_commitment"
INTENT_LABELS = {SAFE, NEEDS_HUMAN, NEEDS_APPROVAL}
APPROVAL_PREFIX = "needs approval:"

MEDIA_TYPES = ("image", "audio", "video")
# Router results that are never a one-to-one chat with a person.
NON_PERSON_CHATS = {"group", "broadcast", "newsletter"}

INTENT_INSTRUCTION = f"""You are a safety classifier for a WhatsApp auto-reply agent.
Classify the incoming message with exactly one label. If more than one applies, use the
first that matches, in this order:
- {NEEDS_HUMAN}: anything involving money, payments, prices, rates, invoices, loans, or
  financial commitments; medical, legal, or other serious personal matters (health,
  emergencies, conflict, grief, relationship-defining topics); or anything genuinely
  ambiguous where a wrong automatic reply could cause harm.
- {NEEDS_APPROVAL}: the message asks the recipient to commit to something — a plan, meetup,
  invite, call, visit, time or date, or "will you come / are you free / can we talk"
  (in English, Tamil, or Tanglish, e.g. "varuviya?", "free a?", "call pannalama?").
- {SAFE}: ordinary casual or logistical chat that asks for no commitment. This includes
  work feedback or requests to review, check, or tweak something (e.g. "the director wants
  a warmer look, can you check?") when they mention no money, deadline, date, or meeting
  time: the reply only acknowledges and says it will be looked into.
Reply with the label only — no punctuation, no explanation, nothing else."""

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _known_relationships() -> frozenset[str]:
    return frozenset(json.loads(PERSONA_PATH.read_text(encoding="utf-8"))["relationships"])


def _normalize(text: str) -> str:
    """Lowercase, trim whitespace, and strip punctuation (including '…', '¿') from both ends."""
    text = text.strip().lower()
    start, end = 0, len(text)
    while start < end and (unicodedata.category(text[start]).startswith("P") or text[start].isspace()):
        start += 1
    while end > start and (unicodedata.category(text[end - 1]).startswith("P") or text[end - 1].isspace()):
        end -= 1
    return text[start:end]


def _classify_intent(text: str) -> str | None:
    """The single LLM call: return Gemini's raw text for this message."""
    response = get_client().models.generate_content(
        model=INTENT_MODEL,
        contents=text,
        config=types.GenerateContentConfig(system_instruction=INTENT_INSTRUCTION, temperature=0),
    )
    return response.text


def check_intent(text: str) -> tuple[str, str | None]:
    """Return (label, raw_if_unexpected). Fails CLOSED: anything but an exact label is NEEDS_HUMAN."""
    try:
        raw = _classify_intent(text)
    except Exception as error:  # network/API/auth errors must never let a message through
        raw = f"<error: {type(error).__name__}: {error}>"
    label = raw.strip() if isinstance(raw, str) else raw
    if label in INTENT_LABELS:
        return label, None
    logger.warning("Intent check failed closed; raw response: %r", raw)
    return NEEDS_HUMAN, "" if raw is None else str(raw)


def _log_decision(message: dict, relationship: str, reply: bool, reason: str) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "text": message.get("text"),
        "relationship": relationship,
        "decision": ("needs_approval" if requires_approval(reason) else "reply") if reply else "ignore",
        "reason": reason,
    }
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(record, ensure_ascii=False) + "\n")


def _decide(message: dict, relationship: str) -> tuple[bool, str]:
    required = {"from_me": bool, "text": str, "message_type": str, "is_forwarded": bool}
    if not isinstance(message, dict) or any(not isinstance(message.get(key), kind) for key, kind in required.items()):
        return False, "malformed message"

    # 1. HARD RULES — instant, no LLM call.
    if message["from_me"]:
        return False, "own message"
    if relationship == "group":
        return False, "group chat, not allowlisted"
    if relationship in NON_PERSON_CHATS:
        return False, f"{relationship} chat, not allowlisted"
    if relationship == "unknown":
        return False, "sender not in allowlist"
    if relationship not in _known_relationships():
        return False, f"relationship '{relationship}' not recognised"

    # 2. SIGNAL RULES — still no LLM call.
    text = message["text"]
    if message["message_type"] in MEDIA_TYPES and not text.strip():
        # Session 4.1 will upgrade this specific case into a rule-based contextual ack
        # (e.g. reacting to a photo) instead of a plain ignore. For now, ignore is correct.
        return False, "media-only, no text to ground a reply"
    if message["is_forwarded"]:
        return False, "forwarded content, not a real question"
    if not text.strip():
        return False, "empty message"
    if _normalize(text) in ONE_WORD_ACKS:
        return False, "low-signal ack, no reply needed"

    # 3. INTENT CHECK — the one LLM call. Fails closed.
    label, unexpected = check_intent(text)
    if unexpected is not None:
        return False, f"intent check failed closed (unexpected response: {unexpected[:200]!r})"
    if label == NEEDS_HUMAN:
        return False, "needs human: money or serious topic"
    if label == NEEDS_APPROVAL:
        return True, f"{APPROVAL_PREFIX} asks for a commitment (plan, invite, call, or time)"

    # 4. Otherwise reply.
    return True, "passed all gates"


def requires_approval(reason: str) -> bool:
    """True when a draft may be written but must be approved before it is sent."""
    return reason.startswith(APPROVAL_PREFIX)


def should_reply(message: dict, relationship: str) -> tuple[bool, str]:
    """Return (should_reply, reason) for an incoming message, and log the decision."""
    reply, reason = _decide(message, relationship)
    _log_decision(message if isinstance(message, dict) else {}, relationship, reply, reason)
    return reply, reason

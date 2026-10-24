"""Deterministic check on every draft before it is used: hold anything that commits Vaideesh.

Pure word-list and pattern rules, no LLM, so the same draft always gets the same verdict.
The check holds a draft (needs_approval) when it:
  - agrees to or confirms a plan, meetup, call, time, or invite ("varen", "sure", "ok da",
    "pesalam", "done", "confirm", "I'll come", "at 7pm", "naalaiki", ...);
  - contains a [placeholder] — a fact only Vaideesh can fill in;
  - is empty, the no-reply fallback, or anything else it can't judge (unsure -> hold);
  - is for an unknown sender (always). A draft that would confirm Vaideesh's name or
    identity to an unknown sender is replaced with a neutral question.
"""

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PERSONA_PATH = PROJECT_ROOT / "persona" / "persona.json"

OK = "ok"
NEEDS_APPROVAL = "needs_approval"
UNKNOWN_SENDER_DRAFT = "Hi! May I know who this is?"

# Each rule: (reason, pattern). Patterns run on the lowercased draft.
COMMITMENT_RULES = [
    (
        "agrees or says yes",
        r"\b(yes|yeah|yep|yup|ya|yaa|aama|aamam|haan|sure|ok|okay|okk|okie|seri|sari|done|fine|"
        r"kandipa|kandippa|definitely|of course|confirm|confirmed|confirming|deal)\b",
    ),
    (
        "says Vaideesh will come or go (Tanglish)",
        r"\b(varen|vaaren|varuven|varuvein|vanthuduven|vandhuduven|vanthuruven|vandhuruven|varalam|"
        r"varalaam|poren|poven|povom|polam|polaam|kelambiduven|kelambaren|kelamburen)\b",
    ),
    (
        "agrees to talk, call, or meet (Tanglish)",
        r"\b(pesalam|pesalaam|pesuvom|pesuven|paapom|paappom|meet pannalam|call pannalam|"
        r"call (pannuren|pandren|panren|pannren|panna?ren|panniduren|pannidren))\b",
    ),
    (
        "commits to an action (English)",
        r"\b(i'?ll|i will|we'?ll|we will|will)\s+(come|be there|join|reach|call|meet|send|share|"
        r"deliver|do it|do that|confirm|see you|ping|connect|be free)\b",
    ),
    (
        "proposes or accepts a plan, call, or meetup (English)",
        r"\b(let'?s|lets)\s+(talk|meet|catch up|connect|speak|go|do it|plan)\b|\b(see you|coming|on my way|count me in)\b",
    ),
    (
        "names a time or day",
        r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b|\b(at|by|around|before|after)\s+\d{1,2}(:\d{2})?\b|"
        r"\b(today|tonight|tomorrow|tmrw|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"naalaiki|nalaiki|naliku|inniki|iniku|innaiku|nethu|morning|evening|night)\b",
    ),
    ("contains a placeholder only Vaideesh can fill in", r"\[[^\]]*\]"),
]
COMPILED_RULES = [(reason, re.compile(pattern)) for reason, pattern in COMMITMENT_RULES]

# Phrases that confirm who someone is, used for unknown senders.
IDENTITY_PHRASES = re.compile(
    r"\b(this is|it'?s me|i am|i'm|that'?s me|speaking|naan thaan|naan dhaan|yes|yeah|yep|aama|haan|correct|right)\b"
)


@dataclass(frozen=True)
class DraftCheck:
    status: str  # OK or NEEDS_APPROVAL
    reason: str
    draft: str  # the draft to use (replaced for unknown senders when it would reveal identity)


@lru_cache(maxsize=1)
def _persona() -> dict:
    return json.loads(PERSONA_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def owner_names() -> frozenset[str]:
    """Vaideesh's name tokens, read from the persona identity ('Vaideesh Srinivasan, ...')."""
    full_name = _persona()["identity"].split(",", 1)[0]
    return frozenset(token.lower() for token in re.findall(r"[A-Za-z]{3,}", full_name))


def reveals_identity(draft: str) -> bool:
    lowered = draft.lower()
    return any(re.search(rf"\b{re.escape(name)}\b", lowered) for name in owner_names()) or bool(
        IDENTITY_PHRASES.search(lowered)
    )


def commitment_reasons(draft: str) -> list[str]:
    lowered = draft.lower()
    return [reason for reason, pattern in COMPILED_RULES if pattern.search(lowered)]


def check_draft(draft: str, relationship: str, fallback: str = "") -> DraftCheck:
    """Return whether a draft can be used as-is (OK) or must be approved first."""
    if not isinstance(draft, str) or not draft.strip() or (fallback and draft.strip() == fallback):
        return DraftCheck(NEEDS_APPROVAL, "unsure: no usable draft", draft if isinstance(draft, str) else "")

    if relationship not in _persona()["relationships"]:
        if reveals_identity(draft):
            return DraftCheck(
                NEEDS_APPROVAL,
                f"unknown sender ('{relationship}'): draft would confirm identity, replaced with a neutral question",
                UNKNOWN_SENDER_DRAFT,
            )
        return DraftCheck(NEEDS_APPROVAL, f"unknown sender ('{relationship}'): always needs approval", draft)

    reasons = commitment_reasons(draft)
    if reasons:
        return DraftCheck(NEEDS_APPROVAL, "held: " + "; ".join(reasons), draft)
    return DraftCheck(OK, "no commitment found", draft)

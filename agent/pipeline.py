"""One incoming message -> a decision, and (if allowed) a checked draft. Never sends anything.

Order: decision_engine.should_reply -> generator.generate_reply -> draft_guard.check_draft.
A draft is "ready" only when every layer agrees; any layer can hold it for approval.
"""

from agent.decision_engine import requires_approval, should_reply
from agent.draft_guard import NEEDS_APPROVAL, check_draft
from agent.generator import NO_REPLY_FALLBACK, generate_reply


IGNORED = "ignored"
READY = "ready"


def prepare_reply(message: dict, relationship: str) -> dict:
    """Return {"status": ignored|needs_approval|ready, "draft": str|None, "reasons": [str, ...]}."""
    reply, reason = should_reply(message, relationship)
    if not reply:
        return {"status": IGNORED, "draft": None, "reasons": [reason]}

    # The decision above uses the current message alone; recent messages only help the draft.
    draft = generate_reply(message["text"], relationship, recent_messages=message.get("recent_messages") or [])
    check = check_draft(draft, relationship, fallback=NO_REPLY_FALLBACK)

    reasons = [reason, f"draft check: {check.reason}"]
    held = requires_approval(reason) or check.status == NEEDS_APPROVAL
    return {"status": NEEDS_APPROVAL if held else READY, "draft": check.draft, "reasons": reasons}

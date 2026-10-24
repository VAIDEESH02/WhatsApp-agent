#!/usr/bin/env python3
"""Local HTTP bridge between the Node/Baileys bot and the Python agent (FastAPI).

POST /process with {jid, text, message_type, is_forwarded, from_me, recent_messages?} runs:
    router.resolve_relationship -> decision_engine.should_reply -> generator.generate_reply
    -> draft check (via agent.pipeline.prepare_reply)
and returns {should_reply, reply, relationship, reason, status, needs_approval,
auto_reply_enabled}.

should_reply is True only when BOTH the auto-reply switch (config/auto_reply.json) is on
AND the draft cleared every check (status "ready"). Everything else comes back with
should_reply False and, if there is a draft, needs_approval True and the draft in reply:
the bot must never send those on its own. With the switch off, ready drafts wait too.

A held draft is also stored as a pending approval (agent/approvals.py). Once Vaideesh
approves it in the console, it appears in GET /outbox; the Node client sends it and reports
back with POST /outbox/{id}/sent.

Every request appends one line to logs/console_feed.jsonl for the live console.
Run: python3 agent/bridge.py   (listens on 127.0.0.1:5001, no auto-reload)
"""

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel, Field, StrictBool  # noqa: E402

from agent import approvals  # noqa: E402
from agent.draft_guard import NEEDS_APPROVAL  # noqa: E402
from agent.pipeline import READY, prepare_reply  # noqa: E402
from agent.router import resolve_relationship  # noqa: E402
from agent.settings import auto_reply_enabled  # noqa: E402
from ingestion.retrieval import retrieve_similar  # noqa: E402


HOST = "127.0.0.1"  # local only: the bot runs on the same machine
PORT = 5001  # not 5000, which AirPlay Receiver uses on modern Macs
LOG_DIR = PROJECT_ROOT / "logs"
CONSOLE_FEED_PATH = LOG_DIR / "console_feed.jsonl"
TRACE_K = 3
MAX_RECENT_MESSAGES = 20  # the WhatsApp client sends at most 8; anything bigger is rejected (422)
MAX_RECENT_CHARS = 500
DECISION_LABELS = {READY: "reply", NEEDS_APPROVAL: "needs_approval"}

_feed_lock = threading.Lock()  # requests run in a thread pool; keep each JSON line whole

app = FastAPI(title="WhatsApp agent bridge")


class RecentMessage(BaseModel):
    """One earlier message in the same chat, oldest first. Context for the draft only."""

    from_me: StrictBool
    text: str = Field(max_length=MAX_RECENT_CHARS)


class IncomingMessage(BaseModel):
    """All fields are required (except the optional ones at the end): a missing from_me must
    never default to "not mine"."""

    jid: str
    text: str
    message_type: str
    is_forwarded: bool
    from_me: bool
    chat_jid: str | None = None  # chat to reply in, when it differs from jid (e.g. an @lid chat)
    # Earlier messages in this chat, oldest first. Never logged; only the draft prompt sees them.
    recent_messages: list[RecentMessage] = Field(default_factory=list, max_length=MAX_RECENT_MESSAGES)


class ProcessResult(BaseModel):
    should_reply: bool
    reply: str | None
    relationship: str
    reason: str
    status: str  # ready | needs_approval | ignored
    needs_approval: bool
    auto_reply_enabled: bool
    approval_id: int | None = None


class SentReport(BaseModel):
    dry_run: StrictBool


def retrieval_trace(relationship: str, collection: str | None, text: str) -> list[dict]:
    """Similar past pairs, for the console only. Never for groups or empty text."""
    if collection is None or not text.strip():
        return []
    return retrieve_similar(relationship, text, k=TRACE_K)


def log_to_console_feed(record: dict) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    line = json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **record}, ensure_ascii=False)
    with _feed_lock, CONSOLE_FEED_PATH.open("a", encoding="utf-8") as feed:
        feed.write(line + "\n")


@app.post("/process", response_model=ProcessResult)
def process(incoming: IncomingMessage) -> ProcessResult:
    message = incoming.model_dump()
    relationship = None
    try:
        relationship, collection = resolve_relationship(incoming.jid)
        result = prepare_reply(message, relationship)
        trace = retrieval_trace(relationship, collection, incoming.text)
    except Exception as error:
        # Fail loudly (HTTP 500, so the bot sends nothing), but still record it for the console.
        reason = f"error: {type(error).__name__}: {error}"
        log_to_console_feed(
            {"jid": incoming.jid, "text": incoming.text, "relationship": relationship, "decision": "error",
             "reason": reason, "reply": None, "retrieval_trace": []}
        )
        raise HTTPException(status_code=500, detail=reason) from error

    status, reasons = result["status"], list(result["reasons"])
    switch_on = auto_reply_enabled()
    if status == READY and not switch_on:
        status = NEEDS_APPROVAL
        reasons.append("auto-reply switch is off")
    reason = " | ".join(reasons)
    approval_id = None
    if status == NEEDS_APPROVAL and result["draft"]:
        approval_id = approvals.add_pending(
            jid=incoming.jid,
            chat_jid=incoming.chat_jid or incoming.jid,
            relationship=relationship,
            incoming_text=incoming.text,
            draft=result["draft"],
            reason=reason,
        )
    log_to_console_feed(
        {"jid": incoming.jid, "text": incoming.text, "relationship": relationship,
         "decision": DECISION_LABELS.get(status, "ignore"), "reason": reason, "reply": result["draft"],
         "approval_id": approval_id, "retrieval_trace": trace}
    )
    return ProcessResult(
        should_reply=status == READY,
        reply=result["draft"],
        relationship=relationship,
        reason=reason,
        status=status,
        needs_approval=status == NEEDS_APPROVAL,
        auto_reply_enabled=switch_on,
        approval_id=approval_id,
    )


@app.get("/outbox")
def outbox() -> list[dict]:
    """Replies Vaideesh approved in the console that the WhatsApp client hasn't sent yet."""
    return approvals.outbox()


@app.post("/outbox/{approval_id}/sent")
def outbox_sent(approval_id: int, report: SentReport) -> dict:
    try:
        return approvals.mark_sent(approval_id, dry_run=report.dry_run)
    except approvals.ApprovalError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


if __name__ == "__main__":
    # reload=False is the uvicorn equivalent of Flask's debug=False: one stable process.
    uvicorn.run(app, host=HOST, port=PORT, reload=False, log_level="info")

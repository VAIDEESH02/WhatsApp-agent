"""Approval queue for held drafts, shared by the bridge (writes, outbox) and the console (decides).

SQLite (data/agent_state.sqlite3, git-ignored) so two processes can use it safely.
Lifecycle:  pending --approve--> approved --(Node client)--> sent | dry_run
            pending --dismiss--> dismissed
Only an explicit approval ever puts a draft in the outbox.
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "agent_state.sqlite3"

PENDING, APPROVED, SENT, DRY_RUN, DISMISSED = "pending", "approved", "sent", "dry_run", "dismissed"
STATUSES = (PENDING, APPROVED, SENT, DRY_RUN, DISMISSED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    jid           TEXT NOT NULL,      -- routing JID (phone)
    chat_jid      TEXT NOT NULL,      -- chat to reply in (may be an @lid id)
    relationship  TEXT NOT NULL,
    incoming_text TEXT NOT NULL,
    draft         TEXT NOT NULL,
    reason        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    final_text    TEXT,
    decided_at    TEXT,
    sent_at       TEXT
);
CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status);
"""


class ApprovalError(ValueError):
    """Raised for an unknown id or a transition the current status doesn't allow."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    return connection


def add_pending(jid: str, chat_jid: str, relationship: str, incoming_text: str, draft: str, reason: str) -> int:
    with closing(_connect()) as db, db:
        cursor = db.execute(
            "INSERT INTO approvals (created_at, jid, chat_jid, relationship, incoming_text, draft, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), jid, chat_jid, relationship, incoming_text, draft, reason),
        )
        return int(cursor.lastrowid)


def get(approval_id: int) -> dict | None:
    with closing(_connect()) as db:
        row = db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return dict(row) if row else None


def list_approvals(status: str | None = None, limit: int = 50) -> list[dict]:
    query, params = "SELECT * FROM approvals", []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with closing(_connect()) as db:
        return [dict(row) for row in db.execute(query, params)]


def counts() -> dict:
    with closing(_connect()) as db:
        found = dict(db.execute("SELECT status, COUNT(*) FROM approvals GROUP BY status").fetchall())
    return {status: found.get(status, 0) for status in STATUSES}


def _transition(approval_id: int, allowed_from: tuple[str, ...], updates: dict) -> dict:
    assignments = ", ".join(f"{column} = ?" for column in updates)
    placeholders = ", ".join("?" for _ in allowed_from)
    with closing(_connect()) as db, db:
        cursor = db.execute(
            f"UPDATE approvals SET {assignments} WHERE id = ? AND status IN ({placeholders})",
            (*updates.values(), approval_id, *allowed_from),
        )
        if cursor.rowcount == 0:
            current = db.execute("SELECT status FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if current is None:
                raise ApprovalError(f"approval {approval_id} not found")
            raise ApprovalError(f"approval {approval_id} is {current['status']}, expected {' or '.join(allowed_from)}")
    return get(approval_id)


def approve(approval_id: int, final_text: str | None = None) -> dict:
    """Approve a pending draft, optionally edited. Empty text is refused."""
    current = get(approval_id)
    text = (final_text if final_text is not None else (current or {}).get("draft", "")).strip()
    if current is not None and not text:
        raise ApprovalError("reply text is empty")
    return _transition(approval_id, (PENDING,), {"status": APPROVED, "final_text": text, "decided_at": _now()})


def dismiss(approval_id: int) -> dict:
    return _transition(approval_id, (PENDING,), {"status": DISMISSED, "decided_at": _now()})


def outbox() -> list[dict]:
    """Approved replies the WhatsApp client has not sent yet, oldest first."""
    with closing(_connect()) as db:
        rows = db.execute(
            "SELECT id, chat_jid, jid, final_text AS text FROM approvals WHERE status = ? ORDER BY id", (APPROVED,)
        )
        return [dict(row) for row in rows]


def mark_sent(approval_id: int, dry_run: bool = False) -> dict:
    return _transition(approval_id, (APPROVED,), {"status": DRY_RUN if dry_run else SENT, "sent_at": _now()})

#!/usr/bin/env python3
"""Reply Desk: live console and control panel for the WhatsApp agent (FastAPI, local only).

GET  /                              Reply Desk frontend (frontend/), refreshes every 2 s
GET  /basic                         the original read-only feed page
GET  /api/feed                      last 20 decisions (newest first) with contact names, plus state
GET  /api/approvals?status=pending  held drafts waiting for Vaideesh
POST /api/approvals/{id}/approve    {"text": optional edited reply} -> outbox (the Node client sends it)
POST /api/approvals/{id}/dismiss
POST /api/auto-reply                {"enabled": true|false} -> config/auto_reply.json
GET  /api/settings                  dry_run, delays, kill switch state
POST /api/settings                  {"dry_run"?, "min_delay_seconds"?, "max_delay_seconds"?} -> config/settings.json
                                    (only the given keys change; validated; written atomically)
POST /api/kill-switch               {"active": true|false} -> creates/deletes kill_switch.flag

Writes are accepted only from this page: the Host must be 127.0.0.1/localhost:5002 (or a
private hostname listed in REPLY_DESK_ALLOWED_HOSTS, e.g. your Tailscale name, reached over
https), any Origin must be that same page, and the request must carry the X-Reply-Desk header
(which a cross-site form or fetch cannot send without a CORS preflight this server never grants).
The client mode (DRY_RUN or LIVE) is read from config/settings.json, which the WhatsApp client
re-reads for every message; the kill switch is the kill_switch.flag file it checks.
Run: python3 console/app.py   ->   http://127.0.0.1:5002
"""

import json
import os
import re
import sys
from collections import deque
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

INGESTION_DIR = str(PROJECT_ROOT / "ingestion")
if INGESTION_DIR not in sys.path:  # ingestion modules import their siblings by bare name at runtime
    sys.path.insert(1, INGESTION_DIR)

import uvicorn  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, StrictBool, StrictFloat, StrictInt  # noqa: E402

from agent import approvals  # noqa: E402
from agent.settings import auto_reply_enabled, set_auto_reply_enabled  # noqa: E402
from config import settings as client_settings  # noqa: E402  (dry run, delays, kill switch)
from ingestion.build_phone_map import CONTACTS_PATH, load_contacts  # noqa: E402
from ingestion.whatsapp_ids import jid_to_phone  # noqa: E402


HOST = "127.0.0.1"
PORT = 5002
FEED_PATH = PROJECT_ROOT / "logs" / "console_feed.jsonl"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
FEED_LIMIT = 20
REFRESH_MS = 2000
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
CSRF_HEADER = "x-reply-desk"
# Extra hostnames for remote access over a private network, e.g. "my-mac.tail1234.ts.net".
# Comma-separated, set in .env or the environment. Reached over https only (tailscale serve).
EXTRA_HOSTS_ENV = "REPLY_DESK_ALLOWED_HOSTS"
HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")

load_dotenv(PROJECT_ROOT / ".env")


def extra_hosts() -> set[str]:
    """Hostnames from REPLY_DESK_ALLOWED_HOSTS. Anything that isn't a plain dotted hostname
    (wildcards, ports, schemes, IPs without dots) is ignored, so a typo can't open the page up."""
    names = (name.strip().lower() for name in os.environ.get(EXTRA_HOSTS_ENV, "").split(","))
    return {name for name in names if HOSTNAME.match(name)}


def _host_allowed(host: str | None) -> bool:
    host = (host or "").lower()
    return host in ALLOWED_HOSTS or host.removesuffix(":443") in extra_hosts()


def _origin_allowed(origin: str) -> bool:
    """The page's own origin: http for this Mac, https for an extra (remote) hostname."""
    origin = origin.lower()
    if origin.startswith("http://"):
        return origin.removeprefix("http://") in ALLOWED_HOSTS
    if origin.startswith("https://"):
        return origin.removeprefix("https://").removesuffix(":443") in extra_hosts()
    return False

app = FastAPI(title="Reply Desk")


@app.middleware("http")
async def local_page_only(request: Request, call_next):
    """Block DNS-rebinding (Host) and cross-site writes (Origin + custom header)."""
    if not _host_allowed(request.headers.get("host")):
        return JSONResponse({"detail": "unknown host"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and not _origin_allowed(origin):
            return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        if request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"detail": "missing X-Reply-Desk header"}, status_code=403)
    return await call_next(request)


@lru_cache(maxsize=4)
def _names_by_phone(_mtime: float) -> dict[str, str]:  # _mtime: cache key, reloads when contacts.md changes
    return {phone: name for name, phones in load_contacts() for phone in phones}


def contact_name(jid: str | None) -> str:
    """Display name for a chat: the saved contact name, 'Group chat', or the bare number."""
    jid = jid or ""
    if jid.endswith("@g.us"):
        return "Group chat"
    if jid.endswith("@broadcast"):
        return "Status update"
    phone = jid_to_phone(jid) if "@" in jid else None
    try:
        names = _names_by_phone(CONTACTS_PATH.stat().st_mtime)
    except (OSError, ValueError):
        names = {}
    if phone and phone in names:
        return names[phone]
    return f"+{phone}" if phone else (jid.split("@")[0] or "Unknown")


def read_mode() -> str:
    return "DRY_RUN" if client_settings.is_dry_run() else "LIVE"


def control_state() -> dict:
    return {**client_settings.load_settings(), "kill_switch_active": client_settings.is_kill_switch_active()}


def read_feed() -> dict:
    """Last FEED_LIMIT records (newest first) and totals over the whole log; malformed lines are skipped."""
    if not FEED_PATH.exists():
        return {"waiting": True, "records": [], "processed": 0, "replies_sent": 0, "awaiting_approval": 0}
    recent: deque = deque(maxlen=FEED_LIMIT)
    processed = replies = awaiting = 0
    with FEED_PATH.open(encoding="utf-8") as feed:
        for line in feed:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            processed += 1
            replies += record.get("decision") == "reply"
            awaiting += record.get("decision") == "needs_approval"
            recent.append(record)
    return {
        "waiting": processed == 0,
        "records": list(reversed(recent)),
        "processed": processed,
        "replies_sent": replies,
        "awaiting_approval": awaiting,
    }


def _with_names(rows: list[dict], jid_key: str = "jid") -> list[dict]:
    return [{**row, "contact_name": contact_name(row.get(jid_key))} for row in rows]


@app.get("/api/feed")
def api_feed() -> dict:
    feed = read_feed()
    approval_ids = {r.get("approval_id") for r in feed["records"] if r.get("approval_id")}
    statuses = {row["id"]: row["status"] for row in approvals.list_approvals(limit=500) if row["id"] in approval_ids}
    feed["records"] = [
        {**record, "approval_status": statuses.get(record.get("approval_id"))} for record in _with_names(feed["records"])
    ]
    counts = approvals.counts()
    return {
        **feed,
        "pending_approvals": counts["pending"],
        "approved_sent": counts["sent"] + counts["dry_run"],
        "mode": read_mode(),
        "auto_reply_enabled": auto_reply_enabled(),
        "controls": control_state(),
    }


@app.get("/api/approvals")
def api_approvals(status: str | None = None, limit: int = 50) -> list[dict]:
    if status is not None and status not in approvals.STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {', '.join(approvals.STATUSES)}")
    return _with_names(approvals.list_approvals(status, limit=min(max(limit, 1), 200)))


class ApproveBody(BaseModel):
    text: str | None = None


class SwitchBody(BaseModel):
    enabled: StrictBool  # only JSON true/false: "yes" or 1 must never switch it on


def _decide(action, approval_id: int, *args) -> dict:
    try:
        return _with_names([action(approval_id, *args)])[0]
    except approvals.ApprovalError as error:
        code = 404 if "not found" in str(error) else 409
        raise HTTPException(status_code=code, detail=str(error)) from error


@app.post("/api/approvals/{approval_id}/approve")
def api_approve(approval_id: int, body: ApproveBody) -> dict:
    return _decide(approvals.approve, approval_id, body.text)


@app.post("/api/approvals/{approval_id}/dismiss")
def api_dismiss(approval_id: int) -> dict:
    return _decide(approvals.dismiss, approval_id)


Seconds = StrictInt | StrictFloat


class SettingsBody(BaseModel):
    dry_run: StrictBool | None = None
    min_delay_seconds: Seconds | None = None
    max_delay_seconds: Seconds | None = None


class KillSwitchBody(BaseModel):
    active: StrictBool


@app.get("/api/settings")
def api_settings() -> dict:
    return control_state()


@app.post("/api/settings")
def api_update_settings(body: SettingsBody) -> dict:
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="nothing to change")
    try:
        client_settings.update_settings(**changes)
    except client_settings.SettingsError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return control_state()


@app.post("/api/kill-switch")
def api_kill_switch(body: KillSwitchBody) -> dict:
    client_settings.set_kill_switch(body.active)
    return control_state()


@app.post("/api/auto-reply")
def api_auto_reply(body: SwitchBody) -> dict:
    return {"auto_reply_enabled": set_auto_reply_enabled(body.enabled)}


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/basic", response_class=HTMLResponse)
def page() -> str:
    return PAGE.replace("__REFRESH_MS__", str(REFRESH_MS)).replace("__FEED_LIMIT__", str(FEED_LIMIT))


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Console</title>
<style>
  :root {
    --bg: #f6f7f9; --panel: #ffffff; --text: #1d2330; --muted: #667085; --border: #e3e6eb;
    --green: #16794a; --green-bg: #e3f5ec; --blue: #1d5fc2; --blue-bg: #e4eefc;
    --purple: #6b3fc4; --purple-bg: #efe8fb; --gray: #59606b; --gray-bg: #eceef1;
    --pink: #b02a6b; --pink-bg: #fbe6f0; --amber: #925c06; --amber-bg: #fcf0d8;
    --red: #b42318; --red-bg: #fde8e7;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #12151b; --panel: #1b1f27; --text: #e7eaf0; --muted: #98a1b0; --border: #2c323d;
      --green: #6fd9a3; --green-bg: #173326; --blue: #8cb8ff; --blue-bg: #18263d;
      --purple: #c3a6ff; --purple-bg: #2a2140; --gray: #b3bac5; --gray-bg: #2a2f38;
      --pink: #ff9ac8; --pink-bg: #3a1d2c; --amber: #f4c16b; --amber-bg: #372a12;
      --red: #ff9b93; --red-bg: #3d1c1a;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  .layout { display: grid; grid-template-columns: 260px 1fr; min-height: 100vh; }
  aside { background: var(--panel); border-right: 1px solid var(--border); padding: 20px 16px; }
  main { padding: 20px; max-width: 980px; width: 100%; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin: 22px 0 8px; }
  .muted { color: var(--muted); font-size: 12px; }
  .metric { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; margin-bottom: 8px; }
  .metric .value { font-size: 24px; font-weight: 600; }
  .metric .label { color: var(--muted); font-size: 12px; }
  .pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-weight: 600; font-size: 12px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; }
  .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .time { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
  .reason { color: var(--muted); margin-top: 6px; word-break: break-word; }
  .reply { margin-top: 8px; padding: 8px 10px; border-left: 3px solid var(--border); background: var(--bg); border-radius: 4px; white-space: pre-wrap; }
  details { margin-top: 8px; }
  summary { cursor: pointer; color: var(--muted); font-size: 12px; }
  .pair { border-top: 1px solid var(--border); padding: 6px 0; font-size: 13px; white-space: pre-wrap; }
  .pair b { font-weight: 600; }
  .empty { color: var(--muted); padding: 40px 0; text-align: center; }
  .green { color: var(--green); background: var(--green-bg); } .blue { color: var(--blue); background: var(--blue-bg); }
  .purple { color: var(--purple); background: var(--purple-bg); } .gray { color: var(--gray); background: var(--gray-bg); }
  .pink { color: var(--pink); background: var(--pink-bg); } .amber { color: var(--amber); background: var(--amber-bg); }
  .red { color: var(--red); background: var(--red-bg); }
  .kill-banner { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; padding: 12px 20px;
                 background: var(--red-bg); color: var(--red); border-bottom: 2px solid var(--red); font-weight: 600; }
  .kill-banner span { flex: 1 1 260px; }
  .controls { display: grid; gap: 12px; }
  fieldset { border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; margin: 0; }
  legend { color: var(--muted); font-size: 12px; padding: 0 4px; }
  fieldset label { margin-right: 12px; cursor: pointer; }
  .field { display: grid; grid-template-columns: 1fr 76px; align-items: center; gap: 6px 8px; }
  .field input { width: 76px; padding: 5px 7px; border: 1px solid var(--border); border-radius: 6px;
                 background: var(--bg); color: var(--text); font: inherit; }
  .btn { padding: 7px 12px; border-radius: 7px; border: 1px solid var(--border); background: var(--panel);
         color: var(--text); font: inherit; font-weight: 600; cursor: pointer; }
  .btn:disabled { opacity: .6; cursor: not-allowed; }
  .btn-kill { width: 100%; padding: 12px; font-size: 15px; letter-spacing: .03em; border: 0;
              background: #c4231a; color: #fff; }
  .btn-kill:hover:not(:disabled) { background: #a51c14; }
  .btn-clear { background: var(--red); color: var(--panel); border: 0; }
  .confirm-box { border: 1px solid var(--amber); background: var(--amber-bg); color: var(--text);
                 border-radius: 8px; padding: 10px; font-size: 13px; }
  .confirm-box .row { margin-top: 8px; }
  .field-error { color: var(--red); font-size: 12px; min-height: 1em; }
  :focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
  @media (max-width: 720px) {
    .layout { grid-template-columns: 1fr; }
    aside { border-right: 0; border-bottom: 1px solid var(--border); padding: 16px; }
    main { padding: 16px; }
  }
</style>
</head>
<body>
<div class="kill-banner" id="kill-banner" role="alert" hidden>
  <span>🛑 Kill switch is ON. The WhatsApp client is processing and sending nothing until you clear it.</span>
  <button class="btn btn-clear" id="clear-kill" type="button">Clear kill switch</button>
</div>
<div class="layout">
  <aside>
    <h1>Agent Console</h1>
    <div class="muted" id="updated">connecting…</div>
    <h2>Mode</h2>
    <div class="row"><span>Client:</span> <span class="pill gray" id="mode">…</span></div>
    <div class="row" style="margin-top:6px"><span>Auto-reply switch:</span> <span class="pill gray" id="switch">…</span></div>
    <div class="muted" style="margin-top:6px">Auto-reply is switched in Reply Desk (the main page).</div>
    <h2>Controls</h2>
    <div class="controls">
      <fieldset>
        <legend>Sending</legend>
        <label><input type="radio" name="send-mode" id="mode-dry" value="dry"> Dry run</label>
        <label><input type="radio" name="send-mode" id="mode-live" value="live"> Live</label>
      </fieldset>
      <div class="confirm-box" id="live-confirm" hidden>
        <strong>Go live?</strong> The WhatsApp client will send real messages to your contacts.
        <div class="row">
          <button class="btn" id="live-yes" type="button">Go live</button>
          <button class="btn" id="live-no" type="button">Stay in dry run</button>
        </div>
      </div>
      <div class="field">
        <label for="min-delay">Min delay before replying (s)</label><input type="number" id="min-delay" min="0.5" step="0.5">
        <label for="max-delay">Max delay before replying (s)</label><input type="number" id="max-delay" min="0.5" step="0.5">
      </div>
      <div><button class="btn" id="save-delays" type="button">Save delays</button></div>
      <div class="field-error" id="delay-error" role="alert"></div>
      <button class="btn btn-kill" id="kill" type="button">🛑 KILL SWITCH</button>
      <div class="muted" id="control-msg" role="status"></div>
    </div>
    <h2>Totals</h2>
    <div class="metric"><div class="value" id="processed">–</div><div class="label">messages processed</div></div>
    <div class="metric"><div class="value" id="replies">–</div><div class="label" id="replies-label">replies sent</div></div>
    <div class="metric"><div class="value" id="awaiting">–</div><div class="label">waiting for approval</div></div>
  </aside>
  <main>
    <div class="muted" style="margin-bottom:10px">Last __FEED_LIMIT__ decisions, newest first · refreshes every 2 s</div>
    <div id="feed"><div class="empty">waiting for first message...</div></div>
  </main>
</div>
<script>
const BADGES = { family: "green", friends: "blue", professional: "purple", girlfriend: "pink",
                 business_freelance: "amber", unknown: "gray", group: "gray", broadcast: "gray", newsletter: "gray" };
const DECISIONS = { reply: ["reply", "green"], needs_approval: ["needs approval", "amber"],
                    ignore: ["ignore", "gray"], error: ["error", "red"] };
const openTraces = new Set();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);  // text only, never parsed as HTML: message text is untrusted
  return node;
}

function formatTime(iso) {
  const date = new Date(iso);
  return isNaN(date) ? (iso || "") : date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function card(record) {
  const key = (record.timestamp || "") + "|" + (record.jid || "");
  const box = el("div", "card");
  const row = el("div", "row");
  row.append(el("span", "time", formatTime(record.timestamp)));
  const relationship = record.relationship || "unknown";
  row.append(el("span", "pill " + (BADGES[relationship] || "gray"), relationship));
  const [label, color] = DECISIONS[record.decision] || [record.decision || "?", "gray"];
  row.append(el("span", "pill " + color, label));
  box.append(row);
  box.append(el("div", "reason", record.reason || ""));
  if (record.reply) box.append(el("div", "reply", record.reply));

  const trace = Array.isArray(record.retrieval_trace) ? record.retrieval_trace : [];
  if (trace.length) {
    const details = el("details");
    details.open = openTraces.has(key);
    details.addEventListener("toggle", () => details.open ? openTraces.add(key) : openTraces.delete(key));
    details.append(el("summary", null, "retrieval trace (" + trace.length + " similar past messages)"));
    for (const pair of trace) {
      const item = el("div", "pair");
      item.append(el("b", null, "They: "), document.createTextNode(pair.their_message || ""), document.createElement("br"));
      item.append(el("b", null, "You: "), document.createTextNode(pair.my_reply || ""));
      if (typeof pair.distance === "number") item.append(el("span", "muted", "  · distance " + pair.distance.toFixed(3)));
      details.append(item);
    }
    box.append(details);
  }
  return box;
}

async function refresh() {
  try {
    const response = await fetch("/api/feed", { cache: "no-store" });
    const data = await response.json();
    const mode = (data.mode || "unknown").toUpperCase();
    const modePill = document.getElementById("mode");
    modePill.textContent = mode;
    modePill.className = "pill " + (mode === "LIVE" ? "red" : mode === "DRY_RUN" ? "amber" : "gray");
    const switchPill = document.getElementById("switch");
    switchPill.textContent = data.auto_reply_enabled ? "ON" : "OFF";
    switchPill.className = "pill " + (data.auto_reply_enabled ? "green" : "gray");
    document.getElementById("processed").textContent = data.processed;
    document.getElementById("replies").textContent = data.replies_sent;
    document.getElementById("replies-label").textContent = mode === "LIVE" ? "replies sent" : "replies sent (would-have-sent, dry run)";
    document.getElementById("awaiting").textContent = data.awaiting_approval;
    if (data.controls) applyControls(data.controls);

    const feed = document.getElementById("feed");
    feed.replaceChildren(...(data.waiting
      ? [el("div", "empty", "waiting for first message...")]
      : data.records.map(card)));
    document.getElementById("updated").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (error) {
    document.getElementById("updated").textContent = "console API unreachable — retrying…";
  }
}

// ---------- controls: write config/settings.json and kill_switch.flag through the API ----------
const $ = (id) => document.getElementById(id);
let delaysDirty = false;
let liveConfirming = false;

async function post(path, body) {
  const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Reply-Desk": "1" }, body: JSON.stringify(body) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail;
    throw new Error(detail || ("request failed (" + response.status + ")"));
  }
  return data;
}

function note(text) {
  $("control-msg").textContent = text;
}

function applyControls(c) {
  if (!liveConfirming) {
    $("mode-dry").checked = c.dry_run !== false;
    $("mode-live").checked = c.dry_run === false;
  }
  const editing = document.activeElement === $("min-delay") || document.activeElement === $("max-delay");
  if (!delaysDirty && !editing) {
    $("min-delay").value = c.min_delay_seconds;
    $("max-delay").value = c.max_delay_seconds;
  }
  $("kill-banner").hidden = !c.kill_switch_active;
  $("kill").disabled = c.kill_switch_active;
  $("kill").textContent = c.kill_switch_active ? "🛑 KILL SWITCH ACTIVE" : "🛑 KILL SWITCH";
}

async function setDryRun(dryRun) {
  try {
    applyControls(await post("/api/settings", { dry_run: dryRun }));
    note(dryRun ? "Dry run on: nothing is sent, only logged." : "Live: the client now sends real replies.");
    refresh();
  } catch (error) {
    note("Couldn't change sending mode: " + error.message);
  }
}

$("mode-dry").addEventListener("change", () => { liveConfirming = false; $("live-confirm").hidden = true; setDryRun(true); });
$("mode-live").addEventListener("change", () => {
  liveConfirming = true;
  $("mode-dry").checked = true;  // stay in dry run until confirmed
  $("live-confirm").hidden = false;
  $("live-yes").focus();
});
$("live-yes").addEventListener("click", () => { liveConfirming = false; $("live-confirm").hidden = true; setDryRun(false); });
$("live-no").addEventListener("click", () => { liveConfirming = false; $("live-confirm").hidden = true; note("Still in dry run."); });

for (const id of ["min-delay", "max-delay"]) $(id).addEventListener("input", () => { delaysDirty = true; $("delay-error").textContent = ""; });
$("save-delays").addEventListener("click", async () => {
  const low = Number($("min-delay").value);
  const high = Number($("max-delay").value);
  if (!Number.isFinite(low) || !Number.isFinite(high) || low <= 0 || high <= 0) {
    $("delay-error").textContent = "Both delays must be positive numbers of seconds.";
    return;
  }
  if (low >= high) {
    $("delay-error").textContent = "The minimum delay must be less than the maximum.";
    return;
  }
  try {
    const c = await post("/api/settings", { min_delay_seconds: low, max_delay_seconds: high });
    delaysDirty = false;
    applyControls(c);
    note("Delays saved: " + c.min_delay_seconds + "–" + c.max_delay_seconds + " s before each reply.");
  } catch (error) {
    $("delay-error").textContent = error.message;
  }
});

$("kill").addEventListener("click", async () => {
  try { applyControls(await post("/api/kill-switch", { active: true })); note("Kill switch on."); }
  catch (error) { note("Couldn't turn on the kill switch: " + error.message); }
});
$("clear-kill").addEventListener("click", async () => {
  try { applyControls(await post("/api/kill-switch", { active: false })); note("Kill switch cleared."); }
  catch (error) { note("Couldn't clear the kill switch: " + error.message); }
});

refresh();
setInterval(refresh, __REFRESH_MS__);
</script>
</body>
</html>
"""


if FRONTEND_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, reload=False, log_level="warning")

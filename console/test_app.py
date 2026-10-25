"""Tests for console/app.py. Run: python3 -m pytest console/test_app.py -v"""

import json

import pytest
from fastapi.testclient import TestClient

from agent import approvals, settings
from config import settings as client_settings
from console import app as console

BASE = "http://127.0.0.1:5002"
WRITE = {"X-Reply-Desk": "1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "FEED_PATH", tmp_path / "logs" / "console_feed.jsonl")
    monkeypatch.setattr(client_settings, "SETTINGS_PATH", tmp_path / "settings.json")  # never the real file
    monkeypatch.setattr(client_settings, "KILL_SWITCH_PATH", tmp_path / "kill_switch.flag")
    monkeypatch.setattr(approvals, "DB_PATH", tmp_path / "state.sqlite3")
    monkeypatch.setattr(settings, "AUTO_REPLY_PATH", tmp_path / "auto_reply.json")
    contacts = tmp_path / "contacts.md"
    contacts.write_text("| Name | Phone(s) | Relationship |\n|---|---|---|\n| Sarath SSN | +919000000001 | friend |\n")
    monkeypatch.setattr(console, "CONTACTS_PATH", contacts)
    monkeypatch.setattr(console, "load_contacts", lambda: [("Sarath SSN", ["919000000001"])])
    console._names_by_phone.cache_clear()
    return TestClient(console.app, base_url=BASE)


def write_feed(path, records, extra_lines=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record) for record in records] + list(extra_lines)
    path.write_text("\n".join(lines) + "\n")


def record(i, decision="ignore", relationship="friends", reply=None, trace=None):
    return {"timestamp": f"2026-10-22T10:00:{i:02d}+00:00", "jid": f"9100000000{i:02d}@s.whatsapp.net",
            "relationship": relationship, "decision": decision, "reason": f"reason {i}", "reply": reply,
            "retrieval_trace": trace or []}


def test_missing_feed_shows_waiting(client):
    data = client.get("/api/feed").json()
    assert data["waiting"] is True and data["records"] == [] and data["processed"] == 0
    assert "waiting for first message..." in client.get("/basic").text
    assert "waiting for first message..." in client.get("/").text


def test_last_20_newest_first_with_totals(client):
    records = [record(i, decision="reply" if i % 5 == 0 else "needs_approval" if i % 3 == 0 else "ignore") for i in range(30)]
    write_feed(console.FEED_PATH, records)
    data = client.get("/api/feed").json()
    assert data["waiting"] is False
    assert [r["reason"] for r in data["records"]] == [f"reason {i}" for i in range(29, 9, -1)]
    assert data["processed"] == 30
    assert data["replies_sent"] == sum(r["decision"] == "reply" for r in records)
    assert data["awaiting_approval"] == sum(r["decision"] == "needs_approval" for r in records)


def test_malformed_lines_are_skipped(client):
    write_feed(console.FEED_PATH, [record(1), record(2, decision="reply")], extra_lines=["{not json", "[1, 2]", ""])
    data = client.get("/api/feed").json()
    assert data["processed"] == 2 and data["replies_sent"] == 1


def test_empty_feed_file_is_still_waiting(client):
    console.FEED_PATH.parent.mkdir(parents=True)
    console.FEED_PATH.write_text("")
    assert client.get("/api/feed").json()["waiting"] is True


@pytest.mark.parametrize("content, expected", [('{"dry_run": true}', "DRY_RUN"), ('{"dry_run": false}', "LIVE"), ("{broken", "DRY_RUN"), (None, "DRY_RUN")])
def test_mode_follows_settings_json(client, content, expected):
    if content is not None:
        client_settings.SETTINGS_PATH.write_text(content)
    assert client.get("/api/feed").json()["mode"] == expected


def test_trace_and_reply_are_passed_through(client):
    trace = [{"their_message": "Lunch", "my_reply": "Kelambu", "distance": 0.11}]
    write_feed(console.FEED_PATH, [record(1, decision="reply", relationship="girlfriend", reply="Saapten da", trace=trace)])
    newest = client.get("/api/feed").json()["records"][0]
    assert newest["reply"] == "Saapten da" and newest["retrieval_trace"] == trace


def test_page_refreshes_every_2_seconds_and_never_uses_innerhtml_for_data(client):
    html = client.get("/basic").text
    assert "setInterval(refresh, 2000)" in html
    assert "innerHTML" not in html
    for relationship, color in [("family", "green"), ("friends", "blue"), ("professional", "purple"), ("group", "gray"), ("unknown", "gray")]:
        assert f'{relationship}: "{color}"' in html


# --- Reply Desk API ------------------------------------------------------------------------

def test_frontend_and_assets_are_served(client):
    assert "<title>Reply Desk</title>" in client.get("/").text
    script = client.get("/assets/app.js").text
    assert "const POLL_MS = 2000" in script and "innerHTML" not in script
    assert client.get("/assets/styles.css").status_code == 200


def test_feed_has_contact_names_and_approval_status(client):
    approval_id = approvals.add_pending("919000000001@s.whatsapp.net", "919000000001@s.whatsapp.net", "friends", "Free a?", "Paakalam da", "needs approval: plan")
    write_feed(console.FEED_PATH, [
        {**record(1, decision="needs_approval"), "jid": "919000000001@s.whatsapp.net", "approval_id": approval_id},
        {**record(2), "jid": "120363041234567890@g.us", "relationship": "group"},
        {**record(3), "jid": "15550000000@s.whatsapp.net", "relationship": "unknown"},
    ])
    data = client.get("/api/feed").json()
    names = {r["jid"]: r["contact_name"] for r in data["records"]}
    assert names == {"919000000001@s.whatsapp.net": "Sarath SSN", "120363041234567890@g.us": "Group chat",
                     "15550000000@s.whatsapp.net": "+15550000000"}
    held = next(r for r in data["records"] if r.get("approval_id") == approval_id)
    assert held["approval_status"] == "pending" and data["pending_approvals"] == 1


def test_approve_with_edit_and_dismiss(client):
    first = approvals.add_pending("919000000001@s.whatsapp.net", "919000000001@s.whatsapp.net", "friends", "Free a?", "Paakalam da", "r")
    second = approvals.add_pending("919000000001@s.whatsapp.net", "919000000001@s.whatsapp.net", "friends", "Movie?", "Paakalam", "r")
    pending = client.get("/api/approvals?status=pending").json()
    assert [row["id"] for row in pending] == [second, first] and pending[0]["contact_name"] == "Sarath SSN"

    approved = client.post(f"/api/approvals/{first}/approve", json={"text": "Paakalam da, will confirm"}, headers=WRITE).json()
    assert approved["status"] == "approved" and approved["final_text"] == "Paakalam da, will confirm"
    assert approvals.outbox()[0]["id"] == first
    assert client.post(f"/api/approvals/{second}/dismiss", json={}, headers=WRITE).json()["status"] == "dismissed"
    assert client.post(f"/api/approvals/{second}/approve", json={}, headers=WRITE).status_code == 409
    assert client.post("/api/approvals/999/dismiss", json={}, headers=WRITE).status_code == 404
    assert client.post(f"/api/approvals/{first}/approve", json={"text": " "}, headers=WRITE).status_code == 409


def test_auto_reply_switch_writes_config(client):
    assert client.post("/api/auto-reply", json={"enabled": True}, headers=WRITE).json() == {"auto_reply_enabled": True}
    assert settings.auto_reply_enabled() is True
    assert client.get("/api/feed").json()["auto_reply_enabled"] is True
    assert client.post("/api/auto-reply", json={"enabled": False}, headers=WRITE).json() == {"auto_reply_enabled": False}
    for not_a_bool in ["yes", "true", 1, None]:
        assert client.post("/api/auto-reply", json={"enabled": not_a_bool}, headers=WRITE).status_code == 422
    assert settings.auto_reply_enabled() is False


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Reply-Desk": "0"}, {"X-Reply-Desk": "1", "Origin": "https://evil.example"}, {"Origin": "http://127.0.0.1:5002"}],
)
def test_writes_from_other_sites_are_refused(client, headers):
    response = client.post("/api/auto-reply", json={"enabled": True}, headers=headers)
    assert response.status_code == 403
    assert settings.auto_reply_enabled() is False


def test_same_page_origin_is_allowed(client):
    headers = {**WRITE, "Origin": "http://127.0.0.1:5002"}
    assert client.post("/api/auto-reply", json={"enabled": False}, headers=headers).status_code == 200


def test_unknown_host_is_refused():
    response = TestClient(console.app, base_url="http://attacker.example:5002").get("/api/feed")
    assert response.status_code == 403


# --- Remote access over a private network (REPLY_DESK_ALLOWED_HOSTS) ----------------------

TAILNET = "my-mac.tail1234.ts.net"


def remote(base=f"https://{TAILNET}"):
    return TestClient(console.app, base_url=base)


def test_tailnet_host_is_refused_until_listed(client, monkeypatch):
    monkeypatch.delenv(console.EXTRA_HOSTS_ENV, raising=False)
    assert remote().get("/api/feed").status_code == 403
    monkeypatch.setenv(console.EXTRA_HOSTS_ENV, f" {TAILNET.upper()} , other.example.ts.net")
    assert remote().get("/api/feed").status_code == 200


def test_listed_host_can_write_only_from_its_https_origin(client, monkeypatch):
    monkeypatch.setenv(console.EXTRA_HOSTS_ENV, TAILNET)
    ok = remote().post("/api/auto-reply", json={"enabled": False}, headers={**WRITE, "Origin": f"https://{TAILNET}"})
    assert ok.status_code == 200
    for origin in (f"http://{TAILNET}", "https://evil.example", f"https://{TAILNET}.evil.example"):
        refused = remote().post("/api/auto-reply", json={"enabled": True}, headers={**WRITE, "Origin": origin})
        assert refused.status_code == 403
    assert remote().post("/api/auto-reply", json={"enabled": True}).status_code == 403  # no X-Reply-Desk
    assert settings.auto_reply_enabled() is False


@pytest.mark.parametrize("value", ["*", "*.ts.net", "localhost", "my-mac.ts.net:8080", "https://my-mac.ts.net", "my mac.ts.net"])
def test_malformed_extra_hosts_are_ignored(monkeypatch, value):
    monkeypatch.setenv(console.EXTRA_HOSTS_ENV, value)
    assert console.extra_hosts() == set()


def test_unlisted_hosts_stay_refused_when_extras_are_set(monkeypatch):
    monkeypatch.setenv(console.EXTRA_HOSTS_ENV, TAILNET)
    assert TestClient(console.app, base_url="http://attacker.example:5002").get("/api/feed").status_code == 403
    assert remote(f"https://attacker.{TAILNET}").get("/api/feed").status_code == 403


# --- Client controls: settings.json and kill switch ---------------------------------------

def test_controls_are_reported_with_safe_defaults(client):
    controls = client.get("/api/feed").json()["controls"]
    assert controls == {"dry_run": True, "min_delay_seconds": 3, "max_delay_seconds": 12, "kill_switch_active": False}
    assert client.get("/api/settings").json() == controls


def test_toggle_dry_run_keeps_the_delays(client):
    client_settings.SETTINGS_PATH.write_text('{"dry_run": true, "min_delay_seconds": 4, "max_delay_seconds": 9, "extra": 1}')
    assert client.post("/api/settings", json={"dry_run": False}, headers=WRITE).json()["dry_run"] is False
    stored = json.loads(client_settings.SETTINGS_PATH.read_text())
    assert stored == {"dry_run": False, "min_delay_seconds": 4, "max_delay_seconds": 9, "extra": 1}
    assert client.get("/api/feed").json()["mode"] == "LIVE"


def test_delays_save_and_keep_dry_run(client):
    client.post("/api/settings", json={"dry_run": False}, headers=WRITE)
    body = client.post("/api/settings", json={"min_delay_seconds": 2.5, "max_delay_seconds": 7}, headers=WRITE).json()
    assert (body["min_delay_seconds"], body["max_delay_seconds"], body["dry_run"]) == (2.5, 7, False)


@pytest.mark.parametrize(
    "body",
    [
        {"min_delay_seconds": 8, "max_delay_seconds": 8},
        {"min_delay_seconds": 9, "max_delay_seconds": 2},
        {"min_delay_seconds": 0, "max_delay_seconds": 5},
        {"min_delay_seconds": -1, "max_delay_seconds": 5},
        {"max_delay_seconds": 2},  # below the current minimum of 3
        {"min_delay_seconds": "3", "max_delay_seconds": 12},
        {"min_delay_seconds": True, "max_delay_seconds": 12},
        {"dry_run": "false"},
        {"dry_run": 0},
        {},
    ],
)
def test_invalid_settings_are_rejected_and_nothing_is_written(client, body):
    assert client.post("/api/settings", json=body, headers=WRITE).status_code == 422
    assert not client_settings.SETTINGS_PATH.exists()
    assert client_settings.load_settings() == client_settings.DEFAULT_SETTINGS


def test_kill_switch_on_and_clear(client):
    on = client.post("/api/kill-switch", json={"active": True}, headers=WRITE).json()
    assert on["kill_switch_active"] is True and client_settings.KILL_SWITCH_PATH.exists()
    assert client_settings.KILL_SWITCH_PATH.read_bytes() == b""
    assert client.get("/api/feed").json()["controls"]["kill_switch_active"] is True
    off = client.post("/api/kill-switch", json={"active": False}, headers=WRITE).json()
    assert off["kill_switch_active"] is False and not client_settings.KILL_SWITCH_PATH.exists()
    assert client.post("/api/kill-switch", json={"active": "yes"}, headers=WRITE).status_code == 422


@pytest.mark.parametrize("path, body", [("/api/settings", {"dry_run": False}), ("/api/kill-switch", {"active": True})])
def test_control_writes_from_other_sites_are_refused(client, path, body):
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, json=body, headers={**WRITE, "Origin": "https://evil.example"}).status_code == 403
    assert client_settings.is_dry_run() is True and not client_settings.KILL_SWITCH_PATH.exists()


def test_pages_have_the_controls(client):
    basic = client.get("/basic").text
    assert "🛑 KILL SWITCH" in basic and "Clear kill switch" in basic and 'id="min-delay"' in basic and 'id="mode-live"' in basic
    desk = client.get("/").text
    assert "🛑 Kill switch" in desk and "Clear kill switch" in desk and 'id="live-switch"' in desk and 'id="max-delay"' in desk

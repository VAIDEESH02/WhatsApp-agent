"""Tests for agent.bridge (pipeline and retrieval stubbed; real router and map files).

Run: python3 -m pytest agent/test_bridge.py -v
"""

import json

import pytest
from fastapi.testclient import TestClient

from agent import approvals, bridge, settings


def payload(**overrides):
    body = {"jid": "15550000000@s.whatsapp.net", "text": "Lunch saaptiya?", "message_type": "text",
            "is_forwarded": False, "from_me": False}
    return {**body, **overrides}


@pytest.fixture
def switch(tmp_path, monkeypatch):
    """Point the auto-reply switch at a temp file; returns a setter (None deletes the file)."""
    path = tmp_path / "auto_reply.json"
    monkeypatch.setattr(settings, "AUTO_REPLY_PATH", path)

    def set_switch(value):
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(value if isinstance(value, str) else json.dumps({"auto_reply_enabled": value}))

    set_switch(True)
    return set_switch


@pytest.fixture
def client(tmp_path, monkeypatch, switch):
    monkeypatch.setattr(approvals, "DB_PATH", tmp_path / "agent_state.sqlite3")  # never the real queue
    monkeypatch.setattr(bridge, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(bridge, "CONSOLE_FEED_PATH", tmp_path / "logs" / "console_feed.jsonl")
    monkeypatch.setattr(bridge, "retrieve_similar", lambda rel, text, k: [{"their_message": "Lunch", "my_reply": "Kelambu", "distance": 0.1}])
    return TestClient(bridge.app)


def feed(tmp_path):
    return [json.loads(line) for line in (tmp_path / "logs" / "console_feed.jsonl").read_text().splitlines()]


def stub_pipeline(monkeypatch, status, draft, reasons):
    calls = []

    def fake(message, relationship):
        calls.append((message, relationship))
        return {"status": status, "draft": draft, "reasons": reasons}

    monkeypatch.setattr(bridge, "prepare_reply", fake)
    return calls


def test_ready_draft_is_sent(client, monkeypatch, tmp_path):
    calls = stub_pipeline(monkeypatch, "ready", "Saapten da, nee?", ["passed all gates", "draft check: no commitment found"])
    response = client.post("/process", json=payload())
    assert response.status_code == 200
    assert response.json() == {
        "should_reply": True, "reply": "Saapten da, nee?", "relationship": "unknown",
        "reason": "passed all gates | draft check: no commitment found", "status": "ready", "needs_approval": False,
        "auto_reply_enabled": True, "approval_id": None,
    }
    assert calls[0][0]["text"] == "Lunch saaptiya?"
    record = feed(tmp_path)[0]
    assert set(record) == {"timestamp", "jid", "text", "relationship", "decision", "reason", "reply",
                           "approval_id", "retrieval_trace"}
    assert record["text"] == "Lunch saaptiya?"
    assert record["decision"] == "reply" and record["retrieval_trace"][0]["my_reply"] == "Kelambu"


def test_held_draft_is_returned_but_not_sendable(client, monkeypatch, tmp_path):
    stub_pipeline(monkeypatch, "needs_approval", "Paakalam da", ["needs approval: asks for a commitment (plan, invite, call, or time)", "draft check: no commitment found"])
    body = client.post("/process", json=payload(text="Dinner ku varuviya?")).json()
    assert body["should_reply"] is False and body["needs_approval"] is True and body["reply"] == "Paakalam da"
    assert feed(tmp_path)[0]["decision"] == "needs_approval"


def test_ignored_message(client, monkeypatch, tmp_path):
    stub_pipeline(monkeypatch, "ignored", None, ["own message"])
    body = client.post("/process", json=payload(from_me=True)).json()
    assert body == {"should_reply": False, "reply": None, "relationship": "unknown", "reason": "own message",
                    "status": "ignored", "needs_approval": False, "auto_reply_enabled": True, "approval_id": None}
    assert feed(tmp_path)[0]["decision"] == "ignore"


def test_group_jid_is_ignored_and_never_retrieved(client, monkeypatch, tmp_path):
    def no_retrieval(*args):
        raise AssertionError("group chats must never be retrieved")

    monkeypatch.setattr(bridge, "retrieve_similar", no_retrieval)
    body = client.post("/process", json=payload(jid="120363041234567890@g.us", text="Hi all")).json()
    assert body["should_reply"] is False and body["relationship"] == "group" and body["reply"] is None
    assert feed(tmp_path)[0]["retrieval_trace"] == []


def test_missing_field_is_rejected(client):
    body = payload()
    del body["from_me"]
    assert client.post("/process", json=body).status_code == 422


def test_pipeline_error_returns_500_and_is_logged(client, monkeypatch, tmp_path):
    def boom(message, relationship):
        raise ValueError("relationship_map.json is not valid JSON")

    monkeypatch.setattr(bridge, "prepare_reply", boom)
    response = client.post("/process", json=payload())
    assert response.status_code == 500 and "not valid JSON" in response.json()["detail"]
    record = feed(tmp_path)[0]
    assert record["decision"] == "error" and record["reply"] is None


# --- Auto-reply switch -------------------------------------------------------------------

READY = ("ready", "Saapten da, nee?", ["passed all gates", "draft check: no commitment found"])


@pytest.mark.parametrize("state", [False, None, "{not json", '{"auto_reply_enabled": "true"}', '{"auto_reply_enabled": 1}', "[]"])
def test_switch_off_missing_or_invalid_holds_even_ready_drafts(client, monkeypatch, switch, tmp_path, state):
    switch(state)
    stub_pipeline(monkeypatch, *READY)
    body = client.post("/process", json=payload()).json()
    assert body["should_reply"] is False and body["auto_reply_enabled"] is False
    assert body["status"] == "needs_approval" and body["needs_approval"] is True
    assert body["reply"] == "Saapten da, nee?" and body["reason"].endswith("auto-reply switch is off")
    assert feed(tmp_path)[0]["decision"] == "needs_approval"


def test_switch_on_sends_ready_drafts_only(client, monkeypatch, switch):
    switch(True)
    stub_pipeline(monkeypatch, *READY)
    assert client.post("/process", json=payload()).json()["should_reply"] is True
    stub_pipeline(monkeypatch, "needs_approval", "Paakalam da", ["needs approval: asks for a commitment (plan, invite, call, or time)"])
    assert client.post("/process", json=payload(text="Dinner ku varuviya?")).json()["should_reply"] is False


def test_switch_is_read_on_every_request(client, monkeypatch, switch):
    stub_pipeline(monkeypatch, *READY)
    switch(True)
    assert client.post("/process", json=payload()).json()["should_reply"] is True
    switch(False)
    assert client.post("/process", json=payload()).json()["should_reply"] is False


@pytest.mark.parametrize("jid, text", [("120363041234567890@g.us", "Hi all"), ("15550000000@s.whatsapp.net", "Hi, is this Vaideesh?")])
def test_switch_on_never_auto_replies_to_groups_or_unknown_senders(client, switch, monkeypatch, jid, text):
    """Real pipeline (no stub): the hard rules stop these before any LLM call."""
    switch(True)
    from agent import decision_engine

    monkeypatch.setattr(decision_engine, "_classify_intent", lambda t: (_ for _ in ()).throw(AssertionError("LLM reached")))
    body = client.post("/process", json=payload(jid=jid, text=text)).json()
    assert body["should_reply"] is False and body["status"] == "ignored" and body["reply"] is None


def test_switch_is_off_until_the_file_says_true(switch):
    # config/auto_reply.json is local state (git-ignored): a fresh clone has none, which means off.
    switch(None)
    assert settings.auto_reply_enabled() is False


def test_persona_rule_describes_auto_send_policy():
    rules = json.loads((bridge.PROJECT_ROOT / "persona" / "persona.json").read_text())["hard_rules"]
    assert not any("Every reply is a draft" in rule for rule in rules)
    assert any(rule.startswith("Auto-send a reply only when the auto-reply switch is on") for rule in rules)


# --- Approval queue and outbox -----------------------------------------------------------

def test_held_draft_becomes_pending_approval_with_chat_jid(client, monkeypatch, tmp_path):
    stub_pipeline(monkeypatch, "needs_approval", "Paakalam da", ["needs approval: asks for a commitment (plan, invite, call, or time)"])
    body = client.post("/process", json=payload(text="Dinner ku varuviya?", chat_jid="123456789012345@lid")).json()
    assert body["approval_id"] is not None
    stored = approvals.get(body["approval_id"])
    assert stored["status"] == "pending" and stored["draft"] == "Paakalam da"
    assert stored["chat_jid"] == "123456789012345@lid" and stored["incoming_text"] == "Dinner ku varuviya?"
    assert feed(tmp_path)[0]["approval_id"] == body["approval_id"]


def test_ignored_and_ready_messages_create_no_approval(client, monkeypatch):
    stub_pipeline(monkeypatch, "ignored", None, ["own message"])
    client.post("/process", json=payload(from_me=True))
    stub_pipeline(monkeypatch, *READY)
    client.post("/process", json=payload())
    assert approvals.list_approvals() == []


def test_only_approved_drafts_reach_the_outbox(client, monkeypatch):
    stub_pipeline(monkeypatch, "needs_approval", "Paakalam da", ["needs approval: x"])
    first = client.post("/process", json=payload()).json()["approval_id"]
    second = client.post("/process", json=payload()).json()["approval_id"]
    assert client.get("/outbox").json() == []
    approvals.approve(first, "Paakalam da, will confirm by evening")
    approvals.dismiss(second)
    assert client.get("/outbox").json() == [
        {"id": first, "chat_jid": "15550000000@s.whatsapp.net", "jid": "15550000000@s.whatsapp.net",
         "text": "Paakalam da, will confirm by evening"}
    ]
    assert client.post(f"/outbox/{first}/sent", json={"dry_run": True}).json()["status"] == "dry_run"
    assert client.get("/outbox").json() == []
    assert client.post(f"/outbox/{first}/sent", json={"dry_run": False}).status_code == 409


# ---------- recent conversation ----------

RECENT = [{"from_me": False, "text": "Machan saaptiya?"}, {"from_me": True, "text": "Saapten da"}]


def test_recent_messages_are_optional_and_reach_the_pipeline(client, monkeypatch):
    calls = stub_pipeline(monkeypatch, "ready", "Saapten da, nee?", ["passed all gates"])
    assert client.post("/process", json=payload()).status_code == 200
    assert client.post("/process", json=payload(text="Nee?", recent_messages=RECENT)).status_code == 200
    assert calls[0][0]["recent_messages"] == []
    assert calls[1][0]["recent_messages"] == RECENT


def test_recent_messages_are_never_written_to_the_console_feed(client, monkeypatch, tmp_path):
    stub_pipeline(monkeypatch, "ready", "Saapten da, nee?", ["passed all gates"])
    client.post("/process", json=payload(text="Nee?", recent_messages=RECENT))
    raw = (tmp_path / "logs" / "console_feed.jsonl").read_text()
    assert "Machan saaptiya?" not in raw and "recent_messages" not in raw


@pytest.mark.parametrize(
    "recent",
    [
        [{"from_me": False, "text": "hi"}] * (bridge.MAX_RECENT_MESSAGES + 1),
        [{"from_me": False, "text": "x" * (bridge.MAX_RECENT_CHARS + 1)}],
        [{"from_me": "yes", "text": "hi"}],
        [{"text": "hi"}],
    ],
)
def test_bad_recent_messages_are_rejected(client, monkeypatch, recent):
    calls = stub_pipeline(monkeypatch, "ready", "x", ["passed all gates"])
    assert client.post("/process", json=payload(recent_messages=recent)).status_code == 422
    assert calls == []

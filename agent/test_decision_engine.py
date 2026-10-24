"""Tests for agent.decision_engine. The LLM call is stubbed, so these run offline.

Run: python3 -m pytest agent/test_decision_engine.py -v
"""

import json

import pytest

from agent import decision_engine as engine


def message(text="Free a? Call pannalama", **overrides):
    return {"from_me": False, "text": text, "message_type": "text", "is_forwarded": False, **overrides}


@pytest.fixture(autouse=True)
def isolated_log(tmp_path, monkeypatch):
    """Write decisions to a temp log, and fail any test that reaches Gemini unexpectedly."""
    monkeypatch.setattr(engine, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(engine, "LOG_PATH", tmp_path / "logs" / "decision_log.jsonl")

    def no_llm(text):
        raise AssertionError("LLM called when an earlier layer should have decided")

    monkeypatch.setattr(engine, "_classify_intent", no_llm)
    return tmp_path / "logs" / "decision_log.jsonl"


def stub_llm(monkeypatch, response):
    calls = []

    def fake(text):
        calls.append(text)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(engine, "_classify_intent", fake)
    return calls


# 1. Hard rules
@pytest.mark.parametrize(
    "msg, relationship, reason",
    [
        (message(from_me=True), "friends", "own message"),
        (message(), "group", "group chat, not allowlisted"),
        (message(), "broadcast", "broadcast chat, not allowlisted"),
        (message(), "unknown", "sender not in allowlist"),
        (message(), "REPLACE_ME", "relationship 'REPLACE_ME' not recognised"),
        ({"text": "hi"}, "friends", "malformed message"),
    ],
)
def test_hard_rules_ignore_without_llm(msg, relationship, reason):
    assert engine.should_reply(msg, relationship) == (False, reason)


def test_own_message_beats_everything(monkeypatch):
    calls = stub_llm(monkeypatch, engine.SAFE)
    assert engine.should_reply(message(from_me=True), "girlfriend") == (False, "own message")
    assert calls == []


# 2. Signal rules
@pytest.mark.parametrize("media", ["image", "audio", "video"])
def test_media_without_caption_is_ignored(media):
    assert engine.should_reply(message("", message_type=media), "family") == (
        False,
        "media-only, no text to ground a reply",
    )


def test_forwarded_is_ignored():
    assert engine.should_reply(message(is_forwarded=True), "friends") == (False, "forwarded content, not a real question")


@pytest.mark.parametrize("ack", ["ok", "Okay!", "  K  ", "kk...", "Thank you!!", "hmm…", "COOL", "¿nice?"])
def test_one_word_acks_are_ignored(ack):
    assert engine.should_reply(message(ack), "friends") == (False, "low-signal ack, no reply needed")


def test_empty_text_is_ignored():
    assert engine.should_reply(message("   "), "friends") == (False, "empty message")


def test_media_with_caption_reaches_intent_check(monkeypatch):
    calls = stub_llm(monkeypatch, engine.SAFE)
    assert engine.should_reply(message("Look at this view!", message_type="image"), "girlfriend") == (True, "passed all gates")
    assert calls == ["Look at this view!"]


# 3. Intent check — fails closed
def test_safe_label_replies(monkeypatch):
    stub_llm(monkeypatch, "safe_to_auto_reply")
    assert engine.should_reply(message(), "friends") == (True, "passed all gates")


def test_needs_human_label_ignores(monkeypatch):
    stub_llm(monkeypatch, "needs_human_money_or_serious")
    assert engine.should_reply(message("Can you lend me 5k?"), "friends") == (False, "needs human: money or serious topic")


def test_label_with_surrounding_whitespace_is_accepted(monkeypatch):
    stub_llm(monkeypatch, "  safe_to_auto_reply\n")
    assert engine.should_reply(message(), "friends") == (True, "passed all gates")


@pytest.mark.parametrize(
    "bad",
    [
        "SAFE_TO_AUTO_REPLY",                       # wrong casing
        "safe_to_auto_reply.",                      # trailing punctuation
        "Label: safe_to_auto_reply",                # extra text
        "safe_to_auto_reply\nneeds_human_money_or_serious",
        "safe",                                     # partial
        "",                                         # empty
        None,                                       # no text (e.g. blocked response)
        RuntimeError("503 UNAVAILABLE"),            # API error
    ],
)
def test_unexpected_intent_response_fails_closed(monkeypatch, bad, caplog):
    stub_llm(monkeypatch, bad)
    reply, reason = engine.should_reply(message(), "friends")
    assert reply is False
    assert reason.startswith("intent check failed closed")
    assert "Intent check failed closed; raw response" in caplog.text


def test_check_intent_never_returns_safe_for_garbage(monkeypatch):
    for bad in ["safe_to_auto_reply!", "yes", "Safe_to_auto_reply", None]:
        stub_llm(monkeypatch, bad)
        assert engine.check_intent("hi")[0] == engine.NEEDS_HUMAN


# Logging
def test_every_decision_is_logged_as_json_line(monkeypatch, isolated_log):
    engine.should_reply(message(from_me=True), "friends")
    stub_llm(monkeypatch, engine.SAFE)
    engine.should_reply(message("Naalaiki free a?"), "friends")

    records = [json.loads(line) for line in isolated_log.read_text(encoding="utf-8").splitlines()]
    assert [r["decision"] for r in records] == ["ignore", "reply"]
    assert records[1]["text"] == "Naalaiki free a?"
    assert records[1]["relationship"] == "friends"
    assert records[1]["reason"] == "passed all gates"
    assert all(set(r) == {"timestamp", "text", "relationship", "decision", "reason"} for r in records)


def test_log_directory_is_created(isolated_log):
    assert not isolated_log.parent.exists()
    engine.should_reply(message(from_me=True), "friends")
    assert isolated_log.exists()

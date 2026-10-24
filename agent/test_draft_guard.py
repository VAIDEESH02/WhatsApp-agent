"""Tests for the commitment guard: agent.draft_guard, the decision engine's commitment label,
the generator prompt, and agent.pipeline end to end (LLM calls stubbed, so deterministic).

Run: python3 -m pytest agent/test_draft_guard.py -v
"""

from types import SimpleNamespace

import pytest

from agent import decision_engine, draft_guard, generator, pipeline
from agent.draft_guard import NEEDS_APPROVAL, OK, check_draft


def message(text, **overrides):
    return {"from_me": False, "text": text, "message_type": "text", "is_forwarded": False, **overrides}


@pytest.fixture(autouse=True)
def isolated_log(tmp_path, monkeypatch):
    monkeypatch.setattr(decision_engine, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(decision_engine, "LOG_PATH", tmp_path / "logs" / "decision_log.jsonl")


@pytest.fixture
def stub(monkeypatch):
    """Stub the intent label and the generated draft; record the generator prompt."""

    def install(label, draft):
        prompts = []
        monkeypatch.setattr(decision_engine, "_classify_intent", lambda text: label)

        def fake_generate(model, contents, config=None):
            prompts.append(contents)
            return SimpleNamespace(text=draft, candidates=[])

        monkeypatch.setattr(generator, "get_client", lambda: SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate)))
        monkeypatch.setattr(generator, "retrieve_similar", lambda relationship, text, k: [])
        return prompts

    return install


# --- The four required scenarios -------------------------------------------------------

def test_dinner_invite_is_held_for_approval(stub):
    stub(decision_engine.NEEDS_APPROVAL, "Aama varen!!")
    result = pipeline.prepare_reply(message("Dinner ku varuviya?"), "family")
    assert result["status"] == NEEDS_APPROVAL
    assert result["reasons"][0].startswith("needs approval:")
    assert "agrees or says yes" in result["reasons"][1] and "will come or go" in result["reasons"][1]


def test_pesalam_draft_is_held():
    check = check_draft("Naalaiki pesalam da", "friends")
    assert check.status == NEEDS_APPROVAL
    assert "agrees to talk, call, or meet" in check.reason and "names a time or day" in check.reason


def test_unknown_sender_identity_question_is_held_and_never_confirms_name():
    check = check_draft("Yes, this is Vaideesh.", "unknown")
    assert check.status == NEEDS_APPROVAL
    assert "vaideesh" not in check.draft.lower()
    assert "yes" not in check.draft.lower()
    assert check.draft == draft_guard.UNKNOWN_SENDER_DRAFT


def test_unknown_sender_through_generator_and_check(stub):
    prompts = stub(decision_engine.SAFE, "Yes, this is Vaideesh speaking.")
    draft = generator.generate_reply("Hi, is this Vaideesh?", "unknown")
    check = check_draft(draft, "unknown", fallback=generator.NO_REPLY_FALLBACK)
    assert check.status == NEEDS_APPROVAL and "vaideesh" not in check.draft.lower()
    assert "Never confirm Vaideesh's name or identity" in prompts[0]


def test_ordinary_small_talk_goes_through(stub):
    stub(decision_engine.SAFE, "Saapten da, nee?")
    result = pipeline.prepare_reply(message("Lunch saaptiya?"), "girlfriend")
    assert result == {
        "status": pipeline.READY,
        "draft": "Saapten da, nee?",
        "reasons": ["passed all gates", "draft check: no commitment found"],
    }


def test_work_request_acknowledgement_goes_through(stub):
    prompts = stub(decision_engine.SAFE, "I'll look into it and get back to you")
    result = pipeline.prepare_reply(message("The director wants a slightly warmer look. Can you check?"), "business_freelance")
    assert result["status"] == pipeline.READY
    assert result["draft"] == "I'll look into it and get back to you"
    assert "I'll look into it and get back to you" in prompts[0] and "never add a deadline" in prompts[0]


@pytest.mark.parametrize(
    "draft",
    ["I'll look into it and get back to you by tonight", "Sure, I'll look into it", "Will share it tomorrow"],
)
def test_work_acknowledgement_with_time_or_yes_is_held(draft):
    assert check_draft(draft, "business_freelance").status == NEEDS_APPROVAL


def test_decision_engine_instruction_treats_work_feedback_as_safe():
    assert "work feedback or requests to review, check, or tweak" in decision_engine.INTENT_INSTRUCTION


# --- Layers are independent: either one alone holds the draft ----------------------------

def test_draft_check_holds_even_when_decision_engine_says_safe(stub):
    stub(decision_engine.SAFE, "Ok da, 7 pm ku varen")
    result = pipeline.prepare_reply(message("Evening plan enna?"), "friends")
    assert result["status"] == NEEDS_APPROVAL
    assert result["reasons"][0] == "passed all gates"


def test_decision_engine_holds_even_when_draft_looks_harmless(stub):
    stub(decision_engine.NEEDS_APPROVAL, "Paakalam da")
    result = pipeline.prepare_reply(message("Weekend trip varuviya?"), "friends")
    assert result["status"] == NEEDS_APPROVAL
    assert result["reasons"][1] == "draft check: no commitment found"


def test_commitment_label_is_logged_as_needs_approval(stub, isolated_log, tmp_path):
    import json

    stub(decision_engine.NEEDS_APPROVAL, "Paakalam da")
    assert decision_engine.should_reply(message("Call pannalama?"), "friends") == (
        True,
        "needs approval: asks for a commitment (plan, invite, call, or time)",
    )
    record = json.loads((tmp_path / "logs" / "decision_log.jsonl").read_text().splitlines()[-1])
    assert record["decision"] == "needs_approval"


def test_prompt_tells_model_never_to_give_a_firm_yes(stub):
    prompts = stub(decision_engine.SAFE, "What time are you thinking?")
    generator.generate_reply("Dinner ku varuviya?", "family")
    assert "Never give a firm yes" in prompts[0] and "Paakalam da, will confirm" in prompts[0]


# --- Word-list coverage ----------------------------------------------------------------

@pytest.mark.parametrize(
    "draft",
    [
        "Sure 👍", "Ok da", "Done!", "Will confirm", "Seri", "Kandipa varen", "Varuven",
        "Call pandren", "Naalaiki paapom", "I'll come", "I will call you", "Let's meet",
        "See you", "At 7 then", "8pm works", "Inniki evening", "Reaching by [time]",
    ],
)
def test_commitments_are_held(draft):
    assert check_draft(draft, "friends").status == NEEDS_APPROVAL


@pytest.mark.parametrize(
    "draft",
    ["Saapten da, nee?", "What time are you thinking?", "Paakalam da 😂", "Super 👌", "Nice da, enjoy pannu"],
)
def test_non_committal_drafts_pass(draft):
    assert check_draft(draft, "friends").status == OK


@pytest.mark.parametrize("draft", ["", "   ", None, generator.NO_REPLY_FALLBACK])
def test_unsure_drafts_are_held(draft):
    assert check_draft(draft, "friends", fallback=generator.NO_REPLY_FALLBACK).status == NEEDS_APPROVAL


def test_unknown_sender_is_always_held_even_without_identity():
    check = check_draft("Hi! May I know who this is?", "unknown")
    assert check.status == NEEDS_APPROVAL and check.draft == "Hi! May I know who this is?"


def test_same_draft_always_gets_same_verdict():
    verdicts = {check_draft("Naalaiki pesalam da", "friends") for _ in range(20)}
    assert len(verdicts) == 1


# --- Recent conversation reaches the draft, never the decision --------------------------

def test_recent_messages_reach_the_prompt_but_the_decision_uses_the_current_message_only(stub, monkeypatch):
    prompts = stub(decision_engine.SAFE, "Saapten da, nee?")
    classified = []
    monkeypatch.setattr(decision_engine, "_classify_intent", lambda text: classified.append(text) or decision_engine.SAFE)
    recent = [{"from_me": False, "text": "Machan saaptiya?"}, {"from_me": True, "text": "Saapten da"}]
    result = pipeline.prepare_reply(message("Nee?", recent_messages=recent), "friends")
    assert result["status"] == "ready"
    assert classified == ["Nee?"]
    assert "Them: Machan saaptiya?" in prompts[0] and "Vaideesh: Saapten da" in prompts[0]


def test_a_commitment_draft_is_still_held_with_recent_messages(stub):
    stub(decision_engine.SAFE, "Ok da, 7 pm ku varen")
    recent = [{"from_me": True, "text": "Paakalam da"}]
    result = pipeline.prepare_reply(message("So varuviya?", recent_messages=recent), "friends")
    assert result["status"] == NEEDS_APPROVAL

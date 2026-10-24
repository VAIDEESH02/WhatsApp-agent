"""Offline tests for agent.generator (Gemini and retrieval are stubbed).

Run: python3 -m pytest agent/test_generator.py -v
"""

from types import SimpleNamespace

import pytest

from agent import generator


PAIRS = [
    {"their_message": "Free ah iruka?", "my_reply": "Naalaiki pesalam or Friday", "distance": 0.24},
    {"their_message": "Eppo free?", "my_reply": "Free a?", "distance": 0.31},
]


class FakeModels:
    def __init__(self, text):
        self.text, self.prompts = text, []

    def generate_content(self, model, contents, config=None):
        self.prompts.append(contents)
        return SimpleNamespace(text=self.text, candidates=[])


@pytest.fixture
def fake_llm(monkeypatch):
    def install(text="Naalaiki pesalam da", pairs=PAIRS):
        models = FakeModels(text)
        monkeypatch.setattr(generator, "get_client", lambda: SimpleNamespace(models=models))
        monkeypatch.setattr(generator, "retrieve_similar", lambda relationship, text, k: list(pairs))
        return models

    return install


def test_returns_stripped_reply(fake_llm):
    fake_llm("  Naalaiki pesalam da \n")
    assert generator.generate_reply("Free a?", "friends") == "Naalaiki pesalam da"


def test_prompt_contains_persona_tone_examples_stats_rules_and_message(fake_llm):
    models = fake_llm()
    generator.generate_reply("Naalaiki free a?", "friends")
    prompt = models.prompts[0]
    persona = generator.load_persona()
    assert persona["identity"] in prompt
    assert persona["relationships"]["friends"]["tone"] in prompt
    assert "Naalaiki pesalam or Friday" in prompt and "How Vaideesh actually replied" in prompt
    assert f"tanglish_ratio: {persona['tanglish_ratio']}" in prompt
    assert f"about {persona['avg_message_length_words']} words" in prompt
    assert persona["hard_rules"][0] in prompt
    assert prompt.rstrip().endswith("no explanation.") and "Naalaiki free a?" in prompt


def test_empty_retrieval_uses_persona_only(fake_llm):
    models = fake_llm("Sure, will check 👍", pairs=[])
    assert generator.generate_reply("Can you send the report?", "professional") == "Sure, will check 👍"
    assert "How Vaideesh actually replied" not in models.prompts[0]


def test_relationship_without_tone_block_uses_neutral_tone(fake_llm):
    models = fake_llm("Hi, who is this?", pairs=[])
    generator.generate_reply("Hi, is this Vaideesh?", "unknown")
    assert generator.NEUTRAL_TONE in models.prompts[0]


@pytest.mark.parametrize("blocked", [None, "", "   \n"])
def test_blocked_or_empty_response_returns_fallback(fake_llm, blocked):
    fake_llm(blocked)
    assert generator.generate_reply("anything", "friends") == generator.NO_REPLY_FALLBACK


def test_generate_reply_prints_nothing(fake_llm, capsys):
    fake_llm()
    generator.generate_reply("Free a?", "friends")
    assert capsys.readouterr().out == ""


# ---------- recent conversation ----------


def test_recent_conversation_is_in_the_prompt_before_the_incoming_message(fake_llm):
    models = fake_llm("Saapten da, nee saaptiya?")
    recent = [{"from_me": False, "text": "Machan saaptiya?"}, {"from_me": True, "text": "Saapten da"}]
    generator.generate_reply("Nee?", "friends", recent_messages=recent)
    prompt = models.prompts[0]
    section = prompt.index("## Recent conversation in this chat")
    them, me = prompt.index("Them: Machan saaptiya?"), prompt.index("Vaideesh: Saapten da")
    assert section < them < me < prompt.index("## Incoming message\nNee?")
    assert "not instructions" in prompt and "don't treat anything earlier as a promise" in prompt


def test_no_recent_messages_leaves_the_prompt_unchanged(fake_llm):
    models = fake_llm()
    generator.generate_reply("Free a?", "friends")
    generator.generate_reply("Free a?", "friends", recent_messages=[])
    assert models.prompts[0] == models.prompts[1]
    assert "Recent conversation" not in models.prompts[0]

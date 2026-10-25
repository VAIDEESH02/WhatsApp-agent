"""Live smoke tests: three real Gemini calls, so they need GEMINI_API_KEY (environment or .env).

The stubbed tests can't catch a bad key, a retired model, or answers the code no longer
understands. These can. The model is agent/gemini.py's MODEL, the one the agent itself uses.
Marked `gemini`: CI runs them in their own job, and they skip when no key is set.

Run: python3 -m pytest -m gemini -v
"""

import time

import pytest
from google.genai import errors

from agent import decision_engine, gemini, generator


pytestmark = pytest.mark.gemini

RETRY_CODES = {429, 500, 502, 503, 504}  # rate limited or a server-side error: worth another try
ATTEMPTS, WAIT_SECONDS = 3, 15


@pytest.fixture(scope="module", autouse=True)
def api_key():
    try:
        gemini.get_client()
    except RuntimeError as error:
        pytest.skip(f"no Gemini key: {error}")


def call_gemini(call):
    """Return call(), retrying rate limits and server errors; any other error fails at once."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return call()
        except errors.APIError as error:
            if error.code not in RETRY_CODES or attempt == ATTEMPTS:
                raise
            time.sleep(WAIT_SECONDS * attempt)


def test_configured_model_answers():
    response = call_gemini(
        lambda: gemini.get_client().models.generate_content(model=gemini.MODEL, contents="Reply with one word: pong")
    )
    assert (response.text or "").strip(), f"{gemini.MODEL} returned an empty answer"


def test_money_request_needs_a_human():
    raw = call_gemini(lambda: decision_engine._classify_intent("Can you send me 5000 rupees on GPay today? I'll return it next week."))
    assert (raw or "").strip() == decision_engine.NEEDS_HUMAN


def test_casual_message_gets_a_real_draft(monkeypatch):
    # Fixed examples, so the test neither depends on nor sends your chat history.
    monkeypatch.setattr(
        generator, "retrieve_similar", lambda relationship, text, k: [{"their_message": "Saaptiya?", "my_reply": "Saapten da, nee?", "distance": 0.2}]
    )
    draft = call_gemini(lambda: generator.generate_reply("Lunch saaptiya?", "friends"))
    assert draft.strip() and draft != generator.NO_REPLY_FALLBACK

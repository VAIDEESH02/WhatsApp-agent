"""Shared Gemini client for the agent (google-genai SDK)."""

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from google import genai


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODEL = "gemini-3.5-flash"


@lru_cache(maxsize=1)
def get_client() -> genai.Client:
    """Created on first use and reused, so importing agent modules needs no API key."""
    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing from .env")
    return genai.Client(api_key=api_key)

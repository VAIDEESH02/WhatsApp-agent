#!/usr/bin/env python3
"""Run a batch of realistic messages through every layer of the agent and print a summary.

For each case: agent.router.resolve_relationship (JID -> relationship), then
agent.pipeline.prepare_reply, which runs decision_engine.should_reply, and only if it
passes, generator.generate_reply plus the draft check. Nothing is sent. Decisions are
logged to logs/batch_test_log.jsonl so the live decision log stays clean.

Run: python3 agent/batch_test.py
"""

import json
import sys
import textwrap
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import decision_engine  # noqa: E402
from agent.pipeline import prepare_reply  # noqa: E402
from agent.router import DEFAULT_MAP_PATH, resolve_relationship  # noqa: E402


PHONE_MAP_PATH = PROJECT_ROOT / DEFAULT_MAP_PATH
GROUP_JID = "120363041234567890@g.us"


def jid_for(category: str, phone_map: dict) -> str:
    """A real contact's JID for a category, read from the phone map (no numbers in code)."""
    phone = next((key for key in sorted(phone_map) if phone_map[key] == category), None)
    if phone is None:
        raise SystemExit(f"No number in {DEFAULT_MAP_PATH} is mapped to '{category}'")
    return f"{phone}@s.whatsapp.net"


def unknown_jid(phone_map: dict) -> str:
    """A well-formed JID whose number is guaranteed not to be in the phone map."""
    number = 15550000000
    while str(number) in phone_map:
        number += 1
    return f"{number}@s.whatsapp.net"


def build_cases(phone_map: dict) -> list[tuple[str, str, dict]]:
    """(gate exercised, jid, message) for every layer of the pipeline."""
    family, friends = jid_for("family", phone_map), jid_for("friends", phone_map)
    girlfriend, freelance = jid_for("girlfriend", phone_map), jid_for("business_freelance", phone_map)

    def message(text, **overrides):
        return {"from_me": False, "text": text, "message_type": "text", "is_forwarded": False, **overrides}

    return [
        ("hard: own message", family, message("Reached home, saaptacha?", from_me=True)),
        ("hard: group chat", GROUP_JID, message("Hi all, are we meeting at 5?")),
        ("hard: unknown sender", unknown_jid(phone_map), message("Hi, is this Vaideesh?")),
        ("signal: media only", girlfriend, message("", message_type="image")),
        ("signal: forwarded", family, message("Good morning! Share this with 10 people 🙏", is_forwarded=True)),
        ("signal: one-word ack", friends, message("thanks")),
        ("intent: money", friends, message("can you send me 5000 rupees")),
        ("intent: commitment", family, message("Dinner ku varuviya?")),
        ("pass: casual", girlfriend, message("Lunch saaptiya?")),
        ("pass: casual", friends, message("Machi epdi iruka? Romba naal aachu!")),
        ("pass: work request", freelance, message("The director wants a slightly warmer look. Can you check?")),
    ]


def shorten(text: str | None, width: int) -> str:
    text = " ".join((text or "—").split())
    return textwrap.shorten(text, width=width, placeholder="…") if len(text) > width else text


def print_table(rows: list[dict]) -> None:
    columns = [("#", 2), ("gate", 20), ("message", 34), ("relationship", 18), ("decision", 14), ("reason", 46), ("reply", 40)]
    line = " | ".join("-" * width for _, width in columns)
    print("\n" + " | ".join(name.ljust(width) for name, width in columns))
    print(line)
    for index, row in enumerate(rows, start=1):
        values = [str(index), row["gate"], row["message"], row["relationship"], row["decision"], row["reason"], row["reply"]]
        print(" | ".join(shorten(value, width).ljust(width) for value, (_, width) in zip(values, columns)))


def main() -> None:
    decision_engine.LOG_PATH = decision_engine.LOG_DIR / "batch_test_log.jsonl"
    phone_map = json.loads(PHONE_MAP_PATH.read_text(encoding="utf-8"))

    rows = []
    for gate, jid, message in build_cases(phone_map):
        relationship, collection = resolve_relationship(jid)
        result = prepare_reply(message, relationship)
        label = message["text"] or f"<{message['message_type']}, no caption>"
        print(f"[{gate}] {label!r} ({relationship}) -> {result['status']}" + (f": {result['draft']!r}" if result["draft"] else ""))
        rows.append(
            {
                "gate": gate,
                "message": label,
                "relationship": relationship,
                "decision": result["status"],
                "reason": " | ".join(result["reasons"]),
                "reply": result["draft"],
            }
        )

    print_table(rows)
    counts = {status: sum(row["decision"] == status for row in rows) for status in ("ignored", "needs_approval", "ready")}
    print(f"\n{len(rows)} messages: {counts['ignored']} ignored, {counts['needs_approval']} need approval, {counts['ready']} ready")
    print(f"Decisions logged to {decision_engine.LOG_PATH.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()

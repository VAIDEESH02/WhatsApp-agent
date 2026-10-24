#!/usr/bin/env python3
"""Add new conversation_ids from processed_pairs.jsonl to the hand-edited relationship map."""

import json
import os

from parse_export import PROJECT_ROOT


PAIRS_PATH = PROJECT_ROOT / "data" / "processed_pairs.jsonl"
MAP_PATH = PROJECT_ROOT / "config" / "contact_relationship_map.json"
PLACEHOLDER = "REPLACE_ME"
DEFAULT_KEY = "_default"
DEFAULT_VALUE = "unknown"


def load_conversation_ids() -> list[str]:
    with PAIRS_PATH.open(encoding="utf-8") as pairs_file:
        return sorted({json.loads(line)["conversation_id"] for line in pairs_file if line.strip()})


def write_json_atomic(path, data: dict) -> None:
    """Write data as JSON to path so the file is never left half-written or unparseable.

    The JSON goes to a temp file beside path, is parsed back to check it is complete, and
    only then replaces path in one atomic rename. On any failure the old file is untouched.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if json.loads(tmp.read_text(encoding="utf-8")) != data:
            raise ValueError(f"{tmp} did not read back as the data written")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> None:
    if not PAIRS_PATH.exists():
        raise SystemExit(f"No pairs file at {PAIRS_PATH}. Run ingestion/parse_export.py first.")

    conversation_ids = load_conversation_ids()
    relationship_map = (
        json.loads(MAP_PATH.read_text(encoding="utf-8")) if MAP_PATH.exists() else {}
    )

    new_ids = [cid for cid in conversation_ids if cid not in relationship_map]
    for conversation_id in new_ids:
        relationship_map[conversation_id] = PLACEHOLDER
    relationship_map.setdefault(DEFAULT_KEY, DEFAULT_VALUE)

    MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(MAP_PATH, relationship_map)

    still_placeholder = sorted(key for key, value in relationship_map.items() if value == PLACEHOLDER)
    print(f"Conversation ids in pairs file: {len(conversation_ids)}")
    print(f"New ids added this run:         {len(new_ids)}")
    if still_placeholder:
        print(f'\nStill set to "{PLACEHOLDER}" — fill these in by hand in {MAP_PATH.relative_to(PROJECT_ROOT)}:')
        for key in still_placeholder:
            print(f"  - {key}")
    else:
        print("\nEvery entry is classified.")


if __name__ == "__main__":
    main()

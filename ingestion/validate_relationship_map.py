#!/usr/bin/env python3
"""Fail (exit 1) unless every conversation_id in the pairs file has a real relationship."""

import json
import sys

from build_relationship_map import MAP_PATH, PAIRS_PATH, PLACEHOLDER, load_conversation_ids
from parse_export import PROJECT_ROOT


def main() -> int:
    for path in (PAIRS_PATH, MAP_PATH):
        if not path.exists():
            print(f"FAIL: {path.relative_to(PROJECT_ROOT)} does not exist.")
            return 1

    relationship_map = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    conversation_ids = load_conversation_ids()

    missing = [cid for cid in conversation_ids if cid not in relationship_map]
    unfilled = [cid for cid in conversation_ids if relationship_map.get(cid) == PLACEHOLDER]

    if not missing and not unfilled:
        print(f"PASS: all {len(conversation_ids)} conversation_ids have a relationship.")
        return 0

    print(f"FAIL: {len(missing) + len(unfilled)} of {len(conversation_ids)} conversation_ids are not classified.")
    if missing:
        print("\nMissing from the map (run ingestion/build_relationship_map.py):")
        for conversation_id in missing:
            print(f"  - {conversation_id}")
    if unfilled:
        print(f'\nStill "{PLACEHOLDER}" (fill in by hand in {MAP_PATH.relative_to(PROJECT_ROOT)}):')
        for conversation_id in unfilled:
            print(f"  - {conversation_id}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

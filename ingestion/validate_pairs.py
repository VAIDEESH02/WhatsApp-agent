#!/usr/bin/env python3
"""Sanity-check data/processed_pairs.jsonl and print a pass/fail report."""

import argparse
import json
import random
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from parse_export import (
    MAX_REPLY_WORDS,
    MIRROR_RATIO as NEAR_IDENTICAL_RATIO,
    PROJECT_ROOT,
    can_form_pairs,
    conversation_id_from,
)


NOISE_MARKERS = ["omitted", "<Media", "This message was deleted", "You deleted this message"]
SAMPLE_SIZE = 5
MAX_FLAGS_SHOWN = 10

RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BOLD = "\033[1m"
RESET = "\033[0m"


def heading(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}")


def indent(text: str, prefix: str = "      ") -> str:
    return "\n".join(prefix + line for line in text.splitlines()) or prefix


def report_flags(name: str, flagged: list[tuple[int, dict, str]]) -> int:
    """Print one check's result and return how many issues it raised."""
    if not flagged:
        print(f"  {GREEN}PASS{RESET}  {name}")
        return 0
    print(f"  {RED}FAIL{RESET}  {name}: {len(flagged)} pair(s)")
    for line_number, pair, reason in flagged[:MAX_FLAGS_SHOWN]:
        print(f"        line {line_number} [{pair['conversation_id']}] {reason}")
    if len(flagged) > MAX_FLAGS_SHOWN:
        print(f"        ... and {len(flagged) - MAX_FLAGS_SHOWN} more")
    return len(flagged)


def normalise(text: str) -> str:
    return " ".join(text.lower().split())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate processed WhatsApp reply pairs.")
    parser.add_argument(
        "--pairs",
        type=Path,
        default=PROJECT_ROOT / "data" / "processed_pairs.jsonl",
        help="JSONL file to validate (defaults to data/processed_pairs.jsonl)",
    )
    parser.add_argument(
        "--exports-dir",
        type=Path,
        default=PROJECT_ROOT / "Whatsapp chats",
        help="Export folder, used to spot conversations that produced zero pairs",
    )
    parser.add_argument("--seed", type=int, help="Random seed for a repeatable sample")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.pairs.exists():
        print(f"{RED}No pairs file at {args.pairs}. Run ingestion/parse_export.py first.{RESET}")
        return

    pairs: list[tuple[int, dict]] = []
    with args.pairs.open(encoding="utf-8") as pairs_file:
        for line_number, line in enumerate(pairs_file, start=1):
            if line.strip():
                pairs.append((line_number, json.loads(line)))

    issues = 0

    # 1. Counts per conversation, including exports that produced nothing.
    heading(f"1. Pair counts — {len(pairs)} total")
    counts = Counter(pair["conversation_id"] for _, pair in pairs)
    expected_ids = {
        conversation_id_from(path) for path in args.exports_dir.glob("**/*.txt") if can_form_pairs(path)
    }
    for conversation_id in sorted(expected_ids | set(counts), key=lambda cid: (-counts[cid], cid)):
        if counts[conversation_id]:
            print(f"  {counts[conversation_id]:>5}  {conversation_id}")
        else:
            print(f"  {RED}    0  {conversation_id}  ← no pairs (check --name or whether you replied){RESET}")
            issues += 1

    # 2. Random sample to eyeball.
    heading(f"2. Random sample of {min(SAMPLE_SIZE, len(pairs))} pairs")
    rng = random.Random(args.seed)
    for line_number, pair in rng.sample(pairs, min(SAMPLE_SIZE, len(pairs))):
        print(f"\n  [{pair['conversation_id']}] {pair['timestamp']} (line {line_number})")
        print(f"    THEM:\n{indent(pair['their_message'])}")
        print(f"    ME:\n{indent(pair['my_reply'])}")

    # 3–5. Per-pair checks.
    heading("3–5. Pair checks")
    noise, too_long, duplicates = [], [], []
    for line_number, pair in pairs:
        for field in ("their_message", "my_reply"):
            text = pair[field]
            if not text.strip():
                noise.append((line_number, pair, f"{field} is empty"))
                continue
            markers = [marker for marker in NOISE_MARKERS if marker.lower() in text.lower()]
            if markers:
                noise.append((line_number, pair, f"{field} contains {markers}: {text[:60]!r}"))

        word_count = len(pair["my_reply"].split())
        if word_count > MAX_REPLY_WORDS:
            too_long.append((line_number, pair, f"my_reply has {word_count} words: {pair['my_reply'][:60]!r}"))

        theirs, mine = normalise(pair["their_message"]), normalise(pair["my_reply"])
        if theirs and mine and SequenceMatcher(None, theirs, mine).ratio() >= NEAR_IDENTICAL_RATIO:
            duplicates.append((line_number, pair, f"{pair['their_message'][:40]!r} ≈ {pair['my_reply'][:40]!r}"))

    issues += report_flags("3. No leaked noise or empty messages", noise)
    issues += report_flags(f"4. No my_reply over {MAX_REPLY_WORDS} words", too_long)
    issues += report_flags("5. No identical or near-identical pairs", duplicates)

    # 6. Summary.
    print()
    if issues:
        print(f"{YELLOW}⚠ {issues} issues found — review above{RESET}")
    else:
        print(f"{GREEN}✅ Looks good{RESET}")


if __name__ == "__main__":
    main()

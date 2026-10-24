#!/usr/bin/env python3
"""Build simple persona style signals from a WhatsApp text export."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path


MESSAGE_START = re.compile(
    r"^\[(?P<date>[^,]+),\s*(?P<time>[^\]]+)\]\s+(?P<sender>[^:]+):\s?(?P<message>.*)$"
)
# iOS exports prefix some lines (media, calls, deleted messages) with invisible
# direction marks; without stripping them those lines would be glued onto the
# previous message.
INVISIBLE_MARKS = re.compile(r"[‎‏‪-‮⁦-⁩]")
# Outgoing messages that are not typed text and must not count as style evidence.
NON_TEXT_MESSAGE = re.compile(
    r"omitted|^(?:Missed )?(?:voice|video) call|deleted this message|"
    r"This message was deleted|^https?://\S+$|Join Zoom Meeting|```|Parcel Notification",
    re.IGNORECASE,
)
# Tanglish here means Tamil written in Latin script, often interwoven with
# English. These are high-confidence Tamil terms observed in Vaideesh-authored
# messages across the supplied exports. This is a vocabulary signal, not a
# general-purpose language detector.
TANGLISH_WORDS = (
    "aagum",
    "aama",
    "athu",
    "da",
    "eppadi",
    "enga",
    "enna",
    "ennaku",
    "ethachu",
    "illa",
    "inniki",
    "intha",
    "ippo",
    "iruka",
    "iruku",
    "konjam",
    "ku",
    "la",
    "maari",
    "naalaiki",
    "nalla",
    "naan",
    "nee",
    "nu",
    "oru",
    "panna",
    "pannanum",
    "pannu",
    "poitu",
    "romba",
    "seekram",
    "seri",
    "thaan",
    "therila",
    "va",
    "vanthuten",
    "veetla",
    "veetuku",
    "venam",
    "vera",
    "yen",
)
TANGLISH_PATTERN = re.compile(
    r"\b(?:" + "|".join(map(re.escape, TANGLISH_WORDS)) + r")\b",
    re.IGNORECASE,
)
EMOJI_PATTERN = re.compile(
    r"[\U0001F300-\U0001FAFF\u2300-\u23FF\u2600-\u27BF\u2B00-\u2BFF]"
)


def parse_messages(file_path: Path) -> list[tuple[str, str]]:
    """Return (sender, message) pairs from a WhatsApp text export."""
    messages: list[tuple[str, str]] = []
    current_sender: str | None = None
    current_message: list[str] = []

    def save_current() -> None:
        if current_sender is not None:
            messages.append((current_sender, "\n".join(current_message).strip()))

    with file_path.open(encoding="utf-8", errors="ignore") as export:
        for raw_line in export:
            line = INVISIBLE_MARKS.sub("", raw_line.rstrip("\n\r"))
            match = MESSAGE_START.match(line)
            if match:
                save_current()
                current_sender = match.group("sender")
                current_message = [match.group("message")]
            elif current_sender is not None:
                current_message.append(line)

    save_current()
    return messages


def is_typed_text(message: str) -> bool:
    """True for a real typed message: not media, a call log, a link, or pasted content."""
    return bool(message) and message not in {".", "!", "?"} and not NON_TEXT_MESSAGE.search(message)


def compute_style_signals(messages: list[str]) -> dict:
    """Compute evidence-based Tanglish and style signals for one sender's messages."""
    tanglish_terms = Counter(
        match.group(0).lower()
        for message in messages
        for match in TANGLISH_PATTERN.finditer(message)
    )
    tanglish_messages = sum(bool(TANGLISH_PATTERN.search(message)) for message in messages)
    word_tokens = [re.findall(r"\b[\w'’-]+\b", message) for message in messages]
    total_tokens = sum(len(tokens) for tokens in word_tokens)
    word_counts = [len(tokens) for tokens in word_tokens]
    emojis = Counter(emoji for message in messages for emoji in EMOJI_PATTERN.findall(message))

    return {
        "tanglish_message_ratio_percent": round(100 * tanglish_messages / len(messages), 2),
        "tanglish_token_ratio_percent": round(100 * sum(tanglish_terms.values()) / total_tokens, 2) if total_tokens else 0.0,
        "observed_tanglish_terms": [
            {"term": term, "frequency": frequency}
            for term, frequency in tanglish_terms.most_common(25)
        ],
        "average_message_length_words": round(sum(word_counts) / len(messages), 2),
        "top_emojis": [
            {"emoji": emoji, "frequency": frequency}
            for emoji, frequency in emojis.most_common(15)
        ],
        "total_sample_size": len(messages),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract persona style signals from a WhatsApp .txt export."
    )
    parser.add_argument("--file", required=True, type=Path, help="Path to the WhatsApp .txt export")
    parser.add_argument("--name", required=True, help="Exact sender name as it appears in the export")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("style_signals.json"),
        help="Output JSON path (defaults to persona/style_signals.json)",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    all_messages = parse_messages(args.file)
    sender_messages = [
        message for sender, message in all_messages if sender == args.name and is_typed_text(message)
    ]

    if not sender_messages:
        print(
            f'Warning: no messages found for sender "{args.name}". '
            "Please double-check the exact sender name in the raw export."
        )
        return

    signals = compute_style_signals(sender_messages)
    output_path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(signals, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Persona style signals for: {args.name}")
    print(f"Tanglish messages: {signals['tanglish_message_ratio_percent']:.2f}%")
    print(f"Tanglish token ratio: {signals['tanglish_token_ratio_percent']:.2f}%\nAverage message length: {signals['average_message_length_words']:.2f} words")
    print(f"Observed Tanglish terms: {signals['observed_tanglish_terms'] or 'none'}\nTop emojis: {signals['top_emojis'] or 'none'}")
    print(f"Total sample size: {signals['total_sample_size']} messages")
    print(f"JSON written to: {output_path}")


if __name__ == "__main__":
    main()
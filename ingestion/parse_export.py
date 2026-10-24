#!/usr/bin/env python3
"""Turn WhatsApp .txt exports into (their_message, my_reply) pairs for the History Brain."""

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Run as a script, only ingestion/ is on sys.path; the project root is needed for config.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.constants import ONE_WORD_ACKS  # noqa: E402
EXPORTS_DIR = PROJECT_ROOT / "Whatsapp chats"

# Lines containing any of these (case-insensitive) are system/media messages and
# are dropped. Exact wording varies by phone language and OS, so extend freely.
SYSTEM_MESSAGE_SUBSTRINGS = [
    "Messages and calls are end-to-end encrypted",
    "<Media omitted>",
    "image omitted",
    "video omitted",
    "audio omitted",
    "sticker omitted",
    "GIF omitted",
    "document omitted",
    "This message was deleted",
    "You deleted this message",
    "Missed voice call",
    "Missed video call",
    "created group",
    "changed the subject",
    "added you",
    "changed this group's icon",
    # iOS wording seen in the project exports
    "Contact card omitted",
    "Voice call,",
    "Video call,",
    # auto-generated meeting invites (a pasted template, not typed text)
    "Join Zoom Meeting",
    "Join with Google Meet",
    "Microsoft Teams meeting",
]

# Appended to edited messages by iOS; the message itself is kept.
EDITED_MARKER = "<This message was edited>"

MAX_PAIRS_PER_CONVERSATION = 500

# conversation_id is a canonical, name-free id: CONVERSATION_ID_PREFIX + the first
# CONVERSATION_ID_LENGTH hex chars of sha1(slugify(chat name)). It is stable across runs
# and export filename styles; config/contact_relationship_map.json maps it to a category.
CONVERSATION_ID_PREFIX = "chat-"
CONVERSATION_ID_LENGTH = 10
EXPORT_NAME_PREFIXES = ("WhatsApp Chat with ", "WhatsApp Chat - ")

# Replies longer than this, or containing a ``` code block, are pasted code or
# documents rather than texting style, so their pairs are dropped.
MAX_REPLY_WORDS = 100
CODE_FENCE = "```"

# Same-sender messages further apart than SESSION_GAP belong to different
# conversations, so they start a new turn instead of being merged into one. A reply
# that comes more than MAX_REPLY_GAP after the other person's last message is a new
# conversation, not a reply, so no pair is made.
SESSION_GAP = timedelta(hours=6)
MAX_REPLY_GAP = timedelta(days=3)

# A reply this similar to the message before it is a parallel post (e.g. everyone
# posting "Hi all, I'll be WFH today" in a group), not a reply, so it is dropped.
MIRROR_RATIO = 0.9

# Private details are masked in both sides of every pair before it is written.
REDACTIONS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"(?<![\w/])\+?\d{10,13}\b"), "[PHONE]"),
    (re.compile(r"(?i)\b(pass(?:word|code)|pwd|otp)(\s*[:=]\s*)\S+"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)\S*pass(?:word|code)\S*\d\S*"), "[REDACTED]"),
]
URL_ONLY_LINE = re.compile(r"https?://\S+")

TIME = r"\d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?:\s?[AaPp]\.?\s?[Mm]\.?)?"
# Android: "20/10/26, 9:29 PM - Sender: message" (12hr or 24hr).
ANDROID_LINE = re.compile(
    r"^(?P<date>\d{1,4}[./-]\d{1,2}[./-]\d{1,4}),?\s+(?P<time>" + TIME + r")\s+-\s+"
    r"(?P<sender>[^:]+?):\s?(?P<message>.*)$"
)
# iOS: "[20/10/26, 9:29:42 PM] Sender: message" (12hr or 24hr).
IOS_LINE = re.compile(
    r"^\[(?P<date>\d{1,4}[./-]\d{1,2}[./-]\d{1,4}),?\s+(?P<time>" + TIME + r")\]\s+"
    r"(?P<sender>[^:]+?):\s?(?P<message>.*)$"
)
# A timestamped line with no "Sender:" part is a system event (e.g. "X created
# group"), not a continuation of the previous message.
SYSTEM_EVENT_LINE = re.compile(
    r"^\[?\d{1,4}[./-]\d{1,2}[./-]\d{1,4},?\s+" + TIME + r"(?:\]|\s+-)\s"
)
# Invisible direction marks that iOS exports put in front of some lines.
INVISIBLE_MARKS = re.compile(r"[‎‏‪-‮⁦-⁩]")


def slugify(name: str) -> str:
    """Contact/chat name -> conversation_id, e.g. 'Sarath SSN' -> 'sarath-ssn'.

    Accents are folded first ('Niccolò' -> 'niccolo'). A name with no Latin letters or
    digits at all (e.g. Tamil script) gets a stable hash-based id instead of an empty one.
    """
    folded = "".join(
        char for char in unicodedata.normalize("NFKD", name) if not unicodedata.combining(char)
    )
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    if slug:
        return slug
    return "contact-" + hashlib.sha1(unicodedata.normalize("NFC", name).encode("utf-8")).hexdigest()[:8]


def chat_name_from(file_path: Path) -> str:
    """'WhatsApp Chat - Sarath SSN.txt' -> 'Sarath SSN'."""
    name = file_path.stem
    for prefix in EXPORT_NAME_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def canonical_conversation_id(chat_name: str) -> str:
    """Name-free, stable conversation id for a chat or contact name, e.g. 'chat-5c1f0e9a2b'."""
    digest = hashlib.sha1(slugify(chat_name).encode("utf-8")).hexdigest()
    return CONVERSATION_ID_PREFIX + digest[:CONVERSATION_ID_LENGTH]


def conversation_id_from(file_path: Path) -> str:
    return canonical_conversation_id(chat_name_from(file_path))


def parse_messages(file_path: Path) -> list[dict]:
    """Return messages in file order; unmatched lines continue the previous message."""
    messages: list[dict] = []
    current: dict | None = None

    with file_path.open(encoding="utf-8", errors="ignore") as export:
        for raw_line in export:
            line = INVISIBLE_MARKS.sub("", raw_line.rstrip("\n\r"))
            match = ANDROID_LINE.match(line) or IOS_LINE.match(line)
            if match:
                current = {
                    "sender": match.group("sender").strip(),
                    "date": match.group("date"),
                    "time": match.group("time").replace("\u202f", " "),
                    "text": match.group("message"),
                }
                current["timestamp"] = f"{current['date']}, {current['time']}"
                messages.append(current)
            elif SYSTEM_EVENT_LINE.match(line):
                current = None
            elif current is not None:
                current["text"] += "\n" + line

    day_first = detect_day_first([message["date"] for message in messages])
    for message in messages:
        message["text"] = message["text"].replace(EDITED_MARKER, "").strip()
        message["sent_at"] = parse_datetime(message["date"], message["time"], day_first)
    return messages


def detect_day_first(dates: list[str]) -> bool:
    """Infer the file's date order from its own data: a first field above 12 means DD/MM."""
    parts = [re.split(r"[./-]", date) for date in dates]
    if any(len(p[0]) == 4 for p in parts):
        return False  # YYYY-MM-DD
    if any(int(p[0]) > 12 for p in parts):
        return True
    if any(int(p[1]) > 12 for p in parts):
        return False
    return True  # ambiguous throughout; DD/MM is the WhatsApp default outside the US


def parse_datetime(date: str, time: str, day_first: bool) -> datetime | None:
    """Parse one export timestamp, or None if it is in an unexpected shape."""
    first, second, third = (int(part) for part in re.split(r"[./-]", date))
    if first > 31:
        year, month, day = first, second, third
    else:
        day, month = (first, second) if day_first else (second, first)
        year = third + 2000 if third < 100 else third
    clock = re.sub(r"\s", "", time).upper().replace("A.M.", "AM").replace("P.M.", "PM").replace(".", ":")
    meridiem = clock[-2:] if clock.endswith(("AM", "PM")) else ""
    hour, minute, *rest = (int(part) for part in clock.removesuffix(meridiem).split(":"))
    if meridiem:
        hour = hour % 12 + (12 if meridiem == "PM" else 0)
    try:
        return datetime(year, month, day, hour, minute, rest[0] if rest else 0)
    except ValueError:
        return None


def sender_count(file_path: Path) -> int:
    return len({message["sender"] for message in parse_messages(file_path)})


def can_form_pairs(file_path: Path) -> bool:
    """A pair needs a message from someone else followed by one from me: at least two senders."""
    return sender_count(file_path) >= 2


def is_group_export(file_path: Path) -> bool:
    """A chat with three or more distinct senders is a group chat (a 1:1 chat has at most two)."""
    return sender_count(file_path) >= 3


def group_conversation_ids(exports_dir: Path = EXPORTS_DIR) -> set[str]:
    return {conversation_id_from(path) for path in exports_dir.glob("**/*.txt") if is_group_export(path)}


def is_system_message(text: str) -> bool:
    lowered = text.lower()
    return any(substring.lower() in lowered for substring in SYSTEM_MESSAGE_SUBSTRINGS)


def is_acknowledgement(text: str) -> bool:
    """A message that is only a one-word acknowledgement (see config.constants.ONE_WORD_ACKS) is dropped."""
    return text.strip().rstrip(".!?,;:…~ ").lower() in ONE_WORD_ACKS


def is_pasted_content(text: str) -> bool:
    return CODE_FENCE in text or len(text.split()) > MAX_REPLY_WORDS


def is_links_only(text: str) -> bool:
    """True when every non-empty line is a bare URL, so the turn carries no wording."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return all(URL_ONLY_LINE.fullmatch(line) for line in lines)


def is_mirror(their_text: str, my_text: str) -> bool:
    theirs, mine = (" ".join(text.lower().split()) for text in (their_text, my_text))
    return SequenceMatcher(None, theirs, mine).ratio() >= MIRROR_RATIO


def redact(text: str) -> str:
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def gap_between(earlier: datetime | None, later: datetime | None) -> timedelta | None:
    return later - earlier if earlier and later else None


def group_into_turns(messages: list[dict]) -> list[dict]:
    """Merge consecutive messages from the same sender into one turn, unless a gap
    longer than SESSION_GAP shows they belong to separate conversations."""
    turns: list[dict] = []
    for message in messages:
        previous = turns[-1] if turns else None
        gap = gap_between(previous["ended_at"], message["sent_at"]) if previous else None
        if previous and previous["sender"] == message["sender"] and not (gap and gap > SESSION_GAP):
            previous["text"] += "\n" + message["text"]
            previous["ended_at"] = message["sent_at"] or previous["ended_at"]
        else:
            turns.append({**message, "started_at": message["sent_at"], "ended_at": message["sent_at"]})
    return turns


def build_pairs(conversation_id: str, turns: list[dict], my_name: str) -> list[dict]:
    pairs = []
    for previous, current in zip(turns, turns[1:]):
        if previous["sender"] == my_name or current["sender"] != my_name:
            continue
        reply_gap = gap_between(previous["ended_at"], current["started_at"])
        if reply_gap and reply_gap > MAX_REPLY_GAP:
            continue
        their_text, my_text = redact(previous["text"]), redact(current["text"])
        if is_pasted_content(my_text) or is_mirror(their_text, my_text):
            continue
        pairs.append(
            {
                "conversation_id": conversation_id,
                "their_message": their_text,
                "my_reply": my_text,
                "timestamp": current["timestamp"],
            }
        )
    return pairs[-MAX_PAIRS_PER_CONVERSATION:]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build (their_message, my_reply) pairs from WhatsApp .txt exports."
    )
    parser.add_argument("--name", required=True, help="Exact sender name as it appears in the export")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=EXPORTS_DIR,
        help="Folder of WhatsApp .txt exports, searched recursively (defaults to 'Whatsapp chats')",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "data" / "processed_pairs.jsonl",
        help="Output JSONL path (defaults to data/processed_pairs.jsonl)",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    export_files = sorted(args.input_dir.glob("**/*.txt"))
    if not export_files:
        print(f"Warning: no .txt exports found in {args.input_dir}")
        return

    all_pairs: list[dict] = []
    unpairable: list[str] = []
    print(f"Pairs per conversation (cap {MAX_PAIRS_PER_CONVERSATION}):")
    for file_path in export_files:
        if not can_form_pairs(file_path):
            unpairable.append(str(file_path.relative_to(args.input_dir)))
            continue
        conversation_id = conversation_id_from(file_path)
        messages = [
            message
            for message in parse_messages(file_path)
            if message["text"].strip()
            and not is_system_message(message["text"])
            and not is_acknowledgement(message["text"])
            and not is_links_only(message["text"])
        ]
        pairs = build_pairs(conversation_id, group_into_turns(messages), args.name)
        all_pairs.extend(pairs)

        print(f"  {conversation_id}  {file_path.relative_to(args.input_dir)}: {len(pairs)} pairs")
        if not pairs:
            print(
                f'  Warning: "{conversation_id}" produced zero pairs. '
                f'Check that --name "{args.name}" exactly matches your sender name in this export.'
            )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as output:
        for pair in all_pairs:
            output.write(json.dumps(pair, ensure_ascii=False) + "\n")

    if unpairable:
        print(f"\nSkipped {len(unpairable)} export(s) with fewer than two senders (no pairs possible):")
        for name in unpairable:
            print(f"  {name}")
    print(f"Total pairs: {len(all_pairs)}")
    print(f"JSONL written to: {args.output}")


if __name__ == "__main__":
    main()

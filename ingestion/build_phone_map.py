#!/usr/bin/env python3
"""Build config/relationship_map.json (phone number -> relationship) for live routing.

The phone map is the source of truth for the whole contact network. Phone numbers come
from config/contacts.md: numbers already in the map are never changed, and new numbers
are added as REPLACE_ME for you to classify by hand (never guessed). The conversation-level
config/contact_relationship_map.json is not read here.
"""

import argparse
import json
import re
import shutil
from datetime import datetime

from build_relationship_map import (
    DEFAULT_KEY,
    DEFAULT_VALUE,
    PAIRS_PATH,
    PLACEHOLDER,
    load_conversation_ids,
    write_json_atomic,
)
from parse_export import PROJECT_ROOT, canonical_conversation_id, group_conversation_ids
from whatsapp_ids import normalize_phone


CONTACTS_PATH = PROJECT_ROOT / "config" / "contacts.md"
PHONE_MAP_PATH = PROJECT_ROOT / "config" / "relationship_map.json"
BACKUP_DIR = PROJECT_ROOT / "config" / "backups"

# A contacts.md table row: "| Name | +91…, +91… | relationship |". Rows without a
# +phone (headers, the bucket summary table) are ignored.
CONTACT_ROW = re.compile(r"^\|\s*(?P<name>[^|]+?)\s*\|\s*(?P<phones>[^|]*\+\d[^|]*)\|")
PHONE_TOKEN = re.compile(r"\+[\d\s()-]{7,}\d")


def load_contacts(path=CONTACTS_PATH) -> list[tuple[str, list[str]]]:
    """(name, [normalized phone, ...]) for every contact row in contacts.md."""
    contacts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = CONTACT_ROW.match(line)
        if match:
            phones = [normalize_phone(token) for token in PHONE_TOKEN.findall(match.group("phones"))]
            contacts.append((match.group("name"), phones))
    return contacts


def backup(path) -> str | None:
    """Copy path into config/backups with a timestamp before it is overwritten."""
    if not path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"{path.stem}.{datetime.now():%Y%m%d-%H%M%S}{path.suffix}"
    shutil.copy2(path, target)
    return str(target.relative_to(PROJECT_ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the phone-keyed live relationship map.")
    parser.add_argument("--name", required=True, help="Your own contact name, excluded from the map")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing the map")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    contacts = load_contacts()
    phone_map = json.loads(PHONE_MAP_PATH.read_text(encoding="utf-8")) if PHONE_MAP_PATH.exists() else {}

    added: list[str] = []
    self_phones: list[str] = []
    for name, phones in contacts:
        if name == args.name:
            self_phones += phones
            continue
        for phone in phones:
            if phone not in phone_map:
                phone_map[phone] = PLACEHOLDER
                added.append(f"{phone} ({name})")
    phone_map.setdefault(DEFAULT_KEY, DEFAULT_VALUE)

    backup_path = None
    if not args.dry_run:
        backup_path = backup(PHONE_MAP_PATH)
        write_json_atomic(PHONE_MAP_PATH, phone_map)

    # Chats with history whose name has no phone number in contacts.md can't be matched live.
    named = {canonical_conversation_id(name) for name, _ in contacts}
    groups = group_conversation_ids()
    history_ids = set(load_conversation_ids()) if PAIRS_PATH.exists() else set()
    no_phone = sorted(history_ids - named - groups)
    counts: dict[str, int] = {}
    for key, value in phone_map.items():
        if key != DEFAULT_KEY:
            counts[value] = counts.get(value, 0) + 1
    still_placeholder = sorted(key for key, value in phone_map.items() if value == PLACEHOLDER)

    print(f"Contacts read: {len(contacts)} ({sum(len(p) for _, p in contacts)} numbers); self excluded: {len(self_phones)} number(s)")
    if args.dry_run:
        print("Dry run: map not written")
    elif backup_path:
        print(f"Backed up previous map to {backup_path}")
    print(f"Phone map: {len(phone_map) - 1} numbers -> {dict(sorted(counts.items()))}")
    print(f"Added {len(added)} new number(s) as {PLACEHOLDER}; existing numbers unchanged")
    for line in added:
        print(f"  + {line}")
    if still_placeholder:
        print(f"Still {PLACEHOLDER} — classify by hand in {PHONE_MAP_PATH.relative_to(PROJECT_ROOT)}: {', '.join(still_placeholder)}")
    if no_phone:
        print(f"Chats with history but no phone number in contacts.md (cannot be routed live): {', '.join(no_phone)}")
    if groups:
        print(f"Group chats (history kept; never drafted for, any @g.us JID is blocked): {', '.join(sorted(groups))}")


if __name__ == "__main__":
    main()

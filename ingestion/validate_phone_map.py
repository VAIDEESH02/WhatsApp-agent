#!/usr/bin/env python3
"""Fail (exit 1) unless config/relationship_map.json is safe to use for live routing."""

import argparse
import json
import sys

from build_phone_map import CONTACTS_PATH, PHONE_MAP_PATH, load_contacts
from build_relationship_map import DEFAULT_KEY, MAP_PATH, PAIRS_PATH, PLACEHOLDER, load_conversation_ids
from embed_to_chroma import relationship_classes
from parse_export import PROJECT_ROOT, canonical_conversation_id, group_conversation_ids, slugify
from whatsapp_ids import E164_DIGITS, jid_to_phone, relationship_for_jid


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the phone-keyed live relationship map.")
    parser.add_argument("--name", required=True, help="Your own contact name; your number must not be in the map")
    args = parser.parse_args()

    for path in (PHONE_MAP_PATH, MAP_PATH, CONTACTS_PATH):
        if not path.exists():
            print(f"FAIL: {path.relative_to(PROJECT_ROOT)} does not exist.")
            return 1

    phone_map = json.loads(PHONE_MAP_PATH.read_text(encoding="utf-8"))
    contact_map = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    contacts = load_contacts()
    allowed = set(relationship_classes(contact_map))
    numbers = {key: value for key, value in phone_map.items() if key != DEFAULT_KEY}

    failures: list[str] = []
    warnings: list[str] = []

    if DEFAULT_KEY not in phone_map:
        failures.append(f'missing "{DEFAULT_KEY}" key')
    failures += [f"{key}: not E.164 digits (country code, no '+')" for key in numbers if not E164_DIGITS.match(key)]
    failures += [f"{key}: still {PLACEHOLDER}" for key, value in numbers.items() if value == PLACEHOLDER]
    failures += [
        f"{key}: '{value}' is not a known relationship ({', '.join(sorted(allowed))})"
        for key, value in numbers.items()
        if value != PLACEHOLDER and value not in allowed
    ]
    # Every contact name must yield its own non-empty id, or routing would merge or lose contacts.
    names_by_slug: dict[str, list[str]] = {}
    for name, _ in contacts:
        names_by_slug.setdefault(slugify(name), []).append(name)
    failures += [f"contact name {names!r} gives an empty id" for slug, names in names_by_slug.items() if not slug]
    failures += [
        f"contact names {names} all give the id '{slug}'"
        for slug, names in names_by_slug.items()
        if slug and len(names) > 1
    ]

    self_numbers = {phone for name, phones in contacts if name == args.name for phone in phones}
    failures += [f"{phone}: your own number is in the map" for phone in self_numbers & set(numbers)]

    # Every key must round-trip through the JIDs Baileys sends, with and without a device suffix.
    for key in numbers:
        for jid in (f"{key}@s.whatsapp.net", f"{key}:7@s.whatsapp.net", f"{key}@c.us"):
            if jid_to_phone(jid) != key or relationship_for_jid(jid, phone_map) != numbers[key]:
                failures.append(f"{key}: does not route correctly from JID {jid}")
                break

    # Every number in contacts.md should be in the phone map (run build_phone_map.py to add it).
    warnings += [
        f"{name} ({phone}) from contacts.md is missing from the phone map"
        for name, phones in contacts
        if name != args.name
        for phone in phones
        if phone not in numbers
    ]

    # Chats with history that cannot be matched to a live sender. Group chats are expected
    # here: they have no phone number and are never drafted for.
    named = {canonical_conversation_id(name) for name, _ in contacts}
    groups = group_conversation_ids()
    if PAIRS_PATH.exists():
        warnings += [
            f"{cid}: has history pairs but no phone number, so live messages from it fall back to '{phone_map.get(DEFAULT_KEY)}'"
            for cid in load_conversation_ids()
            if cid not in named | groups
        ]
    if relationship_for_jid("120363000000000000@g.us", phone_map) is not None:
        failures.append("group JIDs (@g.us) are routable; the agent must never draft in a group chat")

    print(f"Phone map: {len(numbers)} numbers, _default '{phone_map.get(DEFAULT_KEY)}'")
    for cid in sorted(groups):
        print(f"  GROUP: {cid} — history kept as evidence; automated messages blocked (@g.us never routed)")
    for line in warnings:
        print(f"  WARN: {line}")
    if failures:
        print(f"\nFAIL: {len(failures)} problem(s):")
        for line in failures:
            print(f"  - {line}")
        return 1
    print(f"\nPASS: every number is valid E.164, routes from its JID, and maps to a known relationship.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Retrieve my most similar past replies for incoming messages, per relationship collection.

Run with no arguments to test every relationship with built-in sample queries, or narrow it:
    python3 ingestion/retrieval_demo.py --relationship friends --query "Eppadi da iruka"
    python3 ingestion/retrieval_demo.py --contact "Sherwin Jude DP" --query "Contrast feels flat"
"""

import argparse
import json
from functools import lru_cache

from build_phone_map import PHONE_MAP_PATH, load_contacts
from build_relationship_map import DEFAULT_KEY, DEFAULT_VALUE, MAP_PATH, PLACEHOLDER
from embed_to_chroma import CHROMA_HOST, CHROMA_PORT, MODEL_NAME, relationship_classes
from retrieval import collection_size, retrieve_similar
from parse_export import canonical_conversation_id, slugify
from whatsapp_ids import relationship_for_jid


TOP_K = 3
BLOCK_SEPARATOR = "=" * 72
RESULT_SEPARATOR = "---"

# Two or three realistic incoming messages per relationship, matching that tone.
SAMPLE_QUERIES = {
    "family": [
        "Inniki dinner ku varuviya?",
        "Enga iruka? Late aaguma?",
        "Did you reach office?",
    ],
    "friends": [
        "Eppadi da iruka",
        "Free ah iruka? Call pannalama",
        "Machi weekend plan enna?",
    ],
    "girlfriend": [
        "Reached a? Nee enga iruka?",
        "Inniki romba tired a iruku",
        "Lunch saaptiya?",
    ],
    "professional": [
        "Are you coming to office today?",
        "Can you send me the report by today?",
    ],
    "business_freelance": [
        "Can you make the skin tones a bit warmer?",
        "When can you share the final DCP?",
        "Did you get the hard disk with the rushes?",
    ],
    "unknown": [
        "Hi, is this Vaideesh?",
        "Can you call me when you're free?",
    ],
}
GENERIC_QUERIES = ["Hi, how are you?", "Can we talk sometime today?"]


@lru_cache(maxsize=1)
def load_relationship_map() -> dict:
    return json.loads(MAP_PATH.read_text(encoding="utf-8"))


def relationship_for_contact(contact_name: str) -> str:
    """Map a contact name (e.g. 'Sarath SSN') to its relationship collection.

    Chats with history are looked up by canonical conversation_id in the contact map;
    otherwise the contact's numbers from contacts.md are looked up in the phone map.
    """
    relationship_map = load_relationship_map()
    default = relationship_map.get(DEFAULT_KEY, DEFAULT_VALUE)
    relationship = relationship_map.get(canonical_conversation_id(contact_name))
    if relationship is None:
        phone_map = json.loads(PHONE_MAP_PATH.read_text(encoding="utf-8"))
        target = slugify(contact_name)
        numbers = [phone for name, phones in load_contacts() if slugify(name) == target for phone in phones]
        relationship = next((phone_map[phone] for phone in numbers if phone in phone_map), default)
    return default if relationship == PLACEHOLDER else relationship


def indent(text: str, prefix: str = "      ") -> str:
    return "\n".join(prefix + line for line in str(text).splitlines()) or prefix


def print_block(relationship: str, query: str, matches: list[dict]) -> None:
    """One readable, screenshot-friendly block per query."""
    print(f"\n{BLOCK_SEPARATOR}")
    print(f"Relationship : {relationship}")
    print(f"Query        : {query}")
    print(BLOCK_SEPARATOR)
    if not matches:
        print("  (no results)")
    for rank, match in enumerate(matches, start=1):
        print(f"  #{rank}  distance {match['distance']:.3f}")
        print(f"    They said:\n{indent(match['their_message'])}")
        print(f"    I replied:\n{indent(match['my_reply'])}")
        if rank < len(matches):
            print(f"  {RESULT_SEPARATOR}")


def is_monotonic(distances: list[float]) -> bool:
    """Closest match first: each distance should be >= the one above it."""
    return all(earlier <= later for earlier, later in zip(distances, distances[1:]))


def build_parser(known: list[str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show my most similar past replies, per relationship.")
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--relationship",
        choices=known,
        help="Collection to search (default: all of them)",
    )
    target.add_argument("--contact", help="WhatsApp display name, resolved through the contact map")
    target.add_argument(
        "--jid",
        help="Baileys JID as a live message arrives (e.g. 919812345670@s.whatsapp.net), resolved via the phone map",
    )
    parser.add_argument("--query", help="Incoming message to test (default: built-in samples per relationship)")
    parser.add_argument("-k", type=int, default=TOP_K, help=f"Results per query (default {TOP_K})")
    return parser


def main() -> None:
    known = relationship_classes(load_relationship_map())
    args = build_parser(known).parse_args()

    if args.jid:
        phone_map = json.loads(PHONE_MAP_PATH.read_text(encoding="utf-8"))
        relationship = relationship_for_jid(args.jid, phone_map)
        if relationship is None:
            print(f"JID {args.jid!r} is a group/broadcast/channel chat — the agent never drafts there.")
            return
        relationships = [relationship]
        print(f"JID {args.jid!r} -> relationship '{relationship}'")
    elif args.contact:
        relationships = [relationship_for_contact(args.contact)]
        print(f"Contact {args.contact!r} -> relationship '{relationships[0]}'")
    elif args.relationship:
        relationships = [args.relationship]
    else:
        relationships = known

    # The Chroma client and embedding model were loaded once, when retrieval was imported.
    print(f"Using ChromaDB at {CHROMA_HOST}:{CHROMA_PORT} with {MODEL_NAME}")

    sanity: list[str] = []
    for relationship in relationships:
        if collection_size(relationship) == 0:
            print(f"\n⚠ no data in {relationship}, skipping")
            sanity.append(f"⚠ {relationship}: skipped — collection is empty")
            continue

        queries = [args.query] if args.query else SAMPLE_QUERIES.get(relationship, GENERIC_QUERIES)
        out_of_order = []
        for query in queries:
            matches = retrieve_similar(relationship, query, args.k)
            print_block(relationship, query, matches)
            distances = [match["distance"] for match in matches]
            if not is_monotonic(distances):
                out_of_order.append(f"{query!r} {[round(d, 3) for d in distances]}")

        if out_of_order:
            sanity.append(f"⚠ {relationship}: distances NOT increasing for {'; '.join(out_of_order)} — worth a second look")
        else:
            scope = "the query" if len(queries) == 1 else f"all {len(queries)} queries"
            sanity.append(f"✅ {relationship}: distances increase down the results for {scope}")

    print(f"\n{BLOCK_SEPARATOR}\nSanity check\n{BLOCK_SEPARATOR}")
    for line in sanity:
        print(line)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Embed each pair's their_message and upsert it into a per-relationship ChromaDB collection."""

import hashlib
import json
import re

import chromadb
from sentence_transformers import SentenceTransformer

from build_relationship_map import DEFAULT_KEY, DEFAULT_VALUE, MAP_PATH, PAIRS_PATH, PLACEHOLDER
from parse_export import PROJECT_ROOT


PERSONA_PATH = PROJECT_ROOT / "persona" / "persona.json"

MODEL_NAME = "paraphrase-multilingual-mpnet-base-v2"
CHROMA_HOST = "localhost"
CHROMA_PORT = 8000
PROGRESS_EVERY = 50

# ChromaDB collection names: 3-63 chars of [a-zA-Z0-9._-], starting and ending alphanumeric.
VALID_COLLECTION_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,61}[a-zA-Z0-9]$")


def defined_categories() -> set[str]:
    """The categories the agent knows: the relationship blocks in persona.json."""
    return set(json.loads(PERSONA_PATH.read_text(encoding="utf-8"))["relationships"])


def relationship_classes(relationship_map: dict) -> list[str]:
    """Collection names: every defined category plus the map's _default.

    Every category in the map (canonical conversation_id -> category) must be one of them,
    so a typo can't silently create a stray collection. A category with no history yet
    (e.g. professional) still gets its collection, so live routing always has one to query.
    """
    classes = defined_categories() | {relationship_map.get(DEFAULT_KEY, DEFAULT_VALUE)}
    undefined = sorted(
        {value for key, value in relationship_map.items() if key != DEFAULT_KEY and value != PLACEHOLDER} - classes
    )
    if undefined:
        raise SystemExit(f"Categories in {MAP_PATH.name} not defined in {PERSONA_PATH.name}: {undefined}")
    invalid = sorted(name for name in classes if not VALID_COLLECTION_NAME.match(name))
    if invalid:
        raise SystemExit(f"Categories that are not valid ChromaDB collection names: {invalid}")
    return sorted(classes)


def load_pairs() -> list[dict]:
    with PAIRS_PATH.open(encoding="utf-8") as pairs_file:
        return [json.loads(line) for line in pairs_file if line.strip()]


def stable_id(conversation_id: str, timestamp: str, index: int) -> str:
    """Same pair → same id on every run, so upserts replace instead of duplicating."""
    return hashlib.sha256(f"{conversation_id}|{timestamp}|{index}".encode("utf-8")).hexdigest()


def main() -> None:
    pairs = load_pairs()
    relationship_map = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    default = relationship_map.get(DEFAULT_KEY, DEFAULT_VALUE)
    classes = relationship_classes(relationship_map)

    # Tag every pair; unmapped or still-unclassified conversations get the default.
    index_in_conversation: dict[str, int] = {}
    for pair in pairs:
        conversation_id = pair["conversation_id"]
        relationship = relationship_map.get(conversation_id, default)
        pair["relationship"] = default if relationship == PLACEHOLDER else relationship
        index = index_in_conversation.get(conversation_id, 0)
        index_in_conversation[conversation_id] = index + 1
        pair["id"] = stable_id(conversation_id, pair["timestamp"], index)

    print(f"Loaded {len(pairs)} pairs. Loading model {MODEL_NAME} (first run downloads it)...")
    model = SentenceTransformer(MODEL_NAME)

    client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    collections = {
        relationship: client.get_or_create_collection(name=relationship, metadata={"hnsw:space": "cosine"})
        for relationship in classes
    }

    for start in range(0, len(pairs), PROGRESS_EVERY):
        batch = pairs[start:start + PROGRESS_EVERY]
        embeddings = model.encode([pair["their_message"] for pair in batch]).tolist()
        for relationship in {pair["relationship"] for pair in batch}:
            members = [(pair, emb) for pair, emb in zip(batch, embeddings) if pair["relationship"] == relationship]
            collections[relationship].upsert(
                ids=[pair["id"] for pair, _ in members],
                embeddings=[emb for _, emb in members],
                documents=[pair["their_message"] for pair, _ in members],
                metadatas=[
                    {
                        "my_reply": pair["my_reply"],
                        "timestamp": pair["timestamp"],
                        "conversation_id": pair["conversation_id"],
                        "relationship": relationship,
                    }
                    for pair, _ in members
                ],
            )
        print(f"  {min(start + PROGRESS_EVERY, len(pairs))}/{len(pairs)} pairs embedded")

    # Authoritative sync: each collection must mirror processed_pairs.jsonl exactly, so
    # delete any id left over from an earlier run whose pair no longer exists.
    removed = {}
    for relationship, collection in collections.items():
        current_ids = {pair["id"] for pair in pairs if pair["relationship"] == relationship}
        stale_ids = [stored for stored in collection.get(include=[])["ids"] if stored not in current_ids]
        for start in range(0, len(stale_ids), 500):
            collection.delete(ids=stale_ids[start:start + 500])
        removed[relationship] = len(stale_ids)

    print("\nPairs per collection (total stored, stale removed):")
    for relationship, collection in collections.items():
        count = collection.count()
        print(f"  {collection.name}: {count}  (removed {removed[relationship]} stale)")
        if count == 0:
            print(f"  Warning: {collection.name} is empty — no conversation is mapped to '{relationship}'.")


if __name__ == "__main__":
    main()

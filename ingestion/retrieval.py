"""History retrieval: find my past replies to messages similar to an incoming one.

The ChromaDB client and the embedding model are created once, when this module is
imported, and reused by every call. Importing raises ConnectionError if the ChromaDB
server can't be reached; an empty or missing collection is not an error.
"""

import sys
from pathlib import Path

import chromadb
from chromadb.api import ClientAPI
from chromadb.errors import NotFoundError
from sentence_transformers import SentenceTransformer

# The ingestion scripts import each other by bare module name. Make that work when this
# module is imported as ingestion.retrieval from the project root, not only from ingestion/.
INGESTION_DIR = str(Path(__file__).resolve().parent)
if INGESTION_DIR not in sys.path:
    sys.path.insert(0, INGESTION_DIR)

from embed_to_chroma import CHROMA_HOST, CHROMA_PORT, MODEL_NAME  # noqa: E402


def _connect() -> ClientAPI:
    try:
        return chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
    except ValueError as error:
        raise ConnectionError(
            f"Could not connect to ChromaDB at {CHROMA_HOST}:{CHROMA_PORT} ({error}). Is the server running?"
        ) from error


client: ClientAPI = _connect()
embedder = SentenceTransformer(MODEL_NAME)


def collection_size(relationship: str) -> int:
    """Number of stored pairs for a relationship; 0 if its collection doesn't exist."""
    try:
        return client.get_collection(name=relationship).count()
    except NotFoundError:
        return 0


def retrieve_similar(relationship: str, incoming_message: str, k: int = 3) -> list[dict]:
    """Return up to k past pairs most similar to incoming_message, closest first.

    Each item is {"their_message": str, "my_reply": str, "distance": float}. Returns an
    empty list when the relationship has no collection, the collection is empty, or k < 1.
    Only a genuine ChromaDB connection failure raises.
    """
    if k < 1:
        return []
    try:
        collection = client.get_collection(name=relationship)
    except NotFoundError:
        return []
    available = collection.count()
    if available == 0:
        return []

    results = collection.query(
        query_embeddings=[embedder.encode(incoming_message).tolist()],
        n_results=min(k, available),
        include=["documents", "metadatas", "distances"],
    )
    # Chroma types these fields as optional; a query that asked for them returns them.
    documents, metadatas, distances = results["documents"], results["metadatas"], results["distances"]
    if not documents or not metadatas or not distances:
        return []
    return [
        {"their_message": document, "my_reply": str(metadata["my_reply"]), "distance": float(distance)}
        for document, metadata, distance in zip(documents[0], metadatas[0], distances[0])
    ]

"""Route an incoming WhatsApp chat (by Baileys JID) to a relationship and history collection.

Group, status-broadcast, and channel chats are never drafted for, so they resolve to a
relationship with no collection before the relationship map is ever read.
"""

import json
from pathlib import Path

from ingestion.whatsapp_ids import jid_to_phone, parse_jid


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAP_PATH = "config/relationship_map.json"
DEFAULT_KEY = "_default"
FALLBACK_RELATIONSHIP = "unknown"
GROUP = "group"
# Chats the agent never drafts in. Each is returned as-is with no collection.
NO_DRAFT_KINDS = {"group": GROUP, "broadcast": "broadcast", "newsletter": "newsletter"}


def _load_map(map_path: str) -> dict:
    path = Path(map_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        # Fail loudly: a broken map must be fixed, not silently routed as "unknown".
        raise ValueError(
            f"{path} is not valid JSON (line {error.lineno}, column {error.colno}: {error.msg}). "
            "Restore it from config/backups/ or re-run ingestion/build_phone_map.py, "
            "then check it with ingestion/validate_phone_map.py --name <your name>."
        ) from error


def resolve_relationship(
    jid: str, map_path: str = DEFAULT_MAP_PATH, alt_jid: str | None = None
) -> tuple[str, str | None]:
    """Return (relationship, collection_name) for the chat a message belongs to.

    - Group JIDs (@g.us) return ("group", None) without consulting the map; status
      broadcasts and channels likewise return ("broadcast"/"newsletter", None).
    - Person JIDs (@s.whatsapp.net / @c.us, with or without a :device suffix) are looked
      up by phone number. Unmatched numbers, and malformed or empty JIDs, get the map's
      "_default" (or "unknown" if that key is missing).
    - @lid JIDs are privacy ids, not phone numbers: pass the phone JID Baileys supplies
      alongside them as alt_jid, otherwise the lid's own digits are looked up.
    The collection name is the relationship itself (collections are named family,
    friends, girlfriend, professional, business_freelance, unknown).
    """
    try:
        parsed = parse_jid(jid or "")
    except ValueError:
        parsed = None

    if parsed and parsed.kind in NO_DRAFT_KINDS:
        return NO_DRAFT_KINDS[parsed.kind], None
    if (jid or "").strip().endswith("@g.us"):  # belt and braces: never route a group
        return GROUP, None

    relationship_map = _load_map(map_path)
    default = relationship_map.get(DEFAULT_KEY, FALLBACK_RELATIONSHIP)
    if parsed is None:
        return default, default

    if parsed.kind == "lid":
        number = (jid_to_phone(alt_jid) if alt_jid else None) or parsed.user
    else:
        number = jid_to_phone(jid) or parsed.user
    relationship = relationship_map.get(number, default)
    return relationship, relationship

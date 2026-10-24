#!/usr/bin/env python3
"""Parse WhatsApp JIDs the way Baileys emits them, and route a JID to a relationship.

Baileys identifies every chat and sender by a JID ("user[:device]@server"):
    919812345670@s.whatsapp.net      a person (user = phone number with country code)
    919812345670:12@s.whatsapp.net   the same person, from linked device 12
    919812345670@c.us                legacy person JID
    120363041234567890@g.us          a group chat (msg.key.remoteJid; sender is msg.key.participant)
    status@broadcast                 status updates
    123456789012345@lid              privacy "linked ID" — not a phone number; Baileys also
                                     provides the phone JID (e.g. key.remoteJidAlt / senderPn)
    120363...@newsletter             a channel
The live relationship map (config/relationship_map.json) is keyed by the bare phone number,
i.e. the JID user part for person JIDs.
"""

import re
from dataclasses import dataclass


PERSON_SERVERS = {"s.whatsapp.net", "c.us"}
GROUP_SERVER = "g.us"
BROADCAST_SERVER = "broadcast"
NEWSLETTER_SERVER = "newsletter"
LID_SERVER = "lid"

# E.164: country code + subscriber number, 8–15 digits, never starting with 0.
E164_DIGITS = re.compile(r"^[1-9]\d{7,14}$")
JID_PATTERN = re.compile(r"^(?P<user>[^@:]*)(?::(?P<device>\d+))?@(?P<server>[\w.]+)$")


@dataclass(frozen=True)
class Jid:
    user: str
    device: int | None
    server: str

    @property
    def kind(self) -> str:
        if self.server in PERSON_SERVERS:
            return "person"
        if self.server == GROUP_SERVER:
            return "group"
        if self.server == BROADCAST_SERVER:
            return "broadcast"
        if self.server == NEWSLETTER_SERVER:
            return "newsletter"
        if self.server == LID_SERVER:
            return "lid"
        return "unknown"


def parse_jid(jid: str) -> Jid:
    match = JID_PATTERN.match(jid.strip())
    if not match:
        raise ValueError(f"Not a WhatsApp JID: {jid!r}")
    device = match.group("device")
    return Jid(match.group("user"), int(device) if device else None, match.group("server").lower())


def normalize_phone(raw: str, default_country: str | None = None) -> str:
    """'+91 98123-45670' or '0091…' -> '919812345670' (digits with country code, no '+').

    A number without '+' or '00' carries no country code, so '9812345670' could be an
    Indian mobile or country code 98. It is rejected unless default_country is given
    (e.g. '91'), in which case a leading trunk '0' is dropped and the code is prefixed.
    """
    stripped = raw.strip()
    digits = re.sub(r"\D", "", stripped)
    if stripped.startswith("00"):
        digits = digits[2:]
    elif not stripped.startswith("+"):
        if default_country is None:
            raise ValueError(f"Missing country code (expected '+' or '00' prefix): {raw!r}")
        digits = default_country + digits.lstrip("0")
    if not E164_DIGITS.match(digits):
        raise ValueError(f"Not an international phone number: {raw!r}")
    return digits


def phone_to_jid(phone: str, default_country: str | None = None) -> str:
    return f"{normalize_phone(phone, default_country)}@s.whatsapp.net"


def jid_to_phone(jid: str) -> str | None:
    """Phone number for a person JID (device suffix dropped), else None."""
    parsed = parse_jid(jid)
    if parsed.kind != "person" or not E164_DIGITS.match(parsed.user):
        return None
    return parsed.user


def relationship_for_jid(jid: str, phone_map: dict, alt_jid: str | None = None) -> str | None:
    """Relationship for the chat a message came from.

    Returns None when the agent must not draft at all: groups, broadcasts/status, and
    channels. A person not in the map gets the map's _default. For a @lid JID, pass the
    phone JID Baileys supplies alongside it as alt_jid; without it the sender is unknown.
    """
    kind = parse_jid(jid).kind
    if kind in {"group", "broadcast", "newsletter"}:
        return None
    phone = jid_to_phone(alt_jid if kind == "lid" and alt_jid else jid)
    default = phone_map.get("_default", "unknown")
    return phone_map.get(phone, default) if phone else default

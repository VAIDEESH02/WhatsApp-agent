"""Tests for agent.router.resolve_relationship. Run: python3 -m pytest agent/test_router.py -v"""

import json

import pytest

from agent.router import resolve_relationship


KNOWN = "919000000011"


@pytest.fixture
def map_path(tmp_path):
    """A small stand-in for config/relationship_map.json, so tests don't depend on private data."""
    path = tmp_path / "relationship_map.json"
    path.write_text(
        json.dumps(
            {
                "_default": "unknown",
                KNOWN: "business_freelance",
                "919000000012": "family",
                # A hostile entry: even if a group id leaks into the map, it must not be used.
                "120363041234567890": "family",
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def test_known_number_resolves(map_path):
    assert resolve_relationship(f"{KNOWN}@s.whatsapp.net", map_path) == ("business_freelance", "business_freelance")


def test_known_number_from_linked_device_and_legacy_jid(map_path):
    assert resolve_relationship(f"{KNOWN}:12@s.whatsapp.net", map_path) == ("business_freelance", "business_freelance")
    assert resolve_relationship("919000000012@c.us", map_path) == ("family", "family")


def test_unknown_number_falls_back_to_default(map_path):
    assert resolve_relationship("15551234567@s.whatsapp.net", map_path) == ("unknown", "unknown")


def test_missing_default_key_falls_back_to_unknown(tmp_path):
    path = tmp_path / "relationship_map.json"
    path.write_text(json.dumps({KNOWN: "friends"}), encoding="utf-8")
    assert resolve_relationship("15551234567@s.whatsapp.net", str(path)) == ("unknown", "unknown")


def test_invalid_json_map_fails_loudly_for_a_person(tmp_path):
    broken = tmp_path / "relationship_map.json"
    broken.write_text('{"_default": "unknown",}', encoding="utf-8")
    with pytest.raises(ValueError, match="relationship_map.json is not valid JSON"):
        resolve_relationship(f"{KNOWN}@s.whatsapp.net", str(broken))


@pytest.mark.parametrize(
    "group_jid",
    ["120363041234567890@g.us", "919000000012-1600000000@g.us", "  120363041234567890@g.us  "],
)
def test_group_jid_always_returns_group_none(group_jid, map_path, tmp_path):
    # Regardless of map contents — including a map entry for the group's own id ...
    assert resolve_relationship(group_jid, map_path) == ("group", None)
    # ... and even when the map file doesn't exist or is not valid JSON at all.
    assert resolve_relationship(group_jid, str(tmp_path / "missing.json")) == ("group", None)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert resolve_relationship(group_jid, str(broken)) == ("group", None)


def test_group_jid_ignores_alt_jid(map_path):
    # A group message's sender (participant) must not make the group routable.
    assert resolve_relationship("120363041234567890@g.us", map_path, alt_jid=f"{KNOWN}@s.whatsapp.net") == ("group", None)


@pytest.mark.parametrize("jid, expected", [("status@broadcast", "broadcast"), ("120363111111111111@newsletter", "newsletter")])
def test_broadcast_and_channel_are_never_routed(jid, expected, map_path):
    assert resolve_relationship(jid, map_path) == (expected, None)


def test_lid_uses_alt_phone_jid_when_given(map_path):
    assert resolve_relationship("123456789012345@lid", map_path, alt_jid=f"{KNOWN}@s.whatsapp.net") == (
        "business_freelance",
        "business_freelance",
    )


def test_lid_without_alt_falls_back_to_default(map_path):
    assert resolve_relationship("123456789012345@lid", map_path) == ("unknown", "unknown")


@pytest.mark.parametrize("bad_jid", ["", "   ", "not-a-jid", "@s.whatsapp.net", "9198845@@x", None])
def test_malformed_or_empty_jid_returns_safe_fallback(bad_jid, map_path):
    relationship, collection = resolve_relationship(bad_jid, map_path)
    assert relationship == "unknown"
    assert collection == "unknown"


def test_missing_map_file_falls_back_to_unknown(tmp_path):
    assert resolve_relationship(f"{KNOWN}@s.whatsapp.net", str(tmp_path / "missing.json")) == ("unknown", "unknown")


def test_real_map_routes_groups_and_known_contact():
    # Uses the project's own config/relationship_map.json (git-ignored, so skipped on a fresh
    # clone). A number is read from the map itself, so no real number is written in this file.
    from agent.router import DEFAULT_KEY, DEFAULT_MAP_PATH, PROJECT_ROOT

    path = PROJECT_ROOT / DEFAULT_MAP_PATH
    if not path.exists():
        pytest.skip("no config/relationship_map.json on this machine")
    real_map = json.loads(path.read_text(encoding="utf-8"))
    number, relationship = next((k, v) for k, v in sorted(real_map.items()) if k != DEFAULT_KEY)
    assert resolve_relationship("120363041234567890@g.us") == ("group", None)
    assert resolve_relationship(f"{number}@s.whatsapp.net") == (relationship, relationship)

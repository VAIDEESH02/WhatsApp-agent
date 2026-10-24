"""Tests for config/settings.py. Run: python3 -m pytest config/test_settings.py -v"""

import json

import pytest

from config import settings


@pytest.fixture(autouse=True)
def temp_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "KILL_SWITCH_PATH", tmp_path / "kill_switch.flag")
    monkeypatch.setattr(settings, "RELATIONSHIP_MAP_PATH", tmp_path / "relationship_map.json")
    return tmp_path


def write(path, content):
    path.write_text(content if isinstance(content, str) else json.dumps(content))


# --- load_settings / is_dry_run ---------------------------------------------------------

def test_missing_file_gives_safe_defaults():
    assert settings.load_settings() == {"dry_run": True, "min_delay_seconds": 3, "max_delay_seconds": 12}
    assert settings.is_dry_run() is True


@pytest.mark.parametrize("content", ["{not json", "", "[1, 2]", '"dry_run"', "null"])
def test_unparseable_or_wrong_shape_gives_defaults(content):
    write(settings.SETTINGS_PATH, content)
    assert settings.load_settings() == settings.DEFAULT_SETTINGS
    assert settings.is_dry_run() is True


def test_valid_file_overrides_defaults():
    write(settings.SETTINGS_PATH, {"dry_run": False, "min_delay_seconds": 5, "max_delay_seconds": 9, "future_key": "x"})
    assert settings.load_settings() == {"dry_run": False, "min_delay_seconds": 5, "max_delay_seconds": 9, "future_key": "x"}
    assert settings.is_dry_run() is False


@pytest.mark.parametrize("value", ["false", 0, None, "no", [], {}])
def test_dry_run_only_turns_off_for_literal_false(value):
    write(settings.SETTINGS_PATH, {"dry_run": value})
    assert settings.is_dry_run() is True


@pytest.mark.parametrize(
    "low, high",
    [(10, 2), (-1, 5), ("3", 12), (3, None), (True, 12), (3, float("nan"))],
)
def test_bad_delays_fall_back_to_default_pair(low, high):
    write(settings.SETTINGS_PATH, {"min_delay_seconds": low, "max_delay_seconds": high})
    loaded = settings.load_settings()
    assert (loaded["min_delay_seconds"], loaded["max_delay_seconds"]) == (3, 12)


def test_partial_file_keeps_other_defaults():
    write(settings.SETTINGS_PATH, {"max_delay_seconds": 20})
    assert settings.load_settings() == {"dry_run": True, "min_delay_seconds": 3, "max_delay_seconds": 20}


def test_load_settings_never_raises_on_unreadable_path(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path)  # a directory, not a file
    assert settings.load_settings() == settings.DEFAULT_SETTINGS


# --- kill switch -------------------------------------------------------------------------

def test_kill_switch_follows_the_flag_file():
    assert settings.is_kill_switch_active() is False
    settings.KILL_SWITCH_PATH.touch()
    assert settings.is_kill_switch_active() is True
    settings.KILL_SWITCH_PATH.unlink()
    assert settings.is_kill_switch_active() is False


# --- allowlist -----------------------------------------------------------------------------

MAP = {
    "_default": "unknown",
    "919000000001": "friends",
    "919000000002": "family",
    "919000000003": "unknown",
    "919000000004": "REPLACE_ME",
    "13102372227": "business_freelance",
}


def test_allowlist_is_every_classified_number():
    write(settings.RELATIONSHIP_MAP_PATH, MAP)
    assert settings.get_allowlist() == {"919000000001", "919000000002", "13102372227"}


def test_missing_map_allows_nobody():
    assert settings.get_allowlist() == set()
    assert settings.enforce_allowlist("919000000001@s.whatsapp.net") is False


def test_corrupt_map_fails_loudly():
    write(settings.RELATIONSHIP_MAP_PATH, "{broken")
    with pytest.raises(ValueError, match="not valid JSON"):
        settings.get_allowlist()


@pytest.mark.parametrize(
    "jid, allowed",
    [
        ("919000000001@s.whatsapp.net", True),
        ("919000000001:12@s.whatsapp.net", True),  # linked device
        ("919000000002@c.us", True),  # legacy JID
        ("13102372227@s.whatsapp.net", True),
        ("919000000003@s.whatsapp.net", False),  # mapped as unknown
        ("919000000004@s.whatsapp.net", False),  # still REPLACE_ME
        ("15550000000@s.whatsapp.net", False),  # not in the map
        ("_default@s.whatsapp.net", False),
        ("919000000001@g.us", False),  # group id that collides with a person's number
        ("120363041234567890@g.us", False),
        ("status@broadcast", False),
        ("919000000001@lid", False),  # privacy id, not a phone number
        ("", False),
        ("not-a-jid", False),
        (None, False),
    ],
)
def test_enforce_allowlist(jid, allowed):
    write(settings.RELATIONSHIP_MAP_PATH, MAP)
    assert settings.enforce_allowlist(jid) is allowed


# --- update_settings / set_kill_switch -------------------------------------------------------

def test_update_changes_only_the_given_key():
    write(settings.SETTINGS_PATH, {"dry_run": True, "min_delay_seconds": 4, "max_delay_seconds": 9, "note": "keep me"})
    settings.update_settings(dry_run=False)
    assert json.loads(settings.SETTINGS_PATH.read_text()) == {"dry_run": False, "min_delay_seconds": 4, "max_delay_seconds": 9, "note": "keep me"}
    settings.update_settings(min_delay_seconds=2, max_delay_seconds=5)
    assert json.loads(settings.SETTINGS_PATH.read_text()) == {"dry_run": False, "min_delay_seconds": 2, "max_delay_seconds": 5, "note": "keep me"}


def test_update_from_missing_or_broken_file_starts_from_defaults():
    assert settings.update_settings(dry_run=False) == {"dry_run": False, "min_delay_seconds": 3, "max_delay_seconds": 12}
    write(settings.SETTINGS_PATH, "{broken")
    assert settings.update_settings(max_delay_seconds=20)["max_delay_seconds"] == 20


@pytest.mark.parametrize(
    "kwargs",
    [{"dry_run": "false"}, {"dry_run": 0}, {"min_delay_seconds": 5, "max_delay_seconds": 5}, {"min_delay_seconds": 0, "max_delay_seconds": 4},
     {"min_delay_seconds": -2, "max_delay_seconds": 4}, {"max_delay_seconds": 1}, {"min_delay_seconds": "2", "max_delay_seconds": 4},
     {"min_delay_seconds": True, "max_delay_seconds": 4}],
)
def test_invalid_updates_raise_and_leave_the_file_untouched(kwargs):
    write(settings.SETTINGS_PATH, {"dry_run": True, "min_delay_seconds": 3, "max_delay_seconds": 12})
    before = settings.SETTINGS_PATH.read_bytes()
    with pytest.raises(settings.SettingsError):
        settings.update_settings(**kwargs)
    assert settings.SETTINGS_PATH.read_bytes() == before


def test_atomic_write_leaves_no_temp_files_and_readers_never_see_partial_json(tmp_path):
    import threading

    stop, bad_reads = threading.Event(), []

    def reader():
        while not stop.is_set():
            try:
                json.loads(settings.SETTINGS_PATH.read_text())
            except FileNotFoundError:
                pass
            except ValueError as error:  # a half-written file would land here
                bad_reads.append(error)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for i in range(300):
            settings.update_settings(dry_run=bool(i % 2), min_delay_seconds=1 + i % 3, max_delay_seconds=10 + i % 5)
    finally:
        stop.set()
        thread.join()
    assert bad_reads == []
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_concurrent_updates_never_lose_keys():
    import threading

    write(settings.SETTINGS_PATH, {"dry_run": True, "min_delay_seconds": 3, "max_delay_seconds": 12})
    threads = [threading.Thread(target=settings.update_settings, kwargs={"dry_run": False}) for _ in range(10)]
    threads += [threading.Thread(target=settings.update_settings, kwargs={"min_delay_seconds": 2, "max_delay_seconds": 6}) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert json.loads(settings.SETTINGS_PATH.read_text()) == {"dry_run": False, "min_delay_seconds": 2, "max_delay_seconds": 6}


def test_set_kill_switch_creates_an_empty_flag_and_removes_it():
    assert settings.set_kill_switch(True) is True and settings.KILL_SWITCH_PATH.read_bytes() == b""
    assert settings.set_kill_switch(True) is True  # idempotent
    assert settings.set_kill_switch(False) is False and not settings.KILL_SWITCH_PATH.exists()
    assert settings.set_kill_switch(False) is False

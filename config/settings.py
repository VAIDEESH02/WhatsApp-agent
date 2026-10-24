"""Runtime settings, the kill switch, and the reply allowlist.

- load_settings() / is_dry_run(): config/settings.json, with safe defaults. Never raises:
  a missing, unreadable, or malformed file (or a bad value) falls back to the defaults,
  and dry_run is only False when the file says exactly `"dry_run": false`.
- is_kill_switch_active(): True while kill_switch.flag exists in the repo root
  (`touch kill_switch.flag` to stop everything, `rm kill_switch.flag` to resume).
- get_allowlist() / enforce_allowlist(): derived from config/relationship_map.json, the
  phone map you already maintain, so there is no second list to drift out of sync.
"""

import json
import os
import tempfile
import threading
from pathlib import Path

from agent.router import DEFAULT_MAP_PATH, _load_map
from ingestion.whatsapp_ids import jid_to_phone


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
KILL_SWITCH_PATH = PROJECT_ROOT / "kill_switch.flag"
RELATIONSHIP_MAP_PATH = PROJECT_ROOT / DEFAULT_MAP_PATH

DEFAULT_SETTINGS = {"dry_run": True, "min_delay_seconds": 3, "max_delay_seconds": 12}
DEFAULT_KEY = "_default"
NOT_ALLOWED = {"unknown", "REPLACE_ME"}  # REPLACE_ME: not classified yet, so not allowed yet


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_settings() -> dict:
    """Settings from config/settings.json merged over safe defaults. Never raises."""
    settings = dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if not isinstance(raw, dict):
        return settings

    # Unknown keys pass through untouched, for settings added later.
    settings.update({key: value for key, value in raw.items() if key not in DEFAULT_SETTINGS})
    if isinstance(raw.get("dry_run"), bool):
        settings["dry_run"] = raw["dry_run"]
    low = raw.get("min_delay_seconds", settings["min_delay_seconds"])
    high = raw.get("max_delay_seconds", settings["max_delay_seconds"])
    if _is_number(low) and _is_number(high) and 0 <= low <= high:
        settings["min_delay_seconds"], settings["max_delay_seconds"] = low, high
    return settings


def is_dry_run() -> bool:
    return load_settings()["dry_run"] is not False


class SettingsError(ValueError):
    """A settings change that fails validation; nothing is written."""


_write_lock = threading.Lock()  # one read-modify-write at a time within this process


def _write_settings_atomic(data: dict) -> None:
    """Write to a unique temp file beside settings.json, check it parses, then rename it into place.

    os.replace is atomic, so a reader at the same moment sees either the old file or the new
    one, never a half-written file. On any failure the old file is left untouched.
    """
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=SETTINGS_PATH.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(json.dumps(data, indent=2) + "\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        if json.loads(tmp.read_text(encoding="utf-8")) != data:
            raise SettingsError(f"{tmp} did not read back as written")
        os.replace(tmp, SETTINGS_PATH)
    finally:
        tmp.unlink(missing_ok=True)


def update_settings(dry_run: bool | None = None, min_delay_seconds=None, max_delay_seconds=None) -> dict:
    """Change only the given keys in config/settings.json (others, including unknown keys, are kept).

    dry_run must be a real bool. Delays must be positive numbers with min < max (a value you
    don't pass is taken from the current settings). Raises SettingsError without writing anything.
    """
    with _write_lock:
        try:
            existing = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        current = load_settings()  # validated view, for values not being changed
        updated = {**DEFAULT_SETTINGS, **existing}

        if dry_run is not None:
            if not isinstance(dry_run, bool):
                raise SettingsError("dry_run must be true or false")
            updated["dry_run"] = dry_run
        if min_delay_seconds is not None or max_delay_seconds is not None:
            low = current["min_delay_seconds"] if min_delay_seconds is None else min_delay_seconds
            high = current["max_delay_seconds"] if max_delay_seconds is None else max_delay_seconds
            if not (_is_number(low) and _is_number(high)):
                raise SettingsError("delays must be numbers of seconds")
            if low <= 0 or high <= 0:
                raise SettingsError("delays must be positive")
            if low >= high:
                raise SettingsError("the minimum delay must be less than the maximum")
            updated["min_delay_seconds"], updated["max_delay_seconds"] = low, high

        _write_settings_atomic(updated)
    return load_settings()


def is_kill_switch_active() -> bool:
    return KILL_SWITCH_PATH.exists()


def set_kill_switch(active: bool) -> bool:
    """Create (active) or delete the empty kill_switch.flag; returns the resulting state."""
    if active:
        KILL_SWITCH_PATH.touch(exist_ok=True)
    else:
        KILL_SWITCH_PATH.unlink(missing_ok=True)
    return is_kill_switch_active()


def get_allowlist() -> set[str]:
    """Every phone number in the relationship map with a real category.

    A missing map gives an empty allowlist (nobody is allowed). A map that isn't valid
    JSON raises ValueError, exactly like agent.router: a broken map must be fixed, not guessed.
    """
    relationship_map = _load_map(str(RELATIONSHIP_MAP_PATH))
    return {
        number
        for number, relationship in relationship_map.items()
        if number != DEFAULT_KEY and isinstance(relationship, str) and relationship not in NOT_ALLOWED
    }


def enforce_allowlist(jid: str) -> bool:
    """True only for a person's JID whose number is on the allowlist.

    The number is read the way agent.router reads it: the part before "@", minus any
    linked-device suffix (919…:12@s.whatsapp.net -> 919…). Groups, broadcasts, channels,
    @lid privacy ids, and malformed or empty JIDs are never allowed.
    """
    try:
        phone = jid_to_phone(jid or "")
    except ValueError:
        return False
    return phone is not None and phone in get_allowlist()

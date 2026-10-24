"""The auto-reply on/off switch (config/auto_reply.json).

Read on every call, so flipping the switch takes effect immediately, without restarting
the bridge. Anything other than an explicit `"auto_reply_enabled": true` counts as OFF:
a missing, unreadable, or malformed file never turns auto-replies on.
"""

import json
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTO_REPLY_PATH = PROJECT_ROOT / "config" / "auto_reply.json"
SWITCH_KEY = "auto_reply_enabled"


def auto_reply_enabled() -> bool:
    try:
        settings = json.loads(AUTO_REPLY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(settings, dict) and settings.get(SWITCH_KEY) is True


def set_auto_reply_enabled(enabled: bool) -> bool:
    """Write the switch atomically (temp file, read back, replace) and return the stored value."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    data = {SWITCH_KEY: enabled}
    tmp = AUTO_REPLY_PATH.with_name(AUTO_REPLY_PATH.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        if json.loads(tmp.read_text(encoding="utf-8")) != data:
            raise ValueError(f"{tmp} did not read back as written")
        os.replace(tmp, AUTO_REPLY_PATH)
    finally:
        tmp.unlink(missing_ok=True)
    return auto_reply_enabled()

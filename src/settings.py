"""User settings persisted to ~/.config/dictation/settings.json."""

import json
import logging
import os

from src import config

log = logging.getLogger("dictation")

SETTINGS_PATH = os.path.join(config.CONFIG_DIR, "settings.json")

DEFAULTS = {
    "hotkey_keycode": 61,  # Right Option
}


def load() -> dict:
    """Settings merged over defaults. A missing or corrupt file yields defaults."""
    data = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            data.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        log.exception("Unreadable settings file — using defaults")
    return data


def save(**changes):
    """Merge changes into the stored settings. Atomic: write temp, then rename."""
    data = load()
    data.update(changes)
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)

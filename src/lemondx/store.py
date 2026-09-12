"""Persistent user settings: module defaults, and saved bootstrap profiles.

Everything lives in one JSON file under the user's config directory, alongside
the ``modules/`` directory that holds uploaded modules. Writes are atomic so a
crash mid-save cannot leave a truncated file behind.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading

SETTINGS_VERSION = 1
_lock = threading.Lock()

DEFAULTS = {
    "version": SETTINGS_VERSION,
    # Module ids pre-selected when creating a container.
    "default_modules": [],
    # module id -> {PARAM: value}; these override the module's own defaults.
    "module_params": {},
    # name -> {description, modules, params}
    "profiles": {},
}


def config_dir():
    base = os.environ.get("LEMONDX_CONFIG_DIR")
    if base:
        return base
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config_home, "lemondx")


def user_module_dir():
    return os.path.join(config_dir(), "modules")


def settings_path():
    return os.path.join(config_dir(), "settings.json")


def load():
    """Current settings, with any missing keys filled in."""
    settings = dict(DEFAULTS)
    settings["module_params"] = {}
    settings["profiles"] = {}
    settings["default_modules"] = []

    try:
        with open(settings_path(), encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return settings          # missing or corrupt: fall back to defaults

    if not isinstance(stored, dict):
        return settings
    for key in ("default_modules", "module_params", "profiles"):
        value = stored.get(key)
        if isinstance(value, type(settings[key])):
            settings[key] = value
    return settings


def save(settings):
    """Write settings atomically, creating the config directory if needed."""
    directory = config_dir()
    os.makedirs(directory, exist_ok=True)
    payload = dict(settings)
    payload["version"] = SETTINGS_VERSION

    with _lock:
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=".settings-",
            suffix=".tmp", delete=False,
        )
        try:
            with handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, settings_path())
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise
    return payload


def update(mutate):
    """Read-modify-write under the lock. ``mutate`` edits the dict in place."""
    settings = load()
    mutate(settings)
    return save(settings)

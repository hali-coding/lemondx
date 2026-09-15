"""Persistent user state: module defaults, remembered parameters, profiles, templates.

Everything lemondx keeps between runs lives under one data directory,
``~/.local/share/lemondx`` by default::

    settings.json     pre-selected modules and remembered parameter values
    profiles/         one JSON file per bootstrap profile
    templates/        one JSON file per instance template
    modules/          uploaded modules

This is state lemondx writes for itself rather than hand-authored
configuration, which is what ``XDG_DATA_HOME`` is for. Earlier versions kept it
all under ``~/.config/lemondx``; that directory is adopted on first use, so
nothing has to be moved by hand.

Writes are atomic so a crash mid-save cannot leave a truncated file behind.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading

# 1 kept profiles inside settings.json; 2 moved them to profiles/.
SETTINGS_VERSION = 2

_lock = threading.Lock()
# Re-entrant: the migration writes through save() and save_profile(), which
# come back through data_dir() and so through _migrate() again.
_migrate_lock = threading.RLock()
_migrated = False


# -- locations -------------------------------------------------------------


def _default_data_dir():
    home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(home, "lemondx")


def _legacy_config_dir():
    home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(home, "lemondx")


def data_dir():
    """The directory holding everything lemondx persists.

    ``LEMONDX_CONFIG_DIR`` is still honoured alongside ``LEMONDX_DATA_DIR``: it
    named this directory before the move, and scripts pass it. An explicitly
    chosen directory is never migrated away from -- the choice is the answer.
    """
    explicit = os.environ.get("LEMONDX_DATA_DIR") or os.environ.get("LEMONDX_CONFIG_DIR")
    directory = explicit or _default_data_dir()
    _migrate(directory, adopt_legacy=not explicit)
    return directory


def user_module_dir():
    return os.path.join(data_dir(), "modules")


def settings_path():
    return os.path.join(data_dir(), "settings.json")


def profiles_dir():
    return os.path.join(data_dir(), "profiles")


def templates_dir():
    return os.path.join(data_dir(), "templates")


# -- migration -------------------------------------------------------------


def _migrate(target, adopt_legacy):
    """Bring older layouts up to date, once per process.

    Both steps are idempotent and best-effort: failing to migrate must not stop
    lemondx from running, it just leaves the old state where it was.
    """
    global _migrated
    with _migrate_lock:
        if _migrated:
            return
        _migrated = True          # set first; the steps below re-enter data_dir()
        try:
            if adopt_legacy:
                _adopt_legacy_dir(target)
            _split_profiles_out(os.path.join(target, "settings.json"))
        except OSError:
            pass


def _adopt_legacy_dir(target):
    """Move ~/.config/lemondx to the data directory, if we have nothing yet."""
    legacy = _legacy_config_dir()
    if os.path.isdir(target) and not os.listdir(target):
        os.rmdir(target)          # an empty shell; there is nothing to lose
    if os.path.exists(target) or not os.path.isdir(legacy):
        return
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    shutil.move(legacy, target)


def _split_profiles_out(path):
    """Move profiles out of settings.json, one file each.

    Profiles used to be a ``profiles`` key inside settings.json. Finding one
    still there writes every entry to profiles/ and drops the key. An older
    file is rewritten even when it holds no profiles, so its version stops
    claiming a layout it no longer has.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return
    if not isinstance(stored, dict):
        return
    profiles = stored.pop("profiles", None)
    if profiles is None and stored.get("version") == SETTINGS_VERSION:
        return                    # already in the current layout
    if isinstance(profiles, dict):
        for name, entry in profiles.items():
            if isinstance(entry, dict):
                save_profile(name, entry)
    save(stored)


# -- atomic writes ---------------------------------------------------------


def _write_json(path, payload):
    """Replace a JSON file atomically, creating its directory if needed."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=".tmp-",
        suffix=".json", delete=False,
    )
    try:
        with handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


# -- settings --------------------------------------------------------------


def _blank():
    return {
        "version": SETTINGS_VERSION,
        # Module ids pre-selected when creating a container.
        "default_modules": [],
        # module id -> {PARAM: value}; these override the module's own defaults.
        "module_params": {},
    }


def load():
    """Current settings, with any missing keys filled in."""
    settings = _blank()
    try:
        with open(settings_path(), encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return settings          # missing or corrupt: fall back to defaults

    if not isinstance(stored, dict):
        return settings
    for key in ("default_modules", "module_params"):
        value = stored.get(key)
        if isinstance(value, type(settings[key])):
            settings[key] = value
    return settings


def save(settings):
    """Write settings atomically, creating the data directory if needed."""
    payload = dict(settings)
    payload["version"] = SETTINGS_VERSION
    payload.pop("profiles", None)          # profiles live in profiles/ now
    with _lock:
        _write_json(settings_path(), payload)
    return payload


def update(mutate):
    """Read-modify-write under the lock. ``mutate`` edits the dict in place."""
    settings = load()
    mutate(settings)
    return save(settings)


# -- profiles and templates ------------------------------------------------
#
# A profile is a named module selection with its parameters and SSH keys; a
# template is everything needed to create an instance, with such a selection
# embedded. Both are one file each, so they can be copied between machines or
# dropped in from a repo, and a file hand-edited into nonsense costs that one
# record rather than all of them. The real name is inside the file; the
# filename is only a slug of it, since names may contain spaces and mixed case.

_SLUG_STRIP = re.compile(r"[^a-z0-9._-]+")
_SLUG_DASHES = re.compile(r"-{2,}")

INSTANCE_TYPES = ("container", "virtual-machine")


def profile_slug(name):
    """A filename stem for a profile or template name: never empty, never a path."""
    slug = _SLUG_DASHES.sub("-", _SLUG_STRIP.sub("-", (name or "").strip().lower()))
    return slug.strip("-._") or "profile"


def _text(value, limit):
    return str(value if value is not None else "").strip()[:limit]


def _strings(value):
    return list(dict.fromkeys(
        str(v) for v in (value if isinstance(value, list) else [])))


def _clean_selection(stored):
    """Modules, params and keys, as both record kinds hold them.

    Keys are only shaped here, not validated: the service parses every one
    before it is listed or used, and a single-line string is all this layer
    can promise without importing it.
    """
    params = stored.get("params")
    return {
        "modules": _strings(stored.get("modules")),
        "params": {str(k): str(v) for k, v in
                   (params.items() if isinstance(params, dict) else ())},
        "ssh_keys": [k for k in (s.strip() for s in _strings(stored.get("ssh_keys")))
                     if k and "\n" not in k and "\r" not in k],
    }


def _clean_profile(stored, name):
    """Normalise one profile record. These files are meant to be edited by hand."""
    record = {
        "name": name,
        "description": _text(stored.get("description"), 200),
    }
    record.update(_clean_selection(stored))
    return record


def _clean_template(stored, name):
    """Normalise one template record, with the same tolerance as a profile."""
    bootstrap = stored.get("bootstrap")
    kind = stored.get("type")
    return {
        "name": name,
        "description": _text(stored.get("description"), 200),
        "name_prefix": _text(stored.get("name_prefix"), 50),
        "image": _text(stored.get("image"), 200),
        "type": kind if kind in INSTANCE_TYPES else "container",
        "cpu": _text(stored.get("cpu"), 32),
        "memory": _text(stored.get("memory"), 32),
        "disk": _text(stored.get("disk"), 32),
        "pool": _text(stored.get("pool"), 64),
        "network": _text(stored.get("network"), 64),
        "profiles": _strings(stored.get("profiles")),
        "ephemeral": stored.get("ephemeral") is True,
        # Absent means start: an instance that never boots cannot be bootstrapped.
        "start": stored.get("start") is not False,
        # Absent means on, and only a VM can have it off.
        "secureboot": not (kind == "virtual-machine" and stored.get("secureboot") is False),
        "bootstrap": _clean_selection(bootstrap if isinstance(bootstrap, dict) else {}),
    }


class _Records:
    """One directory of named JSON records, with a cleaner applied on read."""

    def __init__(self, directory, clean):
        self.directory = directory
        self.clean = clean

    def scan(self):
        """(path, name, record) for every readable file."""
        directory = self.directory()
        try:
            filenames = sorted(os.listdir(directory))
        except OSError:
            return []

        found = []
        for filename in filenames:
            if filename.startswith(".") or not filename.endswith(".json"):
                continue
            path = os.path.join(directory, filename)
            try:
                with open(path, encoding="utf-8") as handle:
                    stored = json.load(handle)
            except (OSError, ValueError):
                continue          # unreadable or not JSON any more; skip just this one
            if not isinstance(stored, dict):
                continue
            name = stored.get("name")
            if not isinstance(name, str) or not name.strip():
                name = filename[:-len(".json")]          # dropped in without a name
            found.append((path, name.strip(), self.clean(stored, name.strip())))
        return found

    def load(self):
        """Every record on disk, keyed by name.

        Two files can claim the same name if one was copied in by hand. The
        first by filename wins, which is also the one save and delete act on.
        """
        records = {}
        for _, name, record in self.scan():
            records.setdefault(name, record)
        return records

    def path_for(self, name):
        for path, existing, _ in self.scan():
            if existing == name:
                return path
        return None

    def free_path(self, slug):
        """An unused <slug>.json, suffixed when two names slug alike."""
        directory = self.directory()
        candidate = os.path.join(directory, slug + ".json")
        attempt = 2
        while os.path.exists(candidate):
            candidate = os.path.join(directory, "%s-%d.json" % (slug, attempt))
            attempt += 1
        return candidate

    def save(self, name, record):
        """Write one record, reusing its file when that name already exists."""
        name = (name or "").strip()
        payload = self.clean(record, name)
        # Outside the lock: finding the directory can run the migration,
        # which saves profiles itself.
        path = self.path_for(name) or self.free_path(profile_slug(name))
        with _lock:
            _write_json(path, payload)
        return payload

    def delete(self, name):
        """Remove a record. Returns False if there was no such name."""
        path = self.path_for((name or "").strip())
        if path is None:
            return False
        try:
            os.unlink(path)
        except OSError:
            return False
        return True


_profiles = _Records(profiles_dir, _clean_profile)
_templates = _Records(templates_dir, _clean_template)

load_profiles = _profiles.load
save_profile = _profiles.save
delete_profile = _profiles.delete

load_templates = _templates.load
save_template = _templates.save
delete_template = _templates.delete


def prune_module(module_id):
    """Forget a module that no longer exists: in settings, profiles and templates.

    A profile left with nothing to run would be unusable -- saving one requires
    at least one module -- so it is deleted rather than kept as an empty shell.
    A template still describes an instance without it, so it only loses the
    module. Parameters are left alone in both: they are one flat set, and
    another selected module may declare the same name.
    """
    def mutate(settings):
        settings.get("module_params", {}).pop(module_id, None)
        settings["default_modules"] = [
            m for m in (settings.get("default_modules") or []) if m != module_id]
    update(mutate)

    for path, name, record in _profiles.scan():
        if module_id not in record["modules"]:
            continue
        remaining = [m for m in record["modules"] if m != module_id]
        if remaining:
            save_profile(name, dict(record, modules=remaining))
        else:
            try:
                os.unlink(path)
            except OSError:
                pass

    for _, name, record in _templates.scan():
        bootstrap = record["bootstrap"]
        if module_id in bootstrap["modules"]:
            save_template(name, dict(record, bootstrap=dict(
                bootstrap, modules=[m for m in bootstrap["modules"] if m != module_id])))

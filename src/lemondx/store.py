"""Persistent user state: module defaults, remembered parameters, profiles, templates.

Everything lemondx keeps between runs lives under one data directory,
``~/.local/share/lemondx`` by default::

    settings.json     pre-selected modules and remembered parameter values
    profiles/         one JSON file per bootstrap profile
    templates/        one JSON file per instance template
    stacks/           one JSON file per stack (templates launched in sequence)
    nodes/            one JSON file per federated lemondx node
    node-groups/      one JSON file per node group
    modules/          uploaded modules
    auth/             local users and API tokens (0700; only hashes, never secrets)
    config/           one JSON file per `lemondx configure` section, e.g. auth.json
    changes.json      when this node last saved or deleted each shared artifact

This is state lemondx writes for itself rather than hand-authored
configuration, which is what ``XDG_DATA_HOME`` is for. Earlier versions kept it
all under ``~/.config/lemondx``; that directory is adopted on first use, so
nothing has to be moved by hand.

Writes are atomic so a crash mid-save cannot leave a truncated file behind.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import tempfile
import threading
import time

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


def stacks_dir():
    return os.path.join(data_dir(), "stacks")


def nodes_dir():
    return os.path.join(data_dir(), "nodes")


def node_groups_dir():
    return os.path.join(data_dir(), "node-groups")


def auth_dir():
    return os.path.join(data_dir(), "auth")


def config_dir():
    return os.path.join(data_dir(), "config")


def changes_path():
    return os.path.join(data_dir(), "changes.json")


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


# An app check's script travels as an exec argument every round, so it is kept
# to what one argument comfortably carries.
APP_CHECK_SCRIPT_LIMIT = 16 * 1024
APP_CHECK_TIMEOUT = (1, 300, 10)          # lowest, highest, default seconds
APP_CHECK_INTERVAL = (10, 86400, 60)


def _clean_app_check(stored):
    """A template's app check, or None for a template without one."""
    if not isinstance(stored, dict):
        return None
    script = stored.get("script")
    if not isinstance(script, str) or not script.strip() \
            or len(script.encode("utf-8")) > APP_CHECK_SCRIPT_LIMIT:
        return None
    def seconds(value, bounds):
        low, high, default = bounds
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return int(min(high, max(low, value)))
    return {"script": script,
            "interval_seconds": seconds(stored.get("interval_seconds"), APP_CHECK_INTERVAL),
            "timeout_seconds": seconds(stored.get("timeout_seconds"), APP_CHECK_TIMEOUT)}


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
        # Whether instances also get a NIC on this node's fabric bridge, so
        # they can reach instances of the same stack on other nodes.
        "fabric": stored.get("fabric") is True,
        "profiles": _strings(stored.get("profiles")),
        "ephemeral": stored.get("ephemeral") is True,
        # Absent means start: an instance that never boots cannot be bootstrapped.
        "start": stored.get("start") is not False,
        # Absent means on, and only a VM can have it off.
        "secureboot": not (kind == "virtual-machine" and stored.get("secureboot") is False),
        "bootstrap": _clean_selection(bootstrap if isinstance(bootstrap, dict) else {}),
        "app_check": _clean_app_check(stored.get("app_check")),
    }


# -- stacks ----------------------------------------------------------------
#
# A stack is a list of stages run one after another, each holding steps that
# run side by side: launch a template, sleep, or wait for everything launched
# so far to be healthy. The file is as untrusted as a template's, so this only
# shapes it -- a step it cannot make sense of is dropped, numbers are clamped --
# and the service holds a save to the stricter rules (see stacks.py).

STACK_STEP_TYPES = ("launch", "sleep", "wait_healthy")
STACK_SLEEP = (1, 86400, 30)              # lowest, highest, default seconds
STACK_WAIT = (10, 86400, 900)
STACK_LAUNCH_COUNT = (1, 20, 1)


def _bounded(value, bounds):
    low, high, default = bounds
    if isinstance(value, bool):
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, value))


def _clean_stack_step(stored):
    if not isinstance(stored, dict) or stored.get("type") not in STACK_STEP_TYPES:
        return None
    kind = stored["type"]
    step = {"id": _text(stored.get("id"), 32), "type": kind}
    if kind == "launch":
        params = stored.get("params")
        step.update({
            "template": _text(stored.get("template"), 200),
            "count": _bounded(stored.get("count"), STACK_LAUNCH_COUNT),
            "prefix": _text(stored.get("prefix"), 50),
            # Absent means wait: the next stage usually needs what this one built.
            "wait_bootstrap": stored.get("wait_bootstrap") is not False,
            "params": {str(k): str(v) for k, v in
                       (params.items() if isinstance(params, dict) else ())},
            "nodes": _strings(stored.get("nodes")),
            "groups": _strings(stored.get("groups")),
        })
    elif kind == "sleep":
        step["seconds"] = _bounded(stored.get("seconds"), STACK_SLEEP)
    else:
        step["timeout_seconds"] = _bounded(stored.get("timeout_seconds"), STACK_WAIT)
    return step


def _clean_stack(stored, name):
    stages = []
    for stage in stored.get("stages") if isinstance(stored.get("stages"), list) else []:
        raw = stage.get("steps") if isinstance(stage, dict) else None
        steps = [s for s in (_clean_stack_step(r) for r in
                             (raw if isinstance(raw, list) else [])) if s]
        if steps:
            stages.append({"steps": steps})
    return {
        "name": name,
        "description": _text(stored.get("description"), 200),
        "stages": stages,
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


# -- federated nodes and node groups ---------------------------------------
#
# A node record says how to reach another lemondx and how to recognise it: its
# URL and the SHA-256 of the TLS certificate it must present. The API token
# used to call it is *not* here -- it is a credential, so it lives in auth/
# alongside the other secrets. That split is what lets a nodes/ file be copied
# between machines or kept in a repo the way profiles and templates are.
#
# A group is a named list of node names. Membership is kept here rather than on
# the node so there is one place to read it from, and so a group can be made
# before its members are enrolled.

_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


def _clean_node(stored, name):
    """Normalise one node record. Like every record here, it is untrusted input."""
    fingerprint = _text(stored.get("fingerprint"), 95).lower().replace(":", "")
    return {
        "name": name,
        "url": _text(stored.get("url"), 300).rstrip("/"),
        # Empty means "verify against the system CA store" instead of a pin.
        "fingerprint": fingerprint if _FINGERPRINT.match(fingerprint) else "",
        "description": _text(stored.get("description"), 200),
        "added": _epoch(stored.get("added")),
        "fabric": clean_fabric(stored.get("fabric")),
    }


def clean_fabric(stored):
    """A node's fabric claim: the subnet its containers use, and how to reach it.

    This rides on the node record rather than being an artifact of its own
    because a claim is not something reconciliation could ever settle: two
    nodes holding overlapping subnets is a two-sided difference, which
    `_conflicts()` reports and refuses to resolve. Allocation is coordinated
    instead, and this is only how the answer travels.

    An unparseable half is dropped rather than failing the record -- a node
    with no usable claim is simply not on the fabric yet.
    """
    if not isinstance(stored, dict):
        return {"subnet": "", "via": ""}
    try:
        subnet = str(ipaddress.ip_network(_text(stored.get("subnet"), 43), strict=False))
    except ValueError:
        subnet = ""
    try:
        via = str(ipaddress.ip_address(_text(stored.get("via"), 45)))
    except ValueError:
        via = ""
    return {"subnet": subnet, "via": via}


def _clean_node_group(stored, name):
    return {
        "name": name,
        "description": _text(stored.get("description"), 200),
        "members": _strings(stored.get("members")),
    }


def _epoch(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


# -- the change ledger -----------------------------------------------------
#
# When this node last saved or deleted each artifact the cluster shares. It is
# what lets a node that has been switched off work out, on its return, which
# way round a difference goes: a template a peer holds and this node does not
# is either one this node missed while it was away, or one it deleted while
# *the peer* was away, and only a record of the deletion tells the two apart.
# Without it a returning node would resurrect everything it had ever deleted.
#
# Deliberately *not* inside the record files. Those are copyable, hand-editable
# and explicitly untrusted, so a timestamp in one proves nothing; this ledger is
# written only by this node, about its own actions, and is never synced. That is
# also why it is one file rather than a directory: nothing is meant to copy it.
#
# A deletion is kept for TOMBSTONE_TTL and then forgotten. After that a peer
# that was off for longer brings the artifact back -- by then nobody can tell
# the difference between a copy that outlived a deletion and one created since,
# and the alternative is keeping every name ever used for good.

CHANGES_VERSION = 1
TOMBSTONE_TTL = 30 * 86400


def _read_changes():
    try:
        with open(changes_path(), encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return {}
    kinds = stored.get("kinds") if isinstance(stored, dict) else None
    if not isinstance(kinds, dict):
        return {}
    clean = {}
    for kind, entries in kinds.items():
        if not isinstance(kind, str) or not isinstance(entries, dict):
            continue
        for name, entry in entries.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            clean.setdefault(kind, {})[name] = {
                "at": _epoch(entry.get("at")), "deleted": entry.get("deleted") is True}
    return clean


def load_changes(now=None):
    """``{kind: {name: {"at": epoch, "deleted": bool}}}``, expired deletions dropped."""
    cutoff = (now if now is not None else time.time()) - TOMBSTONE_TTL
    return {kind: {name: entry for name, entry in entries.items()
                   if not entry["deleted"] or entry["at"] >= cutoff}
            for kind, entries in _read_changes().items()}


def note_change(kind, name, deleted=False):
    """Record that this node has just saved or deleted one shared artifact."""
    name = (name or "").strip()
    if not kind or not name:
        return None
    entry = {"at": int(time.time()), "deleted": bool(deleted)}
    with _lock:
        kinds = load_changes()
        kinds.setdefault(kind, {})[name] = entry
        _write_json(changes_path(), {"version": CHANGES_VERSION, "kinds": kinds})
    return entry


def drop_tombstones():
    """Forget every recorded deletion, keeping what this node still holds.

    For a node leaving a cluster. Its own artifacts are still its own, but the
    deletions are answers to a question only the cluster it just left was
    asking -- carried into the next one they would push removals for names that
    cluster never agreed to delete.
    """
    with _lock:
        remaining = {kind: {name: entry for name, entry in entries.items()
                            if not entry["deleted"]}
                     for kind, entries in load_changes().items()}
        _write_json(changes_path(), {"version": CHANGES_VERSION, "kinds": remaining})
    return remaining


_profiles = _Records(profiles_dir, _clean_profile)
_templates = _Records(templates_dir, _clean_template)
_stacks = _Records(stacks_dir, _clean_stack)
_nodes = _Records(nodes_dir, _clean_node)
_node_groups = _Records(node_groups_dir, _clean_node_group)

# Node records are the one _Records directory that is not a shared artifact --
# membership converges through sync_members(), not through reconciliation --
# so they are bound straight through without touching the ledger.
load_nodes = _nodes.load
save_node = _nodes.save
delete_node = _nodes.delete

load_node_groups = _node_groups.load
load_profiles = _profiles.load
load_templates = _templates.load
load_stacks = _stacks.load


def _tracked(records, kind):
    """Bind one record directory's save and delete, noting each in the ledger."""
    def save(name, record, note=True):
        saved = records.save(name, record)
        if note:
            note_change(kind, saved.get("name") or name)
        return saved

    def delete(name, note=True):
        removed = records.delete(name)
        # Only a deletion that removed something is a tombstone: a name that
        # was not here never was, and claiming otherwise would have this node
        # pushing the removal of a record it has simply never seen.
        if removed and note:
            note_change(kind, name, deleted=True)
        return removed
    return save, delete


save_profile, delete_profile = _tracked(_profiles, "profiles")
save_template, delete_template = _tracked(_templates, "templates")
save_stack, delete_stack = _tracked(_stacks, "stacks")
save_node_group, delete_node_group = _tracked(_node_groups, "groups")


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

    for _, name, record in _profiles.scan():
        if module_id not in record["modules"]:
            continue
        remaining = [m for m in record["modules"] if m != module_id]
        if remaining:
            save_profile(name, dict(record, modules=remaining))
        else:
            delete_profile(name)

    for _, name, record in _templates.scan():
        bootstrap = record["bootstrap"]
        if module_id in bootstrap["modules"]:
            save_template(name, dict(record, bootstrap=dict(
                bootstrap, modules=[m for m in bootstrap["modules"] if m != module_id])))


# -- users and API tokens --------------------------------------------------
#
# Only hashes are stored, but a hash of a weak password is still worth
# something to an attacker, so the directory is 0700 and every file 0600 (the
# temp file _write_json replaces it with is created 0600). The records are
# opaque here: auth.py validates them on read, since these files are as
# hand-editable as everything else under the data directory.

# "cluster" holds the credential this lemondx calls its peers with, and
# "invites" the hashed one-time codes it will accept for enrolment. Both are
# secrets, and both have to be on disk rather than in one process: `lemondx
# cluster invite` runs in the CLI and the code is redeemed against `serve`.
AUTH_KINDS = ("users", "tokens", "cluster", "invites")


def _auth_path(kind):
    if kind not in AUTH_KINDS:
        raise ValueError("unknown auth record kind: %r" % (kind,))
    return os.path.join(auth_dir(), "%s.json" % kind)


def auth_mtime(kind):
    """A fingerprint of a record file's current version, or None when it is missing.

    The server compares this per request so a token revoked from the CLI stops
    working at once, without a restart and without re-reading the file. The
    inode is part of it because every save replaces the file, and two saves
    inside one timestamp tick would otherwise look the same.
    """
    try:
        info = os.stat(_auth_path(kind))
    except OSError:
        return None
    return (info.st_ino, info.st_mtime_ns, info.st_size)


def load_auth(kind):
    """``{key: record}`` for users or tokens; empty when missing or corrupt."""
    try:
        with open(_auth_path(kind), encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return {}
    records = stored.get(kind) if isinstance(stored, dict) else None
    return {k: v for k, v in records.items() if isinstance(v, dict)} \
        if isinstance(records, dict) else {}


def update_auth(kind, mutate):
    """Read-modify-write one record file under the lock; returns mutate's result."""
    # Resolved before taking the lock: the first data_dir() call may migrate,
    # and migration saves through save(), which takes the same lock.
    directory = auth_dir()
    with _lock:
        records = load_auth(kind)
        result = mutate(records)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        _write_json(_auth_path(kind), {"version": 1, kind: records})
    return result


# -- what `serve` is doing right now ---------------------------------------
#
# Written by the running server and read by the CLI in another process, which
# otherwise has no way to know which port to advertise to a peer -- and a join
# code naming the wrong port is a failure the operator only discovers later,
# from the other node. Best-effort in both directions: a stale file is no worse
# than no file, since anything that matters is verified by connecting.


def runtime_path():
    return os.path.join(data_dir(), "runtime.json")


def write_runtime(payload):
    try:
        _write_json(runtime_path(), dict(payload, pid=os.getpid()))
    except OSError:
        pass                      # a read-only data dir must not stop `serve`


def read_runtime():
    try:
        with open(runtime_path(), encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def clear_runtime():
    try:
        os.unlink(runtime_path())
    except OSError:
        pass


# -- configuration sections ------------------------------------------------
#
# Written by `lemondx configure <section>` and read at startup. Unlike the
# rest of the data directory, a file here that does not parse is an error
# rather than an empty default: silently falling back could mean, for auth,
# a server that starts with authentication off. The contents are validated
# by whichever module owns the section, not here.

_SECTION = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def config_path(section):
    if not _SECTION.match(section or ""):
        raise ValueError("not a configuration section name: %r" % (section,))
    return os.path.join(config_dir(), "%s.json" % section)


def load_config(section):
    """The saved dict for a section, or None when it was never configured.

    Raises ValueError when the file exists but is unreadable or not a JSON
    object, so callers can refuse to start instead of guessing.
    """
    path = config_path(section)
    try:
        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError("%s: %s" % (path, exc))
    if not isinstance(stored, dict):
        raise ValueError("%s: expected a JSON object" % path)
    return stored


def save_config(section, payload):
    """Write a section atomically; the directory is 0700 as it may name secrets' paths."""
    path = config_path(section)          # resolved before the lock; see update_auth
    directory = os.path.dirname(path)
    with _lock:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        _write_json(path, payload)
    return path


def delete_config(section):
    try:
        os.unlink(config_path(section))
        return True
    except FileNotFoundError:
        return False

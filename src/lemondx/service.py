"""Domain layer: turns raw LXD records into the shapes lemondx works with.

Both the HTTP API and the CLI call into :class:`ContainerService`, so the two
front ends can never drift apart in behaviour.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from . import eventlog
from . import health as health_checks
from . import hostnet
from . import metrics as metric_history
from . import store
from .bootstrap import (MAX_OCCURRENCES, BootstrapError, BootstrapRunner, delete_module,
                        discover_modules, effective_params,
                        list_host_ssh_keys, missing_secrets, module_source,
                        normalise_module_id, occurrences, param_key, parse_public_key,
                        public_modules, save_module, secret_param_names,
                        split_param, unrepeatable, check_shell_syntax)
from .lxd import (CGROUP_PAYLOAD_PREFIX, INCUS, NO_SECUREBOOT_CONFIG, LXDClient, LXDError,
                  window_resize_message)
from .simplestreams import CatalogError, fetch_catalog

# `pin:<id or nickname>` names a pinned build wherever an image is named.
PIN_REMOTE = "pin"
# What makes a pin the build it is; a record changing any of them is refused.
PIN_BUILD = ("image", "serial", "arch", "fingerprint", "vm_fingerprint")

VALID_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]{0,61}$")
# Launched instances are named <prefix>-<n>; 50 leaves room for the number.
VALID_PREFIX = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]{0,49}$")
# Set on every instance a template launches, so front ends can group them.
TEMPLATE_CONFIG_KEY = "user.lemondx.template"
# Set on everything a stack launches: the instances themselves are the record
# of what a stack is running, as the template tag is for a template, so it
# survives a restart and needs no reconciling with a file. See stacks.py.
STACK_CONFIG_KEY = "user.lemondx.stack"
# Which version of the template and stack an instance was made from, so one
# made before an edit can be called stale. A digest rather than a time: saves
# arrive from peers and from reconciliation, often unchanged, and a timestamp
# would call every instance stale after any of them.
TEMPLATE_REVISION_KEY = "user.lemondx.template-revision"
STACK_REVISION_KEY = "user.lemondx.stack-revision"
# Who made an instance: "lemondx" on everything lemondx creates, "imported"
# once a person adopts one made some other way (`lxc launch`, a script). An
# instance with neither this nor a template or stack tag is foreign, and the
# inventory flags it. The instance carries it, like the tags, because lemondx
# keeps no list of its own -- the daemon's listing is the inventory.
ORIGIN_KEY = "user.lemondx.origin"
ORIGIN_LEMONDX = "lemondx"
ORIGIN_IMPORTED = "imported"
# Every key lemondx sets on an instance starts with this. A clone inherits its
# source's, so the ones the new instance should not carry are taken off again.
LEMONDX_CONFIG_PREFIX = "user.lemondx."
# Image aliases lemondx makes: what `local:<alias>` must then parse as.
VALID_ALIAS = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_MULTIPART = re.compile(r"^multipart/form-data; boundary=[A-Za-z0-9'()+_,./:=?-]{1,70}$")
# What a template can change without its instances being any different: its
# label, how new instances are named, and the app check, which is read from
# the template every round rather than baked into the instance.
_NOT_INFRA = ("name", "description", "name_prefix", "app_check")
# A shell the caller asks for by path. Deliberately narrow: it is handed to the
# daemon as argv[0], and an absolute path with no metacharacters cannot become
# anything else on the way.
VALID_SHELL = re.compile(r"^/[A-Za-z0-9._/-]{1,127}$")
ZFS_KSTAT_ROOT = "/proc/spl/kstat/zfs"

# Handy starting points for the "new container" form. Anything the remotes
# publish still works -- the UI keeps a free-text field alongside these.
# The two daemons publish different remotes, so the catalogue differs: Incus
# has no `ubuntu:` server and serves everything from linuxcontainers.org.
_COMMON_IMAGES = [
    ("debian/13", "Debian 13 (trixie)", "debian"),
    ("debian/12", "Debian 12 (bookworm)", "debian"),
    ("alpine/3.21", "Alpine 3.21", "alpine"),
    ("archlinux", "Arch Linux", "arch"),
    ("fedora/41", "Fedora 41", "fedora"),
    ("rockylinux/9", "Rocky Linux 9", "rocky"),
    ("almalinux/9", "AlmaLinux 9", "alma"),
]

IMAGE_CATALOG_LXD = [
    {"alias": "ubuntu:24.04", "label": "Ubuntu 24.04 LTS", "family": "ubuntu"},
    {"alias": "ubuntu:22.04", "label": "Ubuntu 22.04 LTS", "family": "ubuntu"},
    {"alias": "ubuntu:26.04", "label": "Ubuntu 26.04 LTS", "family": "ubuntu"},
] + [{"alias": "images:%s" % a, "label": l, "family": f} for a, l, f in _COMMON_IMAGES]

IMAGE_CATALOG_INCUS = [
    {"alias": "images:ubuntu/24.04", "label": "Ubuntu 24.04 LTS", "family": "ubuntu"},
    {"alias": "images:ubuntu/22.04", "label": "Ubuntu 22.04 LTS", "family": "ubuntu"},
] + [{"alias": "images:%s" % a, "label": l, "family": f} for a, l, f in _COMMON_IMAGES]

# Drivers that can actually enforce a root disk size. `dir` only manages it
# when the backing filesystem has project quotas enabled, which we cannot
# detect through the API -- the daemon just logs "skipping set quota" and
# carries on -- so it is reported as unable to enforce.
# LXD reports kernel architecture names; simplestreams uses Debian ones.
ARCH_ALIASES = {
    "x86_64": "amd64", "i686": "i386", "aarch64": "arm64",
    "armv7l": "armhf", "ppc64le": "ppc64el", "s390x": "s390x",
    "riscv64": "riscv64",
}

# Remotes worth showing in the image browser by default. The daily and minimal
# Ubuntu streams are still reachable by name, they just add noise here.
BROWSABLE_REMOTES = {
    "lxd": ("ubuntu", "images"),
    "incus": ("images",),
}

LOCAL_STORAGE_DRIVERS = frozenset({"dir", "btrfs", "lvm", "zfs"})

# Keep generic config input narrow so this surface cannot become an accidental
# path to remote backends or credentials.
_COMMON_POOL_CONFIG = frozenset({
    "rsync.bwlimit", "rsync.compression", "volume.size",
    "volume.block.filesystem", "volume.block.mount_options",
})
LOCAL_POOL_CONFIG = {
    "dir": _COMMON_POOL_CONFIG,
    "btrfs": _COMMON_POOL_CONFIG | {"btrfs.mount_options", "btrfs.rsync_path"},
    "lvm": _COMMON_POOL_CONFIG | {
        "lvm.thinpool_name", "lvm.vg_name", "lvm.use_thinpool",
        "lvm.stripes", "lvm.stripes.size",
    },
    "zfs": _COMMON_POOL_CONFIG | {
        "zfs.pool_name", "zfs.clone_copy", "zfs.remove_snapshots",
        "zfs.use_refquota", "zfs.reserve_space",
    },
}
LOCAL_VOLUME_CONFIG = frozenset({
    "block.filesystem", "block.mount_options", "security.shifted",
    "security.unmapped", "snapshots.expiry", "snapshots.pattern",
    "snapshots.schedule",
})

# The device key and interface name a fabric NIC takes when it is free -- the
# second NIC by construction. A profile may already use it, so `free_nic()`
# moves on to eth2 and beyond rather than replacing that NIC.
FABRIC_NIC = "eth1"


def free_nic(devices):
    """The first eth<N> from eth1 that no device uses as its key or interface name.

    Both, because an instance's devices map is keyed by device name while the
    guest sees the NIC's `name`, and either clashing replaces a NIC.
    """
    taken = set(devices) | {d.get("name") for d in devices.values()
                            if isinstance(d, dict) and d.get("type") == "nic"}
    index = 1
    while "eth%d" % index in taken:
        index += 1
    return "eth%d" % index

VALID_NETWORK_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]{0,14}$")
_DNS_DOMAIN = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,62}\.)*[a-zA-Z0-9-]{1,63}$")

# What a local bridge needs, and no more -- the same narrowing as the pool
# config above. `raw.dnsmasq`, tunnels and uplinks stay with the daemon's CLI.
BRIDGE_CONFIG = frozenset({
    "ipv4.address", "ipv4.nat", "ipv4.dhcp", "ipv4.dhcp.ranges",
    "ipv4.dhcp.expiry", "ipv4.routing", "ipv4.firewall",
    "ipv6.address", "ipv6.nat", "ipv6.dhcp", "ipv6.dhcp.stateful",
    "ipv6.dhcp.ranges", "ipv6.dhcp.expiry", "ipv6.routing", "ipv6.firewall",
    "dns.domain", "dns.mode", "bridge.mtu",
})
_BRIDGE_BOOLEANS = frozenset({
    "ipv4.nat", "ipv4.dhcp", "ipv4.routing", "ipv4.firewall",
    "ipv6.nat", "ipv6.dhcp", "ipv6.dhcp.stateful", "ipv6.routing", "ipv6.firewall",
})
# What `lemondx init` has always created: NATed IPv4 on a free subnet, no IPv6.
BRIDGE_DEFAULTS = {"ipv4.address": "auto", "ipv4.nat": "true", "ipv6.address": "none"}

QUOTA_CAPABLE_DRIVERS = frozenset({
    "btrfs", "zfs", "lvm", "ceph", "cephfs", "pure", "powerflex", "alletra",
})

# What a VM gets when its config says nothing. A container without a limit
# shares the whole host, but a VM has to be handed a fixed machine, so both
# daemons fill in these values -- and they count against the host just the
# same as explicit ones.
VM_DEFAULTS = {"cpu": 1, "memory": "1GiB", "disk": "10GiB"}

# Statuses in which an instance is holding its CPU and memory right now. A
# frozen instance keeps its memory even though it is not scheduled.
ACTIVE_STATUSES = frozenset({"Running", "Frozen"})

# The daemon's own size grammar, which unlike normalize_size() reads a bare
# number as bytes: these values come from the daemon, not from a person.
_BYTE_UNITS = {
    "": 1, "b": 1,
    "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
    "pb": 1000 ** 5, "eb": 1000 ** 6,
    "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
    "pib": 1024 ** 5, "eib": 1024 ** 6,
}
_BYTE_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")

STATE_ACTIONS = {
    "start": "start",
    "stop": "stop",
    "restart": "restart",
    "freeze": "freeze",
    "pause": "freeze",
    "unfreeze": "unfreeze",
    "resume": "unfreeze",
}


# Size suffixes LXD/Incus accept verbatim: decimal (kB) and binary (KiB).
_SIZE_UNITS = {
    "k": "kB", "kb": "kB", "kib": "KiB",
    "m": "MiB", "mb": "MB", "mib": "MiB",
    "g": "GiB", "gb": "GB", "gib": "GiB",
    "t": "TiB", "tb": "TB", "tib": "TiB",
    "p": "PiB", "pb": "PB", "pib": "PiB",
}
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")


def normalize_size(value, field="size"):
    """Turn a human size into one LXD understands.

    LXD reads a bare number as *bytes*, which is never what someone means in a
    memory or disk field -- "4" would be four bytes and get rejected as below
    the 1MiB minimum. Treat a bare number as GiB and expand shorthand units, so
    4, 4G, 4g, 4GiB and "4 gib" all mean the same thing.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return ""

    match = _SIZE.match(text)
    if not match:
        raise ServiceError(
            "Invalid %s '%s'. Use a number with a unit, e.g. 4GiB, 512MiB or 20GB."
            % (field, value)
        )

    amount, unit = match.group(1), match.group(2).lower()
    if not unit:
        unit = "gib"                       # a bare number means gigabytes here
    if unit not in _SIZE_UNITS:
        raise ServiceError(
            "Unknown unit '%s' in %s '%s'. Use kB/MB/GB/TB or KiB/MiB/GiB/TiB."
            % (match.group(2), field, value)
        )
    # LXD wants integers; 1.5GiB is fine as a value but not as "1.5GiB" bytes.
    if amount.endswith(".0"):
        amount = amount[:-2]
    return "%s%s" % (amount, _SIZE_UNITS[unit])


class ServiceError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


class TerminalSession:
    """One attached interactive session, from the domain's point of view.

    Holds the daemon's two channels -- data, and the control channel that
    carries window resizes -- and knows how the browser asks for things. The
    byte pumping itself is the HTTP layer's job, in server.py.
    """

    def __init__(self, client, operation, data, control, what):
        self.lxd = client
        self.operation = operation
        self.data = data
        self.control = control
        self.what = what              # "shell" or "console", for messages

    def read(self):
        """The next chunk of guest output, or None once the session is over.

        The daemon signals end of stream with a zero-length message rather than
        by closing the channel, so an empty read is the end and not a no-op.
        Miss that and the session never finishes: the channel stays open, the
        exit status is never collected, and the reader blocks for good.
        """
        message = self.data.recv()
        if message is None:
            return None
        return message[1] or None

    def write(self, payload):
        """Send keystrokes to the guest."""
        self.data.send(payload)

    def handle_control(self, payload):
        """Act on a control message from the browser. Junk is ignored."""
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(message, dict):
            return
        resize = message.get("resize")
        if isinstance(resize, dict):
            self.resize(resize.get("cols"), resize.get("rows"))

    def resize(self, cols, rows):
        """Tell the guest's TTY it changed size, if the daemon gave us a way."""
        if self.control is None:
            return
        cols, rows = terminal_size(cols, rows)
        try:
            self.control.send_text(window_resize_message(cols, rows))
        except Exception:             # noqa: BLE001 - a lost resize is not fatal
            pass

    def exit_code(self):
        """What the command exited with, or None if that is not knowable.

        Hangs up first, deliberately: the daemon holds the operation open
        while any of its channels are attached, and only a finished operation
        carries the exit status. Reading it without closing first finds an
        operation still marked running, whatever the guest has already done.

        A console attachment has no exit status at all, and an operation that
        has already been reaped answers nothing useful either.
        """
        if self.what != "shell":
            return None
        self.close()
        record = None
        try:
            record = self.lxd.wait_for_operation(self.operation, timeout=5)
        except LXDError:
            # The daemon reports some non-zero exits as a failed operation, but
            # the record still carries the real code. Same recovery as exec.
            try:
                record = self.lxd.get_operation(self.operation)
            except LXDError:
                return None
        value = ((record or {}).get("metadata") or {}).get("return")
        return value if isinstance(value, int) else None

    def close(self):
        """Hang up both channels. Safe to call more than once."""
        for channel in (self.data, self.control):
            if channel is not None:
                try:
                    channel.close()
                except Exception:     # noqa: BLE001 - closing must never raise
                    pass


def terminal_size(cols, rows):
    """A window size the daemon will accept, whatever the client claimed."""
    def clamp(value, fallback):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return fallback
        return max(1, min(1000, number))
    return clamp(cols, 80), clamp(rows, 24)


class ContainerService:
    def __init__(self, client=None, **kwargs):
        self.lxd = client or LXDClient(**kwargs)
        # Template launches, recreates and destroys in progress or last
        # finished, by template name. See track_run().
        self._runs = {}
        self._runs_lock = threading.Lock()
        # Creates in progress, and recently finished, by instance name. See
        # _track_create().
        self._creates = {}
        self._creates_lock = threading.Lock()
        # Held from _check_subnets_free() until the daemon has the bridge, as
        # the daemon does not reject an explicit block that overlaps another.
        # It only covers this process, not lxc or a second lemondx.
        self._networks_lock = threading.Lock()
        # Health rounds: baselines and streaks in the tracker, the latest
        # records here. Only `serve` fills these continuously; see
        # start_health_monitor().
        self._health_tracker = health_checks.Tracker()
        self._health_lock = threading.Lock()
        self._health_records = {}
        # The same, judged as machines only: what an app check result that
        # lands between rounds is folded into (health_checks.fold_app()).
        self._health_base = {}
        self._health_checked_at = None
        self._health_settings = None
        self._host_cpu = None               # (threads, memory bytes), fetched once
        # Per-container load averages, sampled every few seconds; only `serve`
        # runs one, and a one-off check measures over its window instead.
        self._load_sampler = None
        # The Monitor page's history; only `serve` keeps one (start_metrics()).
        self._metrics = None
        # Set by ClusterService: (fabric) -> this node's bridge for that
        # fabric, or "" when it is not on it. The service does not read fabric
        # settings itself -- a template names a fabric, and each node resolves
        # that to its own bridge, the same way it resolves a pool or a network.
        self.fabric_bridge = None
        # Set by ClusterService too: (name, iface, bridge) -> configure the
        # fabric NIC inside a started instance. The device only gives it a link.
        self.fabric_configure = None
        # Set by ClusterService likewise: routes to every fabric in a new
        # macvlan instance, and telling the fabric a LAN network came or went.
        self.lan_configure = None
        self.lan_changed = None
        # Set by ClusterService: (reason) -> put this node into maintenance and
        # tell the cluster, once launches here keep failing. See
        # _note_launch().
        self.launches_failing = None
        self._launch_streak_lock = threading.Lock()
        self._tripping = False
        # Template app checks on their own intervals; `serve` only, like the
        # sampler. Without it a round runs each check itself.
        self._app_checker = None

    # -- readiness ---------------------------------------------------------

    def daemon_identity(self):
        """{"flavor", "product", "version"} of the daemon this node runs."""
        environment = (self.lxd.server_info() or {}).get("environment") or {}
        return {"flavor": self.lxd.flavor, "product": self.lxd.product_name,
                "version": str(environment.get("server_version") or "")}

    def status(self):
        """Server info plus whether LXD is actually usable for containers."""
        info = self.lxd.server_info()
        environment = info.get("environment") or {}
        pools = self.lxd.list_storage_pools()
        networks = [n for n in self.lxd.list_networks() if n.get("managed")]
        profile = self.lxd.get_profile("default")
        devices = profile.get("devices") or {}

        has_root = any(d.get("type") == "disk" and d.get("path") == "/"
                       for d in devices.values())
        has_nic = any(d.get("type") == "nic" for d in devices.values())

        issues = []
        if not pools:
            issues.append("No storage pool exists; containers have nowhere to live.")
        if not networks:
            issues.append("No managed bridge exists; containers would have no network.")
        if not has_root:
            issues.append("The default profile has no root disk device.")
        if not has_nic:
            issues.append("The default profile has no network device.")

        pool_summaries = [
            {"name": p.get("name"), "driver": p.get("driver"),
             "used_by": len(p.get("used_by") or []),
             "supports_quota": p.get("driver") in QUOTA_CAPABLE_DRIVERS}
            for p in pools
        ]

        # Which pool a new container lands on -- the one the default profile's
        # root disk points at, else the first pool. Front ends use this to warn
        # about disk sizes that will not be enforced.
        root_pool_name = next(
            (d["pool"] for d in devices.values()
             if d.get("type") == "disk" and d.get("path") == "/" and d.get("pool")),
            pool_summaries[0]["name"] if pool_summaries else None,
        )
        root_pool = next(
            (p for p in pool_summaries if p["name"] == root_pool_name), None)

        _, nic = self._profile_nic(["default"])

        return {
            "connected": True,
            "ready": not issues,
            "issues": issues,
            "root_pool": root_pool,
            "default_network": _nic_network(nic),
            "flavor": self.lxd.flavor,
            "product": self.lxd.product_name,
            "client_binary": self.lxd.client_binary,
            "socket": self.lxd.socket_path,
            "server_version": environment.get("server_version"),
            "kernel": environment.get("kernel_version"),
            "driver": environment.get("driver"),
            "architectures": environment.get("architectures") or [],
            "project": self.lxd.project,
            "storage_drivers": sorted(
                d["Name"] for d in (environment.get("storage_supported_drivers") or [])
            ),
            "storage_pools": pool_summaries,
            "networks": [
                {"name": n.get("name"), "type": n.get("type"),
                 "ipv4": (n.get("config") or {}).get("ipv4.address")}
                for n in networks
            ],
        }

    def initialize(self, storage_driver="dir", pool_name="default",
                   pool_size=None, bridge="lxdbr0", ipv6=False):
        """Equivalent of ``lxd init --auto``: pool + bridge + default profile."""
        steps = []
        notes = []

        pools = self.lxd.list_storage_pools()
        if pools:
            pool_name = pools[0]["name"]
            steps.append("Reusing existing storage pool '%s'." % pool_name)
        else:
            available = self.status()["storage_drivers"]
            if storage_driver not in available:
                raise ServiceError(
                    "Storage driver '%s' is not supported here. Available: %s"
                    % (storage_driver, ", ".join(available))
                )
            config = {}
            if pool_size and storage_driver != "dir":
                config["size"] = pool_size
            self.lxd.create_storage_pool(pool_name, storage_driver, config)
            steps.append("Created %s storage pool '%s'." % (storage_driver, pool_name))
            if storage_driver not in QUOTA_CAPABLE_DRIVERS:
                quota_capable = sorted(set(available) & QUOTA_CAPABLE_DRIVERS)
                notes.append(
                    "The '%s' driver cannot enforce per-container disk sizes "
                    "unless the backing filesystem has project quotas enabled. "
                    "%s" % (
                        storage_driver,
                        ("For enforced quotas, use one of: %s."
                         % ", ".join(quota_capable)) if quota_capable
                        else "No quota-capable driver is available on this host.",
                    )
                )

        managed = [n for n in self.lxd.list_networks() if n.get("managed")]
        if managed:
            bridge = managed[0]["name"]
            steps.append("Reusing existing bridge '%s'." % bridge)
        else:
            # The daemon picks an auto block around what exists now, which
            # could be the one a concurrent create_network() just found free.
            with self._networks_lock:
                self.lxd.create_network(bridge, {
                    "ipv4.address": "auto",
                    "ipv4.nat": "true",
                    "ipv6.address": "auto" if ipv6 else "none",
                    "ipv6.nat": "true" if ipv6 else "false",
                })
            steps.append("Created bridge '%s' with NAT." % bridge)

        profile = self.lxd.get_profile("default")
        devices = dict(profile.get("devices") or {})
        changed = False
        if not any(d.get("type") == "disk" and d.get("path") == "/"
                   for d in devices.values()):
            devices["root"] = {"type": "disk", "path": "/", "pool": pool_name}
            changed = True
        if not any(d.get("type") == "nic" for d in devices.values()):
            devices["eth0"] = {"type": "nic", "network": bridge, "name": "eth0"}
            changed = True
        if changed:
            profile["devices"] = devices
            self.lxd.update_profile("default", profile)
            steps.append("Attached root disk and eth0 to the default profile.")
        else:
            steps.append("Default profile already had a root disk and a NIC.")

        return {"steps": steps, "notes": notes, "status": self.status()}

    # -- listing -----------------------------------------------------------

    def list_containers(self):
        return self._mark_stale([_summarize(i) for i in self.lxd.list_instances()])

    def import_instance(self, name, template=None):
        """Adopt an instance made outside lemondx; optionally into a template.

        Always marks it, so the inventory stops flagging it. With a template it
        is also tagged as one of that template's, so the template's runs take
        it in -- exec, its app check, and Recreate, which deletes it and makes
        it again from the template, losing what is on it. It gets no revision,
        since it was not made from any version of the template, so it is never
        called stale. A stack is never offered: a stack's teardown destroys
        whatever carries its tag.
        """
        instance = self.lxd.get_instance(name)
        config = instance.get("config") or {}
        if _origin(config) is not None:
            raise ServiceError("'%s' is already lemondx's; there is nothing to import." % name,
                               409)
        changes = {ORIGIN_KEY: ORIGIN_IMPORTED}
        if template:
            record = self._template(template)
            kind = instance.get("type") or "container"
            if record["type"] != kind:
                raise ServiceError(
                    "Template '%s' makes %ss and '%s' is a %s: Recreate would replace "
                    "it with the other kind." % (record["name"], record["type"].replace(
                        "virtual-machine", "virtual machine"), name,
                        kind.replace("virtual-machine", "virtual machine")), 409)
            changes[TEMPLATE_CONFIG_KEY] = record["name"]
        self.lxd.update_instance(name, {"config": changes})
        eventlog.event("change", "instance.import", instance=name,
                       template=changes.get(TEMPLATE_CONFIG_KEY))
        return self.get_container(name)

    def get_container(self, name):
        instance = self.lxd.get_instance(name)
        summary = self._mark_stale([_summarize(instance)])[0]
        summary["config"] = {
            k: v for k, v in (instance.get("config") or {}).items()
            if not k.startswith("volatile.")
        }
        summary["devices"] = instance.get("devices") or {}
        summary["expanded_devices"] = instance.get("expanded_devices") or {}
        summary["snapshots"] = [
            {
                "name": (s.get("name") or "").split("/")[-1],
                "created_at": s.get("created_at"),
                "stateful": s.get("stateful", False),
                "size": (s.get("config") or {}).get("volatile.rootfs.size"),
            }
            for s in self.lxd.list_snapshots(name)
        ]
        state = instance.get("state") or {}
        summary["network_detail"] = _network_detail(state)
        return summary

    # -- lifecycle ---------------------------------------------------------

    def create_container(self, name, image, instance_type="container",
                         profiles=None, cpu=None, memory=None, disk=None, pool=None,
                         network=None, description=None, ephemeral=False, start=True,
                         config=None, wait=True, bootstrap=None,
                         remember_params=True, background=False, secureboot=True,
                         fabric=None, source_snapshot=None):
        """Create an instance, start it and run its bootstrap modules.

        With ``background`` everything that can be checked up front still is
        -- a bad name, a missing pool, the same name already being created --
        and raised to the caller, but the create itself runs on a thread and
        the call returns its progress record at once. Follow it with
        ``creates()``. The web UI always does this: a request held open for the
        minutes an image pull and bootstrap take is one the browser, a proxy or
        a closed tab can drop, and the page has no business being the thing
        that keeps work alive.
        """
        if not VALID_NAME.match(name or ""):
            raise ServiceError(
                "Invalid name '%s'. Use letters, digits and dashes, starting "
                "with a letter (max 62 chars)." % name
            )
        if not image and not source_snapshot:
            raise ServiceError("An image is required, e.g. 'ubuntu:24.04'.")
        if source_snapshot:
            # The clone is whatever the source was; asking for the other kind
            # would only fail inside the daemon.
            instance_type = self.snapshot_type(source_snapshot)

        instance_config = dict(config or {})
        # Every path that makes an instance comes through here -- create,
        # template and stack launches, recreate, clones (whose copy of the
        # source's mark is replaced by this one) -- so this is the one place.
        instance_config[ORIGIN_KEY] = ORIGIN_LEMONDX
        if cpu:
            instance_config["limits.cpu"] = str(cpu).strip()
        if memory:
            instance_config["limits.memory"] = normalize_size(memory, "memory limit")
        if description:
            instance_config.setdefault("user.description", description)
        if not secureboot:
            if instance_type != "virtual-machine":
                raise ServiceError("Secure boot only applies to virtual machines.")
            instance_config.update(NO_SECUREBOOT_CONFIG[self.lxd.flavor])

        payload = {
            "name": name,
            "type": instance_type,
            "source": ({"type": "copy", "source": source_snapshot, "instance_only": True}
                       if source_snapshot else self.image_source(image, instance_type)),
            "profiles": profiles or ["default"],
            "config": instance_config,
            "ephemeral": bool(ephemeral),
        }
        if description:
            payload["description"] = description
        if pool or disk:
            # Overriding the root disk at instance level replaces the profile's
            # device outright, and LXD requires an explicit pool on it.
            pool_name = str(pool).strip() if pool else self._root_pool(payload["profiles"])
            if not pool_name:
                raise ServiceError(
                    "Cannot set a disk size: no storage pool is configured. "
                    "Run setup first."
                )
            if pool and pool_name not in {
                    item.get("name") for item in self.lxd.list_storage_pools()}:
                raise ServiceError("No storage pool called '%s'." % pool_name, 404)
            root = {"type": "disk", "path": "/", "pool": pool_name}
            if disk:
                root["size"] = normalize_size(disk, "disk size")
            payload["devices"] = {"root": root}
        if network:
            key, nic = self._instance_nic(payload["profiles"], str(network).strip())
            payload.setdefault("devices", {})[key] = nic
        fabric_key = None
        if fabric:
            # A second NIC beside the profile's, never instead of it: the
            # fabric carries traffic to other nodes and nothing else.
            devices = dict(self._profile_devices(payload["profiles"]))
            devices.update(payload.get("devices") or {})
            fabric_key, nic = self.fabric_nic(str(fabric).strip(), free_nic(devices))
            payload.setdefault("devices", {})[fabric_key] = nic

        if bootstrap and bootstrap.get("modules") and not (start and wait):
            raise ServiceError(
                "Bootstrap modules need the container to start, so 'start' "
                "cannot be disabled."
            )

        if not wait:
            self.lxd.create_instance(payload, wait=False)
            return {"name": name, "status": "Pending"}

        modules = (bootstrap or {}).get("modules") or []
        record = self._begin_create(name, source_snapshot or image, instance_type,
                                    instance_config.get(TEMPLATE_CONFIG_KEY), len(modules))

        def work():
            return self._run_create(record, payload, start, bootstrap, remember_params,
                                    fabric_key, cloned=bool(source_snapshot))

        if background:
            self._in_background("create-%s" % name, work)
            return self._create_snapshot(record)
        return work()

    def _run_create(self, record, payload, start, bootstrap, remember_params,
                    fabric_key=None, cloned=False):
        name = payload["name"]
        modules = (bootstrap or {}).get("modules") or []
        try:
            try:
                self.lxd.create_instance(payload, wait=True)
                if cloned:
                    self._detach_clone(name, payload)
                if start:
                    self._create_stage(record, stage="starting")
                    self.lxd.set_state(name, "start")
                    # Before bootstrap, not after: a module may well want to reach
                    # another node, and an interface with no address is not there
                    # yet as far as anything inside the instance is concerned.
                    if fabric_key and self.fabric_configure:
                        self.fabric_configure(name, fabric_key,
                                              payload["devices"][fabric_key].get("network"))
                    elif self.lan_configure:
                        # A no-op unless its NIC is on a macvlan network.
                        self.lan_configure(name)
            except Exception as exc:                        # noqa: BLE001
                self._note_launch(exc)
                raise
            # Launched: what bootstrap makes of it is the modules' doing, not
            # this node's, so it ends a streak of failures whatever happens next.
            self._note_launch(None)

            container = self.get_container(name)
            if modules:
                self._create_stage(record, stage="bootstrapping")
                # Report bootstrap failures alongside the container rather than
                # raising: the container exists either way and the user needs
                # to see which module failed and why.
                result = self.bootstrap(
                    name,
                    modules=modules,
                    params=bootstrap.get("params"),
                    ssh_keys=bootstrap.get("ssh_keys"),
                    remember=remember_params,
                )
                container["bootstrap"] = result
                failed = next((m for m in result["modules"] if m["exit_code"] != 0), None)
                if failed:
                    tail = (failed["stderr"] or failed["stdout"] or "").strip().splitlines()
                    self._create_stage(record, ok=False, failed_module=failed["name"],
                                       error_detail=" ".join(tail[-2:])[:300] or None)
            self._create_stage(record, ok=record["ok"] is not False)
            return container
        except (ServiceError, LXDError, BootstrapError) as exc:
            self._create_stage(record, ok=False, error=str(exc))
            raise
        except BaseException:
            self._create_stage(record, ok=False, error="Unexpected error while creating.")
            raise
        finally:
            self._create_stage(record, stage="done", finished_at=time.time())

    # -- launches that keep failing ----------------------------------------
    #
    # A node whose launches keep failing is one to stop sending work to, and
    # maintenance is exactly that: launches spread over a group skip it, and
    # one naming it is refused with the reason. Only failures that say
    # something about this node count, though. A template naming an image that
    # does not exist fails the same on every node, and counting it would put
    # the whole cluster into maintenance with one launch. So a refusal of the
    # request -- lemondx's own (ServiceError), the daemon's 4xx, an image or
    # source not found -- neither counts nor ends a streak, and neither does a
    # failing bootstrap module, which comes after the launch succeeded.

    LAUNCH_FAILURE_LIMIT = 5
    _NOT_THE_NODE = ("could not be found", "not found")

    def _note_launch(self, error):
        """Count one launch here: None for one that worked, else what failed it."""
        if error is not None and not self._node_fault(error):
            return
        with self._launch_streak_lock:
            if error is None:
                if store.load_launch_failures()["count"]:
                    store.clear_launch_failures()
                return
            message = getattr(error, "message", None) or str(error) or type(error).__name__
            count = store.add_launch_failure(message)
            # A launch of several fails on several threads at once; one trips it.
            if count < self.LAUNCH_FAILURE_LIMIT or self._tripping or store.load_maintenance():
                return
            self._tripping = True
        reason = ("%d launches failed in a row here; the last: %s"
                  % (count, message))[:200]
        eventlog.event("change", "node.maintenance.auto", level=logging.WARNING,
                       failures=count, error=message[:200])
        try:
            if self.launches_failing:
                self.launches_failing(reason)
            else:
                store.save_maintenance({"since": int(time.time()), "reason": reason,
                                        "by": "lemondx"})
        except Exception as exc:                            # noqa: BLE001
            # The launch has already failed and says why; this must not
            # replace its error with one about maintenance.
            eventlog.message("could not put this node into maintenance: %s" % exc,
                             level=logging.WARNING)
        finally:
            self._tripping = False

    def _node_fault(self, error):
        if isinstance(error, ServiceError):
            return False
        if isinstance(error, LXDError):
            if 400 <= (error.code or 500) < 500:
                return False
            text = (error.message or "").lower()
            return not any(phrase in text for phrase in self._NOT_THE_NODE)
        return True

    def resolve_source(self, template):
        """``(template, notes)`` as this node can make it: from its snapshot, or an image of it.

        On the snapshot's own node, the snapshot. Anywhere else, an image
        published from it, which is how a snapshot runs on other nodes --
        published once and copied to them -- without a second template. The
        image says what it was made from itself (`lemondx.source`, carried in
        the image file, so copies say it too), and the template is placed to
        launch from it by fingerprint. Refused, with what to do, when there is
        neither: checked before anything is created -- or, for a recreate,
        deleted -- since finding out per instance would be after each one had gone.
        """
        source = template.get("snapshot")
        if not source:
            return template, []
        name = "%s/%s" % (source["instance"], source["name"])
        try:
            self.snapshot_type(name)
            return template, []
        except ServiceError as exc:
            if exc.code != 404:
                raise
        image = self.snapshot_image(source)
        if image is None:
            raise ServiceError(
                "No snapshot '%s' on this node, and no image made from it. Make "
                "an image of it on %s and copy it here (Images tab, or `lemondx "
                "snapshot-publish`)." % (name, source["node"] or "its node"), 404)
        alias = next(iter(image["aliases"]), image["fingerprint"][:12])
        note = ("Launched from image %s, made from snapshot %s on %s."
                % (alias, name, source["node"] or "its node"))
        _log(note)
        return dict(template, snapshot=None, image="local:%s" % image["fingerprint"]), [note]

    def snapshot_image(self, source):
        """The newest image here published from ``source`` ({node, instance, name}), or None.

        An image that names the node it was published on must name the
        snapshot's; one from before that was recorded is taken on the
        instance and snapshot names alone.
        """
        wanted = "%s/%s" % (source["instance"], source["name"])
        found = [i for i in self.image_inventory()["images"]
                 if i["source"] == wanted and i.get("source_node") in ("", None, source["node"])]
        return max(found, key=lambda i: i["created_at"] or "") if found else None

    def snapshot_type(self, source):
        """The instance type of ``instance/snapshot``, or 404 if it is not here."""
        instance, _, snapshot = source.partition("/")
        try:
            record = self.lxd.get_instance_record(instance)
            if not any(s.get("name") == snapshot for s in self.lxd.list_snapshots(instance)):
                raise LXDError("not found", 404)
        except LXDError as exc:
            if exc.code == 404:
                raise ServiceError("No snapshot '%s' on this node." % source, 404)
            raise
        return record.get("type") or "container"

    def _detach_clone(self, name, payload):
        """Make a fresh clone its own instance rather than a second copy of its source.

        The daemon copies the source's config, and ours is merged over it, so
        any lemondx key we did not set -- the source's stack, say, which a
        stack's teardown would then take this clone for -- is removed here.
        And a container that has booted has a machine-id, which the clone
        would share: systemd-networkd derives its DHCP client id from it, so
        every clone would ask for, and be given, the source's address. An empty
        file makes systemd generate a new one at first boot; an image without
        systemd has no file, and is left without one. A VM's files are out of
        reach until its agent runs, so it keeps its source's.
        """
        record = self.lxd.get_instance_record(name)
        config = dict(record.get("config") or {})
        wanted = payload.get("config") or {}
        inherited = [k for k in config if k.startswith(LEMONDX_CONFIG_PREFIX) and k not in wanted]
        if inherited:
            for key in inherited:
                del config[key]
            self.lxd.replace_instance(name, {
                key: record.get(key) for key in (
                    "architecture", "devices", "ephemeral", "profiles", "description")
            } | {"config": config})
        if (record.get("type") or "container") == "container":
            try:
                if self.lxd.read_file(name, "/etc/machine-id").strip():
                    self.lxd.push_file(name, "/etc/machine-id", b"", mode="0444")
            except LXDError as exc:
                if exc.code == 404:
                    return
                _log("%s: could not reset its machine-id, so it shares its "
                     "source's: %s" % (name, exc))

    # Finished creates stay listed this long, so a page reloaded mid-create
    # can still find out how it ended.
    CREATE_RETENTION = 600

    def _begin_create(self, name, image, instance_type, template, module_count):
        """Record a create before it starts, so every client can follow it.

        Only one create per name runs at a time; a second is refused here,
        before anything reaches the daemon.
        """
        now = time.time()
        with self._creates_lock:
            for key, old in list(self._creates.items()):
                if old["finished_at"] and now - old["finished_at"] > self.CREATE_RETENTION:
                    del self._creates[key]
            current = self._creates.get(name)
            if current and current["finished_at"] is None:
                raise ServiceError("'%s' is already being created." % name, 409)
            record = {
                "name": name, "image": image, "type": instance_type,
                "template": template, "modules": module_count,
                "stage": "creating", "started_at": now, "finished_at": None,
                "ok": None, "error": None, "failed_module": None, "error_detail": None,
            }
            self._creates[name] = record
        return record

    def _create_stage(self, record, **changes):
        with self._creates_lock:
            record.update(changes)

    def _create_snapshot(self, record):
        with self._creates_lock:
            return dict(record)

    def creates(self):
        """Creates in progress or finished in the last few minutes, oldest first.

        Held by this process: a create run by the CLI in another process is not
        listed.
        """
        with self._creates_lock:
            return sorted((dict(r) for r in self._creates.values()),
                          key=lambda r: r["started_at"])

    @staticmethod
    def _in_background(label, work):
        """Run ``work`` on its own thread; its own bookkeeping records failures."""
        def target():
            try:
                work()
            except Exception:                         # noqa: BLE001
                pass
        # A daemon thread, so it cannot keep a stopped server alive by itself;
        # serve() waits on pending_work() instead, where it can say what for.
        threading.Thread(target=eventlog.carry(target), name="lemondx-%s" % label,
                         daemon=True).start()

    def pending_work(self):
        """Human descriptions of every create and template run still going."""
        with self._runs_lock:
            runs = ["%s of %d from template '%s'" % (r["action"], r["count"], r["template"])
                    for r in self._runs.values() if r["finished_at"] is None]
        with self._creates_lock:
            creates = ["create of '%s' (%s)" % (r["name"], r["stage"])
                       for r in self._creates.values()
                       if r["finished_at"] is None and not r["template"]]
        return runs + creates

    # -- health ------------------------------------------------------------

    def check_health(self, names=None, window=5, settings=None):
        """Run one round of health checks now and return a record per instance.

        CPU needs two samples of the daemon's usage counter. The server's
        rounds are a minute apart and use the previous one; a caller with no
        previous sample (the CLI) waits ``window`` seconds between two.
        Instances not running or frozen get no record.
        """
        settings = settings or self._health_settings or health_checks.load_settings()[0]
        samples = self._health_samples(names)
        wanted = [s["name"] for s in samples if s["status"] == "Running"]
        cgroups = {s["name"]: s["cgroup"] for s in samples
                   if s["status"] == "Running" and s["cgroup"]}
        sampler = self._load_sampler
        if sampler is not None and names is None:
            sampler.set_targets(cgroups)

        measured = None
        if window and wanted and not self._health_tracker.has_baseline(wanted):
            self._health_tracker.baseline(samples)
            if sampler is None and cgroups:
                # The CPU window doubles as the load window: counted while we wait.
                measured = health_checks.measure_load(cgroups, window)
            else:
                time.sleep(window)
            samples = self._health_samples(names)

        for sample in samples:
            if sample["cgroup"]:
                sample["cgroup_load"] = measured.get(sample["name"]) if measured is not None \
                    else sampler.load(sample["name"]) if sampler is not None else None

        running = [s for s in samples if s["status"] == "Running"]
        timeout = settings["probe_timeout_seconds"]
        app_checks, configured = self._app_checks(running)
        checker = self._app_checker
        if checker is not None and names is None:
            checker.set_targets(app_checks)
        probes, apps = {}, {}

        def check(sample):
            name = sample["name"]
            probe = self._probe(name, timeout)
            if checker is not None:
                # `serve` runs checks on their own intervals; a round reads.
                return probe, checker.result(name) if name in app_checks else None
            # A one-off check runs the script now. An instance that cannot
            # even have a file read will not run one either; the failed probe
            # already says so.
            app = self._run_app_check(name, app_checks[name]) \
                if name in app_checks and probe["ok"] else None
            if app:
                app.update(checked_at=time.time(), interval=None,
                           streak=1 if app["status"] == health_checks.APP_CRITICAL else 0)
            return probe, app

        def app_status(sample):
            if sample["status"] != "Running":
                return None
            return self._app_status(sample["name"], sample["template"],
                                    apps.get(sample["name"]), sample["name"] in configured)

        if running:
            # Each probe opens its own socket to the daemon, as template
            # launches do, so a slow instance only holds up its own slot.
            with ThreadPoolExecutor(max_workers=min(8, len(running))) as pool:
                for sample, (probe, app) in zip(running, pool.map(eventlog.carry(check), running)):
                    probes[sample["name"]] = probe
                    apps[sample["name"]] = app

        host = health_checks.host_tasks()
        bases = [self._health_tracker.evaluate(s, probes.get(s["name"]), settings, host)
                 for s in samples]
        if names is None:
            self._health_tracker.forget_except({s["name"] for s in samples})
        with self._health_lock:
            records = [health_checks.fold_app(base, app_status(sample), settings,
                                              self._health_records.get(base["name"]))
                       for base, sample in zip(bases, samples)]
            previous = dict(self._health_records)
            if names is None:
                self._health_base = {b["name"]: b for b in bases}
                self._health_records = {r["name"]: r for r in records}
            else:
                self._health_base.update((b["name"], b) for b in bases)
                self._health_records.update((r["name"], r) for r in records)
            self._health_checked_at = time.time()
        _note_health_changes(previous, records)
        return sorted(records, key=lambda r: r["name"])

    def health(self):
        """The latest health records, from memory: never touches the daemon."""
        settings = self._health_settings
        with self._health_lock:
            records = sorted(self._health_records.values(), key=lambda r: r["name"])
            checked_at = self._health_checked_at
        return {
            "enabled": settings is not None and settings["enabled"],
            "interval": settings["interval_seconds"] if settings else None,
            "thresholds": health_checks.thresholds(settings) if settings else None,
            "checked_at": checked_at,
            "instances": records,
        }

    def start_health_monitor(self, settings):
        """Check health every ``interval_seconds`` on a daemon thread, for `serve`."""
        self._health_settings = settings
        if not settings["enabled"]:
            return
        self._load_sampler = health_checks.LoadSampler()
        self._load_sampler.start()
        self._app_checker = health_checks.AppChecker(self._run_app_check,
                                                     self._app_result_landed)
        self._app_checker.start()

        def loop():
            # The first round establishes CPU baselines and probes at once, so
            # liveness shows straight away and CPU from the second round on.
            while True:
                started = time.time()
                try:
                    self.check_health(window=0, settings=settings)
                except Exception as exc:                    # noqa: BLE001
                    eventlog.message("health check failed: %s" % exc, level=logging.WARNING)
                elapsed = time.time() - started
                # Rounds never overlap: a slow one just starts the next later.
                time.sleep(max(1.0, settings["interval_seconds"] - elapsed))

        def run():
            with eventlog.system("health"):
                loop()
        threading.Thread(target=run, name="lemondx-health", daemon=True).start()

    # -- performance history -------------------------------------------------

    def start_metrics(self):
        """Keep a rolling history of host and instance usage, for `serve`."""
        self._metrics = metric_history.Recorder()
        self._metrics.start(self._metric_readings)

    def _metric_readings(self):
        readings = []
        for instance in self.lxd.list_instances():
            state = instance.get("state") or {}
            config = instance.get("config") or {}
            network = _network_totals(state)
            readings.append({
                "name": instance.get("name"),
                "status": instance.get("status") or state.get("status") or "Unknown",
                "type": instance.get("type") or "container",
                "template": config.get(TEMPLATE_CONFIG_KEY) or None,
                "stack": config.get(STACK_CONFIG_KEY) or None,
                "processes": state.get("processes") or 0,
                "cpu_ns": (state.get("cpu") or {}).get("usage") or 0,
                "memory": (state.get("memory") or {}).get("usage") or 0,
                "rx": network["rx"], "tx": network["tx"],
            })
        return readings

    def metrics(self, since=None, window=None):
        """This node's usage history after ``since``, from memory.

        Only a running `serve` samples, since history is the point: a one-off
        CLI process has none to give, and `lemondx top` is its live view.
        """
        if self._metrics is None:
            raise ServiceError("Usage history is kept by `lemondx serve`; this process "
                               "has none. `lemondx top` is the live view.", 503)
        try:
            return self._metrics.read(since=since, window=window)
        except ValueError as exc:
            raise ServiceError(str(exc))

    # Runs the template's script inside the instance with its own watchdog, so
    # a hung check is stopped where it runs: the daemon giving up on waiting
    # stops nothing, and a hung script left running would be joined by another
    # every interval. `timeout` is not used even where it exists -- it kills
    # only its direct child (busybox's always; coreutils' unless the child
    # makes its own group), and a check's hung `curl` is a grandchild. Without
    # a tty there is no job control to give the script a process group, so at
    # the limit the watchdog walks /proc for the script's tree, stopping each
    # process as it is found so none is reparented out of reach, then kills
    # them all, deepest first. The script itself goes last and the watchdog
    # ignores TERM from then on: the script dying wakes the wrapper, whose
    # `kill "$dog"` would otherwise cut the list short. It leaves a marker so a timeout is told apart from a script
    # that was killed some other way (the OOM killer's 137 looks the same).
    #
    # The script goes to a file and its interpreter line is read and run by
    # hand, the way the kernel would, so a check can be bash or python without
    # the file needing to be executable -- /tmp is often mounted noexec.
    # Minimal images may have no mktemp. The watchdog's fds go to /dev/null so
    # that one left sleeping never holds the run's output open.
    _APP_CHECK_WRAPPER = (
        'f=$(mktemp 2>/dev/null) || f=/tmp/lemondx-app-check.$$\n'
        'printf "%s" "$1" > "$f" || exit 3\n'
        'limit=$2; interp=/bin/sh\n'
        'case "$1" in "#!"*) IFS= read -r interp < "$f"; interp=${interp#??} ;; esac\n'
        'set -- $interp "$f"\n'
        '"$@" & pid=$!\n'
        '(\n'
        '  sleep "$limit"\n'
        '  trap "" TERM\n'
        '  : > "$f.timeout"\n'
        '  kill -STOP "$pid"; all=$pid; new=$pid\n'
        '  while [ -n "$new" ]; do\n'
        '    next=\n'
        '    for s in /proc/[0-9]*/status; do\n'
        '      while IFS=: read -r k v; do\n'
        '        [ "$k" = PPid ] || continue\n'
        '        for p in $new; do\n'
        '          if [ ${v:-x} = "$p" ]; then\n'
        '            c=${s#/proc/}; c=${c%/status}; kill -STOP "$c"; next="$next $c"\n'
        '          fi\n'
        '        done\n'
        '        break\n'
        '      done < "$s"\n'
        '    done\n'
        '    all="$next $all"; new=$next\n'
        '  done\n'
        '  kill -9 $all\n'
        ') >/dev/null 2>&1 </dev/null & dog=$!\n'
        'wait "$pid"; rc=$?\n'
        'kill "$dog" 2>/dev/null\n'
        'if [ -e "$f.timeout" ]; then rc=124; fi\n'
        'rm -f "$f" "$f.timeout"; exit $rc\n'
    )

    @staticmethod
    def _app_status(name, template, result, configured):
        """What a record says about the app: a result, pending, or ok for none."""
        if result is None:
            return dict(health_checks.app_placeholder(configured), template=template)
        summary = {k: v for k, v in result.items() if k not in health_checks.APP_DETAIL_KEYS}
        return dict(summary, configured=True, template=template)

    def app_check_output(self, name):
        """The latest run of an instance's app check, with everything it printed.

        Kept apart from health(): records go out on every poll, and this is
        what someone asks for when they want to know why a check said what it
        said. ``result`` is None until the first run finishes.
        """
        checker = self._app_checker
        if checker is None:
            raise ServiceError("App checks run under `lemondx serve` with health checks on; "
                               "this process keeps no results.", 409)
        target = checker.target(name)
        if target is None:
            raise ServiceError(self._why_no_app_check(name), 404)
        result = checker.result(name)
        return {
            "name": name,
            "template": target["template"],
            "script": target["script"],
            "interval_seconds": target["interval_seconds"],
            "timeout_seconds": target["timeout_seconds"],
            "result": result,
            # Rounds in a row that did not see it running: the check is kept
            # through a missed round, and this says the result may be old.
            "missed_rounds": checker.missed(name),
        }

    def _why_no_app_check(self, name):
        """Which of the reasons an instance has no check running actually applies.

        Asked of the daemon now, so the answer is the real one rather than a
        list of possibilities -- the last of which, an instance that is
        running but was not seen by the last rounds, is the daemon failing to
        report its state and is otherwise indistinguishable from the rest.
        """
        try:
            instance = self.lxd.get_instance(name)
        except LXDError as exc:
            if exc.code == 404:
                return "There is no instance called '%s' on this node." % name
            return ("Cannot tell why '%s' has no app check: the daemon did not answer "
                    "(%s)." % (name, exc))
        config = instance.get("expanded_config") or instance.get("config") or {}
        status = instance.get("status") or "Unknown"
        template = config.get(TEMPLATE_CONFIG_KEY)
        if not template:
            return "'%s' was not launched from a template, so it has no app check." % name
        if not (store.load_templates().get(template) or {}).get("app_check"):
            return "Template '%s' has no app check." % template
        with self._creates_lock:
            record = self._creates.get(name)
            if record and record["finished_at"] is None:
                return ("'%s' is still being created; its app check starts once it is "
                        "done." % name)
        if status != "Running":
            return "'%s' is %s; app checks run only while it is running." % (
                name, status.lower())
        return ("'%s' is running, but the daemon did not report it as running in the "
                "last health rounds, so its app check was paused. It resumes at the next "
                "round that sees it (every %ds)."
                % (name, (self._health_settings or {}).get("interval_seconds", 60)))

    def _app_result_landed(self, name):
        """Fold a check that finished between rounds into its instance's record."""
        checker = self._app_checker
        result = checker.result(name)
        settings = self._health_settings
        with self._health_lock:
            base, previous = self._health_base.get(name), self._health_records.get(name)
            if base is None or previous is None or previous["app"] is None:
                return                  # not running at the last round; the next says
            # No result: a check just swapped in (pending) or removed (ok).
            record = self._health_records[name] = health_checks.fold_app(
                base, self._app_status(name, previous["app"]["template"], result,
                                       checker.has_target(name)),
                settings, previous)
        _note_health_changes({name: previous}, [record])

    def _app_checks(self, samples):
        """``(checks, configured)``: the app checks to run now, by instance name,
        and the names of every instance whose template has one.

        Read from the template on every round rather than copied onto the
        instance, so editing a template's check reaches the instances already
        launched from it. An instance still being created or bootstrapped is
        left out: the application it checks is not there yet.
        """
        tagged = {s["name"]: s["template"] for s in samples if s.get("template")}
        if not tagged:
            return {}, set()
        templates = store.load_templates()
        with self._creates_lock:
            busy = {n for n, r in self._creates.items() if r["finished_at"] is None}
        checks, configured = {}, set()
        for name, template in tagged.items():
            app_check = (templates.get(template) or {}).get("app_check")
            if app_check:
                configured.add(name)
                if name not in busy:
                    # Tagged with its template so a save can find what to swap.
                    checks[name] = dict(app_check, template=template)
        return checks, configured

    def _app_check_changed(self, template, app_check):
        """Swap a saved (or deleted) check into the running scheduler at once.

        Saving -- here, or a push from another member, which lands in the same
        save -- would otherwise reach the scheduler only at the next round.
        """
        checker = self._app_checker
        if checker is None:
            return
        for name in checker.update_template(template, app_check):
            self._app_result_landed(name)

    def _run_app_check(self, name, app_check):
        """One run of an app check, or None if the instance is being (re)built."""
        with self._creates_lock:
            record = self._creates.get(name)
            if record and record["finished_at"] is None:
                return None
        limit = app_check["timeout_seconds"]
        started = time.time()
        try:
            outcome = self.lxd.exec_command(
                name, ["/bin/sh", "-c", self._APP_CHECK_WRAPPER, "lemondx-app-check",
                       app_check["script"], str(limit)],
                # The daemon-side wait outlasts the in-instance timeout, so
                # the script's own exit is what normally ends it.
                timeout=limit + 5)
        except LXDError as exc:
            return health_checks.app_error(exc.message, int((time.time() - started) * 1000))
        return health_checks.app_result(
            outcome.get("exit_code"), outcome.get("stdout"), outcome.get("stderr"),
            int((time.time() - started) * 1000), limit)

    def _probe(self, name, timeout):
        started = time.time()
        try:
            text = self.lxd.read_file(name, "/proc/loadavg", timeout=timeout)
        except LXDError as exc:
            return {"ok": False, "ms": int((time.time() - started) * 1000),
                    "error": exc.message, "text": None}
        return {"ok": True, "ms": int((time.time() - started) * 1000), "error": None,
                "text": text.decode("ascii", "replace")}

    def _health_samples(self, names=None):
        if self._host_cpu is None:
            host = self.lxd.resources() or {}
            self._host_cpu = ((host.get("cpu") or {}).get("total") or 1,
                              (host.get("memory") or {}).get("total") or 0)
        threads, host_memory = self._host_cpu
        wanted = set(names) if names else None
        samples = []
        for instance in self.lxd.list_instances():
            name = instance.get("name")
            status = instance.get("status") or ""
            if status not in ACTIVE_STATUSES or (wanted is not None and name not in wanted):
                continue
            config = instance.get("expanded_config") or instance.get("config") or {}
            state = instance.get("state") or {}
            is_vm = instance.get("type") == "virtual-machine"
            cores = cpu_count(config.get("limits.cpu")) or (VM_DEFAULTS["cpu"] if is_vm else threads)
            memory_limit = parse_byte_size(
                config.get("limits.memory") or (VM_DEFAULTS["memory"] if is_vm else ""),
                host_memory)
            usage = (state.get("cpu") or {}).get("usage")
            samples.append({
                "name": name,
                # A VM's threads on the host are its emulator's, not its
                # guest's tasks, so only a container's cgroup says anything.
                "cgroup": None if is_vm else health_checks.cgroup_dir(
                    CGROUP_PAYLOAD_PREFIX[self.lxd.flavor], name, self.lxd.project),
                "cgroup_load": None,
                "type": instance.get("type") or "container",
                "status": status,
                # A VM without its agent reports -1 or nothing: no counter.
                "cpu_usage": usage if isinstance(usage, int) and usage > 0 else None,
                "pid": state.get("pid") or 0,
                "processes": state.get("processes") or 0,
                "memory_usage": (state.get("memory") or {}).get("usage") or 0,
                "memory_limit": memory_limit,
                "cores": cores,
                "started_at": health_checks.iso_epoch(instance.get("last_used_at")),
                "template": config.get(TEMPLATE_CONFIG_KEY) or None,
                "at": time.time(),
            })
        if wanted:
            missing = wanted - {s["name"] for s in samples}
            if missing:
                raise ServiceError("Not running: %s" % ", ".join(sorted(missing)), 404)
        return samples

    def root_pool_info(self, profiles=None, pool=None):
        """The pool a new container lands on, and whether it enforces quotas."""
        name = pool or self._root_pool(profiles or ["default"])
        if not name:
            return None
        for pool in self.lxd.list_storage_pools():
            if pool.get("name") == name:
                driver = pool.get("driver")
                return {
                    "name": name,
                    "driver": driver,
                    "supports_quota": driver in QUOTA_CAPABLE_DRIVERS,
                }
        return {"name": name, "driver": None, "supports_quota": False}

    def _root_pool(self, profiles):
        """Which storage pool the root disk should live on."""
        for profile_name in profiles or ["default"]:
            try:
                profile = self.lxd.get_profile(profile_name)
            except LXDError:
                continue
            for device in (profile.get("devices") or {}).values():
                if (device.get("type") == "disk" and device.get("path") == "/"
                        and device.get("pool")):
                    return device["pool"]
        pools = self.lxd.list_storage_pools()
        return pools[0]["name"] if pools else None

    def change_state(self, name, action, force=False, timeout=60):
        if action not in STATE_ACTIONS:
            raise ServiceError(
                "Unknown action '%s'. Try: %s" % (action, ", ".join(sorted(STATE_ACTIONS)))
            )
        self.lxd.set_state(name, STATE_ACTIONS[action], force=force, timeout=timeout)
        return self.get_container(name)

    # A state change is nearly all waiting on the daemon, and the client opens
    # a socket per request, so a ticked list goes out together rather than one
    # round trip after another. Same reasoning as EXEC_WORKERS.
    BULK_STATE_WORKERS = 8

    def change_state_many(self, names, action, force=False, timeout=60):
        """Apply one state action to several containers at once.

        Every instance goes through ``change_state``, so the bulk path
        cannot drift from the single one. A container the daemon refuses --
        already stopped, or wedged -- is that container's result rather than an
        error for the whole request: the caller acted on a list it confirmed,
        and giving up halfway would leave the rest in a state nobody chose.
        Only a request that cannot be carried out at all raises.
        """
        if action not in STATE_ACTIONS:
            raise ServiceError(
                "Unknown action '%s'. Try: %s" % (action, ", ".join(sorted(STATE_ACTIONS)))
            )
        if (not isinstance(names, list) or not names
                or not all(isinstance(n, str) and n.strip() for n in names)):
            raise ServiceError("List the containers to act on in 'names'.")
        # Deduplicated, but kept in the order given, so the CLI's output reads
        # in the order the names were typed.
        wanted = list(dict.fromkeys(name.strip() for name in names))

        def run_one(name):
            try:
                container = self.change_state(name, action, force=force, timeout=timeout)
            except (ServiceError, LXDError) as exc:
                return {"name": name, "ok": False, "error": str(exc), "container": None}
            return {"name": name, "ok": True, "error": None, "container": container}

        instances = self._each(wanted, run_one, workers=self.BULK_STATE_WORKERS)
        return {"action": action, "ok": all(i["ok"] for i in instances),
                "instances": instances}

    def delete_container(self, name, force=False):
        state = self.lxd.get_state(name)
        if state.get("status") != "Stopped":
            if not force:
                raise ServiceError(
                    "Container '%s' is %s. Stop it first, or pass force."
                    % (name, (state.get("status") or "running").lower()),
                    409,
                )
            self.lxd.set_state(name, "stop", force=True)
        self.lxd.delete_instance(name)
        return {"deleted": name}

    def rename_container(self, name, new_name):
        if not VALID_NAME.match(new_name or ""):
            raise ServiceError("Invalid name '%s'." % new_name)
        self.lxd.rename_instance(name, new_name)
        return self.get_container(new_name)

    def update_limits(self, name, cpu=None, memory=None, description=None):
        # PATCH merges, so only the keys we send change. An empty string clears
        # a limit; passing None here means "leave this one alone".
        config = {}
        if cpu is not None:
            config["limits.cpu"] = str(cpu).strip()
        if memory is not None:
            config["limits.memory"] = normalize_size(memory, "memory limit")

        # LXD merges the config/devices maps on PATCH but takes scalar fields
        # straight from the request, so anything we leave out is reset to its
        # zero value. Send the current description back unless it is changing.
        current = self.lxd.get_instance_record(name)
        patch = {
            "config": config,
            "description": current.get("description") or ""
            if description is None else description,
        }
        self.lxd.update_instance(name, patch)
        return self.get_container(name)

    # -- snapshots ---------------------------------------------------------

    def create_snapshot(self, name, snapshot_name, stateful=False):
        if not VALID_NAME.match(snapshot_name or ""):
            raise ServiceError("Invalid snapshot name '%s'." % snapshot_name)
        self.lxd.create_snapshot(name, snapshot_name, stateful)
        return {"created": snapshot_name}

    def delete_snapshot(self, name, snapshot_name):
        self.lxd.delete_snapshot(name, snapshot_name)
        return {"deleted": snapshot_name}

    def restore_snapshot(self, name, snapshot_name):
        self.lxd.restore_snapshot(name, snapshot_name)
        return {"restored": snapshot_name}

    # -- exec --------------------------------------------------------------

    def exec_command(self, name, command, timeout=60):
        state = self.lxd.get_state(name)
        if state.get("status") != "Running":
            raise ServiceError("Container '%s' is not running." % name, 409)
        if isinstance(command, str):
            command = ["/bin/sh", "-c", command]
        if not command:
            raise ServiceError("No command given.")
        return self.lxd.exec_command(name, command, timeout=timeout)

    # -- catalogue ---------------------------------------------------------

    # -- interactive terminals ---------------------------------------------

    TERMINAL_KINDS = ("shell", "console")

    # Minimal images often ship no bash, and `exec` on a missing binary kills
    # the session outright, so ask before committing to one.
    _SHELL_PROBE = "if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi"

    def open_terminal(self, name, kind="shell", shell=None, cols=80, rows=24):
        """Attach to a guest and hand back the live session.

        ``shell`` runs a real TTY on a shell inside the container; ``console``
        attaches to the guest's own console device, which is what a VM needs
        and what shows a login prompt on a container.
        """
        if kind not in self.TERMINAL_KINDS:
            raise ServiceError(
                "Unknown terminal '%s'. Try: %s"
                % (kind, ", ".join(self.TERMINAL_KINDS)), 404)
        if not VALID_NAME.match(name or ""):
            raise ServiceError("Invalid container name '%s'." % name, 404)

        status = (self.lxd.get_state(name) or {}).get("status")
        if status != "Running":
            raise ServiceError(
                "Container '%s' is %s. Start it to open a %s."
                % (name, (status or "not running").lower(), kind), 409)

        cols, rows = terminal_size(cols, rows)
        if kind == "console":
            operation, fds = self.lxd.console_session(name, cols, rows)
        else:
            if shell and not VALID_SHELL.match(shell):
                raise ServiceError(
                    "A shell must be an absolute path, such as /bin/zsh.")
            command = [shell] if shell else ["/bin/sh", "-c", self._SHELL_PROBE]
            operation, fds = self.lxd.exec_interactive(
                name, command,
                # Without TERM the guest assumes a dumb terminal and no curses
                # program will draw anything.
                environment={"TERM": "xterm-256color"},
                width=cols, height=rows,
            )

        data = self.lxd.attach(operation, fds["0"])
        control = None
        if fds.get("control"):
            try:
                control = self.lxd.attach(operation, fds["control"])
            except LXDError:
                pass          # resizing will not work, but the session will
        return TerminalSession(self.lxd, operation, data, control, kind)

    def list_images(self):
        local = []
        for image in self.lxd.list_images():
            aliases = [a.get("name") for a in (image.get("aliases") or [])]
            local.append({
                "fingerprint": (image.get("fingerprint") or "")[:12],
                "aliases": aliases,
                "description": (image.get("properties") or {}).get("description"),
                "architecture": image.get("architecture"),
                "size": image.get("size"),
                "cached": image.get("cached", False),
                "used_by": len(image.get("used_by") or []),
            })
        catalog = (IMAGE_CATALOG_INCUS if self.lxd.flavor == INCUS
                   else IMAGE_CATALOG_LXD)
        return {
            "local": local,
            "catalog": catalog,
            "remotes": sorted(self.lxd.remotes),
        }

    @staticmethod
    def image_alias(alias):
        alias = str(alias or "").strip()
        if not VALID_ALIAS.match(alias):
            raise ServiceError(
                "Invalid image alias '%s'. Use letters, digits, dots, dashes and "
                "underscores, starting with a letter or digit (max 64 chars)." % alias)
        return alias

    def publish_snapshot(self, name, snapshot, alias, description="", node=""):
        """Make a snapshot into an image here, under ``alias``; returns the image.

        An alias already in use on this node is refused rather than moved:
        here it is a new name being chosen, and taking it from an image that
        templates may already launch as ``local:<alias>`` would change what
        they make without anyone having asked. (Where the image is *copied*
        to, the alias does move -- see ``adopt_image()``.)
        """
        alias = self.check_publish(name, snapshot, alias)
        description = str(description or "").strip()[:200] or \
            "%s/%s, published by lemondx" % (name, snapshot)
        properties = {"description": description,
                      "lemondx.source": "%s/%s" % (name, snapshot)}
        if node:
            # Which node's snapshot: instance names are only unique per node.
            # Kept in the image file like the rest, so every copy says it too.
            properties["lemondx.source-node"] = node
        fingerprint = self.lxd.publish_snapshot(name, snapshot, properties)
        self.lxd.set_alias(alias, fingerprint, description)
        return self._image_summary(fingerprint, alias)

    def check_publish(self, name, snapshot, alias):
        """What ``publish_snapshot()`` would refuse, refused now; returns the alias."""
        alias = self.image_alias(alias)
        self.snapshot_type("%s/%s" % (name, snapshot))
        taken = self.lxd.get_alias(alias)
        if taken:
            raise ServiceError("This node already has an image called '%s' (%s). "
                               "Pick another name, or delete that image first."
                               % (alias, (taken.get("target") or "")[:12]), 409)
        return alias

    def image_by_alias(self, alias):
        """``(fingerprint, description)`` of this node's image called ``alias``."""
        alias = self.image_alias(alias)
        target = (self.lxd.get_alias(alias) or {}).get("target")
        if not target:
            raise ServiceError("This node has no image called '%s'." % alias, 404)
        image = self.lxd.get_image(target) or {}
        return target, (image.get("properties") or {}).get("description") or ""

    def adopt_image(self, fingerprint, alias, description=""):
        """Point ``alias`` at ``fingerprint`` if this node has that image.

        Returns the image, or None when it is not here and has to be sent.
        The alias moves if it named another image: the node the copy came from
        holds the image under that name, and a cluster where ``local:<alias>``
        launches something different on each node is worse than one that was
        updated.
        """
        fingerprint = str(fingerprint or "").strip().lower()
        alias = self.image_alias(alias)
        if not re.match(r"^[0-9a-f]{64}$", fingerprint):
            raise ServiceError("An image fingerprint is 64 hex digits.")
        if self.lxd.get_image(fingerprint) is None:
            return None
        self.lxd.set_alias(alias, fingerprint, str(description or "")[:200])
        return self._image_summary(fingerprint, alias)

    def receive_image(self, stream, length, fingerprint, alias, description="",
                      content_type=None):
        """Import an image sent from another node, and check it arrived whole.

        The daemon fingerprints what it received; anything other than the
        fingerprint the sender named is a transfer that went wrong, and the
        image is deleted rather than left under a name it does not deserve.
        """
        fingerprint = str(fingerprint or "").strip().lower()
        alias = self.image_alias(alias)
        # A split image arrives as the multipart body its export was; anything
        # else that claims a type is refused rather than handed to the daemon.
        content_type = str(content_type or "").strip()
        if content_type and not _MULTIPART.match(content_type):
            raise ServiceError("Not an image body type: %r." % content_type[:80])
        got = self.lxd.import_image(stream, length,
                                    {"description": str(description or "")[:200]},
                                    content_type=content_type or "application/octet-stream")
        if got != fingerprint:
            try:
                self.lxd.delete_image(got)
            except LXDError:
                pass
            raise ServiceError("The image arrived damaged (fingerprint %s, expected %s) "
                               "and was discarded." % (got[:12], fingerprint[:12]), 502)
        self.lxd.set_alias(alias, fingerprint, str(description or "")[:200])
        return self._image_summary(fingerprint, alias)

    def image_inventory(self):
        """This node's images, and every snapshot here that could become one.

        One listing each: the instance listing already carries every
        instance's snapshots, so no call per instance is needed.
        """
        images = []
        for image in self.lxd.list_images():
            properties = image.get("properties") or {}
            images.append({
                "fingerprint": image.get("fingerprint") or "",
                "aliases": sorted(a.get("name") for a in image.get("aliases") or []
                                  if a.get("name")),
                "description": properties.get("description") or "",
                "size": image.get("size") or 0,
                "architecture": image.get("architecture") or "",
                "type": image.get("type") or "container",
                # Pulled from a remote to launch something, rather than made
                # or copied here -- the daemon expires those by itself.
                "cached": bool(image.get("cached")),
                "created_at": image.get("uploaded_at") or image.get("created_at"),
                "source": properties.get("lemondx.source") or None,
                "source_node": properties.get("lemondx.source-node") or None,
            })
        snapshots = []
        for instance in self.lxd.list_instances():
            for snapshot in instance.get("snapshots") or []:
                snapshots.append({
                    "instance": instance.get("name"),
                    "name": (snapshot.get("name") or "").split("/")[-1],
                    "created_at": snapshot.get("created_at"),
                    "stateful": bool(snapshot.get("stateful")),
                    "type": instance.get("type") or "container",
                })
        return {"images": images, "snapshots": snapshots}

    def delete_image(self, fingerprint):
        """Remove an image from this node, unless a template launches it by name.

        Only this node's copy: another node's is its own, and removed there.
        A template naming ``local:<alias>`` is refused rather than broken,
        since it would fail every launch here afterwards -- the same rule as
        deleting anything else a saved record names.
        """
        fingerprint = str(fingerprint or "").strip().lower()
        image = self.lxd.get_image(fingerprint) if re.match(r"^[0-9a-f]{64}$", fingerprint) \
            else None
        if image is None:
            raise ServiceError("This node has no image %s." % fingerprint[:12], 404)
        aliases = sorted(a.get("name") for a in image.get("aliases") or [] if a.get("name"))
        names = {"local:%s" % a for a in aliases}
        users = sorted(t["name"] for t in store.load_templates().values()
                       if t["image"] in names)
        if users:
            raise ServiceError("Template%s %s launch%s %s, so it cannot be deleted. "
                               "Point %s at another image first." % (
                                   "s" if len(users) > 1 else "",
                                   ", ".join("'%s'" % u for u in users),
                                   "" if len(users) > 1 else "es",
                                   " / ".join(sorted(names)),
                                   "them" if len(users) > 1 else "it"), 409)
        self.lxd.delete_image(fingerprint)
        return {"deleted": fingerprint, "aliases": aliases}

    def prune_images(self, apply=False, only=None):
        """Delete downloaded images nothing needs; ``apply=False`` only says which.

        Downloaded means pulled from a remote -- the daemon's cache, an image
        with a remote to update from, or a pinned build kept after its pin went
        -- never one made here from a snapshot, which is somebody's work rather
        than a copy of the internet. Kept, whatever else: anything an instance
        was made from (its `volatile.base_image`, which both daemons set and
        which `used_by` is not reliable for), the build any pin names, and an
        image a template launches as `local:<alias>` or `local:<fingerprint>`.

        ``only`` limits the run to fingerprints a preview listed, so a prune a
        person confirmed never deletes something they were not shown.
        """
        in_use = {}
        for instance in self.lxd.list_instances():
            base = (instance.get("config") or {}).get("volatile.base_image")
            if base:
                in_use.setdefault(base, []).append(instance.get("name"))
        pinned = {}
        for pin in store.load_pins().values():
            for fingerprint in (pin["fingerprint"], pin["vm_fingerprint"]):
                if fingerprint:
                    pinned[fingerprint] = pin["name"]
        launched = {}
        for template in store.load_templates().values():
            remote, _, alias = (template.get("image") or "").partition(":")
            if remote == "local" and alias:
                launched.setdefault(alias, []).append(template["name"])
        wanted = None if only is None else {str(f).strip().lower() for f in only}

        deleted, kept, failed = [], [], []
        for image in self.lxd.list_images():
            fingerprint = image.get("fingerprint") or ""
            properties = image.get("properties") or {}
            aliases = sorted(a.get("name") for a in image.get("aliases") or [] if a.get("name"))
            row = {"fingerprint": fingerprint, "aliases": aliases,
                   "description": properties.get("description") or "",
                   "size": image.get("size") or 0,
                   "type": image.get("type") or "container"}
            templates = sorted({t for a in aliases + [fingerprint] for t in launched.get(a, [])})
            downloaded = bool(image.get("cached") or (image.get("update_source") or {}).get("server")
                              or any(a.startswith("pin-") for a in aliases))
            if properties.get("lemondx.source") or not downloaded:
                reason = "made here, not downloaded"
            elif fingerprint in in_use:
                reason = "used by %s" % ", ".join(sorted(in_use[fingerprint]))
            elif fingerprint in pinned:
                reason = "pinned: %s" % pinned[fingerprint]
            elif templates:
                reason = "launched by template %s" % ", ".join(templates)
            else:
                reason = ""
            if reason:
                kept.append(dict(row, reason=reason))
                continue
            if wanted is not None and fingerprint not in wanted:
                kept.append(dict(row, reason="not in the preview that was confirmed"))
                continue
            if apply:
                try:
                    self.lxd.delete_image(fingerprint)
                except LXDError as exc:
                    failed.append(dict(row, error=exc.message))
                    continue
            deleted.append(row)
        return {"applied": bool(apply), "deleted": deleted, "kept": kept, "failed": failed,
                "freed": sum(r["size"] for r in deleted)}

    def _image_summary(self, fingerprint, alias):
        image = self.lxd.get_image(fingerprint) or {}
        return {"fingerprint": fingerprint, "alias": alias,
                "size": image.get("size"), "architecture": image.get("architecture"),
                "type": image.get("type") or "container"}

    def default_image(self):
        """A reasonable starting image for whichever daemon is in use."""
        catalog = (IMAGE_CATALOG_INCUS if self.lxd.flavor == INCUS
                   else IMAGE_CATALOG_LXD)
        return catalog[0]["alias"]

    # -- pinned images -----------------------------------------------------
    #
    # A remote alias is whatever the remote built last, so `images:debian/12`
    # launched in March is not the one launched in January: base OS drift. A
    # pin is one build of a remote image, by fingerprint, and a launch naming
    # `pin:<id>` (or `pin:<nickname>`) takes exactly that build, from this
    # node's store where pinning puts it with auto-update off. A pin's build
    # never changes -- a newer one is another pin -- so what a template makes
    # changes only when the template does, which is also what marks its
    # instances stale. Pins are shared definitions, so every member launches
    # the same base; getting the image itself onto each member is a job
    # (`ClusterService.fetch_pin()`), since it is a download per node.

    def pin_image_name(self, image):
        """``remote:alias`` for a remote image as a template or a create names it."""
        image = str(image or "").strip()
        remote, sep, alias = image.partition(":")
        if not sep:
            remote, alias = _default_remote(self.lxd.remotes), image
        name = "%s:%s" % (remote, alias)
        if remote in ("local", PIN_REMOTE):
            raise ServiceError("Only a remote image (images:debian/12, ubuntu:24.04) "
                               "can be pinned; %s is one build already." % image)
        if not store.PIN_IMAGE.match(name):
            raise ServiceError("'%s' is not a remote image name such as images:debian/12."
                               % image)
        return name

    def list_image_pins(self):
        """Every pin, with whether this node holds its build and what launches it."""
        held = {i.get("fingerprint") for i in self.lxd.list_images()}
        templates = store.load_templates()
        pins = sorted(store.load_pins().values(),
                      key=lambda p: (p["image"], -p["pinned_at"], p["name"]))
        return [dict(pin, held=bool(pin["fingerprint"]) and pin["fingerprint"] in held,
                     held_vm=bool(pin["vm_fingerprint"]) and pin["vm_fingerprint"] in held,
                     local_alias=pin_alias(pin["name"]), used_by=pin_users(pin, templates))
                for pin in pins]

    def find_pin(self, ref):
        """The pin ``ref`` names (``pin:`` optional, id or nickname), or None.

        An id wins over a nickname. Two pins claiming one nickname -- a copied
        file, or two nodes naming at once -- is refused rather than guessed
        at, since guessing is exactly the drift a pin exists to stop.
        """
        ref = str(ref or "").strip()
        if ref.startswith(PIN_REMOTE + ":"):
            ref = ref[len(PIN_REMOTE) + 1:]
        pins = store.load_pins()
        if ref in pins:
            return pins[ref]
        claims = [p for p in pins.values() if ref in p["nicknames"]]
        if len(claims) > 1:
            raise ServiceError("Pins %s all answer to '%s'. Take the nickname off all but "
                               "one." % (", ".join(sorted(p["name"] for p in claims)), ref),
                               409)
        return claims[0] if claims else None

    def _pin(self, ref):
        pin = self.find_pin(ref)
        if pin is None:
            ref = str(ref or "").strip()
            raise ServiceError("No pin called '%s'." % (
                ref if ref.startswith(PIN_REMOTE + ":") else "%s:%s" % (PIN_REMOTE, ref)), 404)
        return pin

    def image_versions(self, image, refresh=False):
        """The builds a remote still serves of ``image``, newest first, and its pins."""
        entry = self._catalog_entry(self.pin_image_name(image), refresh=refresh)
        return {"image": "%s:%s" % (entry["remote"], entry["alias"]), "entry": entry,
                "versions": entry["versions"],
                "pins": [p for p in store.load_pins().values()
                         if p["remote"] == entry["remote"] and p["arch"] == entry["arch"]
                         and entry["alias"] in p["aliases"]]}

    def _catalog_entry(self, name, refresh=False):
        remote, _, alias = name.partition(":")
        remotes = self.lxd.remotes
        if remote not in remotes:
            raise ServiceError("Unknown remote '%s'. This daemon knows: %s"
                               % (remote, ", ".join(sorted(remotes))), 404)
        arch = self.host_architecture()
        try:
            catalog = fetch_catalog(remote, remotes[remote], refresh=refresh)
        except CatalogError as exc:
            raise ServiceError(str(exc), 502)
        for entry in catalog:
            if (not arch or entry["arch"] == arch) and alias in entry["aliases"]:
                return entry
        raise ServiceError("%s publishes no image called '%s' for %s."
                           % (remote, alias, arch or "this architecture"), 404)

    def pin_image(self, image, serial=None, nicknames=None, note="", by=""):
        """Pin one build of ``image``: ``serial``, or the newest the remote has now.

        Only records the pin; fetching the build is the job that follows. The
        catalog is read fresh, because "pin what I would get today" has to
        mean today's build and not the one cached a quarter of an hour ago.
        A build pinned already is refused, not re-pinned: its id is what
        templates name, and it already means this build.
        """
        entry = self._catalog_entry(self.pin_image_name(image), refresh=True)
        builds = entry["versions"]
        build = builds[0] if not serial else \
            next((b for b in builds if b["serial"] == str(serial).strip()), None)
        if build is None:
            raise ServiceError("%s:%s has no build %s any more; the remote still serves %s."
                               % (entry["remote"], entry["alias"], serial,
                                  ", ".join(b["serial"] for b in builds)), 404)
        name = pin_id(entry["remote"], entry["alias"], build["serial"])
        existing = store.load_pins().get(name)
        if existing:
            raise ServiceError("Build %s of %s is pinned already, as pin:%s." % (
                build["serial"], existing["image"], name), 409)
        nicknames = self._free_nicknames(name, nicknames)
        return self.save_pin_record(name, {
            "image": "%s:%s" % (entry["remote"], entry["alias"]),
            "aliases": entry["aliases"], "serial": build["serial"], "arch": entry["arch"],
            "server": self.lxd.remotes[entry["remote"]],
            "fingerprint": build["container_fingerprint"] or "",
            "vm_fingerprint": build["vm_fingerprint"] or "",
            "label": entry["label"], "nicknames": nicknames, "note": str(note or "").strip(),
            "pinned_at": int(time.time()), "pinned_by": str(by or "")})

    def _free_nicknames(self, name, nicknames):
        """``nicknames`` checked for a person: well formed, and no other pin's name."""
        if nicknames is None:
            return []
        if isinstance(nicknames, str):
            nicknames = nicknames.replace(",", " ").split()
        if not isinstance(nicknames, list):
            raise ServiceError("'nicknames' must be a list of names.")
        wanted = list(dict.fromkeys(str(n).strip() for n in nicknames if str(n).strip()))
        bad = [n for n in wanted if not store.PIN_ID.match(n)]
        if bad:
            raise ServiceError("Invalid nickname %s. Use lower-case letters, digits and "
                               ". _ -, starting with a letter or digit."
                               % ", ".join("'%s'" % n for n in bad))
        if len(wanted) > 16:
            raise ServiceError("A pin takes at most 16 nicknames.")
        for other in store.load_pins().values():
            if other["name"] == name:
                continue
            taken = [n for n in wanted if n == other["name"] or n in other["nicknames"]]
            if taken:
                raise ServiceError("'%s' already names pin %s." % (taken[0], other["name"]),
                                   409)
        return [n for n in wanted if n != name]

    def edit_pin(self, ref, nicknames=None, note=None):
        """Change what a person may change on a pin: its nicknames and its note."""
        pin = self._pin(ref)
        changes = {}
        if nicknames is not None:
            changes["nicknames"] = self._free_nicknames(pin["name"], nicknames)
        if note is not None:
            changes["note"] = str(note).strip()
        return store.save_pin(pin["name"], dict(pin, **changes))

    def save_pin_record(self, name, record):
        """Save a pin as given: a person's, or one a member pushed.

        A record for a pin held here already may change only what `edit_pin`
        could: a different build under the same id is a different pin, and
        accepting it would change what every template naming it launches.
        """
        name = str(name or "").strip()
        if not store.PIN_ID.match(name):
            raise ServiceError("'%s' is not a pin id." % name)
        record = dict(record if isinstance(record, dict) else {}, name=name)
        existing = store.load_pins().get(name)
        if existing:
            clean = store.clean_pin(record, name)
            if any(clean[k] != existing[k] for k in PIN_BUILD):
                raise ServiceError("Pin %s already holds build %s of %s; a pin's build "
                                   "never changes. Pin the other build beside it."
                                   % (name, existing["serial"], existing["image"]), 409)
            record = dict(existing, **{k: clean[k] for k in store.PIN_EDITABLE})
        saved = store.save_pin(name, record)
        if not saved["image"] or not (saved["fingerprint"] or saved["vm_fingerprint"]):
            if not existing:
                store.delete_pin(name, note=False)
            raise ServiceError("A pin needs the image it is a build of, and the "
                               "fingerprint of that build.")
        return saved

    def unpin_image(self, ref):
        """Forget a pin. The build it fetched is kept, as an ordinary image."""
        pin = self._pin(ref)
        store.delete_pin(pin["name"])
        return {"unpinned": pin["name"], "kept": pin_alias(pin["name"])}

    def fetch_pin(self, ref):
        """Put the pinned build on this node, if it is not here already.

        Under a `pin-...` alias with auto-update off, so it is neither expired
        as a cache entry nor replaced by a newer build. The container image,
        or the VM one for an image that has no container build.
        """
        pin = self._pin(ref)
        self._check_pin_arch(pin)
        vm = not pin["fingerprint"]
        fingerprint = pin["vm_fingerprint"] if vm else pin["fingerprint"]
        alias = pin_alias(pin["name"]) + ("-vm" if vm else "")
        description = "%s %s, pinned" % (pin["image"], pin["serial"])
        image = self.lxd.get_image(fingerprint)
        state = "present"
        if image is None:
            server = pin["server"] or self.lxd.remotes.get(pin["remote"])
            if not server:
                raise ServiceError("This node has no remote called '%s' to fetch %s from."
                                   % (pin["remote"], pin["image"]), 409)
            try:
                self.lxd.pull_image(server, fingerprint)
            except LXDError as exc:
                raise ServiceError(
                    "Could not fetch build %s of %s from %s: %s. A remote keeps only "
                    "its last few builds, and LXD's and Incus's are different servers; "
                    "fetch it from a node that holds it (`lemondx image-fetch` there)."
                    % (pin["serial"], pin["image"], server, exc.message),
                    exc.code if isinstance(exc.code, int) and 400 <= exc.code < 600 else 502)
            image = self.lxd.get_image(fingerprint) or {}
            state = "pulled"
        elif image.get("auto_update"):
            self.lxd.update_image(fingerprint, auto_update=False)
        self.lxd.set_alias(alias, fingerprint, description)
        return {"name": pin["name"], "image": pin["image"], "serial": pin["serial"],
                "fingerprint": fingerprint, "alias": alias, "state": state,
                "size": image.get("size") or 0}

    def _check_pin_arch(self, pin):
        arch = self.host_architecture()
        if pin["arch"] and arch and pin["arch"] != arch:
            raise ServiceError("Pin %s is a %s build, and this node is %s."
                               % (pin["name"], pin["arch"], arch), 409)

    def image_source(self, image, instance_type="container"):
        """The source block a create sends; ``pin:<name>`` is that pin's build."""
        image = str(image or "").strip()
        if not image.startswith(PIN_REMOTE + ":"):
            return _image_source(image, self.lxd.remotes)
        pin = self._pin(image)
        vm = instance_type == "virtual-machine"
        fingerprint = pin["vm_fingerprint"] if vm else pin["fingerprint"]
        if not fingerprint:
            raise ServiceError("Pin %s is build %s of %s, which has no %s image. Pin a "
                               "build that has one." % (pin["name"], pin["serial"],
                                                        pin["image"], "VM" if vm
                                                        else "container"), 409)
        self._check_pin_arch(pin)
        if self.lxd.get_image(fingerprint) is not None:
            return {"type": "image", "fingerprint": fingerprint}
        # Not fetched here yet (a member that was off when it was pinned):
        # the same build from the remote, while the remote still has it.
        server = pin["server"] or self.lxd.remotes.get(pin["remote"])
        if not server:
            raise ServiceError("Pin %s is not on this node, and it has no remote called "
                               "'%s' to fetch it from." % (pin["name"], pin["remote"]), 409)
        return {"type": "image", "protocol": "simplestreams", "server": server,
                "fingerprint": fingerprint, "mode": "pull"}

    # -- bootstrap ---------------------------------------------------------

    PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")

    def list_modules(self):
        return public_modules()

    def get_module_source(self, module_id):
        try:
            return module_source(module_id)
        except BootstrapError as exc:
            raise ServiceError(exc.message, exc.code) from exc

    def upload_module(self, name, content, overwrite=False):
        try:
            return save_module(name, content, overwrite=overwrite)
        except BootstrapError as exc:
            raise ServiceError(exc.message, exc.code) from exc

    def remove_module(self, module_id):
        try:
            return delete_module(module_id)
        except BootstrapError as exc:
            raise ServiceError(exc.message, exc.code) from exc

    def update_module_settings(self, module_id, params=None, is_default=None):
        """Persist a module's parameter defaults and whether it is pre-selected."""
        module = discover_modules().get(module_id)
        if not module:
            raise ServiceError("No such module '%s'." % module_id, 404)

        declared = {p["name"] for p in module["params"]}
        if params is not None:
            unknown = sorted(set(params) - declared)
            if unknown:
                raise ServiceError(
                    "'%s' does not declare %s." % (module_id, ", ".join(unknown)))
            secret = sorted(set(params) & secret_param_names([module]))
            if secret:
                raise ServiceError(
                    "%s is a secret and is never stored; supply it when you run "
                    "the module." % ", ".join(secret))

        def mutate(settings):
            if params is not None:
                saved = settings.setdefault("module_params", {})
                # An empty value means "go back to the module's own default".
                kept = {k: str(v) for k, v in params.items() if str(v) != ""}
                if kept:
                    saved[module_id] = kept
                else:
                    saved.pop(module_id, None)
            if is_default is not None:
                defaults = [m for m in (settings.get("default_modules") or [])
                            if m != module_id]
                if is_default:
                    defaults.append(module_id)
                settings["default_modules"] = defaults

        store.update(mutate)
        return next(m for m in public_modules() if m["id"] == module_id)

    # -- bootstrap profiles ------------------------------------------------
    # Named module selections. Not to be confused with LXD/Incus profiles,
    # which configure devices and limits -- these only bundle bootstrap steps.

    def _record_name(self, name, kind):
        name = (name or "").strip()
        if not self.PROFILE_NAME.match(name):
            raise ServiceError(
                "Invalid %s name '%s'. Use letters, digits, spaces, dots, "
                "dashes and underscores." % (kind, name))
        return name

    def _stored_selection(self, modules, params, ssh_keys, available):
        """Validate a module selection for saving, and complete it.

        What is stored is every non-secret parameter the modules declare, not
        just the ones someone edited: filled from module defaults and saved
        settings as they are *now*, so the record keeps doing the same thing
        after those change, and a file copied to another machine brings its
        values along.
        """
        modules = [str(m) for m in modules or []]
        unknown = list(dict.fromkeys(m for m in modules if m not in available))
        if unknown:
            raise ServiceError("Unknown module(s): %s" % ", ".join(unknown))
        counted = occurrences(modules)
        if any(n > MAX_OCCURRENCES for _, n in counted):
            raise ServiceError("A module can be added at most %d times." % MAX_OCCURRENCES)
        once = unrepeatable(available, modules)
        if once:
            raise ServiceError("%s can only be added once; only a module whose header "
                               "says `repeatable: yes` can be added again."
                               % ", ".join(available[m]["name"] for m in once))
        selected = [available[m] for m in dict.fromkeys(modules)]

        # NAME@n is the nth occurrence's NAME, so it is only a parameter while
        # some module declaring NAME is selected at least n times.
        declared = {param_key(p["name"], n) for m, n in counted
                    for p in available[m]["params"]}
        given = {str(k): "" if v is None else str(v) for k, v in (params or {}).items()}
        undeclared = sorted(set(given) - declared)
        if undeclared:
            raise ServiceError(
                "%s is not a parameter of the selected modules."
                % ", ".join(undeclared))

        settings = store.load()
        values = {}
        for module_id, n in counted:
            for name, value in effective_params(module_id, available[module_id],
                                                settings).items():
                values[param_key(name, n)] = value
        values.update(given)

        keys = []
        for key in ssh_keys or []:
            try:
                keys.append(parse_public_key(str(key))["line"])
            except BootstrapError as exc:
                raise ServiceError(exc.message, exc.code) from exc

        # Saving from a form sends everything typed, password included. Keep
        # the selection, never the secret.
        secret = secret_param_names(selected)
        return {
            "modules": modules,
            "params": {k: v for k, v in values.items() if split_param(k)[0] not in secret},
            "ssh_keys": list(dict.fromkeys(keys)),
        }

    @staticmethod
    def _public_selection(record, available):
        """A stored selection as it is safe to list and to run.

        Profile and template files are shared and hand-edited; a secret written
        into one is dropped rather than silently reused, and a key that no
        longer parses is dropped rather than handed to a container.
        """
        secret = secret_param_names(
            available[m] for m in record["modules"] if m in available)
        keys = []
        for key in record["ssh_keys"]:
            try:
                keys.append(parse_public_key(key)["line"])
            except BootstrapError:
                continue
        return {
            "modules": record["modules"],
            "params": {k: v for k, v in record["params"].items()
                       if split_param(k)[0] not in secret},
            "ssh_keys": list(dict.fromkeys(keys)),
        }

    def list_bootstrap_profiles(self):
        available = discover_modules()
        return [
            dict({"name": name, "description": profile["description"]},
                 **self._public_selection(profile, available))
            for name, profile in sorted(store.load_profiles().items())
        ]

    def save_bootstrap_profile(self, name, modules, params=None, description="",
                               ssh_keys=None):
        name = self._record_name(name, "profile")
        if not modules:
            raise ServiceError("A profile needs at least one module.")
        entry = self._stored_selection(modules, params, ssh_keys, discover_modules())
        entry["description"] = str(description or "")[:200]
        return store.save_profile(name, entry)

    def delete_bootstrap_profile(self, name):
        if not store.delete_profile(name):
            raise ServiceError("No such profile '%s'." % name, 404)
        return {"deleted": name}

    # -- templates ---------------------------------------------------------
    # Everything the create form collects except the name, saved so that one
    # or many identical instances are a click away.

    MAX_LAUNCH = 20
    # Each create pulls from the same image and each bootstrap runs a package
    # manager, so the host's CPUs are the bottleneck, not lemondx: run one per
    # core. sched_getaffinity honours taskset/cgroup cpusets (a systemd unit
    # with CPUAffinity=) where cpu_count would report every core on the box.
    LAUNCH_WORKERS = max(1, len(os.sched_getaffinity(0))
                         if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1)

    def list_templates(self):
        available = discover_modules()
        return [
            dict(template, bootstrap=self._public_selection(template["bootstrap"], available))
            for _, template in sorted(store.load_templates().items())
        ]

    def save_template(self, name, image, instance_type="container", cpu=None,
                      memory=None, disk=None, pool=None, network=None, profiles=None,
                      ephemeral=False, start=True, bootstrap=None, description="",
                      fabric="",
                      name_prefix=None, secureboot=True, app_check=None, snapshot=None):
        name = self._record_name(name, "template")
        image = str(image or "").strip()
        source = store.clean_snapshot_source(snapshot)
        if snapshot and not source:
            raise ServiceError("A snapshot source needs 'instance' and 'name' (the "
                               "snapshot's), neither containing '/'.")
        if not image and not source:
            raise ServiceError("A template needs an image, e.g. 'ubuntu:24.04', "
                               "or a snapshot to clone.")
        if instance_type not in store.INSTANCE_TYPES:
            raise ServiceError("Unknown instance type '%s'. Try: %s"
                               % (instance_type, ", ".join(store.INSTANCE_TYPES)))
        prefix = str(name_prefix or "").strip() or instance_prefix(name)
        if not VALID_PREFIX.match(prefix):
            raise ServiceError(
                "Invalid name prefix '%s'. Use letters, digits and dashes, "
                "starting with a letter (max 50 chars)." % prefix)

        available = discover_modules()
        bootstrap = bootstrap or {}
        selection = self._stored_selection(
            bootstrap.get("modules"), bootstrap.get("params"),
            bootstrap.get("ssh_keys"), available)
        if selection["modules"] and not start:
            raise ServiceError(
                "Bootstrap modules need the instance to start, so 'start' "
                "cannot be disabled.")
        # Launching is meant to be one click, so anything it would need asking
        # for -- other than a secret, which cannot be kept -- is required now.
        needs_keys = [available[m]["name"] for m in selection["modules"]
                      if available[m]["uses_ssh_keys"]]
        if needs_keys and not selection["ssh_keys"]:
            raise ServiceError(
                "%s installs SSH keys, so the template needs at least one."
                % " and ".join(needs_keys))
        app_check = self._clean_app_check(app_check)
        if app_check and not start:
            raise ServiceError("An app check needs the instance running, so 'start' "
                               "cannot be disabled.")

        saved = store.save_template(name, {
            "description": str(description or "")[:200],
            "name_prefix": prefix,
            "image": "" if source else image,
            "snapshot": source,
            "type": instance_type,
            "cpu": str(cpu).strip() if cpu else "",
            # Checked now so a typo fails on save rather than on every launch.
            "memory": normalize_size(memory, "memory limit") or "",
            "disk": normalize_size(disk, "disk size") or "",
            "pool": str(pool).strip() if pool else "",
            "network": str(network).strip() if network else "",
            "fabric": store.clean_fabric_name(fabric),
            "profiles": [str(p) for p in profiles or []],
            "ephemeral": bool(ephemeral),
            "start": bool(start),
            # Meaningless for a container, so never stored as off for one.
            "secureboot": bool(secureboot) or instance_type != "virtual-machine",
            "bootstrap": selection,
            "app_check": app_check,
        })
        self._app_check_changed(saved["name"], saved["app_check"])
        return saved

    @staticmethod
    def _clean_app_check(app_check):
        """A template's app check as it is stored, None for none, or raise.

        The store would quietly drop what it cannot keep, which is right for a
        hand-edited file; a save is told instead.
        """
        if app_check is None or app_check == {}:
            return None
        if not isinstance(app_check, dict):
            raise ServiceError("app_check must be an object with a 'script'.")
        script = app_check.get("script")
        if script is None or (isinstance(script, str) and not script.strip()):
            return None                   # a cleared script is how one is removed
        if not isinstance(script, str):
            raise ServiceError("app_check.script must be text.")
        script = script.replace("\r\n", "\n")
        if len(script.encode("utf-8")) > store.APP_CHECK_SCRIPT_LIMIT:
            raise ServiceError("The app check script is over %d KiB."
                               % (store.APP_CHECK_SCRIPT_LIMIT // 1024))
        def seconds(key, label, bounds):
            low, high, default = bounds
            value = app_check.get(key)
            if value in (None, ""):
                return default
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
            if value is None or not value.is_integer() or not low <= value <= high:
                raise ServiceError("The app check %s must be a whole number of seconds "
                                   "from %d to %d." % (label, low, high))
            return int(value)

        interval = seconds("interval_seconds", "interval", store.APP_CHECK_INTERVAL)
        timeout = seconds("timeout_seconds", "timeout", store.APP_CHECK_TIMEOUT)
        if timeout >= interval:
            raise ServiceError("The app check timeout must be shorter than its interval.")
        # Only a script without an interpreter line is known to be sh; one
        # naming bash or python is that program's to parse, in the instance.
        if not script.startswith("#!") or script.split("\n", 1)[0].strip() in (
                "#!/bin/sh", "#!/usr/bin/env sh"):
            try:
                check_shell_syntax(script)
            except BootstrapError as exc:
                raise ServiceError("App check: %s" % exc)
        return {"script": script, "interval_seconds": interval, "timeout_seconds": timeout}

    def delete_template(self, name):
        if not store.delete_template(name):
            raise ServiceError("No such template '%s'." % name, 404)
        self._app_check_changed(name, None)
        return {"deleted": name}

    def _template(self, name):
        template = next((t for t in self.list_templates() if t["name"] == name), None)
        if not template:
            raise ServiceError("No such template '%s'." % name, 404)
        return template

    def _launch_bootstrap(self, template, params):
        """The bootstrap a launch of ``template`` would run, or raise.

        Anything that would fail for every instance -- a missing secret, a
        module deleted since the template was saved -- is caught here, before
        anything is created or destroyed.
        """
        selection = template["bootstrap"]
        if not selection["modules"]:
            return None
        available = discover_modules()
        gone = [m for m in selection["modules"] if m not in available]
        if gone:
            raise ServiceError(
                "Template '%s' uses module(s) that no longer exist: %s"
                % (template["name"], ", ".join(gone)), 409)
        selected = [available[m] for m in selection["modules"]]
        values = dict(selection["params"])
        values.update({str(k): str(v) for k, v in (params or {}).items()})
        once = unrepeatable(available, selection["modules"])
        if once:
            raise ServiceError(
                "Template '%s' runs %s more than once, which it no longer allows; edit "
                "the template." % (template["name"], ", ".join(once)), 409)
        missing = missing_secrets(available, selection["modules"], values)
        if missing:
            raise ServiceError(
                "Supply a value for %s -- secrets are never saved in a "
                "template." % ", ".join(missing))
        if any(m["uses_ssh_keys"] for m in selected) and not selection["ssh_keys"]:
            raise ServiceError(
                "Template '%s' has no usable SSH key; edit it to add one."
                % template["name"], 409)
        return {"modules": selection["modules"], "params": values,
                "ssh_keys": selection["ssh_keys"]}

    def place_template(self, template):
        """The template as this host can actually run it, plus what had to change.

        A template is written once and launched anywhere -- on this host, or on
        any node it is synced to -- but a storage pool, a network and a profile
        are all local names. A node that has never heard of "fast-nvme" would
        otherwise fail every instance in the launch, which is the worst of both
        outcomes: nothing runs, and the user finds out one instance at a time.

        So a placement this host cannot honour falls back to its own default --
        the root pool and NIC of the default profile -- and the substitution is
        reported: returned as notes for the run record and the UI, and printed,
        because a launch started from another node's UI is only visible here in
        the log. The notes say "this node" rather than naming it -- a node has
        no name of its own at this layer, and the coordinator that collects them
        knows which node each set came back from.
        """
        notes = []
        placed = dict(template)

        if template["pool"]:
            pools = {p.get("name") for p in self.lxd.list_storage_pools()}
            if template["pool"] not in pools:
                fallback = self._root_pool(template["profiles"] or ["default"])
                placed["pool"] = ""          # empty: take the profile's root disk
                notes.append(
                    "No storage pool '%s' on this node; used %s instead."
                    % (template["pool"],
                       "the default profile's pool (%s)" % fallback if fallback
                       else "the default profile's root disk"))

        if template["network"]:
            networks = {n.get("name") for n in self.lxd.list_networks() if n.get("managed")}
            if template["network"] not in networks:
                _, nic = self._profile_nic(template["profiles"] or ["default"])
                placed["network"] = ""       # empty: take the profile's NIC
                notes.append(
                    "No network '%s' on this node; used %s instead."
                    % (template["network"],
                       "the default profile's network (%s)" % _nic_network(nic)
                       if _nic_network(nic) else "the default profile's NIC"))

        if template.get("fabric"):
            # Resolved to a local name like pool and network above, so
            # `placed["fabric"]` is this node's bridge rather than the name.
            bridge = (self.fabric_bridge(template["fabric"])
                      if self.fabric_bridge else "") or ""
            placed["fabric"] = bridge
            if not bridge:
                notes.append("This node is not on the fabric %s; launched without "
                             "its NIC, so these instances can only be reached "
                             "from this host." % template["fabric"])

        wanted = [p for p in (template["profiles"] or []) if p != "default"]
        if wanted:
            known = {p.get("name") for p in self.lxd.list_profiles()}
            missing = [p for p in wanted if p not in known]
            if missing:
                placed["profiles"] = [p for p in (template["profiles"] or [])
                                      if p not in missing] or ["default"]
                notes.append(
                    "No profile %s on this node; used %s instead."
                    % (", ".join("'%s'" % m for m in missing),
                       ", ".join(placed["profiles"])))

        for note in notes:
            _log(note)
        return placed, notes

    # -- staleness ---------------------------------------------------------

    def _mark_stale(self, summaries):
        """Say, on each instance, which of its template and stack changed since it was made.

        Judged against this node's copies, which sync keeps level with every
        other node's, so each node can answer for its own instances. An
        instance with no revision recorded (made before revisions were) is
        never called stale: there is nothing to compare, and guessing would
        flag every instance there is.
        """
        tracked = [s for s in summaries if s["revisions"]["template"] or s["revisions"]["stack"]]
        if not tracked:
            for summary in summaries:
                summary["stale"] = []
            return summaries
        templates = {n: template_revision(t) for n, t in store.load_templates().items()}
        stacks = {n: stack_revision(s) for n, s in store.load_stacks().items()}
        for summary in summaries:
            summary["stale"] = [kind for kind, current in (
                ("template", templates.get(summary["template"] or "")),
                ("stack", stacks.get(summary["stack"] or "")))
                # A definition that has gone is a different problem from one
                # that has moved on; only the second makes an instance stale.
                if current and summary["revisions"][kind]
                and summary["revisions"][kind] != current]
        return summaries

    def _revision_of(self, kind, name):
        record = (store.load_templates() if kind == "template" else store.load_stacks()).get(name)
        if not record:
            return ""
        return template_revision(record) if kind == "template" else stack_revision(record)

    def _create_from_template(self, template, bootstrap, instance_name, stack=None,
                              stack_rev=None):
        """One instance, reported rather than raised so its siblings carry on.

        ``template`` may be placed for this host; the revision recorded is the
        saved template's, which is the one every node holds a copy of.
        ``stack_rev`` keeps a recreated instance's stack revision: a
        recreate applies the template as it is now, not the stack.
        """
        config = {TEMPLATE_CONFIG_KEY: template["name"],
                  TEMPLATE_REVISION_KEY: self._revision_of("template", template["name"])}
        if stack:
            config[STACK_CONFIG_KEY] = stack
            config[STACK_REVISION_KEY] = stack_rev if stack_rev is not None \
                else self._revision_of("stack", stack)
        try:
            source = template.get("snapshot")
            container = self.create_container(
                name=instance_name, image=template["image"],
                source_snapshot="%s/%s" % (source["instance"], source["name"])
                if source else None,
                instance_type=template["type"],
                profiles=template["profiles"] or None,
                cpu=template["cpu"] or None, memory=template["memory"] or None,
                disk=template["disk"] or None, pool=template["pool"] or None,
                network=template["network"] or None,
                fabric=template.get("fabric") or None,
                ephemeral=template["ephemeral"], start=template["start"],
                secureboot=template["secureboot"],
                config=config, bootstrap=bootstrap,
                # A template launches its own values; it should not quietly
                # rewrite the module settings the create form starts from.
                remember_params=False,
            )
        except (ServiceError, LXDError, BootstrapError) as exc:
            return {"name": instance_name, "ok": False, "error": str(exc),
                    "container": None}
        except Exception:                             # noqa: BLE001
            return {"name": instance_name, "ok": False,
                    "error": "Unexpected error while creating this instance.",
                    "container": None}
        result = container.get("bootstrap")
        return {"name": instance_name, "ok": result is None or result["ok"],
                "error": None, "container": container}

    def _remove_instance(self, instance_name):
        """Stop and delete one instance; reported rather than raised."""
        try:
            self.delete_container(instance_name, force=True)
        except LXDError as exc:
            # An ephemeral instance deletes itself when stopped, so the delete
            # that follows finds nothing -- which is the outcome wanted.
            if exc.code != 404:
                return {"name": instance_name, "ok": False, "error": str(exc),
                        "container": None}
        except ServiceError as exc:
            return {"name": instance_name, "ok": False, "error": str(exc),
                    "container": None}
        return {"name": instance_name, "ok": True, "error": None, "container": None}

    def _each(self, names, work, workers=None):
        """Run ``work`` over instance names one per CPU core, keeping order."""
        if not names:
            return []
        limit = min(len(names), workers or self.LAUNCH_WORKERS)
        with ThreadPoolExecutor(max_workers=limit) as pool:
            return list(pool.map(eventlog.carry(work), names))

    def prepare_launch(self, name, count=1, prefix=None, params=None, names=None,
                       place=True):
        """Everything a launch needs, checked, before anything is created.

        Returns ``(template, bootstrap, names, count, prefix, notes)``, where
        ``names`` is None unless the caller chose them. The template comes back
        with its placement already adjusted to this host -- see
        ``place_template()`` -- and ``notes`` says where that differed from
        what was asked for.

        ``names`` names the instances explicitly instead of taking the next
        free ``<prefix>-<n>``. A cluster launch uses it so numbering is unique
        across every node it spreads over, not just within each one; on its own
        a node picks its own names, inside the run, so two launches racing
        cannot pick alike.

        ``place=False`` validates without adjusting placement, for a cluster
        launch that is only checking a template it will run somewhere else:
        substituting this host's pool would be noise in the log about a node
        that is not taking part.
        """
        template = self._template(name)
        if names is not None:
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                raise ServiceError("'names' must be a list of instance names.")
            names = list(dict.fromkeys(n.strip() for n in names))
            for candidate in names:
                if not VALID_NAME.match(candidate):
                    raise ServiceError(
                        "Invalid name '%s'. Use letters, digits and dashes, "
                        "starting with a letter (max 62 chars)." % candidate)
            count = len(names)
        try:
            count = int(count)
        except (TypeError, ValueError):
            raise ServiceError("Count must be a whole number.")
        if not 1 <= count <= self.MAX_LAUNCH:
            raise ServiceError("Launch between 1 and %d instances at a time."
                               % self.MAX_LAUNCH)
        prefix = (str(prefix or "").strip() or template["name_prefix"]
                  or instance_prefix(template["name"]))
        if not VALID_PREFIX.match(prefix):
            raise ServiceError(
                "Invalid name prefix '%s'. Use letters, digits and dashes, "
                "starting with a letter (max 50 chars)." % prefix)
        bootstrap = self._launch_bootstrap(template, params)
        if template["image"].startswith(PIN_REMOTE + ":") and not template["snapshot"]:
            # A pin that is gone, or has no build of this type or arch, fails
            # every instance alike.
            self.image_source(template["image"], template["type"])
        notes = []
        if place:
            template, source_notes = self.resolve_source(template)
            template, notes = self.place_template(template)
            notes = source_notes + notes
        return template, bootstrap, names, count, prefix, notes

    def launch_instances(self, template, bootstrap, names, stack=None):
        """Create the named instances from an already-prepared template.

        Separate from ``launch_template`` because a cluster-wide launch tracks
        the run itself, across every node, and must not start a second run on
        this one; see ``cluster.py``.
        """
        return self._each(names, lambda n: self._create_from_template(
            template, bootstrap, n, stack))

    def launch_template(self, name, count=1, prefix=None, params=None, background=False,
                        names=None, stack=None):
        """Create ``count`` instances from a template, several at a time.

        Anything that would fail for every instance -- a missing secret or
        module, a bad count -- is refused before one is created. After that,
        each instance reports its own outcome: one failing to create or to
        bootstrap does not stop the others, and the ones that exist stay.
        """
        template, bootstrap, chosen, count, prefix, notes = self.prepare_launch(
            name, count=count, prefix=prefix, params=params, names=names)
        stack = self.stack_tag(stack)

        def work():
            # Names the caller did not choose are picked inside the run, so two
            # launches racing here cannot pick alike.
            return self.launch_instances(
                template, bootstrap, chosen or self._free_names(prefix, count), stack)
        return self.track_run(template["name"], "launch", count, work, background,
                              notes=notes)

    def stack_tag(self, stack):
        """The stack a launch belongs to, as it will be tagged, or None."""
        if stack in (None, ""):
            return None
        return self._record_name(str(stack), "stack")

    def template_instances(self, name, stale=False):
        """Names of the instances launched from template ``name``, sorted.

        ``stale`` narrows it to those made from an older version of the
        template that no stack launched: a stack's instances take values the
        stack rendered for them, which recreating from the template would drop,
        so the stack is what replaces those.
        """
        return sorted(c["name"] for c in self.list_containers()
                      if self.of_template(c, name, stale))

    @staticmethod
    def of_template(container, name, stale=False):
        """Whether a listed instance counts as one of template ``name``'s.

        Shared with ``ClusterService.template_instances()``, which applies it
        to every node's listing: each node then checks its share against its
        own tagged set, so the two must agree on what that set is.
        """
        return container["template"] == name and (
            not stale or ("template" in container["stale"] and not container["stack"]))

    def _confirmed_instances(self, name, confirmed, stale=False):
        """The template's instances, provided they are exactly what was confirmed.

        Destroying by tag alone would also take out an instance launched after
        the user looked -- by another tab, or the CLI. So the caller names what
        it showed, and any difference refuses the whole request. With
        ``stale`` the set compared is the stale one, for the same reason.
        """
        if not isinstance(confirmed, list) or not all(isinstance(n, str) for n in confirmed):
            raise ServiceError(
                "List the instances to act on in 'instances', as confirmed.")
        current = self.template_instances(name, stale=stale)
        if sorted(set(confirmed)) != current:
            raise ServiceError(
                "The %sinstances from template '%s' have changed since they were "
                "confirmed (now: %s). Nothing was changed; review and try again."
                % ("stale " if stale else "", name, ", ".join(current) or "none"), 409)
        if not current:
            raise ServiceError("No %sinstances were launched from template '%s'."
                               % ("stale " if stale else "", name), 404)
        return current

    # The three below come in pairs, like launch: an ``*_instances`` method that
    # does the work and returns per-instance results, and a wrapper that records
    # it as this template's one run. A cluster-wide run tracks itself, across
    # every node, and calls the untracked half here -- tracking twice under the
    # same template name would deadlock it against itself.

    def destroy_instances(self, name, instances):
        """Stop and delete a template's instances. No run tracking."""
        names = self._confirmed_instances(name, instances)
        return self._each(names, self._remove_instance)

    def destroy_template_instances(self, name, instances, background=False):
        """Stop and delete every instance launched from a template.

        Works for a template that has since been deleted too, since the tag on
        the instances is all it needs.
        """
        names = self._confirmed_instances(name, instances)
        return self.track_run(name, "destroy", len(names),
                              lambda: self.destroy_instances(name, names), background)

    def recreate_instances(self, name, instances, params=None, stale=False):
        """Replace a template's instances with fresh ones. ``(results, notes)``."""
        template = self._template(name)
        names = self._confirmed_instances(name, instances, stale=stale)
        bootstrap = self._launch_bootstrap(template, params)
        template, source_notes = self.resolve_source(template)
        template, notes = self.place_template(template)
        notes = source_notes + notes
        # A recreated instance stays in the stack it was launched by, at the
        # stack revision it had: recreating applies the template, not the stack.
        stacks = {c["name"]: (c["stack"], c["revisions"]["stack"])
                  for c in self.list_containers()}

        def replace(instance_name):
            removed = self._remove_instance(instance_name)
            if not removed["ok"]:
                return dict(removed, error="Not recreated: could not delete it: %s"
                            % removed["error"])
            stack, revision = stacks.get(instance_name, (None, None))
            return self._create_from_template(template, bootstrap, instance_name,
                                              stack, revision or "")

        return self._each(names, replace), notes

    def recreate_template_instances(self, name, instances, params=None, background=False,
                                    stale=False):
        """Replace each of a template's instances with a fresh one of the same name.

        The new instances take the template as it is now, so this is also how
        an edited template reaches instances launched before the edit. Every
        check that could fail for all of them runs before anything is deleted;
        after that, an instance that fails to delete is left alone rather than
        recreated alongside itself.
        """
        # Validated before the run is recorded, so a bad request is refused
        # rather than filed as a run that failed.
        names = self._confirmed_instances(name, instances, stale=stale)
        template = self._template(name)
        self._launch_bootstrap(template, params)
        self.resolve_source(template)
        return self.track_run(name, "recreate", len(names),
                              lambda: self.recreate_instances(name, names, params, stale),
                              background)

    # A command is mostly waiting on the guest, not on the host, so it can
    # fan out wider than a create.
    EXEC_WORKERS = 10
    # Each stream is cut to its last part: run records are polled every few
    # seconds, and a chatty command on twenty instances would otherwise ship
    # megabytes each time.
    EXEC_OUTPUT_LIMIT = 32 * 1024
    MAX_EXEC_TIMEOUT = 3600

    def exec_template_instances(self, name, command, instances, timeout=300,
                                background=False):
        """Run one shell command on some or all of a template's instances at once.

        ``instances`` names where to run it -- what the user picked -- and each
        must still be one of the template's. Unlike destroy, a subset is the
        normal case: stopped instances cannot run anything. A command that
        runs and exits non-zero is a result for that instance, not an error for
        the whole run.
        """
        command = str(command or "").strip()
        if not command:
            raise ServiceError("No command given.")
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            raise ServiceError("Timeout must be a whole number of seconds.")
        if not 1 <= timeout <= self.MAX_EXEC_TIMEOUT:
            raise ServiceError("Timeout must be between 1 and %d seconds."
                               % self.MAX_EXEC_TIMEOUT)
        names = self._members_among(name, instances)
        return self.track_run(
            name, "exec", len(names),
            lambda: self.exec_instances(name, command, names, timeout),
            background, command=command)

    def exec_instances(self, name, command, instances, timeout=300):
        """Run one command on a template's instances. No run tracking."""
        names = self._members_among(name, instances)
        limit = self.EXEC_OUTPUT_LIMIT

        def tail(text):
            text = text or ""
            return (text[-limit:], True) if len(text) > limit else (text, False)

        def run_one(instance_name):
            try:
                outcome = self.exec_command(instance_name, command, timeout=timeout)
            except (ServiceError, LXDError) as exc:
                return {"name": instance_name, "ok": False, "error": str(exc),
                        "container": None, "exec": None}
            stdout, cut_out = tail(outcome.get("stdout"))
            stderr, cut_err = tail(outcome.get("stderr"))
            exit_code = outcome.get("exit_code", 0)
            return {"name": instance_name, "ok": exit_code == 0, "error": None,
                    "container": None,
                    "exec": {"exit_code": exit_code, "stdout": stdout, "stderr": stderr,
                             "truncated": cut_out or cut_err}}

        return self._each(names, run_one, workers=self.EXEC_WORKERS)

    def _members_among(self, name, instances):
        """The named instances, provided every one still belongs to the template."""
        if (not isinstance(instances, list) or not instances
                or not all(isinstance(n, str) for n in instances)):
            raise ServiceError("List the instances to run on in 'instances'.")
        members = set(self.template_instances(name))
        strangers = sorted(set(instances) - members)
        if strangers:
            raise ServiceError(
                "%s %s not from template '%s' (any more). Nothing was run."
                % (", ".join(strangers), "is" if len(strangers) == 1 else "are", name), 409)
        return sorted(set(instances))

    def track_run(self, name, action, count, work, background=False, command=None,
                  notes=(), nodes=()):
        """Run ``work`` as the one operation on a template's instances.

        The run is recorded so any client can follow it and find its result.
        With ``background`` the call returns that record at once and the work
        runs on a thread -- how the web UI starts every run, so nothing depends
        on the request staying open. Without, it blocks and returns the result,
        which is what the CLI wants. Only one runs per template, since a second
        recreate or destroy would be deleting instances the first is in the
        middle of bootstrapping.

        ``notes`` are things the user should know that did not stop the run --
        a template asking for a storage pool this host does not have, say.
        ``cluster.py`` calls this for a launch spread over several nodes, which
        is why it is not private: the run belongs to the template either way,
        and one lock must cover both kinds or a cluster launch and a local one
        could work on the same instances.
        """
        with self._runs_lock:
            current = self._runs.get(name)
            if current and current["finished_at"] is None:
                raise ServiceError(
                    "Template '%s' is already busy: %s of %d instance(s) is still "
                    "running. Wait for it to finish." % (
                        name, current["action"], current["count"]), 409)
            run = {"template": name, "action": action, "count": count,
                   "command": command, "started_at": time.time(), "finished_at": None,
                   "result": None, "error": None, "notes": list(notes),
                   "nodes": list(nodes)}
            self._runs[name] = run

        def execute():
            result = error = None
            try:
                instances = work()
                # A run whose work returns notes of its own -- a cluster launch
                # collecting each node's -- adds them to the ones found up front.
                if isinstance(instances, tuple):
                    instances, extra = instances
                    with self._runs_lock:
                        run["notes"] = list(run["notes"]) + list(extra)
                with self._runs_lock:
                    notes = list(run["notes"])
                # Carried on the result as well as the run record, so a caller
                # that waited for the run is told what was substituted without
                # having to go and read the record it never needed.
                result = {"template": name, "ok": all(i["ok"] for i in instances),
                          "instances": instances, "notes": notes}
                return result
            except (ServiceError, LXDError, BootstrapError) as exc:
                error = str(exc)
                raise
            except Exception:
                error = "Unexpected error while running %s." % action
                raise
            finally:
                with self._runs_lock:
                    run.update(finished_at=time.time(), result=_run_record(result),
                               error=error)

        if background:
            self._in_background("%s-%s" % (action, name), execute)
            with self._runs_lock:
                return dict(run)
        return execute()

    def template_runs(self):
        """Every recorded run, in progress or finished, oldest first.

        Kept by this process only, for as long as it runs: the CLI in another
        process neither sees these nor is blocked by them.
        """
        with self._runs_lock:
            return sorted((dict(run) for run in self._runs.values()),
                          key=lambda run: run["started_at"])

    def dismiss_template_run(self, name):
        """Forget a finished run's result. One still running cannot be dismissed."""
        with self._runs_lock:
            run = self._runs.get(name)
            if not run:
                raise ServiceError("No recorded run for template '%s'." % name, 404)
            if run["finished_at"] is None:
                raise ServiceError("That run is still in progress.", 409)
            del self._runs[name]
        return {"dismissed": name}

    def _free_names(self, prefix, count):
        """The lowest ``<prefix>-<n>`` names no instance has taken."""
        taken = {i.get("name") for i in self.lxd.list_instances()}
        names, index = [], 1
        while len(names) < count:
            candidate = "%s-%d" % (prefix, index)
            if candidate not in taken:
                names.append(candidate)
            index += 1
        return names

    def list_ssh_keys(self):
        """Public keys found on the host, offered for installing into containers."""
        return list_host_ssh_keys()

    def bootstrap(self, name, modules, params=None, ssh_keys=None, timeout=900,
                  remember=True):
        """Run the selected bash modules inside a running container.

        Saved per-module defaults are applied first, then anything the caller
        passed. When the run succeeds the values used become the new defaults,
        so the next container starts from what worked last time.
        """
        if not modules:
            raise ServiceError("No bootstrap modules selected.")

        settings = store.load()
        available = discover_modules()
        merged = {}
        for module_id, n in occurrences(modules):
            module = available.get(module_id)
            if module:
                for key, value in effective_params(module_id, module, settings).items():
                    merged[param_key(key, n)] = value
        merged.update(params or {})

        try:
            result = BootstrapRunner(self.lxd).run(
                name, modules, params=merged, ssh_keys=ssh_keys, timeout=timeout
            )
        except BootstrapError as exc:
            raise ServiceError(exc.message, exc.code) from exc

        if remember and result["ok"]:
            self._remember_params(modules, merged, available)
        return result

    def _remember_params(self, modules, values, available):
        """Store the values each module declared, against that module."""
        def mutate(settings):
            saved = settings.setdefault("module_params", {})
            for module_id in modules:
                module = available.get(module_id)
                if not module or not module["params"]:
                    continue
                for param in module["params"]:
                    name = param["name"]
                    if name not in values or param.get("secret"):
                        continue
                    value = str(values[name])
                    if value == param["default"]:
                        # Nothing to remember; keep the file tidy.
                        saved.get(module_id, {}).pop(name, None)
                    else:
                        saved.setdefault(module_id, {})[name] = value
                if not saved.get(module_id):
                    saved.pop(module_id, None)
        try:
            store.update(mutate)
        except OSError:
            pass          # remembering is a convenience, not worth failing a run

    def validate_ssh_key(self, text):
        try:
            return parse_public_key(text)
        except BootstrapError as exc:
            raise ServiceError(exc.message, exc.code) from exc

    def browse_images(self, remote=None, arch=None, refresh=False):
        """The full catalog of one or more remotes, flagged with what is local.

        Entries carry the same fingerprint LXD stores, so "already downloaded"
        is an exact match rather than a guess from the alias.
        """
        remotes = self.lxd.remotes
        if remote:
            if remote not in remotes:
                raise ServiceError(
                    "Unknown remote '%s'. This daemon knows: %s"
                    % (remote, ", ".join(sorted(remotes)))
                )
            wanted = [remote]
        else:
            wanted = [r for r in BROWSABLE_REMOTES.get(self.lxd.flavor, ("images",))
                      if r in remotes]

        if arch is None:
            arch = self.host_architecture()

        local = {img.get("fingerprint") for img in self.lxd.list_images()}
        # A pin is for one architecture's build; matched to the entry by any of
        # its aliases, since an older pin may have been made under another.
        pins = {}
        for pin in sorted(store.load_pins().values(), key=lambda p: -p["pinned_at"]):
            for alias in pin["aliases"]:
                pins.setdefault((pin["remote"], pin["arch"], alias), []).append(
                    {"name": pin["name"], "serial": pin["serial"],
                     "nicknames": pin["nicknames"]})

        entries, errors = [], {}
        for name in wanted:
            try:
                catalog = fetch_catalog(name, remotes[name], refresh=refresh)
            except CatalogError as exc:
                errors[name] = str(exc)
                continue
            for entry in catalog:
                if arch and arch != "all" and entry["arch"] != arch:
                    continue
                entry = dict(entry)
                entry["pins"] = pins.get((name, entry["arch"], entry["alias"]), [])
                entry["cached"] = entry["container_fingerprint"] in local
                entry["cached_vm"] = bool(entry["vm_fingerprint"]) and \
                    entry["vm_fingerprint"] in local
                entries.append(entry)

        return {
            "entries": entries,
            "remotes": sorted(remotes),
            "browsed": wanted,
            "architecture": arch,
            "errors": errors,
        }

    def host_architecture(self):
        """Host architecture in the naming simplestreams uses."""
        architectures = (self.lxd.server_info().get("environment") or {}) \
            .get("architectures") or []
        for value in architectures:
            if value in ARCH_ALIASES:
                return ARCH_ALIASES[value]
            if value in set(ARCH_ALIASES.values()):
                return value
        return None

    # -- networking --------------------------------------------------------

    def list_networks(self):
        """All interfaces the daemon can see, managed ones first.

        ``attachable`` marks what ``create_container(network=...)`` accepts,
        and ``default`` the one a new instance joins when it names none.
        """
        clustered = self._clustered()
        _, nic = self._profile_nic(["default"])
        default = _nic_network(nic)
        summaries = []
        for network in self.lxd.list_networks():
            config = network.get("config") or {}
            reason = _network_read_only_reason(network, clustered)
            summaries.append({
                "name": network.get("name"),
                "type": network.get("type"),
                "managed": bool(network.get("managed")),
                "status": network.get("status"),
                "description": network.get("description") or "",
                "ipv4_address": config.get("ipv4.address") or "",
                "ipv6_address": config.get("ipv6.address") or "",
                "used_by": len(network.get("used_by") or []),
                "default": network.get("name") == default,
                "attachable": _attachable(network),
                "manageable": not reason,
                "read_only_reason": reason,
                "lan": self._lan_info(network),
            })
        summaries.sort(key=lambda n: (not n["managed"], n["name"]))
        return summaries

    def create_network(self, name, description="", config=None):
        """Create a managed bridge.

        Unset addresses follow ``BRIDGE_DEFAULTS``, so a bare name gets what
        ``lemondx init`` would have made: a free IPv4 subnet behind NAT.
        """
        if not VALID_NETWORK_NAME.match(name or ""):
            raise ServiceError(
                "Invalid network name '%s'. Use letters, digits and dashes, starting "
                "with a letter (max 15 chars, the kernel's limit for an interface)."
                % name)
        self._network_mutation_context()
        values = dict(BRIDGE_DEFAULTS)
        values.update(self._network_config(config))
        # An IPv6 subnet with NAT left unsaid would be unreachable from outside
        # and have no way out, which nobody asks for by leaving a box unticked.
        if values.get("ipv6.address", "none") != "none":
            values.setdefault("ipv6.nat", "true")
        values = {k: v for k, v in values.items() if v != ""}
        with self._networks_lock:
            if any(n.get("name") == name for n in self.lxd.list_networks()):
                raise ServiceError(
                    "An interface called '%s' already exists on this host." % name, 409)
            self._check_subnets_free(name, values)
            self.lxd.create_network(name, values, str(description or "")[:200])
        return self.get_network(name)

    def update_network(self, name, description=None, config=None):
        """Change a managed bridge's settings; an empty value unsets a key."""
        clustered = self._network_mutation_context()
        changes = self._network_config(config)
        with self._networks_lock:
            # Read under the lock so the merge below starts from what the
            # previous update left, not from before it.
            current = self.lxd.get_network(name)
            reason = _network_read_only_reason(current, clustered)
            if reason:
                raise ServiceError("Network '%s' is read-only: %s" % (name, reason), 409)
            if _lan_kind(current):
                raise ServiceError(
                    "'%s' puts instances on the LAN, where the router hands out "
                    "addresses; it has no settings to change here. Delete it and "
                    "make another to change which NIC it uses." % name, 409)
            values = dict(current.get("config") or {})
            self._check_subnets_free(name, {
                key: value for key, value in changes.items()
                if key.endswith(".address") and value != values.get(key)})
            for key, value in changes.items():
                if value == "":
                    values.pop(key, None)
                else:
                    values[key] = value
            if description is None:
                description = current.get("description") or ""
            self.lxd.update_network(name, str(description)[:200], values)
        return self.get_network(name)

    def delete_network(self, name):
        """Delete a managed bridge nothing is attached to.

        There is deliberately no cascade: detaching a NIC cuts an instance off
        without it being deleted, which is easy to miss and hard to notice.
        """
        clustered = self._network_mutation_context()
        current = self.lxd.get_network(name)
        reason = _network_read_only_reason(current, clustered)
        if reason:
            raise ServiceError("Network '%s' is read-only: %s" % (name, reason), 409)
        instances, profiles = _network_users(current)
        if instances or profiles or current.get("used_by"):
            parts = []
            if instances:
                parts.append("instance%s %s" % (
                    "s" if len(instances) > 1 else "", ", ".join(instances)))
            if profiles:
                parts.append("profile%s %s" % (
                    "s" if len(profiles) > 1 else "", ", ".join(profiles)))
            raise ServiceError(
                "Network '%s' is still used by %s. Detach %s first."
                % (name, " and ".join(parts) or "other resources",
                   "them" if len(instances) + len(profiles) != 1 else "it"), 409)
        self.lxd.delete_network(name)
        if (_lan_kind(current) or {}).get("kind") == "macvlan" and self.lan_changed:
            self.lan_changed()
        if (_lan_kind(current) or {}).get("kind") == "bridge":
            # Its Docker exception has nothing left to let through.
            return {"deleted": name, "notes": self.sync_lan_firewall()}
        return {"deleted": name}

    # -- containers on the LAN ---------------------------------------------
    #
    # Instances on the network a host NIC is on, addressed by its router rather
    # than the daemon: a bridge over a spare NIC, macvlan on any wired one, or
    # the NIC the host uses turned into a bridge port. Which of those a NIC
    # allows depends on it, so the choice is offered per NIC -- see
    # docs/networking.md and hostnet.py.

    LAN_MODES = ("bridge", "macvlan", "convert")

    def lan_interfaces(self):
        """This host's wired and wireless NICs, and what each can do for the LAN."""
        defaults = hostnet.default_route_devices()
        helper = hostnet.available()
        found = []
        for network in self.lxd.list_networks():
            nic = network.get("name") or ""
            if network.get("managed") or not hostnet.nic_facts(nic)["physical"]:
                continue
            state = self.lxd.get_network_state(nic) or {}
            addresses = ["%s/%s" % (a.get("address"), a.get("netmask"))
                         for a in state.get("addresses") or [] if a.get("scope") == "global"]
            found.append(self._lan_nic(nic, addresses, state, nic in defaults, helper))
        found.sort(key=lambda n: (not n["default_route"], n["name"]))
        return {"interfaces": found, "helper": helper, "docker": hostnet.docker_present(),
                "default_nic": hostnet.default_route_device()}

    @staticmethod
    def _lan_nic(nic, addresses, state, default_route, helper):
        facts = hostnet.nic_facts(nic)
        manager = hostnet.nm_device(nic)
        modes = {}
        for mode in ContainerService.LAN_MODES:
            if facts["wireless"]:
                why = ("Wi-Fi cannot carry them: an access point drops frames from "
                       "any MAC that did not associate with it.")
            elif facts["master"]:
                why = ("It is already a port of %s; instances can join %s directly."
                       % (facts["master"], facts["master"]))
            elif mode == "bridge" and addresses:
                why = ("It holds this host's addresses, which a bridge port cannot "
                       "keep. Convert it instead, or use macvlan.")
            elif mode == "convert" and not addresses:
                why = "It has no address to move; bridge it instead."
            elif mode == "convert" and manager is None:
                why = ("NetworkManager does not run it, so lemondx cannot move its "
                       "address. Make the bridge in your network configuration.")
            else:
                why = ""
            # Converting without the helper is still possible, by hand: the
            # plan is the same commands, so it is offered to read and run.
            manual = mode == "convert" and not why and not helper
            if manual:
                why = ("lemondx cannot run its helper as root here, so the commands "
                       "are shown for you to run.")
            modes[mode] = {"available": not why, "reason": why, "manual": manual}
        return {
            "name": nic,
            "hwaddr": state.get("hwaddr") or "",
            "up": state.get("state") == "up",
            "addresses": addresses,
            "default_route": default_route,
            "wireless": facts["wireless"],
            "master": facts["master"],
            "network_manager": manager["connection"] if manager else "",
            "modes": modes,
        }

    def _lan_nic_record(self, nic):
        record = next((n for n in self.lan_interfaces()["interfaces"] if n["name"] == nic),
                      None)
        if record is None:
            raise ServiceError("'%s' is not a network card on this host." % nic, 404)
        return record

    def create_lan_network(self, nic, mode, name, description="", default=False):
        """A daemon network putting instances on ``nic``'s LAN: ``bridge`` or ``macvlan``.

        Neither has an address of its own, so the daemon runs no DHCP and no
        NAT on it; whatever serves that LAN addresses the instances.

        ``default`` takes the NIC the default route leaves by instead of a
        name, which is how one request means the same thing on every node.
        Asking again for a LAN network that is already there, on the same NIC
        and the same way, returns it (``existing``) rather than refusing: that
        is re-running a cluster-wide create after a node was added or off.
        """
        if default:
            nic = hostnet.default_route_device()
            if not nic:
                raise ServiceError("This host has no IPv4 default route, so it has no "
                                   "default interface to use.", 409)
        if mode not in ("bridge", "macvlan"):
            raise ServiceError("A LAN network is 'bridge' or 'macvlan'; converting the "
                               "NIC the host uses is its own action.")
        if not VALID_NETWORK_NAME.match(name or ""):
            raise ServiceError(
                "Invalid network name '%s'. Use letters, digits and dashes, starting "
                "with a letter (max 15 chars, the kernel's limit for an interface)."
                % name)
        self._network_mutation_context()
        record = self._lan_nic_record(nic)
        if not record["modes"][mode]["available"]:
            raise ServiceError("%s cannot be used for %s: %s"
                               % (nic, mode, record["modes"][mode]["reason"]), 409)
        with self._networks_lock:
            existing = next((n for n in self.lxd.list_networks() if n.get("name") == name),
                            None)
            if existing is not None:
                same = _lan_kind(existing) == {"kind": mode, "nic": nic}
                if not same:
                    raise ServiceError(
                        "An interface called '%s' already exists on this host." % name, 409)
                return dict(self.get_network(name), existing=True)
            description = str(description or "")[:200] or "%s's LAN" % nic
            if mode == "bridge":
                self.lxd.create_network(name, {
                    "bridge.external_interfaces": nic,
                    "ipv4.address": "none", "ipv6.address": "none",
                }, description)
            else:
                self.lxd.create_network(name, {"parent": nic}, description, kind="macvlan")
        network = dict(self.get_network(name), existing=False)
        if mode == "bridge":
            network["notes"] = self.sync_lan_firewall()
        elif self.lan_changed:
            self.lan_changed()
        return network

    def lan_convert_plan(self, nic, bridge):
        """The commands converting ``nic`` would run, for a person to read or run."""
        return [c.record() for c in hostnet.lan_convert_plan(nic, bridge)[0]]

    def convert_nic(self, nic, bridge):
        """Make the NIC the host uses a port of a new bridge that keeps its address.

        Through the root helper and NetworkManager, which keeps it across
        reboots. The helper undoes it by itself if the bridge comes up without
        the address, or the way out, the NIC had -- so a failure here leaves
        the host as it was, and says so.
        """
        if not VALID_NETWORK_NAME.match(bridge or ""):
            raise ServiceError("Invalid bridge name '%s' (letters, digits and dashes, "
                               "max 15 chars)." % bridge)
        record = self._lan_nic_record(nic)
        if not record["modes"]["convert"]["available"]:
            raise ServiceError("%s cannot be converted: %s"
                               % (nic, record["modes"]["convert"]["reason"]), 409)
        if not hostnet.available():
            raise ServiceError("lemondx cannot run its helper as root here, so it "
                               "cannot convert %s. Run the commands it shows yourself, "
                               "or install the sudo rule (docs/networking.md)." % nic, 503)
        answer = hostnet.delegate({"lan": {"action": "convert", "nic": nic,
                                           "bridge": bridge}}, timeout=300)
        if not answer.get("ok"):
            raise ServiceError(answer.get("error") or "Converting %s failed." % nic, 502)
        return {"bridge": bridge, "nic": nic, "applied": answer.get("applied") or [],
                "notes": self.sync_lan_firewall()}

    def revert_lan_bridge(self, bridge):
        """Give a converted bridge's NIC back its own connection, if nothing is on it."""
        if bridge not in hostnet.nm_lan_bridges():
            raise ServiceError("'%s' is not a bridge lemondx converted a NIC into." % bridge,
                               404)
        users = self._bridge_users(bridge)
        if users:
            raise ServiceError("%s is still used by %s. Move them off it first: "
                               "reverting takes the bridge away." % (bridge, ", ".join(users)),
                               409)
        if not hostnet.available():
            raise ServiceError("lemondx cannot run its helper as root here, so it "
                               "cannot revert %s." % bridge, 503)
        answer = hostnet.delegate({"lan": {"action": "revert", "bridge": bridge}},
                                  timeout=300)
        if not answer.get("ok"):
            raise ServiceError(answer.get("error") or "Reverting %s failed." % bridge, 502)
        return {"reverted": bridge, "applied": answer.get("applied") or [],
                "notes": self.sync_lan_firewall()}

    def _bridge_users(self, bridge):
        """Instances and profiles with a NIC on ``bridge``, managed or not."""
        def on_it(devices):
            return any(d.get("type") == "nic" and bridge in (d.get("parent"), d.get("network"))
                       for d in (devices or {}).values())
        users = ["instance %s" % i.get("name") for i in self.lxd.list_instances()
                 if on_it(i.get("expanded_devices") or i.get("devices"))]
        users += ["profile %s" % p.get("name") for p in self.lxd.list_profiles()
                  if on_it(p.get("devices"))]
        return users

    def sync_lan_firewall(self):
        """Keep Docker's FORWARD drop from swallowing LAN bridges; returns notes.

        Docker loads br_netfilter, which puts bridged frames through iptables,
        where its DROP policy ends them: a LAN bridge would carry nothing,
        silently. macvlan never passes a bridge and needs no exception.
        Best-effort, like the fabric's: a host without Docker gets nothing.
        """
        if not hostnet.docker_present():
            return []
        bridges = sorted(set(self.lan_bridges()))
        if not hostnet.available():
            if not bridges:
                return []
            return ["Docker is installed here, and its FORWARD drop blocks bridged "
                    "traffic. lemondx cannot run its helper as root, so allow it "
                    "yourself: %s" % "; ".join(
                        "sudo iptables -I DOCKER-USER -%s %s -j ACCEPT" % (way, bridge)
                        for bridge in bridges for way in "io")]
        try:
            answer = hostnet.delegate({"lan": {"action": "docker", "bridges": bridges}})
        except hostnet.HostNetError as exc:
            return ["Could not let the LAN bridges through Docker's firewall: %s"
                    % exc.message]
        if not answer.get("ok"):
            return ["Could not let the LAN bridges through Docker's firewall: %s"
                    % (answer.get("error") or "the helper failed")]
        return []

    def lan_bridges(self):
        """Every bridge here with a physical port that lemondx made or knows of."""
        bridges = [n.get("name") for n in self.lxd.list_networks()
                   if (_lan_kind(n) or {}).get("kind") == "bridge"]
        return bridges + list(hostnet.nm_lan_bridges())

    def _lan_info(self, network):
        """How a network reaches the LAN, or None: see `_lan_kind()`."""
        kind = _lan_kind(network)
        if kind or network.get("managed") or network.get("type") != "bridge":
            return kind
        ports = hostnet.lan_bridge_ports(network.get("name") or "")
        if not ports:
            return None
        converted = network.get("name") in hostnet.nm_lan_bridges()
        return {"kind": "converted" if converted else "host", "nic": ports[0]}

    def _clustered(self):
        environment = (self.lxd.server_info() or {}).get("environment") or {}
        return bool(environment.get("server_clustered"))

    def _network_mutation_context(self):
        clustered = self._clustered()
        if clustered:
            raise ServiceError("Network changes are not supported on clustered servers.", 409)
        return clustered

    def subnets(self):
        """Every subnet on the host the daemon can see, so a block can be picked around them."""
        return [{"interface": other, "subnet": str(subnet), "family": subnet.version}
                for other, subnet in self._subnets_in_use()]

    def _check_subnets_free(self, name, config):
        """Refuse a chosen subnet that overlaps one already on the host.

        The daemon checks this for ``auto`` but not for an explicit block, and
        a bridge on a subnet the host already routes elsewhere (the LAN,
        docker0, another bridge) quietly breaks traffic to both.
        """
        wanted = []
        for key in ("ipv4.address", "ipv6.address"):
            value = config.get(key) or ""
            if value not in ("", "auto", "none"):
                wanted.append(ipaddress.ip_interface(value).network)
        if not wanted:
            return
        for other, subnet in self._subnets_in_use(exclude=name):
            for block in wanted:
                if block.version == subnet.version and block.overlaps(subnet):
                    raise ServiceError(
                        "%s overlaps %s on %s, which is already on this host. Pick "
                        "another block, or let the daemon pick a free one."
                        % (block, subnet, other), 409)

    def _subnets_in_use(self, exclude=None):
        """(interface, subnet) for every address the daemon can see on the host."""
        found = set()
        for record in self.lxd.list_networks():
            other = record.get("name")
            if other == exclude:
                continue
            config = record.get("config") or {}
            addresses = [config.get("ipv4.address"), config.get("ipv6.address")]
            for entry in (self.lxd.get_network_state(other) or {}).get("addresses") or []:
                if entry.get("scope") == "global" and entry.get("netmask"):
                    addresses.append("%s/%s" % (entry.get("address"), entry["netmask"]))
            for address in addresses:
                if not address or address in ("auto", "none"):
                    continue
                try:
                    found.add((other, ipaddress.ip_interface(address).network))
                except ValueError:
                    continue
        return sorted(found, key=lambda pair: (pair[0], str(pair[1])))

    def _network_config(self, config):
        values = self._storage_config(config, BRIDGE_CONFIG, what="Network")
        for key, value in values.items():
            if value == "":
                continue
            if key in _BRIDGE_BOOLEANS:
                if value.lower() not in ("true", "false"):
                    raise ServiceError("'%s' must be true or false." % key)
                values[key] = value.lower()
            elif key in ("ipv4.address", "ipv6.address"):
                values[key] = _bridge_address(key, value)
            elif key == "bridge.mtu":
                if not value.isdigit() or not 576 <= int(value) <= 65535:
                    raise ServiceError("MTU must be a number between 576 and 65535.")
            elif key == "dns.mode":
                if value not in ("managed", "dynamic", "none"):
                    raise ServiceError("dns.mode must be managed, dynamic or none.")
            elif key == "dns.domain":
                if not _DNS_DOMAIN.match(value) or len(value) > 253:
                    raise ServiceError("'%s' is not a valid DNS domain." % value)
        return values

    def _profile_nic(self, profiles):
        """The (device key, device) of the NIC the profiles give an instance.

        Later profiles override earlier ones key by key, as the daemon applies
        them. With several NICs, the one named eth0 is the primary.
        """
        devices = self._profile_devices(profiles)
        nics = sorted((k, d) for k, d in devices.items() if d.get("type") == "nic")
        if not nics:
            return None, None
        return next(((k, d) for k, d in nics if d.get("name") == "eth0"), nics[0])

    def _profile_devices(self, profiles):
        """The devices the profiles give an instance, later profiles winning."""
        devices = {}
        for profile_name in profiles or ["default"]:
            try:
                profile = self.lxd.get_profile(profile_name)
            except LXDError:
                continue
            devices.update(profile.get("devices") or {})
        return devices

    def fabric_nic(self, network, name=FABRIC_NIC):
        """A NIC that *adds* to the profiles' one instead of replacing it.

        The opposite of `_instance_nic()`: a new device key, so the profile's
        NAT'd eth0 stays exactly as it was and keeps the instance's internet
        access. The fabric is only for reaching other nodes.
        """
        record = next((n for n in self.lxd.list_networks()
                       if n.get("name") == network), None)
        if record is None:
            raise ServiceError("No network called '%s'." % network, 404)
        return name, {"type": "nic", "network": network, "name": name}

    def _instance_nic(self, profiles, network):
        """An instance-level NIC that replaces the profiles' one with ``network``.

        Using the profile's device key is what makes it a replacement: a new
        key would add a second NIC fighting the first for the same name.
        """
        record = next((n for n in self.lxd.list_networks()
                       if n.get("name") == network), None)
        if record is None:
            raise ServiceError("No network called '%s'." % network, 404)
        if not _attachable(record):
            raise ServiceError(
                "'%s' is an unmanaged %s interface. Instances can join a managed "
                "network or a host bridge." % (network, record.get("type") or "unknown"))
        if record.get("managed"):
            device = {"type": "nic", "network": network}
        else:
            device = {"type": "nic", "nictype": "bridged", "parent": network}
        key, current = self._profile_nic(profiles)
        device["name"] = (current or {}).get("name") or "eth0"
        return key or "eth0", device

    def get_network(self, name):
        """Everything about one network: config, live state, and who is on it."""
        network = self.lxd.get_network(name)
        config = network.get("config") or {}
        managed = bool(network.get("managed"))
        reason = _network_read_only_reason(network, self._clustered())
        _, nic = self._profile_nic(["default"])

        state = self.lxd.get_network_state(name) or {}
        counters = state.get("counters") or {}

        leases = []
        if managed:
            for lease in self.lxd.network_leases(name):
                leases.append({
                    "hostname": lease.get("hostname") or "",
                    "address": lease.get("address") or "",
                    "hwaddr": lease.get("hwaddr") or "",
                    "type": lease.get("type") or "",
                })
            leases.sort(key=lambda l: (l["type"] != "gateway", _address_key(l["address"])))

        instances, profiles = _network_users(network)

        return {
            "name": network.get("name"),
            "type": network.get("type"),
            "managed": managed,
            "status": network.get("status"),
            "description": network.get("description") or "",
            "default": network.get("name") == _nic_network(nic),
            "attachable": _attachable(network),
            "manageable": not reason,
            "read_only_reason": reason,
            "config": config,
            "ipv4": {
                "address": config.get("ipv4.address") or "",
                "nat": _is_true(config.get("ipv4.nat")),
                "dhcp": config.get("ipv4.dhcp", "true") != "false",
                "dhcp_ranges": config.get("ipv4.dhcp.ranges") or "",
            },
            "ipv6": {
                "address": config.get("ipv6.address") or "",
                "nat": _is_true(config.get("ipv6.nat")),
                "dhcp": config.get("ipv6.dhcp", "true") != "false",
                "dhcp_ranges": config.get("ipv6.dhcp.ranges") or "",
            },
            "dns_domain": config.get("dns.domain") or ("lxd" if managed else ""),
            "dns_mode": config.get("dns.mode") or "managed",
            "lan": self._lan_info(network),
            "mtu": state.get("mtu") or config.get("bridge.mtu") or "",
            "hwaddr": state.get("hwaddr") or "",
            "state": state.get("state") or "",
            "addresses": [
                "%s/%s" % (a.get("address"), a.get("netmask")) if a.get("netmask")
                else a.get("address")
                for a in (state.get("addresses") or [])
                if a.get("scope") == "global"
            ],
            "counters": {
                "rx": counters.get("bytes_received") or 0,
                "tx": counters.get("bytes_sent") or 0,
            },
            "leases": leases,
            "instances": instances,
            "profiles": profiles,
            "forwards": self.lxd.network_forwards(name) if managed else [],
        }

    def list_profiles(self):
        return [
            {
                "name": p.get("name"),
                "description": p.get("description"),
                "devices": sorted((p.get("devices") or {}).keys()),
                "used_by": len(p.get("used_by") or []),
            }
            for p in self.lxd.list_profiles()
        ]

    # -- storage ----------------------------------------------------------

    def storage(self):
        """Storage pools and volumes, with local-driver management flags."""
        environment = (self.lxd.server_info() or {}).get("environment") or {}
        available = self._available_storage_drivers(environment)
        clustered = bool(environment.get("server_clustered"))
        root_pool = self._root_pool(["default"])
        pools = []
        volumes = []

        for record in self.lxd.list_storage_pools():
            pool = self._storage_pool_summary(record, root_pool, available, clustered)
            pool_volumes = [
                self._storage_volume_summary(pool["name"], volume, pool["manageable"])
                for volume in self.lxd.list_storage_volumes(pool["name"])
            ]
            pool["volume_count"] = len(pool_volumes)
            pool["delete_plan"] = self._storage_pool_delete_plan(pool, pool_volumes)
            pools.append(pool)
            volumes.extend(pool_volumes)

        pools.sort(key=lambda pool: pool["name"] or "")
        volumes.sort(key=lambda volume: (volume["pool"] or "", volume["name"] or ""))
        return {
            "clustered": clustered,
            "local_drivers": [
                {
                    "name": name,
                    "available": name in available,
                    "supports_quota": name in QUOTA_CAPABLE_DRIVERS,
                    "supports_custom_block": True,
                }
                for name in sorted(LOCAL_STORAGE_DRIVERS)
            ],
            "pools": pools,
            "volumes": volumes,
        }

    def get_storage_pool(self, name):
        overview = self.storage()
        pool = next((pool for pool in overview["pools"] if pool["name"] == name), None)
        if pool is None:
            raise ServiceError("No storage pool called '%s'." % name, 404)
        pool["volumes"] = [
            volume for volume in overview["volumes"] if volume["pool"] == name
        ]
        return pool

    def create_storage_pool(self, name, driver, source=None, size=None,
                            description="", config=None):
        self._check_storage_name(name, "pool")
        driver = str(driver or "").strip().lower()
        available = self._storage_mutation_context()
        if driver not in LOCAL_STORAGE_DRIVERS:
            raise ServiceError(
                "Storage driver '%s' is not managed by lemondx. Use one of: %s."
                % (driver, ", ".join(sorted(LOCAL_STORAGE_DRIVERS)))
            )
        if driver not in available:
            raise ServiceError(
                "Storage driver '%s' is not available on this host. Available local "
                "drivers: %s." % (
                    driver, ", ".join(sorted(available & LOCAL_STORAGE_DRIVERS)) or "none")
            )
        values = self._storage_config(config, LOCAL_POOL_CONFIG[driver])
        if "volume.size" in values:
            values["volume.size"] = normalize_size(values["volume.size"], "default volume size")
        if source:
            values["source"] = str(source).strip()
        if size:
            if driver == "dir":
                raise ServiceError("The dir driver does not accept a pool size.")
            if source:
                raise ServiceError(
                    "Pool size is only supported for daemon-managed loop-backed storage.")
            values["size"] = normalize_size(size, "pool size")
        self.lxd.create_storage_pool(name, driver, values)
        if description:
            created = self.lxd.get_storage_pool(name)
            self.lxd.update_storage_pool(name, description, created.get("config") or {})
        return self.get_storage_pool(name)

    def update_storage_pool(self, name, description=None, size=None, config=None):
        available = self._storage_mutation_context()
        current = self.lxd.get_storage_pool(name)
        driver = self._require_local_pool(current, available)
        values = dict(current.get("config") or {})
        updates = self._storage_config(config, LOCAL_POOL_CONFIG[driver])
        if "volume.size" in updates:
            updates["volume.size"] = normalize_size(
                updates["volume.size"], "default volume size")
        values.update(updates)
        if size is not None:
            if driver == "dir":
                raise ServiceError("The dir driver does not accept a pool size.")
            current_size = (current.get("config") or {}).get("size")
            if not current_size:
                raise ServiceError(
                    "Only loop-backed storage pools can be resized by lemondx.")
            normalized = normalize_size(size, "pool size")
            new_bytes = parse_byte_size(normalized)
            current_bytes = parse_byte_size(current_size)
            if new_bytes is None:
                raise ServiceError("Pool size cannot be empty.")
            if current_bytes is None:
                raise ServiceError("The pool's current size cannot be determined safely.")
            if new_bytes < current_bytes:
                raise ServiceError("Storage pools can grow but cannot be shrunk.")
            values["size"] = normalized
        current_description = current.get("description") or ""
        self.lxd.update_storage_pool(
            name, current_description if description is None else description, values)
        return self.get_storage_pool(name)

    def delete_storage_pool(self, name, force=False, confirmation=None,
                            expected_plan=None):
        available = self._storage_mutation_context()
        current = self.lxd.get_storage_pool(name)
        self._require_local_pool(current, available)
        volumes = self.lxd.list_storage_volumes(name)
        used_by = current.get("used_by") or []
        plan = self._storage_pool_delete_plan(current, volumes)
        if force:
            if confirmation != name:
                raise ServiceError("Type the pool name exactly to confirm force deletion.")
            if expected_plan != plan:
                raise ServiceError(
                    "The resources in storage pool '%s' changed. Review them and confirm again."
                    % name, 409)
            if plan["other_references"] or plan["other_volumes"]:
                raise ServiceError(
                    "Storage pool '%s' has resources lemondx cannot safely remove: %s."
                    % (name, ", ".join(
                        plan["other_references"] + plan["other_volumes"])), 409)
        if used_by or volumes:
            if not force:
                raise ServiceError(
                    "Storage pool '%s' is still in use (%d references, %d volumes)."
                    % (name, len(used_by), len(volumes)), 409)

            for instance in plan["instances"]:
                self.delete_container(instance, force=True)
            for instance in plan["attached_instances"]:
                self.delete_container(instance, force=True)
            for profile_name in plan["profiles"]:
                profile = self.lxd.get_profile(profile_name)
                devices = {
                    key: value for key, value in (profile.get("devices") or {}).items()
                    if value.get("pool") != name
                }
                self.lxd.update_profile(profile_name, {
                    "description": profile.get("description") or "",
                    "config": profile.get("config") or {},
                    "devices": devices,
                })
            for fingerprint in plan["images"]:
                self.lxd.delete_image(fingerprint)
            for volume_name in plan["custom_volumes"]:
                self.lxd.delete_storage_volume(name, volume_name)
        detached = current.get("driver") == "zfs"
        if detached:
            config = current.get("config") or {}
            source = config.get("zfs.pool_name") or config.get("source") or name
            zpool = source.split("/", 1)[0]
            if not zpool or zpool in (".", "..") or not os.path.isdir(ZFS_KSTAT_ROOT):
                raise ServiceError(
                    "Cannot verify whether ZFS pool '%s' is imported. Export it on the "
                    "host, then retry." % zpool, 409)
            if os.path.isdir(os.path.join(ZFS_KSTAT_ROOT, zpool)):
                raise ServiceError(
                    "LXD resources were removed. To preserve ZFS pool '%s', run "
                    "'sudo zpool export %s' on the host, then retry this deletion to "
                    "remove only the LXD registration." % (zpool, zpool), 409)

        self.lxd.delete_storage_pool(name)
        return {
            "deleted": name,
            "detached": detached,
            "cascade": plan if force else None,
        }

    def _storage_pool_delete_plan(self, pool, volumes):
        instances = set()
        attached_instances = set()
        images = set()
        profiles = set()
        custom_volumes = set()
        other_references = set()
        other_volumes = set()

        def classify_reference(reference, attached=False):
            parsed = urllib.parse.urlsplit(str(reference or ""))
            query = urllib.parse.parse_qs(parsed.query)
            active_project = getattr(self.lxd, "project", "default") or "default"
            reference_project = (query.get("project") or [active_project])[0]
            if reference_project != active_project:
                other_references.add(str(reference))
                return
            path = parsed.path.rstrip("/")
            parts = path.split("/")
            if len(parts) >= 4 and parts[1] == "1.0":
                kind, value = parts[2], urllib.parse.unquote(parts[3])
                if kind == "instances":
                    (attached_instances if attached else instances).add(value)
                    return
                if kind == "images":
                    images.add(value)
                    return
                if kind == "profiles":
                    profiles.add(value)
                    return
            if reference:
                other_references.add(str(reference))

        for reference in pool.get("used_by") or []:
            classify_reference(reference)
        for volume in volumes:
            volume_type = volume.get("type") or ""
            volume_name = volume.get("name") or ""
            if volume_type in ("container", "virtual-machine"):
                instances.add(volume_name.split("/", 1)[0])
            elif volume_type == "image":
                images.add(volume_name)
            elif volume_type == "custom":
                custom_volumes.add(volume_name)
                for reference in volume.get("used_by") or []:
                    classify_reference(reference, attached=True)
            else:
                other_volumes.add("%s/%s" % (volume_type or "unknown", volume_name))
        return {
            "instances": sorted(instances),
            "attached_instances": sorted(attached_instances - instances),
            "images": sorted(images),
            "custom_volumes": sorted(custom_volumes),
            "profiles": sorted(profiles),
            "other_references": sorted(other_references),
            "other_volumes": sorted(other_volumes),
        }

    def get_storage_volume(self, pool, name):
        pool_record = self.lxd.get_storage_pool(pool)
        environment = (self.lxd.server_info() or {}).get("environment") or {}
        available = self._available_storage_drivers(environment)
        manageable = (
            pool_record.get("driver") in LOCAL_STORAGE_DRIVERS
            and pool_record.get("driver") in available
            and not environment.get("server_clustered")
        )
        try:
            volume = self.lxd.get_storage_volume(pool, "custom", name)
        except LXDError as exc:
            if exc.code == 404:
                raise ServiceError(
                    "No custom volume '%s' in pool '%s'." % (name, pool), 404) from exc
            raise
        return self._storage_volume_summary(pool, volume, manageable)

    def create_storage_volume(self, pool, name, content_type="filesystem", size=None,
                              description="", config=None):
        self._check_storage_name(name, "volume")
        self._require_local_pool(
            self.lxd.get_storage_pool(pool), self._storage_mutation_context())
        if content_type not in ("filesystem", "block"):
            raise ServiceError("Content type must be 'filesystem' or 'block'.")
        values = self._storage_config(config, LOCAL_VOLUME_CONFIG)
        if size:
            values["size"] = normalize_size(size, "volume size")
        self.lxd.create_storage_volume(
            pool, name, content_type=content_type, config=values,
            description=description)
        return self.get_storage_volume(pool, name)

    def update_storage_volume(self, pool, name, description=None, size=None, config=None):
        self._require_local_pool(
            self.lxd.get_storage_pool(pool), self._storage_mutation_context())
        current = self.lxd.get_storage_volume(pool, "custom", name)
        values = dict(current.get("config") or {})
        values.update(self._storage_config(config, LOCAL_VOLUME_CONFIG))
        if size is not None:
            normalized = normalize_size(size, "volume size")
            current_size = (current.get("config") or {}).get("size")
            if current.get("content_type") == "block" and current_size:
                new_bytes = parse_byte_size(normalized)
                current_bytes = parse_byte_size(current_size)
                if new_bytes is None:
                    raise ServiceError("Volume size cannot be empty.")
                if current_bytes is None:
                    raise ServiceError(
                        "The volume's current size cannot be determined safely.")
                if new_bytes < current_bytes:
                    raise ServiceError("Block volumes can grow but cannot be shrunk.")
            values["size"] = normalized
        current_description = current.get("description") or ""
        self.lxd.update_storage_volume(
            pool, name, current_description if description is None else description, values)
        return self.get_storage_volume(pool, name)

    def delete_storage_volume(self, pool, name):
        self._require_local_pool(
            self.lxd.get_storage_pool(pool), self._storage_mutation_context())
        current = self.lxd.get_storage_volume(pool, "custom", name)
        if current.get("used_by"):
            raise ServiceError(
                "Storage volume '%s' is attached and cannot be deleted." % name, 409)
        self.lxd.delete_storage_volume(pool, name)
        return {"deleted": name, "pool": pool}

    def _storage_pool_summary(self, record, root_pool, available, clustered):
        name = record.get("name")
        driver = record.get("driver") or ""
        space = (self.lxd.storage_pool_resources(name) or {}).get("space") or {}
        manageable = driver in LOCAL_STORAGE_DRIVERS and driver in available and not clustered
        config = _safe_storage_config(record.get("config") or {})
        if driver not in LOCAL_STORAGE_DRIVERS:
            config.pop("source", None)
        return {
            "name": name,
            "driver": driver,
            "description": record.get("description") or "",
            "config": config,
            "source": config.get("source") or "",
            "used_by": list(record.get("used_by") or []),
            "used_by_count": len(record.get("used_by") or []),
            "total": space.get("total") or 0,
            "used": space.get("used") or 0,
            "root": name == root_pool,
            "supports_quota": driver in QUOTA_CAPABLE_DRIVERS,
            "manageable": manageable,
            "read_only_reason": "" if manageable else (
                "Clustered storage is read-only in lemondx." if clustered
                else "Only dir, btrfs, lvm and zfs pools are managed by lemondx."
            ),
        }

    def _storage_volume_summary(self, pool, record, pool_manageable):
        volume_type = record.get("type") or ""
        return {
            "pool": pool,
            "name": record.get("name"),
            "type": volume_type,
            "content_type": record.get("content_type") or "filesystem",
            "description": record.get("description") or "",
            "config": _safe_storage_config(record.get("config") or {}),
            "size": (record.get("config") or {}).get("size") or "",
            "used_by": list(record.get("used_by") or []),
            "manageable": pool_manageable and volume_type == "custom",
        }

    def _available_storage_drivers(self, environment=None):
        if environment is None:
            environment = (self.lxd.server_info() or {}).get("environment") or {}
        return {
            driver.get("Name") for driver in environment.get("storage_supported_drivers") or []
            if driver.get("Name")
        }

    def _storage_mutation_context(self):
        environment = (self.lxd.server_info() or {}).get("environment") or {}
        if environment.get("server_clustered"):
            raise ServiceError("Storage changes are not supported on clustered servers.", 409)
        return self._available_storage_drivers(environment)

    def _require_local_pool(self, record, available):
        driver = record.get("driver") or ""
        if driver not in LOCAL_STORAGE_DRIVERS:
            raise ServiceError(
                "Pool '%s' uses the unsupported '%s' driver and is read-only."
                % (record.get("name") or "", driver), 409)
        if driver not in available:
            raise ServiceError(
                "Pool '%s' uses '%s', which is not available on this host."
                % (record.get("name") or "", driver), 409)
        return driver

    def _storage_config(self, config, allowed, what="Storage"):
        if config is None:
            return {}
        if not isinstance(config, dict):
            raise ServiceError("%s config must be an object of key/value strings." % what)
        result = {}
        for key, value in config.items():
            if key not in allowed:
                raise ServiceError("%s config key '%s' is not supported." % (what, key))
            if not isinstance(value, (str, int, float, bool)):
                raise ServiceError("%s config value for '%s' must be a string." % (what, key))
            result[key] = str(value).lower() if isinstance(value, bool) else str(value)
        return result

    def _check_storage_name(self, name, what):
        if not VALID_NAME.match(name or ""):
            raise ServiceError(
                "Invalid %s name '%s'. Use letters, digits and dashes, starting "
                "with a letter (max 62 chars)." % (what, name)
            )

    # -- resources ---------------------------------------------------------

    def resources(self):
        """What the host has, and how much of it instances have claimed.

        "Allocated" is the sum of limits, not a measurement: an instance with
        no limit can use everything and is listed by name instead of being
        guessed at. CPU and memory only count for instances that are running
        or frozen, with what stopped ones would add reported separately; disk
        counts for every instance, since a stopped one still occupies its pool.
        Allocations are for the current project, host figures for the host.
        """
        host = self.lxd.resources() or {}
        cpu_info = host.get("cpu") or {}
        memory_info = host.get("memory") or {}
        sockets = cpu_info.get("sockets") or []
        threads = cpu_info.get("total") or 0
        memory_total = memory_info.get("total") or 0

        pools = self.lxd.list_storage_pools()
        pool_config = {p.get("name"): p.get("config") or {} for p in pools}

        instances = [_instance_allocation(i, memory_total, pool_config)
                     for i in self.lxd.list_instances()]
        instances.sort(key=lambda i: i["name"] or "")
        active = [i for i in instances if i["active"]]
        stopped = [i for i in instances if not i["active"]]

        def claimed(rows, kind, unit):
            return sum(r[kind][unit] for r in rows if r[kind][unit] is not None)

        storage = []
        for pool in pools:
            name = pool.get("name")
            space = (self.lxd.storage_pool_resources(name) or {}).get("space") or {}
            on_pool = [i for i in instances if i["disk"]["pool"] == name]
            storage.append({
                "name": name,
                "driver": pool.get("driver"),
                "supports_quota": pool.get("driver") in QUOTA_CAPABLE_DRIVERS,
                "total": space.get("total") or 0,
                "used": space.get("used") or 0,
                "allocated": claimed(on_pool, "disk", "bytes"),
                "unlimited": [i["name"] for i in on_pool if i["disk"]["bytes"] is None],
            })

        return {
            "host": {
                "architecture": cpu_info.get("architecture") or "",
                "cpu_model": (sockets[0].get("name") or "") if sockets else "",
                "cpu_sockets": len(sockets),
                "cpu_cores": sum(len(s.get("cores") or []) for s in sockets),
                "cpu_threads": threads,
                "memory_total": memory_total,
                "memory_used": memory_info.get("used") or 0,
            },
            "cpu": {
                "total": threads,
                "allocated": claimed(active, "cpu", "count"),
                "stopped": claimed(stopped, "cpu", "count"),
                "unlimited": [i["name"] for i in active if i["cpu"]["count"] is None],
            },
            "memory": {
                "total": memory_total,
                "used": memory_info.get("used") or 0,
                "instances_used": sum(i["memory"]["usage"] for i in active),
                "allocated": claimed(active, "memory", "bytes"),
                "stopped": claimed(stopped, "memory", "bytes"),
                "unlimited": [i["name"] for i in active if i["memory"]["bytes"] is None],
            },
            "storage": storage,
            "instances": instances,
        }


# -- normalisation helpers -------------------------------------------------


def _safe_storage_config(config):
    """Return storage config without values that could contain credentials."""
    sensitive = ("password", "token", "secret", "private", "credential", "api_key")
    return {
        key: value for key, value in config.items()
        if not any(part in key.lower() for part in sensitive)
    }


def _is_true(value):
    return str(value).lower() in ("true", "yes", "1", "on")


def _note_health_changes(previous, records):
    """Log an instance whose health changed: transitions only, never each round,
    which would be one line per instance every interval."""
    for record in records:
        before = (previous.get(record["name"]) or {}).get("status")
        if before is None or before == record["status"]:
            continue
        good = record["status"] in (health_checks.HEALTHY, health_checks.STARTING)
        eventlog.event("system", "instance.health",
                       level=eventlog.NOTICE if good else logging.WARNING,
                       instance=record["name"], was=before, now=record["status"],
                       reasons="; ".join(record.get("reasons") or []))


def _log(message, level=None):
    """Say something worth keeping, through eventlog (never stdout: a launch
    runs from the CLI too, where stdout may be a --json payload)."""
    if level is None:
        level = logging.WARNING if "fail" in message.lower() else logging.INFO
    eventlog.message(message, level=level)


# A failed module keeps this much of its output in a run record, for the UI.
RUN_OUTPUT_TAIL = 4 * 1024


def _run_record(result):
    """A run's result as kept for polling: what the Templates card shows.

    Every open page re-fetches run records every few seconds for as long as
    the server runs, and a full launch result carries each instance's detail
    and every module's output -- megabytes for twenty instances installing
    packages. The caller that waited for the run still gets all of it.
    """
    if not result:
        return result
    instances = []
    for entry in result["instances"]:
        container = entry.get("container")
        if container is not None:
            bootstrap = container.get("bootstrap")
            container = {
                "name": container.get("name"),
                "status": container.get("status"),
                "ipv4": container.get("ipv4") or [],
                "bootstrap": bootstrap and {
                    "container": bootstrap.get("container"),
                    "ok": bootstrap["ok"],
                    "modules": [dict(
                        {k: m.get(k) for k in ("id", "name", "exit_code", "duration")},
                        stdout=(m.get("stdout") or "")[-RUN_OUTPUT_TAIL:] if m["exit_code"] else "",
                        stderr=(m.get("stderr") or "")[-RUN_OUTPUT_TAIL:] if m["exit_code"] else "",
                    ) for m in bootstrap["modules"]],
                },
            }
        instances.append(dict(entry, container=container))
    return dict(result, instances=instances)


def _network_read_only_reason(network, clustered):
    """Why lemondx will not change a network, or "" if it will."""
    if clustered:
        return "clustered networks are shown read-only."
    if not network.get("managed"):
        return "not managed by the daemon."
    if network.get("type") not in ("bridge", "macvlan"):
        return "only bridge and macvlan networks are managed here."
    return ""


def _lan_kind(network):
    """{'kind', 'nic'} for a daemon network on the LAN, from its own config.

    ``bridge`` is one over a spare NIC, ``macvlan`` one on a NIC's side. The
    host bridges -- a converted NIC, or one made by hand -- need /sys to tell,
    so `ContainerService._lan_info()` adds those.
    """
    config = network.get("config") or {}
    if network.get("managed") and network.get("type") == "macvlan":
        return {"kind": "macvlan", "nic": config.get("parent") or ""}
    if network.get("managed") and config.get("bridge.external_interfaces"):
        return {"kind": "bridge", "nic": config["bridge.external_interfaces"]}
    return None


def _attachable(network):
    # An unmanaged physical NIC would be moved into the instance, off the host.
    return bool(network.get("managed")) or network.get("type") == "bridge"


def _nic_network(device):
    """The network a NIC device joins, managed (`network`) or not (`parent`)."""
    if not device:
        return None
    return device.get("network") or device.get("parent")


def _network_users(network):
    """(instances, profiles) named by a network's used_by API paths."""
    instances, profiles = set(), set()
    for reference in network.get("used_by") or []:
        parts = urllib.parse.urlsplit(str(reference)).path.rstrip("/").split("/")
        if len(parts) >= 4 and parts[1] == "1.0":
            value = urllib.parse.unquote(parts[3])
            if parts[2] == "instances":
                instances.add(value)
            elif parts[2] == "profiles":
                profiles.add(value)
    return sorted(instances), sorted(profiles)


def _bridge_address(key, value):
    """``auto``, ``none``, or the bridge's own address with its prefix.

    A CIDR block is accepted too, and means its first host: people pick a
    block, while the daemon wants the gateway address it should hold.
    """
    if value in ("auto", "none"):
        return value
    family = 4 if key.startswith("ipv4") else 6
    example = "10.10.0.0/24" if family == 4 else "fd42:1::/64"
    if "/" not in value:
        raise ServiceError(
            "%s needs a CIDR block with a prefix length, e.g. %s (or 'auto' / 'none')."
            % (key, example))
    try:
        interface = ipaddress.ip_interface(value)
    except ValueError:
        raise ServiceError(
            "'%s' is not a valid %s, e.g. %s." % (value, key, example)) from None
    if interface.version != family:
        raise ServiceError("'%s' is not an IPv%d address, e.g. %s." % (value, family, example))
    network = interface.network
    if network.num_addresses < 4:
        raise ServiceError("%s is too small a subnet to hand out addresses." % network)
    if interface.ip == network.network_address:
        return "%s/%d" % (next(network.hosts()).compressed, network.prefixlen)
    if family == 4 and interface.ip == network.broadcast_address:
        raise ServiceError(
            "%s is the broadcast address of %s; the bridge needs a host address in it."
            % (interface.ip, network))
    return interface.with_prefixlen


def _address_key(address):
    """Sort IPv4 numerically rather than as text."""
    parts = str(address).split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return (0, [int(p) for p in parts])
    return (1, [str(address)])


def parse_byte_size(value, total=None):
    """Bytes in a size the daemon wrote, or None if it is unset or unreadable.

    ``total`` resolves a percentage, which ``limits.memory`` allows.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("%"):
        try:
            percent = float(text[:-1])
        except ValueError:
            return None
        return int(total * percent / 100) if total else None
    match = _BYTE_SIZE.match(text)
    if not match or match.group(2).lower() not in _BYTE_UNITS:
        return None
    return int(float(match.group(1)) * _BYTE_UNITS[match.group(2).lower()])


def cpu_count(value):
    """How many CPUs ``limits.cpu`` grants, or None if it is unset or unreadable.

    The key is overloaded: a bare number is a count, while a range or list
    ("0-3", "1,5", even "2-2") pins specific CPUs and grants that many.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    pinned = set()
    for part in text.split(","):
        low, _, high = part.strip().partition("-")
        if not low.isdigit() or (high and not high.isdigit()):
            return None
        pinned.update(range(int(low), int(high or low) + 1))
    return len(pinned) or None


def _instance_allocation(instance, memory_total, pool_config):
    """One instance's claim on CPU, memory and disk, with where it came from.

    ``implicit`` marks a value the daemon supplied rather than the config, so
    a front end can tell a chosen limit from a default.
    """
    config = instance.get("expanded_config") or instance.get("config") or {}
    devices = instance.get("expanded_devices") or instance.get("devices") or {}
    state = instance.get("state") or {}
    status = instance.get("status") or state.get("status") or "Unknown"
    is_vm = instance.get("type") == "virtual-machine"

    cpu_limit = str(config.get("limits.cpu") or "").strip()
    cpu_implicit = is_vm and not cpu_limit
    if cpu_implicit:
        cpu_limit = str(VM_DEFAULTS["cpu"])

    memory_limit = str(config.get("limits.memory") or "").strip()
    memory_implicit = is_vm and not memory_limit
    if memory_implicit:
        memory_limit = VM_DEFAULTS["memory"]

    root_name, root = next(
        ((n, d) for n, d in sorted(devices.items())
         if d.get("type") == "disk" and d.get("path") == "/"),
        (None, {}))
    pool = root.get("pool")
    disk_size = str(root.get("size") or "").strip()
    disk_implicit = False
    if not disk_size:
        # A pool's volume.size applies to every volume created without one.
        disk_size = str((pool_config.get(pool) or {}).get("volume.size") or "").strip()
        if not disk_size and is_vm:
            disk_size = VM_DEFAULTS["disk"]
        disk_implicit = bool(disk_size)
    disk_state = (state.get("disk") or {}).get(root_name) or {}

    return {
        "name": instance.get("name"),
        "type": instance.get("type") or "container",
        "status": status,
        "active": status in ACTIVE_STATUSES,
        "cpu_time_ns": (state.get("cpu") or {}).get("usage") or 0,
        "cpu": {
            "limit": cpu_limit,
            "count": cpu_count(cpu_limit),
            "implicit": cpu_implicit,
        },
        "memory": {
            "limit": memory_limit,
            "bytes": parse_byte_size(memory_limit, memory_total),
            "usage": (state.get("memory") or {}).get("usage") or 0,
            "implicit": memory_implicit,
        },
        "disk": {
            "pool": pool,
            "size": disk_size,
            "bytes": parse_byte_size(disk_size),
            "usage": disk_state.get("usage") or 0,
            "implicit": disk_implicit,
        },
    }


def _default_remote(remotes):
    """The remote a bare alias means: `ubuntu:` where it exists (LXD), else the
    daemon's general-purpose image server."""
    return "ubuntu" if "ubuntu" in remotes else "images"


def pin_alias(name):
    """The local alias a pinned build is kept under: pin-<its id>."""
    return "pin-" + name


def pin_id(remote, alias, serial):
    """A pin's id: the image and the build, e.g. images-debian-12-20261001-0524."""
    text = "%s-%s-%s" % (remote, alias, serial)
    slug = re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-.")
    return slug[:80].rstrip("-.")


def pin_users(pin, templates=None):
    """The templates that launch ``pin``, by its id or any of its nicknames."""
    refs = {"%s:%s" % (PIN_REMOTE, n) for n in [pin["name"]] + pin["nicknames"]}
    return sorted(t["name"] for t in (templates if templates is not None
                                      else store.load_templates()).values()
                  if t["image"] in refs)


def _image_source(image, remotes):
    """Turn ``remote:alias`` (or a bare alias) into an image source block."""
    image = image.strip()
    remote, _, alias = image.partition(":")
    if not alias:
        remote, alias = _default_remote(remotes), image
    if remote == "local":
        # A whole fingerprint names one image exactly, whatever its aliases
        # here: how a snapshot template launches from an image of it.
        if re.match(r"^[0-9a-f]{64}$", alias):
            return {"type": "image", "fingerprint": alias}
        return {"type": "image", "alias": alias}
    if remote not in remotes:
        raise ServiceError(
            "Unknown image remote '%s'. This daemon knows: %s (or 'local:' for "
            "an already-cached image)." % (remote, ", ".join(sorted(remotes)))
        )
    return {
        "type": "image",
        "protocol": "simplestreams",
        "server": remotes[remote],
        "alias": alias,
        "mode": "pull",
    }


def _origin(config):
    """``lemondx``, ``imported``, or None for an instance made some other way.

    A template or stack tag counts as lemondx's own: lemondx before the mark
    existed set those on everything a template or stack launched, and nobody
    tags an instance by hand to be taken for one. A plain create from that
    time carries nothing, so it reads as foreign until imported -- one click
    clears it, and the alternative was to miss genuinely foreign ones.
    """
    origin = config.get(ORIGIN_KEY)
    if origin in (ORIGIN_LEMONDX, ORIGIN_IMPORTED):
        return origin
    if config.get(TEMPLATE_CONFIG_KEY) or config.get(STACK_CONFIG_KEY):
        return ORIGIN_LEMONDX
    return None


def _summarize(instance):
    config = instance.get("config") or {}
    state = instance.get("state") or {}
    addresses = _addresses(state)
    memory = state.get("memory") or {}
    network_totals = _network_totals(state)

    return {
        "name": instance.get("name"),
        "status": instance.get("status") or state.get("status") or "Unknown",
        "type": instance.get("type") or "container",
        "architecture": instance.get("architecture"),
        "ephemeral": instance.get("ephemeral", False),
        "description": instance.get("description") or "",
        "created_at": instance.get("created_at"),
        "last_used_at": instance.get("last_used_at"),
        "profiles": instance.get("profiles") or [],
        "image": config.get("image.description") or config.get("image.os") or "",
        "image_alias": _image_alias(config),
        "limits": {
            "cpu": config.get("limits.cpu") or "",
            "memory": config.get("limits.memory") or "",
        },
        "ipv4": addresses["ipv4"],
        "ipv6": addresses["ipv6"],
        "pid": state.get("pid") or 0,
        "processes": state.get("processes") or 0,
        "cpu_time_ns": (state.get("cpu") or {}).get("usage") or 0,
        "memory_usage": memory.get("usage") or 0,
        "memory_peak": memory.get("usage_peak") or 0,
        "network_rx": network_totals["rx"],
        "network_tx": network_totals["tx"],
        "snapshot_count": len(instance.get("snapshots") or []),
        "template": config.get(TEMPLATE_CONFIG_KEY) or None,
        "stack": config.get(STACK_CONFIG_KEY) or None,
        "origin": _origin(config),
        "revisions": {"template": config.get(TEMPLATE_REVISION_KEY) or "",
                      "stack": config.get(STACK_REVISION_KEY) or ""},
    }


def _revision(record, ignore):
    body = {k: v for k, v in record.items() if k not in ignore}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def template_revision(record):
    """A digest of what a template makes: changes when its instances would differ."""
    return _revision(record, _NOT_INFRA)


def stack_revision(record):
    """A digest of a stack's stages. Any step can feed another, so it is one revision."""
    return _revision(record, ("name", "description"))


def instance_prefix(name):
    """A default instance-name prefix for a template called ``name``."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if not slug[:1].isalpha():
        slug = ("instance-" + slug).strip("-")
    return slug[:50].rstrip("-")


def _image_alias(config):
    """Best-effort reconstruction of the alias the container was made from."""
    os_name = (config.get("image.os") or "").strip()
    release = (config.get("image.release") or "").strip()
    version = (config.get("image.version") or "").strip()
    if os_name and (version or release):
        return "%s %s" % (os_name, version or release)
    return config.get("image.description") or ""


def _addresses(state):
    ipv4, ipv6 = [], []
    for name, iface in (state.get("network") or {}).items():
        if name == "lo":
            continue
        for address in iface.get("addresses") or []:
            if address.get("scope") != "global":
                continue
            if address.get("family") == "inet":
                ipv4.append(address.get("address"))
            elif address.get("family") == "inet6":
                ipv6.append(address.get("address"))
    return {"ipv4": ipv4, "ipv6": ipv6}


def _network_totals(state):
    rx = tx = 0
    for name, iface in (state.get("network") or {}).items():
        if name == "lo":
            continue
        counters = iface.get("counters") or {}
        rx += counters.get("bytes_received") or 0
        tx += counters.get("bytes_sent") or 0
    return {"rx": rx, "tx": tx}


def _network_detail(state):
    detail = []
    for name, iface in sorted((state.get("network") or {}).items()):
        counters = iface.get("counters") or {}
        detail.append({
            "name": name,
            "type": iface.get("type"),
            "state": iface.get("state"),
            "hwaddr": iface.get("hwaddr"),
            "mtu": iface.get("mtu"),
            "addresses": [
                "%s/%s" % (a.get("address"), a.get("netmask")) if a.get("netmask")
                else a.get("address")
                for a in (iface.get("addresses") or [])
            ],
            "rx": counters.get("bytes_received") or 0,
            "tx": counters.get("bytes_sent") or 0,
        })
    return detail

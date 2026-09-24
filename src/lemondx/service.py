"""Domain layer: turns raw LXD records into the shapes lemondx works with.

Both the HTTP API and the CLI call into :class:`ContainerService`, so the two
front ends can never drift apart in behaviour.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from . import health as health_checks
from . import store
from .bootstrap import (BootstrapError, BootstrapRunner, delete_module,
                        discover_modules, effective_params,
                        list_host_ssh_keys, module_source,
                        normalise_module_id, parse_public_key,
                        public_modules, save_module, secret_param_names,
                        check_shell_syntax)
from .lxd import (CGROUP_PAYLOAD_PREFIX, INCUS, NO_SECUREBOOT_CONFIG, LXDClient, LXDError,
                  window_resize_message)
from .simplestreams import CatalogError, fetch_catalog

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
        # Set by ClusterService: (fabric) -> this node's bridge for that
        # fabric, or "" when it is not on it. The service does not read fabric
        # settings itself -- a template names a fabric, and each node resolves
        # that to its own bridge, the same way it resolves a pool or a network.
        self.fabric_bridge = None
        # Set by ClusterService too: (name, iface, bridge) -> configure the
        # fabric NIC inside a started instance. The device only gives it a link.
        self.fabric_configure = None
        # Template app checks on their own intervals; `serve` only, like the
        # sampler. Without it a round runs each check itself.
        self._app_checker = None

    # -- readiness ---------------------------------------------------------

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
                         fabric=None):
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
        if not image:
            raise ServiceError("An image is required, e.g. 'ubuntu:24.04'.")

        instance_config = dict(config or {})
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
            "source": _image_source(image, self.lxd.remotes),
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
        record = self._begin_create(name, image, instance_type,
                                    instance_config.get(TEMPLATE_CONFIG_KEY), len(modules))

        def work():
            return self._run_create(record, payload, start, bootstrap, remember_params,
                                    fabric_key)

        if background:
            self._in_background("create-%s" % name, work)
            return self._create_snapshot(record)
        return work()

    def _run_create(self, record, payload, start, bootstrap, remember_params,
                    fabric_key=None):
        name = payload["name"]
        modules = (bootstrap or {}).get("modules") or []
        try:
            self.lxd.create_instance(payload, wait=True)
            if start:
                self._create_stage(record, stage="starting")
                self.lxd.set_state(name, "start")
                # Before bootstrap, not after: a module may well want to reach
                # another node, and an interface with no address is not there
                # yet as far as anything inside the instance is concerned.
                if fabric_key and self.fabric_configure:
                    self.fabric_configure(name, fabric_key,
                                          payload["devices"][fabric_key].get("network"))

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
        threading.Thread(target=target, name="lemondx-%s" % label, daemon=True).start()

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
                for sample, (probe, app) in zip(running, pool.map(check, running)):
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
            if names is None:
                self._health_base = {b["name"]: b for b in bases}
                self._health_records = {r["name"]: r for r in records}
            else:
                self._health_base.update((b["name"], b) for b in bases)
                self._health_records.update((r["name"], r) for r in records)
            self._health_checked_at = time.time()
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
                    print("[lemondx] health check failed: %s" % exc)
                elapsed = time.time() - started
                # Rounds never overlap: a slow one just starts the next later.
                time.sleep(max(1.0, settings["interval_seconds"] - elapsed))

        threading.Thread(target=loop, name="lemondx-health", daemon=True).start()

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
            self._health_records[name] = health_checks.fold_app(
                base, self._app_status(name, previous["app"]["template"], result,
                                       checker.has_target(name)),
                settings, previous)

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

    def default_image(self):
        """A reasonable starting image for whichever daemon is in use."""
        catalog = (IMAGE_CATALOG_INCUS if self.lxd.flavor == INCUS
                   else IMAGE_CATALOG_LXD)
        return catalog[0]["alias"]

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
        modules = list(dict.fromkeys(str(m) for m in modules or []))
        unknown = [m for m in modules if m not in available]
        if unknown:
            raise ServiceError("Unknown module(s): %s" % ", ".join(unknown))
        selected = [available[m] for m in modules]

        given = {str(k): "" if v is None else str(v) for k, v in (params or {}).items()}
        undeclared = sorted(set(given) - {p["name"] for m in selected for p in m["params"]})
        if undeclared:
            raise ServiceError(
                "%s is not a parameter of the selected modules."
                % ", ".join(undeclared))

        settings = store.load()
        values = {}
        for module_id, module in zip(modules, selected):
            values.update(effective_params(module_id, module, settings))
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
            "params": {k: v for k, v in values.items() if k not in secret},
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
            "params": {k: v for k, v in record["params"].items() if k not in secret},
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
                      name_prefix=None, secureboot=True, app_check=None):
        name = self._record_name(name, "template")
        image = str(image or "").strip()
        if not image:
            raise ServiceError("A template needs an image, e.g. 'ubuntu:24.04'.")
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
            "image": image,
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
        missing = sorted(n for n in secret_param_names(selected) if not values.get(n))
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
            container = self.create_container(
                name=instance_name, image=template["image"],
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
            return list(pool.map(work, names))

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
        notes = []
        if place:
            template, notes = self.place_template(template)
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
        template, notes = self.place_template(template)
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
        self._launch_bootstrap(self._template(name), params)
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
        for module_id in modules:
            module = available.get(module_id)
            if module:
                merged.update(effective_params(module_id, module, settings))
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
        return {"deleted": name}

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


def _log(message):
    """Say something worth keeping. Under systemd this is the journal.

    stderr rather than stdout: a launch runs from the CLI as well as the
    server, and there stdout may be a --json payload a script is reading.
    """
    print("[lemondx] %s" % message, file=sys.stderr, flush=True)


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
    if network.get("type") != "bridge":
        return "only bridge networks are managed here."
    return ""


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


def _image_source(image, remotes):
    """Turn ``remote:alias`` (or a bare alias) into an image source block."""
    image = image.strip()
    remote, _, alias = image.partition(":")
    if not alias:
        # No remote given. Prefer `ubuntu:` where it exists (LXD), else the
        # daemon's general-purpose image server.
        remote, alias = ("ubuntu" if "ubuntu" in remotes else "images"), image
    if remote == "local":
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

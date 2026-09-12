"""Domain layer: turns raw LXD records into the shapes lemondx works with.

Both the HTTP API and the CLI call into :class:`ContainerService`, so the two
front ends can never drift apart in behaviour.
"""

from __future__ import annotations

import re
from .bootstrap import (BootstrapError, BootstrapRunner, list_host_ssh_keys,
                        parse_public_key, public_modules)
from .lxd import INCUS, LXDClient, LXDError
from .simplestreams import CatalogError, fetch_catalog

VALID_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]{0,61}$")

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

QUOTA_CAPABLE_DRIVERS = frozenset({
    "btrfs", "zfs", "lvm", "ceph", "cephfs", "pure", "powerflex", "alletra",
})

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


class ContainerService:
    def __init__(self, client=None, **kwargs):
        self.lxd = client or LXDClient(**kwargs)

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

        return {
            "connected": True,
            "ready": not issues,
            "issues": issues,
            "root_pool": root_pool,
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
        return [_summarize(i) for i in self.lxd.list_instances()]

    def get_container(self, name):
        instance = self.lxd.get_instance(name)
        summary = _summarize(instance)
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
                         profiles=None, cpu=None, memory=None, disk=None,
                         description=None, ephemeral=False, start=True,
                         config=None, wait=True, bootstrap=None):
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
        if disk:
            # Overriding the root disk at instance level replaces the profile's
            # device outright, and LXD requires an explicit pool on it.
            pool = self._root_pool(payload["profiles"])
            if not pool:
                raise ServiceError(
                    "Cannot set a disk size: no storage pool is configured. "
                    "Run setup first."
                )
            payload["devices"] = {
                "root": {"type": "disk", "path": "/", "pool": pool,
                         "size": normalize_size(disk, "disk size")}
            }

        if bootstrap and bootstrap.get("modules") and not (start and wait):
            raise ServiceError(
                "Bootstrap modules need the container to start, so 'start' "
                "cannot be disabled."
            )

        self.lxd.create_instance(payload, wait=wait)
        if start and wait:
            self.lxd.set_state(name, "start")

        if not wait:
            return {"name": name, "status": "Pending"}

        container = self.get_container(name)
        if bootstrap and bootstrap.get("modules"):
            # Report bootstrap failures alongside the container rather than
            # raising: the container exists either way and the user needs to
            # see which module failed and why.
            container["bootstrap"] = self.bootstrap(
                name,
                modules=bootstrap["modules"],
                params=bootstrap.get("params"),
                ssh_keys=bootstrap.get("ssh_keys"),
            )
        return container

    def root_pool_info(self, profiles=None):
        """The pool a new container lands on, and whether it enforces quotas."""
        name = self._root_pool(profiles or ["default"])
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

    def list_modules(self):
        return public_modules()

    def list_ssh_keys(self):
        """Public keys found on the host, offered for installing into containers."""
        return list_host_ssh_keys()

    def bootstrap(self, name, modules, params=None, ssh_keys=None, timeout=900):
        """Run the selected bash modules inside a running container."""
        if not modules:
            raise ServiceError("No bootstrap modules selected.")
        try:
            return BootstrapRunner(self.lxd).run(
                name, modules, params=params, ssh_keys=ssh_keys, timeout=timeout
            )
        except BootstrapError as exc:
            raise ServiceError(exc.message, exc.code) from exc

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
        """All interfaces the daemon can see, managed ones first."""
        summaries = []
        for network in self.lxd.list_networks():
            config = network.get("config") or {}
            summaries.append({
                "name": network.get("name"),
                "type": network.get("type"),
                "managed": bool(network.get("managed")),
                "status": network.get("status"),
                "description": network.get("description") or "",
                "ipv4_address": config.get("ipv4.address") or "",
                "ipv6_address": config.get("ipv6.address") or "",
                "used_by": len(network.get("used_by") or []),
            })
        summaries.sort(key=lambda n: (not n["managed"], n["name"]))
        return summaries

    def get_network(self, name):
        """Everything about one network: config, live state, and who is on it."""
        network = self.lxd.get_network(name)
        config = network.get("config") or {}
        managed = bool(network.get("managed"))

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

        # used_by entries are API paths; pull out the instance names.
        instances = []
        for path in network.get("used_by") or []:
            marker = "/instances/"
            if marker in path:
                instances.append(path.split(marker, 1)[1].split("?", 1)[0])
        profiles = [p.split("/profiles/", 1)[1].split("?", 1)[0]
                    for p in (network.get("used_by") or []) if "/profiles/" in p]

        return {
            "name": network.get("name"),
            "type": network.get("type"),
            "managed": managed,
            "status": network.get("status"),
            "description": network.get("description") or "",
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
            "instances": sorted(set(instances)),
            "profiles": sorted(set(profiles)),
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


# -- normalisation helpers -------------------------------------------------


def _is_true(value):
    return str(value).lower() in ("true", "yes", "1", "on")


def _address_key(address):
    """Sort IPv4 numerically rather than as text."""
    parts = str(address).split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return (0, [int(p) for p in parts])
    return (1, [str(address)])


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
    }


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

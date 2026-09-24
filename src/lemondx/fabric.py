"""Fabrics: routed, non-NAT networks between the containers of a cluster.

A managed bridge is NAT'd, which is right for reaching the internet and wrong
for reaching a container on another host -- the source address is rewritten and
there is no route back. A fabric is a second bridge on every node with NAT off,
one /24 per node out of a prefix the whole cluster shares, and a host route to
every other node's /24. Each node keeps its existing bridge untouched, so a
container gets its internet through the NAT'd `eth0` exactly as before and its
peers through the fabric NIC. Only traffic between nodes is new.

A cluster may have several, each with a name that is also its bridge's name on
every node, so a stack can keep its database tier on one and its web tier on
another. What a fabric *is* never lives in one place: it is the set of nodes
holding a claim in it, and each node's claims ride on its own member record.
There is no list of fabrics to fall out of step with the claims, and nothing
for reconciliation to get wrong -- two nodes claiming one subnet is a
two-sided difference it would refuse to settle anyway, so allocation is
coordinated instead: the node a fabric is created on works out every member's
/24, checks it against what each host already uses, and tells each one.

That split is also why a fabric bridge must not hand out a default route
(`raw.dnsmasq` below): two default routes in a container is a coin toss over
whether its internet traffic leaves through a bridge that cannot NAT it.

This sits above `ClusterService` -- it needs the member list to know what the
routes should be -- and calls `hostnet.py` to program them, the same way
`cluster.py` calls `nodeclient.py`. `ClusterService` reaches it lazily, as it
already does for stacks, so nothing here is built on a node that never
federates.

A prefix is a /16 to a /22 split into /24s: at most 254 nodes, which is far
past what a flat, leaderless design is good for anyway, and at least four.
"""

from __future__ import annotations

import ipaddress
import os
import sys
import threading
import time

from . import hostnet, store
from .lxd import LXDError
from .service import ServiceError, free_nic

SETTINGS_SECTION = "fabric"
SETTINGS_VERSION = 2

DEFAULT_SETTINGS = {
    # Blank: the address this node's cluster URL names. Set it where a host has
    # several interfaces and peers reach it on a particular one.
    "via": "",
    # name -> {"prefix", "subnet", "nat"}: the fabrics this node is on, the
    # /24 it holds in each, and whether what leaves for anywhere outside the
    # fabric is NAT'd. Written when a claim is accepted, never by hand -- an
    # empty map is "on no fabric", which is how every node starts.
    "fabrics": {},
}

# What a new fabric is called and where it goes when nobody says: the first
# free lemonfabN, and the first /16 in these ranges that no member uses. 10/8
# first, from .100 up because the low /16s are where people already put
# things; then 172.16/12 (docker takes 172.17 and 172.18, and they are
# skipped like anything else in use). 192.168/16 is never offered -- it *is*
# a /16, and it is the one most LANs are in.
NAME_STEM = "lemonfab"
# How a bridge says a fabric made it. The only mark one made before anything
# checked for it carries, so it stays the description rather than a config key.
BRIDGE_MARK = "lemondx fabric %s:"
_CANDIDATE_PREFIXES = ([("10.%d.0.0/16" % n) for n in range(100, 255)]
                       + [("10.%d.0.0/16" % n) for n in range(1, 100)]
                       + [("172.%d.0.0/16" % n) for n in range(16, 32)])

# What dnsmasq is told about a fabric bridge without NAT. Option 3 is the
# default route; offering it empty means "no gateway here", which is what keeps
# a container's internet traffic on the NAT'd bridge where it works. A fabric
# with NAT offers its gateway like any bridge, so an instance can have it as
# its only NIC.
NO_DEFAULT_ROUTE = "dhcp-option=3"

# Attaching a NIC gives an instance a link, not an address: almost every image
# configures only eth0, so a second interface comes up with nothing on it. This
# is the in-guest half of joining a fabric -- persistent where the image has
# somewhere to persist it, and brought up now either way.
#
# An address is not enough either. It brings only the node's own /24 onto the
# interface, so traffic for a peer's /24 would follow the default route out of
# eth0, be NAT'd on the node's ordinary bridge, and arrive looking like the
# host. The route to the whole fabric prefix through this interface is what
# makes the traffic between nodes the instance's own.
#
# POSIX sh for the same reason bootstrap modules are: a minimal image has no
# bash. The branches mirror `modules/_prelude.sh` -- detect what the image
# actually uses rather than assuming a distribution.
#
# UseRoutes/UseDNS are off for networkd: a fabric with NAT does offer a
# gateway, which is right when it is the instance's only NIC and a second
# default route when it is not. The prefix route is all a second NIC needs.
_CONFIGURE_NIC = r"""
set -u
iface=%s
prefix=%s
gateway=%s
ip link set "$iface" up 2>/dev/null
if [ -d /etc/systemd/network ] && [ -d /run/systemd/system ]; then
    printf '[Match]\nName=%%s\n\n[Network]\nDHCP=ipv4\n\n[DHCPv4]\nUseRoutes=false\nUseDNS=false\n\n[Route]\nDestination=%%s\nGateway=%%s\n' \
        "$iface" "$prefix" "$gateway" > "/etc/systemd/network/50-lemondx-$iface.network"
    networkctl reload >/dev/null 2>&1 || systemctl restart systemd-networkd >/dev/null 2>&1
    networkctl reconfigure "$iface" >/dev/null 2>&1
elif [ -f /etc/network/interfaces ]; then
    grep -q "^iface $iface " /etc/network/interfaces ||
        printf '\nauto %%s\niface %%s inet dhcp\n    up ip route replace %%s via %%s dev %%s\n' \
            "$iface" "$iface" "$prefix" "$gateway" "$iface" >> /etc/network/interfaces
fi
i=0
while [ $i -lt 15 ]; do
    if ip -o -4 addr show "$iface" 2>/dev/null | grep -q inet; then
        ip route replace "$prefix" via "$gateway" dev "$iface" && exit 0
    fi
    ifup "$iface" >/dev/null 2>&1 ||
        udhcpc -i "$iface" -n -q >/dev/null 2>&1 ||
        dhclient -1 "$iface" >/dev/null 2>&1 ||
        dhcpcd -n "$iface" >/dev/null 2>&1
    i=$((i + 1))
    sleep 1
done
exit 1
"""

# How long to let the guest pick up a lease. DHCP on a local bridge is fast;
# this is a ceiling on a guest that will never answer, not an expected wait.
CONFIGURE_TIMEOUT = 45


# Matching the reconciler: late enough that `serve` is listening, then a slow
# heartbeat, since every membership change already reapplies on the spot.
START_DELAY = 20
REAPPLY_INTERVAL = 3600

MIN_PREFIX_BITS = 16
MAX_PREFIX_BITS = 22
NODE_BITS = 24

# A bound on what one node can be asked to carry, so a settings file or a
# member record cannot make the route plan arbitrarily large.
MAX_FABRICS = 16

# Reading another node's fabric runs `ip` and asks sudo a question, so a
# member that has gone quiet is given up on well before the UI's poll.
STATUS_TIMEOUT = 8
# Accepting a claim creates a bridge and programs routes, then levels
# membership with every peer; generous, since it is done once.
CLAIM_TIMEOUT = 90


class FabricError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


# -- settings --------------------------------------------------------------


def clean_settings(raw):
    """Validate a saved fabric section. Raises FabricError if it is unusable."""
    raw = dict(raw or {})
    if "fabrics" not in raw and any(k in raw for k in ("enabled", "prefix", "bridge", "subnet")):
        raw = _from_version_1(raw)
    settings = {"via": "", "fabrics": {}}
    for key, value in raw.items():
        if key == "version":
            continue
        if key not in DEFAULT_SETTINGS:
            raise FabricError("fabric settings: unknown key %r" % key)
        settings[key] = value

    via = str(settings["via"] or "").strip()
    if via:
        try:
            via = str(ipaddress.ip_address(via))
        except ValueError:
            raise FabricError("fabric settings: 'via' must be an IP address (%r)." % via)
    settings["via"] = via

    fabrics = settings["fabrics"]
    if not isinstance(fabrics, dict):
        raise FabricError("fabric settings: 'fabrics' must be an object.")
    if len(fabrics) > MAX_FABRICS:
        raise FabricError("fabric settings: at most %d fabrics." % MAX_FABRICS)
    cleaned = {}
    for name, claim in sorted(fabrics.items()):
        name = clean_name(name)
        if not isinstance(claim, dict):
            raise FabricError("fabric settings: '%s' must be an object." % name)
        prefix = clean_prefix(claim.get("prefix"))
        subnet = clean_subnet(claim.get("subnet"), prefix)
        for other, held in cleaned.items():
            if prefix.overlaps(ipaddress.ip_network(held["prefix"])):
                raise FabricError("fabric settings: %s (%s) overlaps %s (%s)."
                                  % (name, prefix, other, held["prefix"]))
        cleaned[name] = {"prefix": str(prefix), "subnet": str(subnet),
                         "nat": claim.get("nat") is not False}
    settings["fabrics"] = cleaned
    return settings


def _from_version_1(raw):
    """The one-fabric layout: a bridge, a prefix and maybe a subnet, flat.

    It becomes a fabric named after its bridge, which is what peers reading
    a legacy node record assume too (`store.clean_fabric`). One that was
    configured but never allocated a subnet was not on the fabric, so it
    carries nothing over but `via`. NAT stays off, as it was then.
    """
    fabrics = {}
    if raw.get("enabled") and raw.get("subnet"):
        fabrics[str(raw.get("bridge") or store.LEGACY_FABRIC)] = {
            "prefix": raw.get("prefix") or "", "subnet": raw.get("subnet"), "nat": False}
    return {"via": raw.get("via") or "", "fabrics": fabrics}


def clean_name(value):
    """A fabric's name, which is also its bridge's. Raises FabricError."""
    from .service import VALID_NETWORK_NAME
    name = str(value or "").strip()
    if not VALID_NETWORK_NAME.match(name):
        raise FabricError(
            "'%s' is not a usable fabric name: it becomes the bridge's name on "
            "every node, so letters, digits and -, starting with a letter, up "
            "to 15 characters." % name)
    return name


def clean_prefix(value):
    """A fabric's prefix, as an ip_network. Raises FabricError if unusable."""
    try:
        prefix = ipaddress.ip_network(str(value or "").strip(), strict=False)
    except ValueError:
        raise FabricError("'%s' is not a subnet." % value)
    if prefix.version != 4:
        raise FabricError("Fabrics are IPv4 only for now (%s)." % prefix)
    if not MIN_PREFIX_BITS <= prefix.prefixlen <= MAX_PREFIX_BITS:
        raise FabricError(
            "A fabric prefix is a /%d to a /%d, so it splits into a /%d per node "
            "(%s is a /%d)." % (MIN_PREFIX_BITS, MAX_PREFIX_BITS, NODE_BITS,
                                prefix, prefix.prefixlen))
    if not prefix.is_private:
        raise FabricError("A fabric prefix must be a private range (%s)." % prefix)
    return prefix


def clean_subnet(value, prefix):
    """One node's /24 inside ``prefix``. Raises FabricError if it is not."""
    try:
        network = ipaddress.ip_network(str(value or "").strip(), strict=False)
    except ValueError:
        raise FabricError("'%s' is not a subnet." % value)
    if network.version != 4 or network.prefixlen != NODE_BITS \
            or not network.subnet_of(prefix):
        raise FabricError("%s is not a /%d inside %s." % (network, NODE_BITS, prefix))
    return network


def load_settings():
    """Saved fabric settings, or the defaults. Raises FabricError if unusable.

    Like auth and cluster and unlike health: a fabric section that does not
    validate names the subnets this node claims cluster-wide, and starting
    with silently different ones would put two nodes on the same addresses.
    """
    try:
        raw = store.load_config(SETTINGS_SECTION)
    except ValueError as exc:
        raise FabricError("Cannot use the saved fabric settings: %s" % exc, 500)
    if raw is None:
        return {"via": "", "fabrics": {}}
    return clean_settings(raw)


def save_settings(settings):
    cleaned = clean_settings(settings)
    store.save_config(SETTINGS_SECTION, dict(cleaned, version=SETTINGS_VERSION))
    return cleaned


def reset_settings():
    return store.delete_config(SETTINGS_SECTION)


def settings_path():
    return store.config_path(SETTINGS_SECTION)


def local_claim(default_via=""):
    """This node's claims, for the record peers keep of it.

    A module function because `ClusterService.local_node()` needs it on every
    member listing, long before anything wants a whole FabricService.
    """
    try:
        settings = load_settings()
    except FabricError:
        return {"via": "", "fabrics": {}}
    if not settings["fabrics"]:
        return {"via": "", "fabrics": {}}
    return {"via": settings["via"] or str(default_via or ""),
            "fabrics": settings["fabrics"]}


def _unreachable_message(nodes):
    """Why a fabric cannot be made while ``nodes`` are down, and the way out.

    Every member has to answer: one that did not cannot be checked for what
    it already uses, and nothing would hand it its subnet later -- claims are
    never reconciled. A member that is gone for good is therefore not waited
    for but evicted, which works on a node that cannot be reached (the
    credential is rotated instead of it being told).
    """
    one = len(nodes) == 1
    return ("A fabric is created on every node of the cluster at once, and %s "
            "did not answer. Bring %s back, or evict %s from the cluster (Nodes "
            "tab, or `lemondx cluster evict NAME`) to go on without %s."
            % (", ".join(nodes), "it" if one else "them", "it" if one else "them",
               "it" if one else "them"))


def firewall_spec(fabrics):
    """What `hostnet.firewall_ruleset()` takes, from {name: claim} settings."""
    return [{"bridge": name, "prefix": f["prefix"], "subnet": f["subnet"],
             "nat": f["nat"]} for name, f in sorted(fabrics.items())]


# -- the service -----------------------------------------------------------


class FabricService:
    """One node's half of every fabric, and the cluster-wide operations on them.

    Most of this is local: the bridges, the claims and the routes on this
    host. `create()`, `extend()`, `delete()`, `check()` and `overview()` are
    the coordinator's side -- the node a person is talking to -- and reach the
    other members through their own copy of this class, over the API.
    """

    def __init__(self, cluster):
        self.cluster = cluster
        self.service = cluster.service
        self._lock = threading.Lock()
        # One fabric change at a time on this node, coordinator side: two
        # creates racing would both find the same /16 free.
        self._change_lock = threading.Lock()
        # Separate from `_lock`, which an apply holds for as long as the
        # helper takes: an attach has no business waiting on routes.
        self._attach_lock = threading.Lock()
        self._settings = None
        self._settings_seen = None
        self._firewall = None

    # -- settings ------------------------------------------------------

    def settings(self):
        """The saved settings, re-read whenever the file has changed.

        Cached, but never trusted past a change on disk -- the same rule
        `AuthService` keeps, for the same reason: the CLI and `serve` are two
        processes over one file. A fabric created from the CLI writes this
        node's claim there, and a `serve` still holding the old copy would
        route nothing for it when the peers' claims reach it a second later.
        """
        fingerprint = self._fingerprint()
        if self._settings is None or fingerprint != self._settings_seen:
            self._settings = load_settings()
            self._settings_seen = fingerprint
        return self._settings

    def _fingerprint(self):
        try:
            stat = os.stat(settings_path())
        except OSError:
            return None
        return (stat.st_ino, stat.st_mtime_ns, stat.st_size)

    def reload_settings(self):
        self._settings = None
        return self.settings()

    def _save(self, **changes):
        settings = dict(self.settings())
        settings.update(changes)
        self._settings = save_settings(settings)
        self._settings_seen = self._fingerprint()
        return self._settings

    def fabrics(self):
        """{name: {"prefix", "subnet"}} for every fabric this node is on."""
        return dict(self.settings()["fabrics"])

    def enabled(self):
        """Is this node on any fabric?"""
        return bool(self.settings()["fabrics"])

    def prefixes(self):
        return [ipaddress.ip_network(f["prefix"]) for f in self.fabrics().values()]

    def via(self):
        """The address peers route this node's subnets to."""
        return self.settings()["via"] or self.cluster.local_route_address()

    def resolve(self, wanted):
        """Which of this node's bridges a request for ``wanted`` means, or ''.

        ``True`` is "a fabric, whichever": the CLI's bare `--fabric`, and the
        only thing a template could say before fabrics had names. It means the
        one this node is on, or the legacy name when it is on several.
        """
        fabrics = self.fabrics()
        if wanted is True:
            if len(fabrics) == 1:
                return next(iter(fabrics))
            return store.LEGACY_FABRIC if store.LEGACY_FABRIC in fabrics else ""
        name = str(wanted or "").strip()
        return name if name in fabrics else ""

    def require(self, wanted):
        """``resolve()``, refusing when it comes to nothing."""
        bridge = self.resolve(wanted)
        if bridge:
            return bridge
        names = ", ".join(sorted(self.fabrics()))
        if wanted is True:
            raise FabricError(
                "This node is on %s, so say which fabric."
                % ("several fabrics (%s)" % names if names else "no fabric"), 409)
        raise FabricError("This node is not on a fabric called '%s'%s."
                          % (wanted, " (it is on %s)" % names if names else ""), 409)

    # -- what the routes should be -------------------------------------

    def _claims(self):
        """(node, fabric, prefix, subnet, via) for every member's claim, ours included."""
        claims = []
        for member in self.cluster.members():
            record = member.get("fabric") or {}
            via = record.get("via") or ""
            for name, claim in sorted((record.get("fabrics") or {}).items()):
                claims.append((member["name"], name, claim.get("prefix") or "",
                               claim.get("subnet") or "", via))
        return claims

    def _nat_of(self, name):
        """Whether fabric ``name`` NATs, by this node's word or else the members'."""
        mine = self.fabrics().get(name)
        if mine:
            return mine["nat"]
        for member in self.cluster.members():
            claim = ((member.get("fabric") or {}).get("fabrics") or {}).get(name)
            if claim:
                return claim.get("nat") is not False
        return True

    def _peer_claims(self, name, prefix):
        """(node, subnet, via) for the peers in fabric ``name`` that agree on its prefix.

        A peer claiming the same name with a different prefix is not in the
        same fabric as far as routing goes: its subnet may be anywhere, and the
        helper would refuse the route in any case. `overview()` shows it.
        """
        local = self.cluster.local_name()
        return [(node, subnet, via) for node, fabric, peer_prefix, subnet, via
                in self._claims()
                if fabric == name and node != local and via and peer_prefix == prefix]

    def desired_routes(self):
        """{subnet: via} for every peer in every fabric this node is on."""
        routes = {}
        for name, fabric in sorted(self.fabrics().items()):
            for _, subnet, via in self._peer_claims(name, fabric["prefix"]):
                routes.setdefault(subnet, via)
        return routes

    # -- status --------------------------------------------------------

    def status(self):
        """This node's half of every fabric. What `GET /api/fabric` serves."""
        try:
            current = hostnet.current_routes()
        except hostnet.HostNetError as exc:
            current, routes_error = {}, exc.message
        else:
            routes_error = ""

        fabrics = []
        for name, fabric in sorted(self.fabrics().items()):
            bridge = self._bridge_record(name)
            routes = []
            for node, subnet, via in sorted(self._peer_claims(name, fabric["prefix"])):
                if current.get(subnet) == via:
                    state = "ok" if hostnet.reaches(via) else "unreachable"
                elif subnet in current:
                    state = "wrong"
                else:
                    state = "missing"
                routes.append({"node": node, "subnet": subnet, "via": via, "state": state})
            fabrics.append({
                "name": name, "prefix": fabric["prefix"], "subnet": fabric["subnet"],
                "nat": fabric["nat"], "gateway": self._gateway(fabric["subnet"]),
                "bridge_ready": self._bridge_ready(bridge, fabric["subnet"]),
                "instances": self._bridge_instances(bridge),
                "routes": routes,
            })

        pending, plan_error = 0, ""
        if fabrics:
            try:
                # The firewall is left out of the count: it is always in the
                # plan (see `hostnet.firewall_commands()`), so counting it
                # would show a change to apply that never goes away.
                pending = len(self._route_plan(current, self.desired_routes()))
            except hostnet.HostNetError as exc:
                plan_error = exc.message

        return {
            "node": self.cluster.local_name(),
            "enabled": bool(fabrics),
            "via": self.via() if fabrics else self.settings()["via"],
            "fabrics": fabrics,
            "privileged": hostnet.available(),
            "pending": pending,
            # What the last apply made of the firewall, since only root can
            # read it back: None until this process has applied once.
            "firewall": self._firewall,
            "error": routes_error or plan_error,
            "check": self.self_check(),
            "warnings": hostnet.diagnose(sorted(self.fabrics())) if fabrics else [],
        }

    def self_check(self):
        """One sentence about why a fabric will not carry traffic, or ''.

        The precondition worth checking is the one this version is built on:
        every node on one L2. A peer that is not directly reachable gets a
        route that resolves to nothing, and the symptom is silent packet loss
        hours later rather than an error at setup.
        """
        fabrics = self.fabrics()
        if not fabrics:
            return ""
        unreachable = sorted({node for name, f in fabrics.items()
                              for node, _, via in self._peer_claims(name, f["prefix"])
                              if not hostnet.reaches(via)})
        if unreachable:
            return ("%s is not on a network this host is directly attached to, so "
                    "routed traffic cannot reach it. Fabrics need every node on "
                    "one L2 network." % ", ".join(unreachable))
        missing = [name for name, f in sorted(fabrics.items())
                   if not self._bridge_ready(self._bridge_record(name), f["subnet"])]
        if missing:
            return ("Not every fabric bridge here holds its subnet yet (%s); run "
                    "`lemondx fabric apply`." % ", ".join(missing))
        return ""

    # -- the plan ------------------------------------------------------

    def _route_plan(self, current, desired):
        commands = hostnet.route_commands(desired, current, self.prefixes())
        return commands + hostnet.forwarding_commands(sorted(self.fabrics()))

    def _plan(self, current, desired):
        return self._route_plan(current, desired) + hostnet.firewall_commands(
            firewall_spec(self.fabrics()))

    def plan(self):
        """The host commands that would bring this node's routes up to date."""
        commands = self._plan(hostnet.current_routes(), self.desired_routes())
        return {"node": self.cluster.local_name(),
                "commands": [command.record() for command in commands],
                "text": hostnet.describe(commands),
                "privileged": hostnet.available()}

    def refuse_here(self, what):
        """Raise 409 while this node is in maintenance; None otherwise.

        Fabric changes are what maintenance stops on a host: its routes,
        bridges and firewall stay exactly as they were until it ends.
        """
        local = self.cluster.local_name()
        if self.cluster.maintenance_of(local):
            raise FabricError(self.cluster._maintenance_message([local], what), 409)
        return None

    def _refuse_members(self, nodes, what):
        held = [n for n in nodes if self.cluster.maintenance_of(n)]
        if held:
            raise FabricError(self.cluster._maintenance_message(held, what), 409)

    def apply(self):
        """Create the bridges if needed, then program the routes.

        The bridges go through the daemon, which lemondx can already talk to.
        The routes are kernel state, so unless this process is already root
        they go to the helper -- which is handed the wanted state, not commands.
        """
        self.refuse_here("change this node's fabric routes")
        with self._lock:
            # One bridge that cannot be ours must not keep every other
            # fabric's routes from being programmed.
            refused = []
            for name in sorted(self.fabrics()):
                try:
                    self.ensure_bridge(name)
                except FabricError as exc:
                    refused.append({"command": "bridge %s" % name,
                                    "why": "create the fabric's bridge",
                                    "ok": False, "error": exc.message})
            results, failed = self._program()
            results, failed = refused + results, refused + failed
        return {"node": self.cluster.local_name(), "ok": not failed,
                "applied": results, "check": self.self_check(),
                "status": self.status()}

    def _program(self):
        """Bring the kernel's routes to what the settings say. (results, failed)."""
        desired = self.desired_routes()
        if hostnet.is_root():
            commands = self._plan(hostnet.current_routes(), desired)
            commands += hostnet.docker_commands(sorted(self.fabrics()))
            results = hostnet.apply(commands) if commands else []
            failed = [r for r in results if not r["ok"]]
        else:
            answer = hostnet.delegate({"routes": desired, "bridges": sorted(self.fabrics())})
            results = answer.get("applied") or []
            failed = [] if answer.get("ok") else [
                r for r in results if not r["ok"]] or [
                {"command": "fabric-helper", "why": "program the routes",
                 "ok": False, "error": answer.get("error") or "refused"}]
        # The firewall is the one command without a route in it; the helper
        # and `hostnet.apply()` both report it by its `nft -f -` spelling.
        wall = next((r for r in results if " -f -" in r["command"]), None)
        self._firewall = {
            "ok": bool(wall and wall["ok"]),
            "error": (wall or {}).get("error") or (
                "" if wall else (failed[0]["error"] if failed else "not applied")),
            "at": int(time.time())}
        return results, failed

    def reapply(self):
        """Bring routes up to date, quietly. Called on every membership change.

        Never raises: this runs from `remember_members()` and from a background
        thread, where a node that cannot program a route must not take down the
        membership change that prompted it. The failure is visible in status().
        """
        try:
            if not hostnet.available():
                return None
            # Frozen for the duration; ending maintenance calls this again.
            if self.cluster.maintenance():
                return None
            # With no fabric left there may still be routes of ours to clear,
            # so this does not stop at `enabled()`; `_program()` with nothing
            # wanted is "remove everything of ours", and a no-op otherwise.
            if not self.enabled() and not hostnet.current_routes():
                return None
            return self.apply()
        except (FabricError, hostnet.HostNetError, ServiceError, LXDError, OSError):
            return None

    def start(self, delay=START_DELAY, interval=REAPPLY_INTERVAL):
        """Bring the routes up after `serve` starts, then keep them up.

        A daemon thread like the reconciler, and for the same reason: routes
        live in the kernel and are gone after a reboot, and a peer that
        changed address while this node was down is only noticed by looking
        again. The first pass waits for the listener so a node that has to be
        asked for its own status is answering by then.
        """
        def run():
            time.sleep(delay)
            while True:
                try:
                    self.reapply()
                except Exception as exc:           # noqa: BLE001 - a chore thread
                    # reapply() means not to raise, but one miss must not end
                    # the loop: routes then stay wrong until `serve` restarts.
                    print("[lemondx] fabric: reapply failed: %s" % exc,
                          file=sys.stderr, flush=True)
                time.sleep(interval)

        thread = threading.Thread(target=run, name="fabric", daemon=True)
        thread.start()
        return thread

    # -- the bridges ---------------------------------------------------

    def _bridge_record(self, name):
        try:
            return self.service.lxd.get_network(name)
        except Exception:
            return None

    def _bridge_ready(self, record, subnet):
        if not record or not subnet:
            return False
        address = (record.get("config") or {}).get("ipv4.address") or ""
        return address == "%s/%d" % (self._gateway(subnet), NODE_BITS)

    def _bridge_instances(self, record):
        from .service import _network_users
        return _network_users(record)[0] if record else []

    def _gateway(self, subnet):
        return str(next(ipaddress.ip_network(subnet).hosts()))

    def ensure_bridge(self, name):
        """Create or correct this node's bridge for fabric ``name``."""
        fabric = self.fabrics().get(name)
        if not fabric:
            raise FabricError("This node is not on a fabric called '%s'." % name, 404)
        address = "%s/%d" % (self._gateway(fabric["subnet"]), NODE_BITS)
        config = {"ipv4.address": address, "ipv4.nat": "false",
                  "ipv4.dhcp": "true", "ipv6.address": "none"}
        existing = {n.get("name"): n for n in self.service.lxd.list_networks()}
        self._refuse_foreign_network(name, existing.get(name))
        if name in existing:
            self.service.update_network(name, description=None, config=config)
        else:
            self.service.create_network(
                name, description=BRIDGE_MARK % name + " routed between nodes",
                config=config)
        self._offer_gateway(name, fabric["nat"])
        return self.service.get_network(name)

    def _refuse_foreign_network(self, name, record):
        """Refuse to take over a network of this name that no fabric made.

        `create()` checks every member for the name, but a node that joins
        later, or is brought in by `extend()`, is only told its subnet -- and
        correcting an existing bridge here would quietly re-address and
        un-NAT whatever a person had put on it. Ours say so in their
        description, which is all a bridge made before this check carries.
        """
        if record is None:
            return
        if not record.get("managed"):
            raise FabricError(
                "'%s' already exists on this host and the daemon does not manage "
                "it, so it cannot be this node's fabric bridge." % name, 409)
        if not str(record.get("description") or "").startswith(BRIDGE_MARK % name):
            raise FabricError(
                "This host already has a network called '%s' that is not a fabric "
                "bridge, and the fabric's bridge takes its name. Rename or delete "
                "it, then run `lemondx fabric extend %s`." % (name, name), 409)

    def _offer_gateway(self, name, nat):
        """Offer a default route on a fabric bridge only when it NATs.

        Without NAT a default route here is a coin toss over whether an
        instance's internet traffic leaves through a bridge that cannot carry
        it, so dnsmasq is told to offer none. With NAT the bridge is a whole
        network, and the gateway is what lets it be an instance's only NIC.

        This goes straight to the daemon rather than through
        `ContainerService.update_network()`, because `raw.dnsmasq` is
        deliberately not in that method's allowlist: dnsmasq runs as root under
        the daemon and `dhcp-script=` would turn a network edit in the REST API
        into arbitrary host execution. lemondx setting one fixed value on its
        own bridge is a different thing from letting a request set any value.
        """
        record = self.service.lxd.get_network(name)
        config = dict(record.get("config") or {})
        wanted = "" if nat else NO_DEFAULT_ROUTE
        if (config.get("raw.dnsmasq") or "") == wanted:
            return
        if wanted:
            config["raw.dnsmasq"] = wanted
        else:
            config.pop("raw.dnsmasq", None)
        self.service.lxd.update_network(name, record.get("description") or "", config)

    # -- what this host already uses -----------------------------------

    def host_inventory(self):
        """What a new fabric has to avoid on this host. What `GET /api/fabric/host` serves.

        Every IPv4 subnet on an interface the daemon can see, plus every route
        the host has that is not ours -- a fabric over a VPN's range would
        shadow it just as surely as one over a bridge. Our own fabric bridges
        are left out of ``subnets``: their prefixes are in ``fabrics``, which
        a new fabric is checked against as a whole, and listing each /24 again
        would only repeat that.
        """
        mine = self.fabrics()
        subnets = []
        for interface, network in self.service._subnets_in_use():
            if network.version == 4 and interface not in mine:
                subnets.append({"interface": interface, "subnet": str(network)})
        seen = {entry["subnet"] for entry in subnets}
        for where, network in hostnet.host_routes():
            if str(network) not in seen and not any(
                    network.subnet_of(ipaddress.ip_network(f["prefix"]))
                    for f in mine.values()):
                subnets.append({"interface": where, "subnet": str(network)})
                seen.add(str(network))
        return {"node": self.cluster.local_name(),
                "networks": sorted(n.get("name") for n in self.service.lxd.list_networks()
                                   if n.get("name")),
                "subnets": subnets,
                "fabrics": mine}

    # -- the cluster: reading ------------------------------------------

    def _peers(self):
        return sorted(self.cluster._peers())

    def _ask_peers(self, call, timeout=STATUS_TIMEOUT):
        """{node: (True, answer) | (False, message)} for ``call(client)`` on every peer."""
        def ask(name):
            try:
                return name, (True, call(self.cluster.client(name, timeout=timeout)))
            except Exception as exc:              # any peer failure is that peer's
                return name, (False, getattr(exc, "message", str(exc)))
        return dict(self.cluster._fanout(self._peers(), ask))

    def _inventories(self):
        """({node: inventory}, {node: why not}) for every member, this one included."""
        hosts = {self.cluster.local_name(): self.host_inventory()}
        unreachable = {}
        for name, (ok, answer) in self._ask_peers(lambda c: c.fabric_host()).items():
            if ok and isinstance(answer, dict):
                hosts[name] = answer
            else:
                unreachable[name] = answer if not ok else "answered with nothing usable"
        return hosts, unreachable

    def definitions(self):
        """{name: prefix} for every fabric any member claims a subnet in.

        Where two members disagree on one fabric's prefix, this node's own
        word wins and otherwise the most common one does -- only for naming
        it, since `_peer_claims()` routes to none that disagree.
        """
        votes = {}
        for _, name, prefix, _, _ in self._claims():
            votes.setdefault(name, {}).setdefault(prefix, 0)
            votes[name][prefix] += 1
        mine = self.fabrics()
        return {name: mine[name]["prefix"] if name in mine
                else max(sorted(counts), key=lambda p: counts[p])
                for name, counts in votes.items()}

    def overview(self):
        """Every fabric across the cluster, and each node's part in it.

        What the Network tab shows. Built from each member's own `status()`,
        not from the claims alone, because a claim says what a node was told
        and only the node can say whether its bridge and routes are there.
        A member that does not answer is shown from its claims, as unknown.
        """
        local = self.status()
        answers = {local["node"]: (True, local)}
        answers.update(self._ask_peers(lambda c: c.fabric_status()))
        records = {m["name"]: m.get("fabric") or {} for m in self.cluster.members()}
        definitions = self.definitions()

        nodes = []
        for node in [local["node"]] + self._peers():
            ok, answer = answers.get(node, (False, "not asked"))
            answer = answer if ok and isinstance(answer, dict) else {}
            nodes.append({
                "node": node, "self": node == local["node"], "ok": bool(answer),
                "error": "" if answer else str(answers.get(node, (0, ""))[1] or ""),
                "privileged": bool(answer.get("privileged")),
                "pending": int(answer.get("pending") or 0),
                "check": str(answer.get("check") or ""),
                "via": str(answer.get("via") or records.get(node, {}).get("via") or ""),
            })

        fabrics = []
        for name in sorted(set(definitions) | {f["name"] for n in nodes if n["ok"]
                                               for f in answers[n["node"]][1]["fabrics"]}):
            prefix = definitions.get(name, "")
            members = []
            for entry in nodes:
                node = entry["node"]
                if entry["ok"]:
                    held = next((f for f in answers[node][1]["fabrics"]
                                 if f["name"] == name), None)
                    members.append(self._member_state(node, prefix, held))
                    continue
                claim = (records.get(node, {}).get("fabrics") or {}).get(name)
                members.append({
                    "node": node, "state": "unknown" if claim else "absent",
                    "detail": entry["error"] or "did not answer",
                    "subnet": (claim or {}).get("subnet", ""),
                    "prefix": (claim or {}).get("prefix", ""),
                    "gateway": "", "bridge_ready": False, "routes_ok": 0,
                    "routes_total": 0, "instances": []})
            fabrics.append({
                "name": name, "prefix": prefix, "nat": self._nat_of(name),
                "conflict": sorted({m["node"] for m in members
                                    if m["prefix"] and m["prefix"] != prefix}),
                "instances": sum(len(m["instances"]) for m in members),
                "members": members,
            })

        return {
            "node": local["node"],
            "clustered": self.cluster.in_cluster(),
            "local": local,
            "nodes": nodes,
            "fabrics": fabrics,
        }

    def _member_state(self, node, prefix, held):
        """One node's part in one fabric, as that node reported it."""
        if held is None:
            return {"node": node, "state": "absent", "detail": "not on this fabric",
                    "subnet": "", "prefix": "", "gateway": "", "bridge_ready": False,
                    "routes_ok": 0, "routes_total": 0, "instances": []}
        routes = held.get("routes") or []
        bad = [r for r in routes if r.get("state") != "ok"]
        if held.get("prefix") != prefix:
            state, detail = "conflict", "holds it as %s" % held.get("prefix")
        elif not held.get("bridge_ready"):
            state, detail = "bridge", "the bridge is missing or on the wrong subnet"
        elif bad:
            state = "routes"
            detail = "; ".join("%s route %s" % (r.get("node"), r.get("state")) for r in bad)
        else:
            state, detail = "ok", ""
        return {"node": node, "state": state, "detail": detail,
                "subnet": held.get("subnet", ""), "prefix": held.get("prefix", ""),
                "gateway": held.get("gateway", ""),
                "bridge_ready": bool(held.get("bridge_ready")),
                "routes_ok": len(routes) - len(bad), "routes_total": len(routes),
                "instances": list(held.get("instances") or [])}

    # -- the cluster: choosing a name and a prefix ---------------------

    def check(self, name="", prefix=""):
        """Would a fabric called ``name`` over ``prefix`` fit on every node?

        Asks every member what it already uses, because a block free here can
        be a VPN's on the next host, and a route to a /24 inside it would
        quietly take that host's traffic away. A blank name or prefix is
        filled with the first free one, which is how the UI proposes both.
        """
        hosts, unreachable = self._inventories()
        definitions = self.definitions()
        for inventory in hosts.values():
            for fabric, claim in (inventory.get("fabrics") or {}).items():
                definitions.setdefault(fabric, claim.get("prefix") or "")

        name = str(name or "").strip()
        suggested_name = not name
        if suggested_name:
            name = self._free_name(hosts, definitions)
        name_error = self._name_error(name, hosts, definitions)

        prefix = str(prefix or "").strip()
        suggested_prefix = not prefix
        conflicts = []
        if suggested_prefix:
            prefix = self._free_prefix(hosts, definitions)
            prefix_error = "" if prefix else (
                "No free /16 left in 10.0.0.0/8 or 172.16.0.0/12 on every node; "
                "give a smaller prefix, such as a /20, by hand.")
        else:
            try:
                prefix = str(clean_prefix(prefix))
                conflicts = self._overlaps(ipaddress.ip_network(prefix), hosts, definitions)
                prefix_error = "" if not conflicts else (
                    "%s overlaps %s." % (prefix, "; ".join(
                        "%s on %s (%s)" % (c["subnet"], c["node"], c["interface"])
                        for c in conflicts[:3])))
            except FabricError as exc:
                prefix_error = exc.message

        allocation = {}
        if prefix and not prefix_error:
            try:
                allocation = self.allocate(name, ipaddress.ip_network(prefix),
                                           members=sorted(hosts), hosts=hosts)
            except FabricError as exc:
                prefix_error = exc.message

        unreachable_error = ""
        if unreachable:
            unreachable_error = _unreachable_message(sorted(unreachable))
        # Every member takes a claim, so one in maintenance stops the fabric.
        held = [n for n in self.cluster.all_nodes() if self.cluster.maintenance_of(n)]
        maintenance_error = self.cluster._maintenance_message(
            held, "create a fabric") if held else ""
        return {
            "name": name, "suggested_name": suggested_name, "name_error": name_error,
            "prefix": prefix, "suggested_prefix": suggested_prefix,
            "prefix_error": prefix_error, "conflicts": conflicts,
            "unreachable": [{"node": n, "error": e} for n, e in sorted(unreachable.items())],
            "unreachable_error": unreachable_error,
            "maintenance_error": maintenance_error,
            "allocation": allocation,
            "ok": not (name_error or prefix_error or unreachable or maintenance_error),
        }

    def _name_error(self, name, hosts, definitions):
        try:
            clean_name(name)
        except FabricError as exc:
            return exc.message
        if name in definitions:
            return "There is already a fabric called '%s'." % name
        taken = sorted(node for node, inventory in hosts.items()
                       if name in (inventory.get("networks") or []))
        if taken:
            return ("%s already %s an interface called '%s', and the fabric's "
                    "bridge takes its name." % (", ".join(taken),
                                                "has" if len(taken) == 1 else "have", name))
        return ""

    def _free_name(self, hosts, definitions):
        taken = set(definitions)
        for inventory in hosts.values():
            taken.update(inventory.get("networks") or [])
        index = 0
        while "%s%d" % (NAME_STEM, index) in taken:
            index += 1
        return "%s%d" % (NAME_STEM, index)

    def _overlaps(self, prefix, hosts, definitions, nodes=None):
        """[{node, interface, subnet}] of everything ``prefix`` would collide with."""
        found = []
        for fabric, other in sorted(definitions.items()):
            try:
                if prefix.overlaps(ipaddress.ip_network(other)):
                    found.append({"node": "the cluster", "interface": "fabric %s" % fabric,
                                  "subnet": other})
            except ValueError:
                continue
        for node, inventory in sorted(hosts.items()):
            if nodes is not None and node not in nodes:
                continue
            for entry in inventory.get("subnets") or []:
                try:
                    used = ipaddress.ip_network(entry.get("subnet"), strict=False)
                except ValueError:
                    continue
                if used.version == 4 and prefix.overlaps(used):
                    found.append({"node": node, "interface": entry.get("interface") or "",
                                  "subnet": str(used)})
        return found

    def _free_prefix(self, hosts, definitions):
        for candidate in _CANDIDATE_PREFIXES:
            if not self._overlaps(ipaddress.ip_network(candidate), hosts, definitions):
                return candidate
        return ""

    def allocate(self, name, prefix, members=None, hosts=None):
        """{node: subnet} in fabric ``name`` for every member, keeping claims already made.

        A /24 that overlaps something on the node it would go to is skipped
        for that node -- only reachable when the prefix was checked against a
        different set of hosts, but cheap to hold to anyway.
        """
        members = list(members if members is not None
                       else [m["name"] for m in self.cluster.members()])
        hosts = hosts or {}
        assigned, taken = {}, set()
        for node, fabric, claim_prefix, subnet, _ in self._claims():
            if fabric != name or node not in members or claim_prefix != str(prefix):
                continue
            network = ipaddress.ip_network(subnet)
            if network not in taken:
                assigned[node] = str(network)
                taken.add(network)
        # .0 is the prefix's own network address and makes a confusing subnet
        # to explain, so hand out from .1 up.
        pool = list(prefix.subnets(new_prefix=NODE_BITS))[1:]
        for node in members:
            if node in assigned:
                continue
            used = []
            for entry in (hosts.get(node) or {}).get("subnets") or []:
                try:
                    used.append(ipaddress.ip_network(entry.get("subnet"), strict=False))
                except ValueError:
                    continue
            network = next((net for net in pool if net not in taken
                            and not any(net.overlaps(u) for u in used if u.version == 4)),
                           None)
            if network is None:
                raise FabricError("%s has no free /%d left for %s."
                                  % (prefix, NODE_BITS, node), 409)
            assigned[node] = str(network)
            taken.add(network)
        return assigned

    # -- the cluster: changing -----------------------------------------

    def create(self, name="", prefix="", nat=True):
        """Create a fabric on every member: a /24 each, a bridge each, routes between.

        Refused unless every member answers, because a member that did not
        could not be checked for what it already uses, and it would have no
        way to catch up later -- nothing reconciles a claim. Then this node
        first, so a failure here stops it before any peer is touched, and the
        peers side by side.
        """
        with self._change_lock:
            report = self.check(name, prefix)
            for key in ("maintenance_error", "unreachable_error", "name_error",
                        "prefix_error"):
                if report[key]:
                    raise FabricError(report[key], 409)
            name, prefix = report["name"], report["prefix"]
            results = self._hand_out(name, prefix, report["allocation"], bool(nat))
        return {"name": name, "prefix": prefix, "nat": bool(nat),
                "assigned": report["allocation"],
                "nodes": results, "ok": all(r["ok"] for r in results)}

    def extend(self, name):
        """Give every member that lacks one a subnet in fabric ``name``.

        For a node that joined without one, or that failed to take its claim
        when the fabric was made. Only the nodes being added are checked for
        what they use; the others already hold their /24.
        """
        with self._change_lock:
            definitions = self.definitions()
            if name not in definitions:
                raise FabricError("There is no fabric called '%s'." % name, 404)
            prefix = ipaddress.ip_network(definitions[name])
            hosts, unreachable = self._inventories()
            holding = {node for node, fabric, claim_prefix, _, _ in self._claims()
                       if fabric == name and claim_prefix == str(prefix)}
            members = [m["name"] for m in self.cluster.members()]
            missing = [node for node in members if node not in holding]
            self._refuse_members(missing, "extend %s there" % name)
            if not missing:
                return {"name": name, "prefix": str(prefix), "assigned": {},
                        "nodes": [], "ok": True}
            down = sorted(node for node in missing if node in unreachable)
            if down:
                raise FabricError(_unreachable_message(down), 409)
            others = {k: v for k, v in definitions.items() if k != name}
            conflicts = self._overlaps(prefix, hosts, others, nodes=missing)
            if conflicts:
                raise FabricError("%s overlaps %s." % (prefix, "; ".join(
                    "%s on %s (%s)" % (c["subnet"], c["node"], c["interface"])
                    for c in conflicts[:3])), 409)
            assignment = self.allocate(name, prefix, members=members, hosts=hosts)
            wanted = {node: assignment[node] for node in missing}
            results = self._hand_out(name, str(prefix), wanted, self._nat_of(name))
        return {"name": name, "prefix": str(prefix), "assigned": wanted,
                "nodes": results, "ok": all(r["ok"] for r in results)}

    def _hand_out(self, name, prefix, assignment, nat):
        """Tell each node in ``assignment`` its subnet. One result per node."""
        local = self.cluster.local_name()
        results = []
        if local in assignment:
            try:
                self.accept_claim(name, assignment[local], prefix, nat)
                results.append({"node": local, "subnet": assignment[local],
                                "ok": True, "error": ""})
            except (FabricError, ServiceError, hostnet.HostNetError) as exc:
                # This node first, and nothing told yet: stop here.
                raise FabricError("Could not create %s on this node: %s"
                                  % (name, exc.message), getattr(exc, "code", 500))

        def tell(node):
            try:
                self.cluster.client(node, timeout=CLAIM_TIMEOUT).set_fabric_claim(
                    name, assignment[node], prefix, nat)
                return {"node": node, "subnet": assignment[node], "ok": True, "error": ""}
            except Exception as exc:                # any peer failure is that peer's
                return {"node": node, "subnet": assignment[node], "ok": False,
                        "error": getattr(exc, "message", str(exc))}

        results += self.cluster._fanout(sorted(n for n in assignment if n != local), tell)
        # Level membership once more now every claim is on its node's record,
        # so each one routes to all the others without waiting for the next
        # sync. Best effort, like every push.
        try:
            self.cluster.sync_members()
        except Exception:
            pass
        return results

    def delete(self, name):
        """Take fabric ``name`` off every member: routes, claim and bridge.

        Refused while any instance is attached anywhere, for the same reason
        deleting a network is: an instance whose NIC goes keeps running cut
        off, which is easy to miss. Likewise while a template names it, whose
        launches would otherwise come up without it. A member that does not answer keeps its
        part and shows in `overview()` as the fabric's last member; deleting
        again once it is back finishes the job.
        """
        with self._change_lock:
            answers = {self.cluster.local_name(): (True, self.status())}
            answers.update(self._ask_peers(lambda c: c.fabric_status()))
            known = name in self.definitions() or any(
                ok and any(f["name"] == name for f in answer.get("fabrics") or [])
                for ok, answer in answers.values())
            if not known:
                raise FabricError("There is no fabric called '%s'." % name, 404)
            # It would come off every member, the one in maintenance included.
            self._refuse_members(self.cluster.all_nodes(), "delete a fabric")
            self.cluster.refuse_in_use("fabrics", "fabric", name)
            attached = []
            for node, (ok, answer) in sorted(answers.items()):
                if not ok:
                    continue
                for fabric in answer.get("fabrics") or []:
                    if fabric["name"] == name and fabric.get("instances"):
                        attached.append("%s (%s)" % (node, ", ".join(fabric["instances"])))
            if attached:
                raise FabricError(
                    "Instances are still on %s: %s. Detach or delete them first; "
                    "lemondx does not do it for you, because an instance whose NIC "
                    "is removed keeps running cut off." % (name, "; ".join(attached)), 409)

            local = self.cluster.local_name()

            def tell(node):
                try:
                    self.cluster.client(node, timeout=CLAIM_TIMEOUT).drop_fabric_claim(name)
                    return {"node": node, "ok": True, "error": ""}
                except Exception as exc:            # any peer failure is that peer's
                    return {"node": node, "ok": False,
                            "error": getattr(exc, "message", str(exc))}

            results = self.cluster._fanout(self._peers(), tell)
            try:
                self.leave(name)
                results.insert(0, {"node": local, "ok": True, "error": ""})
            except (FabricError, ServiceError, hostnet.HostNetError) as exc:
                results.insert(0, {"node": local, "ok": False, "error": exc.message})
            try:
                self.cluster.sync_members()
            except Exception:
                pass
        return {"name": name, "nodes": results, "ok": all(r["ok"] for r in results)}

    def accept_claim(self, name, subnet, prefix, nat=True):
        """Take a subnet allocated for this node in fabric ``name``, and stand it up.

        Refuses rather than reassigning: a collision here means the prefix
        overlaps something this host already uses, and quietly moving to
        another /24 would leave the rest of the cluster routing to the one it
        was told about.
        """
        # The coordinator refuses already; this covers one whose copy of our
        # record predates the mark.
        self.refuse_here("take a fabric subnet on this node")
        name = clean_name(name)
        prefix_network = clean_prefix(prefix)
        network = clean_subnet(subnet, prefix_network)
        # Before the claim is saved, so a refusal leaves nothing behind.
        self._refuse_foreign_network(name, self._bridge_record(name))
        # Checked and saved as one step, like leave(): two claims arriving
        # together (a join's offers and an `extend`) would otherwise each save
        # the map they read, and the later one would drop the earlier claim.
        with self._lock:
            fabrics = self.fabrics()
            held = fabrics.get(name)
            if held and held["prefix"] != str(prefix_network):
                raise FabricError("This node already holds %s as %s, not %s."
                                  % (name, held["prefix"], prefix_network), 409)
            for other, fabric in fabrics.items():
                if other != name and prefix_network.overlaps(
                        ipaddress.ip_network(fabric["prefix"])):
                    raise FabricError("%s overlaps fabric %s (%s) on this node."
                                      % (prefix_network, other, fabric["prefix"]), 409)
            self._refuse_collision(network, name)
            fabrics[name] = {"prefix": str(prefix_network), "subnet": str(network),
                             "nat": nat is not False}
            self._save(fabrics=fabrics)
        self.ensure_bridge(name)
        # Tell the rest of the cluster, rather than waiting for whoever created
        # the fabric to relay it: without this every *other* member would only
        # learn of this subnet on its next membership sync, and route to it
        # hours late. Best effort -- the claim is taken either way.
        try:
            self.cluster.sync_members()
        except Exception:
            pass
        self.reapply()
        return self.status()

    def _refuse_collision(self, network, name):
        """Refuse a claim that overlaps something already on this host."""
        for interface, used in self.service._subnets_in_use(exclude=name):
            if used.version == 4 and network.overlaps(used):
                raise FabricError(
                    "%s overlaps %s on %s, which is already on this host."
                    % (network, used, interface), 409)

    def leave(self, name, remove_bridge=True):
        """Stop being part of fabric ``name`` here: routes, claim and, by default, bridge.

        The claim goes first and the routes are then brought to what is left,
        which is what drops this fabric's -- the helper only ever programs the
        whole wanted state. The bridge goes last and only when nothing is on
        it; `delete()` has checked that already, and `stand_down()` keeps
        bridges because a node leaving its cluster should not cut its
        instances off on the way out.
        """
        fabrics = self.fabrics()
        if name not in fabrics:
            return {"node": self.cluster.local_name(), "name": name, "removed": [],
                    "bridge": ""}
        record = self._bridge_record(name)
        attached = self._bridge_instances(record)
        if remove_bridge and attached:
            raise FabricError("Instances are still on %s here: %s."
                              % (name, ", ".join(attached)), 409)
        with self._lock:
            fabrics.pop(name)
            self._save(fabrics=fabrics)
            removed = []
            if hostnet.available():
                removed, _ = self._program()
        bridge = "kept"
        if remove_bridge and record is not None:
            try:
                self.service.delete_network(name)
                bridge = "deleted"
            except Exception as exc:              # the claim is gone either way
                bridge = "kept: %s" % getattr(exc, "message", str(exc))
        try:
            self.cluster.sync_members()
        except Exception:
            pass
        return {"node": self.cluster.local_name(), "name": name,
                "removed": removed, "bridge": bridge}

    def disable(self):
        """Leave every fabric here, keeping the bridges. The node's half only."""
        results = []
        for name in sorted(self.fabrics()):
            results.append(self.leave(name, remove_bridge=False))
        # Anything of ours left in the table with no fabric to explain it.
        if not self.fabrics() and hostnet.available():
            try:
                self._program()
            except hostnet.HostNetError:
                pass
        return {"node": self.cluster.local_name(), "left": results}

    def stand_down(self):
        """Leave every fabric because this node left the cluster."""
        try:
            return self.disable()
        except (FabricError, hostnet.HostNetError, ServiceError, OSError):
            return None

    def enrolment_claims(self, joiner):
        """[{name, prefix, subnet}] for a node being admitted: one per fabric here.

        The joiner cannot allocate its own subnets -- nothing in two
        conflicting claims says which is right -- so the node admitting it
        picks them, the same way it hands over the credential. Nothing here
        knows what the joiner's host uses; its `accept_claim()` refuses a
        collision, and `extend()` is how that is put right afterwards.
        """
        offers = []
        members = [m["name"] for m in self.cluster.members()]
        if joiner not in members:
            members.append(joiner)
        for name, fabric in sorted(self.fabrics().items()):
            try:
                assignment = self.allocate(name, ipaddress.ip_network(fabric["prefix"]),
                                           members=members)
            except FabricError:
                continue
            if assignment.get(joiner):
                offers.append({"name": name, "prefix": fabric["prefix"],
                               "subnet": assignment[joiner], "nat": fabric["nat"]})
        return offers

    # -- instances -----------------------------------------------------

    def attach(self, name, fabric=True):
        """Give a running instance a NIC on a fabric, and an address on it."""
        self.refuse_here("attach instances to a fabric")
        bridge = self.require(fabric)
        # Read, pick a key and write as one step: two attaches reading the
        # same devices would pick the same eth<N>, and the PATCH below merges
        # by key, so the second would replace the first's NIC.
        with self._attach_lock:
            record = self.service.lxd.get_instance_record(name)
            devices = record.get("expanded_devices") or record.get("devices") or {}
            if any(d.get("type") == "nic" and (d.get("network") or d.get("parent")) == bridge
                   for d in devices.values()):
                raise FabricError("%s is already on %s." % (name, bridge), 409)
            key, device = self.service.fabric_nic(bridge, free_nic(devices))
            # PATCH merges the devices map, so this adds without disturbing eth0.
            self.service.lxd.update_instance(name, {"devices": {key: device}})
        self.configure_guest(name, key, bridge)
        return self.service.get_container(name)

    def configure_guest(self, name, iface, bridge):
        """Bring the instance's fabric interface up, and keep it up at boot.

        Reported rather than raised: the device is attached either way, and an
        image lemondx cannot configure is a thing to say plainly, not a reason
        to fail a launch that otherwise worked.
        """
        fabric = self.fabrics().get(bridge)
        if not fabric:
            return {"ok": False, "interface": iface,
                    "error": "This node is not on a fabric called '%s'." % bridge}
        script = _CONFIGURE_NIC % (iface, fabric["prefix"], self._gateway(fabric["subnet"]))
        try:
            result = self.service.lxd.exec_command(
                name, ["/bin/sh", "-c", script],
                timeout=CONFIGURE_TIMEOUT)
        except Exception as exc:
            return {"ok": False, "interface": iface,
                    "error": getattr(exc, "message", str(exc))}
        ok = (result or {}).get("exit_code") == 0
        return {"ok": ok, "interface": iface,
                "error": "" if ok else
                         "%s did not come up in the instance; its image may need "
                         "the interface configured by hand." % iface}

    def detach(self, name, fabric=None):
        """Take an instance's NIC off one fabric, or off every fabric.

        A whole-record PUT, because PATCH merges maps and so cannot remove a
        key. Everything else is sent back exactly as it was read.
        """
        self.refuse_here("detach instances from a fabric")
        bridges = {self.require(fabric)} if fabric else set(self.fabrics())
        record = self.service.lxd.get_instance_record(name)
        devices = dict(record.get("devices") or {})
        gone = [key for key, device in devices.items()
                if device.get("type") == "nic"
                and (device.get("network") or device.get("parent")) in bridges]
        if not gone:
            raise FabricError("%s has no NIC on %s."
                              % (name, ", ".join(sorted(bridges)) or "a fabric"), 404)
        for key in gone:
            devices.pop(key)
        self.service.lxd.replace_instance(name, dict(record, devices=devices))
        return self.service.get_container(name)

"""The fabric: routed, non-NAT networking between the containers of a cluster.

A managed bridge is NAT'd, which is right for reaching the internet and wrong
for reaching a container on another host -- the source address is rewritten and
there is no route back. The fabric is a second bridge per node with NAT off,
one /24 out of a cluster-wide prefix, and a host route to every other node's
/24. Each node keeps its existing bridge untouched, so a container gets its
internet through the NAT'd `eth0` exactly as before and its peers through
`eth1`. Only traffic between nodes is new.

That split is also why the fabric bridge must not hand out a default route
(`raw.dnsmasq` below): two default routes in a container is a coin toss over
whether its internet traffic leaves through a bridge that cannot NAT it.

This sits above `ClusterService` -- it needs the member list to know what the
routes should be -- and calls `hostnet.py` to program them, the same way
`cluster.py` calls `nodeclient.py`. `ClusterService` reaches it lazily, as it
already does for stacks, so nothing here is built on a node that never
federates.

The prefix is deliberately a /16 split into /24s. It bounds a cluster at 254
nodes, which is far past what a flat, leaderless design is good for anyway, and
it is what lets the sudoers rules name the prefix as two fixed octets and so
refuse any route outside it.
"""

from __future__ import annotations

import ipaddress
import threading
import time

from . import hostnet, store
from .service import ServiceError

SETTINGS_SECTION = "fabric"
SETTINGS_VERSION = 1

DEFAULT_SETTINGS = {
    # Off until somebody asks for it: the fabric needs privilege lemondx does
    # not otherwise hold, so it is never turned on by simply upgrading.
    "enabled": False,
    # Split into a /24 per node. Must be a /16 -- see the module docstring.
    "prefix": "10.100.0.0/16",
    "bridge": "lemonfab0",
    # Blank: the address on this host's default route. Set it where a host has
    # several interfaces and peers reach it on a particular one.
    "via": "",
    # This node's allocated /24. Written when a claim is accepted, not by hand.
    "subnet": "",
}

# What dnsmasq is told about the fabric bridge. Option 3 is the default route;
# offering it empty means "no gateway here", which is what keeps a container's
# internet traffic on the NAT'd bridge where it works.
NO_DEFAULT_ROUTE = "dhcp-option=3"

# Attaching a NIC gives an instance a link, not an address: almost every image
# configures only eth0, so a second interface comes up with nothing on it. This
# is the in-guest half of joining the fabric -- persistent where the image has
# somewhere to persist it, and brought up now either way.
#
# POSIX sh for the same reason bootstrap modules are: a minimal image has no
# bash. The branches mirror `modules/_prelude.sh` -- detect what the image
# actually uses rather than assuming a distribution.
#
# UseRoutes/UseDNS are off for networkd as a second line of defence: the bridge
# is configured not to offer a gateway (NO_DEFAULT_ROUTE), and if that ever
# failed, a default route here would send the instance's internet traffic out
# of a bridge that cannot NAT it.
_CONFIGURE_NIC = r"""
set -u
iface=%s
ip -o -4 addr show "$iface" 2>/dev/null | grep -q inet && exit 0
ip link set "$iface" up 2>/dev/null
if [ -d /etc/systemd/network ] && [ -d /run/systemd/system ]; then
    printf '[Match]
Name=%%s

[Network]
DHCP=ipv4

[DHCPv4]
UseRoutes=false
UseDNS=false
'         "$iface" > "/etc/systemd/network/50-lemondx-$iface.network"
    networkctl reload >/dev/null 2>&1 ||         systemctl restart systemd-networkd >/dev/null 2>&1
elif [ -f /etc/network/interfaces ]; then
    grep -q "^iface $iface " /etc/network/interfaces ||         printf '
auto %%s
iface %%s inet dhcp
' "$iface" "$iface"             >> /etc/network/interfaces
fi
i=0
while [ $i -lt 10 ]; do
    ip -o -4 addr show "$iface" 2>/dev/null | grep -q inet && exit 0
    ifup "$iface" >/dev/null 2>&1 ||         udhcpc -i "$iface" -n -q >/dev/null 2>&1 ||         dhclient -1 "$iface" >/dev/null 2>&1 ||         dhcpcd -n "$iface" >/dev/null 2>&1
    i=$((i + 1))
    sleep 1
done
ip -o -4 addr show "$iface" 2>/dev/null | grep -q inet
"""

# How long to let the guest pick up a lease. DHCP on a local bridge is fast;
# this is a ceiling on a guest that will never answer, not an expected wait.
CONFIGURE_TIMEOUT = 45


# Matching the reconciler: late enough that `serve` is listening, then a slow
# heartbeat, since every membership change already reapplies on the spot.
START_DELAY = 20
REAPPLY_INTERVAL = 3600

PREFIX_BITS = 16
NODE_BITS = 24


class FabricError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


# -- settings --------------------------------------------------------------


def clean_settings(raw):
    """Validate a saved fabric section. Raises FabricError if it is unusable."""
    settings = dict(DEFAULT_SETTINGS)
    for key, value in (raw or {}).items():
        if key == "version":
            continue
        if key not in DEFAULT_SETTINGS:
            raise FabricError("fabric settings: unknown key %r" % key)
        settings[key] = value

    settings["enabled"] = bool(settings["enabled"])
    settings["prefix"] = str(clean_prefix(settings["prefix"]))

    bridge = str(settings["bridge"] or "").strip()
    # The same 15-byte interface-name limit the daemon enforces on any bridge.
    from .service import VALID_NETWORK_NAME
    if not VALID_NETWORK_NAME.match(bridge):
        raise FabricError(
            "fabric settings: '%s' is not a usable bridge name (letters, digits "
            "and -, starting with a letter, up to 15 characters)." % bridge)
    settings["bridge"] = bridge

    via = str(settings["via"] or "").strip()
    if via:
        try:
            via = str(ipaddress.ip_address(via))
        except ValueError:
            raise FabricError("fabric settings: 'via' must be an IP address (%r)." % via)
    settings["via"] = via

    subnet = str(settings["subnet"] or "").strip()
    if subnet:
        prefix = ipaddress.ip_network(settings["prefix"])
        try:
            network = ipaddress.ip_network(subnet, strict=False)
        except ValueError:
            raise FabricError("fabric settings: '%s' is not a subnet." % subnet)
        if network.prefixlen != NODE_BITS or not network.subnet_of(prefix):
            raise FabricError(
                "fabric settings: %s is not a /%d inside %s."
                % (subnet, NODE_BITS, prefix))
        subnet = str(network)
    settings["subnet"] = subnet
    return settings


def clean_prefix(value):
    """The cluster prefix, as an ip_network. Raises FabricError if unusable."""
    try:
        prefix = ipaddress.ip_network(str(value or "").strip(), strict=False)
    except ValueError:
        raise FabricError("fabric settings: '%s' is not a subnet." % value)
    if prefix.version != 4:
        raise FabricError("The fabric is IPv4 only for now (%s)." % prefix)
    if prefix.prefixlen != PREFIX_BITS:
        raise FabricError(
            "The fabric prefix must be a /%d, so it splits into a /%d per node "
            "(%s is a /%d)." % (PREFIX_BITS, NODE_BITS, prefix, prefix.prefixlen))
    if not prefix.is_private:
        raise FabricError("The fabric prefix must be a private range (%s)." % prefix)
    return prefix


def load_settings():
    """Saved fabric settings, or the defaults. Raises FabricError if unusable.

    Like auth and cluster and unlike health: a fabric section that does not
    validate names the subnet this node claims cluster-wide, and starting with
    a silently different one would put two nodes on the same addresses.
    """
    try:
        raw = store.load_config(SETTINGS_SECTION)
    except ValueError as exc:
        raise FabricError("Cannot use the saved fabric settings: %s" % exc, 500)
    if raw is None:
        return dict(DEFAULT_SETTINGS)
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
    """This node's claim, for the record peers keep of it.

    A module function because `ClusterService.local_node()` needs it on every
    member listing, long before anything wants a whole FabricService.
    """
    try:
        settings = load_settings()
    except FabricError:
        return {"subnet": "", "via": ""}
    if not settings["enabled"] or not settings["subnet"]:
        return {"subnet": "", "via": ""}
    return {"subnet": settings["subnet"],
            "via": settings["via"] or str(default_via or "")}


# -- the service -----------------------------------------------------------


class FabricService:
    """One node's half of the fabric: its bridge, its claim and its routes."""

    def __init__(self, cluster):
        self.cluster = cluster
        self.service = cluster.service
        self._lock = threading.Lock()
        self._settings = None

    # -- settings ------------------------------------------------------

    def settings(self):
        if self._settings is None:
            self._settings = load_settings()
        return self._settings

    def reload_settings(self):
        self._settings = None
        return self.settings()

    def _save(self, **changes):
        settings = dict(self.settings())
        settings.update(changes)
        self._settings = save_settings(settings)
        return self._settings

    def enabled(self):
        settings = self.settings()
        return bool(settings["enabled"] and settings["subnet"])

    def bridge(self):
        return self.settings()["bridge"]

    def prefix(self):
        return ipaddress.ip_network(self.settings()["prefix"])

    def via(self):
        """The address peers route this node's subnet to."""
        return self.settings()["via"] or self.cluster.local_route_address()

    # -- what the routes should be -------------------------------------

    def _members(self):
        """Every member with a usable claim, this node included."""
        claims = []
        for member in self.cluster.members():
            fabric = member.get("fabric") or {}
            if fabric.get("subnet") and fabric.get("via"):
                claims.append((member["name"], fabric["subnet"], fabric["via"]))
        return claims

    def desired_routes(self):
        """{subnet: via} for every member but this one."""
        local = self.cluster.local_name()
        return {subnet: via for name, subnet, via in self._members() if name != local}

    # -- status --------------------------------------------------------

    def status(self):
        """Everything the API, the CLI and the UI show. The one payload."""
        settings = self.settings()
        subnet = settings["subnet"]
        enabled = self.enabled()
        try:
            current = hostnet.current_routes()
        except hostnet.HostNetError as exc:
            current = {}
            routes_error = exc.message
        else:
            routes_error = ""

        desired = self.desired_routes() if enabled else {}
        routes = []
        for name, peer_subnet, peer_via in sorted(self._members()):
            if name == self.cluster.local_name():
                continue
            if current.get(peer_subnet) == peer_via:
                state = "ok" if hostnet.reaches(peer_via) else "unreachable"
            elif peer_subnet in current:
                state = "wrong"
            else:
                state = "missing"
            routes.append({"node": name, "subnet": peer_subnet, "via": peer_via,
                           "state": state})

        pending = 0
        plan_error = ""
        if enabled:
            try:
                pending = len(self._plan(current, desired))
            except hostnet.HostNetError as exc:
                plan_error = exc.message

        return {
            "enabled": enabled,
            "configured": bool(settings["enabled"]),
            "node": self.cluster.local_name(),
            "prefix": settings["prefix"],
            "bridge": settings["bridge"],
            "subnet": subnet,
            "via": self.via() if enabled else settings["via"],
            "bridge_ready": self._bridge_ready(),
            "privileged": hostnet.available(settings["prefix"]),
            "routes": routes,
            "pending": pending,
            "error": routes_error or plan_error,
            "check": self.self_check() if enabled else "",
            "warnings": hostnet.diagnose(settings["prefix"], settings["bridge"]),
        }

    def self_check(self):
        """One sentence about why the fabric will not carry traffic, or ''.

        The precondition worth checking is the one this version is built on:
        every node on one L2. A peer that is not directly reachable gets a
        route that resolves to nothing, and the symptom is silent packet loss
        hours later rather than an error at setup.
        """
        if not self.enabled():
            return ""
        unreachable = [name for name, _, via in self._members()
                       if name != self.cluster.local_name() and not hostnet.reaches(via)]
        if unreachable:
            return ("%s is not on a network this host is directly attached to, so "
                    "routed traffic cannot reach it. This version of the fabric "
                    "needs every node on one L2 network."
                    % ", ".join(sorted(unreachable)))
        if not self._bridge_ready():
            return ("The bridge %s does not exist yet or does not hold %s; run "
                    "`lemondx fabric apply`." % (self.bridge(), self.settings()["subnet"]))
        return ""

    # -- the plan ------------------------------------------------------

    def _plan(self, current, desired):
        prefix = self.prefix()
        commands = hostnet.route_commands(desired, current, prefix)
        return commands + hostnet.forwarding_commands(self.bridge())

    def plan(self):
        """The host commands that would bring this node's routes up to date."""
        self._require_enabled()
        commands = self._plan(hostnet.current_routes(), self.desired_routes())
        return {"node": self.cluster.local_name(),
                "commands": [command.record() for command in commands],
                "text": hostnet.describe(commands),
                "privileged": hostnet.available(self.settings()["prefix"])}

    def apply(self):
        """Create the bridge if needed, then program the routes.

        The bridge goes through the daemon, which lemondx can already talk to.
        The routes are kernel state, so unless this process is already root
        they go to the helper -- which is handed the wanted state, not commands.
        """
        self._require_enabled()
        with self._lock:
            self.ensure_bridge()
            desired = self.desired_routes()
            if hostnet.is_root():
                commands = self._plan(hostnet.current_routes(), desired)
                results = hostnet.apply(commands) if commands else []
                failed = [r for r in results if not r["ok"]]
            else:
                answer = hostnet.delegate({"routes": desired, "bridge": self.bridge()})
                results = answer.get("applied") or []
                failed = [] if answer.get("ok") else [
                    r for r in results if not r["ok"]] or [
                    {"command": "fabric-helper", "why": "program the routes",
                     "ok": False, "error": answer.get("error") or "refused"}]
        return {"node": self.cluster.local_name(), "ok": not failed,
                "applied": results, "check": self.self_check(),
                "status": self.status()}

    def reapply(self):
        """Bring routes up to date, quietly. Called on every membership change.

        Never raises: this runs from `remember_members()` and from a background
        thread, where a node that cannot program a route must not take down the
        membership change that prompted it. The failure is visible in status().
        """
        try:
            if not self.enabled() or not hostnet.available(self.settings()["prefix"]):
                return None
            return self.apply()
        except (FabricError, hostnet.HostNetError, ServiceError, OSError):
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
                self.reapply()
                time.sleep(interval)

        thread = threading.Thread(target=run, name="fabric", daemon=True)
        thread.start()
        return thread

    # -- the bridge ----------------------------------------------------

    def _bridge_ready(self):
        settings = self.settings()
        if not settings["subnet"]:
            return False
        try:
            record = self.service.lxd.get_network(settings["bridge"])
        except Exception:
            return False
        address = (record.get("config") or {}).get("ipv4.address") or ""
        return address.startswith(self._gateway(settings["subnet"]))

    def _gateway(self, subnet):
        return str(next(ipaddress.ip_network(subnet).hosts()))

    def ensure_bridge(self):
        """Create or correct this node's fabric bridge."""
        settings = self.settings()
        name, subnet = settings["bridge"], settings["subnet"]
        if not subnet:
            raise FabricError("This node has no fabric subnet yet.", 409)
        address = "%s/%d" % (self._gateway(subnet), NODE_BITS)
        config = {"ipv4.address": address, "ipv4.nat": "false",
                  "ipv4.dhcp": "true", "ipv6.address": "none"}
        existing = {n.get("name") for n in self.service.lxd.list_networks()
                    if n.get("managed")}
        if name in existing:
            self.service.update_network(name, description=None, config=config)
        else:
            self.service.create_network(
                name, description="lemondx fabric: routed between nodes", config=config)
        self._suppress_default_route(name)
        return self.service.get_network(name)

    def _suppress_default_route(self, name):
        """Stop the fabric bridge handing out a default route.

        This goes straight to the daemon rather than through
        `ContainerService.update_network()`, because `raw.dnsmasq` is
        deliberately not in that method's allowlist: dnsmasq runs as root under
        the daemon and `dhcp-script=` would turn a network edit in the REST API
        into arbitrary host execution. lemondx setting one fixed value on its
        own bridge is a different thing from letting a request set any value.
        """
        record = self.service.lxd.get_network(name)
        config = dict(record.get("config") or {})
        if config.get("raw.dnsmasq") == NO_DEFAULT_ROUTE:
            return
        config["raw.dnsmasq"] = NO_DEFAULT_ROUTE
        self.service.lxd.update_network(name, record.get("description") or "", config)

    # -- allocation ----------------------------------------------------

    def allocate(self, members=None):
        """{node: subnet} for every member, keeping the claims already made.

        Allocation is coordinated rather than reconciled: the node asked to
        turn the fabric on works out the whole assignment and tells the others,
        because nothing in two conflicting claims says which is right.
        """
        prefix = self.prefix()
        members = members if members is not None else self.cluster.members()
        assigned, taken = {}, set()
        for member in members:
            claim = (member.get("fabric") or {}).get("subnet")
            if not claim:
                continue
            try:
                network = ipaddress.ip_network(claim)
            except ValueError:
                continue
            if network.subnet_of(prefix) and network not in taken:
                assigned[member["name"]] = str(network)
                taken.add(network)
        # .0 is the prefix's own network address and makes a confusing subnet
        # to explain, so hand out from .1 up.
        free = (net for net in list(prefix.subnets(new_prefix=NODE_BITS))[1:]
                if net not in taken)
        for member in members:
            if member["name"] in assigned:
                continue
            try:
                network = next(free)
            except StopIteration:
                raise FabricError(
                    "%s has no free /%d left for %s."
                    % (prefix, NODE_BITS, member["name"]), 409)
            assigned[member["name"]] = str(network)
            taken.add(network)
        return assigned

    def accept_claim(self, subnet, prefix=None):
        """Take a subnet allocated for this node, and stand the fabric up.

        Refuses rather than reassigning: a collision here means the cluster
        prefix overlaps something this host already uses, and quietly moving
        to another /24 would leave the rest of the cluster routing to the one
        it was told about.
        """
        settings = self.settings()
        prefix_network = clean_prefix(prefix or settings["prefix"])
        try:
            network = ipaddress.ip_network(str(subnet or "").strip(), strict=False)
        except ValueError:
            raise FabricError("'%s' is not a subnet." % subnet)
        if network.prefixlen != NODE_BITS or not network.subnet_of(prefix_network):
            raise FabricError("%s is not a /%d inside %s."
                              % (network, NODE_BITS, prefix_network))
        self._refuse_collision(network)
        self._save(enabled=True, prefix=str(prefix_network), subnet=str(network))
        self.ensure_bridge()
        # Tell the rest of the cluster, rather than waiting for whoever ran
        # `fabric enable` to relay it: the node that allocated pushes each
        # claim to its owner, so without this every *other* member would only
        # learn of this subnet on its next membership sync, and route to it
        # hours late. Best effort -- the claim is taken either way.
        try:
            self.cluster.sync_members()
        except Exception:
            pass
        self.reapply()
        return self.status()

    def _refuse_collision(self, network):
        """Refuse a claim that overlaps something already on this host."""
        for interface, used in self.service._subnets_in_use(exclude=self.bridge()):
            if used.version != 4:
                continue
            if network.overlaps(used):
                raise FabricError(
                    "%s overlaps %s on %s, which is already on this host. Pick a "
                    "fabric prefix that does not collide with it "
                    "(`lemondx configure fabric`)."
                    % (network, used, interface), 409)

    # -- turning it on and off -----------------------------------------

    def enable(self, prefix=None):
        """Allocate a subnet to every member and tell each one.

        Best effort towards the peers, like every other push: this node's own
        fabric is already standing, and a member that is off must not stop it.
        """
        prefix_network = clean_prefix(prefix or self.settings()["prefix"])
        self._save(prefix=str(prefix_network))
        members = self.cluster.members()
        assignment = self.allocate(members)
        local = self.cluster.local_name()

        results = []
        for member in members:
            name = member["name"]
            subnet = assignment[name]
            if name == local:
                continue
            try:
                self.cluster.client(name).set_fabric_claim(
                    subnet, str(prefix_network))
                results.append({"node": name, "subnet": subnet, "ok": True, "error": ""})
            except Exception as exc:                    # any peer failure, not fatal
                results.append({"node": name, "subnet": subnet, "ok": False,
                                "error": getattr(exc, "message", str(exc))})
        # This node last, so a prefix that collides here is reported before any
        # peer has been told to use it.
        self.accept_claim(assignment[local], str(prefix_network))
        self.cluster.sync_members()
        return {"prefix": str(prefix_network), "assigned": assignment,
                "peers": results, "status": self.status()}

    def disable(self):
        """Stop routing, and forget the claim. The bridge is left alone."""
        applied = []
        if hostnet.available(self.settings()["prefix"]):
            if hostnet.is_root():
                applied = hostnet.apply(hostnet.route_commands(
                    {}, hostnet.current_routes(), self.prefix()))
            else:
                # An empty route set is exactly "remove everything of ours".
                applied = (hostnet.delegate(
                    {"routes": {}, "bridge": self.bridge()}).get("applied") or [])
        self._save(enabled=False, subnet="")
        self.cluster.sync_members()
        return {"node": self.cluster.local_name(), "removed": applied,
                "bridge": self.bridge(), "status": self.status()}

    def stand_down(self):
        """Leave the fabric because this node left the cluster."""
        try:
            return self.disable()
        except (FabricError, hostnet.HostNetError, ServiceError, OSError):
            return None

    # -- instances -----------------------------------------------------

    def attach(self, name):
        """Give a running instance a NIC on the fabric, and an address on it."""
        self._require_enabled()
        key, device = self.service.fabric_nic(self.bridge())
        # PATCH merges the devices map, so this adds without disturbing eth0.
        self.service.lxd.update_instance(name, {"devices": {key: device}})
        self.configure_guest(name, key)
        return self.service.get_container(name)

    def configure_guest(self, name, iface="eth1"):
        """Bring the instance's fabric interface up, and keep it up at boot.

        Reported rather than raised: the device is attached either way, and an
        image lemondx cannot configure is a thing to say plainly, not a reason
        to fail a launch that otherwise worked.
        """
        try:
            result = self.service.lxd.exec_command(
                name, ["/bin/sh", "-c", _CONFIGURE_NIC % iface],
                timeout=CONFIGURE_TIMEOUT)
        except Exception as exc:
            return {"ok": False, "interface": iface,
                    "error": getattr(exc, "message", str(exc))}
        ok = (result or {}).get("exit_code") == 0
        return {"ok": ok, "interface": iface,
                "error": "" if ok else
                         "%s did not come up in the instance; its image may need "
                         "the interface configured by hand." % iface}

    def detach(self, name):
        """Take the fabric NIC off an instance.

        A whole-record PUT, because PATCH merges maps and so cannot remove a
        key. Everything else is sent back exactly as it was read.
        """
        record = self.service.lxd.get_instance_record(name)
        devices = dict(record.get("devices") or {})
        bridge = self.bridge()
        gone = [key for key, device in devices.items()
                if device.get("type") == "nic"
                and (device.get("network") or device.get("parent")) == bridge]
        if not gone:
            raise FabricError("%s has no NIC on %s." % (name, bridge), 404)
        for key in gone:
            devices.pop(key)
        self.service.lxd.replace_instance(name, dict(record, devices=devices))
        return self.service.get_container(name)

    # -- helpers -------------------------------------------------------

    def _require_enabled(self):
        if not self.enabled():
            raise FabricError(
                "The fabric is not on for this node. Run `lemondx fabric enable`.", 409)

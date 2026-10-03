"""The root half of the fabric: `lemondx fabric-helper`, run through sudo.

One exact command is all sudoers can usefully grant. Confining a grant with
argument patterns -- `ip route replace 10.100.*` -- is not portable: Ubuntu
25.10 ships sudo-rs, which matches command arguments literally and supports
neither wildcards nor regular expressions, while classic sudo supports both. A
rule that confines on one is wide open or broken on the other. Granting the
bare binary is worse still: `ip netns exec X sh` is a root shell.

So the grant names this, and the confinement lives here, in code that can be
read and tested rather than in a glob:

  * stdin carries a *wanted state* -- subnets and the host each belongs to --
    never a command. Nothing the caller sends is executed; this decides what to
    run from the state it was asked for.
  * every destination must be a /24 inside one of the fabric prefixes, which
    are read from the invoking user's own configuration, not from stdin, and
    every bridge named must be one of those fabrics' own.
  * the firewall -- what may cross into and out of each fabric, and what is
    NAT'd -- is built from that same configuration. stdin cannot add a rule.
  * every gateway must be an address on a directly-connected subnet, which is
    the same-L2 precondition the fabric is built on anyway.

The same command also puts containers on the LAN (`{"lan": {...}}` on stdin),
under rules of the same kind: a NIC to convert must be a physical, wired NIC
that is no bridge's port yet, the bridge a new name; only connections named
`lemondx-<bridge>` are ever removed; and Docker is only told to let through
bridges that /sys shows have a physical port. A conversion that leaves the
host without an address, or without the way out it had, is undone before
this returns.

What that buys is a bound on damage, not a boundary against the person running
lemondx: the group this is granted to is the daemon's admin group, which
`docs/security.md` already treats as root-equivalent. The point is that a bug,
or a request that reached the fabric code with a bad subnet in it, cannot
redirect the host's traffic.
"""

from __future__ import annotations

import ipaddress
import json
import os
import pwd
import re
import sys

from . import hostnet


def _fail(message, code=1):
    json.dump({"ok": False, "error": message, "applied": []}, sys.stdout)
    sys.stdout.write("\n")
    return code


def _caller_settings():
    """The fabric settings, read as the user who invoked sudo.

    Read from their configuration rather than taken from stdin, so the bound
    on what may be routed is not set by the same request it is bounding.
    """
    uid = os.environ.get("SUDO_UID")
    home = None
    if uid is not None:
        try:
            home = pwd.getpwuid(int(uid)).pw_dir
        except (KeyError, ValueError):
            home = None
    if home:
        # Read it the way store.py would for that user, without becoming them:
        # the data directory follows HOME, and sudo has replaced ours.
        os.environ["HOME"] = home
        os.environ.pop("XDG_DATA_HOME", None)
    from . import fabric as fabric_mod
    return fabric_mod.load_settings()


def main(argv=None):
    """Read the wanted state on stdin, program it, answer with what was done."""
    # The caller logs what is applied here, under the identity of whoever
    # asked; this process only knows it is root.
    hostnet.IN_HELPER = True
    if not hostnet.is_root():
        return _fail("The fabric helper only runs as root, through sudo.")
    try:
        wanted = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        return _fail("stdin is not JSON: %s" % exc)
    if not isinstance(wanted, dict):
        return _fail("stdin must be a JSON object.")
    if "lan" in wanted:
        return _lan(wanted["lan"])

    try:
        settings = _caller_settings()
        fabrics = settings["fabrics"]
        prefixes = [ipaddress.ip_network(f["prefix"]) for f in fabrics.values()]
    except Exception as exc:                    # noqa: BLE001
        return _fail("Cannot read the fabric settings: %s"
                     % getattr(exc, "message", str(exc)))

    bridges = wanted.get("bridges") or []
    if not isinstance(bridges, list):
        return _fail("'bridges' must be a list of names.")
    stray = [str(b) for b in bridges if str(b) not in fabrics]
    if stray:
        return _fail("Refusing to touch %s: not a fabric bridge on this node (%s)."
                     % (", ".join(stray), ", ".join(sorted(fabrics)) or "none"))

    try:
        routes = _clean_routes(wanted.get("routes"), prefixes)
    except ValueError as exc:
        return _fail(str(exc))

    try:
        current = hostnet.current_routes()
        from .fabric import firewall_spec, route_sources
        commands = hostnet.route_commands(routes, current, prefixes, route_sources(fabrics),
                                          hostnet.current_route_sources())
        commands += hostnet.forwarding_commands([str(b) for b in bridges])
        # Built from the settings alone, like the prefixes: nothing on stdin
        # has a say in what the firewall lets through.
        commands += hostnet.firewall_commands(firewall_spec(fabrics),
                                              hostnet.connected_subnets(settings["admit"]))
        commands += hostnet.docker_commands(sorted(fabrics))
        applied = hostnet.apply(commands)
    except hostnet.HostNetError as exc:
        return _fail(exc.message)

    failed = [record for record in applied if not record["ok"]]
    json.dump({"ok": not failed, "error": "", "applied": applied}, sys.stdout)
    sys.stdout.write("\n")
    return 1 if failed else 0


BRIDGE_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,14}$")


def _answer(applied, ok=None, error="", **extra):
    failed = [record for record in applied if not record["ok"]]
    ok = not failed if ok is None else ok
    json.dump(dict({"ok": ok, "error": error, "applied": applied}, **extra), sys.stdout)
    sys.stdout.write("\n")
    return 0 if ok else 1


def _lan(request):
    """Convert a NIC to a bridge, undo that, or keep Docker letting LAN bridges through."""
    if not isinstance(request, dict):
        return _fail("'lan' must be an object.")
    action = request.get("action")
    try:
        if action == "convert":
            return _convert(str(request.get("nic") or ""), str(request.get("bridge") or ""))
        if action == "revert":
            bridge = str(request.get("bridge") or "")
            if bridge not in hostnet.nm_lan_bridges():
                return _fail("%s is not a bridge lemondx made." % (bridge or "(none)"))
            return _answer(hostnet.apply(hostnet.lan_revert_plan(bridge)))
        if action == "shim":
            try:
                wanted = _shims(request.get("parents") or {})
            except ValueError as exc:
                return _fail(str(exc))
            return _answer(hostnet.apply(hostnet.shim_commands(wanted)))
        if action == "docker":
            bridges = request.get("bridges") or []
            if not isinstance(bridges, list):
                return _fail("'bridges' must be a list of names.")
            # A LAN bridge is one with a physical NIC as a port. Anything else
            # is refused: this opens Docker's FORWARD drop, and must not do so
            # for a bridge that merely has a plausible name.
            stray = [str(b) for b in bridges
                     if not BRIDGE_NAME.match(str(b)) or not hostnet.lan_bridge_ports(str(b))]
            if stray:
                return _fail("Refusing to let %s through: not a bridge with a "
                             "physical port." % ", ".join(stray))
            return _answer(hostnet.apply(hostnet.lan_docker_commands(
                sorted(str(b) for b in bridges))))
    except hostnet.HostNetError as exc:
        return _fail(exc.message)
    return _fail("Unknown LAN action %r." % action)


def _convert(nic, bridge):
    facts = hostnet.nic_facts(nic) if nic and "/" not in nic else {"exists": False}
    if not facts["exists"] or not facts["physical"]:
        return _fail("%s is not a network card on this host." % (nic or "(none)"))
    if facts["wireless"]:
        return _fail("%s is wireless; an access point drops frames from any MAC that "
                     "did not associate with it, so it cannot be bridged." % nic)
    if facts["master"]:
        return _fail("%s is already a port of %s." % (nic, facts["master"]))
    if not BRIDGE_NAME.match(bridge) or hostnet.nic_facts(bridge)["exists"]:
        return _fail("%s is not a free interface name for the bridge." % (bridge or "(none)"))

    need_default = nic in hostnet.default_route_devices()
    commands, previous = hostnet.lan_convert_plan(nic, bridge)
    applied = hostnet.apply(commands)
    if all(r["ok"] for r in applied) and hostnet.lan_link_up(bridge, need_default):
        return _answer(applied, previous=previous)
    # Whatever got half done is undone, and the NIC's own connection brought
    # back: a host left without its address is one nobody can reach to fix.
    why = next((r["error"] for r in applied if not r["ok"]), "") or (
        "%s came up without %s" % (bridge, "an address and a default route"
                                   if need_default else "an address"))
    undo = hostnet.apply_all(hostnet.lan_revert_plan(bridge, previous))
    return _answer(applied + undo, ok=False,
                   error="Could not move %s onto %s (%s); put back as it was."
                   % (nic, bridge, why), rolled_back=True)


def _shims(raw):
    """{nic: [address]} for `shim`, every NIC wired and every address on its LAN."""
    if not isinstance(raw, dict) or len(raw) > 16:
        raise ValueError("'parents' must be an object of NIC -> addresses.")
    wanted = {}
    for nic, addresses in raw.items():
        nic = str(nic)
        facts = hostnet.nic_facts(nic) if "/" not in nic else {"exists": False}
        if not facts["exists"] or not facts["physical"] or facts["wireless"]:
            raise ValueError("%s is not a wired network card on this host." % nic)
        subnets = hostnet.nic_subnets(nic)
        if not isinstance(addresses, list) or len(addresses) > 1024:
            raise ValueError("The addresses for %s must be a list." % nic)
        kept = []
        for address in addresses:
            try:
                ip = ipaddress.ip_address(str(address))
            except ValueError:
                raise ValueError("'%s' is not an address." % address)
            # Only a neighbour on the NIC's own LAN: this is a host route,
            # and the one thing it must not do is capture anywhere else.
            if not any(ip in subnet for subnet in subnets):
                raise ValueError("Refusing %s: it is not on %s's LAN." % (ip, nic))
            kept.append(str(ip))
        wanted[nic] = sorted(set(kept))
    return wanted


def _clean_routes(raw, prefixes):
    """{subnet: gateway}, every one of them inside a fabric and reachable."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("'routes' must be an object of subnet -> gateway.")
    if len(raw) > 512:
        raise ValueError("Refusing %d routes; a fabric has one per node." % len(raw))
    links = hostnet.link_subnets()
    routes = {}
    for destination, gateway in raw.items():
        try:
            network = ipaddress.ip_network(str(destination), strict=False)
        except ValueError:
            raise ValueError("'%s' is not a subnet." % destination)
        if network.version != 4 or network.prefixlen != 24:
            raise ValueError("%s is not an IPv4 /24." % network)
        if not any(network.subnet_of(prefix) for prefix in prefixes):
            raise ValueError(
                "Refusing to route %s: it is outside every fabric on this node (%s)."
                % (network, ", ".join(str(p) for p in prefixes) or "none"))
        try:
            address = ipaddress.ip_address(str(gateway))
        except ValueError:
            raise ValueError("'%s' is not an address to route %s to."
                             % (gateway, network))
        # The node has to be on a network this host is attached to. The fabric
        # is routed, not tunnelled, so a gateway anywhere else is a route that
        # silently drops -- and it is the one field naming a host off-cluster.
        if not any(address in link for link in links):
            raise ValueError(
                "Refusing to route %s via %s: that address is not on a network "
                "this host is directly attached to." % (network, address))
        routes[str(network)] = str(address)
    return routes

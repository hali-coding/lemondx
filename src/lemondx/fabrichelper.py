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
  * every destination must be a /24 inside the fabric prefix, which is read
    from the invoking user's own configuration, not from stdin.
  * every gateway must be an address on a directly-connected subnet, which is
    the same-L2 precondition the fabric is built on anyway.

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
import sys

from . import hostnet


def _fail(message, code=1):
    json.dump({"ok": False, "error": message, "applied": []}, sys.stdout)
    sys.stdout.write("\n")
    return code


def _caller_prefix():
    """The fabric prefix, read as the user who invoked sudo.

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
    if not hostnet.is_root():
        return _fail("The fabric helper only runs as root, through sudo.")
    try:
        wanted = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        return _fail("stdin is not JSON: %s" % exc)
    if not isinstance(wanted, dict):
        return _fail("stdin must be a JSON object.")

    try:
        settings = _caller_prefix()
        prefix = ipaddress.ip_network(settings["prefix"])
    except Exception as exc:                    # noqa: BLE001
        return _fail("Cannot read the fabric settings: %s"
                     % getattr(exc, "message", str(exc)))

    bridge = str(wanted.get("bridge") or settings["bridge"])
    if bridge != settings["bridge"]:
        return _fail("Refusing to touch '%s': this node's fabric bridge is '%s'."
                     % (bridge, settings["bridge"]))

    try:
        routes = _clean_routes(wanted.get("routes"), prefix)
    except ValueError as exc:
        return _fail(str(exc))

    try:
        current = hostnet.current_routes()
        commands = hostnet.route_commands(routes, current, prefix)
        commands += hostnet.forwarding_commands(bridge)
        applied = hostnet.apply(commands)
    except hostnet.HostNetError as exc:
        return _fail(exc.message)

    failed = [record for record in applied if not record["ok"]]
    json.dump({"ok": not failed, "error": "", "applied": applied}, sys.stdout)
    sys.stdout.write("\n")
    return 1 if failed else 0


def _clean_routes(raw, prefix):
    """{subnet: gateway}, every one of them inside the fabric and reachable."""
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
        if not network.subnet_of(prefix):
            raise ValueError(
                "Refusing to route %s: it is outside this cluster's fabric (%s)."
                % (network, prefix))
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

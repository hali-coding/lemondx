"""Host networking: the routes that carry container traffic between nodes.

Everywhere else lemondx talks to the daemon over its unix socket and never
touches the host. This module is the exception, and it is deliberately the only
one: a route between two nodes is kernel state that no container API can set,
so it has to be programmed with `ip`, and that needs root.

Root comes from `sudo -n` running one exact command: `lemondx fabric-helper`,
which reads a *description of the wanted state* on stdin and works out the
commands itself, as root. The caller never supplies argv.

That shape is forced rather than chosen. Confining a grant with argument
patterns -- `ip route replace 10.100.*` -- does not work portably: Ubuntu 25.10
ships sudo-rs, which supports neither wildcards nor regular expressions in
command arguments and matches them literally, while classic sudo supports both.
A rule that is confining on Debian is therefore either broken or wide open on
Ubuntu. Granting the bare binary is not an option either -- `ip netns exec X sh`
is a root shell -- so the only thing left to name in sudoers is a command of our
own, with the validation in Python where it can be read and tested.

Nothing here assumes the sudoers rule is installed: every read is unprivileged
(`ip route show` and `/proc/sys` need no root), so status always works and only
`apply()` can fail for want of privilege. That is what lets "print the commands
for you to run" be the same code path rather than a second one.

Like `lxd.py`, this layer is transport: it knows about routes and interfaces,
and nothing about clusters or who the peers are. `fabric.py` decides what the
routes should be.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import shlex
import shutil
import subprocess

from . import eventlog

# The route protocol number lemondx stamps on its own routes. The kernel keeps
# it on the route, so it doubles as ownership: a proto-133 route is one we put
# there and may remove, and anything else on the host is not ours to touch.
ROUTE_PROTO = "133"

SUDO = ("sudo", "-n")

# `ip` and `sysctl` live in /usr/sbin on most distributions, which is not on a
# non-root user's PATH on Debian. Resolving them ourselves also means we invoke
# the same absolute path the sudoers rules name -- sudo matches the path as
# given, and `ip` exists at /usr/sbin/ip, /usr/bin/ip and /sbin/ip.
_BINARY_PATH = ("/usr/sbin", "/usr/bin", "/sbin", "/bin", "/usr/local/sbin")

_COMMAND_TIMEOUT = 20


class HostNetError(Exception):
    """A host networking command failed, or cannot be run at all."""

    def __init__(self, message, code=500):
        super().__init__(message)
        self.message = message
        self.code = code


class Command:
    """One host command, with the reason it is in the plan."""

    def __init__(self, argv, why, privileged=True, stdin=None):
        self.argv = list(argv)
        self.why = why
        self.privileged = privileged
        self.stdin = stdin

    def shell(self):
        """The command as a person would type it, for the copyable plan."""
        parts = ["sudo"] if self.privileged else []
        # Quoted, so an argument with a space -- NetworkManager's address
        # lists have one -- still pastes as one argument.
        line = " ".join(parts + [shlex.quote(a) for a in self.argv])
        if self.stdin is not None:
            line += " <<'EOF'\n%sEOF" % self.stdin
        return line

    def record(self):
        return {"command": self.shell(), "why": self.why}


def _binary(name):
    found = shutil.which(name) or shutil.which(name, path=os.pathsep.join(_BINARY_PATH))
    if not found:
        raise HostNetError(
            "%s is not installed, so lemondx cannot manage host routes. "
            "Install iproute2 (see docs/networking.md)." % name, 503)
    return found


def ip_binary():
    return _binary("ip")


def sysctl_binary():
    return _binary("sysctl")


def nft_binary():
    return _binary("nft")


def _run(argv, timeout=_COMMAND_TIMEOUT, stdin=None):
    """Run a command, returning (returncode, stdout, stderr).

    Never raises for a non-zero exit -- a command that ran and failed is a
    result, the same way a failed exec in `lxd.py` is.
    """
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              input=stdin)
    except FileNotFoundError:
        raise HostNetError("%s is not installed." % argv[0], 503)
    except subprocess.SubprocessError as exc:
        raise HostNetError("%s failed: %s" % (os.path.basename(argv[0]), exc), 500)
    return done.returncode, done.stdout or "", done.stderr or ""


# -- reading the host (no privilege needed) --------------------------------


def current_routes():
    """{destination: gateway} for the routes lemondx put there.

    Filtered by our own protocol number, so a host's own routes are invisible
    here and can never be mistaken for ours and removed.
    """
    argv = [ip_binary(), "-j", "route", "show", "proto", ROUTE_PROTO]
    code, out, _ = _run(argv)
    if code == 0:
        try:
            return {entry["dst"]: entry.get("gateway", "")
                    for entry in json.loads(out or "[]") if entry.get("dst")}
        except (ValueError, TypeError, KeyError):
            pass  # fall through to the text form
    # iproute2 gained -j in 4.13; parse the plain output where it is missing or
    # produced something unexpected.
    code, out, err = _run([ip_binary(), "route", "show", "proto", ROUTE_PROTO])
    if code != 0:
        raise HostNetError("Cannot read the routing table: %s" % err.strip()[-200:], 500)
    routes = {}
    for line in out.splitlines():
        fields = line.split()
        if not fields:
            continue
        after = fields[fields.index("via") + 1:] if "via" in fields else []
        routes[fields[0]] = after[0] if after else ""
    return routes


def current_route_sources():
    """{destination: source address} for our routes that carry a `src` hint."""
    code, out, _ = _run([ip_binary(), "-j", "route", "show", "proto", ROUTE_PROTO])
    try:
        return {e["dst"]: e["prefsrc"] for e in json.loads(out or "[]")
                if e.get("dst") and e.get("prefsrc")} if code == 0 else {}
    except (ValueError, TypeError):
        return {}


def local_addresses():
    """Every IPv4 address on this host's interfaces."""
    code, out, _ = _run([ip_binary(), "-j", "-4", "addr", "show"])
    try:
        return {info.get("local") for entry in json.loads(out or "[]")
                for info in entry.get("addr_info") or [] if info.get("local")} \
            if code == 0 else set()
    except ValueError:
        return set()


def forwarding(bridge):
    """{'ip_forward': bool, 'bridge': bool|None} -- None when there is no bridge yet."""
    return {"ip_forward": _sysctl_on("net/ipv4/ip_forward"),
            "bridge": _sysctl_on("net/ipv4/conf/%s/forwarding" % bridge) if bridge else None}


def _sysctl_on(relative):
    """Read a sysctl through /proc, which needs no privilege."""
    try:
        with open("/proc/sys/%s" % relative, encoding="ascii") as handle:
            return handle.read().strip() == "1"
    except OSError:
        return None


def link_subnets():
    """Every directly-connected IPv4 subnet, as ip_network objects.

    A peer is only reachable by a plain route if its address is on one of
    these -- that is the same-L2 precondition, and checking it here is what
    turns "the fabric silently drops packets" into a sentence at setup time.
    """
    code, out, _ = _run([ip_binary(), "-j", "-4", "route", "show", "scope", "link"])
    subnets = []
    if code == 0:
        try:
            entries = json.loads(out or "[]")
        except ValueError:
            entries = []
        for entry in entries:
            try:
                subnets.append(ipaddress.ip_network(entry.get("dst", ""), strict=False))
            except ValueError:
                continue
    return subnets


def host_routes():
    """[(where, subnet)] for every IPv4 route on the host that is not ours.

    What the daemon cannot see: a VPN, a static route to another site, a
    network reached through a router on the LAN. A fabric placed over any of
    them would shadow it -- the /24 routes are more specific -- so choosing a
    free block has to know about them as well as about interfaces. Default
    routes are left out; they overlap everything and mean nothing here.
    """
    try:
        code, out, _ = _run([ip_binary(), "-j", "-4", "route", "show"])
    except HostNetError:
        return []
    found = []
    if code != 0:
        return found
    try:
        entries = json.loads(out or "[]")
    except ValueError:
        return found
    for entry in entries:
        destination = entry.get("dst", "")
        if destination in ("", "default") or str(entry.get("protocol")) in (
                ROUTE_PROTO, "133"):
            continue
        try:
            network = ipaddress.ip_network(destination, strict=False)
        except ValueError:
            continue
        where = entry.get("dev") or ""
        if entry.get("gateway"):
            where = "route via %s" % entry["gateway"]
        found.append((where or "a host route", network))
    return found


def reaches(address):
    """Is this address on a directly-connected subnet?"""
    try:
        host = ipaddress.ip_address(str(address).strip())
    except ValueError:
        return False
    return any(host in subnet for subnet in link_subnets())


# -- building a plan -------------------------------------------------------


def route_commands(desired, current, prefixes, sources=None, current_sources=None):
    """Commands to turn `current` into `desired`. Both are {destination: gateway}.

    ``sources`` is {prefix: address}: this node's own address in each fabric,
    its bridge's gateway. A route into that prefix carries it as its `src`,
    so what the host itself sends to a peer's instance leaves from inside the
    fabric, not from the host's LAN address -- which the peer's firewall
    drops, being closed to everything outside the prefix. Only forwarded
    traffic ignores the hint, so instances are unaffected. An address that is
    not on the host yet (the bridge still coming up) is left out: the kernel
    refuses a route with a source it does not hold, and the next pass adds it.
    ``current_sources`` is what the table has, so a route whose `src` is wrong
    or missing is replaced even when its gateway is right.

    Every route added is checked against the fabric prefixes before it reaches
    argv; the helper refuses anything outside them too, but a check here means
    a plan printed for a person to run holds to the same rule.

    Removal is not held to the prefixes, only to ownership: `current` is read
    by our own protocol number, so every route in it is one lemondx put there.
    That has to be so for a fabric to be deleted -- once it is gone from the
    settings its prefix is no longer one of ours, and a check against the
    remaining ones would strand its routes in the table for good.
    """
    binary = ip_binary()
    current_sources = current_sources or {}
    held = local_addresses() if sources else set()
    commands = []
    for destination in sorted(desired):
        gateway = desired[destination]
        _inside(destination, prefixes)
        source = _source_for(destination, sources or {}, held)
        if current.get(destination) == gateway and current_sources.get(destination) == source:
            continue  # already programmed, and pointing at the right node
        commands.append(Command(
            [binary, "route", "replace", destination, "via", gateway]
            + (["src", source] if source else []) + ["proto", ROUTE_PROTO],
            "route %s to %s%s" % (destination, gateway,
                                  ", sending from %s" % source if source else "")))
    for destination in sorted(current):
        if destination in desired:
            continue
        _subnet(destination)
        commands.append(Command(
            [binary, "route", "del", destination, "proto", ROUTE_PROTO],
            "drop the route to %s, which no fabric member holds any more"
            % destination))
    return commands


def _source_for(destination, sources, held):
    """This node's address in the fabric ``destination`` is in, if the host holds it."""
    network = _subnet(destination)
    for prefix, address in sources.items():
        try:
            inside = network.subnet_of(ipaddress.ip_network(prefix))
        except (ValueError, TypeError):
            continue
        if inside and address in held:
            return address
    return None


def forwarding_commands(bridges):
    """Turn forwarding on, but only where it is actually off.

    On a host that already runs containers this is usually a no-op, which
    keeps sudo out of the common path entirely.
    """
    binary = sysctl_binary()
    commands = []
    if _sysctl_on("net/ipv4/ip_forward") is False:
        commands.append(Command([binary, "-w", "net.ipv4.ip_forward=1"],
                                "IPv4 forwarding is off, so nothing would cross hosts"))
    for bridge in bridges:
        if forwarding(bridge)["bridge"] is False:
            commands.append(Command(
                [binary, "-w", "net.ipv4.conf.%s.forwarding=1" % bridge],
                "forwarding is off on %s" % bridge))
    return commands


# The nftables table lemondx owns. Its own table, beside the daemon's `inet
# lxd` or `inet incus`, so it is replaced whole on every apply and never
# edited rule by rule -- and so the daemon, which rewrites its own table
# whenever a network changes, never touches ours.
FIREWALL_TABLE = "lemondx"


def firewall_ruleset(fabrics, admit=()):
    """The whole `ip lemondx` table for ``fabrics``, as nft input.

    ``fabrics`` is [{"bridge", "prefix", "subnet", "nat"}], one per fabric this
    node is on. ``admit`` is the LAN subnets let in from outside the fabric:
    where the cluster's macvlan instances live, so they reach every fabric
    (see `docs/lan.md`). Replies to them are let out whatever the fabric's NAT.
    Two things, per fabric:

    * **Routed only within the fabric.** Into the bridge, only from the
      fabric's own prefix -- or a reply to something the instance started.
      Out of it, never to another fabric's prefix, and with NAT off never
      anywhere but its own. That is what makes two fabrics two networks
      rather than one with two names, and keeps a LAN host from reaching in.
    * **NAT'd everywhere else.** The daemon's own `ipv4.nat` cannot do this:
      it masquerades everything leaving the bridge's subnet, peers' /24s
      included, so a peer would see this host's address instead of the
      instance's. lemondx masquerades itself, excluding the prefix, and
      leaves the daemon's NAT off on fabric bridges.

    Replaced whole in one transaction: declaring the table first makes the
    delete safe when it does not exist yet.
    """
    lines = ["table ip %s" % FIREWALL_TABLE, "delete table ip %s" % FIREWALL_TABLE]
    if not fabrics:
        return "\n".join(lines) + "\n"
    forward, postrouting = [], []
    for fabric in fabrics:
        bridge, prefix = fabric["bridge"], fabric["prefix"]
        others = [f["prefix"] for f in fabrics if f["bridge"] != bridge]
        forward.append('oifname "%s" ct state established,related accept' % bridge)
        if admit:
            forward.append('oifname "%s" ip saddr { %s } accept'
                           % (bridge, ", ".join(admit)))
        forward.append('oifname "%s" ip saddr != %s drop' % (bridge, prefix))
        # A reply to something a LAN instance opened, which a fabric without
        # NAT would otherwise drop as leaving the fabric.
        forward.append('iifname "%s" ct state established,related accept' % bridge)
        if not fabric["nat"]:
            forward.append('iifname "%s" ip daddr != %s drop' % (bridge, prefix))
        elif others:
            forward.append('iifname "%s" ip daddr { %s } drop'
                           % (bridge, ", ".join(others)))
        if fabric["nat"]:
            postrouting.append('ip saddr %s ip daddr != %s oifname != "%s" masquerade'
                               % (fabric["subnet"], prefix, bridge))
    lines.append("table ip %s {" % FIREWALL_TABLE)
    lines.append("  chain forward {")
    lines.append("    type filter hook forward priority filter; policy accept;")
    lines.extend("    " + rule for rule in forward)
    lines.append("  }")
    if postrouting:
        lines.append("  chain postrouting {")
        lines.append("    type nat hook postrouting priority srcnat; policy accept;")
        lines.extend("    " + rule for rule in postrouting)
        lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def firewall_commands(fabrics, admit=()):
    """The one command that puts the fabric firewall in place.

    Always in the plan, never compared against what is loaded: reading an
    nftables table needs the same privilege as writing one, so an
    unprivileged `status()` cannot know, and replacing the table with itself
    is harmless.
    """
    return [Command([nft_binary(), "-f", "-"],
                    "isolate %d fabric(s) from each other and NAT what leaves them"
                    % len(fabrics) if fabrics else "remove lemondx's fabric firewall",
                    stdin=firewall_ruleset(fabrics, admit))]


# Docker sets the FORWARD policy to DROP and gives users one chain to open it
# with. A drop there is final whatever our own table says, so on a Docker host
# a fabric carries nothing until its bridge is let through -- a failure that
# looks exactly like a working fabric. Accepting here does not undo the
# isolation above: our table is a base chain of its own, and a drop in it
# still drops. The comment is how our rules are told from anyone else's.
DOCKER_CHAIN = "DOCKER-USER"
DOCKER_TAG = "lemondx-fabric"


def docker_commands(bridges, tag=DOCKER_TAG, what="a fabric"):
    """Keep DOCKER-USER letting ``bridges`` through, and nothing that is gone.

    Root only: listing an iptables chain needs the privilege changing one
    does, so this is computed by the helper rather than in the plan, and a
    host without Docker's chain gets nothing at all.
    """
    try:
        iptables = _binary("iptables")
    except HostNetError:
        return []
    code, out, _ = _run([iptables, "-S", DOCKER_CHAIN])
    if code != 0:
        return []
    have = set()
    commands = []
    for line in out.splitlines():
        fields = line.split()
        if tag not in fields or fields[:2] != ["-A", DOCKER_CHAIN]:
            continue
        way = next((f for f in fields if f in ("-i", "-o")), None)
        if way is None:
            continue
        bridge = fields[fields.index(way) + 1]
        if bridge in bridges:
            have.add((way, bridge))
        else:
            commands.append(Command([iptables, "-D", DOCKER_CHAIN] + fields[2:],
                                    "stop letting %s through Docker's FORWARD drop; "
                                    "it is no longer %s" % (bridge, what)))
    for bridge in bridges:
        for way in ("-i", "-o"):
            if (way, bridge) not in have:
                commands.append(Command(
                    [iptables, "-I", DOCKER_CHAIN, way, bridge, "-m", "comment",
                     "--comment", tag, "-j", "ACCEPT"],
                    "let %s through Docker's FORWARD drop" % bridge))
    return commands


def _subnet(destination):
    try:
        return ipaddress.ip_network(destination, strict=False)
    except ValueError:
        raise HostNetError("%s is not a subnet." % destination, 400)


def _inside(destination, prefixes):
    network = _subnet(destination)
    if not any(network.version == prefix.version and network.subnet_of(prefix)
               for prefix in prefixes):
        raise HostNetError(
            "Refusing to route %s: it is outside every fabric on this node (%s), "
            "and lemondx only adds routes inside them."
            % (destination, ", ".join(str(p) for p in prefixes) or "none"), 400)


def describe(commands):
    """The plan as copyable shell, for a host where lemondx cannot run it."""
    return "\n".join(command.shell() for command in commands)


# -- applying --------------------------------------------------------------


# Set by the fabric helper: its commands are logged by the process that asked
# for them (`delegate()`), which knows who that was; the helper, a fresh root
# process, does not.
IN_HELPER = False


def _log_applied(records, summary=None):
    for record in records:
        eventlog.event("change", "host.command",
                       level=logging.INFO if record["ok"] else logging.WARNING,
                       command=record["command"], why=record.get("why"),
                       result="ok" if record["ok"] else "failed", error=record.get("error"))
    if summary is not None:
        eventlog.event("change", "host.apply", level=logging.INFO if summary.get("ok")
                       else logging.WARNING, commands=len(records),
                       result="ok" if summary.get("ok") else "failed",
                       error=summary.get("error"))


def apply(commands):
    """Run a plan as root. Returns one record per command; stops at the first failure.

    Stopping matters: the later commands were computed against the state the
    earlier ones were supposed to produce, and running them anyway would leave
    a half-programmed table that the next pass reads as a different problem.

    This is the root half. An unprivileged process does not call it -- it calls
    `delegate()`, which hands the wanted state to the helper.
    """
    results = []
    for command in commands:
        # Long enough for NetworkManager's own --wait, which the LAN commands
        # pass; everything else finishes in a moment either way.
        code, _, err = _run(command.argv, stdin=command.stdin,
                            timeout=LAN_ACTIVATE_SECONDS + 15)
        results.append({"command": command.shell(), "why": command.why,
                        "ok": code == 0, "error": err.strip()[-300:] if code else ""})
        if code != 0:
            break
    if not IN_HELPER:
        _log_applied(results)
    return results


def apply_all(commands):
    """Run every command whatever the ones before did: for undoing.

    The opposite of `apply()`'s stop-at-the-first-failure, on purpose. A
    rollback's steps are each worth trying on their own -- the bridge's
    connection may never have been made, and the NIC's own must come back up
    regardless.
    """
    results = []
    for command in commands:
        code, _, err = _run(command.argv, stdin=command.stdin,
                            timeout=LAN_ACTIVATE_SECONDS + 15)
        results.append({"command": command.shell(), "why": command.why,
                        "ok": code == 0, "error": err.strip()[-300:] if code else ""})
    if not IN_HELPER:
        _log_applied(results)
    return results


def is_root():
    return os.geteuid() == 0


def helper_command():
    """The argv sudo is asked to run, and the one the sudoers rule names."""
    entry = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "lemondx")
    return [entry, "fabric-helper"]


def delegate(payload, timeout=120):
    """Ask the helper, as root, to bring this host to the wanted state.

    `payload` is data -- subnets, a bridge name -- never a command. The helper
    decides what to run, so a caller that has gone wrong can ask for a state
    that is refused, not for a command that is obeyed.
    """
    argv = list(SUDO) + helper_command()
    try:
        done = subprocess.run(argv, input=json.dumps(payload), capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError:
        raise HostNetError("sudo is not installed, so lemondx cannot program "
                           "host routes.", 503)
    except subprocess.SubprocessError as exc:
        raise HostNetError("The fabric helper failed: %s" % exc, 500)
    if done.returncode != 0 and not (done.stdout or "").strip():
        raise HostNetError(
            "sudo refused to run the fabric helper: %s. Install the rules from "
            "systemd/lemondx-fabric.sudoers (see docs/networking.md), or use "
            "`lemondx fabric plan` and apply it yourself."
            % ((done.stderr or "").strip()[-200:] or "no reason given"), 503)
    try:
        answer = json.loads(done.stdout or "{}")
    except ValueError:
        raise HostNetError("The fabric helper answered with something that is "
                           "not JSON: %s" % (done.stdout or "")[:200], 500)
    if isinstance(answer, dict):
        _log_applied([r for r in answer.get("applied") or [] if isinstance(r, dict)
                      and "command" in r], summary=answer)
    return answer


def available():
    """Can this process program a fabric route right now?

    Asks sudo whether the helper is permitted rather than running it: `sudo -l`
    resolves the policy for a command without executing it, so a capability
    probe never has a side effect on the routing table. Already being root is
    the other way to be able.
    """
    if is_root():
        return True
    try:
        ip_binary()
    except HostNetError:
        return False
    if not shutil.which("sudo"):
        return False
    code, _, _ = _run(["sudo", "-n", "-l"] + helper_command())
    return code == 0


def diagnose(bridges=()):
    """Warnings about this host's ability to carry fabric traffic, for startup.

    Follows `pam.diagnose()`: a list of plain sentences, never an exception,
    never a reason not to start. The fabric degrades to a printable plan.
    """
    warnings = []
    try:
        ip_binary()
    except HostNetError as exc:
        return [exc.message]
    if not shutil.which("sudo"):
        return ["sudo is not installed, so lemondx cannot program host routes. "
                "`lemondx fabric plan` prints the commands to run yourself."]
    if not available():
        if _user_namespace():
            # A user unit's sandbox: NoNewPrivileges=no alone would still
            # leave sudo failing, so saying only that sends people round twice.
            warnings.append(
                "This process runs in a user namespace (a systemd user unit's "
                "filesystem sandbox), where sudo cannot work: fabric routes "
                "cannot be programmed. Add the drop-in in docs/service.md (\"A "
                "user unit that can raise privilege\"), or run `lemondx fabric "
                "apply` yourself.")
        elif _no_new_privs():
            warnings.append(
                "NoNewPrivileges is set on this process, which stops sudo from "
                "raising privilege: fabric routes cannot be programmed. Set "
                "NoNewPrivileges=no for the service (see docs/service.md).")
        else:
            warnings.append(
                "sudo will not run the fabric helper without a password, so "
                "routes cannot be programmed. Install the rules from "
                "systemd/lemondx-fabric.sudoers (see docs/networking.md), or "
                "run `lemondx fabric plan` and apply it yourself.")
    if _sysctl_on("net/ipv4/ip_forward") is False:
        warnings.append("IPv4 forwarding is off on this host; `lemondx fabric apply` "
                        "turns it on.")
    # A host-wide FORWARD DROP is the one failure that looks like a working
    # fabric until traffic is tried. With the helper it is handled
    # (`docker_commands()`); without it, reading the policy needs root, so
    # flag the usual cause instead -- the same trade bootstrap.py makes for
    # lxdbr0.
    if (shutil.which("docker") or os.path.exists("/var/run/docker.sock")) \
            and not available():
        warnings.append(
            "Docker is installed here. It sets the FORWARD policy to DROP, which "
            "blocks traffic between nodes. If the fabric does not carry traffic, "
            "check `sudo iptables -S FORWARD | head -1` and allow the bridge%s:\n%s"
            % ("s" if len(bridges) > 1 else "",
               "\n".join("  sudo iptables -I DOCKER-USER -%s %s -j ACCEPT" % (way, bridge)
                         for bridge in (bridges or ["lemonfab0"]) for way in "io")))
    return warnings


def _no_new_privs():
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("NoNewPrivs:"):
                    return line.split()[1] == "1"
    except (OSError, IndexError):
        pass
    return False


def _user_namespace():
    """Whether root is unmapped here, as in a sandboxed systemd user unit.

    The initial namespace maps every id (`0 0 4294967295`); a user manager's
    sandbox maps only the service's own uid, and a setuid binary cannot
    become a uid that does not exist in its namespace.
    """
    try:
        with open("/proc/self/uid_map", encoding="ascii") as handle:
            return not any(line.split()[:1] == ["0"] for line in handle)
    except OSError:
        return False


# -- containers on the LAN ---------------------------------------------------
#
# Three ways to put an instance on the network a host NIC is on, with the
# router's DHCP rather than the daemon's. Two are the daemon's own networks
# (a bridge over a spare NIC, macvlan) and need nothing from here but a
# Docker exception. The third -- turning the NIC the host itself uses into a
# bridge port -- moves the host's address, and is done through
# NetworkManager, which then keeps it across reboots. Everything lemondx
# creates there is named with LAN_PREFIX: that name is its ownership, the way
# proto 133 is for routes, and nothing else is ever modified or removed.

LAN_PREFIX = "lemondx-"
LAN_DOCKER_TAG = "lemondx-lan"
# What the bridge takes over from the NIC's own connection, so the host keeps
# its addresses, gateway and DNS. Anything left out falls back to NM's
# default, which for a DHCP connection is what it had anyway.
NM_COPY = (
    "ipv4.method", "ipv4.addresses", "ipv4.gateway", "ipv4.dns", "ipv4.dns-search",
    "ipv4.routes", "ipv4.never-default", "ipv4.ignore-auto-dns", "ipv4.dhcp-client-id",
    "ipv6.method", "ipv6.addresses", "ipv6.gateway", "ipv6.dns", "ipv6.dns-search",
    "ipv6.routes", "ipv6.never-default", "ipv6.ignore-auto-dns",
)
# How long NetworkManager gets to bring the bridge up with an address before
# the conversion is undone.
LAN_ACTIVATE_SECONDS = 60


def _sys(nic, *parts):
    return os.path.join("/sys/class/net", nic, *parts)


def nic_facts(nic):
    """What /sys says about an interface; no privilege needed.

    ``physical`` means backed by a device (a NIC, not a veth or bridge),
    ``wireless`` rules it out -- an access point drops frames from MACs that
    did not associate with it, so neither a bridge nor macvlan works over it
    -- and ``master`` is the bridge it is already a port of, if any.
    """
    master = _sys(nic, "master")
    return {
        "exists": os.path.exists(_sys(nic)),
        "physical": os.path.exists(_sys(nic, "device")),
        "wireless": os.path.exists(_sys(nic, "wireless"))
        or os.path.exists(_sys(nic, "phy80211")),
        "bridge": os.path.exists(_sys(nic, "bridge")),
        "master": os.path.basename(os.path.realpath(master)) if os.path.islink(master) else "",
    }


def lan_bridge_ports(bridge):
    """The physical NICs that are ports of ``bridge``: what makes it a LAN bridge."""
    try:
        ports = os.listdir(_sys(bridge, "brif"))
    except OSError:
        return []
    return sorted(p for p in ports if nic_facts(p)["physical"])


def default_route_devices():
    """The interfaces the host's IPv4 default routes leave by."""
    code, out, _ = _run([ip_binary(), "-j", "-4", "route", "show", "default"])
    try:
        return sorted({e.get("dev") for e in json.loads(out or "[]") if e.get("dev")}) \
            if code == 0 else []
    except ValueError:
        return []


def default_route_device():
    """The interface the preferred IPv4 default route leaves by, or "".

    The one with the lowest metric, as the kernel chooses. This is how a NIC
    is named across a cluster without naming it: `eno1` here may be `enp3s0`
    on the next machine, but each has one way out.
    """
    code, out, _ = _run([ip_binary(), "-j", "-4", "route", "show", "default"])
    try:
        routes = [e for e in json.loads(out or "[]") if e.get("dev")] if code == 0 else []
    except ValueError:
        return ""
    return min(routes, key=lambda e: e.get("metric") or 0)["dev"] if routes else ""


def nmcli_binary():
    return _binary("nmcli")


def _nm_values(out):
    """One `nmcli -g` value per line, with its escaping undone."""
    return [line.replace("\\:", ":").replace("\\\\", "\\") for line in out.split("\n")]


def nm_device(nic):
    """{'connection', 'uuid', 'state'} for a NIC NetworkManager runs, or None.

    None covers both "NetworkManager is not here" and "it leaves this NIC
    alone": either way it is not a NIC lemondx can convert through it.
    """
    try:
        nmcli = nmcli_binary()
    except HostNetError:
        return None
    code, out, _ = _run([nmcli, "-g", "GENERAL.STATE,GENERAL.CONNECTION",
                         "device", "show", nic])
    if code != 0:
        return None
    values = _nm_values(out) + ["", ""]
    state, connection = values[0], values[1]
    if not connection or "unmanaged" in state:
        return None
    code, out, _ = _run([nmcli, "-g", "connection.uuid", "connection", "show", "id",
                         connection])
    uuid = _nm_values(out)[0] if code == 0 else ""
    return {"connection": connection, "uuid": uuid, "state": state}


def nm_lan_bridges():
    """{bridge: port NIC} for the bridges lemondx converted through NetworkManager."""
    try:
        nmcli = nmcli_binary()
    except HostNetError:
        return {}
    code, out, _ = _run([nmcli, "-t", "-f", "NAME,TYPE", "connection", "show"])
    if code != 0:
        return {}
    names = {line.rsplit(":", 1)[0] for line in out.splitlines()
             if line.startswith(LAN_PREFIX) and line.endswith(":bridge")}
    bridges = {}
    for name in names:
        bridge = name[len(LAN_PREFIX):]
        code, out, _ = _run([nmcli, "-g", "connection.interface-name", "connection",
                             "show", "id", "%s-port" % name])
        bridges[bridge] = _nm_values(out)[0] if code == 0 else ""
    return bridges


def _nm_settings(uuid):
    """The NM_COPY settings of a connection that are set, in order."""
    code, out, err = _run([nmcli_binary(), "-g", ",".join(NM_COPY), "connection",
                           "show", "uuid", uuid])
    if code != 0:
        raise HostNetError("Cannot read the connection %s: %s" % (uuid, err.strip()[-200:]),
                           500)
    values = _nm_values(out)
    return [(key, value) for key, value in zip(NM_COPY, values)
            if value not in ("", "--")]


def lan_convert_plan(nic, bridge):
    """The commands that make ``nic`` a port of a new bridge holding its addresses.

    Built without privilege, from what NetworkManager reports, so the same
    plan is what the helper runs and what a person without it is shown.
    Returns ``(commands, previous_uuid)``. The NIC's own connection is not
    deleted, only kept from coming back up on its own, so reverting has
    something to go back to.
    """
    device = nm_device(nic)
    if device is None:
        raise HostNetError("NetworkManager does not manage %s, so lemondx cannot "
                           "convert it. Make the bridge in your network "
                           "configuration instead (see docs/networking.md)." % nic, 409)
    try:
        with open(_sys(nic, "address"), encoding="ascii") as handle:
            mac = handle.read().strip()
    except OSError:
        raise HostNetError("Cannot read %s's MAC address." % nic, 500)
    nmcli = nmcli_binary()
    name = LAN_PREFIX + bridge
    # The NIC's MAC on the bridge: DHCP then asks for the address the host
    # had, and whatever the router has on record for this machine still
    # applies. STP off and no forward delay, or the port spends 30s learning
    # before it passes a frame -- long enough for DHCP to give up.
    add = [nmcli, "connection", "add", "type", "bridge", "ifname", bridge,
           "con-name", name, "connection.autoconnect", "yes",
           "bridge.stp", "no", "bridge.forward-delay", "0", "bridge.mac-address", mac]
    for key, value in _nm_settings(device["uuid"]):
        add += [key, value]
    return [
        Command(add, "make bridge %s, with %s's addresses, gateway and DNS" % (bridge, nic)),
        Command([nmcli, "connection", "add", "type", "ethernet", "ifname", nic,
                 "con-name", name + "-port", "slave-type", "bridge", "master", bridge,
                 "connection.autoconnect", "yes"],
                "make %s a port of %s" % (nic, bridge)),
        Command([nmcli, "connection", "modify", "uuid", device["uuid"],
                 "connection.autoconnect", "no"],
                "keep %s's own connection (%s) from taking it back"
                % (nic, device["connection"])),
        Command([nmcli, "--wait", str(LAN_ACTIVATE_SECONDS), "connection", "up", "id",
                 name + "-port"],
                "move %s onto the bridge -- the host's connection drops briefly" % nic),
        Command([nmcli, "--wait", str(LAN_ACTIVATE_SECONDS), "connection", "up", "id", name],
                "bring %s up and wait for its address" % bridge),
    ], device["uuid"]


def lan_revert_plan(bridge, previous=None):
    """The commands that give a converted bridge's NIC back its own connection.

    Only ever deletes the two connections named for the bridge. ``previous``
    is the NIC's own connection when known; otherwise the NIC's ethernet
    connection that conversion switched off is looked up.
    """
    nmcli = nmcli_binary()
    name = LAN_PREFIX + bridge
    nic = nm_lan_bridges().get(bridge, "")
    if previous is None:
        previous = _previous_connection(nic) if nic else ""
    commands = [
        Command([nmcli, "connection", "delete", "id", name + "-port"],
                "take %s off bridge %s" % (nic or "the NIC", bridge)),
        Command([nmcli, "connection", "delete", "id", name], "remove bridge %s" % bridge),
    ]
    if previous:
        commands += [
            Command([nmcli, "connection", "modify", "uuid", previous,
                     "connection.autoconnect", "yes"],
                    "let %s's own connection come up on its own again" % (nic or "the NIC")),
            Command([nmcli, "--wait", str(LAN_ACTIVATE_SECONDS), "connection", "up",
                     "uuid", previous],
                    "bring %s's own connection back up" % (nic or "the NIC")),
        ]
    return commands


def _previous_connection(nic):
    """The ethernet connection for ``nic`` that conversion switched off, or ""."""
    nmcli = nmcli_binary()
    code, out, _ = _run([nmcli, "-t", "-f", "UUID,TYPE,AUTOCONNECT,NAME", "connection", "show"])
    if code != 0:
        return ""
    for line in out.splitlines():
        fields = line.split(":", 3)
        if len(fields) < 4 or fields[1] != "802-3-ethernet" or fields[3].startswith(LAN_PREFIX):
            continue
        code, iface, _ = _run([nmcli, "-g", "connection.interface-name", "connection",
                               "show", "uuid", fields[0]])
        if code == 0 and _nm_values(iface)[0] == nic:
            return fields[0]
    return ""


def lan_link_up(bridge, need_default):
    """Whether a converted bridge came up usable: an address, and the way out if it had one."""
    code, out, _ = _run([ip_binary(), "-j", "-4", "addr", "show", "dev", bridge])
    try:
        has_address = code == 0 and any(
            info.get("family") == "inet"
            for entry in json.loads(out or "[]") for info in entry.get("addr_info") or [])
    except ValueError:
        has_address = False
    return has_address and (not need_default or bridge in default_route_devices())


def docker_present():
    """Whether Docker is on this host, and so its FORWARD drop is too."""
    return bool(shutil.which("docker")) or os.path.exists("/var/run/docker.sock")


# The host side of macvlan. A macvlan NIC never passes frames between its
# parent and its children, so the host cannot reach its own macvlan instances
# -- nor they the host's fabric. A second macvlan child on the host, in the
# same bridge mode, is a sibling they can reach: it needs no address (the
# kernel answers ARP for any of the host's addresses on it, the fabric
# gateway included), only a /32 route per instance so the host's replies go
# out through it rather than the parent. Its routes carry their own protocol
# number, so the fabric's route pass -- which removes every proto-133 route it
# did not ask for -- never touches them.
SHIM_PREFIX = "lmdx-"
SHIM_PROTO = "134"
# The shim has no address, so the kernel would pick any of the host's as the
# source of what leaves through it -- often one in another subnet, which the
# instance then answers via its router and the reply is lost. So its routes
# name the host's own address in the instance's subnet, and what is forwarded
# out of it (a fabric instance opening a connection) is SNAT'd to that, in a
# table of its own, replaced whole like the fabric's.
SHIM_TABLE = "lemondx_shim"


def shim_name(nic):
    return (SHIM_PREFIX + nic)[:15]


def current_shims():
    """{shim: parent} for the shims on this host, by their name."""
    try:
        names = os.listdir("/sys/class/net")
    except OSError:
        return {}
    shims = {}
    for name in names:
        if name.startswith(SHIM_PREFIX):
            code, out, _ = _run([ip_binary(), "-j", "link", "show", "dev", name])
            try:
                shims[name] = (json.loads(out or "[]") or [{}])[0].get("link", "") \
                    if code == 0 else ""
            except ValueError:
                shims[name] = ""
    return shims


def current_shim_routes():
    """{address: (device, source)} for the shim routes on this host."""
    code, out, _ = _run([ip_binary(), "-j", "-4", "route", "show", "proto", SHIM_PROTO])
    try:
        return {e["dst"]: (e.get("dev", ""), e.get("prefsrc", ""))
                for e in json.loads(out or "[]") if e.get("dst")} if code == 0 else {}
    except ValueError:
        return {}


def nic_addresses(nic):
    """The IPv4 interfaces (address with prefix) on ``nic``, in the order it lists them."""
    code, out, _ = _run([ip_binary(), "-j", "-4", "addr", "show", "dev", nic])
    try:
        return [ipaddress.ip_interface("%s/%s" % (i["local"], i["prefixlen"]))
                for entry in json.loads(out or "[]") for i in entry.get("addr_info") or []
                if i.get("local")] if code == 0 else []
    except (ValueError, KeyError):
        return []


def connected_subnets(subnets):
    """The ones among ``subnets`` this host is directly attached to.

    What a fabric firewall may admit from outside: a macvlan instance reaches
    a node's fabric by being on the same L2 as the node, so a subnet the host
    has no leg on can never be one of them, and admitting it would only open
    the fabric to something routed in.
    """
    links = link_subnets()
    kept = []
    for subnet in subnets:
        try:
            network = ipaddress.ip_network(subnet)
        except ValueError:
            continue
        if any(network == link or network.subnet_of(link) for link in links):
            kept.append(str(network))
    return kept


def nic_subnets(nic):
    """The IPv4 subnets on ``nic``'s own addresses."""
    return [interface.network for interface in nic_addresses(nic)]


def shim_commands(wanted):
    """Commands to bring the shims to ``wanted``: {parent NIC: [instance address]}.

    Held to ownership like routes: only `lmdx-` links and proto-134 routes are
    ever removed, and each is removed once nothing wants it.
    """
    binary = ip_binary()
    have = current_shims()
    routes = current_shim_routes()
    commands = []
    wanted_routes = {}
    snat = set()
    for nic, addresses in sorted(wanted.items()):
        shim = shim_name(nic)
        if shim not in have:
            commands += [
                Command([binary, "link", "add", shim, "link", nic, "type", "macvlan",
                         "mode", "bridge"],
                        "give the host a way to its own macvlan instances on %s" % nic),
                Command([binary, "link", "set", shim, "up"], "bring %s up" % shim)]
        own = nic_addresses(nic)
        for address in addresses:
            ip = ipaddress.ip_address(address)
            source = next((str(i.ip) for i in own if ip in i.network), "")
            wanted_routes[str(ip)] = (shim, source)
            if source:
                snat.add((shim, str(next(i.network for i in own if ip in i.network)), source))
    for address, (shim, source) in sorted(wanted_routes.items()):
        if routes.get(address) != (shim, source):
            commands.append(Command(
                [binary, "route", "replace", "%s/32" % address, "dev", shim]
                + (["src", source] if source else []) + ["proto", SHIM_PROTO],
                "reach macvlan instance %s through %s%s" % (
                    address, shim, ", from %s" % source if source else "")))
    for address in sorted(set(routes) - set(wanted_routes)):
        commands.append(Command([binary, "route", "del", address, "proto", SHIM_PROTO],
                                "forget macvlan instance %s, which is gone" % address))
    for shim in sorted(set(have) - {shim_name(n) for n in wanted}):
        commands.append(Command([binary, "link", "del", shim],
                                "remove %s: no macvlan instance here needs it" % shim))
    lines = ["table ip %s" % SHIM_TABLE, "delete table ip %s" % SHIM_TABLE]
    if snat:
        lines += ["table ip %s {" % SHIM_TABLE, "  chain postrouting {",
                  # Ahead of the fabric's masquerade: the first NAT to match a
                  # connection is the one it keeps, and that one would pick
                  # any address for a device that has none.
                  "    type nat hook postrouting priority srcnat - 10; policy accept;"]
        lines += ['    oifname "%s" ip daddr %s ip saddr != %s snat to %s'
                  % (shim, subnet, source, source) for shim, subnet, source in sorted(snat)]
        lines += ["  }", "}"]
    commands.append(Command([nft_binary(), "-f", "-"],
                            "send what leaves through a shim from the host's own address",
                            stdin="\n".join(lines) + "\n"))
    return commands


def lan_docker_commands(bridges):
    """Keep DOCKER-USER letting LAN ``bridges`` through; the fabric's rules are left alone.

    Tagged apart from the fabric's, since each set is kept level by removing
    what it did not ask for, and one must never clear the other's.
    """
    return docker_commands(bridges, tag=LAN_DOCKER_TAG, what="a LAN bridge")

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
import os
import shutil
import subprocess

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

    def __init__(self, argv, why, privileged=True):
        self.argv = list(argv)
        self.why = why
        self.privileged = privileged

    def shell(self):
        """The command as a person would type it, for the copyable plan."""
        parts = ["sudo"] if self.privileged else []
        return " ".join(parts + self.argv)

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


def _run(argv, timeout=_COMMAND_TIMEOUT):
    """Run a command, returning (returncode, stdout, stderr).

    Never raises for a non-zero exit -- a command that ran and failed is a
    result, the same way a failed exec in `lxd.py` is.
    """
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
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
        gateway = fields[fields.index("via") + 1] if "via" in fields else ""
        routes[fields[0]] = gateway
    return routes


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


def reaches(address):
    """Is this address on a directly-connected subnet?"""
    try:
        host = ipaddress.ip_address(str(address).strip())
    except ValueError:
        return False
    return any(host in subnet for subnet in link_subnets())


# -- building a plan -------------------------------------------------------


def route_commands(desired, current, prefix):
    """Commands to turn `current` into `desired`. Both are {destination: gateway}.

    Every destination is checked against the fabric prefix before it reaches
    argv. The sudoers rules refuse anything outside it too, but that file may
    be the older glob form on an older sudo, so the check lives here as well.
    """
    binary = ip_binary()
    commands = []
    for destination in sorted(desired):
        gateway = desired[destination]
        _inside(destination, prefix)
        if current.get(destination) == gateway:
            continue  # already programmed, and pointing at the right node
        commands.append(Command(
            [binary, "route", "replace", destination, "via", gateway,
             "proto", ROUTE_PROTO],
            "route %s to %s" % (destination, gateway)))
    for destination in sorted(current):
        if destination in desired:
            continue
        _inside(destination, prefix)
        commands.append(Command(
            [binary, "route", "del", destination, "proto", ROUTE_PROTO],
            "drop the route to %s, which is no longer a member's" % destination))
    return commands


def forwarding_commands(bridge):
    """Turn forwarding on, but only where it is actually off.

    On a host that already runs containers this is usually a no-op, which
    keeps sudo out of the common path entirely.
    """
    binary = sysctl_binary()
    state = forwarding(bridge)
    commands = []
    if state["ip_forward"] is False:
        commands.append(Command([binary, "-w", "net.ipv4.ip_forward=1"],
                                "IPv4 forwarding is off, so nothing would cross hosts"))
    if state["bridge"] is False:
        commands.append(Command(
            [binary, "-w", "net.ipv4.conf.%s.forwarding=1" % bridge],
            "forwarding is off on %s" % bridge))
    return commands


def _inside(destination, prefix):
    try:
        network = ipaddress.ip_network(destination, strict=False)
    except ValueError:
        raise HostNetError("%s is not a subnet." % destination, 400)
    if not prefix:
        return
    if not network.subnet_of(prefix):
        raise HostNetError(
            "Refusing to touch the route to %s: it is outside the fabric's "
            "%s, and lemondx only manages routes inside it." % (destination, prefix),
            400)


def describe(commands):
    """The plan as copyable shell, for a host where lemondx cannot run it."""
    return "\n".join(command.shell() for command in commands)


# -- applying --------------------------------------------------------------


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
        code, _, err = _run(command.argv)
        results.append({"command": command.shell(), "why": command.why,
                        "ok": code == 0, "error": err.strip()[-300:] if code else ""})
        if code != 0:
            break
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
        return json.loads(done.stdout or "{}")
    except ValueError:
        raise HostNetError("The fabric helper answered with something that is "
                           "not JSON: %s" % (done.stdout or "")[:200], 500)


def available(prefix=""):
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


def diagnose(prefix="", bridge=""):
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
    if not available(prefix):
        if _no_new_privs():
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
    state = forwarding(bridge) if bridge else {"ip_forward": _sysctl_on("net/ipv4/ip_forward"),
                                               "bridge": None}
    if state["ip_forward"] is False:
        warnings.append("IPv4 forwarding is off on this host; `lemondx fabric apply` "
                        "turns it on.")
    # A host-wide FORWARD DROP is the one failure that looks like a working
    # fabric until traffic is tried. Reading the policy needs root, which the
    # sudoers rules deliberately do not grant, so flag the usual cause instead
    # -- the same trade bootstrap.py makes for lxdbr0.
    if shutil.which("docker") or os.path.exists("/var/run/docker.sock"):
        warnings.append(
            "Docker is installed here. It sets the FORWARD policy to DROP, which "
            "blocks traffic between nodes. If the fabric does not carry traffic, "
            "check `sudo iptables -S FORWARD | head -1` and allow the bridge:\n"
            "  sudo iptables -I DOCKER-USER -i %s -j ACCEPT\n"
            "  sudo iptables -I DOCKER-USER -o %s -j ACCEPT"
            % (bridge or "lemonfab0", bridge or "lemonfab0"))
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

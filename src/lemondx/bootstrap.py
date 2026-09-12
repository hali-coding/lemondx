"""Modular bootstrap: run selected bash scripts inside a freshly made container.

A module is a plain bash script with a small metadata header::

    #!/usr/bin/env bash
    # name: SSH server
    # description: Install OpenSSH and start it.
    # os: debian ubuntu
    # order: 20
    # uses: ssh-keys
    # param: PERMIT_ROOT_LOGIN=prohibit-password  sshd PermitRootLogin value

Every module is prepended with ``_prelude.sh``, which gives it distro-neutral
package/service helpers, so modules stay short and portable. Modules run in
``order`` and receive their parameters (and any selected SSH public keys) as
environment variables.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import time

PRELUDE_NAME = "_prelude.sh"
MODULE_SUFFIX = ".sh"
REMOTE_DIR = "/tmp"

# Public key types we are willing to hand to a container.
SSH_KEY_TYPES = (
    "ssh-rsa", "ssh-ed25519", "ssh-dss",
    "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com",
)

_HEADER_LINE = re.compile(r"^#\s*([a-zA-Z][a-zA-Z0-9_-]*)\s*:\s*(.*)$")
_PARAM = re.compile(r"^([A-Z][A-Z0-9_]*)=(\S*)\s*(.*)$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class BootstrapError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


# -- module discovery ------------------------------------------------------


def module_dirs():
    """Directories searched for modules, lowest priority first.

    Later directories win, so a user module shadows a shipped one of the
    same name.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(os.path.dirname(here))
    dirs = [os.path.join(repo, "modules")]

    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    dirs.append(os.path.join(config_home, "lemondx", "modules"))

    extra = os.environ.get("LEMONDX_MODULES")
    if extra:
        dirs.extend(p for p in extra.split(os.pathsep) if p)
    return dirs


def load_prelude():
    """The shared helper preamble, taken from the highest-priority directory."""
    for directory in reversed(module_dirs()):
        path = os.path.join(directory, PRELUDE_NAME)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                return handle.read()
    return "set -euo pipefail\n"


def discover_modules():
    """Every usable module, keyed by id (the filename without .sh)."""
    modules = {}
    for directory in module_dirs():
        if not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            if not entry.endswith(MODULE_SUFFIX) or entry.startswith("_"):
                continue
            path = os.path.join(directory, entry)
            if not os.path.isfile(path):
                continue
            try:
                modules[entry[:-len(MODULE_SUFFIX)]] = parse_module(path)
            except OSError:
                continue
    return modules


def parse_module(path):
    """Read a module's metadata header and body."""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    meta = {"param": [], "uses": []}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#!"):
            continue
        if not stripped.startswith("#"):
            if stripped:
                break          # header ends at the first real statement
            continue
        match = _HEADER_LINE.match(stripped)
        if not match:
            continue
        key, value = match.group(1).lower(), match.group(2).strip()
        if key == "param":
            parsed = _parse_param(value)
            if parsed:
                meta["param"].append(parsed)
        elif key == "uses":
            meta["uses"].extend(value.replace(",", " ").split())
        else:
            meta[key] = value

    module_id = os.path.basename(path)[:-len(MODULE_SUFFIX)]
    try:
        order = int(meta.get("order", 50))
    except ValueError:
        order = 50

    return {
        "id": module_id,
        "name": meta.get("name") or module_id.replace("-", " ").title(),
        "description": meta.get("description", ""),
        "os": meta.get("os", "").replace(",", " ").split(),
        "order": order,
        "params": meta["param"],
        "uses_ssh_keys": "ssh-keys" in meta["uses"],
        "path": path,
        "body": text,
    }


def _parse_param(value):
    match = _PARAM.match(value)
    if not match:
        return None
    return {
        "name": match.group(1),
        "default": match.group(2),
        "description": match.group(3).strip(),
    }


def public_modules():
    """Module metadata for API/CLI consumption, ordered, without the script."""
    modules = discover_modules().values()
    return [
        {k: v for k, v in module.items() if k not in ("body", "path")}
        for module in sorted(modules, key=lambda m: (m["order"], m["id"]))
    ]


# -- ssh keys --------------------------------------------------------------


def parse_public_key(text):
    """Validate an SSH public key and describe it, or raise BootstrapError.

    Deliberately strict: this is the only path by which key material reaches a
    container, and it must never accept a private key.
    """
    text = text.strip()
    if not text:
        raise BootstrapError("Empty SSH key.")
    if "PRIVATE KEY" in text.upper():
        raise BootstrapError(
            "That looks like a *private* key. Only public keys (.pub) belong here."
        )
    # One key per entry. Refusing embedded newlines outright means nothing can
    # smuggle a second authorized_keys line (or an options field) through.
    if "\n" in text or "\r" in text:
        raise BootstrapError("Expected a single key on one line.")
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise BootstrapError("The key contains control characters.")

    line = " ".join(text.split())

    parts = line.split(" ", 2)
    if len(parts) < 2:
        raise BootstrapError("Not an SSH public key: expected '<type> <base64> [comment]'.")

    key_type, blob = parts[0], parts[1]
    comment = parts[2] if len(parts) > 2 else ""
    if key_type not in SSH_KEY_TYPES:
        raise BootstrapError(
            "Unsupported key type '%s'. Supported: %s" % (key_type, ", ".join(SSH_KEY_TYPES))
        )
    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError):
        raise BootstrapError("The key body is not valid base64.")
    if not raw:
        raise BootstrapError("The key body is empty.")

    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    return {
        "type": key_type,
        "comment": comment,
        "fingerprint": "SHA256:%s" % digest,
        "line": "%s %s%s" % (key_type, blob, (" " + comment) if comment else ""),
    }


def list_host_ssh_keys():
    """Public keys found in the user's ~/.ssh, for offering in the UI.

    Only ``*.pub`` files are ever opened, and each must parse as a public key.
    """
    ssh_dir = os.path.expanduser("~/.ssh")
    found = []
    if not os.path.isdir(ssh_dir):
        return found

    for entry in sorted(os.listdir(ssh_dir)):
        if not entry.endswith(".pub"):
            continue
        path = os.path.join(ssh_dir, entry)
        if not os.path.isfile(path) or os.path.getsize(path) > 16 * 1024:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                described = parse_public_key(handle.read())
        except (OSError, BootstrapError):
            continue
        described["source"] = entry
        found.append(described)
    return found


# -- runner ----------------------------------------------------------------


class BootstrapRunner:
    """Executes selected modules inside one container, in order."""

    def __init__(self, client):
        self.lxd = client

    def run(self, name, module_ids, params=None, ssh_keys=None, timeout=900,
            stop_on_error=True):
        available = discover_modules()
        unknown = [m for m in module_ids if m not in available]
        if unknown:
            raise BootstrapError(
                "Unknown bootstrap module(s): %s. Available: %s"
                % (", ".join(unknown), ", ".join(sorted(available)))
            )

        selected = sorted((available[m] for m in module_ids),
                          key=lambda m: (m["order"], m["id"]))
        keys = [parse_public_key(k)["line"] for k in (ssh_keys or [])]

        needs_keys = [m["id"] for m in selected if m["uses_ssh_keys"]]
        if needs_keys and not keys:
            raise BootstrapError(
                "Module(s) %s need SSH keys, but none were supplied."
                % ", ".join(needs_keys)
            )

        environment = _clean_env(params or {})
        if keys:
            environment["LEMONDX_SSH_KEYS"] = "\n".join(keys)

        self._wait_until_reachable(name)
        self._require_connectivity(name)
        # Package indexes are refreshed once per run, not once per module.
        self.lxd.exec_command(name, ["/bin/sh", "-c",
                                     "rm -f /tmp/.lemondx-pkg-refreshed"], timeout=30)

        prelude = load_prelude()
        results = []
        for module in selected:
            result = self._run_one(name, module, prelude, environment, timeout)
            results.append(result)
            if result["exit_code"] != 0 and stop_on_error:
                break

        return {
            "container": name,
            "modules": results,
            "ok": all(r["exit_code"] == 0 for r in results),
        }

    def _run_one(self, name, module, prelude, environment, timeout):
        script = "%s\n\n# --- module: %s ---\n%s" % (prelude, module["id"], module["body"])
        remote = "%s/lemondx-%s.sh" % (REMOTE_DIR, module["id"])

        env = dict(environment)
        env["LEMONDX_MODULE"] = module["id"]
        for param in module["params"]:
            env.setdefault(param["name"], param["default"])

        started = time.time()
        try:
            self.lxd.push_file(name, remote, script, mode="0700")
            outcome = self.lxd.exec_command(
                name, ["/bin/sh", remote], environment=env, timeout=timeout
            )
        except Exception as exc:                      # noqa: BLE001
            return {
                "id": module["id"], "name": module["name"], "exit_code": 1,
                "stdout": "", "stderr": str(exc),
                "duration": round(time.time() - started, 1),
            }
        finally:
            self.lxd.delete_file(name, remote)

        return {
            "id": module["id"],
            "name": module["name"],
            "exit_code": outcome.get("exit_code", 0),
            "stdout": outcome.get("stdout", ""),
            "stderr": outcome.get("stderr", ""),
            "duration": round(time.time() - started, 1),
        }

    # Tries each tool an image might have; exits 0 reachable, 1 unreachable,
    # 2 when nothing suitable is installed to test with.
    _CONNECTIVITY_PROBE = r"""
for t in wget curl ping; do
  command -v "$t" >/dev/null 2>&1 || continue
  case "$t" in
    wget) wget -q -T 6 -O /dev/null http://deb.debian.org/ 2>/dev/null && exit 0 ;;
    curl) curl -sS -m 6 -o /dev/null http://deb.debian.org/ 2>/dev/null && exit 0 ;;
    ping) ping -c 1 -W 4 1.1.1.1 >/dev/null 2>&1 && exit 0 ;;
  esac
  found=1
done
[ "${found:-0}" = "1" ] && exit 1
exit 2
"""

    def _require_connectivity(self, name):
        """Fail fast, and usefully, when the container cannot reach the network.

        Nearly every module installs packages, and a blocked bridge otherwise
        shows up as an opaque package-manager timeout minutes later.
        """
        try:
            probe = self.lxd.exec_command(
                name, ["/bin/sh", "-c", self._CONNECTIVITY_PROBE], timeout=45
            )
        except Exception:                             # noqa: BLE001
            return                                    # never block on the probe itself
        if probe.get("exit_code") != 1:
            return                                    # reachable, or untestable

        raise BootstrapError(
            "Container '%s' has no outbound network, so package installs would "
            "fail. A common cause is Docker setting the iptables FORWARD policy "
            "to DROP, which blocks the container bridge. Check with "
            "`sudo iptables -S FORWARD | head -1`; if it says DROP, allow the "
            "bridge with:\n"
            "  sudo iptables -I DOCKER-USER -i lxdbr0 -j ACCEPT\n"
            "  sudo iptables -I DOCKER-USER -o lxdbr0 -j ACCEPT\n"
            "(substitute your bridge name, and persist the rules if you want "
            "them to survive a reboot)." % name,
            503,
        )

    def _wait_until_reachable(self, name, timeout=90):
        """Wait for the container to be up with an address.

        Modules install packages, so there is no point starting before the
        network is usable.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.lxd.get_state(name)
            if state.get("status") != "Running":
                raise BootstrapError("Container '%s' is not running." % name, 409)
            for interface, detail in (state.get("network") or {}).items():
                if interface == "lo":
                    continue
                for address in detail.get("addresses") or []:
                    if address.get("family") == "inet" and address.get("scope") == "global":
                        return
            time.sleep(1)
        # No address is not fatal -- a module may not need the network.


def _clean_env(params):
    """Keep only well-formed environment names, with string values."""
    env = {}
    for key, value in (params or {}).items():
        if not _ENV_NAME.match(str(key)):
            raise BootstrapError(
                "Invalid parameter name '%s'. Use upper-case letters, digits "
                "and underscores." % key
            )
        env[str(key)] = "" if value is None else str(value)
    return env

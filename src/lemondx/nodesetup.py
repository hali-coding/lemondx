"""How this node is reached: the address `serve` binds, HTTPS, and logins.

A node starts in *unconfigured mode*: bound to loopback, no authentication,
plain HTTP. Those three are safe only together -- whoever can reach a loopback
port is already on this host, where the daemon socket is theirs anyway -- and
opening the port is safe only once the other two are in place. So leaving the
mode is one operation that settles all three, applied in the order that never
has the port open without a login: an admin account, a certificate, the login
method, and only then the address to listen on.

Each piece is saved where it already lives, so `lemondx configure auth|tls`
and this agree by construction: the admin in auth/users.json, the certificate
in the cluster section's `tls_cert`/`tls_key` (which `serve` defaults to), the
method in the auth section. The address is the one thing that had nowhere to
live -- it was only ever a flag -- so it gets the `listen` section, read by
`serve` under its --host/--port flags.

None of it changes a running server, whose socket, certificate and auth
config were fixed at startup. So `serve()` hands `NodeSetup` a way to restart
itself: the process re-executes with its own argv, and every startup decision
is made again from the files. That is also why a flag given to the first
process still wins in the second, and why `configure()` says so.
"""

from __future__ import annotations

import ipaddress

from . import auth as auth_mod
from . import cluster as cluster_mod
from . import store

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8099
ALL_INTERFACES = "0.0.0.0"

LISTEN_SECTION = "listen"
LISTEN_VERSION = 1
DEFAULT_LISTEN = {"host": DEFAULT_HOST, "port": DEFAULT_PORT}

TLS_MODES = ("generate", "upload", "keep")
# A certificate chain is a few KiB; anything near this is not one.
MAX_PEM = 64 * 1024

LOOPBACK_NAMES = ("localhost",)


class SetupError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


def is_loopback(address):
    address = (address or "").split("%", 1)[0]
    if address in LOOPBACK_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def unconfigured(host, tls, auth_enabled):
    """Whether a server bound like this is in unconfigured mode.

    Judged on what is actually being served, not on which files exist: a
    node someone deliberately runs local-only without a login is in the mode
    whatever they saved, and one started with `--host 0.0.0.0` is not.
    """
    return is_loopback(host) and not tls and not auth_enabled


# -- the listen section ----------------------------------------------------
#
# Like auth and cluster, a file that does not validate stops `serve` rather
# than falling back: back on loopback would be the safe direction, but a node
# its peers suddenly cannot reach is a failure nobody would connect to a typo.


def clean_listen(raw):
    if not isinstance(raw, dict):
        raise SetupError("Listen settings must be a JSON object.")
    unknown = sorted(set(raw) - set(DEFAULT_LISTEN) - {"version"})
    if unknown:
        raise SetupError("Unknown listen setting(s): %s. Known: %s."
                         % (", ".join(unknown), ", ".join(DEFAULT_LISTEN)))
    settings = dict(DEFAULT_LISTEN)
    settings.update({k: v for k, v in raw.items() if k != "version"})
    try:
        ip = ipaddress.ip_address(str(settings["host"] or "").strip())
    except ValueError:
        ip = None
    # The server's socket is AF_INET, so only an IPv4 literal would bind.
    if ip is None or ip.version != 4:
        raise SetupError("host: an IPv4 address to listen on, such as %s (every "
                         "interface) or %s (this host only), not %r."
                         % (ALL_INTERFACES, DEFAULT_HOST, settings["host"]))
    port = settings["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise SetupError("port: a TCP port from 1 to 65535, not %r." % (port,))
    return {"host": str(ip), "port": port}


def load_listen():
    """Saved listen settings, or None if never saved. Raises SetupError if unusable."""
    try:
        raw = store.load_config(LISTEN_SECTION)
    except ValueError as exc:
        raise SetupError("Cannot use the saved listen settings: %s" % exc, 500)
    if raw is None:
        return None
    try:
        return clean_listen(raw)
    except SetupError as exc:
        raise SetupError("Invalid saved listen settings in %s: %s Fix the file or run "
                         "`lemondx configure listen`." % (listen_path(), exc.message), 500)


def save_listen(settings):
    cleaned = clean_listen(settings)
    store.save_config(LISTEN_SECTION, dict(cleaned, version=LISTEN_VERSION))
    return cleaned


def reset_listen():
    return store.delete_config(LISTEN_SECTION)


def listen_path():
    return store.config_path(LISTEN_SECTION)


def in_cluster():
    """Whether this node holds a cluster credential, read straight from disk."""
    return bool((store.load_auth("cluster").get("secret") or {}).get("token"))


def cluster_refusal():
    """Why this node's address and certificate may not be changed here, or ""."""
    if not in_cluster():
        return ""
    return ("This node is in a cluster: its peers pin the certificate it serves and "
            "call it at the address it advertises, so changing either from here would "
            "cut it off. Leave the cluster first, or change them on the host with "
            "`lemondx configure tls` / `configure listen` and tell the peers.")


# -- configuring -----------------------------------------------------------


def check(request, auth_service, from_api=True):
    """The validated plan for a configure request. Writes nothing.

    Everything that can be refused is refused here, before the first write,
    so a bad certificate never leaves a node with logins switched on and the
    old address -- or worse, the new address and no certificate.
    """
    if not isinstance(request, dict):
        raise SetupError("Expected a JSON object.")
    plan = {}

    username = request.get("username")
    password = request.get("password")
    username = username.strip() if isinstance(username, str) else ""
    if username or password:
        if not username:
            raise SetupError("Give the admin a user name.")
        username = auth_mod.check_user_name(username)
        if not isinstance(password, str) or len(password) < auth_mod.MIN_PASSWORD:
            raise SetupError("The admin password must be at least %d characters."
                             % auth_mod.MIN_PASSWORD)
        plan["admin"] = {"name": username, "password": password}
    elif not _admins(auth_service):
        # Switching logins on with nobody able to log in would lock everyone
        # out of the UI, with only the CLI left to undo it.
        raise SetupError("There is no admin account yet: give a user name and a password "
                         "for one, so there is somebody to log in as.")

    tls = request.get("tls")
    tls = tls if isinstance(tls, dict) else {"mode": "generate"}
    mode = tls.get("mode") or "generate"
    if mode not in TLS_MODES and not (mode == "paths" and not from_api):
        raise SetupError("tls.mode must be one of: %s." % ", ".join(TLS_MODES))
    if mode == "generate":
        host = str(tls.get("host") or "").strip() or cluster_mod.guess_local_address() \
            or "localhost"
        plan["tls"] = {"mode": mode, "host": host}
    elif mode == "upload":
        for key in ("cert", "key"):
            value = tls.get(key)
            if not isinstance(value, str) or not value.strip():
                raise SetupError("Upload both the certificate and its private key (PEM).")
            if len(value) > MAX_PEM:
                raise SetupError("That %s is too large to be one." % key)
        cluster_mod.check_certificate(tls["cert"], tls["key"])
        plan["tls"] = {"mode": mode, "cert": tls["cert"], "key": tls["key"]}
    elif mode == "keep":
        cert, key = _saved_certificate()
        if not cert:
            raise SetupError("There is no certificate to keep yet: generate or upload one.")
        plan["tls"] = {"mode": mode, "cert": cert, "key": key}
    else:
        # The CLI names files where they are, as `configure tls` always has, so
        # a certificate a tool renews in place stays the one served.
        if not tls.get("cert") or not tls.get("key"):
            raise SetupError("Name both the certificate and its private key.")
        cluster_mod.certificate_fingerprint(tls["cert"])      # raises if it is not one
        plan["tls"] = {"mode": mode, "cert": tls["cert"], "key": tls["key"]}

    try:
        current = load_listen() or DEFAULT_LISTEN
    except SetupError:
        current = DEFAULT_LISTEN      # configuring is how a broken file gets fixed
    plan["listen"] = clean_listen({"host": request.get("host") or current["host"],
                                   "port": request.get("port") or current["port"]})
    return plan


def apply(plan, auth_service):
    """Leave unconfigured mode as ``plan`` says; returns what was done.

    The order is the safety: the account first (harmless while logins are
    off), then the certificate, then logins, and the address last -- so a
    failure part way leaves the node where it was, never listening on the
    network without a login.
    """
    refusal = cluster_refusal()
    if refusal:
        raise SetupError(refusal, 409)

    admin = plan.get("admin")
    if admin:
        auth_service.set_user(admin["name"], password=admin["password"], role=auth_mod.ADMIN)

    tls = plan["tls"]
    if tls["mode"] == "generate":
        made = cluster_mod.generate_certificate(tls["host"])
        cert, key = made["cert"], made["key"]
    elif tls["mode"] == "upload":
        made = cluster_mod.install_certificate(tls["cert"], tls["key"])
        cert, key = made["cert"], made["key"]
    else:
        cert, key = tls["cert"], tls["key"]
    settings = cluster_mod.load_settings()
    settings.update(tls_cert=cert, tls_key=key)
    cluster_mod.save_settings(settings)

    saved = auth_mod.load_settings() or dict(auth_mod.DEFAULT_SETTINGS)
    if "local" not in saved["methods"]:
        saved["methods"] = saved["methods"] + ["local"]
    saved = auth_mod.save_settings(saved)

    listen = save_listen(plan["listen"])
    fingerprint = cluster_mod.certificate_fingerprint(cert)
    return {
        "admin": admin["name"] if admin else None,
        "certificate": {"path": cert, "mode": tls["mode"], "fingerprint": fingerprint,
                        "fingerprint_pretty": cluster_mod.pretty_fingerprint(fingerprint)},
        "auth_methods": saved["methods"],
        "host": listen["host"],
        "port": listen["port"],
    }


def reset():
    """Back to unconfigured mode at the next start. Accounts are left alone.

    Users and tokens stay because they are harmless with logins off and a
    nuisance to recreate; what goes is everything that makes `serve` use them,
    plus HTTPS and the address. Refused in a cluster, for the reason
    `cluster_refusal()` gives.
    """
    refusal = cluster_refusal()
    if refusal:
        raise SetupError(refusal, 409)
    removed = []
    if reset_listen():
        removed.append("listen")
    if auth_mod.reset_settings():
        removed.append("auth")
    settings = cluster_mod.load_settings()
    if settings["tls_cert"]:
        cluster_mod.save_settings(dict(settings, tls_cert=None, tls_key=None))
        removed.append("tls")
    return removed


def _admins(auth_service):
    return [u["name"] for u in auth_service.list_users() if u["role"] == auth_mod.ADMIN]


def _saved_certificate():
    settings = cluster_mod.load_settings()
    return settings["tls_cert"], settings["tls_key"]


def _certificate_summary():
    cert, _ = _saved_certificate()
    if not cert:
        return None
    try:
        fingerprint = cluster_mod.certificate_fingerprint(cert)
    except cluster_mod.ClusterError as exc:
        return {"path": cert, "fingerprint": "", "fingerprint_pretty": "", "error": exc.message}
    return {"path": cert, "fingerprint": fingerprint,
            "fingerprint_pretty": cluster_mod.pretty_fingerprint(fingerprint), "error": ""}


class NodeSetup:
    """The running server's side of all this: what it serves, and changing it.

    ``serving`` is what this process resolved at startup -- the files only
    say what the next start will do, and a flag may be overriding them --
    with ``pinned`` naming the flags that did. ``restart`` and ``busy`` are
    set by `serve()`; without a restart there is no way to apply anything,
    so configuring is refused.
    """

    def __init__(self, auth, serving=None):
        self.auth = auth
        self.serving = dict({"host": DEFAULT_HOST, "port": DEFAULT_PORT, "tls": False,
                             "pinned": []}, **(serving or {}))
        self.restart = None
        self.busy = lambda: []
        self._restarting = False

    def state(self):
        host, port, tls = self.serving["host"], self.serving["port"], self.serving["tls"]
        return {
            "unconfigured": unconfigured(host, tls, self.auth.config.enabled),
            "host": host,
            "port": port,
            "tls": tls,
            "auth": self.auth.config.enabled,
            "auth_methods": list(self.auth.config.methods),
            "public": not is_loopback(host),
            "in_cluster": in_cluster(),
            "pinned": list(self.serving["pinned"]),
            "certificate": _certificate_summary(),
            "admins": _admins(self.auth),
            "suggested_address": cluster_mod.guess_local_address(),
            "blocked": self.blocked(),
        }

    def blocked(self):
        """Why configuring is refused right now, or ""."""
        if self._restarting:
            return "The server is already restarting with a new configuration."
        refusal = cluster_refusal()
        if refusal:
            return refusal
        if self.restart is None:
            return ("This server cannot restart itself to apply a configuration; use "
                    "`lemondx configure node` on the host and restart it.")
        busy = self.busy()
        if busy:
            # Restarting is a new process: these would stop half done.
            return ("Applying a configuration restarts the server, which would cut "
                    "short: %s. Try again once it has finished." % "; ".join(busy))
        return ""

    def configure(self, request):
        why = self.blocked()
        if why:
            raise SetupError(why, 409)
        plan = check(request, self.auth)
        done = apply(plan, self.auth)
        pinned = set(self.serving["pinned"])
        ignored = "--ignore-config" in pinned
        overridden = []
        host, port = done["host"], done["port"]
        if ignored or "--host" in pinned:
            host = self.serving["host"]
            if host != done["host"]:
                overridden.append(
                    "serve was started with %s, so it keeps listening on %s; drop the "
                    "flag (in a systemd unit, from ExecStart) for the saved %s to apply."
                    % ("--ignore-config" if ignored else "--host " + host, host, done["host"]))
        if ignored or "--port" in pinned:
            port = self.serving["port"]
        tls = not (ignored or "--no-tls" in pinned) or "--tls-cert" in pinned
        if not tls:
            overridden.append("serve was started with %s, so it keeps serving plain HTTP."
                              % ("--ignore-config" if ignored else "--no-tls"))
        elif "--tls-cert" in pinned:
            overridden.append("serve was started with --tls-cert, so it keeps serving that "
                              "certificate rather than this one.")
        if ignored or "--auth" in pinned:
            overridden.append("serve was started with %s, so its login methods stay %s."
                              % ("--ignore-config" if ignored else "--auth",
                                 ", ".join(self.auth.config.methods) or "off"))
        self._restarting = True
        self.restart()
        return dict(done, restarting=True, scheme="https" if tls else "http",
                    serving_host=host, serving_port=port, overridden=overridden)

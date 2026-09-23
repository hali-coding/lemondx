"""Federation: other lemondx nodes, the groups they form, and what spans them.

``ClusterService`` sits beside ``ContainerService`` and ``AuthService`` in the
same role they have -- the one place the logic lives, called by both the HTTP
server and the CLI -- but for a set of hosts rather than one. It owns the member
registry, the join handshake, pushing templates and modules between nodes, and
launching a template across several of them at once.

The model is deliberately flat. There is no leader, no quorum and no shared
database: every node is a complete lemondx that works on its own, and a cluster
only means each one knows how to call the others. A node that is down costs you
that node, nothing else.

**Joining is one way and then it spreads.** A node redeems a join code against
any one member, and gets back the cluster's credential and its whole member
list; it then announces itself to each of those members. So a host joins *the
cluster*, not a node -- there is no reciprocal handshake to arrange, and which
member issued the code does not matter. Membership afterwards is kept level by
``sync_members()``, pulling each peer's list and pushing our own.

**Only the first node needs setting up**, and barely that. A node's name, the
address peers reach it on and its TLS certificate are all worked out and
provisioned on demand by ``ensure_identity()`` -- hostname, the address on this
host's default route, and a self-signed certificate written on first use.
``lemondx configure cluster`` exists to override any of them (a DNS name, an
address behind NAT, a real certificate), not to make federation work.

Trust is the cluster credential over TLS pinned per member -- see
``nodeclient.py``. The credential is an ordinary lemondx API token that every
member holds, so authenticating a peer is the same code path as authenticating
any other client, and ``lemondx tokens`` lists it. Nothing is trusted for being
on the same network.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import store
from .auth import ADMIN, TOKEN_PREFIX
from .bootstrap import discover_modules, module_source, normalise_module_id
from .lxd import LXDError
from .nodeclient import (DEFAULT_TIMEOUT, LONG_TIMEOUT, NodeClient, NodeError,
                         fingerprint_of,
                         normalize_url, parse_url, peer_fingerprint, pretty_fingerprint)
from .service import ServiceError

SETTINGS_SECTION = "cluster"
SETTINGS_VERSION = 1

# Every one of these is an override. Left blank, `ensure_identity()` works the
# value out and writes back what it chose, so a node that is simply told to
# join ends up with a complete, stable identity without anyone configuring one.
DEFAULT_SETTINGS = {
    # Blank: this host's name. Peers see it, so it has to be stable.
    "name": "",
    # Blank: https:// the address on this host's default route, at the port
    # `serve` is listening on. Set it for a DNS name, or an address behind NAT.
    "url": "",
    "tls_cert": None,
    "tls_key": None,
    # Whether `/api/cluster/enroll` answers at all. Off means no new node can
    # join through this one, even holding a valid code.
    "allow_enrollment": True,
}

NODE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
GROUP_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 ._-]{0,63}$")
FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")

JOIN_PREFIX = "lmdxjoin"
CODE_PREFIX = "lemondx-join."
# Long enough that guessing is hopeless, short enough to paste in one line.
INVITE_SECRET_BYTES = 32
DEFAULT_INVITE_MINUTES = 30
MAX_INVITE_MINUTES = 24 * 60

# The default port `serve` listens on, for working out an address before one
# has ever been served. Imported lazily in _default_port() to keep this module
# importable without server.py.
FALLBACK_PORT = 8099

# Reachability is shown on a page that polls; probing every node on every poll
# would turn one open tab into a load generator.
PROBE_CACHE_SECONDS = 10
PROBE_TIMEOUT = 6
# How many nodes to talk to at once. Federations here are tens of hosts, not
# thousands, and each call is mostly waiting on the network.
FANOUT_WORKERS = 8
# A remote launch is started in the background there and polled; this bounds
# how long the coordinator will follow it before giving up on the answer.
REMOTE_RUN_DEADLINE = 3600
REMOTE_POLL_SECONDS = 3

# The two groups `auto_groups()` maintains, and how far from the cluster
# average still counts as "the same size". The tolerance is not cosmetic: two
# hosts built to one spec report memory totals that differ by a few MiB and a
# hypervisor may hand one of them a thread fewer, so an exact comparison would
# call one of a pair of identical machines large and the other small.
SIZE_GROUPS = ("large", "small")
SIZE_TOLERANCE = 0.05

# Telling a node it has been evicted is what makes the removal clean, so it is
# worth a real wait -- but not the full one: an operator is watching a dialog,
# and a host that is switched off refuses at once anyway.
EVICT_TIMEOUT = 10

SYNC_KINDS = ("templates", "modules", "profiles", "groups", "users", "stacks")

# What a node reconciles on its own when it comes back. The auto-propagated
# kinds and only those: `users` crosses as a password hash and is pushed only
# when somebody asks for it by name, so a pass that runs unattended has no
# business creating accounts on this host or anywhere else.
RECONCILE_KINDS = tuple(k for k in SYNC_KINDS if k != "users")
# The first pass waits for `serve` to be listening -- a peer reconciling back
# at us mid-startup would find nothing answering -- and then it is an hourly
# background chore. Drift is not an emergency; it only has to be found.
RECONCILE_DELAY = 20
RECONCILE_INTERVAL = 3600
# Saving something shared pushes it to every member there and then. Kept short:
# a node that is off refuses instantly, and one that is merely unreachable must
# not hold up the save that is already done here.
AUTO_SYNC_TIMEOUT = 10

# The name and owner the cluster credential is stored under, in the same token
# file as every other API token. Recognisable in `lemondx tokens`, revocable
# there, and authenticated by exactly the same code as any other token.
CLUSTER_TOKEN_NAME = "cluster"
CLUSTER_TOKEN_OWNER = "cluster"


class ClusterError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


def from_peer(principal):
    """Whether this request is another member pushing, rather than a person.

    What stops a push echoing round the cluster for ever: a member that is
    handed a template saves it and stops, instead of announcing it onward to
    everyone who just told it. Only the cluster credential looks like this, and
    a caller cannot present it without already being trusted as a member.
    """
    return bool(principal) and principal.name == CLUSTER_TOKEN_OWNER \
        and principal.via == "token:%s" % CLUSTER_TOKEN_NAME


def _log(message):
    # stderr, not stdout: these happen inside CLI commands too, where stdout may
    # be a --json payload something else is parsing.
    print("[lemondx] cluster: %s" % message, file=sys.stderr, flush=True)


# -- settings --------------------------------------------------------------


def clean_settings(raw):
    """Validate a saved cluster section. Raises ClusterError if it is unusable."""
    settings = dict(DEFAULT_SETTINGS)
    for key, value in (raw or {}).items():
        if key == "version":
            continue
        if key not in DEFAULT_SETTINGS:
            raise ClusterError("cluster settings: unknown key %r" % key)
        settings[key] = value

    name = str(settings["name"] or "").strip()
    if name and not NODE_NAME.match(name):
        raise ClusterError(
            "cluster settings: a node name is letters, digits and . _ -, up to "
            "64 characters (%r)." % name)
    settings["name"] = name

    url = str(settings["url"] or "").strip()
    if url:
        try:
            url = normalize_url(url)
        except NodeError as exc:
            raise ClusterError("cluster settings: %s" % exc.message)
    settings["url"] = url

    for key in ("tls_cert", "tls_key"):
        value = settings[key]
        if value in (None, ""):
            settings[key] = None
            continue
        if not isinstance(value, str):
            raise ClusterError("cluster settings: %s must be a path." % key)
        settings[key] = os.path.abspath(os.path.expanduser(value.strip()))
    if bool(settings["tls_cert"]) != bool(settings["tls_key"]):
        raise ClusterError("cluster settings: tls_cert and tls_key go together.")
    settings["allow_enrollment"] = bool(settings["allow_enrollment"])
    return settings


def load_settings():
    """Saved cluster settings, or the defaults. Raises ClusterError if unusable.

    Like auth and unlike health, a file that does not validate is an error: a
    cluster section names the certificate that identifies this node to every
    peer, and quietly falling back to "no certificate" would take the node out
    of its own cluster without saying so.
    """
    try:
        raw = store.load_config(SETTINGS_SECTION)
    except ValueError as exc:
        raise ClusterError("Cannot use the saved cluster settings: %s" % exc, 500)
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


def default_node_name():
    """This host's name, cleaned up enough to be a node name."""
    try:
        raw = socket.gethostname().split(".")[0]
    except OSError:
        raw = ""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
    return cleaned or "lemondx"


def guess_local_address():
    """The address on this host peers are most likely to reach it on.

    Opening a UDP socket towards a public address sends nothing; it only makes
    the kernel choose a source address, which is the one for the route out of
    this host. Better than the hostname, which often resolves to 127.0.1.1.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("198.51.100.1", 9))        # TEST-NET-2: never routed anywhere
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


def default_certificate_paths():
    directory = os.path.join(store.data_dir(), "tls")
    return (os.path.join(directory, "node-cert.pem"),
            os.path.join(directory, "node-key.pem"))


def certificate_fingerprint(path):
    """The SHA-256 of a PEM certificate file, or raise ClusterError."""
    try:
        with open(path, encoding="utf-8") as handle:
            pem = handle.read()
    except OSError as exc:
        raise ClusterError("Cannot read the TLS certificate %s: %s" % (path, exc))
    try:
        # Only the first certificate: in a chain it is the leaf, which is the
        # one the peer will present and pin.
        end = pem.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")
        der = ssl.PEM_cert_to_DER_cert(pem[:end] + "\n")
    except (ValueError, ssl.SSLError) as exc:
        raise ClusterError("%s is not a PEM certificate: %s" % (path, exc))
    return fingerprint_of(der)


def generate_certificate(host, days=3650, directory=None):
    """Write a self-signed certificate and key for this node, via openssl.

    lemondx has no dependencies and the standard library cannot make a
    certificate, so this shells out -- the same way `lemondx shell` hands off to
    the real client rather than reimplementing a terminal. Being self-signed
    costs nothing: federation pins the key rather than trusting an issuer.
    """
    cert, key = default_certificate_paths()
    if directory:
        cert = os.path.join(directory, os.path.basename(cert))
        key = os.path.join(directory, os.path.basename(key))
    os.makedirs(os.path.dirname(cert), mode=0o700, exist_ok=True)
    host = str(host or "").strip()
    if not host:
        raise ClusterError("Give the address peers will use to reach this node.")
    try:
        alt = "IP:%s" % str(ipaddress.ip_address(host))
    except ValueError:
        alt = "DNS:%s" % host
    command = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", key, "-out", cert, "-days", str(int(days)),
        "-subj", "/CN=%s" % host, "-addext", "subjectAltName=%s" % alt,
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise ClusterError(
            "openssl is not installed, and lemondx cannot make a certificate "
            "without it. Install openssl, or point tls_cert/tls_key at a "
            "certificate from elsewhere with `lemondx configure cluster`.", 503)
    except subprocess.SubprocessError as exc:
        raise ClusterError("openssl failed: %s" % exc, 500)
    if completed.returncode != 0:
        raise ClusterError("openssl failed: %s"
                           % (completed.stderr or "").strip()[-400:], 500)
    os.chmod(key, 0o600)
    return {"cert": cert, "key": key, "fingerprint": certificate_fingerprint(cert),
            "host": host, "days": int(days)}


# -- join codes ------------------------------------------------------------


def encode_join_code(payload):
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return CODE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_join_code(text):
    """The payload inside a join code, or raise ClusterError."""
    body = (text or "").strip()
    if body.startswith(CODE_PREFIX):
        body = body[len(CODE_PREFIX):]
    body = "".join(body.split())
    if not body:
        raise ClusterError("Paste the join code from a node in the cluster "
                           "(`lemondx cluster invite` prints one).")
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise ClusterError("That is not a lemondx join code -- it should start "
                           "with '%s'. Copy the whole line." % CODE_PREFIX)
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ClusterError("That join code was made by a different version of lemondx.")
    for key in ("name", "url", "code"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ClusterError("That join code is incomplete (no %s)." % key)
    return payload


def clean_member(record):
    """One member record from a peer, normalised. Untrusted input, like any record."""
    if not isinstance(record, dict):
        return None
    name = str(record.get("name") or "").strip()
    url = str(record.get("url") or "").strip()
    fingerprint = str(record.get("fingerprint") or "").strip().lower().replace(":", "")
    if not NODE_NAME.match(name) or not url:
        return None
    try:
        url = normalize_url(url)
    except NodeError:
        return None
    if parse_url(url)[0] == "https" and not FINGERPRINT.match(fingerprint):
        return None                 # nothing to recognise it by; not a usable member
    return {"name": name, "url": url,
            "fingerprint": fingerprint if FINGERPRINT.match(fingerprint) else "",
            "description": str(record.get("description") or "")[:200],
            "fabric": store.clean_fabric(record.get("fabric"))}


# -- the service -----------------------------------------------------------


class ClusterService:
    """Members, groups and everything that spans them.

    Holds the local ``ContainerService`` for work on this host and an
    ``AuthService``, which is where the cluster credential is kept: it is an
    ordinary API token, so a peer authenticates through the same path as any
    other client.
    """

    def __init__(self, service, auth, settings=None):
        self.service = service
        self.auth = auth
        self._settings = settings
        self._lock = threading.Lock()
        self._probes = {}            # node name -> (checked_at, record)
        # What the last reconciliation found, kept like a template run: in
        # memory on the one service `serve` builds, so the UI can read drift
        # without setting a pass going every time somebody opens the tab.
        self._drift = None
        # Held for a whole pass, and never by anything that serves a read:
        # `drift()` must answer while one is running. Not `_lock`, which is
        # taken and dropped around each small piece of shared state.
        self._reconcile_lock = threading.Lock()
        self._stacks = None          # set by StackService, for applying a pulled stack
        self._fabric_service = None  # set by FabricService, for routes between nodes
        self._serving_fingerprint = ""  # set by serve(), see note_serving()
        # How a template's "put these on the fabric" becomes a device on this
        # node: the service resolves the flag through here, so it never has to
        # read fabric settings itself.
        service.fabric_bridge = self._fabric_bridge
        service.fabric_configure = self._fabric_configure

    # -- this node ---------------------------------------------------------

    def settings(self):
        if self._settings is None:
            self._settings = load_settings()
        return self._settings

    def reload_settings(self):
        self._settings = None
        return self.settings()

    def local_name(self):
        return self.settings()["name"] or default_node_name()

    def _default_port(self):
        """The port to advertise: the one `serve` is on, else the default."""
        runtime = store.read_runtime()
        port = runtime.get("port") if isinstance(runtime, dict) else None
        return int(port) if isinstance(port, int) and 0 < port < 65536 else FALLBACK_PORT

    def default_url(self):
        """Where peers would reach this node, worked out rather than configured."""
        address = guess_local_address()
        if not address:
            return ""
        return "https://%s:%d" % (address, self._default_port())

    def local_url(self):
        return self.settings()["url"] or self.default_url()

    def certificate(self):
        """``(cert, key)`` this node identifies itself with, or ``(None, None)``."""
        settings = self.settings()
        if settings["tls_cert"]:
            return settings["tls_cert"], settings["tls_key"]
        cert, key = default_certificate_paths()
        if os.path.isfile(cert) and os.path.isfile(key):
            return cert, key
        return None, None

    def note_serving(self, host, port, cert):
        """Record that this process serves this node's URL with ``cert``.

        Then `local_fingerprint()` need not ask the network: this process *is*
        the running server it would otherwise probe. The probe is worse than
        redundant from here -- `serve()` reads the member list before its
        accept loop runs, so a connection to its own bound-but-idle socket
        waits out the whole timeout, once per call, and peers' handshakes
        queue behind it for as long.
        """
        try:
            scheme, url_host, url_port = parse_url(self.local_url())[:3]
        except NodeError:
            return
        if scheme != "https" or int(url_port) != int(port):
            return
        if host not in ("0.0.0.0", "::", "") and host != url_host:
            return      # serving somewhere else, e.g. only on loopback
        try:
            self._serving_fingerprint = certificate_fingerprint(cert)
        except ClusterError:
            pass

    def local_fingerprint(self):
        """What peers will pin this node by.

        The running server is the authority: whatever it presents is what a peer
        actually sees, which a certificate file on disk only predicts. Falling
        back to the file covers a node that is not serving yet -- the usual case
        when setting one up. When this process is that server, it already knows.
        """
        if self._serving_fingerprint:
            return self._serving_fingerprint
        url = self.local_url()
        if url:
            try:
                return peer_fingerprint(url, timeout=PROBE_TIMEOUT)
            except NodeError:
                pass
        cert, _ = self.certificate()
        if not cert:
            return ""
        try:
            return certificate_fingerprint(cert)
        except ClusterError:
            return ""

    def local_node(self):
        """The record a peer keeps for this node."""
        from . import fabric as fabric_mod
        return {
            "name": self.local_name(),
            "url": self.local_url(),
            "fingerprint": self.local_fingerprint(),
            "description": "",
            "added": 0,
            # How peers learn where to route this node's containers, one claim
            # per fabric. Empty on a node that is on none, which is how a mixed
            # cluster works: a node without a fabric is one nobody routes to.
            "fabric": fabric_mod.local_claim(self.local_route_address()),
        }

    def local_route_address(self):
        """The address peers should route this node's fabric subnet to.

        The host of this node's own URL when that is an address, because it is
        the one peers demonstrably reach it on -- a host with several addresses
        on one interface (or one on a second interface) would otherwise
        advertise whichever the kernel picks for a route to the internet, which
        is not necessarily the one the cluster talks to.
        """
        try:
            host = parse_url(self.local_url())[1]
            return str(ipaddress.ip_address(host))
        except (NodeError, ValueError):
            return guess_local_address()

    def ensure_identity(self):
        """Give this node everything it needs to be a member, provisioning it.

        A name, an address and a certificate. Anything not configured is worked
        out here and written back, so it stays the same next time: peers pin
        this certificate and address it by this URL, and a node that quietly
        changed either would drop out of its own cluster.

        This is what makes joining a cluster need no setup. It is not automatic
        magic in `serve` -- it happens when a node is actually federating, which
        is the point at which having an identity starts to matter.
        """
        settings = dict(self.settings())
        changed = False

        if not settings["name"]:
            settings["name"] = default_node_name()
            changed = True

        if not settings["url"]:
            url = self.default_url()
            if not url:
                raise ClusterError(
                    "Cannot work out an address other nodes would reach this host "
                    "on -- it appears to have no route off this machine. Set one "
                    "with `lemondx configure cluster`.", 409)
            settings["url"] = url
            changed = True

        cert, key = self.certificate()
        if not cert:
            host = parse_url(settings["url"])[1]
            made = generate_certificate(host)
            cert, key = made["cert"], made["key"]
            _log("generated a TLS certificate for %s (%s)"
                 % (host, pretty_fingerprint(made["fingerprint"])))
        if (settings["tls_cert"], settings["tls_key"]) != (cert, key):
            settings["tls_cert"], settings["tls_key"] = cert, key
            changed = True

        if changed:
            self._settings = save_settings(settings)
        return self.local_node()

    def self_check(self):
        """Whether a peer could actually reach this node as it advertises itself.

        Returns "" when it could, else what is wrong. Checked when handing out a
        join code and when joining, because every one of these is a mistake the
        operator can only otherwise discover as another node failing to call
        back, hours later.
        """
        url = self.local_url()
        if not url:
            return ("This host has no address off this machine, so no peer could "
                    "reach it.")
        if parse_url(url)[0] != "https":
            return "%s is plain HTTP; peers only speak HTTPS to each other." % url
        cert, _ = self.certificate()
        try:
            served = peer_fingerprint(url, timeout=PROBE_TIMEOUT)
        except NodeError as exc:
            if "not with TLS" in exc.message:
                return ("%s is answering without TLS. Restart `lemondx serve` without "
                        "--no-tls so it serves the node certificate peers pin."
                        % url)
            return ("Nothing answered at %s. Start `lemondx serve --host 0.0.0.0`; it "
                    "picks up this node's certificate automatically." % url)
        if cert:
            try:
                if certificate_fingerprint(cert) != served:
                    return ("%s is serving a different certificate than the one "
                            "this node would hand out. Restart `lemondx serve` so "
                            "it picks up %s." % (url, cert))
            except ClusterError as exc:
                return exc.message
        return ""

    def info(self):
        """What the UI needs to describe this node's place in a cluster."""
        settings = self.settings()
        local = self.local_node()
        return {
            "node": dict(local, **{"self": True}),
            "in_cluster": self.in_cluster(),
            "allow_enrollment": settings["allow_enrollment"],
            "auth_enabled": self.auth.config.enabled,
            "remote_requires_token": self.requires_remote_token(),
            "reachable_because": self.self_check(),
            "fingerprint_pretty": pretty_fingerprint(local["fingerprint"]),
            "peers": len(store.load_nodes()),
            # Peer records or node groups with no credential to use them with:
            # a cluster this node is in only from its own side. Nothing works,
            # and `leave()` is what clears it -- so the UI has to know to offer.
            "leftovers": not self.in_cluster() and bool(
                store.load_nodes() or store.load_node_groups()),
            "configured_url": settings["url"],
            "settings_path": settings_path(),
        }

    # -- the cluster credential --------------------------------------------
    #
    # One secret, held by every member, used in both directions: to call a peer
    # and to authenticate one calling us. That is what lets a node join in one
    # direction and immediately be a full member -- there is no per-pair token
    # to arrange, so no node has to be reachable at the moment another joins.
    #
    # It is stored twice, deliberately: the plaintext in auth/cluster.json for
    # calling out, and its hash as an ordinary token record for calls coming in.
    # Nothing new authenticates it -- `AuthService.authenticate_token` does,
    # exactly as for any other token.

    def cluster_secret(self):
        record = store.load_auth("cluster").get("secret")
        return (record or {}).get("token") or None

    def in_cluster(self):
        return self.cluster_secret() is not None

    def requires_remote_token(self):
        """Whether a caller from another host must present a credential.

        A cluster member accepts API calls from other hosts, so it cannot also
        treat whoever reaches the port as an admin. With authentication
        configured this is already true; without it, membership alone turns it
        on for anyone who is not on this machine -- so joining needs no auth
        setup, and does not quietly open the node up either. Loopback is
        untouched, so nobody is locked out of their own UI.
        """
        return self.in_cluster() and not self.auth.config.enabled

    def _install_secret(self, secret, revoke=None):
        """Hold ``secret`` as this node's cluster credential, both directions."""
        parts = (secret or "").split("_", 2)
        if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
            raise ClusterError("That is not a usable cluster credential.", 400)
        token_id = parts[1]
        record = {
            "name": CLUSTER_TOKEN_NAME,
            "owner": CLUSTER_TOKEN_OWNER,
            "role": ADMIN,
            "sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
            "created": int(time.time()),
            "expires": None,
            "last_used": None,
        }

        def mutate_tokens(records):
            records[token_id] = record
            if revoke and revoke != token_id:
                records.pop(revoke, None)
        store.update_auth("tokens", mutate_tokens)

        def mutate_secret(records):
            records["secret"] = {"token": secret, "id": token_id,
                                 "updated": int(time.time())}
        store.update_auth("cluster", mutate_secret)
        return token_id

    def _form_cluster(self):
        """Make this node a cluster of one, if it is not in one already."""
        existing = self.cluster_secret()
        if existing:
            return existing
        secret = "%s_%s_%s" % (TOKEN_PREFIX, secrets.token_hex(6),
                               secrets.token_urlsafe(32))
        self._install_secret(secret)
        _log("formed a new cluster around %s" % self.local_name())
        return secret

    def rotate_secret(self):
        """Replace the cluster credential everywhere, in one pass.

        The way a removed node is actually cut off: until this runs it still
        holds a working credential, because the credential is shared. Members
        are switched first, using the old secret, and this node last -- so a
        member that cannot be reached is left behind rather than locking us out
        of the ones that answered. It is reported, and it has to rejoin.
        """
        if not self.in_cluster():
            raise ClusterError("This node is not in a cluster.", 409)
        old = self.cluster_secret()
        old_id = (old or "").split("_", 2)[1]
        new = "%s_%s_%s" % (TOKEN_PREFIX, secrets.token_hex(6), secrets.token_urlsafe(32))

        def switch(name):
            try:
                self.client(name).set_cluster_secret(new)
                return {"node": name, "ok": True, "error": None}
            except (ClusterError, NodeError) as exc:
                return {"node": name, "ok": False, "error": exc.message}

        peers = sorted(self._peers())
        results = self._fanout(peers, switch)
        self._install_secret(new, revoke=old_id)
        stranded = [r["node"] for r in results if not r["ok"]]
        if stranded:
            _log("rotated the cluster credential; %s did not get it and is now "
                 "cut off" % ", ".join(stranded))
        else:
            _log("rotated the cluster credential across %d node(s)" % len(peers))
        return {"ok": not stranded, "nodes": peers, "stranded": stranded,
                "results": results}

    def accept_secret(self, secret):
        """Take a new cluster credential from a member that is rotating it."""
        if not self.in_cluster():
            raise ClusterError("This node is not in a cluster.", 409)
        old_id = (self.cluster_secret() or "").split("_", 2)[1]
        self._install_secret(str(secret or ""), revoke=old_id)
        _log("accepted a rotated cluster credential")
        return {"rotated": True}

    # -- the registry ------------------------------------------------------

    def _peers(self):
        return store.load_nodes()

    def _peer(self, name):
        peer = self._peers().get(name)
        if not peer:
            raise ClusterError("No node called '%s'. Join a cluster with "
                               "`lemondx cluster join`." % name, 404)
        return peer

    def client(self, name, timeout=DEFAULT_TIMEOUT):
        """A client for one peer, carrying the cluster credential."""
        peer = self._peer(name)
        secret = self.cluster_secret()
        if not secret:
            raise ClusterError(
                "This node is not in a cluster, so it has no credential to call "
                "'%s' with. Join with `lemondx cluster join`." % name, 403)
        return NodeClient(peer["url"], token=secret, fingerprint=peer["fingerprint"],
                          timeout=timeout)

    def members(self):
        """Every member as a peer would record it, this node included.

        What `GET /api/cluster/members` serves and what a joining node is given,
        so membership spreads without anyone listing it by hand.
        """
        records = [self.local_node()]
        for _, peer in sorted(self._peers().items()):
            records.append({"name": peer["name"], "url": peer["url"],
                            "fingerprint": peer["fingerprint"],
                            "description": peer["description"], "added": peer["added"],
                            "fabric": peer.get("fabric")})
        return records

    def remember_members(self, records, source="", authoritative=None):
        """Merge member records into the registry. Returns the names added.

        A member's own record wins for its own entry -- it is the only thing
        that knows its address, certificate and fabric subnet -- and this
        node's entry is never taken from someone else's copy of it.

        ``authoritative`` is the one node in ``records`` speaking for itself.
        Everything else in the list is gossip: it may introduce a node we have
        never heard of, but it may not overwrite one we already know, because
        the peer handing it over may have a stale copy. That matters most for
        something that changes at a moment: levelling with three peers pulls
        three copies of every other node, and without this the last one read
        wins over the node's own.
        """
        # Kept up to date as we go: one call is routinely handed the same node
        # twice -- a join gets the inviter both on its own and inside the member
        # list -- and re-reading the directory per record would be worse.
        known = dict(self._peers())
        added, updated, notes = [], [], []
        for raw in records or []:
            member = clean_member(raw)
            if member is None or member["name"] == self.local_name():
                continue
            current = known.get(member["name"])
            # The fabric claim is part of what a member's own record tells us:
            # left out of this comparison, a node that was just given a subnet
            # would never have it noticed here, and nothing would route to it.
            if current and (current["url"], current["fingerprint"], current.get("fabric")) \
                    == (member["url"], member["fingerprint"], member["fabric"]):
                continue
            if current and authoritative and member["name"] != authoritative:
                continue        # second-hand, and we already have its own word
            saved = store.save_node(member["name"], dict(member, added=int(
                (current or {}).get("added") or time.time())))
            (updated if current else added).append(member["name"])
            # An address change is spelled out: it is where calls start going to
            # another host, and the log is the only record of who repointed what.
            notes.append("~%s %s -> %s" % (member["name"], current["url"], member["url"])
                         if current and current["url"] != member["url"]
                         else "%s%s" % ("~" if current else "+", member["name"]))
            known[member["name"]] = saved
            with self._lock:
                self._probes.pop(member["name"], None)
        if notes:
            _log("membership from %s: %s" % (source or "a peer", ", ".join(notes)))
        if added or updated:
            # Membership changed, so the fabric's routes are now wrong. This is
            # the choke point every path funnels through -- join, announce,
            # sync_members -- so hooking it here covers them all at once.
            self._fabric().reapply()
        return {"added": added, "updated": updated}

    def announce(self, body, peer_address=None):
        """Another node telling us it is a member. Authenticated as a peer.

        The second half of a one-way join: the joiner has the credential and
        calls everyone, so every member learns about it without the member that
        issued the code having to broker anything.
        """
        member = clean_member(body if isinstance(body, dict) else {})
        if member is None:
            raise ClusterError("A member announcement needs a name, a URL and, "
                               "over HTTPS, a certificate fingerprint.", 400)
        if member["name"] == self.local_name():
            raise ClusterError(
                "That node calls itself '%s', which is this node's own name. Give "
                "one of them a different name with `lemondx configure cluster`."
                % member["name"], 409)
        self.remember_members([member], source=peer_address or member["name"],
                              authoritative=member["name"])
        return {"registered": member["name"], "members": self.members()}

    def sync_members(self):
        """Level the member list with every peer: pull theirs, push ours.

        Membership is the one thing that has to converge, and this is how --
        not a broadcast at join time, which would miss whoever was down. Any
        member run through this catches up on whoever joined while it was away.
        """
        if not self.in_cluster():
            raise ClusterError("This node is not in a cluster.", 409)
        me = self.ensure_identity()
        results = []

        def one(name):
            try:
                client = self.client(name)
                answer = client.announce(me) or {}
                return {"node": name, "ok": True, "error": None,
                        "members": answer.get("members") or []}
            except (ClusterError, NodeError) as exc:
                return {"node": name, "ok": False, "error": exc.message, "members": []}

        for outcome in self._fanout(sorted(self._peers()), one):
            results.append({k: outcome[k] for k in ("node", "ok", "error")})
            if outcome["ok"]:
                self.remember_members(outcome["members"], source=outcome["node"],
                                      authoritative=outcome["node"])
        return {"ok": all(r["ok"] for r in results), "results": results,
                "members": self.members()}

    def list_nodes(self, probe=True):
        """Every node, this one first. Reachability is probed and briefly cached."""
        local = dict(self.local_node())
        local.update({"self": True, "groups": self._groups_of(local["name"])})
        nodes = [local]
        for name, peer in sorted(self._peers().items()):
            nodes.append(dict(peer, **{"self": False, "groups": self._groups_of(name)}))
        if not probe:
            return [dict(n, state=None) for n in nodes]

        states = self._probe_all([n for n in nodes if not n["self"]])
        return [dict(node, state=self._local_state() if node["self"]
                     else states.get(node["name"])) for node in nodes]

    def _local_state(self):
        try:
            status = self.service.status()
            containers = self.service.list_containers()
        except (ServiceError, LXDError) as exc:
            return {"reachable": False, "error": str(exc), "product": None,
                    "server_version": None, "ready": False, "containers": 0, "running": 0}
        return {
            "reachable": True, "error": None,
            "product": status["product"], "server_version": status["server_version"],
            "ready": status["ready"], "containers": len(containers),
            "running": sum(1 for c in containers if c["status"] == "Running"),
        }

    def _probe_all(self, peers):
        now = time.monotonic()
        fresh, stale = {}, []
        with self._lock:
            for peer in peers:
                cached = self._probes.get(peer["name"])
                if cached and now - cached[0] < PROBE_CACHE_SECONDS:
                    fresh[peer["name"]] = cached[1]
                else:
                    stale.append(peer)
        if stale:
            for name, state in zip([p["name"] for p in stale],
                                   self._fanout(stale, self._probe_one)):
                fresh[name] = state
                with self._lock:
                    self._probes[name] = (time.monotonic(), state)
        return fresh

    def _probe_one(self, peer):
        try:
            client = self.client(peer["name"], timeout=PROBE_TIMEOUT)
            status = client.status()
            containers = client.containers()
        except (ClusterError, NodeError) as exc:
            return {"reachable": False, "error": exc.message, "product": None,
                    "server_version": None, "ready": False, "containers": 0, "running": 0}
        return {
            "reachable": True, "error": None,
            "product": status.get("product"), "server_version": status.get("server_version"),
            "ready": bool(status.get("ready")), "containers": len(containers),
            "running": sum(1 for c in containers if c.get("status") == "Running"),
        }

    def _fanout(self, items, work):
        """Run ``work`` over nodes, several at a time, keeping order."""
        if not items:
            return []
        with ThreadPoolExecutor(max_workers=min(len(items), FANOUT_WORKERS)) as pool:
            return list(pool.map(work, items))

    def _forget(self, name):
        """Drop one node here: its record, its group memberships, its probe."""
        store.delete_node(name)
        for group in store.load_node_groups().values():
            if name in group["members"]:
                store.save_node_group(group["name"], dict(
                    group, members=[m for m in group["members"] if m != name]))
        with self._lock:
            self._probes.pop(name, None)
        # A forgotten node's subnet must stop being routed here, or its address
        # space stays claimed on this host and a later member cannot be given it.
        self._fabric().reapply()

    def _tell_peers(self, name):
        """Ask every remaining member to forget ``name``. Never raises."""
        def tell(peer):
            try:
                self.client(peer).forget_member(name)
                return {"node": peer, "ok": True, "error": None}
            except (ClusterError, NodeError) as exc:
                return {"node": peer, "ok": False, "error": exc.message}
        return self._fanout(sorted(self._peers()), tell)

    def evict_node(self, name, rotate=None):
        """Put a node out of the cluster, leaving nothing of it anywhere.

        The order is the whole point. The node is told *first*, while its
        record is still here to call it with, so it gives up the credential and
        its own member list instead of carrying on as a member nobody answers
        -- still pushing templates, still forwarding calls, still listing hosts
        that have forgotten it. Only then does it go from this registry and
        from every other member's.

        Rotating the credential afterwards is what the cluster used to rely on
        for all of this, and it still happens when the node could not be told:
        an evicted node that stood down has already given its copy up, and
        rotating regardless would strand any member that merely happened to be
        switched off -- turning one deliberate eviction into two. Pass
        ``rotate`` to decide it by hand.
        """
        name = (name or "").strip()
        if name == self.local_name():
            raise ClusterError(
                "A node cannot evict itself. Run `lemondx cluster leave` on %s to "
                "take it out of the cluster from its own side." % name, 409)
        if name not in self._peers():
            raise ClusterError("No node called '%s'." % name, 404)

        try:
            self.client(name, timeout=EVICT_TIMEOUT).evicted()
            stood_down, why = True, None
        except (ClusterError, NodeError) as exc:
            stood_down, why = False, exc.message

        self._forget(name)
        told = self._tell_peers(name)

        rotating = (not stood_down) if rotate is None else bool(rotate)
        rotated = self.rotate_secret() if rotating and self.in_cluster() else None
        _log("evicted node %s%s" % (name, "" if stood_down
                                    else " (could not be told: %s)" % why))
        return {"removed": name, "stood_down": stood_down, "stand_down_error": why,
                "told": told, "rotated": rotated,
                "still_holds_credential": not stood_down and rotated is None}

    def drop_member(self, name):
        """A peer telling us a node is out. Local only: no onward broadcast.

        Otherwise one removal would ricochet around the cluster forever -- the
        node that started it is the one that tells everybody.
        """
        name = (name or "").strip()
        if name == self.local_name():
            raise ClusterError("This node cannot be asked to forget itself.", 409)
        if name not in self._peers():
            # Already gone: a removal that reaches us twice (or after we heard
            # it from the node itself) is a no-op, not a failure to report.
            return {"removed": name, "told": [], "still_holds_credential": False}
        self._forget(name)
        _log("forgot node %s at a member's request" % name)
        return {"removed": name, "told": [], "still_holds_credential": False}

    def describe_node(self, name):
        """One node with its instances, for the node's card in the UI."""
        if name == self.local_name():
            node = dict(self.local_node(), **{"self": True,
                                              "groups": self._groups_of(name)})
            return dict(node, state=self._local_state(),
                        instances=self.service.list_containers())
        peer = self._peer(name)
        node = dict(peer, **{"self": False, "groups": self._groups_of(name)})
        state = self._probe_one(peer)
        instances = []
        if state["reachable"]:
            try:
                instances = self.client(name).containers()
            except (ClusterError, NodeError):
                instances = []
        return dict(node, state=state, instances=instances)

    # -- groups ------------------------------------------------------------

    def _groups_of(self, node_name):
        return sorted(g["name"] for g in store.load_node_groups().values()
                      if node_name in g["members"])

    def list_groups(self):
        known = set(self._peers()) | {self.local_name()}
        groups = []
        for _, group in sorted(store.load_node_groups().items()):
            members = group["members"]
            groups.append(dict(group, **{
                "members": members,
                # Not stored: it is a property of the name, so a file copied
                # from another machine cannot claim to be managed or deny it.
                "managed": group["name"] in SIZE_GROUPS,
                # A group is a plain list of names, so it can name a node that
                # has since been removed or has not joined yet. Say so
                # rather than dropping it: the name is what the user wrote.
                "unknown_members": [m for m in members if m not in known],
            }))
        return groups

    def _refuse_managed(self, name, managed, verb):
        """Guard the two groups `auto_groups()` owns.

        They say what the cluster measured, so editing one by hand would leave
        a group whose name promises something it no longer means. ``managed``
        is the sizing itself, or a member relaying it -- the guard is on the
        person at either end, not on the cluster keeping itself level.
        """
        if not managed and name in SIZE_GROUPS:
            raise ClusterError(
                "'%s' is maintained by lemondx from what each node has, so it "
                "cannot be %s by hand. Run `lemondx cluster group auto` to build "
                "it again from the nodes as they are now." % (name, verb), 409)

    @staticmethod
    def refuse_in_use(kind, noun, name):
        """Refuse a person's delete of something another record still names.

        Only a person's: a member relaying a delete, or reconciliation settling
        one, is carrying out a decision this check already met where it was
        made, and refusing it here would leave the cluster disagreeing for good.
        """
        users = store.dependents(kind, name)
        if users:
            raise ClusterError(
                "%s '%s' is used by %s. Change or delete %s first."
                % (noun[:1].upper() + noun[1:], name, ", ".join(users),
                   "it" if len(users) == 1 else "them"), 409)

    def save_group(self, name, members=None, description="", propagate=True,
                   managed=False):
        name = (name or "").strip()
        self._refuse_managed(name, managed, "edited")
        if not GROUP_NAME.match(name):
            raise ClusterError(
                "Invalid group name '%s'. Use letters, digits, spaces and . _ -, "
                "up to 64 characters." % name)
        if members is not None and (not isinstance(members, list)
                                    or not all(isinstance(m, str) for m in members)):
            raise ClusterError("'members' must be a list of node names.")
        existing = store.load_node_groups().get(name)
        chosen = [m.strip() for m in (members if members is not None
                                      else (existing or {}).get("members", []))]
        record = store.save_node_group(name, {
            "name": name,
            "description": str(description or "")[:200],
            "members": [m for m in chosen if m],
        })
        # A group names nodes, so it means the same thing on every one of them:
        # kept level automatically, like templates and modules.
        return self._with_sync(record, "groups", name, propagate)

    def _capacity_of(self, name):
        """One node's CPU threads and total memory, or a NodeError/ClusterError."""
        if name == self.local_name():
            host = (self.service.resources() or {}).get("host") or {}
        else:
            host = (self.client(name).resources() or {}).get("host") or {}
        return int(host.get("cpu_threads") or 0), int(host.get("memory_total") or 0)

    def node_capacity(self):
        """What each node has, for sizing it against the rest of the cluster.

        Capacity, not what happens to be free: a group is a saved record that
        outlives the reading it was made from, so a momentary figure would be
        wrong by the time anything launched against it.
        """
        def one(name):
            try:
                cpu, memory = self._capacity_of(name)
            except (ClusterError, NodeError, ServiceError, LXDError) as exc:
                return {"node": name, "ok": False, "cpu": 0, "memory": 0,
                        "error": getattr(exc, "message", None) or str(exc)}
            if cpu <= 0 or memory <= 0:
                # A daemon that answers without figures cannot be compared with
                # one that does, and a zero would drag every other node's share
                # of the average with it.
                return {"node": name, "ok": False, "cpu": cpu, "memory": memory,
                        "error": "reported no CPU or memory"}
            return {"node": name, "ok": True, "cpu": cpu, "memory": memory,
                    "error": None}

        return self._fanout(self.all_nodes(), one)

    def auto_groups(self, propagate=True):
        """Rebuild the `large` and `small` groups from what each node has.

        Size is relative, because "large" only means anything next to the rest
        of the cluster: each node's CPU and memory are scored as a share of the
        cluster average -- the two weighted equally, so neither a core count
        nor a memory total decides on its own -- and a node at or above average
        is large, at or below it small. Nothing has to be chosen in advance,
        and a cluster of identical hosts falls out of the same arithmetic with
        every node in both groups, which is the honest answer: none of them is
        bigger or smaller than the others.

        A node that cannot be reached is left out of both rather than guessed
        at, and named in the result -- running this while a host is down would
        otherwise quietly shrink the groups it belongs to.
        """
        readings = self.node_capacity()
        usable = [r for r in readings if r["ok"]]
        if not usable:
            raise ClusterError(
                "No node could be asked what it has, so there is nothing to "
                "compare. Check that the nodes are reachable and try again.", 503)

        mean_cpu = sum(r["cpu"] for r in usable) / float(len(usable))
        mean_memory = sum(r["memory"] for r in usable) / float(len(usable))

        def place(reading):
            if not reading["ok"]:
                return dict(reading, score=None, groups=[])
            # Each resource as a share of the cluster's average, the two
            # averaged: a node with twice the memory and half the cores of its
            # peers is neither large nor small, which is the right answer.
            score = (reading["cpu"] / mean_cpu + reading["memory"] / mean_memory) / 2
            return dict(reading, score=round(score, 4), groups=[
                name for name, fits in (("large", score >= 1 - SIZE_TOLERANCE),
                                        ("small", score <= 1 + SIZE_TOLERANCE)) if fits])

        sized = [place(r) for r in readings]
        saved = [self.save_group(
            name,
            members=[r["node"] for r in sized if name in r["groups"]],
            description="sized automatically from CPU and memory",
            propagate=propagate, managed=True,
        ) for name in SIZE_GROUPS]

        skipped = [r["node"] for r in sized if not r["ok"]]
        # "Every node is the same size" is a claim about the whole cluster, so
        # a node nobody could measure makes it unknown, not true.
        uniform = not skipped and all(len(r["groups"]) == 2 for r in sized)
        _log("sized %d node(s) into large/small%s%s"
             % (len(usable), " (all the same size)" if uniform else "",
                "; skipped %s" % ", ".join(skipped) if skipped else ""))
        return {"groups": saved, "nodes": sized, "skipped": skipped,
                "uniform": uniform}

    def delete_group(self, name, everywhere=True, managed=False, relayed=False):
        self._refuse_managed((name or "").strip(), managed, "deleted")
        if not relayed:
            self.refuse_in_use("groups", "node group", (name or "").strip())
        if not store.delete_node_group((name or "").strip()):
            raise ClusterError("No such node group '%s'." % name, 404)
        return self._with_sync({"deleted": name}, "groups", name, everywhere,
                               deleted=True)

    def all_nodes(self):
        """Every member's name, this node included."""
        return sorted(set(self._peers()) | {self.local_name()})

    def resolve_targets(self, nodes=None, groups=None, everything=False):
        """Node names for a request that names nodes, groups, both or neither.

        Neither means this node: the local host is the default everywhere, so a
        cluster-unaware call behaves exactly as it did before there was one.
        ``everything`` is how a caller says "the whole cluster" without listing
        it -- a listed cluster would silently miss a node that joined since.
        """
        if everything:
            return self.all_nodes()
        known = set(self._peers()) | {self.local_name()}
        chosen = []
        for name in (nodes or []):
            name = str(name).strip()
            if name not in known:
                raise ClusterError("No node called '%s'." % name, 404)
            if name not in chosen:
                chosen.append(name)
        stored = store.load_node_groups()
        for group_name in (groups or []):
            group = stored.get(str(group_name).strip())
            if not group:
                raise ClusterError("No node group '%s'." % group_name, 404)
            for member in group["members"]:
                if member not in known:
                    raise ClusterError(
                        "Group '%s' lists '%s', which is not a node here. Fix the "
                        "group, or have that node join." % (group_name, member), 409)
                if member not in chosen:
                    chosen.append(member)
        if not chosen:
            chosen = [self.local_name()]
        return chosen


    # -- joining: the node handing out the code ----------------------------

    def create_invite(self, expires_minutes=DEFAULT_INVITE_MINUTES, note=""):
        """A one-time code any node can redeem to join this node's cluster.

        Issuing one forms a cluster if this node is not in one yet, so the first
        node is set up by the act of inviting the second -- there is nothing to
        configure first. The code carries this node's address and certificate
        fingerprint as well as the secret, so the operator moves one string and
        both directions of trust come from it: the secret proves the joiner was
        given permission, the fingerprint proves it reached the right node.
        Only the code's hash is kept here, like any other credential.
        """
        if not self.settings()["allow_enrollment"]:
            raise ClusterError(
                "Enrolment is switched off for this node (`lemondx configure cluster`).",
                409)
        try:
            minutes = float(expires_minutes)
        except (TypeError, ValueError):
            raise ClusterError("Expiry must be a number of minutes.")
        if not 1 <= minutes <= MAX_INVITE_MINUTES:
            raise ClusterError("A join code may last between 1 and %d minutes."
                               % MAX_INVITE_MINUTES)

        local = self.ensure_identity()
        self._form_cluster()
        warning = self.self_check()

        invite_id = secrets.token_hex(6)
        secret = "%s_%s_%s" % (JOIN_PREFIX, invite_id,
                               secrets.token_urlsafe(INVITE_SECRET_BYTES))
        expires = int(time.time() + minutes * 60)
        record = {
            "sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
            "created": int(time.time()),
            "expires": expires,
            "note": str(note or "")[:100],
        }

        def mutate(records):
            now = time.time()
            for key in [k for k, v in records.items()
                        if not isinstance(v, dict) or (v.get("expires") or 0) <= now]:
                del records[key]          # tidy up codes nobody redeemed
            records[invite_id] = record
        store.update_auth("invites", mutate)

        code = encode_join_code({"v": 1, "name": local["name"], "url": local["url"],
                                 "fp": local["fingerprint"], "code": secret})
        _log("issued join code %s, valid for %g minutes" % (invite_id, minutes))
        return {"id": invite_id, "code": code, "expires": expires,
                "node": local, "fingerprint_pretty": pretty_fingerprint(local["fingerprint"]),
                "expires_in_minutes": minutes, "members": len(self._peers()) + 1,
                "warning": warning}

    def list_invites(self):
        now = time.time()
        return sorted(
            ({"id": key, "created": record.get("created"), "expires": record.get("expires"),
              "note": record.get("note") or "",
              "expired": (record.get("expires") or 0) <= now}
             for key, record in store.load_auth("invites").items()
             if isinstance(record, dict)),
            key=lambda i: i["created"] or 0, reverse=True)

    def revoke_invite(self, invite_id):
        found = []

        def mutate(records):
            found.append(records.pop(invite_id, None) is not None)
        store.update_auth("invites", mutate)
        if not found[0]:
            raise ClusterError("No such join code '%s'." % invite_id, 404)
        return {"revoked": invite_id}

    def _redeem(self, presented):
        """Consume a join code, or raise. Constant-time, single use, expiring."""
        presented = (presented or "").strip()
        parts = presented.split("_", 2)
        if len(parts) != 3 or parts[0] != JOIN_PREFIX:
            raise ClusterError("That join code is not valid here.", 403)
        digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()

        # Checked read-only first so a wrong code costs a read rather than a
        # rewrite of the invites file: this endpoint answers callers with no
        # credential, and a write per bad guess is a lever worth not handing
        # out. The write below re-checks, so consuming stays atomic.
        candidate = store.load_auth("invites").get(parts[1])
        if not isinstance(candidate, dict) or not secrets.compare_digest(
                digest, str(candidate.get("sha256") or "")):
            # Unknown and mismatched are the same answer on purpose: a caller
            # must not learn which half of the code it got right.
            raise ClusterError("That join code is not valid here.", 403)

        outcome = []

        def mutate(records):
            record = records.get(parts[1])
            if not isinstance(record, dict):
                outcome.append("unknown")
                return
            if not secrets.compare_digest(digest, str(record.get("sha256") or "")):
                outcome.append("mismatch")
                return
            if (record.get("expires") or 0) <= time.time():
                del records[parts[1]]
                outcome.append("expired")
                return
            del records[parts[1]]         # one use, whatever happens next
            outcome.append("ok")
        store.update_auth("invites", mutate)

        if outcome[0] == "expired":
            raise ClusterError("That join code has expired. Make a new one with "
                               "`lemondx cluster invite`.", 403)
        if outcome[0] != "ok":
            # Reached only by losing a race with another redemption of the
            # same code, which the read above cannot see.
            raise ClusterError("That join code is not valid here.", 403)
        return parts[1]

    def enroll(self, body, peer_address=None):
        """Redeem a join code and hand back the cluster. The joined side.

        Called by another node over its own pinned TLS connection, with no
        credential other than the code -- which is the point: this is how the
        first credential gets there. What goes back is the cluster's credential
        and its whole member list, so the joiner is a member of the cluster
        rather than of this node. Everything after this is an ordinary
        authenticated API call.
        """
        if not isinstance(body, dict):
            raise ClusterError("Expected a JSON object.", 400)
        if not self.settings()["allow_enrollment"]:
            raise ClusterError("This node is not accepting new members.", 403)
        if not self.in_cluster():
            raise ClusterError(
                "This node is not in a cluster, so it has nothing to admit anyone "
                "to. Issue the code with `lemondx cluster invite`.", 409)

        invite_id = self._redeem(body.get("code"))
        joiner = clean_member(body.get("node") or {})
        if joiner is None:
            raise ClusterError(
                "A joining node has to say what it is called, where it is and "
                "what certificate it presents.", 400)
        if joiner["name"] == self.local_name():
            raise ClusterError(
                "That node calls itself '%s', which is this node's own name. Give "
                "one of them a different name with `lemondx configure cluster`."
                % joiner["name"], 409)
        # A name is the routing key, so admitting a second node under one that is
        # taken would repoint the first node's traffic -- here and, once the
        # joiner announces itself, on every other member. Two hosts default to
        # the same name easily enough (`ensure_identity()` uses the hostname),
        # and only the collision with *this* node's name is caught above, so the
        # one with a peer has to be refused rather than merged.
        clash = self._peers().get(joiner["name"])
        if clash and (clash["url"], clash["fingerprint"]) != \
                (joiner["url"], joiner["fingerprint"]):
            raise ClusterError(
                "'%s' is already a member of this cluster at %s. Names are how "
                "calls are routed between nodes, so a second one cannot take it: "
                "rename the joining node with `lemondx configure cluster` and "
                "issue a fresh code." % (joiner["name"], clash["url"]), 409)

        # Recorded before answering, so this node knows the joiner even if the
        # joiner's own announcements never arrive.
        self.remember_members([joiner], source=peer_address or joiner["name"],
                              authoritative=joiner["name"])
        _log("node %s joined the cluster with code %s from %s"
             % (joiner["name"], invite_id, peer_address or "an unknown address"))
        return {
            "node": self.local_node(),
            "secret": self.cluster_secret(),
            "members": self.members(),
            "groups": [g["name"] for g in self.list_groups()],
            # The joiner cannot allocate its own subnet -- nothing in two
            # conflicting claims says which is right -- so the node admitting
            # it picks one, the same way it hands over the credential.
            "fabrics": self._enrolment_claims(joiner["name"]),
        }

    def _enrolment_claims(self, joiner):
        """[{name, prefix, subnet, nat}] for a node being admitted, one per fabric."""
        try:
            return self._fabric().enrolment_claims(joiner)
        except Exception:                 # never fail a join over a fabric
            return []

    # -- joining: the node redeeming the code ------------------------------

    def join(self, code, description=""):
        """Redeem a join code and become a member of that cluster.

        One direction, and no setup: this node works out its own name, address
        and certificate first (``ensure_identity``), redeems the code, takes the
        cluster's credential and member list, and then announces itself to every
        member. Which member issued the code does not matter -- what is joined
        is the cluster.
        """
        payload = decode_join_code(code)
        name = str(payload["name"]).strip()
        url = str(payload["url"]).strip()
        fingerprint = str(payload.get("fp") or "").strip().lower().replace(":", "")
        if not NODE_NAME.match(name):
            raise ClusterError("That join code names an unusable node: %r." % name)
        try:
            url = normalize_url(url)
        except NodeError as exc:
            raise ClusterError("That join code names an unusable address: %s" % exc.message)
        # The rule `clean_member()` holds every member record to, applied here
        # too because this call is the one that carries a credential to a node
        # nothing has vouched for yet: over HTTPS the pinned fingerprint is the
        # only thing identifying it, and without one NodeClient falls back to CA
        # verification -- which any host with a certificate some CA signed would
        # pass, and it would be handed the code and given the cluster back.
        if parse_url(url)[0] == "https" and not FINGERPRINT.match(fingerprint):
            raise ClusterError(
                "That join code carries no certificate fingerprint for %s, so "
                "there would be nothing to recognise that node by. Issue a fresh "
                "code with `lemondx cluster invite`." % url)

        mine = self.ensure_identity()
        if name == mine["name"]:
            raise ClusterError(
                "That code is from a node calling itself '%s', which is this "
                "node's own name. Rename one of them with "
                "`lemondx configure cluster`." % name, 409)
        if self.in_cluster():
            raise ClusterError(
                "This node is already in a cluster with %d other node(s). Leave it "
                "first with `lemondx cluster leave`." % len(self._peers()), 409)

        client = NodeClient(url, fingerprint=fingerprint, timeout=DEFAULT_TIMEOUT)
        try:
            answer = client.enroll({"code": payload["code"],
                                    "node": dict(mine, description=description)})
        except NodeError as exc:
            raise ClusterError("Could not join %s: %s" % (url, exc.message), exc.code)
        if not isinstance(answer, dict) or not answer.get("secret"):
            raise ClusterError("%s answered the join without a cluster credential."
                               % url, 502)

        # Checked before the credential is installed: a member already using this
        # node's name means every call routed to that name would end up at one of
        # them arbitrarily, and remember_members() drops a record with our own
        # name rather than reporting it. Better to stay out of the cluster.
        #
        # Only a *different* host holding the name is that, though. The list we
        # are looking at always names this node: enroll() records the joiner
        # before answering, so we are in the very answer we are checking -- and
        # a cluster still holding the record from an earlier join of ours that
        # failed after this point would otherwise be unjoinable for good.
        ours = clean_member(mine) or {"url": "", "fingerprint": ""}
        taken = [m for m in map(clean_member, answer.get("members") or [])
                 if m and m["name"] == mine["name"]
                 and (m["url"], m["fingerprint"]) != (ours["url"], ours["fingerprint"])]
        if taken:
            raise ClusterError(
                "That cluster already has a node called '%s', at %s. Rename this "
                "one with `lemondx configure cluster` and join with a fresh code."
                % (mine["name"], taken[0]["url"]), 409)

        self._install_secret(str(answer["secret"]))
        remote = answer.get("node") or {}
        # The fingerprint the operator carried is the one that counts: the
        # node's own answer cannot loosen what the code pinned.
        remote = dict(remote, fingerprint=fingerprint or remote.get("fingerprint") or "")
        self.remember_members([remote] + list(answer.get("members") or []),
                              source=name, authoritative=remote.get("name"))

        # Take the subnet the cluster allocated before announcing, so the
        # claim is on this node's record by the time peers read it and they
        # route to it on the first pass rather than the next hourly one.
        fabric_note = self._accept_enrolment(answer.get("fabrics"))

        # Now that we hold the credential, tell everyone else we exist. The node
        # that admitted us already knows; the rest learn here, or on the next
        # sync_members() if they are down right now.
        spread = self.sync_members()
        warning = self.self_check()
        _log("joined the cluster through %s (%d member(s))"
             % (name, len(self.members())))
        return {
            "node": remote,
            "members": self.members(),
            "unreachable": [r["node"] for r in spread["results"] if not r["ok"]],
            "warning": warning,
            "fabrics": fabric_note,
        }

    def _accept_enrolment(self, offered):
        """Take the fabric subnets the cluster allocated, one per fabric it has.

        A refusal here is reported, not raised: the node has joined, and a
        prefix that collides with this host's own network is a thing to fix
        rather than a reason to undo a membership that otherwise works.
        `fabric extend` is how it is put right once it is.
        """
        notes = []
        for offer in offered if isinstance(offered, list) else []:
            if not isinstance(offer, dict) or not offer.get("subnet"):
                continue
            note = {"name": str(offer.get("name") or ""), "subnet": offer["subnet"],
                    "ok": True, "error": ""}
            try:
                self._fabric().accept_claim(offer.get("name"), offer["subnet"],
                                            offer.get("prefix"), offer.get("nat") is not False)
            except Exception as exc:
                note.update(ok=False, error=getattr(exc, "message", str(exc)))
                _log("joined, but could not take a subnet in fabric %s: %s"
                     % (note["name"], note["error"]))
            notes.append(note)
        return notes

    def _stand_down(self):
        """Erase every trace of membership here: credential, peers, groups.

        What leaving and being evicted have in common -- whichever end starts
        it, what has to be true here afterwards is the same. The groups go too:
        a group is a list of node names, and without the nodes it names nothing.
        """
        peers = sorted(self._peers())
        for name in peers:
            store.delete_node(name)
        for group in list(store.load_node_groups()):
            # note=False: a group going because there are no nodes left to name
            # is not a deletion the cluster decided on, and a tombstone for it
            # would push that removal into whatever cluster this node joins next.
            store.delete_node_group(group, note=False)
        # Same reasoning for every other recorded deletion. What this node still
        # holds stays in the ledger -- those are its own artifacts either way.
        store.drop_tombstones()
        # Not always three parts: standing down is also how a node with nothing
        # but leftovers is cleaned up, and there is no credential to pick an id
        # out of then.
        parts = (self.cluster_secret() or "").split("_", 2)
        token_id = parts[1] if len(parts) == 3 else None

        def drop_token(records):
            records.pop(token_id, None)
        if token_id:
            store.update_auth("tokens", drop_token)

        def drop_secret(records):
            records.pop("secret", None)
        store.update_auth("cluster", drop_secret)
        with self._lock:
            self._probes.clear()
        # The fabric was this cluster's address space. Give the claim up with
        # the credential, so the next cluster this node joins allocates it one
        # rather than finding it already holding somebody else's subnet.
        self._fabric().stand_down()
        return peers

    def leave(self):
        """Give up membership, and be forgotten by the cluster.

        The mirror of ``evict_node()`` from the other end, and told in the same
        order: every member hears about it while the credential is still here
        to tell them with, and only then is anything dropped locally. A member
        that cannot be reached keeps its record of this node -- reported,
        because the only way to clear it is to evict there.

        Nothing has to be rotated afterwards: the credential this node held is
        gone from it, which is exactly what a rotation would have achieved.

        It also works on the half-state a broken cluster leaves behind. A node
        whose credential is gone -- revoked, rotated past, a stand-down that
        failed part way -- still holds peer records and node groups, and every
        call it makes with them fails; refusing to leave because it is "not in
        a cluster" would be true and useless, since that leftover state is
        exactly what wants clearing. There is nothing to tell the peers with in
        that case, so the telling is skipped and said to have been.
        """
        me = self.local_name()
        member = self.in_cluster()
        leftovers = bool(self._peers()) or bool(store.load_node_groups())
        if not member and not leftovers:
            raise ClusterError("This node is not in a cluster.", 409)

        told = self._tell_peers(me) if member else []
        left = self._stand_down()
        stale = [row["node"] for row in told if not row["ok"]]
        if member:
            _log("left the cluster (told %d of %d member(s))"
                 % (len(told) - len(stale), len(told)))
        else:
            _log("cleared leftover cluster state (forgot %s)" % (", ".join(left) or "nobody"))
        note = ""
        if stale:
            note = ("%s could not be told and still lists this node: run `lemondx "
                    "cluster evict %s` there." % (", ".join(stale), me))
        elif not member:
            note = ("This node had no cluster credential, so no member could be told "
                    "it has gone. Any that still list '%s' need `lemondx cluster evict "
                    "%s` run there." % (me, me))
        return {"left": left, "told": told, "stale": stale,
                "had_credential": member, "note": note}

    def evicted(self, peer_address=None):
        """A member telling this node it has been put out of the cluster.

        Authenticated as a peer, like every call between members. Deliberately
        not a broadcast: the node doing the evicting tells the others itself,
        so a node standing down here never calls back into a cluster it has
        just left -- and an eviction cannot ricochet.
        """
        if not self.in_cluster():
            return {"left": [], "already": True}
        left = self._stand_down()
        _log("evicted from the cluster by %s; stood down" % (peer_address or "a member"))
        return {"left": left, "already": False}

    def probe_fingerprint(self, url):
        """What certificate an address presents right now, for an operator to compare."""
        digest = peer_fingerprint(url)
        return {"url": normalize_url(url), "fingerprint": digest,
                "fingerprint_pretty": pretty_fingerprint(digest)}

    # -- syncing what nodes hold -------------------------------------------

    def sync(self, kinds=("templates",), names=None, nodes=None, groups=None):
        """Push templates, modules or bootstrap profiles to other nodes.

        One direction on purpose: this node's copy is written over the target's.
        There is no merge and no last-writer-wins clock, because there is no
        shared clock -- the node the operator pushed from is the answer to "which
        copy is right?", and it is the only answer that needs no coordination.

        A template is pushed with the uploaded modules it uses, since a template
        naming a module the target has never seen would fail every launch there.
        """
        kinds = [k for k in (kinds or ()) if k]
        unknown = [k for k in kinds if k not in SYNC_KINDS]
        if unknown:
            raise ClusterError("Cannot sync %s. Try: %s"
                               % (", ".join(unknown), ", ".join(SYNC_KINDS)))
        if not kinds:
            raise ClusterError("Say what to sync: %s." % ", ".join(SYNC_KINDS))

        targets = [n for n in self.resolve_targets(nodes, groups) if n != self.local_name()]
        if not targets:
            raise ClusterError(
                "Syncing needs somewhere to send to, and the only node chosen was "
                "this one.", 400)
        items = self._sync_payload(kinds, names)
        if not items:
            raise ClusterError("Nothing to sync: no %s matched." % " or ".join(kinds), 404)

        def to_node(node_name):
            return self._sync_to(node_name, items)

        results = []
        for outcome in self._fanout(targets, to_node):
            results.extend(outcome)
        return {"ok": all(r["ok"] for r in results), "nodes": targets,
                "items": len(items), "results": results}

    def _sync_payload(self, kinds, names):
        """``[(kind, name, body)]`` for everything this node is about to push.

        A template or profile drags the uploaded modules it names along with it,
        whether or not "modules" was asked for: one that arrives without them is
        accepted by the far side but fails on its first launch.
        """
        wanted = None if names is None else {str(n) for n in names}
        items, module_ids = [], set()

        # A stack drags its templates along the way a template drags its
        # modules: one that arrives naming a template the far side lacks is
        # accepted there and then refused on its first launch.
        stack_templates = set()
        if "stacks" in kinds:
            for name, stack in sorted(store.load_stacks().items()):
                if wanted is not None and name not in wanted:
                    continue
                items.append(("stacks", name, {"description": stack["description"],
                                               "stages": stack["stages"]}))
                stack_templates.update(step["template"] for stage in stack["stages"]
                                       for step in stage["steps"] if step["type"] == "launch")

        for template in self.service.list_templates():
            asked = "templates" in kinds and (wanted is None or template["name"] in wanted)
            if not asked and template["name"] not in stack_templates:
                continue
            items.append(("templates", template["name"], _template_body(template)))
            module_ids.update(template["bootstrap"]["modules"])
        if "profiles" in kinds:
            for profile in self.service.list_bootstrap_profiles():
                if wanted is not None and profile["name"] not in wanted:
                    continue
                items.append(("profiles", profile["name"], {
                    "modules": profile["modules"],
                    "params": profile["params"],
                    "ssh_keys": profile["ssh_keys"],
                    "description": profile["description"],
                }))
                module_ids.update(profile["modules"])

        if "groups" in kinds:
            for group in self.list_groups():
                if wanted is not None and group["name"] not in wanted:
                    continue
                items.append(("groups", group["name"], {
                    "members": group["members"], "description": group["description"]}))

        if "users" in kinds:
            # As stored, hash and all: an account is only usable on the far
            # side if the hash goes with it, and there is no plaintext kept
            # anywhere to re-hash there. See AuthService.export_users().
            for user in self.auth.export_users(names):
                items.append(("users", user["name"], user))

        available = discover_modules()
        if "modules" in kinds:
            for module_id, module in sorted(available.items()):
                if wanted is not None and module_id not in wanted:
                    continue
                # Built-ins ship with lemondx and exist on every node already;
                # pushing one would only turn it into an upload that shadows
                # itself over there.
                if not module.get("editable"):
                    continue
                module_ids.add(module_id)

        for module_id in sorted(module_ids):
            module = available.get(module_id)
            if not module or not module.get("editable"):
                continue
            try:
                source = module_source(module_id)
            except Exception:                        # noqa: BLE001 - reported per node
                continue
            items.append(("modules", module_id, {"content": source["content"]}))

        # Modules first: a template that arrives before the module it names is
        # accepted but cannot be launched until the module follows.
        order = {"modules": 0, "profiles": 1, "templates": 2, "stacks": 3, "groups": 4,
                 "users": 5}
        return sorted(items, key=lambda item: (order[item[0]], item[1]))

    def _sync_to(self, node_name, items, timeout=DEFAULT_TIMEOUT):
        results = []
        try:
            client = self.client(node_name, timeout=timeout)
        except ClusterError as exc:
            return [{"node": node_name, "kind": kind, "name": name, "ok": False,
                     "error": exc.message} for kind, name, _ in items]
        for kind, name, body in items:
            error = None
            try:
                if kind == "templates":
                    client.save_template(name, body)
                elif kind == "profiles":
                    client.save_profile(name, body)
                elif kind == "groups":
                    client.save_group(name, body)
                elif kind == "users":
                    client.adopt_user(name, body)
                elif kind == "stacks":
                    client.save_stack(name, body)
                else:
                    client.upload_module(name, body["content"], overwrite=True)
            except NodeError as exc:
                error = exc.message
            results.append({"node": node_name, "kind": kind, "name": name,
                            "ok": error is None, "error": error})
        if all(r["ok"] for r in results):
            _log("synced %d item(s) to %s" % (len(items), node_name))
        return results

    # -- keeping shared definitions level ----------------------------------
    #
    # A template, a module, a bootstrap profile and a node group are all things
    # the whole cluster is meant to agree on, so saving one here pushes it to
    # every member at once rather than waiting for somebody to remember to sync.
    # The push is best-effort by design: the save has already happened locally
    # and must not be undone because another host is switched off. What each
    # node made of it comes back on the record, and `cluster sync` is the way to
    # catch up whatever missed it.

    def _propagate(self, kind, name, deleted=False):
        """Push one saved (or deleted) definition to every other member."""
        peers = sorted(self._peers())
        if not peers or not self.in_cluster():
            return None
        if deleted:
            results = self._fanout(peers, lambda node: self._delete_on(node, kind, name))
        else:
            items = self._sync_payload([kind], [name])
            if not items:
                return None
            results = []
            for outcome in self._fanout(peers, lambda node: self._sync_to(
                    node, items, timeout=AUTO_SYNC_TIMEOUT)):
                results.extend(outcome)
        failed = [r for r in results if not r["ok"]]
        if failed:
            _log("%s '%s' did not reach %s"
                 % (kind[:-1] if kind.endswith("s") else kind, name,
                    ", ".join(r["node"] for r in failed)))
        return {"ok": not failed, "nodes": peers, "results": results,
                "deleted": bool(deleted)}

    def _delete_on(self, node_name, kind, name):
        try:
            client = self.client(node_name, timeout=AUTO_SYNC_TIMEOUT)
            if kind == "templates":
                client.delete_template(name)
            elif kind == "profiles":
                client.delete_profile(name)
            elif kind == "groups":
                client.delete_group(name)
            elif kind == "stacks":
                client.delete_stack(name)
            else:
                client.delete_module(name)
        except NodeError as exc:
            # Already gone there is the outcome asked for, not a failure.
            if exc.code == 404:
                return {"node": node_name, "kind": kind, "name": name, "ok": True,
                        "error": None}
            return {"node": node_name, "kind": kind, "name": name, "ok": False,
                    "error": exc.message}
        except ClusterError as exc:
            return {"node": node_name, "kind": kind, "name": name, "ok": False,
                    "error": exc.message}
        return {"node": node_name, "kind": kind, "name": name, "ok": True, "error": None}

    # -- reconciling a node that has been away -----------------------------
    #
    # Propagation is a push at the moment of the save, so a node that is
    # switched off misses every one made while it was away -- and, being
    # push-only, has no way to find out. Reconciling is that missing half: the
    # node asks each member for a digest of everything shared, and the two
    # ledgers settle which way each difference goes.
    #
    # Digests rather than whole records because the comparison is the cheap
    # part and should stay that way: an hourly pass over a few dozen artifacts
    # on every member must not turn a cluster into a load generator, and only
    # what actually differs is then fetched. The digest is taken over the very
    # body `_sync_payload()` would push, so "this differs" and "this is what
    # would be sent" can never disagree.
    #
    # What it deliberately does not do is resolve a genuine conflict. Two nodes
    # holding different copies of one template is not something the data can
    # settle -- there is no shared clock and the records carry no version -- so
    # a difference on both sides is reported and left for a person to push
    # whichever copy is right. A copy that is merely out of date is not an
    # emergency; being unable to see that it is would be.

    def manifest(self, kinds=None):
        """A digest per shared artifact, with this node's change ledger.

        What a peer compares itself against. The ledger rides along because a
        digest alone cannot say whether an artifact one side lacks was never
        received or was deliberately deleted.
        """
        kinds = tuple(k for k in (kinds or RECONCILE_KINDS) if k in RECONCILE_KINDS)
        digests = {kind: {} for kind in kinds}
        for kind, name, body in self._sync_payload(kinds, None):
            # _sync_payload drags a stack's templates and a template's modules
            # in whether or not they were asked for; a manifest reports only
            # what it was asked about.
            if kind in digests:
                digests[kind][name] = _digest(body)
        changes = store.load_changes()
        return {"node": self.local_name(), "kinds": digests,
                "changes": {kind: changes.get(kind, {}) for kind in kinds}}

    def artifacts(self, wanted):
        """The bodies behind named manifest entries, for a peer pulling them."""
        asked = {(str(item.get("kind")), str(item.get("name")))
                 for item in (wanted or []) if isinstance(item, dict)}
        asked = {(kind, name) for kind, name in asked if kind in RECONCILE_KINDS}
        if not asked:
            return {"items": []}
        kinds = tuple({kind for kind, _ in asked})
        names = sorted({name for _, name in asked})
        return {"items": [{"kind": kind, "name": name, "body": body}
                          for kind, name, body in self._sync_payload(kinds, names)
                          if (kind, name) in asked]}

    def reconcile(self, apply=True, nodes=None, groups=None):
        """Settle this node's shared artifacts against every member's.

        Run when `serve` starts, hourly after that, and on demand. The order is
        local first, then outward: what this node is missing (or should have
        dropped) is put right here, and only then is the corrected local state
        pushed to the members that are behind. Doing it the other way round
        would have a node that has been off for a week push its stale copies
        over everyone else's before discovering they were stale.

        One pass at a time, whoever asked for it. The hourly thread and a
        person pressing Reconcile otherwise each act on a manifest the other
        is busy invalidating, and push corrections for drift the other has
        already settled. Waiting rather than refusing, because somebody who
        asked for a pass asked because they had just changed something. This
        is per process: a `lemondx cluster reconcile` run beside a `serve` is
        two processes and only `store`'s atomic writes stand between them.
        """
        if not self.in_cluster():
            raise ClusterError("This node is not in a cluster.", 409)
        with self._reconcile_lock:
            return self._reconcile_pass(apply, nodes, groups)

    def _reconcile_pass(self, apply, nodes, groups):
        # Every member unless told otherwise: reconciling against some of the
        # cluster would settle this node against a subset and leave it as far
        # from the rest as it started.
        chosen = self.resolve_targets(nodes, groups) if (nodes or groups) else self.all_nodes()
        targets = [n for n in chosen if n != self.local_name()]
        if not targets:
            return self._record_drift({
                "ok": True, "node": self.local_name(), "applied": bool(apply),
                "nodes": [], "unreachable": [], "actions": [], "conflicts": [],
                "checked": int(time.time())})

        def fetch(node_name):
            try:
                return {"node": node_name, "ok": True, "error": None,
                        "manifest": self.client(node_name).manifest()}
            except (ClusterError, NodeError) as exc:
                return {"node": node_name, "ok": False, "error": exc.message,
                        "manifest": None}

        fetched = self._fanout(targets, fetch)
        peers = {row["node"]: _clean_manifest(row["manifest"])
                 for row in fetched if row["ok"]}
        unreachable = [{"node": row["node"], "error": row["error"]}
                       for row in fetched if not row["ok"]]

        actions = self._inward(peers, apply)
        # Read again: what goes out, and what counts as a conflict, is judged on
        # the state _inward() has just put right, not the one it started from.
        settled = self.manifest()
        actions.extend(self._outward(settled, peers, apply))
        conflicts = self._conflicts(settled, peers)
        failed = [a for a in actions if a["applied"] and not a["ok"]]
        report = {
            "ok": not failed and not unreachable and not conflicts,
            "node": self.local_name(),
            "applied": bool(apply), "nodes": sorted(peers),
            "unreachable": unreachable, "actions": actions, "conflicts": conflicts,
            "checked": int(time.time()),
        }
        if apply and (actions or conflicts):
            _log("reconciled with %s: %d change(s), %d conflict(s)"
                 % (", ".join(sorted(peers)) or "nobody", len(actions), len(conflicts)))
        return self._record_drift(report)

    def _inward(self, peers, apply):
        """Adopt what this node missed, drop what it should have: local changes only."""
        local = self.manifest()
        actions = []
        for kind in RECONCILE_KINDS:
            mine, my_changes = local["kinds"][kind], local["changes"][kind]
            for name in sorted(_names(peers, kind) - set(mine)):
                # Somebody has it and this node does not. Either a push that
                # arrived while this node was off, or something this node
                # deleted while *they* were off -- the ledgers say which.
                deleted_here = my_changes.get(name, {})
                holders = [node for node, m in peers.items() if name in m["kinds"][kind]]
                if deleted_here.get("deleted") and all(
                        deleted_here["at"] >= _changed_at(peers[node], kind, name)
                        for node in holders):
                    continue          # our deletion is the newer word; _outward pushes it
                actions.append(self._apply_local(
                    kind, name, "pull", apply,
                    source=self._newest_copy(peers, kind, name, holders)))
            for name in sorted(set(mine)):
                # We hold it; a member may have deleted it while we were off.
                buried = [(node, _changed_at(peers[node], kind, name)) for node in peers
                          if peers[node]["changes"][kind].get(name, {}).get("deleted")
                          and name not in peers[node]["kinds"][kind]]
                newest = max((at for _, at in buried), default=0)
                if newest and newest > my_changes.get(name, {}).get("at", 0):
                    actions.append(self._apply_local(
                        kind, name, "delete", apply,
                        source=next(node for node, at in buried if at == newest)))
        return actions

    def _outward(self, local, peers, apply):
        """Push this node's now-current state to the members that are behind."""
        actions = []
        for node, peer in sorted(peers.items()):
            push, drop = [], []
            for kind in RECONCILE_KINDS:
                mine, my_changes = local["kinds"][kind], local["changes"][kind]
                theirs = peer["kinds"][kind]
                for name in sorted(set(mine) - set(theirs)):
                    # Their deletion beats our copy only if it is the newer
                    # word; otherwise ours is a save they never received.
                    their_delete = peer["changes"][kind].get(name, {})
                    if their_delete.get("deleted") and \
                            their_delete["at"] > my_changes.get(name, {}).get("at", 0):
                        continue      # _inward already dropped it here
                    push.append((kind, name))
                for name in sorted(set(theirs) - set(mine)):
                    mine_deleted = my_changes.get(name, {})
                    if mine_deleted.get("deleted") and \
                            mine_deleted["at"] >= _changed_at(peer, kind, name):
                        drop.append((kind, name))
            actions.extend(self._push(node, push, apply))
            actions.extend(self._drop(node, drop, apply))
        return actions

    def _conflicts(self, local, peers):
        """Artifacts two nodes both hold and disagree about. Reported, never resolved."""
        found = []
        for kind in RECONCILE_KINDS:
            mine = local["kinds"][kind]
            for name in sorted(mine):
                differing = sorted(node for node, peer in peers.items()
                                   if peer["kinds"][kind].get(name) not in (None, mine[name]))
                if differing:
                    found.append({"kind": kind, "name": name, "nodes": differing})
        return found

    def _newest_copy(self, peers, kind, name, holders):
        """Which member to take an artifact from, when more than one has it.

        The one that saved it most recently. They may disagree about its
        contents -- ``_conflicts()`` reports that separately -- but a node with
        nothing at all is better off holding the freshest copy than the
        alphabetically first one.
        """
        return max(holders, key=lambda node: (_changed_at(peers[node], kind, name), node))

    def _apply_local(self, kind, name, action, apply, source=None):
        row = {"node": self.local_name(), "from": source, "kind": kind, "name": name,
               "action": action, "applied": bool(apply), "ok": True, "error": None}
        if not apply:
            return row
        try:
            if action == "pull":
                self._adopt(kind, name, self._fetch_artifact(source, kind, name))
            else:
                self._remove_local(kind, name)
        except (ClusterError, NodeError, ServiceError, LXDError) as exc:
            row["ok"], row["error"] = False, getattr(exc, "message", str(exc))
        return row

    def _fetch_artifact(self, node_name, kind, name):
        answer = self.client(node_name).artifacts([{"kind": kind, "name": name}]) or {}
        for item in answer.get("items") or []:
            if item.get("kind") == kind and item.get("name") == name:
                return item.get("body")
        raise ClusterError("%s no longer has %s '%s'." % (node_name, kind, name), 404)

    def _adopt(self, kind, name, body):
        """Save an artifact taken from a member, without pushing it back out."""
        body = body if isinstance(body, dict) else {}
        if kind == "templates":
            self.service.save_template(
                name=name, image=body.get("image"),
                instance_type=body.get("type", "container"),
                cpu=body.get("cpu"), memory=body.get("memory"), disk=body.get("disk"),
                pool=body.get("pool"), network=body.get("network"),
                profiles=body.get("profiles"),
                ephemeral=bool(body.get("ephemeral", False)),
                start=bool(body.get("start", True)),
                secureboot=bool(body.get("secureboot", True)),
                bootstrap=body.get("bootstrap"),
                description=body.get("description", ""),
                name_prefix=body.get("name_prefix"), app_check=body.get("app_check"))
        elif kind == "profiles":
            self.service.save_bootstrap_profile(
                name=name, modules=body.get("modules") or [], params=body.get("params"),
                description=body.get("description", ""), ssh_keys=body.get("ssh_keys"))
        elif kind == "groups":
            # Through save_group, so `large` and `small` keep the one route that
            # may write them -- a member's sizing is exactly what managed= is for.
            self.save_group(name, members=body.get("members"),
                            description=body.get("description", ""), propagate=False,
                            managed=name in SIZE_GROUPS)
        elif kind == "stacks":
            self._stack_service().save_stack(name, stages=body.get("stages"),
                                             description=body.get("description", ""),
                                             propagate=False)
        else:
            self.service.upload_module(name, body.get("content"), overwrite=True)

    def _remove_local(self, kind, name):
        if kind == "templates":
            self.service.delete_template(name)
        elif kind == "profiles":
            self.service.delete_bootstrap_profile(name)
        elif kind == "groups":
            self.delete_group(name, everywhere=False, managed=name in SIZE_GROUPS,
                              relayed=True)
        elif kind == "stacks":
            self._stack_service().delete_stack(name, everywhere=False)
        else:
            self.service.remove_module(name)

    def _stack_service(self):
        """The StackService, for saving a stack to the rules a person's save meets.

        Imported here rather than at the top: stacks.py sits above this module
        and imports from it, so the dependency can only go this way at runtime.
        """
        if self._stacks is None:
            from .stacks import StackService
            self._stacks = StackService(self)
        return self._stacks

    def _fabric(self):
        """The FabricService, for the routes that carry traffic between nodes.

        Lazy for the same reason as stacks: fabric.py sits above this module
        and reads the member list, so the dependency can only go this way at
        runtime. Building it costs nothing on a node that never federates.
        """
        if self._fabric_service is None:
            from .fabric import FabricService
            self._fabric_service = FabricService(self)
        return self._fabric_service

    def fabric(self):
        """The fabric, for the API and the CLI."""
        return self._fabric()

    def _fabric_configure(self, name, iface, bridge):
        """Configure a new instance's fabric NIC. Never fails the launch."""
        try:
            return self._fabric().configure_guest(name, iface, bridge)
        except Exception:
            return None

    def _fabric_bridge(self, wanted):
        """This node's bridge for fabric ``wanted``, or "" when it is not on it."""
        try:
            return self._fabric().resolve(wanted)
        except Exception:                 # never fail a launch over a fabric
            return ""

    def _push(self, node_name, items, apply):
        rows = [{"node": node_name, "from": self.local_name(), "kind": kind,
                 "name": name, "action": "push", "applied": bool(apply),
                 "ok": True, "error": None} for kind, name in items]
        if not items or not apply:
            return rows
        payload = self._sync_payload(tuple({k for k, _ in items}),
                                     sorted({n for _, n in items}))
        wanted = set(items)
        outcome = {(r["kind"], r["name"]): r for r in self._sync_to(
            node_name, [i for i in payload if (i[0], i[1]) in wanted])}
        for row in rows:
            result = outcome.get((row["kind"], row["name"]))
            if result and not result["ok"]:
                row["ok"], row["error"] = False, result["error"]
        return rows

    def _drop(self, node_name, items, apply):
        rows = []
        for kind, name in items:
            row = {"node": node_name, "from": self.local_name(), "kind": kind,
                   "name": name, "action": "delete", "applied": bool(apply),
                   "ok": True, "error": None}
            if apply:
                result = self._delete_on(node_name, kind, name)
                row["ok"], row["error"] = result["ok"], result["error"]
            rows.append(row)
        return rows

    def _record_drift(self, report):
        with self._lock:
            self._drift = report
        return report

    def drift(self):
        """The last reconciliation's report, without setting another going."""
        with self._lock:
            return self._drift

    def start_reconciler(self, interval=RECONCILE_INTERVAL, delay=RECONCILE_DELAY):
        """A daemon thread that reconciles shortly after startup and then hourly.

        Startup is the point of it: a node that has been off is out of date the
        moment it returns, and nothing else would tell it. The repeat is for the
        pushes that failed while everyone was up -- best-effort propagation
        means a member that was merely busy stays behind for ever otherwise.
        """
        def loop():
            while True:
                time.sleep(delay if self._drift is None else interval)
                if not self.in_cluster():
                    continue
                try:
                    # Waits its turn behind a manual pass rather than skipping
                    # this hour: a chore thread has nothing to lose by waiting,
                    # and the pass it skipped would be an hour late.
                    self.reconcile()
                except (ClusterError, NodeError, ServiceError, LXDError) as exc:
                    _log("reconciliation failed: %s" % getattr(exc, "message", exc))
                except Exception as exc:              # noqa: BLE001 - a chore thread
                    _log("reconciliation failed: %s" % exc)

        thread = threading.Thread(target=loop, name="lemondx-reconcile", daemon=True)
        thread.start()
        return thread

    def save_template(self, propagate=True, **kwargs):
        record = self.service.save_template(**kwargs)
        return self._with_sync(record, "templates", record["name"], propagate)

    def delete_template(self, name, everywhere=True, relayed=False):
        if not relayed:
            self.refuse_in_use("templates", "template", name)
        record = self.service.delete_template(name)
        return self._with_sync(record, "templates", name, everywhere, deleted=True)

    def upload_module(self, name, content, overwrite=False, propagate=True):
        record = self.service.upload_module(name, content, overwrite)
        return self._with_sync(record, "modules", record["id"], propagate)

    def remove_module(self, module_id, everywhere=True, relayed=False):
        module = discover_modules().get(normalise_module_id(module_id))
        # Removing a shadow brings the built-in back under the same id, so
        # nothing naming it is left without a module to run; a module that
        # cannot be deleted at all is left to say why itself.
        if not relayed and module and module["editable"] and not module["builtin"] \
                and not module["shadows_builtin"]:
            self.refuse_in_use("modules", "module", module["id"])
        record = self.service.remove_module(module_id)
        # A built-in that was only shadowed still exists everywhere, so the
        # upload coming off this node is what the others are told about.
        return self._with_sync(record, "modules", module_id, everywhere, deleted=True)

    def save_bootstrap_profile(self, propagate=True, **kwargs):
        record = self.service.save_bootstrap_profile(**kwargs)
        return self._with_sync(record, "profiles", record["name"], propagate)

    def delete_bootstrap_profile(self, name, everywhere=True):
        record = self.service.delete_bootstrap_profile(name)
        return self._with_sync(record, "profiles", name, everywhere, deleted=True)

    def _with_sync(self, record, kind, name, propagate, deleted=False):
        """The saved record, with what the rest of the cluster made of it."""
        if not propagate:
            return record
        outcome = self._propagate(kind, name, deleted=deleted)
        return dict(record, synced=outcome) if isinstance(record, dict) else record

    # -- launching across nodes --------------------------------------------

    def launch_template(self, name, count=1, nodes=None, groups=None, params=None,
                        prefix=None, background=False, sync=True, names=None, stack=None):
        """Launch a template's instances spread over several nodes.

        The run is recorded on this node's template exactly as a local launch
        is, so every client follows it the same way -- with each instance saying
        which node it landed on. Instance names are allocated across all the
        chosen nodes at once, so a name means one instance in the cluster rather
        than one per host -- as far as one coordinator can tell; see
        ``_share_names()`` for what that is and is not worth.

        A node that cannot honour the template's storage pool or network
        substitutes its own default rather than failing; the run's notes say so,
        per node. What a node cannot substitute -- an image it cannot pull, no
        space -- fails that node's instances and leaves the rest alone.
        """
        targets = self.resolve_targets(nodes, groups)
        local_name = self.local_name()
        if targets == [local_name]:
            # Nothing federated about this one: the same call a lemondx that has
            # never been joined to anything makes.
            return self.service.launch_template(name, count=count, prefix=prefix,
                                                params=params, names=names,
                                                background=background, stack=stack)
        if names is not None:
            raise ClusterError(
                "Instance names cannot be chosen for a launch spread over several "
                "nodes: each node is given its share of one numbering.")
        # Validated here, before anything is created anywhere: a missing secret
        # or a deleted module would otherwise fail on every node separately.
        template, bootstrap, _, total, chosen_prefix, local_notes = \
            self.service.prepare_launch(name, count=count, prefix=prefix, params=params,
                                        place=local_name in targets)
        stack = self.service.stack_tag(stack)

        shares = self._share_names(chosen_prefix, total, targets)
        # The template as saved, not as placed for this host: what goes to a
        # peer must be the operator's template, so that node can fall back to
        # its own defaults rather than inherit this one's.
        raw = next(t for t in self.service.list_templates() if t["name"] == template["name"])

        def work():
            plans = [(node, shares[node]) for node in targets if shares[node]]
            collected, notes = [], []
            for outcome in self._fanout(plans, lambda plan: self._launch_on(
                    plan[0], raw, template, local_notes, bootstrap, plan[1], params, sync,
                    stack)):
                collected.extend(outcome[0])
                notes.extend(outcome[1])
            return collected, notes

        return self.service.track_run(
            template["name"], "launch", total, work, background,
            nodes=[t for t in targets if shares[t]])

    def _share_names(self, prefix, total, targets):
        """Instance names for each node: round robin, and unique across them all.

        A name is free only if *no* member holds it, not merely no chosen one:
        `web-3` should mean one instance in the cluster, and a launch aimed at
        two nodes out of five must not reuse a name the other three have. Names
        for a node that cannot be reached are allocated anyway -- its share
        fails, and taking the numbers out would renumber everyone else's.

        **Deliberately best-effort.** This is a snapshot, then a fan-out: two
        launches started at the same moment -- from two nodes, or here with one
        `--prefix` given to two templates -- can both see `web-3` free and each
        create it, on different hosts. Closing that needs a lock or a leader to
        allocate from, which is the one thing federation here refuses to have,
        and the cost of losing the race is small: an instance's identity is node
        plus name everywhere (`keyOf()` in the UI, `{node, name}` in the API), so
        a duplicate across two hosts is untidy rather than ambiguous, and two
        instances of one name on the *same* host is refused by the daemon and
        reported against that share.
        """
        taken = set()
        for names in self._fanout(self.all_nodes(), self._instance_names):
            taken.update(names)
        allocated, index = [], 1
        while len(allocated) < total:
            candidate = "%s-%d" % (prefix, index)
            if candidate not in taken:
                allocated.append(candidate)
            index += 1
        shares = {node: [] for node in targets}
        for position, instance in enumerate(allocated):
            shares[targets[position % len(targets)]].append(instance)
        return shares

    def _instance_names(self, node_name):
        if node_name == self.local_name():
            try:
                return {c["name"] for c in self.service.list_containers()}
            except (ServiceError, LXDError):
                return set()
        try:
            return {c.get("name") for c in self.client(node_name).containers()}
        except (ClusterError, NodeError):
            return set()

    def _launch_on(self, node_name, template, placed, placed_notes, bootstrap, names,
                   params, sync, stack=None):
        """One node's share. Returns ``(instances, notes)``; never raises.

        ``placed`` is the template already adjusted to this host, from the
        validation every launch does up front -- so the local share is not
        placed a second time, which would repeat every note.
        """
        if node_name == self.local_name():
            instances = self.service.launch_instances(placed, bootstrap, names, stack)
            return ([dict(i, node=node_name) for i in instances],
                    ["%s: %s" % (node_name, n) for n in placed_notes])
        try:
            client = self.client(node_name)
            if sync:
                failures = [r for r in self._sync_to(
                    node_name, self._sync_payload(["templates"], [template["name"]]))
                    if not r["ok"]]
                if failures:
                    raise NodeError(
                        "could not copy the template there first: %s"
                        % "; ".join(f["error"] or "failed" for f in failures), 502)
            started = client.launch(template["name"], names, params=params, background=True,
                                    stack=stack)
            run = self._await_run(client, template["name"], started)
        except (ClusterError, NodeError) as exc:
            _log("launch on %s failed: %s" % (node_name, exc.message))
            # Not prefixed with the node: the record names it already, and
            # every front end shows that alongside the message.
            return ([{"name": instance, "ok": False, "node": node_name,
                      "error": exc.message, "container": None}
                     for instance in names], [])
        result = run.get("result") or {}
        instances = result.get("instances") or []
        if not instances and run.get("error"):
            instances = [{"name": instance, "ok": False, "error": run["error"],
                          "container": None} for instance in names]
        return ([dict(i, node=node_name) for i in instances],
                ["%s: %s" % (node_name, note) for note in (run.get("notes") or [])])

    def _await_run(self, client, template, started):
        """Follow a run this node started on a peer until it finishes.

        Started in the background over there and polled from here rather than
        held open for the minutes an image pull takes: a connection kept open
        that long is the one thing most likely to be dropped by something in
        between, and the peer would carry on regardless with nobody reading the
        answer.
        """
        marker = started.get("started_at") if isinstance(started, dict) else None
        if isinstance(started, dict) and started.get("finished_at") is not None:
            return started
        deadline = time.monotonic() + REMOTE_RUN_DEADLINE
        while True:
            time.sleep(REMOTE_POLL_SECONDS)
            runs = client.template_runs() or []
            run = next((r for r in runs if r.get("template") == template
                        and (marker is None or r.get("started_at") == marker)), None)
            if run is None:
                raise NodeError("the launch stopped being reported there; check that "
                                "node for what happened.", 502)
            if run.get("finished_at") is not None:
                return run
            if time.monotonic() > deadline:
                raise NodeError("the launch is still running there after %d minutes; "
                                "it was left to finish."
                                % (REMOTE_RUN_DEADLINE // 60), 504)

    # -- calling one node's own API ----------------------------------------

    # How long to wait on a proxied call, by what it is. A bootstrap run
    # installs packages inside a container and is the one thing here that can
    # genuinely take many minutes; everything else is a normal API call.
    PROXY_TIMEOUTS = (("/bootstrap", 1800), ("/exec", 600))

    def proxy_timeout(self, path):
        for suffix, seconds in self.PROXY_TIMEOUTS:
            if path.endswith(suffix):
                return seconds
        return LONG_TIMEOUT

    def proxy(self, method, node, path, body=None, params=None):
        """Make one call against another member's own API, as this node.

        What lets the container drawer work on an instance wherever it lives:
        snapshots, limits, a console command and a bootstrap run are all
        ordinary per-container endpoints, and forwarding them whole is far less
        to get wrong than mirroring each one. The caller's access is checked
        against the *target* route before anything is sent -- see
        ``server.py`` -- so proxying cannot widen what someone may do.
        """
        client = self.client(node, timeout=self.proxy_timeout(path))
        return client.request(method, path, body=body or None,
                              params={k: v for k, v in (params or {}).items()})

    # -- acting on instances anywhere --------------------------------------
    #
    # The Containers tab can be switched from this host to the whole cluster,
    # and its buttons have to keep working when a row belongs to another node.
    # Each of these takes ``[{"node": ..., "name": ...}]``, groups by node and
    # fans out, so one selection can span hosts and every instance still
    # reports its own outcome.

    def _by_node(self, instances, what="instance"):
        """Group ``[{node, name}]`` by node, defaulting a missing node to this one."""
        if not isinstance(instances, list) or not instances:
            raise ClusterError("List the %ss to act on." % what, 400)
        known = set(self._peers()) | {self.local_name()}
        grouped = {}
        for entry in instances:
            if isinstance(entry, str):
                entry = {"node": self.local_name(), "name": entry}
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise ClusterError("Each %s needs a name and the node it is on." % what, 400)
            node = str(entry.get("node") or self.local_name())
            if node not in known:
                raise ClusterError("No node called '%s'." % node, 404)
            grouped.setdefault(node, []).append(entry["name"])
        return grouped

    def change_state(self, instances, action, force=False, timeout=60):
        """One state change over instances that may live on different nodes."""
        grouped = self._by_node(instances)

        def one(node):
            names = grouped[node]
            try:
                if node == self.local_name():
                    result = self.service.change_state_many(
                        names, action, force=force, timeout=timeout)
                else:
                    result = self.client(node, timeout=LONG_TIMEOUT).change_state(
                        names, action, force=force, timeout=timeout)
                return [dict(i, node=node) for i in (result or {}).get("instances", [])]
            except (ClusterError, NodeError, ServiceError, LXDError) as exc:
                message = getattr(exc, "message", str(exc))
                return [{"name": n, "node": node, "ok": False, "error": message}
                        for n in names]

        results = []
        for outcome in self._fanout(sorted(grouped), one):
            results.extend(outcome)
        return {"ok": all(r["ok"] for r in results), "instances": results}

    def delete_containers(self, instances, force=False):
        """Delete instances that may live on different nodes."""
        grouped = self._by_node(instances)

        def one(node):
            names = grouped[node]
            local = node == self.local_name()
            client = None if local else self.client(node, timeout=LONG_TIMEOUT)

            def remove(name):
                try:
                    if local:
                        self.service.delete_container(name, force=force)
                    else:
                        client.delete_container(name, force=force)
                    return {"name": name, "node": node, "ok": True, "error": None}
                except (ClusterError, NodeError, ServiceError, LXDError) as exc:
                    return {"name": name, "node": node, "ok": False,
                            "error": getattr(exc, "message", str(exc))}
            try:
                if not local:
                    self.client(node)          # fail fast on an unusable node
            except ClusterError as exc:
                return [{"name": n, "node": node, "ok": False, "error": exc.message}
                        for n in names]
            return [remove(n) for n in names]

        results = []
        for outcome in self._fanout(sorted(grouped), one):
            results.extend(outcome)
        return {"ok": all(r["ok"] for r in results), "instances": results}

    # -- template runs across nodes ----------------------------------------

    def template_action(self, name, action, instances, params=None, command=None,
                        timeout=300, background=False):
        """Destroy, recreate or exec over a template's instances, wherever they are.

        Recorded as one run on this node's template, exactly as the local
        versions are, so every client follows it the same way and two runs
        cannot overlap on the same template. Each node is handed its own share
        and validates it against its own tagged set, so a node that has since
        gained or lost an instance refuses its share rather than the whole run
        silently acting on the wrong thing.
        """
        if action not in ("destroy", "recreate", "exec"):
            raise ClusterError("Unknown template action '%s'." % action, 400)
        grouped = self._by_node(instances)
        if list(grouped) == [self.local_name()]:
            # Nothing federated about it: the same call an unjoined lemondx makes.
            local = grouped[self.local_name()]
            if action == "destroy":
                return self.service.destroy_template_instances(name, local, background)
            if action == "recreate":
                return self.service.recreate_template_instances(
                    name, local, params=params, background=background)
            return self.service.exec_template_instances(
                name, command, local, timeout=timeout, background=background)

        total = sum(len(v) for v in grouped.values())

        def work():
            collected, notes = [], []
            for outcome in self._fanout(sorted(grouped), lambda node: self._act_on(
                    node, name, action, grouped[node], params, command, timeout)):
                collected.extend(outcome[0])
                notes.extend(outcome[1])
            return collected, notes

        return self.service.track_run(name, action, total, work, background,
                                      command=command, nodes=sorted(grouped))

    def _act_on(self, node_name, template, action, names, params, command, timeout):
        """One node's share of a template run. ``(instances, notes)``; never raises."""
        if node_name == self.local_name():
            try:
                if action == "destroy":
                    results, notes = self.service.destroy_instances(template, names), []
                elif action == "recreate":
                    results, notes = self.service.recreate_instances(
                        template, names, params=params)
                else:
                    results, notes = self.service.exec_instances(
                        template, command, names, timeout=timeout), []
            except (ServiceError, LXDError) as exc:
                return ([{"name": n, "node": node_name, "ok": False,
                          "error": str(exc), "container": None} for n in names], [])
            return ([dict(r, node=node_name) for r in results],
                    ["%s: %s" % (node_name, n) for n in notes])

        try:
            client = self.client(node_name)
            if action == "destroy":
                started = client.destroy(template, names)
            elif action == "recreate":
                started = client.recreate(template, names, params=params)
            else:
                started = client.exec_instances(template, command, names, timeout=timeout)
            run = self._await_run(client, template, started)
        except (ClusterError, NodeError) as exc:
            _log("%s on %s failed: %s" % (action, node_name, exc.message))
            return ([{"name": n, "node": node_name, "ok": False, "error": exc.message,
                      "container": None} for n in names], [])
        result = run.get("result") or {}
        results = result.get("instances") or []
        if not results and run.get("error"):
            results = [{"name": n, "ok": False, "error": run["error"], "container": None}
                       for n in names]
        return ([dict(r, node=node_name) for r in results],
                ["%s: %s" % (node_name, note) for note in (run.get("notes") or [])])

    # -- reading across nodes ----------------------------------------------

    def containers(self, nodes=None, groups=None, everything=False):
        """Every instance on the chosen nodes, each tagged with the node it is on.

        Each node's latest health records come along, tagged the same way.
        Every node still judges its own instances on its own schedule -- this
        only reads what each one last concluded, from memory, so it adds no
        checks. A node whose health cannot be read (checks off, or an older
        lemondx) contributes none; its instances are still listed.
        """
        targets = self.resolve_targets(nodes, groups, everything)

        def one(node_name):
            if node_name == self.local_name():
                try:
                    found = self.service.list_containers()
                except (ServiceError, LXDError) as exc:
                    return node_name, [], {}, str(exc)
                return node_name, found, self.service.health(), None
            try:
                client = self.client(node_name)
                found = client.containers()
            except (ClusterError, NodeError) as exc:
                return node_name, [], {}, exc.message
            try:
                report = client.health() or {}
            except NodeError:
                report = {}
            return node_name, found, report, None

        instances, health, monitored, errors = [], [], [], []
        for node_name, found, report, error in self._fanout(targets, one):
            if error:
                errors.append({"node": node_name, "error": error})
            instances.extend(dict(c, node=node_name) for c in found)
            health.extend(dict(r, node=node_name) for r in report.get("instances") or [])
            # Which nodes are checking at all, so a running instance with no
            # record yet reads as pending there and as unchecked elsewhere.
            if report.get("enabled"):
                monitored.append(node_name)
        return {"nodes": targets, "instances": instances, "health": health,
                "monitored": monitored, "errors": errors}


def _digest(body):
    """The fingerprint of one artifact, taken over the body that would be pushed.

    Separators and sorted keys so two nodes that hold the same record agree on
    the bytes; ensure_ascii off so a description in any language hashes the
    same either side.
    """
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _clean_manifest(raw):
    """A peer's manifest, held to the shape ours has. It is untrusted input."""
    raw = raw if isinstance(raw, dict) else {}
    kinds, changes = {}, {}
    source_kinds = raw.get("kinds") if isinstance(raw.get("kinds"), dict) else {}
    source_changes = raw.get("changes") if isinstance(raw.get("changes"), dict) else {}
    for kind in RECONCILE_KINDS:
        entries = source_kinds.get(kind)
        kinds[kind] = {str(name): str(digest)
                       for name, digest in (entries or {}).items()
                       if isinstance(name, str) and isinstance(digest, str)} \
            if isinstance(entries, dict) else {}
        ledger = source_changes.get(kind)
        changes[kind] = {
            str(name): {"at": max(0, int(entry.get("at") or 0)),
                        "deleted": entry.get("deleted") is True}
            for name, entry in (ledger or {}).items()
            if isinstance(name, str) and isinstance(entry, dict)
            and isinstance(entry.get("at"), int)
        } if isinstance(ledger, dict) else {}
    return {"node": str(raw.get("node") or ""), "kinds": kinds, "changes": changes}


def _names(peers, kind):
    """Every artifact name of one kind held anywhere in the cluster."""
    found = set()
    for peer in peers.values():
        found |= set(peer["kinds"][kind])
    return found


def _changed_at(peer, kind, name):
    """When a member last touched one artifact, or 0 if it has never said."""
    return peer["changes"][kind].get(name, {}).get("at", 0)


def _template_body(template):
    """A template as the REST API takes it, for pushing to another node."""
    return {
        "image": template["image"],
        "type": template["type"],
        "cpu": template["cpu"] or None,
        "memory": template["memory"] or None,
        "disk": template["disk"] or None,
        "pool": template["pool"] or None,
        "network": template["network"] or None,
        "fabric": template.get("fabric") or "",
        "profiles": template["profiles"],
        "ephemeral": template["ephemeral"],
        "start": template["start"],
        "secureboot": template["secureboot"],
        "description": template["description"],
        "name_prefix": template["name_prefix"],
        "bootstrap": {
            "modules": template["bootstrap"]["modules"],
            "params": template["bootstrap"]["params"],
            # Already the single-line form the far side parses: the service
            # hands out keys as strings (_public_selection), not records.
            # Secrets are already gone -- a template never stores one.
            "ssh_keys": template["bootstrap"]["ssh_keys"],
        },
        # A PUT replaces the whole template, so a field left out here is not
        # "unchanged" on the far side but deleted there.
        "app_check": template.get("app_check"),
    }

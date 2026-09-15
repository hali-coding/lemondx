"""Who is calling, and what they may do: tokens, sessions, users, roles.

``AuthService`` is to authentication what ``ContainerService`` is to the
domain: the one place the logic lives, called by both the HTTP server and the
CLI so they cannot disagree about what a token or a user is. The server only
extracts credentials from a request and applies the answer.

Everything is opt-in. With no ``--auth`` method and no static token,
``enabled`` is false and every request is an anonymous admin, which is how
lemondx behaved before any of this existed.

Roles are deliberately coarse -- ``read`` can look, ``admin`` can change --
because the daemon's own access model is all-or-nothing: anyone who can
create a container can become root on the host.
"""

from __future__ import annotations

import base64
import getpass
import grp
import hashlib
import ipaddress
import os
import pwd
import re
import secrets
import threading
import time

from . import pam, store
from .lxd import ADMIN_GROUP

READ = "read"
ADMIN = "admin"
ROLES = (READ, ADMIN)
_RANK = {READ: 1, ADMIN: 2}

METHODS = ("local", "pam", "proxy", "token")
PASSWORD_METHODS = ("local", "pam")

SESSION_COOKIE = "lemondx_session"
TOKEN_PREFIX = "lmdx"

MIN_PASSWORD = 8
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@-]{0,63}$")
_TOKEN_NAME_LIMIT = 64

# How stale a session's role may get before it is re-derived from its source.
# Local users are re-checked whenever users.json changes, so this only bounds
# how long a PAM user keeps access after leaving the admin group.
RECHECK_SECONDS = 300
# last_used is for "is this token still in use?", not auditing. Writing it on
# every request would rewrite tokens.json every three seconds per open tab.
LAST_USED_FLUSH = 300

# Per username, and a looser limit per address: several people behind one NAT
# or proxy should not lock each other out after a handful of typos.
LOCKOUT_AFTER = {"user": 5, "ip": 20}
LOCKOUT_BASE = 15
LOCKOUT_MAX = 900
_FAILURE_MEMORY = 3600

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
PBKDF2_ITERATIONS = 600000


class AuthError(Exception):
    def __init__(self, message, code=401):
        super().__init__(message)
        self.message = message
        self.code = code


class Principal:
    """An authenticated caller. ``via`` is how they got in, for logs and rules."""

    __slots__ = ("name", "role", "via")

    def __init__(self, name, role, via):
        self.name = name
        self.role = role
        self.via = via

    def can(self, role):
        return _RANK.get(self.role, 0) >= _RANK.get(role or ADMIN, 2)

    def to_dict(self):
        return {"name": self.name, "role": self.role, "via": self.via}


def local_principal():
    """The CLI's identity: whoever can already read and write the data directory."""
    try:
        name = getpass.getuser()
    except (KeyError, OSError):
        name = "uid-%d" % os.getuid()
    return Principal(name, ADMIN, "cli")


class AuthConfig:
    """What ``serve`` was asked to enforce. Plain values; built by the CLI."""

    def __init__(self, methods=(), static_token=None, pam_service="lemondx",
                 pam_admin_groups=None, pam_read_groups=(), trusted_proxies=(),
                 proxy_user_header="X-Forwarded-User", proxy_groups_header=None,
                 proxy_admin_group=None, proxy_read_group=None, session_hours=12,
                 allow_insecure_login=False):
        unknown = [m for m in methods if m not in METHODS]
        if unknown:
            raise AuthError("Unknown auth method: %s" % ", ".join(unknown), 400)
        # In the order given: it is the order a password is tried in.
        self.methods = []
        for method in methods:
            if method not in self.methods:
                self.methods.append(method)
        self.static_token = static_token or None
        self.pam_service = pam_service
        # Default to the daemon's own admin group(s): the people who could
        # already drive LXD from a shell are the ones who may do it here.
        if pam_admin_groups is None:
            pam_admin_groups = default_pam_admin_groups()
        self.pam_admin_groups = list(pam_admin_groups)
        self.pam_read_groups = list(pam_read_groups or ())
        self.trusted_proxies = [_network(p) for p in trusted_proxies or ()]
        if "proxy" in self.methods and not self.trusted_proxies:
            raise AuthError("--auth proxy needs at least one --trust-proxy address: "
                            "without it any client could claim to be anyone.", 400)
        self.proxy_user_header = proxy_user_header
        self.proxy_groups_header = proxy_groups_header
        self.proxy_admin_group = proxy_admin_group
        self.proxy_read_group = proxy_read_group
        self.session_seconds = max(1, int(float(session_hours) * 3600))
        self.allow_insecure_login = bool(allow_insecure_login)

    @property
    def enabled(self):
        return bool(self.methods or self.static_token)

    @property
    def password_login(self):
        return any(m in self.methods for m in PASSWORD_METHODS)


def _group_exists(name):
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def _network(value):
    try:
        return ipaddress.ip_network(value.strip(), strict=False)
    except ValueError:
        raise AuthError("Not an IP address or network: %r" % value, 400)


# -- passwords -------------------------------------------------------------


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def hash_password(password):
    salt = secrets.token_bytes(16)
    if hasattr(hashlib, "scrypt"):
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
        return "scrypt$%d$%d$%d$%s$%s" % (SCRYPT_N, SCRYPT_R, SCRYPT_P, _b64(salt), _b64(digest))
    # hashlib.scrypt needs OpenSSL 1.1+; PBKDF2 is always there.
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ITERATIONS, _b64(salt), _b64(digest))


def verify_password(password, encoded):
    try:
        scheme, rest = encoded.split("$", 1)
        if scheme == "scrypt":
            n, r, p, salt, digest = rest.split("$")
            expected = base64.b64decode(digest)
            actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt),
                                    n=int(n), r=int(r), p=int(p), dklen=len(expected))
        elif scheme == "pbkdf2_sha256":
            iterations, salt, digest = rest.split("$")
            expected = base64.b64decode(digest)
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                         base64.b64decode(salt), int(iterations))
        else:
            return False
    except (ValueError, TypeError, AttributeError):
        return False               # a hand-edited or truncated hash: no match
    return secrets.compare_digest(actual, expected)


# Checked against when the user does not exist, so a missing account costs the
# same time as a wrong password and response timing does not list usernames.
_DUMMY_HASH = None


def _dummy_hash():
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(8))
    return _DUMMY_HASH


# -- the service -----------------------------------------------------------


class AuthService:
    def __init__(self, config=None):
        self.config = config or AuthConfig()
        self._lock = threading.Lock()
        self._cache = {}             # kind -> (mtime, records)
        self._sessions = {}          # session id -> dict
        self._failures = {}          # throttle key -> [count, last_failure]
        self._last_used = {}         # token id -> time seen in memory
        local = local_principal()
        self._anonymous = Principal(local.name, ADMIN, "none")

    # -- records -----------------------------------------------------------

    def _records(self, kind):
        mtime = store.auth_mtime(kind)
        cached = self._cache.get(kind)
        if cached and cached[0] == mtime:
            return cached[1]
        records = store.load_auth(kind) if mtime is not None else {}
        clean = _clean_user if kind == "users" else _clean_token
        records = {k: v for k, v in ((k, clean(k, v)) for k, v in records.items()) if v}
        self._cache[kind] = (mtime, records)
        return records

    # -- status ------------------------------------------------------------

    def info(self, principal):
        return {
            "enabled": self.config.enabled,
            "methods": list(self.config.methods) + (
                ["token"] if self.config.static_token and "token" not in self.config.methods else []),
            "password_login": self.config.password_login,
            "principal": principal.to_dict() if principal else None,
        }

    def anonymous(self):
        """The principal every request gets when auth is off."""
        return self._anonymous

    # -- API tokens --------------------------------------------------------

    def authenticate_token(self, raw):
        """The principal for a presented token, or None. Never raises."""
        raw = (raw or "").strip()
        if not raw:
            return None
        static = self.config.static_token
        if static and secrets.compare_digest(raw.encode("utf-8"), static.encode("utf-8")):
            return Principal("api-token", ADMIN, "static-token")
        parts = raw.split("_", 2)
        if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
            return None
        record = self._records("tokens").get(parts[1])
        if not record:
            return None
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if not secrets.compare_digest(digest, record["sha256"]):
            return None
        now = time.time()
        if record["expires"] and record["expires"] <= now:
            return None
        self._touch(parts[1], record, now)
        return Principal(record["owner"], record["role"], "token:%s" % record["name"])

    def _touch(self, token_id, record, now):
        with self._lock:
            previous = self._last_used.get(token_id) or record["last_used"] or 0
            self._last_used[token_id] = now
        if now - previous < LAST_USED_FLUSH:
            return

        def mutate(records):
            if token_id in records:
                records[token_id]["last_used"] = int(now)
        try:
            store.update_auth("tokens", mutate)
        except OSError:
            pass                    # a read-only data dir must not fail requests

    def list_tokens(self, principal):
        self._manage_tokens(principal)
        tokens = []
        for token_id, record in self._records("tokens").items():
            if principal.role != ADMIN and record["owner"] != principal.name:
                continue
            tokens.append(self._public_token(token_id, record))
        return sorted(tokens, key=lambda t: t["created"], reverse=True)

    def create_token(self, principal, name, role=None, expires_days=None, owner=None):
        self._manage_tokens(principal)
        name = (name or "").strip()
        if not name or len(name) > _TOKEN_NAME_LIMIT or _has_control(name):
            raise AuthError("A token needs a name of up to %d characters." % _TOKEN_NAME_LIMIT, 400)
        role = role or principal.role
        if role not in ROLES:
            raise AuthError("Role must be one of: %s." % ", ".join(ROLES), 400)
        if not principal.can(role):
            raise AuthError("A %s principal cannot create %s tokens." % (principal.role, role), 403)
        if owner and owner != principal.name and principal.role != ADMIN:
            raise AuthError("Only an admin can create tokens for someone else.", 403)
        expires = None
        if expires_days not in (None, "", 0):
            try:
                days = float(expires_days)
            except (TypeError, ValueError):
                raise AuthError("expires_days must be a number.", 400)
            if days <= 0:
                raise AuthError("expires_days must be positive.", 400)
            expires = int(time.time() + days * 86400)

        token_id = secrets.token_hex(6)
        secret = "%s_%s_%s" % (TOKEN_PREFIX, token_id, secrets.token_urlsafe(32))
        record = {
            "name": name,
            "owner": owner or principal.name,
            "role": role,
            "sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
            "created": int(time.time()),
            "expires": expires,
            "last_used": None,
        }

        def mutate(records):
            records[token_id] = record
        store.update_auth("tokens", mutate)
        public = self._public_token(token_id, record)
        public["token"] = secret      # the only time it is ever shown
        return public

    def revoke_token(self, principal, token_id):
        self._manage_tokens(principal)
        record = self._records("tokens").get(token_id)
        if not record or (principal.role != ADMIN and record["owner"] != principal.name):
            raise AuthError("No such token: %s" % token_id, 404)

        def mutate(records):
            records.pop(token_id, None)
        store.update_auth("tokens", mutate)
        with self._lock:
            self._last_used.pop(token_id, None)
        return {"revoked": token_id}

    @staticmethod
    def _manage_tokens(principal):
        # A token that could mint tokens could outlive its own expiry and
        # revocation, so managing them takes a login (or the CLI).
        if principal.via.startswith("token:") or principal.via == "static-token":
            raise AuthError("API tokens cannot manage tokens; log in instead.", 403)

    def _public_token(self, token_id, record):
        with self._lock:
            seen = self._last_used.get(token_id)
        last_used = max(seen or 0, record["last_used"] or 0) or None
        return {
            "id": token_id,
            "name": record["name"],
            "owner": record["owner"],
            "role": record["role"],
            "created": record["created"],
            "expires": record["expires"],
            "last_used": int(last_used) if last_used else None,
            "expired": bool(record["expires"] and record["expires"] <= time.time()),
        }

    # -- local users -------------------------------------------------------

    def list_users(self):
        return sorted(({"name": name, "role": r["role"], "created": r["created"],
                        "updated": r["updated"]}
                       for name, r in self._records("users").items()),
                      key=lambda u: u["name"])

    def set_user(self, name, password=None, role=None):
        """Create a user, or change an existing one's password and/or role."""
        name = (name or "").strip()
        if not _NAME.match(name):
            raise AuthError("User names are letters, digits and _ . @ -, up to 64 "
                            "characters, not starting with punctuation.", 400)
        if role is not None and role not in ROLES:
            raise AuthError("Role must be one of: %s." % ", ".join(ROLES), 400)
        if password is not None and not isinstance(password, str):
            raise AuthError("Password must be a string.", 400)
        if password is not None and len(password) < MIN_PASSWORD:
            raise AuthError("Passwords must be at least %d characters." % MIN_PASSWORD, 400)
        encoded = hash_password(password) if password is not None else None
        now = int(time.time())

        def mutate(records):
            existing = records.get(name)
            if existing is None:
                if encoded is None:
                    raise AuthError("A new user needs a password.", 400)
                records[name] = {"role": role or READ, "hash": encoded,
                                 "created": now, "updated": now}
                return True
            if encoded is not None:
                existing["hash"] = encoded
            if role is not None:
                existing["role"] = role
            existing["updated"] = now
            return False
        created = store.update_auth("users", mutate)
        record = self._records("users").get(name) or {}
        return {"name": name, "role": record.get("role"), "created": created}

    def remove_user(self, name):
        def mutate(records):
            return records.pop(name, None) is not None
        if not store.update_auth("users", mutate):
            raise AuthError("No such user: %s" % name, 404)
        return {"removed": name}

    # -- password login and sessions ---------------------------------------

    def login(self, username, password, client, secure_transport):
        """Check a password against the enabled backends; returns (principal, session id).

        ``client`` keys the throttle. ``secure_transport`` is the server's
        judgement that the password did not cross a network in the clear.
        """
        if not self.config.password_login:
            raise AuthError("Password login is not enabled on this server.", 404)
        if not secure_transport and not self.config.allow_insecure_login:
            raise AuthError("Refusing a password over plain HTTP from another host. "
                            "Serve with --tls-cert/--tls-key or behind a TLS proxy, or "
                            "pass --allow-insecure-login.", 403)
        username = (username or "").strip()
        password = password or ""
        if not username or not password or len(username) > 128 or len(password) > 4096:
            raise AuthError("Username and password are required.", 400)

        keys = ("ip:%s" % client, "user:%s" % username.lower())
        self._check_throttle(keys)

        principal, method = None, None
        for candidate in self.config.methods:
            if candidate == "local":
                principal = self._login_local(username, password)
            elif candidate == "pam":
                principal = self._login_pam(username, password)
            if principal:
                method = candidate
                break

        if not principal:
            self._record_failure(keys)
            raise AuthError("Wrong username or password, or this account may not use lemondx.", 401)

        with self._lock:
            for key in keys:
                self._failures.pop(key, None)
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        # Remembered so a password changed from anywhere -- the CLI included,
        # which has no way to reach this process -- ends the sessions opened
        # with the old one.
        credential = (self._records("users").get(principal.name) or {}).get("hash") \
            if method == "local" else None
        with self._lock:
            self._sessions = {k: s for k, s in self._sessions.items() if s["expires"] > now}
            self._sessions[session_id] = {
                "name": principal.name, "role": principal.role, "method": method,
                "credential": credential,
                "created": now, "expires": now + self.config.session_seconds, "checked": now,
            }
        return principal, session_id

    def _login_local(self, username, password):
        record = self._records("users").get(username)
        if not verify_password(password, record["hash"] if record else _dummy_hash()) or not record:
            return None
        return Principal(username, record["role"], "local")

    def _login_pam(self, username, password):
        role = self._pam_role(username)
        ok, reason = pam.authenticate(self.config.pam_service, username, password)
        if not ok:
            _log("PAM refused %s: %s" % (username, reason))
            return None
        if not role:
            _log("PAM accepted %s, but they are in none of the groups %s"
                 % (username, ", ".join(self.config.pam_admin_groups + self.config.pam_read_groups)
                    or "(none configured)"))
            return None
        return Principal(username, role, "pam")

    def _pam_role(self, username):
        groups = _groups_of(username)
        if groups is None:
            return None
        if groups & set(self.config.pam_admin_groups):
            return ADMIN
        if groups & set(self.config.pam_read_groups):
            return READ
        return None

    def session_principal(self, session_id):
        if not session_id:
            return None
        now = time.time()
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            if session["expires"] <= now:
                del self._sessions[session_id]
                return None
        role = self._current_role(session, now)
        if not role:
            with self._lock:
                self._sessions.pop(session_id, None)
            return None
        return Principal(session["name"], role, "session")

    def _current_role(self, session, now):
        """The session's role as its source sees it now; None ends the session."""
        if session["method"] == "local":
            if "local" not in self.config.methods:
                return None
            record = self._records("users").get(session["name"])
            if not record or record["hash"] != session["credential"]:
                return None
            return record["role"]
        if now - session["checked"] < RECHECK_SECONDS:
            return session["role"]
        role = self._pam_role(session["name"]) if "pam" in self.config.methods else None
        with self._lock:
            session["role"], session["checked"] = role, now
        return role

    def logout(self, session_id):
        with self._lock:
            self._sessions.pop(session_id or "", None)
        return {"logged_out": True}

    # -- throttling --------------------------------------------------------

    def _check_throttle(self, keys):
        now = time.time()
        with self._lock:
            for key in keys:
                entry = self._failures.get(key)
                limit = LOCKOUT_AFTER[key.split(":", 1)[0]]
                if not entry or entry[0] < limit:
                    continue
                wait = min(LOCKOUT_BASE * 2 ** min(entry[0] - limit, 16), LOCKOUT_MAX)
                remaining = int(entry[1] + wait - now) + 1
                if remaining > 0:
                    raise AuthError("Too many failed logins. Try again in %d seconds." % remaining, 429)

    def _record_failure(self, keys):
        now = time.time()
        with self._lock:
            if len(self._failures) > 10000:
                self._failures = {k: v for k, v in self._failures.items()
                                  if now - v[1] < _FAILURE_MEMORY}
            for key in keys:
                entry = self._failures.get(key)
                if not entry or now - entry[1] > _FAILURE_MEMORY:
                    entry = [0, now]
                entry[0] += 1
                entry[1] = now
                self._failures[key] = entry

    # -- trusted proxy -----------------------------------------------------

    def is_trusted_proxy(self, address):
        if not self.config.trusted_proxies:
            return False
        try:
            ip = ipaddress.ip_address((address or "").split("%", 1)[0])
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in network for network in self.config.trusted_proxies)

    def proxy_principal(self, address, headers):
        """The user a trusted proxy vouches for, or None. Headers from anyone else are ignored."""
        if "proxy" not in self.config.methods or not self.is_trusted_proxy(address):
            return None
        name = (headers.get(self.config.proxy_user_header) or "").strip()
        if not name or len(name) > 256 or _has_control(name):
            return None
        wanted = [g for g in (self.config.proxy_admin_group, self.config.proxy_read_group) if g]
        if not wanted:
            return Principal(name, ADMIN, "proxy")
        raw = headers.get(self.config.proxy_groups_header or "") or ""
        groups = set(g for g in re.split(r"[,\s]+", raw) if g)
        if self.config.proxy_admin_group and self.config.proxy_admin_group in groups:
            return Principal(name, ADMIN, "proxy")
        if self.config.proxy_read_group and self.config.proxy_read_group in groups:
            return Principal(name, READ, "proxy")
        return None

    # -- startup -----------------------------------------------------------

    def startup_warnings(self):
        warnings = []
        if "pam" in self.config.methods:
            warnings.extend(pam.diagnose(self.config.pam_service))
            if not (self.config.pam_admin_groups or self.config.pam_read_groups):
                warnings.append("No --pam-admin-group given and neither 'lxd' nor "
                                "'incus-admin' exists, so no PAM user may log in.")
        if "local" in self.config.methods and not self._records("users"):
            warnings.append("--auth local is on but there are no users yet: "
                            "add one with `lemondx user add NAME --role admin`.")
        return warnings


# -- record hygiene --------------------------------------------------------
#
# users.json and tokens.json are as hand-editable as the rest of the data
# directory, so a record that would confuse the checks above is dropped
# rather than half-trusted.


def _clean_user(name, record):
    if not _NAME.match(name or "") or record.get("role") not in ROLES \
            or not isinstance(record.get("hash"), str):
        return None
    return {"role": record["role"], "hash": record["hash"],
            "created": _int(record.get("created")), "updated": _int(record.get("updated"))}


def _clean_token(token_id, record):
    digest = record.get("sha256")
    if not re.match(r"^[0-9a-f]{12}$", token_id or "") or record.get("role") not in ROLES \
            or not isinstance(digest, str) or not re.match(r"^[0-9a-f]{64}$", digest) \
            or not isinstance(record.get("owner"), str) or not record["owner"]:
        return None
    return {
        "name": str(record.get("name") or token_id)[:_TOKEN_NAME_LIMIT],
        "owner": record["owner"],
        "role": record["role"],
        "sha256": digest,
        "created": _int(record.get("created")),
        "expires": _int(record.get("expires")) or None,
        "last_used": _int(record.get("last_used")) or None,
    }


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _has_control(text):
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def _groups_of(username):
    try:
        entry = pwd.getpwnam(username)
    except KeyError:
        return None
    names = set()
    for gid in os.getgrouplist(username, entry.pw_gid):
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            pass
    return names


def _log(message):
    print("[lemondx] auth: %s" % message)


def parse_duration_days(text):
    """``30d``, ``12h``, ``2w`` or a bare number of days, as days; None for never."""
    if text in (None, "", "never"):
        return None
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([hdw]?)\s*$", str(text))
    if not match:
        raise AuthError("Durations look like 12h, 30d or 2w.", 400)
    value = float(match.group(1))
    return value * {"h": 1 / 24.0, "d": 1, "w": 7, "": 1}[match.group(2)]


# -- saved settings --------------------------------------------------------
#
# `lemondx configure auth` writes these to store.config_path("auth") so a
# service does not need a dozen flags in its unit file; `serve` loads them and
# lets any flag given on its command line override the matching field. The
# keys mirror AuthConfig's arguments, except that a static token is only ever
# referenced by file: the settings file is meant to be copied and shown.
#
# A saved file that fails validation stops `serve` rather than being skipped.
# Skipping would start the server with authentication off, which is the one
# failure that must never be silent -- and that is also why unknown keys are
# errors: a misspelt "methods" would otherwise do exactly that.

SETTINGS_SECTION = "auth"
SETTINGS_VERSION = 1

DEFAULT_SETTINGS = {
    "methods": [],
    "token_file": None,
    "session_hours": 12,
    "allow_insecure_login": False,
    "pam_service": "lemondx",
    "pam_admin_groups": None,        # None: the daemon's admin group(s) on this host
    "pam_read_groups": [],
    "trusted_proxies": [],
    "proxy_user_header": "X-Forwarded-User",
    "proxy_groups_header": None,
    "proxy_admin_group": None,
    "proxy_read_group": None,
}

_HEADER = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_PAM_SERVICE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_GROUP = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@-]{0,63}$")


def default_pam_admin_groups():
    return [g for g in sorted(set(ADMIN_GROUP.values())) if _group_exists(g)]


def _setting_error(key, message):
    return AuthError("%s: %s" % (key, message), 400)


def _string_list(key, value, pattern=None):
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _setting_error(key, "expected a list of strings")
    items = []
    for item in (v.strip() for v in value):
        if not item or (pattern and not pattern.match(item)):
            raise _setting_error(key, "not a valid entry: %r" % item)
        if item not in items:
            items.append(item)
    return items


def _optional_string(key, value, pattern):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not pattern.match(value.strip()):
        raise _setting_error(key, "not a valid value: %r" % (value,))
    return value.strip()


def _clean_setting(key, value):
    if key == "methods":
        methods = _string_list(key, value)
        unknown = [m for m in methods if m not in METHODS]
        if unknown:
            raise _setting_error(key, "unknown method(s) %s; choose from %s"
                                 % (", ".join(unknown), ", ".join(METHODS)))
        return methods
    if key == "token_file":
        if value is None or value == "":
            return None
        if not isinstance(value, str) or not os.path.isabs(value) or _has_control(value):
            # A service runs from another working directory, so relative
            # paths would resolve somewhere else there.
            raise _setting_error(key, "must be an absolute path")
        return value
    if key == "session_hours":
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not 0 < value <= 24 * 366:
            raise _setting_error(key, "must be a number of hours above 0")
        return value
    if key == "allow_insecure_login":
        if not isinstance(value, bool):
            raise _setting_error(key, "must be true or false")
        return value
    if key == "pam_service":
        if not isinstance(value, str) or not _PAM_SERVICE.match(value):
            raise _setting_error(key, "must be a PAM service name such as 'lemondx'")
        return value
    if key == "pam_admin_groups":
        return None if value is None else _string_list(key, value, _GROUP)
    if key == "pam_read_groups":
        return _string_list(key, value, _GROUP)
    if key == "trusted_proxies":
        proxies = _string_list(key, value)
        for proxy in proxies:
            try:
                ipaddress.ip_network(proxy, strict=False)
            except ValueError:
                raise _setting_error(key, "not an IP address or network: %r" % proxy)
        return proxies
    if key == "proxy_user_header":
        if not isinstance(value, str) or not _HEADER.match(value):
            raise _setting_error(key, "must be an HTTP header name")
        return value
    if key == "proxy_groups_header":
        return _optional_string(key, value, _HEADER)
    if key in ("proxy_admin_group", "proxy_read_group"):
        return _optional_string(key, value, _GROUP)
    raise _setting_error(key, "unknown setting")


def clean_settings(raw):
    """Validated settings with every key present; raises AuthError naming the bad key."""
    if not isinstance(raw, dict):
        raise AuthError("Auth settings must be a JSON object.", 400)
    unknown = sorted(set(raw) - set(DEFAULT_SETTINGS) - {"version"})
    if unknown:
        raise AuthError("Unknown auth setting(s): %s. Known: %s."
                        % (", ".join(unknown), ", ".join(DEFAULT_SETTINGS)), 400)
    settings = dict(DEFAULT_SETTINGS)
    for key, value in raw.items():
        if key != "version":
            settings[key] = _clean_setting(key, value)
    AuthConfig(**_config_arguments(settings))      # combinations, e.g. proxy without trust
    return settings


def _config_arguments(settings):
    return {key: settings[key] for key in DEFAULT_SETTINGS if key != "token_file"}


def load_settings():
    """Saved settings, or None if `configure auth` never ran. Raises AuthError if unusable."""
    try:
        raw = store.load_config(SETTINGS_SECTION)
    except ValueError as exc:
        raise AuthError("Cannot use the saved auth settings: %s" % exc, 500)
    if raw is None:
        return None
    try:
        return clean_settings(raw)
    except AuthError as exc:
        raise AuthError("Invalid saved auth settings in %s: %s. Fix the file or run "
                        "`lemondx configure auth`." % (settings_path(), exc.message.rstrip(".")),
                        exc.code)


def save_settings(settings):
    """Validate and write; returns the cleaned settings."""
    cleaned = clean_settings(settings)
    store.save_config(SETTINGS_SECTION, dict(cleaned, version=SETTINGS_VERSION))
    return cleaned


def reset_settings():
    return store.delete_config(SETTINGS_SECTION)


def settings_path():
    return store.config_path(SETTINGS_SECTION)


def read_token_file(path):
    try:
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError as exc:
        raise AuthError("Cannot read the token file %s: %s" % (path, exc), 400)
    if not token:
        raise AuthError("The token file %s is empty." % path, 400)
    return token


def build_config(settings=None, overrides=None, static_token=None):
    """The AuthConfig for a launch: saved settings, then non-None overrides on top.

    ``static_token`` is a token the caller already has (from --token or the
    environment); only without one is the merged ``token_file`` read.
    """
    merged = dict(DEFAULT_SETTINGS)
    merged.update(settings or {})
    for key, value in (overrides or {}).items():
        if key not in DEFAULT_SETTINGS:
            raise ValueError("unknown auth override: %s" % key)
        if value is not None:
            merged[key] = value
    if static_token is None and merged["token_file"]:
        static_token = read_token_file(merged["token_file"])
    return AuthConfig(static_token=static_token, **_config_arguments(merged))

"""Client for the LXD / Incus REST API, spoken over the local unix socket.

Both daemons expose the same ``/1.0`` API and authenticate members of their
admin group as trusted without any certificate exchange, so a plain
HTTP-over-AF_UNIX client covers everything lemondx needs with no third-party
dependencies. Incus is a fork of LXD and is what Debian ships; the two differ
mainly in where the socket lives, which group grants access, which image
servers they know, and the name of their CLI client.

Note that this is *not* liblxc (the ``lxc-create`` / ``lxc-ls`` tools). Those
have no REST API and a different model entirely.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import shutil
import socket
import tempfile
import urllib.parse

from . import eventlog
from .websocket import WebSocketError, connect_unix

LXD = "lxd"
INCUS = "incus"

# Searched in order. Each entry is (flavor, socket path).
SOCKET_CANDIDATES = (
    (LXD, "/var/snap/lxd/common/lxd/unix.socket"),   # LXD snap (Ubuntu default)
    (LXD, "/var/lib/lxd/unix.socket"),               # LXD from a distro package
    (INCUS, "/var/lib/incus/unix.socket"),           # Incus (Debian 13+, Incus repo)
    (INCUS, "/run/incus/unix.socket"),
    (INCUS, "/var/lib/incus/unix.socket.user"),      # Incus restricted user socket
)

# Image servers each daemon ships with. Incus does not know Canonical's
# `ubuntu:` simplestreams servers, and the two use different `images:` hosts.
REMOTES_BY_FLAVOR = {
    LXD: {
        "ubuntu": "https://cloud-images.ubuntu.com/releases/",
        "ubuntu-daily": "https://cloud-images.ubuntu.com/daily/",
        "ubuntu-minimal": "https://cloud-images.ubuntu.com/minimal/releases/",
        "images": "https://images.lxd.canonical.com",
    },
    INCUS: {
        "images": "https://images.linuxcontainers.org",
    },
}

# The CLI client, used only to hand off an interactive shell.
CLIENT_BINARY = {LXD: "lxc", INCUS: "incus"}

# The group that grants access to the socket, for error messages.
ADMIN_GROUP = {LXD: "lxd", INCUS: "incus-admin"}

# The oldest daemon a node may run to join a cluster: each project's current
# long-term release. Older ones differ in what the API accepts and how new
# guests behave under them (Ubuntu 26.04's systemd cannot start its network
# under LXD 5.0's AppArmor profile), and a cluster is only as dependable as
# its oldest member. Checked by the joiner before it redeems a code and again
# by the member admitting it.
MIN_CLUSTER_VERSION = {LXD: (5, 21), INCUS: (6, 0)}
# How to get there, for the refusal.
UPGRADE_HINT = {
    LXD: "install the snap (`snap install lxd --channel=5.21/stable`, then "
         "`sudo lxd.migrate` to bring over a distribution package's instances)",
    INCUS: "install it from your distribution's backports or the Zabbly repository",
}


def parse_version(text):
    """``(major, minor, ...)`` from a daemon version such as "5.21.8", or None."""
    parts = []
    for piece in str(text or "").strip().split("."):
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


def cluster_version_problem(flavor, version):
    """Why a daemon may not be a cluster member, or "" when it may."""
    minimum = MIN_CLUSTER_VERSION.get(flavor)
    product = "Incus" if flavor == INCUS else "LXD"
    if minimum is None:
        return "%s is not a daemon lemondx knows." % (flavor or "The daemon")
    found = parse_version(version)
    wanted = ".".join(str(n) for n in minimum)
    if found is None:
        return ("%s did not report a version lemondx can read (%r); a cluster "
                "member needs %s %s or later." % (product, version, product, wanted))
    if found[:len(minimum)] < minimum:
        return ("%s %s is older than %s, the oldest a cluster member may run. To "
                "upgrade, %s." % (product, version, wanted, UPGRADE_HINT[flavor]))
    return ""

# What a container's cgroup is named on the host, before the instance name.
# Both run containers through liblxc, which picks the name; the Incus entry is
# from its source, not a live host.
CGROUP_PAYLOAD_PREFIX = {LXD: "lxc.payload.", INCUS: "lxc.payload."}

# VM config that boots UEFI without secure boot, for images whose bootloader
# is not signed. LXD replaced security.secureboot with boot.mode; Incus kept it.
NO_SECUREBOOT_CONFIG = {
    LXD: {"boot.mode": "uefi-nosecureboot"},
    INCUS: {"security.secureboot": "false"},
}


class LXDError(Exception):
    """An error reported by the daemon (or a failure reaching it)."""

    def __init__(self, message, code=500):
        super().__init__(message)
        self.message = message
        self.code = code


def _flavor_for_path(path):
    """Guess which daemon owns a socket path."""
    return INCUS if "incus" in path.lower() else LXD


def find_socket(explicit=None):
    """Locate the daemon socket. Returns (path, flavor), or raises LXDError.

    Explicit settings win, then the daemons' own environment variables, then
    the well-known locations for each packaging style.
    """
    forced_flavor = os.environ.get("LEMONDX_FLAVOR", "").strip().lower() or None
    if forced_flavor and forced_flavor not in (LXD, INCUS):
        raise LXDError("LEMONDX_FLAVOR must be '%s' or '%s'." % (LXD, INCUS))

    ordered = []          # (flavor, path) to try, highest priority first
    if explicit:
        ordered.append((forced_flavor or _flavor_for_path(explicit), explicit))
    for variable in ("LEMONDX_SOCKET", "INCUS_SOCKET"):
        value = os.environ.get(variable)
        if value:
            ordered.append((forced_flavor or _flavor_for_path(value), value))
    for variable, flavor in (("INCUS_DIR", INCUS), ("LXD_DIR", LXD)):
        value = os.environ.get(variable)
        if value:
            ordered.append((forced_flavor or flavor, os.path.join(value, "unix.socket")))
    if not ordered:
        ordered = [(flavor, path) for flavor, path in SOCKET_CANDIDATES
                   if forced_flavor in (None, flavor)]

    for flavor, path in ordered:
        if os.path.exists(path):
            if not os.access(path, os.R_OK | os.W_OK):
                raise LXDError(
                    "Found %s at %s but cannot use it. Add yourself to the '%s' "
                    "group (newgrp %s, or log out and back in)."
                    % (flavor.upper(), path, ADMIN_GROUP[flavor], ADMIN_GROUP[flavor]),
                    503,
                )
            return path, flavor

    raise LXDError(
        "No LXD or Incus socket found. Looked in: %s. Install LXD "
        "(snap install lxd) or Incus (apt install incus), make sure the daemon "
        "is running, and that you are in its admin group."
        % ", ".join(path for _, path in ordered),
        503,
    )


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that dials an AF_UNIX socket instead of TCP."""

    def __init__(self, socket_path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


class LXDClient:
    """Thin wrapper over the LXD/Incus 1.0 API with operation handling."""

    def __init__(self, socket_path=None, project="default", timeout=60):
        self.socket_path, self.flavor = find_socket(socket_path)
        self.project = project
        self.timeout = timeout

    @property
    def remotes(self):
        """Image servers this daemon knows about."""
        return REMOTES_BY_FLAVOR[self.flavor]

    @property
    def client_binary(self):
        """Name of the CLI client (`lxc` or `incus`)."""
        return CLIENT_BINARY[self.flavor]

    @property
    def product_name(self):
        return "Incus" if self.flavor == INCUS else "LXD"

    # -- transport ---------------------------------------------------------

    def _request(self, method, path, body=None, params=None, raw=False, timeout=None,
                 logged=True):
        """One call to the daemon; every mutation is logged as a change.

        ``logged=False`` is for `_async()`, which logs once the operation has
        actually finished -- the request itself only says it was accepted.
        """
        if method == "GET" or not logged:
            return self._send(method, path, body, params, raw, timeout)
        change = _change_of(method, path, body)
        try:
            result = self._send(method, path, body, params, raw, timeout)
        except LXDError as exc:
            _log_change(change, exc)
            raise
        _log_change(change)
        return result

    def _send(self, method, path, body=None, params=None, raw=False, timeout=None):
        query = dict(params or {})
        # Every instance-scoped call must carry the project or LXD assumes default.
        if self.project and self.project != "default":
            query.setdefault("project", self.project)
        if query:
            path = "%s?%s" % (path, urllib.parse.urlencode(query))

        payload = None
        headers = {"Accept": "application/json"}
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        conn = _UnixHTTPConnection(
            self.socket_path, self.timeout if timeout is None else timeout)
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            data = response.read()
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            raise LXDError("Cannot talk to LXD: %s" % exc, 503) from exc
        finally:
            conn.close()

        if raw:
            if status >= 400:
                raise LXDError(_decode_error(data, status), status)
            return data

        try:
            parsed = json.loads(data)
        except ValueError as exc:
            raise LXDError("Malformed response from LXD: %s" % exc, 502) from exc

        if parsed.get("type") == "error" or status >= 400:
            raise LXDError(parsed.get("error") or "LXD returned HTTP %d" % status,
                           parsed.get("error_code") or status)
        return parsed

    # -- operations --------------------------------------------------------

    def _sync(self, method, path, body=None, params=None):
        return self._request(method, path, body, params).get("metadata")

    def _async(self, method, path, body=None, params=None, wait=True, timeout=None):
        """Issue a call that returns a background operation.

        Returns the finished operation metadata when ``wait`` is set, otherwise
        the operation record so the caller can poll it.
        """
        change = _change_of(method, path, body)
        try:
            result = self._request(method, path, body, params, logged=False)
            operation = result.get("operation") or ""
            if not operation:
                _log_change(change)
                return result.get("metadata")
            if not wait:
                _log_change(change, started=True)
                return result.get("metadata")
            finished = self.wait_for_operation(operation.rsplit("/", 1)[-1], timeout)
        except LXDError as exc:
            _log_change(change, exc)
            raise
        _log_change(change)
        return finished

    def wait_for_operation(self, operation_id, timeout=None):
        """Block until an operation finishes; raise LXDError if it failed."""
        wait = timeout if timeout is not None else self.timeout
        # The socket read must outlast the server-side wait or we time out first.
        # Passed per request, never set on the client: creates, bootstraps and
        # polls share one client across threads, and a timeout swapped on it
        # leaks into whichever request another thread makes meanwhile.
        result = self._request(
            "GET", "/1.0/operations/%s/wait" % operation_id, params={"timeout": wait},
            timeout=wait + 15,
        )

        metadata = result.get("metadata") or {}
        if metadata.get("status_code", 0) not in (200, 0) or metadata.get("err"):
            raise LXDError(metadata.get("err") or "Operation failed", 500)
        return metadata

    def get_operation(self, operation_id):
        return self._sync("GET", "/1.0/operations/%s" % operation_id)

    # -- server ------------------------------------------------------------

    def server_info(self):
        return self._sync("GET", "/1.0")

    def resources(self):
        return self._sync("GET", "/1.0/resources")

    def projects(self):
        return self._sync("GET", "/1.0/projects", params={"recursion": "1"})

    # -- instances ---------------------------------------------------------

    def list_instances(self):
        """All instances with their live state, in a single recursive call."""
        return self._sync("GET", "/1.0/instances", params={"recursion": "2"}) or []

    def get_instance_record(self, name):
        """The instance without its runtime state (one call instead of two)."""
        return self._sync("GET", "/1.0/instances/%s" % _seg(name))

    def get_instance(self, name):
        instance = self.get_instance_record(name)
        instance["state"] = self.get_state(name)
        return instance

    def get_state(self, name):
        return self._sync("GET", "/1.0/instances/%s/state" % _seg(name))

    def create_instance(self, config, wait=True):
        return self._async("POST", "/1.0/instances", config, wait=wait, timeout=600)

    def update_instance(self, name, config):
        return self._async("PATCH", "/1.0/instances/%s" % _seg(name), config)

    def replace_instance(self, name, config):
        """PUT the whole record. PATCH merges the maps, so removing a device
        -- rather than adding or changing one -- has to go through here."""
        return self._async("PUT", "/1.0/instances/%s" % _seg(name), config)

    def delete_instance(self, name):
        return self._async("DELETE", "/1.0/instances/%s" % _seg(name), timeout=120)

    def rename_instance(self, name, new_name):
        return self._async("POST", "/1.0/instances/%s" % _seg(name), {"name": new_name})

    def set_state(self, name, action, force=False, stateful=False, timeout=60):
        body = {
            "action": action,
            "timeout": timeout,
            "force": force,
            "stateful": stateful,
        }
        return self._async(
            "PUT", "/1.0/instances/%s/state" % _seg(name), body, timeout=timeout + 30
        )

    def restore_snapshot(self, name, snapshot):
        return self._async("PUT", "/1.0/instances/%s" % _seg(name), {"restore": snapshot})

    # -- snapshots ---------------------------------------------------------

    def list_snapshots(self, name):
        return self._sync(
            "GET", "/1.0/instances/%s/snapshots" % _seg(name), params={"recursion": "1"}
        ) or []

    def create_snapshot(self, name, snapshot_name, stateful=False):
        return self._async(
            "POST",
            "/1.0/instances/%s/snapshots" % _seg(name),
            {"name": snapshot_name, "stateful": stateful},
            timeout=300,
        )

    def delete_snapshot(self, name, snapshot_name):
        return self._async(
            "DELETE", "/1.0/instances/%s/snapshots/%s" % (_seg(name), _seg(snapshot_name))
        )

    # -- exec --------------------------------------------------------------

    def exec_command(self, name, command, environment=None, cwd=None, timeout=60):
        """Run a command non-interactively and collect its recorded output.

        ``record-output`` lets LXD stash stdout/stderr as log files, so we get
        the result without needing a websocket client.
        """
        body = {
            "command": command,
            "wait-for-websocket": False,
            "interactive": False,
            "record-output": True,
            "environment": environment or {},
        }
        if cwd:
            body["cwd"] = cwd

        started = self._request("POST", "/1.0/instances/%s/exec" % _seg(name), body)
        operation = (started.get("operation") or "").rsplit("/", 1)[-1]
        if not operation:
            raise LXDError("LXD did not start the command.", 500)

        try:
            metadata = self.wait_for_operation(operation, timeout)
        except LXDError:
            # LXD marks some non-zero exits (127 "Command not found" among them)
            # as a *failed operation*, yet the record still carries the real
            # exit code and the captured output. A command that ran and failed
            # is a result, not an API error, so recover it.
            metadata = self.get_operation(operation)
            if not (metadata.get("metadata") or {}).get("output"):
                raise

        inner = metadata.get("metadata") or {}
        output = inner.get("output") or {}
        result = {
            "exit_code": inner.get("return", 0),
            "stdout": self._read_log(name, output.get("1")),
            "stderr": self._read_log(name, output.get("2")),
        }
        # Recorded output accumulates on disk; clean it up now that we have it.
        for handle in output.values():
            self._delete_log(name, handle)
        return result

    def _read_log(self, name, handle):
        if not handle:
            return ""
        try:
            return self._request("GET", handle, raw=True).decode("utf-8", "replace")
        except LXDError:
            return ""

    def _delete_log(self, name, handle):
        if not handle:
            return
        try:
            self._request("DELETE", handle)
        except LXDError:
            pass  # Best effort: a stale log file is harmless.

    # -- interactive sessions ----------------------------------------------
    #
    # Unlike exec_command above, these need a real terminal, which the daemon
    # offers only over WebSockets: the POST returns an operation holding one
    # secret per channel, and the session begins when those are attached to.

    def exec_interactive(self, name, command, environment=None, width=80, height=24):
        """Start a command with a TTY, ready for a WebSocket to attach.

        Returns ``(operation_id, fds)``; fds maps ``"0"`` to the data channel's
        secret and ``"control"`` to the one that carries window resizes.
        """
        return self._attachable("/1.0/instances/%s/exec" % _seg(name), {
            "command": command,
            "wait-for-websocket": True,
            "interactive": True,
            "environment": environment or {},
            "width": int(width),
            "height": int(height),
        })

    def console_session(self, name, width=80, height=24):
        """Attach to the guest's own console device, as `lxc console` does."""
        return self._attachable("/1.0/instances/%s/console" % _seg(name), {
            "type": "console",
            "width": int(width),
            "height": int(height),
        })

    def _attachable(self, path, body):
        started = self._request("POST", path, body)
        operation = (started.get("operation") or "").rsplit("/", 1)[-1]
        fds = ((started.get("metadata") or {}).get("metadata") or {}).get("fds") or {}
        if not operation or "0" not in fds:
            raise LXDError(
                "%s did not offer a terminal to attach to." % self.product_name, 502)
        return operation, fds

    def attach(self, operation_id, secret):
        """Open one of a waiting operation's WebSocket channels."""
        path = "/1.0/operations/%s/websocket?secret=%s" % (
            _seg(operation_id), urllib.parse.quote(secret))
        try:
            return connect_unix(self.socket_path, path)
        except WebSocketError as exc:
            raise LXDError("Cannot attach to %s: %s" % (self.product_name, exc), 502)

    def push_file(self, name, path, content, mode="0644", uid=0, gid=0):
        """Write a file inside an instance.

        LXD reads ``X-LXD-*`` metadata headers and Incus reads ``X-Incus-*``;
        both ignore headers they do not know, so we send each spelling.
        """
        if isinstance(content, str):
            content = content.encode()
        headers = {"Content-Type": "application/octet-stream"}
        for prefix in ("X-LXD", "X-Incus"):
            headers["%s-uid" % prefix] = str(uid)
            headers["%s-gid" % prefix] = str(gid)
            headers["%s-mode" % prefix] = mode
            headers["%s-type" % prefix] = "file"

        endpoint = "/1.0/instances/%s/files?path=%s" % (_seg(name), _seg(path))
        conn = _UnixHTTPConnection(self.socket_path, self.timeout)
        try:
            conn.request("POST", endpoint, body=content, headers=headers)
            response = conn.getresponse()
            data = response.read()
            if response.status >= 400:
                raise LXDError(_decode_error(data, response.status), response.status)
        except (OSError, http.client.HTTPException) as exc:
            raise LXDError("Cannot write %s in %s: %s" % (path, name, exc), 503) from exc
        finally:
            conn.close()

    def read_file(self, name, path, timeout=None):
        """A file's bytes from inside an instance.

        A VM serves this through its agent, so it fails while the agent is not
        running -- which is also what makes it a useful liveness probe.

        A file in /proc has no real size, and the daemon's Content-Length can
        come from a different read than the body it then sends: /proc/loadavg
        changes length whenever its task count or last pid does, and the reply
        falls a byte short. That is still the file, so a short body on a
        successful response is returned rather than raised.
        """
        query = {"path": path}
        if self.project and self.project != "default":
            query["project"] = self.project
        endpoint = "/1.0/instances/%s/files?%s" % (_seg(name), urllib.parse.urlencode(query))
        conn = _UnixHTTPConnection(self.socket_path, self.timeout if timeout is None else timeout)
        try:
            conn.request("GET", endpoint)
            response = conn.getresponse()
            try:
                data = response.read()
            except http.client.IncompleteRead as exc:
                if response.status >= 400:
                    raise
                data = exc.partial
            if response.status >= 400:
                raise LXDError(_decode_error(data, response.status), response.status)
            return data
        except (OSError, http.client.HTTPException) as exc:
            raise LXDError("Cannot read %s in %s: %s" % (path, name, exc), 503) from exc
        finally:
            conn.close()

    def delete_file(self, name, path):
        try:
            self._request("DELETE", "/1.0/instances/%s/files" % _seg(name),
                          params={"path": path})
        except LXDError:
            pass  # best effort cleanup

    def console_log(self, name):
        try:
            return self._request(
                "GET", "/1.0/instances/%s/console" % _seg(name), raw=True
            ).decode("utf-8", "replace")
        except LXDError:
            return ""

    # -- images ------------------------------------------------------------

    def list_images(self):
        return self._sync("GET", "/1.0/images", params={"recursion": "1"}) or []

    def delete_image(self, fingerprint):
        return self._async("DELETE", "/1.0/images/%s" % _seg(fingerprint), timeout=120)

    def get_image(self, fingerprint):
        """The image record, or None when this daemon has no such image."""
        try:
            return self._sync("GET", "/1.0/images/%s" % _seg(fingerprint))
        except LXDError as exc:
            if exc.code == 404:
                return None
            raise

    def get_alias(self, name):
        try:
            return self._sync("GET", "/1.0/images/aliases/%s" % _seg(name))
        except LXDError as exc:
            if exc.code == 404:
                return None
            raise

    def set_alias(self, name, fingerprint, description=""):
        """Point an alias at an image, creating it or moving it as needed."""
        if self.get_alias(name) is None:
            return self._sync("POST", "/1.0/images/aliases", {
                "name": name, "target": fingerprint, "description": description})
        return self._sync("PUT", "/1.0/images/aliases/%s" % _seg(name), {
            "target": fingerprint, "description": description})

    def publish_snapshot(self, instance, snapshot, properties=None, timeout=3600):
        """Make an image of a snapshot; returns the new image's fingerprint.

        Publishing compresses the whole root filesystem, which for a large
        instance is minutes of work, hence the long wait.
        """
        metadata = self._async("POST", "/1.0/images", {
            "source": {"type": "snapshot", "name": "%s/%s" % (instance, snapshot)},
            "properties": dict(properties or {}),
            "public": False,
        }, timeout=timeout)
        fingerprint = ((metadata or {}).get("metadata") or {}).get("fingerprint")
        if not fingerprint:
            raise LXDError("Publishing gave no image fingerprint.", 502)
        return fingerprint

    def pull_image(self, server, fingerprint, protocol="simplestreams", timeout=3600):
        """Download one exact image from a remote into this daemon's store.

        By fingerprint, never by alias -- an alias is whatever the remote
        built last, which is the drift a pinned image exists to stop -- and
        with auto-update off, so the daemon does not swap it for a newer
        build of the same name either. Returns the fingerprint.
        """
        metadata = self._async("POST", "/1.0/images", {
            "source": {"type": "image", "mode": "pull", "server": server,
                       "protocol": protocol, "fingerprint": fingerprint},
            "auto_update": False,
            "public": False,
        }, timeout=timeout)
        got = ((metadata or {}).get("metadata") or {}).get("fingerprint") or fingerprint
        self.update_image(got, auto_update=False)
        return got

    def update_image(self, fingerprint, **changes):
        """PATCH an image's settable fields, e.g. ``auto_update``."""
        return self._sync("PATCH", "/1.0/images/%s" % _seg(fingerprint), dict(changes))

    def open_image_export(self, fingerprint, spool_dir=None):
        """``(stream, length, close, content_type)`` for an image, read as it arrives.

        Streamed rather than read whole: an image is routinely gigabytes. A
        unified image (what publishing makes) is one tarball with a length. A
        split one -- metadata and rootfs, as remotes serve them -- comes back
        multipart and chunked, with no length to send it on with, so it is
        spooled to a temporary file in ``spool_dir`` first. Its content type
        is returned with it, boundary and all: `import_image()` given the same
        bytes and type imports both parts as they were.
        """
        conn = _UnixHTTPConnection(self.socket_path, self.timeout)
        try:
            conn.request("GET", self._path("/1.0/images/%s/export" % _seg(fingerprint)))
            response = conn.getresponse()
            if response.status >= 400:
                raise LXDError(_decode_error(response.read(), response.status),
                               response.status)
            content_type = response.getheader("Content-Type") or "application/octet-stream"
            length = response.getheader("Content-Length")
            if "multipart" in content_type and not (length and length.isdigit()):
                spool = tempfile.TemporaryFile(dir=spool_dir, prefix=".image-")
                try:
                    shutil.copyfileobj(response, spool, 1024 * 1024)
                    size = spool.tell()
                    spool.seek(0)
                except BaseException:
                    spool.close()
                    raise
                conn.close()
                return spool, size, spool.close, content_type
            if not length or not length.isdigit():
                raise LXDError("The daemon did not say how large image %s is."
                               % fingerprint[:12], 502)
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise LXDError("Cannot read image %s: %s" % (fingerprint[:12], exc), 503) from exc
        except BaseException:
            conn.close()
            raise
        return response, int(length), conn.close, content_type

    def import_image(self, stream, length, properties=None, timeout=1800,
                     content_type="application/octet-stream"):
        """Upload an image tarball from ``stream``; returns its fingerprint.

        The fingerprint is the daemon's own SHA-256 of what arrived, so a
        caller comparing it with the source's fingerprint has checked the
        whole transfer end to end.
        """
        headers = {"Content-Type": content_type, "Content-Length": str(length)}
        encoded = urllib.parse.urlencode(dict(properties or {}))
        # Both spellings, as for files: each daemon ignores the other's.
        for prefix in ("X-LXD", "X-Incus"):
            headers["%s-public" % prefix] = "0"
            if encoded:
                headers["%s-properties" % prefix] = encoded
        conn = _UnixHTTPConnection(self.socket_path, timeout)
        try:
            conn.request("POST", self._path("/1.0/images"), body=stream, headers=headers)
            response = conn.getresponse()
            data = response.read()
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            raise LXDError("Cannot import the image: %s" % exc, 503) from exc
        finally:
            conn.close()
        try:
            parsed = json.loads(data)
        except ValueError as exc:
            raise LXDError("Malformed response from LXD: %s" % exc, 502) from exc
        if parsed.get("type") == "error" or status >= 400:
            raise LXDError(parsed.get("error") or "LXD returned HTTP %d" % status,
                           parsed.get("error_code") or status)
        operation = (parsed.get("operation") or "").rsplit("/", 1)[-1]
        metadata = self.wait_for_operation(operation, timeout) if operation \
            else parsed.get("metadata") or {}
        fingerprint = (metadata.get("metadata") or {}).get("fingerprint")
        if not fingerprint:
            raise LXDError("Importing gave no image fingerprint.", 502)
        eventlog.event("change", "image.import", image=fingerprint[:12], result="ok")
        return fingerprint

    def _path(self, path):
        if self.project and self.project != "default":
            return "%s?%s" % (path, urllib.parse.urlencode({"project": self.project}))
        return path

    # -- profiles, storage, networks ---------------------------------------

    def list_profiles(self):
        return self._sync("GET", "/1.0/profiles", params={"recursion": "1"}) or []

    def get_profile(self, name):
        return self._sync("GET", "/1.0/profiles/%s" % _seg(name))

    def update_profile(self, name, profile):
        return self._async("PUT", "/1.0/profiles/%s" % _seg(name), profile)

    def list_storage_pools(self):
        return self._sync("GET", "/1.0/storage-pools", params={"recursion": "1"}) or []

    def get_storage_pool(self, name):
        return self._sync("GET", "/1.0/storage-pools/%s" % _seg(name))

    def create_storage_pool(self, name, driver, config=None):
        return self._async(
            "POST",
            "/1.0/storage-pools",
            {"name": name, "driver": driver, "config": config or {}},
            timeout=300,
        )

    def update_storage_pool(self, name, description, config):
        return self._async(
            "PUT",
            "/1.0/storage-pools/%s" % _seg(name),
            {"description": description or "", "config": config or {}},
            timeout=300,
        )

    def delete_storage_pool(self, name):
        return self._async(
            "DELETE", "/1.0/storage-pools/%s" % _seg(name), timeout=300
        )

    def storage_pool_resources(self, name):
        """Space and inodes on a pool, or None where the driver cannot say."""
        try:
            return self._sync("GET", "/1.0/storage-pools/%s/resources" % _seg(name))
        except LXDError:
            return None

    def list_storage_volumes(self, pool):
        return self._sync(
            "GET", "/1.0/storage-pools/%s/volumes" % _seg(pool),
            params={"recursion": "1"},
        ) or []

    def get_storage_volume(self, pool, volume_type, name):
        return self._sync(
            "GET", "/1.0/storage-pools/%s/volumes/%s/%s"
            % (_seg(pool), _seg(volume_type), _seg(name))
        )

    def create_storage_volume(self, pool, name, content_type="filesystem", config=None,
                              description=""):
        return self._async(
            "POST",
            "/1.0/storage-pools/%s/volumes/custom" % _seg(pool),
            {
                "name": name,
                "type": "custom",
                "content_type": content_type,
                "description": description or "",
                "config": config or {},
            },
            timeout=300,
        )

    def update_storage_volume(self, pool, name, description, config):
        return self._async(
            "PUT",
            "/1.0/storage-pools/%s/volumes/custom/%s" % (_seg(pool), _seg(name)),
            {"description": description or "", "config": config or {}},
            timeout=300,
        )

    def delete_storage_volume(self, pool, name):
        return self._async(
            "DELETE",
            "/1.0/storage-pools/%s/volumes/custom/%s" % (_seg(pool), _seg(name)),
            timeout=300,
        )

    def list_networks(self):
        return self._sync("GET", "/1.0/networks", params={"recursion": "1"}) or []

    def get_network(self, name):
        return self._sync("GET", "/1.0/networks/%s" % _seg(name))

    def get_network_state(self, name):
        try:
            return self._sync("GET", "/1.0/networks/%s/state" % _seg(name))
        except LXDError:
            return {}

    def network_leases(self, name):
        """DHCP leases handed out on a managed bridge."""
        try:
            return self._sync("GET", "/1.0/networks/%s/leases" % _seg(name)) or []
        except LXDError:
            return []          # unmanaged networks have none

    def network_forwards(self, name):
        try:
            return self._sync(
                "GET", "/1.0/networks/%s/forwards" % _seg(name),
                params={"recursion": "1"}) or []
        except LXDError:
            return []          # not supported on every driver

    def create_network(self, name, config=None, description="", kind="bridge"):
        return self._async(
            "POST",
            "/1.0/networks",
            {"name": name, "type": kind, "description": description or "",
             "config": config or {}},
            timeout=120,
        )

    def update_network(self, name, description, config):
        return self._async(
            "PUT",
            "/1.0/networks/%s" % _seg(name),
            {"description": description or "", "config": config or {}},
            timeout=120,
        )

    def delete_network(self, name):
        return self._async("DELETE", "/1.0/networks/%s" % _seg(name), timeout=120)


def window_resize_message(width, height):
    """The control-channel message that retells the TTY its size.

    The daemon wants the numbers as strings; it rejects the message otherwise.
    """
    return json.dumps({
        "command": "window-resize",
        "args": {"width": str(int(width)), "height": str(int(height))},
    })


def _seg(value):
    """Percent-encode a single path segment (instance names are user input)."""
    return urllib.parse.quote(str(value), safe="")


def _decode_error(data, status):
    try:
        return json.loads(data).get("error") or "HTTP %d" % status
    except ValueError:
        return data.decode("utf-8", "replace")[:200] or "HTTP %d" % status


# -- the change log ------------------------------------------------------------
#
# Every daemon mutation is logged from here, whichever path asked for it, so a
# new feature's changes are recorded without anyone remembering to. What it is
# comes from the URL rather than a table of calls: /1.0/instances/web/state is
# instance.state with instance=web. Only names and which keys changed go in,
# never values -- an instance's config can hold anything.

_NOUNS = {"instances": "instance", "snapshots": "snapshot", "images": "image",
          "aliases": "image.alias", "networks": "network", "storage-pools": "storage.pool",
          "volumes": "storage.volume", "profiles": "profile", "projects": "project",
          "network-acls": "network.acl", "certificates": "certificate",
          "operations": "operation"}
# Exec, files and the like are how lemondx works inside an instance -- every
# health probe and bootstrap step -- not changes to it; the action that wanted
# them is logged where it was asked for.
_PLUMBING = {"exec", "console", "files", "logs", "operations", "metadata", "export",
             "sftp"}


def _change_of(method, path, body):
    """``(action, level, fields)`` for one daemon mutation."""
    path = path.split("?", 1)[0]
    parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/")[1:]]
    fields = {}
    noun, item, tail = "daemon", False, None
    index = 0
    while index < len(parts):
        segment = parts[index]
        if segment in _NOUNS:
            noun, item, tail = _NOUNS[segment], False, None
            key = noun.rsplit(".", 1)[-1]
            if segment == "volumes" and index + 2 < len(parts):
                fields["volume_type"], fields[key] = parts[index + 1], parts[index + 2]
                item, index = True, index + 3
            elif index + 1 < len(parts) and parts[index + 1] not in _NOUNS:
                fields[key], item, index = parts[index + 1], True, index + 2
            else:
                index += 1
            continue
        tail = segment
        index += 1
    body = body if isinstance(body, dict) else {}
    level = logging.INFO
    if tail:
        action = "%s.%s" % (noun, tail)
        if tail in _PLUMBING:
            level = logging.DEBUG
        if tail == "state":
            fields["state"] = body.get("action")
    elif method == "POST" and not item:
        action = "%s.create" % noun
        fields["name"] = body.get("name") if isinstance(body.get("name"), str) else None
        source = body.get("source") if isinstance(body.get("source"), dict) else {}
        fields["source"] = source.get("alias") or (source.get("fingerprint") or "")[:12] \
            or source.get("type")
    elif method == "POST":
        action = "%s.%s" % (noun, "rename" if body.get("name") else "post")
        fields["new_name"] = body.get("name") if isinstance(body.get("name"), str) else None
    elif method == "DELETE":
        action = "%s.delete" % noun
        if noun == "operation":
            level = logging.DEBUG
    elif body.get("restore"):
        action, fields["snapshot"] = "%s.restore" % noun, body.get("restore")
    else:
        action = "%s.update" % noun
        config = body.get("config") if isinstance(body.get("config"), dict) else {}
        devices = body.get("devices") if isinstance(body.get("devices"), dict) else {}
        fields["config_keys"] = sorted(config)
        fields["devices"] = sorted(devices)
    return action, level, fields


def _log_change(change, error=None, started=False):
    action, level, fields = change
    if error is not None:
        eventlog.event("change", action, level=max(level, logging.WARNING), result="failed",
                       error=getattr(error, "message", str(error)), **fields)
    else:
        eventlog.event("change", action, level=level,
                       result="started" if started else "ok", **fields)

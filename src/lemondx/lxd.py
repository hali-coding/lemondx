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
import os
import socket
import urllib.parse

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

    def _request(self, method, path, body=None, params=None, raw=False):
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

        conn = _UnixHTTPConnection(self.socket_path, self.timeout)
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
        result = self._request(method, path, body, params)
        operation = result.get("operation") or ""
        if not operation:
            return result.get("metadata")
        if not wait:
            return result.get("metadata")
        return self.wait_for_operation(operation.rsplit("/", 1)[-1], timeout)

    def wait_for_operation(self, operation_id, timeout=None):
        """Block until an operation finishes; raise LXDError if it failed."""
        wait = timeout if timeout is not None else self.timeout
        # The socket read must outlast the server-side wait or we time out first.
        original, self.timeout = self.timeout, wait + 15
        try:
            result = self._request(
                "GET", "/1.0/operations/%s/wait" % operation_id, params={"timeout": wait}
            )
        finally:
            self.timeout = original

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

    def create_network(self, name, config=None):
        return self._async(
            "POST",
            "/1.0/networks",
            {"name": name, "type": "bridge", "config": config or {}},
            timeout=120,
        )


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

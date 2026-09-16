"""HTTP layer: the JSON API plus static hosting for the built Vite frontend."""

from __future__ import annotations

import errno
import ipaddress
import json
import mimetypes
import os
import re
import socket
import ssl
import sys
import threading
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from . import store, websocket
from .auth import ADMIN, READ, SESSION_COOKIE, AuthConfig, AuthError, AuthService
from .cluster import ClusterError, ClusterService, from_peer
from .lxd import LXDError
from .nodeclient import NodeError
from .service import ContainerService, ServiceError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8099
TLS_HANDSHAKE_TIMEOUT = 10

# The Vite dev server proxies /api with changeOrigin, which rewrites Host but
# leaves the browser's Origin alone, so --dev has to accept that origin too.
DEV_ORIGINS = ("localhost:5173", "127.0.0.1:5173")
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")
_TOKEN_IN_LOG = re.compile(r"(token=)[^&\s\"]*")

def _find_web_dist():
    """Locate the built Vite output by walking up from this package."""
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(4):
        candidate = os.path.join(here, "web", "dist")
        if os.path.isdir(candidate):
            return candidate
        here = os.path.dirname(here)
    # Not built yet: still return the conventional path so the error message helps.
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "web", "dist")


WEB_DIST = _find_web_dist()


class Router:
    """Tiny regex router: register handlers, then dispatch by method + path.

    Every route has a role. Reads default to ``read`` and everything else to
    ``admin``, so a new mutating route is admin-only unless someone decides
    otherwise; pass ``role=`` only where the method gets it wrong. A route
    with ``principal=True`` receives the caller after the query, for the few
    handlers whose answer depends on who is asking.
    """

    def __init__(self):
        self.routes = []

    def add(self, method, pattern, handler, role=None, principal=False):
        role = role or (READ if method == "GET" else ADMIN)
        self.routes.append((method, re.compile("^%s$" % pattern), handler, role, principal))

    def resolve(self, method, path):
        """``(handler, args, role, wants_principal)``; handler is None for no match."""
        allowed = set()
        for route_method, pattern, handler, role, wants in self.routes:
            match = pattern.match(path)
            if not match:
                continue
            if route_method == method:
                return handler, [unquote(g) for g in match.groups()], role, wants
            allowed.add(route_method)
        if allowed:
            raise ServiceError("Method not allowed (try: %s)" % ", ".join(sorted(allowed)), 405)
        return None, [], ADMIN, False


def build_router(service, auth=None, cluster=None):
    r = Router()
    NAME = r"([^/]+)"
    auth = auth or AuthService()
    cluster = cluster or ClusterService(service, auth)

    r.add("GET", r"/api/status", lambda body, q: service.status())
    r.add("POST", r"/api/setup", lambda body, q: service.initialize(
        storage_driver=body.get("storage_driver", "dir"),
        pool_name=body.get("pool_name", "default"),
        pool_size=body.get("pool_size"),
        bridge=body.get("bridge", "lxdbr0"),
        ipv6=bool(body.get("ipv6", False)),
    ))

    r.add("GET", r"/api/containers", lambda body, q: service.list_containers())
    r.add("POST", r"/api/containers", lambda body, q: service.create_container(
        name=body.get("name"),
        image=body.get("image"),
        instance_type=body.get("type", "container"),
        profiles=body.get("profiles"),
        cpu=body.get("cpu"),
        memory=body.get("memory"),
        disk=body.get("disk"),
        pool=body.get("pool"),
        network=body.get("network"),
        description=body.get("description"),
        ephemeral=body.get("ephemeral", False),
        start=body.get("start", True),
        secureboot=bool(body.get("secureboot", True)),
        config=body.get("config"),
        bootstrap=body.get("bootstrap"),
        background=bool(body.get("background", False)),
    ))

    r.add("GET", r"/api/creates", lambda body, q: service.creates())

    r.add("GET", r"/api/containers/%s" % NAME,
          lambda body, q, name: service.get_container(name))
    r.add("PATCH", r"/api/containers/%s" % NAME,
          lambda body, q, name: service.update_limits(
              name, cpu=body.get("cpu"), memory=body.get("memory"),
              description=body.get("description")))
    r.add("DELETE", r"/api/containers/%s" % NAME,
          lambda body, q, name: service.delete_container(
              name, force=_flag(q.get("force")) or bool(body.get("force"))))

    # One action over several containers at once. Distinct from the per-name
    # route by path length, and POST /api/containers is the create, so nothing
    # here shadows a container called "state".
    r.add("POST", r"/api/containers/state",
          lambda body, q: service.change_state_many(
              body.get("names"), body.get("action", ""), force=bool(body.get("force")),
              timeout=int(body.get("timeout", 60))))

    r.add("POST", r"/api/containers/%s/state" % NAME,
          lambda body, q, name: service.change_state(
              name, body.get("action", ""), force=bool(body.get("force")),
              timeout=int(body.get("timeout", 60))))
    r.add("POST", r"/api/containers/%s/rename" % NAME,
          lambda body, q, name: service.rename_container(name, body.get("name")))
    r.add("POST", r"/api/containers/%s/exec" % NAME,
          lambda body, q, name: service.exec_command(
              name, body.get("command"), timeout=int(body.get("timeout", 60))))

    r.add("GET", r"/api/containers/%s/snapshots" % NAME,
          lambda body, q, name: service.get_container(name)["snapshots"])
    r.add("POST", r"/api/containers/%s/snapshots" % NAME,
          lambda body, q, name: service.create_snapshot(
              name, body.get("name"), bool(body.get("stateful", False))))
    r.add("DELETE", r"/api/containers/%s/snapshots/%s" % (NAME, NAME),
          lambda body, q, name, snap: service.delete_snapshot(name, snap))
    r.add("POST", r"/api/containers/%s/snapshots/%s/restore" % (NAME, NAME),
          lambda body, q, name, snap: service.restore_snapshot(name, snap))

    r.add("GET", r"/api/resources", lambda body, q: service.resources())
    # The monitor's latest results; there is deliberately no way to trigger a
    # round from here, so checks stay at the configured interval.
    r.add("GET", r"/api/health", lambda body, q: service.health())

    r.add("GET", r"/api/storage", lambda body, q: service.storage())
    r.add("GET", r"/api/storage/pools", lambda body, q: service.storage()["pools"])
    r.add("POST", r"/api/storage/pools", lambda body, q: service.create_storage_pool(
        body.get("name"), body.get("driver"), source=body.get("source"),
        size=body.get("size"), description=body.get("description", ""),
        config=body.get("config")))
    r.add("GET", r"/api/storage/pools/%s" % NAME,
          lambda body, q, pool: service.get_storage_pool(pool))
    r.add("PATCH", r"/api/storage/pools/%s" % NAME,
          lambda body, q, pool: service.update_storage_pool(
              pool, description=body.get("description"), size=body.get("size"),
              config=body.get("config")))
    r.add("DELETE", r"/api/storage/pools/%s" % NAME,
          lambda body, q, pool: service.delete_storage_pool(
              pool, force=_flag(q.get("force")) or bool(body.get("force")),
              confirmation=body.get("confirmation"),
              expected_plan=body.get("expected_plan")))
    r.add("GET", r"/api/storage/pools/%s/volumes" % NAME,
          lambda body, q, pool: service.get_storage_pool(pool)["volumes"])
    r.add("POST", r"/api/storage/pools/%s/volumes" % NAME,
          lambda body, q, pool: service.create_storage_volume(
              pool, body.get("name"), content_type=body.get("content_type", "filesystem"),
              size=body.get("size"), description=body.get("description", ""),
              config=body.get("config")))
    r.add("GET", r"/api/storage/pools/%s/volumes/custom/%s" % (NAME, NAME),
          lambda body, q, pool, volume: service.get_storage_volume(pool, volume))
    r.add("PATCH", r"/api/storage/pools/%s/volumes/custom/%s" % (NAME, NAME),
          lambda body, q, pool, volume: service.update_storage_volume(
              pool, volume, description=body.get("description"), size=body.get("size"),
              config=body.get("config")))
    r.add("DELETE", r"/api/storage/pools/%s/volumes/custom/%s" % (NAME, NAME),
          lambda body, q, pool, volume: service.delete_storage_volume(pool, volume))

    r.add("GET", r"/api/images/browse", lambda body, q: service.browse_images(
        remote=q.get("remote") or None,
        arch=q.get("arch") or None,
        refresh=_flag(q.get("refresh")) if "refresh" in q else False))

    r.add("GET", r"/api/networks", lambda body, q: service.list_networks())
    r.add("GET", r"/api/subnets", lambda body, q: service.subnets())
    r.add("POST", r"/api/networks", lambda body, q: service.create_network(
        body.get("name"), description=body.get("description", ""),
        config=body.get("config")))
    r.add("GET", r"/api/networks/%s" % NAME,
          lambda body, q, name: service.get_network(name))
    r.add("PATCH", r"/api/networks/%s" % NAME,
          lambda body, q, name: service.update_network(
              name, description=body.get("description"), config=body.get("config")))
    r.add("DELETE", r"/api/networks/%s" % NAME,
          lambda body, q, name: service.delete_network(name))

    r.add("GET", r"/api/modules", lambda body, q: service.list_modules())
    # Saving anything the cluster shares pushes it to every member, unless the
    # caller *is* a member pushing it here -- see cluster.from_peer(). A node
    # that is not in a cluster takes the plain local path.
    r.add("POST", r"/api/modules", lambda body, q, who: cluster.upload_module(
        body.get("name"), body.get("content"), bool(body.get("overwrite")),
        propagate=not from_peer(who)), principal=True)
    r.add("GET", r"/api/modules/%s/source" % NAME,
          lambda body, q, mid: service.get_module_source(mid))
    r.add("PUT", r"/api/modules/%s/settings" % NAME,
          lambda body, q, mid: service.update_module_settings(
              mid, params=body.get("params"), is_default=body.get("is_default")))
    r.add("DELETE", r"/api/modules/%s" % NAME,
          lambda body, q, who, mid: cluster.remove_module(
              mid, everywhere=_everywhere(body, q, who)), principal=True)

    r.add("GET", r"/api/bootstrap-profiles",
          lambda body, q: service.list_bootstrap_profiles())
    r.add("PUT", r"/api/bootstrap-profiles/%s" % NAME,
          lambda body, q, who, name: cluster.save_bootstrap_profile(
              propagate=not from_peer(who), name=name,
              modules=body.get("modules") or [], params=body.get("params"),
              description=body.get("description", ""), ssh_keys=body.get("ssh_keys")),
          principal=True)
    r.add("DELETE", r"/api/bootstrap-profiles/%s" % NAME,
          lambda body, q, who, name: cluster.delete_bootstrap_profile(
              name, everywhere=_everywhere(body, q, who)), principal=True)

    r.add("GET", r"/api/templates", lambda body, q: service.list_templates())
    r.add("PUT", r"/api/templates/%s" % NAME,
          lambda body, q, who, name: cluster.save_template(
              propagate=not from_peer(who),
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
              name_prefix=body.get("name_prefix")), principal=True)
    r.add("DELETE", r"/api/templates/%s" % NAME,
          lambda body, q, who, name: cluster.delete_template(
              name, everywhere=_everywhere(body, q, who)), principal=True)
    # Launching names nodes or groups; with neither it is this node alone, so
    # an older client -- or a script that has never heard of federation -- gets
    # exactly the behaviour it had before. `names` is how a coordinating node
    # asks for particular instance names, so numbering stays unique across a
    # cluster; on its own this node picks them.
    r.add("POST", r"/api/templates/%s/launch" % NAME,
          lambda body, q, name: cluster.launch_template(
              name, count=body.get("count", 1), prefix=body.get("prefix"),
              params=body.get("params"), nodes=body.get("nodes"),
              groups=body.get("groups"), names=body.get("names"),
              background=bool(body.get("background", False))))
    r.add("GET", r"/api/template-runs", lambda body, q: service.template_runs())
    r.add("DELETE", r"/api/template-runs/%s" % NAME,
          lambda body, q, name: service.dismiss_template_run(name))
    r.add("GET", r"/api/templates/%s/instances" % NAME,
          lambda body, q, name: service.template_instances(name))
    # `instances` is either plain names (this node, as always) or
    # {"node": ..., "name": ...} entries, which is what the UI sends when the
    # Containers tab is scoped to the cluster. cluster.template_action() hands
    # each node its own share and falls through to the local call when the only
    # node named is this one.
    r.add("POST", r"/api/templates/%s/destroy" % NAME,
          lambda body, q, name: cluster.template_action(
              name, "destroy", body.get("instances"),
              background=bool(body.get("background", False))))
    r.add("POST", r"/api/templates/%s/exec" % NAME,
          lambda body, q, name: cluster.template_action(
              name, "exec", body.get("instances"), command=body.get("command"),
              timeout=body.get("timeout", 300),
              background=bool(body.get("background", False))))
    r.add("POST", r"/api/templates/%s/recreate" % NAME,
          lambda body, q, name: cluster.template_action(
              name, "recreate", body.get("instances"), params=body.get("params"),
              background=bool(body.get("background", False))))
    r.add("GET", r"/api/ssh-keys", lambda body, q: service.list_ssh_keys())
    # Parses a key the caller pasted; changes nothing.
    r.add("POST", r"/api/ssh-keys/validate",
          lambda body, q: service.validate_ssh_key(body.get("key", "")), role=READ)
    r.add("POST", r"/api/containers/%s/bootstrap" % NAME,
          lambda body, q, name: service.bootstrap(
              name,
              modules=body.get("modules") or [],
              params=body.get("params"),
              ssh_keys=body.get("ssh_keys"),
              timeout=int(body.get("timeout", 900))))

    r.add("GET", r"/api/images", lambda body, q: service.list_images())
    r.add("GET", r"/api/profiles", lambda body, q: service.list_profiles())

    # -- federation --------------------------------------------------------
    # /api/cluster/enroll is handled before routing, like login: it is how a
    # node that has no credential yet gets one, so it authenticates itself with
    # the one-time join code in its body instead.
    r.add("GET", r"/api/cluster", lambda body, q: cluster.info())
    r.add("GET", r"/api/cluster/nodes", lambda body, q: cluster.list_nodes(
        probe=_flag(q.get("probe")) if "probe" in q else True))
    r.add("GET", r"/api/cluster/nodes/%s" % NAME,
          lambda body, q, name: cluster.describe_node(name))
    r.add("POST", r"/api/cluster/nodes", lambda body, q: cluster.join(
        body.get("code"), description=body.get("description", "")))
    r.add("DELETE", r"/api/cluster/nodes/%s" % NAME,
          lambda body, q, name: cluster.forget_node(name))
    r.add("POST", r"/api/cluster/leave", lambda body, q: cluster.leave())

    # Membership, spoken between nodes as well as to the UI. A joining node
    # announces itself here with the cluster credential, which is how one-way
    # joining reaches every member rather than only the one that invited it.
    r.add("GET", r"/api/cluster/members", lambda body, q: cluster.members())
    r.add("POST", r"/api/cluster/members",
          lambda body, q: cluster.announce(body))
    r.add("DELETE", r"/api/cluster/members/%s" % NAME,
          lambda body, q, name: cluster.drop_member(name))
    r.add("POST", r"/api/cluster/refresh", lambda body, q: cluster.sync_members())
    # Rotating replaces the credential everywhere; accepting one is a member
    # being told by whoever ran the rotation.
    r.add("POST", r"/api/cluster/rotate", lambda body, q: cluster.rotate_secret())
    r.add("PUT", r"/api/cluster/secret",
          lambda body, q: cluster.accept_secret(body.get("secret")))
    r.add("GET", r"/api/cluster/containers", lambda body, q: cluster.containers(
        nodes=_list(q.get("nodes")), groups=_list(q.get("groups")),
        everything=_flag(q.get("all")) if "all" in q else False))
    # One action over instances that may sit on different nodes, for the
    # Containers tab while it is scoped wider than this host.
    r.add("POST", r"/api/cluster/containers/state", lambda body, q: cluster.change_state(
        body.get("instances"), body.get("action", ""), force=bool(body.get("force")),
        timeout=int(body.get("timeout", 60))))
    r.add("POST", r"/api/cluster/containers/delete",
          lambda body, q: cluster.delete_containers(
              body.get("instances"), force=bool(body.get("force"))))

    r.add("GET", r"/api/cluster/groups", lambda body, q: cluster.list_groups())
    r.add("PUT", r"/api/cluster/groups/%s" % NAME,
          lambda body, q, who, name: cluster.save_group(
              name, members=body.get("members"),
              description=body.get("description", ""),
              propagate=not from_peer(who)), principal=True)
    r.add("DELETE", r"/api/cluster/groups/%s" % NAME,
          lambda body, q, who, name: cluster.delete_group(
              name, everywhere=_everywhere(body, q, who)), principal=True)

    # Issuing a join code is handing out the right to federate with this node,
    # so it is admin-only like every other mutation -- and a read-only user
    # cannot see the codes either, since the id is all a listing shows.
    r.add("GET", r"/api/cluster/invites", lambda body, q: cluster.list_invites(),
          role=ADMIN)
    r.add("POST", r"/api/cluster/invites", lambda body, q: cluster.create_invite(
        expires_minutes=body.get("expires_minutes", 30), note=body.get("note", "")))
    r.add("DELETE", r"/api/cluster/invites/%s" % NAME,
          lambda body, q, invite_id: cluster.revoke_invite(invite_id))

    r.add("POST", r"/api/cluster/sync", lambda body, q: cluster.sync(
        kinds=body.get("kinds") or ["templates"], names=body.get("names"),
        nodes=body.get("nodes"), groups=body.get("groups")))
    # Looks, changes nothing: what certificate an address presents right now.
    r.add("POST", r"/api/cluster/fingerprint",
          lambda body, q: cluster.probe_fingerprint(body.get("url")), role=ADMIN)

    # Any one node's own API, reached through this one:
    #   /api/nodes/<node>/containers/web-1/exec  ->  <node>/api/containers/web-1/exec
    # which is what lets the container drawer manage an instance wherever it
    # lives. The role enforced is the *target* route's, resolved against this
    # router below, so forwarding can never grant more than calling the same
    # endpoint here would -- and the route's own role has to be the looser of
    # the two, or a read-only caller could not reach a read endpoint at all.
    def _proxy(method):
        def handler(body, q, who, node, rest):
            return _forward(r, cluster, method, who, node, rest, body, q)
        return handler

    for _method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        r.add(_method, r"/api/nodes/%s/(.+)" % NAME, _proxy(_method),
              role=READ, principal=True)

    # GET /api/auth, login and logout are handled before routing: they must
    # answer callers who are not authenticated yet, and they set cookies.
    # Token routes are open to read-only users for their own tokens; the
    # service enforces ownership.
    r.add("GET", r"/api/auth/tokens", lambda body, q, who: auth.list_tokens(who),
          principal=True)
    r.add("POST", r"/api/auth/tokens", lambda body, q, who: auth.create_token(
        who, body.get("name"), role=body.get("role"),
        expires_days=body.get("expires_days"), owner=body.get("owner")),
          role=READ, principal=True)
    r.add("DELETE", r"/api/auth/tokens/%s" % NAME,
          lambda body, q, who, token_id: auth.revoke_token(who, token_id),
          role=READ, principal=True)
    r.add("GET", r"/api/auth/users", lambda body, q: auth.list_users(), role=ADMIN)
    r.add("PUT", r"/api/auth/users/%s" % NAME, lambda body, q, name: auth.set_user(
        name, password=body.get("password"), role=body.get("role")))
    r.add("DELETE", r"/api/auth/users/%s" % NAME, lambda body, q, name: auth.remove_user(name))
    return r


class LemondxHandler(BaseHTTPRequestHandler):
    server_version = "lemondx"
    protocol_version = "HTTP/1.1"

    # Injected by make_server().
    router = None
    service = None
    auth = None
    cluster = None
    web_root = None
    allow_origin = None
    quiet = False
    tls = False
    dev = False
    bound_host = DEFAULT_HOST

    # The caller of the request being handled, for the log line. A handler
    # serves every request on a kept-alive connection, so _handle resets it.
    principal = None

    # -- plumbing ----------------------------------------------------------

    def setup(self):
        # The listening socket is wrapped with do_handshake_on_connect=False,
        # so the TLS handshake happens here on the request's own thread: done
        # in accept(), one client that never finishes it would stall them all.
        if self.tls:
            self.request.settimeout(TLS_HANDSHAKE_TIMEOUT)
            self.request.do_handshake()
            self.request.settimeout(None)
        super().setup()

    def log_message(self, fmt, *args):
        if self.quiet:
            return
        # A WebSocket client can only send its token in the query string, and
        # the request line lands in this log -- which, under systemd, is the
        # journal every admin on the host can read.
        line = _TOKEN_IN_LOG.sub(r"\1***", fmt % args)
        who = " %s" % self.principal.name if self.principal else ""
        print("[lemondx] %s%s - %s" % (self.address_string(), who, line))

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method):
        path = urlparse(self.path).path
        query = _parse_query(urlparse(self.path).query)
        self.principal = None
        self._body_read = False

        if not self._host_allowed():
            self._send_json({"error": "Unexpected Host header. lemondx without auth only "
                                      "answers to loopback names."}, 403)
        elif method == "GET" and websocket.is_upgrade(self.headers):
            self._handle_upgrade(path, query)
        elif path.startswith("/api/"):
            self._handle_api(method, path, query)
        elif method == "GET":
            self._serve_static(path)
        else:
            self._send_json({"error": "Not found"}, 404)

    def _handle_api(self, method, path, query):
        try:
            if path in ("/api/auth", "/api/auth/login", "/api/auth/logout"):
                self._handle_auth(method, path)
                return
            if path == "/api/cluster/enroll":
                self._handle_enroll(method)
                return
            principal, explicit = self._principal()
            if principal is None:
                self._send_json({"error": "Unauthorized: log in or send a valid API token"}, 401)
                return
            self.principal = principal
            if method not in ("GET", "HEAD") and not explicit and not self._same_origin():
                self._send_json({"error": "Cross-origin request refused"}, 403)
                return
            handler, args, role, wants_principal = self.router.resolve(method, path)
            if handler is None:
                self._send_json({"error": "No such endpoint: %s" % path}, 404)
                return
            if not principal.can(role):
                self._send_json({"error": "Forbidden: %s access is read-only" % principal.name}, 403)
                return
            body = self._read_body()
            if wants_principal:
                args = [principal] + list(args)
            result = handler(body, query, *args)
            self._send_json({"data": result}, 200)
        except ServiceError as exc:
            self._send_json({"error": exc.message}, exc.code)
        except AuthError as exc:
            self._send_json({"error": exc.message}, exc.code)
        except (LXDError, ClusterError, NodeError) as exc:
            self._send_json({"error": exc.message}, _http_code(exc.code))
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the client
            self.log_message("unhandled error: %r", exc)
            self._send_json({"error": "Internal error: %s" % exc}, 500)

    # -- interactive terminals ---------------------------------------------

    TERMINALS = re.compile(r"^/api/containers/([^/]+)/(shell|console)$")

    def _handle_upgrade(self, path, query):
        """Turn this connection into a terminal attached to a container."""
        match = self.TERMINALS.match(path)
        if not match:
            self._send_json({"error": "Not a terminal endpoint: %s" % path}, 404)
            return
        # A browser cannot put headers on a WebSocket, so a token arrives in
        # the query string here rather than in Authorization. A session cookie
        # does come along -- which is exactly why the Origin must be checked:
        # otherwise any page the user visits could open a shell.
        principal, explicit = self._principal(query.get("token"))
        if principal is None:
            self._send_json({"error": "Unauthorized: log in or send a valid API token"}, 401)
            return
        self.principal = principal
        if not explicit and not self._same_origin():
            self._send_json({"error": "Cross-origin WebSocket refused"}, 403)
            return
        if not principal.can(ADMIN):
            self._send_json({"error": "Forbidden: terminals need admin access"}, 403)
            return

        name, kind = unquote(match.group(1)), match.group(2)
        try:
            session = self.service.open_terminal(
                name, kind, shell=query.get("shell"),
                cols=query.get("cols"), rows=query.get("rows"),
            )
        except (ServiceError, LXDError) as exc:
            # Still an ordinary HTTP response: the upgrade never happened, so
            # the browser sees the status and the reason.
            self._send_json({"error": exc.message}, _http_code(exc.code))
            return

        # Past this point the connection is no longer HTTP and must not be
        # reused for another request.
        self.close_connection = True
        try:
            websocket.server_handshake(self.headers, self.wfile.write)
        except (websocket.WebSocketError, OSError) as exc:
            session.close()
            self._send_json({"error": "Bad WebSocket upgrade: %s" % exc}, 400)
            return

        browser = websocket.WebSocket(
            self.rfile, self.wfile.write,
            closer=websocket.shutdown_closer(self.connection),
        )
        self.log_message('"%s" attached %s', self.path, kind)
        try:
            _bridge(browser, session)
        finally:
            session.close()

    def _read_body(self):
        self._body_read = True
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise ServiceError("Request body is not valid JSON.")
        if not isinstance(parsed, dict):
            raise ServiceError("Request body must be a JSON object.")
        return parsed

    # -- authentication ----------------------------------------------------

    def _principal(self, query_token=None):
        """``(principal or None, explicit)`` for this request.

        ``explicit`` means the caller presented a credential itself (a token
        header or query value), which a cross-site page cannot make a browser
        do. Cookies and proxy identity are ambient -- the browser attaches
        them to any request -- so those need the Origin check.
        """
        if not self.auth.config.enabled and not self._remote_needs_token():
            return self.auth.anonymous(), False
        header = self.headers.get("Authorization") or ""
        if header[:7].lower() == "bearer ":
            return self.auth.authenticate_token(header[7:]), True
        if self.headers.get("X-Lemondx-Token") is not None:
            return self.auth.authenticate_token(self.headers.get("X-Lemondx-Token")), True
        if query_token is not None:
            return self.auth.authenticate_token(query_token), True
        principal = self.auth.session_principal(self._cookie(SESSION_COOKIE))
        if principal:
            return principal, False
        proxied = self.auth.proxy_principal(self.client_address[0], self.headers)
        if proxied or self.auth.config.enabled:
            return proxied, False
        # Only here because membership, not configuration, is asking for a
        # credential: on this host there is still nothing to log in with, so
        # anyone local stays the anonymous admin they were before joining.
        return (self.auth.anonymous(), False) if _is_loopback(self.client_address[0]) \
            else (None, False)

    def _handle_auth(self, method, path):
        if path == "/api/auth" and method == "GET":
            principal, _ = self._principal()
            info = self.auth.info(principal)
            # A member with auth off still refuses remote callers without a
            # token, so say tokens are accepted -- otherwise the login gate a
            # remote browser gets would offer it no way in.
            if self.cluster is not None and self.cluster.requires_remote_token() \
                    and "token" not in info["methods"]:
                info["methods"] = info["methods"] + ["token"]
            self._send_json({"data": info})
            return
        if method != "POST":
            raise ServiceError("Method not allowed (try: %s)"
                               % ("GET" if path == "/api/auth" else "POST"), 405)
        # Login CSRF is a thing too: a forged login plants the attacker's session.
        if not self._same_origin():
            self._send_json({"error": "Cross-origin request refused"}, 403)
            return
        if path == "/api/auth/logout":
            self.auth.logout(self._cookie(SESSION_COOKIE))
            self._send_json({"data": {"logged_out": True}},
                            headers=[("Set-Cookie", self._session_cookie("", 0))])
            return
        body = self._read_body()
        principal, session_id = self.auth.login(
            body.get("username") if isinstance(body.get("username"), str) else "",
            body.get("password") if isinstance(body.get("password"), str) else "",
            client=self._client_ip(), secure_transport=self._secure_transport())
        self.principal = principal
        self.log_message("logged in as %s (%s) via %s", principal.name, principal.role, principal.via)
        self._send_json({"data": self.auth.info(principal)}, headers=[
            ("Set-Cookie", self._session_cookie(session_id, self.auth.config.session_seconds))])

    def _remote_needs_token(self):
        """Whether this caller must present a credential although auth is off.

        Being in a cluster means accepting API calls from other hosts, which a
        node cannot do while treating whoever reaches the port as an admin. So
        membership alone requires a credential from anyone who is not on this
        machine -- joining needs no auth setup, and does not quietly open the
        node up either. Loopback is untouched, so nobody is shut out of the UI
        on their own host.
        """
        return self.cluster is not None and self.cluster.requires_remote_token() \
            and not _is_loopback(self.client_address[0])

    def _handle_enroll(self, method):
        """Redeem a join code: the one API call a node makes before it has a token.

        The code in the body *is* the credential, and a strong one -- 32 random
        bytes, single use, expiring -- so no session or token is required here.
        It is an explicit credential in the sense _principal() means: a browser
        on another site cannot make the user's browser produce one, so the
        same-origin check that guards ambient credentials has nothing to add.
        The connection is already pinned to this node's certificate by the
        caller, which is what makes the code safe to send at all.
        """
        if method != "POST":
            raise ServiceError("Method not allowed (try: POST)", 405)
        body = self._read_body()
        result = self.cluster.enroll(body, peer_address=self._client_ip())
        self.log_message("cluster enrolment from %s accepted", self._client_ip())
        self._send_json({"data": result})

    def _cookie(self, name):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            morsel = SimpleCookie(raw).get(name)
        except CookieError:
            return None
        return morsel.value if morsel else None

    def _session_cookie(self, value, max_age):
        parts = ["%s=%s" % (SESSION_COOKIE, value), "Path=/", "HttpOnly",
                 "SameSite=Strict", "Max-Age=%d" % max_age]
        if self._https():
            parts.append("Secure")
        return "; ".join(parts)

    def _trusted_peer(self):
        return self.auth.is_trusted_proxy(self.client_address[0])

    def _https(self):
        if self.tls:
            return True
        return self._trusted_peer() and \
            (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

    def _secure_transport(self):
        """Whether a password in this request stayed off the network in the clear."""
        return self.tls or self._trusted_peer() or _is_loopback(self.client_address[0])

    def _client_ip(self):
        # Behind a trusted proxy every login would otherwise share one throttle.
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded and self._trusted_peer():
            return forwarded.split(",")[-1].strip()
        return self.client_address[0]

    def _request_host(self):
        host = self.headers.get("Host") or ""
        forwarded = self.headers.get("X-Forwarded-Host")
        if forwarded and self._trusted_peer():
            host = forwarded.split(",")[0].strip()
        return host.lower()

    def _same_origin(self):
        """True unless the browser says this request comes from another site.

        No Origin means not a browser (curl, scripts), which cannot carry the
        user's cookies without the user's help.
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        netloc = urlparse(origin).netloc.lower()
        if netloc and netloc == self._request_host():
            return True
        return self.dev and netloc in DEV_ORIGINS

    def _host_allowed(self):
        """Refuse DNS rebinding against an unauthenticated loopback server.

        Without auth, anything that reaches the port is trusted, and a page
        on evil.example that re-resolves to 127.0.0.1 reaches it with its own
        Origin *and* Host -- so the Origin check alone would pass. Only
        loopback names are answered in that configuration.
        """
        if self.auth.config.enabled or not _is_loopback(self.bound_host):
            return True
        host = _strip_port(self.headers.get("Host") or "")
        return host in LOOPBACK_NAMES or host == ""

    # -- responses ---------------------------------------------------------

    def _cors(self):
        if self.allow_origin:
            self.send_header("Access-Control-Allow-Origin", self.allow_origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")

    def _send_json(self, payload, status=200, headers=()):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers:
            self.send_header(name, value)
        # Refused before its body was read: on a kept-alive connection those
        # bytes would be parsed as the next request, so hang up instead.
        if not getattr(self, "_body_read", True) and (self.headers.get("Content-Length") or "0") != "0":
            self.close_connection = True
            self.send_header("Connection", "close")
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve_static(self, path):
        if not self.web_root or not os.path.isdir(self.web_root):
            self._send_json({
                "error": "Frontend is not built. Run `npm --prefix web install && "
                         "npm --prefix web run build`, or use `lemondx serve --dev` "
                         "alongside `npm --prefix web run dev`."
            }, 503)
            return

        relative = path.lstrip("/") or "index.html"
        target = os.path.normpath(os.path.join(self.web_root, relative))
        # Reject anything that escapes the dist directory.
        if not target.startswith(os.path.realpath(self.web_root) + os.sep) and \
                target != os.path.realpath(self.web_root):
            target = os.path.join(self.web_root, "index.html")
        if not os.path.isfile(target):
            target = os.path.join(self.web_root, "index.html")  # SPA fallback
        if not os.path.isfile(target):
            self._send_json({"error": "index.html missing from build output"}, 404)
            return

        content_type, _ = mimetypes.guess_type(target)
        with open(target, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        # Vite fingerprints assets, so they are safe to cache hard; the HTML is not.
        if "/assets/" in path:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def _bridge(browser, session):
    """Pump bytes between a browser and an attached session until one stops.

    One thread per direction, both doing blocking reads. A select loop would be
    tempting, but the browser's bytes arrive through the request handler's
    buffered reader, and select on the socket beneath it cannot see what is
    already sitting in that buffer.
    """
    def to_guest():
        try:
            while True:
                message = browser.recv()
                if message is None:
                    break
                opcode, payload = message
                if opcode == websocket.TEXT:
                    session.handle_control(payload)      # resizes, not keystrokes
                else:
                    session.write(payload)
        except (websocket.WebSocketError, OSError):
            pass
        finally:
            # Whatever ended this direction ends the session: closing the
            # daemon's channel frees the reader below.
            session.close()

    pump = threading.Thread(target=to_guest, name="terminal-in", daemon=True)
    pump.start()

    try:
        while True:
            chunk = session.read()
            if chunk is None:
                break
            browser.send(chunk)
    except (websocket.WebSocketError, OSError):
        pass

    # Say why it ended before hanging up, so the page can show it.
    code = session.exit_code()
    if code is not None:
        try:
            browser.send_text(json.dumps({"exit": code}))
        except (websocket.WebSocketError, OSError):
            pass
    browser.close(websocket.GOING_AWAY)
    pump.join(timeout=2)


def _parse_query(query):
    out = {}
    for part in (query or "").split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        out[unquote(key)] = unquote(value.replace("+", " "))
    return out


def _flag(value):
    return str(value).lower() in ("1", "true", "yes", "on", "")


def _forward(router, cluster, method, principal, node, rest, body, query):
    """Send one call on to a member's own API, after checking the caller may.

    The target path is resolved against this node's routes purely to find what
    access it needs. An unknown path resolves to admin, so a route this node
    has never heard of fails closed rather than open.
    """
    path = "/api/%s" % rest.lstrip("/")
    if rest.lstrip("/").startswith("nodes/"):
        raise ClusterError("A proxied call cannot be proxied again.", 400)
    handler, args, role, wants = router.resolve(method, path)
    if not principal.can(role):
        raise AuthError("Forbidden: %s access is read-only" % principal.name, 403)
    if node == cluster.local_name():
        # Addressing this node by name is the same request without the prefix;
        # answering it here keeps the front end from having to special-case it.
        if handler is None:
            raise ServiceError("No such endpoint: %s" % path, 404)
        return handler(body, query, *([principal] + args if wants else args))
    return cluster.proxy(method, node, path, body=body, params=query)


def _everywhere(body, query, principal):
    """Whether a delete should also remove the thing from every other member.

    On by default, because these definitions are kept level automatically and a
    copy left behind on one node is drift a push-only sync can never clear. A
    peer relaying the delete never re-broadcasts it, and `?everywhere=false`
    (or `"everywhere": false`) keeps one deliberately local.
    """
    if from_peer(principal):
        return False
    if "everywhere" in query:
        return _flag(query.get("everywhere"))
    return body.get("everywhere", True) is not False


def _list(value):
    """A comma-separated query value as a list; None stays None."""
    if value is None:
        return None
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _http_code(code):
    return code if isinstance(code, int) and 400 <= code < 600 else 500


def _strip_port(host):
    host = host.strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _is_loopback(address):
    address = (address or "").split("%", 1)[0]
    if address == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


class LemondxHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Port scanners, plain HTTP sent to a TLS port and clients that hang
        # up mid-handshake are routine, not worth a traceback each.
        if isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, socket.timeout)):
            return
        super().handle_error(request, client_address)


def tls_context(cert, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


def make_server(host=DEFAULT_HOST, port=DEFAULT_PORT, service=None, token=None,
                web_root=WEB_DIST, allow_origin=None, quiet=False, auth=None,
                tls=None, dev=False, cluster=None):
    service = service or ContainerService()
    auth = auth or AuthService(AuthConfig(static_token=token))
    cluster = cluster or ClusterService(service, auth)
    handler = type("BoundHandler", (LemondxHandler,), {
        "router": build_router(service, auth, cluster),
        "service": service,
        "auth": auth,
        "cluster": cluster,
        "web_root": os.path.realpath(web_root) if web_root else None,
        "allow_origin": allow_origin,
        "quiet": quiet,
        "tls": tls is not None,
        "dev": dev,
        "bound_host": host,
    })
    httpd = LemondxHTTPServer((host, port), handler)
    if tls is not None:
        httpd.socket = tls.wrap_socket(httpd.socket, server_side=True,
                                       do_handshake_on_connect=False)
    return httpd


def serve(host=DEFAULT_HOST, port=DEFAULT_PORT, token=None, dev=False, quiet=False,
          open_browser=False, auth_config=None, tls_cert=None, tls_key=None, auth_source=None,
          health_settings=None, cluster_settings=None):
    allow_origin = "*" if dev else None
    auth = AuthService(auth_config or AuthConfig(static_token=token))
    tls = None
    if tls_cert or tls_key:
        if not (tls_cert and tls_key):
            raise SystemExit("--tls-cert and --tls-key go together.")
        try:
            tls = tls_context(tls_cert, tls_key)
        except (OSError, ssl.SSLError) as exc:
            raise SystemExit("Cannot load the TLS certificate or key: %s" % exc)
    service = ContainerService()
    cluster = ClusterService(service, auth, settings=cluster_settings)
    try:
        httpd = make_server(host=host, port=port, service=service, auth=auth,
                            allow_origin=allow_origin, quiet=quiet, tls=tls, dev=dev,
                            cluster=cluster)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            raise SystemExit(
                "Port %d on %s is already in use -- another lemondx may be "
                "running. Use --port to pick a different one, or stop the other "
                "instance." % (port, host)
            )
        raise SystemExit("Cannot listen on %s:%d: %s" % (host, port, exc))

    url = "%s://%s:%d" % ("https" if tls else "http",
                          "localhost" if host in ("0.0.0.0", "127.0.0.1") else host, port)
    print("lemondx API + UI listening on %s" % url)
    # So `lemondx cluster invite` in another process can advertise the port and
    # scheme actually being served, rather than guessing at the default.
    store.write_runtime({"host": host, "port": port, "tls": tls is not None})
    config = auth.config
    if config.enabled:
        # Never the token itself: this output ends up in the journal.
        print("Auth: %s%s" % (
            ", ".join(config.methods + (["static token"] if config.static_token else [])),
            " (sessions last %gh)" % (config.session_seconds / 3600.0)
            if config.password_login else ""))
    if auth_source:
        print("Auth settings: %s (flags override)" % auth_source)
    elif not _is_loopback(host):
        print("WARNING: bound to %s with no authentication. Anyone who can reach this "
              "port can create and delete containers. See docs/security.md." % host)
    if config.password_login and not tls and not _is_loopback(host) \
            and not config.allow_insecure_login and not config.trusted_proxies:
        print("Note: password logins from other hosts are refused over plain HTTP; "
              "use --tls-cert/--tls-key or a TLS proxy.")
    for warning in auth.startup_warnings():
        print("WARNING: %s" % warning)

    if cluster.in_cluster():
        peers = len(store.load_nodes())
        print("Cluster: this node is '%s' at %s, with %d other member(s)"
              % (cluster.local_name(), cluster.local_url(), peers))
        if cluster.requires_remote_token():
            # Worth saying: it is the one place lemondx enforces a credential
            # that nobody configured, and it changes what a remote browser sees.
            print("         authentication is off, so requests from other hosts "
                  "need an API token; loopback is unchanged.")
    if dev:
        print("Dev mode: CORS is open for the Vite dev server (npm --prefix web run dev).")
    if not os.path.isdir(WEB_DIST) and not dev:
        print("Note: web/dist not found -- build the UI with `./build.sh`, "
              "or use a release archive, which ships it built.")

    if health_settings is not None:
        service.start_health_monitor(health_settings)
        print("Health checks: %s" % ("every %gs" % health_settings["interval_seconds"]
                                     if health_settings["enabled"] else "off"))

    if open_browser:
        threading.Timer(0.5, _open, args=(url,)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _wait_for_background_work(getattr(httpd.RequestHandlerClass, "service", None))
        print("Shutting down.")
    finally:
        store.clear_runtime()
        httpd.server_close()


def _wait_for_background_work(service):
    """Give creates and template runs the chance to finish before exiting.

    They run on threads the process takes down with it, and stopping one
    halfway leaves an instance created but never bootstrapped, or a recreate
    that deleted without recreating. So say what is running and wait, unless
    asked a second time.
    """
    pending = service.pending_work() if service else []
    if not pending:
        return
    print("\nStill running: %s." % "; ".join(pending))
    print("Waiting for it to finish -- press Ctrl-C again to stop anyway, "
          "leaving that work half done.")
    try:
        while service.pending_work():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nAbandoning: %s." % "; ".join(service.pending_work()))


def _open(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass

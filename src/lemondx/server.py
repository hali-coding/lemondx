"""HTTP layer: the JSON API plus static hosting for the built Vite frontend."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from .lxd import LXDError
from .service import ContainerService, ServiceError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8099

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
    """Tiny regex router: register handlers, then dispatch by method + path."""

    def __init__(self):
        self.routes = []

    def add(self, method, pattern, handler):
        self.routes.append((method, re.compile("^%s$" % pattern), handler))

    def resolve(self, method, path):
        allowed = set()
        for route_method, pattern, handler in self.routes:
            match = pattern.match(path)
            if not match:
                continue
            if route_method == method:
                return handler, [unquote(g) for g in match.groups()]
            allowed.add(route_method)
        if allowed:
            raise ServiceError("Method not allowed (try: %s)" % ", ".join(sorted(allowed)), 405)
        return None, []


def build_router(service):
    r = Router()
    NAME = r"([^/]+)"

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
        description=body.get("description"),
        ephemeral=body.get("ephemeral", False),
        start=body.get("start", True),
        config=body.get("config"),
        bootstrap=body.get("bootstrap"),
    ))

    r.add("GET", r"/api/containers/%s" % NAME,
          lambda body, q, name: service.get_container(name))
    r.add("PATCH", r"/api/containers/%s" % NAME,
          lambda body, q, name: service.update_limits(
              name, cpu=body.get("cpu"), memory=body.get("memory"),
              description=body.get("description")))
    r.add("DELETE", r"/api/containers/%s" % NAME,
          lambda body, q, name: service.delete_container(
              name, force=_flag(q.get("force")) or bool(body.get("force"))))

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

    r.add("GET", r"/api/modules", lambda body, q: service.list_modules())
    r.add("GET", r"/api/ssh-keys", lambda body, q: service.list_ssh_keys())
    r.add("POST", r"/api/ssh-keys/validate",
          lambda body, q: service.validate_ssh_key(body.get("key", "")))
    r.add("POST", r"/api/containers/%s/bootstrap" % NAME,
          lambda body, q, name: service.bootstrap(
              name,
              modules=body.get("modules") or [],
              params=body.get("params"),
              ssh_keys=body.get("ssh_keys"),
              timeout=int(body.get("timeout", 900))))

    r.add("GET", r"/api/images", lambda body, q: service.list_images())
    r.add("GET", r"/api/profiles", lambda body, q: service.list_profiles())
    return r


class LemondxHandler(BaseHTTPRequestHandler):
    server_version = "lemondx"
    protocol_version = "HTTP/1.1"

    # Injected by make_server().
    router = None
    token = None
    web_root = None
    allow_origin = None
    quiet = False

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt, *args):
        if not self.quiet:
            print("[lemondx] %s - %s" % (self.address_string(), fmt % args))

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

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method):
        path = urlparse(self.path).path
        query = _parse_query(urlparse(self.path).query)

        if path.startswith("/api/"):
            self._handle_api(method, path, query)
        elif method == "GET":
            self._serve_static(path)
        else:
            self._send_json({"error": "Not found"}, 404)

    def _handle_api(self, method, path, query):
        if not self._authorized():
            self._send_json({"error": "Unauthorized: missing or bad API token"}, 401)
            return
        try:
            handler, args = self.router.resolve(method, path)
            if handler is None:
                self._send_json({"error": "No such endpoint: %s" % path}, 404)
                return
            body = self._read_body()
            result = handler(body, query, *args)
            self._send_json({"data": result}, 200)
        except ServiceError as exc:
            self._send_json({"error": exc.message}, exc.code)
        except LXDError as exc:
            self._send_json({"error": exc.message}, _http_code(exc.code))
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the client
            self.log_message("unhandled error: %r", exc)
            self._send_json({"error": "Internal error: %s" % exc}, 500)

    def _read_body(self):
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

    def _authorized(self):
        if not self.token:
            return True
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            return secrets.compare_digest(header[7:], self.token)
        return secrets.compare_digest(self.headers.get("X-Lemondx-Token") or "", self.token)

    # -- responses ---------------------------------------------------------

    def _cors(self):
        if self.allow_origin:
            self.send_header("Access-Control-Allow-Origin", self.allow_origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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


def _http_code(code):
    return code if isinstance(code, int) and 400 <= code < 600 else 500


def make_server(host=DEFAULT_HOST, port=DEFAULT_PORT, service=None, token=None,
                web_root=WEB_DIST, allow_origin=None, quiet=False):
    service = service or ContainerService()
    handler = type("BoundHandler", (LemondxHandler,), {
        "router": build_router(service),
        "token": token,
        "web_root": os.path.realpath(web_root) if web_root else None,
        "allow_origin": allow_origin,
        "quiet": quiet,
    })
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def serve(host=DEFAULT_HOST, port=DEFAULT_PORT, token=None, dev=False, quiet=False,
          open_browser=False):
    allow_origin = "*" if dev else None
    httpd = make_server(host=host, port=port, token=token, allow_origin=allow_origin,
                        quiet=quiet)

    url = "http://%s:%d" % ("localhost" if host in ("0.0.0.0", "127.0.0.1") else host, port)
    print("lemondx API + UI listening on %s" % url)
    if token:
        print("API token: %s" % token)
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        print("WARNING: bound to %s with no --token. Anyone who can reach this port "
              "can create and delete containers." % host)
    if dev:
        print("Dev mode: CORS is open for the Vite dev server (npm --prefix web run dev).")
    if not os.path.isdir(WEB_DIST) and not dev:
        print("Note: web/dist not found -- build the UI with "
              "`npm --prefix web install && npm --prefix web run build`.")

    if open_browser:
        threading.Timer(0.5, _open, args=(url,)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        httpd.server_close()


def _open(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass

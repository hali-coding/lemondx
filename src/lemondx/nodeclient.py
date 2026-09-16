"""Client for another lemondx node's REST API, over TLS with a pinned certificate.

This is to federation what ``lxd.py`` is to the daemon: the transport, and
nothing else. It speaks the same ``{"data": ...}`` / ``{"error": ...}`` envelope
``server.py`` serves, turns an error back into an exception carrying the peer's
status code, and knows nothing about what the calls mean.

Peers are almost always identified by a *pinned* certificate rather than a CA
chain: a node is usually an address on someone's own network with a self-signed
certificate, where asking for a publicly trusted chain would mean nobody turns
federation on. Pinning gives the stronger property anyway -- this exact key, not
"someone a CA vouched for" -- provided the fingerprint travels out of band,
which is what the join code in ``cluster.py`` is for.

The pin is checked after the handshake and *before* the request is written, so a
node that answers with the wrong certificate never sees the API token.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import urllib.parse

DEFAULT_TIMEOUT = 20
# A create pulling an image can take minutes; a launch fanned out to a peer is
# started in the background there and polled, so no single call waits that long.
LONG_TIMEOUT = 120


class NodeError(Exception):
    """A peer refused, or could not be reached. ``code`` is HTTP-ish."""

    def __init__(self, message, code=502):
        super().__init__(message)
        self.message = message
        self.code = code


def fingerprint_of(der):
    """The SHA-256 of a certificate in DER form, lowercase hex."""
    return hashlib.sha256(der).hexdigest()


def pretty_fingerprint(hex_digest):
    """``ab:cd:...`` -- how a fingerprint is read out loud and compared by eye."""
    return ":".join(hex_digest[i:i + 2] for i in range(0, len(hex_digest), 2))


def parse_url(url):
    """``(scheme, host, port)`` for a node URL, or raise NodeError.

    Plain HTTP is only accepted for a loopback address, where the traffic never
    reaches a network. Everything else must be HTTPS: a token and a template
    full of SSH keys are not things to put on the wire in the clear.
    """
    text = (url or "").strip().rstrip("/")
    if "://" not in text:
        text = "https://" + text
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise NodeError("A node URL must be http:// or https://, not %r." % parts.scheme, 400)
    host = parts.hostname
    if not host:
        raise NodeError("No host in node URL %r." % url, 400)
    if parts.path or parts.query or parts.fragment:
        raise NodeError("A node URL is just scheme, host and port: %r." % url, 400)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme == "http" and not _is_loopback(host):
        raise NodeError(
            "%s is plain HTTP. A federated node must be reachable over HTTPS, so "
            "its token and templates do not cross the network in the clear "
            "(`lemondx cluster cert` makes a certificate)." % url, 400)
    return parts.scheme, host, port


def normalize_url(url):
    scheme, host, port = parse_url(url)
    default = 443 if scheme == "https" else 80
    host = "[%s]" % host if ":" in host else host
    return "%s://%s" % (scheme, host) if port == default else "%s://%s:%d" % (scheme, host, port)


def _is_loopback(host):
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _pinned_context():
    """A context that completes a handshake without judging it; the pin decides."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # Both off deliberately: a self-signed peer has no chain to build and its
    # certificate names an address, not a name a CA would have issued for. What
    # replaces them is _check_pin(), which is stricter than either.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _ca_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    return context


def peer_fingerprint(url, timeout=DEFAULT_TIMEOUT):
    """Fetch the certificate a node presents, without sending it anything.

    For showing an operator what they are about to trust. It proves nothing on
    its own -- whoever answers that address chooses what to present -- so it is
    for confirming a fingerprint received out of band, not for obtaining one.
    """
    scheme, host, port = parse_url(url)
    if scheme != "https":
        raise NodeError("%s is not HTTPS, so it has no certificate." % url, 400)
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with _pinned_context().wrap_socket(raw, server_hostname=host) as tls:
                return fingerprint_of(tls.getpeercert(binary_form=True))
    except ssl.SSLError as exc:
        # Something answered but did not speak TLS -- almost always a lemondx
        # serving plain HTTP on that port. Worth saying so rather than passing
        # OpenSSL's wording on to someone setting a node up.
        raise NodeError("%s answered, but not with TLS (%s)."
                        % (url, getattr(exc, "reason", None) or exc), 502)
    except OSError as exc:
        raise NodeError("Cannot reach %s: %s" % (url, exc), 502)


class NodeClient:
    """Calls one peer's API. Cheap to make; a connection is opened per request."""

    def __init__(self, url, token=None, fingerprint="", timeout=DEFAULT_TIMEOUT):
        self.url = normalize_url(url)
        self.scheme, self.host, self.port = parse_url(url)
        self.token = token or None
        self.fingerprint = (fingerprint or "").lower().replace(":", "")
        self.timeout = timeout

    # -- plumbing ----------------------------------------------------------

    def _connect(self, timeout):
        """An open, verified connection -- or a NodeError saying why not.

        Every failure below is a node that cannot be reached or is not who it
        claims to be, so none of them should escape as a socket error: a caller
        turns a NodeError into a message, and anything else into a 500.
        """
        if self.scheme == "http":
            return http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        context = _pinned_context() if self.fingerprint else _ca_context()
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=timeout,
                                           context=context)
        try:
            # Connected explicitly rather than lazily inside request(): the
            # certificate has to be judged before a single byte of the request
            # -- token included -- is handed to the socket.
            conn.connect()
            if self.fingerprint:
                self._check_pin(conn.sock)
        except NodeError:
            conn.close()
            raise
        except (OSError, ssl.SSLError) as exc:
            conn.close()
            raise NodeError("Cannot reach %s: %s" % (self.url, exc), 502)
        except BaseException:
            conn.close()
            raise
        return conn

    def _check_pin(self, sock):
        presented = fingerprint_of(sock.getpeercert(binary_form=True))
        if not secrets.compare_digest(presented, self.fingerprint):
            raise NodeError(
                "%s presented a different TLS certificate than the one recorded "
                "when it joined (%s, expected %s). Nothing was sent. Either the "
                "node's certificate was replaced -- have it join again -- or "
                "something is answering in its place."
                % (self.url, pretty_fingerprint(presented)[:23] + "...",
                   pretty_fingerprint(self.fingerprint)[:23] + "..."), 495)

    def request(self, method, path, body=None, params=None, timeout=None):
        """One API call. Returns the ``data`` envelope, or raises NodeError."""
        target = path
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items()
                                            if v is not None})
            if query:
                target = "%s?%s" % (path, query)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token

        conn = self._connect(timeout or self.timeout)
        try:
            conn.request(method, target, body=payload, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            status = response.status
        except NodeError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise NodeError("%s %s failed: %s" % (method, self.url + path, exc), 502)
        finally:
            conn.close()

        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            raise NodeError(
                "%s answered %s with something that is not JSON -- is it a lemondx "
                "node?" % (self.url, status), 502)
        if not isinstance(parsed, dict):
            raise NodeError("%s answered with an unexpected payload." % self.url, 502)
        if status >= 400:
            raise NodeError(parsed.get("error") or "%s refused: HTTP %d" % (self.url, status),
                            status)
        return parsed.get("data")

    # -- the calls federation makes ----------------------------------------

    def status(self):
        return self.request("GET", "/api/status")

    def whoami(self):
        return self.request("GET", "/api/auth")

    def containers(self):
        return self.request("GET", "/api/containers")

    def templates(self):
        return self.request("GET", "/api/templates")

    def save_template(self, name, body):
        return self.request("PUT", "/api/templates/%s" % _seg(name), body)

    def modules(self):
        return self.request("GET", "/api/modules")

    def upload_module(self, name, content, overwrite=True):
        return self.request("POST", "/api/modules",
                            {"name": name, "content": content, "overwrite": overwrite},
                            timeout=LONG_TIMEOUT)

    def save_profile(self, name, body):
        return self.request("PUT", "/api/bootstrap-profiles/%s" % _seg(name), body)

    def save_group(self, name, body):
        return self.request("PUT", "/api/cluster/groups/%s" % _seg(name), body)

    # Deletes of shared definitions, so removing one here removes it everywhere
    # rather than leaving copies behind that a push-only sync can never clear.
    def delete_template(self, name):
        return self.request("DELETE", "/api/templates/%s" % _seg(name))

    def delete_profile(self, name):
        return self.request("DELETE", "/api/bootstrap-profiles/%s" % _seg(name))

    def delete_group(self, name):
        return self.request("DELETE", "/api/cluster/groups/%s" % _seg(name))

    def delete_module(self, module_id):
        return self.request("DELETE", "/api/modules/%s" % _seg(module_id))

    def profiles(self):
        return self.request("GET", "/api/bootstrap-profiles")

    def launch(self, template, names, params=None, background=True):
        return self.request("POST", "/api/templates/%s/launch" % _seg(template),
                            {"names": names, "params": params or {},
                             "background": background},
                            timeout=LONG_TIMEOUT)

    def template_runs(self):
        return self.request("GET", "/api/template-runs")

    def destroy(self, template, instances, background=True):
        return self.request("POST", "/api/templates/%s/destroy" % _seg(template),
                            {"instances": instances, "background": background},
                            timeout=LONG_TIMEOUT)

    def recreate(self, template, instances, params=None, background=True):
        return self.request("POST", "/api/templates/%s/recreate" % _seg(template),
                            {"instances": instances, "params": params or {},
                             "background": background},
                            timeout=LONG_TIMEOUT)

    def exec_instances(self, template, command, instances, timeout=300, background=True):
        return self.request("POST", "/api/templates/%s/exec" % _seg(template),
                            {"command": command, "instances": instances,
                             "timeout": timeout, "background": background},
                            timeout=LONG_TIMEOUT)

    def change_state(self, names, action, force=False, timeout=60):
        return self.request("POST", "/api/containers/state",
                            {"names": names, "action": action, "force": force,
                             "timeout": timeout}, timeout=LONG_TIMEOUT)

    def delete_container(self, name, force=False):
        return self.request("DELETE", "/api/containers/%s" % _seg(name),
                            params={"force": "true" if force else "false"},
                            timeout=LONG_TIMEOUT)

    def template_instances(self, template):
        return self.request("GET", "/api/templates/%s/instances" % _seg(template))

    def enroll(self, body):
        return self.request("POST", "/api/cluster/enroll", body)

    def announce(self, member):
        """Tell a peer we are a member, and read back who it knows."""
        return self.request("POST", "/api/cluster/members", member)

    def members(self):
        return self.request("GET", "/api/cluster/members")

    def forget_member(self, name):
        return self.request("DELETE", "/api/cluster/members/%s" % _seg(name))

    def set_cluster_secret(self, secret):
        """Hand a peer a rotated cluster credential, authenticated with the old one."""
        return self.request("PUT", "/api/cluster/secret", {"secret": secret})

    def health(self):
        return self.request("GET", "/api/health")


def _seg(value):
    return urllib.parse.quote(str(value), safe="")

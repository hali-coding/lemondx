"""What lemondx did, who asked for it and how: one log for every change.

Every entry point -- an API request, a CLI command, a peer's relayed call, a
background thread of `serve` -- opens an `acting()` context saying who is
acting and through what channel. Everything logged while it is open carries
that, so the hooks that record a change (a daemon mutation in lxd.py, a host
command in hostnet.py, a record saved in store.py) never need to be told who
asked: they ask `current()`. A request id rides along, and across to peers in
a header, so one click can be followed through every node it touched.

Context is a ContextVar, which threads and thread pools do not inherit; work
handed to another thread goes through `carry()`, or it is logged as nobody.

Where it goes is the cluster's `logging` setting (a synced record, see
cluster.py): local syslog always, plus any destinations configured. A
destination is a type in `DESTINATIONS` -- only `syslog` so far -- so another
kind of log stream is one class here and one editor in the UI. Delivery runs
on a listener thread behind a queue: a slow or dead collector must never hold
up the request being logged.

Nothing here may ever be handed a secret. Callers pass names and outcomes,
never request bodies or parameter values; `_quote()` also strips newlines, so
a crafted instance name cannot forge a second log line.
"""

from __future__ import annotations

import atexit
import collections
import contextlib
import contextvars
import datetime
import json
import logging
import logging.handlers
import os
import queue
import re
import secrets
import socket
import sys
import threading
import time

LOGGER = logging.getLogger("lemondx")
LOGGER.propagate = False

# Syslog has a severity between info and warning that Python lacks; auth,
# membership and settings changes are notices.
NOTICE = 25
logging.addLevelName(NOTICE, "NOTICE")

LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "notice": NOTICE,
          "warning": logging.WARNING, "error": logging.ERROR}
LEVEL_NAMES = {value: name for name, value in LEVELS.items()}
_SYSLOG_SEVERITY = {logging.DEBUG: 7, logging.INFO: 6, NOTICE: 5,
                    logging.WARNING: 4, logging.ERROR: 3, logging.CRITICAL: 2}
FACILITIES = {"user": 1, "daemon": 3, "auth": 4, "local0": 16, "local1": 17,
              "local2": 18, "local3": 19, "local4": 20, "local5": 21,
              "local6": 22, "local7": 23}

# The record that holds the settings: one of the cluster's shared settings.
SETTING_NAME = "logging"
APP_NAME = "lemondx"
LOCAL_SOCKET = "/dev/log"

DEFAULT_SETTINGS = {
    "level": "info",
    "local": {"facility": "daemon"},
    "destinations": [],
}

# Fields every line starts with, from the acting context.
# A call relayed from another node keeps the actor, role and via it had there;
# its channel here is `peer`, `origin` names the node it came from and
# `origin_channel` how it was made there (ui, api, cli, system).
CONTEXT_KEYS = ("req", "actor", "role", "via", "channel", "origin", "origin_channel")
_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?"
                   r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*$")
_PLAIN = re.compile(r"^[A-Za-z0-9_.,:/@+-]*$")
MAX_VALUE = 200
MAX_DESTINATIONS = 8
# The live tail: how many events a `serve` keeps, how long a reader may wait
# for the next one, and the most a forwarded CLI event may weigh.
BUFFER_SIZE = 5000
MAX_WAIT = 25
MAX_PAGE = 1000
INGEST_LIMIT = 8192
KINDS = ("request", "change", "auth", "system", "access")
# Fields that name an instance, for the `instance` filter.
INSTANCE_KEYS = ("instance", "target", "name", "names", "arg_name", "new_name")


class LoggingSettingsError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


# -- who is acting -----------------------------------------------------------

_CONTEXT = contextvars.ContextVar("lemondx_acting", default=None)


def new_id():
    return secrets.token_hex(4)


@contextlib.contextmanager
def acting(actor="", role="", via="", channel="", origin="", req=None, origin_channel=""):
    """Everything logged inside is done by ``actor``, through ``channel``."""
    context = {"req": req or new_id(), "actor": actor, "role": role, "via": via,
               "channel": channel, "origin": origin, "origin_channel": origin_channel}
    token = _CONTEXT.set(context)
    try:
        yield context
    finally:
        _CONTEXT.reset(token)


def system(task):
    """The context for work `serve` starts by itself: reconcile, health, startup."""
    return acting(actor="system", channel="system", via=task)


def current():
    return _CONTEXT.get() or {}


def carry(fn):
    """``fn`` run with the caller's acting context, from whatever thread runs it.

    A fresh copy per call, not one shared Context: a pool runs the same
    function on several threads at once, and one Context cannot be entered
    twice.
    """
    snapshot = _CONTEXT.get()

    def run(*args, **kwargs):
        token = _CONTEXT.set(snapshot)
        try:
            return fn(*args, **kwargs)
        finally:
            _CONTEXT.reset(token)
    return run


# -- across nodes ------------------------------------------------------------
#
# A call relayed to a peer arrives wearing the cluster credential, which says
# only "a member". These headers say whose call it really is, and carry the
# request id so the two nodes' lines can be matched. The receiver believes
# them only from a member (server.py checks), since anyone else could claim
# to be anybody.

REQUEST_HEADER = "X-Lemondx-Request"
ACTOR_HEADER = "X-Lemondx-Actor"
_NODE = {"name": ""}
_REQ_ID = re.compile(r"^[0-9a-f]{8}$")


def set_node(name):
    """This node's name, for the `origin` a relayed call carries."""
    _NODE["name"] = str(name or "")


def relay_headers():
    context = current()
    if not context:
        return {}
    import urllib.parse
    relayed_here = context.get("channel") == "peer"
    actor = {"actor": context.get("actor") or "", "role": context.get("role") or "",
             "via": context.get("via") or "",
             # Passed on again (a stack's remote share fanning out), it is still
             # the original node and channel that made it.
             "channel": context.get("origin_channel") if relayed_here
             else context.get("channel") or "",
             "origin": context.get("origin") if relayed_here else _NODE["name"]}
    return {REQUEST_HEADER: context.get("req") or "",
            ACTOR_HEADER: urllib.parse.urlencode({k: v for k, v in actor.items() if v})}


def relayed(headers):
    """``(req, {actor, role, via, channel, origin})`` a member's call says it is for."""
    import urllib.parse
    req = str(headers.get(REQUEST_HEADER) or "")
    raw = str(headers.get(ACTOR_HEADER) or "")
    fields = {k: v[0][:64] for k, v in urllib.parse.parse_qs(raw).items()
              if k in ("actor", "role", "via", "channel", "origin") and v}
    return (req if _REQ_ID.match(req) else None), fields


# -- emitting ----------------------------------------------------------------

def enabled(level):
    return LOGGER.isEnabledFor(level)


def event(msgid, action, level=logging.INFO, _buffered=True, **fields):
    """Log one thing that happened: ``msgid`` is the kind, ``action`` what it was.

    Kinds: ``request`` (something asked for: an API call, a CLI command),
    ``change`` (something changed: the daemon, the host, a saved record),
    ``auth``, ``system`` and ``access``. ``fields`` are names and outcomes;
    empty ones are left out.
    """
    _ensure_setup()
    if not LOGGER.isEnabledFor(level):
        return
    context = current()
    parts = [("action", action)]
    parts += [(key, context.get(key)) for key in CONTEXT_KEYS]
    parts += list(fields.items())
    parts = [(key, value) for key, value in parts if value not in (None, "", [], (), {})]
    body = " ".join("%s=%s" % (key, _quote(value)) for key, value in parts)
    # The same parts as data, for the in-memory tail: a reader filters on them
    # rather than parsing the line back. `_buffered` off keeps a line out of
    # the tail (the tail's own polling) without keeping it out of syslog.
    LOGGER.log(level, body, extra={"msgid": msgid, "buffered": _buffered,
                                   "fields": {key: _plain(value) for key, value in parts}})


def message(text, level=logging.INFO, msgid="system", **fields):
    """A free-text line (what used to be printed), with the acting context."""
    event(msgid, "note", level=level, text=text, **fields)


def _plain(value):
    """A field as JSON-safe data, cleaned like `_quote()` cleans it for a line."""
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [_plain(str(v)) for v in list(value)[:50]]
    value = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return value if len(value) <= MAX_VALUE else value[:MAX_VALUE - 3] + "..."


def _quote(value):
    if isinstance(value, bool):
        value = "yes" if value else "no"
    elif isinstance(value, (list, tuple, set)):
        value = ",".join(str(v) for v in value)
    elif isinstance(value, float):
        value = "%g" % value
    value = str(value)
    # One line per event, whatever a name or an error message contains.
    value = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    if len(value) > MAX_VALUE:
        value = value[:MAX_VALUE - 3] + "..."
    if _PLAIN.match(value):
        return value
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


# -- formats -----------------------------------------------------------------

class Rfc5424Formatter(logging.Formatter):
    """``1 TIMESTAMP HOST APP PROCID MSGID - body``; the handler adds ``<PRI>``."""

    def format(self, record):
        stamp = datetime.datetime.fromtimestamp(record.created).astimezone() \
            .isoformat(timespec="milliseconds")
        return "1 %s %s %s %d %s - %s" % (
            stamp, socket.gethostname() or "-", APP_NAME, record.process,
            getattr(record, "msgid", "-") or "-", record.getMessage())


class LocalFormatter(logging.Formatter):
    """The local socket's own form, ``lemondx[PID]: MSGID body``.

    journald (and rsyslog's imuxsock) parse this one: RFC 5424 on /dev/log
    would leave journald without an identifier, and `journalctl -t lemondx`
    with nothing to find.
    """

    def format(self, record):
        return "%s[%d]: %s %s" % (APP_NAME, record.process,
                                  getattr(record, "msgid", "-") or "-", record.getMessage())


class ConsoleFormatter(logging.Formatter):
    def format(self, record):
        return "[lemondx] %s %s %s" % (LEVEL_NAMES.get(record.levelno, "info"),
                                       getattr(record, "msgid", "-") or "-",
                                       record.getMessage())


def _priority(facility, levelno):
    return FACILITIES.get(facility, 3) * 8 + _SYSLOG_SEVERITY.get(levelno, 6)


# -- destinations ------------------------------------------------------------

class Destination(logging.Handler):
    """One place lines go. Keeps its last failure, for status and the UI.

    Runs on the listener thread only, so a slow one delays other destinations
    but never a request.
    """

    def __init__(self, label):
        super().__init__()
        self.label = label
        self.sent = 0
        self.last_error = None
        self.last_error_at = None

    def failed(self, exc):
        self.last_error = str(exc) or exc.__class__.__name__
        self.last_error_at = int(time.time())

    def status(self):
        return {"label": self.label, "sent": self.sent, "error": self.last_error,
                "error_at": self.last_error_at}

    def outcome(self):
        """How the last line went, for the test button: ``(ok, detail)``."""
        return self.last_error is None, self.last_error or "delivered"


class LocalSyslog(Destination):
    """/dev/log -- journald or the local syslog daemon."""

    def __init__(self, facility):
        super().__init__("local syslog (%s)" % LOCAL_SOCKET)
        self.facility = facility
        self.setFormatter(LocalFormatter())
        self.sock = None

    def _connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.connect(LOCAL_SOCKET)
        return sock

    def emit(self, record):
        line = "<%d>%s" % (_priority(self.facility, record.levelno), self.format(record))
        try:
            if self.sock is None:
                self.sock = self._connect()
            try:
                self.sock.send(line.encode("utf-8", "replace"))
            except OSError:
                # journald restarted: the socket is new, so connect again once.
                self.sock.close()
                self.sock = self._connect()
                self.sock.send(line.encode("utf-8", "replace"))
            self.sent += 1
        except OSError as exc:
            self.sock = None
            self.failed(exc)

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        super().close()


class RemoteSyslog(Destination):
    """RFC 5424 over UDP or TCP (RFC 6587 octet counting), connected lazily.

    Neither stdlib SysLogHandler mode fits: both resolve and connect in the
    constructor, where a dead collector would block `serve` starting or a CLI
    command running. Here a failure costs the lines it drops and a status.
    """

    TIMEOUT = 3
    RETRY_SECONDS = 30

    def __init__(self, host, port, protocol, facility):
        super().__init__("%s://%s:%d" % (protocol, host, port))
        self.host, self.port, self.protocol, self.facility = host, port, protocol, facility
        self.setFormatter(Rfc5424Formatter())
        self.sock = None
        self.down_until = 0

    def _connect(self):
        kind = socket.SOCK_STREAM if self.protocol == "tcp" else socket.SOCK_DGRAM
        family, _, _, _, address = socket.getaddrinfo(self.host, self.port, 0, kind)[0]
        sock = socket.socket(family, kind)
        sock.settimeout(self.TIMEOUT)
        sock.connect(address)
        return sock

    def _frame(self, record):
        data = ("<%d>%s" % (_priority(self.facility, record.levelno),
                            self.format(record))).encode("utf-8", "replace")
        return b"%d %s" % (len(data), data) if self.protocol == "tcp" else data

    def _deliver(self, data):
        if self.sock is None:
            self.sock = self._connect()
        if self.protocol == "tcp":
            self.sock.sendall(data)
        else:
            self.sock.send(data)

    def emit(self, record):
        if time.time() < self.down_until:
            return
        try:
            self._deliver(self._frame(record))
            self.sent += 1
            self.last_error = None
        except OSError as exc:
            self._drop()
            # Not every line again: a dead TCP collector would otherwise cost
            # a connect timeout per event.
            self.down_until = time.time() + self.RETRY_SECONDS
            self.failed(exc)

    def _drop(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def ready(self):
        """Forget a backoff, so a test line is really tried."""
        self.down_until = 0

    def outcome(self):
        if self.last_error is not None:
            return False, self.last_error
        return True, ("sent; UDP cannot confirm delivery" if self.protocol == "udp"
                      else "delivered")

    def close(self):
        self._drop()
        super().close()


class EventBuffer(Destination):
    """The last BUFFER_SIZE events, in this `serve`'s memory, for the live tail.

    Not a destination anyone configures: `serve` always has one, and it
    outlives a settings change (``apply()`` keeps it), so changing the level
    does not empty the tail. ``boot`` changes with the process, which is how a
    reader's cursor from before a restart is recognised as one.
    """

    def __init__(self):
        super().__init__("recent events (in memory)")
        self.events = collections.deque(maxlen=BUFFER_SIZE)
        self.boot = new_id()
        self.seq = 0
        self.changed = threading.Condition()

    def emit(self, record):
        if not getattr(record, "buffered", True):
            return
        fields = getattr(record, "fields", None) or {"action": "note",
                                                     "text": record.getMessage()}
        self.add(record.created, record.levelno, getattr(record, "msgid", "-") or "-",
                 fields, "serve")

    def add(self, ts, levelno, kind, fields, source):
        with self.changed:
            self.seq += 1
            self.events.append({
                "seq": self.seq, "ts": ts, "node": _NODE["name"],
                "level": LEVEL_NAMES.get(levelno, "info"), "levelno": levelno,
                "kind": kind, "action": fields.get("action") or "", "fields": fields,
                "source": source})
            self.sent += 1
            self.changed.notify_all()


class ServeForwarder(Destination):
    """A CLI process's events, handed to the local `serve` for its tail.

    Over a datagram socket in the data directory, so only this user's own
    processes can reach it. Fire and forget: a CLI command never fails or
    waits because `serve` is not running.
    """

    def __init__(self, path):
        super().__init__("local serve (%s)" % path)
        self.path = path
        self.sock = None

    def emit(self, record):
        if not getattr(record, "buffered", True):
            return
        payload = json.dumps({
            "ts": record.created, "level": LEVEL_NAMES.get(record.levelno, "info"),
            "kind": getattr(record, "msgid", "-") or "-",
            "fields": getattr(record, "fields", None) or {}}).encode("utf-8", "replace")
        if len(payload) > INGEST_LIMIT:
            return
        try:
            if self.sock is None:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.sock.sendto(payload, self.path)
            self.sent += 1
        except OSError as exc:
            self.failed(exc)

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        super().close()


class SyslogDestinationType:
    """`{"type": "syslog", "host", "port", "protocol", "facility", "enabled"}`."""

    name = "syslog"

    @staticmethod
    def clean(raw):
        host = str(raw.get("host") or "").strip()
        if not host or not (_HOST.match(host) or _is_ip(host)):
            raise LoggingSettingsError("A syslog destination needs a host name or address "
                                       "(got '%s')." % host)
        try:
            port = int(raw.get("port") or 514)
        except (TypeError, ValueError):
            raise LoggingSettingsError("The syslog port must be a number.")
        if not 1 <= port <= 65535:
            raise LoggingSettingsError("The syslog port must be between 1 and 65535.")
        protocol = str(raw.get("protocol") or "udp").lower()
        if protocol not in ("udp", "tcp"):
            raise LoggingSettingsError("The syslog protocol is udp or tcp.")
        facility = str(raw.get("facility") or "local0").lower()
        if facility not in FACILITIES:
            raise LoggingSettingsError("Unknown syslog facility '%s'. Use one of: %s"
                                       % (facility, ", ".join(FACILITIES)))
        return {"type": "syslog", "enabled": raw.get("enabled") is not False,
                "host": host, "port": port, "protocol": protocol, "facility": facility}

    @staticmethod
    def handler(destination):
        return RemoteSyslog(destination["host"], destination["port"],
                            destination["protocol"], destination["facility"])


# Another kind of log stream (a file, TLS syslog, an HTTP collector) is one
# more entry here and one editor in web/src/components/LoggingSettings.tsx.
DESTINATIONS = {"syslog": SyslogDestinationType}


def _is_ip(text):
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, text)
            return True
        except OSError:
            continue
    return False


# -- settings ----------------------------------------------------------------

def clean_settings(raw):
    """Validate a settings object; raises LoggingSettingsError."""
    if not isinstance(raw, dict):
        raise LoggingSettingsError("Logging settings must be an object.")
    level = str(raw.get("level") or "info").lower()
    if level not in LEVELS:
        raise LoggingSettingsError("Unknown log level '%s'. Use one of: %s"
                                   % (level, ", ".join(LEVELS)))
    local = raw.get("local") if isinstance(raw.get("local"), dict) else {}
    facility = str(local.get("facility") or "daemon").lower()
    if facility not in FACILITIES:
        raise LoggingSettingsError("Unknown syslog facility '%s'. Use one of: %s"
                                   % (facility, ", ".join(FACILITIES)))
    destinations = raw.get("destinations") or []
    if not isinstance(destinations, list):
        raise LoggingSettingsError("'destinations' must be a list.")
    if len(destinations) > MAX_DESTINATIONS:
        raise LoggingSettingsError("At most %d log destinations." % MAX_DESTINATIONS)
    cleaned = []
    for destination in destinations:
        if not isinstance(destination, dict):
            raise LoggingSettingsError("Each destination must be an object.")
        kind = DESTINATIONS.get(str(destination.get("type") or ""))
        if kind is None:
            raise LoggingSettingsError("Unknown log destination type '%s'. Known: %s"
                                       % (destination.get("type"), ", ".join(DESTINATIONS)))
        cleaned.append(kind.clean(destination))
    return {"level": level, "local": {"facility": facility}, "destinations": cleaned}


def load_settings():
    """``(settings, warning)`` from the shared record; defaults when unusable."""
    from . import store
    record = store.load_settings_records().get(SETTING_NAME)
    if record is None:
        return clean_settings(DEFAULT_SETTINGS), None
    try:
        return clean_settings(record["value"]), None
    except LoggingSettingsError as exc:
        return clean_settings(DEFAULT_SETTINGS), "Logging settings ignored: %s" % exc.message


# -- the pipeline --------------------------------------------------------------

_lock = threading.RLock()
_state = {"setup": False, "settings": None, "handlers": [], "listener": None,
          "queue": None, "echo": False, "forward": False, "fingerprint": None,
          "checked": 0.0, "local_fallback": False, "warning": None}
_BUFFER = {"buffer": None}
REFRESH_SECONDS = 5


def setup(echo=False, buffer=False, forward=False):
    """Start logging for this process from the saved settings.

    ``echo`` mirrors lines to stderr too: `serve` in a terminal, where nobody
    is watching the journal. Under systemd stderr is the journal already, so
    it stays off there. ``buffer`` keeps recent events in memory for the live
    tail (`serve`); ``forward`` hands them to the local `serve`'s tail instead
    (a CLI command).
    """
    with _lock:
        _state["echo"] = bool(echo)
        _state["forward"] = bool(forward)
        _state["setup"] = True
        if buffer and _BUFFER["buffer"] is None:
            _BUFFER["buffer"] = EventBuffer()
        settings, warning = load_settings()
        _state["fingerprint"] = _fingerprint()
        apply(settings)
    if warning:
        event("system", "logging.settings", level=logging.WARNING, error=warning)


def _ensure_setup():
    if not _state["setup"]:
        setup()
    else:
        refresh()


def apply(settings):
    """Swap every destination for the ones ``settings`` describe."""
    settings = clean_settings(settings)
    level = LEVELS[settings["level"]]
    # The tail shows every change whatever syslog is set to: a person watching
    # wants to see what is happening, and a quiet syslog is about storage.
    tail_level = min(level, logging.INFO)
    with _lock:
        handlers = []
        local = LocalSyslog(settings["local"]["facility"])
        _state["local_fallback"] = not os.path.exists(LOCAL_SOCKET)
        handlers.append(local)
        if _state["local_fallback"] or _state["echo"]:
            console = logging.StreamHandler(sys.stderr)
            console.setFormatter(ConsoleFormatter())
            handlers.append(console)
        for destination in settings["destinations"]:
            if destination["enabled"]:
                handlers.append(DESTINATIONS[destination["type"]].handler(destination))
        for handler in handlers:
            handler.setLevel(level)
        tail = None
        if _BUFFER["buffer"] is not None:
            tail = _BUFFER["buffer"]
        elif _state["forward"]:
            from . import store
            path = store.events_socket_path()
            if os.path.exists(path):
                tail = ServeForwarder(path)
        if tail is not None:
            tail.setLevel(tail_level)
            handlers.append(tail)

        old = _state["listener"]
        if old is not None:
            old.stop()
            for handler in _state["handlers"]:
                if handler is not _BUFFER["buffer"]:
                    handler.close()
        if _state["queue"] is None:
            _state["queue"] = queue.SimpleQueue()
            LOGGER.handlers = [logging.handlers.QueueHandler(_state["queue"])]
        listener = logging.handlers.QueueListener(_state["queue"], *handlers,
                                                  respect_handler_level=True)
        listener.start()
        _state.update(listener=listener, handlers=handlers, settings=settings)
        LOGGER.setLevel(tail_level if tail is not None else level)
    return settings


def refresh(force=False):
    """Pick up settings another process saved (the CLI, a peer's push)."""
    now = time.time()
    if not force and now - _state["checked"] < REFRESH_SECONDS:
        return
    _state["checked"] = now
    fingerprint = _fingerprint()
    if fingerprint == _state["fingerprint"]:
        return
    with _lock:
        _state["fingerprint"] = fingerprint
        settings, warning = load_settings()
        apply(settings)
    if warning:
        event("system", "logging.settings", level=logging.WARNING, error=warning)


def _fingerprint():
    from . import store
    path = store.settings_record_path(SETTING_NAME)
    try:
        info = os.stat(path) if path else None
    except OSError:
        return None
    return (info.st_ino, info.st_mtime_ns, info.st_size) if info else None


def flush():
    """Deliver what is queued; for a CLI command about to exit."""
    with _lock:
        listener = _state["listener"]
        if listener is not None:
            listener.stop()
            listener.start()


atexit.register(flush)


def status():
    """This process's settings and how each destination is doing."""
    _ensure_setup()
    with _lock:
        settings = _state["settings"] or clean_settings(DEFAULT_SETTINGS)
        handlers = [h for h in _state["handlers"] if isinstance(h, Destination)]
        return {
            "settings": settings,
            "levels": list(LEVELS),
            "facilities": list(FACILITIES),
            "destination_types": list(DESTINATIONS),
            "local_socket": LOCAL_SOCKET,
            "local_fallback": _state["local_fallback"],
            "echo": _state["echo"],
            "destinations": [h.status() for h in handlers
                             if not isinstance(h, (EventBuffer, ServeForwarder))],
            "buffer": ({"events": len(_BUFFER["buffer"].events), "size": BUFFER_SIZE,
                        "boot": _BUFFER["buffer"].boot}
                       if _BUFFER["buffer"] is not None else None),
        }


def test(by=""):
    """Send a test line everywhere, now, and say how each went."""
    _ensure_setup()
    with _lock:
        for handler in _state["handlers"]:
            if isinstance(handler, RemoteSyslog):
                handler.ready()
    event("system", "logging.test", level=NOTICE, by=by)
    flush()
    with _lock:
        results = []
        for handler in _state["handlers"]:
            if isinstance(handler, Destination) \
                    and not isinstance(handler, (EventBuffer, ServeForwarder)):
                ok, detail = handler.outcome()
                results.append({"label": handler.label, "ok": ok, "detail": detail})
    return {"results": results, "ok": all(r["ok"] for r in results)}


# -- the live tail -------------------------------------------------------------
#
# What `GET /api/logs` reads. A cursor is ``<boot>:<seq>``: everything after
# that event of that process. A reader with no cursor gets the latest events
# (the backlog a tail opens with); one with a cursor waits, up to ``wait``
# seconds, for the next matching event. The cursor returned is always where
# to ask from next, including past events that did not match the filters, so
# a filtered tail does not scan the same events again.

def buffer_present():
    return _BUFFER["buffer"] is not None


def read(after=None, limit=200, wait=0, filters=None):
    buffer = _BUFFER["buffer"]
    if buffer is None:
        raise LoggingSettingsError("This process keeps no recent events; `serve` does.", 404)
    try:
        limit = max(1, min(MAX_PAGE, int(limit or 200)))
        wait = max(0.0, min(float(MAX_WAIT), float(wait or 0)))
    except (TypeError, ValueError):
        raise LoggingSettingsError("limit and wait must be numbers.")
    match = _matcher(filters or {})
    boot, _, seq = str(after or "").partition(":")
    reset = bool(after) and boot != buffer.boot
    try:
        seq = 0 if reset or not after else int(seq)
    except ValueError:
        raise LoggingSettingsError("'after' must be a cursor this API returned.")
    deadline = time.time() + wait
    with buffer.changed:
        oldest = buffer.events[0]["seq"] if buffer.events else buffer.seq + 1
        truncated = bool(after) and not reset and seq + 1 < oldest

        def page(events, cursor):
            return {"node": _NODE["name"], "boot": buffer.boot,
                    "cursor": "%s:%d" % (buffer.boot, cursor), "reset": reset,
                    "truncated": truncated,
                    "events": [{k: v for k, v in e.items() if k != "levelno"}
                               for e in events]}

        if not after:
            return page([e for e in buffer.events if match(e)][-limit:], buffer.seq)
        while True:
            matched = []
            for entry in buffer.events:
                if entry["seq"] > seq and match(entry):
                    matched.append(entry)
                    if len(matched) >= limit:
                        return page(matched, matched[-1]["seq"])
            if matched:
                return page(matched, buffer.seq)
            # Nothing that matches up to here: no need to look at these again.
            seq = buffer.seq
            remaining = deadline - time.time()
            if remaining <= 0:
                return page([], seq)
            buffer.changed.wait(remaining)


def _matcher(filters):
    wanted = {key: str(value).strip() for key, value in filters.items()
              if value not in (None, "") and str(value).strip()}
    minimum = LEVELS.get(wanted.get("level", "").lower())
    kinds = set(wanted["kind"].split(",")) if "kind" in wanted else None
    text = wanted.get("q", "").lower()

    def match(entry):
        fields = entry["fields"]
        if minimum is not None and entry["levelno"] < minimum:
            return False
        if kinds and entry["kind"] not in kinds:
            return False
        for key in ("actor", "req", "channel"):
            if key in wanted and str(fields.get(key, "")) != wanted[key]:
                return False
        if "instance" in wanted:
            name = wanted["instance"]
            if not any(value == name or (isinstance(value, list) and name in value)
                       for key, value in fields.items() if key in INSTANCE_KEYS):
                return False
        if text and text not in line_of(entry).lower():
            return False
        return True
    return match


def line_of(entry):
    """An event as the key=value line syslog got, for searching and the CLI."""
    return "%s %s" % (entry["kind"], " ".join(
        "%s=%s" % (key, _quote(value)) for key, value in entry["fields"].items()))


# -- events from CLI processes -------------------------------------------------

def start_ingest(path):
    """Take CLI processes' events into this `serve`'s tail; returns a closer.

    A datagram socket in the data directory, 0600: the same user's processes
    only, which is the boundary everything else in that directory has too.
    What arrives goes into the tail and nowhere else -- the CLI sent it to
    syslog itself -- and is cleaned like any other untrusted input.
    """
    buffer = _BUFFER["buffer"]
    if buffer is None:
        return lambda: None
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    umask = os.umask(0o177)
    try:
        sock.bind(path)
    finally:
        os.umask(umask)

    def loop():
        while True:
            try:
                data = sock.recv(INGEST_LIMIT + 1)
            except OSError:
                return
            entry = _ingested(data)
            if entry is not None:
                buffer.add(*entry)

    threading.Thread(target=loop, name="lemondx-ingest", daemon=True).start()

    def close():
        try:
            sock.close()
            os.unlink(path)
        except OSError:
            pass
    return close


def _ingested(data):
    """``(ts, levelno, kind, fields, source)`` from one datagram, or None."""
    if len(data) > INGEST_LIMIT:
        return None
    try:
        raw = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("fields"), dict):
        return None
    level = LEVELS.get(str(raw.get("level")))
    kind = str(raw.get("kind"))
    try:
        ts = float(raw.get("ts"))
    except (TypeError, ValueError):
        return None
    if level is None or kind not in KINDS or abs(ts - time.time()) > 3600:
        return None
    fields = {}
    for key, value in list(raw["fields"].items())[:40]:
        if isinstance(key, str) and re.match(r"^[a-z_]{1,32}$", key):
            fields[key] = _plain(value) if isinstance(value, (str, int, float, bool, list)) \
                else _plain(str(value))
    if not fields.get("action"):
        return None
    return ts, level, kind, fields, "cli"

"""Performance history for the Monitor page: the host and each instance, over time.

``lemondx top`` works its rates out in the terminal, between two readings it
took itself, so it starts from nothing every time it is opened. The web UI
cannot do the same: a page opened in the middle of a load test should show
how the load arrived, not start drawing from the moment it was opened, and a
tab switched away and back should not lose what it had. So `serve` samples
every ``PERIOD`` seconds on a thread of its own and keeps ``RETENTION``
seconds of points in memory, and the page asks for what came after the last
point it holds.

Each sample is one instance listing from the daemon -- a few milliseconds --
plus host figures read from ``/proc``. The daemon's ``/1.0/resources`` would
give host memory too, but it walks the whole of sysfs (a sixth of a second
here), far too heavy to ask every two seconds; and it has no CPU usage at all,
which is why ``top`` can only show instances' share of the host. Reading
``/proc/stat`` gives the host's real CPU, which is the number wanted when the
load generator itself runs on the host. Like ``health.LoadSampler``, that
needs lemondx on the daemon's host, which the unix socket already implies.

Rates are differences between consecutive readings, so an instance's first
sample has none, and a counter that went backwards (a restart) gives none for
that step rather than a negative rate. A point is kept only for a running
instance: a stopped one leaves a gap, which the chart draws as a break.
"""

from __future__ import annotations

import collections
import logging
import threading
import time

from . import eventlog

PERIOD = 2.0
RETENTION = 30 * 60
# The most a caller may ask for at once, so a page's first request is bounded
# by what is kept rather than by what it asks for.
MAX_WINDOW = RETENTION


# -- host readings ----------------------------------------------------------


def read_cpu():
    """``(busy, total, threads)`` in jiffies since boot from /proc/stat, or None."""
    try:
        with open("/proc/stat", "r", encoding="ascii") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    fields = lines[0].split() if lines else []
    if len(fields) < 5 or fields[0] != "cpu":
        return None
    try:
        # user nice system idle iowait irq softirq steal; guest time is
        # already inside user and nice, so it is not added again.
        values = [int(v) for v in fields[1:9]]
    except ValueError:
        return None
    total = sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    threads = sum(1 for line in lines if line.startswith("cpu") and line[3:4].isdigit())
    return total - idle, total, threads


def read_memory():
    """``(used, total)`` in bytes, used being what is not available to start new work."""
    found = {}
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    found[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    if "MemTotal" not in found or "MemAvailable" not in found:
        return None
    return found["MemTotal"] - found["MemAvailable"], found["MemTotal"]


def uplink_device():
    """The interface the preferred IPv4 default route leaves by, or "".

    Read from /proc/net/route rather than asked of ``ip`` (as
    ``hostnet.default_route_device()`` does), since this runs every sample
    and a file read costs nothing.
    """
    best = None
    try:
        with open("/proc/net/route", "r", encoding="ascii") as handle:
            next(handle, None)
            for line in handle:
                f = line.split()
                if len(f) < 8 or f[1] != "00000000" or f[7] != "00000000":
                    continue
                if not int(f[3], 16) & 0x1:         # RTF_UP
                    continue
                metric = int(f[6])
                if best is None or metric < best[0]:
                    best = (metric, f[0])
    except (OSError, ValueError):
        return ""
    return best[1] if best else ""


def read_interface(device):
    """``(rx_bytes, tx_bytes)`` for one host interface, or None."""
    if not device:
        return None
    try:
        with open("/proc/net/dev", "r", encoding="ascii") as handle:
            for line in handle:
                name, sep, rest = line.partition(":")
                if sep and name.strip() == device:
                    f = rest.split()
                    return int(f[0]), int(f[8])
    except (OSError, ValueError, IndexError):
        return None
    return None


def read_host():
    device = uplink_device()
    return {"cpu": read_cpu(), "memory": read_memory(), "uplink": device,
            "net": read_interface(device)}


# -- the history ----------------------------------------------------------


def _rate(current, previous, span):
    if current is None or previous is None or current < previous:
        return None
    return (current - previous) / span


class Recorder:
    """Every node's ring of samples: one for the host, one per instance.

    Thread-safe: the sampling thread writes, request threads read.
    """

    def __init__(self, period=PERIOD, retention=RETENTION):
        self.period = period
        self.retention = retention
        size = int(retention / period) + 1
        self._size = size
        self._lock = threading.Lock()
        self._host = collections.deque(maxlen=size)
        self._host_info = {"cpu_threads": 0, "memory_total": 0, "uplink": ""}
        self._host_last = None          # (at, busy, total, rx, tx)
        self._instances = {}            # name -> {"info", "points", "last"}
        self._at = None
        self.started_at = time.time()

    def record(self, at, host, instances):
        """Fold one sample in. ``instances`` are ``ContainerService`` readings."""
        # Rounded as the points are, so a cursor taken from ``at`` never sits
        # just below a point it has already been given.
        at = round(at, 2)
        with self._lock:
            self._at = at
            self._record_host(at, host)
            seen = set()
            for reading in instances:
                name = reading.get("name")
                if not name:
                    continue
                seen.add(name)
                self._record_instance(at, reading)
            # An instance that is gone takes its history with it: a new one of
            # the same name is another instance and must not be compared with it.
            for name in list(self._instances):
                if name not in seen:
                    del self._instances[name]

    def _record_host(self, at, host):
        cpu, memory, net = host.get("cpu"), host.get("memory"), host.get("net")
        if cpu:
            self._host_info["cpu_threads"] = cpu[2]
        if memory:
            self._host_info["memory_total"] = memory[1]
        uplink = host.get("uplink") or ""
        previous = self._host_last
        if previous and uplink != self._host_info["uplink"]:
            # A different interface's counters are not a continuation.
            previous = (previous[0], previous[1], previous[2], None, None)
        self._host_info["uplink"] = uplink
        self._host_last = (at, cpu[0] if cpu else None, cpu[1] if cpu else None,
                           net[0] if net else None, net[1] if net else None)
        if not previous or at <= previous[0]:
            return
        span = at - previous[0]
        busy = total = None
        if cpu and previous[1] is not None:
            busy, total = cpu[0] - previous[1], cpu[1] - previous[2]
        cpu_percent = round(busy / total * 100, 1) if total and busy >= 0 else None
        rx = _rate(net[0] if net else None, previous[3], span)
        tx = _rate(net[1] if net else None, previous[4], span)
        self._host.append((at, cpu_percent, memory[0] if memory else None,
                           _round(rx), _round(tx)))

    def _record_instance(self, at, reading):
        name = reading["name"]
        entry = self._instances.get(name)
        if entry is None:
            entry = self._instances[name] = {
                "points": collections.deque(maxlen=self._size), "last": None}
        entry["info"] = {k: reading.get(k) for k in
                         ("status", "type", "template", "stack", "processes")}
        running = reading.get("status") == "Running"
        last = entry["last"]
        current = (at, reading.get("cpu_ns") or 0, reading.get("rx") or 0,
                   reading.get("tx") or 0)
        entry["last"] = current if running else None
        if not running or not last or at <= last[0]:
            return
        span = at - last[0]
        cpu = _rate(current[1], last[1], span)
        cpu = round(cpu / 1e9 * 100, 1) if cpu is not None else None
        entry["points"].append((at, cpu, reading.get("memory") or 0,
                                _round(_rate(current[2], last[2], span)),
                                _round(_rate(current[3], last[3], span))))

    def read(self, since=None, window=None):
        """What came after ``since`` (and within ``window`` seconds of now).

        Points are ``[at, cpu %, memory bytes, rx B/s, tx B/s]`` -- arrays
        rather than objects, because a first request carries every instance's
        whole window. A host's CPU is a share of all its threads; an
        instance's is of one, as ``top`` shows it.
        """
        try:
            since = float(since) if since not in (None, "") else None
            window = float(window) if window not in (None, "") else None
        except (TypeError, ValueError):
            raise ValueError("since and window must be numbers.")
        window = min(MAX_WINDOW, window) if window and window > 0 else MAX_WINDOW
        floor = time.time() - window
        if since is not None:
            floor = max(floor, since)

        def after(points):
            return [list(p) for p in points if p[0] > floor]

        with self._lock:
            return {
                "period": self.period, "retention": self.retention,
                "started_at": self.started_at, "at": self._at,
                "host": dict(self._host_info, points=after(self._host)),
                "instances": [dict(entry["info"], name=name, points=after(entry["points"]))
                              for name, entry in sorted(self._instances.items())],
            }

    def start(self, sample):
        """Sample every ``period`` on a daemon thread. ``sample()`` returns readings."""
        def loop():
            failing = False
            while True:
                started = time.time()
                try:
                    instances = sample()
                    self.record(time.time(), read_host(), instances)
                    failing = False
                except Exception as exc:                    # noqa: BLE001
                    # Said once per spell, not every two seconds.
                    if not failing:
                        eventlog.message("metrics sampling failed: %s" % exc,
                                         level=logging.WARNING)
                    failing = True
                time.sleep(max(0.5, self.period - (time.time() - started)))

        def run():
            with eventlog.system("metrics"):
                loop()
        threading.Thread(target=run, name="lemondx-metrics", daemon=True).start()


def _round(value):
    return None if value is None else int(round(value))

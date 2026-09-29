"""``lemondx top``: a live, full-screen view of nodes, instances and stacks.

In the spirit of btop -- redrawn in place, keys to scroll and sort, nothing to
click -- and read-only: every figure comes from calls the CLI and the UI
already make, fanned out to every member on a background thread, so a slow
peer delays its own rows and never the keyboard.

Two things exist only inside a running ``serve``: health records and stack
runs, both kept in memory by the process that made them. For this node they
are asked of the local ``serve`` over loopback, and for a peer of that peer's
API like everything else about it. With no ``serve`` here the view still
works; those columns are blank for this node and the header says why.

CPU is worked out here, from the daemon's cumulative CPU time between two
readings, so the first frame has none. A node's CPU meter is the share of its
threads its instances are using rather than the host's whole load, which the
API does not report; its memory meter is the host's, which it does.

``c`` (or Enter) attaches to the selected instance's console, handing the whole
terminal to it until Ctrl-] -- ``virsh console``'s key, since a guest
never needs it. On this node that is the daemon's console session directly,
as the server's own terminal bridge opens it; on a peer it is that peer's
terminal endpoint, over the same pinned connection and cluster credential as
every other call to it.

Space marks instances and ``b`` runs a bootstrap on the marked ones (or the
selected one), wherever each lives: a saved profile, or modules ticked by hand,
then their parameters -- this node's saved values, which is what is sent, so
every target runs with what was on screen. Secrets are typed in and never
kept. Nothing is remembered as a module default either way: a run from here is
a one-off, and on this node and on a peer alike it passes ``remember=False``.
Runs happen on threads and are followed in their own panel; quitting while one
is running asks twice, since a run on this node stops part-way with top.

Tab moves from the instances to the stacks panel and the templates panel,
each listing every definition this node holds, with keys for what can be done
to the selected one: start, stop and restart its instances; relaunch, destroy
or cancel a stack; launch, recreate or destroy from a template. Those go to
this node's ``serve`` -- a launch, recreate, destroy or relaunch is an
ordinary run there, which outlives top and shows in the panel and the web UI
like any other -- and a cancel goes to whichever node holds the run. Start,
stop and restart are quick enough to run here when there is no ``serve``; the
actions that start a run are not, since that run would die with top.
"""

from __future__ import annotations

import collections
import json
import os
import re
import select
import shutil
import signal
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from . import store
from .cluster import ClusterError, certificate_fingerprint
from .lxd import LXDError
from .nodeclient import LONG_TIMEOUT, NodeClient, NodeError
from .service import ServiceError
from .websocket import BINARY, WebSocketError

# A peer that takes longer than this is shown as unreachable for the round
# rather than holding up every other node's rows.
PEER_TIMEOUT = 6
FANOUT_WORKERS = 8
HISTORY = 120               # samples of each node's meters, for the sparklines
INTERVALS = (1, 2, 3, 5, 10)
# A finished stack run stays on screen this long, so a launch that ends
# between two glances is still seen to have ended -- and how.
RUN_LINGER = 600
TICK = 0.25                 # how often keys and resizes are noticed
DETACH = b"\x1d"            # Ctrl-]: back from a console to top

_ERRORS = (ClusterError, NodeError, ServiceError, LXDError)


def _message(exc):
    return getattr(exc, "message", None) or str(exc)


def _natural(name):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name or "")]


# -- gathering -------------------------------------------------------------


class Collector:
    """Builds one snapshot of the whole cluster per call. Not thread-safe.

    Keeps the previous reading of each instance, since CPU and network rates
    are differences between two, and a short history of each node's meters.
    """

    def __init__(self, cluster):
        self.cluster = cluster
        self.service = cluster.service
        self._samples = {}      # (node, name) -> (at, cpu_ns, rx, tx)
        self._history = {}      # node -> {"cpu": deque, "mem": deque}

    def snapshot(self):
        local = self.cluster.local_name()
        names = self.cluster.all_nodes()
        api, api_error = self.local_api()
        with ThreadPoolExecutor(max_workers=min(len(names), FANOUT_WORKERS)) as pool:
            views = list(pool.map(lambda n: self._read_node(n, n == local, api), names))

        now = time.time()
        nodes, instances, runs, template_runs, seen = [], [], [], [], set()
        for view in views:
            health = {r.get("name"): r for r in (view["health"] or {}).get("instances") or []
                      if isinstance(r, dict)}
            rows = [self._instance(view["node"], c, health.get(c.get("name")), now)
                    for c in view["instances"] if isinstance(c, dict)]
            seen.update((r["node"], r["name"]) for r in rows)
            instances.extend(rows)
            runs.extend(_run_summary(r, view["node"], now)
                        for r in view["runs"] or [] if isinstance(r, dict))
            template_runs.extend(_template_run_summary(r, view["node"], now)
                                 for r in view["template_runs"] or [] if isinstance(r, dict))
            nodes.append(self._node(view, rows))
            if view["local"] and view["api_error"]:
                api_error = view["api_error"]
        # An instance that is gone must not leave a reading behind for a new
        # one of the same name to be compared against.
        self._samples = {k: v for k, v in self._samples.items() if k in seen}
        return {
            "at": now, "node": local, "in_cluster": self.cluster.in_cluster(),
            "serve": {"running": api is not None, "error": api_error},
            "nodes": nodes, "instances": instances,
            # Every definition this node holds, not only those with instances:
            # they are synced, so this node's list is the cluster's, and one
            # that has never been launched is still something to launch.
            "stacks": _stacks(instances, runs, now, sorted(store.load_stacks())),
            "templates": _templates(instances, template_runs, now,
                                    self._defined_templates()),
        }

    def _defined_templates(self):
        try:
            return self.service.list_templates()
        except _ERRORS:
            return []

    def local_api(self):
        """A client for this node's own ``serve``, or ``(None, why not)``."""
        runtime = store.read_runtime()
        if not isinstance(runtime, dict) or not _alive(runtime.get("pid")):
            return None, "no `lemondx serve` running here"
        port = runtime.get("port")
        if not isinstance(port, int):
            return None, "serve did not record its port"
        host = str(runtime.get("host") or "")
        host = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        fingerprint = ""
        if runtime.get("tls"):
            # Pinned like any peer: the token below must never be handed to
            # whatever else might be answering on that port.
            cert, _ = self.cluster.certificate()
            if not cert:
                return None, "serve uses a TLS certificate this node cannot pin"
            try:
                fingerprint = certificate_fingerprint(cert)
            except ClusterError as exc:
                return None, exc.message
        token = None
        if self.cluster.auth.config.enabled or self.cluster.requires_remote_token():
            # The cluster credential is an ordinary admin token this node
            # accepts too, so a member needs nothing configured to read itself.
            token = os.environ.get("LEMONDX_TOKEN") or self.cluster.cluster_secret()
        url = "%s://%s:%d" % ("https" if runtime.get("tls") else "http",
                              "[%s]" % host if ":" in host else host, port)
        try:
            return NodeClient(url, token=token, fingerprint=fingerprint,
                              timeout=PEER_TIMEOUT), None
        except NodeError as exc:
            return None, exc.message

    def _read_node(self, name, local, api):
        view = {"node": name, "local": local, "error": None, "instances": [],
                "host": None, "health": None, "runs": None, "template_runs": None,
                "api_error": None}
        try:
            if local:
                source = api
                view["instances"] = self.service.list_containers()
            else:
                source = self.cluster.client(name, timeout=PEER_TIMEOUT)
                view["instances"] = source.containers() or []
        except _ERRORS as exc:
            view["error"] = _message(exc)
            return view
        try:
            report = self.service.resources() if local else (source.resources() or {})
            view["host"] = report.get("host")
        except _ERRORS:
            pass
        if source is not None:
            try:
                view["health"] = source.health()
                view["runs"] = source.request("GET", "/api/stack-runs")
                view["template_runs"] = source.template_runs()
            except NodeError as exc:
                if local:
                    view["api_error"] = "serve here: %s" % exc.message
        return view

    def _instance(self, node, c, health, now):
        key = (node, c.get("name"))
        cpu_ns = c.get("cpu_time_ns") or 0
        rx, tx = c.get("network_rx") or 0, c.get("network_tx") or 0
        previous = self._samples.get(key)
        self._samples[key] = (now, cpu_ns, rx, tx)
        running = c.get("status") == "Running"
        cpu = rx_rate = tx_rate = None
        if previous and running and now > previous[0]:
            span = now - previous[0]
            # A counter that went backwards is a restarted instance, not a
            # negative rate.
            if cpu_ns >= previous[1] and cpu_ns:
                cpu = round((cpu_ns - previous[1]) / (span * 1e9) * 100, 1)
            if rx >= previous[2]:
                rx_rate = (rx - previous[2]) / span
            if tx >= previous[3]:
                tx_rate = (tx - previous[3]) / span
        app = (health or {}).get("app") or {}
        return {
            "node": node, "name": c.get("name") or "", "status": c.get("status") or "Unknown",
            "type": c.get("type") or "container", "ipv4": c.get("ipv4") or [],
            "memory": (c.get("memory_usage") or 0) if running else 0,
            "processes": (c.get("processes") or 0) if running else 0,
            "cpu": cpu, "rx_rate": rx_rate, "tx_rate": tx_rate,
            "template": c.get("template"), "stack": c.get("stack"),
            "stale": c.get("stale") or [],
            "health": (health or {}).get("status") if running else None,
            "app": app.get("status") if running else None,
        }

    def _node(self, view, rows):
        name = view["node"]
        host = view["host"] or {}
        threads = host.get("cpu_threads") or 0
        total, used = host.get("memory_total") or 0, host.get("memory_used") or 0
        running = [r for r in rows if r["status"] == "Running"]
        measured = [r["cpu"] for r in running if r["cpu"] is not None]
        # Percent of one core per instance, summed, over the host's threads.
        # Nothing running is a real zero; running but not yet measured is not.
        cpu = None
        if threads and (measured or not running):
            cpu = round(min(100.0, sum(measured) / threads), 1)
        memory = round(used / total * 100, 1) if total else None
        history = self._history.setdefault(name, {"cpu": collections.deque(maxlen=HISTORY),
                                                  "mem": collections.deque(maxlen=HISTORY)})
        if not view["error"]:
            history["cpu"].append(cpu)
            history["mem"].append(memory)
        maintenance = self.cluster.maintenance_of(name)
        return {
            "name": name, "self": view["local"],
            "state": "unreachable" if view["error"] else
                     "maintenance" if maintenance else "ok",
            "error": view["error"], "maintenance": maintenance,
            "instances": len(rows), "running": len(running),
            "cpu": cpu, "cpu_threads": threads,
            "memory": memory, "memory_used": used, "memory_total": total,
            "history": {"cpu": list(history["cpu"]), "mem": list(history["mem"])},
        }


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stage_state(steps):
    states = [s.get("state") for s in steps]
    if "failed" in states:
        return "failed"
    if "running" in states:
        return "running"
    if states and all(s in ("done", "ready") for s in states):
        return "done"
    if states and all(s in ("skipped", "cancelled") for s in states):
        return "skipped"
    return "pending"


def _run_summary(run, node, now):
    """A stack run cut down to what a status line needs."""
    stages, active = [], []
    for stage in run.get("stages") or []:
        steps = [s for s in stage.get("steps") or [] if isinstance(s, dict)]
        stages.append(_stage_state(steps))
        for step in steps:
            if step.get("state") == "running":
                label = "teardown" if step.get("id") == "_teardown" else step.get("id")
                active.append("%s: %s" % (label, step.get("detail") or "running"))
    started = run.get("started_at") or now
    finished = run.get("finished_at")
    return {
        "stack": run.get("stack") or "", "node": node, "action": run.get("action") or "launch",
        "started_at": started, "finished_at": finished, "ok": run.get("ok"),
        "cancelling": bool(run.get("cancelling")), "cancelled": bool(run.get("cancelled")),
        "error": run.get("error"), "stages": stages, "active": "; ".join(active),
        "elapsed": max(0.0, (finished or now) - started),
    }


def _stacks(instances, runs, now, defined=()):
    """Every stack defined here, with instances somewhere, or a run worth showing."""
    found = {name: {"members": [], "run": None} for name in defined}
    for c in instances:
        if c["stack"]:
            found.setdefault(c["stack"], {"members": [], "run": None})["members"].append(c)
    for run in runs:
        if run["finished_at"] is not None and now - run["finished_at"] > RUN_LINGER:
            continue
        entry = found.setdefault(run["stack"], {"members": [], "run": None})
        if entry["run"] is None or run["started_at"] > entry["run"]["started_at"]:
            entry["run"] = run
    stacks = []
    for name in sorted(found, key=_natural):
        members, run = found[name]["members"], found[name]["run"]
        up = [m for m in members if m["status"] == "Running"]
        stacks.append({
            "name": name, "defined": name in defined,
            "instances": len(members), "running": len(up),
            "nodes": sorted({m["node"] for m in members}),
            # What a stop, destroy or relaunch confirms: the service refuses
            # if the tagged set has moved on from this.
            "members": [{"node": m["node"], "name": m["name"], "status": m["status"]}
                        for m in sorted(members, key=lambda m: (_natural(m["name"]),
                                                                 m["node"]))],
            "healthy": sum(1 for m in up if m["health"] == "healthy"
                           and m["app"] in (None, "ok")),
            "unhealthy": sum(1 for m in up if m["health"] in ("unhealthy", "degraded")
                             or m["app"] in ("critical", "warning")),
            "run": run,
        })
    return stacks


def _template_run_summary(run, node, now):
    """A template run (launch, recreate, destroy, exec) cut down to a status line."""
    result = run.get("result") or {}
    instances = [i for i in result.get("instances") or [] if isinstance(i, dict)]
    started = run.get("started_at") or now
    finished = run.get("finished_at")
    failed = [i for i in instances if not i.get("ok")]
    return {
        "template": run.get("template") or "", "node": node,
        "action": run.get("action") or "launch", "count": run.get("count") or 0,
        "nodes": [n for n in run.get("nodes") or [] if isinstance(n, str)],
        "started_at": started, "finished_at": finished,
        "ok": None if finished is None else not run.get("error") and not failed,
        "error": run.get("error") or (failed[0].get("error") if failed else None),
        "done": len(instances) - len(failed), "failed": len(failed),
        "elapsed": max(0.0, (finished or now) - started),
    }


def _templates(instances, runs, now, defined):
    """Every template defined here, or with instances, or a run worth showing."""
    found = {t["name"]: {"template": t, "members": [], "run": None} for t in defined}
    for c in instances:
        if c["template"]:
            found.setdefault(c["template"], {"template": None, "members": [], "run": None})[
                "members"].append(c)
    recent = {}
    for run in runs:
        if run["finished_at"] is None or now - run["finished_at"] <= RUN_LINGER:
            recent.setdefault(run["template"], []).append(run)
    for name, candidates in recent.items():
        # A launch over several nodes leaves a run on the node that
        # coordinated it and one for each node's share, started a moment
        # later. The coordinator's is the one that describes the launch: of
        # the runs around the latest, the one still going, covering most nodes.
        latest = max(r["started_at"] for r in candidates)
        around = [r for r in candidates if latest - r["started_at"] <= 60]
        found.setdefault(name, {"template": None, "members": [], "run": None})["run"] = max(
            around, key=lambda r: (r["finished_at"] is None, len(r["nodes"]), r["started_at"]))
    templates = []
    for name in sorted(found, key=_natural):
        template, members = found[name]["template"], found[name]["members"]
        members = sorted(members, key=lambda m: (_natural(m["name"]), m["node"]))
        templates.append({
            "name": name, "defined": template is not None,
            "image": (template or {}).get("image") or "",
            "description": (template or {}).get("description") or "",
            "instances": len(members),
            "running": sum(1 for m in members if m["status"] == "Running"),
            "nodes": sorted({m["node"] for m in members}),
            "members": [{"node": m["node"], "name": m["name"], "status": m["status"]}
                        for m in members],
            "stale": sum(1 for m in members if "template" in (m["stale"] or [])),
            "stacks": sorted({m["stack"] for m in members if m["stack"]}),
            "run": found[name]["run"],
        })
    return templates


# -- drawing ---------------------------------------------------------------

_color = True

BORDER = "38;5;240"
TITLE = "1;38;5;221"
LEMON = "38;5;221"
DIM = "38;5;245"
FAINT = "38;5;238"
BOLD = "1"
GREEN = "38;5;114"
YELLOW = "38;5;221"
ORANGE = "38;5;215"
RED = "38;5;203"
CYAN = "38;5;116"
SELECTED = "48;5;236"

STATUS = {"Running": ("running", GREEN), "Stopped": ("stopped", DIM),
          "Frozen": ("frozen", CYAN), "Error": ("error", RED)}
HEALTH = {"healthy": GREEN, "degraded": ORANGE, "unhealthy": RED, "starting": CYAN,
          "unknown": DIM, "paused": DIM}
APP = {"ok": ("ok", GREEN), "warning": ("warn", ORANGE), "critical": ("crit", RED),
       "unknown": ("?", DIM)}
STAGE = {"done": ("●", GREEN), "failed": ("✕", RED), "skipped": ("·", DIM),
         "pending": ("○", FAINT)}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPARK = "▁▂▃▄▅▆▇█"
SORTS = ("node", "cpu", "mem", "name")


def _paint(text, code):
    return "\033[%sm%s\033[0m" % (code, text) if code and _color and text else text


def _fit(segments, width, bg=None):
    """Segments of ``(text, style)`` cut or padded to exactly ``width`` columns."""
    out, used = [], 0
    for text, code in segments:
        if used >= width:
            break
        text = str(text)[:width - used]
        out.append(_paint(text, ";".join(c for c in (bg, code) if c)))
        used += len(text)
    out.append(_paint(" " * (width - used), bg))
    return "".join(out)


def _cut(text, width, align="<"):
    text = str(text)
    if len(text) > width:
        text = text[:max(0, width - 1)] + "…" if width > 1 else text[:width]
    return text.rjust(width) if align == ">" else text.ljust(width)


def _box(title, lines, width, height, footer=""):
    """A rounded box of exactly ``height`` lines; ``lines`` are ``(segments, bg)``."""
    inner = width - 2
    head = [("╭─", BORDER)] + title
    used = sum(len(t) for t, _ in head)
    out = [_fit(head + [("─" * max(0, width - used - 1), BORDER), ("╮", BORDER)], width)]
    for segments, bg in lines[:max(0, height - 2)]:
        out.append(_paint("│", BORDER) + _fit(segments, inner, bg) + _paint("│", BORDER))
    while len(out) < height - 1:
        out.append(_paint("│", BORDER) + " " * inner + _paint("│", BORDER))
    tail = " %s " % footer if footer else ""
    rule = "─" * max(0, inner - len(tail) - 1)
    out.append(_fit([("╰" + rule, BORDER), (tail, DIM), ("─╯", BORDER)], width))
    return out[:height]


def _meter(percent, width):
    if percent is None:
        return [("·" * width, FAINT)]
    filled = max(0, min(width, int(round(percent / 100.0 * width))))
    cells = []
    for i in range(filled):
        at = (i + 1) / float(width)
        cells.append(("■", GREEN if at <= 0.6 else YELLOW if at <= 0.85 else RED))
    # Without colour the empty half has to look empty on its own.
    return cells + [(("■" if _color else "·") * (width - filled), FAINT)]


def _level(percent):
    return GREEN if percent < 60 else YELLOW if percent < 85 else RED


def _spark(values, width):
    values = list(values)[-width:]
    cells = [(" " * (width - len(values)), "")]
    for value in values:
        if value is None:
            cells.append((" ", ""))
            continue
        index = int(round(max(0.0, min(100.0, value)) / 100.0 * (len(SPARK) - 1)))
        cells.append((SPARK[index], _level(value) if value > 0 else FAINT))
    return cells


def _size(n):
    if not n:
        return "-"
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return ("%.1f%s" if n < 10 and unit != "B" else "%.0f%s") % (n, unit)
        n /= 1024


def _rate(n):
    if n is None:
        return "-"
    return "0" if n < 1 else _size(n) + "/s"


def _duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60)


class View:
    """What is being looked at: scroll, sort and filters. Survives refreshes."""

    def __init__(self, interval):
        self.interval = interval
        self.sort = "node"
        self.running_only = False
        self.node = None            # None: every node
        self.selected = None        # (node, name), kept across re-sorts
        self.offset = 0
        self.page = 10              # rows the instance table showed last frame
        self.frame = 0
        self.notice = None          # (text, style, until): a line in the header
        self.marked = set()         # (node, name) chosen for a bootstrap
        self.focus = "instances"    # or a panel: "stacks", "templates"
        self.picked = {"stacks": None, "templates": None}   # selected name per panel

    def say(self, text, style=None, seconds=6):
        self.notice = (text, style or LEMON, time.monotonic() + seconds)

    def instances(self, snap):
        rows = [r for r in snap["instances"]
                if (not self.running_only or r["status"] == "Running")
                and (self.node is None or r["node"] == self.node)]
        keys = {
            "node": lambda r: (r["node"], _natural(r["name"])),
            "name": lambda r: (_natural(r["name"]), r["node"]),
            "cpu": lambda r: (-(r["cpu"] or 0), _natural(r["name"])),
            "mem": lambda r: (-(r["memory"] or 0), _natural(r["name"])),
        }
        return sorted(rows, key=keys[self.sort])

    def panel_key(self, name, snap):
        names = [item["name"] for item in (snap or {}).get(self.focus) or []]
        if not names:
            self.focus = "instances"
            return
        picked = self.picked[self.focus]
        index = names.index(picked) if picked in names else 0
        if name in ("up", "down", "pgup", "pgdn", "home", "end"):
            step = {"up": -1, "down": 1, "pgup": -5, "pgdn": 5,
                    "home": -len(names), "end": len(names)}[name]
            index = max(0, min(len(names) - 1, index + step))
        self.picked[self.focus] = names[index]

    def selected_item(self, snap):
        """The stack or template picked in the focused panel, or None."""
        if self.focus not in self.picked:
            return None
        return next((item for item in (snap or {}).get(self.focus) or []
                     if item["name"] == self.picked[self.focus]), None)

    def key(self, name, snap):
        if name == "tab":
            # instances -> stacks -> templates -> instances, past empty panels.
            order = ["instances"] + [p for p in PANELS if (snap or {}).get(p)]
            at = order.index(self.focus) if self.focus in order else 0
            self.focus = order[(at + 1) % len(order)]
            return
        if self.focus in PANELS:
            self.panel_key(name, snap)
            return
        rows = self.instances(snap) if snap else []
        keys = [(r["node"], r["name"]) for r in rows]
        index = keys.index(self.selected) if self.selected in keys else 0
        if name in ("up", "down", "pgup", "pgdn", "home", "end") and keys:
            step = {"up": -1, "down": 1, "pgup": -self.page, "pgdn": self.page,
                    "home": -len(keys), "end": len(keys)}[name]
            self.selected = keys[max(0, min(len(keys) - 1, index + step))]
        elif name == "sort":
            self.sort = SORTS[(SORTS.index(self.sort) + 1) % len(SORTS)]
        elif name == "running":
            self.running_only = not self.running_only
        elif name == "node" and snap:
            names = [None] + [n["name"] for n in snap["nodes"]]
            self.node = names[(names.index(self.node) + 1) % len(names)] \
                if self.node in names else None
        elif name == "mark" and keys:
            key = keys[index]
            self.marked.symmetric_difference_update({key})
            self.selected = keys[min(len(keys) - 1, index + 1)]
        elif name in ("slower", "faster"):
            at = INTERVALS.index(self.interval) if self.interval in INTERVALS else 1
            at = min(len(INTERVALS) - 1, at + 1) if name == "slower" else max(0, at - 1)
            self.interval = INTERVALS[at]


def render(snap, view, width, height, busy=False, error=None, dialog=None, jobs=()):
    """One whole frame, as ``height`` lines of exactly ``width`` columns."""
    view.frame += 1
    if width < 60 or height < 12:
        return [_fit([("lemondx top needs at least 60x12", YELLOW)], width)] + \
            [" " * width] * (height - 1)
    lines = [_header(snap, view, width, busy, error)]
    body = height - 2
    if snap is None:
        box = _box([(" lemondx ", TITLE)],
                   [([("  gathering the first reading from every node…", DIM)], None)],
                   width, body)
        return lines + box + [_footer(view, width, dialog)]

    nodes_h = len(snap["nodes"]) + 2
    panels = [(draw, len(snap[kind]) + 2) for kind, draw in
              (("stacks", _stacks_box), ("templates", _templates_box)) if snap.get(kind)]
    if panels and width >= 140:
        # Side by side: the panels share the right column, as tall as the
        # nodes box or their own rows, whichever is more, up to half the body.
        top_h = min(max(nodes_h, sum(h for _, h in panels)), max(4, body // 2))
        heights = _share(top_h, [h for _, h in panels])
        left = width - max(46, min(72, int(width * 0.38)))
        right = []
        for (draw, _), h in zip(panels, heights):
            right += draw(snap, view, width - left, h)
        top = [a + b for a, b in zip(_nodes_box(snap, view, left, top_h), right)]
    else:
        top = _nodes_box(snap, view, width, min(nodes_h, max(3, body // 3)))
        for draw, h in panels:
            top += draw(snap, view, width, min(h, max(3, body // 4)))
    if jobs:
        top += _jobs_box(snap, view, jobs, width, min(len(jobs) + 2, max(3, body // 4)))
    rest = body - len(top)
    main = dialog.box(width, rest) if dialog else \
        _instances_box(snap, view, width, rest)
    return lines + top + main + [_footer(view, width, dialog, jobs, snap)]


def _share(total, wanted):
    """Split ``total`` rows between boxes wanting ``wanted``, 3 each at least.

    Whatever is left over goes to the last box, so the column is exactly as
    tall as the one beside it.
    """
    heights = [min(w, 3) for w in wanted]
    for i, w in enumerate(wanted):
        heights[i] += max(0, min(w - heights[i], total - sum(heights)))
    heights[-1] += max(0, total - sum(heights))
    return heights


def _header(snap, view, width, busy, error):
    left = [(" lemondx ", "1;38;5;235;48;5;221"), (" top ", TITLE)]
    if snap:
        running = sum(n["running"] for n in snap["nodes"])
        total = sum(n["instances"] for n in snap["nodes"])
        down = [n["name"] for n in snap["nodes"] if n["state"] == "unreachable"]
        left += [("  %s" % snap["node"], BOLD),
                 ("  %d node%s" % (len(snap["nodes"]), "" if len(snap["nodes"]) == 1 else "s"),
                  DIM),
                 ("  %d/%d running" % (running, total), DIM)]
        if snap["stacks"]:
            left.append(("  %d stack%s" % (len(snap["stacks"]),
                                           "" if len(snap["stacks"]) == 1 else "s"), DIM))
        if down:
            left.append(("  ✕ %s down" % ", ".join(down), RED))
        if snap["serve"]["error"]:
            left.append(("  ! health and runs unavailable here: %s" % snap["serve"]["error"],
                         ORANGE))
    if error:
        left.append(("  ! %s" % error, RED))
    if view.notice and view.notice[2] > time.monotonic():
        left.append(("  %s" % view.notice[0], view.notice[1]))
    right = "%s %s " % (SPINNER[view.frame % len(SPINNER)] if busy else " ",
                        time.strftime("%H:%M:%S"))
    space = width - len(right)
    return _fit(left, space) + _paint(right, DIM)


def _footer(view, width, dialog=None, jobs=(), snap=None):
    if dialog is not None:
        keys = dialog.hints()
    else:
        keys = [("q", "quit"), ("↑↓", "select"), ("tab", "panels"), ("space", "mark"),
                ("c", "console"),
                ("b", "bootstrap"), ("s", "sort:%s" % view.sort),
                ("r", "running only" if view.running_only else "all states"),
                ("n", "node:%s" % (view.node or "all")), ("+/-", "every %ds" % view.interval)]
        if any(j["finished"] for j in jobs):
            keys.append(("x", "clear finished"))
        if view.focus in PANELS:
            shortcuts, blocked, label, _ = PANELS[view.focus]
            item = view.selected_item(snap)
            serve_error = ((snap or {}).get("serve") or {}).get("error")
            order = ["instances"] + [p for p in PANELS if (snap or {}).get(p)]
            after = order[(order.index(view.focus) + 1) % len(order)]
            keys = [("q", "quit"), ("tab", after), ("↑↓", "select")]
            for key, action in shortcuts:
                # Dimmed rather than hidden, so the row does not jump about as
                # the selection moves between items in different states.
                off = item is None or blocked(item, action, serve_error)
                keys.append((key, label(item, action) if item else action, bool(off)))
            keys.append(("enter", "menu"))
    segments = [(" ", "")]
    for entry in keys:
        key, label, off = (entry + (False,))[:3]
        segments += [(key, FAINT if off else "1;" + LEMON),
                     (" %s   " % label, FAINT if off else DIM)]
    return _fit(segments, width)


def _nodes_box(snap, view, width, height):
    inner = width - 2
    up = sum(1 for n in snap["nodes"] if n["state"] != "unreachable")
    title = [(" nodes ", TITLE), ("%d/%d up " % (up, len(snap["nodes"])), DIM)]
    name_w = min(18, max(len(n["name"]) for n in snap["nodes"]) + 1)
    # Everything on a row but the two bars and the sparkline: dot and name,
    # count, both meters' labels and percentages, memory figures, gaps. The
    # sparkline is what goes when there is not room for all three, and makes
    # room for the maintenance note rather than having it cut off.
    fixed = name_w + 46 + (13 if any(n["maintenance"] for n in snap["nodes"]) else 0)
    bar = max(5, min(16, (inner - fixed - 12) // 2))
    spark = inner - fixed - 2 * bar
    spark = min(spark, HISTORY) if spark >= 8 else 0
    lines = []
    for node in snap["nodes"][:max(0, height - 2)]:
        dot = {"ok": ("●", GREEN), "maintenance": ("●", YELLOW)}.get(node["state"], ("✕", RED))
        segments = [(" ", ""), dot, (" ", ""),
                    (_cut(node["name"] + ("*" if node["self"] else ""), name_w), BOLD),
                    (" ", "")]
        if node["state"] == "unreachable":
            segments += [("unreachable  ", RED), (node["error"] or "", DIM)]
            lines.append((segments, None))
            continue
        count = "%d/%d up" % (node["running"], node["instances"])
        segments += [(_cut(count, 9), GREEN if node["running"] else DIM), (" ", "")]
        segments += [("cpu ", DIM)] + _meter(node["cpu"], bar) + [
            (" %s" % _cut("-" if node["cpu"] is None else "%d%%" % round(node["cpu"]), 4, ">"),
             DIM if node["cpu"] is None else _level(node["cpu"])), (" ", "")]
        segments += [("mem ", DIM)] + _meter(node["memory"], bar) + [
            (" %s" % _cut("-" if node["memory"] is None else "%d%%" % round(node["memory"]),
                          4, ">"),
             DIM if node["memory"] is None else _level(node["memory"])),
            (" %s" % _cut("%s/%s" % (_size(node["memory_used"]), _size(node["memory_total"])),
                          10, ">"), DIM)]
        if spark:
            segments += [("  ", "")] + _spark(node["history"]["cpu"], spark)
        if node["maintenance"]:
            segments.append(("  maintenance", YELLOW))
        lines.append((segments, None))
    hidden = len(snap["nodes"]) - len(lines)
    return _box(title, lines, width, height, "+%d more" % hidden if hidden > 0 else "")


def _stacks_box(snap, view, width, height):
    inner = width - 2
    stacks = snap["stacks"]
    focused, title, shown = _panel_frame(view, "stacks", stacks, height)
    name_w = min(20, max(len(s["name"]) for s in stacks) + 1)
    lines = []
    for stack in shown:
        run = stack["run"]
        active = run is not None and run["finished_at"] is None
        if active:
            glyph = (SPINNER[view.frame % len(SPINNER)], CYAN)
        elif run is not None and run["ok"] is False:
            glyph = ("✕", RED)
        elif not stack["running"]:
            glyph = ("○", DIM)
        elif stack["unhealthy"]:
            glyph = ("●", RED)
        elif stack["running"] < stack["instances"]:
            glyph = ("●", ORANGE)
        else:
            glyph = ("●", GREEN)
        count = "%d/%d up" % (stack["running"], stack["instances"]) if stack["instances"] \
            else "-"
        segments = [(" ", ""), glyph, (" ", ""), (_cut(stack["name"], name_w), BOLD), (" ", ""),
                    (_cut(count, 9), GREEN if stack["running"] == stack["instances"]
                     and stack["instances"] else ORANGE if stack["running"] else DIM),
                    (" ", "")]
        if run is not None:
            segments.append((run["action"] + " ", DIM))
            for state in run["stages"]:
                segments.append(("◐", YELLOW) if state == "running" else STAGE[state])
            if active:
                note = run["active"] or ("cancelling" if run["cancelling"] else "starting")
                segments += [(" %s" % note, CYAN if not run["cancelling"] else ORANGE),
                             (" %s" % _duration(run["elapsed"]), DIM)]
            elif run["ok"]:
                segments += [(" done", GREEN), (" in %s" % _duration(run["elapsed"]), DIM)]
            else:
                segments.append((" %s" % ("cancelled" if run["cancelled"]
                                          else run["error"] or "failed"), RED))
        else:
            if stack["unhealthy"]:
                segments.append(("! %d unhealthy" % stack["unhealthy"], RED))
            elif stack["healthy"]:
                segments.append(("✓ %d healthy" % stack["healthy"], GREEN))
            if stack["nodes"] and len(snap["nodes"]) > 1:
                segments.append(("  on %s" % ", ".join(stack["nodes"]), DIM))
            if not stack["instances"] and not stack["run"]:
                segments.append(("not running", FAINT))
        lines.append((segments, SELECTED if focused and stack["name"] == view.picked["stacks"]
                       else None))
    return _box(title, lines, inner + 2, height, _panel_footer(stacks, shown))


def _panel_frame(view, kind, items, height):
    """``(focused, title, visible items)`` for a scrolling side panel."""
    focused = view.focus == kind
    names = [item["name"] for item in items]
    if view.picked[kind] not in names:
        view.picked[kind] = names[0]
    page = max(1, height - 2)
    index = names.index(view.picked[kind])
    start = max(0, min(index - page // 2, len(items) - page)) if len(items) > page else 0
    title = [("▸ %s " % kind if focused else " %s " % kind,
              "1;38;5;235;48;5;221" if focused else TITLE), (" %d " % len(items), DIM)]
    return focused, title, items[start:start + page]


def _panel_footer(items, shown):
    if len(shown) >= len(items):
        return ""
    first = items.index(shown[0]) + 1
    return "%d-%d of %d" % (first, first + len(shown) - 1, len(items))


def _templates_box(snap, view, width, height):
    templates = snap["templates"]
    focused, title, shown = _panel_frame(view, "templates", templates, height)
    name_w = min(22, max(len(t["name"]) for t in templates) + 1)
    lines = []
    for t in shown:
        run = t["run"]
        active = run is not None and run["finished_at"] is None
        if active:
            glyph = (SPINNER[view.frame % len(SPINNER)], CYAN)
        elif run is not None and run["ok"] is False:
            glyph = ("✕", RED)
        elif not t["running"]:
            glyph = ("○", DIM)
        elif t["running"] < t["instances"]:
            glyph = ("●", ORANGE)
        else:
            glyph = ("●", GREEN)
        count = "%d/%d up" % (t["running"], t["instances"]) if t["instances"] else "-"
        segments = [(" ", ""), glyph, (" ", ""), (_cut(t["name"], name_w), BOLD), (" ", ""),
                    (_cut(count, 9), GREEN if t["running"] == t["instances"] and t["instances"]
                     else ORANGE if t["running"] else DIM), (" ", "")]
        if active:
            segments += [("%s " % run["action"], DIM),
                         ("%d on %s" % (run["count"], run["node"]) if run["count"]
                          else "on %s" % run["node"], CYAN),
                         (" %s" % _duration(run["elapsed"]), DIM)]
        elif run is not None and run["ok"]:
            segments += [("%s " % run["action"], DIM), ("done", GREEN),
                         (" in %s" % _duration(run["elapsed"]), DIM)]
        elif run is not None:
            segments += [("%s " % run["action"], DIM),
                         (run["error"] or "%d failed" % run["failed"], RED)]
        else:
            segments.append((t["image"] or "not defined here", FAINT if t["image"] else ORANGE))
            if t["stale"]:
                segments.append(("  %d stale" % t["stale"], ORANGE))
            if t["stacks"]:
                segments.append(("  in %s" % ", ".join(t["stacks"]), DIM))
        lines.append((segments, SELECTED if focused and t["name"] == view.picked["templates"]
                       else None))
    return _box(title, lines, width, height, _panel_footer(templates, shown))


# key, header, alignment, drop order (higher goes first when narrow), cap
COLUMNS = (
    ("node", "NODE", "<", 1, 16), ("name", "NAME", "<", 0, 28),
    ("state", "STATE", "<", 0, 8), ("health", "HEALTH", "<", 1, 9),
    ("app", "APP", "<", 5, 4), ("cpu", "CPU%", ">", 1, 6), ("mem", "MEM", ">", 2, 6),
    ("rx", "NET↓", ">", 6, 8), ("tx", "NET↑", ">", 6, 8), ("proc", "PROC", ">", 7, 5),
    ("ipv4", "IPV4", "<", 3, 18), ("template", "TEMPLATE", "<", 4, 16),
    ("stack", "STACK", "<", 4, 14),
)


def _cells(row):
    status = STATUS.get(row["status"], (row["status"].lower(), YELLOW))
    cpu = row["cpu"]
    app = APP.get(row["app"], ("-", FAINT)) if row["app"] else ("-", FAINT)
    ipv4 = row["ipv4"][0] + (" +%d" % (len(row["ipv4"]) - 1) if len(row["ipv4"]) > 1 else "") \
        if row["ipv4"] else "-"
    return {
        "node": (row["node"], DIM),
        "name": (row["name"] + (" (vm)" if row["type"] == "virtual-machine" else ""), BOLD),
        "state": status,
        "health": (row["health"] or "-", HEALTH.get(row["health"], FAINT)),
        "app": app,
        "cpu": ("-" if cpu is None else "%.1f" % cpu,
                FAINT if cpu is None else GREEN if cpu < 50 else YELLOW if cpu < 100 else RED),
        "mem": (_size(row["memory"]), "" if row["memory"] else FAINT),
        "rx": (_rate(row["rx_rate"]), DIM), "tx": (_rate(row["tx_rate"]), DIM),
        "proc": (str(row["processes"] or "-"), DIM),
        "ipv4": (ipv4, CYAN if row["ipv4"] else FAINT),
        "template": (row["template"] or "-", ORANGE if row["stale"] else
                     "" if row["template"] else FAINT),
        "stack": (row["stack"] or "-", LEMON if row["stack"] else FAINT),
    }


def _instances_box(snap, view, width, height):
    inner = width - 2
    rows = view.instances(snap)
    title = [(" instances ", TITLE), ("%d " % len(rows), DIM)]
    if view.marked:
        title.append(("· %d marked " % len(view.marked), LEMON))
    if view.sort != "node":
        title.append(("· by %s " % view.sort, DIM))
    if view.running_only:
        title.append(("· running ", DIM))
    if view.node:
        title.append(("· on %s " % view.node, DIM))

    cells = [_cells(r) for r in rows]
    columns = [c for c in COLUMNS if c[0] != "node" or len(snap["nodes"]) > 1]
    widths = {key: min(cap, max([len(header)] + [len(c[key][0]) for c in cells]))
              for key, header, _, _, cap in columns}
    while len(columns) > 3 and \
            sum(widths[c[0]] for c in columns) + 2 * len(columns) > inner:
        columns.remove(max(columns, key=lambda c: (c[3], columns.index(c))))

    lines = [([(" ", "")] + [(_cut(header, widths[key], align) + "  ", "1;38;5;250")
                             for key, header, align, _, _ in columns], None)]
    page = max(1, height - 3)
    view.page = page
    keys = [(r["node"], r["name"]) for r in rows]
    if view.selected not in keys:
        view.selected = keys[0] if keys else None
    index = keys.index(view.selected) if keys else 0
    if index < view.offset:
        view.offset = index
    elif index >= view.offset + page:
        view.offset = index - page + 1
    view.offset = max(0, min(view.offset, max(0, len(rows) - page)))

    for i in range(view.offset, min(len(rows), view.offset + page)):
        segments = [("◆", LEMON) if keys[i] in view.marked else (" ", "")]
        for key, _, align, _, _ in columns:
            text, code = cells[i][key]
            segments.append((_cut(text, widths[key], align) + "  ", code))
        lines.append((segments, SELECTED if keys[i] == view.selected
                       and view.focus == "instances" else None))
    if not rows:
        lines.append(([("  nothing to show", DIM)], None))
    footer = ""
    if len(rows) > page:
        footer = "%d-%d of %d" % (view.offset + 1, min(len(rows), view.offset + page), len(rows))
    return _box(title, lines, width, height, footer)


# -- consoles ----------------------------------------------------------------


class _LocalConsole:
    """This node's daemon session, as ``ContainerService.open_terminal()`` gives it."""

    def __init__(self, session):
        self.session = session

    def read(self):
        return self.session.read()

    def write(self, data):
        self.session.write(data)

    def resize(self, cols, rows):
        self.session.resize(cols, rows)

    def close(self):
        self.session.close()


class _RemoteConsole:
    """A peer's terminal endpoint: guest bytes as binary, control as JSON text."""

    def __init__(self, ws):
        self.ws = ws

    def read(self):
        while True:
            message = self.ws.recv()
            if message is None:
                return None
            opcode, payload = message
            # Text is the server's control channel ({"exit": n} for a shell);
            # a console has nothing on it worth showing.
            if opcode == BINARY and payload:
                return payload

    def write(self, data):
        self.ws.send(data)

    def resize(self, cols, rows):
        self.ws.send_text(json.dumps({"resize": {"cols": cols, "rows": rows}}))

    def close(self):
        self.ws.close()


def open_console(cluster, node, name, cols, rows):
    """Attach to an instance's console wherever it lives."""
    if node == cluster.local_name():
        return _LocalConsole(cluster.service.open_terminal(name, "console",
                                                           cols=cols, rows=rows))
    client = cluster.client(node, timeout=PEER_TIMEOUT)
    return _RemoteConsole(client.open_websocket(
        "/api/containers/%s/console" % urllib.parse.quote(name, safe=""),
        {"cols": cols, "rows": rows}))


def _attached(fd, guest, resized):
    """Hand the terminal to ``guest`` until Ctrl-] or the far end hangs up.

    Raw mode, so every key -- Ctrl-C included -- is the guest's. Output is
    pumped on a thread because the guest talks whenever it likes; input is
    read here, where a resize can be noticed between keystrokes.
    """
    import tty
    tty.setraw(fd)
    out = sys.stdout.fileno()
    ended = threading.Event()
    lost = []

    def pump():
        try:
            while True:
                chunk = guest.read()
                if chunk is None:
                    break
                os.write(out, chunk)
        except (WebSocketError, OSError, LXDError) as exc:
            lost.append(str(exc))
        finally:
            ended.set()

    threading.Thread(target=pump, name="lemondx-top-console", daemon=True).start()
    try:
        while not ended.is_set():
            if resized.is_set():
                resized.clear()
                size = shutil.get_terminal_size((80, 24))
                guest.resize(size.columns, size.lines)
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            data = os.read(fd, 4096)
            if DETACH in data:
                if data.index(DETACH):
                    guest.write(data[:data.index(DETACH)])
                return None
            guest.write(data)
        return "the console closed%s" % (": %s" % lost[0] if lost else "")
    except (WebSocketError, OSError) as exc:
        return "the console connection was lost: %s" % exc
    finally:
        guest.close()


# -- bootstrap -------------------------------------------------------------


class BootstrapDialog:
    """Pick a profile or modules, fill in their parameters, and say go.

    Two stages in one box: the pick list (profiles, then modules to tick), and
    the form (one row per parameter, the SSH keys when a module installs them,
    and the run row). Lists and values are this node's: profiles and modules
    are kept level across the cluster, and a module missing on a target is
    that target's run failing with the reason, like the web UI's picker.
    """

    def __init__(self, service, targets):
        self.targets = targets
        self.profiles = service.list_bootstrap_profiles()
        self.modules = service.list_modules()
        self.by_id = {m["id"]: m for m in self.modules}
        self.host_keys = [k["line"] for k in service.list_ssh_keys()]
        self.stage = "pick"
        self.cursor = 0
        self.ticked = []
        self.profile = None
        self.chosen = []
        self.fields = []
        self.key_options = []
        self.key_choice = 0
        self.editing = None         # index into fields while typing a value
        self.buffer = ""
        self.problem = None

    # -- the pick list ---------------------------------------------------

    def _pick_rows(self):
        return [("profile", p) for p in self.profiles] + [("module", m) for m in self.modules]

    def _choose(self, module_ids, profile):
        known = [i for i in module_ids if i in self.by_id]
        self.chosen = sorted(known, key=lambda i: (self.by_id[i]["order"], i)) + \
            [i for i in module_ids if i not in self.by_id]
        self.profile = profile
        given = (profile or {}).get("params") or {}
        self.fields, seen = [], set()
        for module_id in known:
            for param in self.by_id[module_id]["params"]:
                if param["name"] in seen:
                    continue
                seen.add(param["name"])
                if param["name"] in given:
                    value = given[param["name"]]
                elif param.get("secret"):
                    value = ""
                elif param.get("multiline"):
                    value = None        # left to each node's own setting
                else:
                    value = param.get("value", param["default"])
                self.fields.append({"name": param["name"], "secret": bool(param.get("secret")),
                                    "multiline": bool(param.get("multiline")),
                                    "value": value, "module": module_id})
        self.key_options = []
        if any(self.by_id[i]["uses_ssh_keys"] for i in known):
            if profile and profile["ssh_keys"]:
                self.key_options.append(("the profile's (%d)" % len(profile["ssh_keys"]),
                                         profile["ssh_keys"]))
            if self.host_keys:
                self.key_options.append(("every key in ~/.ssh (%d)" % len(self.host_keys),
                                         self.host_keys))
            self.key_options.append(("none", []))
        self.key_choice = 0
        self.stage, self.cursor, self.problem = "form", 0, None

    # -- the form --------------------------------------------------------

    def _form_rows(self):
        rows = [("field", f) for f in self.fields]
        if self.key_options:
            rows.append(("keys", None))
        return rows + [("run", None)]

    def request(self):
        return {
            "modules": self.chosen,
            "params": {f["name"]: f["value"] for f in self.fields if f["value"] is not None},
            "ssh_keys": self.key_options[self.key_choice][1] if self.key_options else [],
        }

    def _missing(self):
        return [f["name"] for f in self.fields if f["secret"] and not f["value"]]

    # -- input -----------------------------------------------------------

    def handle(self, name):
        """A named key. Returns "close", "run" or None."""
        rows = self._pick_rows() if self.stage == "pick" else self._form_rows()
        if name == "quit":
            if self.stage == "form":
                self.stage, self.cursor, self.problem = "pick", 0, None
                return None
            return "close"
        if name in ("up", "down", "pgup", "pgdn", "home", "end"):
            step = {"up": -1, "down": 1, "pgup": -10, "pgdn": 10,
                    "home": -len(rows), "end": len(rows)}[name]
            self.cursor = max(0, min(len(rows) - 1, self.cursor + step))
            return None
        if not rows:
            return None
        kind, item = rows[self.cursor]
        if self.stage == "pick":
            if name == "mark" and kind == "module":
                if item["id"] in self.ticked:
                    self.ticked.remove(item["id"])
                else:
                    self.ticked.append(item["id"])
                self.cursor = min(len(rows) - 1, self.cursor + 1)
            elif name == "enter" and kind == "profile":
                self._choose(item["modules"], item)
            elif name == "enter":
                self._choose(self.ticked or [item["id"]], None)
            return None
        self.problem = None
        if name not in ("enter", "mark"):
            return None
        if kind == "field":
            if item["multiline"]:
                self.problem = ("%s is multi-line: set it in the web UI or with "
                                "`lemondx module-set`" % item["name"])
                return None
            self.editing = self.fields.index(item)
            self.buffer = "" if item["secret"] else (item["value"] or "")
        elif kind == "keys":
            self.key_choice = (self.key_choice + 1) % len(self.key_options)
        elif kind == "run" and name == "enter":
            missing = self._missing()
            if missing:
                self.problem = "type a value for %s first" % ", ".join(missing)
                return None
            return "run"
        return None

    def type(self, data):
        """Raw bytes while a value is being typed."""
        self.buffer, done = _edit(self.buffer, data)
        if done == "keep":
            self.fields[self.editing]["value"] = self.buffer
        if done:
            self.editing = None

    def box(self, width, height):
        return _dialog_box(self, width, height)

    def hints(self):
        if self.editing is not None:
            return [("enter", "keep"), ("esc", "undo"), ("ctrl-u", "clear")]
        if self.stage == "pick":
            return [("enter", "choose"), ("space", "tick modules"), ("esc", "cancel")]
        return [("enter", "edit / run"), ("esc", "back")]


def _edit(buffer, data):
    """Apply raw keystrokes to a value being typed: ``(buffer, None|"keep"|"undo")``."""
    if data.startswith(b"\x1b"):
        # Esc alone leaves the value as it was; arrows and the like do nothing.
        return buffer, "undo" if len(data) == 1 else None
    # In order: a paste can end with the Enter that commits it.
    for ch in data.decode("utf-8", "ignore"):
        if ch in "\r\n":
            return buffer, "keep"
        if ch in "\x7f\x08":
            buffer = buffer[:-1]
        elif ch == "\x15":                  # Ctrl-U
            buffer = ""
        elif ch.isprintable():
            buffer += ch
    return buffer, None


# The panels' own keys while one has focus; Enter opens the same actions as a
# menu. Letters that mean something else over the instances (s sort, r running
# only, x clear, c console) mean these there, which the footer says.
STACK_SHORTCUTS = (("u", "start"), ("s", "stop"), ("r", "restart"), ("l", "relaunch"),
                   ("d", "destroy"), ("x", "cancel"))
TEMPLATE_SHORTCUTS = (("l", "launch"), ("u", "start"), ("s", "stop"), ("r", "restart"),
                      ("c", "recreate"), ("d", "destroy"))
NEEDS_SERVE = "needs `lemondx serve` on this node, so the run outlives top"


def _state_blocked(item, action):
    """Why start, stop or restart cannot apply to ``item``'s instances, or None."""
    if not item["members"]:
        return "no instances"
    if action == "start" and item["running"] == item["instances"]:
        return "all running"
    if action == "stop" and not item["running"]:
        return "none running"
    return None


def _stack_blocked(stack, action, serve_error):
    """None if ``action`` can be done to ``stack`` now, else why not."""
    run = stack["run"]
    active = run is not None and run["finished_at"] is None
    if action == "cancel":
        if not active:
            return "no run in progress"
        return "already cancelling" if run["cancelling"] else None
    if active:
        return "a %s is running; wait for it or cancel it" % run["action"]
    if action in ("relaunch", "destroy") and serve_error:
        return NEEDS_SERVE
    if action == "relaunch":
        return None if stack["defined"] else "not defined on this node"
    return _state_blocked(stack, action)


def _stack_label(stack, action):
    return "launch" if action == "relaunch" and not stack["members"] else action


def _template_blocked(template, action, serve_error):
    """None if ``action`` can be done with ``template`` now, else why not."""
    run = template["run"]
    if run is not None and run["finished_at"] is None:
        # One run per template, whichever node started it.
        return "a %s is running; wait for it to finish" % run["action"]
    if action in ("launch", "recreate", "destroy") and serve_error:
        return NEEDS_SERVE
    if action in ("launch", "recreate") and not template["defined"]:
        return "not defined on this node"
    if action == "launch":
        return None
    return _state_blocked(template, action)


def _template_label(template, action):
    return action


class _ActionDialog:
    """Pick an action on one stack or template, give it what it needs, confirm.

    ``menu`` lists the actions, each with why it is not available when it is
    not; ``inputs`` asks for what an action needs (secrets, a count, where);
    ``confirm`` names every instance it will touch, which is also the list the
    service checks against before it acts. Subclasses say what the actions
    are and how the item and its confirmation read.
    """

    KIND = ""
    ACTIONS = ()
    SHORTCUTS = ()
    IMMEDIATE = ()          # actions that need no confirmation
    WARNINGS = {"destroy": "their filesystems and snapshots are deleted",
                "stop": "anything they are serving goes down",
                "restart": "anything they are serving drops for a moment"}

    def __init__(self, item, field_sets, serve_error):
        self.item = item
        self.field_sets = field_sets        # action -> [field], see _field()
        self.fields = []
        self.serve_error = serve_error      # why there is no local serve, or None
        self.stage = "menu"
        self.cursor = 0
        self.action = None
        self.editing = None
        self.buffer = ""
        self.problem = None
        self.direct = False

    def _why_not(self, action):
        raise NotImplementedError

    def _label(self, action):
        return action

    def begin(self, action, direct=False):
        """Take ``action`` from the menu or a shortcut. "run", or None to go on.

        ``direct`` is a shortcut: Esc then closes rather than falling back to
        a menu that was never opened.
        """
        self.direct = direct
        why = self._why_not(action)
        if why:
            self.problem = why
            return None
        self.action, self.problem = action, None
        self.fields = [dict(f) for f in self.field_sets.get(action) or []]
        if self.fields:
            self.stage, self.cursor = "inputs", 0
        elif action in self.IMMEDIATE:
            return "run"
        else:
            self.stage = "confirm"
        return None

    def _back(self):
        if self.direct:
            return "close"
        self.stage, self.problem = "menu", None
        self.cursor = [a for a, _ in self.ACTIONS].index(self.action)
        return None

    def handle(self, name):
        if self.stage == "menu":
            if name == "quit":
                return "close"
            if name in ("up", "down", "home", "end"):
                step = {"up": -1, "down": 1, "home": -9, "end": 9}[name]
                self.cursor = max(0, min(len(self.ACTIONS) - 1, self.cursor + step))
            elif name == "enter":
                return self.begin(self.ACTIONS[self.cursor][0])
            elif name.startswith(self.KIND + ":"):
                return self.begin(name.split(":", 1)[1])
            return None
        if self.stage == "inputs":
            rows = len(self.fields) + 1
            if name == "quit":
                return self._back()
            if name in ("up", "down"):
                self.cursor = max(0, min(rows - 1, self.cursor + (1 if name == "down" else -1)))
            elif name == "enter" and self.cursor < len(self.fields):
                field = self.fields[self.cursor]
                if field["kind"] == "choice":
                    field["choice"] = (field["choice"] + 1) % len(field["options"])
                else:
                    self.editing = self.cursor
                    self.buffer = "" if field["kind"] == "secret" else field["value"]
            elif name == "enter":
                self.problem = self._invalid()
                if self.problem is None:
                    self.stage = "confirm"
            return None
        # confirm
        if name == "yes":
            return "run"
        if name == "quit":
            if self.fields:
                self.stage, self.problem = "inputs", None
                return None
            return self._back()
        return None

    def _invalid(self):
        missing = [f["name"] for f in self.fields if f["kind"] != "choice" and not f["value"]]
        if missing:
            return "type a value for %s first" % ", ".join(missing)
        for field in self.fields:
            if field.get("integer") and not (field["value"].isdigit()
                                             and int(field["value"]) >= 1):
                return "%s must be a whole number, 1 or more" % field["name"]
        return None

    def type(self, data):
        self.buffer, done = _edit(self.buffer, data)
        if done == "keep":
            self.fields[self.editing]["value"] = self.buffer
            self.problem = None
        if done:
            self.editing = None

    def value(self, name):
        field = next((f for f in self.fields if f["name"] == name), None)
        if field is None:
            return None
        return field["options"][field["choice"]][1] if field["kind"] == "choice" \
            else field["value"]

    def secrets(self):
        """What goes to the launch as params: every secret or input typed in."""
        return {f["name"]: f["value"] for f in self.fields
                if f["kind"] == "secret" and f["value"]}

    def hints(self):
        if self.editing is not None:
            return [("enter", "keep"), ("esc", "undo"), ("ctrl-u", "clear")]
        if self.stage == "confirm":
            return [("y", self._label(self.action)), ("esc", "back")]
        if self.stage == "inputs":
            return [("enter", "edit / continue"), ("esc", "back")]
        return [("enter", "choose"), (" ".join(k for k, _ in self.SHORTCUTS),
                                      "or press the action's key"), ("esc", "close")]

    # -- drawing ---------------------------------------------------------

    def _summary(self):
        return []

    def _heading(self, verb, count):
        return "%s these %d:" % (verb, count)

    def _confirm_extra(self):
        return []

    def box(self, width, height):
        item = self.item
        title = [(" %s " % self.KIND, TITLE), ("%s " % item["name"], BOLD)]
        body = [(segments, None) for segments in self._summary()]
        body.append(([("", "")], None))
        if self.stage == "menu":
            letters = dict((a, k) for k, a in self.SHORTCUTS)
            for index, (action, what) in enumerate(self.ACTIONS):
                why = self._why_not(action)
                body.append(([("  %s " % ("▶" if index == self.cursor else " "), LEMON),
                              ("%s " % letters[action], FAINT if why else "1;" + LEMON),
                              (_cut(self._label(action), 10), FAINT if why else BOLD),
                              (what, FAINT if why else DIM),
                              ("   %s" % why if why else "", FAINT)],
                             SELECTED if index == self.cursor else None))
        elif self.stage == "inputs":
            body.append(([("  %s needs these; secrets are sent once and never kept"
                           % self._label(self.action), DIM)], None))
            name_w = max(len(f["name"]) for f in self.fields) + 2
            for index, field in enumerate(self.fields):
                if self.editing == index:
                    shown = self.buffer if field["kind"] == "text" else "•" * len(self.buffer)
                    value = (shown + "█", LEMON)
                elif field["kind"] == "choice":
                    value = (field["options"][field["choice"]][0], CYAN)
                elif not field["value"]:
                    value = ("required", RED)
                elif field["kind"] == "secret":
                    value = ("••••••", "")
                else:
                    value = (field["value"], "")
                note = "enter changes" if field["kind"] == "choice" else field.get("why", "")
                body.append(([("    ", ""), (_cut(field["name"], name_w), BOLD), value,
                              ("   %s" % note, FAINT)],
                             SELECTED if index == self.cursor else None))
            body.append(([("  ▶ ", LEMON), ("continue", "1;" + LEMON)],
                         SELECTED if self.cursor == len(self.fields) else None))
        else:
            verb = self._label(self.action)
            members = item["members"] if self.action not in ("launch",) else []
            if members:
                multi = len({m["node"] for m in members}) > 1
                body.append(([("  " + self._heading(verb, len(members)), BOLD)], None))
                for m in members:
                    body.append(([("    %s" % m["name"], ""),
                                  (" on %s" % m["node"] if multi else "", DIM),
                                  ("  %s" % m["status"].lower(), DIM)], None))
            body += [(segments, None) for segments in self._confirm_extra()]
            body.append(([("", "")], None))
            warning = self.WARNINGS.get(self.action, "") if members else ""
            body.append(([("  y ", "1;" + LEMON), ("to %s" % verb, BOLD),
                          ("  —  %s" % warning if warning else "", ORANGE)], None))
        if self.problem:
            body.append(([("", "")], None))
            body.append(([("  ! %s" % self.problem, ORANGE)], None))
        return _box(title, body, width, height)


def _run_line(run):
    if run is None:
        return []
    state = "running" if run["finished_at"] is None else "done" if run["ok"] \
        else "cancelled" if run.get("cancelled") else "failed"
    return [[("  last run: %s on %s, %s" % (run["action"], run["node"], state), DIM)]]


class StackDialog(_ActionDialog):
    KIND = "stack"
    SHORTCUTS = STACK_SHORTCUTS
    IMMEDIATE = ("start", "cancel")
    ACTIONS = (("start", "start every instance"), ("stop", "stop every instance"),
               ("restart", "restart every instance"),
               ("relaunch", "destroy the instances and launch the stack again"),
               ("destroy", "stop and delete every instance"),
               ("cancel", "stop the run starting anything new"))
    WARNINGS = dict(_ActionDialog.WARNINGS,
                    relaunch="their filesystems and snapshots are deleted first")

    @property
    def stack(self):
        return self.item

    def _why_not(self, action):
        return _stack_blocked(self.item, action, self.serve_error)

    def _label(self, action):
        return _stack_label(self.item, action)

    def _summary(self):
        stack = self.item
        return [[("  %d instance%s, %d running" % (
            stack["instances"], "" if stack["instances"] == 1 else "s", stack["running"]),
            DIM), ("  on %s" % ", ".join(stack["nodes"]) if stack["nodes"] else "", DIM)]] \
            + _run_line(stack["run"])

    def _heading(self, verb, count):
        if self.action == "relaunch":
            return "destroy, then launch again — these %d:" % count
        return super()._heading(verb, count)

    def _confirm_extra(self):
        return [] if self.item["members"] else [[("  launch the stack", BOLD)]]


class TemplateDialog(_ActionDialog):
    KIND = "template"
    SHORTCUTS = TEMPLATE_SHORTCUTS
    IMMEDIATE = ("start",)
    ACTIONS = (("launch", "create new instances from it"),
               ("start", "start every instance"), ("stop", "stop every instance"),
               ("restart", "restart every instance"),
               ("recreate", "delete each instance and create it again from the template"),
               ("destroy", "stop and delete every instance"))
    WARNINGS = dict(_ActionDialog.WARNINGS,
                    recreate="their filesystems and snapshots are deleted first")

    def _why_not(self, action):
        return _template_blocked(self.item, action, self.serve_error)

    def _summary(self):
        t = self.item
        lines = [[("  %s" % (t["image"] or "not defined on this node"), DIM),
                  ("  %s" % t["description"] if t["description"] else "", DIM)],
                 [("  %d instance%s, %d running" % (
                     t["instances"], "" if t["instances"] == 1 else "s", t["running"]), DIM),
                  ("  on %s" % ", ".join(t["nodes"]) if t["nodes"] else "", DIM),
                  ("  %d stale" % t["stale"] if t["stale"] else "", ORANGE)]]
        return lines + _run_line(t["run"])

    def _confirm_extra(self):
        t = self.item
        if self.action == "launch":
            count = int(self.value("count") or 1)
            where = next((f["options"][f["choice"]][0] for f in self.fields
                          if f["name"] == "where"), "this node")
            return [[("  launch %d from %s on %s" % (count, t["name"], where), BOLD)]]
        if t["stacks"] and self.action in ("recreate", "destroy"):
            what = "they stay part of it" if self.action == "recreate" \
                else "they drop out of it, and it will count fewer"
            return [[("  ", ""), ("part of stack %s: %s" % (", ".join(t["stacks"]), what),
                                  ORANGE)]]
        return []


# Per panel: its shortcuts, what blocks an action, how an action is named, and
# the dialog that carries it out.
PANELS = {
    "stacks": (STACK_SHORTCUTS, _stack_blocked, _stack_label, StackDialog),
    "templates": (TEMPLATE_SHORTCUTS, _template_blocked, _template_label, TemplateDialog),
}


def _stack_fields(cluster, stack_name):
    """What a relaunch must be given: the stack's inputs and unset module secrets.

    The CLI's ``stack-launch`` asks for the same two things; a secret a step
    already sets (to ``{{params.X}}``) is covered by that input instead.
    """
    from .stacks import StackService
    service = cluster.service
    stack = next((st for st in StackService(cluster).list_stacks()
                  if st["name"] == stack_name), None)
    if stack is None:
        raise ServiceError("Stack '%s' is not defined on this node." % stack_name, 404)
    inputs = list(stack["inputs"])
    fields = [{"name": n, "kind": "secret", "value": "", "why": "stack input"}
              for n in inputs]
    templates = {t["name"]: t for t in service.list_templates()}
    modules = {m["id"]: m for m in service.list_modules()}
    seen = set(inputs)
    for stage in stack["stages"]:
        for step in stage["steps"]:
            if step["type"] != "launch" or step["template"] not in templates:
                continue
            for module_id in templates[step["template"]]["bootstrap"]["modules"]:
                for param in (modules.get(module_id) or {}).get("params") or []:
                    name = param["name"]
                    if param.get("secret") and name not in seen \
                            and name not in step.get("params", {}):
                        seen.add(name)
                        fields.append({"name": name, "kind": "secret", "value": "",
                                       "why": "secret of %s" % module_id})
    return fields


def _template_fields(cluster, template_name):
    """``{action: [field]}`` for a template: what a launch and a recreate need.

    Both need every secret its modules declare, since a template never stores
    one; a launch also takes a count and, in a cluster, where to put them --
    this node, a node group, or every node.
    """
    service = cluster.service
    template = next((t for t in service.list_templates() if t["name"] == template_name), None)
    if template is None:
        return {}
    modules = {m["id"]: m for m in service.list_modules()}
    secrets, seen = [], set()
    for module_id in template["bootstrap"]["modules"]:
        for param in (modules.get(module_id) or {}).get("params") or []:
            if param.get("secret") and param["name"] not in seen:
                seen.add(param["name"])
                secrets.append({"name": param["name"], "kind": "secret", "value": "",
                                "why": "secret of %s" % module_id})
    launch = [{"name": "count", "kind": "text", "value": "1", "integer": True,
               "why": "instances to create"}]
    if cluster.in_cluster():
        options = [("this node", {}), ("every node", {"nodes": cluster.all_nodes()})]
        options += [("group %s (%s)" % (g["name"], ", ".join(g["members"]) or "empty"),
                     {"groups": [g["name"]]}) for g in cluster.list_groups()]
        launch.append({"name": "where", "kind": "choice", "options": options, "choice": 0})
    return {"launch": launch + secrets, "recreate": [dict(f) for f in secrets]}


def _dialog_box(dialog, width, height):
    inner = width - 2
    targets = dialog.targets
    nodes = {n for n, _ in targets}
    where = ", ".join(name if len(nodes) == 1 else "%s on %s" % (name, node)
                      for node, name in targets)
    title = [(" bootstrap ", TITLE), ("%s " % _cut(where, max(10, inner // 2)), DIM)]
    lines = []
    if dialog.stage == "pick":
        rows = dialog._pick_rows()
        width_id = max([len(p["name"]) for p in dialog.profiles] +
                       [len(m["id"]) for m in dialog.modules] + [8])
        body = []
        for index, (kind, item) in enumerate(rows):
            if kind == "profile" and index == 0:
                body.append(([("  PROFILES", "1;38;5;250")], None, None))
            if kind == "module" and (index == 0 or rows[index - 1][0] != "module"):
                body.append(([("  MODULES", "1;38;5;250"),
                              ("  tick with space, enter runs the ticked (or this one)", DIM)],
                             None, None))
            bg = SELECTED if index == dialog.cursor else None
            if kind == "profile":
                segments = [("    ", ""), (_cut(item["name"], width_id + 2), BOLD),
                            (" → ".join(item["modules"]) or "-", ""),
                            ("  %d key(s)" % len(item["ssh_keys"]) if item["ssh_keys"] else "",
                             DIM),
                            ("  %s" % item["description"] if item["description"] else "", DIM)]
            else:
                ticked = item["id"] in dialog.ticked
                segments = [("  ", ""), ("[x] " if ticked else "[ ] ", LEMON if ticked else DIM),
                            (_cut(item["id"], width_id + 2), BOLD),
                            (item["description"] or item["name"], DIM)]
            body.append((segments, bg, index))
        if not rows:
            body.append(([("  no profiles or modules on this node", DIM)], None, None))
    else:
        body = []
        body.append(([("  modules  ", DIM), (" → ".join(dialog.chosen), BOLD)] +
                     ([("   profile %s" % dialog.profile["name"], DIM)] if dialog.profile else []),
                     None, None))
        missing = [i for i in dialog.chosen if i not in dialog.by_id]
        if missing:
            body.append(([("  ! not on this node: %s -- runs where it is installed, "
                           "fails elsewhere" % ", ".join(missing), ORANGE)], None, None))
        body.append(([("", "")], None, None))
        name_w = max([len(f["name"]) for f in dialog.fields] + [8]) + 2
        for index, (kind, item) in enumerate(dialog._form_rows()):
            bg = SELECTED if index == dialog.cursor else None
            if kind == "field":
                if dialog.editing is not None and dialog.fields[dialog.editing] is item:
                    shown = ("•" * len(dialog.buffer) if item["secret"] else dialog.buffer) + "█"
                    value = (shown, LEMON)
                elif item["multiline"] and item["value"] is None:
                    value = ("each node's own setting", DIM)
                elif item["multiline"]:
                    value = ("(%d lines)" % len(item["value"].splitlines()), DIM)
                elif item["secret"]:
                    value = ("••••••", "") if item["value"] else ("required, never saved", RED)
                else:
                    value = (item["value"] or "(empty)", "" if item["value"] else FAINT)
                segments = [("    ", ""), (_cut(item["name"], name_w), BOLD), value,
                            ("   %s" % item["module"], FAINT)]
            elif kind == "keys":
                segments = [("    ", ""), (_cut("ssh keys", name_w), BOLD),
                            (dialog.key_options[dialog.key_choice][0], CYAN),
                            ("   enter changes", FAINT)]
            else:
                segments = [("  ▶ ", LEMON),
                            ("run on %d instance%s" % (len(targets), "" if len(targets) == 1
                                                       else "s"), "1;" + LEMON)]
            body.append((segments, bg, index))
        if not dialog.fields:
            body.insert(len(body) - 1, ([("    no parameters", DIM)], None, None))
        if dialog.problem:
            body.append(([("", "")], None, None))
            body.append(([("  ! %s" % dialog.problem, ORANGE)], None, None))

    page = max(1, height - 2)
    at = next((i for i, (_, _, index) in enumerate(body) if index == dialog.cursor), 0)
    start = max(0, min(at - page // 2, len(body) - page))
    for segments, bg, _ in body[start:start + page]:
        lines.append((segments, bg))
    footer = "%d-%d of %d" % (start + 1, min(len(body), start + page), len(body)) \
        if len(body) > page else ""
    return _box(title, lines, width, height, footer)


def _bootstrap_outcome(result):
    """``(ok, one line)`` for a finished bootstrap run."""
    modules = (result or {}).get("modules") or []
    failed = next((m for m in modules if m.get("exit_code")), None)
    if result and result.get("ok") and not failed:
        return True, "%d module%s in %ss" % (len(modules), "" if len(modules) == 1 else "s",
                                            round(sum(m.get("duration") or 0 for m in modules)))
    if failed is None:
        return False, "failed"
    output = ((failed.get("stderr") or "") + (failed.get("stdout") or "")).strip()
    last = output.splitlines()[-1] if output else ""
    return False, "%s exited %s%s" % (failed.get("name"), failed.get("exit_code"),
                                      ": %s" % last if last else "")


def _jobs_box(snap, view, jobs, width, height):
    running = sum(1 for j in jobs if not j["finished"])
    title = [(" bootstrap ", TITLE), ("%d running " % running if running else "done ", DIM)]
    multi = len({j["node"] for j in jobs}) > 1 or len(snap["nodes"]) > 1
    name_w = min(34, max(len(j["name"]) + (len(j["node"]) + 4 if multi else 0)
                         for j in jobs) + 1)
    mod_w = min(30, max(len(j["modules"]) for j in jobs) + 1)
    lines = []
    for job in jobs[-max(0, height - 2):]:
        if not job["finished"]:
            glyph = (SPINNER[view.frame % len(SPINNER)], CYAN)
            state = ("running %s" % _duration(time.time() - job["started"]), CYAN)
        elif job["ok"]:
            glyph, state = ("✓", GREEN), (job["summary"], GREEN)
        else:
            glyph, state = ("✕", RED), (job["summary"], RED)
        label = job["name"] + (" on %s" % job["node"] if multi else "")
        lines.append(([(" ", ""), glyph, (" ", ""), (_cut(label, name_w), BOLD), (" ", ""),
                       (_cut(job["modules"], mod_w), DIM), (" ", ""), state], None))
    hidden = len(jobs) - len(lines)
    return _box(title, lines, width, height, "+%d earlier" % hidden if hidden > 0 else "")


# -- running it --------------------------------------------------------------

KEYS = (
    (b"\x1b[A", "up"), (b"\x1bOA", "up"), (b"\x1b[B", "down"), (b"\x1bOB", "down"),
    (b"\x1b[5~", "pgup"), (b"\x1b[6~", "pgdn"), (b"\x1b[H", "home"), (b"\x1b[1~", "home"),
    (b"\x1b[F", "end"), (b"\x1b[4~", "end"), (b"\x1b", "quit"),
    (b"k", "up"), (b"j", "down"), (b"g", "home"), (b"G", "end"), (b"q", "quit"),
    (b"Q", "quit"), (b"s", "sort"), (b"r", "running"), (b"n", "node"),
    (b"+", "slower"), (b"=", "slower"), (b"-", "faster"), (b" ", "mark"),
    (b"c", "console"), (b"\r", "enter"), (b"\n", "enter"), (b"b", "bootstrap"),
    (b"x", "clear"), (b"\t", "tab"), (b"y", "yes"), (b"Y", "yes"),
)


PANEL_KEYS = {
    "stacks": tuple((key.encode(), "stack:" + action) for key, action in STACK_SHORTCUTS),
    "templates": tuple((key.encode(), "template:" + action)
                       for key, action in TEMPLATE_SHORTCUTS),
}


def _keys(data, extra=()):
    """The key names in one read from the terminal; escape sequences first.

    ``extra`` bindings are tried before the usual ones -- the stacks panel's
    letters while it has focus.
    """
    found = []
    while data:
        for raw, name in extra + KEYS:
            if data.startswith(raw) and (raw != b"\x1b" or len(data) == 1):
                found.append(name)
                data = data[len(raw):]
                break
        else:
            data = data[1:]                 # anything unbound
    return found


def run(cluster, interval=2, once=False, as_json=False):
    """``lemondx top``. Interactive unless ``once`` (one frame) or ``as_json``."""
    global _color
    collector = Collector(cluster)
    if once or as_json:
        # Two readings, a second apart, so CPU and network rates are there.
        collector.snapshot()
        time.sleep(1)
        snap = collector.snapshot()
        if as_json:
            print(json.dumps(snap, indent=2, default=str))
            return 0
        _color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        size = shutil.get_terminal_size((120, 40))
        print("\n".join(render(snap, View(interval), size.columns, max(12, size.lines - 1))))
        return 0

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ServiceError("`lemondx top` needs a terminal; --once prints one frame "
                           "and --json one snapshot.")
    _color = not os.environ.get("NO_COLOR")
    return _Screen(collector, View(interval if interval in INTERVALS else 2)).loop()


class _Screen:
    def __init__(self, collector, view):
        self.collector = collector
        self.view = view
        self.snap = None
        self.error = None
        self.busy = False
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.dialog = None
        self.jobs = []
        self.quitting = 0.0         # when q was pressed with runs still going

    def _gather(self):
        while not self.stop.is_set():
            self.busy = True
            try:
                snap = self.collector.snapshot()
                with self.lock:
                    self.snap, self.error = snap, None
            except Exception as exc:                  # noqa: BLE001 - shown, not fatal
                with self.lock:
                    self.error = _message(exc)
            self.busy = False
            self.wake.wait(self.view.interval)
            self.wake.clear()

    def loop(self):
        import termios
        import tty
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        resized = self.resized = threading.Event()
        previous = signal.signal(signal.SIGWINCH, lambda *_: resized.set())
        out = sys.stdout
        threading.Thread(target=self._gather, name="lemondx-top", daemon=True).start()
        try:
            tty.setcbreak(fd)
            out.write("\033[?1049h\033[?25l\033[2J")
            last = 0.0
            while True:
                if resized.is_set():
                    resized.clear()
                    out.write("\033[2J")
                if time.monotonic() - last >= 0.5:
                    self._draw(out)
                    last = time.monotonic()
                ready, _, _ = select.select([fd], [], [], TICK)
                if not ready:
                    continue
                data = os.read(fd, 64)
                if self.dialog is not None and self.dialog.editing is not None:
                    self.dialog.type(data)
                    self._draw(out)
                    continue
                panel = self.view.focus if self.view.focus in PANELS else None
                table = PANEL_KEYS[panel] if panel and (
                    self.dialog is None or isinstance(self.dialog, _ActionDialog)) else ()
                for name in _keys(data, table):
                    if self.dialog is not None:
                        self._dialog_key(name)
                        continue
                    if ":" in name:
                        self._panel_shortcut(panel, name.split(":", 1)[1])
                        continue
                    if panel and name in ("console", "bootstrap", "mark", "sort",
                                          "running", "node", "clear"):
                        continue
                    if name == "quit":
                        if self._may_quit():
                            return 0
                        continue
                    if name == "bootstrap":
                        self._open_bootstrap()
                        continue
                    if name == "clear":
                        with self.lock:
                            self.jobs = [j for j in self.jobs if not j["finished"]]
                        continue
                    if panel and name == "enter":
                        self._open_panel(panel)
                        continue
                    if name in ("console", "enter"):
                        self._console(fd, saved, out)
                        tty.setcbreak(fd)
                        out.write("\033[?1049h\033[?25l\033[2J")
                        break
                    with self.lock:
                        self.view.key(name, self.snap)
                    if name in ("slower", "faster"):
                        self.wake.set()
                self._draw(out)
                last = time.monotonic()
        except KeyboardInterrupt:
            return 0
        finally:
            self.stop.set()
            self.wake.set()
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            signal.signal(signal.SIGWINCH, previous)
            out.write("\033[0m\033[?25h\033[?1049l")
            out.flush()

    def _may_quit(self):
        running = [j for j in self.jobs if not j["finished"]]
        if not running or time.monotonic() - self.quitting < 5:
            return True
        self.quitting = time.monotonic()
        here = sum(1 for j in running if j["node"] == self.collector.cluster.local_name())
        self.view.say("%d bootstrap%s still running%s -- q again to quit"
                      % (len(running), "" if len(running) == 1 else "s",
                         "; %d on this node would stop part-way" % here if here else
                         "; they carry on on their nodes"), ORANGE, 5)
        return False

    def _open_bootstrap(self):
        with self.lock:
            snap = self.snap
            wanted = set(self.view.marked) or {self.view.selected}
        rows = [r for r in (snap or {}).get("instances") or [] if (r["node"], r["name"]) in wanted]
        targets = sorted((r["node"], r["name"]) for r in rows if r["status"] == "Running")
        if not targets:
            self.view.say("mark running instances with space, or select one, then b", ORANGE)
            return
        try:
            self.dialog = BootstrapDialog(self.collector.cluster.service, targets)
        except _ERRORS as exc:
            self.view.say("! %s" % _message(exc), RED, 10)
            return
        skipped = len(rows) - len(targets)
        if skipped:
            self.view.say("%d marked instance%s not running, left out"
                          % (skipped, "" if skipped == 1 else "s"), ORANGE)

    def _dialog_key(self, name):
        outcome = self.dialog.handle(name)
        if outcome == "close":
            self.dialog = None
        elif outcome == "run":
            dialog, self.dialog = self.dialog, None
            if isinstance(dialog, _ActionDialog):
                self._start_action(dialog)
                return
            self._run_bootstrap(dialog.targets, dialog.request())
            self.view.marked.clear()

    def _start_action(self, dialog):
        work = self._stack_action if isinstance(dialog, StackDialog) else self._template_action
        threading.Thread(target=work, args=(dialog,), name="lemondx-top-action",
                         daemon=True).start()
        self.view.say("%s %s…" % (dialog._label(dialog.action), dialog.item["name"]), CYAN)

    def _panel_shortcut(self, panel, action):
        """An action key in a panel: straight to its confirmation."""
        dialog = self._open_panel(panel)
        if dialog is None:
            return
        outcome = dialog.begin(action, direct=True)
        if dialog.problem:
            self.dialog = None
            self.view.say("%s %s: %s" % (dialog._label(action), dialog.item["name"],
                                         dialog.problem), ORANGE)
        elif outcome == "run":
            self.dialog = None
            self._start_action(dialog)

    def _open_panel(self, panel):
        with self.lock:
            item = self.view.selected_item(self.snap)
        if item is None:
            return None
        cluster = self.collector.cluster
        try:
            if panel == "stacks":
                fields = {"relaunch": _stack_fields(cluster, item["name"])} \
                    if item["defined"] else {}
            else:
                fields = _template_fields(cluster, item["name"])
        except _ERRORS as exc:
            fields = {}
            self.view.say("! %s" % _message(exc), ORANGE)
        _, serve_error = self.collector.local_api()
        self.dialog = PANELS[panel][3](item, fields, serve_error)
        return self.dialog

    def _report_state(self, name, action, result, count):
        failed = [i for i in (result or {}).get("instances") or [] if not i.get("ok")]
        if failed:
            self.view.say("! %s %s: %d failed -- %s" % (
                action, name, len(failed), "; ".join(
                    "%s: %s" % (i.get("name"), i.get("error") or "failed")
                    for i in failed[:3])), RED, 12)
        else:
            self.view.say("%s: %s done on %d instance%s" % (
                name, action, count, "" if count == 1 else "s"), GREEN)

    def _stack_action(self, dialog):
        """Carry out one stack action; the outcome lands in the header."""
        cluster = self.collector.cluster
        stack, action = dialog.item, dialog.action
        name = stack["name"]
        path = urllib.parse.quote(name, safe="")
        members = [{"node": m["node"], "name": m["name"]} for m in stack["members"]]
        api, why = self.collector.local_api()
        try:
            if action == "cancel":
                run = stack["run"]
                target = "/api/stack-runs/%s/cancel" % path
                if run["node"] == cluster.local_name():
                    if api is None:
                        raise ServiceError("cannot reach serve here: %s" % why)
                    api.request("POST", target)
                else:
                    cluster.proxy("POST", run["node"], target)
                self.view.say("cancelling %s: nothing new starts; launches under way "
                              "finish" % name, ORANGE)
            elif action in ("start", "stop", "restart"):
                if api is not None:
                    result = api.request("POST", "/api/stacks/%s/state" % path,
                                         {"action": action, "instances": members},
                                         timeout=LONG_TIMEOUT)
                else:
                    from .stacks import StackService
                    result = StackService(cluster).stack_state(name, action, members)
                self._report_state(name, action, result, len(members))
            else:
                if api is None:
                    raise ServiceError("needs `lemondx serve` on this node: %s" % why)
                if action == "destroy":
                    api.request("POST", "/api/stacks/%s/destroy" % path,
                                {"instances": members, "background": True})
                else:
                    api.request("POST", "/api/stacks/%s/launch" % path,
                                {"params": dialog.secrets(), "replace": members,
                                 "background": True})
                self.view.say("%s: %s started -- follow it in the stacks panel"
                              % (name, dialog._label(action)), CYAN)
            self.wake.set()
        except _ERRORS as exc:
            self.view.say("! %s %s: %s" % (action, name, _message(exc)), RED, 12)

    def _template_action(self, dialog):
        """Carry out one template action; the outcome lands in the header.

        Launch, recreate and destroy are the template's own routes on this
        node's serve, so each is an ordinary template run there; start, stop
        and restart go over the cluster's instances route, which fans out to
        each node that holds one.
        """
        cluster = self.collector.cluster
        template, action = dialog.item, dialog.action
        name = template["name"]
        path = urllib.parse.quote(name, safe="")
        members = [{"node": m["node"], "name": m["name"]} for m in template["members"]]
        api, why = self.collector.local_api()
        try:
            if action in ("start", "stop", "restart"):
                if api is not None:
                    result = api.request("POST", "/api/cluster/containers/state",
                                         {"action": action, "instances": members},
                                         timeout=LONG_TIMEOUT)
                else:
                    result = cluster.change_state(members, action)
                self._report_state(name, action, result, len(members))
            else:
                if api is None:
                    raise ServiceError("needs `lemondx serve` on this node: %s" % why)
                if action == "launch":
                    body = dict({"count": int(dialog.value("count") or 1),
                                 "params": dialog.secrets(), "background": True},
                                **(dialog.value("where") or {}))
                    api.request("POST", "/api/templates/%s/launch" % path, body,
                                timeout=LONG_TIMEOUT)
                elif action == "recreate":
                    api.request("POST", "/api/templates/%s/recreate" % path,
                                {"instances": members, "params": dialog.secrets(),
                                 "background": True}, timeout=LONG_TIMEOUT)
                else:
                    api.request("POST", "/api/templates/%s/destroy" % path,
                                {"instances": members, "background": True},
                                timeout=LONG_TIMEOUT)
                self.view.say("%s: %s started -- follow it in the templates panel"
                              % (name, action), CYAN)
            self.wake.set()
        except _ERRORS as exc:
            self.view.say("! %s %s: %s" % (action, name, _message(exc)), RED, 12)

    def _run_bootstrap(self, targets, request):
        label = " → ".join(request["modules"])
        gate = threading.Semaphore(FANOUT_WORKERS)
        for node, name in targets:
            job = {"node": node, "name": name, "modules": label, "started": time.time(),
                   "finished": None, "ok": None, "summary": None}
            with self.lock:
                self.jobs.append(job)
            threading.Thread(target=self._bootstrap_one, args=(job, request, gate),
                             name="lemondx-top-bootstrap", daemon=True).start()

    def _bootstrap_one(self, job, request, gate):
        cluster = self.collector.cluster
        with gate:
            try:
                if job["node"] == cluster.local_name():
                    result = cluster.service.bootstrap(
                        job["name"], request["modules"], params=request["params"],
                        ssh_keys=request["ssh_keys"], remember=False)
                else:
                    # The node's own route, as the web UI's drawer reaches it;
                    # remember=False keeps its module settings as they were.
                    result = cluster.proxy(
                        "POST", job["node"], "/api/containers/%s/bootstrap"
                        % urllib.parse.quote(job["name"], safe=""),
                        body=dict(request, remember=False))
                ok, summary = _bootstrap_outcome(result)
            except _ERRORS as exc:
                ok, summary = False, _message(exc)
            except Exception as exc:              # noqa: BLE001 - shown on the row
                ok, summary = False, "unexpected error: %s" % exc
        with self.lock:
            job.update(ok=ok, summary=summary, finished=time.time())

    def _console(self, fd, saved, out):
        """Leave top's screen for the selected instance's console, then come back."""
        import termios
        with self.lock:
            snap, key = self.snap, self.view.selected
        row = next((r for r in (snap or {}).get("instances") or []
                    if (r["node"], r["name"]) == key), None)
        if row is None:
            return
        label = row["name"] if row["node"] == snap["node"] else \
            "%s on %s" % (row["name"], row["node"])
        if row["status"] != "Running":
            self.view.say("%s is %s; only a running instance has a console"
                          % (label, row["status"].lower()), ORANGE)
            return
        # Top's screen goes first, so the console has the whole terminal and
        # what it printed is still there to scroll back through afterwards.
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        out.write("\033[0m\033[?25h\033[?1049l\033[2J\033[H")
        out.write(_paint("console of %s  ·  Ctrl-] returns to top  ·  "
                         "Enter if it stays blank\n" % label, DIM))
        out.flush()
        size = shutil.get_terminal_size((80, 24))
        try:
            guest = open_console(self.collector.cluster, row["node"], row["name"],
                                 size.columns, size.lines)
        except _ERRORS + (WebSocketError,) as exc:
            self.view.say("! console of %s: %s" % (label, _message(exc)), RED, 10)
            return
        self.resized.clear()
        ending = _attached(fd, guest, self.resized)
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        self.view.say("back from %s%s" % (label, " -- %s" % ending if ending else ""),
                      ORANGE if ending else LEMON)

    def _draw(self, out):
        size = shutil.get_terminal_size((120, 40))
        with self.lock:
            lines = render(self.snap, self.view, size.columns, size.lines,
                           busy=self.busy, error=self.error, dialog=self.dialog,
                           jobs=list(self.jobs))
        # Home and overwrite rather than clear: every line is full width, so
        # nothing of the last frame survives and nothing flickers.
        out.write("\033[H" + "\r\n".join(lines))
        out.flush()

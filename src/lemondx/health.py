"""Health of running instances: judging samples, and the settings that tune it.

A round of checks is one instance listing plus a liveness probe per running
instance (``ContainerService.check_health``); this module turns those into one
record per instance. It never talks to the daemon itself, so the judgement
can be reasoned about -- and reused by the CLI and the server -- apart from
how the numbers were fetched.

Load average is the signal people reach for, and a container's own
``/proc/loadavg`` is the host's unless LXCFS virtualises it (``lxcfs -l``),
which is off by default. So ``LoadSampler`` keeps a container's load average
itself, the way the kernel keeps the host's: every five seconds it counts the
threads in the container's cgroup that are running, waiting to run or in
uninterruptible sleep, and folds that into 1, 5 and 15 minute averages. That
reads host files, not the daemon, and needs lemondx on the daemon's host --
which talking to it over a unix socket already implies. A VM's load comes
from its own kernel, through the probe.

An instance launched from a template whose record carries an ``app_check``
also gets the template's own check each round: a script run inside it that
answers the way a Nagios plugin does, by exit code (``APP_STATES``). That is
the only part of a round that knows what the instance is *for*; everything
else here judges it as a machine.
"""

from __future__ import annotations

import datetime
import math
import os
import threading
import time

from . import store

HEALTHY = "healthy"
DEGRADED = "degraded"
UNHEALTHY = "unhealthy"
STARTING = "starting"
UNKNOWN = "unknown"
PAUSED = "paused"

SETTINGS_SECTION = "health"
SETTINGS_VERSION = 1
MIN_INTERVAL = 15

DEFAULT_SETTINGS = {
    "enabled": True,
    "interval_seconds": 60,
    "cpu_percent": 90,
    "memory_percent": 90,
    "load_average": 3,
    "probe_timeout_seconds": 10,
    "failures_before_unhealthy": 2,
    "start_grace_seconds": 120,
}

# key -> (lowest, highest, integer only)
_RANGES = {
    "interval_seconds": (MIN_INTERVAL, 86400, False),
    "cpu_percent": (1, 100, False),
    "memory_percent": (1, 100, False),
    "load_average": (0.1, 10000, False),
    "probe_timeout_seconds": (0.001, 300, False),
    "failures_before_unhealthy": (1, 100, True),
    "start_grace_seconds": (0, 86400, False),
}

# Keys a saved file may still carry but that no longer mean anything.
# load_per_cpu became the absolute load_average; its old value is not
# carried over, since a per-CPU figure is not an absolute one.
_RETIRED = frozenset({"version", "load_per_cpu"})

# How close an instance's task count must be to the host's for its load
# average to be taken as the host's own numbers.
HOST_TASKS_TOLERANCE = 0.10

# The kernel recomputes load averages every five seconds (LOAD_FREQ); sampling
# at the same rate gives numbers that read like the ones people know.
LOAD_SAMPLE_SECONDS = 5
_LOAD_PERIODS = (60.0, 300.0, 900.0)
# Where a container's cgroup can live: the unified (v2) hierarchy first, then
# the v1 controllers that hold every task.
CGROUP_ROOTS = ("/sys/fs/cgroup", "/sys/fs/cgroup/pids", "/sys/fs/cgroup/cpu,cpuacct",
                "/sys/fs/cgroup/cpu", "/sys/fs/cgroup/unified")


# Nagios plugin exit codes. Anything else -- 127 for a missing interpreter
# among them -- is unknown, as Nagios itself treats it.
APP_OK, APP_WARNING, APP_CRITICAL, APP_UNKNOWN = "ok", "warning", "critical", "unknown"
# Configured, but with no result yet: not run, or the instance is being built.
APP_PENDING = "pending"
APP_STATES = {0: APP_OK, 1: APP_WARNING, 2: APP_CRITICAL, 3: APP_UNKNOWN}
# What the wrapper exits with when its watchdog had to stop the script, as
# coreutils' `timeout` would.
APP_TIMEOUT_CODES = (124,)
APP_OUTPUT_LIMIT = 500
# The full output kept per stream for the latest run, for someone asking what
# the script said. Never part of a health record: those are re-sent on every
# poll, and a chatty check would make each one heavy.
APP_DETAIL_LIMIT = 16 * 1024
# Keys of a result that stay with the checker and out of health records.
APP_DETAIL_KEYS = ("stdout", "stderr", "truncated")


class HealthSettingsError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


def clean_settings(raw):
    """Validated settings with every key present; raises HealthSettingsError."""
    if not isinstance(raw, dict):
        raise HealthSettingsError("Health settings must be a JSON object.")
    unknown = sorted(set(raw) - set(DEFAULT_SETTINGS) - _RETIRED)
    if unknown:
        raise HealthSettingsError("Unknown health setting(s): %s. Known: %s."
                                  % (", ".join(unknown), ", ".join(DEFAULT_SETTINGS)))
    settings = dict(DEFAULT_SETTINGS)
    for key, value in raw.items():
        if key in _RETIRED:
            continue
        if key == "enabled":
            if not isinstance(value, bool):
                raise HealthSettingsError("enabled: must be true or false")
            settings[key] = value
            continue
        low, high, integer = _RANGES[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or (integer and not float(value).is_integer()) or not low <= value <= high:
            raise HealthSettingsError("%s: must be %s from %g to %g"
                                      % (key, "a whole number" if integer else "a number",
                                         low, high))
        settings[key] = int(value) if integer else value
    return settings


def load_settings():
    """``(settings, warning)``: saved settings, or the defaults and why they were used.

    Unlike auth, a broken file here falls back rather than stopping anything:
    the worst a wrong threshold does is mislabel an instance.
    """
    try:
        raw = store.load_config(SETTINGS_SECTION)
    except ValueError as exc:
        return dict(DEFAULT_SETTINGS), "Ignoring saved health settings: %s" % exc
    if raw is None:
        return dict(DEFAULT_SETTINGS), None
    try:
        return clean_settings(raw), None
    except HealthSettingsError as exc:
        return dict(DEFAULT_SETTINGS), "Ignoring saved health settings in %s: %s" % (
            store.config_path(SETTINGS_SECTION), exc.message)


def save_settings(settings):
    cleaned = clean_settings(settings)
    store.save_config(SETTINGS_SECTION, dict(cleaned, version=SETTINGS_VERSION))
    return cleaned


def reset_settings():
    return store.delete_config(SETTINGS_SECTION)


def thresholds(settings):
    return {k: v for k, v in settings.items() if k not in ("enabled", "interval_seconds")}


# -- load average ----------------------------------------------------------


def parse_loadavg(text):
    """``([1, 5, 15], total_tasks)`` from /proc/loadavg text, or None."""
    parts = (text or "").split()
    try:
        averages = [float(p) for p in parts[:3]]
        tasks = int(parts[3].split("/", 1)[1])
    except (IndexError, ValueError):
        return None
    return (averages, tasks) if len(averages) == 3 else None


def host_tasks():
    """The host's own task count, to recognise a container echoing it back."""
    try:
        with open("/proc/loadavg", encoding="ascii") as handle:
            parsed = parse_loadavg(handle.read())
    except OSError:
        return None
    return parsed[1] if parsed else None


def load_scope(instance_type, tasks, host):
    """Whether a load average describes the instance, or is the host's."""
    if instance_type == "virtual-machine":
        return "instance"                  # its own kernel
    if not host or tasks is None:
        return "unknown"
    return "host" if abs(tasks - host) <= host * HOST_TASKS_TOLERANCE else "instance"


def cgroup_dir(prefix, name, project):
    """The host directory of a container's cgroup, or None if it cannot be found.

    The daemon names a container's cgroup ``<prefix><name>``, or
    ``<prefix><project>_<name>`` outside the default project.
    """
    leaf = prefix + (name if not project or project == "default" else "%s_%s" % (project, name))
    for root in CGROUP_ROOTS:
        path = os.path.join(root, leaf)
        if os.path.isdir(path):
            return path
    return None


def count_active(directory):
    """Threads in a cgroup tree that the kernel's load average would count.

    That is state R (running, or runnable and waiting for a CPU) and D
    (uninterruptible sleep, usually on I/O). Threads that exit mid-count are
    skipped. Returns None if the cgroup is gone.
    """
    if not os.path.isdir(directory):
        return None
    count = 0
    for current, _subdirs, files in os.walk(directory):
        listing = "cgroup.threads" if "cgroup.threads" in files else "tasks" if "tasks" in files else None
        if not listing:
            continue
        try:
            with open(os.path.join(current, listing), "rb") as handle:
                tids = handle.read().split()
        except OSError:
            continue
        for tid in tids:
            try:
                with open(b"/proc/%s/stat" % tid, "rb") as handle:
                    stat = handle.read()
            except OSError:
                continue
            # The command name is in parentheses and may itself contain
            # spaces or parentheses, so the state is found after the last one.
            end = stat.rfind(b")")
            if end != -1 and stat[end + 2:end + 3] in (b"R", b"D"):
                count += 1
    return count


def measure_load(targets, window, step=1.0):
    """Average active threads per container over ``window`` seconds, for one-off checks.

    A one-off caller has no minute of history, so this is a plain average of
    samples a second apart rather than the kernel-style moving average.
    """
    rounds = max(1, int(round(window / step)))
    totals, seen = {}, {}
    for index in range(rounds):
        for name, directory in targets.items():
            count = count_active(directory)
            if count is not None:
                totals[name] = totals.get(name, 0) + count
                seen[name] = seen.get(name, 0) + 1
        if index < rounds - 1:
            time.sleep(step)
    return {name: {"avg": [round(totals[name] / float(seen[name]), 2), None, None],
                   "warming": False, "window": window}
            for name in totals}


class LoadSampler:
    """Per-container load averages, kept the way the kernel keeps the host's.

    Runs on its own thread for as long as `serve` does. It only reads host
    files, so the five-second rate costs no daemon calls; the health round
    that judges the numbers still runs on its own interval.
    """

    def __init__(self, period=LOAD_SAMPLE_SECONDS):
        self.period = period
        self._lock = threading.Lock()
        self._targets = {}      # name -> cgroup directory
        self._loads = {}        # name -> {"avg": [1, 5, 15], "at": t, "since": t}

    def set_targets(self, targets):
        """Sample these containers from now on; averages of others are dropped.

        A container seen for the first time is counted at once, so the round
        that introduced it already has a number rather than falling back to
        the host's.
        """
        with self._lock:
            self._targets = dict(targets)
            for name in list(self._loads):
                if name not in targets:
                    del self._loads[name]
            new = {n: d for n, d in targets.items() if n not in self._loads}
        if new:
            self.tick(new)

    def tick(self, targets=None):
        with self._lock:
            targets = dict(self._targets if targets is None else targets)
        for name, directory in targets.items():
            count = count_active(directory)
            now = time.time()
            if count is None:
                continue
            with self._lock:
                if name not in self._targets:
                    continue
                entry = self._loads.get(name)
                if entry is None:
                    entry = self._loads[name] = {"avg": [0.0, 0.0, 0.0], "at": now,
                                                 "since": now, "sum": 0, "samples": 0}
                if entry["samples"] is not None:
                    # The kernel starts its averages from zero, which takes
                    # minutes to climb to a steady load of 4. For the first
                    # minute use the plain mean of what has been counted, then
                    # carry on from there as a moving average.
                    entry["sum"] += count
                    entry["samples"] += 1
                    mean = entry["sum"] / float(entry["samples"])
                    entry["avg"] = [mean, mean, mean]
                    if now - entry["since"] >= _LOAD_PERIODS[0]:
                        entry["samples"] = None
                else:
                    elapsed = max(0.0, now - entry["at"])
                    entry["avg"] = [load * math.exp(-elapsed / span) + count * (1 - math.exp(-elapsed / span))
                                    for load, span in zip(entry["avg"], _LOAD_PERIODS)]
                entry["at"] = now

    def load(self, name):
        """``{avg, warming, window}`` for a container, or None before its first sample."""
        with self._lock:
            entry = self._loads.get(name)
            if entry is None:
                return None
            return {"avg": [round(v, 2) for v in entry["avg"]],
                    # Under a minute of samples it is a mean of a few counts,
                    # too jumpy to call a container degraded on.
                    "warming": entry["samples"] is not None,
                    "window": None}

    def start(self):
        def loop():
            while True:
                started = time.time()
                try:
                    self.tick()
                except Exception as exc:                    # noqa: BLE001
                    print("[lemondx] load sampling failed: %s" % exc)
                time.sleep(max(0.5, self.period - (time.time() - started)))
        threading.Thread(target=loop, name="lemondx-loadavg", daemon=True).start()


def iso_epoch(value):
    """Seconds since the epoch for a daemon timestamp, or None (including year 1)."""
    text = (value or "").strip()
    if not text or text.startswith("0001-"):
        return None
    text = text.replace("Z", "+00:00")
    # Python before 3.11 accepts at most six fractional digits; the daemon sends nine.
    if "." in text:
        head, _, rest = text.partition(".")
        # Only the leading run of digits is the fraction. The daemon stamps in
        # the host's local zone, so what follows is usually an offset whose own
        # digits ("+02:00") would otherwise be eaten as precision and the zone
        # lost -- silently reading the record as local time, hours out.
        digits = rest[:len(rest) - len(rest.lstrip("0123456789"))]
        zone = rest[len(digits):]
        text = "%s.%s%s" % (head, (digits + "000000")[:6], zone)
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


# -- application checks -----------------------------------------------------


def app_result(exit_code, stdout, stderr, ms, timeout):
    """One app check's record from what its script did.

    The first line of output is the status text and anything after a ``|``
    on it is performance data, both as Nagios plugins print them; the
    performance data is dropped because nothing here graphs it. A script that
    printed nothing to stdout is described by its stderr, which is where a
    shell puts "not found".
    """
    def tail(text):
        text = text or ""
        return (text[-APP_DETAIL_LIMIT:], True) if len(text) > APP_DETAIL_LIMIT else (text, False)

    out, cut_out = tail(stdout)
    err, cut_err = tail(stderr)
    detail = {"stdout": out, "stderr": err, "truncated": cut_out or cut_err}
    if exit_code in APP_TIMEOUT_CODES:
        # Nagios reports a plugin that overran as critical by default: a check
        # that hangs usually means the thing it checks does.
        return dict(detail, status=APP_CRITICAL, code=exit_code, ms=ms,
                    output="timed out after %gs" % timeout)
    lines = [line for line in (stdout or "").splitlines() if line.strip()] \
        or [line for line in (stderr or "").splitlines() if line.strip()]
    output = lines[0].split("|", 1)[0].strip()[:APP_OUTPUT_LIMIT] if lines else ""
    return dict(detail, status=APP_STATES.get(exit_code, APP_UNKNOWN), code=exit_code,
                ms=ms, output=output)


def app_placeholder(configured):
    """The app status of an instance with no result to report.

    With no check configured there is nothing application-level to be wrong,
    so it reads as ok rather than as missing; a configured check that has not
    answered yet is pending, and judged on nothing.
    """
    return {"status": APP_PENDING if configured else APP_OK, "code": None, "ms": None,
            "output": "" if configured else "no app check configured",
            "checked_at": None, "streak": 0, "interval": None, "configured": configured}


def app_error(message, ms):
    """An app check that could not be run at all: unknown, as Nagios would say."""
    return {"status": APP_UNKNOWN, "code": None, "ms": ms,
            "output": str(message)[:APP_OUTPUT_LIMIT],
            "stdout": "", "stderr": str(message), "truncated": False}


class AppChecker:
    """Runs each instance's app check on the template's own interval, for `serve`.

    A check's interval is the template's to choose and need not match the
    health round's, so checks are scheduled here and a round only reads the
    latest result. The critical streak is counted per run, as Nagios counts
    check attempts, so "two criticals in a row" means two runs of the script
    whatever the round's interval.

    ``run(name, app_check)`` returns an ``app_result()``, or None when the
    instance should be skipped this time (it is being bootstrapped).
    ``on_result(name)`` is told when a new result is in, so the instance's
    record can take it at once rather than at the next round.
    """

    TICK_SECONDS = 1.0
    WORKERS = 4

    def __init__(self, run, on_result=None):
        self._run = run
        self._on_result = on_result
        self._lock = threading.Lock()
        self._targets = {}      # name -> app_check
        self._results = {}      # name -> latest result, with checked_at and streak
        self._due = {}          # name -> when to run next
        self._running = set()

    def set_targets(self, targets):
        """Check these instances from now on; results for any others are dropped.

        A new instance, or one whose template changed its check, is due at
        once, so the round that introduced it is followed by a result rather
        than a full interval of nothing.
        """
        with self._lock:
            for name in list(self._results):
                if name not in targets:
                    del self._results[name]
            for name in list(self._due):
                if name not in targets or targets[name] != self._targets.get(name):
                    del self._due[name]
            self._targets = dict(targets)
            now = time.time()
            for name in targets:
                self._due.setdefault(name, now)

    def update_template(self, template, app_check):
        """A template's check was saved or deleted: apply it now, not next round.

        Instances launched from it switch to the new check and run it at once;
        with ``app_check`` None they stop being checked. Their old result goes
        too -- its streak belongs to a script that is no longer the check.
        Returns the names affected, for the caller to refold their records.
        """
        wanted = dict(app_check, template=template) if app_check else None
        with self._lock:
            changed = [n for n, c in self._targets.items()
                       if c.get("template") == template and c != wanted]
            now = time.time()
            for name in changed:
                self._results.pop(name, None)
                if wanted is None:
                    del self._targets[name]
                    self._due.pop(name, None)
                else:
                    self._targets[name] = wanted
                    self._due[name] = now
        return changed

    def has_target(self, name):
        with self._lock:
            return name in self._targets

    def target(self, name):
        """The check an instance is on (with its template), or None."""
        with self._lock:
            target = self._targets.get(name)
            return dict(target) if target else None

    def result(self, name):
        with self._lock:
            result = self._results.get(name)
            return dict(result) if result else None

    def _check(self, name, app_check):
        try:
            outcome = self._run(name, app_check)
        except Exception as exc:                            # noqa: BLE001
            outcome = app_error("the check could not be run: %s" % exc, 0)
        with self._lock:
            self._running.discard(name)
            # Retargeted while it ran: the answer is to a question nobody asks now.
            if outcome is None or self._targets.get(name) != app_check:
                return
            previous = self._results.get(name) or {}
            streak = previous.get("streak", 0) + 1 if outcome["status"] == APP_CRITICAL else 0
            self._results[name] = dict(outcome, checked_at=time.time(), streak=streak,
                                       interval=app_check["interval_seconds"])
        if self._on_result is not None:
            try:
                self._on_result(name)
            except Exception as exc:                        # noqa: BLE001
                print("[lemondx] app check for %s not recorded: %s" % (name, exc))

    def start(self):
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=self.WORKERS,
                                  thread_name_prefix="lemondx-appcheck")

        def loop():
            while True:
                now = time.time()
                with self._lock:
                    due = [(n, self._targets[n]) for n, at in self._due.items()
                           if at <= now and n not in self._running]
                    for name, app_check in due:
                        # Scheduled from the start, not the end, of a run: an
                        # interval means how often, and a run never overlaps
                        # its predecessor because `_running` holds it back.
                        self._due[name] = now + app_check["interval_seconds"]
                        self._running.add(name)
                for name, app_check in due:
                    pool.submit(self._check, name, app_check)
                time.sleep(self.TICK_SECONDS)
        threading.Thread(target=loop, name="lemondx-appchecks", daemon=True).start()


# -- judgement -------------------------------------------------------------


class Tracker:
    """What one round needs from the last: CPU baselines, failure streaks, since-when.

    One per ContainerService, so the server's rounds build on each other while
    a CLI invocation starts cold and samples over a short window instead.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {}        # name -> {usage, pid, at, failures, status, since}

    def has_baseline(self, names):
        with self._lock:
            return all(self._state.get(n, {}).get("usage") is not None for n in names)

    def baseline(self, samples):
        """Remember CPU counters without judging anything, for a later round."""
        with self._lock:
            for sample in samples:
                entry = self._state.setdefault(sample["name"], {})
                entry.update(usage=sample["cpu_usage"], pid=sample["pid"], at=sample["at"])

    def forget_except(self, names):
        """Drop instances no longer running, so a restart starts a fresh streak."""
        with self._lock:
            for name in list(self._state):
                if name not in names:
                    del self._state[name]

    def evaluate(self, sample, probe, settings, host):
        """One instance's record from this round's sample and probe result.

        ``sample``: name, type, status, cpu_usage (ns or None), pid, processes,
        memory_usage, memory_limit, cores, started_at, at.
        ``probe``: ok, ms, error, text.

        This is the instance judged as a machine; ``fold_app()`` adds its
        template's app check, which arrives on its own schedule.
        """
        now = sample["at"]
        with self._lock:
            entry = self._state.setdefault(sample["name"], {})
            record = {
                "name": sample["name"],
                "type": sample["type"],
                "checked_at": now,
                "processes": sample["processes"],
                "cpu": None,
                "memory": _memory(sample),
                "load": None,
                "probe": {k: probe.get(k) for k in ("ok", "ms", "error")} if probe else None,
                "app": None,
                # Private to fold_app(): an app check is not judged while the
                # instance is still starting. A time rather than a flag, since
                # a check can land between rounds after the grace has ended.
                # Stripped before it is served.
                "_grace_until": None,
                "failures": 0,
                "reasons": [],
            }

            if sample["status"] == "Frozen":
                status = PAUSED
                entry.update(usage=None, failures=0)
            else:
                record["cpu"] = self._cpu(entry, sample)
                status = self._judge(entry, sample, probe, settings, host, record)

            if entry.get("status") != status:
                entry["status"], entry["since"] = status, now
            record["status"] = status
            record["since"] = entry["since"]
            return record

    @staticmethod
    def _cpu(entry, sample):
        usage, pid = sample["cpu_usage"], sample["pid"]
        previous, previous_pid, previous_at = entry.get("usage"), entry.get("pid"), entry.get("at")
        entry.update(usage=usage, pid=pid, at=sample["at"])
        # No baseline, a restart (new pid, counter reset), or no counter at all
        # (a VM without its agent): nothing to average this round.
        if usage is None or previous is None or pid != previous_pid or usage < previous:
            return None
        elapsed = sample["at"] - previous_at
        if elapsed <= 0:
            return None
        cores_used = (usage - previous) / (elapsed * 1e9)
        cores = sample["cores"] or 1
        return {"percent": round(100.0 * cores_used / cores, 1),
                "cores_used": round(cores_used, 3), "cores": cores}

    @staticmethod
    def _judge(entry, sample, probe, settings, host, record):
        reasons = record["reasons"]
        is_vm = sample["type"] == "virtual-machine"
        started = sample["started_at"]
        in_grace = started is not None and sample["at"] - started < settings["start_grace_seconds"]
        if started is not None:
            record["_grace_until"] = started + settings["start_grace_seconds"]

        parsed = parse_loadavg(probe.get("text")) if probe and probe.get("ok") else None
        counted = sample.get("cgroup_load")
        if counted:
            record["load"] = dict(counted, scope="instance", source="cgroup")
        elif parsed:
            # No cgroup to count (a VM, or a layout we could not find): fall
            # back to what the instance itself reports, which for a container
            # is usually the host's.
            averages, tasks = parsed
            record["load"] = {"avg": averages, "scope": load_scope(sample["type"], tasks, host),
                              "source": "guest" if is_vm else "proc",
                              "warming": False, "window": None}

        probe_ok = bool(probe and probe.get("ok"))
        agentless = is_vm and not probe_ok and "agent" in ((probe or {}).get("error") or "").lower()
        if probe_ok or agentless or in_grace:
            entry["failures"] = 0
        else:
            entry["failures"] = entry.get("failures", 0) + 1
        record["failures"] = entry["failures"]

        if in_grace and not probe_ok:
            reasons.append("started %ds ago; not answering yet" % int(sample["at"] - started))
            return STARTING
        if agentless:
            reasons.append("its VM agent is not running, so it cannot be probed")
            return UNKNOWN

        if not probe_ok:
            reasons.append("did not answer a probe: %s" % ((probe or {}).get("error") or "no reply"))
        if not is_vm and sample["processes"] == 0:
            reasons.append("running with no processes")
            return UNHEALTHY
        if entry["failures"] >= settings["failures_before_unhealthy"]:
            return UNHEALTHY

        if not in_grace:
            cpu, memory, load = record["cpu"], record["memory"], record["load"]
            if cpu and cpu["percent"] >= settings["cpu_percent"]:
                reasons.append("CPU at %g%% of %d core%s" % (
                    cpu["percent"], cpu["cores"], "" if cpu["cores"] == 1 else "s"))
            if memory["percent"] is not None and memory["percent"] >= settings["memory_percent"]:
                reasons.append("memory at %g%% of its limit" % memory["percent"])
            if load and load["scope"] == "instance" and not load["warming"] \
                    and load["avg"][0] > settings["load_average"]:
                reasons.append("load average %.2f, over %g" % (load["avg"][0], settings["load_average"]))
        # A failed probe short of the streak lands here too: degraded, with its
        # reason, so one slow minute shows without being called a failure.
        return DEGRADED if reasons else HEALTHY


def fold_app(base, app, settings, previous=None):
    """An instance's record: its machine judgement with its app check folded in.

    ``base`` is ``Tracker.evaluate()``'s record, ``app`` the latest app check
    (``app_result()`` plus ``streak``, the run of criticals it ends, and
    ``configured``), ``previous`` what this returned last time, for ``since``.
    Kept apart from evaluate() because the two change at different times: a
    round every interval_seconds, a check on its template's own interval.
    """
    record = dict(base, app=app, reasons=list(base["reasons"]))
    grace_until = record.pop("_grace_until", None)
    grace = grace_until is not None and time.time() < grace_until
    status = base["status"]
    # Starting, paused, or a VM that cannot be reached to run anything: the
    # machine-level answer already says all there is to say.
    if app and not grace and status in (HEALTHY, DEGRADED):
        if app["status"] not in (APP_OK, APP_PENDING):
            record["reasons"].append("app check %s%s" % (
                app["status"], ": " + app["output"] if app["output"] else ""))
            status = DEGRADED
        # Critical takes the same streak as a failed probe, like a Nagios
        # soft state: one bad answer shows as degraded, a run of them is a
        # failure. Warning and unknown never go further than degraded.
        if app["status"] == APP_CRITICAL \
                and app.get("streak", 1) >= settings["failures_before_unhealthy"]:
            status = UNHEALTHY
    record["status"] = status
    if previous is not None:
        record["since"] = previous["since"] if previous["status"] == status \
            else time.time()
    elif status != base["status"]:
        record["since"] = time.time()
    return record


def _memory(sample):
    usage, limit = sample["memory_usage"], sample["memory_limit"]
    percent = round(100.0 * usage / limit, 1) if limit and usage is not None else None
    return {"usage": usage, "limit": limit, "percent": percent}


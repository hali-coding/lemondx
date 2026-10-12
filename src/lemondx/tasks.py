"""Tasks: job modules run in a throwaway instance, on a schedule or when invoked.

A task names a template -- image, size, network, fabric and the setup modules
that make it what it is -- and adds its own *job* modules, run as a second
pass once setup is done. Every run gets an instance of its own, made for that
run and destroyed when it ends however it ends, so nothing lingers between
runs and a task has no home: the cluster decides, per occurrence, which node
runs it. That is the Lambda shape, made of the parts lemondx already has --
templates, modules, the bootstrap runner, the member list.

**Inputs** are what a caller hands one run, like the message an SNS topic
delivers to a function: declared on the task (``inputs``, each with a
default, and marked required or secret), given per invocation, and read when
the run starts. A job module sees each as ``LEMONDX_INPUT_<NAME>`` and a
parameter can be set to ``{{inputs.NAME}}``. A whole message may come along
too, as the ``payload``: written to a file in the instance and named by
``LEMONDX_TASK_PAYLOAD``, never put in the environment, the log or a record.
A scheduled run has nobody to hand it anything, so it runs on the defaults,
and a task with a schedule cannot have a required input without one.

**Who runs an occurrence.** There is no leader and no shared state, so a
scheduled occurrence is settled by vote, and the guarantee is *at most once*:

* An occurrence is ``(task, slot)``, the slot being the UTC second the cron
  expression fires at -- derived from the record alone, so every node names it
  alike. Clocks decide *when* a node tries, never *whether* two runs happen.
* Every node ranks the task's eligible nodes by rendezvous hashing over the
  slot, the same order everywhere, and so a different first choice for each
  slot. A node waits ``HANDOFF_SECONDS`` for each node ranked above it (one
  known to be in maintenance does not count), then becomes a candidate --
  unless it declines: in maintenance, launches failing, already running as
  many task instances as it takes. Declining is the node's own word.
* A candidate asks every member, itself included, for its grant. A member
  grants each occurrence once, to whoever asks first, and writes it to disk
  before answering (``store.claim_task_grant()``), so a restart cannot make it
  grant twice.
* A member **vetoes** a candidate that is asking at the wrong time, judged
  against its own clock and its own copy of the task: the candidate's clock
  reading is more than ``MAX_CLOCK_SKEW`` from its own, the slot is not an
  occurrence of the schedule as this member has it (or it has the task
  disabled, or not at all), or the slot is not due -- early, or later than
  every node's turn could have come. A veto is a vote withheld, with the
  reason, logged on both sides (``task.veto``) so a drifting clock shows up as
  that rather than as runs quietly going missing. One member with a wrong
  clock is outvoted; a candidate with one is vetoed by the rest. A strict majority of members means run; two majorities always
  share a member, which granted only one of them. Short of a majority, the
  candidate gives back what it got, so a node ranked below can still gather
  one.
* So a split or a mostly-down cluster *misses* occurrences rather than running
  them twice, and says so (``task.missed``). Nothing is caught up: an
  occurrence nobody could run is gone.

An invocation needs no vote -- one request is one run -- so the node that
receives it picks the first eligible node, in rank order over a random slot,
that does not decline, and forwards it there.

A run lives in the memory of the process running it, like a template run, and
is written to this node's history (``store.save_task_run()``) as it starts and
as it ends. Its instance is tagged ``user.lemondx.task``/``task-run``, never
with its template's tag, and is ephemeral as well as destroyed, so it goes
even if this process dies with it -- and the sweeper takes any task instance
on this node that no live run owns.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets as secrets_module
import threading
import time

from . import cron, eventlog, store
from .bootstrap import (BootstrapError, BootstrapRunner, check_shell_syntax, discover_modules,
                        load_prelude, missing_secrets, redact, secret_param_names,
                        split_param)
from .cluster import ClusterError
from .lxd import LXDError
from .nodeclient import NodeError
from .service import (RUN_OUTPUT_TAIL, TASK_CONFIG_KEY, TASK_RUN_CONFIG_KEY, ServiceError,
                      _summary, instance_prefix)

# How long a node waits for each node ranked above it before standing itself.
# Longer than a vote takes (GRANT_TIMEOUT), so a candidate has won or given
# its grants back before the next one asks.
HANDOFF_SECONDS = 15
GRANT_TIMEOUT = 5
# How far apart two members' clocks may be before one vetoes the other's
# candidacy. Generous next to NTP (milliseconds) and to a vote's round trip,
# tight next to HANDOFF_SECONDS: past this, nodes no longer agree whose turn
# it is, which is exactly when a veto is wanted.
MAX_CLOCK_SKEW = 10
# Grants outlive the window an occurrence can be voted on in, so one can never
# be granted afresh once its grant has expired.
GRANT_KEEP = 3600
# Task instances one node runs at once before it declines more.
MAX_LOCAL_RUNS = 8
# A node whose last launches failed declines until one works again.
DECLINE_AFTER_FAILURES = 2
SWEEP_SECONDS = 300
TICK_SECONDS = 1
# Asking members what they are running: short, a silent one is not waited for.
RUNS_TIMEOUT = 5
PAYLOAD_LIMIT = 256 * 1024
PAYLOAD_PATH = "/tmp/lemondx-task-payload"
SCRIPT_PATH = "/tmp/lemondx-task-script"
# A job's output is what a task is for, so it keeps more than a launch does.
OUTPUT_LIMIT = 16 * 1024
LISTED_RUNS = 200

PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
INPUT_REFERENCE = re.compile(r"^inputs\.([A-Za-z_][A-Za-z0-9_]{0,63})$")
SECRET_REFERENCE = re.compile(r"^secrets\.([A-Za-z_][A-Za-z0-9_]{0,63})$")
GRANT_KEY = re.compile(r"^[^\x00-\x1f]{1,200}@[0-9]{1,12}$")

RUNNING, OK, FAILED, TIMED_OUT, CANCELLED, MISSED, VETOED, LOST = (
    "running", "ok", "failed", "timed_out", "cancelled", "missed", "vetoed", "lost")


def _log(message, level=logging.INFO):
    eventlog.message("task: %s" % message, level=level)


class _Stopped(Exception):
    """The run's deadline passed or somebody cancelled it."""


def rank(task, slot, nodes):
    """``nodes`` in the order they stand for one occurrence: the same on every node."""
    return sorted(nodes, key=lambda node: hashlib.sha256(
        ("%s|%s|%s" % (task, slot, node)).encode("utf-8")).hexdigest(), reverse=True)


def late_limit(members):
    """How long after its slot an occurrence may still be stood for: every
    member's turn, plus what clocks and the vote itself may add."""
    return HANDOFF_SECONDS * max(1, members) + MAX_CLOCK_SKEW + GRANT_TIMEOUT


def env_name(name):
    """The environment variable a job sees input ``name`` as."""
    return "LEMONDX_INPUT_%s" % name.upper()


def secret_env_name(name):
    """The environment variable a job *script* sees task secret ``name`` as."""
    return "LEMONDX_SECRET_%s" % name.upper()


def _placeholder_only(value):
    """Nothing but ``{{inputs.NAME}}`` or ``{{secrets.NAME}}``: what a secret param may hold."""
    references = PLACEHOLDER.findall(value)
    return bool(references) and not PLACEHOLDER.sub("", value).strip() and all(
        INPUT_REFERENCE.match(r) or SECRET_REFERENCE.match(r) for r in references)


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


class TaskService:
    def __init__(self, cluster):
        self.cluster = cluster
        self.service = cluster.service
        # Handed back for reconciliation, as StackService does.
        cluster._tasks = self
        # Runs going in this process, by run id.
        self._live = {}
        self._threads = {}
        self._lock = threading.Lock()
        self._started = False
        self._tasks_seen = (None, {})

    # -- definitions -------------------------------------------------------

    def list_tasks(self):
        groups = store.load_node_groups()
        return [self._public(task, groups) for _, task in sorted(store.load_tasks().items())]

    def get_task(self, name):
        return self._public(self._task(name))

    def _task(self, name):
        task = store.load_tasks().get(name)
        if task is None:
            raise ServiceError("No such task '%s'." % name, 404)
        return task

    def _public(self, task, groups=None):
        """A task as served: its next run, its schedule in words, its secrets' status.

        A secret parameter holding anything but a reference is dropped, as a
        stack's is: only a file copied in by hand can have one, and it is
        untrusted input on the way out as well as in.
        """
        available = discover_modules()
        secret = secret_param_names(available[m] for m in task["bootstrap"]["modules"]
                                    if m in available)
        params = {k: v for k, v in task["bootstrap"]["params"].items()
                  if split_param(k)[0] not in secret or _placeholder_only(v)}
        schedule, next_run, problem = task["schedule"], None, None
        if schedule:
            try:
                parsed = cron.Schedule(schedule["cron"], schedule["timezone"])
                if task["enabled"]:
                    next_run = parsed.next_after(time.time())
            except cron.CronError as exc:
                problem = str(exc)
        eligible = self.eligible(task, groups)
        placement = None
        if not task["groups"]:
            placement = "names no node group, so it runs nowhere; edit it to pick one"
        elif not eligible:
            placement = "its node groups have no members here, so it runs nowhere"
        return dict(task, bootstrap=dict(task["bootstrap"], params=params),
                    next_run=next_run if eligible else None, eligible=eligible,
                    placement_problem=placement,
                    schedule_text=cron.describe(schedule["cron"]) if schedule else None,
                    schedule_problem=problem,
                    secret_status=store.stack_secret_status(task["name"],
                                                            store.TASK_SECRETS_KIND))

    def save_task(self, name, template=None, bootstrap=None, script=None, description="",
                  schedule=None,
                  nodes=None, groups=None, timeout_seconds=None, overlap=None, inputs=None,
                  secrets=None, enabled=True, previous=None, propagate=True, **_ignored):
        """Save a task to the rules a person's save meets; a member's push meets them too."""
        name = self.service._record_name(name, "task")
        warnings = []
        template = str(template or "").strip()
        if not template:
            raise ServiceError("A task needs a template to launch its instance from.")
        templates = store.load_templates()
        if template not in templates:
            raise ServiceError("No such template '%s'." % template, 404)
        declared_inputs = _check_inputs(inputs)
        declared_secrets = _check_secret_names(secrets)
        script = _check_script(script)
        if script:
            if (bootstrap or {}).get("modules"):
                raise ServiceError("A task's job is modules or a script, not both.")
            selection = {"modules": [], "params": {}, "ssh_keys": []}
        else:
            selection = self._check_job(bootstrap, declared_inputs, declared_secrets, warnings)
        # The template's own setup has to run without anyone to ask: a secret
        # it needs at launch would fail every run, so it is caught here.
        try:
            self.service._launch_bootstrap(self.service._template(template), None)
        except ServiceError as exc:
            raise ServiceError("Template '%s' cannot run unattended: %s"
                               % (template, exc.message), exc.code)
        cleaned_schedule = _check_schedule(schedule)
        if cleaned_schedule:
            needed = sorted(n for n, i in declared_inputs.items()
                            if i["required"] and not i["default"])
            if needed:
                raise ServiceError(
                    "A scheduled run has nobody to give it %s: give %s a default, or "
                    "make %s optional." % (", ".join(needed), "it" if len(needed) == 1
                                           else "them", "it" if len(needed) == 1 else "them"))
        nodes = _names(nodes, "nodes")
        groups = _names(groups, "groups")
        known_groups = store.load_node_groups()
        known_nodes = set(self.cluster.all_nodes())
        # Placement is a group's business: a task tied to one node stops when
        # that node does, which is the single owner tasks exist to avoid. A
        # node may be named only beside a group, as an addition to it.
        if not groups:
            raise ServiceError(
                "A task runs on a node group: name at least one. A group of one node is "
                "allowed, but the point of a group is that a node can join it when the "
                "one it has is gone.")
        if propagate and not any(g in known_groups for g in groups):
            raise ServiceError("No node group called %s here." % " or ".join(
                "'%s'" % g for g in groups), 404)
        warnings += ["Group '%s' does not exist here." % g for g in groups
                     if g not in known_groups]
        warnings += ["Group '%s' has no members." % g for g in groups
                     if g in known_groups and not known_groups[g]["members"]]
        warnings += ["Node '%s' is not a member." % n for n in nodes if n not in known_nodes]
        low, high, default = store.TASK_TIMEOUT
        if timeout_seconds in (None, ""):
            timeout_seconds = default
        try:
            timeout_seconds = int(timeout_seconds)
        except (TypeError, ValueError):
            raise ServiceError("timeout_seconds must be a whole number of seconds.")
        if not low <= timeout_seconds <= high:
            raise ServiceError("A task's timeout is between %d seconds and %d hours."
                               % (low, high // 3600))
        overlap = overlap or "skip"
        if overlap not in store.TASK_OVERLAP:
            raise ServiceError("overlap is one of: %s." % ", ".join(store.TASK_OVERLAP))

        saved = store.save_task(name, {
            "description": str(description or "")[:200], "enabled": enabled is not False,
            "template": template, "bootstrap": selection, "script": script,
            "schedule": cleaned_schedule,
            "nodes": nodes, "groups": groups, "timeout_seconds": timeout_seconds,
            "overlap": overlap, "inputs": declared_inputs, "secrets": declared_secrets})
        result = self.cluster._with_sync(self._public(saved), "tasks", saved["name"], propagate)
        moved = previous and previous != saved["name"] and propagate and \
            store.rename_stack_secrets(str(previous), saved["name"], store.TASK_SECRETS_KIND)
        held = store.stack_secret_status(saved["name"], store.TASK_SECRETS_KIND)
        orphans = sorted(set(held) - set(declared_secrets))
        for orphan in orphans:
            store.clear_stack_secret(saved["name"], orphan, kind=store.TASK_SECRETS_KIND)
        if (moved or orphans) and propagate:
            self.cluster._propagate(store.TASK_SECRETS_KIND, saved["name"])
        result["warnings"] = warnings
        return result

    def _check_job(self, bootstrap, inputs, secrets, warnings):
        """The job's module selection as stored, or raise.

        A secret parameter may only be a reference -- to a secret input or a
        task secret -- since a value saved here would sit in a synced file. A
        reference anywhere must name something the task declares.
        """
        bootstrap = bootstrap if isinstance(bootstrap, dict) else {}
        modules = bootstrap.get("modules")
        if not isinstance(modules, list) or not modules:
            raise ServiceError("A task needs a job: at least one module, or a script.")
        modules = [str(m) for m in modules]
        available = discover_modules()
        unknown = [m for m in dict.fromkeys(modules) if m not in available]
        if unknown:
            raise ServiceError("Unknown module(s): %s" % ", ".join(unknown))
        secret = secret_param_names(available[m] for m in dict.fromkeys(modules))
        params = bootstrap.get("params") if isinstance(bootstrap.get("params"), dict) else {}
        params = {str(k): "" if v is None else str(v) for k, v in params.items()}
        held_back = {}
        for key, value in params.items():
            for reference in PLACEHOLDER.findall(value):
                found = INPUT_REFERENCE.match(reference) or SECRET_REFERENCE.match(reference)
                if not found:
                    raise ServiceError("%s: '{{%s}}' is not a reference a task has; use "
                                       "{{inputs.NAME}} or {{secrets.NAME}}." % (key, reference))
                kind, wanted = reference.split(".", 1)
                if kind == "inputs" and wanted not in inputs:
                    raise ServiceError("%s names input '%s', which the task does not "
                                       "declare." % (key, wanted))
                if kind == "secrets" and wanted not in secrets:
                    raise ServiceError("%s names secret '%s', which the task does not "
                                       "declare." % (key, wanted))
                hidden = kind == "secrets" or inputs[wanted]["secret"]
                if hidden and split_param(key)[0] not in secret:
                    warnings.append("%s is not a secret parameter of its module, so the "
                                    "value given to it is not hidden from that module's "
                                    "output by the module itself (the task still masks "
                                    "it)." % key)
            if split_param(key)[0] in secret:
                if not value:
                    continue
                if not _placeholder_only(value) or any(
                        INPUT_REFERENCE.match(r) and not inputs[r.split(".", 1)[1]]["secret"]
                        for r in PLACEHOLDER.findall(value)):
                    raise ServiceError(
                        "%s is a secret: a task may only set it to {{secrets.NAME}} or a "
                        "secret input's {{inputs.NAME}}, never a value." % key)
                held_back[key] = value
        stored = self.service._stored_selection(
            modules, {k: v for k, v in params.items() if k not in held_back},
            bootstrap.get("ssh_keys"), available)
        stored["params"].update(held_back)
        return stored

    def delete_task(self, name, everywhere=True, relayed=False):
        """Delete a task's definition. Runs going keep going; its history stays."""
        if not store.delete_task(name):
            raise ServiceError("No such task '%s'." % name, 404)
        store.drop_stack_secrets(name, kind=store.TASK_SECRETS_KIND)
        return self.cluster._with_sync({"deleted": name}, "tasks", name, everywhere,
                                       deleted=True)

    def set_enabled(self, name, enabled):
        task = {k: v for k, v in self._task(name).items() if k != "name"}
        return self.save_task(name, **dict(task, enabled=bool(enabled)))

    # -- secrets -----------------------------------------------------------
    #
    # As a stack's (see stacks.py), less the reveal: a task's secrets are only
    # ever handed to its job, so there is nothing a person needs to read back.

    def _declared(self, task, secret):
        found = self._task(task)
        if secret not in found["secrets"]:
            raise ServiceError("Task '%s' has no secret '%s'." % (task, secret), 404)
        return found

    def secret_status(self, task):
        self._task(task)
        return store.stack_secret_status(task, store.TASK_SECRETS_KIND)

    def set_secret(self, task, secret, value):
        self._declared(task, secret)
        if not isinstance(value, str) or not value:
            raise ServiceError("Give the secret a value.")
        if len(value) > store.STACK_VARIABLE_BYTES or "\x00" in value:
            raise ServiceError("A secret is text of at most %d bytes."
                               % store.STACK_VARIABLE_BYTES)
        store.set_stack_secret(task, secret, value, kind=store.TASK_SECRETS_KIND)
        synced = self.cluster._propagate(store.TASK_SECRETS_KIND, task)
        return dict(self.secret_status(task)[secret], name=secret, synced=synced)

    def clear_secret(self, task, secret):
        self._declared(task, secret)
        if not store.clear_stack_secret(task, secret, kind=store.TASK_SECRETS_KIND):
            raise ServiceError("Secret '%s' of task '%s' holds no value." % (secret, task), 404)
        synced = self.cluster._propagate(store.TASK_SECRETS_KIND, task)
        return {"name": secret, "cleared": True, "synced": synced}

    def adopt_secrets(self, task, body):
        """A member's whole set of values for one task, sealed here."""
        values = (body or {}).get("values") if isinstance(body, dict) else None
        if not isinstance(values, dict):
            raise ServiceError("values must be an object.")
        cleaned = {}
        for name, entry in values.items():
            if not isinstance(name, str) or not store.STACK_VALUE_NAME.match(name) \
                    or not isinstance(entry, dict) or not isinstance(entry.get("value"), str):
                raise ServiceError("Bad secret record '%s'." % name)
            cleaned[name] = {"value": entry["value"], "generated": False,
                             "updated_at": entry.get("updated_at")
                             if isinstance(entry.get("updated_at"), int) else None}
        store.replace_stack_secrets(task, cleaned, kind=store.TASK_SECRETS_KIND)
        return {"task": task, "secrets": sorted(cleaned)}

    # -- where a run may go ------------------------------------------------

    def eligible(self, task, groups=None):
        """The members a task may run on: its groups' members, and any nodes named beside.

        Never "every member" for a task that names nothing -- a record from
        before groups were required, or a hand-edited one -- since that is
        exactly the placement nobody chose. It runs nowhere and says so.
        """
        known = set(self.cluster.all_nodes())
        groups = store.load_node_groups() if groups is None else groups
        chosen = set(n for n in task["nodes"] if n in known)
        for group in task["groups"]:
            chosen.update(m for m in (groups.get(group) or {}).get("members", []) if m in known)
        return sorted(chosen)

    def declines(self, task=None):
        """Why this node will not take a run now, or None. Its own word, like maintenance."""
        mark = store.load_maintenance()
        if mark:
            return "this node is in maintenance%s" % (
                " (%s)" % mark["reason"] if mark.get("reason") else "")
        failures = store.load_launch_failures()
        if failures["count"] >= DECLINE_AFTER_FAILURES:
            return "this node's last %d launches failed" % failures["count"]
        with self._lock:
            live = len(self._live)
        if live >= MAX_LOCAL_RUNS:
            return "this node is already running %d task instances" % live
        return None

    def _running_anywhere(self, task):
        """A member (this one included) running ``task`` now, or None. Best effort."""
        with self._lock:
            if any(r["task"] == task and r["status"] == RUNNING for r in self._live.values()):
                return self.cluster.local_name()
        if not self.cluster.in_cluster():
            return None
        for run in self._peer_runs(task, live=True)[0]:
            return run["node"]
        return None

    # -- inputs ------------------------------------------------------------

    def _resolve_inputs(self, task, given):
        """``(values, public)`` for one run: every declared input, given or defaulted.

        ``public`` leaves the secret ones out, and is what the run record keeps.
        """
        if given in (None, ""):
            given = {}
        if not isinstance(given, dict):
            raise ServiceError("inputs must be an object of NAME: value.")
        declared = task["inputs"]
        unknown = sorted(set(map(str, given)) - set(declared))
        if unknown:
            raise ServiceError("Task '%s' has no input %s." % (
                task["name"], ", ".join("'%s'" % u for u in unknown)))
        values = {}
        for name, spec in declared.items():
            value = given.get(name)
            if value is None:
                value = spec["default"]
            elif isinstance(value, bool):
                value = "true" if value else "false"
            elif isinstance(value, (int, float, str)):
                value = str(value)
            else:
                raise ServiceError("Input '%s' is text, not %s." % (name, type(value).__name__))
            if len(value.encode("utf-8")) > store.TASK_INPUT_BYTES or "\x00" in value:
                raise ServiceError("Input '%s' is at most %d bytes of text."
                                   % (name, store.TASK_INPUT_BYTES))
            if spec["required"] and not value:
                raise ServiceError("Task '%s' needs input '%s'%s." % (
                    task["name"], name, ": %s" % spec["description"]
                    if spec["description"] else ""))
            values[name] = value
        public = {n: v for n, v in values.items() if not declared[n]["secret"]}
        return values, public

    @staticmethod
    def _payload_bytes(payload):
        if payload is None:
            return None
        data = payload if isinstance(payload, str) else json.dumps(payload)
        data = data.encode("utf-8")
        if len(data) > PAYLOAD_LIMIT:
            raise ServiceError("A payload is at most %d KiB." % (PAYLOAD_LIMIT // 1024))
        return data

    # -- invoking ----------------------------------------------------------

    def invoke(self, name, inputs=None, payload=None, node=None, wait=False, here=False):
        """Start one run of a task now, on the first node that will take it.

        ``node`` names where it must run instead; ``here`` is a member's
        forward of a choice already made, which runs on this node or refuses.
        Inputs and the payload are checked on the node asked, before anything
        is forwarded, so a mistake is reported once and by the right node.
        """
        task = self._task(name)
        self._resolve_inputs(task, inputs)
        self._payload_bytes(payload)
        local = self.cluster.local_name()
        if node and not here and node not in self.eligible(task):
            raise ServiceError("Task '%s' runs on %s; '%s' is not one of its nodes." % (
                name, ", ".join("group %s" % g for g in task["groups"]) or "no group", node),
                409)
        if node and node != local and not here:
            return self._forward(node, name, inputs, payload, wait)
        if here or node == local:
            return self._run_here(task, inputs, payload, wait)
        if task["overlap"] == "skip":
            busy = self._running_anywhere(name)
            if busy:
                raise ServiceError("Task '%s' is already running on %s, and its overlap "
                                   "setting is skip." % (name, busy), 409)
        eligible = self.eligible(task)
        if not eligible:
            raise ServiceError("Task '%s' has no node to run on: its node groups (%s) have no "
                               "members here." % (name, ", ".join(task["groups"]) or "none"),
                               409)
        refusals = []
        for candidate in rank(name, secrets_module.token_hex(8), eligible):
            if candidate == local:
                reason = self.declines(task)
                if reason is None:
                    return self._run_here(task, inputs, payload, wait, checked=True)
                refusals.append("%s: %s" % (candidate, reason))
                continue
            try:
                return self._forward(candidate, name, inputs, payload, wait)
            except (NodeError, ClusterError) as exc:
                refusals.append("%s: %s" % (candidate, exc.message))
        raise ServiceError("No node would take task '%s': %s." % (name, "; ".join(refusals)), 409)

    def _forward(self, node, name, inputs, payload, wait):
        task = store.load_tasks().get(name) or {}
        timeout = (task.get("timeout_seconds") or 900) + 120 if wait else 30
        return self.cluster.client(node, timeout=timeout).request(
            "POST", "/api/tasks/%s/invoke" % _seg(name),
            {"inputs": inputs or {}, "payload": payload, "here": True, "wait": bool(wait)})

    def _run_here(self, task, inputs, payload, wait, checked=False):
        if not checked:
            reason = self.declines(task)
            if reason:
                raise ServiceError("%s will not take it: %s." % (
                    self.cluster.local_name(), reason), 409)
        if task["overlap"] == "skip":
            with self._lock:
                busy = any(r["task"] == task["name"] and r["status"] == RUNNING
                           for r in self._live.values())
            if busy:
                raise ServiceError("Task '%s' is already running here, and its overlap "
                                   "setting is skip." % task["name"], 409)
        return self._start(task, "invoke", None, inputs, payload, wait)

    # -- negotiating a scheduled occurrence --------------------------------

    def grant(self, body):
        """This member's vote on one occurrence: to the first candidate that asks.

        ``{key, to, granted, veto, now}``. A veto is an answer, not an error:
        the candidate counts it as a vote withheld and reports the reason.
        """
        body = body if isinstance(body, dict) else {}
        key, candidate = str(body.get("key") or ""), str(body.get("candidate") or "")
        try:
            slot = int(body.get("slot"))
            sent_at = float(body.get("sent_at"))
        except (TypeError, ValueError):
            raise ServiceError("slot and sent_at must be numbers.")
        name = key.rpartition("@")[0]
        if not GRANT_KEY.match(key) or not candidate or not key.endswith("@%d" % slot):
            raise ServiceError("Not an occurrence this node can vote on.")
        now = time.time()
        veto = self._veto(name, slot, sent_at, now)
        if veto:
            eventlog.event("system", "task.veto", level=logging.WARNING, task=name, slot=slot,
                           candidate=candidate, skew=round(now - sent_at, 1), reason=veto)
            return {"key": key, "to": None, "granted": False, "veto": veto, "now": now}
        expires = slot + late_limit(len(self.cluster.all_nodes())) + GRANT_KEEP
        holder, _ = store.claim_task_grant(key, candidate, expires)
        return {"key": key, "to": holder, "granted": holder == candidate, "veto": None,
                "now": now}

    def _veto(self, name, slot, sent_at, now):
        """Why this member refuses a candidacy on timing, by its own clock and copy, or None."""
        skew = now - sent_at
        if abs(skew) > MAX_CLOCK_SKEW:
            return ("the candidate's clock is %.1f s %s this node's (at most %d s allowed)"
                    % (abs(skew), "behind" if skew > 0 else "ahead of", MAX_CLOCK_SKEW))
        task = store.load_tasks().get(name)
        if task is None:
            return "this node has no task '%s'" % name
        if not task["enabled"] or not task["schedule"]:
            return "task '%s' is %s here" % (name, "disabled" if not task["enabled"]
                                             else "not scheduled")
        try:
            schedule = cron.Schedule(task["schedule"]["cron"], task["schedule"]["timezone"])
            fires = schedule.next_after(slot - 1)
        except cron.CronError as exc:
            return "this node cannot read the task's schedule: %s" % exc
        if fires != slot:
            return ("%s is not an occurrence of '%s' (%s) as this node has it"
                    % (_when(slot), schedule.expression, schedule.timezone))
        if now < slot - MAX_CLOCK_SKEW:
            return "%s is not due yet by this node's clock (%d s early)" % (
                _when(slot), slot - now)
        late = late_limit(len(self.cluster.all_nodes()))
        if now > slot + late:
            return "%s was due %d s ago; every node's turn ended %d s after it" % (
                _when(slot), now - slot, late)
        return None

    def release_grant(self, body):
        body = body if isinstance(body, dict) else {}
        return {"released": store.release_task_grant(str(body.get("key") or ""),
                                                     str(body.get("candidate") or ""))}

    def _negotiate(self, task, slot):
        """``(won, holder, granted, voters)`` for this node standing for one occurrence."""
        me = self.cluster.local_name()
        voters = self.cluster.all_nodes()
        body = {"key": "%s@%d" % (task["name"], slot), "candidate": me, "slot": slot}

        def ask(node):
            # Stamped per call, so the time a request spends queued behind
            # others is not mistaken for this node's clock being behind.
            stamped = dict(body, sent_at=time.time())
            try:
                if node == me:
                    return self.grant(stamped)
                return self.cluster.client(node, timeout=GRANT_TIMEOUT).grant_task(stamped)
            except (ServiceError, NodeError, ClusterError, OSError):
                return None
        replies = dict(zip(voters, self.cluster._fanout(voters, ask)))
        answers = {n: (r or {}).get("to") for n, r in replies.items()}
        vetoes = {n: r["veto"] for n, r in replies.items() if r and r.get("veto")}
        granted = [n for n, holder in answers.items() if holder == me]
        holder = next((h for h in answers.values() if h and h != me), None)
        if vetoes:
            eventlog.event("system", "task.veto", level=logging.WARNING, task=task["name"],
                           slot=slot, candidate=me, vetoed_by=",".join(sorted(vetoes)),
                           reason="; ".join("%s: %s" % kv for kv in sorted(vetoes.items())))
        if len(granted) > len(voters) // 2:
            return True, me, len(granted), len(voters), vetoes

        def give_back(node):
            try:
                if node == me:
                    self.release_grant(body)
                else:
                    self.cluster.client(node, timeout=GRANT_TIMEOUT).release_task_grant(body)
            except (ServiceError, NodeError, ClusterError, OSError):
                pass
        # Every voter asked, not just those that answered yes: one whose yes
        # was lost in a timeout still holds a grant for this node.
        self.cluster._fanout([n for n in voters if answers[n] == me
                              or (answers[n] is None and n not in vetoes)], give_back)
        return False, holder, len(granted), len(voters), vetoes

    def _contend(self, task, slot):
        """Stand for one occurrence when this node's turn comes; run it if it wins."""
        me = self.cluster.local_name()
        eligible = self.eligible(task)
        if me not in eligible:
            return
        order = rank(task["name"], slot, eligible)
        ahead = [n for n in order[:order.index(me)] if not self.cluster.maintenance_of(n)]
        delay = slot + len(ahead) * HANDOFF_SECONDS - time.time()
        if delay > 0:
            time.sleep(delay)
        reason = self.declines(task)
        if reason:
            _log("%s at %s: declined, %s" % (task["name"], _when(slot), reason),
                 level=logging.DEBUG)
            return
        if task["overlap"] == "skip":
            busy = self._running_anywhere(task["name"])
            if busy:
                eventlog.event("system", "task.skip", task=task["name"], slot=slot,
                               running_on=busy)
                return
        won, holder, granted, voters, vetoes = self._negotiate(task, slot)
        if won:
            try:
                self._start(task, "schedule", slot, None, None, False)
            except ServiceError as exc:
                self._record_unstarted(task, slot, FAILED, exc.message)
            return
        if holder:
            _log("%s at %s: %s holds it" % (task["name"], _when(slot), holder),
                 level=logging.DEBUG)
            return
        if len(vetoes) > voters // 2:
            # Most members say this node is asking at the wrong time: its
            # clock is the odd one out, and every turn after this one is
            # judged by the same clocks, so it is recorded here and now.
            why = "vetoed on timing by %s" % "; ".join(
                "%s (%s)" % kv for kv in sorted(vetoes.items()))
            self._record_unstarted(task, slot, VETOED, why)
            return
        why = ("no majority of members granted it (%d of %d%s) -- a split cluster, or too "
               "many members down" % (granted, voters, ", %d vetoed" % len(vetoes)
                                      if vetoes else ""))
        if me == order[-1]:
            # The last to stand says it was missed, once, for everyone.
            eventlog.event("system", "task.missed", level=logging.WARNING,
                           task=task["name"], slot=slot, granted=granted, voters=voters)
            self._record_unstarted(task, slot, MISSED, why)
        else:
            _log("%s at %s: %s; the next node may still" % (task["name"], _when(slot), why),
                 level=logging.WARNING)

    def _record_unstarted(self, task, slot, status, error):
        now = time.time()
        store.save_task_run({
            "id": secrets_module.token_hex(6), "task": task["name"],
            "node": self.cluster.local_name(), "trigger": "schedule", "slot": slot,
            "status": status, "error": error, "started_at": now, "finished_at": now,
            "instance": None, "exit_code": None, "inputs": {}, "notes": [], "setup": None,
            "job": None, "pid": os.getpid(), "stage": "done", "payload_bytes": 0})

    # -- one run -----------------------------------------------------------

    def _start(self, task, trigger, slot, inputs, payload, wait):
        values, public = self._resolve_inputs(task, inputs)
        data = self._payload_bytes(payload)
        run_id = secrets_module.token_hex(6)
        instance = "%s-%s" % (("task-" + instance_prefix(task["name"]))[:50].rstrip("-"),
                              run_id[:6])
        job = self._job(task, run_id, trigger, slot, values, data is not None)
        run = {
            "id": run_id, "task": task["name"], "node": self.cluster.local_name(),
            "trigger": trigger, "slot": slot, "status": RUNNING, "stage": "creating",
            "error": None, "started_at": time.time(), "finished_at": None,
            "instance": instance, "exit_code": None, "inputs": public, "notes": [],
            "setup": None, "job": None, "pid": os.getpid(),
            "payload_bytes": len(data) if data is not None else 0,
        }
        with self._lock:
            self._live[run_id] = run
        store.save_task_run(dict(run))
        eventlog.event("system", "task.start", task=task["name"], run=run_id,
                       trigger=trigger, slot=slot, instance=instance)

        def work():
            self._execute(run, task, job, data)
        if wait:
            work()
            return self._snapshot(run)
        thread = threading.Thread(target=eventlog.carry(work), name="lemondx-task-%s" % run_id,
                                  daemon=True)
        with self._lock:
            self._threads[run_id] = thread
        thread.start()
        return self._snapshot(run)

    def _job(self, task, run_id, trigger, slot, values, has_payload):
        """``{modules, params, ssh_keys, hide}`` for the job pass, rendered, or raise."""
        if task.get("script"):
            return self._script_job(task, run_id, trigger, slot, values, has_payload)
        selection = task["bootstrap"]
        available = discover_modules()
        gone = [m for m in dict.fromkeys(selection["modules"]) if m not in available]
        if gone:
            raise ServiceError("Task '%s' uses module(s) that no longer exist: %s"
                               % (task["name"], ", ".join(gone)), 409)
        held = store.load_stack_secrets(task["name"], store.TASK_SECRETS_KIND)
        hide = [held[s]["value"] for s in task["secrets"] if s in held]
        hide += [values[n] for n, spec in task["inputs"].items() if spec["secret"] and values[n]]

        def fill(match):
            reference = match.group(1)
            found = INPUT_REFERENCE.match(reference)
            if found:
                return values.get(found.group(1), "")
            found = SECRET_REFERENCE.match(reference)
            if found and found.group(1) in held:
                return held[found.group(1)]["value"]
            raise ServiceError("Task '%s' has no value for secret '%s'; set it with "
                               "`lemondx task-secret-set`." % (task["name"], reference[8:]), 409)
        params = {key: PLACEHOLDER.sub(fill, value) for key, value in selection["params"].items()}
        missing = missing_secrets(available, selection["modules"], params)
        if missing:
            raise ServiceError("Task '%s' has nothing to give %s: set it to a task secret "
                               "or a secret input." % (task["name"], ", ".join(missing)), 409)
        params.update({
            "LEMONDX_TASK": task["name"], "LEMONDX_TASK_RUN": run_id,
            "LEMONDX_TASK_TRIGGER": trigger, "LEMONDX_TASK_SLOT": str(slot or ""),
            "LEMONDX_TASK_NODE": self.cluster.local_name(),
        })
        params.update({env_name(n): v for n, v in values.items()})
        if has_payload:
            params["LEMONDX_TASK_PAYLOAD"] = PAYLOAD_PATH
        return {"modules": selection["modules"], "params": params,
                "ssh_keys": selection["ssh_keys"], "hide": hide}

    def _script_job(self, task, run_id, trigger, slot, values, has_payload):
        """The job for a pasted script: everything it is given is its environment.

        Nothing is filled into the script's text -- a secret rendered into a
        file is a secret on disk -- so inputs and secrets arrive as variables,
        and every declared secret must hold a value, since nothing says which
        ones the script reads.
        """
        held = store.load_stack_secrets(task["name"], store.TASK_SECRETS_KIND)
        unset = sorted(s for s in task["secrets"] if s not in held)
        if unset:
            raise ServiceError("Task '%s' has no value for secret %s; set it with "
                               "`lemondx task-secret-set`." % (task["name"], ", ".join(unset)),
                               409)
        env = {"LEMONDX_TASK": task["name"], "LEMONDX_TASK_RUN": run_id,
               "LEMONDX_TASK_TRIGGER": trigger, "LEMONDX_TASK_SLOT": str(slot or ""),
               "LEMONDX_TASK_NODE": self.cluster.local_name(),
               # What the prelude, under `set -u`, expects every module to have.
               "LEMONDX_MODULE": "task-script", "LEMONDX_OCCURRENCE": "1"}
        env.update({env_name(n): v for n, v in values.items()})
        env.update({secret_env_name(s): held[s]["value"] for s in task["secrets"]})
        if has_payload:
            env["LEMONDX_TASK_PAYLOAD"] = PAYLOAD_PATH
        hide = [held[s]["value"] for s in task["secrets"]]
        hide += [values[n] for n, spec in task["inputs"].items() if spec["secret"] and values[n]]
        return {"modules": [], "script": task["script"], "env": env, "ssh_keys": [],
                "hide": hide}

    def _run_script(self, instance, script, env, timeout):
        """Run a job script in the instance; a result shaped like a bootstrap run's.

        Without a `#!` line it is a module in all but name: POSIX sh with the
        prelude's helpers (pkg_install and friends) in front. With one, it runs
        as written under that interpreter, which must exist in the image.
        """
        runner = BootstrapRunner(self.service.lxd)
        runner._wait_until_reachable(instance)
        own = script.startswith("#!")
        body = script if own else "%s\n\n# --- task script ---\n%s" % (load_prelude(), script)
        env = dict(env, LEMONDX_INSTANCE=instance)
        started = time.time()
        try:
            self.service.lxd.push_file(instance, SCRIPT_PATH, body, mode="0700")
            outcome = self.service.lxd.exec_command(
                instance, [SCRIPT_PATH] if own else ["/bin/sh", SCRIPT_PATH],
                environment=env, timeout=timeout)
        except LXDError as exc:
            outcome = {"exit_code": 1, "stdout": "", "stderr": exc.message}
        code = outcome.get("exit_code", 0)
        return {"ok": code == 0, "modules": [{
            "id": "script", "name": "Script", "occurrence": 1, "exit_code": code,
            "stdout": outcome.get("stdout", ""), "stderr": outcome.get("stderr", ""),
            "duration": round(time.time() - started, 1)}]}

    def _execute(self, run, task, job, payload):
        deadline = time.monotonic() + task["timeout_seconds"]
        timer = threading.Timer(task["timeout_seconds"], eventlog.carry(
            lambda: self._stop(run, TIMED_OUT, "Ran past its %d-second timeout."
                               % task["timeout_seconds"])))
        timer.daemon = True
        instance = run["instance"]
        status, error, exit_code = FAILED, None, None
        try:
            template, setup, _, _, _, notes = self.service.prepare_launch(
                task["template"], count=1, names=[instance])
            self._update(run, notes=notes)
            timer.start()
            placed = dict(template, ephemeral=True, start=True)
            # The job's keys, else the template's, for a job module that wants them.
            keys = job["ssh_keys"] or template["bootstrap"]["ssh_keys"]
            container = self.service.create_from_template(
                placed, instance, {TASK_CONFIG_KEY: task["name"],
                                   TASK_RUN_CONFIG_KEY: run["id"]}, setup)
            self._check(run)
            result = container.get("bootstrap")
            if result is not None:
                self._update(run, setup=_trim(result, job["hide"], keep_all=False))
                failed = next((m for m in result["modules"] if m["exit_code"]), None)
                if failed:
                    exit_code = failed["exit_code"]
                    raise ServiceError("Setup module %s failed (exit %d)."
                                       % (failed["name"], failed["exit_code"]))
            if payload is not None:
                self.service.lxd.push_file(instance, PAYLOAD_PATH, payload, mode="0600")
            self._update(run, stage="running")
            remaining = max(1, int(deadline - time.monotonic()))
            if job.get("script"):
                result = self._run_script(instance, job["script"], job["env"], remaining)
            else:
                result = self.service.bootstrap(
                    instance, modules=job["modules"], params=job["params"], ssh_keys=keys,
                    timeout=remaining, remember=False)
            self._check(run)
            self._update(run, job=_trim(result, job["hide"], keep_all=True))
            failed = next((m for m in result["modules"] if m["exit_code"]), None)
            exit_code = failed["exit_code"] if failed else 0
            status = OK if failed is None else FAILED
            if failed:
                error = ("The script exited %d." % failed["exit_code"] if job.get("script")
                         else "Job module %s exited %d." % (failed["name"], failed["exit_code"]))
        except _Stopped:
            pass
        except (ServiceError, LXDError, BootstrapError) as exc:
            error = getattr(exc, "message", None) or str(exc)
        except Exception as exc:                             # noqa: BLE001
            error = "Unexpected error: %s" % type(exc).__name__
            _log("%s: run %s failed unexpectedly: %r" % (task["name"], run["id"], exc),
                 level=logging.ERROR)
        finally:
            timer.cancel()
            self._update(run, stage="destroying")
            removed = self.service._remove_instance(instance)
            if not removed["ok"]:
                run["notes"] = run["notes"] + ["Could not destroy %s: %s; the sweeper will "
                                               "try again." % (instance, removed["error"])]
            with self._lock:
                stopped = run.get("stopped")
                if stopped:
                    status, error = stopped
                run.update(status=status, error=error and redact(error, job["hide"]),
                           exit_code=exit_code, finished_at=time.time(), stage="done")
                run.pop("stopped", None)
                self._live.pop(run["id"], None)
                self._threads.pop(run["id"], None)
                final = dict(run)
            store.save_task_run(final)
            eventlog.event("system", "task.finish",
                           level=logging.INFO if status == OK else logging.WARNING,
                           task=task["name"], run=run["id"], trigger=run["trigger"],
                           slot=run["slot"], status=status, exit_code=exit_code,
                           seconds=int(final["finished_at"] - final["started_at"]))

    def _update(self, run, **changes):
        with self._lock:
            run.update(changes)
            snapshot = dict(run)
        store.save_task_run(snapshot)

    def _check(self, run):
        with self._lock:
            if run.get("stopped"):
                raise _Stopped()

    def _stop(self, run, status, error):
        """End a run early: mark it, and destroy its instance, which ends whatever runs in it."""
        with self._lock:
            if run["finished_at"] is not None or run.get("stopped"):
                return False
            run["stopped"] = (status, error)
        self.service._remove_instance(run["instance"])
        return True

    def cancel_run(self, run_id):
        with self._lock:
            run = self._live.get(run_id)
        if run is None:
            raise ServiceError("No run '%s' going on this node." % run_id, 404)
        self._stop(run, CANCELLED, "Cancelled.")
        return self._snapshot(run)

    def cancel_all(self):
        """Stop every run in this process: `serve` is going away."""
        with self._lock:
            runs = list(self._live.values())
        for run in runs:
            self._stop(run, CANCELLED, "Stopped with lemondx serve.")
        return [r["id"] for r in runs]

    def pending_work(self):
        with self._lock:
            return ["task '%s' run %s (%s)" % (r["task"], r["id"], r["stage"])
                    for r in self._live.values()]

    def _snapshot(self, run):
        with self._lock:
            return {k: v for k, v in run.items() if k != "stopped"}

    # -- what has run ------------------------------------------------------

    def local_runs(self, task=None, live=False):
        """This node's runs: what is going now, over its history."""
        with self._lock:
            going = {r["id"]: {k: v for k, v in r.items() if k != "stopped"}
                     for r in self._live.values() if task is None or r["task"] == task}
        if live:
            return sorted(going.values(), key=lambda r: r["started_at"], reverse=True)
        runs = {r.get("id"): r for r in store.load_task_runs(task)}
        runs.update(going)
        return sorted(runs.values(), key=lambda r: r.get("started_at") or 0, reverse=True)

    def runs(self, task=None, local=False, live=False, detail=False):
        """Every member's runs of one task, or of all; ``local`` for this node's alone.

        Outputs are left out unless ``detail``: the list is polled.
        """
        found = self.local_runs(task, live)
        errors = []
        if not local and self.cluster.in_cluster():
            peer_runs, errors = self._peer_runs(task, live)
            found += peer_runs
        found.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
        found = found[:LISTED_RUNS]
        return {"runs": found if detail else [_brief(r) for r in found], "errors": errors}

    def _peer_runs(self, task, live):
        me = self.cluster.local_name()
        peers = [n for n in self.cluster.all_nodes() if n != me]
        params = {"local": "1"}
        if task:
            params["task"] = task
        if live:
            params["live"] = "1"

        def ask(node):
            try:
                answer = self.cluster.client(node, timeout=RUNS_TIMEOUT).request(
                    "GET", "/api/task-runs", params=params)
                return node, answer.get("runs") or [], None
            except (NodeError, ClusterError, OSError) as exc:
                return node, [], getattr(exc, "message", None) or str(exc)
        runs, errors = [], []
        for node, found, error in self.cluster._fanout(peers, ask):
            runs += [dict(r, node=r.get("node") or node) for r in found if isinstance(r, dict)]
            if error:
                errors.append({"node": node, "error": error})
        return runs, errors

    def run(self, run_id):
        """One run of this node's, outputs and all."""
        for run in self.local_runs():
            if run.get("id") == run_id:
                return run
        raise ServiceError("No run '%s' on this node." % run_id, 404)

    # -- the scheduler and the sweeper -------------------------------------

    def start(self):
        """Begin scheduling and sweeping, once per process. `serve` calls it."""
        if self._started:
            return
        self._started = True

        def run():
            with eventlog.system("tasks"):
                self._loop()
        threading.Thread(target=run, name="lemondx-tasks", daemon=True).start()

    def _scheduled(self):
        """Enabled tasks with a schedule, re-read only when the directory changes."""
        try:
            info = os.stat(store.tasks_dir())
            fingerprint = (info.st_ino, info.st_mtime_ns)
        except OSError:
            return {}
        seen, tasks = self._tasks_seen
        if fingerprint != seen:
            tasks = {}
            for name, task in store.load_tasks().items():
                if not task["enabled"] or not task["schedule"]:
                    continue
                try:
                    tasks[name] = (task, cron.Schedule(task["schedule"]["cron"],
                                                       task["schedule"]["timezone"]))
                except cron.CronError as exc:
                    _log("%s: schedule not used: %s" % (name, exc), level=logging.WARNING)
            self._tasks_seen = (fingerprint, tasks)
        return tasks

    def _loop(self):
        upcoming = {}            # task name -> (schedule signature, next slot)
        last_sweep = 0
        while True:
            try:
                now = time.time()
                if now - last_sweep >= SWEEP_SECONDS:
                    last_sweep = now
                    self.sweep()
                tasks = self._scheduled()
                for name in list(upcoming):
                    if name not in tasks:
                        del upcoming[name]
                for name, (task, schedule) in tasks.items():
                    signature = (schedule.expression, schedule.timezone)
                    entry = upcoming.get(name)
                    if entry is None or entry[0] != signature:
                        # Starting, or the schedule changed: from now, never
                        # catching up on what fell before.
                        upcoming[name] = (signature, schedule.next_after(now))
                        continue
                    slot = entry[1]
                    if now < slot:
                        continue
                    upcoming[name] = (signature, schedule.next_after(max(slot, now)))
                    if now - slot <= late_limit(len(self.cluster.all_nodes())):
                        threading.Thread(target=eventlog.carry(self._contend),
                                         args=(task, slot), daemon=True,
                                         name="lemondx-task-%s" % name).start()
            except Exception as exc:                         # noqa: BLE001
                # A chore thread that dies stops every schedule on this node
                # silently; a round that fails is only one round.
                _log("scheduler round failed: %r" % exc, level=logging.ERROR)
            time.sleep(TICK_SECONDS)

    def sweep(self):
        """Destroy task instances here that no live run owns, and close out lost runs.

        Each node owns its own instances, so this needs nobody's agreement. A
        run in another process still running (a CLI run without `serve`) is
        left alone: its history record names a live PID.
        """
        try:
            containers = self.service.list_containers()
        except (LXDError, ServiceError, OSError):
            return []
        me, own = self.cluster.local_name(), os.getpid()
        history = {r.get("id"): r for r in store.load_task_runs()}
        with self._lock:
            live = set(self._live)
        removed = []
        for container in containers:
            run_id = container.get("task_run")
            if not container.get("task") or not run_id or run_id in live:
                continue
            record = history.get(run_id) or {}
            if record.get("status") == RUNNING and record.get("pid") != own \
                    and _pid_alive(record.get("pid")):
                continue
            outcome = self.service._remove_instance(container["name"])
            if outcome["ok"]:
                removed.append(container["name"])
                eventlog.event("change", "task.sweep", task=container["task"], run=run_id,
                               instance=container["name"])
        for run_id, record in history.items():
            if record.get("status") == RUNNING and record.get("node") == me \
                    and run_id not in live and (record.get("pid") == own
                                                or not _pid_alive(record.get("pid"))):
                store.save_task_run(dict(record, status=LOST, stage="done",
                                         finished_at=time.time(),
                                         error="lemondx stopped while it ran."))
        return removed

    # -- schedules, for the editor -----------------------------------------

    @staticmethod
    def preview_schedule(expression, timezone="UTC", count=5):
        try:
            schedule = cron.Schedule(expression, timezone or "UTC")
            upcoming = schedule.upcoming(time.time(), max(1, min(int(count or 5), 20)))
        except (cron.CronError, ValueError) as exc:
            raise ServiceError(str(exc))
        return {"cron": schedule.expression, "timezone": schedule.timezone,
                "text": schedule.describe(), "next": upcoming}


def _seg(value):
    from urllib.parse import quote
    return quote(str(value), safe="")


def _when(slot):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(slot))


def _names(value, what):
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ServiceError("%s must be a list of names." % what)
    return list(dict.fromkeys(v.strip() for v in value if v.strip()))


def _check_schedule(raw):
    if raw in (None, "", {}):
        return None
    if isinstance(raw, str):
        raw = {"cron": raw}
    if not isinstance(raw, dict):
        raise ServiceError("schedule must be {cron, timezone}.")
    if not str(raw.get("cron") or "").strip():
        return None
    try:
        schedule = cron.Schedule(raw.get("cron"), str(raw.get("timezone") or "UTC").strip())
        schedule.next_after(time.time())
    except cron.CronError as exc:
        raise ServiceError(str(exc))
    return {"cron": schedule.expression, "timezone": schedule.timezone}


def _check_inputs(raw):
    if raw in (None, ""):
        return {}
    if isinstance(raw, list):
        raw = {name: {} for name in raw}
    if not isinstance(raw, dict):
        raise ServiceError("inputs must be an object of NAME: {description, default, "
                           "required, secret}.")
    if len(raw) > store.STACK_VALUES_MAX:
        raise ServiceError("A task has at most %d inputs." % store.STACK_VALUES_MAX)
    cleaned, seen = {}, {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not store.STACK_VALUE_NAME.match(name):
            raise ServiceError("Invalid input name '%s'. Use letters, digits and underscores, "
                               "starting with a letter or _." % name)
        if name.upper() in seen:
            raise ServiceError("Inputs '%s' and '%s' are both %s to a job."
                               % (seen[name.upper()], name, env_name(name)))
        seen[name.upper()] = name
        entry = entry if isinstance(entry, dict) else {}
        default = entry.get("default")
        if entry.get("secret") is True and default not in (None, ""):
            raise ServiceError("Input %s is secret, so it has no default: a default would be "
                               "saved in the clear. Declare a task secret instead." % name)
        if default is not None and len(str(default).encode("utf-8")) > store.TASK_INPUT_BYTES:
            raise ServiceError("Input %s's default is over %d bytes."
                               % (name, store.TASK_INPUT_BYTES))
        cleaned[name] = store._clean_task_input(entry)
    return cleaned


def _check_script(raw):
    """A pasted job script as stored, '' for none, or raise."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    if not isinstance(raw, str) or "\x00" in raw:
        raise ServiceError("A task's script is text.")
    if len(raw.encode("utf-8")) > store.TASK_SCRIPT_BYTES:
        raise ServiceError("A task's script is at most %d KiB." % (store.TASK_SCRIPT_BYTES // 1024))
    if not raw.startswith("#!"):
        # Checked here, not when it fails in an instance at two in the morning.
        try:
            check_shell_syntax(raw)
        except BootstrapError as exc:
            raise ServiceError(exc.message)
    return raw


def _check_secret_names(raw):
    if raw in (None, ""):
        return {}
    if isinstance(raw, list):
        raw = {name: {} for name in raw}
    if not isinstance(raw, dict):
        raise ServiceError("secrets must be an object of NAME: {description}.")
    cleaned = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not store.STACK_VALUE_NAME.match(name):
            raise ServiceError("Invalid secret name '%s'." % name)
        entry = entry if isinstance(entry, dict) else {}
        if "value" in entry:
            raise ServiceError("Secret %s: a task holds no secret values. Save the task, then "
                               "set it with `lemondx task-secret-set`." % name)
        clash = next((other for other in cleaned if other.upper() == name.upper()), None)
        if clash:
            raise ServiceError("Secrets '%s' and '%s' are both %s to a script."
                               % (clash, name, secret_env_name(name)))
        cleaned[name] = {"description": str(entry.get("description") or "")[:200]}
    return cleaned


def _trim(result, hide, keep_all):
    """A bootstrap result as a run keeps it: the job's output whole-ish, setup's in brief."""
    modules = []
    for m in result["modules"]:
        stdout = redact(m.get("stdout") or "", hide)
        stderr = redact(m.get("stderr") or "", hide)
        if keep_all:
            stdout, stderr = stdout[-OUTPUT_LIMIT:], stderr[-OUTPUT_LIMIT:]
        elif m["exit_code"]:
            stdout, stderr = stdout[-RUN_OUTPUT_TAIL:], stderr[-RUN_OUTPUT_TAIL:]
        else:
            stdout, stderr = _summary(stdout), ""
        modules.append({"id": m.get("id"), "name": m.get("name"),
                        "occurrence": m.get("occurrence", 1), "exit_code": m["exit_code"],
                        "duration": m.get("duration"), "stdout": stdout, "stderr": stderr})
    return {"ok": result["ok"], "modules": modules}


def _brief(run):
    """A run for the polled list: each module's last lines instead of its output."""
    def brief(part):
        if not part:
            return part
        return dict(part, modules=[
            dict({k: v for k, v in m.items() if k not in ("stdout", "stderr")},
                 summary=_summary(m.get("stdout") or m.get("stderr")))
            for m in part.get("modules") or []])
    return dict(run, setup=brief(run.get("setup")), job=brief(run.get("job")))


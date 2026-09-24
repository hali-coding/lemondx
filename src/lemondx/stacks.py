"""Stacks: templates launched in order, with pauses and health gates between.

A stack is a list of stages. The stages run one after another; the steps in a
stage run side by side. A step either launches a template (``launch``), waits
a fixed time (``sleep``), or waits until everything launched by earlier stages
is healthy (``wait_healthy``). "One database, then three app servers pointed at
it, then six load generators pointed at those" is three stages of one launch
each.

What a launch produced -- instance names, addresses, nodes -- is handed to the
launches after it two ways, so both a module written for stacks and one that
never heard of them can use it:

* every later launch's modules get ``LEMONDX_STACK_<STEP>_NAMES``, ``_IPS``,
  ``_IPV6`` and ``_NODES`` in their environment, space-separated, which is
  what a shell ``for`` loop wants;
* a launch step's own ``params`` may say ``{{db.ip}}`` or ``{{app.ips}}``,
  filled in when that step starts -- comma-separated, which is what a config
  file usually wants -- so an existing module's ``DB_HOST`` parameter can be
  pointed at the database without the module changing.

What was typed in when the stack was launched -- a database password, say --
is ``{{params.NAME}}``. That is the one way a step may set a *secret*
parameter: what is stored is the placeholder, never the value, so the rule that
secrets are never saved holds, and the receiving module still redacts it from
its output because it declared the parameter secret.

Every instance a stack launches is tagged ``user.lemondx.stack`` on the
instance itself, which is the whole of the state kept about what a stack is
running: it survives a restart of lemondx, it is where the instance is on
whichever node that is, and a recreate from the template keeps it. So stopping,
starting, destroying and relaunching a stack all start from the tag, like a
template's fleet actions do.

This sits above ``ClusterService`` rather than in ``ContainerService`` because
a launch step may name nodes and groups: every launch goes through
``ClusterService.launch_template()``, which takes the plain local path when the
only node is this one, so an unfederated lemondx never touches cluster code
here either. Each launch is an ordinary template run, so the Templates tab
shows it and the one-run-per-template lock still holds; a stack run is the
record *around* those, kept in memory like them (one per ``serve`` process).
"""

from __future__ import annotations

import ipaddress
import re
import sys
import threading
import time

from . import store
from .bootstrap import BootstrapError, discover_modules, secret_param_names
from .cluster import ClusterError
from .lxd import LXDError
from .nodeclient import NodeError
from .service import VALID_PREFIX, ServiceError

TEARDOWN = "_teardown"          # not a valid step id, so never a user's
STATE_ACTIONS = ("start", "stop", "restart")

STEP_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
REFERENCE = re.compile(r"^([a-z][a-z0-9_]{0,31})\.([a-z0-9_]+)$")
# A value supplied when the stack is launched. "params" is reserved as a step id.
INPUT_REFERENCE = re.compile(r"^params\.([A-Za-z_][A-Za-z0-9_]{0,63})$")
# What a placeholder may ask of an earlier launch. The plural ones are
# comma-separated, one entry per instance, in instance-name order.
FIELDS = ("names", "name", "ips", "ip", "ipv6", "nodes", "count")

MAX_STAGES = 50
MAX_STEPS_PER_STAGE = 20

# A template run lives in this process's memory, so following one is cheap.
RUN_POLL_SECONDS = 1
# Listing instances for readiness can mean asking every target node.
READY_POLL_SECONDS = 3
HEALTH_POLL_SECONDS = 5
# An instance that is running has usually got its address within seconds; a
# network that never hands one out should not hold a stack forever, and the
# instance is still worth passing on by name.
ADDRESS_GRACE_SECONDS = 60
# Instances launched without waiting for bootstrap: how long to wait for them
# to show up and start before giving up on the step.
READY_DEADLINE_SECONDS = 1800
# A one-off round (no monitor in this process) needs a CPU window.
ONE_OFF_WINDOW = 5

PENDING, RUNNING, READY, DONE, FAILED, SKIPPED, CANCELLED = (
    "pending", "running", "ready", "done", "failed", "skipped", "cancelled")


def _log(message):
    print("[lemondx] stack: %s" % message, file=sys.stderr, flush=True)


class _Cancelled(Exception):
    pass


class StackService:
    def __init__(self, cluster):
        self.cluster = cluster
        self.service = cluster.service
        # Handed back, so reconciliation saves a stack pulled from a member
        # through the same checks a person's save meets rather than dropping it
        # straight into the store. The dependency still only goes one way --
        # the cluster knows nothing about stacks beyond "something can save one".
        cluster._stacks = self
        # Stack runs in progress or last finished, by stack name.
        self._runs = {}
        self._lock = threading.Lock()

    # -- definitions -------------------------------------------------------

    def list_stacks(self):
        # `inputs` is derived, not stored: the values a launch must be given
        # because some step's parameter says {{params.NAME}}.
        stored = [stack for _, stack in sorted(store.load_stacks().items())]
        return [dict(stack, inputs=stack_inputs(stack))
                for stack in self._public(stored)]

    def get_stack(self, name):
        stack = store.load_stacks().get(name)
        if stack is None:
            raise ServiceError("No such stack '%s'." % name, 404)
        return self._public([stack])[0]

    def _public(self, stacks):
        """Stored stacks as everything but the store may see them.

        Takes the whole listing so the templates and modules are read once for
        it rather than once a stack, and not at all unless some step has
        parameters -- which one carrying a literal secret must have.
        """
        if not any(step.get("params") for stack in stacks
                   for stage in stack["stages"] for step in stage["steps"]):
            return stacks
        templates, modules = store.load_templates(), discover_modules()
        return [public_stack(stack, templates, modules) for stack in stacks]

    def save_stack(self, name, stages=None, description="", propagate=True):
        name = self.service._record_name(name, "stack")
        cleaned = self._check_stages(stages)
        saved = store.save_stack(name, {"description": str(description or "")[:200],
                                        "stages": cleaned})
        return self.cluster._with_sync(saved, "stacks", saved["name"], propagate)

    def delete_stack(self, name, everywhere=True):
        with self._lock:
            run = self._runs.get(name)
            if run and run["finished_at"] is None:
                raise ServiceError("Stack '%s' is running; cancel it first." % name, 409)
        if not store.delete_stack(name):
            raise ServiceError("No such stack '%s'." % name, 404)
        return self.cluster._with_sync({"deleted": name}, "stacks", name, everywhere,
                                       deleted=True)

    def _check_stages(self, stages):
        """The stages as they will be stored, or raise with what is wrong.

        The store would quietly drop what it cannot keep, which is right for a
        file copied in by hand; a save is told instead.
        """
        if not isinstance(stages, list) or not stages:
            raise ServiceError("A stack needs at least one stage with a step in it.")
        if len(stages) > MAX_STAGES:
            raise ServiceError("A stack has at most %d stages." % MAX_STAGES)
        templates = store.load_templates()
        available = discover_modules()
        seen, earlier_launches, cleaned = set(), set(), []
        position = 0
        for index, stage in enumerate(stages, 1):
            steps = stage.get("steps") if isinstance(stage, dict) else stage
            if not isinstance(steps, list) or not steps:
                raise ServiceError("Stage %d has no steps." % index)
            if len(steps) > MAX_STEPS_PER_STAGE:
                raise ServiceError("Stage %d has more than %d steps."
                                   % (index, MAX_STEPS_PER_STAGE))
            stage_steps, stage_templates, stage_launches = [], set(), set()
            for raw in steps:
                position += 1
                if not isinstance(raw, dict):
                    raise ServiceError("Stage %d: each step must be an object." % index)
                raw = dict(raw)
                # A hand-written file may leave ids out; nothing refers to a
                # step without one, so any unique id will do.
                if not raw.get("id"):
                    raw["id"] = "step%d" % position
                step_id = str(raw["id"]).strip()
                if not STEP_ID.match(step_id) or step_id == "params":
                    raise ServiceError(
                        "Invalid step id '%s'. Use lowercase letters, digits and "
                        "underscores, starting with a letter (max 32)." % step_id)
                if step_id in seen:
                    raise ServiceError("Two steps are called '%s'; ids must be unique."
                                       % step_id)
                seen.add(step_id)
                raw["id"] = step_id
                kind = raw.get("type")
                if kind == "launch":
                    self._check_launch(raw, templates, available, earlier_launches)
                    if raw["template"] in stage_templates:
                        # Each launch is that template's one run; two at once
                        # would have the second refused as soon as it started.
                        raise ServiceError(
                            "Stage %d launches '%s' twice. Put one in a later stage, or "
                            "raise its count." % (index, raw["template"]))
                    stage_templates.add(raw["template"])
                    stage_launches.add(step_id)
                elif kind == "sleep":
                    self._check_seconds(raw, "seconds", "sleep", store.STACK_SLEEP)
                elif kind == "wait_healthy":
                    if not earlier_launches:
                        raise ServiceError(
                            "Step '%s' waits for earlier launches to be healthy, but no "
                            "stage before it launches anything." % step_id)
                    self._check_seconds(raw, "timeout_seconds", "health wait",
                                        store.STACK_WAIT)
                else:
                    raise ServiceError("Step '%s' has unknown type '%s'. Try: %s."
                                       % (step_id, kind, ", ".join(store.STACK_STEP_TYPES)))
                stage_steps.append(raw)
            earlier_launches |= stage_launches
            cleaned.append({"steps": stage_steps})
        # Through the store's own cleaner, so what is returned is exactly what
        # a later read will give back.
        return store._clean_stack({"stages": cleaned}, "")["stages"]

    def _check_launch(self, step, templates, available, earlier):
        step_id = step["id"]
        template = templates.get(str(step.get("template") or "").strip())
        if template is None:
            raise ServiceError("Step '%s' launches template '%s', which does not exist."
                               % (step_id, step.get("template") or ""), 409)
        step["template"] = template["name"]
        low, high, _ = store.STACK_LAUNCH_COUNT
        count = step.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, (int, float, str)):
            count = None
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = None
        if count is None or not low <= count <= high:
            raise ServiceError("Step '%s': launch between %d and %d instances."
                               % (step_id, low, high))
        step["count"] = count
        prefix = str(step.get("prefix") or "").strip()
        if prefix and not VALID_PREFIX.match(prefix):
            raise ServiceError(
                "Step '%s': invalid name prefix '%s'. Use letters, digits and dashes, "
                "starting with a letter (max 50 chars)." % (step_id, prefix))
        params = step.get("params") or {}
        if not isinstance(params, dict):
            raise ServiceError("Step '%s': params must be an object." % step_id)
        secrets = secret_param_names([available[m] for m in
                                      template["bootstrap"]["modules"] if m in available])
        for key, value in params.items():
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise ServiceError("Step '%s': the value of %s must be text."
                                   % (step_id, key))
            value = str(value)
            if key in secrets and not _input_only(value):
                raise ServiceError(
                    "Step '%s' sets %s, a secret. Secrets are never saved: give it "
                    "{{params.NAME}} to use a value entered at launch." % (step_id, key))
            for reference in PLACEHOLDER.findall(value):
                if INPUT_REFERENCE.match(reference):
                    continue
                match = REFERENCE.match(reference)
                if not match or match.group(2) not in FIELDS:
                    raise ServiceError(
                        "Step '%s', %s: '{{%s}}' is not a reference. Use {{params.NAME}}, "
                        "or {{step.field}} with one of: %s."
                        % (step_id, key, reference, ", ".join(FIELDS)))
                if match.group(1) not in earlier:
                    raise ServiceError(
                        "Step '%s', %s: '{{%s}}' names '%s', which is not a launch in an "
                        "earlier stage." % (step_id, key, reference, match.group(1)))
        step["params"] = {str(k): str(v) for k, v in params.items()}
        for key in ("nodes", "groups"):
            value = step.get(key) or []
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ServiceError("Step '%s': %s must be a list of names." % (step_id, key))
        step["wait_bootstrap"] = step.get("wait_bootstrap", True) is not False

    @staticmethod
    def _check_seconds(step, key, label, bounds):
        low, high, default = bounds
        value = step.get(key, default)
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = None
        if isinstance(value, bool) or number is None or not number.is_integer() \
                or not low <= number <= high:
            raise ServiceError("Step '%s': the %s must be a whole number of seconds from "
                               "%d to %d." % (step["id"], label, low, high))
        step[key] = int(number)

    # -- launching ---------------------------------------------------------

    def launch_stack(self, name, params=None, background=False, replace=None):
        """Run a stack, stage by stage.

        Everything that would stop it partway for a reason known now -- a
        template gone, a missing secret, a node that is not a member, a
        template already busy -- is refused before anything is launched. After
        that a failure stops the stack from starting any further stage; what
        was launched stays, as it does for a template launch.

        With ``background`` the run record comes back at once and the stack
        runs on a thread, which is how the web UI starts one.

        ``replace`` makes it a relaunch: the stack's instances, exactly as the
        caller confirmed them, are destroyed first, as the run's first stage --
        and only after every check above has passed, so a relaunch refused for
        a missing secret has not already destroyed anything.
        """
        stack = self.get_stack(name)
        if not stack["stages"]:
            raise ServiceError("Stack '%s' has no steps." % name, 409)
        if params is not None and not isinstance(params, dict):
            raise ServiceError("params must be an object.")
        params = {str(k): str(v) for k, v in (params or {}).items()}
        missing = [n for n in stack_inputs(stack) if not params.get(n)]
        if missing:
            raise ServiceError("Stack '%s' needs %s when it is launched."
                               % (name, " and ".join(missing)))
        templates = {t["name"]: t for t in self.service.list_templates()}
        busy = {r["template"] for r in self.service.template_runs()
                if r["finished_at"] is None}
        for stage in stack["stages"]:
            for step in stage["steps"]:
                if step["type"] != "launch":
                    continue
                template = templates.get(step["template"])
                if template is None:
                    raise ServiceError("Step '%s' launches template '%s', which no longer "
                                       "exists." % (step["id"], step["template"]), 409)
                if template["name"] in busy:
                    raise ServiceError("Template '%s' is busy with another run; wait for it "
                                       "to finish." % template["name"], 409)
                # Placeholders become addresses later; for checking, what
                # matters is that the parameter will have a value.
                self.service._launch_bootstrap(template, dict(
                    params, **{k: v or "-" for k, v in step["params"].items()}))
                try:
                    # Maintenance included: a relaunch refused here has not
                    # torn anything down yet.
                    self.cluster.launch_targets(step["nodes"], step["groups"])
                except ClusterError as exc:
                    raise ServiceError("Step '%s': %s" % (step["id"], exc.message), exc.code)

        plan = list(stack["stages"])
        action = "launch"
        if replace is not None:
            doomed = self._confirmed(name, replace)
            action = "relaunch"
            if doomed:
                plan.insert(0, {"steps": [self._teardown_step(doomed)]})
        return self._start(name, action, plan, params, background)

    def destroy_stack(self, name, instances, background=False):
        """Stop and delete every instance the stack launched, as confirmed."""
        doomed = self._confirmed(name, instances)
        if not doomed:
            raise ServiceError("Stack '%s' has no instances." % name, 404)
        return self._start(name, "destroy", [{"steps": [self._teardown_step(doomed)]}],
                           {}, background)

    @staticmethod
    def _teardown_step(doomed):
        return {"id": TEARDOWN, "type": "destroy", "instances": doomed}

    def _start(self, name, action, plan, params, background):
        with self._lock:
            current = self._runs.get(name)
            if current and current["finished_at"] is None:
                raise ServiceError("Stack '%s' is already running." % name, 409)
            run = {
                "stack": name, "action": action, "started_at": time.time(),
                "finished_at": None, "ok": None, "error": None, "cancelling": False,
                "cancelled": False,
                "stages": [{"steps": [self._step_record(s) for s in stage["steps"]]}
                           for stage in plan],
            }
            self._runs[name] = run

        def work():
            self._execute(run, plan, params)
            return self._snapshot(run)

        if background:
            threading.Thread(target=self._guarded, args=(run, work),
                             name="lemondx-stack-%s" % name, daemon=True).start()
            return self._snapshot(run)
        return self._guarded(run, work)

    def _guarded(self, run, work):
        try:
            return work()
        except Exception as exc:                          # noqa: BLE001
            _log("'%s' stopped: %s" % (run["stack"], exc))
            with self._lock:
                run.update(ok=False, error=run["error"] or "Unexpected error: %s" % exc,
                           finished_at=run["finished_at"] or time.time())
            return self._snapshot(run)

    @staticmethod
    def _step_record(step):
        record = {"id": step["id"], "type": step["type"], "state": PENDING,
                  "started_at": None, "finished_at": None, "until": None,
                  "detail": None, "error": None}
        if step["type"] == "launch":
            record.update(template=step["template"], count=step["count"],
                          wait_bootstrap=step["wait_bootstrap"], nodes=[], instances=[],
                          outputs=None)
        elif step["type"] == "destroy":
            record.update(count=len(step["instances"]),
                          instances=[dict(i, ok=None, error=None) for i in step["instances"]])
        return record

    def _update(self, record, **changes):
        with self._lock:
            record.update(changes)

    def _snapshot(self, run):
        with self._lock:
            return _copy(run)

    def _cancelling(self, run):
        with self._lock:
            return run["cancelling"]

    def _sleep(self, run, seconds):
        """Sleep, but wake to notice a cancel within a second."""
        deadline = time.monotonic() + seconds
        while True:
            if self._cancelling(run):
                raise _Cancelled()
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(1.0, left))

    def _execute(self, run, plan, params):
        # What each finished launch produced, by step id; what later launches
        # are handed. Launches that did not wait for bootstrap are still going
        # on followers; anything that depends on them joins them first.
        context = {}
        followers = []            # (step id, template, thread)
        stopped = None
        for index, stage in enumerate(plan):
            records = run["stages"][index]["steps"]
            if stopped is None and self._cancelling(run):
                stopped = CANCELLED
            if stopped is None and self._any_failed(run):
                stopped = SKIPPED
            if stopped is not None:
                for record in records:
                    self._update(record, state=stopped)
                continue
            earlier = [(s["id"], s) for past in plan[:index]
                       for s in past["steps"] if s["type"] == "launch"]
            threads = []
            for step, record in zip(stage["steps"], records):
                thread = threading.Thread(
                    target=self._run_step,
                    args=(run, step, record, context, params, earlier, followers),
                    name="lemondx-stack-step-%s" % step["id"], daemon=True)
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()

        # The run is not over while a launch is still bootstrapping.
        for _, _, thread in list(followers):
            thread.join()
        with self._lock:
            states = [s["state"] for stage in run["stages"] for s in stage["steps"]]
            run["cancelled"] = run["cancelling"] and CANCELLED in states
            run["ok"] = all(state == DONE for state in states)
            if run["cancelled"]:
                run["error"] = "Cancelled."
            elif not run["ok"]:
                failed = [s for stage in run["stages"] for s in stage["steps"]
                          if s["state"] == FAILED]
                run["error"] = "; ".join("%s: %s" % (s["id"], s["error"] or "failed")
                                         for s in failed) or None
            run["finished_at"] = time.time()
        _log("'%s' %s %s" % (run["stack"], run["action"], "finished" if run["ok"]
                          else "cancelled" if run["cancelled"] else "failed"))

    def _any_failed(self, run):
        with self._lock:
            return any(s["state"] == FAILED for stage in run["stages"]
                       for s in stage["steps"])

    def _run_step(self, run, step, record, context, params, earlier, followers):
        self._update(record, state=RUNNING, started_at=time.time())
        try:
            if step["type"] == "sleep":
                self._update(record, until=time.time() + step["seconds"],
                             detail="Waiting %ds" % step["seconds"])
                self._sleep(run, step["seconds"])
                self._update(record, state=DONE, finished_at=time.time(), detail=None)
            elif step["type"] == "wait_healthy":
                self._wait_healthy(run, step, record, earlier, followers)
            elif step["type"] == "destroy":
                self._teardown(step, record)
            else:
                self._launch(run, step, record, context, params, followers)
        except _Cancelled:
            self._update(record, state=CANCELLED, finished_at=time.time(),
                         detail=None, until=None)
        except (ServiceError, ClusterError, LXDError, BootstrapError, NodeError) as exc:
            self._update(record, state=FAILED, finished_at=time.time(),
                         error=str(getattr(exc, "message", None) or exc), until=None)
        except Exception as exc:                          # noqa: BLE001
            _log("step '%s' of '%s' failed: %r" % (step["id"], run["stack"], exc))
            self._update(record, state=FAILED, finished_at=time.time(),
                         error="Unexpected error: %s" % exc, until=None)

    # -- launch steps ------------------------------------------------------

    def _launch(self, run, step, record, context, params, followers):
        name = step["template"]
        # An earlier launch of the same template may still be bootstrapping;
        # its run holds the template until then.
        for _, template, thread in list(followers):
            if template == name:
                self._update(record, detail="Waiting for the earlier launch of '%s'" % name)
                while thread.is_alive():
                    self._sleep(run, RUN_POLL_SECONDS)
        if self._cancelling(run):
            raise _Cancelled()

        with self._lock:
            done = dict(context)
        values = dict(_exports(run["stack"], done), **params)
        values.update({k: _render(v, done, params) for k, v in step["params"].items()})
        # Where it will actually go: a group member in maintenance is skipped.
        targets, _ = self.cluster.launch_targets(step["nodes"], step["groups"])
        before = self._template_keys(name, targets)
        self._update(record, nodes=targets, detail="Launching %d on %s" % (
            step["count"], ", ".join(targets)))
        started = self.cluster.launch_template(
            name, count=step["count"], nodes=step["nodes"] or None,
            groups=step["groups"] or None, params=values,
            prefix=step["prefix"] or None, background=True, stack=run["stack"])

        if step["wait_bootstrap"]:
            finished = self._await_template_run(name, started)
            self._finish_launch(step, record, finished, targets, context)
            return

        keys = self._await_ready(run, name, started, targets, before, step["count"], record)
        outputs, addresses = self._outputs(keys, targets)
        with self._lock:
            context[step["id"]] = outputs
            record.update(state=READY, outputs=outputs,
                          instances=[{"name": n, "node": node, "ok": None, "error": None,
                                      "ipv4": addresses.get((node, n))}
                                     for node, n in keys],
                          detail="Running; bootstrap carries on in the background")

        def follow():
            try:
                finished = self._await_template_run(name, started)
                self._finish_launch(step, record, finished, targets, None)
            except (ServiceError, ClusterError) as exc:
                self._update(record, state=FAILED, finished_at=time.time(),
                             error=str(getattr(exc, "message", None) or exc))
            except Exception as exc:                      # noqa: BLE001
                self._update(record, state=FAILED, finished_at=time.time(),
                             error="Unexpected error: %s" % exc)

        thread = threading.Thread(target=follow, daemon=True,
                                  name="lemondx-stack-follow-%s" % step["id"])
        with self._lock:
            followers.append((step["id"], name, thread))
        thread.start()

    def _finish_launch(self, step, record, finished, targets, context):
        """Record how a launch's template run ended; ``context`` None keeps outputs."""
        local = self.cluster.local_name()
        result = finished.get("result") or {}
        instances = [{"name": i.get("name"), "node": i.get("node") or local,
                      "ok": bool(i.get("ok")), "error": _instance_error(i)}
                     for i in result.get("instances") or []]
        failed = [i for i in instances if not i["ok"]]
        error = finished.get("error") or (
            "%d of %d failed: %s" % (len(failed), len(instances), "; ".join(
                "%s: %s" % (i["name"], i["error"] or "failed") for i in failed))
            if failed else None)
        if not instances and not error:
            error = "The launch created nothing."
        changes = {"instances": instances, "error": error,
                   "state": FAILED if error else DONE, "finished_at": time.time(),
                   "detail": "; ".join(result.get("notes") or finished.get("notes") or [])
                   or None}
        if context is not None:
            keys = [(i["node"], i["name"]) for i in instances if i["ok"]]
            outputs, addresses = self._outputs(keys, targets, settle=not error)
            changes["outputs"] = outputs
            with self._lock:
                context[step["id"]] = outputs
        else:
            # The follower keeps the outputs taken when the instances came up,
            # so it keeps the addresses that were taken with them rather than
            # listing every node again to learn the same thing.
            with self._lock:
                addresses = {(i["node"], i["name"]): i.get("ipv4")
                             for i in record.get("instances") or []}
        for instance in instances:
            instance["ipv4"] = addresses.get((instance["node"], instance["name"]))
        self._update(record, **changes)

    def _await_template_run(self, template, started):
        """Follow the template run a launch step started until it finishes."""
        if started.get("finished_at") is not None:
            return started
        marker = started.get("started_at")
        while True:
            time.sleep(RUN_POLL_SECONDS)
            run = next((r for r in self.service.template_runs()
                        if r["template"] == template and r["started_at"] == marker), None)
            if run is None:
                raise ServiceError("The launch of '%s' stopped being reported; its result "
                                   "was dismissed before the stack read it." % template, 409)
            if run["finished_at"] is not None:
                return run

    def _template_keys(self, template, targets):
        return {(c["node"], c["name"]) for c in self._listing(targets)
                if c.get("template") == template}

    def _listing(self, targets):
        return self.cluster.containers(nodes=targets)["instances"]

    def _await_ready(self, run, template, started, targets, before, count, record):
        """The new instances once each is running, for a launch not waiting on bootstrap.

        "New" is whatever carries the template's tag now and did not before:
        the template's one-run lock means nothing else is creating from it
        meanwhile. If the launch finishes first -- a fast one, or one that
        failed -- its result says what there is.
        """
        deadline = time.monotonic() + READY_DEADLINE_SECONDS
        marker = started.get("started_at")
        while True:
            self._sleep(run, READY_POLL_SECONDS)
            current = next((r for r in self.service.template_runs()
                            if r["template"] == template and r["started_at"] == marker), None)
            if current is None or current["finished_at"] is not None:
                result = (current or {}).get("result") or {}
                local = self.cluster.local_name()
                good = [(i.get("node") or local, i["name"])
                        for i in result.get("instances") or [] if i.get("ok")]
                if not good:
                    raise ServiceError((current or {}).get("error") or
                                       "The launch of '%s' created nothing." % template)
                return good
            fresh = [c for c in self._listing(targets)
                     if c.get("template") == template and (c["node"], c["name"]) not in before]
            running = [c for c in fresh if c["status"] == "Running"]
            self._update(record, detail="%d of %d running" % (len(running), count))
            if len(running) >= count:
                return sorted((c["node"], c["name"]) for c in running)
            if time.monotonic() > deadline:
                raise ServiceError("Only %d of %d instances were running after %d minutes."
                                   % (len(running), count, READY_DEADLINE_SECONDS // 60))

    def _outputs(self, keys, targets, settle=True):
        """What a launch hands on, and each instance's address beside it.

        Waits a little for a running instance still without an IPv4 address,
        since DHCP usually lands seconds after the start, and passes it on by
        name alone if it never does.

        The returned lists are *values for placeholders*, not a table with one
        row per instance: `ips` leaves out an instance that has no address
        because `{{db.ips}}` usually ends up in a config file, where a trailing
        empty entry is a broken line, and `nodes` is deduplicated because
        `{{db.nodes}}` is usually a shell `for` loop that wants each node once.
        So nothing may read them positionally -- which is what the second
        return value is for: (node, name) -> address, for the run record, where
        each instance is its own row and its own address belongs to it.
        """
        wanted = set(keys)
        deadline = time.monotonic() + (ADDRESS_GRACE_SECONDS if settle else 0)
        while True:
            found = {(c["node"], c["name"]): c for c in self._listing(targets)
                     if (c["node"], c["name"]) in wanted}
            waiting = [c for c in found.values()
                       if c["status"] == "Running" and not c.get("ipv4")]
            if not waiting or time.monotonic() > deadline:
                break
            time.sleep(READY_POLL_SECONDS)
        ordered = sorted(found.values(), key=lambda c: _natural(c["name"]))
        reachable = self._reachable_address
        return {
            "names": [c["name"] for c in ordered],
            "ips": [reachable(c) for c in ordered if reachable(c)],
            "ipv6": [c["ipv6"][0] for c in ordered if c.get("ipv6")],
            "nodes": sorted({c["node"] for c in ordered}),
        }, {(c["node"], c["name"]): reachable(c) for c in ordered}

    def _reachable_address(self, container):
        """The address of an instance that a step on another node can reach.

        An instance on the fabric has two: the NAT'd one on its own node's
        bridge, which means nothing anywhere else, and one on the fabric. A
        stack exists to let a later step talk to an earlier one, and a step may
        land on any node, so the fabric address is the one worth handing on --
        picking the first would hand on whichever the daemon happened to list
        first and work only by luck.
        """
        addresses = container.get("ipv4") or []
        prefixes = self._fabric_prefixes()
        for address in addresses:
            try:
                if any(ipaddress.ip_address(address) in p for p in prefixes):
                    return address
            except ValueError:
                continue
        return addresses[0] if addresses else None

    def _fabric_prefixes(self):
        """Every fabric prefix this node is on; [] when it is on none.

        Asked each time rather than kept: a fabric created after `serve`
        started must count, and the settings behind it are already cached
        until the file changes.
        """
        try:
            return self.cluster.fabric().prefixes()
        except Exception:
            return []

    # -- the instances a stack is running ------------------------------------

    def stack_instances(self):
        """Every tagged instance on every node, by stack, plus nodes that did not answer.

        Read from the instances' own tags each time -- there is no list kept
        anywhere else to fall out of step with them.
        """
        listing = self.cluster.containers(everything=True)
        stacks = {}
        for c in listing["instances"]:
            if c.get("stack"):
                stacks.setdefault(c["stack"], []).append({
                    "node": c["node"], "name": c["name"], "status": c["status"],
                    "template": c.get("template"), "ipv4": c.get("ipv4") or [],
                    "stale": c.get("stale") or []})
        for members in stacks.values():
            members.sort(key=lambda c: (_natural(c["name"]), c["node"]))
        return {"stacks": stacks, "errors": listing["errors"]}

    def _members(self, name):
        found = self.stack_instances()
        if found["errors"]:
            raise ServiceError(
                "Cannot tell which instances belong to '%s': %s. Nothing was changed."
                % (name, "; ".join("%s: %s" % (e["node"], e["error"])
                                   for e in found["errors"])), 502)
        return found["stacks"].get(name, [])

    def _confirmed(self, name, confirmed):
        """The stack's instances, provided they are exactly what the caller showed.

        As with a template's destroy: acting on the tag alone would also take
        out an instance launched after the user looked.
        """
        if not isinstance(confirmed, list):
            raise ServiceError("List the instances to act on in 'instances', as confirmed.")
        local = self.cluster.local_name()
        wanted = set()
        for entry in confirmed:
            if isinstance(entry, str):
                entry = {"node": local, "name": entry}
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise ServiceError("Each instance needs a name and the node it is on.")
            wanted.add((str(entry.get("node") or local), entry["name"]))
        members = self._members(name)
        current = {(m["node"], m["name"]) for m in members}
        if wanted != current:
            raise ServiceError(
                "The instances of stack '%s' have changed since they were confirmed "
                "(now: %s). Nothing was changed; review and try again."
                % (name, ", ".join(_label(k, current) for k in sorted(current)) or "none"),
                409)
        return [{"node": m["node"], "name": m["name"]} for m in members]

    def stack_state(self, name, action, instances):
        """Start, stop or restart every instance of a stack, wherever it is."""
        if action not in STATE_ACTIONS:
            raise ServiceError("Unknown action '%s'. Try: %s."
                               % (action, ", ".join(STATE_ACTIONS)))
        with self._lock:
            run = self._runs.get(name)
            if run and run["finished_at"] is None:
                raise ServiceError("Stack '%s' is running; wait for it or cancel it." % name,
                                   409)
        members = self._confirmed(name, instances)
        if not members:
            raise ServiceError("Stack '%s' has no instances." % name, 404)
        result = self.cluster.change_state(members, action)
        return dict(result, stack=name, action=action)

    def _teardown(self, step, record):
        doomed = step["instances"]
        self._update(record, detail="Destroying %d" % len(doomed))
        results = self.cluster.delete_containers(doomed, force=True)["instances"]
        # An ephemeral instance deletes itself when stopped; gone is the goal.
        for r in results:
            if not r["ok"] and "not found" in (r["error"] or "").lower():
                r.update(ok=True, error=None)
        failed = [r for r in results if not r["ok"]]
        self._update(
            record, instances=[{"node": r["node"], "name": r["name"], "ok": r["ok"],
                                "error": r["error"]} for r in results],
            state=FAILED if failed else DONE, finished_at=time.time(), detail=None,
            error="Could not destroy %s" % "; ".join(
                "%s: %s" % (r["name"], r["error"]) for r in failed) if failed else None)

    # -- health gates ------------------------------------------------------

    def _wait_healthy(self, run, step, record, earlier, followers):
        """Wait until every instance earlier stages launched is healthy.

        Healthy means green on the node it lives on: its own monitor's latest
        record says ``healthy``, and its template's app check, if it has one,
        has answered ``ok`` rather than still being pending. Launches that did
        not wait for their bootstrap are waited for here first: an instance
        whose modules are still installing its application is not ready in
        the sense anyone waiting on health means.
        """
        ids = {step_id for step_id, _ in earlier}
        waiting_on = [t for step_id, _, t in list(followers) if step_id in ids]
        if any(t.is_alive() for t in waiting_on):
            self._update(record, detail="Waiting for earlier launches to finish bootstrapping")
            while any(t.is_alive() for t in waiting_on):
                self._sleep(run, RUN_POLL_SECONDS)
        with self._lock:
            launched = [s for stage in run["stages"] for s in stage["steps"]
                        if s["id"] in ids]
            if any(s["state"] != DONE for s in launched):
                raise ServiceError("An earlier launch did not finish cleanly.")
            keys = sorted({(i["node"], i["name"]) for s in launched
                           for i in s["instances"] if i["ok"]})
        if not keys:
            raise ServiceError("Nothing was launched to wait for.")

        timeout = step["timeout_seconds"]
        deadline = time.monotonic() + timeout
        self._update(record, until=time.time() + timeout)
        while True:
            verdicts = self._health_of(keys)
            green = [k for k in keys if verdicts[k] is None]
            self._update(record, detail="%d of %d healthy" % (len(green), len(keys)))
            if len(green) == len(keys):
                self._update(record, state=DONE, finished_at=time.time(), until=None)
                return
            if time.monotonic() > deadline:
                raise ServiceError("Not healthy after %ds: %s" % (timeout, "; ".join(
                    "%s (%s)" % (_label(k, keys), verdicts[k])
                    for k in keys if verdicts[k] is not None)))
            self._sleep(run, HEALTH_POLL_SECONDS)

    def _health_of(self, keys):
        """``{(node, name): None if green, else why not}``.

        Read from each node's own monitor where one runs. Where none does on
        this node -- the CLI, or `serve` with checks off -- a one-off round is
        run for just these instances instead, so a stack still works there. A
        remote node with checks off can never say healthy, which is said
        rather than waited out.
        """
        local = self.cluster.local_name()
        records, problems = {}, {}
        remote = sorted({node for node, _ in keys if node != local})
        mine = [name for node, name in keys if node == local]
        if mine:
            report = self.service.health()
            if report["enabled"]:
                found = report["instances"]
            else:
                found = self.service.check_health(names=mine, window=ONE_OFF_WINDOW)
            records.update(((local, r["name"]), r) for r in found)
        if remote:
            listing = self.cluster.containers(nodes=remote)
            for problem in listing["errors"]:
                problems[problem["node"]] = "node not answering: %s" % problem["error"]
            for node in remote:
                if node not in listing["monitored"] and node not in problems:
                    raise ServiceError("Health checks are off on %s, so nothing there can "
                                       "report healthy." % node, 409)
            records.update(((r["node"], r["name"]), r) for r in listing["health"])
        verdicts = {}
        for key in keys:
            record = records.get(key)
            if key[0] in problems:
                verdicts[key] = problems[key[0]]
            elif record is None:
                verdicts[key] = "not checked yet"
            else:
                verdicts[key] = _not_green(record)
        return verdicts

    # -- runs --------------------------------------------------------------

    def stack_runs(self):
        with self._lock:
            return sorted((_copy(r) for r in self._runs.values()),
                          key=lambda r: r["started_at"])

    def cancel_stack_run(self, name):
        """Stop starting anything new. Launches already under way finish."""
        with self._lock:
            run = self._runs.get(name)
            if not run or run["finished_at"] is not None:
                raise ServiceError("Stack '%s' is not running." % name, 404)
            run["cancelling"] = True
            return _copy(run)

    def dismiss_stack_run(self, name):
        with self._lock:
            run = self._runs.get(name)
            if not run:
                raise ServiceError("No recorded run for stack '%s'." % name, 404)
            if run["finished_at"] is None:
                raise ServiceError("That run is still in progress.", 409)
            del self._runs[name]
        return {"dismissed": name}

    def pending_work(self):
        with self._lock:
            return ["%s of stack '%s'" % (r["action"], r["stack"]) for r in self._runs.values()
                    if r["finished_at"] is None]


def _copy(run):
    return dict(run, stages=[{"steps": [dict(s, instances=[dict(i) for i in s["instances"]])
                                        if "instances" in s else dict(s)
                                        for s in stage["steps"]]}
                             for stage in run["stages"]])


def _not_green(record):
    if record["status"] != "healthy":
        reasons = "; ".join(record.get("reasons") or [])
        return record["status"] + (": " + reasons if reasons else "")
    app = record.get("app")
    if app and app.get("status") != "ok":
        return "app check %s" % app.get("status")
    return None


def _label(key, keys):
    nodes = {node for node, _ in keys}
    return key[1] if len(nodes) == 1 else "%s on %s" % (key[1], key[0])


def _instance_error(instance):
    if instance.get("error"):
        return instance["error"]
    bootstrap = (instance.get("container") or {}).get("bootstrap") or {}
    failed = next((m for m in bootstrap.get("modules") or [] if m.get("exit_code")), None)
    return "module '%s' failed" % failed["name"] if failed else None


def _natural(name):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def _field(outputs, field):
    if field == "count":
        return str(len(outputs["names"]))
    if field == "name":
        return outputs["names"][0] if outputs["names"] else ""
    if field == "ip":
        return outputs["ips"][0] if outputs["ips"] else ""
    return ",".join(outputs[field])


def _input_only(value):
    """Whether a value is nothing but ``{{params.NAME}}`` references.

    The one form a secret may take in a saved stack: it names a value to be
    entered at launch instead of carrying one. A save refuses anything else and
    a listing strips it, so the two must agree about what "anything else" is --
    hence one predicate rather than the same condition written twice.
    """
    return not PLACEHOLDER.sub("", value).strip() and all(
        INPUT_REFERENCE.match(reference) for reference in PLACEHOLDER.findall(value))


def public_stack(stack, templates, modules):
    """A stack as it may be served: a step's literal secret dropped.

    ``_check_stages()`` refuses one on save, so this only fires for a record
    that never went through a save -- a file written or copied into the store
    by hand, which every record here is built to expect. It is the same reason
    a template's listing re-derives its public selection instead of trusting
    what is on disk: the file is untrusted input, so the check belongs on the
    way out as well as on the way in.
    """
    stages = []
    for stage in stack["stages"]:
        steps = []
        for step in stage["steps"]:
            params = step.get("params") or {}
            template = templates.get(step["template"]) if step["type"] == "launch" else None
            if params:
                # A step naming a template this node does not have cannot say
                # which of its parameters are secret, so every name that is
                # secret in any module counts. The record is unusable as it
                # stands -- a launch refuses a template that is not here -- so
                # failing closed costs a working stack nothing.
                secrets = secret_param_names(
                    [modules[m] for m in template["bootstrap"]["modules"] if m in modules]
                    if template is not None else modules.values())
                kept = {k: v for k, v in params.items()
                        if k not in secrets or _input_only(v)}
                if len(kept) != len(params):
                    step = dict(step, params=kept)
            steps.append(step)
        stages.append(dict(stage, steps=steps))
    return dict(stack, stages=stages)


def stack_inputs(stack):
    """Names a launch of the stack must be given, for its ``{{params.NAME}}``."""
    return sorted({m.group(1) for stage in stack["stages"] for step in stage["steps"]
                   for value in (step.get("params") or {}).values()
                   for m in (INPUT_REFERENCE.match(r) for r in PLACEHOLDER.findall(value))
                   if m})


def _render(value, context, params):
    """Fill in ``{{step.field}}`` from earlier launches, ``{{params.X}}`` from the launch."""
    def one(match):
        entered = INPUT_REFERENCE.match(match.group(1))
        if entered:
            return params.get(entered.group(1), "")
        reference = REFERENCE.match(match.group(1))
        if not reference or reference.group(1) not in context:
            return ""
        return _field(context[reference.group(1)], reference.group(2))
    return PLACEHOLDER.sub(one, value)


def _exports(stack, context):
    """The environment every later launch's modules see."""
    env = {"LEMONDX_STACK": stack}
    for step_id, outputs in context.items():
        base = "LEMONDX_STACK_%s_" % step_id.upper()
        env[base + "NAMES"] = " ".join(outputs["names"])
        env[base + "IPS"] = " ".join(outputs["ips"])
        env[base + "IPV6"] = " ".join(outputs["ipv6"])
        env[base + "NODES"] = " ".join(outputs["nodes"])
    return env

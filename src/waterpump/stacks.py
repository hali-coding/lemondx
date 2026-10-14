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

* every later launch's modules get ``WATERPUMP_STACK_<STEP>_NAMES``, ``_IPS``,
  ``_IPV6`` and ``_NODES`` in their environment, space-separated, which is
  what a shell ``for`` loop wants;
* a launch step's own ``params`` may say ``{{db.ip}}`` or ``{{app.ips}}``,
  filled in when that step starts -- comma-separated, which is what a config
  file usually wants -- so an existing module's ``DB_HOST`` parameter can be
  pointed at the database without the module changing.

A launch's parameters are handed on too, as ``{{db.param.POSTGRES_DB}}``: the
value that step launched with, so the database's name is said once and an app
server's ``DB_NAME`` follows it. A secret one cannot be named that way -- it
would hand a secret to a module that never declared it secret, and so would
not redact it -- which is what ``{{params.NAME}}`` in both steps is for.

A stack can also carry its own values, so they are said once rather than
typed at every launch: ``{{vars.NAME}}`` is a *variable* -- an NFS server's
name, say -- stored in the stack record in the clear and synced with it;
``{{secrets.NAME}}`` is a *secret* -- a database password -- declared in the
record but whose value is kept apart, sealed under ``auth/`` (see
``store.set_stack_secret()``), synced only over the members-only route, and
read back only by an admin who confirms their password. A secret may be marked
``generate``: its value is made the first time a launch needs it, and kept.

What was typed in when the stack was launched -- a database password, say --
is ``{{params.NAME}}``. That is the one way a step may set a *secret*
parameter: what is stored is the placeholder, never the value, so the rule that
secrets are never saved holds, and the receiving module still redacts it from
its output because it declared the parameter secret.

Every instance a stack launches is tagged ``user.waterpump.stack`` on the
instance itself, which is the whole of the state kept about what a stack is
running: it survives a restart of waterpump, it is where the instance is on
whichever node that is, and a recreate from the template keeps it. So stopping,
starting, destroying and relaunching a stack all start from the tag, like a
template's fleet actions do.

This sits above ``ClusterService`` rather than in ``ContainerService`` because
a launch step may name nodes and groups: every launch goes through
``ClusterService.launch_template()``, which takes the plain local path when the
only node is this one, so an unfederated waterpump never touches cluster code
here either. Each launch is an ordinary template run, so the Templates tab
shows it and the one-run-per-template lock still holds; a stack run is the
record *around* those, kept in memory like them (one per ``serve`` process).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import secrets as secrets_module
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote

from . import auth as auth_module, eventlog, stackreport, store
from .bootstrap import (BootstrapError, discover_modules, occurrence_value, occurrences,
                        param_key, redact, secret_param_names, split_param)
from .cluster import ClusterError, from_peer
from .lxd import LXDError
from .nodeclient import NodeError
from .service import VALID_PREFIX, ServiceError

TEARDOWN = "_teardown"          # not a valid step id, so never a user's
STATE_ACTIONS = ("start", "stop", "restart")

STEP_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
REFERENCE = re.compile(r"^([a-z][a-z0-9_]{0,31})\.([a-z0-9_]+)$")
# A parameter an earlier launch ran with; NAME@n for a repeated module's.
PARAM_REFERENCE = re.compile(
    r"^([a-z][a-z0-9_]{0,31})\.param\.([A-Za-z_][A-Za-z0-9_]{0,63}(?:@[0-9]+)?)$")
# A value supplied when the stack is launched. "params" is reserved as a step id.
INPUT_REFERENCE = re.compile(r"^params\.([A-Za-z_][A-Za-z0-9_]{0,63})$")
# The stack's own variables and secrets; "vars" and "secrets" are reserved too.
VAR_REFERENCE = re.compile(r"^vars\.([A-Za-z_][A-Za-z0-9_]{0,63})$")
SECRET_REFERENCE = re.compile(r"^secrets\.([A-Za-z_][A-Za-z0-9_]{0,63})$")
RESERVED_IDS = ("params", "vars", "secrets")
# A script step's parameter, which becomes an environment variable.
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# A generated secret: 32 characters of URL-safe base64, about 192 bits, and
# nothing a shell, a URL or a config file would need quoting for.
GENERATED_SECRET_BYTES = 24
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
# A script step's output rides on the run record, which every poll re-sends:
# the tail is what says why it failed.
SCRIPT_OUTPUT_LIMIT = 16 * 1024
# After the timeout, or a cancel, the script's process group is asked to stop
# and given this long before it is killed.
SCRIPT_KILL_GRACE = 5
# A one-off round (no monitor in this process) needs a CPU window.
ONE_OFF_WINDOW = 5
# Asking every other member whether it is running a stack before starting one
# here: short, since a member that does not answer is not waited for.
PEER_RUNS_TIMEOUT = 5
# A swarm stopped mid-launch: how often its teardown looks for instances the
# launch is still creating, until that launch has finished.
TEARDOWN_SWEEP_SECONDS = 3

PENDING, RUNNING, READY, DONE, FAILED, SKIPPED, CANCELLED = (
    "pending", "running", "ready", "done", "failed", "skipped", "cancelled")


def _log(message, level=None):
    if level is None:
        level = logging.WARNING if "fail" in message.lower() else logging.INFO
    eventlog.message("stack: %s" % message, level=level)


class _Cancelled(Exception):
    pass


class _Halted(Exception):
    """A swarm step stopped because another step of the run failed.

    Not a cancel: nobody pressed Stop, and the run is a failure, not a
    cancelled one -- the step is marked stopped with the reason as its detail.
    """


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
        # The template runs each swarm run started, by run id: what its
        # teardown waits out after a Stop, since a launch cut short may still
        # be creating instances.
        self._launched = {}

    # -- definitions -------------------------------------------------------

    def list_stacks(self):
        # `inputs` is derived, not stored: the values a launch must be given
        # because some step's parameter says {{params.NAME}}.
        stored = [stack for _, stack in sorted(store.load_stacks().items())]
        return [_listed(stack) for stack in self._public(stored)]

    def get_stack(self, name):
        stack = store.load_stacks().get(name)
        if stack is None:
            raise ServiceError("No such stack '%s'." % name, 404)
        return _listed(self._public([stack])[0])

    def _public(self, stacks):
        """Stored stacks as everything but the store may see them.

        Takes the whole listing so the templates and modules are read once for
        it rather than once a stack, and not at all unless some step has
        parameters -- which one carrying a literal secret must have.
        """
        if not any(step.get("params") for stack in stacks
                   for stages in [stack["stages"]] + [w["stages"] for w in stack["swarms"]]
                   for stage in stages for step in stage["steps"]):
            return stacks
        templates, modules = store.load_templates(), discover_modules()
        return [public_stack(stack, templates, modules) for stack in stacks]

    def save_stack(self, name, stages=None, description="", variables=None, secrets=None,
                   propagate=True, previous=None, swarms=None):
        """Save a stack; ``previous`` names the stack this one was, for a rename.

        A rename is a save under the new name and then a delete of the old, by
        the caller -- and the delete drops the old name's secret values. So
        they are moved here first, while the old name still holds them.
        """
        name = self.service._record_name(name, "stack")
        variables = _check_variables(variables)
        declared = _check_secret_declarations(secrets)
        warnings = []
        known = {"vars": set(variables), "secrets": set(declared)}
        cleaned, launches = self._check_flow(stages, known, warnings)
        checked_swarms = self._check_swarms(swarms, cleaned, launches, known, warnings)
        saved = store.save_stack(name, {"description": str(description or "")[:200],
                                        "stages": cleaned, "variables": variables,
                                        "secrets": declared, "swarms": checked_swarms})
        result = self.cluster._with_sync(saved, "stacks", saved["name"], propagate)
        moved = previous and previous != saved["name"] and propagate \
            and store.rename_stack_secrets(str(previous), saved["name"])
        # A value whose declaration is gone is a secret nothing can name any
        # more, and still readable by an admin: not worth keeping.
        held = store.stack_secret_status(saved["name"])
        orphans = sorted(set(held) - set(declared))
        for orphan in orphans:
            store.clear_stack_secret(saved["name"], orphan)
        if (moved or orphans) and propagate:
            self.cluster._propagate(store.STACK_SECRETS_KIND, saved["name"])
        result["warnings"] = warnings + self._unknown_references(cleaned)
        return _with_secret_status(result)

    def _unknown_references(self, stages):
        """Groups and nodes the stack names that do not exist here: said, not refused.

        A template must exist to be saved against (`_check_stages()`), but a
        group or a node may come later -- a member still joining, a group made
        after the stack -- so a save cannot require them. A typo would
        otherwise be heard about only when a launch fails, so the save says so.
        """
        groups = set(store.load_node_groups())
        nodes = set(self.cluster.all_nodes())
        warnings = []
        for stage in stages:
            for step in stage["steps"]:
                if step["type"] != "launch":
                    continue
                for group in step["groups"]:
                    if group not in groups:
                        warnings.append("Step '%s' launches on group '%s', which does "
                                        "not exist here." % (step["id"], group))
                for node in step["nodes"]:
                    if node not in nodes:
                        warnings.append("Step '%s' launches on node '%s', which is not "
                                        "a member." % (step["id"], node))
        return warnings

    def delete_stack(self, name, everywhere=True, instances=None):
        """Delete a stack's definition; with ``instances``, destroy them first.

        Left alone, the instances keep their tag and outlive the definition --
        which is the default, since deleting a design is not deciding to throw
        away what it built. ``instances`` is that decision, as confirmed: the
        exact list the person saw, checked like a destroy's. The destroy runs
        to the end before the definition goes (a delete is refused while the
        stack has a run going), and a destroy that fails keeps the stack, so
        what is left over can still be found and destroyed by name.
        """
        with self._lock:
            run = self._runs.get(name)
            if run and run["finished_at"] is None:
                raise ServiceError("Stack '%s' is running; cancel it first." % name, 409)
        if store.load_stacks().get(name) is None:
            raise ServiceError("No such stack '%s'." % name, 404)
        destroyed = []
        if instances is not None:
            doomed = self._confirmed(name, instances)
            if doomed:
                run = self.destroy_stack(name, doomed)
                if not run["ok"]:
                    raise ServiceError("Destroying the instances of '%s' failed, so the stack "
                                       "was kept: %s" % (name, run.get("error") or "see its run"),
                                       409)
                destroyed = doomed
                # Its run record would outlive the stack in the Stacks tab.
                with self._lock:
                    self._runs.pop(name, None)
        if not store.delete_stack(name):
            raise ServiceError("No such stack '%s'." % name, 404)
        # Here only: every member deletes the stack, and drops its own copy.
        store.drop_stack_secrets(name)
        return self.cluster._with_sync({"deleted": name, "destroyed": destroyed}, "stacks",
                                       name, everywhere, deleted=True)

    # -- variables and secrets ---------------------------------------------
    #
    # A secret's value is never in the stack record and never in a listing:
    # status here, values only through reveal_secret() and the members-only
    # sync. Every change is propagated at once, like the stack itself, and
    # reconciliation settles a member that was away (RECONCILE_KINDS) -- which
    # matters more here than anywhere: a node that missed a generated password
    # would otherwise generate a second one at its next launch.

    def _declared(self, stack, secret):
        found = self.get_stack(stack)
        if secret not in found["secrets"]:
            raise ServiceError("Stack '%s' has no secret '%s'." % (stack, secret), 404)
        return found

    def secret_status(self, stack):
        return _with_secret_status(self.get_stack(stack))["secrets"]

    def set_secret(self, stack, secret, value):
        self._declared(stack, secret)
        if not isinstance(value, str) or not value:
            raise ServiceError("Give the secret a value.")
        if len(value) > store.STACK_VARIABLE_BYTES or "\x00" in value:
            raise ServiceError("A secret is text of at most %d bytes."
                               % store.STACK_VARIABLE_BYTES)
        store.set_stack_secret(stack, secret, value)
        synced = self.cluster._propagate(store.STACK_SECRETS_KIND, stack)
        return dict(self.secret_status(stack)[secret], name=secret, synced=synced)

    def clear_secret(self, stack, secret):
        self._declared(stack, secret)
        if not store.clear_stack_secret(stack, secret):
            raise ServiceError("Secret '%s' of stack '%s' holds no value." % (secret, stack),
                               404)
        synced = self.cluster._propagate(store.STACK_SECRETS_KIND, stack)
        return dict(self.secret_status(stack)[secret], name=secret, synced=synced)

    def reveal_secret(self, stack, secret, principal, password=None):
        """A secret's value, to an admin who has just confirmed who they are.

        The one route that sends a stored secret to a person, so the checks
        are all here rather than in the router: the role, the password again
        (``AuthService.confirm()`` -- a session cookie alone is not enough, it
        is ambient), and a log line naming who read what. Never the value.
        """
        if principal is None or not principal.can(auth_module.ADMIN):
            raise ServiceError("Only an admin can read a stored secret.", 403)
        if from_peer(principal) or auth_module.is_relayed(principal):
            # A call relayed through another node cannot confirm a password
            # here, and an older one arrives as the bare cluster credential,
            # which would pass for an admin token. The secret is synced: read
            # it where you signed in.
            raise ServiceError("A stored secret is read on the node you are signed in "
                               "to, not relayed through another.", 403)
        self._declared(stack, secret)
        self.cluster.auth.confirm(principal, password)
        held = store.load_stack_secrets(stack).get(secret)
        if held is None:
            raise ServiceError("Secret '%s' of stack '%s' holds no value%s."
                               % (secret, stack, "" if not store.stack_secret_status(stack)
                                  .get(secret) else " this node can open"), 404)
        eventlog.event("access", "secret.reveal", level=logging.WARNING,
                       kind=store.STACK_SECRETS_KIND, stack=stack, secret=secret)
        return {"name": secret, "value": held["value"], "generated": held["generated"],
                "updated_at": held["updated_at"]}

    def secret_values(self, stack):
        """``{NAME: {value, generated, updated_at}}``: what sync carries to a member."""
        return store.load_stack_secrets(stack)

    def adopt_secrets(self, stack, body):
        """Take a member's whole set of values for one stack, as it holds them."""
        values = (body or {}).get("values") if isinstance(body, dict) else None
        if not isinstance(values, dict):
            raise ServiceError("values must be an object.")
        cleaned = {}
        for name, entry in values.items():
            if not isinstance(name, str) or not store.STACK_VALUE_NAME.match(name) \
                    or not isinstance(entry, dict) or not isinstance(entry.get("value"), str):
                raise ServiceError("Bad secret record '%s'." % name)
            cleaned[name] = {"value": entry["value"],
                             "generated": entry.get("generated") is True,
                             "updated_at": entry.get("updated_at")
                             if isinstance(entry.get("updated_at"), int) else None}
        store.replace_stack_secrets(stack, cleaned)
        return {"stack": stack, "secrets": sorted(cleaned)}

    def _check_stages(self, stages, declared=None, warnings=None):
        """The stages as they will be stored, or raise with what is wrong.

        The store would quietly drop what it cannot keep, which is right for a
        file copied in by hand; a save is told instead.
        """
        return self._check_flow(stages, declared, warnings)[0]

    def _check_flow(self, stages, declared=None, warnings=None, before=None, swarm=None):
        """``(stages as stored, launches by id)``, or raise with what is wrong.

        ``before`` is what a swarm runs against: the stack's launches, each an
        earlier launch to every step of the swarm, and its step ids, which a
        swarm's may not reuse since a reference names a step by id.
        """
        what = "Swarm '%s'" % swarm if swarm else "A stack"
        if not isinstance(stages, list) or not stages:
            raise ServiceError("%s needs at least one stage with a step in it." % what)
        if len(stages) > MAX_STAGES:
            raise ServiceError("%s has at most %d stages." % (what, MAX_STAGES))
        templates = store.load_templates()
        available = discover_modules()
        # Earlier launches by id, with the parameter names each hands on.
        before_launches, before_ids = before or ({}, set())
        seen, earlier_launches, cleaned = set(), dict(before_launches), []
        position = 0
        for index, stage in enumerate(stages, 1):
            steps = stage.get("steps") if isinstance(stage, dict) else stage
            if not isinstance(steps, list) or not steps:
                raise ServiceError("Stage %d has no steps." % index)
            if len(steps) > MAX_STEPS_PER_STAGE:
                raise ServiceError("Stage %d has more than %d steps."
                                   % (index, MAX_STEPS_PER_STAGE))
            stage_steps, stage_templates, stage_launches = [], set(), {}
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
                if not STEP_ID.match(step_id) or step_id in RESERVED_IDS:
                    raise ServiceError(
                        "Invalid step id '%s'. Use lowercase letters, digits and "
                        "underscores, starting with a letter (max 32)." % step_id)
                if step_id in seen:
                    raise ServiceError("Two steps are called '%s'; ids must be unique."
                                       % step_id)
                if step_id in before_ids:
                    raise ServiceError(
                        "%s: step '%s' has the id of one of the stack's own steps, which "
                        "its references would mean instead. Give it another." % (what, step_id))
                seen.add(step_id)
                raw["id"] = step_id
                kind = raw.get("type")
                if kind == "launch":
                    self._check_launch(raw, templates, available, earlier_launches,
                                       declared or {"vars": set(), "secrets": set()},
                                       warnings if warnings is not None else [],
                                       swarm=swarm)
                    if raw["template"] in stage_templates:
                        # Each launch is that template's one run; two at once
                        # would have the second refused as soon as it started.
                        raise ServiceError(
                            "Stage %d launches '%s' twice. Put one in a later stage, or "
                            "raise its count." % (index, raw["template"]))
                    stage_templates.add(raw["template"])
                    stage_launches[step_id] = handed_names(
                        templates[raw["template"]], raw["params"], available)
                elif kind == "sleep":
                    self._check_seconds(raw, "seconds", "sleep", store.STACK_SLEEP)
                elif kind == "script":
                    script = raw.get("script")
                    if script is None or (isinstance(script, str) and not script.strip()):
                        script = store.STACK_SCRIPT_DEFAULT
                    if not isinstance(script, str):
                        raise ServiceError("Step '%s': the script must be text." % step_id)
                    if len(script.encode("utf-8")) > store.STACK_SCRIPT_BYTES or "\x00" in script:
                        raise ServiceError("Step '%s': a script is text of at most %d KiB."
                                           % (step_id, store.STACK_SCRIPT_BYTES // 1024))
                    raw["script"] = script
                    known = declared or {"vars": set(), "secrets": set()}
                    self._check_references(step_id, "script", script, earlier_launches,
                                           known, None)
                    # Its parameters are what a launch's are -- values, with any
                    # placeholder -- handed to the script as environment variables.
                    params = raw.get("params") or {}
                    if not isinstance(params, dict):
                        raise ServiceError("Step '%s': params must be an object." % step_id)
                    for key, value in params.items():
                        if not ENV_NAME.match(str(key)):
                            raise ServiceError(
                                "Step '%s': '%s' cannot be an environment variable. Use "
                                "letters, digits and _, not starting with a digit."
                                % (step_id, key))
                        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                            raise ServiceError("Step '%s': the value of %s must be text."
                                               % (step_id, key))
                        self._check_references(step_id, key, str(value), earlier_launches,
                                               known, None)
                    raw["params"] = {str(k): str(v) for k, v in params.items()}
                    self._check_seconds(raw, "timeout_seconds", "script timeout",
                                        store.STACK_SCRIPT_TIMEOUT)
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
            earlier_launches.update(stage_launches)
            cleaned.append({"steps": stage_steps})
        # Through the store's own cleaner, so what is returned is exactly what
        # a later read will give back.
        return store._clean_stages(cleaned, swarm=bool(swarm)), earlier_launches

    def _check_swarms(self, swarms, stages, launches, known, warnings):
        """A stack's swarms as they will be stored, or raise.

        Each is checked as though it were the stack's next stage: every launch
        of the stack is an earlier launch to it, which is what lets a load
        generator be pointed at `{{app.ips}}`.
        """
        if swarms is None:
            return []
        if not isinstance(swarms, list):
            raise ServiceError("swarms must be a list of {name, description, stages, "
                               "sequence}.")
        if len(swarms) > store.SWARMS_MAX:
            raise ServiceError("A stack has at most %d swarms." % store.SWARMS_MAX)
        ids = {step["id"] for stage in stages for step in stage["steps"]}
        checked, names = [], set()
        for raw in swarms:
            if not isinstance(raw, dict):
                raise ServiceError("Each swarm must be an object.")
            name = str(raw.get("name") or "").strip()
            if not store.STACK_VALUE_NAME.match(name):
                raise ServiceError("Invalid swarm name '%s'. Use letters, digits and "
                                   "underscores, starting with a letter or _." % name)
            if name in names:
                raise ServiceError("Two swarms are called '%s'." % name)
            names.add(name)
            flow, _ = self._check_flow(raw.get("stages"), known, warnings,
                                       before=(launches, ids), swarm=name)
            sequence = raw.get("sequence", True)
            if not isinstance(sequence, bool):
                raise ServiceError("Swarm '%s': sequence must be true or false." % name)
            checked.append({"name": name, "description": str(raw.get("description") or "")[:200],
                            "stages": flow, "sequence": sequence})
        return checked

    def _check_launch(self, step, templates, available, earlier, declared, warnings,
                      swarm=None):
        step_id = step["id"]
        template = templates.get(str(step.get("template") or "").strip())
        if template is None:
            raise ServiceError("Step '%s' launches template '%s', which does not exist."
                               % (step_id, step.get("template") or ""), 409)
        step["template"] = template["name"]
        low, high, _ = store.SWARM_LAUNCH_COUNT if swarm else store.STACK_LAUNCH_COUNT
        if swarm and not template.get("ephemeral") and warnings is not None:
            # Assumed, not required: the swarm destroys what it made either way.
            warnings.append(
                "Swarm '%s', step '%s': template '%s' is not ephemeral. The swarm still "
                "destroys its instances when it ends, but if waterpump stops mid-swarm they "
                "stay until the swarm is stopped." % (swarm, step_id, template["name"]))
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
        low, high, default = store.STACK_LAUNCH_MIN_PERCENT
        share = step.get("min_percent", default)
        try:
            share = int(share) if not isinstance(share, bool) else None
        except (TypeError, ValueError):
            share = None
        if share is None or not low <= share <= high:
            raise ServiceError("Step '%s': the share of instances that must start is a "
                               "percentage from %d to %d." % (step_id, low, high))
        step["min_percent"] = share
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
            secret_param = split_param(key)[0] in secrets
            if secret_param and not _input_only(value):
                raise ServiceError(
                    "Step '%s' sets %s, a secret. Its value cannot be written into a "
                    "stack: give it {{secrets.NAME}} for one of the stack's secrets, or "
                    "{{params.NAME}} for a value entered at launch." % (step_id, key))
            self._check_references(step_id, key, value, earlier, declared,
                                   warnings if not secret_param else None)
        step["params"] = {str(k): str(v) for k, v in params.items()}
        for key in ("nodes", "groups"):
            value = step.get(key) or []
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ServiceError("Step '%s': %s must be a list of names." % (step_id, key))
        step["wait_bootstrap"] = step.get("wait_bootstrap", True) is not False

    @staticmethod
    def _check_references(step_id, key, value, earlier, declared, warnings):
        """Refuse a placeholder in ``value`` that names nothing this step may use.

        ``warnings`` collects a secret handed to a parameter its module does
        not declare secret; None where that is not a concern -- a module's own
        secret parameter, or a script step, whose output is redacted here.
        """
        for reference in PLACEHOLDER.findall(value):
            if INPUT_REFERENCE.match(reference):
                continue
            own = VAR_REFERENCE.match(reference) or SECRET_REFERENCE.match(reference)
            if own:
                space = "vars" if reference.startswith("vars.") else "secrets"
                if own.group(1) not in declared[space]:
                    raise ServiceError(
                        "Step '%s', %s: '{{%s}}' names no %s of this stack. Add it "
                        "under the stack's %s first."
                        % (step_id, key, reference,
                           "variable" if space == "vars" else "secret",
                           "variables" if space == "vars" else "secrets"))
                if space == "secrets" and warnings is not None:
                    # Allowed -- a connection string is a fair place for a
                    # password -- but the module did not declare the name
                    # secret, so nothing redacts it from what it prints.
                    warnings.append(
                        "Step '%s' puts secret %s in %s, which its module does not "
                        "declare secret: the module's output will not hide it."
                        % (step_id, own.group(1), key))
                continue
            handed = PARAM_REFERENCE.match(reference)
            if handed:
                source, name = handed.groups()
                if source not in earlier:
                    raise ServiceError(
                        "Step '%s', %s: '{{%s}}' names '%s', which is not a launch "
                        "in an earlier stage." % (step_id, key, reference, source))
                if name not in earlier[source]:
                    raise ServiceError(
                        "Step '%s', %s: '{{%s}}': step '%s' has no parameter %s it "
                        "can hand on. A secret is shared as {{params.NAME}} instead."
                        % (step_id, key, reference, source, name))
                continue
            match = REFERENCE.match(reference)
            if not match or match.group(2) not in FIELDS:
                raise ServiceError(
                    "Step '%s', %s: '{{%s}}' is not a reference. Use {{params.NAME}}, "
                    "{{vars.NAME}}, {{secrets.NAME}}, {{step.param.NAME}}, or "
                    "{{step.field}} with one of: %s."
                    % (step_id, key, reference, ", ".join(FIELDS)))
            if match.group(1) not in earlier:
                raise ServiceError(
                    "Step '%s', %s: '{{%s}}' names '%s', which is not a launch in an "
                    "earlier stage." % (step_id, key, reference, match.group(1)))

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

    def launch_stack(self, name, params=None, background=False, replace=None,
                     variables=None, secrets=None):
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

        ``variables`` overrides the stack's variables for this launch only.
        ``secrets`` gives values for the stack's secrets that hold none yet;
        each is kept, since the stack declares it as one to keep.
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
        values, held, offered = self._stack_values(name, stack, variables, secrets)
        self._preflight(stack["stages"], params, stack["secrets"])
        self._refuse_busy(name)

        plan = list(stack["stages"])
        action = "launch"
        doomed = self._confirmed(name, replace) if replace is not None else None
        # Kept only now that every check has passed: a launch refused above
        # must not have stored, or generated, anything.
        secret_values = self._settle_secrets(name, stack, held, offered)
        if replace is not None:
            action = "relaunch"
            if doomed:
                plan.insert(0, {"steps": [self._teardown_step(doomed)]})
        return self._start(name, action, plan, _Given(params, values, secret_values),
                           background)

    def _stack_values(self, name, stack, variables, secrets):
        """``(variables, secrets held, secrets offered)`` for a run, or raise.

        Every secret must be held, offered now, or one to generate; nothing is
        stored here -- that waits until every other check has passed.
        """
        for label, given in (("variables", variables), ("secrets", secrets)):
            if given is not None and not isinstance(given, dict):
                raise ServiceError("%s must be an object." % label)
        overrides = {str(k): str(v) for k, v in (variables or {}).items()}
        unknown = sorted(set(overrides) - set(stack["variables"]))
        if unknown:
            raise ServiceError("Stack '%s' has no variable %s." % (name, ", ".join(unknown)))
        values = {k: v["value"] for k, v in stack["variables"].items()}
        values.update(overrides)
        offered = {str(k): str(v) for k, v in (secrets or {}).items() if v}
        held = store.load_stack_secrets(name)
        lacking = [n for n in sorted(stack["secrets"]) if n not in held and n not in offered
                   and not stack["secrets"][n]["generate"]]
        if lacking:
            raise ServiceError("Stack '%s' needs secret %s: set it on the stack, or give "
                               "it when launching." % (name, " and ".join(lacking)))
        return values, held, offered

    def _preflight(self, stages, params, covered=()):
        """Refuse now what would fail partway: a template gone or busy, a missing
        module secret, a node that cannot take a launch. ``covered`` is the
        stack's secrets, each of which fills a module secret of its name."""
        templates = {t["name"]: t for t in self.service.list_templates()}
        busy = {r["template"] for r in self.service.template_runs()
                if r["finished_at"] is None}
        for stage in stages:
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
                    {name: "-" for name in covered}, **dict(
                        params, **{k: v or "-" for k, v in step["params"].items()})))
                try:
                    # Maintenance included: a relaunch refused here has not
                    # torn anything down yet.
                    self.cluster.launch_targets(step["nodes"], step["groups"])
                except ClusterError as exc:
                    raise ServiceError("Step '%s': %s" % (step["id"], exc.message), exc.code)

    # -- swarms --------------------------------------------------------------
    #
    # A swarm is load against a stack that is already running: stages like the
    # stack's own, designed with it, started on demand. Its references to the
    # stack's steps are rebuilt from the instances' tags (`_live_context()`),
    # since the launch that made them may be long finished and its outputs only
    # ever lived in that run's memory. Whatever way it ends -- its last stage
    # done, a step failed, Stop -- its instances are destroyed: the teardown
    # stage is marked `always`, and finds them by their swarm tag at the time.
    #
    # Several swarms may run as one sequence -- a functional test, then load
    # once it has passed. That is one run, not a run per swarm: the stack's
    # one-run lock then holds for the whole of it, so nothing can start
    # between them, and Stop ends the sequence rather than just its current
    # swarm. Each swarm is followed by its own teardown, so the next starts
    # against the stack alone, and a failure skips every swarm after it.
    # The stack's own sequence is part of its definition -- its swarms in
    # order, each with `sequence` saying whether it is in -- so running it is
    # one action, and what it runs is what the designer shows.

    def _swarm(self, stack, swarm):
        found = next((w for w in stack["swarms"] if w["name"] == swarm), None)
        if found is None:
            raise ServiceError("Stack '%s' has no swarm '%s'." % (stack["name"], swarm), 404)
        return found

    def set_swarm_sequence(self, name, swarm, enabled):
        """Put one swarm in or out of the stack's sequence.

        A save of the stored stack with that one flag changed, rather than a
        write of the flag alone: it then meets every check a save does and
        reaches the other members as one, and nobody has to send the whole
        definition back to flip a switch on the Stacks tab.
        """
        if not isinstance(enabled, bool):
            raise ServiceError("enabled must be true or false.")
        stack = store.load_stacks().get(name)
        if stack is None:
            raise ServiceError("No such stack '%s'." % name, 404)
        self._swarm(stack, swarm)
        return self.save_stack(
            stack["name"], stack["stages"], stack["description"], stack["variables"],
            stack["secrets"], swarms=[dict(w, sequence=enabled) if w["name"] == swarm else w
                                      for w in stack["swarms"]])

    def launch_swarm(self, name, swarm, counts=None, variables=None, secrets=None,
                     params=None, background=False):
        """Run one of a stack's swarms against its running instances."""
        return self.launch_swarms(name, [swarm], {swarm: counts} if counts else None,
                                  variables=variables, secrets=secrets, params=params,
                                  background=background)

    def launch_swarms(self, name, swarms=None, counts=None, variables=None, secrets=None,
                      params=None, background=False):
        """Run several of a stack's swarms one after another, as one run.

        ``swarms`` names them in order; None runs the stack's sequence.
        ``counts`` is ``{swarm: {step: n}}``. Everything is checked for every
        swarm before the first starts -- inputs, templates, what each points
        at -- since a load test refused after its functional test had run
        would have spent the test for nothing.
        """
        stack = self.get_stack(name)
        if swarms is None:
            swarms = sequence_of(stack)
            if not swarms:
                raise ServiceError("Stack '%s' has no swarms in its sequence; turn some on in "
                                   "the designer, or name the swarms to run." % name)
        if not isinstance(swarms, list) or not swarms:
            raise ServiceError("swarms must be a list of swarm names, in the order to run.")
        swarms = [str(w) for w in swarms]
        twice = sorted({w for w in swarms if swarms.count(w) > 1})
        if twice:
            # Its teardown finds instances by swarm and run, which a second
            # pass in the same run would share with the first.
            raise ServiceError("Swarm %s is named twice; a sequence runs each swarm once."
                               % ", ".join("'%s'" % w for w in twice))
        designs = [self._swarm(stack, w) for w in swarms]
        if counts is not None and not isinstance(counts, dict):
            raise ServiceError("counts must be an object of swarm: {step: number}.")
        counts = dict(counts or {})
        unknown = sorted(set(counts) - set(swarms))
        if unknown:
            raise ServiceError("counts names %s, which this run does not include."
                               % ", ".join("'%s'" % w for w in unknown))
        if params is not None and not isinstance(params, dict):
            raise ServiceError("params must be an object.")
        params = {str(k): str(v) for k, v in (params or {}).items()}
        missing = sorted({n for design in designs for n in swarm_inputs(design)
                          if not params.get(n)})
        if missing:
            raise ServiceError("%s %s when it is started." % (
                "Swarm '%s' needs" % swarms[0] if len(swarms) == 1 else "These swarms need",
                " and ".join(missing)))
        scaled = [(design["name"], self._scaled(design, counts.get(design["name"])))
                  for design in designs]
        every = [stage for _, stages in scaled for stage in stages]
        values, held, offered = self._stack_values(name, stack, variables, secrets)
        self._preflight(every, params, stack["secrets"])
        self._refuse_busy(name)
        given = _Given(params, values, {})
        context, handed = self._live_context(stack, given, every)
        given.secrets = self._settle_secrets(name, stack, held, offered)
        plan = []
        for swarm, stages in scaled:
            plan.extend({"swarm": swarm, "steps": [dict(step, swarm=swarm)
                                                    for step in stage["steps"]]}
                        for stage in stages)
            plan.append({"swarm": swarm, "steps": [
                {"id": TEARDOWN, "type": "destroy", "instances": None, "swarm": swarm,
                 "always": True}]})
        # Kept on the run for its report: what the load was pointed at, and
        # the variables it ran with -- both readable by whoever can read the
        # stack, unlike its inputs and secrets, which stay off the record.
        report = {"against": {step: {k: list(found.get(k) or []) for k in ("names", "ips", "nodes")}
                              for step, found in context.items()},
                  "variables": dict(values)}
        return self._start(name, "swarm", plan, given, background, swarm=swarms[0],
                           seed=(context, handed), sequence=swarms, report=report)

    @staticmethod
    def _scaled(design, counts):
        """The swarm's stages with this run's instance counts."""
        if counts is not None and not isinstance(counts, dict):
            raise ServiceError("counts must be an object of step: number.")
        counts = dict(counts or {})
        low, high, _ = store.SWARM_LAUNCH_COUNT
        launches = {step["id"] for stage in design["stages"] for step in stage["steps"]
                    if step["type"] == "launch"}
        unknown = sorted(set(counts) - launches)
        if unknown:
            raise ServiceError("Swarm '%s' has no launch step %s."
                               % (design["name"], ", ".join(unknown)))
        stages = []
        for stage in design["stages"]:
            steps = []
            for step in stage["steps"]:
                if step["id"] in counts:
                    try:
                        count = int(counts[step["id"]])
                    except (TypeError, ValueError):
                        count = 0
                    if not low <= count <= high:
                        raise ServiceError("Step '%s': a swarm launches between %d and %d "
                                           "instances." % (step["id"], low, high))
                    step = dict(step, count=count)
                steps.append(step)
            stages.append({"steps": steps})
        return stages

    def _live_context(self, stack, given, swarm_stages):
        """``(context, handed)`` for the stack as it is running now.

        What a launch run would have built as it went, rebuilt from the
        instances: grouped by the step that made each (its stack-step tag), or
        -- for one launched before steps were tagged -- by its template, where
        only one step of the stack launches that template. Only instances that
        are running count: a stopped stack still carries its tags, and its
        addresses are gone. A step the swarm refers to that has nothing
        running -- or, asked for an address, nothing with one -- is refused:
        the load would be pointed at nothing.
        """
        members = [m for m in self._members(stack["name"])
                   if not m.get("swarm") and m.get("status") == "Running"]
        steps = [step for stage in stack["stages"] for step in stage["steps"]
                 if step["type"] == "launch"]
        by_template = {}
        for step in steps:
            by_template.setdefault(step["template"], []).append(step["id"])
        templates, modules = store.load_templates(), discover_modules()
        context, handed = {}, {}
        for step in steps:
            mine = [m for m in members if m.get("step") == step["id"]
                    or (not m.get("step") and by_template.get(m.get("template"))
                        == [step["id"]])]
            if mine:
                ordered = sorted(mine, key=lambda m: _natural(m["name"]))
                reachable = self._reachable_address
                context[step["id"]] = {
                    "names": [m["name"] for m in ordered],
                    "ips": [reachable(m) for m in ordered if reachable(m)],
                    "ipv6": [m["ipv6"][0] for m in ordered if m.get("ipv6")],
                    "nodes": sorted({m["node"] for m in ordered}),
                }
            template = templates.get(step["template"])
            if template is not None:
                rendered = {k: _render(v, context, given, handed)
                            for k, v in step["params"].items()}
                handed[step["id"]] = handed_values(template, rendered, modules)
        wanted = {(match.group(1), match.group(2))
                  for stage in swarm_stages for step in stage["steps"]
                  for value in list((step.get("params") or {}).values())
                  + ([step["script"]] if step["type"] == "script" else [])
                  for reference in PLACEHOLDER.findall(value)
                  for match in [PARAM_REFERENCE.match(reference) or REFERENCE.match(reference)]
                  if match}
        ours = {s["id"] for s in steps}
        idle = sorted({step_id for step_id, _ in wanted
                       if step_id in ours and step_id not in context})
        if idle:
            raise ServiceError("Step %s of stack '%s' has no running instances to swarm "
                               "against; start or launch the stack first."
                               % (", ".join("'%s'" % i for i in idle), stack["name"]), 409)
        unaddressed = sorted({step_id for step_id, field in wanted
                              if step_id in context and field in ("ip", "ips")
                              and not context[step_id]["ips"]})
        if unaddressed:
            raise ServiceError("Step %s of stack '%s' has no address yet to swarm against; "
                               "wait for its instances to get one."
                               % (", ".join("'%s'" % i for i in unaddressed),
                                  stack["name"]), 409)
        return context, handed

    def stop_swarm(self, name, swarm, instances=None, background=True):
        """End a swarm: cancel it if it is running, else destroy what it left.

        Left behind is what a swarm's teardown never reached -- `serve` stopped
        mid-swarm, say. Those go only as confirmed, like a stack's destroy.
        """
        with self._lock:
            run = self._runs.get(name)
            running = run and run["finished_at"] is None
            if running and run["action"] == "swarm" and swarm in _sequence(run):
                run["cancelling"] = True
                return _copy(run)
        if running:
            raise ServiceError("Stack '%s' is busy with a %s, not swarm '%s'."
                               % (name, _what(run), swarm), 409)
        elsewhere = self._running_elsewhere(name)
        if elsewhere:
            node, other = elsewhere
            if other.get("action") == "swarm" and swarm in _sequence(other):
                # Stopped where it runs, which is the only place that can:
                # destroying its instances from here would leave it running
                # against nothing, and its teardown would then find none.
                stopped = self.cluster.client(node).request(
                    "POST", "/api/stacks/%s/swarms/%s/stop" % (quote(name, safe=""),
                                                                quote(swarm, safe="")),
                    body={})
                return dict(stopped or {}, node=node)
            raise ServiceError("Stack '%s' is busy with a %s on %s, not swarm '%s'."
                               % (name, _what(other), node, swarm), 409)
        stack = self.get_stack(name)
        self._swarm(stack, swarm)
        doomed = self._confirmed(name, instances, swarm=swarm)
        if not doomed:
            raise ServiceError("Swarm '%s' has no instances." % swarm, 404)
        step = {"id": TEARDOWN, "type": "destroy", "instances": doomed, "swarm": swarm}
        return self._start(name, "swarm-stop", [{"steps": [step]}], _Given({}, {}, {}),
                           background, swarm=swarm)

    # -- previews --------------------------------------------------------------
    #
    # What a launch or a swarm would do, without doing any of it: every check
    # a start runs, reported as problems rather than raised, and each step as
    # it would run -- where, how many, and each parameter with whatever can be
    # known now filled in. A launch knows only the stack's own values up
    # front; a swarm runs against instances that exist, so its references to
    # the stack resolve to real addresses. Secrets are never shown.

    def preview_launch(self, name, params=None, variables=None, secrets=None):
        stack = self.get_stack(name)
        return self._preview(stack, stack["stages"], params, variables, secrets, None, None)

    def preview_swarm(self, name, swarm, counts=None, variables=None, params=None,
                      secrets=None):
        stack = self.get_stack(name)
        design = self._swarm(stack, swarm)
        return self._preview(stack, design["stages"], params, variables, secrets, design, counts)

    def _preview(self, stack, stages, params, variables, secrets, swarm, counts):
        name = stack["name"]
        problems = []
        params = {str(k): str(v) for k, v in (params or {}).items()} \
            if isinstance(params, dict) else {}
        inputs = swarm_inputs(swarm) if swarm else stack_inputs(stack)
        missing = [n for n in inputs if not params.get(n)]
        if missing:
            problems.append("Needs %s when it is %s (--param NAME=VALUE, or the variable "
                            "of that name)." % (" and ".join(missing),
                                                "started" if swarm else "launched"))
        if swarm:
            try:
                stages = self._scaled(swarm, counts)
            except ServiceError as exc:
                problems.append(exc.message)
        values, held, offered = {}, {}, {}
        try:
            values, held, offered = self._stack_values(name, stack, variables, secrets)
        except ServiceError as exc:
            problems.append(exc.message)
            values = {k: v["value"] for k, v in stack["variables"].items()}
            held = store.load_stack_secrets(name)
        secret_state = {n: ("set" if n in held else "given now" if n in offered
                            else "generated at start" if d["generate"] else "missing")
                        for n, d in stack["secrets"].items()}
        try:
            self._preflight(stages, params, stack["secrets"])
        except (ServiceError, BootstrapError) as exc:
            problems.append(getattr(exc, "message", str(exc)))
        busy = self._busy(name)
        if busy:
            problems.append(busy)
        given = _Given(params, values, {n: "********" for n in stack["secrets"]})
        context, handed = {}, {}
        if swarm:
            try:
                context, handed = self._live_context(stack, given, stages)
            except ServiceError as exc:
                problems.append(exc.message)
        # What later steps will be handed that is settled already -- a
        # parameter set from a stack variable, or left at the template's
        # value -- so {{db.param.POSTGRES_DB}} previews as "drill", not as
        # something only the launch can tell. What the running stack says wins.
        handed = dict(_static_handed(stages, given), **handed)
        templates = {t["name"]: t for t in self.service.list_templates()}
        launches = {step["id"]: index for index, stage in enumerate(stack["stages"])
                    for step in stage["steps"] if step["type"] == "launch"}
        own = {step["id"]: index for index, stage in enumerate(stages)
               for step in stage["steps"] if step["type"] == "launch"} if swarm else {}
        explain = lambda value: _explain(value, context, given, handed, secret_state,  # noqa: E731
                                         launches, own, bool(swarm))
        filled = self.filled_by_name(stack, stages)
        out_stages = []
        for stage in stages:
            steps = []
            for step in stage["steps"]:
                previewed = self._preview_step(step, templates, explain, problems)
                for secret in filled.get(step["id"], ()):
                    if params.get(secret):
                        continue
                    previewed["params"].append({
                        "name": secret, "value": "{{secrets.%s}}" % secret,
                        "resolved": "********", "complete": True, "by_name": True,
                        "refs": [{"ref": "secrets.%s" % secret, "known": True, "masked": True,
                                  "source": "stack secret of the same name, %s"
                                  % secret_state.get(secret, "unknown")}]})
                steps.append(previewed)
            out_stages.append({"steps": steps})
        return {
            "stack": name, "action": "swarm" if swarm else "launch",
            "swarm": swarm["name"] if swarm else None,
            "ok": not problems, "problems": problems,
            "variables": values, "secrets": secret_state, "inputs": inputs,
            "stages": out_stages,
            # What the swarm is pointed at, as it is now.
            "against": [{"step": k, "names": v["names"], "ips": v["ips"]}
                        for k, v in context.items()] if swarm else None,
            "teardown": bool(swarm),
        }

    def filled_by_name(self, stack, stages=None):
        """``{step id: [secret, ...]}``: module secrets a launch leaves unset that
        a stack secret of the same name fills (see `_secrets_by_name()`)."""
        if not stack["secrets"]:
            return {}
        templates, modules = store.load_templates(), discover_modules()
        flows = [stages] if stages is not None else \
            [stack["stages"]] + [w["stages"] for w in stack["swarms"]]
        found = {}
        for flow in flows:
            for stage in flow:
                for step in stage["steps"]:
                    template = templates.get(step.get("template")) \
                        if step["type"] == "launch" else None
                    if template is None:
                        continue
                    wanted = secret_param_names([modules[m] for m in
                                                 template["bootstrap"]["modules"]
                                                 if m in modules])
                    names = sorted(n for n in wanted
                                   if n in stack["secrets"] and n not in step["params"])
                    if names:
                        found[step["id"]] = names
        return found

    def _preview_step(self, step, templates, explain, problems):
        base = {"id": step["id"], "type": step["type"]}
        if step["type"] == "sleep":
            return dict(base, seconds=step["seconds"])
        if step["type"] == "wait_healthy":
            return dict(base, timeout_seconds=step["timeout_seconds"])
        if step["type"] == "script":
            body = explain(step["script"])
            return dict(base, timeout_seconds=step["timeout_seconds"],
                        node=self.cluster.local_name(), script=step["script"],
                        script_resolved=body["resolved"], script_refs=body["refs"],
                        params=[dict(explain(v), name=k)
                                for k, v in sorted(step["params"].items())])
        template = templates.get(step["template"]) or {}
        targets, error = [], None
        try:
            targets, _ = self.cluster.launch_targets(*self.cluster.template_targets(
                step["template"], step["nodes"], step["groups"]))
        except (ClusterError, ServiceError) as exc:
            error = getattr(exc, "message", str(exc))
            problems.append("Step '%s': %s" % (step["id"], error))
        placement = {}
        for index in range(step["count"]):
            if targets:
                node = targets[index % len(targets)]
                placement[node] = placement.get(node, 0) + 1
        modules = template.get("bootstrap", {}).get("modules", [])
        return dict(
            base, template=step["template"], count=step["count"],
            image=template.get("image"), ephemeral=bool(template.get("ephemeral")),
            modules=modules, prefix=step["prefix"] or template.get("name_prefix") or "",
            wait_bootstrap=step["wait_bootstrap"], nodes=step["nodes"], groups=step["groups"],
            placement=placement, error=error,
            params=[dict(explain(v), name=k) for k, v in sorted(step["params"].items())])

    def _settle_secrets(self, name, stack, held, offered):
        """Every declared secret's value, storing those given or generated now."""
        result, changed = {}, False
        for secret, declared in sorted(stack["secrets"].items()):
            if secret in held:
                result[secret] = held[secret]["value"]
                continue
            generated = secret not in offered
            value = offered.get(secret) or secrets_module.token_urlsafe(
                GENERATED_SECRET_BYTES)
            store.set_stack_secret(name, secret, value, generated=generated)
            result[secret], changed = value, True
            _log("'%s': %s secret %s" % (name, "generated" if generated else "stored",
                                          secret))
        if changed:
            # Before anything launches: a member that launches this stack next
            # must use the same password the database is about to be given.
            self.cluster._propagate(store.STACK_SECRETS_KIND, name)
        return result

    def _busy(self, name):
        """Why stack ``name`` cannot start something now, or None.

        Busy here, or busy on another member: a run lives in the memory of the
        node that started it, so this node's own lock says nothing about the
        others, and without asking two nodes could launch one stack -- or run
        one swarm -- at once, each later tearing down what the other made.
        """
        with self._lock:
            current = self._runs.get(name)
            if current and current["finished_at"] is None:
                return "Stack '%s' is busy with a %s here; wait for it, or stop it." % (
                    name, _what(current))
        elsewhere = self._running_elsewhere(name)
        if elsewhere:
            node, run = elsewhere
            return "Stack '%s' is busy with a %s on %s; wait for it, or stop it there." % (
                name, _what(run), node)
        return None

    def _refuse_busy(self, name):
        why = self._busy(name)
        if why:
            raise ServiceError(why, 409)

    def _running_elsewhere(self, name):
        """``(node, run)`` for a run of stack ``name`` going on another member, or None."""
        return next(((r["node"], r) for r in self.peer_runs() if r.get("stack") == name),
                    None)

    def peer_runs(self):
        """Every stack run going on another member now, each tagged ``node``.

        Best effort: a member that does not answer is passed over, since
        refusing would make every stack hostage to the least available node.
        """
        local = self.cluster.local_name()
        try:
            peers = sorted(n for n in self.cluster._peers() if n != local)
        except Exception:                                 # noqa: BLE001
            return []
        if not peers or not self.cluster.cluster_secret():
            return []

        def ask(node):
            try:
                runs = self.cluster.client(node, timeout=PEER_RUNS_TIMEOUT).request(
                    "GET", "/api/stack-runs") or []
            except (NodeError, ClusterError, OSError):
                return []
            return [dict(r, node=node) for r in runs
                    if isinstance(r, dict) and r.get("finished_at") is None]

        return [run for found in self.cluster._fanout(peers, ask) for run in found]

    def destroy_stack(self, name, instances, background=False):
        """Stop and delete every instance the stack launched, as confirmed."""
        self._refuse_busy(name)
        doomed = self._confirmed(name, instances)
        if not doomed:
            raise ServiceError("Stack '%s' has no instances." % name, 404)
        return self._start(name, "destroy", [{"steps": [self._teardown_step(doomed)]}],
                           _Given({}, {}, {}), background)

    @staticmethod
    def _teardown_step(doomed):
        return {"id": TEARDOWN, "type": "destroy", "instances": doomed}

    def _start(self, name, action, plan, params, background, swarm=None, seed=None,
               sequence=None, report=None):
        with self._lock:
            current = self._runs.get(name)
            if current and current["finished_at"] is None:
                raise ServiceError("Stack '%s' is already running." % name, 409)
            run = {
                # Tags a swarm's instances, so its teardown takes only its own.
                "id": secrets_module.token_hex(8),
                "stack": name, "action": action, "started_at": time.time(),
                "finished_at": None, "ok": None, "error": None, "cancelling": False,
                # `swarm` is the one running now; `swarms` the whole sequence.
                "cancelled": False, "swarm": swarm, "swarms": sequence or (
                    [swarm] if swarm else []),
                "stages": [dict({"swarm": stage["swarm"]} if stage.get("swarm") else {},
                                steps=[self._step_record(s) for s in stage["steps"]])
                           for stage in plan],
            }
            run.update(report or {})
            self._runs[name] = run

        def work():
            self._execute(run, plan, params, seed)
            return self._snapshot(run)

        if background:
            threading.Thread(target=eventlog.carry(self._guarded), args=(run, work),
                             name="waterpump-stack-%s" % name, daemon=True).start()
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
            # A swarm's own teardown names no instances until it runs.
            doomed = step["instances"] or []
            record.update(count=len(doomed), swarm=step.get("swarm"),
                          instances=[dict(i, ok=None, error=None) for i in doomed])
        elif step["type"] == "script":
            record.update(timeout_seconds=step["timeout_seconds"], exit_code=None, output=None)
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

    def _halted(self, run):
        """Why a swarm's step in flight should stop now, or None.

        A swarm is a test: once any step has failed -- a load generator's
        bootstrap reported late by a launch that did not wait for it, say --
        the rest of it measures nothing, so a sleep, a script, a health wait
        or a wait on a launch stops rather than running to its end. A stack's
        own runs keep going, as they always have: what they launch is the
        deployment, and stopping half of it helps nobody.
        """
        if run["action"] != "swarm":
            return None
        with self._lock:
            failed = next((s["id"] for stage in run["stages"] for s in stage["steps"]
                           if s["state"] == FAILED), None)
        return "Stopped: step %s failed." % failed if failed else None

    def _check_running(self, run):
        """Raise if the run has been stopped, or (a swarm) has failed elsewhere."""
        if self._cancelling(run):
            raise _Cancelled()
        halted = self._halted(run)
        if halted:
            raise _Halted(halted)

    def _sleep(self, run, seconds):
        """Sleep, but wake to notice a cancel -- or a swarm's failure -- within a second."""
        deadline = time.monotonic() + seconds
        while True:
            self._check_running(run)
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(1.0, left))

    def _execute(self, run, plan, params, seed=None):
        # What each finished launch produced, by step id; what later launches
        # are handed. Launches that did not wait for bootstrap are still going
        # on followers; anything that depends on them joins them first. A
        # swarm starts with the running stack's, from `_live_context()`.
        context = dict(seed[0]) if seed else {}
        # The parameters each started launch ran with, for {{step.param.X}}.
        # Apart from `context` because those land in the run record, which
        # every poll re-sends, and these are no one's to read but a later step.
        handed = dict(seed[1]) if seed else {}
        followers = []            # (step id, template, thread)
        stopped = None
        begun = set()             # swarms of a sequence that got as far as starting
        for index, stage in enumerate(plan):
            records = run["stages"][index]["steps"]
            if stage.get("swarm") and index and plan[index - 1].get("swarm") != stage["swarm"]:
                # Between two swarms of a sequence: a launch that did not wait
                # for its bootstrap reports how that went from a follower, and
                # the next swarm may start only once every one of them has --
                # its teardown having waited for the launch is not enough, as
                # the follower notices a moment later.
                for _, _, thread in list(followers):
                    thread.join()
            if stopped is None and self._cancelling(run):
                stopped = CANCELLED
            if stopped is None and self._any_failed(run):
                stopped = SKIPPED
            # A swarm's teardown runs however the stages before it ended --
            # but not for a swarm of a sequence that never started, which has
            # nothing to destroy and would otherwise read as done.
            always = all(step.get("always") for step in stage["steps"]) and (
                stage.get("swarm") is None or stage["swarm"] in begun)
            if stopped is not None and not always:
                for record in records:
                    self._update(record, state=stopped)
                continue
            if stage.get("swarm") and stage["swarm"] not in begun:
                begun.add(stage["swarm"])
                # What the swarm before made is destroyed by now: its outputs
                # must not reach this one's scripts as WATERPUMP_STACK_* values.
                gone = {s["id"] for past in plan[:index] for s in past["steps"]
                        if past.get("swarm") and s["type"] == "launch"}
                with self._lock:
                    run["swarm"] = stage["swarm"]
                    for step_id in gone:
                        context.pop(step_id, None)
                        handed.pop(step_id, None)
            # Within one swarm of a sequence: an earlier swarm's launches are
            # torn down by now, and may share this one's step ids.
            earlier = [(s["id"], s, r) for at, past in enumerate(plan[:index])
                       if past.get("swarm") == stage.get("swarm")
                       for s, r in zip(past["steps"], run["stages"][at]["steps"])
                       if s["type"] == "launch"]
            threads = []
            for step, record in zip(stage["steps"], records):
                thread = threading.Thread(
                    target=eventlog.carry(self._run_step),
                    args=(run, step, record, context, handed, params, earlier,
                          followers),
                    name="waterpump-stack-step-%s" % step["id"], daemon=True)
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

    def _run_step(self, run, step, record, context, handed, params, earlier, followers):
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
                self._teardown(step, record, run)
            elif step["type"] == "script":
                self._script(run, step, record, context, handed, params)
            else:
                self._launch(run, step, record, context, handed, params, followers)
        except _Cancelled:
            self._update(record, state=CANCELLED, finished_at=time.time(),
                         detail=None, until=None)
        except _Halted as exc:
            self._update(record, state=CANCELLED, finished_at=time.time(),
                         detail=str(exc), until=None)
        except (ServiceError, ClusterError, LXDError, BootstrapError, NodeError) as exc:
            self._update(record, state=FAILED, finished_at=time.time(),
                         error=str(getattr(exc, "message", None) or exc), until=None)
        except Exception as exc:                          # noqa: BLE001
            _log("step '%s' of '%s' failed: %r" % (step["id"], run["stack"], exc))
            self._update(record, state=FAILED, finished_at=time.time(),
                         error="Unexpected error: %s" % exc, until=None)

    # -- script steps ------------------------------------------------------
    #
    # The one step that runs on a host rather than in an instance: a shell
    # script, on the node driving the stack, as the account waterpump runs as --
    # a webhook to call, an external API to tell, a DNS record to add once the
    # addresses are known. Written by an admin (a stack is saved by one), run by
    # whoever launches the stack, as everything in a stack is.

    def _script(self, run, step, record, context, handed, given):
        with self._lock:
            done, handed_now = dict(context), dict(handed)
        script = _render(step["script"], done, given, handed_now)
        # What a launch's modules see, so the same names work in both: the
        # earlier launches' WATERPUMP_STACK_* lists, every value entered at
        # launch under its own name, and this step's parameters on top --
        # rendered like a launch's, so a stack variable, a secret or an
        # earlier step's address is a drag away.
        env = dict(os.environ, **_exports(run["stack"], done))
        env.update(given.params)
        env.update({k: _render(v, done, given, handed_now) for k, v in step["params"].items()})
        env["WATERPUMP_STACK_STEP"] = step["id"]
        # The stack's secrets and anything entered at launch, scrubbed from
        # what is kept: the output rides on a record anyone may read.
        hidden = list(given.secrets.values()) + list(given.params.values())
        timeout = step["timeout_seconds"]
        self._update(record, detail="Running on %s (up to %ds)"
                     % (self.cluster.local_name(), timeout))
        # The script is a file, not `sh -c`: rendered, it may hold a secret,
        # and an argument is readable by every user through /proc.
        workdir = tempfile.mkdtemp(prefix=".stack-script-", dir=store.data_dir())
        started = time.monotonic()
        try:
            path = os.path.join(workdir, "script.sh")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(script)
            process = subprocess.Popen(
                ["/bin/sh", path], cwd=workdir, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                # Its own session, so a timeout or a cancel can stop everything
                # it started, not just the shell.
                start_new_session=True)
            collected = []
            reader = threading.Thread(target=lambda: collected.append(process.stdout.read()),
                                      daemon=True, name="waterpump-stack-script-%s" % step["id"])
            reader.start()
            stopped = None
            halted = None
            while process.poll() is None:
                halted = self._halted(run)
                if self._cancelling(run):
                    stopped = "cancelled"
                elif halted:
                    stopped = "halted"
                elif time.monotonic() - started > timeout:
                    stopped = "timeout"
                if stopped:
                    _stop_group(process)
                    break
                time.sleep(0.2)
            process.wait()
            reader.join(timeout=SCRIPT_KILL_GRACE)
            raw = collected[0] if collected else b""
            output = redact(raw.decode("utf-8", "replace"), hidden)
            if len(output) > SCRIPT_OUTPUT_LIMIT:
                output = "…" + output[-SCRIPT_OUTPUT_LIMIT:]
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        seconds = time.monotonic() - started
        code = process.returncode
        eventlog.event("change", "stack.script", level=logging.INFO if code == 0 and not stopped
                       else logging.WARNING, stack=run["stack"], step=step["id"],
                       exit_code=code, seconds=round(seconds, 1), stopped=stopped)
        self._update(record, exit_code=code, output=output or None, detail=None)
        if stopped == "cancelled":
            raise _Cancelled()
        if stopped == "halted":
            raise _Halted(halted)
        if stopped == "timeout":
            raise ServiceError("Timed out after %ds, and was stopped." % timeout)
        if code != 0:
            last = [line for line in (output or "").splitlines() if line.strip()][-1:]
            raise ServiceError("Exited with code %d%s" % (code, ": " + last[0] if last else ""))
        self._update(record, state=DONE, finished_at=time.time(),
                     detail="exit 0 in %.1fs" % seconds)

    # -- launch steps ------------------------------------------------------

    def _launch(self, run, step, record, context, handed, params, followers):
        name = step["template"]
        # An earlier launch of the same template may still be bootstrapping;
        # its run holds the template until then.
        for _, template, thread in list(followers):
            if template == name:
                self._update(record, detail="Waiting for the earlier launch of '%s'" % name)
                while thread.is_alive():
                    self._sleep(run, RUN_POLL_SECONDS)
        self._check_running(run)

        with self._lock:
            done, handed_now = dict(context), dict(handed)
        # Only what was entered at launch reaches every module; the stack's
        # variables and secrets go where a step's parameter names them.
        values = dict(_exports(run["stack"], done), **params.params)
        # `params` is the run's _Given: entered values, variables and secrets.
        rendered = {k: _render(v, done, params, handed_now)
                    for k, v in step["params"].items()}
        values.update(_secrets_by_name(name, rendered, params))
        values.update(rendered)
        # Known before anything is launched, so recorded now: what this step
        # runs with is settled, unlike its instances' addresses.
        template = store.load_templates().get(name)
        if template is not None:
            mine = handed_values(template, rendered, discover_modules())
            with self._lock:
                handed[step["id"]] = mine
        # Where it will actually go: a group member in maintenance is skipped,
        # and a template cloning a snapshot goes to the snapshot's node.
        targets, _ = self.cluster.launch_targets(*self.cluster.template_targets(
            name, step["nodes"], step["groups"]))
        before = self._template_keys(name, targets)
        self._update(record, nodes=targets, detail="Launching %d" % step["count"])
        swarming = run["action"] == "swarm"
        started = self.cluster.launch_template(
            name, count=step["count"], nodes=step["nodes"] or None,
            groups=step["groups"] or None, params=values,
            prefix=step["prefix"] or None, background=True,
            stack={"stack": run["stack"], "step": step["id"],
                   "swarm": step.get("swarm") or run.get("swarm"),
                   "run": run["id"] if swarming else None})
        if swarming:
            with self._lock:
                self._launched.setdefault(run["id"], []).append(
                    (name, started.get("started_at")))
        # Where the launch put its shares: a group's members that got none --
        # fewer instances than nodes -- are not where it is.
        placed = [n for n in started.get("nodes") or [] if n in targets] or targets
        self._update(record, nodes=placed, detail="Launching %d on %s" % (
            step["count"], ", ".join(placed)))

        if step["wait_bootstrap"]:
            finished = self._await_template_run(name, started, run)
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
                finished = self._await_template_run(name, started, run)
                self._finish_launch(step, record, finished, targets, None)
            except _Cancelled:
                self._update(record, state=CANCELLED, finished_at=time.time(), detail=None)
            except _Halted as exc:
                self._update(record, state=CANCELLED, finished_at=time.time(),
                             detail=str(exc))
            except (ServiceError, ClusterError) as exc:
                self._update(record, state=FAILED, finished_at=time.time(),
                             error=str(getattr(exc, "message", None) or exc))
            except Exception as exc:                      # noqa: BLE001
                self._update(record, state=FAILED, finished_at=time.time(),
                             error="Unexpected error: %s" % exc)

        thread = threading.Thread(target=eventlog.carry(follow), daemon=True,
                                  name="waterpump-stack-follow-%s" % step["id"])
        with self._lock:
            followers.append((step["id"], name, thread))
        thread.start()

    def _finish_launch(self, step, record, finished, targets, context):
        """Record how a launch's template run ended; ``context`` None keeps outputs."""
        local = self.cluster.local_name()
        result = finished.get("result") or {}
        instances = [dict({"name": i.get("name"), "node": i.get("node") or local,
                           "ok": bool(i.get("ok")), "error": _instance_error(i)},
                          **({"output": _instance_output(i)} if _instance_output(i) else {}))
                     for i in result.get("instances") or []]
        failed = [i for i in instances if not i["ok"]]
        summary = "%d of %d failed: %s" % (len(failed), len(instances), "; ".join(
            "%s: %s" % (i["name"], i["error"] or "failed") for i in failed)) if failed else None
        notes = list(result.get("notes") or finished.get("notes") or [])
        error = finished.get("error")
        if failed and not error:
            # Tolerated when enough came up: what failed is still said, and
            # only what came up is handed on.
            needed = -(-step["count"] * step.get("min_percent", 100) // 100)
            if len(instances) - len(failed) >= needed and step.get("min_percent", 100) < 100:
                notes.insert(0, "%s (allowed: at least %d%% must start)"
                             % (summary, step["min_percent"]))
            else:
                error = summary
        if not instances and not error:
            error = "The launch created nothing."
        changes = {"instances": instances, "error": error,
                   "state": FAILED if error else DONE, "finished_at": time.time(),
                   "detail": "; ".join(notes) or None}
        if instances:
            changes["nodes"] = sorted({i["node"] for i in instances})
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

    def _await_template_run(self, template, started, run=None):
        """Follow the template run a launch step started until it finishes.

        A swarm being stopped -- or failed by another step -- stops waiting:
        its load is meant to end now, not once every generator has finished
        its bootstrap -- which is often the test itself. Its teardown then destroys what there is, and keeps
        sweeping until this launch is over. A stack's own launch is left to
        finish, as a cancel always has.
        """
        if started.get("finished_at") is not None:
            return started
        marker = started.get("started_at")
        while True:
            if run is not None and run["action"] == "swarm":
                self._check_running(run)
            time.sleep(RUN_POLL_SECONDS)
            current = next((r for r in self.service.template_runs()
                            if r["template"] == template and r["started_at"] == marker), None)
            if current is None:
                raise ServiceError("The launch of '%s' stopped being reported; its result "
                                   "was dismissed before the stack read it." % template, 409)
            if current["finished_at"] is not None:
                return current

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
                    "ipv6": c.get("ipv6") or [], "stale": c.get("stale") or [],
                    "step": c.get("stack_step"), "swarm": c.get("swarm"),
                    "swarm_run": c.get("swarm_run")})
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

    def _confirmed(self, name, confirmed, swarm=None):
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
        if swarm is not None:
            members = [m for m in members if m.get("swarm") == swarm]
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
        self._refuse_busy(name)
        members = self._confirmed(name, instances)
        if not members:
            raise ServiceError("Stack '%s' has no instances." % name, 404)
        result = self.cluster.change_state(members, action)
        return dict(result, stack=name, action=action)

    def _teardown(self, step, record, run):
        if step["instances"] is None:
            self._swarm_teardown(step, record, run)
            return
        doomed = step["instances"]
        self._update(record, detail="Destroying %d" % len(doomed))
        results = self._destroy(doomed)
        failed = [r for r in results if not r["ok"]]
        self._update(
            record, instances=[{"node": r["node"], "name": r["name"], "ok": r["ok"],
                                "error": r["error"]} for r in results],
            state=FAILED if failed else DONE, finished_at=time.time(), detail=None,
            error="Could not destroy %s" % "; ".join(
                "%s: %s" % (r["name"], r["error"]) for r in failed) if failed else None)

    def _swarm_teardown(self, step, record, run):
        """A swarm's own end: what *this run* made, found by its run tag.

        Not every instance of the swarm -- another run of it may be going, and
        those are its to destroy. After a Stop, a launch may still be creating
        instances, so the sweep repeats until every launch this run started has
        finished, then once more.
        """
        done, results = set(), []
        while True:
            with self._lock:
                launches = list(self._launched.get(run["id"], []))
            settled = not any(self._template_running(t, marker) for t, marker in launches)
            doomed = [{"node": m["node"], "name": m["name"]}
                      for m in self._members(run["stack"])
                      if m.get("swarm") == step["swarm"] and m.get("swarm_run") == run["id"]
                      and (m["node"], m["name"]) not in done]
            if doomed:
                self._update(record, detail="Destroying %d" % (len(done) + len(doomed)))
                for r in self._destroy(doomed):
                    done.add((r["node"], r["name"]))
                    results.append(r)
                self._update(record, count=len(results), instances=[
                    {"node": r["node"], "name": r["name"], "ok": r["ok"], "error": r["error"]}
                    for r in results])
            if settled:
                break
            self._update(record, detail="Destroyed %d; waiting for a launch still under way"
                         % len(results))
            time.sleep(TEARDOWN_SWEEP_SECONDS)
        with self._lock:
            self._launched.pop(run["id"], None)
        failed = [r for r in results if not r["ok"]]
        self._update(
            record, state=FAILED if failed else DONE, finished_at=time.time(),
            detail=None if results else "Nothing to destroy",
            error="Could not destroy %s" % "; ".join(
                "%s: %s" % (r["name"], r["error"]) for r in failed) if failed else None)

    def _template_running(self, template, marker):
        return any(r["template"] == template and r["started_at"] == marker
                   and r["finished_at"] is None for r in self.service.template_runs())

    def _destroy(self, doomed):
        results = self.cluster.delete_containers(doomed, force=True)["instances"]
        # An ephemeral instance deletes itself when stopped; gone is the goal.
        for r in results:
            if not r["ok"] and "not found" in (r["error"] or "").lower():
                r.update(ok=True, error=None)
        return results

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
        ids = {step_id for step_id, _, _ in earlier}
        waiting_on = [t for step_id, _, t in list(followers) if step_id in ids]
        if any(t.is_alive() for t in waiting_on):
            self._update(record, detail="Waiting for earlier launches to finish bootstrapping")
            while any(t.is_alive() for t in waiting_on):
                self._sleep(run, RUN_POLL_SECONDS)
        with self._lock:
            launched = [record for _, _, record in earlier]
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
            self._update(record, detail=_waiting_detail(keys, verdicts, len(green)))
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

    def stop_swarms(self):
        """Stop every swarm running here, for a `serve` that is stopping; their
        teardown runs as on any Stop. Returns what was stopped."""
        stopped = []
        with self._lock:
            for run in self._runs.values():
                if run["action"] == "swarm" and run["finished_at"] is None:
                    run["cancelling"] = True
                    stopped.append("%s of %s" % (" then ".join(_sequence(run)),
                                                 run["stack"]))
        return stopped

    def run_report(self, name):
        """The stack's last run, finished, as a Markdown report.

        Only finished: a report of a run still going would be out of date
        before it was read, and the run record is what the tab shows meanwhile.
        """
        with self._lock:
            run = self._runs.get(name)
            run = _copy(run) if run else None
        if run is None:
            raise ServiceError("No recorded run for stack '%s'." % name, 404)
        if run["finished_at"] is None:
            raise ServiceError("That run is still in progress; its report is ready when it "
                               "has finished.", 409)
        return {"stack": name, "filename": stackreport.filename(run),
                "markdown": stackreport.render(run, self.cluster.local_name())}

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


def _what(run):
    """"swarm heavy", "launch": what a run is doing, for a busy message."""
    if run.get("swarm") and run.get("action") == "swarm":
        sequence = _sequence(run)
        if len(sequence) > 1:
            return "swarm sequence %s (now %s)" % (" then ".join(sequence), run["swarm"])
        return "swarm %s" % run["swarm"]
    return str(run.get("action") or "run")


def _sequence(run):
    """Every swarm a run is for, in order; a peer's older record has only `swarm`."""
    return run.get("swarms") or ([run["swarm"]] if run.get("swarm") else [])


def _copy(run):
    return dict(run, swarms=list(run.get("swarms") or []),
                stages=[dict(stage, steps=[dict(s, instances=[dict(i) for i in s["instances"]])
                                           if "instances" in s else dict(s)
                                           for s in stage["steps"]])
                        for stage in run["stages"]])


def _not_green(record):
    if record["status"] != "healthy":
        reasons = "; ".join(record.get("reasons") or [])
        return record["status"] + (": " + reasons if reasons else "")
    app = record.get("app")
    if app and app.get("status") != "ok":
        return "app check %s" % app.get("status")
    return None


def _waiting_detail(keys, verdicts, green):
    """"2 of 4 healthy; waiting on web-1 on lxd2 (unhealthy: ...)".

    The reasons, not just the count: a gate that will only ever time out --
    an app check failing the same way every round -- says so from its first
    round rather than after its whole timeout. Two named at most, since every
    client re-reads this on each poll.
    """
    pending = [k for k in keys if verdicts[k] is not None]
    text = "%d of %d healthy" % (green, len(keys))
    if not pending:
        return text
    named = "; ".join("%s (%s)" % (_label(k, keys), verdicts[k]) for k in pending[:2])
    more = " and %d more" % (len(pending) - 2) if len(pending) > 2 else ""
    return ("%s; waiting on %s%s" % (text, named, more))[:400]


def _label(key, keys):
    nodes = {node for node, _ in keys}
    return key[1] if len(nodes) == 1 else "%s on %s" % (key[1], key[0])


def _instance_error(instance):
    """Why one instance of a launch failed, with the module's own last words.

    The module's full output stays in the template run on the node that ran
    it; the stack's record is the only one most people read, so it carries
    the exit code and the last line that says anything.
    """
    if instance.get("error"):
        return instance["error"]
    bootstrap = (instance.get("container") or {}).get("bootstrap") or {}
    failed = next((m for m in bootstrap.get("modules") or [] if m.get("exit_code")), None)
    if not failed:
        return None
    said = [line.strip() for line in ((failed.get("stderr") or "") + "\n"
                                      + (failed.get("stdout") or "")).splitlines()
            if line.strip()]
    stderr = [line.strip() for line in (failed.get("stderr") or "").splitlines()
              if line.strip()]
    last = (stderr or said)[-1:]
    return ("module '%s' failed (exit %s)%s" % (
        failed["name"], failed["exit_code"], ": " + last[0][:200] if last else ""))


def _instance_output(instance):
    """What an instance's modules said last when they succeeded -- a swarm's
    load generator's result, say. Kept short: it rides on every poll."""
    bootstrap = (instance.get("container") or {}).get("bootstrap") or {}
    said = [(m.get("stdout") or "").strip() for m in bootstrap.get("modules") or []
            if not m.get("exit_code")]
    said = [text for text in said if text]
    return said[-1].splitlines()[-1][:300] if said else None


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
    """Whether a value is nothing but ``{{params.NAME}}`` or ``{{secrets.NAME}}``.

    The one form a secret may take in a saved stack: it names a value entered
    at launch, or one of the stack's own secrets, instead of carrying one. A save refuses anything else and
    a listing strips it, so the two must agree about what "anything else" is --
    hence one predicate rather than the same condition written twice.
    """
    return not PLACEHOLDER.sub("", value).strip() and all(
        INPUT_REFERENCE.match(reference) or SECRET_REFERENCE.match(reference)
        for reference in PLACEHOLDER.findall(value))


def public_stack(stack, templates, modules):
    """A stack as it may be served: a step's literal secret dropped.

    ``_check_stages()`` refuses one on save, so this only fires for a record
    that never went through a save -- a file written or copied into the store
    by hand, which every record here is built to expect. It is the same reason
    a template's listing re-derives its public selection instead of trusting
    what is on disk: the file is untrusted input, so the check belongs on the
    way out as well as on the way in.
    """
    def public(stages):
        return [dict(stage, steps=[_public_step(step, templates, modules)
                                   for step in stage["steps"]]) for stage in stages]
    return dict(stack, stages=public(stack["stages"]),
                swarms=[dict(w, stages=public(w["stages"])) for w in stack["swarms"]])


def _public_step(step, templates, modules):
    """One step as it may be served; see ``public_stack()``.

    Only a launch's parameters are a module's: a script step's are its own
    environment variables, which no module declares secret or not.
    """
    params = step.get("params") or {}
    if step["type"] != "launch" or not params:
        return step
    template = templates.get(step["template"])
    # A step naming a template this node does not have cannot say which of
    # its parameters are secret, so every name that is secret in any module
    # counts. The record is unusable as it stands -- a launch refuses a
    # template that is not here -- so failing closed costs a working stack
    # nothing.
    secrets = secret_param_names(
        [modules[m] for m in template["bootstrap"]["modules"] if m in modules]
        if template is not None else modules.values())
    kept = {k: v for k, v in params.items()
            if split_param(k)[0] not in secrets or _input_only(v)}
    return step if len(kept) == len(params) else dict(step, params=kept)


def _check_variables(raw):
    """A stack's variables as stored, or raise: ``{NAME: {value, description}}``.

    A bare string is taken as the value, which is what a hand-written stack
    file or a quick ``--json`` body most likely says.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ServiceError("variables must be an object of NAME: value.")
    if len(raw) > store.STACK_VALUES_MAX:
        raise ServiceError("A stack has at most %d variables." % store.STACK_VALUES_MAX)
    cleaned = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not store.STACK_VALUE_NAME.match(name):
            raise ServiceError("Invalid variable name '%s'. Use letters, digits and "
                               "underscores, starting with a letter or _." % name)
        entry = {"value": entry} if not isinstance(entry, dict) else entry
        value = entry.get("value")
        if value is None:
            value = ""
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ServiceError("Variable %s: the value must be text." % name)
        if len(str(value)) > store.STACK_VARIABLE_BYTES:
            raise ServiceError("Variable %s: at most %d characters."
                               % (name, store.STACK_VARIABLE_BYTES))
        cleaned[name] = {"value": str(value),
                         "description": str(entry.get("description") or "")[:200]}
    return cleaned


def _check_secret_declarations(raw):
    """A stack's secrets as declared: ``{NAME: {description, generate}}``, never a value.

    A declaration carrying one is refused outright rather than stripped: the
    value would be lost without anyone noticing, and the caller plainly meant
    to set it -- which is what the secret's own route is for.
    """
    if raw is None:
        return {}
    if isinstance(raw, list):
        raw = {name: {} for name in raw}
    if not isinstance(raw, dict):
        raise ServiceError("secrets must be an object of NAME: {description, generate}.")
    if len(raw) > store.STACK_VALUES_MAX:
        raise ServiceError("A stack has at most %d secrets." % store.STACK_VALUES_MAX)
    cleaned = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not store.STACK_VALUE_NAME.match(name):
            raise ServiceError("Invalid secret name '%s'. Use letters, digits and "
                               "underscores, starting with a letter or _." % name)
        entry = entry if isinstance(entry, dict) else {}
        if "value" in entry:
            raise ServiceError("Secret %s: a stack holds no secret values. Save the stack, "
                               "then set it with its own route (or `waterpump "
                               "stack-secret-set`)." % name)
        cleaned[name] = {"description": str(entry.get("description") or "")[:200],
                         "generate": entry.get("generate") is True}
    return cleaned


def _listed(stack):
    """A stack as listed: what its launch and each swarm must be given, and
    which of its secrets hold a value here."""
    return _with_secret_status(dict(
        stack, inputs=stack_inputs(stack),
        swarms=[dict(w, inputs=swarm_inputs(w)) for w in stack["swarms"]]))


def _with_secret_status(stack):
    """A stack whose secret declarations say whether this node holds a value."""
    held = store.stack_secret_status(stack["name"])
    secrets = {name: dict(declared, set=name in held,
                          generated=held.get(name, {}).get("generated", False),
                          updated_at=held.get(name, {}).get("updated_at") or None)
               for name, declared in (stack.get("secrets") or {}).items()}
    return dict(stack, secrets=secrets)


def stack_inputs(stack):
    """Names a launch of the stack must be given, for its ``{{params.NAME}}``."""
    return _inputs(stack["stages"])


def sequence_of(stack):
    """The swarms the stack's sequence runs, in order."""
    return [w["name"] for w in stack["swarms"] if w.get("sequence", True)]


def swarm_inputs(swarm):
    """Names a swarm must be given when started, for its ``{{params.NAME}}``."""
    return _inputs(swarm["stages"])


def _inputs(stages):
    return sorted({m.group(1) for stage in stages for step in stage["steps"]
                   for value in list((step.get("params") or {}).values())
                   + ([step["script"]] if step.get("type") == "script" else [])
                   for m in (INPUT_REFERENCE.match(r) for r in PLACEHOLDER.findall(value))
                   if m})


class _Given:
    """What a run is handed from outside its steps: values entered at launch,
    the stack's variables and its secrets. Passed down the run's threads, and
    never put in the run record, which every poll of the UI re-sends."""

    __slots__ = ("params", "vars", "secrets")

    def __init__(self, params, variables, secrets):
        self.params, self.vars, self.secrets = params, variables, secrets


def _explain(value, context, given, handed, secret_state, launches, own, in_swarm):
    """A value as a preview shows it: ``{value, resolved, refs}``.

    ``resolved`` is the value with every reference that is known now filled in
    and the rest left as ``{{...}}``; each reference says where it comes from
    and whether it is known yet. A secret is never filled in.
    """
    refs, unknown = [], False
    for reference in dict.fromkeys(PLACEHOLDER.findall(value)):
        entry = {"ref": reference}
        entered, variable = INPUT_REFERENCE.match(reference), VAR_REFERENCE.match(reference)
        secret, earlier = SECRET_REFERENCE.match(reference), PARAM_REFERENCE.match(reference)
        plain = REFERENCE.match(reference)
        if entered:
            known = entered.group(1) in given.params
            entry.update(source="entered at %s" % ("start" if in_swarm else "launch"),
                         known=known, masked=True)
        elif variable:
            entry.update(source="stack variable", known=variable.group(1) in given.vars)
        elif secret:
            entry.update(source="stack secret, %s" % secret_state.get(secret.group(1), "unknown"),
                         known=True, masked=True)
        elif earlier or plain:
            step = (earlier or plain).group(1)
            what = "parameter of %s" % step if earlier else "from %s" % step
            settled = bool(earlier) and earlier.group(2) in (handed.get(step) or {})
            if settled and (step in own or not in_swarm):
                entry.update(source="%s, set in the stack" % what, known=True)
            elif in_swarm and step in own:
                entry.update(source="%s, once the swarm's stage %d has run"
                             % (what, own[step] + 1), known=False)
            elif in_swarm and step in launches:
                entry.update(source="%s, the running stack" % what
                             if step in context or (earlier and step in handed)
                             else "%s, the running stack -- which has none" % what,
                             known=step in context or bool(earlier and step in handed))
            elif step in launches:
                entry.update(source="%s, once stage %d has run" % (what, launches[step] + 1),
                             known=False)
            else:
                entry.update(source="nothing: no such launch", known=False)
        else:
            entry.update(source="not a reference", known=False)
        unknown = unknown or not entry["known"]
        refs.append(entry)

    def fill(match):
        reference = match.group(1)
        if SECRET_REFERENCE.match(reference) or INPUT_REFERENCE.match(reference):
            known = not INPUT_REFERENCE.match(reference) or \
                INPUT_REFERENCE.match(reference).group(1) in given.params
            return "********" if known else match.group(0)
        if VAR_REFERENCE.match(reference):
            return given.vars.get(VAR_REFERENCE.match(reference).group(1), match.group(0))
        earlier, plain = PARAM_REFERENCE.match(reference), REFERENCE.match(reference)
        if earlier and earlier.group(2) in (handed.get(earlier.group(1)) or {}):
            return handed[earlier.group(1)][earlier.group(2)]
        if plain and plain.group(1) in context:
            return _field(context[plain.group(1)], plain.group(2))
        return match.group(0)
    return {"value": value, "resolved": PLACEHOLDER.sub(fill, value), "refs": refs,
            "complete": not unknown}


def _static_handed(stages, given):
    """``{step id: {param: value}}`` for the parameters a launch will run with
    that are settled before anything runs.

    Settled means its value is the template's own, or the step's built only
    from stack variables and other settled parameters. One entered at launch
    is left out (it may be a password), as is a secret, an address -- and
    anything built from one of those.
    """
    templates, modules = store.load_templates(), discover_modules()
    known = {}
    for stage in stages:
        for step in stage["steps"]:
            template = templates.get(step.get("template")) if step["type"] == "launch" \
                else None
            if template is None:
                continue
            settled, unsettled = {}, set()
            for key, value in step["params"].items():
                if all(_settled(reference, given, known)
                       for reference in PLACEHOLDER.findall(value)):
                    settled[key] = _render(value, {}, given, known)
                else:
                    unsettled.add(key)
            values = handed_values(template, settled, modules)
            known[step["id"]] = {k: v for k, v in values.items() if k not in unsettled}
    return known


def _settled(reference, given, known):
    variable = VAR_REFERENCE.match(reference)
    if variable:
        return variable.group(1) in given.vars
    earlier = PARAM_REFERENCE.match(reference)
    return bool(earlier) and earlier.group(2) in (known.get(earlier.group(1)) or {})


def _secrets_by_name(template_name, step_params, given):
    """The stack's secrets that fill a module secret of the same name.

    A stack secret called POSTGRES_PASSWORD is meant for every PostgreSQL in
    the stack: it goes to each launch whose modules declare that secret, unless
    the step sets the parameter itself or a value of that name was entered at
    launch -- both of which are someone saying what this one should get.
    """
    if not given.secrets:
        return {}
    template = store.load_templates().get(template_name)
    if template is None:
        return {}
    modules = discover_modules()
    wanted = secret_param_names([modules[m] for m in template["bootstrap"]["modules"]
                                 if m in modules])
    return {name: given.secrets[name] for name in wanted
            if name in given.secrets and name not in step_params
            and not given.params.get(name)}


def _stop_group(process):
    """Stop a script and everything it started: TERM, then KILL after a grace."""
    for sig, wait in ((signal.SIGTERM, SCRIPT_KILL_GRACE), (signal.SIGKILL, None)):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        if wait is None:
            return
        try:
            process.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _render(value, context, given, handed):
    """Fill in ``{{step.field}}`` and ``{{step.param.X}}`` from earlier launches,
    ``{{params.X}}`` from the launch, ``{{vars.X}}``/``{{secrets.X}}`` from the stack."""
    def one(match):
        entered = INPUT_REFERENCE.match(match.group(1))
        if entered:
            return given.params.get(entered.group(1), "")
        variable = VAR_REFERENCE.match(match.group(1))
        if variable:
            return given.vars.get(variable.group(1), "")
        secret = SECRET_REFERENCE.match(match.group(1))
        if secret:
            return given.secrets.get(secret.group(1), "")
        earlier = PARAM_REFERENCE.match(match.group(1))
        if earlier:
            return (handed.get(earlier.group(1)) or {}).get(earlier.group(2), "")
        reference = REFERENCE.match(match.group(1))
        if not reference or reference.group(1) not in context:
            return ""
        return _field(context[reference.group(1)], reference.group(2))
    return PLACEHOLDER.sub(one, value)


def handed_names(template, step_params, modules):
    """Parameter names a launch of ``template`` may hand on: every one its
    modules declare, and any the step sets, less the secrets.

    The secrets are left out because a later step's module may not declare
    the name it receives one under as secret, and then nothing would redact it.
    """
    selected = [modules[m] for m in template["bootstrap"]["modules"] if m in modules]
    secrets = secret_param_names(selected)
    names = {param_key(p["name"], n)
             for m, n in occurrences(template["bootstrap"]["modules"]) if m in modules
             for p in modules[m]["params"]}
    names.update(str(k) for k in step_params or {})
    return {k for k in names if split_param(k)[0] not in secrets}


def handed_values(template, rendered, modules):
    """What each of ``handed_names()`` is when the launch runs.

    Merged the way ``_launch_bootstrap()`` merges them -- the template's saved
    answers, the step's on top -- and read per occurrence the way the runner
    reads them, so the value handed on is the value the module was given. A
    template's selection already holds every non-secret parameter its modules
    declare, so the module's own default is only for one saved before that.
    """
    values = dict(template["bootstrap"]["params"])
    values.update(rendered)
    defaults = {}
    for m, n in occurrences(template["bootstrap"]["modules"]):
        for p in (modules.get(m) or {}).get("params", []):
            defaults.setdefault(param_key(p["name"], n), p.get("default"))
    result = {}
    for key in handed_names(template, rendered, modules):
        name, n = split_param(key)
        value = occurrence_value(values, name, n)
        if value is None:
            value = defaults.get(key)
        result[key] = "" if value is None else str(value)
    return result


def _exports(stack, context):
    """The environment every later launch's modules see."""
    env = {"WATERPUMP_STACK": stack}
    for step_id, outputs in context.items():
        base = "WATERPUMP_STACK_%s_" % step_id.upper()
        env[base + "NAMES"] = " ".join(outputs["names"])
        env[base + "IPS"] = " ".join(outputs["ips"])
        env[base + "IPV6"] = " ".join(outputs["ipv6"])
        env[base + "NODES"] = " ".join(outputs["nodes"])
    return env

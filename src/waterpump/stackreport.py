"""A finished stack run as a Markdown report, for keeping or passing on.

Built from the run record alone -- the same record every poll sends -- so it
says nothing the Stacks tab could not already show, and nothing that record
leaves out: a script's output and a module's last lines are already redacted
of the run's secrets and inputs before they get there. Rendered here, once,
so the web UI's download and the CLI's file are the same document.
"""

from __future__ import annotations

import re
import time

RESULT = {"passed": "✅ Passed", "failed": "❌ Failed", "stopped": "⏹ Stopped"}
STEP_STATE = {"done": "✅ done", "failed": "❌ failed", "cancelled": "⏹ stopped",
              "skipped": "⏭ skipped", "pending": "· not started", "running": "… running",
              "ready": "… bootstrapping"}


def render(run, node):
    """``run``: a finished stack run record; ``node``: where it ran."""
    sequence = list(run.get("swarms") or [])
    swarming = run.get("action") == "swarm"
    lines = ["# %s" % _title(run, sequence, swarming), ""]
    rows = [("Result", RESULT[_outcome(run)]), ("Stack", _code(run["stack"]))]
    if swarming:
        rows.append(("Swarm" if len(sequence) == 1 else "Sequence",
                     " → ".join(_code(w) for w in sequence)))
    else:
        rows.append(("Action", run.get("action") or "run"))
    rows += [("Started", _when(run["started_at"])), ("Finished", _when(run["finished_at"])),
             ("Duration", _duration(run["finished_at"] - run["started_at"])),
             ("Run on", _code(node)), ("Run id", _code(run.get("id") or "-"))]
    if run.get("error"):
        rows.append(("Error", _cell(run["error"])))
    lines += _table(("", ""), rows) + [""]

    against = run.get("against") or {}
    if against:
        lines += ["## Against the stack", "",
                  "What the swarm%s pointed at: the stack's running instances when it started."
                  % ("s were" if len(sequence) > 1 else " was"), ""]
        lines += _table(("Step", "Instances", "Addresses", "Nodes"), [
            (_code(step), _cell(", ".join(found.get("names") or [])) or "-",
             _cell(", ".join(found.get("ips") or [])) or "-",
             _cell(", ".join(found.get("nodes") or [])) or "-")
            for step, found in sorted(against.items())]) + [""]
    variables = run.get("variables") or {}
    if variables:
        lines += ["## Variables", ""]
        lines += _table(("Name", "Value"), [(_code(k), _code(v) if v != "" else "*(empty)*")
                                            for k, v in sorted(variables.items())]) + [""]

    groups = _groups(run)
    if swarming and len(sequence) > 1:
        lines += ["## Sequence", ""]
        lines += _table(("#", "Swarm", "Result", "Duration"), [
            (str(n), _code(swarm), _swarm_result(steps), _span(steps))
            for n, (swarm, steps) in enumerate(_by_swarm(groups, sequence), 1)]) + [""]
        for n, (swarm, steps) in enumerate(_by_swarm(groups, sequence), 1):
            lines += ["## %d. %s — %s" % (n, swarm, _swarm_result(steps)), ""]
            lines += _steps(steps, "###")
    else:
        for index, steps in groups:
            if not swarming:
                lines += ["## Stage %d" % (index + 1), ""]
            lines += _steps(steps, "###" if not swarming else "##")
    lines.append("---")
    lines.append("*Exported from waterpump on %s.*" % _when(time.time()))
    return "\n".join(lines).rstrip() + "\n"


def filename(run):
    """``drill-smoke-heavy-20261012-1530.md``: what was run, and when."""
    parts = [run["stack"]] + list(run.get("swarms") or [])
    if run.get("action") != "swarm":
        parts.append(run.get("action") or "run")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", "-".join(parts)).strip("-.") or "stack"
    return "%s-%s.md" % (stem[:80], time.strftime("%Y%m%d-%H%M",
                                                  time.localtime(run["started_at"])))


def _title(run, sequence, swarming):
    if swarming and len(sequence) > 1:
        return "Swarm sequence on %s" % run["stack"]
    if swarming:
        return "Swarm %s on %s" % (sequence[0] if sequence else run.get("swarm"), run["stack"])
    return "Stack %s: %s" % (run["stack"], run.get("action") or "run")


def _outcome(run):
    return "passed" if run.get("ok") else "stopped" if run.get("cancelled") else "failed"


def _groups(run):
    """``[(stage index, [(step record, stage swarm)])]``."""
    return [(index, [(step, stage.get("swarm")) for step in stage["steps"]])
            for index, stage in enumerate(run["stages"])]


def _by_swarm(groups, sequence):
    found = {swarm: [] for swarm in sequence}
    for _, steps in groups:
        for step, swarm in steps:
            found.setdefault(swarm, []).append((step, swarm))
    return [(swarm, found[swarm]) for swarm in sequence]


def _swarm_result(steps):
    states = [step["state"] for step, _ in steps]
    if any(s == "failed" for s in states):
        return "❌ failed"
    if states and all(s == "done" for s in states):
        return "✅ passed"
    if states and all(s == "skipped" for s in states):
        return "⏭ skipped"
    if any(s == "cancelled" for s in states):
        return "⏹ stopped"
    return "· not finished"


def _span(steps):
    started = [s["started_at"] for s, _ in steps if s.get("started_at")]
    finished = [s["finished_at"] for s, _ in steps if s.get("finished_at")]
    if not started or not finished:
        return "-"
    return _duration(max(finished) - min(started))


def _steps(steps, heading):
    lines = []
    for step, _ in steps:
        took = (" (%s)" % _duration(step["finished_at"] - step["started_at"])
                if step.get("started_at") and step.get("finished_at") else "")
        lines += ["%s %s — %s%s" % (heading, _what(step), STEP_STATE.get(step["state"],
                                                                         step["state"]), took),
                  ""]
        if step.get("error"):
            lines += ["**Error:** %s" % _inline(step["error"]), ""]
        elif step.get("detail") and step["state"] in ("done", "failed", "cancelled"):
            # A finished launch's detail is its notes: a node skipped,
            # failures the step was allowed; a stopped step's, why.
            lines += ["%s" % _inline(step["detail"]), ""]
        if step["type"] == "launch":
            lines += _launch(step)
        elif step["type"] == "destroy" and step.get("instances"):
            gone = [i for i in step["instances"] if i.get("ok")]
            lines += ["Destroyed %d of %d: %s" % (
                len(gone), len(step["instances"]),
                ", ".join(_code(i["name"]) for i in step["instances"])), ""]
            lines += ["- %s: %s" % (_code(i["name"]), _inline(i["error"]))
                      for i in step["instances"] if i.get("error")]
        elif step["type"] == "script":
            if step.get("exit_code") is not None:
                lines += ["Exit code: %s" % _code(str(step["exit_code"])), ""]
            if step.get("output"):
                lines += ["Output:", ""] + _fence(step["output"]) + [""]
    return lines


def _what(step):
    if step["type"] == "launch":
        return "%s: launch %s ×%d" % (step["id"], step.get("template"), step.get("count") or 0)
    if step["type"] == "destroy":
        return "Teardown" if step.get("swarm") else "Destroy"
    if step["type"] == "script":
        return "%s: script" % step["id"]
    if step["type"] == "sleep":
        return "%s: sleep" % step["id"]
    return "%s: wait until healthy" % step["id"]


def _launch(step):
    lines = []
    if step.get("nodes"):
        lines += ["On %s." % ", ".join(_code(n) for n in step["nodes"]), ""]
    instances = step.get("instances") or []
    if instances:
        lines += _table(("Instance", "Node", "Address", "Result"), [
            (_code(i["name"]), _code(i["node"]), _code(i["ipv4"]) if i.get("ipv4") else "-",
             "✅ ok" if i.get("ok") else ("❌ %s" % _cell(i.get("error") or "failed"))
             if i.get("ok") is False else "-")
            for i in instances]) + [""]
    said = [i for i in instances if i.get("output")]
    if said:
        # What each instance's modules said last: a load generator's result.
        lines += ["Results:", ""]
        lines += _fence("\n".join("%s: %s" % (i["name"], i["output"]) for i in said)) + [""]
    return lines


def _table(header, rows):
    return (["| %s |" % " | ".join(header), "|%s|" % "|".join("---" for _ in header)]
            + ["| %s |" % " | ".join(row) for row in rows])


def _fence(text):
    """A code block no backtick run inside ``text`` can close early."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence + "text", text.rstrip("\n"), fence]


def _cell(text):
    return _inline(text).replace("|", "\\|")


def _inline(text):
    return " ".join(str(text).split())


def _code(text):
    text = _inline(text).replace("|", "\\|")
    return "``%s``" % text if "`" in text else "`%s`" % text


def _when(seconds):
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(seconds))


def _duration(seconds):
    seconds = int(round(max(0, seconds)))
    if seconds < 60:
        return "%ds" % seconds
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return "%dm %02ds" % (minutes, seconds)
    hours, minutes = divmod(minutes, 60)
    return "%dh %02dm" % (hours, minutes)

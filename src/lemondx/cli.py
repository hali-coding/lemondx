"""Command line interface for lemondx."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import shutil
import sys

from .lxd import LXDError
from .service import ContainerService, LOCAL_STORAGE_DRIVERS, ServiceError
from .server import DEFAULT_HOST, DEFAULT_PORT, serve

# ANSI colours, disabled when stdout is not a terminal or NO_COLOR is set.
_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(text, code):
    return "\033[%sm%s\033[0m" % (code, text) if _COLOR else str(text)


DIM = lambda s: _c(s, "2")
BOLD = lambda s: _c(s, "1")
GREEN = lambda s: _c(s, "32")
RED = lambda s: _c(s, "31")
YELLOW = lambda s: _c(s, "33")
CYAN = lambda s: _c(s, "36")

STATUS_COLORS = {
    "Running": GREEN,
    "Stopped": DIM,
    "Frozen": CYAN,
    "Error": RED,
}


# -- output helpers --------------------------------------------------------


def table(rows, headers):
    """Render a list of row-lists as an aligned, headed table."""
    if not rows:
        return DIM("(none)")
    widths = [len(h) for h in headers]
    cells = [[_plain(c) for c in row] for row in rows]
    for row in cells:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    out = ["  ".join(BOLD(h.upper().ljust(widths[i])) for i, h in enumerate(headers)).rstrip()]
    for original, plain in zip(rows, cells):
        line = []
        for i, cell in enumerate(original):
            pad = " " * (widths[i] - len(plain[i]))
            line.append("%s%s" % (cell, pad))
        out.append("  ".join(line).rstrip())
    return "\n".join(out)


def _plain(value):
    """Length of a cell ignoring ANSI escapes."""
    text = str(value)
    while "\033[" in text:
        start = text.index("\033[")
        end = text.find("m", start)
        if end == -1:
            break
        text = text[:start] + text[end + 1:]
    return text


def human_bytes(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "-"
    if n <= 0:
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024
    return "-"


def emit(args, payload, renderer):
    """Print JSON when --json was given, otherwise the human rendering."""
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=str))
    else:
        text = renderer(payload)
        if text:
            print(text)


def confirm(prompt, assume_yes=False):
    if assume_yes or not sys.stdin.isatty():
        return assume_yes
    answer = input("%s [y/N] " % prompt).strip().lower()
    return answer in ("y", "yes")


# -- commands --------------------------------------------------------------


def cmd_serve(args, _service):
    serve(host=args.host, port=args.port, token=args.token, dev=args.dev,
          quiet=args.quiet, open_browser=args.open)
    return 0


def cmd_status(args, service):
    status = service.status()

    def render(s):
        lines = [
            "%s  %s" % (BOLD(s.get("product") or "LXD"), s.get("server_version") or "?"),
            "%s  %s" % (DIM("socket "), s.get("socket") or "?"),
            "%s  %s" % (DIM("kernel "), s.get("kernel") or "?"),
            "%s  %s" % (DIM("project"), s.get("project")),
            "%s  %s" % (DIM("ready  "),
                        GREEN("yes") if s["ready"] else YELLOW("no -- run `lemondx init`")),
        ]
        if s["storage_pools"]:
            lines.append("%s  %s" % (DIM("storage"), ", ".join(
                "%s (%s%s)" % (p["name"], p["driver"],
                               "" if p.get("supports_quota") else ", no disk quotas")
                for p in s["storage_pools"])))
        if s["networks"]:
            lines.append("%s  %s" % (DIM("network"), ", ".join(
                "%s %s" % (n["name"], n.get("ipv4") or "") for n in s["networks"])))
        for issue in s["issues"]:
            lines.append(YELLOW("  ! " + issue))
        return "\n".join(lines)

    emit(args, status, render)
    return 0 if status["ready"] else 1


def cmd_init(args, service):
    result = service.initialize(storage_driver=args.storage, pool_name=args.pool,
                                pool_size=args.size, bridge=args.bridge, ipv6=args.ipv6)

    def render(r):
        lines = [GREEN("+ ") + step for step in r["steps"]]
        lines += [YELLOW("! " + note) for note in r.get("notes") or []]
        return "\n".join(lines)

    emit(args, result, render)
    return 0


def cmd_list(args, service):
    containers = service.list_containers()
    if args.running:
        containers = [c for c in containers if c["status"] == "Running"]

    def render(items):
        rows = []
        for c in items:
            color = STATUS_COLORS.get(c["status"], str)
            rows.append([
                BOLD(c["name"]),
                color(c["status"]),
                c["image_alias"] or c["image"] or "-",
                ", ".join(c["ipv4"]) or "-",
                human_bytes(c["memory_usage"]),
                str(c["snapshot_count"] or "-"),
            ])
        return table(rows, ["name", "state", "image", "ipv4", "memory", "snaps"])

    emit(args, containers, render)
    return 0


def cmd_info(args, service):
    container = service.get_container(args.name)

    def render(c):
        lines = [
            "%s %s" % (BOLD(c["name"]), STATUS_COLORS.get(c["status"], str)(c["status"])),
            "%s %s" % (DIM("type     "), c["type"]),
            "%s %s" % (DIM("image    "), c["image"] or c["image_alias"] or "-"),
            "%s %s" % (DIM("created  "), c["created_at"]),
            "%s %s" % (DIM("profiles "), ", ".join(c["profiles"])),
            "%s %s" % (DIM("ipv4     "), ", ".join(c["ipv4"]) or "-"),
            "%s %s" % (DIM("ipv6     "), ", ".join(c["ipv6"]) or "-"),
            "%s cpu=%s memory=%s" % (DIM("limits   "),
                                     c["limits"]["cpu"] or "unlimited",
                                     c["limits"]["memory"] or "unlimited"),
            "%s %s / peak %s" % (DIM("memory   "), human_bytes(c["memory_usage"]),
                                 human_bytes(c["memory_peak"])),
            "%s rx %s / tx %s" % (DIM("network  "), human_bytes(c["network_rx"]),
                                  human_bytes(c["network_tx"])),
            "%s %s" % (DIM("processes"), c["processes"] or "-"),
        ]
        if c["description"]:
            lines.insert(1, "%s %s" % (DIM("about    "), c["description"]))
        if c["snapshots"]:
            lines.append("")
            lines.append(BOLD("snapshots"))
            lines.append(table(
                [[s["name"], s["created_at"], "yes" if s["stateful"] else "no"]
                 for s in c["snapshots"]],
                ["name", "created", "stateful"]))
        return "\n".join(lines)

    emit(args, container, render)
    return 0


def cmd_create(args, service):
    image = args.image or service.default_image()
    if args.disk and not args.json:
        pool = service.root_pool_info(args.profile, args.pool)
        if pool and not pool["supports_quota"]:
            print(YELLOW(
                "! Pool '%s' uses the %s driver, which cannot enforce a disk "
                "size unless the filesystem has project quotas enabled. The "
                "value will be recorded but not applied."
                % (pool["name"], pool["driver"])), file=sys.stderr)
    if not args.json:
        print(DIM("Creating %s from %s (this pulls the image on first use)..."
                  % (args.name, image)), flush=True)
    modules, params, ssh_keys = resolve_selection(args, service)
    if not modules and not args.no_default_modules:
        modules = [m["id"] for m in service.list_modules() if m["is_default"]]
    fill_secrets(modules, params, service)

    bootstrap = None
    if modules:
        bootstrap = {
            "modules": modules,
            "params": params,
            "ssh_keys": ssh_keys,
        }
        if not args.json:
            print(DIM("Will run bootstrap modules: %s" % ", ".join(modules)),
                  flush=True)

    container = service.create_container(
        name=args.name, image=image,
        instance_type="virtual-machine" if args.vm else "container",
        profiles=args.profile or None, cpu=args.cpu, memory=args.memory,
        disk=args.disk, pool=args.pool, description=args.description, ephemeral=args.ephemeral,
        start=not args.no_start, bootstrap=bootstrap,
    )

    def render(c):
        lines = ["%s %s is %s%s" % (
            GREEN("+"), BOLD(c["name"]), c["status"].lower(),
            (" at " + ", ".join(c["ipv4"])) if c["ipv4"] else "")]
        if c.get("bootstrap"):
            lines.append("")
            lines.append(render_bootstrap(c["bootstrap"]))
        return "\n".join(lines)

    emit(args, container, render)
    if container.get("bootstrap") and not container["bootstrap"]["ok"]:
        return 1
    return 0


def cmd_limits(args, service):
    if args.cpu is None and args.memory is None:
        raise ServiceError("Give at least one of --cpu or --memory.")
    container = service.update_limits(args.name, cpu=args.cpu, memory=args.memory)
    emit(args, container, lambda c: "%s %s: cpu=%s memory=%s" % (
        GREEN("+"), BOLD(c["name"]),
        c["limits"]["cpu"] or "unlimited", c["limits"]["memory"] or "unlimited"))
    return 0


def cmd_state(action):
    def run(args, service):
        code = 0
        for name in args.name:
            try:
                container = service.change_state(name, action, force=getattr(args, "force", False))
                if not args.json:
                    print("%s %s is now %s" % (GREEN("+"), BOLD(name),
                                               container["status"].lower()))
                elif args.json:
                    print(json.dumps(container, indent=2, default=str))
            except (ServiceError, LXDError) as exc:
                print("%s %s: %s" % (RED("!"), name, exc), file=sys.stderr)
                code = 1
        return code
    return run


def cmd_delete(args, service):
    code = 0
    for name in args.name:
        if not confirm("Delete container '%s'?" % name, args.yes):
            print(DIM("skipped %s" % name))
            continue
        try:
            service.delete_container(name, force=args.force)
            print("%s deleted %s" % (GREEN("+"), BOLD(name)))
        except (ServiceError, LXDError) as exc:
            print("%s %s: %s" % (RED("!"), name, exc), file=sys.stderr)
            code = 1
    return code


def cmd_exec(args, service):
    result = service.exec_command(args.name, args.command, timeout=args.timeout)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["stdout"]:
            sys.stdout.write(result["stdout"])
        if result["stderr"]:
            sys.stderr.write(result["stderr"])
    return int(result.get("exit_code") or 0)


def cmd_shell(args, service):
    """Hand off to the real CLI client, which can do interactive terminals."""
    name = service.lxd.client_binary          # "lxc" for LXD, "incus" for Incus
    binary = shutil.which(name)
    if not binary:
        print(RED("The `%s` client is not on PATH; cannot open an interactive "
                  "shell." % name), file=sys.stderr)
        return 1
    os.execv(binary, [binary, "exec", args.name, "--", args.shell])
    return 0  # unreachable


def cmd_snapshot(args, service):
    result = service.create_snapshot(args.name, args.snapshot, args.stateful)
    emit(args, result, lambda r: "%s snapshot %s created" % (GREEN("+"), BOLD(r["created"])))
    return 0


def cmd_snapshots(args, service):
    snapshots = service.get_container(args.name)["snapshots"]
    emit(args, snapshots, lambda s: table(
        [[x["name"], x["created_at"], "yes" if x["stateful"] else "no"] for x in s],
        ["name", "created", "stateful"]))
    return 0


def cmd_restore(args, service):
    service.restore_snapshot(args.name, args.snapshot)
    print("%s restored %s from %s" % (GREEN("+"), BOLD(args.name), args.snapshot))
    return 0


def cmd_snap_delete(args, service):
    service.delete_snapshot(args.name, args.snapshot)
    print("%s deleted snapshot %s" % (GREEN("+"), args.snapshot))
    return 0


def cmd_modules(args, service):
    modules = service.list_modules()

    def render(items):
        if not items:
            return DIM("No bootstrap modules found.")
        out = []
        for module in items:
            flags = []
            if module.get("is_default"):
                flags.append(GREEN("default"))
            if not module.get("builtin", True):
                flags.append(CYAN("uploaded"))
            if module["uses_ssh_keys"]:
                flags.append(CYAN("ssh-keys"))
            if module["os"]:
                flags.append(DIM("os: " + " ".join(module["os"])))
            head = "%s  %s" % (BOLD(module["id"]), module["name"])
            if flags:
                head += "  " + " ".join(flags)
            out.append(head)
            if module["description"]:
                out.append("    " + DIM(module["description"]))
            for param in module["params"]:
                value = param.get("value", param["default"])
                line = "    %s=%s" % (param["name"], value or DIM("(empty)"))
                if param.get("saved"):
                    line += "  " + GREEN("saved") + DIM(" (module default: %s)"
                                                        % (param["default"] or "empty"))
                out.append("%s  %s" % (line, DIM(param["description"])))
            out.append("")
        return "\n".join(out).rstrip()

    emit(args, modules, render)
    return 0


def cmd_ssh_keys(args, service):
    keys = service.list_ssh_keys()
    emit(args, keys, lambda items: table(
        [[k["source"], k["type"], k["fingerprint"], k["comment"] or "-"] for k in items],
        ["file", "type", "fingerprint", "comment"]))
    return 0


def collect_ssh_keys(paths, use_all):
    """Read public keys named on the command line, plus ~/.ssh/*.pub if asked."""
    from .bootstrap import list_host_ssh_keys, parse_public_key
    keys = []
    for path in paths or []:
        expanded = os.path.expanduser(path)
        try:
            with open(expanded, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise ServiceError("Cannot read SSH key '%s': %s" % (path, exc))
        keys.append(parse_public_key(text)["line"])
    if use_all:
        keys.extend(k["line"] for k in list_host_ssh_keys())
    # Preserve order, drop duplicates.
    return list(dict.fromkeys(keys))


def parse_params(pairs):
    params = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ServiceError("Parameter '%s' must be KEY=VALUE." % pair)
        key, _, value = pair.partition("=")
        params[key.strip()] = value
    return params


def render_bootstrap(result):
    lines = []
    for module in result["modules"]:
        ok = module["exit_code"] == 0
        mark = GREEN("+") if ok else RED("!")
        lines.append("%s %s (%ss)" % (mark, BOLD(module["name"]), module["duration"]))
        body = (module["stdout"] or "") + (module["stderr"] or "")
        for line in body.splitlines():
            lines.append("    " + (line if ok else RED(line)))
        if not ok:
            lines.append("    " + RED("exited with code %d" % module["exit_code"]))
    lines.append("")
    lines.append(GREEN("bootstrap finished") if result["ok"]
                 else RED("bootstrap failed"))
    return "\n".join(lines)


def cmd_module_add(args, service):
    path = os.path.expanduser(args.path)
    try:
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
    except OSError as exc:
        raise ServiceError("Cannot read '%s': %s" % (args.path, exc))
    module = service.upload_module(args.name or os.path.basename(path), content,
                                   overwrite=args.force)
    if args.default:
        service.update_module_settings(module["id"], is_default=True)
    emit(args, module, lambda m: "%s installed module %s%s" % (
        GREEN("+"), BOLD(m["id"]),
        " (default for new containers)" if args.default else ""))
    return 0


def cmd_module_remove(args, service):
    result = service.remove_module(args.id)
    emit(args, result, lambda r: "%s removed module %s" % (GREEN("+"), BOLD(r["deleted"])))
    return 0


def cmd_module_set(args, service):
    is_default = None
    if args.default:
        is_default = True
    elif args.no_default:
        is_default = False
    module = service.update_module_settings(
        args.id, params=parse_params(args.param) or None, is_default=is_default)
    emit(args, module, lambda m: "%s %s: default=%s %s" % (
        GREEN("+"), BOLD(m["id"]), m["is_default"],
        " ".join("%s=%s" % (p["name"], p["value"]) for p in m["params"])))
    return 0


def cmd_profiles(args, service):
    profiles = service.list_bootstrap_profiles()
    emit(args, profiles, lambda items: table(
        [[p["name"], " → ".join(p["modules"]),
          " ".join("%s=%s" % kv for kv in p["params"].items()) or "-",
          len(p["ssh_keys"]) or "-",
          p["description"] or "-"] for p in items],
        ["name", "modules", "params", "keys", "description"]))
    return 0


def cmd_profile_save(args, service):
    modules, params, ssh_keys = resolve_selection(args, service)
    profile = service.save_bootstrap_profile(
        args.name, modules, params, args.description or "", ssh_keys)
    emit(args, profile, lambda p: "%s saved profile %s (%s%s)" % (
        GREEN("+"), BOLD(p["name"]), " → ".join(p["modules"]),
        ", %d key(s)" % len(p["ssh_keys"]) if p["ssh_keys"] else ""))
    return 0


def cmd_profile_delete(args, service):
    result = service.delete_bootstrap_profile(args.name)
    emit(args, result, lambda r: "%s deleted profile %s" % (GREEN("+"), r["deleted"]))
    return 0


def resolve_selection(args, service):
    """Modules, params and keys for this run: a saved profile, plus overrides."""
    modules = list(args.module or [])
    params = {}
    ssh_keys = []
    if getattr(args, "bootstrap_profile", None):
        match = next((p for p in service.list_bootstrap_profiles()
                      if p["name"].lower() == args.bootstrap_profile.lower()), None)
        if not match:
            raise ServiceError(
                "No bootstrap profile called '%s'. See `lemondx profiles`."
                % args.bootstrap_profile)
        modules = match["modules"] + [m for m in modules if m not in match["modules"]]
        params.update(match["params"])
        ssh_keys.extend(match["ssh_keys"])
    params.update(parse_params(args.param))
    ssh_keys.extend(collect_ssh_keys(args.ssh_key, args.all_ssh_keys))
    return modules, params, list(dict.fromkeys(ssh_keys))


def find_template(service, name):
    """A template by name, ignoring case the way profile lookups do."""
    match = next((t for t in service.list_templates()
                  if t["name"].lower() == (name or "").lower()), None)
    if not match:
        raise ServiceError("No template called '%s'. See `lemondx templates`." % name)
    return match


def describe_template(t):
    size = ", ".join(part for part in (
        "%s cpu" % t["cpu"] if t["cpu"] else "",
        t["memory"], "disk " + t["disk"] if t["disk"] else "") if part)
    return "%s%s%s" % (t["image"], " (vm)" if t["type"] == "virtual-machine" else "",
                       " · " + size if size else "")


def cmd_templates(args, service):
    templates = service.list_templates()
    emit(args, templates, lambda items: table(
        [[t["name"], describe_template(t), t["name_prefix"] + "-N",
          " → ".join(t["bootstrap"]["modules"]) or "-",
          len(t["bootstrap"]["ssh_keys"]) or "-",
          t["description"] or "-"] for t in items],
        ["name", "instance", "names", "modules", "keys", "description"]))
    return 0


def cmd_template_save(args, service):
    modules, params, ssh_keys = resolve_selection(args, service)
    template = service.save_template(
        args.name, args.image or service.default_image(),
        instance_type="virtual-machine" if args.vm else "container",
        cpu=args.cpu, memory=args.memory, disk=args.disk, pool=args.pool,
        profiles=args.profile, ephemeral=args.ephemeral, start=not args.no_start,
        bootstrap={"modules": modules, "params": params, "ssh_keys": ssh_keys},
        description=args.description or "", name_prefix=args.prefix,
    )
    emit(args, template, lambda t: "%s saved template %s: %s%s" % (
        GREEN("+"), BOLD(t["name"]), describe_template(t),
        (" · " + " → ".join(t["bootstrap"]["modules"]))
        if t["bootstrap"]["modules"] else ""))
    return 0


def cmd_template_delete(args, service):
    result = service.delete_template(args.name)
    emit(args, result, lambda r: "%s deleted template %s" % (GREEN("+"), r["deleted"]))
    return 0


def render_template_run(result):
    """Per-instance outcome of a launch, recreate or destroy."""
    lines = []
    for instance in result["instances"]:
        container = instance["container"]
        if instance["error"]:
            lines.append("%s %s: %s" % (RED("!"), BOLD(instance["name"]), instance["error"]))
        elif container is None:
            lines.append("%s deleted %s" % (GREEN("+"), BOLD(instance["name"])))
        else:
            lines.append("%s %s is %s%s" % (
                GREEN("+") if instance["ok"] else RED("!"), BOLD(instance["name"]),
                container["status"].lower(),
                (" at " + ", ".join(container["ipv4"])) if container["ipv4"] else ""))
            if container.get("bootstrap") and not container["bootstrap"]["ok"]:
                lines.append(render_bootstrap(container["bootstrap"]))
    return "\n".join(lines)


def confirm_template_instances(args, service, verb):
    """The instances to act on, after the user has seen and accepted them."""
    names = service.template_instances(args.template)
    if not names:
        raise ServiceError("No instances were launched from template '%s'." % args.template)
    if not args.json:
        print("Instances from %s: %s" % (BOLD(args.template), ", ".join(names)))
    if not confirm("%s all %d? Their filesystems and snapshots are deleted."
                   % (verb, len(names)), args.yes):
        return None
    return names


def cmd_template_destroy(args, service):
    names = confirm_template_instances(args, service, "Destroy")
    if names is None:
        print(DIM("nothing destroyed"))
        return 1
    result = service.destroy_template_instances(args.template, names)
    emit(args, result, render_template_run)
    return 0 if result["ok"] else 1


def cmd_template_recreate(args, service):
    template = find_template(service, args.template)
    args.template = template["name"]
    params = parse_params(args.param)
    names = confirm_template_instances(args, service, "Recreate")
    if names is None:
        print(DIM("nothing recreated"))
        return 1
    fill_secrets(template["bootstrap"]["modules"], params, service)
    if not args.json:
        print(DIM("Recreating %d instance(s) from %s..." % (len(names), template["name"])),
              flush=True)
    result = service.recreate_template_instances(template["name"], names, params=params)
    emit(args, result, render_template_run)
    return 0 if result["ok"] else 1


def cmd_template_exec(args, service):
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise ServiceError("Give a command to run, e.g. `lemondx template-exec web -- uptime`.")
    members = [c for c in service.list_containers() if c["template"] == args.template]
    if not members:
        raise ServiceError("No instances were launched from template '%s'." % args.template)
    names = [c["name"] for c in members if c["status"] == "Running"]
    skipped = [c["name"] for c in members if c["status"] != "Running"]
    if not names:
        raise ServiceError("None of the instances from '%s' are running." % args.template)
    if skipped and not args.json:
        print(DIM("skipping %s (not running)" % ", ".join(skipped)), file=sys.stderr)

    # One string, run by sh -c on each: quoting keeps the words as they were
    # typed, so `-- echo "a b"` means the same here as in a local shell.
    result = service.exec_template_instances(
        args.template, command[0] if len(command) == 1 else shlex.join(command),
        names, timeout=args.timeout)

    def render(r):
        lines = []
        for instance in r["instances"]:
            outcome = instance["exec"]
            if instance["error"]:
                lines.append("%s %s: %s" % (RED("!"), BOLD(instance["name"]), instance["error"]))
                continue
            code = outcome["exit_code"]
            lines.append("%s %s %s" % (GREEN("+") if code == 0 else RED("!"),
                                       BOLD(instance["name"]),
                                       DIM("exit %d" % code)))
            for stream, colour in (("stdout", str), ("stderr", RED)):
                for line in (outcome[stream] or "").splitlines():
                    lines.append("    " + colour(line))
            if outcome["truncated"]:
                lines.append("    " + DIM("(output cut to its last %d KiB)"
                                          % (service.EXEC_OUTPUT_LIMIT // 1024)))
        return "\n".join(lines)

    emit(args, result, render)
    return 0 if result["ok"] else 1


def cmd_launch(args, service):
    template = find_template(service, args.template)
    params = parse_params(args.param)
    fill_secrets(template["bootstrap"]["modules"], params, service)
    if not args.json:
        print(DIM("Launching %d instance(s) from %s (this pulls the image on first "
                  "use)..." % (args.count, template["name"])), flush=True)
    result = service.launch_template(template["name"], count=args.count,
                                     prefix=args.prefix, params=params)
    emit(args, result, render_template_run)
    return 0 if result["ok"] else 1


def fill_secrets(modules, params, service):
    """Supply secret parameters without putting them on the command line.

    A value in --param works but lands in shell history and `ps`. So for each
    secret still missing, use an environment variable of the same name (the
    PGPASSWORD convention), else prompt without echo when there is a terminal.
    Anything still missing is left for the service to reject with a clear
    message.
    """
    for module in service.list_modules():
        if module["id"] not in modules:
            continue
        for param in module["params"]:
            name = param["name"]
            if not param.get("secret") or params.get(name):
                continue
            if os.environ.get(name):
                params[name] = os.environ[name]
            elif sys.stdin.isatty():
                label = "%s (%s)" % (name, param["description"]) if param["description"] \
                    else name
                value = getpass.getpass("%s: " % label)
                if value:
                    params[name] = value
    return params


def cmd_bootstrap(args, service):
    modules, params, ssh_keys = resolve_selection(args, service)
    fill_secrets(modules, params, service)
    result = service.bootstrap(
        args.name,
        modules=modules,
        params=params,
        ssh_keys=ssh_keys,
        timeout=args.timeout,
    )
    emit(args, result, render_bootstrap)
    return 0 if result["ok"] else 1


def meter(part, whole, width=20):
    """A text bar for part/whole; past full it is coloured as a warning."""
    if not whole:
        return DIM("[%s]" % ("." * width))
    ratio = part / whole
    filled = min(width, int(round(ratio * width)))
    bar = "[%s%s]" % ("#" * filled, "." * (width - filled))
    painted = RED(bar) if ratio > 1 else YELLOW(bar) if ratio >= 0.9 else bar
    return "%s %3d%%" % (painted, round(ratio * 100))


def cmd_resources(args, service):
    resources = service.resources()

    def unlimited(names, what):
        if not names:
            return ""
        return YELLOW("  %d without a %s limit: %s" % (len(names), what, ", ".join(names)))

    def render(r):
        host, cpu, memory = r["host"], r["cpu"], r["memory"]
        lines = [
            "%s  %s" % (DIM("host   "), " · ".join(part for part in (
                host["cpu_model"],
                "%d threads" % host["cpu_threads"],
                "%s RAM" % human_bytes(host["memory_total"]),
                host["architecture"]) if part)),
            "",
            "%s  %s  %s of %d threads allocated%s" % (
                BOLD("cpu    "), meter(cpu["allocated"], cpu["total"]),
                cpu["allocated"], cpu["total"],
                DIM(" (+%d stopped)" % cpu["stopped"]) if cpu["stopped"] else ""),
        ]
        if cpu["unlimited"]:
            lines.append(unlimited(cpu["unlimited"], "CPU"))
        lines.append("%s  %s  %s of %s allocated%s" % (
            BOLD("memory "), meter(memory["allocated"], memory["total"]),
            human_bytes(memory["allocated"]), human_bytes(memory["total"]),
            DIM(" (+%s stopped)" % human_bytes(memory["stopped"]))
            if memory["stopped"] else ""))
        lines.append("%s  %s  %s in use on the host, %s by instances" % (
            DIM("  used "), meter(memory["used"], memory["total"]),
            human_bytes(memory["used"]), human_bytes(memory["instances_used"])))
        if memory["unlimited"]:
            lines.append(unlimited(memory["unlimited"], "memory"))
        for pool in r["storage"]:
            lines.append("%s  %s  %s of %s allocated on %s (%s)%s" % (
                BOLD("disk   "), meter(pool["allocated"], pool["total"]),
                human_bytes(pool["allocated"]), human_bytes(pool["total"]),
                pool["name"], pool["driver"],
                "" if pool["supports_quota"] else DIM(", not enforced")))
            lines.append("%s  %s  %s used" % (
                DIM("  used "), meter(pool["used"], pool["total"]),
                human_bytes(pool["used"])))
            if pool["unlimited"]:
                lines.append(unlimited(pool["unlimited"], "disk"))

        def limit(entry, value):
            if not entry["limit"]:
                return DIM("unlimited")
            return DIM("%s (default)" % value) if entry["implicit"] else value

        lines.append("")
        lines.append(table(
            [[BOLD(i["name"]),
              STATUS_COLORS.get(i["status"], str)(i["status"]),
              "vm" if i["type"] == "virtual-machine" else "container",
              limit(i["cpu"], i["cpu"]["limit"]),
              limit(i["memory"], i["memory"]["limit"]),
              human_bytes(i["memory"]["usage"]) if i["active"] else "-",
              limit({"limit": i["disk"]["size"], "implicit": i["disk"]["implicit"]},
                    i["disk"]["size"])]
             for i in r["instances"]],
            ["name", "state", "type", "cpu", "memory", "in use", "disk"]))
        return "\n".join(lines)

    emit(args, resources, render)
    return 0


def cmd_storage_pools(args, service):
    pools = service.storage()["pools"]
    emit(args, pools, lambda items: table([
        [
            BOLD(pool["name"]), pool["driver"],
            human_bytes(pool["used"]), human_bytes(pool["total"]),
            pool["volume_count"],
            "yes" if pool["manageable"] else "read-only",
        ]
        for pool in items
    ], ["name", "driver", "used", "total", "volumes", "managed"]))
    return 0


def cmd_storage_pool_show(args, service):
    pool = service.get_storage_pool(args.name)

    def render(item):
        lines = [
            "%s  %s" % (BOLD(item["name"]), item["driver"]),
            "%s  %s of %s" % (DIM("space "), human_bytes(item["used"]),
                                human_bytes(item["total"])),
            "%s  %s" % (DIM("source"), item["source"] or "-"),
            "%s  %s" % (DIM("status"), "managed" if item["manageable"] else "read-only"),
        ]
        if item["description"]:
            lines.append("%s  %s" % (DIM("about "), item["description"]))
        if item["used_by"]:
            lines.append("%s  %s" % (DIM("used by"), ", ".join(item["used_by"])))
        if item["config"]:
            lines.append("")
            lines.append(BOLD("configuration"))
            lines.extend("  %s=%s" % pair for pair in sorted(item["config"].items()))
        return "\n".join(lines)

    emit(args, pool, render)
    return 0


def cmd_storage_pool_create(args, service):
    pool = service.create_storage_pool(
        args.name, args.driver, source=args.source, size=args.size,
        description=args.description or "", config=parse_params(args.config))
    emit(args, pool, lambda item: "%s created pool %s (%s)" % (
        GREEN("+"), BOLD(item["name"]), item["driver"]))
    return 0


def cmd_storage_pool_set(args, service):
    if args.description is None and args.size is None and not args.config:
        raise ServiceError("Give at least one of --description, --size or --config.")
    pool = service.update_storage_pool(
        args.name, description=args.description, size=args.size,
        config=parse_params(args.config))
    emit(args, pool, lambda item: "%s updated pool %s" % (
        GREEN("+"), BOLD(item["name"])))
    return 0


def cmd_storage_pool_delete(args, service):
    force = bool(args.force)
    plan = None
    confirmation = None
    if force:
        pool = service.get_storage_pool(args.name)
        plan = pool["delete_plan"]
        if not args.json:
            labels = (
                ("instances stored here", plan["instances"]),
                ("instances attached to custom volumes", plan["attached_instances"]),
                ("cached images", plan["images"]),
                ("custom volumes", plan["custom_volumes"]),
                ("profiles to update", plan["profiles"]),
                ("unsupported references", plan["other_references"]),
                ("unsupported volumes", plan["other_volumes"]),
            )
            print(RED("Force deletion will permanently remove:"), file=sys.stderr)
            for label, values in labels:
                if values:
                    print("  %s: %s" % (label, ", ".join(values)), file=sys.stderr)
            if pool["driver"] == "zfs":
                print("  The backing zpool will be preserved; export it when prompted.",
                      file=sys.stderr)
        if args.confirm_name is not None:
            confirmation = args.confirm_name
        elif sys.stdin.isatty():
            confirmation = input("Type '%s' to confirm: " % args.name).strip()
        if confirmation != args.name:
            raise ServiceError(
                "Type the pool name exactly, or pass --confirm-name %s." % args.name)
    elif not confirm("Delete storage pool '%s'?" % args.name, args.yes):
        print(DIM("skipped %s" % args.name))
        return 0
    result = service.delete_storage_pool(
        args.name, force=force, confirmation=confirmation, expected_plan=plan)
    emit(args, result, lambda item: "%s %s pool %s" % (
        GREEN("+"), "detached" if item.get("detached") else "deleted",
        BOLD(item["deleted"])))
    return 0


def cmd_storage_volumes(args, service):
    volumes = service.storage()["volumes"]
    if args.pool:
        volumes = [volume for volume in volumes if volume["pool"] == args.pool]
    emit(args, volumes, lambda items: table([
        [
            BOLD(volume["name"]), volume["pool"], volume["type"],
            volume["content_type"], volume["size"] or "-",
            "yes" if volume["manageable"] else "read-only",
        ]
        for volume in items
    ], ["name", "pool", "type", "content", "size", "managed"]))
    return 0


def cmd_storage_volume_show(args, service):
    volume = service.get_storage_volume(args.pool, args.name)
    emit(args, volume, lambda item: "\n".join([
        "%s  %s" % (BOLD(item["name"]), item["content_type"]),
        "%s  %s" % (DIM("pool  "), item["pool"]),
        "%s  %s" % (DIM("size  "), item["size"] or "unlimited"),
        "%s  %s" % (DIM("status"), "managed" if item["manageable"] else "read-only"),
    ]))
    return 0


def cmd_storage_volume_create(args, service):
    volume = service.create_storage_volume(
        args.pool, args.name, content_type=args.content_type, size=args.size,
        description=args.description or "", config=parse_params(args.config))
    emit(args, volume, lambda item: "%s created volume %s in %s" % (
        GREEN("+"), BOLD(item["name"]), item["pool"]))
    return 0


def cmd_storage_volume_set(args, service):
    if args.description is None and args.size is None and not args.config:
        raise ServiceError("Give at least one of --description, --size or --config.")
    volume = service.update_storage_volume(
        args.pool, args.name, description=args.description, size=args.size,
        config=parse_params(args.config))
    emit(args, volume, lambda item: "%s updated volume %s" % (
        GREEN("+"), BOLD(item["name"])))
    return 0


def cmd_storage_volume_delete(args, service):
    label = "%s/%s" % (args.pool, args.name)
    if not confirm("Delete storage volume '%s'?" % label, args.yes):
        print(DIM("skipped %s" % label))
        return 0
    result = service.delete_storage_volume(args.pool, args.name)
    emit(args, result, lambda item: "%s deleted volume %s/%s" % (
        GREEN("+"), item["pool"], BOLD(item["deleted"])))
    return 0


def cmd_images(args, service):
    images = service.list_images()

    def render(data):
        out = [BOLD("Cached locally")]
        out.append(table(
            [[i["fingerprint"], ", ".join(i["aliases"]) or "-",
              i["description"] or "-", human_bytes(i["size"])]
             for i in data["local"]],
            ["fingerprint", "aliases", "description", "size"]))
        out.append("")
        out.append(BOLD("Suggested images"))
        out.append(table([[i["alias"], i["label"]] for i in data["catalog"]],
                         ["alias", "name"]))
        return "\n".join(out)

    emit(args, images, render)
    return 0


# -- parser ----------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="lemondx",
        description="Manage local LXD/Incus containers from the terminal or a web UI.",
    )
    parser.add_argument("--json", action="store_true", help="output raw JSON")
    parser.add_argument("--project", default="default",
                        help="project to work in (default: default)")
    parser.add_argument("--socket", help="path to the LXD/Incus unix socket")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # Shared flags, so `lemondx --json ls` and `lemondx ls --json` both work.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="output raw JSON")

    def add(name, **kwargs):
        kwargs.setdefault("parents", [common])
        return sub.add_parser(name, **kwargs)

    # Bootstrap selection, shared by `create` and `bootstrap`.
    boot = argparse.ArgumentParser(add_help=False)
    # -b, not -m: `create` already uses -m for --memory.
    boot.add_argument("-b", "--module", action="append", metavar="ID",
                      help="bootstrap module to run (repeatable; see `lemondx modules`)")
    boot.add_argument("--param", action="append", metavar="KEY=VALUE",
                      help="parameter passed to the modules (repeatable)")
    boot.add_argument("--ssh-key", action="append", metavar="PATH",
                      help="public key file to install (repeatable)")
    boot.add_argument("--all-ssh-keys", action="store_true",
                      help="install every public key in ~/.ssh")
    # Not --profile: `create` already uses that for LXD/Incus profiles, which
    # are a different thing entirely.
    boot.add_argument("-P", "--bootstrap-profile", metavar="NAME",
                      help="start from a saved bootstrap profile (see `lemondx profiles`)")

    p = add("serve", help="run the web UI and REST API")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--token", help="require this bearer token on API calls")
    p.add_argument("--dev", action="store_true", help="allow cross-origin Vite dev server")
    p.add_argument("--open", action="store_true", help="open a browser window")
    p.add_argument("--quiet", action="store_true", help="do not log requests")
    p.set_defaults(func=cmd_serve, needs_service=False)

    p = add("status", help="show daemon connection and readiness")
    p.set_defaults(func=cmd_status)

    p = add("init",
                       help="prepare the daemon (storage pool, bridge, profile)")
    p.add_argument("--storage", default="dir", help="storage driver (default: dir)")
    p.add_argument("--pool", default="default", help="storage pool name")
    p.add_argument("--size", help="pool size, e.g. 30GiB (ignored for the dir driver)")
    p.add_argument("--bridge", default="lxdbr0", help="bridge name")
    p.add_argument("--ipv6", action="store_true", help="also hand out IPv6")
    p.set_defaults(func=cmd_init)

    p = add("resources", help="show allocated CPU/memory/disk against the host")
    p.set_defaults(func=cmd_resources)

    storage = add("storage", help="view and manage local storage")
    storage_sub = storage.add_subparsers(dest="storage_command", metavar="<storage-command>")

    p = storage_sub.add_parser("pools", parents=[common], help="list storage pools")
    p.set_defaults(func=cmd_storage_pools)

    pool = storage_sub.add_parser("pool", help="manage a storage pool")
    pool_sub = pool.add_subparsers(dest="pool_command", metavar="<pool-command>")

    p = pool_sub.add_parser("show", parents=[common], help="show a storage pool")
    p.add_argument("name")
    p.set_defaults(func=cmd_storage_pool_show)

    p = pool_sub.add_parser("create", parents=[common], help="create a local storage pool")
    p.add_argument("name")
    p.add_argument("--driver", required=True, choices=sorted(LOCAL_STORAGE_DRIVERS))
    p.add_argument("--source", help="existing directory, device, volume group or zpool")
    p.add_argument("--size", help="loop-backed pool size, e.g. 30GiB")
    p.add_argument("--description")
    p.add_argument("--config", action="append", metavar="KEY=VALUE",
                   help="allowlisted local-driver option (repeatable)")
    p.set_defaults(func=cmd_storage_pool_create)

    p = pool_sub.add_parser("set", parents=[common], help="update a local storage pool")
    p.add_argument("name")
    p.add_argument("--size", help="new size for a managed loop-backed pool")
    p.add_argument("--description")
    p.add_argument("--config", action="append", metavar="KEY=VALUE",
                   help="allowlisted local-driver option (repeatable)")
    p.set_defaults(func=cmd_storage_pool_set)

    p = pool_sub.add_parser("delete", parents=[common], help="delete a local pool")
    p.add_argument("name")
    p.add_argument("--force", action="store_true",
                   help="delete resources in the pool first")
    p.add_argument("--confirm-name", metavar="NAME",
                   help="exact pool name required for non-interactive force deletion")
    p.add_argument("-y", "--yes", action="store_true",
                   help="do not ask for normal empty-pool confirmation")
    p.set_defaults(func=cmd_storage_pool_delete)

    p = storage_sub.add_parser("volumes", parents=[common], help="list storage volumes")
    p.add_argument("--pool", help="only volumes in this pool")
    p.set_defaults(func=cmd_storage_volumes)

    volume = storage_sub.add_parser("volume", help="manage a custom storage volume")
    volume_sub = volume.add_subparsers(dest="volume_command", metavar="<volume-command>")

    p = volume_sub.add_parser("show", parents=[common], help="show a custom volume")
    p.add_argument("pool")
    p.add_argument("name")
    p.set_defaults(func=cmd_storage_volume_show)

    p = volume_sub.add_parser("create", parents=[common], help="create a custom volume")
    p.add_argument("pool")
    p.add_argument("name")
    p.add_argument("--content-type", choices=("filesystem", "block"),
                   default="filesystem")
    p.add_argument("--size", help="volume size, e.g. 20GiB")
    p.add_argument("--description")
    p.add_argument("--config", action="append", metavar="KEY=VALUE",
                   help="allowlisted volume option (repeatable)")
    p.set_defaults(func=cmd_storage_volume_create)

    p = volume_sub.add_parser("set", parents=[common], help="update a custom volume")
    p.add_argument("pool")
    p.add_argument("name")
    p.add_argument("--size", help="new volume size")
    p.add_argument("--description")
    p.add_argument("--config", action="append", metavar="KEY=VALUE",
                   help="allowlisted volume option (repeatable)")
    p.set_defaults(func=cmd_storage_volume_set)

    p = volume_sub.add_parser("delete", parents=[common], help="delete a custom volume")
    p.add_argument("pool")
    p.add_argument("name")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_storage_volume_delete)

    p = add("list", aliases=["ls"], help="list containers")
    p.add_argument("--running", action="store_true", help="only running containers")
    p.set_defaults(func=cmd_list)

    p = add("info", help="show one container in detail")
    p.add_argument("name")
    p.set_defaults(func=cmd_info)

    # What an instance is made of, shared by `create` and `template-save`.
    spec = argparse.ArgumentParser(add_help=False)
    spec.add_argument("-i", "--image",
                      help="image alias, e.g. ubuntu:24.04 or images:debian/12 "
                           "(default: latest Ubuntu LTS for this daemon)")
    spec.add_argument("-c", "--cpu", help="CPU limit, e.g. 2")
    spec.add_argument("-m", "--memory", help="memory limit, e.g. 2GiB")
    spec.add_argument("-d", "--disk", help="root disk size, e.g. 10GiB")
    spec.add_argument("--pool", help="storage pool for the root disk")
    spec.add_argument("--profile", action="append", help="profile to apply (repeatable)")
    spec.add_argument("--vm", action="store_true", help="create a virtual machine instead")
    spec.add_argument("--ephemeral", action="store_true", help="delete on stop")
    spec.add_argument("--no-start", action="store_true", help="create without starting")

    p = add("create", parents=[common, spec, boot], help="create a container")
    p.add_argument("name")
    p.add_argument("--description", help="free-text description")
    p.add_argument("--no-default-modules", action="store_true",
                   help="skip modules marked as default")
    p.set_defaults(func=cmd_create)

    p = add("limits", help="change a container's CPU/memory limits")
    p.add_argument("name")
    p.add_argument("-c", "--cpu", help="CPU limit, e.g. 2 (pass '' to clear)")
    p.add_argument("-m", "--memory", help="memory limit, e.g. 2GiB (pass '' to clear)")
    p.set_defaults(func=cmd_limits)

    for action, helptext in [
        ("start", "start containers"),
        ("stop", "stop containers"),
        ("restart", "restart containers"),
        ("pause", "freeze containers"),
        ("resume", "unfreeze containers"),
    ]:
        p = add(action, help=helptext)
        p.add_argument("name", nargs="+")
        if action in ("stop", "restart"):
            p.add_argument("-f", "--force", action="store_true", help="kill instead of waiting")
        p.set_defaults(func=cmd_state(action))

    p = add("delete", aliases=["rm"], help="delete containers")
    p.add_argument("name", nargs="+")
    p.add_argument("-f", "--force", action="store_true", help="stop first if running")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_delete)

    p = add("exec", help="run a command inside a container")
    p.add_argument("name")
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="command to run (prefix with -- to pass flags)")
    p.add_argument("--timeout", type=int, default=60)
    p.set_defaults(func=cmd_exec)

    p = add("shell", help="open an interactive shell (via the lxc/incus client)")
    p.add_argument("name")
    p.add_argument("--shell", default="/bin/bash")
    p.set_defaults(func=cmd_shell)

    p = add("snapshot", help="create a snapshot")
    p.add_argument("name")
    p.add_argument("snapshot")
    p.add_argument("--stateful", action="store_true", help="include running memory state")
    p.set_defaults(func=cmd_snapshot)

    p = add("snapshots", help="list snapshots")
    p.add_argument("name")
    p.set_defaults(func=cmd_snapshots)

    p = add("restore", help="restore a snapshot")
    p.add_argument("name")
    p.add_argument("snapshot")
    p.set_defaults(func=cmd_restore)

    p = add("snap-delete", help="delete a snapshot")
    p.add_argument("name")
    p.add_argument("snapshot")
    p.set_defaults(func=cmd_snap_delete)

    p = add("images", help="list cached and suggested images")
    p.set_defaults(func=cmd_images)

    p = add("modules", help="list available bootstrap modules")
    p.set_defaults(func=cmd_modules)

    p = add("ssh-keys", help="list public keys found in ~/.ssh")
    p.set_defaults(func=cmd_ssh_keys)

    p = add("module-add", help="install a module from a file")
    p.add_argument("path", help="path to a .sh file")
    p.add_argument("--name", help="module id (default: the filename)")
    p.add_argument("--default", action="store_true",
                   help="pre-select it for new containers")
    p.add_argument("-f", "--force", action="store_true",
                   help="replace an existing module of the same name")
    p.set_defaults(func=cmd_module_add)

    p = add("module-remove", help="remove an uploaded module")
    p.add_argument("id")
    p.set_defaults(func=cmd_module_remove)

    p = add("module-set", help="save a module's default settings")
    p.add_argument("id")
    p.add_argument("--param", action="append", metavar="KEY=VALUE",
                   help="value to remember (repeatable; empty resets it)")
    p.add_argument("--default", action="store_true",
                   help="pre-select for new containers")
    p.add_argument("--no-default", action="store_true",
                   help="stop pre-selecting it")
    p.set_defaults(func=cmd_module_set)

    p = add("profiles", help="list saved bootstrap profiles")
    p.set_defaults(func=cmd_profiles)

    p = add("profile-save", parents=[common, boot],
            help="save a module selection as a bootstrap profile")
    p.add_argument("name")
    p.add_argument("--description", default="")
    p.set_defaults(func=cmd_profile_save)

    p = add("profile-delete", help="delete a bootstrap profile")
    p.add_argument("name")
    p.set_defaults(func=cmd_profile_delete)

    p = add("templates", help="list saved instance templates")
    p.set_defaults(func=cmd_templates)

    p = add("template-save", parents=[common, spec, boot],
            help="save an instance spec and bootstrap selection as a template")
    p.add_argument("name")
    p.add_argument("--description", default="", help="what the template is for")
    p.add_argument("--prefix", help="instance name prefix (default: from the name)")
    p.set_defaults(func=cmd_template_save)

    p = add("template-delete", help="delete an instance template")
    p.add_argument("name")
    p.set_defaults(func=cmd_template_delete)

    p = add("template-destroy",
            help="stop and delete every instance launched from a template")
    # The instance tag, not the template: this works after the template is gone.
    p.add_argument("template")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_template_destroy)

    p = add("template-recreate",
            help="replace every instance from a template with a fresh one")
    p.add_argument("template")
    p.add_argument("--param", action="append", metavar="KEY=VALUE",
                   help="override a saved parameter or supply a secret (repeatable)")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_template_recreate)

    p = add("template-exec",
            help="run a command on every running instance from a template")
    p.add_argument("template")
    p.add_argument("command", nargs=argparse.REMAINDER,
                   help="command to run (prefix with -- to pass flags)")
    p.add_argument("--timeout", type=int, default=300,
                   help="seconds allowed per instance (default: 300)")
    p.set_defaults(func=cmd_template_exec)

    p = add("launch", help="create one or more instances from a template")
    p.add_argument("template")
    p.add_argument("-n", "--count", type=int, default=1,
                   help="how many instances (default: 1)")
    p.add_argument("--prefix", help="name them <prefix>-N instead of the template's")
    p.add_argument("--param", action="append", metavar="KEY=VALUE",
                   help="override a saved parameter or supply a secret (repeatable)")
    p.set_defaults(func=cmd_launch)

    p = add("bootstrap", parents=[common, boot],
            help="run bootstrap modules against an existing container")
    p.add_argument("name")
    p.add_argument("--timeout", type=int, default=900,
                   help="seconds allowed per module (default: 900)")
    p.set_defaults(func=cmd_bootstrap)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0

    service = None
    if getattr(args, "needs_service", True):
        try:
            service = ContainerService(project=args.project, socket_path=args.socket)
        except LXDError as exc:
            print(RED("! %s" % exc), file=sys.stderr)
            return 2

    try:
        return args.func(args, service)
    except (ServiceError, LXDError) as exc:
        print(RED("! %s" % exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

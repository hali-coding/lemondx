"""Command line interface for lemondx."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import shutil
import sys
import time

from . import auth, cluster as cluster_mod, configure, pam
from . import health as health_checks
from .auth import (METHODS, ROLES, AuthConfig, AuthError, AuthService, local_principal,
                   parse_duration_days)
from .cluster import ClusterError, ClusterService
from .fabric import FabricError
from .hostnet import HostNetError
from .configure import SECTIONS, ConfigureError, Prompter
from .lxd import LXDError
from .nodeclient import NodeError
from .service import ContainerService, LOCAL_STORAGE_DRIVERS, ServiceError
from .server import DEFAULT_HOST, DEFAULT_PORT, serve
from .stacks import StackService

# ANSI colours, disabled when stdout is not a terminal or NO_COLOR is set.
_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(text, code):
    return "\033[%sm%s\033[0m" % (code, text) if _COLOR else str(text)


DIM = lambda s: _c(s, "2")
BOLD = lambda s: _c(s, "1")
GREEN = lambda s: _c(s, "32")
RED = lambda s: _c(s, "31")
YELLOW = lambda s: _c(s, "33")
ORANGE = lambda s: _c(s, "38;5;208")
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
    # Saved settings first, then each flag actually given on top: the auth
    # flags all default to None so "not given" can be told from a value.
    settings = None if args.ignore_config else auth.load_settings()
    overrides = {
        "methods": args.auth,
        "token_file": args.token_file,
        "session_hours": args.session_hours,
        "allow_insecure_login": args.allow_insecure_login,
        "pam_service": args.pam_service,
        "pam_admin_groups": args.pam_admin_group,
        "pam_read_groups": args.pam_read_group,
        "trusted_proxies": args.trust_proxy,
        "proxy_user_header": args.proxy_user_header,
        "proxy_groups_header": args.proxy_groups_header,
        "proxy_admin_group": args.proxy_admin_group,
        "proxy_read_group": args.proxy_read_group,
    }
    # The cluster section names the certificate that identifies this node to its
    # peers, so `serve` uses it unless told otherwise -- a node whose peers pin
    # one certificate and which serves another is federated with nobody.
    tls_cert, tls_key = args.tls_cert, args.tls_key
    cluster_settings = None if args.ignore_config else cluster_mod.load_settings()
    if not tls_cert and not args.no_tls and cluster_settings and cluster_settings["tls_cert"]:
        tls_cert, tls_key = cluster_settings["tls_cert"], cluster_settings["tls_key"]

    token = args.token
    if token is None and not args.token_file:
        # From the environment rather than argv, where every local user can
        # read it in `ps`.
        token = os.environ.get("LEMONDX_TOKEN") or None
    config = auth.build_config(settings, overrides, static_token=token)
    health_settings, warning = health_checks.load_settings()
    if warning:
        print(YELLOW("WARNING: %s" % warning), file=sys.stderr)
    if args.no_health:
        health_settings = dict(health_settings, enabled=False)
    serve(host=args.host, port=args.port, dev=args.dev, quiet=args.quiet,
          open_browser=args.open, auth_config=config,
          tls_cert=tls_cert, tls_key=tls_key,
          auth_source=auth.settings_path() if settings is not None else None,
          health_settings=health_settings, cluster_settings=cluster_settings)
    return 0


# -- configure ---------------------------------------------------------------


def _describe_rows(rows):
    width = max(len(label) for label, _ in rows) if rows else 0
    return "\n".join("  %s  %s" % (DIM(label.ljust(width)), value) for label, value in rows)


def cmd_configure(args, _service):
    if not args.section:
        sections = [{"section": name, "help": section.help, "configured": section.configured(),
                     "path": section.path()} for name, section in SECTIONS.items()]
        emit(args, sections, lambda items: "%s\n\n%s" % (table(
            [[BOLD(i["section"]), GREEN("yes") if i["configured"] else DIM("no"), i["help"]]
             for i in items], ["section", "configured", "covers"]),
            DIM("Run `lemondx configure <section>`; --show prints the saved settings.")))
        return 0

    section = SECTIONS[args.section]
    if args.reset:
        if not confirm("Delete the saved %s settings at %s?" % (section.name, section.path()),
                       args.yes):
            print("Nothing deleted." if sys.stdin.isatty() else
                  "Refusing to delete without a terminal; pass --yes.", file=sys.stderr)
            return 1
        removed = section.reset()
        emit(args, {"section": section.name, "path": section.path(), "removed": removed},
             lambda r: "%s %s" % (GREEN("+") if r["removed"] else DIM("-"),
                                  "removed %s" % r["path"] if r["removed"]
                                  else "%s was not configured" % r["section"]))
        return 0

    if args.show:
        current = section.load()
        payload = {"section": section.name, "path": section.path(),
                   "configured": current is not None,
                   "settings": current if current is not None else section.defaults()}
        emit(args, payload, lambda r: "%s %s\n%s" % (
            BOLD(r["section"]), DIM(r["path"] if r["configured"] else "(not configured; defaults)"),
            _describe_rows(section.describe(r["settings"]))))
        return 0

    if not sys.stdin.isatty():
        raise ConfigureError("`lemondx configure %s` asks questions and needs a terminal. "
                             "Edit %s instead, or see --show." % (section.name, section.path()))
    prompter = Prompter()
    try:
        current = section.load()
    except (AuthError, ConfigureError) as exc:
        # Configuring is how a broken file gets fixed, so start over from defaults.
        prompter.say(YELLOW("! %s" % exc.message))
        prompter.say("Starting from defaults.")
        current = None
    settings = section.prompt(prompter, current)
    prompter.say()
    prompter.say(BOLD("Summary"))
    prompter.say(_describe_rows(section.describe(settings)))
    prompter.say()
    if not prompter.yes_no("Save to %s?" % section.path(), True):
        raise ConfigureError("Nothing was saved.", 1)
    saved = section.save(settings)
    prompter.say("%s saved %s" % (GREEN("+"), section.path()))
    section.after_save(prompter, saved)
    if getattr(args, "json", False):
        emit(args, {"section": section.name, "path": section.path(), "settings": saved}, None)
    return 0


# -- access ------------------------------------------------------------------
#
# These edit the data directory directly rather than asking a running server:
# whoever can write there already controls every credential in it, and the
# server notices changed files on its next request.


def _when(epoch):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch)) if epoch else "-"


def cmd_tokens(args, _service):
    tokens = AuthService().list_tokens(local_principal())
    emit(args, tokens, lambda items: table(
        [[t["id"], t["name"], t["owner"], t["role"], _when(t["created"]),
          RED("expired") if t["expired"] else _when(t["expires"]) if t["expires"] else DIM("never"),
          _when(t["last_used"])] for t in items],
        ["id", "name", "owner", "role", "created", "expires", "last used"]))
    return 0


def cmd_token_create(args, _service):
    token = AuthService().create_token(
        local_principal(), args.name, role=args.role,
        expires_days=parse_duration_days(args.expires), owner=args.owner)
    emit(args, token, lambda t: "%s created token %s (%s, owner %s, %s)\n\n  %s\n\n%s" % (
        GREEN("+"), BOLD(t["name"]), t["role"], t["owner"],
        "expires %s" % _when(t["expires"]) if t["expires"] else "no expiry",
        t["token"], DIM("Shown once. Send it as `Authorization: Bearer <token>`.")))
    return 0


def cmd_token_revoke(args, _service):
    result = AuthService().revoke_token(local_principal(), args.id)
    emit(args, result, lambda r: "%s revoked token %s" % (GREEN("+"), r["revoked"]))
    return 0


def cmd_users(args, _service):
    users = AuthService().list_users()
    emit(args, users, lambda items: table(
        [[u["name"], u["role"], _when(u["created"]), _when(u["updated"])] for u in items],
        ["name", "role", "created", "updated"]))
    return 0


def _new_password(name):
    # Never from argv. An env var serves scripts, as it does for module secrets.
    password = os.environ.get("LEMONDX_PASSWORD")
    if password is not None:
        return password
    if not sys.stdin.isatty():
        raise AuthError("No terminal to ask for a password on; set LEMONDX_PASSWORD.", 400)
    password = getpass.getpass("Password for %s: " % name)
    if getpass.getpass("Again: ") != password:
        raise AuthError("Passwords do not match.", 400)
    return password


def cmd_user_add(args, _service):
    service = AuthService()
    if any(u["name"] == args.name for u in service.list_users()):
        raise AuthError("User %s already exists; use `lemondx user-set`." % args.name, 409)
    result = service.set_user(args.name, password=_new_password(args.name), role=args.role)
    emit(args, result, lambda r: "%s added %s user %s" % (GREEN("+"), r["role"], BOLD(r["name"])))
    return 0


def cmd_user_set(args, _service):
    service = AuthService()
    if not any(u["name"] == args.name for u in service.list_users()):
        raise AuthError("No such user: %s" % args.name, 404)
    if not args.password and not args.role:
        raise AuthError("Nothing to change: pass --password and/or --role.", 400)
    result = service.set_user(
        args.name, password=_new_password(args.name) if args.password else None, role=args.role)
    emit(args, result, lambda r: "%s updated %s (%s)" % (GREEN("+"), BOLD(r["name"]), r["role"]))
    return 0


def cmd_user_remove(args, _service):
    result = AuthService().remove_user(args.name)
    emit(args, result, lambda r: "%s removed user %s" % (GREEN("+"), r["removed"]))
    return 0


def cmd_pam_test(args, _service):
    """Run the same PAM check the server would, to debug a PAM stack."""
    # The same service and groups `serve` would use, unless overridden here.
    saved = auth.load_settings() or auth.DEFAULT_SETTINGS
    args.service = args.service or saved["pam_service"]
    config = AuthConfig(
        methods=["pam"], pam_service=args.service,
        pam_admin_groups=args.pam_admin_group if args.pam_admin_group is not None
        else saved["pam_admin_groups"],
        pam_read_groups=args.pam_read_group if args.pam_read_group is not None
        else saved["pam_read_groups"])
    service = AuthService(config)
    for warning in service.startup_warnings():
        print(YELLOW("! %s" % warning), file=sys.stderr)
    password = os.environ.get("LEMONDX_PASSWORD")
    if password is None:
        password = getpass.getpass("Password for %s: " % args.user)
    ok, reason = pam.authenticate(args.service, args.user, password)
    role = service._pam_role(args.user)
    result = {"user": args.user, "service": args.service, "authenticated": ok,
              "reason": reason or None, "role": role}

    def render(r):
        if not r["authenticated"]:
            return "%s PAM refused %s: %s" % (RED("x"), r["user"], r["reason"])
        if not r["role"]:
            return "%s password OK, but %s is in none of: %s" % (
                YELLOW("!"), r["user"],
                ", ".join(config.pam_admin_groups + config.pam_read_groups) or "(no groups)")
        return "%s %s would log in as %s" % (GREEN("+"), r["user"], r["role"])
    emit(args, result, render)
    return 0 if ok and role else 1


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
        if fabric_line:
            lines.append("%s  %s" % (DIM("fabric "), fabric_line))
        for issue in s["issues"]:
            lines.append(YELLOW("  ! " + issue))
        return "\n".join(lines)

    # Only when it is actually on: a line saying "off" on every status of
    # every unfederated host is noise, not information.
    fabric_line = ""
    try:
        state = _fabric(service).status()
        if state["enabled"]:
            fabric_line = "%s on %s, %d peer route(s)%s" % (
                state["subnet"], state["bridge"],
                sum(1 for r in state["routes"] if r["state"] == "ok"),
                "" if state["privileged"] else YELLOW(" -- cannot program routes"))
            status = dict(status, fabric=state)
    except Exception:                       # status must never fail over this
        pass

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
        disk=args.disk, pool=args.pool, network=args.network,
        fabric=_fabric_bridge(service) if args.fabric else None,
        description=args.description, ephemeral=args.ephemeral,
        start=not args.no_start, secureboot=not args.no_secureboot, bootstrap=bootstrap,
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
        # One call for every name, so several containers change together and
        # --json prints one document rather than one per name concatenated.
        result = service.change_state_many(
            args.name, action, force=getattr(args, "force", False))

        def render(payload):
            lines = []
            for item in payload["instances"]:
                if item["ok"]:
                    lines.append("%s %s is now %s" % (
                        GREEN("+"), BOLD(item["name"]),
                        item["container"]["status"].lower()))
                else:
                    lines.append("%s %s: %s" % (RED("!"), item["name"], item["error"]))
            return "\n".join(lines)

        emit(args, result, render)
        return 0 if result["ok"] else 1
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
                line = "    %s=%s" % (param["name"], short_value(value) or DIM("(empty)"))
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


def short_value(value):
    """A parameter value for a one-line listing; a config file is summarised."""
    if "\n" not in value:
        return value
    return DIM("(%d lines)" % len(value.rstrip("\n").splitlines()))


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


def sync_note(record):
    """A line about what the rest of the cluster made of a save or a delete.

    Nothing at all on a lemondx that is not federated, which is most of them.
    """
    outcome = (record or {}).get("synced") if isinstance(record, dict) else None
    if not outcome:
        return ""
    failed = [r for r in outcome["results"] if not r["ok"]]
    verb = "removed from" if outcome["deleted"] else "synced to"
    if not failed:
        return "\n  %s %s %d node(s): %s" % (
            GREEN("+"), verb, len(outcome["nodes"]), ", ".join(outcome["nodes"]))
    return "\n  %s not %s %s -- run `lemondx cluster sync` when they are back" % (
        YELLOW("!"), verb,
        "; ".join("%s (%s)" % (r["node"], r["error"] or "failed") for r in failed))


def cmd_module_add(args, service):
    path = os.path.expanduser(args.path)
    try:
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
    except OSError as exc:
        raise ServiceError("Cannot read '%s': %s" % (args.path, exc))
    module = cluster_service(service).upload_module(
        args.name or os.path.basename(path), content, overwrite=args.force)
    if args.default:
        service.update_module_settings(module["id"], is_default=True)
    emit(args, module, lambda m: "%s installed module %s%s%s" % (
        GREEN("+"), BOLD(m["id"]),
        " (default for new containers)" if args.default else "", sync_note(m)))
    return 0


def cmd_module_remove(args, service):
    result = cluster_service(service).remove_module(
        args.id, everywhere=not args.local_only)
    emit(args, result, lambda r: "%s removed module %s%s%s" % (
        GREEN("+"), BOLD(r["deleted"]),
        " -- the built-in module is back" if r["restored_builtin"] else "",
        sync_note(r)))
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
        " ".join("%s=%s" % (p["name"], short_value(p["value"])) for p in m["params"])))
    return 0


def cmd_profiles(args, service):
    profiles = service.list_bootstrap_profiles()
    emit(args, profiles, lambda items: table(
        [[p["name"], " → ".join(p["modules"]),
          " ".join("%s=%s" % (k, short_value(v)) for k, v in p["params"].items()) or "-",
          len(p["ssh_keys"]) or "-",
          p["description"] or "-"] for p in items],
        ["name", "modules", "params", "keys", "description"]))
    return 0


def cmd_profile_save(args, service):
    modules, params, ssh_keys = resolve_selection(args, service)
    profile = cluster_service(service).save_bootstrap_profile(
        name=args.name, modules=modules, params=params,
        description=args.description or "", ssh_keys=ssh_keys)
    emit(args, profile, lambda p: "%s saved profile %s (%s%s)%s" % (
        GREEN("+"), BOLD(p["name"]), " → ".join(p["modules"]),
        ", %d key(s)" % len(p["ssh_keys"]) if p["ssh_keys"] else "", sync_note(p)))
    return 0


def cmd_profile_delete(args, service):
    result = cluster_service(service).delete_bootstrap_profile(
        args.name, everywhere=not args.local_only)
    emit(args, result, lambda r: "%s deleted profile %s%s" % (
        GREEN("+"), r["deleted"], sync_note(r)))
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
    kind = ""
    if t["type"] == "virtual-machine":
        kind = " (vm)" if t["secureboot"] else " (vm, no secure boot)"
    return "%s%s%s" % (t["image"], kind,
                       " · " + size if size else "")


def cmd_templates(args, service):
    templates = service.list_templates()
    emit(args, templates, lambda items: table(
        [[t["name"], describe_template(t), t["name_prefix"] + "-N",
          " → ".join(t["bootstrap"]["modules"]) or "-",
          len(t["bootstrap"]["ssh_keys"]) or "-",
          "every %ds" % t["app_check"]["interval_seconds"] if t["app_check"] else "-",
          t["description"] or "-"] for t in items],
        ["name", "instance", "names", "modules", "keys", "app check", "description"]))
    return 0


def read_app_check(args, service):
    """The template's app check from `template-save`'s flags.

    Without --app-check an existing template keeps the check it has, since
    retyping a script to change a memory limit would be a trap; --no-app-check
    is how one is removed.
    """
    if args.no_app_check:
        return None
    if args.app_check is None:
        current = next((t for t in service.list_templates() if t["name"] == args.name), None)
        app_check = dict((current or {}).get("app_check") or {}) or None
        if app_check is None and (args.app_check_interval or args.app_check_timeout):
            raise ServiceError("--app-check-interval and --app-check-timeout need "
                               "--app-check, or a template that already has one.")
    else:
        try:
            if args.app_check == "-":
                script = sys.stdin.read()
            else:
                with open(args.app_check, encoding="utf-8") as handle:
                    script = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise ServiceError("Cannot read the app check script: %s" % exc)
        app_check = {"script": script}
    if app_check is not None:
        if args.app_check_interval is not None:
            app_check["interval_seconds"] = args.app_check_interval
        if args.app_check_timeout is not None:
            app_check["timeout_seconds"] = args.app_check_timeout
    return app_check


def describe_app_check(app_check):
    return "every %ds, timeout %ds" % (app_check["interval_seconds"],
                                       app_check["timeout_seconds"])


def cmd_template_save(args, service):
    modules, params, ssh_keys = resolve_selection(args, service)
    app_check = read_app_check(args, service)
    template = cluster_service(service).save_template(
        name=args.name, image=args.image or service.default_image(),
        instance_type="virtual-machine" if args.vm else "container",
        cpu=args.cpu, memory=args.memory, disk=args.disk, pool=args.pool,
        network=args.network, fabric=args.fabric, profiles=args.profile,
        ephemeral=args.ephemeral,
        start=not args.no_start, secureboot=not args.no_secureboot,
        bootstrap={"modules": modules, "params": params, "ssh_keys": ssh_keys},
        description=args.description or "", name_prefix=args.prefix,
        app_check=app_check,
    )
    emit(args, template, lambda t: "%s saved template %s: %s%s%s%s" % (
        GREEN("+"), BOLD(t["name"]), describe_template(t),
        (" · " + " → ".join(t["bootstrap"]["modules"]))
        if t["bootstrap"]["modules"] else "",
        " · app check " + describe_app_check(t["app_check"]) if t["app_check"] else "",
        sync_note(t)))
    return 0


def cmd_template_delete(args, service):
    result = cluster_service(service).delete_template(
        args.name, everywhere=not args.local_only)
    emit(args, result, lambda r: "%s deleted template %s%s" % (
        GREEN("+"), r["deleted"], sync_note(r)))
    return 0


def render_template_run(result):
    """Per-instance outcome of a launch, recreate or destroy."""
    lines = []
    if "instances" not in result:
        # A run record rather than a result: what a background launch returns.
        result = result.get("result") or {"instances": [], "notes": result.get("notes")}
    for note in result.get("notes") or []:
        lines.append(YELLOW("! %s" % note))
    for instance in result["instances"]:
        container = instance["container"]
        where = CYAN(" on %s" % instance["node"]) if instance.get("node") else ""
        if instance["error"]:
            lines.append("%s %s%s: %s" % (RED("!"), BOLD(instance["name"]), where,
                                          instance["error"]))
        elif container is None:
            lines.append("%s deleted %s%s" % (GREEN("+"), BOLD(instance["name"]), where))
        else:
            lines.append("%s %s%s is %s%s" % (
                GREEN("+") if instance["ok"] else RED("!"), BOLD(instance["name"]), where,
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
    cluster = cluster_service(service)
    targets = cluster.resolve_targets(args.node, args.group)
    if not args.json:
        # Where only matters once there is more than one: on a host that has
        # never been federated, naming it would be noise about a feature the
        # user is not using.
        where = "" if targets == [cluster.local_name()] else " on %s" % ", ".join(targets)
        print(DIM("Launching %d instance(s) from %s%s (this pulls the image on "
                  "first use)..." % (args.count, template["name"], where)), flush=True)
    result = cluster.launch_template(template["name"], count=args.count,
                                     prefix=args.prefix, params=params,
                                     nodes=args.node, groups=args.group)
    emit(args, result, render_template_run)
    return 0 if result["ok"] else 1


# -- stacks ----------------------------------------------------------------


def stack_service(service):
    return StackService(cluster_service(service))


def describe_step(step):
    if step["type"] == "sleep":
        return "sleep %ds" % step["seconds"]
    if step["type"] == "wait_healthy":
        return "wait healthy (%ds)" % step["timeout_seconds"]
    where = ", ".join(step["nodes"] + ["group " + g for g in step["groups"]])
    return "%s: %s x%d%s%s" % (step["id"], step["template"], step["count"],
                               "" if step["wait_bootstrap"] else " (no wait)",
                               " on " + where if where else "")


def describe_stack(stack):
    return " → ".join(" | ".join(describe_step(s) for s in stage["steps"])
                      for stage in stack["stages"])


def cmd_stacks(args, service):
    stacks = stack_service(service)
    items = stacks.list_stacks()
    running = stacks.stack_instances()
    for item in items:
        item["instances"] = running["stacks"].get(item["name"], [])
    for problem in running["errors"]:
        print(YELLOW("! %s did not answer; its instances are not counted: %s"
                     % (problem["node"], problem["error"])), file=sys.stderr)
    emit(args, items, lambda rows: table(
        [[s["name"], describe_stack(s) or "-",
          "%d (%d running)" % (len(s["instances"]), sum(
              1 for i in s["instances"] if i["status"] == "Running"))
          if s["instances"] else "-", s["description"] or "-"] for s in rows],
        ["name", "stages", "instances", "description"]))
    return 0


def stack_members(stacks, name):
    members = stacks.stack_instances()
    if members["errors"]:
        raise ServiceError("Cannot list the stack's instances: %s" % "; ".join(
            "%s: %s" % (e["node"], e["error"]) for e in members["errors"]))
    return members["stacks"].get(name, [])


def describe_members(members):
    nodes = {m["node"] for m in members}
    return ", ".join(m["name"] if len(nodes) == 1 else "%s on %s" % (m["name"], m["node"])
                     for m in members)


def cmd_stack_state(args, service):
    stacks = stack_service(service)
    name = stacks.get_stack(args.name)["name"]
    members = stack_members(stacks, name)
    if not members:
        raise ServiceError("Stack '%s' has no instances." % name)
    result = stacks.stack_state(name, args.action, members)
    emit(args, result, lambda r: "\n".join(
        "%s %s%s" % (GREEN("+") if i["ok"] else RED("!"), BOLD(i["name"]),
                     "" if i["ok"] else ": " + (i.get("error") or "failed"))
        for i in r["instances"]))
    return 0 if result["ok"] else 1


def cmd_stack_destroy(args, service):
    stacks = stack_service(service)
    name = stacks.get_stack(args.name)["name"]
    members = stack_members(stacks, name)
    if not members:
        raise ServiceError("Stack '%s' has no instances." % name)
    if not args.json:
        print("Instances of %s: %s" % (BOLD(name), describe_members(members)))
    if not confirm("Destroy all %d? Their filesystems and snapshots are deleted."
                   % len(members), args.yes):
        print(DIM("nothing destroyed"))
        return 1
    run = stacks.destroy_stack(name, members)
    emit(args, run, render_stack_run)
    return 0 if run["ok"] else 1


def cmd_stack_show(args, service):
    stack = stack_service(service).get_stack(args.name)
    # The definition is the useful output either way: it is what stack-save
    # takes back, so show, edit and save is the round trip.
    print(json.dumps({"description": stack["description"], "stages": stack["stages"]},
                     indent=2))
    return 0


def cmd_stack_save(args, service):
    try:
        if args.file == "-":
            raw = json.load(sys.stdin)
        else:
            with open(args.file, encoding="utf-8") as handle:
                raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ServiceError("Cannot read the stack definition: %s" % exc)
    if isinstance(raw, list):
        raw = {"stages": raw}
    if not isinstance(raw, dict):
        raise ServiceError("A stack definition is an object with 'stages', or a list of them.")
    description = args.description if args.description is not None \
        else raw.get("description", "")
    stack = stack_service(service).save_stack(args.name, stages=raw.get("stages"),
                                              description=description)
    emit(args, stack, lambda s: "%s saved stack %s: %s%s" % (
        GREEN("+"), BOLD(s["name"]), describe_stack(s), sync_note(s)))
    return 0


def cmd_stack_delete(args, service):
    result = stack_service(service).delete_stack(args.name, everywhere=not args.local_only)
    emit(args, result, lambda r: "%s deleted stack %s%s" % (
        GREEN("+"), r["deleted"], sync_note(r)))
    return 0


STEP_MARK = {"done": GREEN("+"), "failed": RED("!"), "ready": CYAN("~"),
             "skipped": DIM("-"), "cancelled": DIM("-")}


def render_step(record):
    what = record["id"]
    if record["type"] == "launch":
        what += " (%s x%d)" % (record["template"], record["count"])
    elif record["type"] == "sleep":
        what += " (sleep)"
    elif record["type"] == "destroy":
        what = "destroy %d existing" % record["count"]
    else:
        what += " (wait healthy)"
    line = "%s %s %s" % (STEP_MARK.get(record["state"], DIM("·")), BOLD(what),
                         DIM(record["state"]))
    outputs = record.get("outputs")
    if outputs and outputs["names"]:
        line += ": " + ", ".join(
            "%s%s" % (name, " " + DIM(ip) if ip else "")
            for name, ip in zip(outputs["names"],
                                outputs["ips"] + [""] * len(outputs["names"])))
    if record["error"]:
        line += "\n    " + RED(record["error"])
    elif record["detail"] and record["state"] not in ("done", "ready"):
        line += DIM(" -- %s" % record["detail"])
    return line


def render_stack_run(run):
    lines = []
    for index, stage in enumerate(run["stages"], 1):
        lines.append(DIM("stage %d" % index))
        lines.extend("  " + render_step(step) for step in stage["steps"])
    what = "stack %s%s" % (run["stack"], "" if run["action"] == "launch"
                           else " " + run["action"])
    lines.append(GREEN("%s finished" % what) if run["ok"]
                 else RED("%s %s" % (what, "cancelled" if run["cancelled"] else "failed")))
    return "\n".join(lines)


def cmd_stack_launch(args, service):
    stacks = stack_service(service)
    stack = next((s for s in stacks.list_stacks() if s["name"] == args.name), None) \
        or stacks.get_stack(args.name)
    params = parse_params(args.param)
    templates = {t["name"]: t for t in service.list_templates()}
    modules = [m for stage in stack["stages"] for step in stage["steps"]
               if step["type"] == "launch" and step["template"] in templates
               for m in templates[step["template"]]["bootstrap"]["modules"]]
    fill_secrets(modules, params, service)
    # Values the stack's own steps ask for with {{params.NAME}}: likely a
    # password, so never echoed, and never needed on the command line.
    for input_name in [n for n in stack["inputs"] if not params.get(n)]:
        if os.environ.get(input_name):
            params[input_name] = os.environ[input_name]
        elif sys.stdin.isatty():
            value = getpass.getpass("%s: " % input_name)
            if value:
                params[input_name] = value
    replace = None
    if args.relaunch:
        replace = stack_members(stacks, stack["name"])
        if replace and not args.json:
            print("Instances of %s: %s" % (BOLD(stack["name"]), describe_members(replace)))
        if replace and not confirm("Destroy all %d and launch the stack again?"
                                   % len(replace), args.yes):
            print(DIM("nothing changed"))
            return 1
    run = stacks.launch_stack(stack["name"], params=params, background=True,
                              replace=replace)
    seen = {}
    try:
        while run["finished_at"] is None:
            if not args.json:
                # Each step once per state it reaches, as it reaches it.
                for stage in run["stages"]:
                    for step in stage["steps"]:
                        if step["state"] != "pending" and seen.get(step["id"]) != step["state"]:
                            seen[step["id"]] = step["state"]
                            print(render_step(step), flush=True)
            time.sleep(1)
            run = next(r for r in stacks.stack_runs() if r["stack"] == stack["name"])
    except KeyboardInterrupt:
        print(YELLOW("cancelling: nothing new starts; launches under way finish "
                     "(Ctrl-C again to leave them)"), file=sys.stderr)
        stacks.cancel_stack_run(stack["name"])
        while run["finished_at"] is None:
            time.sleep(1)
            run = next(r for r in stacks.stack_runs() if r["stack"] == stack["name"])
    emit(args, run, render_stack_run)
    return 0 if run["ok"] else 1


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


HEALTH_COLORS = {
    "healthy": GREEN, "degraded": ORANGE, "unhealthy": RED,
    "starting": CYAN, "unknown": DIM, "paused": DIM,
}


APP_COLORS = {"ok": GREEN, "warning": ORANGE, "critical": RED, "unknown": DIM}


def cmd_health(args, service):
    settings, warning = health_checks.load_settings()
    if warning:
        print(YELLOW("! %s" % warning), file=sys.stderr)
    records = service.check_health(names=args.name or None, window=args.window, settings=settings)

    def render(items):
        rows = []
        for r in items:
            cpu, memory, load, probe = r["cpu"], r["memory"], r["load"], r["probe"]
            if load and load["scope"] == "instance":
                # 1, 5 and 15 minute averages from serve; a one-off check has
                # only its window's average.
                load_text = " ".join("%.2f" % v for v in load["avg"] if v is not None)
                if load["window"]:
                    load_text += DIM(" (%gs)" % load["window"])
                elif load["warming"]:
                    load_text += DIM(" (warming)")
            elif load:
                load_text = DIM("host-wide")
            else:
                load_text = "-"
            rows.append([
                BOLD(r["name"]),
                HEALTH_COLORS.get(r["status"], str)(r["status"]),
                "%g%% of %d" % (cpu["percent"], cpu["cores"]) if cpu else "-",
                "%g%%" % memory["percent"] if memory["percent"] is not None
                else human_bytes(memory["usage"]),
                load_text,
                ("%dms" % probe["ms"]) if probe and probe["ok"] else (RED("failed") if probe else "-"),
                APP_COLORS.get(r["app"]["status"], str)(r["app"]["status"]) if r["app"] else "-",
                "; ".join(r["reasons"]) or DIM("-"),
            ])
        return table(rows, ["name", "health", "cpu", "memory", "load", "probe", "app",
                            "reasons"])

    emit(args, records, render)
    statuses = {r["status"] for r in records}
    if "unhealthy" in statuses:
        return 2
    return 1 if statuses & {"degraded", "unknown", "starting"} else 0


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


def cmd_networks(args, service):
    networks = service.list_networks()
    emit(args, networks, lambda items: table([
        [
            BOLD(n["name"]) + (DIM(" (default)") if n["default"] else ""),
            n["type"], n["ipv4_address"] or "-", n["ipv6_address"] or "-",
            n["used_by"],
            "yes" if n["manageable"] else ("host" if not n["managed"] else "read-only"),
        ]
        for n in items
    ], ["name", "type", "ipv4", "ipv6", "used by", "managed"]))
    return 0


def cmd_network_show(args, service):
    network = service.get_network(args.name)

    def render(n):
        def family(f):
            if not f["address"] or f["address"] == "none":
                return "disabled"
            return "%s  nat %s, dhcp %s" % (f["address"], "on" if f["nat"] else "off",
                                            "on" if f["dhcp"] else "off")
        lines = [
            "%s  %s%s" % (BOLD(n["name"]), n["type"], DIM(" (default)") if n["default"] else ""),
            "%s  %s" % (DIM("ipv4   "), family(n["ipv4"])),
            "%s  %s" % (DIM("ipv6   "), family(n["ipv6"])),
            "%s  %s" % (DIM("dns    "), n["dns_domain"] or "-"),
            "%s  %s" % (DIM("status "), "managed" if n["manageable"]
                        else "read-only: " + n["read_only_reason"]),
        ]
        if n["description"]:
            lines.append("%s  %s" % (DIM("about  "), n["description"]))
        if n["instances"] or n["profiles"]:
            lines.append("%s  %s" % (DIM("used by"), ", ".join(
                ["profile " + p for p in n["profiles"]] + n["instances"])))
        if n["leases"]:
            lines.append("")
            lines.append(table([[l["hostname"] or "-", l["address"], l["hwaddr"] or "-",
                                 l["type"]] for l in n["leases"]],
                               ["name", "address", "mac", "kind"]))
        return "\n".join(lines)

    emit(args, network, render)
    return 0


def cmd_network_subnets(args, service):
    subnets = service.subnets()
    emit(args, subnets, lambda items: table(
        [[s["subnet"], s["interface"]] for s in items], ["subnet", "interface"]))
    return 0


def network_config(args):
    """The bridge config keys the flags stand for, plus any --config pairs."""
    config = parse_params(args.config)
    for flag, key in (("ipv4", "ipv4.address"), ("ipv6", "ipv6.address"),
                      ("dns_domain", "dns.domain"), ("mtu", "bridge.mtu")):
        if getattr(args, flag) is not None:
            config[key] = getattr(args, flag)
    if args.nat is not None:
        config["ipv4.nat"] = "true" if args.nat else "false"
        if config.get("ipv6.address", "none") != "none":
            config["ipv6.nat"] = config["ipv4.nat"]
    return config


def cmd_network_create(args, service):
    network = service.create_network(
        args.name, description=args.description or "", config=network_config(args))
    emit(args, network, lambda n: "%s created network %s (%s)" % (
        GREEN("+"), BOLD(n["name"]), n["ipv4"]["address"] or "no IPv4"))
    return 0


def cmd_network_set(args, service):
    config = network_config(args)
    if args.description is None and not config:
        raise ServiceError("Give at least one setting to change.")
    network = service.update_network(args.name, description=args.description, config=config)
    emit(args, network, lambda n: "%s updated network %s" % (GREEN("+"), BOLD(n["name"])))
    return 0


def cmd_network_delete(args, service):
    if not confirm("Delete network '%s'?" % args.name, args.yes):
        print(DIM("skipped %s" % args.name))
        return 0
    result = service.delete_network(args.name)
    emit(args, result, lambda r: "%s deleted network %s" % (GREEN("+"), BOLD(r["deleted"])))
    return 0


# -- cluster ---------------------------------------------------------------


def cluster_service(service):
    """A ClusterService configured the way `serve` on this host would be.

    The same auth settings matter here: issuing a join code hands a peer an API
    token, and whether a token means anything is exactly what those settings
    decide.
    """
    return ClusterService(service, AuthService(auth.build_config(auth.load_settings())))


def _node_state(node):
    state = node.get("state") or {}
    if not state:
        return DIM("-")
    if not state["reachable"]:
        return RED("unreachable")
    if not state["ready"]:
        return YELLOW("not set up")
    return GREEN("ok")


def _render_nodes(nodes):
    return table([[
        BOLD(n["name"]) + (DIM(" (this node)") if n["self"] else ""),
        n["url"] or DIM("-"),
        _node_state(n),
        "%s %s" % ((n["state"] or {}).get("product") or "-",
                   (n["state"] or {}).get("server_version") or ""),
        str((n["state"] or {}).get("containers", "-")),
        ", ".join(n["groups"]) or DIM("-"),
        (n["state"] or {}).get("error") or "",
    ] for n in nodes], ["node", "url", "state", "daemon", "instances", "groups", "note"])


def cmd_cluster_refresh(args, service):
    result = cluster_service(service).sync_members()

    def render(r):
        lines = ["%s %s %s" % (GREEN("+") if row["ok"] else RED("!"), BOLD(row["node"]),
                               "" if row["ok"] else row["error"] or "unreachable")
                 for row in r["results"]]
        lines.append(DIM("%d member(s) known here" % len(r["members"])))
        return "\n".join(lines)

    emit(args, result, render)
    return 0 if result["ok"] else 1


def cmd_cluster_reconcile(args, service):
    """Settle this node's shared definitions against the rest of the cluster."""
    result = cluster_service(service).reconcile(apply=not args.dry_run)

    def render(r):
        lines = []
        for row in r["unreachable"]:
            lines.append("%s %s %s" % (RED("!"), BOLD(row["node"]), row["error"]))
        for row in r["actions"]:
            verb = {"pull": "pull", "push": "push", "delete": "delete"}[row["action"]]
            where = "from %s" % row["from"] if row["action"] == "pull" else \
                "on %s" % row["node"] if row["node"] != r.get("node") else "here"
            mark = DIM("~") if not row["applied"] else \
                GREEN("+") if row["ok"] else RED("!")
            lines.append("%s %s %s %s%s" % (
                mark, verb, BOLD("%s/%s" % (row["kind"], row["name"])), where,
                "" if row["ok"] else DIM(" -- %s" % row["error"])))
        for row in r["conflicts"]:
            lines.append("%s %s differs on %s -- push the copy that is right with "
                         "`lemondx cluster sync`"
                         % (YELLOW("?"), BOLD("%s/%s" % (row["kind"], row["name"])),
                            ", ".join(row["nodes"])))
        if not lines:
            return GREEN("+") + " every member holds the same definitions"
        lines.append(DIM("%d change(s), %d conflict(s), against %s"
                         % (len(r["actions"]), len(r["conflicts"]),
                            ", ".join(r["nodes"]) or "nobody")))
        if not r["applied"]:
            lines.append(DIM("nothing was changed (--dry-run)"))
        return "\n".join(lines)

    emit(args, result, render)
    return 0 if result["ok"] else 1


def cmd_cluster_leave(args, service):
    cluster = cluster_service(service)
    peers = [n["name"] for n in cluster.list_nodes(probe=False) if not n["self"]]
    if not confirm(
            "Leave the cluster? %s will be told to forget this node; its instances, "
            "templates and modules stay." % (", ".join(peers) or "Nobody"), args.yes):
        print(DIM("still a member"))
        return 1
    result = cluster.leave()

    def render(r):
        lines = ["%s left the cluster (forgot %s)"
                 % (GREEN("+"), ", ".join(r["left"]) or "nobody")]
        for row in r["told"]:
            lines.append("%s %s %s" % (GREEN("+") if row["ok"] else RED("!"),
                                       BOLD(row["node"]),
                                       "forgot this node" if row["ok"]
                                       else row["error"] or "unreachable"))
        if r["note"]:
            lines.append(YELLOW("! %s" % r["note"]))
        return "\n".join(lines)

    emit(args, result, render)
    return 0


def cmd_cluster_rotate(args, service):
    result = cluster_service(service).rotate_secret()

    def render(r):
        lines = ["%s %s %s" % (GREEN("+") if row["ok"] else RED("!"), BOLD(row["node"]),
                               "took the new credential" if row["ok"]
                               else row["error"] or "unreachable")
                 for row in r["results"]]
        if r["stranded"]:
            lines.append(YELLOW(
                "! %s did not get it and is now cut off; it has to rejoin."
                % ", ".join(r["stranded"])))
        else:
            lines.append("%s every member holds the new credential" % GREEN("+"))
        return "\n".join(lines)

    emit(args, result, render)
    return 0 if result["ok"] else 1


def cmd_cluster_nodes(args, service):
    nodes = cluster_service(service).list_nodes(probe=not args.no_probe)
    emit(args, nodes, _render_nodes)
    return 0


def cmd_cluster_status(args, service):
    cluster = cluster_service(service)
    info = cluster.info()
    payload = dict(info, nodes=cluster.list_nodes(probe=not args.no_probe))

    def render(p):
        node = p["node"]
        rows = [("name", BOLD(node["name"])),
                ("address", node["url"] or DIM("none -- no route off this host")),
                ("fingerprint", p["fingerprint_pretty"] or DIM("no certificate yet")),
                ("cluster", GREEN("member of %d node(s)" % len(p["nodes"]))
                 if p["in_cluster"] else DIM("not in a cluster")),
                ("new members", "accepted" if p["allow_enrollment"] else "refused")]
        if p["remote_requires_token"]:
            rows.append(("authentication", "off for this host, API token required "
                                           "from others"))
        else:
            rows.append(("authentication", GREEN("on") if p["auth_enabled"] else RED("off")))
        out = [BOLD("This node"), _describe_rows(rows)]
        if p["reachable_because"]:
            out.append(YELLOW("! %s" % p["reachable_because"]))
        if not p["in_cluster"]:
            out.append(DIM("  `lemondx cluster invite` starts a cluster here; "
                           "`lemondx cluster join <code>` joins one."))
        out += ["", BOLD("Nodes"), _render_nodes(p["nodes"])]
        return "\n".join(out)

    emit(args, payload, render)
    return 0


def cmd_cluster_show(args, service):
    node = cluster_service(service).describe_node(args.name)

    def render(n):
        state = n["state"] or {}
        rows = [("address", n["url"] or DIM("-")),
                ("fingerprint", cluster_mod.pretty_fingerprint(n["fingerprint"])
                 or DIM("none")),
                ("state", _node_state(n)),
                ("groups", ", ".join(n["groups"]) or DIM("-"))]
        if state.get("error"):
            rows.append(("error", RED(state["error"])))
        out = [BOLD(n["name"]), _describe_rows(rows), "", BOLD("Instances")]
        out.append(table([[c["name"], STATUS_COLORS.get(c["status"], str)(c["status"]),
                           ", ".join(c.get("ipv4") or []) or DIM("-"),
                           c.get("template") or DIM("-")]
                          for c in n["instances"]],
                         ["name", "status", "ipv4", "template"]))
        return "\n".join(out)

    emit(args, node, render)
    return 0


def cmd_cluster_invite(args, service):
    minutes = args.expires
    result = cluster_service(service).create_invite(expires_minutes=minutes, note=args.note or "")

    def render(r):
        lines = [
            "%s join code for the cluster around %s (%d member(s)), valid for "
            "%g minutes:" % (GREEN("+"), BOLD(r["node"]["name"]), r["members"],
                             r["expires_in_minutes"]),
            "",
            "    %s" % r["code"],
            "",
            DIM("Run `lemondx cluster join '<code>'` on the node that should join -- "
                "it needs no setup of its own."),
            DIM("Single use, and it carries this node's address and certificate:"),
            DIM("    %s" % r["node"]["url"]),
            DIM("    %s" % r["fingerprint_pretty"]),
        ]
        if r["warning"]:
            lines += ["", YELLOW("! %s" % r["warning"])]
        return "\n".join(lines)

    emit(args, result, render)
    return 0


def cmd_cluster_invites(args, service):
    invites = cluster_service(service).list_invites()
    emit(args, invites, lambda items: table(
        [[i["id"], _when(i["created"]),
          RED("expired") if i["expired"] else _when(i["expires"]), i["note"] or DIM("-")]
         for i in items], ["id", "created", "expires", "note"]))
    return 0


def cmd_cluster_revoke_invite(args, service):
    result = cluster_service(service).revoke_invite(args.id)
    emit(args, result, lambda r: "%s revoked join code %s" % (GREEN("+"), r["revoked"]))
    return 0


def cmd_cluster_join(args, service):
    code = args.code
    if code == "-":
        code = sys.stdin.read().strip()
    cluster = cluster_service(service)
    result = cluster.join(code, description=args.description or "")
    local, local_url = cluster.local_name(), cluster.local_url()

    def render(r):
        others = [m["name"] for m in r["members"] if m["name"] != local]
        lines = ["%s joined the cluster through %s -- now a member alongside %s"
                 % (GREEN("+"), BOLD(r["node"]["name"]), ", ".join(others) or "nobody")]
        lines.append(DIM("  this node is %s at %s" % (local, local_url)))
        if r["unreachable"]:
            lines.append(YELLOW(
                "! %s could not be told about this node; they will pick it up from "
                "another member, or run `lemondx cluster refresh` when they are back."
                % ", ".join(r["unreachable"])))
        if r["warning"]:
            lines.append(YELLOW("! %s" % r["warning"]))
        return "\n".join(lines)

    emit(args, result, render)
    return 0


def cmd_cluster_remove(args, service):
    if not confirm("Evict node %s from the cluster? It will be told to stand down, "
                   "and every other member to forget it." % args.name, args.yes):
        print(DIM("nothing evicted"))
        return 1
    rotate = None if args.rotate is None else args.rotate
    result = cluster_service(service).evict_node(args.name, rotate=rotate)

    def render(r):
        lines = ["%s evicted node %s" % (GREEN("+"), r["removed"])]
        if r["stood_down"]:
            lines.append("%s it stood down: credential, members and groups dropped "
                         "there" % GREEN("+"))
        else:
            lines.append(YELLOW("! it could not be told (%s), so it still lists the "
                                "cluster" % (r["stand_down_error"] or "unreachable")))
        for row in r["told"]:
            if not row["ok"]:
                lines.append(YELLOW("! %s still lists it: %s"
                                    % (row["node"], row["error"] or "unreachable")))
        rotated = r.get("rotated")
        if rotated:
            lines.append("%s rotated the cluster credential%s" % (
                GREEN("+"), "" if rotated["ok"]
                else ", but %s missed it and is cut off" % ", ".join(rotated["stranded"])))
        elif r["still_holds_credential"]:
            # Worth saying plainly: the credential is shared, so a node that was
            # never told to give its copy up can still call in.
            lines.append(YELLOW(
                "! %s still holds the cluster credential. Run `lemondx cluster "
                "rotate` to cut it off." % r["removed"]))
        return "\n".join(lines)

    emit(args, result, render)
    return 0


def _fabric(service):
    return cluster_service(service).fabric()


def _fabric_bridge(service):
    """This node's fabric bridge, refusing if the fabric is not on here."""
    fabric = _fabric(service)
    if not fabric.enabled():
        raise FabricError(
            "This node is not on the fabric, so an instance cannot join it. "
            "Run `lemondx fabric enable`.", 409)
    return fabric.bridge()


def _fabric_state(state):
    if not state["enabled"]:
        return YELLOW("off")
    return GREEN("on")


def cmd_fabric_status(args, service):
    status = _fabric(service).status()

    def render(s):
        lines = [
            "%s  %s" % (DIM("fabric "), _fabric_state(s)),
            "%s  %s" % (DIM("subnet "), s["subnet"] or DIM("not allocated")),
            "%s  %s" % (DIM("bridge "), "%s%s" % (
                s["bridge"], "" if s["bridge_ready"] else YELLOW(" (not created yet)"))),
            "%s  %s" % (DIM("space  "), s["prefix"]),
            "%s  %s" % (DIM("routes "), GREEN("can program") if s["privileged"]
                        else YELLOW("no privilege -- see `lemondx fabric plan`")),
        ]
        if s["routes"]:
            colour = {"ok": GREEN, "missing": YELLOW, "wrong": YELLOW, "unreachable": RED}
            lines.append("")
            lines.append(table(
                [[BOLD(r["node"]), r["subnet"], r["via"],
                  colour[r["state"]](r["state"])] for r in s["routes"]],
                ["node", "subnet", "via", "route"]))
        if s["pending"]:
            lines.append(YELLOW("  %d command(s) to run: `lemondx fabric apply`"
                                % s["pending"]))
        if s["error"]:
            lines.append(RED("  ! " + s["error"]))
        if s["check"]:
            lines.append(YELLOW("  ! " + s["check"]))
        for warning in s["warnings"]:
            lines.append(YELLOW("  ! " + warning))
        return "\n".join(lines)

    emit(args, status, render)
    return 0 if status["enabled"] and not status["check"] else 1


def cmd_fabric_helper(args, service):
    """The root half of `fabric apply`, run through sudo. Not for people."""
    from .fabrichelper import main as helper_main
    return helper_main()


def cmd_fabric_plan(args, service):
    plan = _fabric(service).plan()

    def render(p):
        if not p["commands"]:
            return "%s nothing to do; this node's routes are up to date." % GREEN("+")
        lines = ["These run on %s:" % BOLD(p["node"]), ""]
        for command in p["commands"]:
            lines.append("  %s" % DIM("# " + command["why"]))
            lines.append("  %s" % command["command"])
        if not p["privileged"]:
            lines.append("")
            lines.append(YELLOW("lemondx cannot run these itself; see docs/networking.md."))
        return "\n".join(lines)

    emit(args, plan, render)
    return 0


def cmd_fabric_apply(args, service):
    result = _fabric(service).apply()

    def render(r):
        lines = []
        for step in r["applied"]:
            mark = GREEN("+") if step["ok"] else RED("!")
            lines.append("%s %s" % (mark, step["command"]))
            if not step["ok"]:
                lines.append(RED("  %s" % step["error"]))
        if not lines:
            lines.append("%s routes already up to date." % GREEN("+"))
        if r["check"]:
            lines.append(YELLOW("! " + r["check"]))
        return "\n".join(lines)

    emit(args, result, render)
    return 0 if result["ok"] else 1


def cmd_fabric_enable(args, service):
    result = _fabric(service).enable(prefix=args.prefix)

    def render(r):
        lines = ["%s fabric on, %s split between %d node(s)"
                 % (GREEN("+"), BOLD(r["prefix"]), len(r["assigned"]))]
        for node, subnet in sorted(r["assigned"].items()):
            lines.append("  %-20s %s" % (node, subnet))
        for peer in r["peers"]:
            if not peer["ok"]:
                lines.append(RED("! %s was not told: %s" % (peer["node"], peer["error"])))
        if r["status"]["check"]:
            lines.append(YELLOW("! " + r["status"]["check"]))
        return "\n".join(lines)

    emit(args, result, render)
    return 0


def cmd_fabric_disable(args, service):
    result = _fabric(service).disable()
    emit(args, result, lambda r: "%s fabric off on %s; %d route(s) removed. The "
         "bridge %s was left alone." % (GREEN("+"), BOLD(r["node"]),
                                        len(r["removed"]), r["bridge"]))
    return 0


def cmd_fabric_attach(args, service):
    container = _fabric(service).attach(args.name)
    emit(args, container, lambda c: "%s %s is on the fabric." % (GREEN("+"), BOLD(args.name)))
    return 0


def cmd_fabric_detach(args, service):
    container = _fabric(service).detach(args.name)
    emit(args, container, lambda c: "%s %s is off the fabric." % (GREEN("+"), BOLD(args.name)))
    return 0


def cmd_cluster_groups(args, service):
    groups = cluster_service(service).list_groups()
    emit(args, groups, lambda items: table(
        [[BOLD(g["name"]), DIM("auto") if g["managed"] else "", str(len(g["members"])),
          ", ".join(GREEN(m) if m not in g["unknown_members"] else RED(m)
                    for m in g["members"]) or DIM("-"),
          g["description"] or DIM("-")] for g in items],
        ["group", "kind", "nodes", "members", "description"]))
    return 0


def cmd_cluster_group_set(args, service):
    group = cluster_service(service).save_group(
        args.name, members=args.node, description=args.description or "")
    emit(args, group, lambda g: "%s saved group %s: %s%s" % (
        GREEN("+"), BOLD(g["name"]), ", ".join(g["members"]) or "no members",
        sync_note(g)))
    return 0


def cmd_cluster_group_auto(args, service):
    result = cluster_service(service).auto_groups()

    def render(r):
        lines = [table(
            [[BOLD(n["node"]),
              str(n["cpu"]) if n["ok"] else DIM("-"),
              human_bytes(n["memory"]) if n["ok"] else DIM("-"),
              "%.2f" % n["score"] if n["ok"] else DIM("-"),
              ", ".join(n["groups"]) if n["ok"] else RED("not measured")]
             for n in r["nodes"]],
            ["node", "threads", "memory", "vs average", "groups"])]
        if r["uniform"]:
            lines.append(DIM("every node is the same size, so each is in both groups"))
        for reading in r["nodes"]:
            if not reading["ok"]:
                lines.append(YELLOW("! %s is in neither group: %s"
                                    % (reading["node"], reading["error"])))
        for group in r["groups"]:
            lines.append("%s %s: %s%s" % (GREEN("+"), BOLD(group["name"]),
                                          ", ".join(group["members"]) or "no members",
                                          sync_note(group)))
        return "\n".join(lines)

    emit(args, result, render)
    return 0


def cmd_cluster_group_delete(args, service):
    result = cluster_service(service).delete_group(
        args.name, everywhere=not args.local_only)
    emit(args, result, lambda r: "%s deleted group %s%s" % (
        GREEN("+"), r["deleted"], sync_note(r)))
    return 0


def cmd_cluster_sync(args, service):
    result = cluster_service(service).sync(
        kinds=args.kind or ["templates"], names=args.name or None,
        nodes=args.node or None, groups=args.group or None)

    def render(r):
        lines = []
        for row in r["results"]:
            lines.append("%s %s %s %s%s" % (
                GREEN("+") if row["ok"] else RED("!"), BOLD(row["node"]),
                DIM(row["kind"][:-1] if row["kind"].endswith("s") else row["kind"]),
                row["name"], "" if row["ok"] else ": " + (row["error"] or "failed")))
        lines.append("")
        lines.append("%d item(s) to %d node(s)" % (r["items"], len(r["nodes"])))
        return "\n".join(lines)

    emit(args, result, render)
    return 0 if result["ok"] else 1


def cmd_cluster_containers(args, service):
    # No flags means the whole cluster: a command called `cluster containers`
    # that showed only this host would have nothing to do with a cluster.
    result = cluster_service(service).containers(
        nodes=args.node or None, groups=args.group or None,
        everything=not (args.node or args.group))

    def render(r):
        health = {(h["node"], h["name"]): h for h in r.get("health") or []}

        def judged(c):
            record = health.get((c["node"], c["name"])) if c["status"] == "Running" else None
            if not record:
                return DIM("-"), DIM("-")
            app = record.get("app")
            return (HEALTH_COLORS.get(record["status"], str)(record["status"]),
                    APP_COLORS.get(app["status"], str)(app["status"]) if app else DIM("-"))

        out = [table([[BOLD(c["node"]), c["name"],
                       STATUS_COLORS.get(c["status"], str)(c["status"]),
                       *judged(c),
                       ", ".join(c.get("ipv4") or []) or DIM("-"),
                       c.get("template") or DIM("-")]
                      for c in r["instances"]],
                     ["node", "name", "status", "health", "app", "ipv4", "template"])]
        for failure in r["errors"]:
            out.append(RED("! %s: %s" % (failure["node"], failure["error"])))
        return "\n".join(out)

    emit(args, result, render)
    return 0 if not result["errors"] else 1


def cmd_cluster_cert(args, _service):
    host = args.host or cluster_mod.guess_local_address() or "localhost"
    made = cluster_mod.generate_certificate(host, days=args.days)

    def render(m):
        return "\n".join([
            "%s wrote a self-signed certificate for %s" % (GREEN("+"), BOLD(m["host"])),
            DIM("  %s" % m["cert"]),
            DIM("  %s" % m["key"]),
            "  fingerprint %s" % cluster_mod.pretty_fingerprint(m["fingerprint"]),
            "",
            DIM("Run `lemondx configure cluster` to use it, or serve with "
                "--tls-cert/--tls-key."),
        ])

    emit(args, made, render)
    return 0


def cmd_cluster_fingerprint(args, _service):
    result = ClusterService(None, None).probe_fingerprint(args.url)
    emit(args, result, lambda r: "%s presents %s" % (r["url"], r["fingerprint_pretty"]))
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

    # Templates, modules, profiles and node groups are kept level across a
    # cluster, so removing one removes it everywhere unless this says otherwise.
    only_here = argparse.ArgumentParser(add_help=False)
    only_here.add_argument("--local-only", action="store_true",
                           help="delete only on this node, leaving other members' "
                                "copies in place")

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
    p.add_argument("--dev", action="store_true", help="allow cross-origin Vite dev server")
    p.add_argument("--open", action="store_true", help="open a browser window")
    p.add_argument("--quiet", action="store_true", help="do not log requests")
    p.add_argument("--no-health", action="store_true",
                   help="do not run health checks (see `lemondx configure health`)")
    g = p.add_argument_group(
        "authentication",
        "Off unless configured. Settings saved by `lemondx configure auth` apply first; "
        "each flag below overrides its setting.")
    g.add_argument("--ignore-config", action="store_true",
                   help="ignore settings saved by `lemondx configure`")
    g.add_argument("--auth", action="append", choices=METHODS, metavar="METHOD",
                   help="login method: %s (repeatable; order is the order passwords "
                        "are tried; replaces the saved methods)" % ", ".join(METHODS))
    g.add_argument("--token",
                   help="also accept this static admin token (visible in `ps`; "
                        "prefer --token-file or LEMONDX_TOKEN)")
    g.add_argument("--token-file", metavar="PATH", help="read the static admin token from a file")
    g.add_argument("--session-hours", type=float,
                   help="how long a login lasts (default: 12)")
    g.add_argument("--allow-insecure-login", action="store_true", default=None,
                   help="accept passwords over plain HTTP from other hosts")
    g.add_argument("--pam-service",
                   help="PAM service name, i.e. /etc/pam.d/<name> (default: lemondx)")
    g.add_argument("--pam-admin-group", action="append", metavar="GROUP",
                   help="members get admin (repeatable; default: lxd / incus-admin)")
    g.add_argument("--pam-read-group", action="append", metavar="GROUP",
                   help="members get read-only access (repeatable)")
    g.add_argument("--trust-proxy", action="append", metavar="CIDR",
                   help="reverse proxy address(es) whose identity headers are believed")
    g.add_argument("--proxy-user-header",
                   help="header carrying the user name (default: X-Forwarded-User)")
    g.add_argument("--proxy-groups-header", metavar="HEADER",
                   help="header carrying the user's groups, comma separated")
    g.add_argument("--proxy-admin-group", metavar="GROUP",
                   help="with --proxy-groups-header: group granted admin")
    g.add_argument("--proxy-read-group", metavar="GROUP",
                   help="with --proxy-groups-header: group granted read-only access")
    g = p.add_argument_group("TLS")
    g.add_argument("--tls-cert", metavar="PATH", help="serve HTTPS with this certificate (PEM)")
    g.add_argument("--tls-key", metavar="PATH", help="private key for --tls-cert (PEM)")
    g.add_argument("--no-tls", action="store_true",
                   help="serve plain HTTP even though `configure cluster` saved a "
                        "certificate, e.g. behind a proxy that terminates TLS")
    p.set_defaults(func=cmd_serve, needs_service=False)

    p = add("configure", help="interactively save settings for future launches")
    p.add_argument("section", nargs="?", choices=list(SECTIONS), metavar="SECTION",
                   help="what to configure: %s (omit to list)" % ", ".join(SECTIONS))
    p.add_argument("--show", action="store_true", help="print the saved settings")
    p.add_argument("--reset", action="store_true", help="delete the saved settings")
    p.add_argument("-y", "--yes", action="store_true", help="with --reset: do not ask")
    p.set_defaults(func=cmd_configure, needs_service=False)

    p = add("tokens", help="list API tokens")
    p.set_defaults(func=cmd_tokens, needs_service=False)

    p = add("token-create", help="create an API token (shown once)")
    p.add_argument("name")
    p.add_argument("--role", choices=ROLES, default="admin", help="default: admin")
    p.add_argument("--expires", metavar="DURATION", help="e.g. 12h, 30d, 2w (default: never)")
    p.add_argument("--owner", help="user the token acts as (default: you)")
    p.set_defaults(func=cmd_token_create, needs_service=False)

    p = add("token-revoke", help="revoke an API token")
    p.add_argument("id")
    p.set_defaults(func=cmd_token_revoke, needs_service=False)

    p = add("users", help="list local lemondx users (--auth local)")
    p.set_defaults(func=cmd_users, needs_service=False)

    p = add("user-add", help="add a local user; asks for the password")
    p.add_argument("name")
    p.add_argument("--role", choices=ROLES, default="read", help="default: read")
    p.set_defaults(func=cmd_user_add, needs_service=False)

    p = add("user-set", help="change a local user's password or role")
    p.add_argument("name")
    p.add_argument("--password", action="store_true", help="ask for a new password")
    p.add_argument("--role", choices=ROLES)
    p.set_defaults(func=cmd_user_set, needs_service=False)

    p = add("user-remove", help="remove a local user and end their sessions")
    p.add_argument("name")
    p.set_defaults(func=cmd_user_remove, needs_service=False)

    p = add("pam-test", help="check a PAM login the way `serve --auth pam` would")
    p.add_argument("user")
    p.add_argument("--service", help="PAM service name (default: saved setting, or lemondx)")
    p.add_argument("--pam-admin-group", action="append", metavar="GROUP")
    p.add_argument("--pam-read-group", action="append", metavar="GROUP")
    p.set_defaults(func=cmd_pam_test, needs_service=False)

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

    p = add("health", help="check running instances now: responsiveness, CPU, memory, load")
    p.add_argument("name", nargs="*", help="only these instances (default: all running)")
    p.add_argument("--window", type=float, default=5,
                   help="seconds between the two CPU samples (default: 5)")
    p.set_defaults(func=cmd_health)

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

    network = add("network", help="view and manage networks")
    network_sub = network.add_subparsers(dest="network_command", metavar="<network-command>")

    p = network_sub.add_parser("list", aliases=["ls"], parents=[common],
                               help="list networks and host interfaces")
    p.set_defaults(func=cmd_networks)

    p = network_sub.add_parser("subnets", parents=[common],
                               help="subnets already on the host, to pick a free block")
    p.set_defaults(func=cmd_network_subnets)

    p = network_sub.add_parser("show", parents=[common], help="show one network")
    p.add_argument("name")
    p.set_defaults(func=cmd_network_show)

    # Shared by `network create` and `network set`.
    bridge = argparse.ArgumentParser(add_help=False)
    bridge.add_argument("--ipv4", metavar="ADDR",
                        help="CIDR block, e.g. 10.10.0.0/24 (bridge takes .1), or auto/none")
    bridge.add_argument("--ipv6", metavar="ADDR",
                        help="CIDR block, e.g. fd42:1::/64 (bridge takes ::1), or auto/none")
    bridge.add_argument("--nat", dest="nat", action="store_true", default=None,
                        help="NAT outbound traffic behind the host")
    bridge.add_argument("--no-nat", dest="nat", action="store_false",
                        help="route instead of NAT (needs a route on your network)")
    bridge.add_argument("--dns-domain", help="DNS domain for instances on the bridge")
    bridge.add_argument("--mtu", help="bridge MTU")
    bridge.add_argument("--description")
    bridge.add_argument("--config", action="append", metavar="KEY=VALUE",
                        help="allowlisted bridge option (repeatable; empty value unsets)")

    p = network_sub.add_parser("create", parents=[common, bridge],
                               help="create a managed bridge")
    p.add_argument("name")
    p.set_defaults(func=cmd_network_create)

    p = network_sub.add_parser("set", parents=[common, bridge],
                               help="change a managed bridge")
    p.add_argument("name")
    p.set_defaults(func=cmd_network_set)

    p = network_sub.add_parser("delete", aliases=["rm"], parents=[common],
                               help="delete a managed bridge nothing uses")
    p.add_argument("name")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_network_delete)

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
    spec.add_argument("--network", help="network for eth0 (default: the default profile's)")
    spec.add_argument("--fabric", action="store_true",
                      help="also give it a NIC on the fabric, so it can reach "
                           "instances on other nodes")
    spec.add_argument("--profile", action="append", help="profile to apply (repeatable)")
    spec.add_argument("--vm", action="store_true", help="create a virtual machine instead")
    spec.add_argument("--no-secureboot", action="store_true",
                      help="boot a VM without UEFI secure boot, for images that "
                           "are incompatible with it")
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

    p = add("module-remove", parents=[common, only_here],
            help="remove an uploaded module")
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

    p = add("profile-delete", parents=[common, only_here],
            help="delete a bootstrap profile")
    p.add_argument("name")
    p.set_defaults(func=cmd_profile_delete)

    p = add("templates", help="list saved instance templates")
    p.set_defaults(func=cmd_templates)

    p = add("template-save", parents=[common, spec, boot],
            help="save an instance spec and bootstrap selection as a template")
    p.add_argument("name")
    p.add_argument("--description", default="", help="what the template is for")
    p.add_argument("--prefix", help="instance name prefix (default: from the name)")
    p.add_argument("--app-check", metavar="SCRIPT",
                   help="a script (file, or - for stdin) run in each instance as a health "
                        "check: exit 0 ok, 1 warning, 2 critical, 3 unknown")
    p.add_argument("--app-check-interval", type=int, metavar="SECONDS",
                   help="how often the app check runs (default: 60)")
    p.add_argument("--app-check-timeout", type=int, metavar="SECONDS",
                   help="stop the app check after this long; counts as critical (default: 10)")
    p.add_argument("--no-app-check", action="store_true",
                   help="remove the template's app check")
    p.set_defaults(func=cmd_template_save)

    p = add("template-delete", parents=[common, only_here],
            help="delete an instance template")
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

    p = add("stacks", help="list saved stacks (templates launched in sequence)")
    p.set_defaults(func=cmd_stacks)

    p = add("stack-show", help="print a stack's definition as JSON, as stack-save takes it")
    p.add_argument("name")
    p.set_defaults(func=cmd_stack_show)

    p = add("stack-save", help="save a stack from a JSON definition")
    p.add_argument("name")
    p.add_argument("file", help="the definition (a file, or - for stdin); see docs/stacks.md")
    p.add_argument("--description", help="what the stack is for (default: from the file)")
    p.set_defaults(func=cmd_stack_save)

    p = add("stack-delete", parents=[common, only_here], help="delete a stack")
    p.add_argument("name")
    p.set_defaults(func=cmd_stack_delete)

    p = add("stack-launch", help="run a stack, stage by stage, and wait for it")
    p.add_argument("name")
    p.add_argument("--param", action="append", metavar="KEY=VALUE",
                   help="supply a secret, or a value every launch gets (repeatable)")
    p.add_argument("--relaunch", action="store_true",
                   help="destroy the stack's current instances first (asks unless -y)")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_stack_launch)

    for action, text in (("start", "start every instance a stack launched"),
                         ("stop", "stop every instance a stack launched"),
                         ("restart", "restart every instance a stack launched")):
        p = add("stack-%s" % action, help=text)
        p.add_argument("name")
        p.set_defaults(func=cmd_stack_state, action=action)

    p = add("stack-destroy", help="stop and delete every instance a stack launched")
    p.add_argument("name")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_stack_destroy)

    # -- cluster -----------------------------------------------------------
    cluster_p = add("cluster", help="federate with other lemondx nodes")
    cluster_sub = cluster_p.add_subparsers(dest="cluster_command", metavar="<command>")
    cluster_p.set_defaults(func=cmd_cluster_status, no_probe=False)

    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument("--no-probe", action="store_true",
                       help="do not contact the nodes, just list what is registered")

    p = cluster_sub.add_parser("status", parents=[common, probe],
                               help="this node and the cluster it is part of")
    p.set_defaults(func=cmd_cluster_status)

    p = cluster_sub.add_parser("nodes", aliases=["ls"], parents=[common, probe],
                               help="list the cluster's nodes")
    p.set_defaults(func=cmd_cluster_nodes)

    p = cluster_sub.add_parser("show", parents=[common],
                               help="one node, with the instances on it")
    p.add_argument("name")
    p.set_defaults(func=cmd_cluster_show)

    p = cluster_sub.add_parser("invite", parents=[common],
                               help="print a one-time code for a node to join this "
                                    "cluster with (starts one if there is none)")
    p.add_argument("--expires", type=float, default=cluster_mod.DEFAULT_INVITE_MINUTES,
                   metavar="MINUTES", help="how long the code stays valid (default: %d)"
                   % cluster_mod.DEFAULT_INVITE_MINUTES)
    p.add_argument("--note", help="a reminder of who the code was for")
    p.set_defaults(func=cmd_cluster_invite)

    p = cluster_sub.add_parser("invites", parents=[common],
                               help="join codes issued here that nobody has redeemed")
    p.set_defaults(func=cmd_cluster_invites)

    p = cluster_sub.add_parser("revoke-invite", parents=[common],
                               help="withdraw an unredeemed join code")
    p.add_argument("id")
    p.set_defaults(func=cmd_cluster_revoke_invite)

    p = cluster_sub.add_parser("join", parents=[common],
                               help="join a cluster with a code from any of its nodes")
    p.add_argument("code", help="the code `lemondx cluster invite` printed there, "
                                "or - to read it from stdin")
    p.add_argument("--description", help="a note about this node")
    p.set_defaults(func=cmd_cluster_join)

    p = cluster_sub.add_parser("evict", aliases=["remove", "rm"], parents=[common],
                               help="put a node out of the cluster: it stands down, "
                                    "every member forgets it")
    p.add_argument("name")
    # Three states, not two: by default the credential is rotated only when the
    # node could not be told to give its own copy up. See evict_node().
    p.add_argument("--rotate", dest="rotate", action="store_true", default=None,
                   help="replace the cluster credential even if the node stood down")
    p.add_argument("--no-rotate", dest="rotate", action="store_false",
                   help="leave the credential alone even if the node was not reached")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_cluster_remove)

    p = cluster_sub.add_parser("refresh", parents=[common],
                               help="level the member list with every other node")
    p.set_defaults(func=cmd_cluster_refresh)

    p = cluster_sub.add_parser("reconcile", parents=[common],
                               help="settle this node's templates, modules, profiles, "
                                    "stacks and groups against every member's")
    p.add_argument("--dry-run", action="store_true",
                   help="report what differs without changing anything")
    p.set_defaults(func=cmd_cluster_reconcile)

    p = cluster_sub.add_parser("leave", parents=[common],
                               help="give up membership: every member is told to "
                                    "forget this node")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_cluster_leave)

    p = cluster_sub.add_parser("rotate", parents=[common],
                               help="replace the cluster credential on every member")
    p.set_defaults(func=cmd_cluster_rotate)

    # Top level and hidden from help: sudo runs this, people do not. It is the
    # one command the shipped sudoers rules name, so its spelling is part of
    # that file -- keep the two in step.
    p = sub.add_parser("fabric-helper")
    p.set_defaults(func=cmd_fabric_helper)

    fabric_p = sub.add_parser(
        "fabric", help="routed, non-NAT networking between the containers of a cluster")
    fabric_sub = fabric_p.add_subparsers(dest="fabric_command", metavar="<command>")

    p = fabric_sub.add_parser("status", parents=[common],
                              help="this node's subnet, bridge and routes")
    p.set_defaults(func=cmd_fabric_status)

    p = fabric_sub.add_parser("plan", parents=[common],
                              help="the host commands that would bring routes up to date")
    p.set_defaults(func=cmd_fabric_plan)

    p = fabric_sub.add_parser("apply", parents=[common],
                              help="create the bridge and program the routes")
    p.set_defaults(func=cmd_fabric_apply)

    p = fabric_sub.add_parser("enable", parents=[common],
                              help="allocate a subnet to every member and turn the fabric on")
    p.add_argument("--prefix", metavar="CIDR",
                   help="address space for the cluster, a /16 (default: 10.100.0.0/16)")
    p.set_defaults(func=cmd_fabric_enable)

    p = fabric_sub.add_parser("disable", parents=[common],
                              help="stop routing here and give up this node's subnet")
    p.set_defaults(func=cmd_fabric_disable)

    p = fabric_sub.add_parser("attach", parents=[common],
                              help="give an existing instance a NIC on the fabric")
    p.add_argument("name")
    p.set_defaults(func=cmd_fabric_attach)

    p = fabric_sub.add_parser("detach", parents=[common],
                              help="take an instance's fabric NIC away")
    p.add_argument("name")
    p.set_defaults(func=cmd_fabric_detach)

    p = cluster_sub.add_parser("groups", parents=[common], help="list node groups")
    p.set_defaults(func=cmd_cluster_groups)

    group_p = cluster_sub.add_parser("group", help="manage a node group")
    group_sub = group_p.add_subparsers(dest="group_command", metavar="<command>")

    p = group_sub.add_parser("set", parents=[common],
                             help="create a group or replace its members")
    p.add_argument("name")
    p.add_argument("--node", action="append", metavar="NAME",
                   help="a member (repeatable; replaces the current members)")
    p.add_argument("--description")
    p.set_defaults(func=cmd_cluster_group_set)

    p = group_sub.add_parser("auto", parents=[common],
                             help="rebuild the large and small groups from each "
                                  "node's CPU and memory")
    p.set_defaults(func=cmd_cluster_group_auto)

    p = group_sub.add_parser("delete", aliases=["rm"], parents=[common, only_here],
                             help="delete a group; its nodes stay")
    p.add_argument("name")
    p.set_defaults(func=cmd_cluster_group_delete)

    p = cluster_sub.add_parser("sync", parents=[common],
                               help="copy templates, modules or profiles to other nodes")
    p.add_argument("--kind", action="append", choices=list(cluster_mod.SYNC_KINDS),
                   help="what to copy (repeatable; default: templates)")
    p.add_argument("--name", action="append", metavar="NAME",
                   help="only this template/module/profile (repeatable; default: all)")
    p.add_argument("--node", action="append", metavar="NAME", help="send to this node")
    p.add_argument("--group", action="append", metavar="NAME", help="send to this group")
    p.set_defaults(func=cmd_cluster_sync)

    p = cluster_sub.add_parser("containers", aliases=["instances"], parents=[common],
                               help="every instance across the chosen nodes")
    p.add_argument("--node", action="append", metavar="NAME")
    p.add_argument("--group", action="append", metavar="NAME")
    p.set_defaults(func=cmd_cluster_containers)

    p = cluster_sub.add_parser("cert", parents=[common],
                               help="write a self-signed TLS certificate for this node")
    p.add_argument("--host", help="address peers will use (default: this host's)")
    p.add_argument("--days", type=int, default=3650)
    p.set_defaults(func=cmd_cluster_cert, needs_service=False)

    p = cluster_sub.add_parser("fingerprint", parents=[common],
                               help="show the certificate an address presents right now")
    p.add_argument("url")
    p.set_defaults(func=cmd_cluster_fingerprint, needs_service=False)

    p = add("launch", help="create one or more instances from a template")
    p.add_argument("template")
    p.add_argument("-n", "--count", type=int, default=1,
                   help="how many instances (default: 1)")
    p.add_argument("--prefix", help="name them <prefix>-N instead of the template's")
    p.add_argument("--param", action="append", metavar="KEY=VALUE",
                   help="override a saved parameter or supply a secret (repeatable)")
    p.add_argument("--node", action="append", metavar="NAME",
                   help="launch on this node instead of here (repeatable; see "
                        "`lemondx cluster nodes`)")
    p.add_argument("--group", action="append", metavar="NAME",
                   help="launch across every node in this group (repeatable)")
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
    except (ServiceError, LXDError, AuthError, ConfigureError, ClusterError,
            NodeError, FabricError, HostNetError) as exc:
        print(RED("! %s" % exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

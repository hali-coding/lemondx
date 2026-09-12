"""Command line interface for lemondx."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from .lxd import LXDError
from .service import ContainerService, ServiceError
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
        pool = service.root_pool_info(args.profile)
        if pool and not pool["supports_quota"]:
            print(YELLOW(
                "! Pool '%s' uses the %s driver, which cannot enforce a disk "
                "size unless the filesystem has project quotas enabled. The "
                "value will be recorded but not applied."
                % (pool["name"], pool["driver"])), file=sys.stderr)
    if not args.json:
        print(DIM("Creating %s from %s (this pulls the image on first use)..."
                  % (args.name, image)), flush=True)
    container = service.create_container(
        name=args.name, image=image,
        instance_type="virtual-machine" if args.vm else "container",
        profiles=args.profile or None, cpu=args.cpu, memory=args.memory,
        disk=args.disk, description=args.description, ephemeral=args.ephemeral,
        start=not args.no_start,
    )
    emit(args, container, lambda c: "%s %s is %s%s" % (
        GREEN("+"), BOLD(c["name"]), c["status"].lower(),
        (" at " + ", ".join(c["ipv4"])) if c["ipv4"] else ""))
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

    p = add("list", aliases=["ls"], help="list containers")
    p.add_argument("--running", action="store_true", help="only running containers")
    p.set_defaults(func=cmd_list)

    p = add("info", help="show one container in detail")
    p.add_argument("name")
    p.set_defaults(func=cmd_info)

    p = add("create", help="create a container")
    p.add_argument("name")
    p.add_argument("-i", "--image",
                   help="image alias, e.g. ubuntu:24.04 or images:debian/12 "
                        "(default: latest Ubuntu LTS for this daemon)")
    p.add_argument("-c", "--cpu", help="CPU limit, e.g. 2")
    p.add_argument("-m", "--memory", help="memory limit, e.g. 2GiB")
    p.add_argument("-d", "--disk", help="root disk size, e.g. 10GiB")
    p.add_argument("--description", help="free-text description")
    p.add_argument("--profile", action="append", help="profile to apply (repeatable)")
    p.add_argument("--vm", action="store_true", help="create a virtual machine instead")
    p.add_argument("--ephemeral", action="store_true", help="delete on stop")
    p.add_argument("--no-start", action="store_true", help="create without starting")
    p.set_defaults(func=cmd_create)

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

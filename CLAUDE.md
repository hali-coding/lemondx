# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

lemondx is a web UI and CLI for local LXD/Incus containers. `README.md` is the user-facing
reference (CLI commands, REST endpoints, module authoring, socket discovery) and is kept
current — read it for behaviour; this file covers how the code is put together.

## Commands

```bash
./lemondx serve --open              # run API + committed UI on :8099
./lemondx serve --dev               # same, with CORS open for the Vite dev server
npm --prefix web install            # once, only if changing the frontend
npm --prefix web run dev            # Vite on :5173, proxies /api to :8099
npm --prefix web run build          # tsc -b && vite build -> web/dist (commit the result)
npm --prefix web run lint           # oxlint
python3 -m compileall -q src/lemondx # syntax check; there is no Python linter configured
sh -n modules/foo.sh                # modules must parse as POSIX sh
LEMONDX_FLAVOR=incus ./lemondx status  # force the other daemon
./systemd/install-service.sh        # install as a systemd --user unit (see systemd/)
```

There is **no test suite** and no test runner configured. Verify changes by running the CLI
or the server against a live daemon. `./lemondx status` reports which socket and flavor it
resolved; `--json` on any CLI command prints the same payload the REST API returns.

## Hard constraints

- **The backend is stdlib-only, and Python 3.9+.** `pyproject.toml` declares no
  dependencies by design — do not add any, and do not use syntax newer than 3.9
  (every module starts with `from __future__ import annotations`, so annotations
  themselves are free).
- **`web/dist` is committed** so a clone runs without Node. Any change under `web/src`
  must be followed by `npm --prefix web run build` and the rebuilt `dist` committed in the
  same change.
- **Node is not required to run lemondx**, only to change the UI. Nothing in the Python
  side may depend on a build step having happened.

## Architecture

Four layers, one direction of dependency:

```
lxd.py          transport: HTTP over the daemon's AF_UNIX socket, no shelling out
  ↓
service.py      domain: ContainerService turns raw daemon records into lemondx shapes
  ↓
server.py       JSON API + static hosting      cli.py   argparse front end
```

`ContainerService` (`src/lemondx/service.py`) is the only place domain logic lives, so the
API and the CLI cannot drift. **Adding a feature means: a method on `ContainerService`,
then a route in `build_router()` (`server.py:60`) and a subcommand in `build_parser()`
(`cli.py:551`).** A route handler is a one-line lambda that unpacks the JSON body and calls
the service; a CLI command calls the same method and passes the result to `emit()` with a
human renderer. Because `emit()` prints the service payload verbatim under `--json`, the
service must return exactly what the API should serve — never reshape data in a route or a
command.

Supporting modules: `bootstrap.py` (module discovery, validation, SSH-key parsing, the
runner), `simplestreams.py` (remote image catalogs, 15-minute in-process cache),
`store.py` (atomic JSON state under `~/.local/share/lemondx`).

### Errors and the response envelope

Every layer raises an exception carrying an HTTP-ish `.code`: `LXDError`, `ServiceError`,
`BootstrapError`. `_handle_api()` turns those into `{"error": msg}` with that status and
everything else into a generic 500 — tracebacks never reach a client. Success is always
`{"data": ...}`. `cli.main()` catches the same exception types and exits 1. So raising the
right exception with the right code is all that either front end needs.

### Two daemons

LXD and Incus share the `/1.0` API but differ in socket path, admin group, CLI binary and
known image remotes. Every such difference is a flavor-keyed table at the top of `lxd.py`
(`SOCKET_CANDIDATES`, `REMOTES_BY_FLAVOR`, `CLIENT_BINARY`, `ADMIN_GROUP`) or in
`service.py` (`IMAGE_CATALOG_LXD` / `IMAGE_CATALOG_INCUS`, `BROWSABLE_REMOTES`). Add to
those tables rather than branching on flavor in logic. Incus support is written from its
published API and has not been exercised against a live daemon.

### Exec has no websocket

`LXDClient.exec_command()` uses the daemon's `record-output` mode, reads the resulting log
files over HTTP and deletes them, which avoids a websocket client entirely. It deliberately
recovers output from *failed* operations, because the daemon reports some non-zero exits
(127 among them) as operation failures even though the record holds the real exit code. A
command that ran and failed is a result, not an API error. The consequence is that there is
no TTY and no streaming anywhere — the browser console is non-interactive, and
`lemondx shell` hands off to the real `lxc`/`incus` binary instead.

### Bootstrap modules

A module is a POSIX shell script with a `# key: value` metadata header (`name`,
`description`, `order`, `uses`, `param`, `secret`). `BootstrapRunner` sorts by `order`, prepends
`modules/_prelude.sh` to each script, pushes it to `/tmp` in the container, runs it with
declared params as environment variables, and deletes it. Modules run under `/bin/sh`
(dash, busybox ash) because minimal images often have no bash — **keep them POSIX**, and
put anything distro-specific behind a prelude helper (`pkg_install`, `svc_enable`,
`install_ssh_keys`) rather than in the module.

`secret:` declares a param that must never persist. Params carry `"secret": True`, and
every path that stores or reports a value checks it: `effective_params()` ignores a saved
one, `_remember_params()` and `update_module_settings()` skip or reject it, profiles strip
it on save and on list, the runner refuses to start when one is empty and passes every
module result through `redact()`, and the CLI takes it from an env var of the same name or
`getpass` rather than argv. A new place that persists or echoes params needs the same check.

Search order is built-ins → `~/.config/lemondx/modules` → `$LEMONDX_MODULES`, later wins,
so an uploaded module shadows a shipped one and built-ins can never be deleted. Uploads are
validated before storage (safe id, UTF-8 under 256 KiB, `sh -n` parse) and never executed
at upload time. Bootstrap probes outbound connectivity first so a blocked bridge fails
immediately with an explanation instead of as a package-manager timeout.

SSH keys: only `*.pub` files are ever opened, keys are validated by `parse_public_key()`
(known type, no `PRIVATE KEY`, no embedded newlines or control characters) before they go
anywhere, and reach the container as `LEMONDX_SSH_KEYS`.

### Running as a service

`systemd/` holds unit templates and `install-service.sh`. The user unit (`lemondx.service`) is the
one to reach for -- it runs as the invoking user, which is what lemondx's own design assumes
everywhere else: daemon-group membership, `~/.ssh/*.pub` for the bootstrap key picker, and
`store.py`'s data directory are all *that user's*. The system unit (`lemondx-system.service`) runs
as a dedicated account instead, which is why it needs `@LEMONDX_GROUP@` filled in by the installer
(a group that does not exist on the host would make systemd refuse the whole unit) and documents
that the account's `~/.ssh` starts empty. Both run under `ProtectSystem=strict`/`ProtectHome=read-only`
with only the data directory carved out via `ReadWritePaths=`, since nothing else in the codebase
writes to the filesystem outside it (bootstrap pushes files to the container over the API, not to
local disk).

### Persistent state

`store.py` owns everything lemondx keeps between runs, under
`~/.local/share/lemondx` (`LEMONDX_DATA_DIR`, or `XDG_DATA_HOME`, moves it;
`LEMONDX_CONFIG_DIR` is still honoured as the name it had before the move):
`settings.json` for default modules and remembered params, `profiles/` for one
JSON file per bootstrap profile, `modules/` for uploads. Nothing else in the
codebase builds those paths — go through `store.py` so migration and atomic
writes apply.

Profiles are one file each so they can be copied between machines or checked
into a project, which means **every profile file is untrusted input**: the name
in the file is authoritative, the filename is only `profile_slug()` of it (two
names that slug alike get `-2`, `-3` suffixes), and `_clean_profile()`
normalises every record on read. A file that no longer parses is skipped rather
than failing the listing.

`_migrate()` runs once per process, is best-effort (an `OSError` must never
stop lemondx from starting) and handles both older layouts: adopting
`~/.config/lemondx` wholesale, and splitting a `profiles` key out of an older
`settings.json`. Bump `SETTINGS_VERSION` and add a step there for the next
layout change.

### Frontend

Vite + React 19 + TypeScript, no UI framework or state library; plain CSS in
`web/src/index.css` with theme variables. `web/src/lib/types.ts` hand-mirrors the payload
shapes `service.py` returns — **change a service payload and update it in the same
change.** `web/src/lib/api.ts` is the only place `fetch` is called; it throws `ApiError`
with the status so `App.tsx` can treat 401 as "prompt for token" (`TokenGate`, kept in
`sessionStorage` for that tab only).

`App.tsx` polls `/api/status` and `/api/containers` every 3s and holds a `mutating` ref
that pauses polling while a mutation is in flight, so a poll cannot clobber optimistic
state. Long operations (create, bootstrap) block on the daemon and show a spinner.

## Style

Comments explain *why*, not what — the existing ones are the model: they record the
trade-off or the daemon quirk that forced the code's shape. Keep that density rather than
annotating obvious lines. Commit messages are a short imperative subject followed by
prose paragraphs explaining the reasoning, wrapped at ~72 columns.

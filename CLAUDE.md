# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

lemondx is a web UI and CLI for local LXD/Incus containers. `README.md` and `docs/*.md`
are the user-facing reference (CLI commands, REST endpoints, module authoring, socket
discovery) and are kept current — read them for behaviour; this file covers how the code
is put together. README.md is deliberately short: it links out to docs/ for anything more
than a paragraph, so update the doc a change actually affects rather than growing the
README back -- see `docs/` for the current split (modules, REST API, daemons/storage,
web UI, service, security, frontend dev).

## Commands

```bash
./lemondx serve --open              # run API + built UI on :8099
./lemondx serve --dev               # same, with CORS open for the Vite dev server
./build.sh                          # check the Node toolchain, install, build -> web/dist
npm --prefix web run dev            # Vite on :5173, proxies /api to :8099
npm --prefix web run build          # the build step alone (web/dist is not committed)
.github/scripts/bundle.sh 1.2.3     # the release archive, exactly as CI builds it
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
- **`web/dist` is a build output, not source**, and is gitignored. The release workflow
  builds it and ships it inside the release archive, which is how a user gets a runnable
  tree; a clone runs `./build.sh` once. Nothing in the Python side may
  depend on that build having happened -- `serve` prints a note and still serves the API
  when `web/dist` is absent.
- **Node is not required to run a release**, only to build from a clone or change the UI.
- **Commit subjects are conventional commits** (`feat:`, `fix:`, `feat!:` ...): release-please
  derives the next semantic version from them, so the subject line is a release decision.
  The PR workflow rejects a PR title that does not parse. See docs/development.md.

## Architecture

Four layers, one direction of dependency:

```
lxd.py          transport: HTTP over the daemon's AF_UNIX socket, no shelling out
  ↓
service.py      domain: ContainerService turns raw daemon records into lemondx shapes
  ↓
server.py       JSON API + static hosting      cli.py   argparse front end
```

`auth.py` (`AuthService`) sits beside `service.py` with the same role for
authentication: server and CLI both call it, so neither decides alone what a
token or user is.

`ContainerService` (`src/lemondx/service.py`) is the only place domain logic lives, so the
API and the CLI cannot drift. **Adding a feature means: a method on `ContainerService`,
then a route in `build_router()` (`server.py:83`) and a subcommand in `build_parser()`
(`cli.py:1249`).** A route's role defaults to `read` for `GET` and `admin` otherwise;
pass `role=` to `Router.add()` only where that is wrong, and `principal=True` when
the answer depends on the caller. A route handler is a one-line lambda that unpacks the JSON body and calls
the service; a CLI command calls the same method and passes the result to `emit()` with a
human renderer. Because `emit()` prints the service payload verbatim under `--json`, the
service must return exactly what the API should serve — never reshape data in a route or a
command.

Supporting modules: `bootstrap.py` (module discovery, validation, SSH-key parsing, the
runner), `simplestreams.py` (remote image catalogs, 15-minute in-process cache),
`store.py` (atomic JSON state under `~/.local/share/lemondx`).

### Errors and the response envelope

Every layer raises an exception carrying an HTTP-ish `.code`: `LXDError`, `ServiceError`,
`BootstrapError`, `AuthError`. `_handle_api()` turns those into `{"error": msg}` with that status and
everything else into a generic 500 — tracebacks never reach a client. Success is always
`{"data": ...}`. `cli.main()` catches the same exception types and exits 1. So raising the
right exception with the right code is all that either front end needs.

### Authentication

Opt-in: with no `--auth` method and no static token, `AuthConfig.enabled` is false
and every request is an anonymous admin. `LemondxHandler._principal()` resolves the
caller in a fixed order -- token header, `?token=` (WebSockets cannot send headers),
session cookie, trusted-proxy header -- and reports whether the credential was
*explicit*. Ambient credentials (cookies, proxy identity, or none at all) must pass
`_same_origin()` on anything but `GET`, and on every WebSocket upgrade; that check is
what stops a page on another site driving the API through the user's browser, so a
new request path must not bypass it. With auth off and a loopback bind,
`_host_allowed()` also refuses non-loopback `Host` headers against DNS rebinding.

Users and token hashes live in `store.py`'s `auth/` directory. `AuthService` re-reads
those files when their inode/mtime fingerprint changes, which is how a CLI revoke or
password change reaches a running server without IPC -- keep auth state on disk, not
only in the process. Sessions are the exception (in memory; a restart logs out).
`pam.py` calls libpam through ctypes; non-root it can only verify the service's own
account, and `NoNewPrivileges=yes` breaks it -- `pam.diagnose()` says so at startup.
Never log a credential: `log_message()` masks `token=` and startup prints no token.

`lemondx configure <section>` (`configure.py`) saves settings to
`store.config_path(section)` for later launches. A section is a `Section` subclass
decorated `@register`; the CLI builds its choices from `SECTIONS`, so a new section
needs no parser change. Sections own prompts and presentation only -- validation
lives with the consumer (`auth.clean_settings()`), so a hand-edited file and an
answer meet the same rules. `serve` merges saved settings under its flags with
`auth.build_config()`; every auth flag defaults to `None` so "not given" is
distinguishable, and a new flag needs a `DEFAULT_SETTINGS` key to match. A saved
file that fails validation must stop `serve` (`load_settings()` raises): skipping it
would start the server with auth off. Keep it that way for any security section.

### Health checks

`serve` starts `ContainerService.start_health_monitor()`: a daemon thread that runs
`check_health()` every `interval_seconds`, never overlapping, and keeps the records in
memory on the service (one per `serve` process, like template runs). `health()` only
reads that memory, so `/api/health` is safe in the UI's 3s poll; there is deliberately
no route that triggers a round. The CLI has no previous round, so `check_health()`
takes two samples `window` seconds apart for CPU. `health.py` judges samples and never
calls the daemon; `service.py` gathers them (one `list_instances()` plus a
`read_file("/proc/loadavg")` probe per instance on a small pool). A container's
`/proc/loadavg` is the host's unless LXCFS runs with `-l`, so `health.LoadSampler` (a
second `serve` thread, every 5s like the kernel's LOAD_FREQ) counts R and D threads in
the container's cgroup on the host (`cgroup_dir()`, prefix from `CGROUP_PAYLOAD_PREFIX`
in `lxd.py`) and keeps 1/5/15 minute averages; the CLI averages over its window with
`measure_load()`. This is the one place lemondx reads host kernel state instead of the
API, which relies on being on the daemon's host. VMs use the guest's load via the
probe, and a container whose cgroup is missing falls back to the probe, judged only when
`load_scope()` says it is the instance's own. The load threshold is absolute
(`load_average`, strictly greater than). The file API's Content-Length for a `/proc`
file can disagree with its body, which `read_file()` tolerates. Health settings are the `health` configure section; unlike auth, a broken
file falls back to defaults with a warning.

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
`description`, `order`, `uses`, `param`, `secret`, `text`). `BootstrapRunner` sorts by `order`, prepends
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
JSON file per bootstrap profile, `templates/` for one per instance template,
`modules/` for uploads, `auth/` (0700) for local users and API token hashes, `config/`
for `lemondx configure` sections -- the one place where an unparseable file is an error,
not a default. Nothing else in the codebase builds those paths — go
through `store.py` so migration and atomic writes apply.

Profiles and templates share one `_Records` implementation and are one file
each so they can be copied between machines or checked into a project, which
means **every such file is untrusted input**: the name in the file is
authoritative, the filename is only `profile_slug()` of it (two names that slug
alike get `-2`, `-3` suffixes), and `_clean_profile()`/`_clean_template()`
normalise every record on read. A file that no longer parses is skipped rather
than failing the listing. The service then drops secrets and re-parses SSH keys
on every listing (`_public_selection()`), and on save completes the selection
with every non-secret parameter its modules declare (`_stored_selection()`).

A template is a create request minus the name. `launch_template()` refuses
anything that would fail for every instance before creating one, then creates
`<prefix>-<n>` instances on a small thread pool (the LXD client opens a socket
per request, so this is safe), tags each with `user.lemondx.template`, and
passes `remember_params=False` so launches never rewrite module settings.
Recreate and destroy take the instance list the user confirmed and refuse with
409 if the tagged set differs (`_confirmed_instances()`); recreate runs
`_launch_bootstrap()`'s checks before deleting anything. All three run through
`_tracked()`, which records the run on the service instance (one per `serve`
process) and refuses a second one on the same template, so the UI reads
progress from `/api/template-runs` rather than holding it in component state
that unmounting would lose.

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
with the status so `App.tsx` can treat 401 as "ask `/api/auth` what the server accepts
and show `LoginGate`" (a password login sets an HttpOnly cookie; a pasted token is kept
in `sessionStorage` for that tab only). `hooks/useAuth.ts` exposes the principal;
`useCanWrite()` disables mutating controls for read-only users -- a convenience only,
the server's route roles are the enforcement -- so gate any new mutating button with it.

`App.tsx` polls `/api/status`, `/api/containers` and `/api/creates` every 3s and holds a
`mutating` ref that pauses polling while a mutation is in flight, so a poll cannot clobber
optimistic state. Long operations **must not depend on the page that started them**: a
user closes the dialog, switches tab or reloads, and a request held open for minutes also
ties up one of the browser's few connections per host. So the UI starts creates and
template runs with `background: true` -- the service validates, records the job
(`_begin_create()`, `_tracked()`), runs it on a daemon thread and returns the record at
once -- and `App.tsx` follows every job through the same 3s poll, reporting each one
exactly once when a poll finds it finished. Without the flag the same calls block, which
is what the CLI and scripts get. `serve()` waits on `pending_work()` at Ctrl-C. Template exec is the same
kind of run (action `exec`); its output is cut to `EXEC_OUTPUT_LIMIT` per stream
because run records are re-sent on every poll.
`BootstrapPanel` (modules on an existing container) still keeps its progress in component
state.

## Style

Comments explain *why*, not what — the existing ones are the model: they record the
trade-off or the daemon quirk that forced the code's shape. Keep that density rather than
annotating obvious lines. Commit messages are a conventional-commit subject
(`feat:`, `fix:`, `docs:`, `feat!:` for a break) in the imperative, followed by prose
paragraphs explaining the reasoning, wrapped at ~72 columns. The subject picks the next
version number -- see docs/development.md.

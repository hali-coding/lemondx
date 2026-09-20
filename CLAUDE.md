# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

lemondx is a web UI and CLI for local LXD/Incus containers, optionally federated
with other lemondx nodes. `README.md` and `docs/*.md`
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
LEMONDX_DATA_DIR=/tmp/n1 ./lemondx cluster status   # a second node's state, for testing federation
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
token or user is. `cluster.py` (`ClusterService`) is the same again for
federation, one layer up: it holds a `ContainerService` for this host and an
`AuthService`, where the cluster credential is kept as an ordinary token, and
calls other nodes through `nodeclient.py` -- which is to a peer what `lxd.py` is
to the daemon, transport and nothing else. The dependency only goes one way:
`service.py` knows nothing about nodes.

`ContainerService` (`src/lemondx/service.py`) is the only place domain logic lives, so the
API and the CLI cannot drift. **Adding a feature means: a method on `ContainerService`,
then a route in `build_router()` (`server.py:85`) and a subcommand in `build_parser()`
(`cli.py:1653`).** A route's role defaults to `read` for `GET` and `admin` otherwise;
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

### Federation

Flat by design: no leader, no quorum, no shared state. A node is a complete
lemondx; a cluster means each one knows how to call the others, so losing one
costs that node alone and `cluster.py` never has to reconcile anything. Template
sync is a one-way push -- the node pushed from wins -- because there is no
shared clock to do better with.

**Joining is one way and then it spreads.** `join()` redeems a code against any
one member and gets back the cluster credential *and* the whole member list,
then announces itself to each member (`sync_members()`). There is deliberately
no reciprocal handshake: a per-pair token exchange would need every node up at
the moment a new one joined. Membership converges through `sync_members()`,
which pulls each peer's list and pushes ours, rather than a broadcast that would
miss whoever was down.

The cluster credential is one shared secret held by every member, stored twice
on purpose: plaintext in `auth/cluster.json` for calling out, and its hash as an
ordinary token record for calls coming in -- so `AuthService.authenticate_token`
authenticates a peer with no new code path, and `lemondx tokens` lists it. The
consequences are in the design, not accidents: a copy of it sits on every host,
and any member is an admin of every other.

**Leaving and eviction are one operation seen from two ends**, and both are
ordered so nothing is left half-in. `evict_node()` calls the target's
`evicted()` *first*, while its record is still here to call it with, so it
`_stand_down()`s -- credential, peer records and node groups gone -- instead of
carrying on as a member nobody answers; only then is it forgotten here and on
every other member. `leave()` is the same in reverse: tell every peer, then
`_stand_down()`. The shared credential is why the order matters: a node that
stood down has given its copy up, so `rotate_secret()` is only needed when the
target could not be told -- and rotating regardless would strand any member
that merely happened to be off, turning one eviction into two. `drop_member()`
(a peer relaying either) never re-broadcasts, for the same reason
`from_peer()` exists.

`SYNC_KINDS` includes `users`, which is the only thing sync moves that is a
credential: an account is a name, a role and a hash, and the hash is the only
part that makes it usable elsewhere -- there is no plaintext to re-hash on the
far side. So it crosses as the stored record, which is why `AuthService` grew
`export_users()`/`adopt_user()` beside `list_users()`/`set_user()` rather than
inside them: the pair a person and the API use must never carry a hash. The
receiving route is members-only (`_peers_only()` in server.py), narrower than
admin, because a person setting a password already has the ordinary route and
never needs to post a hash.

`auto_groups()` is the one thing that writes a node group without being told
what to put in it: it reads each node's CPU threads and total memory (capacity,
never free space -- a group outlives the reading) and scores each as a share of
the cluster *average*, so `large`/`small` mean something relative and a cluster
of identical hosts falls out as every node in both groups rather than needing a
special case. `SIZE_TOLERANCE` exists because two hosts of one spec never report
the same totals. Those two names are reserved: `_refuse_managed()` guards
`save_group()` and `delete_group()`, and the `managed=` flag that lifts it is
passed only by the sizing itself and by `from_peer()` on the routes -- a member
relaying the sizing must be able to write what a person may not. `list_groups()`
derives `managed` from the name rather than storing it, so a copied file cannot
claim to be one.

**Nothing needs configuring to federate.** `ensure_identity()` works out a name
(hostname), an address (`guess_local_address()` plus the port from
`store.read_runtime()`, which `serve` writes) and a certificate (generated), and
saves what it chose so peers keep pinning the same thing. `configure cluster` is
override-only. `self_check()` connects to this node's own advertised URL and
compares what is served with what would be handed out, because every mismatch
there is otherwise discovered hours later as another node failing to call back.

A cluster member requires a credential from non-loopback callers even when
`AuthConfig.enabled` is false (`requires_remote_token()`, applied in
`_principal()`): accepting API calls from other hosts is incompatible with
treating whoever reaches the port as an admin, and the loopback exception is
what keeps that from locking the user out of their own UI with no way back in.

Trust between nodes is pinned certificates. `NodeClient._connect()` checks the
pin after the handshake and **before writing the request**, so a wrong
certificate never sees the credential -- keep any new call path going through
`_connect()`. `/api/cluster/enroll` is the one unauthenticated endpoint, handled
in `_handle_enroll()` before routing like login, because the one-time code in
the body *is* the credential; invite hashes live in `auth/` on disk, so a code
issued by the CLI is redeemable against a running `serve`.

`/api/nodes/{node}/{path}` forwards one call to a member (`_forward()` in
server.py, `ClusterService.proxy()` for the transport), which is what lets the
container drawer manage an instance anywhere: snapshots, limits, the console
and a bootstrap run are all ordinary per-container endpoints, and forwarding
them whole is far less to get wrong than mirroring each. **The role enforced is
the target route's**, resolved against the same router, so proxying can never
widen what a caller may do -- an unknown path resolves to admin and fails
closed. Naming the local node dispatches through the router here rather than
looping back over HTTP, so the front end never special-cases itself. Timeouts
come from `PROXY_TIMEOUTS`, since a bootstrap genuinely takes minutes and
nothing else does.

The Containers tab's scope (`hooks/useScope.ts`) is the one setting both it and
the Templates tab read, so the instances a template lists and the table's rows
always cover the same hosts; it lives in `localStorage` because it is how one
person is looking right now, not cluster state. Widened, an instance's identity
becomes `keyOf()` -- node plus name -- since two nodes may each hold a `web-1`
unless a cluster-wide launch named them, and `_share_names()` allocates against
*every* member for that reason, not just the targets. Health is judged where the instance lives -- each node's monitor, its own
schedule -- and `cluster.containers()` only carries each node's latest records
(`health`, tagged `node`), so the UI keys them by `keyOf()` like the rows.
The drawer takes the owning node and routes every call there,
including the bootstrap pickers, since a module runs on that host and a key is
installed from what that host can see.

**Reconciliation is the pull half of that push.** `reconcile()` runs on a
daemon thread shortly after `serve` starts and hourly after, because a node
that was off missed every push made meanwhile and push-only sync gives it no
way to find out. It compares `manifest()`s -- one digest per artifact, taken
over the very body `_sync_payload()` would send, so "differs" and "what would
be pushed" cannot disagree -- and settles each difference with `store`'s change
ledger (`changes.json`, `note_change()`): when *this* node last saved or deleted
each thing. The ledger is why a returning node does not resurrect what it
deleted while a peer was off, and it is local and never synced, because the
record files are copyable and untrusted so a timestamp in one proves nothing.
`_inward()` acts locally first and `_outward()` then pushes the corrected state,
in that order -- the other way round a week-stale node overwrites everyone
before discovering it is stale. A difference on *both* sides is a conflict:
reported by `_conflicts()`, never resolved, since nothing in the data can say
which copy is right. `RECONCILE_KINDS` leaves `users` out on purpose -- it
carries password hashes and only an explicit `cluster sync` should move it.
`_stand_down()` drops the tombstones (`store.drop_tombstones()`) so deletions
decided by one cluster are not carried into the next.

`leave()` also clears the half-state a broken cluster leaves behind -- peer
records and groups with no credential to use them with. Refusing that as "not
in a cluster" would be true and useless, since the leftovers are what wants
clearing, and it is the one case with no other way out: `info()["leftovers"]`
is what the UI reads to offer it.

Templates, modules, bootstrap profiles and node groups are pushed to every
member the moment they are saved, and removed from every member when deleted --
`ClusterService.save_template()` and friends wrap the `ContainerService` call
and then `_propagate()`. Two things keep that from going wrong: `from_peer()`
(a member relaying a push never re-broadcasts, so nothing echoes round the
cluster) and the push being best-effort with a short timeout, since the local
save has already happened and must not be undone because another host is off.
What each node made of it rides back on the record as `synced`, which is also
why those routes take `principal=True`. Deleting propagates because sync only
ever pushes -- it cannot clear a copy the target has and this node does not.

`ClusterService.change_state()` / `delete_containers()` / `template_action()`
take `[{node, name}]`, group by node and fan out. Each takes the plain local
path when the only node named is this one, so an unfederated lemondx never
touches cluster code. The `*_instances` methods on `ContainerService`
(`launch_`, `destroy_`, `recreate_`, `exec_`) are the untracked halves these
call: tracking twice under one template name would deadlock a run against
itself.

A cluster launch is tracked as an ordinary template run
(`ContainerService.track_run`, public for exactly this) with each instance
tagged `node`, so the UI's existing 3s poll follows it with no second mechanism.
The local share calls `launch_instances()` rather than `launch_template()`,
which would start a second run on the same template. Remote shares are started
with `background: true` and polled (`_await_run`), never held open for the
minutes an image pull takes.

`place_template()` is why a template written for one host launches on another: a
pool, network or profile the node lacks falls back to the default profile's, and
the substitution is returned as run notes *and* logged, since a launch started
from another node's UI is only visible here in the log. It says "this node"
rather than naming one -- naming is the coordinator's job, and it knows where
each set of notes came from.

### Stacks

`stacks.py` (`StackService`) sits one layer above `ClusterService`, because a
launch step may name nodes and groups: every launch goes through
`ClusterService.launch_template()`, which takes the plain local path when the
only node is this one. Each launch is therefore an ordinary tracked template run
(the Templates tab shows it, the one-run-per-template lock holds), and a stack
run is the record around those, in memory on the `StackService` like template
runs are on `ContainerService` -- `serve` builds one and hands it to
`build_router()`, and `_wait_for_background_work()` counts its runs too. Stages
run in order and the steps in one on threads side by side; a launch with
`wait_bootstrap` off returns once its instances run and leaves a follower
thread on its template run, which a health gate, a later launch of the same
template and the end of the stack all join. Metadata crosses to later launches
as ordinary launch params: `LEMONDX_STACK_<ID>_*` values (the runner exports
every param as an env var) plus `{{step.field}}` placeholders rendered into the
step's own params, so remote shares get it through the existing API. Stacks
sync like templates (`SYNC_KINDS`), dragging their templates along in
`_sync_payload()`. A stack may set a secret param only to `{{params.NAME}}`
(a value entered at launch), never a literal; `_check_stages()` refuses one on
save and `public_stack()` strips one on the way out, because a stack file is
untrusted input like every other record and only a save goes through the check
-- the two share `_input_only()` so they cannot disagree about what a literal
is. A step naming a template this node lacks is judged against every module's
secrets, since nothing says which of its params are secret.
The designer's parameter panel (`LaunchParams` in `StackEditor.tsx`) lists every
param the step's template *declares*, not just the overrides the stack stores,
with the inherited value as the box's placeholder -- the panel answers "what
will this launch use?", while the record stays a diff. `ReferenceInput` completes
`{{...}}` at the caret from `suggestionsFor()`, which is the one place the
designer's idea of a valid reference lives; it must stay in step with `FIELDS`
and `REFERENCE`/`INPUT_REFERENCE` in `stacks.py`, since the server is what
actually refuses a bad one.
What a stack is running is the `user.lemondx.stack` tag (`STACK_CONFIG_KEY`)
threaded through every launch path -- `launch_instances()`, the peer launch API,
and recreate, which carries it over -- and read back with
`cluster.containers(everything=True)`; stop/start/destroy/relaunch confirm
against that like a template's destroy, and destroy/relaunch run as a stack run
whose first step is `_teardown`. Because the tag is the whole of what a teardown
acts on, the launch route takes it only from a member (`_stack_tag()` in
server.py, and again in `_forward()`, where a relayed call would otherwise reach
the target wearing the cluster credential): a stack's local share never goes
through the API at all, and a person naming a stack on an ordinary launch would
be giving it instances to destroy later.

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

A template may carry an `app_check` (a Nagios-style script: exit 0/1/2/3), run
in each instance tagged with that template. Under `serve`, `health.AppChecker`
runs checks on each template's own `interval_seconds`, not the round's, so the
two are kept apart: `Tracker.evaluate()` judges the machine, `fold_app()` adds
the latest app result, and the service keeps both (`_health_base`,
`_health_records`) so a result landing between rounds refolds at once. The
script goes through `_APP_CHECK_WRAPPER` (file in `/tmp`, interpreter from its
`#!` line, a /proc-walking watchdog that kills the script's whole tree --
not `timeout`, which kills only its child), and the check is read from the template every
round, never copied onto the instance; a template save or delete (including a
peer's push) also swaps it into the scheduler at once
(`AppChecker.update_template()`). `_template_body()` must carry `app_check`:
a push is a whole-record PUT, so a field it omits is deleted on the peer. A
running instance with no check reports app `ok`, never null. A run's full
stdout/stderr (`APP_DETAIL_KEYS`) stays in the checker and is stripped from
records by `_app_status()`; `app_check_output()` serves it on request.

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
`nodes/` and `node-groups/` for federation, `modules/` for uploads, `auth/`
(0700) for local users, API token hashes, the cluster credential and join-code
hashes, `runtime.json` for what `serve` is listening on, `changes.json` for
when this node last saved or deleted each shared artifact (the one file here
that is *not* meant to be copied -- see reconciliation above), `config/`
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

Node records and groups (`nodes/`, `node-groups/`) are two more `_Records`
directories held to exactly those rules, and copyability is the point of them
too -- which is why the token for calling a peer is not in its record but in
`auth/`.

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
`web/src/index.css` with theme variables. The Nodes tab (`NodesView.tsx`) polls on its own 10s interval rather than App's
3s one, because every listing probes each peer; the server caches those probes
for a few seconds so an open tab is not a load generator.
`web/src/lib/types.ts` hand-mirrors the payload
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

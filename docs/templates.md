# Instance templates

[← back to README](../README.md)

A template is a whole instance setup saved under a name: image, container or
VM, CPU/memory/disk limits, storage pool, network, daemon profiles, the ephemeral,
start and secure boot flags, and a bootstrap selection — modules, every one of their
parameters, and the SSH public keys to install. Launching one creates one or
many identical instances in a single step.

A [bootstrap profile](modules.md#defaults-saved-settings-and-profiles) only
covers the bootstrap part; a template covers everything. The template editor
can start its module selection from a profile.

## In the web UI

The **Templates** tab lists every template with what it creates and which
instances were launched from it. Pick a count and press **Launch**; the names
the launch will use are shown beside the button. Instances are created one per
host CPU core at a time, and each reports its own result, so one that fails to
create or bootstrap does not stop the rest, and the ones that exist are kept.

Once a template has instances, its card also offers **Run command**,
**Recreate all** and **Destroy all** — see
[running commands](#running-a-command-on-every-instance) and
[recreating and destroying](#recreating-and-destroying).

A launch, recreate or destroy runs in the background on the server, not in the
page: the request only waits for the server to accept it. Leaving the tab,
reloading or closing the browser does not stop it, and a notification reports
how it went wherever you are by then. The card shows it in progress from any
tab, then keeps the last result until you dismiss it. Only one of these runs
per template at a time; starting another while one is going is refused. Stopping
the server does stop it — see
[Creating in the background](web-ui.md#creating-in-the-background).

Make a template with **New template**, or fill in the new-container dialog and
press **Save as template** there — the container name you typed becomes the
instance name prefix. The same dialog's **Template** menu does the reverse and
fills the form from a template, for a one-off variation.

## From the CLI

`template-save` takes the same instance and bootstrap flags as `create`:

```bash
./lemondx template-save "Web server" -i images:debian/12 -c 2 -m 2GiB \
    -b base -b ssh-access --param USERNAME=hampus --all-ssh-keys \
    --prefix web --description "nginx boxes"
./lemondx template-save "Pg" -i ubuntu:24.04 -P "Web server" -b postgresql
./lemondx templates
./lemondx launch "Web server" -n 3            # web-1, web-2, web-3
./lemondx launch "Web server" --prefix canary # canary-1
./lemondx template-delete "Pg"
```

`launch` exits 1 if any instance failed, and `--json` prints the full result.
Deleting a template is refused (`409`) while a [stack](stacks.md) launches it;
the error names the stacks.

```bash
./lemondx template-exec "Web server" -- apt-get -y upgrade
./lemondx template-recreate "Web server"      # lists them, then asks
./lemondx template-destroy "Web server" -y
```

## Running a command on every instance

**Run command** takes a shell command and runs it through `sh -c`, as root, on
the template's running instances at the same time — up to ten at once. The
dialog lists every instance: running ones are ticked (untick any to leave them
out), stopped ones are skipped. It remembers the template's last command, so
running it again is one click, and the timeout applies to each instance
(default 300 seconds, at most an hour).

Like every template run it happens in the background: the card says it is
running, a notification reports how many instances exited non-zero, and the
card then shows each instance's exit code and output — failures expanded. It
is non-interactive with no TTY, as in a container's own console, so anything
that prompts needs its `-y`-style flag. Each instance's stdout and stderr are
kept to their last 32 KiB.

From the CLI, `template-exec` runs on every running instance (saying which
stopped ones it skipped), prints each instance's output and exits 1 if any
exited non-zero. The words after `--` are quoted for you, so
`-- echo "a b"` behaves as it would in a local shell; pass one quoted string to
use pipes or `&&`.

## Recreating and destroying

Both act on every instance launched from the template, whatever its state —
running, stopped or frozen — since a stopped one left behind by "destroy all"
would only be found later.

**Destroy** stops and deletes each instance, with its filesystem and
snapshots. The template itself stays. It works from the CLI for a template
that has since been deleted, because it goes by the tag on the instances.

**Recreate** deletes each instance and creates it again under the same name
from the template *as it is now*, so it is also how an edited template reaches
instances launched before the edit. Everything inside the old instances is
lost. Secrets are asked for as on launch, and every check that could fail for
all of them runs before anything is deleted. An instance that fails to delete
is left as it is rather than recreated.

Both run one instance per host CPU core at a time and report each one. To avoid
destroying an instance someone launched after you looked, the request carries
the list of instances you confirmed; if the template's instances no longer
match it, nothing happens and the request fails with `409`.

## App health checks

The built-in [health check](web-ui.md#health-checks) judges an instance as a
machine: does it answer, is it out of CPU, memory or load. A template can also
say what "working" means for the application on it, with a script run inside
each of its instances — optional, and set per template under **App health
check** in the template editor, or with `--app-check` on `template-save`.

The script answers the way a Nagios plugin does, by exit code, and the first
line it prints (up to any `|`, where performance data would start) is shown as
the status text:

| Exit | App status | Instance becomes |
| --- | --- | --- |
| 0 | ok | no change |
| 1 | warning | degraded |
| 2 | critical | degraded; unhealthy once it repeats as many times in a row as the health settings' `failures_before_unhealthy` (2) |
| 3, or anything else | unknown | degraded — a missing command (127) lands here |

An instance whose template has no app check always reports its app as `ok`, so
the app status is never blank. A configured check reads `pending` until it
first answers, and is not judged while the instance is being created or
bootstrapped, or within the start grace period.

```sh
# Exit 0 ok, 1 warning, 2 critical, 3 unknown.
if curl -fsS -o /dev/null --max-time 5 http://localhost/; then
  echo "OK - web server answering"; exit 0
fi
echo "CRITICAL - web server not answering"; exit 2
```

- **Interval** (`interval_seconds`, default 60, 10 to 86400): how often it
  runs, on its own schedule rather than the health round's — a result shows
  on the instance as soon as it is in.
- **Timeout** (`timeout_seconds`, default 10, 1 to 300, shorter than the
  interval): the script is stopped inside the instance — it and every process
  it started, so a hung `curl` under it goes too — and the check counts as
  **critical**, as Nagios treats a plugin that overran. The watchdog that does
  it is plain `sh` reading `/proc`, so it needs nothing installed in the image,
  and a hung check can never pile up copies of itself.

It runs as root through the daemon's exec API, like
[Run command](#running-a-command-on-every-instance). Without a `#!` line it is
`/bin/sh` and must parse as POSIX sh, which is checked on save; with one
(`#!/bin/bash`, `#!/usr/bin/env python3`) that interpreter runs it and must
exist in the instance. At most 16 KiB. The check is read from the template on
every run rather than copied onto instances, so editing it reaches instances
already launched — and a template that is deleted takes its check with it.

```bash
./lemondx template-save "Web server" -i images:debian/12 -b base \
    --app-check checks/web.sh --app-check-interval 30 --app-check-timeout 5
./lemondx template-save "Web server" -i images:debian/12 -b base -m 4GiB  # keeps the check
./lemondx template-save "Web server" -i images:debian/12 -b base --no-app-check
```

`--app-check -` reads the script from stdin.

Health records carry only the first line of output, since they are re-sent on
every poll. The latest run's full stdout and stderr (the last 16 KiB of each)
stay on the node that ran it, in memory, and `GET
/api/containers/{name}/app-check` returns them — which is what clicking the
diamond in the web UI shows.

## Naming

Instances are named `<prefix>-<n>` with the lowest numbers no instance is
using, so deleting `web-2` means the next launch creates `web-2` again. The
prefix defaults to a slug of the template name and can be overridden per
launch. Every launched instance carries `user.lemondx.template` in its config,
which is how the UI groups them; renaming a template does not relabel
instances launched before the rename.

## Launching across nodes

A launch stays on this host unless it is told otherwise. If this lemondx is
federated with others (see [Nodes and federation](cluster.md)), `launch` takes
`--node` and `--group`, and the UI's Launch button opens a node picker:

```bash
lemondx launch web -n 6 --group edge
```

Instances go round robin, and the `<prefix>-<n>` numbering is allocated across
every chosen node at once, so a name means one instance in the cluster rather
than one per host. The numbering is a scan of what every member holds, not a
reservation, so two launches started at the same moment from two different nodes
can still land on the same name — nothing addresses an instance by name alone,
so the result is untidy rather than ambiguous. The template is pushed to each
node first, so a node that has never seen it can still run the launch.

A node that lacks the template's storage pool, network or profile substitutes
its own default rather than failing every instance, and the run says so, per
node. A node that fails outright fails only its own share.

## Parameters, keys and secrets

Saving fills in every non-secret parameter the selected modules declare — from
your input, then the module settings as they are at that moment, then the
module's own default — so a template keeps launching the same thing after
those settings change, and a copied file brings its values along. Launching
from a template never rewrites your saved module settings.

SSH public keys are stored in the template, and a template whose modules
install keys cannot be saved without one. Keys pasted into the UI need not be
in `~/.ssh`.

Secrets are the exception: they are never written to a template (see
[secret parameters](modules.md#secret-parameters)). A template that needs one
asks for it on each launch — the UI prompts, the CLI reads an environment
variable of the same name or prompts without echo, and the API takes it in
`params`. Every instance in one launch gets the same value. Anything that would
fail for every instance, like a missing secret or a module that has since been
deleted, is refused before any instance is created.

## API

```bash
curl -s localhost:8099/api/templates | jq
curl -s -X PUT localhost:8099/api/templates/Web%20server \
  -H 'content-type: application/json' \
  -d '{"image":"images:debian/12","cpu":"2","memory":"2GiB","name_prefix":"web",
       "bootstrap":{"modules":["base"],"params":{},"ssh_keys":[]},
       "app_check":{"script":"curl -fs localhost >/dev/null || exit 2",
                    "interval_seconds":30,"timeout_seconds":5}}'
curl -s -X POST localhost:8099/api/templates/Web%20server/launch \
  -H 'content-type: application/json' -d '{"count":3}'
curl -s localhost:8099/api/templates/Web%20server/instances   # ["web-1","web-2","web-3"]
curl -s -X POST localhost:8099/api/templates/Web%20server/destroy \
  -H 'content-type: application/json' -d '{"instances":["web-1","web-2","web-3"]}'
```

`PUT` replaces the whole template, so `app_check` absent or `null` means no
check.

Launch, recreate and destroy all respond with `{"template", "ok", "notes",
"instances": [{"name", "ok", "error", "container"}]}`. `notes` lists what a node
substituted rather than failing on — a storage pool or network it does not have
— and each instance carries `"node"` when the launch spanned more than this one. `POST /api/templates/{name}/exec`
takes `{"command", "instances": [...], "timeout"}` — every named instance must
be from the template, or nothing runs — and each entry also carries `"exec":
{"exit_code", "stdout", "stderr", "truncated"}`. `container` is the same detail
`POST /api/containers` returns, including its `bootstrap` result, or `null`
for an instance that was destroyed or never created. Recreate takes
`{"instances": [...], "params": {...}}`.

Launch, recreate and destroy block until done by default. Add `"background":
true` to the body and they return the run record at once instead, which is how
the web UI calls them.

`POST /api/templates/{name}/launch` also takes `nodes` and `groups` to spread a
launch over a cluster, and `names` to ask for particular instance names — which
is how a coordinating node keeps numbering unique across one.

`GET /api/template-runs` lists what the server is running or last ran for each
template — `{"template", "action", "count", "started_at", "finished_at",
"result", "error", "notes", "nodes"}`, with `finished_at` null while in
progress — and
`DELETE /api/template-runs/{name}` forgets a finished one. This is held by the
`serve` process, so the CLI in a separate process neither sees these runs nor
is blocked by them.

## On disk

One JSON file per template in `~/.local/share/lemondx/templates/`, named after
a slug of the template name, just like profiles — copy them between machines
or check them into a project:

```json
{
  "name": "Web server",
  "description": "nginx boxes",
  "name_prefix": "web",
  "image": "images:debian/12",
  "type": "container",
  "cpu": "2",
  "memory": "2GiB",
  "disk": "",
  "pool": "",
  "network": "",
  "profiles": [],
  "ephemeral": false,
  "start": true,
  "secureboot": true,
  "bootstrap": {
    "modules": ["base", "ssh-access"],
    "params": { "USERNAME": "hampus", "SHELL_PATH": "/bin/bash" },
    "ssh_keys": ["ssh-ed25519 AAAA… hampus@laptop"]
  },
  "app_check": {
    "script": "curl -fs localhost >/dev/null || exit 2\necho OK\n",
    "interval_seconds": 30,
    "timeout_seconds": 5
  }
}
```

A blank `pool` or `network` means whichever one the daemon's default profile
uses. `"secureboot": false` launches VMs from an image that is incompatible
with UEFI secure boot, by setting `boot.mode=uefi-nosecureboot` (on Incus,
`security.secureboot=false`); it is ignored for a container. A file
that no longer parses is skipped; a secret or an invalid key written into one
by hand is ignored.

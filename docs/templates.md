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
       "bootstrap":{"modules":["base"],"params":{},"ssh_keys":[]}}'
curl -s -X POST localhost:8099/api/templates/Web%20server/launch \
  -H 'content-type: application/json' -d '{"count":3}'
curl -s localhost:8099/api/templates/Web%20server/instances   # ["web-1","web-2","web-3"]
curl -s -X POST localhost:8099/api/templates/Web%20server/destroy \
  -H 'content-type: application/json' -d '{"instances":["web-1","web-2","web-3"]}'
```

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
  }
}
```

A blank `pool` or `network` means whichever one the daemon's default profile
uses. `"secureboot": false` launches VMs from an image that is incompatible
with UEFI secure boot, by setting `boot.mode=uefi-nosecureboot` (on Incus,
`security.secureboot=false`); it is ignored for a container. A file
that no longer parses is skipped; a secret or an invalid key written into one
by hand is ignored.

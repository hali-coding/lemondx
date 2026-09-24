# Stacks

[← back to README](../README.md)

A stack launches several [templates](templates.md) in a fixed order at the click
of a button: one database server, then three app servers pointed at it, then
six load-test runners pointed at those. Later launches are handed the names and
addresses of what earlier ones made, so nothing has to be copied by hand between
steps.

A stack is a list of **stages**. Stages run one after another, and the **steps**
inside one stage run at the same time. A step is one of:

| Step | What it does |
| --- | --- |
| **Launch** | Launches a template, `count` instances (1–20), on this node or on nodes or a group of a cluster. |
| **Sleep** | Waits a fixed number of seconds (1–86400). |
| **Wait until healthy** | Waits until every instance launched by an earlier stage is healthy, or gives up after its timeout (default 15 minutes). |

## Designing one in the web UI

The **Stacks** tab lists every stack as a pipeline, left to right. **New stack**
(or the pencil on a card) opens the designer:

- **Left:** the step types, and every template. Drag one onto the canvas, or
  click it to add it as a new last stage.
- **Middle:** the stack, top to bottom. Drop a step *between* two stages to make
  it a stage of its own, which runs after the one above. Drop it *onto* a stage to
  run it next to the steps already there. Existing steps can be dragged the same
  way; the × on a card removes it.
- **Right:** the selected step's settings: its id, template, count, name prefix,
  where it runs, the **Wait for bootstrap modules** switch and parameter
  overrides. The buttons at the bottom move it to the stage above or below
  without dragging.

Problems show on the card in red before you can save: a template that does not
exist, the same template twice in one stage (a template runs one launch at a
time), a health wait with nothing launched before it, or a reference to a step
that is not an earlier launch.

### Setting a launch step's parameters

The **Parameters** panel lists every parameter the step's template actually
declares, grouped by the module that declares it, so what you are looking at is
the whole set the launch will run with rather than only what you have changed.

Each box shows, greyed out, the value the launch would use if you left it
alone — the template's own answer, or failing that the module's default — with
a line underneath saying which of the two it is. Type over it and the name turns
gold with a **set here** badge: that parameter is now the step's, and only what
you set is saved in the stack. Empty the box again and it goes back to
inheriting. A parameter no module declares can be added at the bottom, under
**Anything else**, for a module that reads something straight out of the
environment.

**Typing `{{` in any value opens the list of things it can refer to** — every
earlier launch crossed with every field it exposes, and every value entered at
launch — each with a line saying what it becomes. It filters as you type, so
`{{ipv` narrows to the IPv6 fields. Arrow keys move, Enter or Tab accepts, and
the closing `}}` is written for you. A reference that names no earlier launch is
marked on the field as you type rather than waiting for the save to be refused.

A **secret** parameter is marked as such and left empty means *asked for when the
stack is launched*. The only thing it may be set to is `{{params.NAME}}` — a
literal is refused, on the field and again on save, because a stack file is
saved to disk and synced to every node. See [Secrets](#secrets).

**Launch stack** starts it. It runs on the server, so leaving the tab or closing
the browser does not stop it. The card shows each step as it goes: pending,
running, *bootstrapping* (running, with its modules still going in the
background), done, failed, skipped or cancelled. Under that it lists the
instances each launch made and their addresses. A notification says how the
stack ended. **Cancel** stops anything new from starting. Launches already
under way finish, because stopping one halfway would leave an instance created
but never bootstrapped.

## A stack's instances: stop, start, relaunch, destroy

Every instance a stack launches is tagged with the stack's name
(`user.lemondx.stack`) on the instance itself, next to its template tag. That
tag is the stack's state. It survives a restart of lemondx, it is wherever the
instance is in a cluster, and recreating the instance from its template keeps
it. There is no separate list that could disagree with what is actually running.

Once a stack has instances, its card lists them and offers:

- **Stop all / Start all:** stop or start every one of them, wherever they are.
- **Relaunch:** destroy them all, then run the stack again from the start, as
  one run. The first step, *destroy N existing*, shows on the card like any
  other. Everything a launch checks up front is checked before anything is
  destroyed, so a relaunch refused for a missing password has deleted nothing.
- **Destroy:** stop and delete them all. The stack itself is kept.

As with a template's Destroy all, the request carries the instances you were
shown. If the stack's instances have changed since (another launch, or one
deleted by hand), nothing happens and you are asked to look again (`409`). If a
node does not answer, nothing happens either, since its instances cannot be
checked.

```bash
./lemondx stacks                        # includes how many instances each has running
./lemondx stack-stop "Load test"        # also stack-start, stack-restart
./lemondx stack-launch "Load test" --relaunch    # lists them, asks, then relaunches
./lemondx stack-destroy "Load test" -y
```

A stack launched onto a node running an older lemondx gets its instances there
untagged, so they are not counted as the stack's.

Because the tag is the state, deleting a stack -- or renaming one, which saves
it under the new name and deletes the old -- leaves its instances tagged with
the name that has gone: nothing reaches them as a group any more, and a stack
later given that same name counts them as its own and would destroy them with
the rest. Both dialogs say so and list what would be left, so destroy the
instances first if they should go with the name. This is the template tag's
behaviour too, and for the same reason: the instance is the record.

### Stale instances

A stack's instances also record which version of the stack made them. Edit
the stack and every one of them is marked **stale** -- on its card, its rows
and on Home -- until a **Relaunch**. The whole stack counts, not only the step
that made an instance, because a step's parameters can come from any earlier
one. A template change marks them stale too, as it does any template's
instances; recreating from the template clears that, but not a stack change,
which only a relaunch applies. **Replace N stale** on the stack's card is a
relaunch that says why: a stack is replaced as a whole, since its steps hand
addresses and values to each other, so every instance is destroyed and the
stack runs again, not just the stale ones.

## Waiting for bootstrap, or not

Each launch step has a **Wait for bootstrap modules** switch, on by default. On,
the next stage starts only once every module has finished on every instance.
Use this when the next stage needs what the modules installed, such as a database
it connects to.

Off, the next stage starts as soon as the instances are running and have an
address, and their modules carry on in the background. That is quicker when
the next stage only needs names and addresses. A stage is not over until its
background bootstrap has finished, so:

- a **Wait until healthy** step waits for every earlier launch's bootstrap
  first;
- a later launch of the *same* template waits for the earlier one's run to end;
- the stack as a whole finishes only when every bootstrap has, and one that
  fails in the background still stops any stage that has not started yet.

## Passing metadata to later launches

What a launch made is available to every launch after it, in two forms, and so
is anything entered when the stack was launched.

**In any module's environment.** Every module of every later launch sees these
variables, space-separated, one entry per instance, in name order. The step id
is written in capitals:

| Variable | Contents |
| --- | --- |
| `LEMONDX_STACK` | the stack's name |
| `LEMONDX_STACK_<ID>_NAMES` | instance names, e.g. `db-1` |
| `LEMONDX_STACK_<ID>_IPS` | each instance's first IPv4 address |
| `LEMONDX_STACK_<ID>_IPV6` | each instance's first global IPv6 address |
| `LEMONDX_STACK_<ID>_NODES` | the nodes the instances are on |

```sh
# in a module run by the "runners" launch
for ip in $LEMONDX_STACK_APP_IPS; do
  echo "server $ip:8080" >> /etc/loadtest/targets
done
```

**In a parameter.** A launch step's parameter overrides may contain
`{{step.field}}`, filled in when that step starts. This way a module that has
never heard of stacks can still be pointed at an earlier launch, for example
by setting its `DB_HOST` parameter to `{{db.ip}}`. In the designer, typing `{{`
in a value offers the list.

| Placeholder | Becomes |
| --- | --- |
| `{{db.ip}}` / `{{db.name}}` | the first instance's IPv4 address / name |
| `{{app.ips}}` / `{{app.names}}` | every instance's, comma-separated |
| `{{app.ipv6}}` / `{{app.nodes}}` | the same for IPv6 addresses and nodes |
| `{{app.count}}` | how many instances it made |

### What a parameter's value ends up being

A step's `params` are *overrides*, so three things are consulted in turn and the
first that has an answer wins:

1. **the step's own `params`** — what the stack sets for this launch, including
   any `{{…}}` resolved at the moment the step starts;
2. **the template's saved parameters** — what the template was given when it was
   saved, which is every non-secret parameter its modules declare;
3. **the module's own default** — the value in its `# param:` header, or the
   default you saved for that module.

So a stack only has to say what is different about *this* launch. Launching the
same template from two steps with different `DB_NAME` values is two overrides,
not two templates. A secret is the exception and has no step 2 or 3: it is never
stored anywhere, so it is either `{{params.NAME}}` or asked for at launch.

**Values entered at launch.** `{{params.NAME}}` is whatever was entered for
`NAME` when the stack was launched. The launch dialog asks for every name the
stack's steps use this way; the CLI reads an environment variable of that name,
or prompts without echo. This is how one database password is typed once and
handed to every step that needs it, even when the app's module calls it
something else:

```json
{"id": "app", "type": "launch", "template": "App", "count": 3,
 "params": {"DB_HOST": "{{db.ip}}", "APP_DB_PASSWORD": "{{params.DB_PASSWORD}}"}}
```

It is also the only thing a step may set a *secret* parameter to. What is saved
is the placeholder, never the value, and a module that declared the parameter
`secret:` still has it redacted from its output. Every value entered at launch
is also in every launch's module environment under its own name.

A reference must name a launch in an *earlier* stage. A step in the same stage
is running at the same time and has made nothing yet. An instance that is
running but has no IPv4 address after a minute is passed on by name only.

## Health gates

**Wait until healthy** passes once every instance launched by an earlier stage
is green on the node it lives on. That means its own [health check](web-ui.md#health-checks)
says `healthy`, and its template's [app check](templates.md#app-health-checks),
if it has one, has answered `ok` rather than still being pending. If some are
not green by the timeout, the step fails and names each one with its reason.

The health monitor must be on for each node involved. On a remote node with
checks off, the step fails straight away rather than waiting out its timeout.
Where no monitor runs in the process running the stack (the CLI, or `serve` with
checks off), it runs its own checks on just those instances.

## When something fails

Before anything is launched, the stack is checked the way a template launch is.
The check covers templates that no longer exist, missing secrets, deleted
modules, unknown nodes or groups, and templates already busy with another run.
Any of these refuses the whole stack.

Once it is running, a failed step means no later stage starts. Steps already
running in the same stage finish, and instances that were launched are kept,
as they are when a template launch fails. The run records which step failed and
why. Only one run of a stack happens at a time.

## Secrets

Secrets are never saved, in a stack or in a template. A step can set a secret
parameter only to `{{params.NAME}}`; a literal value is refused on save. When the
stack is launched, the UI asks once for every secret its templates need that no
step fills in, plus every `{{params.NAME}}`. Each launch that needs a value gets
the same one, and the launch dialog names the step parameters each value fills,
so it is clear what a password is about to be used for. The CLI takes them from
environment variables of the same name, or prompts, as `launch` does.

## Clusters

A launch step can name nodes or a node group, like the Launch dialog. Instances
go round robin over them, and names are numbered across the whole cluster. The
stack runs on the node you started it from, and each launch step is an ordinary
cluster launch, so templates are pushed to a node before it is asked to launch
one.

Stacks are kept level across a cluster like templates: saving one pushes it,
**with the templates it launches**, to every member. Deleting one removes it
everywhere unless you pass `--local-only` or untick the box. `lemondx cluster
sync --kind stacks` catches up a node that was down.

## From the CLI

A stack is saved from a JSON definition, which `stack-show` prints back:

```bash
./lemondx stack-save "Load test" loadtest.json
./lemondx stacks
./lemondx stack-show "Load test" > loadtest.json    # edit, then save again
./lemondx stack-launch "Load test"                  # prints each step as it goes
./lemondx stack-delete "Load test"
```

`stack-launch` waits for the whole stack and exits 1 unless every step
finished. Ctrl-C cancels it the way the UI's Cancel does. `--param KEY=VALUE`
supplies a secret, or a value every launch gets.

```json
{
  "description": "one database, three app servers, six load runners",
  "stages": [
    {"steps": [{"id": "db", "type": "launch", "template": "Postgres", "count": 1}]},
    {"steps": [{"id": "app", "type": "launch", "template": "App", "count": 3,
                "params": {"DB_HOST": "{{db.ip}}"}}]},
    {"steps": [{"id": "ready", "type": "wait_healthy", "timeout_seconds": 600}]},
    {"steps": [{"id": "runners", "type": "launch", "template": "Runner", "count": 6,
                "wait_bootstrap": false, "params": {"TARGETS": "{{app.ips}}"}},
               {"id": "settle", "type": "sleep", "seconds": 30}]}
  ]
}
```

A launch step also takes `prefix` (instance names; default the template's),
`nodes` and `groups`. A step without an `id` gets `step<n>`. Ids are lowercase
letters, digits and `_`, and must be unique in the stack. A bare list of stages
is accepted in place of the object.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/stacks` | every stack |
| `PUT` | `/api/stacks/{name}` | `{"description", "stages"}`: create or replace one |
| `DELETE` | `/api/stacks/{name}` | delete one (`?everywhere=false` keeps other nodes' copies) |
| `POST` | `/api/stacks/{name}/launch` | `{"params": {...}, "background": true}`: run it; `"replace": [{"node","name"}...]` destroys those, as confirmed, first (a relaunch) |
| `GET` | `/api/stack-instances` | `{"stacks": {name: [{"node","name","status","template","ipv4","stale"}]}, "errors"}`, by the instances' tags on every node |
| `POST` | `/api/stacks/{name}/state` | `{"action": "start"\|"stop"\|"restart", "instances": [...]}` |
| `POST` | `/api/stacks/{name}/destroy` | `{"instances": [...], "background": true}`: stop and delete them, as a run |
| `GET` | `/api/stack-runs` | runs in progress, or last finished, per stack |
| `POST` | `/api/stack-runs/{name}/cancel` | start nothing new |
| `DELETE` | `/api/stack-runs/{name}` | dismiss a finished run |

`GET /api/stacks` adds `inputs` to each stack: the names its steps use as
`{{params.NAME}}`, which a launch must be given. Both it and
`GET /api/stacks/{name}` drop any step parameter that is a secret of that
step's template and holds something other than `{{params.NAME}}`: saving one is
refused, so this only shows up in a file edited or copied in by hand, and such
a stack launches with that parameter unset rather than with the value on disk. A run record is `{"stack", "action"
(launch, relaunch or destroy), "started_at", "finished_at", "ok", "error",
"cancelling", "cancelled", "stages": [{"steps": [...]}]}`. Each step carries
`{"id", "type", "state", "started_at", "finished_at", "until", "detail",
"error"}`, where `until` is when a sleep ends or a health wait gives up. A
launch step also carries `template`, `count`, `nodes`, `instances` (`[{"name",
"node", "ok", "error", "ipv4"}]`) and `outputs` (`{"names", "ips", "ipv6",
"nodes"}`, what later steps are handed). An instance's own address is its
`ipv4`: `outputs` holds values for placeholders, so `ips` leaves out an
instance that has none and `nodes` names each node once, and neither lines up
with `names` position by position. Like template runs, these are held by the `serve`
process: the CLI in another process neither sees them nor is blocked by them.
Without `background` the launch blocks until the stack has finished and returns
the final record.

## On disk

One JSON file per stack in `~/.local/share/lemondx/stacks/`, named after a slug
of its name, in the format above. Copy them between machines or check them into
a project, as with templates.

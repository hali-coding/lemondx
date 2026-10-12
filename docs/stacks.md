# Stacks

[← back to README](../README.md)

A stack is how lemondx describes a deployment, and the thing it is built to
run. It launches several [templates](templates.md) in a fixed order: one
database server, then three app servers pointed at it, then six load-test
runners pointed at those. Later launches are handed the names and addresses of
what earlier ones made, so nothing has to be copied by hand between steps, and
health gates hold a stage back until what it depends on is actually up.

Once launched, the stack is operated as a whole: stopped, started, relaunched
and destroyed together, marked stale when its definition changes, and followed
in the web UI, the CLI and `lemondx top`. Templates and modules are its parts;
launching a template on its own is for a single tier with nothing to wire up.

Stacks are meant to span **several nodes**: each launch step can name nodes or
a node group, and a [fabric](networking.md) lets the tiers reach each other
across hosts. That is the recommended deployment — see [Clusters](#clusters).

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
  without dragging. **Variables & secrets** at the top opens it on the
  stack's [variables and secrets](#variables-and-secrets) instead. With no
  step chosen and nothing unsaved, the pane is closed and the canvas has the
  room.

Edits show in the drawing as you make them, but nothing is kept until the
stack is saved. The foot of the right-hand pane says which it is: **Unsaved
changes** or **All changes saved**. **Save & close** there saves the stack and
closes the pane, leaving the designer open to carry on -- or, when it cannot
save yet (no name, or a step marked in red), says why in the same place and
takes you to the name if that is what is missing; **Close** only closes
the pane, and the changes stay in the drawing, unsaved. **Save stack** at the
top saves and leaves the designer.

Problems show on the card in red before you can save: a template that does not
exist, the same template twice in one stage (a template runs one launch at a
time), a health wait with nothing launched before it, or a reference to a step
that is not an earlier launch.

### Setting a launch step's parameters

The **Parameters** panel lists every parameter the step's template actually
declares, grouped by the module that declares it, so what you are looking at is
the whole set the launch will run with rather than only what you have changed.
A module the template adds more than once gets a group per copy, numbered, and
the second copy's parameters are named `NAME@2` (see
[Adding a module more than once](modules.md#adding-a-module-more-than-once)).

Each box shows, greyed out, the value the launch would use if you left it
alone — the template's own answer, or failing that the module's default — with
a line underneath saying which of the two it is. Type over it and the name turns
gold with a **set here** badge: that parameter is now the step's, and only what
you set is saved in the stack. Empty the box again and it goes back to
inheriting. A parameter no module declares can be added at the bottom, under
**Anything else**, for a module that reads something straight out of the
environment.

**What a launch hands on is shown under its card**, under *hands on →*: what
it made (`ip`, `ips`, `name`, `names`, `ipv6`, `nodes`, `count`) and, in blue,
the parameters it runs with, under the name of the module that declares them
(*PostgreSQL*: `POSTGRES_DB` `POSTGRES_USER`). A dropped parameter becomes
`{{db.param.POSTGRES_DB}}`: the module is only shown, not part of the
reference, because a template's parameters are one set of names. **Drag a chip onto a
parameter of a step in a later stage** and it becomes `{{db.ip}}` where you let
go. While you drag, the steps that can take it are outlined and the rest fade:
a value only flows down the stack. Hold the chip over a later step for a moment
and it opens in the inspector, with its parameter boxes ready for the drop. The
inspector also lists everything available to the selected step under
**Available to this step**. You can drag from there too, or click a box and
then click a chip to insert it at the caret.

A step that takes something shows it on its card as `← db  ip, POSTGRES_DB`,
and each box that holds a reference says underneath where it comes from
(`← db.ip · first instance's IPv4 address · stage 1`). Pointing at either end
lights up both cards.

**Typing works too.** `{{` in any value opens the list of everything it can
refer to, with a line saying what each becomes, filtered as you type. A bare
reference completes with **Tab** as in a shell: `db.i` then Tab is `{{db.ip}}`.
When there are several candidates the first Tab shows them. Arrow keys move,
Enter or Tab accepts, and the braces are written for you. A reference that names
nothing an earlier launch hands on is marked on the field as you type, rather
than waiting for the save to be refused.

A **secret** parameter is marked as such and left empty means *asked for when the
stack is launched*. The only things it may be set to are one of the stack's
own secrets, `{{secrets.NAME}}`, or `{{params.NAME}}` — a literal is refused,
on the field and again on save, because a stack file is saved to disk and
synced to every node. See [Variables and secrets](#variables-and-secrets) and
[Values entered at launch](#values-entered-at-launch).

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
the rest. Both dialogs say so and list what would be left. Deleting offers
**Also destroy its instances**, off by default: ticked, the instances listed are
stopped and deleted first, and the stack goes only once they have (if the
destroy fails, the stack is kept so what is left can still be found by name).
For a rename, destroy them first if they should go with the name. This is the
template tag's behaviour too, and for the same reason: the instance is the
record.

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
| `{{db.param.POSTGRES_DB}}` | the value that step's `POSTGRES_DB` launched with |

**A parameter of an earlier launch.** `{{db.param.NAME}}` is what `NAME` was
for that launch. It is resolved as in
[What a parameter's value ends up being](#what-a-parameters-value-ends-up-being):
the step's own value, else the template's, else the module's default. So the
database's name is said once, on the database step, and an app server's
`DB_NAME` follows it. A repeated module's parameter is `NAME@2` and so on. Any
parameter the template's modules declare can be named, and so can any the step
sets. A **secret** one cannot: handing it on would give a secret to a module
that may not declare it secret, and so would not redact it. Share a secret with
`{{params.NAME}}` in both steps instead.

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

## Script steps

**Run a script** runs a shell script on the node driving the stack -- the one
it was launched from -- as the account lemondx runs as: a webhook to call once
the addresses are known, a DNS record to add, an external system to tell. It is
the one step that runs on a host rather than in an instance. The default script
is `exit 0`, which does nothing.

- It runs with `/bin/sh`, in an empty directory of its own under the data
  directory, removed afterwards. The script is written to a file there rather
  than passed on a command line, so a secret filled into it is not visible to
  other users through `ps`.
- It has what a launch step has. **Parameters** work exactly like a launch's --
  any value, holding any reference from the same chips: an earlier launch's
  `{{db.ip}}` or `{{db.param.POSTGRES_DB}}`, a stack variable, a stack secret, a
  value entered at launch -- and each is handed to the script as an environment
  variable of that name. The script also gets every value entered at launch
  under its own name, `LEMONDX_STACK_<ID>_IPS`/`_NAMES`/`_IPV6`/`_NODES` for each
  earlier launch, and `LEMONDX_STACK_STEP`. References can also be written
  straight into the script, where they are filled in as text first -- quote them
  (`"{{vars.NFS_SERVER}}"`). A secret is better passed as a parameter, so it never
  sits in the script's text.
- A non-zero exit fails the step, and the stack, as any failed step does: no
  further stage starts. So does running past **Give up after** (default 300 s,
  up to a day), when the script and everything it started are sent `TERM`, then
  `KILL` five seconds later. Cancelling the stack stops it the same way.
- Its output (stdout and stderr together, the last 16 KiB) is kept on the run
  and shown under the step, with the stack's secrets and every value entered at
  launch masked. The event log records each run's stack, step, exit code and
  time -- never the script.

A stack is written by an admin and may be launched by an operator, so whoever
can launch a stack can run its scripts, as written. Stacks sync to every
member, and a script runs on whichever node the stack is launched from.

## Swarms: load tests against a running stack

Running load against a deployment is what stacks are mostly for, and the load
generators are not part of the deployment: they come and go, and in very
different numbers from one test to the next. So a stack can carry **swarms**:
named load tests, designed with the stack, that its launch never starts. One
is started on demand while the stack runs, and its instances are destroyed
when it ends.

**Designing one.** In the designer, **+ Swarm** above the canvas adds one, and
the tabs there switch between the stack and each swarm. A swarm's canvas works
like the stack's (stages, steps side by side, the same step types), with two
differences:

- **The whole stack has run before it.** Every launch of the stack is an
  earlier launch to every step of the swarm, so its chips are available as they
  are to a later stage. A load generator's `TARGETS` can be `{{app.ips}}`, and a
  script can ping `{{db.ip}}`. The stack's variables and secrets work too. A
  swarm step may not reuse a stack step's id, since a reference names a step by
  id.
- **More instances.** A swarm launch step can make up to **200** instances, not
  20. The count in the design is the default, and it can be changed each time
  the swarm is started.
- **A share may fail.** **Must start** on a launch step (`min_percent`, any
  launch step, not only a swarm's) is the share of its instances that must come
  up. At 100, the default, one failure fails the step. At 90, a swarm of 200
  carries on with 180, and the failures are noted on the step. Only what came
  up is handed on.

A typical swarm is two stages: launch the generators, then a **Run a script**
or **Sleep** step that *is* the test. The swarm ends when its last stage does.

**Results.** A generator can also run its test in its own bootstrap module and
print the result as its last line ("4772 ok, 0 failed"). Each instance's last
line is kept on the swarm's run, before the instance is destroyed: under
**results** on the Stacks tab, and beneath the step in `stack-swarm`'s output.
A module that succeeds keeps its last three lines (300 bytes) on its run record
for this; a failed one keeps its tail, and a failed instance's row in a stack
run says the module, its exit code and its last line of stderr.

**Running one.** On the Stacks tab, each stack lists its swarms. **Swarm** is
enabled once the stack has instances. It asks for:

- how many of each generator, up to 200 per step;
- the stack's variables, for this run only;
- any value the swarm's steps ask for as `{{params.NAME}}`;
- any stack secret still without a value.

The swarm then runs as the stack's run (the stack's own launch, relaunch and
destroy wait for it):

1. The stack's addresses are worked out from its running instances.
2. The swarm's stages run.
3. **Its instances are destroyed**, whether the last stage finished, a step
   failed, or you pressed **Stop**. Stop takes effect at once: a launch still
   bootstrapping (often the test itself) is not waited out. The teardown
   destroys what exists and keeps sweeping until that launch has finished
   creating instances.

A step the swarm refers to that has no *running* instances refuses the start
("start or launch the stack first"), as does one asked for an address that
has none yet. A stopped stack still carries its tags, but has no addresses to
point load at.

**In sequence.** A stack with more than one swarm has a **sequence**: some of
its swarms, run one after another. For example, a functional test first, then
the load test only once the functional test has passed. The sequence is part
of the stack and is set in the designer's **Sequence** tab, which draws it as
a flow:

- Each swarm has a switch. **On** puts it in the sequence; **off** leaves it
  out but keeps its place, so switching it back on puts it where it was. An
  off swarm still runs on its own from the Stacks tab.
- The arrows move a swarm up or down. The order is the swarms' own order, so
  the designer's swarm tabs follow it too.

A swarm's header in the designer says where it stands (`Sequence: #2`, or
`Not in the sequence`) and opens the Sequence tab. New swarms start switched
on, and so does every swarm of a stack saved before sequences existed.

On the Stacks tab, the stack's card shows the sequence above its swarms as a
numbered strip (`1 smoke → 2 heavy`) with **Run sequence**. Every swarm row
carries its number, or `disabled · not in sequence`, and its own **Run**.
**Disable** and **Enable** on a row flip the same switch as the designer
without opening it (admin). They save the stack, which syncs like any save. Run sequence
asks only for counts (per swarm), variables, inputs and missing secrets. It
lays the sequence out as a timeline that says what happens after each swarm.

A sequence is one run of the stack:

1. Everything is checked for every swarm before the first one starts: inputs,
   templates, and what each points at. So a load test is never refused after
   its functional test has already run.
2. Each swarm runs its stages and then **its own teardown** before the next
   one starts. Each swarm starts against the stack alone, and its references
   to its own steps never see an earlier swarm's instances, even when two
   swarms share a step id.
3. **A failure ends the sequence.** That swarm is still torn down, and every
   swarm after it is skipped. "Passed" means every step finished. For a test
   that is a script step's exit code, or a launch step whose modules failed
   (below its `min_percent`). A launch that does not wait for bootstrap
   reports its result later. The next swarm starts only once every such
   launch of the one before has reported, so a late failure still stops the
   sequence.

**A failure stops the swarm at once.** Once any step of a swarm has failed,
whatever is still going stops instead of running to its end: a sleep, a
script, a health wait, or a wait on a launch. Those steps are marked stopped
with the reason (`Stopped: step gen failed.`), the run counts as failed, not
cancelled, and the teardown runs. A stack's own launch keeps going after a
failure as before, because what it launches is the deployment.

**Stop sequence** ends the whole sequence: the running swarm is torn down, and
the rest are skipped. While a sequence runs, and until its run is dismissed,
the strip marks each swarm `passed`, `running`, `queued`, `failed` or
`skipped`, and the run's steps are grouped by swarm.

**One at a time, cluster-wide.** A run lives on the node that started it, so
before a stack launches, relaunches, is destroyed, or swarms, every other
member is asked whether it is running that stack. One that is refuses the
start ("busy with a swarm heavy on prdev1"), and the preview says so. A member
that does not answer is passed over. **Stop** works from any node: a swarm
running on another member is stopped there. Each run of a swarm also tags its
instances with its own run (`user.lemondx.swarm-run`), and its teardown takes
only those, so one run can never destroy another's generators.

From the CLI:

```bash
./lemondx stack-swarm "Load test" heavy --count runners=150 --dry-run   # preview
./lemondx stack-swarm "Load test" heavy --count runners=150 --var DURATION=600
./lemondx stack-swarm-stop "Load test" heavy
./lemondx stack-swarm "Load test" --sequence                     # the stack's sequence
./lemondx stack-swarm "Load test" smoke heavy --count heavy/runners=150  # these, in order
```

`stack-swarm` hands the run to `serve` when one is running here, like
`stack-launch`. Ctrl-C there stops the swarm, and so tears it down. Given
several swarms, it runs them as a sequence in the order given. `--sequence`
runs the stack's own sequence, and `stack-show` prints it.
`stack-swarm-sequence NAME SWARM on|off` puts a swarm in or out of it. `--count STEP=N` is enough when
only one of them has that step; otherwise write `SWARM/STEP=N`.
`stack-swarm-stop` with any swarm of a running sequence stops all of it.
`stack-swarm-stop` follows the stop until the instances are gone.

**Reports.** When a swarm or a sequence has finished, **Export report** beside
the run on the Stacks tab downloads it as Markdown. The report has:

- the result, start and finish times, duration, and the node it ran on;
- the stack instances and addresses it was pointed at, and the variables it
  ran with;
- for a sequence, a table of each swarm's result and duration;
- every step with its state, timing, error and the instances it launched;
- what each load generator's modules said last, and each script's exit code
  and output.

It is built from the run record, so it holds nothing the tab does not show.
Outputs are already redacted of the run's secrets and launch inputs. From the
CLI, `stack-swarm ... --report FILE` writes it when the run ends (a directory
names the file for the run, `-` is stdout). `lemondx stack-report NAME [-o
FILE]` prints the last finished run's report: the run held by `serve` here,
like everything the tab shows. The API route is `GET
/api/stack-runs/{name}/report` → `{"stack", "filename", "markdown"}`. It is
refused with 409 while the run is still going.

**When `serve` stops.** A `serve` stopped by its service manager (SIGTERM, as
`systemctl restart` sends) stops every swarm it is running and gives each up to
a minute to tear down before it exits. Anything else it was running is left,
and said in its log.

**How it knows the stack's addresses.** The launch that made the stack's
instances may be days old, and its run only ever lived in memory. So every
instance a stack launches is tagged with its stack **step**
(`user.lemondx.stack-step`), and a swarm's also with the swarm
(`user.lemondx.swarm`). A swarm rebuilds `{{db.ip}}`, `{{app.ips}}` and
`{{app.param.NAME}}` from the instances as they are now, using the fabric
address where there is one, exactly as a launch would have handed them on.
Instances launched before steps were tagged are matched by template, where
only one step of the stack launches it. A stack step's parameter that came from
`{{params.NAME}}` at the stack's launch is not known later; to hand one to a
swarm, keep it as a stack variable or secret.

**Ephemeral.** Swarm templates are expected to be ephemeral. One that is not
is allowed, and noted on save, in the designer and when starting. Its
instances are destroyed when the swarm ends all the same. But if lemondx stops
mid-swarm without a chance to tear down (killed, or the host lost power), the
instances stay, tagged with their swarm. The Stacks tab lists them as **left
behind** with a **Clear** button. `lemondx stacks` shows them as `heavy (6
left)` and says which `stack-swarm-stop` clears them, and `stack-show` notes
them under the swarm. Clearing destroys exactly those, after confirmation.

Swarm instances carry the stack's tag too, so destroying or relaunching the
stack takes them with it. A swarm does not count towards the stack's revision:
editing a load test never marks the stack's instances stale.

## Health gates

**Wait until healthy** passes once every instance launched by an earlier stage
is green on the node it lives on. That means its own [health check](web-ui.md#health-checks)
says `healthy`, and its template's [app check](templates.md#app-health-checks),
if it has one, has answered `ok` rather than still being pending. While it
waits, the step says which instances it is waiting on and why (the first two,
then a count), so a gate that can only time out -- an app check failing the
same way every round -- shows it from its first round. If some are not green
by the timeout, the step fails and names each one with its reason.

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
why, and for a launch each failed instance's module, exit code and last line of
stderr. Only one run of a stack happens at a time, across the whole cluster
(see [Swarms](#swarms-load-tests-against-a-running-stack)). A launch step with
**Must start** below 100% succeeds when enough of its instances come up, and
lists the rest in its notes.

## Variables and secrets

A stack can carry values of its own, so they are said once instead of typed at
every launch. Set them under **Variables & secrets** at the top of the
designer (also shown whenever no step is selected). Steps use them like
anything else: drag the chip from **This stack**, or type the reference.

**Variables** are plain values: an NFS server's name, a domain, a version.
`{{vars.NFS_SERVER}}` in a step's `NFS_SOURCE`, say, as `{{vars.NFS_SERVER}}:/srv`.
They are kept in the stack record, so anyone who can read the stack can read
them, and they sync with it. The launch dialog shows each one filled in, and a
change there applies to that launch only (`--var NAME=VALUE` on the CLI).

**Secrets** are values no one should read off the stack: a database password,
an API key. `{{secrets.DB_PASSWORD}}` in the database step's `POSTGRES_PASSWORD`
and in the app step's `DB_PASSWORD` gives both the same password without anyone
typing it at launch. What the stack keeps is only the name, a description, and
whether to generate it:

- **Set a value** in the designer (applied when the stack is saved), with
  `lemondx stack-secret-set STACK NAME` (from `$NAME`, a prompt, or stdin --
  never the command line), or by giving it the first time the stack is
  launched, when the launch dialog asks for any secret with no value.
- **Generate** instead: the first launch that needs it makes a random value
  (32 URL-safe characters), keeps it, and pushes it to every member before
  anything is launched, so every later launch -- from any node -- uses the same
  one.
- **Read it back**: **Reveal…** in the designer, for an admin. Signed in with a
  password, you type it again first; an admin API token is enough on its own.
  The value is shown for 30 seconds. `lemondx stack-secret-reveal STACK NAME` on
  the node itself needs no password: the CLI runs as the account that owns the
  data, which could read the key anyway. Every reveal is logged with who and
  which secret, never the value.

The value is never in the stack file, a listing, a run record or the event log.
It is encrypted on each node under `auth/` (see [Security](security.md#stack-secrets))
and travels between members only over the members-only sync route. Reveal is
refused through another node (`/api/nodes/{node}/…`): the relayed call would
arrive under the cluster credential and skip the password, and the secret is
on every node anyway.

**A stack secret named like a module's secret fills it everywhere.** A stack
secret called `POSTGRES_PASSWORD` goes to every launch whose modules declare a
`POSTGRES_PASSWORD` secret. So one generated password covers every PostgreSQL in
the stack, with no wiring and nothing asked at launch. It does not apply where
a step sets the parameter itself, or where a value of that name was typed at
launch, since both are someone saying what this one should get. In
particular, a step set to `{{params.POSTGRES_PASSWORD}}` still asks. The
designer shows a covered box as "the stack secret POSTGRES_PASSWORD".

One caveat: a module redacts only parameters **it** declares secret. Handing
`{{secrets.X}}` to a parameter its module does not declare secret is allowed --
a connection string is a fair place for a password -- but that module's
output is not scrubbed of it, and the designer and the save both say so.

Renaming a stack moves its secrets with it; deleting it deletes them on every
member. Removing a secret from the stack forgets its value.

## Values entered at launch

Module secrets are never saved in a template, and a stack sets one only to
`{{secrets.NAME}}` (kept, as above) or `{{params.NAME}}` (typed at launch); a
literal value is refused on save. When the stack is launched, the UI asks once
for every secret its templates need that no step fills in, plus every
`{{params.NAME}}`. Each launch that needs a value gets
the same one, and the launch dialog names the step parameters each value fills,
so it is clear what a password is about to be used for. The CLI takes them from
environment variables of the same name, or prompts, as `launch` does.

## Clusters

A stack on one host works, and is a fine way to develop one. A stack across
several is what lemondx is for: losing a host costs its share rather than the
deployment, tiers can be put where there is room (`lemondx cluster group auto`
sorts nodes into `large` and `small`), and one join code is all a new host
needs. For tiers to reach each other across hosts, put their templates on a
[fabric](networking.md) — a step's `{{db.ip}}` is then the fabric address,
which means something on every node. See
[the recommended deployment](../README.md#recommended-deployment-several-nodes).

A launch step can name nodes or a node group, like the Launch dialog. In a
cluster, a new launch step in the designer starts out on the first node group
in alphabetical order (with `lemondx cluster group auto`, that is `large`),
so a stack spreads over the cluster unless told otherwise. A single node, or a
cluster with no groups, starts it on this node. Either way it is only where the
step starts: the group is saved on the step and can be changed under
**Where**. Instances
go round robin over them, and names are numbered across the whole cluster. The
stack runs on the node you started it from, and each launch step is an ordinary
cluster launch, so templates are pushed to a node before it is asked to launch
one.

Stacks are kept level across a cluster like templates: saving one pushes it,
**with the templates it launches**, to every member. Its secret values follow
on their own route whenever one changes, and reconciliation brings a node that
was off up to date, so a generated password is the same everywhere. Deleting one removes it
everywhere unless you pass `--local-only` or untick the box. `lemondx cluster
sync --kind stacks` catches up a node that was down.

## From the CLI

A stack is saved from a JSON definition, which `stack-show --json` prints back:

```bash
./lemondx stack-save "Load test" loadtest.json
./lemondx stacks                                    # with its swarms and instances
./lemondx stack-show "Load test"                    # what each step and swarm does
./lemondx stack-show "Load test" --json > loadtest.json   # edit, then save again
./lemondx stack-launch "Load test" --dry-run        # what a launch would do; changes nothing
./lemondx stack-launch "Load test"                  # prints each step as it goes
./lemondx stack-launch "Load test" --var NFS_SERVER=nas2.lan   # this launch only
./lemondx stack-secrets "Load test"                 # which secrets hold a value
DB_PASSWORD=... ./lemondx stack-secret-set "Load test" DB_PASSWORD
./lemondx stack-secret-reveal "Load test" DB_PASSWORD
./lemondx stack-secret-clear "Load test" DB_PASSWORD
./lemondx stack-delete "Load test" -y              # its instances are kept
./lemondx stack-delete "Load test" --destroy        # destroys them too (asks)
```

`stack-show` describes the stack in words:
- its variables and secrets (whether each holds a value, never what);
- each stage and step: what it launches, how many and where, the image and
  modules, each parameter, and what it hands on;
- each script's lines;
- each swarm the same way, ending with its teardown.

`--json` prints the definition instead, as `stack-save` takes it.

**`--dry-run` previews a start without changing anything**, on `stack-launch`
and on `stack-swarm`. It runs every check a start runs (templates, secrets,
module secrets, maintenance, a template busy elsewhere, a stack already
running) and lists what would stop it, rather than stopping at the first. Each
step then shows:
- the nodes it lands on and how many go to each, round robin;
- each parameter with whatever is known now filled in, and where each
  reference comes from. Stack variables are filled in and secrets show as
  `********`. A reference to an earlier launch says it is known "once stage N
  has run".

A swarm's preview is against the stack as it runs, so `{{target.ip}}` is the
real address, in its parameters and in its script. The command exits 1 if the
start would be refused. `--json` prints the same preview as
`POST /api/stacks/{name}/preview` and `.../swarms/{swarm}/preview` return.

`lemondx top` shows each instance's stack and the step that made it
(`shop·db`), or its swarm (`shop ⚡heavy`). The stacks panel names a running
swarm and counts the stack's own instances apart from its load generators.

`stack-launch` waits for the whole stack and exits 1 unless every step
finished. When `serve` runs on the node, the run is handed to it and the
command only follows it: the Stacks tab shows it too, and it carries on if
the terminal or ssh session goes. Ctrl-C cancels it the way the UI's Cancel
does; a second Ctrl-C stops waiting, and launches already under way finish on
`serve`. `--no-serve` runs it in the command instead, as does a `serve` on
another daemon or project than the command's (`--socket`, `--project`); there
a second Ctrl-C leaves them cut off. A value a step asks for with `{{params.NAME}}` is read
from an environment variable of that name, or prompted for, so a password
need not be on the command line; `--param KEY=VALUE` supplies it too, or a
value every launch gets.

Saving names any group or node a step launches on that does not exist here,
as a warning (the UI shows it after saving). The stack is saved regardless,
since a member may still be joining or a group be made afterwards. A template
it names must exist. Deleting a stack asks first; `-y` skips the question.

```json
{
  "description": "one database, three app servers, six load runners",
  "variables": {"NFS_SERVER": {"value": "nas.lan", "description": "the NAS"}},
  "secrets": {"DB_PASSWORD": {"description": "postgres", "generate": true}},
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
`nodes`, `groups` and `min_percent` (1-100, default 100: the share of its
instances that must come up for the step to succeed). A step without an `id` gets `step<n>`. Ids are lowercase
letters, digits and `_`, and must be unique in the stack; `params`, `vars` and
`secrets` are reserved. A bare list of stages is accepted in place of the
object. `variables` may give a bare string as the value. A secret's entry
holding a `value` is refused: values are set on their own route.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/stacks` | every stack |
| `PUT` | `/api/stacks/{name}` | `{"description", "stages", "variables", "secrets", "swarms"}`: create or replace one; `"previous": "<old name>"` on a rename moves its secret values. `swarms` is `[{"name", "description", "stages", "sequence"}]`, in order. `sequence` (default true) puts the swarm in the stack's sequence |
| `DELETE` | `/api/stacks/{name}` | delete one (`?everywhere=false` keeps other nodes' copies); a body of `{"instances": [{"node","name"}...]}`, exactly the stack's instances as confirmed, destroys them first → `{"deleted", "destroyed"}` |
| `POST` | `/api/stacks/{name}/launch` | `{"params": {...}, "background": true}`: run it; `"replace": [{"node","name"}...]` destroys those, as confirmed, first (a relaunch); `"variables": {...}` overrides for this launch; `"secrets": {...}` gives (and keeps) secrets that hold no value (operator) |
| `POST` | `/api/stacks/{name}/preview` | `{"params", "variables", "secrets"}` → what a launch would do, changing nothing: `{"ok", "problems", "stages": [...each step with "placement" {node: n} and "params" [{"name", "value", "resolved", "refs": [{"ref", "source", "known"}]}]]}`; secrets never filled in (operator) |
| `POST` | `/api/stacks/{name}/swarms/{swarm}/preview` | the same for a swarm, with `"counts"`, its references resolved against the running stack, and `"against"` naming it (operator) |
| `POST` | `/api/stacks/{name}/swarms/{swarm}/start` | `{"counts": {step: n}, "variables", "params", "secrets", "background": true}` → the run (action `swarm`) (operator) |
| `GET` | `/api/stack-runs/{name}/report` | the stack's last run, finished, as `{"stack", "filename", "markdown"}`; 409 while it runs (read) |
| `PUT` | `/api/stacks/{name}/swarms/{swarm}/sequence` | `{"enabled": bool}`: put the swarm in or out of the stack's sequence, as a save of the stack (synced; answers like one) (admin) |
| `POST` | `/api/stacks/{name}/swarm-sequence` | `{"swarms": [names, in order]` (left out: the stack's sequence)`, "counts": {swarm: {step: n}}, "variables", "params", "secrets", "background": true}` → one run (action `swarm`) that runs each swarm and its teardown in turn. A failure skips the rest. The run's `swarms` lists the sequence, `swarm` is the one running now, and each stage carries its `swarm` (operator) |
| `POST` | `/api/stacks/{name}/swarms/{swarm}/stop` | stop the swarm if it is running (it then tears itself down); otherwise `{"instances": [...]}`, exactly what it left behind, destroys those (operator) |
| `GET` | `/api/stacks/{name}/secrets` | `{NAME: {"description", "generate", "set", "generated", "updated_at"}}` -- never a value (read) |
| `PUT` | `/api/stacks/{name}/secrets/{secret}` | `{"value"}`: set one (admin) |
| `DELETE` | `/api/stacks/{name}/secrets/{secret}` | forget its value (admin) |
| `POST` | `/api/stacks/{name}/secrets/{secret}/reveal` | `{"password"}` → `{"name", "value", "generated", "updated_at"}`: admin; a password session must send its password, a token needs none; logged; refused when relayed through another node |
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
of its name, in the format above. Secret values are not in it: they are in
`auth/stack-secrets.json`, encrypted under `auth/secrets.key`. Copy them between machines or check them into
a project, as with templates.

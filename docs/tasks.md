# Tasks

[← back to README](../README.md)

A **task** runs bootstrap modules in a throwaway instance, on a schedule or
when invoked, and destroys the instance when the run ends. It is cron without
a host that owns the job, and Lambda without the cloud. No node owns a task.
For every run the cluster decides which member runs it, so losing a node
loses nothing but that node.

A task is made of parts lemondx already has:

- **A [template](templates.md)**: image, size, network, fabric, and the
  template's own modules. Its setup is whatever the template does.
- **The job**: either ordinary [modules](modules.md) or a **script pasted
  into the task**, run as a second pass once the template's modules are done.
  The job's exit code is the run's. See [a pasted script](#a-pasted-script).
- **Where it runs**: one or more **node groups**, which are required. Each run
  goes to one member of them, chosen by the cluster.
- **When it runs**: a cron schedule, when invoked, or both.
- **Inputs**: values a caller hands a run, read when it starts.
- **Secrets**: kept sealed on every member and handed to the job.

```bash
./lemondx task-save nightly-dump -t "Debian" -b pg-dump --group workers \
    --cron "0 2 * * *" --tz Europe/Stockholm \
    --secret DB_PASSWORD --param 'PGPASSWORD={{secrets.DB_PASSWORD}}'
./lemondx task-secret-set nightly-dump DB_PASSWORD      # from $DB_PASSWORD or a prompt
./lemondx task-run nightly-dump                          # run it now and wait
./lemondx task-runs nightly-dump                         # every member's runs
```

## Where it runs

A task is placed on **node groups**, never on a node alone and never on
"anywhere". A task tied to one node stops when that node does, which is the
single owner tasks exist to avoid. A group, by contrast, can gain a member
when the one it has is gone. `large` and `small` (the cluster's own sizing)
are groups too. So is a group of one node, which is how to pin a task to one
host if you really mean to.

- Saving a task without a group is refused. A group that does not exist here
  is refused too. A group with no members is saved with a warning.
- Nodes can be named **beside** the groups, as additions. Naming a node never
  replaces a group.
- The task's members are its groups' members plus any nodes named beside
  them, as they are now. Changing a group changes where its tasks run, without
  editing the tasks.
- A task with no members runs nowhere. The listing says why
  (`placement_problem`).
- Deleting a group a task uses is refused (`409`).
- An invocation's `node` must be one of the task's members.

In the web UI, **Runs on** is a type-to-search box. Groups come first, each
showing its members, and nodes come after. The run dialog's **Node** box
completes from the task's own members.

## A run

1. A member takes the run (see [who runs it](#who-runs-a-scheduled-run)).
2. It launches one instance of the template, named `task-<task>-<id>`. The
   instance is always ephemeral, and is tagged `user.lemondx.task` and
   `user.lemondx.task-run` but *not* with the template. So the template's
   Recreate, Destroy and app check never take in an instance that exists for
   one run.
3. The template's own modules run, then the job's. Each job module gets these
   environment variables:

   | Variable | Value |
   | --- | --- |
   | `LEMONDX_TASK`, `LEMONDX_TASK_RUN` | the task and this run's id |
   | `LEMONDX_TASK_TRIGGER` | `schedule` or `invoke` |
   | `LEMONDX_TASK_SLOT` | for a scheduled run, the occurrence it is for (epoch seconds) |
   | `LEMONDX_TASK_NODE` | the member running it |
   | `LEMONDX_INPUT_<NAME>` | every declared input, given or defaulted |
   | `LEMONDX_TASK_PAYLOAD` | the path of the payload file, when the run was given one |

4. The instance is destroyed, however the run ended.

**Timeout** (`timeout_seconds`, default 900, 60 s to a day) covers the whole
run, from creating the instance to destroying it. A run that overstays has
its instance deleted, which ends everything in it, and is marked
`timed_out`. Cancelling does the same.

**Overlap**: with `skip` (the default), a run is not started while another
run of the same task is going on any member. With `allow`, runs may overlap.

**History.** Each member keeps the last 50 runs of each task it ran, on disk
(`task-runs/`), with the job's output: the last 16 KiB of each stream, with
secrets masked. The list and the CLI ask every member, so the history
survives a restart and shows the whole cluster. A run that was going when
`serve` stopped is marked `lost`, and its instance is destroyed by the
sweeper. The sweeper also runs when `serve` starts and every five minutes
after, and takes any task instance on its node that no run owns.

The event log has `task.start` and `task.finish` for every run, with status,
exit code and duration. It also has `task.missed`, `task.veto`, `task.skip`
and `task.sweep`. These reach your [remote syslog](logging.md), which is the
place to alert from.

## A pasted script

A job that is a few lines of shell does not need to be a module first.
Paste the script into the task instead, using **Paste a script** in the
editor or `--script FILE` (or `-`) on `task-save`. A job is modules *or* a
script, never both.

- **Without a `#!` line**, the script is POSIX `sh` with the module prelude
  in front, so it can use `log`, `die`, `have`, `pkg_install` and
  `svc_enable`, and it runs under `set -eu` like a module. Its syntax is
  checked on save.
- **With one** (`#!/usr/bin/env python3`), that interpreter runs it as
  written, without the prelude. The interpreter must exist in the image,
  either from the template's own modules or from the image itself.
- **Its environment** carries everything a job is given: `LEMONDX_TASK_*`,
  `LEMONDX_INPUT_<NAME>` for each input, `LEMONDX_TASK_PAYLOAD`, and
  `LEMONDX_SECRET_<NAME>` for each task secret. Secret values are masked in
  the output.
- **No placeholders.** Nothing is filled into the script's text, so a secret
  never sits in a file in the instance. Every declared secret must hold a
  value, since nothing says which ones the script reads.
- **Size:** at most 64 KiB. It is stored in the task record and synced with
  it.

```bash
./lemondx task-save ping-hook -t Debian --group workers --secret API_KEY --script hook.sh
```

where `hook.sh` is:

```sh
pkg_install curl
curl -fsS -H "Authorization: Bearer $LEMONDX_SECRET_API_KEY" \
     --data @"${LEMONDX_TASK_PAYLOAD:-/dev/null}" https://example.com/hook
```

## Inputs and payloads

An invocation can carry inputs, the way a message to a function does. A task
declares each one:

| Field | Meaning |
| --- | --- |
| `default` | used when a run is not given it, and by every scheduled run |
| `required` | an invocation without it is refused (`400`) |
| `secret` | masked in all output, never kept on the run record; cannot have a default |
| `description` | shown in the run dialog and on `task-show` |

A job sees every input as `LEMONDX_INPUT_<NAME>`, and a job parameter can use
one by name: `--param 'TARGET={{inputs.TARGET}}'`. A module's *secret*
parameter can only be set to a reference: `{{secrets.NAME}}` or a secret
input's `{{inputs.NAME}}`, never a value.

A **payload** is a whole message, as JSON or text, up to 256 KiB. The job
gets it as a file whose path is in `$LEMONDX_TASK_PAYLOAD`. It is never put in
the environment, the event log or the run record. Only its size is kept.

```bash
echo '{"order": 42, "status": "shipped"}' | \
    ./lemondx task-run notify --input CHANNEL=ops --payload -
curl -X POST https://node:8099/api/tasks/notify/invoke \
    -H "Authorization: Bearer $TOKEN" \
    -d '{"inputs": {"CHANNEL": "ops"}, "payload": {"order": 42}}'
```

The second form is how to call a task from a webhook, using an `operator`
token. A secret input on the CLI is read from an environment variable of the
same name, or a prompt. Passing it with `--input` works, but it lands in shell
history, and the CLI says so.

A scheduled run has nobody to hand it anything, so it runs on the defaults.
That is why saving a schedule is refused while a required input has no
default.

## Schedules

Five cron fields (minute, hour, day of month, month, day of week) read the
way Vixie cron reads them, or `@hourly`, `@daily`, `@weekly`, `@monthly` and
`@yearly`. Ranges, lists, steps and names (`mon-fri`, `jan`) all work. When
both day fields are restricted, either one matching is enough. Times are
wall-clock times in the schedule's `timezone` (default UTC):

- `0 2 * * *` stays at 02:00 across a daylight-saving change.
- A time skipped in spring runs just after the gap.
- A time repeated in autumn runs once.

```bash
./lemondx cron "*/15 9-17 * * mon-fri" --tz Europe/London   # what it means, the next five
./lemondx task-disable nightly-dump                         # pause the schedule
```

Pausing (`enabled: false`) stops the schedule only. Invoking still works.

**Missed runs are not caught up.** If nobody could run an occurrence, it is
gone, and the next one runs on time.

## Who runs a scheduled run

There is no leader, so each occurrence is settled by a vote. The guarantee is
**at most once**: an occurrence never runs twice, even when the cluster is
split. The cost is that under a split, or with most members down, an
occurrence can be **missed**. When that happens it is logged and recorded as
`missed`.

1. **Every member names the occurrence alike.** An occurrence is the task plus
   the UTC second its schedule fires at, worked out from the record alone.
2. **Every member ranks the eligible nodes alike.** The eligible nodes are the
   members of the task's groups, plus any nodes named beside them. They are
   ranked by a hash of task, occurrence and node, so the first choice differs
   from one occurrence to the next and load spreads.
3. **Turns are staggered.** A node waits 15 seconds for each node ranked above
   it, not counting nodes it knows are in maintenance, then stands as
   candidate. A node **declines** when it is in maintenance, when its last
   launches failed, or when it is already running 8 task instances. The next
   node then stands 15 seconds later.
4. **A candidate asks every member for its grant**, including members not
   eligible to run the task. Each member grants an occurrence once, to the
   first candidate that asks, and writes the grant to disk before it answers,
   so a restart cannot make it grant twice. A strict majority of members means
   run. Two majorities always share a member, which granted only one of them.
   A candidate short of a majority gives its grants back, so the next node can
   still gather one.

A one-node lemondx is its own majority, so a task runs there exactly as
scheduled. A member running a lemondx from before tasks cannot vote. Upgrade
until a majority of members have tasks, or every scheduled occurrence is
missed. That is the at-most-once rule doing its job, not a fault.

### Clocks and the timing veto

Nodes do not share a clock, and a node with a wrong clock would stand for an
occurrence at the wrong time. So each member checks a candidate against its
*own* clock and its *own* copy of the task, and **vetoes** the candidate when:

- the candidate's clock reading is more than **10 seconds** from its own;
- the occurrence is not one the schedule fires at, as this member has the
  schedule;
- the task is disabled here, has no schedule here, or does not exist here;
- the occurrence is not yet due, or is later than every node's turn could
  have come (15 s × members, plus slack).

A veto is a vote withheld, with its reason. If one member's clock is wrong,
its veto is outvoted. If the candidate's clock is wrong, the rest veto it and
nothing runs. That occurrence is recorded as `vetoed` on the candidate, and
both sides log `task.veto` with the measured skew. A drifting clock therefore
shows up as a warning naming the node, not as runs quietly going missing.
**Run NTP (chrony, systemd-timesyncd) on every member.**

## Invoking

An invocation needs no vote: one request is one run. The member that receives
it checks the inputs and the payload. It then tries the eligible nodes in a
random rank order, starting the run on the first that does not decline, and
forwards the request to that node if it is not itself. `node` in the request,
or `--node`, names where the run must go instead. Either way the answer is the
run record, as soon as the run has started. The run is then followed in the
run list.

## In the web UI

The **Tasks** tab lists each task with its schedule, next run, inputs,
secrets and last run. Its buttons:

- **Run** opens a form for the inputs and a payload.
- **Pause** stops the schedule.
- **Edit** opens the task's definition.
- **Delete** removes the task from every member.
- A secret's button sets its value.

Below the tasks, **Runs** lists every member's runs. Click one for its full
output.

The editor's **Job**, **Inputs** and **Secrets** headings, and the run dialog,
each have a **?** that opens an explanation filled in from the task itself.
It shows each input's variable, the parameters that name it, and what a
scheduled run gives it; how each secret reaches the job; and the CLI command
for the run as typed.

## From the CLI

```bash
./lemondx tasks                                   # list
./lemondx task-show NAME                          # one, with its latest runs
./lemondx task-save NAME -t TEMPLATE (-b MOD[,MOD] [--param K=V] | --script FILE|-)
    [--cron EXPR --tz TZ]
    [--input NAME[=DEFAULT]] [--require NAME] [--secret-input NAME] [--secret NAME]
    --group G [--node N] [--timeout S] [--overlap skip|allow] [--disable|--enable]
./lemondx task-save NAME --file task.json         # the whole definition as JSON
./lemondx task-enable NAME / task-disable NAME
./lemondx task-run NAME [--input K=V] [--payload FILE|-] [--node N] [--no-wait]
./lemondx task-runs [NAME]                        # every member's runs
./lemondx task-run-show ID                        # one run, full output
./lemondx task-cancel ID
./lemondx task-secrets NAME / task-secret-set NAME SECRET / task-secret-clear NAME SECRET
./lemondx task-delete NAME [--local-only] [-y]
./lemondx cron EXPR [--tz TZ]
```

`task-save` with flags changes only what the flags name, so an existing
task's other fields are kept. `task-run` hands the run to this node's `serve`
when one is running, like `stack-launch`, so the run outlives the command and
shows in the UI.

## API

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/tasks` | read | every task, with `next_run`, `schedule_text`, `secret_status`, `eligible` (its members now) and `placement_problem` |
| `GET` | `/api/tasks/{name}` | read | one |
| `PUT` | `/api/tasks/{name}` | admin | `{template, bootstrap or script, description, schedule:{cron,timezone}\|null, groups (required), nodes, timeout_seconds, overlap, inputs, secrets, enabled, previous}` → saved and pushed to every member |
| `DELETE` | `/api/tasks/{name}` | admin | delete it everywhere (`?everywhere=false` for here only) |
| `PUT` | `/api/tasks/{name}/enabled` | admin | `{"enabled": false}` pauses the schedule |
| `POST` | `/api/tasks/{name}/invoke` | operator | `{inputs, payload, node, wait}` → the run record once started (`wait`: once finished) |
| `GET` | `/api/tasks/{name}/secrets` | read | which secrets hold a value, never the values |
| `PUT`/`DELETE` | `/api/tasks/{name}/secrets/{secret}` | admin | `{"value"}` → set one, or forget it |
| `GET` | `/api/task-runs` | read | `?task=&live=1&detail=1&local=1` → `{runs, errors}` from every member (`local`: this node's only); outputs are last lines unless `detail` |
| `GET` | `/api/task-runs/{id}` | read | one run of this node's, full output; another node's through `/api/nodes/{node}/task-runs/{id}` |
| `POST` | `/api/task-runs/{id}/cancel` | operator | stop a run of this node's and destroy its instance |
| `GET` | `/api/cron` | read | `?expr=&tz=&count=` → `{cron, timezone, text, next}` |

`POST /api/task-grants`, `POST /api/task-grants/release` and
`PUT /api/task-secrets/{name}` are members-only: the vote, and secret sync.

## On disk

| Path | What |
| --- | --- |
| `tasks/<name>.json` | the definition: synced and reconciled like a stack |
| `auth/task-secrets.json` | secret values, sealed under this node's key (`auth/secrets.key`) |
| `task-runs/<name>.json` | this node's last 50 runs of the task, never synced |
| `task-grants.json` | this node's votes, kept an hour past each occurrence, never synced |

Deleting a template or a module that a task uses is refused while the task
uses it, as for stacks.

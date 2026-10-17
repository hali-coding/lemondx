# Tasks

[← back to README](../README.md)

A **task** runs bootstrap modules in a throwaway instance, on a schedule or
when invoked, and destroys the instance when the run ends. It is cron without
a host that owns the job, and Lambda without the cloud. No node owns a task.
For every run the cluster decides which member runs it, so losing a node
loses nothing but that node.

A task is made of parts waterpump already has:

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
./waterpump task-save nightly-dump -t "Debian" -b pg-dump --group workers \
    --cron "0 2 * * *" --tz Europe/Stockholm \
    --secret DB_PASSWORD --param 'PGPASSWORD={{secrets.DB_PASSWORD}}'
./waterpump task-secret-set nightly-dump DB_PASSWORD  # from $DB_PASSWORD or a prompt
./waterpump task-run nightly-dump                      # run it now and wait
./waterpump task-runs nightly-dump                     # every member's runs
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
   instance is always ephemeral, and is tagged `user.waterpump.task` and
   `user.waterpump.task-run` but *not* with the template. So the template's
   Recreate, Destroy and app check never take in an instance that exists for
   one run.
3. The template's own modules run, then the job's. Each job module gets these
   environment variables:

   | Variable | Value |
   | --- | --- |
   | `WATERPUMP_TASK`, `WATERPUMP_TASK_RUN` | the task and this run's id |
   | `WATERPUMP_TASK_TRIGGER` | `schedule` or `invoke` |
   | `WATERPUMP_TASK_SLOT` | for a scheduled run, the occurrence it is for (epoch seconds) |
   | `WATERPUMP_TASK_NODE` | the member running it |
   | `WATERPUMP_INPUT_<NAME>` | every declared input, given or defaulted |
   | `WATERPUMP_TASK_PAYLOAD` | the path of the payload file, when the run was given one |

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
- **Its environment** carries everything a job is given: `WATERPUMP_TASK_*`,
  `WATERPUMP_INPUT_<NAME>` for each input, `WATERPUMP_TASK_PAYLOAD`, and
  `WATERPUMP_SECRET_<NAME>` for each task secret. Secret values are masked in
  the output.
- **No placeholders.** Nothing is filled into the script's text, so a secret
  never sits in a file in the instance. Every declared secret must hold a
  value, since nothing says which ones the script reads.
- **Size:** at most 64 KiB. It is stored in the task record and synced with
  it.

```bash
./waterpump task-save ping-hook -t Debian --group workers --secret API_KEY --script hook.sh
```

where `hook.sh` is:

```sh
pkg_install curl
curl -fsS -H "Authorization: Bearer $WATERPUMP_SECRET_API_KEY" \
     --data @"${WATERPUMP_TASK_PAYLOAD:-/dev/null}" https://example.com/hook
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

A job sees every input as `WATERPUMP_INPUT_<NAME>`, and a job parameter can use
one by name: `--param 'TARGET={{inputs.TARGET}}'`. A module's *secret*
parameter can only be set to a reference: `{{secrets.NAME}}` or a secret
input's `{{inputs.NAME}}`, never a value.

A **payload** is a whole message, as JSON or text, up to 256 KiB. The job
gets it as a file whose path is in `$WATERPUMP_TASK_PAYLOAD`. It is never put in
the environment, the event log or the run record. Only its size is kept.

```bash
echo '{"order": 42, "status": "shipped"}' | \
    ./waterpump task-run notify --input CHANNEL=ops --payload -
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
./waterpump cron "*/15 9-17 * * mon-fri" --tz Europe/London  # what it means, the next five
./waterpump task-disable nightly-dump                     # pause the schedule
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

A one-node waterpump is its own majority, so a task runs there exactly as
scheduled. A member running a waterpump from before tasks cannot vote. Upgrade
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
forwards the request to that node if it is not itself. A node that cannot be
reached within 3 seconds is passed over. One that took the request but never
answered may have started the run, so the invocation stops there with a `504`
rather than risk starting it twice. `node` in the request,
or `--node`, names where the run must go instead. Either way the answer is the
run record, as soon as the run has started. The run is then followed in the
run list.

## GitHub Actions runners from a webhook

A task can start a run for every GitHub Actions job that is queued: GitHub
sends a `workflow_job` webhook, the run's instance holds a runner that takes
one job and exits, and the instance is destroyed when the run ends. Every job
gets a clean machine and nothing is left registered. It is the
[`github-agent`](modules.md#the-github-runner-module) module in `jit` mode as
the job.

**The template** has what every job needs. `RUNNER_MODE=install` puts the
runner and its dependencies in without registering anything:

```bash
./waterpump template-save gh-runner -i ubuntu:24.04 -c 4 -m 8GiB \
    -b base -b docker -b github-agent --param RUNNER_MODE=install
```

That runs on every job (a minute or two). To start in seconds, run the same
modules on one instance, snapshot it, and point the template at the snapshot
(`--snapshot`, see [Templates](templates.md)).

**The task** runs the module again in `jit` mode, with what the webhook hands
it:

```bash
./waterpump task-save gh-jobs -t gh-runner -b github-agent \
    --param RUNNER_MODE=jit \
    --param 'GITHUB_REPO_URL={{inputs.GITHUB_URL}}' \
    --param 'RUNNER_LABELS={{inputs.GITHUB_LABELS}}' \
    --param 'GITHUB_RUNNER_TOKEN={{secrets.GITHUB_PAT}}' \
    --input GITHUB_URL --input GITHUB_LABELS --input GITHUB_JOB_ID \
    --secret GITHUB_PAT --secret WEBHOOK_SECRET \
    --webhook-secret WEBHOOK_SECRET --webhook-label self-hosted,Linux,X64,waterpump \
    --group small --overlap allow --timeout 21600
WEBHOOK_SECRET=$(openssl rand -hex 32); echo "$WEBHOOK_SECRET"    # paste it into GitHub too
WEBHOOK_SECRET=$WEBHOOK_SECRET ./waterpump task-secret-set gh-jobs WEBHOOK_SECRET
./waterpump task-secret-set gh-jobs GITHUB_PAT                       # prompts
```

- **`WEBHOOK_SECRET`** is a random string you make up, entered in GitHub and
  stored here. GitHub signs every delivery with it and never sends it, so it
  cannot be used for anything else -- it is not a waterpump or GitHub token.
- **`GITHUB_PAT`** is what the runner registers with: a fine-grained token
  with *Administration: write* on the repository, a classic one with `repo`
  (`admin:org` for an organisation's runners), or an app's installation
  token. It stays in waterpump; GitHub never sees it in the webhook.
- **The labels** are what the runner offers. A queued job is started here only
  when every label its `runs-on` asks for is among them, which is how GitHub
  itself matches jobs to runners; anything else is acknowledged and ignored.
  The run registers with exactly the labels the job asked for.
- **`--overlap allow`** lets jobs run side by side; each node takes at most
  8 task runs at once, and a member that declines is skipped.
- **`--webhook-scope org`** makes `GITHUB_URL` the organisation's, for an
  organisation webhook and runners shared across its repositories.

**On GitHub**, under the repository's (or organisation's) *Settings →
Webhooks → Add webhook*:

| Field | Value |
| --- | --- |
| Payload URL | `https://<public name>/api/hooks/gh-jobs` |
| Content type | `application/json` (a form-encoded body is refused) |
| Secret | the `WEBHOOK_SECRET` value |
| SSL verification | enabled |
| Events | *Let me select individual events* → only **Workflow jobs** |

GitHub has to reach a member over HTTPS with a certificate it trusts, so the
payload URL is a reverse proxy or tunnel in front of one or more members, not a
node's own self-signed address. A delivery to any member works: it is placed
by the task's groups like any invocation. Point the proxy at more than one
member, or a member that is down receives nothing.

What the endpoint answers, which GitHub shows under *Recent Deliveries*:

| Answer | When |
| --- | --- |
| `202` + run id | a queued job matched and a run started |
| `200` ignored, with the reason | `ping`, another event or action, a label not offered, a delivery id already received |
| `403` | no signature, a wrong one, or no such task or webhook -- one answer, so the URL does not say which tasks exist; the log says which it was |
| `413` / `415` | a body over 1 MiB / not `application/json` |
| `503` | no member would take the run (all busy, in maintenance or unreachable); press *Redeliver* once there is room |
| `504` | a member took the run but did not answer, so it may be running there; check its runs before redelivering, since this node now ignores a redelivery of that id |

A run receives the `GITHUB_*` inputs it declares: `GITHUB_URL`,
`GITHUB_REPOSITORY`, `GITHUB_JOB_ID`, `GITHUB_JOB_NAME`, `GITHUB_RUN_ID`,
`GITHUB_RUN_ATTEMPT`, `GITHUB_WORKFLOW`, `GITHUB_LABELS` and
`GITHUB_DELIVERY`. The whole delivery is its payload file. A runner whose job
went to another runner, or was cancelled, gives up after `RUNNER_IDLE_SECONDS`
and removes its registration, so the run ends instead of waiting out its
timeout.

Things to know:

- GitHub does not retry a failed delivery by itself. A job whose delivery got
  a `503` waits in the queue until someone redelivers it.
- GitHub gives a delivery 10 seconds. Placing the run gets 8 of them: an
  unreachable member costs 3, and the rest are tried in turn while time is
  left.
- A runner on a **public** repository runs whatever a fork's pull request
  asks of it. Require approval for outside contributors, use a label only
  your own workflows ask for, and keep the template off fabrics and LAN
  networks so a job cannot reach the rest of the cluster.
- A delivery id is remembered by the process that received it, so a
  redelivery to a different member, or after a restart, starts a second run.
  That costs one idle runner, which gives up as above.

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

The editor's **GitHub webhook** section turns the webhook on, picks the
secret it is signed with, and shows the URL to give GitHub.

The editor's **Job**, **Inputs** and **Secrets** headings, and the run dialog,
each have a **?** that opens an explanation filled in from the task itself.
It shows each input's variable, the parameters that name it, and what a
scheduled run gives it; how each secret reaches the job; and the CLI command
for the run as typed.

## From the CLI

```bash
./waterpump tasks                               # list
./waterpump task-show NAME                      # one, with its latest runs
./waterpump task-save NAME -t TEMPLATE (-b MOD[,MOD] [--param K=V] | --script FILE|-)
    [--cron EXPR --tz TZ]
    [--input NAME[=DEFAULT]] [--require NAME] [--secret-input NAME] [--secret NAME]
    --group G [--node N] [--timeout S] [--overlap skip|allow] [--disable|--enable]
    [--webhook-secret SECRET --webhook-label L[,L] [--webhook-scope repo|org] | --no-webhook]
./waterpump task-save NAME --file task.json     # the whole definition as JSON
./waterpump task-enable NAME / task-disable NAME
./waterpump task-run NAME [--input K=V] [--payload FILE|-] [--node N] [--no-wait]
./waterpump task-runs [NAME]                    # every member's runs
./waterpump task-run-show ID                    # one run, full output
./waterpump task-cancel ID
./waterpump task-secrets NAME / task-secret-set NAME SECRET / task-secret-clear NAME SECRET
./waterpump task-delete NAME [--local-only] [-y]
./waterpump cron EXPR [--tz TZ]
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
| `PUT` | `/api/tasks/{name}` | admin | `{template, bootstrap or script, description, schedule:{cron,timezone}\|null, groups (required), nodes, timeout_seconds, overlap, inputs, secrets, webhook:{provider,secret,labels,scope}\|null, enabled, previous}` → saved and pushed to every member |
| `DELETE` | `/api/tasks/{name}` | admin | delete it everywhere (`?everywhere=false` for here only) |
| `PUT` | `/api/tasks/{name}/enabled` | admin | `{"enabled": false}` pauses the schedule |
| `POST` | `/api/tasks/{name}/invoke` | operator | `{inputs, payload, node, wait}` → the run record once started (`wait`: once finished) |
| `POST` | `/api/hooks/{name}` | signature | a GitHub webhook delivery; see [below](#github-actions-runners-from-a-webhook) |
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

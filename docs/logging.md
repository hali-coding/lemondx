# Logging

[← back to README](../README.md)

lemondx logs every change it makes and who asked for it: instance state and
configuration, daemon resources (networks, storage, images, profiles), host
networking, lemondx's own definitions and settings, logins and cluster
membership. Each line also says *how* the change was made: the web UI, the API,
the CLI, another node relaying it, or `serve` itself.

Lines always go to the local syslog. Under systemd that is the journal:

```bash
journalctl -t lemondx -f                 # everything lemondx logs on this host
journalctl -t lemondx | grep req=3cbcc6f8   # one request, end to end
```

Optionally they also go to a remote syslog host. Both the level and the remote
host are one setting for the whole cluster: set them in the **Cluster** tab
under **Configuration → Logging**, or from the CLI:

```bash
lemondx logging                                     # the settings, and how each destination is doing here
lemondx logging-set --level info --remote logs.example.net:514 --protocol tcp
lemondx logging-set --no-remote
lemondx logging-test                                # send a test line everywhere, now
lemondx configure logging                           # the same, asked interactively
```

## Watching it live

Besides syslog, every `serve` keeps its last 5000 events in memory, so you
can follow what is happening without a shell on each host:

- **The Logs tab** follows every node at once, merged by time. You can filter
  by node or group, level, kind, who, instance or text, and pause. Clicking a
  request id shows everything that one request did, on every node it touched.
  Clicking a person or an instance filters to them.
- **An instance's Activity tab**, in its details panel, shows that instance's
  history on its own node: creates, state changes, snapshots, bootstrap runs,
  and who did each.
- **The CLI:**

  ```bash
  lemondx logs                       # the last 50 events, from every node
  lemondx logs -f                    # ...and keep following
  lemondx logs -f -n 0 --node prdev2 --kind change
  lemondx logs --req 3cbcc6f8        # one request, everywhere
  lemondx logs -f --json | jq .      # one JSON event per line
  ```

  It asks the `serve` running on this host, so it needs one running.

CLI commands show up too. A CLI process hands its events to the `serve` on
the same host through `events.sock` in the data directory, which only the
same user can reach. Under the system unit, which runs as its own account,
an admin's CLI writes to its own data directory, so its commands appear in
syslog but not in the tail.

**Limits.** The buffer lives in memory. It starts empty when `serve`
restarts, and holds the latest 5000 events per node. Syslog is the record.
The tail shows `info` and above, whatever level syslog is set to, so changes
are always visible; `debug` events appear only when the level is `debug`. The
tail's own polling is left out of it.

**Who can see it:** anyone who can log in, `read` role included. That
includes failed logins (with the username that was tried) and client
addresses.

### Through the API

`GET /api/logs` is one node's tail; `GET /api/cluster/logs` is every node's,
merged. Both take the filters `level` (the minimum), `kind` (comma-separated),
`actor`, `req`, `channel`, `instance` and `q` (text), plus `limit`, and
`wait`: up to 20 seconds to hold the call open until something new arrives.

- **Without a cursor** you get the latest events (the backlog) and a `cursor`.
- **With the cursor** (`after` for one node, `cursor` for the cluster) you get
  what came after it, and a new cursor to ask from next.

```bash
U=https://node:8099/api; H="Authorization: Bearer $TOKEN"
page=$(curl -s -H "$H" "$U/cluster/logs?limit=20")
while :; do
  echo "$page" | jq -c '.data.events[]'
  cursor=$(echo "$page" | jq -r .data.cursor)
  page=$(curl -s -H "$H" "$U/cluster/logs?cursor=$cursor&wait=20")
done
```

The cluster cursor is opaque. It holds each node's place in its own buffer, so
nothing is skipped or repeated, even though nodes answer at different times:
- **The call returns early:** as soon as any node has something new.
- **Nodes still waiting** show as `pending` in `nodes`, and are asked again
  from the same place next time.
- **An unreachable node** shows `ok: false` with an error, and the others go on.
- **A node that restarted** shows `reset: true`, and its events start again
  from its new beginning.
- **`truncated: true`** means more events happened than its buffer holds
  between two calls.

## What a line looks like

```
request action=containers.state req=052d43ed actor=hampus role=admin via=session channel=ui route="POST /api/containers/{}/state" node=prdev2 target=web-1 client=192.168.75.20 result=ok ms=4629 verb=stop
change action=instance.state req=052d43ed actor=hampus role=admin via=session channel=peer origin=orange origin_channel=ui result=ok instance=web-1 state=stop
```

The first word is the kind of event:

| Kind | What |
| --- | --- |
| `request` | something asked for: an API call (one line each, with its outcome and duration), a CLI command, a terminal opened or closed |
| `change` | something that changed: a daemon mutation (`instance.create`, `network.update`, `image.alias.delete`, …), a host command, a saved or deleted definition or setting |
| `auth` | logins, failed logins and lockouts, users and tokens created, changed or removed, a node enrolling |
| `system` | what `serve` does by itself: start and stop, reconciliation, health transitions, fabric upkeep, failures |
| `access` | the raw HTTP access line (debug only) |

The fields every line starts with:

| Field | Meaning |
| --- | --- |
| `action` | what happened, e.g. `containers.create`, `instance.state`, `cli.launch` |
| `req` | an id shared by everything one request or command did, on every node it touched |
| `actor` | who: a user, a token's owner, or `system` |
| `role` | their role: `read`, `operator` or `admin` |
| `via` | how they authenticated: `session`, `pam`, `proxy`, `token:<name>`, `static-token`, `cli`, `anonymous`; for `system`, which chore (`reconcile`, `health`, `fabric`, …) |
| `channel` | how the request arrived: `ui`, `api`, `cli`, `peer` (relayed by another node) or `system` |
| `origin` / `origin_channel` | for `channel=peer`: the node it came from, and how it was made there |

`ui` and `api` are told apart by a header the web UI sends. That is
attribution, not security: any client can claim to be the UI.

**One action across nodes.** When a person on orange stops an instance on
prdev2, orange logs the `request` with `node=prdev2`. prdev2 logs the same
`req` as `channel=peer origin=orange`, keeping the person as `actor`, and logs
the daemon `change` that followed. Grep every node's log for that one `req` to
see the whole thing. Work that runs in the background, such as a template
launch, a stack run or an image fetch, keeps the `req` and actor of whoever
started it.

## Levels

| Level | Adds |
| --- | --- |
| `error` | lemondx's own faults (an API call that ended in a 500) |
| `warning` | anything refused or failed: denied requests, failed changes, sync failures, unhealthy instances |
| `notice` | logins and logouts, users and tokens, cluster membership, settings, terminals, `serve` starting and stopping |
| `info` (default) | every change and every API call or CLI command that changes something |
| `debug` | every read and every request, exec and file plumbing inside instances, HTTP access lines |

Health is logged as transitions only, e.g. healthy → degraded and back. A line
per instance per round would drown everything else.

## Format and transport

- **Local:** sent to `/dev/log` in the form journald and rsyslog parse,
  `lemondx[PID]: <kind> key=value…`, so `journalctl -t lemondx` works. A host
  with no syslog socket logs to stderr instead, and says so in
  `lemondx logging` and in the Cluster tab. `serve` started in a terminal also
  echoes lines there.
- **Remote:** RFC 5424 (`<PRI>1 TIMESTAMP HOST lemondx PID KIND - key=value…`)
  over UDP, or over TCP with RFC 6587 octet-counted framing.
  - **Facility:** `local0` by default.
  - **Delivery:** lines are queued and sent from a thread of their own, so a
    slow or dead collector never holds up a request.
  - **Failures:** a TCP host that refuses is retried after 30 seconds, and the
    error shows per node in the Cluster tab.
  - **UDP:** cannot report a lost line.

An rsyslog collector, for example:

```
module(load="imudp")  input(type="imudp" port="514")
module(load="imtcp")  input(type="imtcp" port="514")
if $programname == "lemondx" then /var/log/lemondx.log
```

Under the hood, a destination is a type, and `syslog` is the only one so far.
Another kind of stream (a file, TLS syslog, an HTTP collector) is one class in
`eventlog.py` and one editor in the Cluster tab.

## What is never logged

Request bodies are not logged. Only an allowlist of identifying fields is: the
instance or template name, the action, the target nodes, and so on.

Never logged anywhere:
- parameter values, so a module's secret is never logged
- passwords and tokens (a `?token=` in an access line is masked)
- scripts, keys and config values
- an instance's config, only *which keys* changed
- a CLI command's option values, so `--param` stays out; only positional names
  are logged

Values are stripped of newlines, so a crafted name cannot forge a second line.

Remote syslog over UDP or TCP is not encrypted. What it carries is names,
addresses and what was done. Keep the collector on a network you trust.

## How the setting is shared

The setting is one record, `logging`, in `~/.local/share/lemondx/cluster-settings/`.
It is a [shared definition](cluster.md#shared-definitions-stay-level-by-themselves) like a
template: saving it pushes it to every member, and a running `serve` applies it
at once. A CLI change reaches `serve` within a few seconds.

A node that was off when it changed keeps its old copy. Two different copies
are a conflict that reconciliation reports but does not settle. The Cluster tab
marks that node **behind** and offers **Push to …**, or use **Sync** with
*Cluster settings*.

An unreadable record falls back to the defaults with a warning. Local syslog is
always on whatever the setting says.

# REST API

[← back to README](../README.md)

All responses are `{"data": ...}` on success and `{"error": "..."}` on failure,
with a matching HTTP status.

When the server runs with `--auth` (see [Security](security.md)), requests need
a session cookie from `/api/auth/login` or `Authorization: Bearer <token>`.
Every `GET` needs `read` access and everything else `admin`, except where the
auth table below says otherwise and these, which need `operator`: container
`state` (single and bulk, and `/api/cluster/containers/state` and `/delete`),
`DELETE /api/containers/{name}`, container `exec` and `bootstrap`, template
`launch`, `exec`, `recreate` and `destroy`, stack `launch`, `state` and
`destroy`, and cancelling or dismissing a template or stack run. Missing or bad
credentials get `401`, too little access `403`, too many failed logins `429`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/status` | daemon flavor/version, readiness, pools, networks |
| `POST` | `/api/setup` | create pool + bridge, attach to default profile |
| `GET` | `/api/configure` | how this node is reached: `unconfigured`, bound `host`/`port`, `tls`, `auth`, `pinned` flags, saved `certificate`, `admins`, and `blocked` (why configuring is refused now, or `""`) |
| `POST` | `/api/configure` | leave [unconfigured mode](security.md#unconfigured-mode): `{"username","password","tls":{"mode":"generate","host"}\|{"mode":"upload","cert","key"}\|{"mode":"keep"},"host":"0.0.0.0"}` → what was saved, then the server restarts; `409` in a cluster or while work is running |
| `GET` | `/api/logging` | this node's [logging](logging.md): the cluster's `settings` as applied here, whether any were `saved`, `local_fallback` (no syslog socket), and each destination's `sent` count and last `error` |
| `PUT` | `/api/logging` | `{"settings":{"level","local":{"facility"},"destinations":[{"type":"syslog","enabled","host","port","protocol":"udp"\|"tcp","facility"}]}}` → saved and pushed to every member; returns `synced` |
| `GET` | `/api/logs` | this node's [live tail](logging.md#watching-it-live): `?after=<cursor>&wait=0..25&limit=&level=&kind=&actor=&req=&channel=&instance=&q=` → `{node, boot, cursor, events, reset, truncated}`; without `after`, the latest events. Any logged-in user |
| `GET` | `/api/cluster/logs` | every node's tail merged by time: same filters, `cursor` (opaque, from the last answer), `wait` (≤20), `nodes`/`group` → `{cursor, events (each with node), nodes: [{node, ok, error, pending, reset, truncated}]}`; returns as soon as any node has something |
| `POST` | `/api/logging/test` | send a test line to every destination of this node now → per-destination `results` |
| `PUT` | `/api/settings/{name}` | a cluster setting as stored — cluster members only (sync) |
| `DELETE` | `/api/settings/{name}` | back to the default — cluster members only (sync) |
| `GET` | `/api/containers` | list with live state |
| `POST` | `/api/containers` | create (and optionally start); `"background":true` returns progress at once |
| `GET` | `/api/creates` | creates in progress (stage: creating/starting/bootstrapping) or finished in the last 10 minutes |
| `GET` | `/api/containers/{name}` | full detail, config, devices, snapshots |
| `PATCH` | `/api/containers/{name}` | update cpu/memory limits, description |
| `DELETE` | `/api/containers/{name}` | delete (`?force=true` stops it first) |
| `POST` | `/api/containers/{name}/state` | `{"action":"start\|stop\|restart\|freeze\|unfreeze"}` |
| `POST` | `/api/containers/state` | `{"names":[...],"action":"start\|stop\|..."}` → each container's outcome, applied in parallel |
| `POST` | `/api/containers/{name}/rename` | `{"name":"new-name"}` |
| `POST` | `/api/containers/{name}/exec` | `{"command":"uname -a"}` → stdout/stderr/exit_code |
| `GET`/`POST` | `/api/containers/{name}/snapshots` | list / create |
| `DELETE` | `/api/containers/{name}/snapshots/{snap}` | delete |
| `POST` | `/api/containers/{name}/snapshots/{snap}/restore` | restore |
| `POST` | `/api/containers/{name}/snapshots/{snap}/publish` | `{"alias","nodes":[...]}` → make an image of it here and copy it to those nodes (see [Templates](templates.md#from-a-snapshot)) |
| `GET` | `/api/resources` | host CPU/memory/disk against what instances have allocated |
| `GET` | `/api/health` | latest health check per running instance (see [Web UI tour](web-ui.md#health-checks)) |
| `GET` | `/api/metrics` | this node's usage history, sampled every 2 s by `serve` and kept for 30 minutes: `?since=<at>&window=<seconds>` → `{period, retention, started_at, at, host: {cpu_threads, memory_total, uplink, points}, instances: [{name, status, type, template, stack, processes, points}]}`; each point is `[at, cpu %, memory bytes, rx B/s, tx B/s]` (host CPU is of all threads, an instance's of one core). 503 outside `serve` |
| `GET` | `/api/containers/{name}/app-check` | the latest [app check](templates.md#app-health-checks) run in full: `{template, script, interval_seconds, timeout_seconds, result}`, `result` carrying `stdout`/`stderr` (last 16 KiB each) or null before the first run |
| `GET` | `/api/storage` | pools, volumes, local-driver availability and management status |
| `GET`/`POST` | `/api/storage/pools` | list pools / create a local pool |
| `GET`/`PATCH`/`DELETE` | `/api/storage/pools/{pool}` | inspect, update or remove a local pool (`?force=true` cascades) |
| `GET`/`POST` | `/api/storage/pools/{pool}/volumes` | list volumes / create a custom volume |
| `GET`/`PATCH`/`DELETE` | `/api/storage/pools/{pool}/volumes/custom/{volume}` | manage an unattached custom volume |
| `GET` | `/api/images` | cached images, suggested catalog, remotes |
| `GET` | `/api/images/browse` | full remote catalogs, flagged with what is local |
| `POST` | `/api/images/{alias}/copy` | `{"nodes":[...]}` → copy this node's image to other nodes |
| `DELETE` | `/api/images/{fingerprint}` | remove this node's copy of an image (`409` while a template launches it as `local:<alias>`) |
| `GET` | `/api/images/inventory` | this node's images (full fingerprints) and every snapshot on it |
| `GET` | `/api/cluster/images` | the same from every member, each entry tagged `node`, plus `errors` for members that did not answer |
| `GET` | `/api/image-jobs` | publishes, copies and pinned-build fetches held by this node, running or finished in the last 3 minutes |
| `POST` | `/api/images/adopt` | "have you got this image?" — name it if so — cluster members only |
| `PUT` | `/api/images/receive` | an image, streamed and checked by fingerprint (`?content_type=` carries a split image's multipart type) — cluster members only |
| `POST` | `/api/images/prune` | `{"apply"?,"only"?}` → this node's downloaded images that nothing uses, pins or launches (`deleted`, `kept` with a `reason`, `freed`); deleted only with `"apply": true`, and then only the fingerprints in `only` when given |
| `POST` | `/api/cluster/images/prune` | the same on `nodes` (default all), per node; `only` is `{node: [fingerprint, …]}` from a preview |
| `GET` | `/api/images/pins` | every [pinned build](daemons-and-storage.md#pinned-images), with whether this node `held` it and the templates that launch it (`used_by`); `{pin}` below is its id or a nickname |
| `GET` | `/api/images/versions` | `?image=images:debian/12` → the builds its remote still serves, newest first, and its `pins` |
| `POST` | `/api/images/pins` | `{"image","serial"?,"nicknames"?,"note"?,"nodes"?}` → pin that build (newest by default) everywhere, then fetch it onto `nodes` (default all) as an image job; 409 if the build is pinned already; `"background":true` returns the job at once |
| `PATCH` | `/api/images/pins/{pin}` | `{"nicknames"?,"note"?}` → the only things a pin may change; a nickname a template launches by cannot be removed |
| `DELETE` | `/api/images/pins/{pin}` | unpin, everywhere unless `?everywhere=false`; refused while a template launches it; fetched builds stay |
| `POST` | `/api/images/pins/{pin}/fetch` | `{"nodes"?}` → fetch the pinned build here (if needed) and send it to those nodes |
| `POST` | `/api/images/pins/{pin}/pull` | fetch the pinned build onto this node only |
| `PUT` | `/api/images/pins/{id}` | a pin as stored — cluster members only (sync); refused if it changes the build |
| `GET`/`POST` | `/api/networks` | every interface the daemon can see / create a managed bridge |
| `GET` | `/api/subnets` | every subnet already on the host, which a new bridge must not overlap |
| `GET` | `/api/cluster/networks/{name}` | which members have a managed network of that name, and whether it is a fabric |
| `DELETE` | `/api/cluster/networks/{name}` | delete it on every member that has it (`?nodes=` narrows), reported per node; refused for a fabric |
| `GET` | `/api/lan` | the host's NICs, and which ways each can put instances on its LAN (see [LAN](lan.md)) |
| `POST` | `/api/lan/networks` | `{"nic","mode":"bridge\|macvlan","name"}` → a network on that NIC's LAN; `"default": true` instead of `nic` uses the default-route NIC; `"everywhere": true` makes a macvlan of that name on every member's default-route NIC, reported per node |
| `GET` | `/api/lan/plan` | `?nic=&bridge=` → the commands converting that NIC would run |
| `POST` | `/api/lan/convert` | `{"nic","bridge"}` → make the host's NIC a bridge port, keeping its address; undone if it fails |
| `POST` | `/api/lan/revert` | `{"bridge"}` → give a converted bridge's NIC back its own connection |
| `GET`/`PATCH`/`DELETE` | `/api/networks/{name}` | config, state, DHCP leases, attachments / change or delete a managed bridge |
| `GET` | `/api/cluster` | this node's name, address and certificate fingerprint |
| `GET` | `/api/cluster/nodes` | federated nodes and their state (`?probe=false` skips contacting them) |
| `GET` | `/api/cluster/nodes/{name}` | one node, with the instances on it |
| `POST` | `/api/cluster/nodes` | `{"code":"lemondx-join.…"}` → join that node's cluster |
| `DELETE` | `/api/cluster/nodes/{name}` | evict a node: stand it down, forget it everywhere (`?rotate=` overrides) |
| `POST` | `/api/cluster/groups/auto` | rebuild the `large` and `small` groups from each node's CPU and memory |
| `PUT` | `/api/auth/users/{name}/record` | take a synced account as stored, hash included — cluster members only |
| `POST` | `/api/cluster/leave` | give up membership of the cluster |
| `GET`/`POST` | `/api/cluster/members` | the member list / announce a member (node to node) |
| `DELETE` | `/api/cluster/members/{name}` | a peer saying a node has left — cluster members only |
| `POST` | `/api/cluster/evicted` | a peer saying this node was evicted, so it stands down — cluster members only |
| `POST` | `/api/cluster/refresh` | pull every peer's member list and push ours |
| `PUT` | `/api/cluster/maintenance` | `{"enabled":true,"reason":"..."}` → put this node into maintenance, or take it out; another node's through `/api/nodes/{node}/cluster/maintenance` |
| `POST` | `/api/cluster/reconcile` | settle this node's shared definitions against every member's (`{"apply": false}` reports without changing) |
| `GET` | `/api/cluster/drift` | what the last reconciliation found; `null` until one has run in this `serve` |
| `GET` | `/api/cluster/manifest` | a digest per shared artifact, with this node's change ledger — cluster members only |
| `POST` | `/api/cluster/artifacts` | `{"items":[{"kind","name"}]}` → the bodies behind them — cluster members only |
| `POST` | `/api/cluster/rotate` | replace the cluster credential on every member |
| `PUT` | `/api/cluster/secret` | take a rotated credential (node to node) |
| `GET` | `/api/cluster/metrics` | every node's `/api/metrics` side by side: `cursor` (opaque, from the last answer), `window`, `nodes`/`groups` → `{cursor, nodes: [{node, self, ok, error, ...}]}`; a node that does not answer keeps its place |
| `GET` | `/api/cluster/containers` | instances across nodes (`?all=true`, `?nodes=`, `?groups=`), with each node's latest health records under `health` |
| `POST` | `/api/cluster/containers/state` | `{"instances":[{"node","name"}],"action":...}` over several nodes |
| `POST` | `/api/cluster/containers/delete` | delete instances that sit on several nodes |
| *any* | `/api/nodes/{node}/{path}` | make that call against one node's own API and return its answer |
| `GET` | `/api/cluster/groups` | node groups |
| `PUT`/`DELETE` | `/api/cluster/groups/{name}` | create or replace / delete a group (`large` and `small` are refused: they are sized, not written; `409` deleting one a stack targets) |
| `GET`/`POST` | `/api/cluster/invites` | unredeemed join codes / issue one |
| `DELETE` | `/api/cluster/invites/{id}` | withdraw a join code |
| `POST` | `/api/cluster/sync` | push templates, modules or profiles to other nodes |
| `POST` | `/api/cluster/fingerprint` | what certificate an address presents right now |
| `POST` | `/api/cluster/enroll` | redeem a join code — node to node, no token (see [Nodes](cluster.md)); refused (`409`) unless `node.daemon` reports LXD ≥ 5.21 or Incus ≥ 6.0 |
| `GET` | `/api/modules` | bootstrap modules with their parameters |
| `GET` | `/api/ssh-keys` | public keys found in `~/.ssh` |
| `POST` | `/api/ssh-keys/validate` | check one pasted public key |
| `POST` | `/api/containers/{name}/bootstrap` | run modules in a container |
| `POST` | `/api/modules` | upload a module |
| `GET` | `/api/modules/{id}/source` | the module's script |
| `PUT` | `/api/modules/{id}/settings` | save its defaults / pre-select it |
| `DELETE` | `/api/modules/{id}` | remove an uploaded module (`409` while a template or profile selects it) |
| `GET` | `/api/bootstrap-profiles` | saved module selections |
| `PUT` | `/api/bootstrap-profiles/{name}` | create or replace one |
| `DELETE` | `/api/bootstrap-profiles/{name}` | delete one |
| `GET` | `/api/templates` | saved instance templates |
| `PUT` | `/api/templates/{name}` | create or replace one |
| `DELETE` | `/api/templates/{name}` | delete one (`409` while a stack launches it) |
| `POST` | `/api/templates/{name}/launch` | `{"count":3}` → create instances from it; `nodes`/`groups` spread it across a cluster |
| `GET` | `/api/templates/{name}/instances` | names of the instances launched from it |
| `POST` | `/api/templates/{name}/exec` | `{"command":"uptime","instances":[...]}` → run on each, with output |
| | | `instances` entries may be `{"node","name"}`, spreading destroy/recreate/exec over a cluster |
| `POST` | `/api/templates/{name}/recreate` | `{"instances":[...]}` → delete and recreate each, same names; with `"stale":true`, `instances` must be exactly the stale ones no stack launched |
| `POST` | `/api/templates/{name}/destroy` | `{"instances":[...]}` → stop and delete each |
| `GET` | `/api/template-runs` | launches/recreates/destroys in progress, or last finished, per template |
| `DELETE` | `/api/template-runs/{name}` | dismiss a finished run |
| `GET` | `/api/stacks` | saved stacks; see [Stacks](stacks.md#api) for the rest |
| `PUT` | `/api/stacks/{name}` | `{"description","stages"}` → create or replace one |
| `DELETE` | `/api/stacks/{name}` | delete one |
| `POST` | `/api/stacks/{name}/launch` | `{"params":{},"background":true}` → run it stage by stage |
| `GET` | `/api/stack-runs` | stacks running, or last finished |
| `GET` | `/api/stack-instances` | each stack's instances, by their tags, on every node |
| `POST` | `/api/stacks/{name}/state` | `{"action":"stop","instances":[...]}` → start/stop/restart them all |
| `POST` | `/api/stacks/{name}/destroy` | `{"instances":[...]}` → stop and delete them all |
| `POST` | `/api/stack-runs/{name}/cancel` | start nothing new in a running stack |
| `DELETE` | `/api/stack-runs/{name}` | dismiss a finished run |
| `GET` | `/api/profiles` | available profiles |

Authentication:

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/auth` | none | `{enabled, methods, password_login, principal}` — who you are, what the server accepts |
| `POST` | `/api/auth/login` | none | `{"username","password"}` → sets the session cookie |
| `POST` | `/api/auth/logout` | none | ends the session |
| `GET` | `/api/auth/tokens` | read | your API tokens (admins: all) |
| `POST` | `/api/auth/tokens` | read | `{"name","role","expires_days"}` → the token, including its secret, once |
| `DELETE` | `/api/auth/tokens/{id}` | read | revoke your token (admins: any) |
| `GET` | `/api/auth/users` | admin | local users |
| `PUT` | `/api/auth/users/{name}` | admin | `{"password","role"}` → create, or change either |
| `DELETE` | `/api/auth/users/{name}` | admin | remove a local user |

Token routes refuse callers who authenticated with a token. Cookie- and
proxy-authenticated requests other than `GET` must not carry a foreign `Origin`.

```bash
curl -s localhost:8099/api/containers | jq
curl -s -H "Authorization: Bearer $LEMONDX_API_TOKEN" localhost:8099/api/containers   # with --auth
curl -s -X POST localhost:8099/api/containers \
  -H 'content-type: application/json' \
  -d '{"name":"demo","image":"images:alpine/3.21","memory":"512MiB"}'

curl -s -X POST localhost:8099/api/storage/pools \
  -H 'content-type: application/json' \
  -d '{"name":"fast","driver":"zfs","size":"30GiB"}'

curl -s -X POST localhost:8099/api/networks \
  -H 'content-type: application/json' \
  -d '{"name":"labbr0","config":{"ipv4.address":"10.20.0.0/24"}}'
curl -s -X POST localhost:8099/api/containers \
  -H 'content-type: application/json' \
  -d '{"name":"lab1","image":"images:alpine/3.21","network":"labbr0"}'
```

Creating a container and template launch, recreate and destroy block until they
finish by default, which suits scripts. With `"background": true` in the body
they validate, start the work on a server thread and return its progress record
straight away — follow it with `GET /api/creates` or `GET /api/template-runs`.

Storage mutations are limited to local `dir`, `btrfs`, `lvm` and `zfs`
drivers that the connected daemon reports as available. Other existing pools
are returned as read-only inventory. Instance and image volumes are also
read-only; only custom volumes on managed local pools can be changed.

Pool details include `delete_plan`, grouped into stored instances, instances
attached to custom volumes, cached images, custom volumes, profiles and unknown
references. A forced `DELETE` must send the exact pool name as `confirmation`
and that unchanged plan as `expected_plan`; otherwise it returns `409` without
deleting anything.

For ZFS, `DELETE` is deliberately a detach operation. If its backing zpool is
still imported, the request returns `409` after any confirmed cascade and asks
the operator to export it. Repeating the request after export removes the LXD
registration without destroying the zpool. A successful response includes
`"detached": true`; other drivers return `"detached": false` and retain their
normal destructive pool-delete behavior.

`/api/nodes/{node}/{path}` forwards any call to one member —
`/api/nodes/prdev2/containers/web-1/exec` runs a command on `web-1` over there.
The access checked is the target endpoint's, resolved against the same route
table, so forwarding grants nothing extra; an unrecognised path is treated as
admin-only, and a proxied call cannot be proxied again. Naming this node runs
the call locally.

In a cluster, `PUT /api/templates/{name}`, `POST /api/modules`,
`PUT /api/bootstrap-profiles/{name}` and `PUT /api/cluster/groups/{name}` push
the saved record to every other member, and the `DELETE` of each removes it from
them (`?everywhere=false` keeps a delete local). The answer carries `synced`:
`{ok, nodes, results, deleted}`, or null when the node is not federated. A
member relaying one of these never re-broadcasts it.

`/api/cluster/enroll` is the one endpoint that answers without a credential of
the usual kind, because it is how a node obtains its first one. The single-use
join code in its body *is* the credential, and what comes back is the cluster's
shared credential plus its whole member list — so joining any member joins the
cluster. A node that is in a cluster requires a token from callers on other
hosts even when authentication is otherwise off; loopback is unaffected. See
[Nodes and federation](cluster.md).

See also [Nodes and federation](cluster.md) for `/api/cluster`,
[Bootstrap modules](modules.md) for `/api/modules`,
`/api/bootstrap-profiles` and `/api/containers/{name}/bootstrap`,
[Instance templates](templates.md) for `/api/templates`, and
[Web UI tour](web-ui.md) for `/api/resources`, `/api/networks` and
`/api/images/browse`.

## Fabrics

| Endpoint | What it does |
| --- | --- |
| `GET /api/fabrics` | every fabric in the cluster, and each node's own report of its part (asks every member) |
| `GET /api/fabrics/check?name=&prefix=` | would this fabric fit on every node? Blank fields are filled with the first free name and /16 |
| `POST /api/fabrics` | create a fabric on every node: `{name, prefix, nat}`, any of them may be omitted |
| `POST /api/fabrics/{name}/extend` | give every member that lacks one a subnet in it |
| `DELETE /api/fabrics/{name}` | remove it from every node; refused while a template names it or any instance is attached |
| `GET /api/fabric` | this node's half: its subnet, bridge and per-peer routes in each fabric |
| `GET /api/fabric/host` | what this host already uses, which `check` asks each member for |
| `GET /api/fabric/plan` | the host commands that would bring routes and firewall up to date |
| `POST /api/fabric/apply` | create the bridges, program the routes and the firewall |
| `POST /api/fabric/instances/{name}/attach` | give an instance a NIC on a fabric, `{fabric}` (`detach` removes it) |

`PUT` and `DELETE /api/fabric/claims/{name}` exist too, but are members-only:
they are how the node a fabric is created or deleted on tells each peer.
Setting one node's subnet by hand is how two nodes end up holding the same
range.

See [Networking between nodes](networking.md).

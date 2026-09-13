# REST API

[← back to README](../README.md)

All responses are `{"data": ...}` on success and `{"error": "..."}` on failure,
with a matching HTTP status.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/status` | daemon flavor/version, readiness, pools, networks |
| `POST` | `/api/setup` | create pool + bridge, attach to default profile |
| `GET` | `/api/containers` | list with live state |
| `POST` | `/api/containers` | create (and optionally start); `"background":true` returns progress at once |
| `GET` | `/api/creates` | creates in progress (stage: creating/starting/bootstrapping) or finished in the last 10 minutes |
| `GET` | `/api/containers/{name}` | full detail, config, devices, snapshots |
| `PATCH` | `/api/containers/{name}` | update cpu/memory limits, description |
| `DELETE` | `/api/containers/{name}` | delete (`?force=true` stops it first) |
| `POST` | `/api/containers/{name}/state` | `{"action":"start\|stop\|restart\|freeze\|unfreeze"}` |
| `POST` | `/api/containers/{name}/rename` | `{"name":"new-name"}` |
| `POST` | `/api/containers/{name}/exec` | `{"command":"uname -a"}` → stdout/stderr/exit_code |
| `GET`/`POST` | `/api/containers/{name}/snapshots` | list / create |
| `DELETE` | `/api/containers/{name}/snapshots/{snap}` | delete |
| `POST` | `/api/containers/{name}/snapshots/{snap}/restore` | restore |
| `GET` | `/api/resources` | host CPU/memory/disk against what instances have allocated |
| `GET` | `/api/storage` | pools, volumes, local-driver availability and management status |
| `GET`/`POST` | `/api/storage/pools` | list pools / create a local pool |
| `GET`/`PATCH`/`DELETE` | `/api/storage/pools/{pool}` | inspect, update or remove a local pool (`?force=true` cascades) |
| `GET`/`POST` | `/api/storage/pools/{pool}/volumes` | list volumes / create a custom volume |
| `GET`/`PATCH`/`DELETE` | `/api/storage/pools/{pool}/volumes/custom/{volume}` | manage an unattached custom volume |
| `GET` | `/api/images` | cached images, suggested catalog, remotes |
| `GET` | `/api/images/browse` | full remote catalogs, flagged with what is local |
| `GET` | `/api/networks` | every interface the daemon can see |
| `GET` | `/api/networks/{name}` | config, state, DHCP leases, attachments |
| `GET` | `/api/modules` | bootstrap modules with their parameters |
| `GET` | `/api/ssh-keys` | public keys found in `~/.ssh` |
| `POST` | `/api/ssh-keys/validate` | check one pasted public key |
| `POST` | `/api/containers/{name}/bootstrap` | run modules in a container |
| `POST` | `/api/modules` | upload a module |
| `GET` | `/api/modules/{id}/source` | the module's script |
| `PUT` | `/api/modules/{id}/settings` | save its defaults / pre-select it |
| `DELETE` | `/api/modules/{id}` | remove an uploaded module |
| `GET` | `/api/bootstrap-profiles` | saved module selections |
| `PUT` | `/api/bootstrap-profiles/{name}` | create or replace one |
| `DELETE` | `/api/bootstrap-profiles/{name}` | delete one |
| `GET` | `/api/templates` | saved instance templates |
| `PUT` | `/api/templates/{name}` | create or replace one |
| `DELETE` | `/api/templates/{name}` | delete one |
| `POST` | `/api/templates/{name}/launch` | `{"count":3}` → create instances from it |
| `GET` | `/api/templates/{name}/instances` | names of the instances launched from it |
| `POST` | `/api/templates/{name}/exec` | `{"command":"uptime","instances":[...]}` → run on each, with output |
| `POST` | `/api/templates/{name}/recreate` | `{"instances":[...]}` → delete and recreate each, same names |
| `POST` | `/api/templates/{name}/destroy` | `{"instances":[...]}` → stop and delete each |
| `GET` | `/api/template-runs` | launches/recreates/destroys in progress, or last finished, per template |
| `DELETE` | `/api/template-runs/{name}` | dismiss a finished run |
| `GET` | `/api/profiles` | available profiles |

```bash
curl -s localhost:8099/api/containers | jq
curl -s -X POST localhost:8099/api/containers \
  -H 'content-type: application/json' \
  -d '{"name":"demo","image":"images:alpine/3.21","memory":"512MiB"}'

curl -s -X POST localhost:8099/api/storage/pools \
  -H 'content-type: application/json' \
  -d '{"name":"fast","driver":"zfs","size":"30GiB"}'
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

See also [Bootstrap modules](modules.md) for `/api/modules`,
`/api/bootstrap-profiles` and `/api/containers/{name}/bootstrap`,
[Instance templates](templates.md) for `/api/templates`, and
[Web UI tour](web-ui.md) for `/api/resources`, `/api/networks` and
`/api/images/browse`.

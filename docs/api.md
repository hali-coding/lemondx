# REST API

[← back to README](../README.md)

All responses are `{"data": ...}` on success and `{"error": "..."}` on failure,
with a matching HTTP status.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/status` | daemon flavor/version, readiness, pools, networks |
| `POST` | `/api/setup` | create pool + bridge, attach to default profile |
| `GET` | `/api/containers` | list with live state |
| `POST` | `/api/containers` | create (and optionally start) |
| `GET` | `/api/containers/{name}` | full detail, config, devices, snapshots |
| `PATCH` | `/api/containers/{name}` | update cpu/memory limits, description |
| `DELETE` | `/api/containers/{name}` | delete (`?force=true` stops it first) |
| `POST` | `/api/containers/{name}/state` | `{"action":"start\|stop\|restart\|freeze\|unfreeze"}` |
| `POST` | `/api/containers/{name}/rename` | `{"name":"new-name"}` |
| `POST` | `/api/containers/{name}/exec` | `{"command":"uname -a"}` → stdout/stderr/exit_code |
| `GET`/`POST` | `/api/containers/{name}/snapshots` | list / create |
| `DELETE` | `/api/containers/{name}/snapshots/{snap}` | delete |
| `POST` | `/api/containers/{name}/snapshots/{snap}/restore` | restore |
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
| `GET` | `/api/profiles` | available profiles |

```bash
curl -s localhost:8099/api/containers | jq
curl -s -X POST localhost:8099/api/containers \
  -H 'content-type: application/json' \
  -d '{"name":"demo","image":"images:alpine/3.21","memory":"512MiB"}'
```

See also [Bootstrap modules](modules.md) for `/api/modules`,
`/api/bootstrap-profiles` and `/api/containers/{name}/bootstrap`, and
[Web UI tour](web-ui.md) for `/api/networks` and `/api/images/browse`.

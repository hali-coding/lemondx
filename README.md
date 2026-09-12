# lemondx

A small web UI and CLI for managing local LXD or Incus containers.

Everything goes through one REST API: the web UI is a client of it, and so is
the CLI. The backend talks to the daemon over its unix socket, so there is no
shelling out and no parsing of CLI output.

```
┌──────────────┐        ┌──────────────┐        ┌──────────────┐
│ Vite + React │  HTTP  │ lemondx API  │  unix  │  LXD / Incus │
│    web UI    │───────▶│   (Python)   │───────▶│    daemon    │
└──────────────┘        └──────────────┘ socket └──────────────┘
       ▲                       ▲
       └── CLI ────────────────┘
```

## Requirements

- **LXD or Incus**, with the daemon running:
  - Ubuntu: `snap install lxd` (or `apt install lxd`)
  - Debian 13+: `apt install incus`
- Membership of the daemon's admin group — `lxd` for LXD, `incus-admin` for
  Incus. Check with `id`; after `usermod -aG`, run `newgrp <group>` or log out
  and back in.
- **Python 3.9+**. That is the whole dependency list — the backend is stdlib
  only, so there is no pip install, virtualenv or lockfile.

Node is **not** required to run lemondx; the built UI is committed. You only
need it if you want to change the frontend.

Not to be confused with liblxc's `lxc-create` / `lxc-ls` tools, which are a
different thing with no REST API. lemondx targets the LXD/Incus API.

## Quick start

```bash
git clone <repo> lemondx && cd lemondx
./lemondx serve --open
```

That is it — no build step. Then open <http://127.0.0.1:8099>.

Handy on PATH:

```bash
ln -s "$PWD/lemondx" ~/.local/bin/lemondx
```

On a host where the daemon has never been initialized, the UI shows a setup
banner with an **Initialize** button; the CLI equivalent is `./lemondx init`.

## Which daemon it talks to

Sockets are searched in this order, first match wins:

| Order | Path | Daemon |
| --- | --- | --- |
| 1 | `--socket` / `LEMONDX_SOCKET` / `INCUS_SOCKET` | inferred from the path |
| 2 | `$INCUS_DIR/unix.socket`, `$LXD_DIR/unix.socket` | Incus / LXD |
| 3 | `/var/snap/lxd/common/lxd/unix.socket` | LXD (snap) |
| 4 | `/var/lib/lxd/unix.socket` | LXD (deb) |
| 5 | `/var/lib/incus/unix.socket`, `/run/incus/unix.socket` | Incus |

Force one with `LEMONDX_FLAVOR=lxd` or `LEMONDX_FLAVOR=incus`. `lemondx status`
prints which socket and daemon it settled on.

The daemons know different image servers, and lemondx adapts: on LXD you get
`ubuntu:`, `ubuntu-daily`, `ubuntu-minimal` and `images:`
(images.lxd.canonical.com); on Incus just `images:`
(images.linuxcontainers.org), so Ubuntu is `images:ubuntu/24.04` there rather
than `ubuntu:24.04`. The image picker and the default for `lemondx create` both
follow whichever daemon is in use.

## Storage, and disk sizes

`lemondx init` creates a pool with the `dir` driver by default: it works
everywhere, needs no extra packages, and creates no large backing file.

The trade-off is that **`dir` cannot enforce a per-container disk size** unless
the backing filesystem has project quotas enabled. The daemon accepts the size,
logs `skipping set quota`, and ignores it. lemondx does not hide this:

- `lemondx status` marks such a pool `no disk quotas`
- `lemondx init` prints a note when it creates one
- `lemondx create -d ...` warns before creating
- the web UI warns under the Disk field, naming the pool and driver

If you want enforced disk quotas, initialize with a driver that supports them:

```bash
./lemondx init --storage btrfs --size 50GiB    # or zfs, lvm
```

CPU and memory limits are enforced on every driver; only disk size depends on
the pool.

## CLI

```
./lemondx status                 # which daemon, version, readiness
./lemondx init                   # create storage pool + bridge, wire up default profile
./lemondx ls                     # list containers
./lemondx info NAME              # details, limits, network, snapshots
./lemondx create NAME -i ubuntu:24.04 -c 2 -m 2GiB -d 10GiB
                                 # -i defaults to Ubuntu LTS for this daemon
./lemondx start|stop|restart|pause|resume NAME...
./lemondx delete NAME -y         # --force also stops it first
./lemondx exec NAME -- uname -a  # exit code is propagated
./lemondx shell NAME             # interactive shell (hands off to lxc/incus exec)
./lemondx snapshot NAME SNAP
./lemondx snapshots NAME
./lemondx restore NAME SNAP
./lemondx snap-delete NAME SNAP
./lemondx images                 # cached images + suggested aliases
./lemondx serve                  # web UI + API
```

Every command takes `--json` for scripting:

```bash
./lemondx ls --json | jq -r '.[] | select(.status=="Running") | .name'
```

Images are given as `remote:alias`; `lemondx images` lists what the current
daemon knows. A bare alias assumes `ubuntu:` on LXD and `images:` on Incus,
and `local:FINGERPRINT` uses an already-cached image.

## REST API

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
| `GET` | `/api/profiles` | available profiles |

```bash
curl -s localhost:8099/api/containers | jq
curl -s -X POST localhost:8099/api/containers \
  -H 'content-type: application/json' \
  -d '{"name":"demo","image":"images:alpine/3.21","memory":"512MiB"}'
```

## Frontend development

Only needed if you are changing the UI. `npm run dev` serves it with hot reload
on :5173 and proxies `/api` to the Python backend, so run both:

```bash
npm --prefix web install       # once
./lemondx serve --dev          # terminal 1
npm --prefix web run dev       # terminal 2 → http://localhost:5173
```

Point the proxy somewhere else with `LEMONDX_API=http://host:port npm run dev`.

`web/dist` is committed so clones run without Node. After changing anything
under `web/src`, rebuild and commit the result:

```bash
npm --prefix web run build
```

## Security

`serve` binds to `127.0.0.1` by default. The API can create, modify and delete
containers, so anyone who can reach the port controls your containers — there
is no per-user authorization beyond the token.

If you bind it to a routable address, set a token:

```bash
./lemondx serve --host 0.0.0.0 --port 8099 --token "$(openssl rand -hex 24)"
```

Clients then send `Authorization: Bearer <token>` (or `X-Lemondx-Token`). The
web UI prompts for the token the first time the API answers `401` and keeps it
in `sessionStorage` for that tab only.

Serving over plain HTTP sends that token in the clear; put it behind a
TLS-terminating proxy if it leaves the machine. The server warns when it binds
to a non-loopback address without a token. The static UI itself is served
without authentication — only `/api/*` is gated.

## Notes and limitations

- The in-browser console runs commands through `sh -c` non-interactively. It is
  not a terminal — no TTY, no streaming, no long-running processes. Use
  `./lemondx shell NAME` for a real shell.
- Creating a container blocks until the daemon finishes, which includes
  downloading the image the first time an alias is used. The UI shows a
  spinner throughout.
- `--json` output mirrors the REST payloads exactly.
- Virtual machines work wherever the daemon supports them, but need a
  VM-capable image.
- Incus support is implemented from its published API and socket layout but
  has not been exercised against a live Incus daemon; LXD 6.9 has.

## Layout

```
lemondx            # CLI entry point, runs from the checkout
src/lemondx/
  lxd.py           # LXD/Incus REST client over the unix socket
  service.py       # domain logic shared by the API and the CLI
  server.py        # HTTP routing, JSON API, static hosting
  cli.py           # argparse front end
web/               # Vite + React + TypeScript UI
  src/lib/api.ts   # typed client for the REST API
  dist/            # committed build output, served by `lemondx serve`
```

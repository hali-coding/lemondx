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

### Units

`--cpu` is a core count. `--memory` and `--disk` take a size: `4GiB`, `512MiB`,
`20GB`, or the shorthands `4G` / `512M`. A bare number is read as **GiB**, since
LXD's own reading of it — bytes — is never what anyone means in a size field
(`4` would be four bytes, and rejected as under the 1MiB minimum).

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
./lemondx modules                # bootstrap modules
./lemondx ssh-keys               # public keys found in ~/.ssh
./lemondx bootstrap NAME -b docker   # run modules on an existing container
./lemondx serve                  # web UI + API
```

Every command takes `--json` for scripting:

```bash
./lemondx ls --json | jq -r '.[] | select(.status=="Running") | .name'
```

Images are given as `remote:alias`; `lemondx images` lists what the current
daemon knows. A bare alias assumes `ubuntu:` on LXD and `images:` on Incus,
and `local:FINGERPRINT` uses an already-cached image.

## Bootstrap modules

A module is a plain shell script that runs *inside* a container after it
starts. Pick the ones you want when creating a container, or run them later
against an existing one.

```bash
./lemondx modules                       # what is available
./lemondx create dev -i images:debian/12 \
    -b base -b user -b ssh-server \
    --param USERNAME=hampus --all-ssh-keys
./lemondx bootstrap dev -b docker       # add more later; modules are re-runnable
```

In the web UI the create dialog has a **Bootstrap** section, and each container
has a **Bootstrap** tab with the same picker and a live log.

### Shipped modules

| id | what it does | parameters |
| --- | --- | --- |
| `base` | package index, curl, CA certs, sudo, editor | — |
| `ssh-server` | install and start OpenSSH | `PERMIT_ROOT_LOGIN`, `PASSWORD_AUTH` |
| `user` | user with passwordless sudo + your keys | `USERNAME`, `SHELL_PATH` |
| `ssh-keys` | add keys to an existing account | `TARGET_USER` |
| `docker` | Docker plus the service | — |
| `nodejs` | Node.js and npm | — |

Modules run in `order`, low to high, so `base` (10) precedes `user` (25)
whatever sequence you tick them in. A failing module stops the run and its
output is reported.

### SSH keys

Modules marked **ssh-keys** receive the keys you select as `LEMONDX_SSH_KEYS`,
and the prelude's `install_ssh_keys USER` writes them to that user's
`authorized_keys` with the right ownership and permissions.

Keys come from `~/.ssh/*.pub` (`--all-ssh-keys`, or ticked in the UI),
`--ssh-key PATH`, or pasted into the UI. Selecting a key-installing module
promotes the key picker into the main create dialog — outside the collapsible
Bootstrap section — and creation is blocked until at least one key is chosen,
since it would otherwise fail in the container. Every key is validated before it goes
anywhere: only known public-key types are accepted, anything containing
`PRIVATE KEY` is refused outright, and embedded newlines and control
characters are rejected so nothing can smuggle a second `authorized_keys`
entry or an options field past the parser. **Private keys are never read** —
only `*.pub` files are ever opened.

Accounts created by the `user` module get shadow field `*` (no usable
password) rather than `!` (locked): OpenSSH built without PAM, as on Alpine,
refuses a locked account even for key authentication.

### Writing your own

Drop a `.sh` file in `modules/`, or in `~/.config/lemondx/modules` to keep it
outside the repo (a file there shadows a shipped module of the same name).
`LEMONDX_MODULES` adds more directories.

```sh
#!/bin/sh
# name: Redis
# description: Install Redis and start it.
# order: 60
# param: REDIS_PORT=6379  Port to listen on

pkg_install redis
svc_enable redis
log "redis on port ${REDIS_PORT}"
```

Every module is prepended with `modules/_prelude.sh`, which provides
`log` / `warn` / `die`, `have`, `pkg_refresh`, `pkg_install`, `svc_enable` and
`install_ssh_keys`, and sets `LEMONDX_OS_ID` and `LEMONDX_PKG`. The package
helpers cover apt, dnf/yum/microdnf, apk, pacman and zypper; `svc_enable`
handles systemd and OpenRC.

Modules run under `/bin/sh` — dash on Debian, busybox ash on Alpine — because
minimal images often have no bash. **Keep them POSIX.** `dash -n module.sh`
catches most mistakes.

Declared `param` values arrive as environment variables, with the declared
default applied when you do not override it.

### If modules cannot install anything

Bootstrap checks outbound connectivity first and stops with an explanation
rather than letting a package manager hang. The usual culprit on a host that
also runs Docker is Docker setting the iptables `FORWARD` policy to `DROP`,
which blocks the container bridge:

```bash
sudo iptables -S FORWARD | head -1          # says: -P FORWARD DROP
sudo iptables -I DOCKER-USER -i lxdbr0 -j ACCEPT
sudo iptables -I DOCKER-USER -o lxdbr0 -j ACCEPT
```

These rules are not persistent; save them if you want them after a reboot.

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
| `GET` | `/api/modules` | bootstrap modules with their parameters |
| `GET` | `/api/ssh-keys` | public keys found in `~/.ssh` |
| `POST` | `/api/ssh-keys/validate` | check one pasted public key |
| `POST` | `/api/containers/{name}/bootstrap` | run modules in a container |
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
  bootstrap.py     # module discovery, SSH key validation, the runner
modules/           # bootstrap modules (POSIX sh)
  _prelude.sh      # helpers prepended to every module
web/               # Vite + React + TypeScript UI
  src/lib/api.ts   # typed client for the REST API
  dist/            # committed build output, served by `lemondx serve`
```

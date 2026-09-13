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
./lemondx limits NAME -c 4 -m 8GiB   # change an existing container's limits
                                 # pass '' to clear one: -m ''
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
./lemondx module-add FILE.sh     # install a module (--default to pre-select)
./lemondx module-set ID --param K=V --default
./lemondx profiles               # saved bootstrap profiles
./lemondx profile-save NAME -b base -b docker
./lemondx serve                  # web UI + API
```

Every command takes `--json` for scripting:

```bash
./lemondx ls --json | jq -r '.[] | select(.status=="Running") | .name'
```

Images are given as `remote:alias`; `lemondx images` lists what the current
daemon knows. A bare alias assumes `ubuntu:` on LXD and `images:` on Incus,
and `local:FINGERPRINT` uses an already-cached image.

## Network view

The **Network** tab explains how container networking is actually wired up,
rather than just listing interfaces: the bridge's IPv4/IPv6 subnets, whether
NAT and DHCP are on, the DNS domain, MTU and bridge MAC — followed by a
plain-English summary ("containers get an address on 10.x/24 by DHCP, with
10.x.1 as their gateway and DNS resolver…"), what is attached (profiles and
instances), and the live DHCP lease table showing which container holds which
address.

Host interfaces the daemon does not manage (`docker0`, physical NICs) are
listed separately so it is clear what is and is not under its control.

From the CLI:

```bash
curl -s localhost:8099/api/networks | jq
curl -s localhost:8099/api/networks/lxdbr0 | jq '.ipv4, .leases'
```

## Browsing images

The create dialog offers a short list of common images, then **Browse all
images…** opens the full catalog of every remote the daemon knows — read
straight from their simplestreams indexes.

- search by alias, OS, version or codename (`bookworm` finds Debian 12)
- filter by remote, or tick **Downloaded only**
- images already on this host are marked **Downloaded**; everything else shows
  its download size

"Downloaded" is exact, not a guess: simplestreams publishes the same combined
SHA256 that the daemon stores as an image fingerprint, so container and VM
variants are tracked separately — you can have the VM image of a release
without the container one.

Catalogs are cached for 15 minutes. `GET /api/images/browse?refresh=true`
forces a re-read, and `?remote=ubuntu-daily` browses a remote that is not in
the default set.

## Bootstrap modules

A module is a plain shell script that runs *inside* a container after it
starts. Pick the ones you want when creating a container, or run them later
against an existing one.

```bash
./lemondx modules                       # what is available
./lemondx create dev -i images:debian/12 \
    -b base -b ssh-access \
    --param USERNAME=hampus --all-ssh-keys
./lemondx bootstrap dev -b docker       # add more later; modules are re-runnable
```

In the web UI the create dialog has a **Bootstrap** section, and each container
has a **Bootstrap** tab with the same picker and a live log.

### Shipped modules

| id | what it does | parameters |
| --- | --- | --- |
| `base` | package index, curl, CA certs, sudo, editor | — |
| `ssh-access` | install OpenSSH, create a user, install your key for it | `USERNAME`, `SHELL_PATH`, `PERMIT_ROOT_LOGIN`, `PASSWORD_AUTH` |
| `docker` | Docker plus the service | — |
| `nodejs` | Node.js and npm | — |
| `postgresql` | PostgreSQL, a role with a password, optional database | `POSTGRES_PASSWORD` (secret), `POSTGRES_USER`, `POSTGRES_DB`, `LISTEN_ADDRESSES` |

Modules run in `order`, low to high, so `base` (10) precedes `ssh-access` (20)
whatever sequence you tick them in. A failing module stops the run and its
output is reported.

`ssh-access` used to be three separate modules (a server, a user, and a key
installer), pickable independently. In practice every real use needed all
three -- a user with no way in, or a server with no one to log in as, isn't
useful on its own -- so they are one module now: installing sshd, creating
the account and putting your key on it always happen together, and a key is
mandatory rather than optional.

### SSH keys

A module declares `uses: ssh-keys` in its header (only `ssh-access` does, but a
module you write can too) to receive the keys you select as `LEMONDX_SSH_KEYS`;
the prelude's `install_ssh_keys USER` writes them to that user's
`authorized_keys` with the right ownership and permissions. Selecting such a
module makes the runner refuse to start without at least one key.

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

Accounts created by `ssh-access` get shadow field `*` (no usable
password) rather than `!` (locked): OpenSSH built without PAM, as on Alpine,
refuses a locked account even for key authentication.

### Defaults, saved settings and profiles

The **Modules** tab manages all of this; the CLI mirrors it.

**Defaults.** Tick *Default* on a module and it is pre-selected every time you
create a container.

```bash
./lemondx module-set docker --default
./lemondx create dev -i images:debian/12      # runs your defaults
./lemondx create bare --no-default-modules    # opt out
```

**Saved settings.** A module's parameters are remembered. Edit them in the
Modules tab, or just run a bootstrap — values that differed from the module's
own default are saved automatically, so the next container starts from what
worked last time. The UI marks these *saved* and shows the module's original
value beside them. Blank a field to go back to that original.

```bash
./lemondx module-set ssh-access --param USERNAME=hampus --param SHELL_PATH=/bin/sh
```

**Bootstrap profiles** are named module selections with their parameters —
"Save as profile" in the create dialog, or:

```bash
./lemondx profile-save "Web server" -b base -b ssh-access -b docker \
    --param USERNAME=hampus --description "my usual dev box"
./lemondx profiles
./lemondx create web -P "Web server" --all-ssh-keys
```

Deleting a module drops it from every profile that used it; a profile left with
nothing to run is removed rather than kept as an empty shell.

These are lemondx's own; they have nothing to do with LXD/Incus profiles, which
configure devices and limits. The CLI flag is `-P/--bootstrap-profile`, because
`--profile` already means the daemon's kind.

### Where all this is kept

Everything lemondx remembers lives under `~/.local/share/lemondx`, and is meant
to be readable and editable by hand:

```
~/.local/share/lemondx/
  settings.json         default modules and remembered parameter values
  profiles/
    web-server.json     one file per bootstrap profile
  modules/
    redis.sh            uploaded modules
```

`LEMONDX_DATA_DIR` points the whole thing elsewhere; `XDG_DATA_HOME` is
honoured if you set it.

A profile is one JSON file, named after a slug of the profile name, with the
real name inside it:

```json
{
  "name": "Web server",
  "description": "my usual dev box",
  "modules": ["base", "ssh-access", "docker"],
  "params": { "USERNAME": "hampus" }
}
```

So a profile can be copied between machines, or checked into a project and
dropped in — anything in `profiles/` is picked up, and a file mangled into
invalid JSON costs that one profile rather than all of them.

Earlier versions kept all of this in `~/.config/lemondx`. That directory is
moved here the first time you run a newer lemondx, profiles included, so there
is nothing to do by hand. `LEMONDX_CONFIG_DIR` still works and still names the
whole directory; a directory you name explicitly is never migrated away from.

### Uploading modules

Drop a `.sh` file into `~/.local/share/lemondx/modules`, or upload it through
the Modules tab (choose a file or paste the script). From the CLI:

```bash
./lemondx module-add ./redis.sh --default
./lemondx module-remove redis
```

Uploads are validated before they are stored: the name is reduced to a safe id
that cannot escape the module directory, the content must be UTF-8 text under
256 KiB, and it must parse as POSIX shell (`sh -n`, which parses without
running anything). A module with the same name as a built-in shadows it rather
than replacing it, and built-ins can never be deleted.

Uploading does not execute anything. The script runs later, inside a container,
and only when you select it.

### Writing your own

Drop a `.sh` file in `modules/`, or in `~/.local/share/lemondx/modules` to keep
it outside the repo (a file there shadows a shipped module of the same name).
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

### Secret parameters

Declare a password, token or key with `secret:` instead of `param:` — same
grammar, different handling:

```sh
# secret: POSTGRES_PASSWORD=  Password for the role (at least 8 characters)
```

A secret has no default (anything after `=` is ignored) and must be supplied on
every run; bootstrap refuses to start without it. It is never remembered as a
setting, cannot be saved with `module-set`, is stripped from bootstrap profiles
(including hand-edited ones), and every occurrence of its value is replaced
with `********` in the output shown in the UI, the terminal and `--json`. The
UI renders it as a password field and clears it once used.

From the CLI, avoid `--param` for secrets — it lands in shell history and
`ps`. Instead set an environment variable of the same name, or let lemondx
prompt without echo:

```bash
./lemondx create db -i images:debian/12 -b postgresql      # prompts
POSTGRES_PASSWORD="$(pass show db)" ./lemondx bootstrap db -b postgresql
```

Redaction is a safety net, not a licence: a module should still never print a
secret, and should keep it off command lines inside the container too (pipe it
through stdin; `printf` is a shell builtin, so it reaches no argv).

### The PostgreSQL module

`postgresql` installs the server, sets a role's password (creating the role if
needed), optionally creates a database it owns, and controls where it accepts
TCP connections. Verified on Debian 12, Alpine 3.21, Rocky Linux 9 and Arch.

```bash
./lemondx create db -i images:debian/12 -b postgresql \
    --param POSTGRES_USER=app --param POSTGRES_DB=appdb --param 'LISTEN_ADDRESSES=*'
psql -h <container-ip> -U app -d appdb
```

It prepends a managed block to `pg_hba.conf` — first match wins there — so
the password is actually required on every distro: peer authentication on the
local socket, `scram-sha-256` for every TCP connection. Distro defaults cannot
be relied on for this: Alpine's initdb writes `trust`, which lets any password
(or none) log in over localhost, and Fedora/Rocky write `ident`, which makes
password logins fail. The block is regenerated on each run, so setting
`LISTEN_ADDRESSES` back to `localhost` removes the remote rules. Rerunning the
module is also how you change the password.

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

## Running as a service

```bash
./systemd/install-service.sh              # user service, runs as you (recommended)
sudo ./systemd/install-service.sh --system   # system service, runs as a dedicated account
./systemd/install-service.sh --uninstall     # remove
```

Prefer the user service unless the box has no interactive login: lemondx needs
your `lxd`/`incus-admin` group membership to reach the daemon, reads your
`~/.ssh/*.pub` for the SSH-key picker, and keeps its state under your
`~/.local/share/lemondx` — a system service running as a separate account
would need all three set up by hand for an account with no login shell to set
them up with. Both units bind to `127.0.0.1:8099` and run under
`ProtectSystem=strict`/`ProtectHome=read-only` with the state directory as the
one exception, since lemondx has no other reason to touch the filesystem.

```bash
systemctl --user status lemondx     # or plain systemctl for --system
journalctl --user -u lemondx -f
```

A user service only starts after you log in; for it to survive to boot with no
session open, `sudo loginctl enable-linger $USER` (the installer prints this
when it applies). The unit files are in `systemd/` if you want to edit the
host/port or add `--token` by hand before installing.

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
  simplestreams.py # reads remote image catalogs, with caching
  store.py         # persistent state under ~/.local/share/lemondx
modules/           # bootstrap modules (POSIX sh)
  _prelude.sh      # helpers prepended to every module
web/               # Vite + React + TypeScript UI
  src/lib/api.ts   # typed client for the REST API
  dist/            # committed build output, served by `lemondx serve`
```

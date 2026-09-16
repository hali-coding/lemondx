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

![Containers list](docs/screenshot1.png)


## Requirements

- **LXD or Incus**, with the daemon running:
  - Ubuntu: `snap install lxd` (or `apt install lxd`)
  - Debian 13+: `apt install incus`
- Membership of the daemon's admin group — `lxd` for LXD, `incus-admin` for
  Incus. Check with `id`; after `usermod -aG`, run `newgrp <group>` or log out
  and back in.
- **Python 3.9+**. That is the whole dependency list — the backend is stdlib
  only, so there is no pip install, virtualenv or lockfile.

Node is **not** required to run a release: the archive on the
[releases page](../../releases) already contains the built UI. You only need it
to build from a clone or to change the frontend — see
[Development](docs/development.md).

Not to be confused with liblxc's `lxc-create` / `lxc-ls` tools, which are a
different thing with no REST API. lemondx targets the LXD/Incus API — see
[Daemons, images and storage](docs/daemons-and-storage.md) for exactly how it
finds and adapts to whichever one is installed.

## Quick start

Download the latest release — Python 3.9+ is all it needs, the UI is already
built:

```bash
curl -LO <repo>/releases/latest/download/lemondx-<version>.tar.gz
tar xzf lemondx-<version>.tar.gz && cd lemondx-<version>
./lemondx serve --open
```

Then open <http://127.0.0.1:8099>.

From a clone instead — the UI is a build output and is not in git, so build it
once. `./build.sh` checks for a usable Node toolchain, installs the frontend
dependencies and builds; it is the only step that needs Node:

```bash
git clone <repo> lemondx && cd lemondx
./build.sh
./lemondx serve --open
```

Handy on PATH:

```bash
ln -s "$PWD/lemondx" ~/.local/bin/lemondx
```

On a host where the daemon has never been initialized, the UI shows a setup
banner with an **Initialize** button; the CLI equivalent is `./lemondx init`.
Want it running all the time? See [Running as a service](docs/service.md).

## CLI

```
./lemondx status                 # which daemon, version, readiness
./lemondx init                   # create storage pool + bridge, wire up default profile
./lemondx ls                     # list containers
./lemondx info NAME              # details, limits, network, snapshots
./lemondx resources              # allocated CPU/memory/disk against the host
./lemondx health                 # are running instances responsive, overloaded, near their limits?
./lemondx storage pools          # local pools, capacity and management status
./lemondx storage volumes        # custom, instance and image volumes
./lemondx network ls             # networks; network create|set|delete manage bridges
./lemondx create NAME -i ubuntu:24.04 -c 2 -m 2GiB -d 10GiB
                                 # -i defaults to Ubuntu LTS for this daemon
                                 # --pool and --network pick where it lands
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
./lemondx template-save NAME -i images:debian/12 -c 2 -b base   # a whole instance setup
./lemondx launch NAME -n 3       # create NAME's instances: prefix-1..3
                                 # --node/--group spread them over a cluster
./lemondx template-recreate|template-destroy NAME   # every instance from NAME
./lemondx template-exec NAME -- uptime              # on every running one
./lemondx cluster status         # this node and the lemondx nodes it federates with
./lemondx cluster invite         # start a cluster here and print a one-time join code
./lemondx cluster add CODE       # join that cluster -- needs no setup on this host
./lemondx cluster refresh        # level the member list with every other node
./lemondx cluster sync --group edge  # copy templates and modules to other nodes
./lemondx serve                  # web UI + API
```

Every command takes `--json` for scripting:

```bash
./lemondx ls --json | jq -r '.[] | select(.status=="Running") | .name'
```

CPU is a core count; memory and disk take a unit like `4GiB` or `512MiB` — see
[Units](docs/daemons-and-storage.md#units) for the full rules, and
[Image aliases](docs/daemons-and-storage.md#image-aliases) for what you can
pass to `-i`.

![Container detail panel](docs/screenshot2.png)

## Documentation

| Doc | Covers |
| --- | --- |
| [Bootstrap modules](docs/modules.md) | writing and uploading modules, defaults, saved settings, profiles, secrets, the shipped modules |
| [Instance templates](docs/templates.md) | saving a full instance setup and launching one or many from it |
| [Nodes and federation](docs/cluster.md) | joining nodes securely, node groups, launching and syncing across them |
| [REST API](docs/api.md) | every endpoint, request/response shapes, curl examples |
| [Daemons, images and storage](docs/daemons-and-storage.md) | LXD vs Incus, socket discovery, image remotes, disk quotas, units |
| [Web UI tour](docs/web-ui.md) | the Resources and Network views, the full image browser |
| [Running as a service](docs/service.md) | systemd units, user vs system install |
| [Security](docs/security.md) | logins (local users, PAM, proxy), roles, API tokens, TLS |
| [Development](docs/development.md) | Vite dev server, building `web/dist`, how releases are cut |

## Security

`serve` binds to `127.0.0.1` by default with authentication off — anyone who
can reach the port controls your containers. Run `lemondx configure auth` to
turn on logins — local users, PAM, a trusted proxy or API tokens, with read-only
and admin roles — or pass `--auth` flags to `serve`, and
use `--tls-cert`/`--tls-key` or a TLS proxy before exposing it; see
[Security](docs/security.md).

![Network view](docs/screenshot3.png)

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
- Federating nodes is not LXD clustering. Each node runs its own lemondx against
  its own daemon and they only learn how to call each other, so there is no
  shared state, no leader and no live migration. Members share one credential,
  so every node in a cluster is an admin of every other — federate hosts you
  administer. See [Nodes and federation](docs/cluster.md).

## Layout

```
lemondx            # CLI entry point, runs from the checkout
src/lemondx/
  lxd.py           # LXD/Incus REST client over the unix socket
  service.py       # domain logic shared by the API and the CLI
  server.py        # HTTP routing, JSON API, static hosting
  cli.py           # argparse front end
  bootstrap.py     # module discovery, SSH key validation, the runner
  cluster.py       # federated nodes: enrolment, groups, sync, cluster launches
  nodeclient.py    # REST client for another node, over pinned TLS
  simplestreams.py # reads remote image catalogs, with caching
  store.py         # persistent state under ~/.local/share/lemondx
modules/           # bootstrap modules (POSIX sh)
  _prelude.sh      # helpers prepended to every module
web/               # Vite + React + TypeScript UI
  src/lib/api.ts   # typed client for the REST API
  dist/            # committed build output, served by `lemondx serve`
docs/              # everything below -- REST API, modules, storage, security...
```

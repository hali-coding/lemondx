# lemondx

Application orchestration on LXD and Incus. Describe a deployment once, as a
**stack** of instance templates, and lemondx launches it in order across a set
of hosts, hands each tier the addresses of the ones before it, waits for them
to be healthy, and from then on runs it as one thing: stop, start, relaunch and
destroy the whole deployment, see when it has drifted from its definition,
watch it live.

```
  stack "shop"                     node-a          node-b          node-c
  ──────────────────────────────   ─────────────   ─────────────   ─────────────
  1  db    Postgres x1        ──▶  shop-db-1
  2  wait until healthy
  3  app   App x3             ──▶  app-1           app-2           app-3
       DB_HOST={{db.ip}}           └──────────── fabric 10.101.0.0/16 ─────────┘
  4  load  Runner x6          ──▶  … round robin over the "small" group …
```

There is a web UI, a CLI and a REST API, and they are the same thing: the UI
and the CLI are both clients of the one API. The backend is stdlib-only Python
that talks to each host's daemon over its unix socket.

![Containers list](docs/screenshot1.png)

## How it fits together

Each layer is built from the one before it, and a stack is what you deploy:

| | What it is | Docs |
| --- | --- | --- |
| **Module** | A POSIX shell script that configures an instance: packages, users, services. Parameters, and secrets that are never stored. | [modules](docs/modules.md) |
| **Template** | A whole instance: image, container or VM, limits, network, the modules to run and an app health check. | [templates](docs/templates.md) |
| **Stack** | Templates launched in stages, each handed the names and addresses of earlier ones, with health gates between. **The unit you deploy and operate.** | [stacks](docs/stacks.md) |
| **Nodes** | Several hosts, each with its own daemon and its own lemondx, joined with one code. A stack step lands on named nodes or a node group. | [nodes](docs/cluster.md) |
| **Fabric** | A routed network across every node, so a stack's tiers reach each other by their own addresses, whichever host they are on. | [networking](docs/networking.md) |

Health checks — the machine, plus each template's app check — are what a
stack's gates wait on, and what the UI and `lemondx top` show.

## Recommended deployment: several nodes

lemondx works on one host, and that is the easiest way to try it. It is meant
for **several**: a stack spread over nodes survives losing a host, can put each
tier where there is room for it, and is the case the fabric, node groups and
cluster-wide views are built for. A typical setup:

1. **Two or more hosts on one L2 network**, each with LXD or Incus and its own
   lemondx [run as a service](docs/service.md). Turn on logins before
   exposing it — see [security](docs/security.md).
2. **Join them:** `lemondx cluster invite` on one, `lemondx cluster join CODE`
   on each of the others. Nothing else needs configuring. There is no leader,
   and a node that is down costs you that node only.
3. **Group them:** `lemondx cluster group auto` sorts nodes into `large` and
   `small` by what they have, or make your own groups for steps to target.
4. **Connect them:** `lemondx fabric create` gives every node a routed network
   its instances share.
5. **Write templates and a stack, and launch it.** Templates, modules, profiles
   and stacks are pushed to every node as you save them, so any node can launch
   and operate the deployment, and a node that was off catches up by itself.

## What lemondx is not

- **Not a host management tool.** It does some host work — initialising a
  daemon (`lemondx init`), storage pools and bridges, a maintenance mark that
  keeps new instances off a node, a view of what instances have claimed
  against a host — because a deployment needs a host that is ready and you
  need to see what it is using. Provisioning, patching and monitoring the hosts
  themselves belong to the tools built for that.
- **Not LXD or Incus clustering.** Those make several daemons one database and
  one API. lemondx sits above that: each node talks to its own daemon,
  clustered or not, and nodes only learn how to call each other.
- **Not a scheduler.** A step's instances go round robin over the nodes it
  names. Nothing is moved or restarted elsewhere when a host fails, and there
  is no live migration: an instance belongs to the node that made it.

## Requirements

- **LXD or Incus** on every host, with the daemon running:
  - Ubuntu: `snap install lxd` (or `apt install lxd`)
  - Debian 13+: `apt install incus`
- Membership of the daemon's admin group — `lxd` for LXD, `incus-admin` for
  Incus. Check with `id`; after `usermod -aG`, run `newgrp <group>` or log out
  and back in.
- **Python 3.10+**. That is the whole dependency list — the backend is stdlib
  only, so there is no pip install, virtualenv or lockfile.

Node is **not** required to run a release: the archive on the
[releases page](../../releases) already contains the built UI. You only need it
to build from a clone or to change the frontend — see
[Development](docs/development.md).

Not to be confused with liblxc's `lxc-create` / `lxc-ls` tools, which have no
REST API. lemondx targets the LXD/Incus API — see
[Daemons, images and storage](docs/daemons-and-storage.md) for how it finds
and adapts to whichever one is installed.

## Quick start

Download the latest release — Python 3.10+ is all it needs, the UI is already
built:

```bash
curl -LO <repo>/releases/latest/download/lemondx-<version>.tar.gz
tar xzf lemondx-<version>.tar.gz && cd lemondx-<version>
./lemondx serve --open
```

Then open <http://127.0.0.1:8099>. On a host whose daemon has never been
initialised, the UI offers **Initialize**; the CLI equivalent is
`./lemondx init`.

From a clone instead, build the UI once — `./build.sh` checks for a usable
Node toolchain, installs the frontend dependencies and builds; it is the only
step that needs Node:

```bash
git clone <repo> lemondx && cd lemondx
./build.sh
./lemondx serve --open
```

Handy on PATH: `ln -s "$PWD/lemondx" ~/.local/bin/lemondx`. From there, run it
as a service on each host and join them — see
[the recommended deployment](#recommended-deployment-several-nodes).

## CLI

```
# stacks: the deployment
lemondx stacks                   # every stack, and how many instances it runs
lemondx stack-save NAME FILE     # from a JSON definition; stack-show prints one back
lemondx stack-launch NAME        # run it step by step; --relaunch replaces what runs
lemondx stack-stop|stack-start|stack-restart NAME
lemondx stack-destroy NAME

# templates, modules and profiles: what a stack is built from
lemondx template-save NAME -i images:debian/12 -c 2 -b base   # a whole instance setup
lemondx templates
lemondx launch NAME -n 3 --group large     # a template on its own, over nodes
lemondx template-recreate|template-destroy|template-exec NAME
lemondx modules                  # module-add FILE.sh, module-set ID --param K=V
lemondx profiles                 # profile-save NAME -b base -b docker

# the cluster
lemondx cluster invite           # start a cluster here and print a one-time join code
lemondx cluster join CODE        # join it -- needs no setup on this host
lemondx cluster status           # this node and every member
lemondx cluster containers       # every instance on every node, with health
lemondx cluster group auto       # size nodes into large/small groups
lemondx cluster maintenance on   # keep new instances off this node
lemondx fabric create            # a routed network across every node

# watching it
lemondx top                      # live view of nodes, instances, stacks and templates;
                                 # console, bootstrap, stack and template actions
lemondx health                   # are instances responsive, overloaded, near limits?
lemondx status                   # daemon, readiness, cluster membership

# single instances, on this node
lemondx ls                       # info NAME for one; create NAME -i ubuntu:24.04 -c 2
lemondx start|stop|restart|delete NAME...
lemondx exec NAME -- uname -a    # shell NAME for an interactive one
lemondx bootstrap NAME -b docker # run modules on an existing instance
lemondx snapshot NAME SNAP       # snapshots, restore, snap-delete
lemondx snapshot-publish NAME SNAP ALIAS --to NODE   # as an image, copied to nodes

# this host, to get it ready
lemondx init                     # storage pool + bridge + default profile
lemondx resources                # what instances have claimed against the host
lemondx storage pools            # storage volumes; network ls|create|set|delete

lemondx serve                    # web UI + API
```

Every command takes `--json` and prints exactly what the REST API returns:

```bash
lemondx cluster containers --json | jq -r '.instances[] | select(.stack=="shop") | .name'
```

CPU is a core count; memory and disk take a unit like `4GiB` or `512MiB` — see
[Units](docs/daemons-and-storage.md#units) and
[Image aliases](docs/daemons-and-storage.md#image-aliases).

![Container detail panel](docs/screenshot2.png)

## Documentation

| Doc | Covers |
| --- | --- |
| [Stacks](docs/stacks.md) | designing a deployment: stages, launch steps, handing addresses along, health gates, secrets, operating it |
| [Instance templates](docs/templates.md) | a full instance setup, launched by a stack or on its own; app health checks |
| [Bootstrap modules](docs/modules.md) | writing and uploading modules, parameters, secrets, profiles, the shipped modules |
| [Nodes and federation](docs/cluster.md) | joining nodes, node groups, maintenance, sync and reconciliation, `lemondx top` |
| [Networking between nodes](docs/networking.md) | fabrics: routed networks shared by every node |
| [Web UI tour](docs/web-ui.md) | Home, health checks, bulk actions, the Storage, Network and Access views |
| [Security](docs/security.md) | logins (local users, PAM, proxy), roles, API tokens, TLS |
| [Running as a service](docs/service.md) | systemd units, user vs system install |
| [REST API](docs/api.md) | every endpoint, request and response shapes, curl examples |
| [Daemons, images and storage](docs/daemons-and-storage.md) | LXD vs Incus, socket discovery, image remotes, disk quotas, units |
| [Development](docs/development.md) | Vite dev server, building `web/dist`, how releases are cut |

## Security

`serve` binds to `127.0.0.1` by default with authentication off — anyone who
can reach the port controls your instances. Run `lemondx configure auth` to
turn on logins — local users, PAM, a trusted proxy or API tokens, with read,
operator and admin roles — and `lemondx configure tls` (or a TLS proxy) before
exposing it. A cluster member refuses other hosts without a credential even
with logins off. Members share one credential, so every node in a cluster is
an admin of every other: federate hosts you administer. See
[Security](docs/security.md).

![Network view](docs/screenshot3.png)

## Notes and limitations

- The web UI's console runs commands through `sh -c`, non-interactively. For a
  real terminal use `lemondx shell NAME`, or `c` in `lemondx top`, which
  attaches to an instance's console on any node.
- Launches, creates and stack runs happen on the server: closing the browser
  does not stop them, stopping `serve` does.
- Virtual machines work wherever the daemon supports them, but need a
  VM-capable image.
- LXD and Incus are both supported, and a cluster may mix them; every
  difference between the two is a table at the top of `lxd.py`.

## Layout

```
lemondx            # CLI entry point, runs from the checkout
src/lemondx/
  lxd.py           # LXD/Incus REST client over the unix socket
  service.py       # domain logic for this node, shared by the API and the CLI
  stacks.py        # stacks: staged launches, values between steps, health gates
  cluster.py       # federation: joining, groups, sync, reconciliation, cluster launches
  fabric.py        # fabrics: routed networks across nodes
  hostnet.py       # the host's routes and firewall -- the one place that shells out
  health.py        # health judgement, load sampling, app checks
  auth.py, pam.py  # logins, roles, API tokens
  server.py        # HTTP routing, JSON API, terminals, static hosting
  cli.py, top.py   # argparse front end; the live `lemondx top` view
  bootstrap.py     # module discovery, SSH key validation, the runner
  nodeclient.py    # REST client for another node, over pinned TLS
  store.py         # persistent state under ~/.local/share/lemondx
modules/           # bootstrap modules (POSIX sh); _prelude.sh is prepended to each
web/               # Vite + React + TypeScript UI; builds to web/dist, not in git
systemd/           # service units and installer
docs/              # everything linked above
```

# Web UI tour

[← back to README](../README.md)

Parts of the web UI that are easy to miss: bulk start and stop, health checks,
the Home, Storage, Network and Access views, and the full image browser.

## Starting and stopping several at once

Each row on the Containers tab has a checkbox, and the one in the header ticks
every container that is not already busy — a container mid-create is left out,
since starting or stopping it would pull the rug out from under its bootstrap.
Ticking any of them raises a bar above the table with **Start**, **Stop** and
**Clear**, and either action asks for confirmation naming the containers it is
about before anything happens.

One request carries the whole list (`POST /api/containers/state`) and the server
applies the action to each in parallel, reporting every outcome separately: a
container the daemon refuses — one already stopped, say — is that container's
result, not a failure for the rest. The notification afterwards says so,
naming only the ones that did not do as asked. `lemondx start`/`stop` take
several names for the same reason and go through the same code.

## Logging in and the Access view

With `--auth` on, the UI asks for a username and password (or an API token,
when the server accepts tokens) the first time the API answers `401`. The
header then shows who you are and a **Log out** button. A read-only user gets a
banner and disabled controls for anything that would change state; the server
refuses those requests anyway. Views remount on login, so nothing loaded as one
user stays on screen for the next.

**Access** lists your API tokens — create one with a name, access level and
expiry, copy the secret from the dialog (it is not shown again), or revoke one.
Admins see every user's tokens and manage local users there too: add, change
access, set a password (which ends that user's sessions) or remove. With auth
off the tab still works, so tokens and users can be prepared before turning it
on. See [Security](security.md) for the methods and what each needs.

## Creating in the background

Creating a container, especially with bootstrap modules, can take minutes, so
the web UI never waits for it. Pressing **Create** hands the job to the server,
which checks what it can up front (name, pool, a create of the same name already
running) and then runs the rest on its own thread; the dialog closes as soon as
the server has accepted it. The container list shows the instance with its
stage — *creating*, *starting*, *bootstrapping · N modules* — and a notification
says how it went, whichever tab you are on by then. Leaving the page, reloading
or closing the browser changes nothing: the server keeps track of each create
(`GET /api/creates`) for ten minutes after it finishes. Start and stop are
disabled on the row until then; delete stays available.

Template launches, recreates and destroys behave the same way on the
Templates tab — see [Instance templates](templates.md).

That work belongs to the `serve` process, so stopping the server does stop it.
Ctrl-C lists what is still running and waits for it to finish; a second Ctrl-C
abandons it, which can leave an instance created but not bootstrapped. Stopping
the systemd unit, or killing the process, abandons it straight away.

Only the `serve` process's own creates are tracked: one started with
`lemondx create` in a terminal appears in the list once the daemon has it,
without a stage.

## API explorer

The web UI does nothing the [REST API](api.md) cannot, and the dialogs that
create, change or delete something show how. The **API** button at the top
right of such a dialog (new container, templates, stacks, modules, storage,
networks, fabrics, users, node groups, invites, sync) opens a panel beside the
form with the request its main button would send, as curl, JavaScript or
Python, and it follows the form while you fill it in. It is built from the same
code that sends the real request, so the two cannot differ.

Secrets typed into the form (bootstrap secrets, passwords, a join code) are
shown as `<name>` placeholders rather than their values. With authentication
on, the examples read a token from `LEMONDX_API_TOKEN`; make one on the Access
tab. Requests the UI sends with `background: true` answer at once with a run
record; leave it out in a script and the call waits until the work is done.
Whether the panel is open, and which language, is remembered in the browser.

## Showing one node or the whole cluster

A lemondx joined to a cluster puts a **Showing** picker on the Containers tab:
this node, the whole cluster, a single node, or a node group. The Templates tab
follows the same setting, so the instances a template lists and the containers
in the table always cover the same hosts, and Launch opens on the nodes you are
already looking at.

Widened, the table gains a Node column, and start/stop/restart/delete are routed
to whichever node owns each row — a selection may span hosts. Clicking any row
opens the full detail drawer, wherever the instance lives: overview and limits,
snapshots, bootstrap and the console all act on its own node. The bootstrap
picker's modules and keys come from that node too.

Health dots and app-check diamonds show for every node's rows: each node's
monitor judges its own instances, and the listing carries what the owner last
concluded. The
link on a remote row opens that node's own UI, for what is about the host rather
than the instance.

The choice is remembered in your browser. Without a cluster the picker is not
shown at all.

## Shared definitions sync themselves

Saving a template, a module, a bootstrap profile, a node group or a [stack](stacks.md) on a clustered
node pushes it to every other member as part of the save, and the toast says
where it went — or names the node that missed it and points at Sync. Deleting
one offers to remove it from the others too, ticked by default; untick it to
leave their copies alone.

A node that was switched off when something changed catches up on its own when
it comes back: see [Nodes view](#nodes-view) and
[A node that was switched off catches itself up](cluster.md#a-node-that-was-switched-off-catches-itself-up).

## Nodes view

Only interesting once this lemondx is federated with another; on its own it
shows this host and not much else. It has four parts:

- **This node** — the name, address and certificate fingerprint peers know it
  by, and a warning naming whatever is stopping another node from joining it.
  **Leave cluster** takes this host out and has every member forget it.
- **Nodes** — a card per node with its daemon, instance counts and groups, and
  a dot for reachable / not set up / unreachable. *Show instances* lists what is
  on a node without leaving the page. **Invite new node** issues a one-time join
  code (shown once — it is a credential), **Join cluster** redeems one issued elsewhere,
  **Reconcile** settles this node's shared definitions against every member's,
  and **Sync** copies this node's templates, modules or bootstrap profiles over
  another's.
- **Shared definitions** — what the last reconciliation found: what was taken
  from a member, what was sent on, and anything two nodes disagree about. A
  conflict is the one thing nothing puts right on its own, so it is listed
  first and says which node to push from. The panel is shown even when
  everything agrees — a panel that only appears with bad news is one you cannot
  trust the absence of.
- **Node groups** — a name for a set of nodes, so a launch or a sync can say
  "everywhere" once. A member that is not a node here is marked in red.

Each of the four listings loads on its own, so a call that fails leaves the rest
of the view working. That matters here more than anywhere: this is the tab a
broken cluster is fixed from, and **Leave cluster** is the way out of one. A
node whose credential is gone but which still lists peers shows a banner saying
so, and the button reads **Clear cluster state** — see
[Leaving a cluster that is already broken](cluster.md#leaving-a-cluster-that-is-already-broken).

The listing contacts every node, so this view polls every 10 seconds rather than
the 3 the rest of the UI uses, and the server caches each probe for a few seconds
so several open tabs do not multiply the traffic.

See [Nodes and federation](cluster.md) for how joining works and what it rests
on.

## Health checks

While `lemondx serve` runs, it checks every running instance once a minute.
Each check reads the instance's `/proc/loadavg` through the daemon's file API,
which proves it responds, and compares its CPU and memory with its limits:

| Status | When |
| --- | --- |
| healthy | answered, and under every threshold |
| degraded | load average over 3, CPU ≥ 90% of its CPUs over the minute, memory ≥ 90% of its limit, or one missed probe |
| unhealthy | missed two probes in a row, or a running container with no processes |
| starting | within two minutes of starting and not answering yet |
| unknown | a VM whose agent is not running, so it cannot be probed |
| paused | frozen; not checked |

An instance launched from a template can also have an
[app check](templates.md#app-health-checks) — a script of the template's own,
Nagios-style — which adds its verdict to the same status: warning or unknown
makes the instance degraded, critical does too and makes it unhealthy once it
repeats. Every running instance reports an app status; with no check
configured it is always `ok`. The container list shows it as a small diamond
after the health dot — green ok, orange warning, red critical, grey unknown
or pending. A running instance its node has not checked yet (up to one
interval after it starts) shows both markers grey rather than none; a node
with checks off shows none — and the detail panel's Health section has an App row
with the first line of output and when it last ran. Click the diamond, or
**Output** on that row, for everything the latest run printed — stdout, stderr,
exit code and timing — and the script itself; it is fetched from the node the
instance is on, where the check ran.

The container list shows a coloured dot beside **Running** — green when
healthy, orange when degraded, red when unhealthy (hover for the reasons); the detail panel shows the status, how long it has held, the reasons,
CPU as a percentage of the instance's CPUs, and load. The page raises a toast
once when an instance becomes unhealthy and once when it recovers. Degraded
changes only show in the list, as they come and go with ordinary work.

CPU comes from the daemon's per-instance counter, averaged between two checks,
so it appears from the second check on. Its denominator is `limits.cpu`, or the
host's CPUs for a container without one. Memory is only judged against a
`limits.memory`.

**Load average.** A container's own `/proc/loadavg` is the host's (unless
LXCFS virtualises it, which is off by default), so lemondx counts each
container's load itself, the way the kernel does for the host: every five
seconds it counts the container's threads that are running, waiting for a CPU
or in uninterruptible sleep — read from the container's cgroup on the host —
and keeps 1, 5 and 15 minute averages. A container pinned to one CPU with four
busy processes has a load of about 4 even though its CPU reads 100% or less,
which is exactly what CPU percentage alone hides. The 1 minute average is
judged once it has a minute of samples. It is an absolute count of tasks, not
divided by CPUs.

A VM reports its own load average through its agent. If a container's cgroup
cannot be found or read, lemondx falls back to the container's `/proc/loadavg`;
when that is the host's, it shows "host-wide" and does not judge it.

Tune any of it with `lemondx configure health` (interval of 15 seconds or more,
each threshold including `load_average`, probe timeout, missed probes before unhealthy, start grace);
`serve --no-health` turns checks off for one run. A saved file that does not
validate is ignored with a warning. The same checks run on demand from the
CLI, which measures CPU and load over a short window since it has no previous
check (its load is that window's average, shown as e.g. `4.00 (5s)`):

```bash
lemondx health                  # every running instance
lemondx health web-1 --window 10 --json
```

The CLI also runs each instance's app check once, and shows its status in the
`app` column. It exits 0 when everything is healthy, 1 when anything is degraded, starting or
unknown, and 2 when anything is unhealthy, so it can drive a cron job or a
monitoring check. `GET /api/health` returns the server's latest results
without running a check:

```json
{"enabled": true, "interval": 60, "thresholds": {"cpu_percent": 90, ...},
 "checked_at": 1789482783.3,
 "instances": [{"name": "web-1", "status": "degraded",
   "reasons": ["CPU at 99.6% of 1 core"], "checked_at": 1789482783.3, "since": 1789482723.1,
   "cpu": {"percent": 99.6, "cores_used": 0.996, "cores": 1},
   "memory": {"usage": 22446080, "limit": 134217728, "percent": 16.7},
   "load": {"avg": [4.02, 3.61, 2.10], "scope": "instance", "source": "cgroup", "warming": false, "window": null},
   "probe": {"ok": true, "ms": 18, "error": null},
   "app": {"status": "ok", "configured": false, "template": "Web server", "code": null,
           "output": "no app check configured", "ms": null, "checked_at": null,
           "streak": 0, "interval": null},
   "processes": 7, "failures": 0}]}
```

## Home

**Home** is the first screen. Across the top, one tile each for nodes
answering, containers running, instances whose health check says degraded or
unhealthy, stacks running and templates in use; each opens the view behind it.
Below them, every saved stack and template with what it is running across the
whole cluster: how many instances, on which nodes, the least healthy of them,
and any launch, recreate or destroy under way. In a cluster a node table
follows, with each member's state, daemon and running instances. Home asks
every member, so it refreshes every ten seconds rather than every three, and
a node that does not answer is named rather than silently left out.

### This node

The bottom of **Home** sets what instances have claimed against what this host
has — CPU threads, memory, and space on each storage pool — with the host's
processor, thread count and memory above it and a per-instance table below.

Each resource has two bars. **Allocated** is the sum of configured limits, not
a measurement: CPU and memory count only running (and frozen) instances, with
what stopped ones would add shown as a lighter segment, while disk counts
every instance because a stopped one still occupies its pool. The second bar
is what is actually in use — memory split between instances and the rest of
the host, pool space as the daemon reports it, and CPU as the threads
instances kept busy between two polls (so it reads "measuring…" for the first
five seconds).

A few things the numbers deliberately do not hide:

- An instance with **no limit** can use the whole host, so it is named under
  the bar instead of being counted as some guessed amount.
- A **VM without a limit** still gets a fixed machine — 1 vCPU, 1 GiB and a
  10 GiB root disk by default — and those count, shown faint as "default".
- Allocations **can exceed the host**. Limits are ceilings rather than
  reservations, so this is often fine for CPU; the bar rescales, marks where
  the host runs out and says *overcommitted*.
- On a pool whose driver cannot enforce sizes (`dir`), disk sizes are
  intentions, and the card says so.

Allocations cover the current project; host figures cover the whole host.

```bash
./lemondx resources
curl -s localhost:8099/api/resources | jq '.memory, .instances[].memory'
```

## Storage view

The **Storage** tab has separate Pools and Volumes views. Pools show their
driver, source, capacity, resident instances, volume count and whether lemondx
can manage them. Instances whose root disk is on the pool are listed directly;
instances that only attach a custom volume from it are labeled separately.
The create form offers only local `dir`, `btrfs`, `lvm` and `zfs` drivers that
the connected daemon reports as available. Existing pools using other drivers
remain visible with a **read-only** badge.

The Volumes view includes custom volumes alongside the daemon-owned volumes
for containers, virtual machines and images. Edit and delete actions appear
only for custom volumes on managed local pools. Deletion is disabled while a
custom volume is attached.

Deleting a pool with resources opens a force-delete confirmation that lists
every affected instance, cached image, custom volume and profile. The action
stays disabled until the exact pool name is entered. The server rejects the
request if that list changes before deletion begins.

For ZFS, the action removes the pool from LXD but preserves its backing zpool.
After the confirmed LXD resources are removed, the dialog asks the operator to
export the zpool on the host and retry. lemondx verifies the export before it
lets LXD remove the registration, preventing LXD's normal `zpool destroy`.

Pool sizes apply only to loop-backed Btrfs, LVM and ZFS pools; `dir` has no
pool size. A custom volume can have a filesystem or block content type and an
optional size. Block volumes can grow but cannot be shrunk.

The new-instance dialog lists these pools in its **Storage pool** selector and
defaults to the pool used by the default profile. Selecting another pool adds
an instance-specific root disk override; it does not change the profile.

## Network view

The **Network** tab explains how container networking is actually wired up,
rather than just listing interfaces: the bridge's IPv4/IPv6 subnets, whether
NAT and DHCP are on, the DNS domain, MTU and bridge MAC — followed by a
plain-English summary ("containers get an address on 10.x/24 by DHCP, with
10.x.1 as their gateway and DNS resolver…"), what is attached (profiles and
instances), and the live DHCP lease table showing which container holds which
address.

Host interfaces the daemon does not manage (`docker0`, physical NICs) are
listed separately so it is clear what is and is not under its control, with
host bridges marked as ones a new instance can join.

### Fabrics

The top of the tab is **Fabrics**: routed networks that span every node (see
[networking.md](networking.md)). Each fabric has a card with its prefix,
whether it NATs, and one row per node: the /24 that node holds, its gateway,
how many of its routes to the other nodes are in place, which instances are
attached, and a state (*ok*, *no bridge*, *routes*, *not on it*, *no answer*).
Each row is the node's own report, fetched from it, so a missing bridge or
route on another host shows up here rather than as lost packets. The card
polls every 10 seconds, because every poll asks each member.

**New fabric** opens a dialog pre-filled with the first free name and /16,
checked against every interface and route on every node. Editing either field
checks again, shows the /24 each node would get, and names the node and
interface that any overlap is with. The fabric is created on every node at
once, so every node must be reachable: while one is not, **New fabric** is
disabled and the section says which node it is and that the way on without it
is to evict it on the Nodes tab.
**Add missing nodes** gives a subnet to members that lack one, and **Delete**
removes the fabric everywhere once no instance is attached to it. **Apply**
and **Show plan** act on this node's routes and firewall.

In the create and template dialogs, fabrics are a group of their own in the
**Network** picker, so there is one choice to make. A fabric with NAT becomes
the instance's only NIC: it reaches the fabric on every node directly and the
internet through the NAT. A fabric without NAT cannot carry internet traffic,
so it is added as a second NIC beside the default network. The hint under the
picker says which of the two you get.

### Managing networks

**New network** creates a managed bridge. Each address family is *Pick a free
subnet for me* (the daemon's `auto`), *Choose the CIDR block…*, or *Disabled*,
with NAT and DHCP toggles.

Choosing the block prefills a /24 (or /64) nothing on the host uses, and you
can type any other, such as `10.44.8.0/22`. The bridge takes the block's first
host (`10.44.8.1`); type a host address instead (`10.44.8.254/22`) to give it
that one. Below the field you see the bridge address, how many addresses are
left for instances and the usable range, plus every subnet already on the
host. A block that overlaps one of them — another bridge, `docker0`, your LAN —
is refused before you can submit, and again by the server, because the daemon
itself accepts it and routing to both then quietly breaks. Public IPv4 ranges
and IPv6 prefixes other than /64 are allowed but get a warning. The defaults are what `lemondx init`
creates: a free IPv4 subnet behind NAT and no IPv6. DNS domain, MTU and a
short allowlist of other bridge keys (`ipv4.dhcp.ranges`, `ipv4.routing`,
`dns.mode`, …) are available too; `raw.dnsmasq`, tunnels and uplinks are left
to the daemon's own CLI. Names are limited to 15 characters, the kernel's
limit for an interface.

**Edit** sends only the keys that changed, so a network with options lemondx
does not allow can still be edited. **Delete** is refused while any profile
or instance uses the network, and the dialog lists them. lemondx will not
detach them for you, because an instance whose NIC is removed keeps running
with no network. Only managed bridges on non-clustered servers can be edited
or deleted.

The new-instance dialog (and the template editor) has a **Network** selector
next to **Storage pool**. It defaults to the network the default profile's NIC
joins and lists managed networks and host bridges. Picking another one adds an
instance-level NIC under the profile's device name, which replaces the
profile's NIC rather than adding a second one; the profile itself does not
change. On a host bridge the instance gets its address from whatever serves
that network, not from the daemon. **New network…** creates a bridge in place,
with the same CIDR block picker, and selects it.

From the CLI:

```bash
./lemondx network ls
./lemondx network show lxdbr0
./lemondx network subnets                             # what a new block must avoid
./lemondx network create labbr0 --ipv4 10.20.0.0/24 --dns-domain lab
./lemondx network set labbr0 --no-nat --mtu 1400
./lemondx network set labbr0 --config dns.domain=   # an empty value unsets a key
./lemondx create lab1 --network labbr0
./lemondx network delete labbr0
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

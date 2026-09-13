# Web UI tour

[← back to README](../README.md)

Parts of the web UI that are easy to miss because they are not on the
Containers tab: the Resources, Storage and Network views, and the full image
browser.

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

## Resources view

The **Resources** tab sets what instances have claimed against what the host
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

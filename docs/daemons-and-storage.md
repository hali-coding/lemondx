# Daemons, images and storage

[← back to README](../README.md)

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

## Image aliases

Images are given as `remote:alias`; `lemondx images` lists what the current
daemon knows. A bare alias assumes `ubuntu:` on LXD and `images:` on Incus,
and `local:FINGERPRINT` uses an already-cached image. See also the
[image browser](web-ui.md#browsing-images) for searching a daemon's full
catalog rather than typing an alias by hand.

## Pinned images

A remote alias is whatever the remote built last: `images:debian/12` launched
next month is not the build launched today. A **pin** is one build of a remote
image, by fingerprint, shared by every node. A template (or a single create)
that names it as `pin:<name>` launches exactly that build, on every node, for
as long as it names it.

A pin's build never changes. A newer build is another pin beside it, so one
image can have several, and a template moves to a newer base only when it is
edited to name the newer pin — which is also what marks its instances stale.
Plain `images:debian/12` is unaffected by pins and keeps following the remote.

Each pin has an id made from the image and the build
(`images-debian-12-20261001-0524`) and any number of **nicknames**
(`debian-golden`), which a template can name instead: `pin:debian-golden`.
Nicknames are the one part of a pin that changes; each names one pin across
the cluster, and one that a template launches by cannot be taken off, just as
a pin a template launches cannot be unpinned.

```bash
lemondx image-search debian 12 --versions   # every build the remotes still serve
lemondx image-pin images:debian/12 --nickname debian-golden   # today's build, fetched on every node
lemondx image-pin images:debian/12 --serial 20261001_0350 --note "March base"
lemondx image-pins                          # every pin, its names, what launches it, whether this node has it
lemondx image-pin-edit debian-golden --add debian-stable --remove debian-old
lemondx template-save web --image pin:debian-golden
lemondx image-fetch debian-golden --node new-node   # a node that missed it
lemondx image-unpin images-debian-12-20261001-0350  # refused while a template launches it
```

Remotes keep only their last few builds — `images:` about a day's — so pinning
downloads the build at once: on the node you pin from, which then sends it to
the others over the peer channel. Sending rather than letting each node pull
matters in a mixed cluster: `images:` is a different server on LXD
(images.lxd.canonical.com) and on Incus (images.linuxcontainers.org), with
different builds, so there is no one remote every node could fetch the same
build from. The copy is kept under `local:pin-<id>` with auto-update off, so
the daemon neither expires nor replaces it.

A pin is a shared definition like a template: pinning, nickname changes and
unpinning reach every member, and reconciliation brings a node that was off up
to date. A member pushing a different build under an existing id is refused.
A node that has the pin but not the build — one that was off, or joined since —
fetches it from the remote at launch while the remote still serves it; after
that, **Fetch on …** in the Images tab (or `image-fetch` on a node that has it)
sends it over. Unpinning keeps the fetched builds as ordinary images. A pin is
for one architecture's build, and a node of another architecture refuses to
launch it rather than quietly taking something else.

## Pruning images

Every launch from a remote leaves the image it downloaded behind, and the
daemon keeps refreshing those copies. `lemondx image-prune` (or **Prune
images…** on the Images tab) deletes the downloaded ones nothing needs, on
every node:

```bash
lemondx image-prune --dry-run      # what each node would delete, and what it keeps and why
lemondx image-prune                # the same, then asks before deleting
lemondx image-prune --node prdev2 -y
```

Downloaded means pulled from a remote: the daemon's cache, an image with a
remote to update from, or a pinned build kept after its pin went. Kept
whatever else: an image any instance was made from (running or stopped — read
from the instance's `volatile.base_image`, since the daemons' own `used_by`
is not reliable for this), the build any pin names, and an image a template
launches as `local:<alias>`. Images made here from snapshots, or imported by
hand, are never touched. Each node judges its own images against its own
instances. Confirming deletes only what the preview listed, so an image a
launch picked up in between is kept.

## Storage, and disk sizes

`lemondx init` gets a fresh host ready for instances — a pool, a bridge and a
default profile that uses them. It is a convenience for a new node, not a
substitute for setting up the daemon deliberately: a host whose daemon is
already configured needs none of it. It creates the pool with the `dir` driver
by default: it works everywhere, needs no extra packages, and creates no large
backing file.

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

## Managing pools and volumes

The **Storage** tab and `lemondx storage` commands manage local storage through
the daemon API. The supported management drivers are deliberately limited to:

- `dir` — an existing directory or daemon-managed directory storage
- `btrfs` — an existing filesystem/device or a loop-backed pool
- `lvm` — an existing volume group/device or a loop-backed pool
- `zfs` — an existing zpool/dataset/device or a loop-backed pool

A driver appears in the create form only when the connected daemon reports it
as available. Distributed, clustered, shared, object and enterprise drivers
are outside lemondx's management scope. Existing pools using one of those
drivers are still listed, but are read-only, as are their volumes. Storage is
also read-only when the connected daemon is clustered because pool creation
then requires per-member configuration.

The Volumes view lists every daemon volume. Only `custom` volumes on a managed
local pool can be created, resized, edited or deleted. Container, virtual
machine and image volumes belong to their corresponding daemon objects and are
shown for context rather than edited independently. Pools with references or
volumes require force deletion, and custom volumes that are attached cannot be
deleted individually.

Force deletion shows the complete cascade before it starts and requires the
pool name to be typed exactly. It deletes instances and virtual machines whose
storage uses the pool, removes cached images and custom volumes, removes
pool-backed devices from profiles, then deletes the pool. If the resource list
changes after confirmation, or an unknown resource type is present, deletion
stops before making any changes.

An instance attached to a custom volume in the pool is part of the cascade
even when its root disk is elsewhere. Cached images are daemon-wide, so an
image listed in the plan is removed from every pool where it is cached. The
cascade is ordered but not transactional: if the daemon rejects a later step,
resources removed by earlier steps cannot be restored automatically.

ZFS pool deletion is a detach operation. lemondx first deletes the confirmed
LXD-managed resources, but it never asks LXD to delete an imported backing
zpool because LXD couples unregistering a ZFS pool with `zpool destroy`.
Instead, lemondx asks you to run `sudo zpool export ZPOOL` on the host and
retry. Once the zpool is no longer imported, LXD removes its storage-pool
registration without destroying the zpool or its remaining datasets. The
zpool can be imported again after the LXD registration is gone. lemondx does
not run privileged ZFS commands or modify LXD's database directly.

```bash
./lemondx storage pools
./lemondx storage pool create fast --driver zfs --size 30GiB
./lemondx storage volumes --pool fast
./lemondx storage volume create fast data --size 10GiB
./lemondx storage volume set fast data --size 20GiB
./lemondx storage volume delete fast data
./lemondx storage pool delete fast --force
./lemondx storage pool delete fast --force --confirm-name fast  # automation
```

Use `./lemondx create NAME --pool fast` to place a new instance's root disk on
a specific existing pool without changing the default profile.

Pool and volume configuration can be supplied with repeated
`--config KEY=VALUE`, but only documented local-driver keys are accepted.
lemondx does not accept remote-backend credentials through this interface.

## Units

`--cpu` is a core count. `--memory` and `--disk` take a size: `4GiB`, `512MiB`,
`20GB`, or the shorthands `4G` / `512M`. A bare number is read as **GiB**, since
LXD's own reading of it — bytes — is never what anyone means in a size field
(`4` would be four bytes, and rejected as under the 1MiB minimum).

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

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

## Units

`--cpu` is a core count. `--memory` and `--disk` take a size: `4GiB`, `512MiB`,
`20GB`, or the shorthands `4G` / `512M`. A bare number is read as **GiB**, since
LXD's own reading of it — bytes — is never what anyone means in a size field
(`4` would be four bytes, and rejected as under the 1MiB minimum).

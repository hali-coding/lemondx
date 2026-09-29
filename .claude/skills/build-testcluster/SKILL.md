---
name: build-testcluster
description: The clean Proxmox test environment on 164.132.166.12 — building LXD (or Incus) VMs there with cloud-init, installing lemondx on them as a service, forming a cluster, a fabric and stacks, reaching everything through SSH tunnels, and the host/systemd quirks learned doing it. Use when asked for test VMs or nodes, a fresh cluster, a firedrill, a CI/daemon host, or anything on the Proxmox test host.
---

# The Proxmox test environment

The long-lived 5-node LAN cluster is the `test-cluster` skill. This one is the
**disposable** environment on a dedicated Proxmox host. Everything on it was
built from the steps below and can be rebuilt from them.

## The host

| | |
|---|---|
| Host | `164.132.166.12` (`ns3034278`), Proxmox VE 9.2, 12 threads, 31 GB RAM, nested virt on |
| Access | `ssh root@164.132.166.12` (key). The web UI is at `https://164.132.166.12:8006`, with the same rights. |
| Storage | `vmdata` is a zfspool on pool `data` (1.7 TB, sparse): VM disks, instant snapshots and resizes. `local` (`/var/lib/vz`) holds snippets, imports and ISOs. |
| Network | `vmbr1` = `172.30.0.0/24`, host at `.1`, NAT out via vmbr0 (`post-up` in `/etc/network/interfaces`, original saved as `/root/interfaces.orig`). **No DHCP**: every VM has a static address. Nothing on vmbr1 is reachable from outside, so use tunnels. |
| Images | `/var/lib/vz/import/`: Ubuntu 24.04 (`noble-server-cloudimg-amd64.qcow2`), plus Debian 13 when a Debian profile is used. |

172.30/24 was picked so it can't collide with the 10.x bridges LXD creates or
with fabric prefixes (10.100/16 and up).

## Current nodes (2026-09-26)

| VMID | Name | IP | Spec | Contents |
|---|---|---|---|---|
| 101 | `lxd1` | 172.30.0.11 | 3c / 6 GB (balloon 3G) / 40G | Ubuntu 24.04, LXD 5.21.8 (snap), lemondx user service |
| 102 | `lxd2` | 172.30.0.12 | same | same |
| 103 | `lxd3` | 172.30.0.13 | same | same |
| 104 | `lxd4` | 172.30.0.14 | same | same |

- **Cluster:** 4 members, formed on lxd1. Auth is off, so loopback callers are admin and peers use the cluster credential.
- **Fabric:** `lemonfab0`, `10.100.0.0/16` with NAT. Each lxdN has `10.100.N.0/24`, and all 3/3 routes are ok.
- **Stack:** `lemon3tier`. The steps are:
  - `db` (`lx-db`: Ubuntu + postgresql) on lxd1
  - `web` ×2 (`lx-web`: Alpine + apache2) on lxd2 and lxd3
  - wait until healthy
  - `probe` (`lx-probe`: Ubuntu + the `stack-probe` module) on lxd4

  Every template puts its instance on the fabric. The probe fails the stack
  unless both web servers and a Postgres login answer across the fabric.
  Launch it with `--param DB_PASSWORD=...`. It is never stored.

Keep this table current when adding or removing VMs.

## Reaching things (tunnels)

```sh
ssh -J root@164.132.166.12 ci@172.30.0.11          # a node (user ci, passwordless sudo, in lxd)
ssh -J root@164.132.166.12 -L 8099:127.0.0.1:8099 ci@172.30.0.11   # lemondx UI -> https://localhost:8099
ssh -L 8006:127.0.0.1:8006 root@164.132.166.12     # Proxmox UI, if 8006 is ever firewalled
```

**Tunnel to the node's loopback, not its 172.30 address.** A cluster member
requires a credential from any non-loopback caller, even with auth off. A
tunnel ending at `172.30.0.11:8099` arrives from `172.30.0.1` and gets a 401.
`serve` speaks HTTPS once the node is in a cluster (self-signed), so `curl -k`.

To close a tunnel, kill it by PID (`pgrep -af 'L 8099'`, then `kill`). A
`pkill -f` pattern on a shell's command line matches the shell itself.

## Building VMs

```sh
.claude/skills/build-testcluster/scripts/create-vm.sh <vmid> <name> <octet> [lxd|basic|lemondx-ci] [cores] [memMB] [diskGB]
# e.g. .claude/skills/build-testcluster/scripts/create-vm.sh 105 lxd5 15 lxd 3 6144 40
```

- **Profiles** are in `cloud-init/`. Each one's `# image:` line picks the cloud image:
  - `lxd`: Ubuntu 24.04, LXD `5.21/stable` snap, `lxd init --auto`. About 4 minutes.
  - `basic`: Debian 13 + Incus.
  - `lemondx-ci`: Debian 13, Incus + LXD, node/npm. Slow: Debian's npm pulls in 600+ packages.
- **Per-VM snippet:** the script renders `/var/lib/vz/snippets/testcluster-<vmid>.yaml`,
  filling in the hostname and ssh keys (this machine's and the host root's).
  With a shared snippet, Proxmox sets no hostname and the guest comes up as `localhost`.
- **Checks:** it refuses a VMID that exists or an IP that answers ping, grows
  the disk *before* first boot so growpart sees it, and waits for cloud-init
  (`/var/lib/cloud/testcluster-ready`).
- **Parallel builds:** download the image on the host first. Otherwise every
  build fetches it at once.
- **Other hosts:** the defaults are this host. Override them with `PVE_HOST`,
  `PVE_BRIDGE`, `PVE_NET` (first three octets), `PVE_STORAGE`. The script uses
  `sudo -n` when not root and handles dir storage (qcow2, and `qemu-img`
  resizing, because `qm resize` times out there).
- **Headroom:** 4 × 6 GB is about 24 of 31 GB, so check `free -g` before adding more.

Snapshots: `qm snapshot <id> <name>`, `qm rollback <id> <name>`.
Removal is destructive, so confirm first: `qm stop <id> && qm destroy <id> --purge && rm /var/lib/vz/snippets/testcluster-<id>.yaml`.

## Installing lemondx on the nodes

1. Pack the working tree without `.git`, `node_modules`, `.claude` or
   `__pycache__`, and unpack it to `~ci/lemondx`. Check `web/dist` is newer
   than `web/src`.
2. As ci:
   - `sudo loginctl enable-linger ci`
   - `export XDG_RUNTIME_DIR=/run/user/$(id -u)` (needed for `systemctl --user` over ssh)
   - `./systemd/install-service.sh`
   - `sudo ./systemd/install-service.sh --fabric`. This installs only the sudoers rule for `fabric-helper`.
3. Add the drop-in `~/.config/systemd/user/lemondx.service.d/testcluster.conf`,
   then `daemon-reload` and restart:
   ```ini
   [Service]
   ExecStart=
   ExecStart=/usr/bin/python3 /home/ci/lemondx/lemondx serve --host 0.0.0.0 --port 8099
   NoNewPrivileges=no
   ProtectSystem=no
   ProtectHome=no
   PrivateTmp=no
   ReadWritePaths=
   ```
   **All of the sandbox lines are needed for a fabric.** A user manager applies
   `ProtectSystem`/`ProtectHome`/`PrivateTmp`/`ReadWritePaths` inside a user
   namespace, where root's files show as `nobody` and sudo fails
   (`/etc/sudo.conf is owned by uid 65534`). `serve`'s `reapply()` then fails
   **silently**: `fabric create` says created, and `fabric list` shows every
   node at `routes 0/N`. The same applies to PAM (`unix_chkpwd` exits 9).
   docs/service.md has the drop-in.
4. **Cluster:**
   - Run `cluster invite` on the first node and restart its service, so it serves the new certificate.
   - Codes are **single-use**, so issue one per joiner.
   - Pass each code through a file (`cat > /tmp/code`), never through a filter
     in the same pipe as `join`. A broken filter kills the join halfway and the
     node stays out.
   - Restart each joiner after it joins.
   - Revoke the code left over from the first `invite`.
5. **Fabric:** `lemondx fabric check`, then `lemondx fabric create` on any
   node. `serve` programs the routes itself about 20 seconds after a restart
   (`START_DELAY`). After that it only re-applies on a membership change or
   hourly.

## Stacks and the `images:` remote

LXD's `images:` is `images.lxd.canonical.com` (Alpine, Debian and others are
there); `ubuntu:` is Canonical's cloud images. `template-save` has no
`--no-default-modules`, because any `-b` already replaces the defaults.
Module output is not kept in run records. To prove cross-node traffic, look at
the receiver (Apache's access log shows the caller's fabric IP when routed, the
host's when NAT'd).

## Legacy: sysvmbox5

The previous host (`github@sysvmbox5.trustsno1.com`, vmbr29 `192.168.29.0/24`)
still has:
- VM 103 `lemondx-runner` (.20), the self-hosted GitHub Actions runner for
  `pr.yml`'s `daemon` job, label `lemondx-daemon`
- 104/105 `debian13-test1/2`, stopped
- 106–108 `lemondx-fd1..3`, the firedrill cluster

Its API token `root@pam!github` has no ACLs. To target it, set
`PVE_HOST=github@sysvmbox5.trustsno1.com PVE_BRIDGE=vmbr29 PVE_NET=192.168.29 PVE_STORAGE=local`.

The runner was registered with the repo's fork-PR approval set to
`all_external_contributors`. Keep it that way: the repo is public, and the
runner user is root-equivalent through the daemon group.

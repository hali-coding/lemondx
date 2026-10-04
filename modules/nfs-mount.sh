#!/bin/sh
# name: NFS file system
# description: Install the NFS client and mount a remote export, now and at every boot. Add it more than once for several mounts.
# os: debian ubuntu fedora alpine arch opensuse
# order: 30
# repeatable: yes
# param: NFS_SOURCE=  The export, as server:/path (e.g. 192.168.1.10:/srv/share)
# param: NFS_MOUNTPOINT=/mnt/nfs  Where to mount it in the instance
# param: NFS_OPTIONS=defaults,_netdev,nofail  Mount options for /etc/fstab (add vers=4.2, ro, ...)

source="${NFS_SOURCE:-}"
target="${NFS_MOUNTPOINT:-/mnt/nfs}"
options="${NFS_OPTIONS:-defaults,_netdev,nofail}"

case "$source" in
    ?*:/*) ;;
    *) die "NFS_SOURCE must be server:/path, e.g. 192.168.1.10:/srv/share (got '$source')" ;;
esac
case "$target" in
    /?*) ;;
    *) die "NFS_MOUNTPOINT must be an absolute path other than / (got '$target')" ;;
esac
# fstab fields are separated by whitespace, so a value holding any would
# corrupt the line for every mount after it, not just this one.
for value in "$source" "$target" "$options"; do
    case "$value" in
        *[[:space:]]*) die "NFS values cannot contain spaces (got '$value')" ;;
    esac
done
target="${target%/}"

case "$LEMONDX_PKG" in
    apt)    pkg_install nfs-common ;;
    zypper) pkg_install nfs-client ;;
    *)      pkg_install nfs-utils ;;
esac

# An unprivileged container may mount NFS only through the daemon's mount
# interception, which the instance has to be given from outside.
in_container() {
    grep -qa 'container=' /proc/1/environ 2>/dev/null
}

mkdir -p "$target"

# One line per mount point: re-running (or changing the source) replaces the
# entry rather than stacking a second one that would fight it at boot.
write_fstab() {
    tmp="$(mktemp)"
    awk -v t="$target" '$2 != t' /etc/fstab > "$tmp"
    printf '%s\t%s\tnfs\t%s\t0\t0\n' "$source" "$target" "$1" >> "$tmp"
    cat "$tmp" > /etc/fstab
    rm -f "$tmp"
}
# A version an earlier run had to name (see below) still has to be named.
existing="$(awk -v t="$target" '$2 == t { print $1, $4 }' /etc/fstab | tail -n 1)"
case "$existing" in
    "$source $options,vers="*) options="${existing#* }" ;;
esac
write_fstab "$options"
log "added $source on $target to /etc/fstab"

if have systemctl && [ -d /run/systemd/system ]; then
    systemctl daemon-reload
elif have rc-update; then
    if in_container; then
        # OpenRC's netmount is keyworded -lxc, so it never runs in a
        # container; the local service does, and mounts them instead.
        mkdir -p /etc/local.d
        printf '#!/bin/sh\n# Written by lemondx (nfs-mount): netmount does not run in a container.\nmount -a -t nfs,nfs4\n' \
            > /etc/local.d/lemondx-nfs.start
        chmod 0755 /etc/local.d/lemondx-nfs.start
        rc-update add local default >/dev/null 2>&1 || true
    else
        rc-update add netmount default >/dev/null 2>&1 || true
    fi
fi

# What is mounted there now, as source and options. The kernel may name the
# server differently from how it was asked (a host name for an address, when
# it shares a mount the host already has), but always records its addr=.
current="$(awk -v t="$target" '$2 == t { print $1, $4 }' /proc/mounts | tail -n 1)"
mounted="${current%% *}"
host="${source%%:*}"
case "$mounted ,${current#* }," in
    "$source "*|*":${source#*:} "*",addr=$host,"*)
        log "$source is already mounted on $target"
        exit 0 ;;
esac
if [ -n "$mounted" ]; then
    log "unmounting $mounted from $target"
    umount "$target"
fi

if error="$(mount "$target" 2>&1)"; then
    log "mounted $source on $target"
    exit 0
fi

if in_container; then
    # Through the daemon's mount interception the client cannot fall back to
    # an older protocol when the server refuses the newest -- the refusal
    # comes back as "not permitted", like no interception at all -- so name
    # each in turn; the one that works goes into fstab, or the next boot
    # fails the same way.
    case ",$options," in
        *,vers=*|*,nfsvers=*) ;;
        *)
            for version in 4.1 4.0 3; do
                if mount -t nfs -o "$options,vers=$version" "$source" "$target" 2>/dev/null; then
                    write_fstab "$options,vers=$version"
                    log "mounted $source on $target (NFS $version; added vers=$version to fstab)"
                    exit 0
                fi
            done ;;
    esac
    case "$error" in
        *"not permitted"*)
            printf '%s\n' "$error" >&2
            die "an unprivileged container may not mount NFS unless the daemon mounts it on its behalf. On the host, run:
  lxc config set ${LEMONDX_INSTANCE:-<instance>} security.syscalls.intercept.mount=true security.syscalls.intercept.mount.allowed=nfs,nfs4
  lxc restart ${LEMONDX_INSTANCE:-<instance>}
(incus for lxc on Incus; or put both keys in a profile the template uses), then run this module again. If they are set already, check that the server exports $source to this host. A virtual machine needs neither." ;;
    esac
fi
printf '%s\n' "$error" >&2
die "could not mount $source on $target; check that the server exports it to this instance's address"

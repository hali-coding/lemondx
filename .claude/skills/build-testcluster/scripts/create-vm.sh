#!/bin/sh
# Create one cloud-image VM on the Proxmox host and wait for cloud-init.
#
#   create-vm.sh <vmid> <name> <ip-last-octet> [lxd|basic|lemondx-ci] [cores] [memory-MB] [disk-GB]
#
# Runs from this machine: renders a per-VM user-data snippet (so the guest
# gets its hostname -- a shared snippet cannot carry one), copies it to the
# host and drives `qm` there over ssh. The profile's `# image:` line picks the
# cloud image. Defaults are the current test host; override with the env vars
# below (see SKILL.md).
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
HOST=${PVE_HOST:-root@164.132.166.12}
BRIDGE=${PVE_BRIDGE:-vmbr1}
NET=${PVE_NET:-172.30.0}
STORAGE=${PVE_STORAGE:-vmdata}
[ $# -ge 3 ] || { sed -n '4p' "$0" | sed 's/^# *//' >&2; exit 2; }
vmid=$1 name=$2 octet=$3 profile=${4:-lxd} cores=${5:-2} mem=${6:-2048} disk=${7:-20}
ip=$NET.$octet
tmpl=$here/cloud-init/$profile.yaml
[ -f "$tmpl" ] || { echo "no profile $profile" >&2; exit 2; }
url=$(sed -n 's/^# image: *//p' "$tmpl" | head -1)
[ -n "$url" ] || { echo "$tmpl has no '# image:' line" >&2; exit 2; }

# This machine's keys and the host's, so the VM is reachable both through
# the jump host and from the host itself.
keys=$( { cat ~/.ssh/id_*.pub 2>/dev/null; ssh -o BatchMode=yes "$HOST" 'cat ~/.ssh/id_*.pub 2>/dev/null' || true; } \
        | sed 's/^/      - /')
snippet=$(mktemp)
awk -v h="$name" -v k="$keys" '{ gsub(/@HOSTNAME@/, h); if ($0 == "@SSH_KEYS@") print k; else print }' \
    "$tmpl" > "$snippet"
scp -q "$snippet" "$HOST:/tmp/testcluster-$vmid.yaml"
rm -f "$snippet"

ssh -o BatchMode=yes "$HOST" sh -s -- "$vmid" "$name" "$ip" "$cores" "$mem" "$disk" \
    "$BRIDGE" "$NET.1" "$STORAGE" "$url" < "$here/scripts/host-create.sh"

echo "waiting for cloud-init on $ip..."
jump="-o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -J $HOST"
i=0
until ssh $jump "ci@$ip" 'test -f /var/lib/cloud/testcluster-ready || cloud-init status | grep -q error' 2>/dev/null; do
    i=$((i + 1)); [ "$i" -lt 120 ] || { echo "cloud-init did not finish on $ip" >&2; exit 1; }
    sleep 10
done
ssh $jump "ci@$ip" 'echo "$(hostname): $(cloud-init status)"; df -h / | tail -1;
    { lxc version 2>/dev/null || incus version 2>/dev/null; } | tail -1'

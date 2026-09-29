# Runs on the Proxmox host (piped over ssh by create-vm.sh).
set -eu
vmid=$1 name=$2 ip=$3 cores=$4 mem=$5 disk=$6 bridge=$7 gw=$8 storage=$9 url=${10}
SUDO=; [ "$(id -u)" -eq 0 ] || SUDO="sudo -n"
image=/var/lib/vz/import/$(basename "$url")
case "$image" in *.img) image=${image%.img}.qcow2 ;; esac   # Ubuntu's .img is qcow2

if $SUDO qm status "$vmid" >/dev/null 2>&1; then echo "VMID $vmid exists" >&2; exit 1; fi
# Every VM gets a static address (no DHCP on the test bridge), so refuse one in use.
if ping -c1 -W1 "$ip" >/dev/null 2>&1; then echo "$ip answers ping, in use" >&2; exit 1; fi
if [ ! -f "$image" ]; then
    $SUDO mkdir -p "$(dirname "$image")"
    $SUDO curl -sSfL -o "$image.part" "$url" && $SUDO mv "$image.part" "$image"
fi
$SUDO install -m 0644 "/tmp/testcluster-$vmid.yaml" "/var/lib/vz/snippets/testcluster-$vmid.yaml"
rm -f "/tmp/testcluster-$vmid.yaml"

type=$($SUDO pvesm status --storage "$storage" | awk 'NR==2 {print $2}')
fmt=; [ "$type" = dir ] && fmt=",format=qcow2"   # a raw file on a dir storage cannot be snapshotted
$SUDO qm create "$vmid" --name "$name" --ostype l26 --machine q35 --cpu host \
    --cores "$cores" --memory "$mem" --balloon $((mem / 2)) \
    --scsihw virtio-scsi-single \
    --scsi0 "$storage:0,import-from=$image$fmt,discard=on,iothread=1" \
    --ide2 "$storage:cloudinit" --boot order=scsi0 --serial0 socket --vga serial0 --agent 1 \
    --net0 "virtio,bridge=$bridge" \
    --ipconfig0 "ip=$ip/24,gw=$gw" --nameserver "1.1.1.1 8.8.4.4" \
    --cicustom "user=local:snippets/testcluster-$vmid.yaml" \
    --description "test cluster VM (build-testcluster skill)" 2>&1 | grep -vE '^transferred|^Formatting'

# Grow the disk before first boot, so cloud-init's growpart sees all of it.
# On a dir storage `qm resize` wraps qemu-img in a short timeout that a
# metadata-preallocated qcow2 regularly exceeds, so resize the file directly.
if [ "$type" = dir ]; then
    vol=$($SUDO qm config "$vmid" | sed -n 's/^scsi0: \([^,]*\),.*/\1/p')
    $SUDO qemu-img resize -q --preallocation=off -f qcow2 "$($SUDO pvesm path "$vol")" "${disk}G"
    $SUDO qm disk rescan --vmid "$vmid" >/dev/null
else
    $SUDO qm resize "$vmid" scsi0 "${disk}G"
fi
$SUDO qm config "$vmid" | grep '^scsi0'
$SUDO qm start "$vmid"
echo "$vmid $name $ip started"

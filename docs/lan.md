# Containers on your LAN

[← back to README](../README.md)

A managed bridge like `lxdbr0` gives instances addresses of its own and NATs
them behind the host. To put an instance *on your LAN* instead -- an address
from your router, reachable from other machines like any other device, no NAT
and no DHCP from the daemon -- connect it to the network one of the host's NICs
is on.

**Network → New network → On your LAN** lists the host's NICs and offers, per
NIC, only what works for it:

| | What it is | Host changes | Host ↔ instances |
| --- | --- | --- | --- |
| **Bridge a spare NIC** | A daemon bridge with the NIC as its port (`bridge.external_interfaces`), no addresses of its own. | None | Only through the LAN (the host has no address on the bridge) |
| **macvlan** | Each instance gets its own MAC on the NIC, beside the host's. | None | **Cannot reach each other** over that NIC; everything else on the LAN can |
| **Bridge the host's own NIC** | The NIC becomes a port of a new bridge (`br0`), and the host's address moves onto the bridge. | Through NetworkManager, kept across reboots | Like any two machines on the LAN |

macvlan is chosen by default whenever a NIC allows it, since it changes nothing
on the host.

Instances then join it like any network: pick it under **Network** when creating
one or in a template. Its name shows as *LAN via eno1* there.

## On every node at once

In a cluster, **Apply to the default interface on all nodes** makes one macvlan
network, under one name (`lan` unless you change it), on every member. NIC
names differ between machines -- `eno1` here, `enp3s0` there -- so each node
uses the NIC its own preferred IPv4 default route leaves by, which every
machine has exactly one of. The shared name is what lets a template name the
network and get the LAN on whichever node it launches.

A node that **joins** later gets every macvlan network the cluster has in the
same way, as part of joining (see [Joining](cluster.md#joining)).

It is macvlan only: converting moves each host's own connection and is done
node by node, and a spare NIC is by definition not the default one. A node that
already has a macvlan of that name on its default NIC reports it as already
there, so running it again after a node joins or comes back fills the gap; a
node where the name means something else, or with no default route, is
reported and the rest go ahead.

From the CLI:

```bash
lemondx network lan                               # the NICs, and what each can do
lemondx network lan-create lan0 eno1                 # macvlan, the default
lemondx network lan-create lan1 enp2s0 --mode bridge   # a spare NIC
lemondx network lan-create lan --default          # this node's default-route NIC
lemondx network lan-create lan --all-nodes        # every node, on its default-route NIC
lemondx network convert eno1 --dry-run            # the commands, without running them
lemondx network convert eno1 --bridge br0         # asks first
lemondx network revert br0
```

## What each NIC allows

- **Wi-Fi: nothing.** An access point drops frames from any MAC that did not
  associate with it, so neither a bridge nor macvlan works over Wi-Fi.
- **A NIC with no address** (a spare port): a bridge, or macvlan.
- **A NIC holding the host's addresses:** macvlan, or converting it. It cannot
  simply be added to a daemon bridge -- a bridge port cannot keep an address,
  and the host would drop off the LAN.
- **A NIC that is already a bridge's port:** nothing to do -- instances can
  join that bridge directly, and it is listed as a LAN bridge already.

## Reaching the fabrics

An instance on a macvlan network reaches every [fabric](networking.md)
instance in the cluster, on any node, by its fabric address:

- **Routes in the instance.** At launch lemondx gives it a static route to each
  node's fabric subnet, via that node's LAN address -- and to its own host's
  via the host's fabric gateway (below). They are kept where the guest's
  network manager keeps them: a drop-in for the interface's own
  systemd-networkd file (so networkd does not clear them as foreign), or an
  ifupdown `if-up.d` hook elsewhere (Alpine, Debian without networkd). When
  fabrics or members change, lemondx writes them again into every running
  macvlan instance on that node, on the same clock as fabric routes.
- **The fabrics let the LAN in.** Every member publishes which LAN subnets its
  macvlan networks are on, and every fabric firewall admits them. That is the
  whole subnet, not just lemondx's instances: any device on that LAN that adds
  a route can reach fabric instances.
- **The host shim.** macvlan never passes frames between a NIC and its own
  macvlan instances, so on its own the host could not reach them, nor they its
  fabric. lemondx adds a small macvlan interface to the host (`lmdx-<nic>`, no
  address) that its instances *can* reach, plus a `/32` route per instance
  through it (route protocol 134, apart from the fabric's 133), sending from the
  host's own address on that LAN. What a fabric instance opens towards one is
  SNAT'd to that address too (`ip lemondx_shim`). The shim is removed with the
  last macvlan instance on that host. As a side effect the host now reaches
  its own macvlan instances.

An instance that has a fabric NIC as well already routes the whole prefix
through it, and is left alone. The shim and the per-instance routes need the
root helper; without it, an instance still reaches every *other* node's
fabric.

## Converting the host's own NIC

This is the one that changes the host, so it is worth knowing what it does.
lemondx asks NetworkManager to:

1. make a bridge connection `lemondx-br0` with the NIC's **MAC, addresses,
   gateway, DNS and routes** copied from the NIC's own connection -- the same
   MAC means the router hands back the same address, and static extra
   addresses carry over;
2. make a port connection `lemondx-br0-port` putting the NIC in the bridge;
3. keep the NIC's own connection from coming back up on its own (it is not
   deleted: reverting goes back to it);
4. bring the port and bridge up, and wait.

The network drops for a few seconds while the address moves; an open page may
lose the server briefly. If the bridge does not come up with an address -- and,
when the NIC was the host's way out, with the default route -- **everything is
undone** before lemondx answers, and the NIC's own connection brought back.
STP is off and the forwarding delay zero, or the port would spend 30 seconds
learning before passing DHCP.

**Revert** (on the bridge, in *Other host interfaces*) deletes the two
`lemondx-` connections and brings the NIC's own connection back. It is refused
while an instance or profile still has a NIC on the bridge.

It needs NetworkManager to be running the NIC (the default on Ubuntu desktop and
many servers; on Ubuntu, NetworkManager stores these in netplan itself). A host
configured by systemd-networkd or ifupdown is not converted: make the bridge in
that configuration instead, and it shows up as a host bridge instances can join.
For netplan with networkd, for example:

```yaml
network:
  version: 2
  ethernets:
    eno1: {}
  bridges:
    br0:
      interfaces: [eno1]
      dhcp4: true
      macaddress: c8:7f:54:0b:ce:01   # eno1's, to keep its DHCP address
      parameters: {stp: false, forward-delay: 0}
```

### Privilege

Converting runs as root through the same helper as fabrics (`lemondx
fabric-helper`, see [networking](networking.md#privilege)), which reads a
wanted state -- `{"lan": {"action": "convert", "nic": "eno1", "bridge": "br0"}}`
-- rather than a command, and holds it to what it can check itself: the NIC is
a physical, wired NIC that is no bridge's port yet, the bridge a free name, and
only connections named `lemondx-<bridge>` are ever removed. Without the helper,
the dialog and `network convert --dry-run` show the exact commands to run
yourself.

## Docker hosts

Docker loads `br_netfilter`, which sends bridged frames through iptables, and
sets `FORWARD` to `DROP`: a LAN bridge on a Docker host carries nothing until
it is let through. lemondx adds an accept in `DOCKER-USER` for each LAN bridge
(tagged `lemondx-lan`, apart from the fabric's rules), removes it when the
bridge goes, and puts it back after Docker restarts on the same clock as fabric
routes. Without the helper, it says which rules to add. macvlan never passes a
bridge and needs none.

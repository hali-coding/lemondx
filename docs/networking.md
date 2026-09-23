# Fabrics: networking between nodes

[← back to README](../README.md)

A managed bridge is NAT'd. That is right for reaching the internet and wrong
for reaching a container on another host: the source address is rewritten to
the host's, and there is no route back. So by default a container on one node
cannot open a connection to a container on another, even when the hosts are
sitting on the same switch.

A **fabric** fixes that. It is one bridge on every node, each on a /24 of its
own out of a prefix the whole cluster shares, with a host route from every node
to every other node's /24. An instance that joins gets a NIC on it:

```
  container on nodeA                         container on nodeB
    eth0 -> lxdbr0      NAT'd                  eth0 -> lxdbr0      NAT'd
    eth1 -> lemonfab1   10.101.2.31            eth1 -> lemonfab1   10.101.4.18
              |   route 10.101.0.0/16 via eth1           |
        nodeA 192.168.1.10 ---------- LAN ---------- nodeB 192.168.1.11
             ip route 10.101.4.0/24 via 192.168.1.11
```

A cluster can have several fabrics, each with its own prefix. Each is named
after the bridge it puts on every node, so a stack can keep its database tier
on one and its web tier on another.

## What a fabric lets through

- **Within the fabric, routed.** Instances reach each other across nodes by
  their own addresses. Nothing is NAT'd on the way, so the receiver sees who
  really called.
- **Into the fabric, nothing else.** A bridge only accepts traffic from its own
  fabric's prefix, or replies to connections its instances started. A host on
  your LAN, a container on `lxdbr0`, and an instance on another fabric are all
  dropped. The node itself can still reach its own instances.
- **Out of the fabric, NAT'd** (the default). Traffic for anywhere else leaves
  behind the host's address, like any managed bridge. A NAT'd fabric also
  offers a gateway over DHCP, so an instance can have the fabric as its *only*
  NIC and still reach the internet.
- **Without NAT** (`--no-nat`, or the checkbox in the UI), a fabric carries
  traffic within itself and nothing else. It offers no gateway, and anything
  sent elsewhere through it is dropped.

lemondx enforces this with an nftables table of its own, `ip lemondx`, next to
the daemon's `inet lxd` / `inet incus`. It is rebuilt whole on every apply. The
daemon's own `ipv4.nat` stays off on fabric bridges because it would
masquerade traffic to the other nodes' /24s as well.

## What this version needs

- **Every node on one L2 network.** Routing is plain `ip route … via <host>`:
  no tunnel, no encryption, no NAT traversal. `lemondx fabric status` checks
  that each peer is directly reachable and says so if not, rather than leaving
  you with a route that silently drops.
- **Traffic between nodes is in the clear** on your LAN, as it would be between
  two hosts on that LAN.
- **sudo for one command**, below. Without it everything still works except
  writing routes and the firewall, and `lemondx fabric plan` prints what to run
  yourself.
- **iproute2 and nftables** on every node (`ip`, `nft`).

Not in this version: IPv6 on a fabric, and cross-node name resolution. An
instance is reached by address, not by name.

## Creating one

From the **Network** tab, choose **New fabric**. The dialog asks every node what
it already uses (every interface the daemon can see *and* every route in the
host's table, so a VPN's range counts too) and proposes a name and the first
free /16. Each edit is checked again, and the dialog shows which /24 each node
would get before anything is made.

Or from the CLI, on any node:

```bash
sudo ./systemd/install-service.sh --fabric   # once per node
lemondx fabric check                         # what would be picked, and does it fit?
lemondx fabric create                        # lemonfabN on the first free /16, NAT on
lemondx fabric create db --prefix 10.120.0.0/20 --no-nat
```

A prefix is a private /16 to /22 (at most 254 nodes, at least 4). The node you
create it on goes first, then the others side by side; each creates its bridge
and programs its routes and firewall.

**Every node in the cluster must be reachable.** A node that does not answer
cannot be checked for what it already uses, and it would have no way to catch
up later, because nothing reconciles a subnet claim. So while any member is
down, **New fabric** is disabled and `fabric create` is refused. Bring the node
back, or, if it is gone for good, evict it (**Evict** on the Nodes tab, or
`lemondx cluster evict NAME`). Eviction works on a node that cannot be reached:
instead of telling it to stand down, it rotates the cluster credential so the
node can no longer act as a member.

A node that joins the cluster later is given a subnet in every fabric as part
of joining. If that failed, or a node joined while a fabric was being made,
the fabric shows it as *not on it* and **Add missing nodes**
(`lemondx fabric extend NAME`) allocates the rest.

```bash
lemondx fabric list       # every fabric, and each node's part in it
lemondx fabric status     # this node: its subnets, bridges and routes
lemondx fabric plan       # the exact host commands, to run yourself
lemondx fabric apply      # create the bridges, program routes and firewall
lemondx fabric delete db  # remove it from every node
lemondx fabric leave [NAME]   # this node only
```

Deleting is refused while a template names the fabric (as its extra NIC or
as its network), and while any instance on any node still has a NIC on the
fabric. As with deleting a network, lemondx does not detach them for you,
because an instance whose NIC goes keeps running, cut off.

## Putting instances on one

```bash
lemondx create web -i images:alpine/3.21 --fabric lemonfab1
lemondx create solo -i images:alpine/3.21 --network lemonfab1   # the fabric as its only NIC
lemondx template-save web --image images:alpine/3.21 --fabric lemonfab1
lemondx fabric attach existing-instance --fabric lemonfab1     # and `detach`
```

In the UI, fabrics are a group in the **Network** picker of the create and
template dialogs. A fabric with NAT becomes the instance's only NIC (as
`--network` does); one without NAT is added beside the default network (as
`--fabric` does), since it carries nothing else. On the CLI, a bare `--fabric`
means the one fabric this node is on. A template names its
fabric. A node that is not on it launches the instance anyway, without the
NIC, and says so in the run notes. That is the same substitution a template
gets for a pool or a network the node does not have.

Attaching a NIC only gives an instance a *link*: almost every image configures
`eth0` alone. lemondx configures the interface inside the instance as well,
persistently where the image has somewhere to persist it (systemd-networkd or
`/etc/network/interfaces`), and adds the route to the whole fabric prefix
through it. Without that route, traffic to another node's /24 would follow the
default route out of `eth0` and arrive NAT'd behind the host.

### Stacks

This is what makes a stack spanning several nodes work. A step's `{{step.ip}}`
and `{{step.ips}}` hand on the *fabric* address when the instance has one, so a
later step reaches the earlier one wherever it landed. Without a fabric they
hand on a node-local address that means nothing on another host.

## Privilege

lemondx reads the routing table and `/proc/sys` without privilege, so status
and the plan always work. It needs root to write routes and the firewall, and
the shipped rules (`systemd/lemondx-fabric.sudoers`, installed to
`/etc/sudoers.d/lemondx-fabric` by `install-service.sh --fabric`) grant exactly
one command:

```
%lxd ALL=(root) NOPASSWD: /path/to/lemondx fabric-helper
```

The helper reads a *description of the wanted state* on stdin (subnets and the
host each belongs to) and works out the commands itself. Nothing the caller
sends is executed:

- every route must be a /24 inside one of this node's fabric prefixes, reached
  through a directly-connected network;
- every bridge named must be one of this node's fabrics;
- the firewall is built from this node's saved fabric settings alone, so stdin
  cannot add a rule;
- removing a route is held to ownership rather than to the prefixes. lemondx
  marks its routes with protocol 133 and only ever lists those, which is what
  lets a deleted fabric's routes be cleared once its prefix is gone.

It is a command of lemondx's own rather than `ip` for two reasons worth
knowing before you edit that file:

- **`ip` cannot be granted safely.** Without arguments it allows
  `ip netns exec X sh`, which is a root shell.
- **Argument patterns do not confine portably.** Ubuntu 25.10 ships sudo-rs,
  which matches command arguments literally and supports neither wildcards nor
  regular expressions; classic sudo supports both. A pattern that confines on
  Debian is wide open or simply broken on Ubuntu.

The grant goes to the daemon's own admin group (`lxd` or `incus-admin`), which
[docs/security.md](security.md) already treats as root-equivalent: creating a
container on a host is enough to become root on it. Fabrics add no access that
group did not already have.

## Docker hosts

Docker sets the host's `FORWARD` policy to `DROP`, which blocks everything
crossing between bridges. It looks like a working fabric until traffic is
tried. On a host with Docker's `DOCKER-USER` chain, the helper keeps an accept
rule for each fabric bridge there, tagged `lemondx-fabric`, and removes it when
the fabric goes. Isolation is unaffected, because a drop in lemondx's own
table still drops. Without the helper, `lemondx fabric status` prints the
rules to add yourself.

## When it does not work

**`serve` cannot program routes.** The shipped systemd units set
`NoNewPrivileges=yes`, which stops sudo raising privilege at all. That is the
same thing that breaks PAM password logins. Either set `NoNewPrivileges=no` for
the unit (see [docs/service.md](service.md)) or run `lemondx fabric apply`
yourself; the result is identical.

**A node shows `routes` or `no bridge`.** It was told about the fabric but has
not stood it up, usually for want of privilege there. Run
`lemondx fabric apply` on that node; its row says which route is missing.

**A peer's route shows `unreachable`.** Its address is not on a network this
host is directly attached to. This version routes rather than tunnels, so the
nodes have to share an L2 network.

**The prefix collides.** `fabric check` names what it overlaps and on which
node, and a blank prefix proposes one that fits everywhere.

**The name is taken on a node.** A fabric's bridge takes the fabric's name, so
a node that already has a network by that name -- one a fabric did not make --
refuses its subnet rather than re-addressing that network. This mostly shows
up on a node that joined later. Rename or delete the network there, then run
`lemondx fabric extend NAME`.

## Settings

`lemondx configure fabric` sets the one thing that is per node: the address
peers route this node's subnets to (by default the one its cluster URL names).
The fabrics themselves are each a subnet the cluster allocated. They are kept
in the same file, but only ever written by lemondx.

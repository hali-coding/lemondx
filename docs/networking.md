# The fabric: networking between nodes

[← back to README](../README.md)

A managed bridge is NAT'd. That is right for reaching the internet and wrong
for reaching a container on another host: the source address is rewritten to
the host's, and there is no route back. So by default a container on one node
cannot open a connection to a container on another, even when the hosts are
sitting on the same switch.

The **fabric** fixes that. Each node gets a second bridge with NAT off, a /24
of its own out of one cluster-wide /16, and a host route to every other node's
/24. An instance that joins the fabric gets a second NIC:

```
  container on nodeA                     container on nodeB
    eth0 -> lxdbr0        NAT'd            eth0 -> lxdbr0        NAT'd
    eth1 -> lemonfab0  10.100.2.31         eth1 -> lemonfab0  10.100.4.18
             |                                      |
        nodeA 192.168.1.10 ------ LAN ------ nodeB 192.168.1.11
             ip route 10.100.4.0/24 via 192.168.1.11
```

`eth0` is untouched, so the internet keeps working exactly as before. Only
traffic between nodes is new.

## What this version needs

- **Every node on one L2 network.** Routing is plain `ip route … via <host>`:
  no tunnel, no encryption, no NAT traversal. `lemondx fabric status` checks
  each peer is directly reachable and says so if not, rather than leaving you
  with a route that silently drops.
- **Traffic between nodes is in the clear** on your LAN, as it would be between
  two hosts on that LAN.
- **sudo for one command**, below. Without it everything still works except
  writing routes, and `lemondx fabric plan` prints what to run yourself.

Not in this version: IPv6 on the fabric, and cross-node name resolution -- an
instance is reached by address, not by name.

## Turning it on

```bash
sudo ./systemd/install-service.sh --fabric   # once per node
lemondx fabric enable                        # once, on any node
```

`enable` allocates a /24 to every member, tells each one, creates the bridge
and programs the routes. A node joining later is allocated a subnet as part of
joining, with nothing else to do.

```bash
lemondx fabric status     # this node's subnet, bridge and per-peer routes
lemondx fabric plan       # the exact host commands, to run yourself
lemondx fabric apply      # create the bridge and program the routes
lemondx fabric disable    # stop routing here and give the subnet back
```

## Putting instances on it

```bash
lemondx create web -i images:alpine/3.21 --fabric
lemondx template-save web --image images:alpine/3.21 --fabric
lemondx fabric attach existing-instance     # and `detach`
```

A template carrying `fabric` launches onto the fabric wherever it lands. A node
that is not on the fabric launches the instance anyway, without the second NIC,
and says so in the run notes -- the same substitution a template gets for a
pool or a network it does not have.

Attaching a NIC only gives an instance a *link*; almost every image configures
`eth0` alone. lemondx configures the interface inside the instance as well,
persistently where the image has somewhere to persist it (systemd-networkd or
`/etc/network/interfaces`) and by running a DHCP client either way.

### Stacks

This is what makes a stack spanning several nodes work. A step's `{{step.ip}}`
and `{{step.ips}}` hand on the *fabric* address when the instance has one, so a
later step reaches the earlier one wherever it landed. Without the fabric they
hand on a node-local address that means nothing on another host.

## Privilege

lemondx reads the routing table and `/proc/sys` without privilege, so status
and the plan always work. The only thing it needs root for is writing a route,
and the shipped rules grant exactly one command:

```
%lxd ALL=(root) NOPASSWD: /path/to/lemondx fabric-helper
```

The helper reads a *description of the wanted state* on stdin -- subnets and
the host each belongs to -- and works out the commands itself. Nothing the
caller sends is executed. It refuses any route that is not a /24 inside this
cluster's fabric prefix, reached through a directly-connected network, so a
request that reached the fabric code with a bad subnet in it cannot redirect
the host's traffic.

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
container on a host is enough to become root on it. The fabric adds no access
that group did not already have.

## When it does not work

**`serve` cannot program routes.** The shipped systemd units set
`NoNewPrivileges=yes`, which stops sudo raising privilege at all -- the same
thing that breaks PAM password logins. Either set `NoNewPrivileges=no` for the
unit (see [docs/service.md](service.md)) or run `lemondx fabric apply` yourself;
the routes are the same either way.

**Routes are there but nothing crosses.** Check the host's `FORWARD` policy.
Docker sets it to `DROP`, which blocks traffic between bridges:

```bash
sudo iptables -S FORWARD | head -1          # says: -P FORWARD DROP
sudo iptables -I DOCKER-USER -i lemonfab0 -j ACCEPT
sudo iptables -I DOCKER-USER -o lemonfab0 -j ACCEPT
```

`lemondx fabric status` warns when Docker is installed, since this is the one
failure that looks like a working fabric until traffic is tried.

**A peer shows `unreachable`.** Its address is not on a network this host is
directly attached to. This version routes rather than tunnels, so the nodes
have to share an L2 network.

**The subnet collides.** `fabric enable` refuses a prefix overlapping something
the host already uses rather than quietly picking another, because the rest of
the cluster has already been told which subnet this node holds. Pick a
different address space with `lemondx configure fabric`.

## Settings

`lemondx configure fabric` overrides the defaults: the cluster's address space
(`10.100.0.0/16`, always a /16 so it splits into a /24 per node), the bridge
name (`lemonfab0`), and the address peers should route this node's subnet to
(by default the one its cluster URL names).

# Nodes and federation

[← back to README](../README.md)

lemondx manages the host it runs on. Federation lets one lemondx *also* reach
others, so a single UI can list, launch on and sync to every host you run.

There is no leader, no quorum and no shared database. Every node is a complete
lemondx that works on its own; a cluster only means each one knows how to call
the others. A node that is down costs you that node and nothing else.

Joining is **one way, and once**. A node redeems a code against any one member
and comes away a member of the whole cluster — it is given the cluster's
credential and its entire member list, and announces itself to every node in it.
Which node issued the code does not matter, and nothing has to be arranged in
the other direction.

**Only the first node needs any setup**, and barely that: `lemondx cluster
invite` is the setup. A node's name, the address peers reach it on and its TLS
certificate are all worked out and provisioned the first time it federates —
its hostname, the address on its default route, and a self-signed certificate.
`lemondx configure cluster` exists to *override* those, not to make federation
work.

The local host is the default everywhere. A lemondx joined to nothing behaves
exactly as it did before this existed, and a launch that names no node still
runs here.

## What it is not

- **Not LXD clustering.** LXD and Incus have their own clustering, where several
  daemons share one database and one API. lemondx federation sits a layer above
  that and knows nothing about it: each node talks to its own local daemon,
  clustered or not. If you already run an LXD cluster, point one lemondx at it
  and use that — you do not need this.
- **Not a replicated store.** A template synced to four nodes is four
  independent copies. Sync is a push: the node you pushed from wins, wholesale.
- **Not live migration.** Instances belong to the node that created them.

## Starting a cluster

Nothing. On the node you want to start from:

```bash
lemondx cluster invite
```

That one command works out this node's name, address and certificate,
generates the certificate if there is none, forms a cluster around this node,
and prints a code:

```
+ join code for the cluster around nodeA (1 member(s)), valid for 30 minutes:

    lemondx-join.eyJjb2RlIjoibG1keGpvaW5fMDQ4ZDc3NTg4N2M5X19…

Run `lemondx cluster add '<code>'` on the node that should join -- it needs no
setup of its own.
```

The first time it generates a certificate it will also tell you to restart
`lemondx serve`, which then picks the certificate up by itself. The code stays
valid across that restart. Peers only speak HTTPS, so a node has to be serving
it — bind somewhere they can reach, `lemondx serve --host 0.0.0.0`.

`lemondx cluster status` says where a node stands, and names anything that would
stop a peer reaching it:

```
This node
  name            nodeA
  address         https://192.168.75.158:8099
  fingerprint     0e:9a:f0:b4:43:6f:b4:ff:…
  cluster         member of 3 node(s)
  new members     accepted
  authentication  off for this host, API token required from others
```

### Overriding what it worked out

`lemondx configure cluster` sets the node's name, the address peers use and the
certificate. You need it when the guess is wrong — a DNS name rather than an IP,
an address behind NAT, a port other than the one being served, or a certificate
from your own CA. Saved values win over anything worked out, and `lemondx serve`
uses the saved certificate unless `--tls-cert` or `--no-tls` says otherwise.

`lemondx cluster cert` writes a fresh self-signed certificate on demand. Any
certificate works: federation pins the key rather than trusting an issuer, so
being self-signed costs nothing.

## Joining

On the joining node — which needs **no** setup, no certificate and no
configuration:

```bash
lemondx cluster add 'lemondx-join.eyJjb2RlIjoibG1keGpvaW5f…'
```

In the web UI the same pair is **Invite** and **Add node** on the Nodes tab.

```
+ joined the cluster through nodeA -- now a member alongside nodeA, nodeB
  this node is nodeC at https://192.168.75.192:8099
```

What happens:

1. The joining node works out its own identity and generates a certificate if it
   has none.
2. It connects to the address in the code and checks the certificate against the
   fingerprint in it **before sending anything**. A mismatch aborts with nothing
   transmitted.
3. It presents the code along with its own name, address and fingerprint. The
   other node redeems it — constant-time, single use, expiring — and answers with
   the cluster credential and the full member list.
4. The joiner announces itself to every member it just learned about.

So joining *any* member joins the cluster. A member that was down when you
joined finds out later, from `lemondx cluster refresh` on either side.

Codes last 30 minutes by default (`--expires`), are consumed on first use, and
can be listed and withdrawn:

```bash
lemondx cluster invites
lemondx cluster revoke-invite <id>
```

## Membership

```bash
lemondx cluster refresh      # pull every peer's member list, push ours
lemondx cluster leave        # give up membership (local; keeps instances)
```

Membership converges by `refresh`: it announces this node to every peer and
merges back what each of them knows. Joining runs it once; run it by hand after
a node has been away.

## Nodes and groups

```bash
lemondx cluster nodes                # who this node knows, and how they are
lemondx cluster show nodeB           # one node, with the instances on it
lemondx cluster containers           # every instance, across nodes
lemondx cluster remove nodeB         # forget a node
```

A group is a name for a set of nodes, so a launch or a sync can say "everywhere"
once:

```bash
lemondx cluster group set edge --node nodeA --node nodeB
lemondx cluster groups
lemondx cluster group delete edge
```

A group is just a list of names. It can name a node that is not enrolled yet —
listings mark those as unknown rather than dropping them — but launching at a
group with an unknown member is refused rather than quietly doing less than
asked.

Removing a node drops it from this node's registry and from every group, and
tells every other member to do the same:

```bash
lemondx cluster remove nodeC            # from the cluster, everywhere
lemondx cluster remove nodeC --rotate   # and cut off its credential
```

Because the credential is shared, removal alone does not stop the removed node
calling in — it still holds one. `lemondx cluster rotate` replaces the credential
on every remaining member, which is what actually cuts it off. `remove --rotate`
does both, and the CLI says so when you skip it.

## Seeing the cluster in the Containers tab

Once a node is in a cluster, the Containers tab grows a **Showing** picker:
this node, the whole cluster, one node, or one group. The Templates tab reads
the same setting, so a template's instances and the container list never
disagree about which hosts are in view.

Widened, the table gains a **Node** column and every row says where it is. The
buttons keep working across hosts — start, stop, restart and delete are routed
to the node that owns the instance, and a multi-row selection may span nodes.
Each instance reports its own outcome, so one unreachable node does not sink
the rest.

**Every row opens.** Clicking an instance on another node opens the same detail
drawer it would on its own host: overview and limits, snapshots, bootstrap, and
the console. Each call is routed to the node that owns the instance, so a
cluster is managed from wherever you happen to be looking.

The bootstrap picker's modules and SSH keys come from *that* node, not this one:
a module runs there and a key is installed from what that host can see, so
offering this node's would be offering the wrong thing.

Health dots are the exception — each node's health monitor judges its own
instances on its own schedule, so only this node's rows show one. The external
link on a remote row opens that node's own UI, for the things that are still
about the host rather than the instance: its storage, networks and nodes.

### Reaching one node's own API

`/api/nodes/{node}/{path}` forwards a call to that member and returns its
answer, which is what the drawer is built on:

```bash
curl .../api/nodes/prdev2/containers/web-1                 # its detail
curl -X POST .../api/nodes/prdev2/containers/web-1/exec \
     -d '{"command":"uptime"}'                             # a console command
```

Naming this node works too and simply runs the call here, so a client never has
to special-case it. Two limits are deliberate: a proxied call cannot itself be
proxied, and the access required is the *target* endpoint's, resolved against
the same route table — so forwarding can never let someone do more than calling
that endpoint directly would. A path this node does not recognise is treated as
admin-only rather than waved through.

The choice is remembered per browser, not on the server: it is how one person
is looking at their cluster right now, not a property of the cluster. A
remembered scope naming a node that has since gone falls back to this node.

The picker only appears when there is more than one node, so an unfederated
lemondx looks exactly as it did.

## Launching across nodes

```bash
lemondx launch web -n 6 --group edge
lemondx launch web -n 2 --node nodeB
```

In the UI, Launch opens a dialog with a node picker whenever more than one node
exists, and it starts on whatever the Containers tab is scoped to — so
switching the view to a group and pressing Launch does the obvious thing.

Instances are spread round robin, and names are allocated across every chosen
node at once — so `web-3` is one instance in the cluster, not one per host.
The template is pushed to each node first, so a node that has never seen it can
still run the launch.

Each node reports its own outcome. One node failing does not stop the others,
and what it would have created is reported as failed with the reason:

```
+ web-1 on nodeA is running
+ web-3 on nodeA is running
! web-2 on nodeB: Cannot reach https://…:8099: [Errno 111] Connection refused
```

### Placement falls back per node

A storage pool, a network and a profile are all *local* names. A template
written against `fast-nvme` and `dmz-br` would fail every instance on a node
that has neither — the worst outcome, since nothing runs and you find out one
instance at a time.

So each node substitutes its own default for a placement it cannot honour, and
says so. The template is not changed, and every other node still gets what it
asked for:

| Template names | Node does not have it | Node uses |
| --- | --- | --- |
| `pool` | no such storage pool | the default profile's root disk |
| `network` | no such managed network | the default profile's NIC |
| `profiles` | no such profile | the remaining profiles, else `default` |

Every substitution is reported twice: on the run, where the UI shows it against
the template and `launch` prints it, and in that node's own log, because a
launch started from another node's UI is only visible there.

```
! nodeA: No storage pool 'fast-nvme' on this node; used the default profile's pool (default) instead.
! nodeB: No network 'dmz-br' on this node; used the default profile's network (lxdbr0) instead.
```

What a node cannot substitute — an image it cannot pull, no space, a module that
fails — fails that node's instances and leaves the rest alone.

### Acting on a template's instances across nodes

Destroy, recreate and run-command work the same way. Each node is handed its
own share and checks it against its own tagged set, so a node that has gained
or lost an instance since you looked refuses its share rather than the run
quietly acting on the wrong thing. The whole thing is recorded as one run on
the template, exactly as a single-node run is.

## Shared definitions stay level by themselves

Templates, modules, bootstrap profiles and node groups are things the whole
cluster is meant to agree on, so **saving one pushes it to every member there
and then** — no sync step to remember. The same goes for deleting one, since a
copy left behind on one node is drift that a push-only sync can never clear.

```
$ lemondx template-save web --image images:alpine/3.24
+ saved template web: images:alpine/3.24
  + synced to 2 node(s): prdev1, prdev2
```

It works from whichever node you are on — there is no primary — and a node that
receives a push does not pass it on again, so nothing echoes round the cluster.

The push is best-effort by design: the save has already happened locally and is
not undone because another host is switched off. What each node made of it comes
back with the record, and a node that missed out is named:

```
$ lemondx template-save web --image images:alpine/3.24
+ saved template web: images:alpine/3.24
  ! not synced to prdev2 (Cannot reach https://…:8099: Connection refused)
    -- run `lemondx cluster sync` when they are back
```

Deleting removes it from every member. Pass `--local-only` (or untick the box in
the UI's confirmation) to leave the other nodes' copies alone.

## Catching up a node that drifted

Sync is the manual counterpart, for a node that was down when something changed:

```bash
lemondx cluster sync --group edge                        # every template
lemondx cluster sync --kind modules --kind templates --node nodeB
lemondx cluster sync --name web --node nodeB             # just this one
```

The UI has the same thing behind **Sync** on the Nodes tab.

Notes:

- Node groups sync too, as `--kind groups`.

- It is one direction, and it overwrites. There is no merge and no
  last-writer-wins clock, because there is no shared clock — the node you pushed
  from is the answer to "which copy is right?", and the only answer that needs
  no coordination between hosts.
- Syncing a template also pushes the uploaded modules it uses, so it is not
  broken on arrival. Built-in modules ship with lemondx and are skipped.
- Only uploaded modules are pushed. A built-in exists on every node already.
- Templates carry no secrets — a template never stores one — so a sync never
  moves a credential. Public SSH keys in a template do travel.
- A cluster launch pushes the template it is launching, so the common case needs
  no explicit sync at all.
- Sync only pushes. It never removes something the target has and this node does
  not, which is why deleting propagates on its own.

## Security

Federation is off until you set it up, and setting it up is deliberately
hand-carried. See [Security](security.md#federation) for the whole picture; in
short:

- **Every call between nodes is HTTPS.** A node URL must be `https://` unless
  its host is a loopback address, where the traffic never reaches a network.
- **Certificates are pinned, not trusted.** A peer is recognised by the SHA-256
  of the certificate it presents, recorded when it joined. The pin is checked
  after the handshake and before the request is written, so a node that answers
  with the wrong certificate never sees the credential.
- **The join code is the only thing sent over an unauthenticated call**, and it
  is a 32-byte single-use secret that expires.
- **One credential, shared by the cluster.** It is an ordinary lemondx API
  token — it shows up in `lemondx tokens` as `cluster`, and every member holds
  the same one. That is what lets joining be one-way: a new node can call
  everybody immediately, and everybody can call it, without a per-pair exchange
  that would need every node up at the same moment. The cost is that removing a
  node means rotating it (`lemondx cluster rotate`), and that any member is an
  admin of every other — so federate hosts you administer, not someone else's.
- **A member demands a credential from other hosts even with authentication
  off.** Joining a cluster means accepting API calls from elsewhere, which a
  node cannot do while treating whoever reaches its port as an admin. Loopback
  is untouched, so nobody is locked out of the UI on their own host, and no
  auth setup is needed to join. Configure real authentication
  (`lemondx configure auth`) if people other than you use that node.
- **Node records are safe to copy; the credential is not in them.**
  `nodes/*.json` holds a name, URL and fingerprint. The credential lives in
  `auth/cluster.json` in the `0700` auth directory.

Rotating a node's TLS certificate changes its fingerprint, so its peers will
refuse it — with a message saying exactly that. Re-join it with a fresh code.

## Where it is kept

```
~/.local/share/lemondx/
  nodes/<name>.json        name, URL, certificate fingerprint  (safe to copy)
  node-groups/<name>.json  a group's members
  auth/cluster.json        the cluster credential this node calls peers with (0700)
  auth/tokens.json         its hash too, so peers can authenticate to us
  auth/invites.json        hashes of join codes not yet redeemed (0700)
  config/cluster.json      name, address and certificate, once worked out
  tls/node-cert.pem        generated on first federating
  runtime.json             what `serve` is listening on, so the CLI can advertise it
```

Like profiles and templates, a `nodes/` or `node-groups/` file is one record
that can be copied between machines or checked into a repo, and is treated as
untrusted input on read. Adding a node by hand takes a token it will accept:

```bash
lemondx cluster fingerprint https://10.0.0.5:8099    # what it presents right now
```

That only shows what answers at an address — whoever that is chooses what to
present — so it is for confirming a fingerprint you were given out of band, not
for obtaining one.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/cluster` | this node's name, address, fingerprint, whether it can be joined |
| `GET` | `/api/cluster/nodes` | every node with its state (`?probe=false` to skip contacting them) |
| `GET` | `/api/cluster/nodes/{name}` | one node, with the instances on it |
| `POST` | `/api/cluster/nodes` | `{"code":"lemondx-join.…"}` → join that node's cluster |
| `DELETE` | `/api/cluster/nodes/{name}` | remove a node, here and on every member |
| `POST` | `/api/cluster/leave` | give up membership |
| `GET`/`POST` | `/api/cluster/members` | the member list / announce a member |
| `DELETE` | `/api/cluster/members/{name}` | a peer telling us a node has gone |
| `POST` | `/api/cluster/refresh` | pull every peer's member list and push ours |
| `POST` | `/api/cluster/rotate` | replace the cluster credential everywhere |
| `PUT` | `/api/cluster/secret` | take a rotated credential from the member rotating it |
| `GET` | `/api/cluster/containers` | instances across nodes (`?all=true`, `?nodes=`, `?groups=`) |
| `POST` | `/api/cluster/containers/state` | one state change over instances on several nodes |
| `POST` | `/api/cluster/containers/delete` | delete instances on several nodes |
| *any* | `/api/nodes/{node}/{path}` | make that call against one node's own API |
| `GET` | `/api/cluster/groups` | node groups |
| `PUT`/`DELETE` | `/api/cluster/groups/{name}` | create or replace / delete one |
| `GET`/`POST` | `/api/cluster/invites` | unredeemed join codes / issue one |
| `DELETE` | `/api/cluster/invites/{id}` | withdraw one |
| `POST` | `/api/cluster/sync` | `{"kinds":["templates"],"groups":["edge"]}` |
| `POST` | `/api/cluster/fingerprint` | `{"url":"https://…"}` → what it presents now |
| `POST` | `/api/cluster/enroll` | redeem a join code — **node to node**, no token |

`POST /api/templates/{name}/launch` takes `nodes` and `groups` to spread a
launch; with neither it runs on this node alone. `names` asks for particular
instance names, which is how a coordinating node keeps numbering unique across a
cluster.

`/api/cluster/enroll` is the one endpoint answered without a token, because it is
how a node gets its first one. The code in the body is the credential.

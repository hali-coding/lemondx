# Security

[← back to README](../README.md)

`serve` binds to `127.0.0.1` and, unless you ask for more, has no
authentication: anyone who can reach the port controls your containers. Keep
in mind what that means — creating a container is enough to become root on
the host, which is why the daemon's own `lxd`/`incus-admin` group is treated
as root-equivalent.

Authentication is opt-in. There are four methods, usable together:

| Method | Who logs in | Good for |
| --- | --- | --- |
| `local` | users lemondx keeps itself (`lemondx user-add`) | a shared box, containers, macOS — anywhere PAM is awkward |
| `pam` | accounts on the host, by password | a personal workstation; see the caveats below |
| `proxy` | whoever a trusted reverse proxy says | SSO, OIDC, 2FA via Authelia, oauth2-proxy, Tailscale and friends |
| `token` | API tokens only (`lemondx token-create`) | scripts and CI, no browser logins |

API tokens are accepted whenever any method is on, and `--token` still works as
a single static admin token.

The easiest way to set this up is to answer a few questions once:

```bash
lemondx configure auth          # pick methods, PAM groups, proxy, token file; offers a first user
lemondx configure auth --show   # what is saved (add --json for the raw settings)
lemondx configure auth --reset  # forget them: authentication off again
./lemondx serve                 # uses the saved settings from now on
```

Settings go to `~/.local/share/lemondx/config/auth.json` (0600) and every later
`serve` loads them. Any flag given to `serve` overrides just that setting —
`--auth` replaces the saved method list, `--session-hours 1` changes only that —
and `--ignore-config` skips the file altogether. If the file exists but does not
validate (bad JSON, a misspelt key, an impossible value), `serve` refuses to
start and names the problem, rather than quietly starting with authentication
off. The file only ever references a static token by path, never contains one.

The same can be done with flags alone:

```bash
lemondx user-add alice --role admin          # asks for a password
./lemondx serve --auth local                 # the UI now shows a login form
```

## Roles

There are two: `admin` may do anything, `read` may only look. Every `GET` is
`read`; every other request, and opening a terminal, is `admin`. The UI hides
nothing from a read-only user but disables what they cannot do, and the
server refuses it regardless with `403`.

## Without auth

Even with auth off, lemondx refuses two kinds of request a browser can be
tricked into sending:

- a non-`GET` request (or WebSocket upgrade) whose `Origin` is another site, so
  a page you visit cannot create or delete containers through your browser;
- when bound to loopback, any request whose `Host` is not a loopback name, so
  DNS rebinding cannot turn `evil.example` into `127.0.0.1`.

Scripts that send no `Origin` are unaffected.

## Sessions

A password login sets an `HttpOnly`, `SameSite=Strict` cookie (`Secure` over
TLS), valid for `--session-hours` (default 12). Sessions live in memory:
restarting lemondx logs everyone out. A session ends early when its user is
removed or has their password changed — from the UI or the CLI, which edits
the data directory the server re-reads — and a PAM user's role is re-checked
every five minutes, so leaving the admin group takes effect without a restart.

Failed logins are throttled: five per username or twenty per address, then a
lockout that doubles from 15 seconds up to 15 minutes, answered with `429`.

Password logins are refused over plain HTTP unless the request comes from
loopback, arrives over TLS, or comes through a `--trust-proxy` address.
`--allow-insecure-login` lifts that, if you really mean it.

## API tokens

```bash
lemondx token-create ci --role read --expires 30d   # prints the token once
lemondx tokens
lemondx token-revoke 3f9c0a1b2d4e
```

Clients send `Authorization: Bearer lmdx_…` (or `X-Lemondx-Token`). A browser
WebSocket cannot set headers, so terminals also take `?token=`; that value is
masked in the request log. Tokens can also be made and revoked from the
**Access** tab, by anyone logged in, for themselves — admins see everyone's.

Only a SHA-256 of each token is stored, in `~/.local/share/lemondx/auth/`
(`0700`, files `0600`). Revocation applies on the next request, no restart. A
token cannot create more tokens (that would let it outlive its own expiry),
and a read-only user cannot mint an admin one.

For the static token, prefer `--token-file PATH` or the `LEMONDX_TOKEN`
environment variable over `--token`, which any local user can read in `ps`.

## Local users

```bash
lemondx user-add bob                   # read-only unless --role admin
lemondx user-set bob --role admin
lemondx user-set bob --password
lemondx user-remove bob
```

Passwords are hashed with scrypt (PBKDF2-SHA256 where Python's OpenSSL lacks
it). `LEMONDX_PASSWORD` supplies one non-interactively. Admins can also manage
users from the **Access** tab.

## PAM

`--auth pam` checks host passwords through libpam. Membership decides the role:
`--pam-admin-group` (default: `lxd` and/or `incus-admin`, whichever exist) gives
admin, `--pam-read-group` gives read, anyone else is refused even with the right
password.

lemondx uses the PAM service named by `--pam-service` (default `lemondx`).
Create it, or PAM falls back to `other`, which denies everything on some
distributions. `systemd/lemondx.pam` is a ready one -- it delegates to the
host's own stack, and carries the one-line swap for distributions with a
single `system-auth` instead of Debian's `common-auth`:

```bash
sudo install -m 644 systemd/lemondx.pam /etc/pam.d/lemondx
```

Two properties of `pam_unix` decide who can actually log in, and `serve` warns
about both at startup:

- **Not running as root, PAM can only verify the account lemondx runs as.** Its
  `unix_chkpwd` helper refuses to check anyone else's password. As a user
  service that means you, which is usually exactly right.
- **`NoNewPrivileges=yes` breaks it entirely.** The helper reads `/etc/shadow`
  through its setgid bit, which that setting disables — and the shipped systemd
  units set it. See [Running as a service](service.md#authentication).

To check a stack without starting the server:

```bash
lemondx pam-test "$USER"                  # asks for the password
```

## Trusted proxy

```bash
./lemondx serve --auth proxy --trust-proxy 127.0.0.1 \
  --proxy-user-header X-Forwarded-User \
  --proxy-groups-header X-Forwarded-Groups --proxy-admin-group ops --proxy-read-group dev
```

The user header is believed only from `--trust-proxy` addresses (IPs or CIDRs);
from anywhere else it is ignored. Without the group flags every proxied user is
an admin. Keep lemondx bound to loopback or firewalled so the proxy is the only
way in, and make sure the proxy strips these headers from incoming requests.
Behind a trusted proxy, `X-Forwarded-For` keys the login throttle,
`X-Forwarded-Host` the Origin check, and `X-Forwarded-Proto: https` marks
cookies `Secure`.

## TLS

```bash
./lemondx serve --host 0.0.0.0 --auth local \
  --tls-cert /etc/lemondx/cert.pem --tls-key /etc/lemondx/key.pem
```

TLS 1.2 or newer, with the handshake done per connection so a client that
stalls cannot block others. A TLS-terminating reverse proxy works just as well;
either way, do not send passwords or tokens across a network in the clear.

`lemondx configure cluster` saves a certificate for federation, and `serve` then
uses it without being told to; `--tls-cert` overrides it and `--no-tls` declines
it, for a node behind a proxy that terminates TLS.

The static UI files are served without authentication — they contain no data.
Only `/api/*` is gated.

## The fabric holds privilege, opt in

Everything else in lemondx is a front end to a socket. The fabric is the one
feature that writes host state: a route between two nodes is kernel state no
container API can set. It is off until you install its sudo rules, and without
them `lemondx fabric plan` prints the commands for you to run instead.

The rules grant one command, to the daemon's own admin group:

```
%lxd ALL=(root) NOPASSWD: /path/to/lemondx fabric-helper
```

Two things bound what that means:

- **The helper takes data, not commands.** stdin carries subnets and the host
  each belongs to; the helper decides what to run. It refuses any route that is
  not a /24 inside this cluster's fabric prefix, reached through a
  directly-connected network -- so the default route and the host's own LAN
  cannot be expressed through it at all.
- **The group is already root-equivalent.** As above, creating a container is
  enough to become root on the host, which is why `lxd`/`incus-admin` is
  treated that way throughout. The fabric grants that group nothing it did not
  already have.

`ip` itself is deliberately *not* granted: without arguments it allows
`ip netns exec X sh`, a root shell, and argument patterns cannot confine it
portably -- Ubuntu 25.10's sudo-rs matches arguments literally and supports
neither wildcards nor regular expressions, while classic sudo supports both.
See [networking.md](networking.md).

## Federation

Federating nodes is opt-in and hand-carried: nothing is trusted for being on the
same network. See [Nodes and federation](cluster.md) for the workflow; this is
what it rests on.

**Nodes recognise each other by certificate, not by CA.** A peer's record holds
the SHA-256 of the certificate it must present, taken when it joined. The pin is
checked after the TLS handshake and *before* the request is written, so a node
answering with the wrong certificate never sees the credential. A self-signed
certificate is expected and fine — pinning is the stronger claim anyway: this
exact key, rather than "someone a CA vouched for". Rotating a node's certificate
changes its fingerprint, and its peers will refuse it, saying so, until it
re-joins.

**Every call between nodes is HTTPS.** A node URL is rejected unless it is
`https://`, or `http://` to a loopback address where the traffic never reaches a
network.

**Joining is the only unauthenticated call**, and only because it is how a node
gets its first credential. `lemondx cluster invite` mints a 32-byte single-use
code that expires (30 minutes by default) and stores only its hash, in the `0700`
auth directory — so a code issued from the CLI is honoured by an already-running
`serve`, and redeeming it consumes it. The code carries the issuing node's
fingerprint, so the joiner pins the connection before sending the secret: the
secret proves the joiner was given permission, the fingerprint proves it reached
the right node.

**The cluster shares one credential.** It is an ordinary lemondx API token,
stored like any other and listed by `lemondx tokens` as `cluster`, and every
member holds the same one. That is what makes joining one-way: a node that has
redeemed a code can immediately call every member and be called by them, with no
per-pair exchange needing every host up at once. Two consequences worth being
clear about:

- **Any member is an admin of every other.** Federate hosts you administer.
  Anyone who can create a container on a node can become root on it.
- **An evicted node is cut off by giving up its own copy of the credential.**
  `lemondx cluster evict` tells it to stand down before forgetting it, and
  standing down deletes the credential there. A node that could not be told
  still holds a working one, so eviction then replaces the credential on every
  remaining member instead — which is also what `lemondx cluster rotate` does on
  its own. A member that is down during a rotation is left behind and has to
  re-join, which is why the rotation is not unconditional; `--rotate` and
  `--no-rotate` decide it by hand. Either way the command says what happened.

- **Syncing users copies password hashes between members.** `lemondx cluster
  sync --kind users` exists so one set of logins works on every node, and an
  account is only usable on another host if its hash goes with it — there is no
  plaintext kept anywhere to re-hash there. The hash travels over the same
  pinned-certificate TLS as everything else, to a host that is already an admin
  of this one, and `PUT /api/auth/users/{name}/record` refuses any caller that
  is not a member. It replaces an account of the same name on the target, so it
  can change an administrator's password on another host: that is the point of
  it, and the reason it is never automatic. Nothing else sync carries is a
  credential.

**A member demands a credential from other hosts even with authentication off.**
Joining a cluster means accepting API calls from elsewhere, and a node cannot do
that while treating whoever reaches its port as an admin. So membership alone
turns on "callers from other hosts must present a token", which is why joining
needs no auth setup and does not quietly open the node up either. Loopback is
deliberately untouched: the person at that host keeps the anonymous-admin UI they
had. Configure real authentication if anyone else uses that node directly.

**Secrets stay out of copyable records.** `nodes/*.json` holds only a name, URL
and fingerprint, so it can be copied between machines the way profiles and
templates are; the cluster credential lives in `auth/cluster.json` with the other
credentials. A synced template carries no module secret — a template never stores
one — though public SSH keys in it do travel.

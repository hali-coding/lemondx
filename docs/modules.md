# Bootstrap modules

[← back to README](../README.md)

A module is a plain shell script that runs *inside* a container after it
starts. Pick the ones you want when creating a container, or run them later
against an existing one.

```bash
./lemondx modules                       # what is available
./lemondx create dev -i images:debian/12 \
    -b base -b ssh-access \
    --param USERNAME=hampus --all-ssh-keys
./lemondx bootstrap dev -b docker       # add more later; modules are re-runnable
```

In the web UI the create dialog has a **Bootstrap** section, and each container
has a **Bootstrap** tab with the same picker and a live log.

## Shipped modules

| id | what it does | parameters |
| --- | --- | --- |
| `base` | package index, curl, CA certs, sudo, editor | — |
| `ssh-access` | install OpenSSH, create a user, install your key for it | `USERNAME`, `SHELL_PATH`, `PERMIT_ROOT_LOGIN`, `PASSWORD_AUTH` |
| `docker` | Docker plus the service | — |
| `nodejs` | Node.js and npm | — |
| `postgresql` | PostgreSQL, a role with a password, optional database | `POSTGRES_PASSWORD` (secret), `POSTGRES_USER`, `POSTGRES_DB`, `LISTEN_ADDRESSES` |
| `apache2` | Apache httpd, optionally with a virtual host you paste in | `VHOST_CONFIG` (text), `VHOST_NAME`, `ENABLE_MODULES`, `DISABLE_DEFAULT_SITE` |

Modules run in `order`, low to high, so `base` (10) precedes `ssh-access` (20)
whatever sequence you tick them in. A failing module stops the run and its
output is reported.

`ssh-access` used to be three separate modules (a server, a user, and a key
installer), pickable independently. In practice every real use needed all
three -- a user with no way in, or a server with no one to log in as, isn't
useful on its own -- so they are one module now: installing sshd, creating
the account and putting your key on it always happen together, and a key is
mandatory rather than optional.

## SSH keys

A module declares `uses: ssh-keys` in its header (only `ssh-access` does, but a
module you write can too) to receive the keys you select as `LEMONDX_SSH_KEYS`;
the prelude's `install_ssh_keys USER` writes them to that user's
`authorized_keys` with the right ownership and permissions. Selecting such a
module makes the runner refuse to start without at least one key.

Keys come from `~/.ssh/*.pub` (`--all-ssh-keys`, or ticked in the UI),
`--ssh-key PATH`, or pasted into the UI. Selecting a key-installing module
promotes the key picker into the main create dialog — outside the collapsible
Bootstrap section — and creation is blocked until at least one key is chosen,
since it would otherwise fail in the container. Every key is validated before it goes
anywhere: only known public-key types are accepted, anything containing
`PRIVATE KEY` is refused outright, and embedded newlines and control
characters are rejected so nothing can smuggle a second `authorized_keys`
entry or an options field past the parser. **Private keys are never read** —
only `*.pub` files are ever opened.

Accounts created by `ssh-access` get shadow field `*` (no usable
password) rather than `!` (locked): OpenSSH built without PAM, as on Alpine,
refuses a locked account even for key authentication.

## Defaults, saved settings and profiles

The **Modules** tab manages all of this; the CLI mirrors it. It lists every
module with how many settings it has, which profiles and templates use it, and
whether it is pre-selected; click one to open its page, which has its settings,
its script, and the facts from its header. Profiles have their own sub-tab.

**Defaults.** Tick *Pre-selected* on a module and it is pre-selected every time
you create a container.

```bash
./lemondx module-set docker --default
./lemondx create dev -i images:debian/12      # runs your defaults
./lemondx create bare --no-default-modules    # opt out
```

**Saved settings.** A module's parameters are remembered. Edit them in the
module's page, or just run a bootstrap — values that differed from the module's
own default are saved automatically, so the next container starts from what
worked last time. The UI marks these *saved* and shows the module's original
value beside them, with *Reset* to go back to it.

```bash
./lemondx module-set ssh-access --param USERNAME=hampus --param SHELL_PATH=/bin/sh
```

**Bootstrap profiles** are named module selections with their parameters and
SSH public keys — "Save as profile" in the create dialog, or:

```bash
./lemondx profile-save "Web server" -b base -b ssh-access -b docker \
    --param USERNAME=hampus --all-ssh-keys --description "my usual dev box"
./lemondx profiles
./lemondx create web -P "Web server"
```

A profile stores every non-secret parameter of its modules, not only the ones
you changed: values you gave, then your saved settings at that moment, then the
module's defaults. So it keeps doing the same thing when those settings change
later. Keys given alongside `-P` are added to the profile's own. To save the
image, limits and everything else as well, use an
[instance template](templates.md).

Deleting a module is refused (`409`) while a profile or a template still
selects it; the error names them. Removing an upload that shadows a built-in
is always allowed, since the built-in takes its place. When a delete arrives
from another cluster member instead, it was checked there, and the module is
dropped from every profile and template here that used it; a profile left
with nothing to run is removed rather than kept as an empty shell.

These are lemondx's own; they have nothing to do with LXD/Incus profiles, which
configure devices and limits. The CLI flag is `-P/--bootstrap-profile`, because
`--profile` already means the daemon's kind.

## Where all this is kept

Everything lemondx remembers lives under `~/.local/share/lemondx`, and is meant
to be readable and editable by hand:

```
~/.local/share/lemondx/
  settings.json         default modules and remembered parameter values
  profiles/
    web-server.json     one file per bootstrap profile
  templates/
    web-server.json     one file per instance template
  modules/
    redis.sh            uploaded modules
```

`LEMONDX_DATA_DIR` points the whole thing elsewhere; `XDG_DATA_HOME` is
honoured if you set it.

A profile is one JSON file, named after a slug of the profile name, with the
real name inside it:

```json
{
  "name": "Web server",
  "description": "my usual dev box",
  "modules": ["base", "ssh-access", "docker"],
  "params": { "USERNAME": "hampus", "SHELL_PATH": "/bin/bash" },
  "ssh_keys": ["ssh-ed25519 AAAA… hampus@laptop"]
}
```

So a profile can be copied between machines, or checked into a project and
dropped in — anything in `profiles/` is picked up, and a file mangled into
invalid JSON costs that one profile rather than all of them.

Earlier versions kept all of this in `~/.config/lemondx`. That directory is
moved here the first time you run a newer lemondx, profiles included, so there
is nothing to do by hand. `LEMONDX_CONFIG_DIR` still works and still names the
whole directory; a directory you name explicitly is never migrated away from.

## Uploading modules

Drop a `.sh` file into `~/.local/share/lemondx/modules`, or upload it through
the Modules tab (choose a file or paste the script). An uploaded module's
script can be edited on its page, and a built-in one can be *customised*:
saving writes your copy under the same name, which shadows the built-in until
*Revert to built-in* deletes it. Reverting keeps the defaults, profiles and
templates that name the module, since the built-in is still there to run.
From the CLI:

```bash
./lemondx module-add ./redis.sh --default
./lemondx module-remove redis
```

Uploads are validated before they are stored: the name is reduced to a safe id
that cannot escape the module directory, the content must be UTF-8 text under
256 KiB, and it must parse as POSIX shell (`sh -n`, which parses without
running anything). A module with the same name as a built-in shadows it rather
than replacing it, and built-ins can never be deleted.

Uploading does not execute anything. The script runs later, inside a container,
and only when you select it.

## Writing your own

Drop a `.sh` file in `modules/`, or in `~/.local/share/lemondx/modules` to keep
it outside the repo (a file there shadows a shipped module of the same name).
`LEMONDX_MODULES` adds more directories. Those win over uploads, so lemondx
refuses to upload over, or delete, a module one of them supplies.

```sh
#!/bin/sh
# name: Redis
# description: Install Redis and start it.
# order: 60
# param: REDIS_PORT=6379  Port to listen on

pkg_install redis
svc_enable redis
log "redis on port ${REDIS_PORT}"
```

Every module is prepended with `modules/_prelude.sh`, which provides
`log` / `warn` / `die`, `have`, `pkg_refresh`, `pkg_install`, `svc_enable` and
`install_ssh_keys`, and sets `LEMONDX_OS_ID` and `LEMONDX_PKG`. The package
helpers cover apt, dnf/yum/microdnf, apk, pacman and zypper; `svc_enable`
handles systemd and OpenRC.

Modules run under `/bin/sh` — dash on Debian, busybox ash on Alpine — because
minimal images often have no bash. **Keep them POSIX.** `dash -n module.sh`
catches most mistakes.

Declared `param` values arrive as environment variables, with the declared
default applied when you do not override it.

## Multi-line parameters

A parameter that holds a config file rather than a word is declared with
`text:` -- same grammar as `param:`:

```sh
# text: VHOST_CONFIG=  Virtual host configuration
```

The UI edits it in a text area instead of a one-line input, and the CLI lists
it as `(N lines)`. Otherwise it is an ordinary parameter: saved, stored in
profiles and templates, and passed to the module as one environment variable,
newlines intact. Write it out with `printf '%s\n' "$VAR" > file` rather than
`echo`, which some shells let interpret backslashes. From the CLI, read the
value from a file:

```bash
./lemondx bootstrap web -b apache2 --param "VHOST_CONFIG=$(cat site.conf)"
```

## Secret parameters

Declare a password, token or key with `secret:` instead of `param:` — same
grammar, different handling:

```sh
# secret: POSTGRES_PASSWORD=  Password for the role (at least 8 characters)
```

A secret has no default (anything after `=` is ignored) and must be supplied on
every run; bootstrap refuses to start without it. It is never remembered as a
setting, cannot be saved with `module-set`, is stripped from bootstrap profiles
and templates (including hand-edited ones), and every occurrence of its value is replaced
with `********` in the output shown in the UI, the terminal and `--json`. The
UI renders it as a password field and clears it once used.

From the CLI, avoid `--param` for secrets — it lands in shell history and
`ps`. Instead set an environment variable of the same name, or let lemondx
prompt without echo:

```bash
./lemondx create db -i images:debian/12 -b postgresql      # prompts
POSTGRES_PASSWORD="$(pass show db)" ./lemondx bootstrap db -b postgresql
```

Redaction is a safety net, not a licence: a module should still never print a
secret, and should keep it off command lines inside the container too (pipe it
through stdin; `printf` is a shell builtin, so it reaches no argv).

## The PostgreSQL module

`postgresql` installs the server, sets a role's password (creating the role if
needed), optionally creates a database it owns, and controls where it accepts
TCP connections. Verified on Debian 12, Alpine 3.21, Rocky Linux 9 and Arch.

```bash
./lemondx create db -i images:debian/12 -b postgresql \
    --param POSTGRES_USER=app --param POSTGRES_DB=appdb --param 'LISTEN_ADDRESSES=*'
psql -h <container-ip> -U app -d appdb
```

It prepends a managed block to `pg_hba.conf` — first match wins there — so
the password is actually required on every distro: peer authentication on the
local socket, `scram-sha-256` for every TCP connection. Distro defaults cannot
be relied on for this: Alpine's initdb writes `trust`, which lets any password
(or none) log in over localhost, and Fedora/Rocky write `ident`, which makes
password logins fail. The block is regenerated on each run, so setting
`LISTEN_ADDRESSES` back to `localhost` removes the remote rules. Rerunning the
module is also how you change the password.

## The Apache module

`apache2` installs Apache httpd, starts it, and, when `VHOST_CONFIG` is not
blank, installs that text as a virtual host. Verified on Debian 12, Alpine
3.21, Rocky Linux 9 and Arch.

```bash
cat > site.conf <<'CONF'
<VirtualHost *:80>
    ServerName demo.test
    DocumentRoot "/var/www/demo"
    <Directory "/var/www/demo">
        Require all granted
    </Directory>
</VirtualHost>
CONF
./lemondx create web -i images:debian/12 -b apache2 \
    --param "VHOST_CONFIG=$(cat site.conf)" --param VHOST_NAME=demo --param ENABLE_MODULES=rewrite
curl -H 'Host: demo.test' http://<container-ip>/
```

The config is written verbatim as `VHOST_NAME.conf` in the distro's own
drop-in directory (`sites-available` plus `a2ensite` on Debian/Ubuntu,
`conf.d` on Fedora/RHEL and Alpine, `vhosts.d` on openSUSE). Arch's
`httpd.conf` includes no such directory, so the module adds an
`IncludeOptional` for `/etc/httpd/conf/lemondx.d`. A `DocumentRoot` that does
not exist yet is created with a placeholder `index.html`.

The new config must pass Apache's config test before anything reloads. If it
does not, the previous file is put back (or the new one removed), the test's
error is shown, and the run fails, so a typo never takes down a server that
was working. Rerunning with a changed config replaces the file and reloads;
blanking `VHOST_CONFIG` leaves an installed vhost alone.

`ENABLE_MODULES` uses `a2enmod` where it exists (Debian, openSUSE) and
otherwise uncomments the `LoadModule` line, installing `mod_ssl`,
`apache2-ssl` or `apache2-proxy` first where those are separate packages.
On Debian/Ubuntu `000-default` is a catch-all on port 80 that sorts first and
would answer instead of a vhost without a matching `ServerName`, so it is
disabled when a vhost is installed; set `DISABLE_DEFAULT_SITE=no` to keep it.

Write the config for the container's distro: `${APACHE_LOG_DIR}` is defined
only on Debian/Ubuntu, and log directories are `/var/log/apache2` there and on
Alpine, `/var/log/httpd` elsewhere.

## If modules cannot install anything

Bootstrap checks outbound connectivity first and stops with an explanation
rather than letting a package manager hang. The usual culprit on a host that
also runs Docker is Docker setting the iptables `FORWARD` policy to `DROP`,
which blocks the container bridge:

```bash
sudo iptables -S FORWARD | head -1          # says: -P FORWARD DROP
sudo iptables -I DOCKER-USER -i lxdbr0 -j ACCEPT
sudo iptables -I DOCKER-USER -o lxdbr0 -j ACCEPT
```

These rules are not persistent; save them if you want them after a reboot.

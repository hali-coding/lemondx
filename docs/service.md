# Running as a service

[← back to README](../README.md)

```bash
./systemd/install-service.sh              # user service, runs as you (recommended)
sudo ./systemd/install-service.sh --system   # system service, runs as a dedicated account
./systemd/install-service.sh --uninstall     # remove
```

Prefer the user service unless the box has no interactive login: lemondx needs
your `lxd`/`incus-admin` group membership to reach the daemon, reads your
`~/.ssh/*.pub` for the SSH-key picker, and keeps its state under your
`~/.local/share/lemondx` — a system service running as a separate account
would need all three set up by hand for an account with no login shell to set
them up with. Both units bind to `127.0.0.1:8099` and run under
`ProtectSystem=strict`/`ProtectHome=read-only` with the state directory as the
one exception, since lemondx has no other reason to touch the filesystem.

```bash
systemctl --user status lemondx     # or plain systemctl for --system
journalctl --user -u lemondx -f
```

A user service only starts after you log in; for it to survive to boot with no
session open, `sudo loginctl enable-linger $USER` (the installer prints this
when it applies). The unit files are in `systemd/` if you want to edit the
host/port or turn on authentication before installing.

## Authentication

Run `lemondx configure auth` as the account the service runs as, then restart
it; the unit's `serve` picks the saved settings up with no change to the unit
file (see [Security](security.md)):

```bash
lemondx configure auth && systemctl --user restart lemondx
# --system: the account's data directory is under /var/lib/lemondx
sudo -u lemondx env HOME=/var/lib/lemondx /path/to/lemondx configure auth
sudo systemctl restart lemondx
```

Flags still work, and override the saved settings one by one, if you prefer
them in the unit: `systemctl --user edit lemondx`, then clear `ExecStart=` and
add your own with `--auth ...`.

Local users and API tokens live in the state directory the units already make
writable, so `--auth local` and `--auth token` need nothing else. Create them
with the CLI as the account the service runs as (for `--system`,
`sudo -u lemondx env HOME=/var/lib/lemondx ./lemondx user-add NAME`).

`--auth pam` needs more. Install the PAM service first
(`sudo install -m 644 systemd/lemondx.pam /etc/pam.d/lemondx`), then work
around `NoNewPrivileges=yes`, which both units set and which stops PAM's
`unix_chkpwd` helper from reading `/etc/shadow`:

- **User unit:** add `NoNewPrivileges=no` to the drop-in. PAM will then accept
  only your own account — the one the service runs as.
- **System unit:** add `SupplementaryGroups=shadow` instead (Debian/Ubuntu,
  where `/etc/shadow` is group-readable), which lets `pam_unix` read it
  directly and log in any member of `--pam-admin-group`. On distributions whose
  `/etc/shadow` is root-only, prefer `--auth local` or `--auth proxy`.

`serve` prints a warning at startup when PAM cannot work as configured, so
check `journalctl` after the change.

The fabric (routed networking between nodes) is blocked by `NoNewPrivileges=yes`
for the same reason: it raises privilege through `sudo` for one command. Set it
to `no` for the unit, or program the routes yourself with `lemondx fabric apply`
-- the result is identical. See [networking.md](networking.md).

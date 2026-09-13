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
host/port or add `--token` by hand before installing.

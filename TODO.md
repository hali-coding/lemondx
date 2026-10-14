# TODO — firedrill 2026-10-11

Findings from the stack/swarm firedrill on the live 6-node cluster (stack
`drill`, swarms `heavy`/`smoke`), and what became of them. Everything marked
fixed was verified live on the cluster afterwards.

## Fixed

- [x] Two nodes could run one swarm at once, and one's teardown destroyed the
  other's generators. Starts are now refused while any member runs the stack
  (`_busy()` / `peer_runs()`); each swarm run tags its instances
  (`user.waterpump.swarm-run`) and tears down only its own; Stop forwards to the
  member running it.
- [x] A swarm against a stopped stack wasn't refused: `_live_context()` counts
  running instances only, and refuses a referenced address that is missing.
- [x] Stop waited for the in-flight launch's bootstrap (47 s); now it cuts in
  within a second, and teardown sweeps until the launch has finished creating.
- [x] A swarm's results went nowhere: a successful module keeps its last lines,
  each instance's last line rides on the step (`output`), shown as **results**
  in the UI and under the step in the CLI.
- [x] One failed generator failed the whole swarm: launch steps take
  `min_percent` (**Must start** in the designer); failures within it are noted.
- [x] Leftover swarm instances were invisible in the CLI: `waterpump stacks`
  shows `heavy (6 left)` plus the command to clear them; `stack-show` notes them.
- [x] `serve` stopped by systemd mid-swarm left instances behind: SIGTERM now
  stops swarms and waits up to 60 s for their teardown. The CLI follower says
  what to do when serve goes away.
- [x] `stack-swarm-stop` through serve returned at "destroying"; it follows the
  run to the end, and checks runs on every member first.
- [x] A failed launch step said only "module failed": it now carries the module,
  exit code and last stderr line.
- [x] `stack-launch` didn't print a script step's output, or the launch notes.
- [x] `top` showed "launch 3 on orange" for a launch that went elsewhere.
- [x] Progress listed every group member, not where instances went.
- [x] `stack-stop`/`-start`/`-restart` rejected `-y`.
- [x] `stack-save` didn't mention the swarms saved.
- [x] Preview hid module secrets filled by a same-named stack secret
  (`stack-show` too), and previewed settled `{{db.param.X}}` as unknown.
- [x] `fabric apply` was refused in maintenance; it now restores the node's own
  claims (orange has 5/5 routes again). Automatic re-application still waits.
- [x] Found while fixing: `_live_context()` read `stack_step` from rows that
  call it `step` (it only worked through the template fallback), and
  `_await_template_run()` shadowed `run`.

## Hosts

- [x] **prdev5** works: all four `small` nodes ran a generator in the last
  swarm. There were three problems, one after another:
  - **No idmap:** `/etc/subuid` and `/etc/subgid` had no `root` range for Incus.
    Added `root:1000000:1000000000` (backups are `*.bak-20261011`), restarted
    Incus, and ended the auto-maintenance.
  - **No DHCP or DNS for containers:** firewalld put the unzoned `lxdbr0` and
    the fabric bridge in `FedoraServer`, which only admits ssh, cockpit and 8099.
    firewalld is now **disabled** on prdev5 (by hand). If it comes back, give
    both bridges a zone that allows DHCP, DNS and forwarding first.
  - **No fabric routes:** restarting Incus re-created the fabric bridge, and the
    routes that use its address as `src` went with it (0/5).
    `waterpump fabric apply` on prdev5 restored them.
- [ ] **prdev3** gained an empty LXD snap during the drill: Ubuntu's
  `lxd-installer` installed it when `lxc list` was run there. waterpump prefers an
  LXD socket over Incus, so its serve is now started with `WATERPUMP_FLAVOR=incus`.
  A plain `restart-all.sh` will flip it back to the empty LXD until the snap is
  removed: `sudo snap remove --purge lxd` (it holds no instances, storage,
  networks or images).
- [ ] **orange** is still in the maintenance set by hand (no reason given).

## Ideas, not done

- waterpump picks the first daemon socket that exists. A host with both (prdev1,
  and now prdev3) silently switches daemon when one appears. Worth a saved
  per-node daemon choice, or at least a warning in `status` when both exist.
- `serve` could check the idmap at startup rather than learning from five
  failed launches.
- **firewalld hosts:** waterpump neither documents nor handles firewalld. On
  Fedora, its bridges fall into the default zone and containers get no DHCP or
  DNS. `docs/networking.md` should say which zone settings a bridge needs.
  The fabric helper could put its own bridges (and say which other bridge to
  add) in a zone, the way it already keeps `DOCKER-USER` rules for Docker
  hosts. `status` could warn when firewalld is active and a waterpump bridge has
  no zone.
- **Fabric routes after a daemon restart:** when Incus or LXD restarts and
  re-creates a fabric bridge, the routes stay missing until the periodic
  re-apply or a manual `fabric apply`. prdev5 still showed 0/5 minutes later.
  `serve` could re-apply as soon as it sees a fabric bridge lose its address,
  or as soon as `fabric status` finds routes missing.
- **"no outbound network" message:** it only suggests Docker's FORWARD policy.
  On prdev5 the cause was firewalld blocking DHCP, so the instance had no
  address at all. The probe could say "no IPv4 address" in that case and
  mention firewalld zones.

#!/usr/bin/env bash
# Install lemondx as a systemd service.
#
#   ./systemd/install-service.sh              # user service, runs as you (recommended)
#   ./systemd/install-service.sh --system     # system service, dedicated account
#   ./systemd/install-service.sh --uninstall  # remove whichever was installed
#   ./systemd/install-service.sh --fabric     # also let lemondx program host routes
#
# Safe to re-run: re-installing just overwrites the unit file and reloads.
set -euo pipefail

mode=user
action=install
fabric=no
for arg in "$@"; do
    case "$arg" in
        --system) mode=system ;;
        --user) mode=user ;;
        --uninstall) action=uninstall ;;
        --fabric) fabric=yes ;;
        -h|--help)
            sed -n '2,9p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

# The repo root is this script's parent directory, wherever the checkout
# lives -- so the unit works whether it is a snap-installed clone, a
# ~/dev checkout, or anything else.
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(dirname "$script_dir")
python_bin=$(command -v python3 || true)
[ -n "$python_bin" ] || { echo "python3 not found on PATH." >&2; exit 1; }

if [ ! -x "$repo_dir/lemondx" ]; then
    echo "Expected $repo_dir/lemondx (the launcher) -- run this from a lemondx checkout." >&2
    exit 1
fi

# --- which daemon is on this host? (lemondx's own lxd.py SOCKET_CANDIDATES) --

detect_group() {
    for path in /var/snap/lxd/common/lxd/unix.socket /var/lib/lxd/unix.socket; do
        [ -S "$path" ] && { echo lxd; return; }
    done
    for path in /var/lib/incus/unix.socket /run/incus/unix.socket; do
        [ -S "$path" ] && { echo incus-admin; return; }
    done
    echo ""
}


# --- is the port already taken? --------------------------------------------

port_in_use() {
    python3 -c '
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sys.exit(0 if s.connect_ex(("127.0.0.1", 8099)) == 0 else 1)
' 2>/dev/null
}

# --- user mode ----------------------------------------------------------

install_user() {
    local unit_dir="$HOME/.config/systemd/user"
    local unit_path="$unit_dir/lemondx.service"

    if ! id -nG "$USER" | tr ' ' '\n' | grep -qE '^(lxd|incus-admin)$'; then
        echo "Warning: $USER is not in the 'lxd' or 'incus-admin' group yet." >&2
        echo "  lemondx will start but every LXD/Incus call will fail until you" >&2
        echo "  run: sudo usermod -aG lxd \"\$USER\"   (or incus-admin)" >&2
        echo "  and log out and back in (or: newgrp lxd)." >&2
    fi

    mkdir -p "$unit_dir"
    sed "s|@LEMONDX_PATH@|$repo_dir|g" "$script_dir/lemondx.service" > "$unit_path"
    echo "Installed $unit_path"

    systemctl --user daemon-reload

    if port_in_use; then
        systemctl --user enable lemondx.service
        echo
        echo "Port 8099 is already in use (probably a manually-started lemondx)."
        echo "Enabled for next login/boot, but not started now. Stop the other"
        echo "one and run: systemctl --user start lemondx"
        return
    fi

    systemctl --user enable --now lemondx.service
    echo
    echo "lemondx is running: http://127.0.0.1:8099"
    echo "Logs:   journalctl --user -u lemondx -f"
    echo "Status: systemctl --user status lemondx"

    if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
        echo
        echo "Note: this only starts after you log in. For it to survive to boot"
        echo "with no session open, run:  sudo loginctl enable-linger $USER"
    fi
}

uninstall_user() {
    systemctl --user disable --now lemondx.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/lemondx.service"
    systemctl --user daemon-reload
    echo "Removed the user service. Its data under ~/.local/share/lemondx is untouched."
}

# --- system mode ----------------------------------------------------------

install_system() {
    [ "$(id -u)" -eq 0 ] || { echo "--system needs root: sudo $0 --system" >&2; exit 1; }

    local group unit_path=/etc/systemd/system/lemondx.service
    group=$(detect_group)
    if [ -z "$group" ]; then
        echo "No LXD or Incus socket found -- install and start one first." >&2
        exit 1
    fi

    local home=/var/lib/lemondx
    if ! id lemondx >/dev/null 2>&1; then
        useradd --system --home-dir "$home" --create-home --shell /usr/sbin/nologin \
            --gid "$group" lemondx
        echo "Created system account 'lemondx' (group: $group, home: $home)."
    else
        usermod -aG "$group" lemondx
    fi

    sed -e "s|@LEMONDX_PATH@|$repo_dir|g" -e "s|@LEMONDX_GROUP@|$group|g" \
        "$script_dir/lemondx-system.service" > "$unit_path"
    echo "Installed $unit_path"

    systemctl daemon-reload

    if port_in_use; then
        systemctl enable lemondx.service
        echo
        echo "Port 8099 is already in use. Enabled for next boot, but not"
        echo "started now. Free the port and run: systemctl start lemondx"
        return
    fi

    systemctl enable --now lemondx.service
    echo
    echo "lemondx is running: http://127.0.0.1:8099"
    echo "Logs:   journalctl -u lemondx -f"
    echo "Status: systemctl status lemondx"
    echo
    echo "The 'lemondx' account has no SSH keys of its own -- the bootstrap"
    echo "module picker's ~/.ssh list will be empty. Paste keys into the UI,"
    echo "or drop .pub files in $home/.ssh/ owned by lemondx:lemondx."
}

uninstall_system() {
    [ "$(id -u)" -eq 0 ] || { echo "--uninstall --system needs root." >&2; exit 1; }
    systemctl disable --now lemondx.service 2>/dev/null || true
    rm -f /etc/systemd/system/lemondx.service
    systemctl daemon-reload
    echo "Removed the system service and unit file."
    echo "Left in place: the 'lemondx' account and /var/lib/lemondx (its state)."
    echo "To remove those too: userdel -r lemondx"
}

# --- the fabric: let lemondx program host routes between cluster nodes -------

install_fabric() {
    [ "$(id -u)" -eq 0 ] || { echo "--fabric needs root: sudo $0 --fabric" >&2; exit 1; }
    local group template target tmp
    group=$(detect_group)
    [ -n "$group" ] || { echo "No LXD or Incus socket found, so there is no admin group to grant this to." >&2; exit 1; }
    template="$script_dir/lemondx-fabric.sudoers"
    target=/etc/sudoers.d/lemondx-fabric
    [ -f "$template" ] || { echo "Missing $template" >&2; exit 1; }

    tmp=$(mktemp)
    # `visudo -c` before it is in place: a sudoers file that does not parse
    # takes sudo out on the whole host, and this script is run with it.
    sed -e "s|@HELPER@|$repo_dir/lemondx|g" -e "s|@GROUP@|$group|g" "$template" > "$tmp"
    if ! visudo -c -f "$tmp" >/dev/null; then
        echo "Refusing to install: the generated sudoers file does not parse." >&2
        visudo -c -f "$tmp" >&2 || true
        rm -f "$tmp"
        exit 1
    fi
    install -m 0440 -o root -g root "$tmp" "$target"
    rm -f "$tmp"

    echo "Installed $target"
    echo "  members of '$group' may now run: $repo_dir/lemondx fabric-helper"
    echo "  and nothing else. That group already grants root on this host via"
    echo "  the daemon, so this adds no access it did not have."
    echo
    echo "Next: lemondx fabric enable   (on one node; it allocates for the cluster)"
    if [ "$mode" = user ] && [ -f "$HOME/.config/systemd/user/lemondx.service" ]; then
        echo
        echo "Note: the unit sets NoNewPrivileges=yes, which stops sudo from raising"
        echo "privilege. Set NoNewPrivileges=no for lemondx.service if you want"
        echo "\`serve\` to program routes itself -- see docs/service.md."
    fi
}

uninstall_fabric() {
    [ "$(id -u)" -eq 0 ] || { echo "--fabric --uninstall needs root." >&2; exit 1; }
    rm -f /etc/sudoers.d/lemondx-fabric
    echo "Removed /etc/sudoers.d/lemondx-fabric; lemondx can no longer program routes."
}

if [ "$fabric" = yes ]; then
    case "$action" in
        install)   install_fabric ;;
        uninstall) uninstall_fabric ;;
    esac
    exit 0
fi

case "$mode-$action" in
    user-install)    install_user ;;
    user-uninstall)  uninstall_user ;;
    system-install)  install_system ;;
    system-uninstall) uninstall_system ;;
esac

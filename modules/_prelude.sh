# lemondx bootstrap prelude -- prepended to every module before it runs.
#
# Modules get a small, distro-neutral toolkit so they can stay short:
#   log / warn / die        progress output
#   have CMD                is a command available
#   pkg_refresh             refresh the package index (once per run)
#   pkg_install PKG...      install packages non-interactively
#   svc_enable NAME         enable + start a service (systemd or OpenRC)
#   install_ssh_keys USER   write $LEMONDX_SSH_KEYS to that user's authorized_keys
#
# Available variables: LEMONDX_OS_ID, LEMONDX_OS_LIKE, LEMONDX_PKG,
# LEMONDX_SSH_KEYS, LEMONDX_MODULE, plus any parameters the module declares.

# Modules run under /bin/sh, which is dash on Debian/Ubuntu and busybox ash on
# Alpine -- not necessarily bash, which minimal images often lack entirely.
# Keep modules POSIX-compatible. pipefail is not POSIX, so enable it only where
# the shell actually supports it.
set -eu
if (set -o pipefail) 2>/dev/null; then set -o pipefail; fi

log()  { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# --- distro detection ------------------------------------------------------

LEMONDX_OS_ID="unknown"
LEMONDX_OS_LIKE=""
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    LEMONDX_OS_ID="${ID:-unknown}"
    LEMONDX_OS_LIKE="${ID_LIKE:-}"
fi

if   have apt-get; then LEMONDX_PKG=apt
elif have dnf;     then LEMONDX_PKG=dnf
elif have microdnf;then LEMONDX_PKG=microdnf
elif have yum;     then LEMONDX_PKG=yum
elif have apk;     then LEMONDX_PKG=apk
elif have pacman;  then LEMONDX_PKG=pacman
elif have zypper;  then LEMONDX_PKG=zypper
else LEMONDX_PKG=none
fi

export DEBIAN_FRONTEND=noninteractive

# --- packages --------------------------------------------------------------

# Refreshing is slow, so only do it once per bootstrap run. The marker lives
# in the container, and each run clears it before the first module.
_LEMONDX_REFRESH_MARKER=/tmp/.lemondx-pkg-refreshed

pkg_refresh() {
    [ -f "$_LEMONDX_REFRESH_MARKER" ] && return 0
    log "refreshing package index ($LEMONDX_PKG)"
    case "$LEMONDX_PKG" in
        apt)      apt-get update -qq ;;
        dnf|yum)  "$LEMONDX_PKG" -q makecache ;;
        microdnf) microdnf -q makecache ;;
        apk)      apk update -q ;;
        pacman)   pacman -Sy --noconfirm >/dev/null ;;
        zypper)   zypper --non-interactive --quiet refresh ;;
        none)     die "no supported package manager found" ;;
    esac
    : > "$_LEMONDX_REFRESH_MARKER"
}

pkg_install() {
    [ "$#" -gt 0 ] || return 0
    pkg_refresh
    log "installing: $*"
    case "$LEMONDX_PKG" in
        apt)      apt-get install -y -qq --no-install-recommends "$@" ;;
        dnf|yum)  "$LEMONDX_PKG" install -y -q "$@" ;;
        microdnf) microdnf install -y "$@" ;;
        apk)      apk add --no-cache -q "$@" ;;
        pacman)   pacman -S --noconfirm --needed "$@" >/dev/null ;;
        zypper)   zypper --non-interactive --quiet install "$@" ;;
        none)     die "no supported package manager found" ;;
    esac
}

# --- services --------------------------------------------------------------

svc_enable() {
    local name="$1"
    if have systemctl && [ -d /run/systemd/system ]; then
        systemctl enable --now "$name"
    elif have rc-update; then
        rc-update add "$name" default >/dev/null 2>&1 || true
        rc-service "$name" restart >/dev/null 2>&1 || rc-service "$name" start
    else
        warn "no init system found; start '$name' yourself"
    fi
}

# --- ssh -------------------------------------------------------------------

# Write the keys supplied to this bootstrap run into a user's authorized_keys,
# creating ~/.ssh with the right ownership and permissions. Existing keys are
# preserved and duplicates are skipped.
install_ssh_keys() {
    local user="${1:-root}"
    [ -n "${LEMONDX_SSH_KEYS:-}" ] || { warn "no SSH keys supplied"; return 0; }

    local home
    home="$(getent passwd "$user" | cut -d: -f6)"
    [ -n "$home" ] || die "user '$user' does not exist"

    mkdir -p "$home/.ssh"
    local file="$home/.ssh/authorized_keys"
    touch "$file"

    local added=0
    while IFS= read -r key; do
        [ -n "$key" ] || continue
        case "$key" in \#*) continue ;; esac
        if grep -qxF "$key" "$file" 2>/dev/null; then continue; fi
        printf '%s\n' "$key" >> "$file"
        added=$((added + 1))
    done <<EOF
$LEMONDX_SSH_KEYS
EOF

    chown -R "$user:$(id -gn "$user" 2>/dev/null || echo "$user")" "$home/.ssh" 2>/dev/null || true
    chmod 700 "$home/.ssh"
    chmod 600 "$file"
    log "added $added key(s) to $file"
}

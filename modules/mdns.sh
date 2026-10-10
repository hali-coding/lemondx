#!/bin/sh
# name: mDNS (Avahi)
# description: Announce the instance as <name>.local with Avahi, optionally advertise its services, and let it resolve other .local names.
# order: 70
# param: MDNS_HOSTNAME=  Name to announce, without .local (blank uses the instance's name)
# param: MDNS_SERVICES=  Services to advertise as name:port, space-separated (e.g. http:80 ssh:22 postgresql:5432; add /udp for UDP)
# param: MDNS_RESOLVE=yes  Resolve other .local names from inside the instance too (yes or no)

name="${MDNS_HOSTNAME:-${LEMONDX_INSTANCE:-}}"
[ -n "$name" ] || name="$(hostname)"
services="$(printf '%s' "${MDNS_SERVICES:-}" | tr ',' ' ')"
resolve="${MDNS_RESOLVE:-yes}"

# One DNS label: Avahi refuses anything else, and the name goes into its
# config file unquoted. Instance names on the daemon already fit.
case "$name" in
    ''|-*|*-|*[!A-Za-z0-9-]*) die "MDNS_HOSTNAME must be letters, digits and '-', not starting or ending with '-' (got '$name')" ;;
esac
[ "${#name}" -le 63 ] || die "MDNS_HOSTNAME is longer than the 63 characters a DNS label may have"
# Checked whole before the unquoted loop below, so no glob can get in.
case "$services" in
    *[!a-z0-9:/\ -]*) die "MDNS_SERVICES takes entries like http:80 or ssh:22, separated by spaces (got '$services')" ;;
esac
case "$resolve" in
    yes|no) ;;
    *) die "MDNS_RESOLVE must be yes or no (got '$resolve')" ;;
esac

# Each entry is a DNS-SD service type (RFC 6335: at most 15 characters) and a
# port; checked before anything is installed, so a typo costs nothing.
for entry in $services; do
    case "$entry" in *:*) ;; *) die "'$entry' has no port; write it as $entry:<port>" ;; esac
    type="${entry%%:*}"
    rest="${entry#*:}"
    port="${rest%/*}"
    case "$rest" in
        */*/*) die "'$entry': the protocol after / must be tcp or udp" ;;
        */udp|*/tcp) ;;
        */*) die "'$entry': the protocol after / must be tcp or udp" ;;
    esac
    case "$type" in
        ''|-*|*-|*[!a-z0-9-]*) die "'$entry': the service type must be like http or ssh" ;;
    esac
    [ "${#type}" -le 15 ] || die "'$entry': a service type has at most 15 characters"
    case "$port" in
        ''|*[!0-9]*) die "'$entry': the port must be a number" ;;
    esac
    [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || die "'$entry': the port must be 1-65535"
done

# --- install ---------------------------------------------------------------

# Avahi talks to its clients over the system bus and will not start without
# one, which minimal images do not always run. Alpine has no nss-mdns: musl
# has no Name Service Switch at all (its "nss" package is Mozilla's crypto
# library, unrelated), so see the resolving section below.
nss=""
case "$LEMONDX_PKG" in
    apt)              pkg_install avahi-daemon dbus; nss=libnss-mdns ;;
    dnf|yum|microdnf) pkg_install avahi dbus dbus-tools; nss=nss-mdns ;;
    apk)              pkg_install avahi dbus ;;
    pacman)           pkg_install avahi dbus; nss=nss-mdns ;;
    zypper)           pkg_install avahi dbus-1; nss=nss-mdns ;;
    *)                die "no Avahi recipe for package manager '$LEMONDX_PKG'" ;;
esac

conf=/etc/avahi/avahi-daemon.conf
[ -f "$conf" ] || die "Avahi installed no $conf"

# Set one key in one [section] of an ini file, adding either if missing and
# replacing the key whether it is set or commented out.
ini_set() {
    file="$1" section="$2" key="$3" value="$4"
    tmp="$(mktemp)"
    awk -v s="[$section]" -v k="$key" -v v="$value" '
        function emit() { if (insec && !done) { print k "=" v; done = 1 } }
        /^\[/ { emit(); insec = ($0 == s); seen = seen || insec }
        insec && $0 ~ "^[#;]?[[:space:]]*" k "[[:space:]]*=" { if (!done) { print k "=" v; done = 1 } next }
        { print }
        END { emit(); if (!seen) { print ""; print s; print k "=" v } }
    ' "$file" > "$tmp"
    cat "$tmp" > "$file"
    rm -f "$tmp"
}

ini_set "$conf" server host-name "$name"
# Avahi limits its own user to three processes. Under the daemon's shared ID
# map that user is the same host uid in every container, and kernels before
# 5.14 counted processes per host uid rather than per user namespace -- so on
# such a host, with Avahi in a couple of instances, the next one cannot fork
# and fails to start. Lifted everywhere: Avahi runs two processes regardless.
sed -i 's/^[[:space:]]*rlimit-nproc[[:space:]]*=.*/#&/' "$conf"
log "announcing as $name.local"

# --- services --------------------------------------------------------------

svcfile=/etc/avahi/services/lemondx.service
if [ -n "$services" ]; then
    mkdir -p /etc/avahi/services
    {
        printf '<?xml version="1.0" standalone="no"?>\n'
        printf '<!DOCTYPE service-group SYSTEM "avahi-service.dtd">\n'
        printf '<!-- Written by lemondx (mdns); re-running the module replaces it. -->\n'
        printf '<service-group>\n  <name replace-wildcards="yes">%%h</name>\n'
        for entry in $services; do
            type="${entry%%:*}"
            rest="${entry#*:}"
            proto=tcp
            case "$rest" in */udp) proto=udp ;; esac
            printf '  <service>\n    <type>_%s._%s</type>\n    <port>%s</port>\n  </service>\n' \
                "$type" "$proto" "${rest%/*}"
        done
        printf '</service-group>\n'
    } > "$svcfile"
    log "advertising: $services"
elif [ -f "$svcfile" ]; then
    # Blank now means none, not whatever an earlier run left behind.
    rm -f "$svcfile"
    log "removed the services an earlier run advertised"
fi

# --- resolving .local --------------------------------------------------------

systemd_running() { have systemctl && [ -d /run/systemd/system ]; }

# systemd-resolved can answer mDNS itself and then holds port 5353; two
# responders on one host confuse every client asking. Avahi is the one this
# module configures, so resolved stands aside.
if systemd_running && systemctl is-active --quiet systemd-resolved 2>/dev/null; then
    mkdir -p /etc/systemd/resolved.conf.d
    printf '# Written by lemondx (mdns): Avahi answers mDNS here.\n[Resolve]\nMulticastDNS=no\n' \
        > /etc/systemd/resolved.conf.d/lemondx-mdns.conf
    systemctl restart systemd-resolved
fi

if [ "$resolve" = yes ] && [ -n "$nss" ]; then
    pkg_install "$nss"
    # Debian's and Fedora's packages add themselves to nsswitch.conf; the
    # others leave it to the admin. mdns4_minimal only asks about .local, and
    # [NOTFOUND=return] keeps a missing .local name from going to DNS.
    if [ -f /etc/nsswitch.conf ] && ! grep -Eq '^hosts:.*mdns' /etc/nsswitch.conf; then
        sed -i -E '/^hosts:/{
            /[[:space:]](resolve|dns)([[:space:]]|$)/!s/$/ mdns4_minimal [NOTFOUND=return]/
            s/[[:space:]](resolve|dns)([[:space:]]|$)/ mdns4_minimal [NOTFOUND=return]&/
        }' /etc/nsswitch.conf
        log "added mdns4_minimal to the hosts line of /etc/nsswitch.conf"
    fi
elif [ "$resolve" = yes ]; then
    # musl resolves from /etc/hosts and the servers in resolv.conf only, and
    # nothing on Alpine adds mDNS to that (gcompat loads no NSS modules), so
    # programs cannot look up .local names. avahi-resolve asks Avahi itself,
    # which is what a script or health check here can use instead.
    [ "$LEMONDX_PKG" != apk ] || pkg_install avahi-tools
    warn "this instance is announced, but its programs cannot resolve other .local names (musl has no mDNS support); scripts can use: avahi-resolve -n4 <name>.local"
fi

# --- start -----------------------------------------------------------------

if systemd_running; then
    svc_enable dbus 2>/dev/null || true
    svc_enable avahi-daemon
    # enable --now leaves a running daemon on its old name and services.
    systemctl restart avahi-daemon
else
    # Only started, never restarted: under OpenRC restarting dbus restarts
    # everything that needs it, Avahi included, and Avahi's own restart below
    # then finds it half-started and fails -- which made every re-run fail.
    if have rc-update; then
        rc-update add dbus default >/dev/null 2>&1 || true
        rc-service dbus status >/dev/null 2>&1 || rc-service dbus start
    fi
    svc_enable avahi-daemon
fi

# A running daemon has not necessarily won its name: it first probes the
# network for anyone already answering to it, and on a conflict takes name-2.
# So ask Avahi itself, over the bus, once it says it has settled (state 2,
# AVAHI_SERVER_RUNNING) -- the service manager reports it running before that.
avahi() {
    dbus-send --system --print-reply=literal --dest=org.freedesktop.Avahi / \
        "org.freedesktop.Avahi.Server.$1" 2>/dev/null | tr -d ' \n' | sed 's/^int32//'
}
announced=""
if have dbus-send; then
    i=0
    while [ "$i" -lt 20 ]; do
        state="$(avahi GetState || true)"
        if [ "$state" = 2 ]; then
            announced="$(avahi GetHostNameFqdn || true)"
            break
        fi
        [ "$state" != 4 ] || break
        i=$((i + 1))
        sleep 1
    done
    [ -n "$announced" ] || die "avahi-daemon did not start announcing (state '${state:-none}'); see its log (journalctl -u avahi-daemon, or /var/log/messages)"
else
    sleep 2
    avahi-daemon --check 2>/dev/null || die "avahi-daemon is not running; see its log"
    announced="$name.local"
    warn "dbus-send is missing, so whether another host already holds $name.local was not checked"
fi

if [ "$announced" != "$name.local" ]; then
    warn "another host on this network already answers for $name.local, so this instance is $announced"
fi
log "announced as $announced; from the host or another instance on this network: ping $announced"

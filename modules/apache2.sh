#!/bin/sh
# name: Apache httpd
# description: Install the Apache web server, start it, and optionally install a virtual host you supply.
# order: 55
# text: VHOST_CONFIG=  Virtual host configuration, e.g. a <VirtualHost *:80> block; blank installs Apache only
# param: VHOST_NAME=lemondx  File name for the virtual host, without .conf
# param: ENABLE_MODULES=  Extra Apache modules to enable, space-separated (e.g. rewrite headers proxy proxy_http)
# param: DISABLE_DEFAULT_SITE=yes  On Debian/Ubuntu, disable 000-default when a vhost is given (yes or no)

vhost_name="${VHOST_NAME:-lemondx}"
modules="$(printf '%s' "${ENABLE_MODULES:-}" | tr ',' ' ')"
# A config pasted from elsewhere may carry CRLF line endings, which Apache
# reads as part of each directive's last argument.
vhost="$(printf '%s' "${VHOST_CONFIG:-}" | tr -d '\r')"

# The name becomes a path, and on Debian an a2ensite argument, so hold it to a
# shape that needs neither quoting nor a check for "..".
case "$vhost_name" in
    ''|.*|*[!A-Za-z0-9._-]*) die "VHOST_NAME may only use letters, digits, '.', '_' and '-', and may not start with '.'" ;;
esac
# Checked as a whole before any unquoted expansion, so no glob can get in.
case "$modules" in
    *[!a-z0-9_\ ]*) die "ENABLE_MODULES takes module names like rewrite or proxy_http, separated by spaces" ;;
esac

# --- install ---------------------------------------------------------------

# Every family names the package, the service and the drop-in directory
# differently. Arch's httpd.conf includes no directory of its own, so the
# module adds one (below).
case "$LEMONDX_PKG" in
    apt)              pkg_install apache2;  svc=apache2; confdir=/etc/apache2/sites-available ;;
    dnf|yum|microdnf) pkg_install httpd;    svc=httpd;   confdir=/etc/httpd/conf.d ;;
    apk)              pkg_install apache2;  svc=apache2; confdir=/etc/apache2/conf.d ;;
    pacman)           pkg_install apache;   svc=httpd;   confdir=/etc/httpd/conf/lemondx.d ;;
    zypper)           pkg_install apache2;  svc=apache2; confdir=/etc/apache2/vhosts.d ;;
    *)                die "no Apache recipe for package manager '$LEMONDX_PKG'" ;;
esac

mkdir -p "$confdir"

if [ "$LEMONDX_PKG" = pacman ]; then
    main=/etc/httpd/conf/httpd.conf
    include="IncludeOptional $confdir/*.conf"
    if ! grep -qxF "$include" "$main"; then
        printf '\n# Added by lemondx: virtual hosts from the apache2 module.\n%s\n' "$include" >> "$main"
    fi
fi

# openSUSE's apache2ctl regenerates the module list from sysconfig first, and
# Debian's reads envvars for ${APACHE_LOG_DIR} and friends; plain httpd -t
# would see neither.
config_test() {
    if have apache2ctl; then apache2ctl -t 2>&1; else httpd -t 2>&1; fi
}

# --- modules ---------------------------------------------------------------

# Some modules are separate packages outside Debian and SUSE.
for m in $modules; do
    case "$LEMONDX_PKG:$m" in
        apk:ssl)              pkg_install apache2-ssl ;;
        apk:proxy*)           pkg_install apache2-proxy ;;
        dnf:ssl|yum:ssl|microdnf:ssl) pkg_install mod_ssl ;;
    esac
done

for m in $modules; do
    if have a2enmod; then
        # Debian's a2enmod symlinks mods-enabled; SUSE's edits sysconfig.
        a2enmod "$m" >/dev/null
        log "enabled module $m"
        continue
    fi
    # Elsewhere modules are LoadModule lines, mostly present and some commented
    # out, spread over httpd.conf and (on Fedora/RHEL) conf.modules.d.
    file=$(grep -lE "^[[:space:]]*#[[:space:]]*LoadModule[[:space:]]+${m}_module[[:space:]]" \
        /etc/httpd/conf/httpd.conf /etc/httpd/conf.modules.d/*.conf \
        /etc/apache2/httpd.conf /etc/apache2/conf.d/*.conf 2>/dev/null | head -n 1 || true)
    if [ -n "$file" ]; then
        sed -i "s|^[[:space:]]*#[[:space:]]*\(LoadModule[[:space:]][[:space:]]*${m}_module[[:space:]]\)|\1|" "$file"
        log "enabled module $m in $file"
    elif httpd -M 2>/dev/null | grep -q "[[:space:]]${m}_module[[:space:]]"; then
        log "module $m is already loaded"
    else
        warn "module $m was not found; it may need a separate package"
    fi
done

# --- virtual host ----------------------------------------------------------

if [ -n "$vhost" ]; then
    target="$confdir/$vhost_name.conf"
    backup=""
    if [ -f "$target" ]; then
        backup="$target.lemondx-previous"
        cp -p "$target" "$backup"
    fi

    printf '%s\n' "$vhost" > "$target"
    if [ "$LEMONDX_PKG" = apt ]; then
        a2ensite "$vhost_name" >/dev/null
    fi

    # A missing DocumentRoot is only a warning to the config test, but the
    # site then answers 404 or 403 for everything. Create the directory, with
    # a page to show the vhost is the one answering.
    printf '%s\n' "$vhost" | awk 'tolower($1) == "documentroot" { print $2 }' | tr -d '"' |
    while IFS= read -r root; do
        case "$root" in /*) ;; *) continue ;; esac
        [ -e "$root" ] && continue
        mkdir -p "$root"
        printf '<!doctype html>\n<title>%s</title>\n<p>%s is served by Apache, installed by lemondx.</p>\n' \
            "$vhost_name" "$vhost_name" > "$root/index.html"
        log "created $root"
    done

    # Test before anything reloads, and put the previous config back if the
    # new one does not parse, so a typo cannot take down a working server.
    if ! output=$(config_test); then
        if [ -n "$backup" ]; then
            mv "$backup" "$target"
        else
            [ "$LEMONDX_PKG" = apt ] && a2dissite "$vhost_name" >/dev/null 2>&1
            rm -f "$target"
        fi
        printf '%s\n' "$output" >&2
        die "the virtual host does not pass Apache's config test; the previous configuration was kept"
    fi
    rm -f "$backup"
    log "installed virtual host $target"

    # Debian's default site is a catch-all on *:80 that sorts first, so a vhost
    # without a matching ServerName would never be reached.
    if [ "$LEMONDX_PKG" = apt ] && [ "${DISABLE_DEFAULT_SITE:-yes}" = yes ] \
        && [ -e /etc/apache2/sites-enabled/000-default.conf ]; then
        a2dissite 000-default >/dev/null
        log "disabled the default site"
    fi
elif ! output=$(config_test); then
    printf '%s\n' "$output" >&2
    die "Apache's configuration does not pass its config test"
fi

# --- start -----------------------------------------------------------------

svc_enable "$svc"
# enable --now leaves an already running server on its old config.
if have systemctl && [ -d /run/systemd/system ]; then
    systemctl reload-or-restart "$svc"
fi

version=$(httpd -v 2>/dev/null || apache2 -v 2>/dev/null || apache2ctl -v 2>/dev/null || true)
version=$(printf '%s\n' "$version" | sed -n 's/^Server version: *//p')
log "${version:-Apache} running"

if [ -n "$vhost" ]; then
    server_name=$(printf '%s\n' "$vhost" | awk 'tolower($1) == "servername" { print $2; exit }')
    if [ -n "$server_name" ]; then
        log "try: curl -H 'Host: $server_name' http://<container-ip>/"
    fi
fi

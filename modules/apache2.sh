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

# Every <Directory> and <DirectoryMatch> in the files Apache loads, one per
# line as "path GLOB" or "regex RE", with "/" as an empty path. Fails when
# the files cannot be listed, so the caller grants nothing rather than guess.
directory_sections() {
    if have apache2ctl; then
        dump=$(apache2ctl -t -D DUMP_INCLUDES 2>/dev/null) || return 1
    else
        dump=$(httpd -t -D DUMP_INCLUDES 2>/dev/null) || return 1
    fi
    files=$(printf '%s\n' "$dump" | sed -n 's/^[[:space:]]*([^)]*)[[:space:]]*//p')
    [ -n "$files" ] || return 1
    printf '%s\n' "$files" |
    while IFS= read -r f; do cat "$f"; done |
    awk '{
        line = $0; sub(/^[ \t]+/, "", line); sub(/>[ \t]*$/, "", line)
        lower = tolower(line)
        if (lower ~ /^<directorymatch[ \t]/) kind = "regex"
        else if (lower ~ /^<directory[ \t]/) kind = "path"
        else next
        sub(/^<[^ \t]+[ \t]+/, "", line)
        if (kind == "path" && line ~ /^~[ \t]/) { kind = "regex"; sub(/^~[ \t]+/, "", line) }
        gsub(/^"|"$/, "", line)
        if (kind == "path") sub(/\/+$/, "", line)
        print kind, line
    }'
}

# Whether a section in $sections other than "/" applies to directory $1:
# one naming it or a directory above it.
covered() {
    while read -r kind pattern; do
        [ -n "$pattern" ] || continue
        dir=$1
        while [ -n "$dir" ]; do
            case "$kind" in
                # Unquoted on purpose: Apache's <Directory> takes wildcards.
                path) case "$dir" in $pattern) return 0 ;; esac ;;
                # Apache's expressions are PCRE; one grep cannot read, or may
                # read differently (groups with (?, escapes like \d), counts as
                # a match, since unsure has to mean hands off.
                regex)
                    case "$pattern" in *'(?'*|*\\[A-Za-z]*) return 0 ;; esac
                    grep -qE -e "$pattern" 2>/dev/null <<PATHS && return 0
$dir
$dir/
PATHS
                    [ $? -eq 2 ] && return 0 ;;
            esac
            dir=${dir%/*}
        done
    done <<EOF
$sections
EOF
    return 1
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
    roots=$(printf '%s\n' "$vhost" | awk 'tolower($1) == "documentroot" { print $2 }' | tr -d '"')
    printf '%s\n' "$roots" |
    while IFS= read -r root; do
        case "$root" in /*) ;; *) continue ;; esac
        [ -e "$root" ] && continue
        mkdir -p "$root"
        printf '<!doctype html>\n<title>%s</title>\n<p>%s is served by Apache, installed by lemondx.</p>\n' \
            "$vhost_name" "$vhost_name" > "$root/index.html"
        log "created $root"
    done

    # Apache 2.4 refuses every directory it is not told to serve. Debian's
    # main config allows all of /var/www, so a vhost written there works
    # without a <Directory> block -- and the same vhost answers 403 to
    # everything on Alpine, Fedora or Arch, which allow only their own
    # default root, while its config test passes. So a root nothing covers
    # is granted here, in the same file, so a re-run rewrites it rather
    # than adding another.
    #
    # Only a root nothing but <Directory /> covers, though. A Require in a
    # deeper block replaces its parent's rather than adding to it, so
    # granting a root beneath a block someone wrote -- in the vhost or
    # anywhere in the server's config -- would open what that block
    # restricts. Such a root is left to whatever covers it.
    if sections=$(directory_sections); then
        printf '%s\n' "$roots" |
        while IFS= read -r root; do
            case "$root" in /?*) ;; *) continue ;; esac
            root="${root%/}"
            if covered "$root"; then
                log "left access to $root to the <Directory> block that covers it"
                continue
            fi
            printf '\n# Added by lemondx (apache2): Apache serves no directory it is not\n# told to, and no <Directory> block covers this one.\n<Directory "%s">\n    Require all granted\n</Directory>\n' \
                "$root" >> "$target"
            log "granted access to $root (no <Directory> block covers it)"
        done
    else
        warn "could not list Apache's configuration files, so no DocumentRoot was granted access; add a <Directory> block if the site answers 403"
    fi

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

# The status a request gets, or nothing when there is no answer (or no client:
# curl or busybox/GNU wget, whichever the image has). wget exits non-zero on
# any error status, and curl on no answer, which pipefail and set -e would
# turn into the module failing.
http_status() {
    if have curl; then
        curl -s -o /dev/null -m 5 -w '%{http_code}' -H "Host: $2" "$1" 2>/dev/null | grep -v '^000$' || true
    elif have wget; then
        wget -S -O /dev/null -T 5 --header "Host: $2" "$1" 2>&1 |
            awk '/^ *HTTP\// { code = $2 } END { if (code) print code }' || true
    fi
}

if [ -n "$vhost" ]; then
    server_name=$(printf '%s\n' "$vhost" | awk 'tolower($1) == "servername" { print $2; exit }')
    port=$(printf '%s\n' "$vhost" | awk 'tolower($1) ~ /^<virtualhost/ {
        n = split($0, part, ":"); p = part[n]; sub(/[^0-9].*/, "", p)
        if (p != "") { print p; exit } }')
    # Ask the site itself: a config test passes a vhost that refuses every
    # request. Only plain HTTP, and only warned about, since a vhost may
    # refuse this host on purpose (Require ip ...).
    if [ "${port:-80}" != 443 ] && ! printf '%s\n' "$vhost" | grep -qi '^[[:space:]]*SSLEngine[[:space:]]*on'; then
        url="http://127.0.0.1:${port:-80}/"
        status=""
        for _ in 1 2 3 4 5; do
            status=$(http_status "$url" "${server_name:-localhost}")
            [ -n "$status" ] && break
            sleep 1
        done
        case "$status" in
            "") warn "could not check the site: no answer from $url, or no curl or wget here" ;;
            403) warn "the site answers 403 Forbidden: Apache is refusing the request -- check its <Directory> and Require lines (error log: /var/log/apache2 or /var/log/httpd)" ;;
            5??) warn "the site answers $status: see Apache's error log (/var/log/apache2 or /var/log/httpd)" ;;
            *) log "the site answers $status on port ${port:-80}" ;;
        esac
    fi
    if [ -n "$server_name" ]; then
        log "try: curl -H 'Host: $server_name' http://<container-ip>/"
    fi
fi

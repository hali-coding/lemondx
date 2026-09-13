#!/bin/sh
# name: PostgreSQL
# description: Install PostgreSQL, start it, and set a role's password.
# order: 50
# secret: POSTGRES_PASSWORD=  Password for the role (at least 8 characters)
# param: POSTGRES_USER=postgres  Role to set the password on; created if missing
# param: POSTGRES_DB=devdb  Database to create, owned by that role (blank for none)
# param: LISTEN_ADDRESSES=* Where to accept TCP connections; * for everywhere

# Commands below run as the postgres user, which cannot read /root -- the
# directory exec starts in -- so psql would warn on every call.
cd /

user="${POSTGRES_USER:-postgres}"
db="${POSTGRES_DB:-devdb}"
listen="${LISTEN_ADDRESSES:-\*}"

password="${POSTGRES_PASSWORD:-}"
[ "${#password}" -ge 8 ] || die "POSTGRES_PASSWORD must be at least 8 characters"

# Role and database names go into SQL as identifiers, so hold them to a shape
# that needs no quoting rather than trying to escape them.
case "$user" in
    [a-z_]*) ;;
    *) die "POSTGRES_USER must start with a lower-case letter or underscore" ;;
esac
case "$user$db" in
    *[!a-z0-9_]*) die "POSTGRES_USER and POSTGRES_DB may only use a-z, 0-9 and _" ;;
esac

# --- install and initialise ------------------------------------------------

# Only Debian and Ubuntu create a cluster on install; everywhere else it is a
# separate step, skipped when a cluster already exists so reruns are safe.
case "$LEMONDX_PKG" in
    apt)
        pkg_install postgresql
        ;;
    apk)
        pkg_install postgresql postgresql-client
        if ! ls /var/lib/postgresql/*/data/PG_VERSION >/dev/null 2>&1; then
            log "initialising cluster"
            rc-service postgresql setup
        fi
        ;;
    dnf|yum|microdnf)
        pkg_install postgresql-server postgresql
        if [ ! -f /var/lib/pgsql/data/PG_VERSION ]; then
            log "initialising cluster"
            postgresql-setup --initdb
        fi
        ;;
    pacman)
        pkg_install postgresql
        if [ ! -f /var/lib/postgres/data/PG_VERSION ]; then
            log "initialising cluster"
            su -s /bin/sh postgres -c "initdb -D /var/lib/postgres/data --auth-local=peer --auth-host=scram-sha-256" >/dev/null
        fi
        ;;
    zypper)
        # The systemd unit initialises the cluster on its first start.
        pkg_install postgresql-server postgresql
        ;;
    *)
        die "no PostgreSQL recipe for package manager '$LEMONDX_PKG'"
        ;;
esac

svc_enable postgresql

# Run SQL as the postgres superuser, reading it from stdin: nothing sensitive
# ever appears in a process's argument list.
psql_admin() {
    su -s /bin/sh postgres -c "psql -X -q -v ON_ERROR_STOP=1 -d postgres $*"
}

log "waiting for the server to accept connections"
tries=0
until su -s /bin/sh postgres -c "psql -X -q -d postgres -c 'select 1'" >/dev/null 2>&1; do
    tries=$((tries + 1))
    [ "$tries" -le 60 ] || die "PostgreSQL did not come up within 60 seconds"
    sleep 1
done

# --- role and password -----------------------------------------------------

# The password is spliced into a string literal, so double any single quotes.
# That is the whole escaping job once standard_conforming_strings is on (the
# default since 9.1, forced here anyway): backslashes are then literal. printf
# is a shell builtin, so the value never reaches an argv either.
password_sql=$(printf '%s' "$password" | sed "s/'/''/g")

log "setting the password for role '$user'"
psql_admin >/dev/null <<SQL
SET standard_conforming_strings = on;
-- Keep the statement out of the server log even if statement logging is on.
SET log_statement = 'none';
SET password_encryption = 'scram-sha-256';
SELECT 'CREATE ROLE $user LOGIN'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$user') \gexec
ALTER ROLE $user WITH LOGIN PASSWORD '$password_sql';
SQL

if [ -n "$db" ]; then
    log "ensuring database '$db' owned by '$user'"
    psql_admin >/dev/null <<SQL
SELECT 'CREATE DATABASE $db OWNER $user'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$db') \gexec
SQL
fi
unset password password_sql

# --- authentication and network access ------------------------------------

# Ask the server where its config lives instead of guessing per distro.
hba=$(su -s /bin/sh postgres -c "psql -X -At -d postgres -c 'show hba_file'")
current=$(su -s /bin/sh postgres -c "psql -X -At -d postgres -c 'show listen_addresses'")
[ -n "$hba" ] && [ -f "$hba" ] || die "could not locate pg_hba.conf"

# Distro defaults cannot be trusted to make the password matter: Alpine's
# initdb writes `trust` for local and localhost connections, so any password
# -- or none -- logs in, while Fedora/Rocky write `ident` for localhost, so a
# password login fails outright. pg_hba.conf is first-match-wins, so put our
# rules at the top: peer for the local socket (how the postgres superuser is
# reached), a password for every TCP connection. The block is regenerated on
# each run, so changing LISTEN_ADDRESSES back to localhost removes remote rules.
begin="# BEGIN lemondx postgresql module"
end="# END lemondx postgresql module"

block=$(
    printf '%s\n' "$begin"
    printf '%s\n' "# Managed by lemondx; rerun the module rather than editing by hand."
    printf 'local  all  all                 peer\n'
    printf 'host   all  all  127.0.0.1/32   scram-sha-256\n'
    printf 'host   all  all  ::1/128        scram-sha-256\n'
    if [ "$listen" != "localhost" ]; then
        printf 'host   all  all  0.0.0.0/0      scram-sha-256\n'
        printf 'host   all  all  ::/0           scram-sha-256\n'
    fi
    printf '%s\n' "$end"
)

# Rebuild the file with the old block (if any) removed and the new one first.
# Writing through `cat >` keeps the file's owner and permissions.
{
    printf '%s\n\n' "$block"
    awk -v b="$begin" -v e="$end" '
        $0 == b { skip = 1; next }
        $0 == e { skip = 0; next }
        !skip
    ' "$hba" | awk 'NF || seen { seen = 1; print }'
} > "$hba.lemondx-new"
if ! cmp -s "$hba.lemondx-new" "$hba"; then
    log "password authentication enforced in $hba"
    cat "$hba.lemondx-new" > "$hba"
fi
rm -f "$hba.lemondx-new"

if [ "$current" != "$listen" ]; then
    log "listen_addresses: $current -> $listen"
    # ALTER SYSTEM writes postgresql.auto.conf, the same on every distro.
    printf "ALTER SYSTEM SET listen_addresses = '%s';\n" "$(printf '%s' "$listen" | sed "s/'/''/g")" \
        | psql_admin >/dev/null
    # listen_addresses only takes effect on restart.
    if have systemctl && [ -d /run/systemd/system ]; then
        systemctl restart postgresql
    else
        rc-service postgresql restart >/dev/null
    fi
    tries=0
    until su -s /bin/sh postgres -c "psql -X -q -d postgres -c 'select 1'" >/dev/null 2>&1; do
        tries=$((tries + 1))
        [ "$tries" -le 60 ] || die "PostgreSQL did not come back after restart"
        sleep 1
    done
else
    # pg_hba.conf changes only need a reload.
    printf 'SELECT pg_reload_conf();\n' | psql_admin >/dev/null
fi

version=$(su -s /bin/sh postgres -c "psql -X -At -d postgres -c 'show server_version'")
log "PostgreSQL $version ready; role '$user' can log in with its password"
if [ -n "$db" ]; then log "database '$db' is owned by '$user'"; fi
if [ "$listen" = "localhost" ]; then
    log "listening on localhost only; from inside the container: psql -h localhost -U $user${db:+ -d $db}"
else
    log "connect with: psql -h <container-ip> -U $user${db:+ -d $db}"
fi

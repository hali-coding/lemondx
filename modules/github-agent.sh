#!/bin/sh
# name: GitHub runner
# description: Install a self-hosted GitHub Actions runner: registered as a service, installed only, or for one job (jit) as a task's job.
# os: debian ubuntu fedora centos rhel rocky almalinux opensuse-leap sles
# order: 90
# repeatable: yes
# secret: GITHUB_RUNNER_TOKEN=  Registration token (expires after an hour), or a PAT / app token to mint one at every run (jit needs the latter)
# optional: GITHUB_RUNNER_TOKEN
# param: GITHUB_REPO_URL=  Repository or organisation URL, e.g. https://github.com/owner/repo or https://github.com/org
# param: RUNNER_MODE=service  service (registered, runs as a service), install (download only), or jit (one job, then exit)
# param: RUNNER_NAME=  Runner name (blank: the instance name, plus -N for a repeat)
# param: RUNNER_LABELS=  Labels, comma separated (service: added to self-hosted,Linux,X64; jit: required)
# param: RUNNER_GROUP=  Runner group: a name for service, a numeric id for jit (blank: Default)
# param: RUNNER_USER=runner  Account the runner and its jobs run as
# param: RUNNER_VERSION=latest  Runner release, e.g. 2.338.0
# param: RUNNER_SHA256=  Tarball checksum; blank looks it up in the release notes
# param: RUNNER_IDLE_SECONDS=300  jit: give up when no job has started this long after registering

mode="${RUNNER_MODE:-service}"
case "$mode" in
    service|install|jit) ;;
    *) die "RUNNER_MODE is service, install or jit, not '$mode'" ;;
esac
if [ "$mode" != install ]; then
    [ -n "${GITHUB_REPO_URL:-}" ] || die "GITHUB_REPO_URL is required"
    [ -n "${GITHUB_RUNNER_TOKEN:-}" ] || die "GITHUB_RUNNER_TOKEN is required"
fi

# The runner is a .NET app built against glibc, and svc.sh writes a systemd
# unit; neither exists on Alpine or an OpenRC image.
if [ "$mode" = service ]; then
    have systemctl && [ -d /run/systemd/system ] || die "the runner service needs systemd"
fi

case "$(uname -m)" in
    x86_64)        arch=x64 ;;
    aarch64|arm64) arch=arm64 ;;
    armv7l)        arch=arm ;;
    *)             die "no runner build for $(uname -m)" ;;
esac

# A repeat is a second runner -- for another repository, say -- so it gets
# its own directory and, by default, its own name.
suffix=""
[ "${WATERPUMP_OCCURRENCE:-1}" -gt 1 ] && suffix="-$WATERPUMP_OCCURRENCE"
dir="/opt/actions-runner$suffix"
name="${RUNNER_NAME:-$WATERPUMP_INSTANCE$suffix}"
RUNNER_USER="${RUNNER_USER:-runner}"

have curl && have tar && have sha256sum || pkg_install curl ca-certificates tar coreutils

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# --- the GitHub API ----------------------------------------------------------

url="${GITHUB_REPO_URL:-}"
url="${url%/}"
url="${url%.git}"
host="$(printf '%s' "$url" | sed -n 's#^https://\([^/]*\)/.*#\1#p')"
if [ "$mode" != install ]; then
    [ -n "$host" ] || die "GITHUB_REPO_URL must be an https:// URL: $url"
    path="${url#https://$host/}"
    if [ "$host" = github.com ]; then api=https://api.github.com; else api="https://$host/api/v3"; fi
    case "$path" in
        enterprises/*/*) die "not a runner scope: $url" ;;
        enterprises/*)   scope="enterprises/${path#enterprises/}" ;;
        */*/*)           die "not a repository or organisation URL: $url" ;;
        */*)             scope="repos/$path" ;;
        *)               scope="orgs/$path" ;;
    esac
fi

# A credential (ghp_, github_pat_, an app's ghs_ ...) can mint a registration
# token or a just-in-time config here, which is what lets a template or a
# task run long after a pasted registration token has expired. It goes to
# curl on stdin as a header, never in argv, where any process in the
# instance could read it.
is_credential() {
    case "${GITHUB_RUNNER_TOKEN:-}" in
        ghp_*|github_pat_*|gho_*|ghs_*|ghu_*) return 0 ;;
    esac
    return 1
}

gh_api() {  # METHOD URL [JSON] -- authenticated when we hold a credential
    target="$2" data="${3:-}"
    set -- -fsSL -X "$1" -H 'Accept: application/vnd.github+json'
    [ -n "$data" ] && set -- "$@" -H 'Content-Type: application/json' --data "$data"
    if is_credential; then
        printf 'Authorization: Bearer %s\n' "$GITHUB_RUNNER_TOKEN" | curl "$@" -H @- "$target"
    else
        curl "$@" "$target"
    fi
}

# GitHub's answers are pretty-printed; one line makes them sed-able.
json_string() {  # KEY < JSON
    tr -d '\n' | sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p"
}

# --- install -----------------------------------------------------------------

getent passwd "$RUNNER_USER" >/dev/null ||
    useradd --create-home --shell /bin/bash "$RUNNER_USER"
home="$(getent passwd "$RUNNER_USER" | cut -d: -f6)"
# Docker installed earlier in the run (its module is order 60) is only usable
# by a job if the runner's account can reach the socket.
getent group docker >/dev/null && usermod -aG docker "$RUNNER_USER"

# An image baked with RUNNER_MODE=install already has the runner, and the
# runner updates itself, so a later pass only checks it is there.
if [ -x "$dir/run.sh" ]; then
    log "runner already installed in $dir"
else
    # Releases are always on github.com, even for a GHES registration. The
    # release notes carry each tarball's checksum between BEGIN/END SHA
    # markers, which is what the runner's own docs point at; one call gives
    # the version and the checksum both.
    version="${RUNNER_VERSION:-latest}"
    version="${version#v}"
    sha="${RUNNER_SHA256:-}"
    if [ "$version" = latest ] || [ -z "$sha" ]; then
        if [ "$version" = latest ]; then release=latest; else release="tags/v$version"; fi
        # Only a github.com credential means anything to api.github.com.
        if [ "$host" = github.com ]; then
            notes="$(gh_api GET "https://api.github.com/repos/actions/runner/releases/$release")"
        else
            notes="$(curl -fsSL "https://api.github.com/repos/actions/runner/releases/$release")"
        fi || die "cannot read the runner release (rate limited? pin RUNNER_VERSION and RUNNER_SHA256)"
        if [ "$version" = latest ]; then
            version="$(printf '%s' "$notes" | json_string tag_name)"
            version="${version#v}"
            [ -n "$version" ] || die "cannot find the latest runner version"
        fi
        if [ -z "$sha" ]; then
            sha="$(printf '%s' "$notes" |
                sed -n "s/.*BEGIN SHA linux-$arch -->\([0-9a-f]\{64\}\)<.*/\1/p" | head -n1)"
            [ -n "$sha" ] || die "no checksum for linux-$arch in the v$version release notes; set RUNNER_SHA256"
        fi
    fi

    tarball="actions-runner-linux-$arch-$version.tar.gz"
    log "downloading runner $version ($arch)"
    curl -fsSL -o "$tmp/$tarball" \
        "https://github.com/actions/runner/releases/download/v$version/$tarball"
    printf '%s  %s\n' "$sha" "$tmp/$tarball" | sha256sum -c - >/dev/null ||
        die "checksum mismatch for $tarball"

    mkdir -p "$dir"
    tar -xzf "$tmp/$tarball" -C "$dir"
    chown -R "$RUNNER_USER:" "$dir"

    log "installing runner dependencies"
    "$dir/bin/installdependencies.sh" >"$tmp/deps.log" 2>&1 || {
        tail -n 20 "$tmp/deps.log" >&2
        die "installdependencies.sh failed"
    }
fi

[ "$mode" = install ] && { log "runner installed in $dir, not registered"; exit 0; }

# --- jit: one job, in the foreground -------------------------------------------

if [ "$mode" = jit ]; then
    is_credential || die "jit mode mints its config with a PAT or app token (ghp_, github_pat_, ghs_ ...), not a registration token"
    case "$scope" in
        enterprises/*) die "jit mode registers with a repository or organisation, not $url" ;;
    esac
    group="${RUNNER_GROUP:-1}"
    case "$group" in
        *[!0-9]*) die "in jit mode RUNNER_GROUP is a numeric runner group id (1 is Default), not '$group'" ;;
    esac
    # The job's own labels, as the webhook hands them on. They reach a JSON
    # body, so anything that would need escaping is refused rather than
    # quoted: no real label has it.
    case "${RUNNER_LABELS:-}" in
        "") die "jit mode needs RUNNER_LABELS (a task passes the job's: {{inputs.GITHUB_LABELS}})" ;;
        *[\"\\]*) die "a runner label cannot contain a quote or a backslash" ;;
    esac
    labels="$(printf '%s' "$RUNNER_LABELS" | sed 's/ *, */","/g; s/^ *//; s/ *$//')"
    body="{\"name\":\"$name\",\"runner_group_id\":$group,\"labels\":[\"$labels\"],\"work_folder\":\"_work\"}"

    log "requesting a just-in-time config for '$name' on $scope"
    gh_api POST "$api/$scope/actions/runners/generate-jitconfig" "$body" > "$tmp/jit.json" ||
        die "the token could not create a runner for $url (does it have admin on it?)"
    runner_id="$(tr -d '\n' < "$tmp/jit.json" | sed -n 's/.*"runner": *{ *"id": *\([0-9]*\).*/\1/p')"

    deregister() {
        [ -n "$runner_id" ] || { warn "no runner id to remove; GitHub drops it when it goes offline"; return 0; }
        gh_api DELETE "$api/$scope/actions/runners/$runner_id" >/dev/null &&
            log "removed runner $runner_id" || warn "could not remove runner $runner_id"
    }

    # The config is a credential for this one runner: into the runner's
    # environment (it reads ACTIONS_RUNNER_INPUT_JITCONFIG and masks it),
    # never onto a command line. setsid gives it a process group of its own,
    # so giving up on it stops everything it started.
    ACTIONS_RUNNER_INPUT_JITCONFIG="$(json_string encoded_jit_config < "$tmp/jit.json")"
    rm -f "$tmp/jit.json"
    [ -n "$ACTIONS_RUNNER_INPUT_JITCONFIG" ] || die "no encoded_jit_config in GitHub's answer"
    export ACTIONS_RUNNER_INPUT_JITCONFIG
    log "runner '$name' (id ${runner_id:-?}) waiting for its job"
    cd "$dir"
    # The job inherits the runner's environment, and bootstrap exports every
    # param of the run -- GITHUB_RUNNER_TOKEN (admin on the repo or org),
    # other modules' secrets, the task's inputs -- so a workflow could read
    # them all. Only an allowlist goes through; the token stays here for
    # deregister(). env -u drops by name, so no value reaches argv, and
    # unlike unset it cannot trip over a readonly variable. A continuation
    # line of a multi-line value may look like a name too; dropping a name
    # that is not set is harmless.
    (
        set --
        for var in $(env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p'); do
            case "$var" in
                PATH|HOME|LANG|LANGUAGE|LC_*|TZ|ACTIONS_RUNNER_INPUT_JITCONFIG) ;;
                *) set -- "$@" -u "$var" ;;
            esac
        done
        HOME="$home" exec env "$@" setsid runuser -u "$RUNNER_USER" -- ./run.sh
    ) &
    runner=$!
    unset ACTIONS_RUNNER_INPUT_JITCONFIG

    # A runner that no job reaches -- another runner took it, it was
    # cancelled, the delivery was a duplicate -- would wait until the task's
    # timeout. A Worker log is the runner starting a job.
    idle="${RUNNER_IDLE_SECONDS:-300}"
    (
        sleep "$idle"
        if ! ls "$dir"/_diag/Worker_*.log >/dev/null 2>&1; then
            : > "$tmp/idle"
            kill -TERM -"$runner" 2>/dev/null || kill -TERM "$runner" 2>/dev/null
        fi
    ) &
    watcher=$!
    code=0
    wait "$runner" || code=$?
    kill "$watcher" 2>/dev/null || true
    if [ -f "$tmp/idle" ]; then
        log "no job started within ${idle}s; giving up"
        deregister
        exit 0
    fi
    [ "$code" -eq 0 ] || deregister
    log "runner finished (exit $code)"
    exit "$code"
fi

# --- service: registered, and run by systemd ------------------------------------

if [ -f "$dir/.runner" ]; then
    # Bootstrap run again on a registered runner: registering again would
    # need a fresh token for nothing, and the runner updates itself.
    log "runner already registered in $dir; leaving it as it is"
else
    if is_credential; then
        log "requesting a registration token for $scope"
        gh_api POST "$api/$scope/actions/runners/registration-token" > "$tmp/reg.json" ||
            die "the token could not mint a registration token for $url (does it have admin on it?)"
        token="$(json_string token < "$tmp/reg.json")"
        rm -f "$tmp/reg.json"
        [ -n "$token" ] || die "no registration token in GitHub's answer"
    else
        token="$GITHUB_RUNNER_TOKEN"
    fi

    # config.sh reads any argument from ACTIONS_RUNNER_INPUT_<NAME>, which
    # keeps the token out of argv. runuser without -l keeps the environment.
    # --replace is for recreate: the new instance takes the old one's name.
    log "registering '$name' with $url"
    (
        cd "$dir"
        export ACTIONS_RUNNER_INPUT_TOKEN="$token"
        set -- --unattended --replace --url "$url" --name "$name" --work _work
        [ -n "${RUNNER_LABELS:-}" ] && set -- "$@" --labels "$RUNNER_LABELS"
        [ -n "${RUNNER_GROUP:-}" ] && set -- "$@" --runnergroup "$RUNNER_GROUP"
        HOME="$home" runuser -u "$RUNNER_USER" -- ./config.sh "$@"
    )
fi

# svc.sh writes a systemd unit named after the registration and starts it,
# so the runner survives a restart.
log "starting the runner service"
(
    cd "$dir"
    systemctl is-enabled --quiet "$(cat .service 2>/dev/null || echo none)" 2>/dev/null ||
        ./svc.sh install "$RUNNER_USER" >/dev/null
    ./svc.sh start >/dev/null
    ./svc.sh status | sed -n 's/^ *Active: /runner service: /p'
)

#!/bin/sh
# CI check against a live daemon: the part of waterpump pr.yml's other jobs
# cannot reach, run on the self-hosted `waterpump-daemon` runner. Everything goes
# through ./waterpump, so the CLI, ContainerService and lxd.py are exercised the
# way a user drives them, and `serve` is checked over real HTTP.
#
# The runner is long-lived, so every name carries the run id and the trap
# removes what this run made even when a step fails -- a leftover instance
# would otherwise sit on a 1 GB VM until someone noticed.
set -eu

flavor=${WATERPUMP_FLAVOR:-incus}
run=${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}
name=ci-$run
port=${DAEMON_SMOKE_PORT:-18099}
export WATERPUMP_FLAVOR=$flavor
export WATERPUMP_DATA_DIR=${RUNNER_TEMP:-/tmp}/waterpump-$run
mkdir -p "$WATERPUMP_DATA_DIR"
serve_pid=

cleanup() {
    [ -n "$serve_pid" ] && kill "$serve_pid" 2>/dev/null || true
    # -y: with no TTY the confirmation prompt reads as "no", which skips the
    # delete and still exits 0.
    ./waterpump delete -y -f "$name" >/dev/null 2>&1 || true
    rm -rf "$WATERPUMP_DATA_DIR"
}
trap cleanup EXIT

step() { echo "::group::$*"; }
end() { echo "::endgroup::"; }

step "status ($flavor)"
./waterpump status
./waterpump status --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["ready"], d'
end

# Alpine: small enough for the runner, and busybox ash is the strictest /bin/sh
# a bootstrap module has to survive.
step "create with the base module"
./waterpump create "$name" -i images:alpine/3.22 --no-default-modules -b base -c 1 -m 256MiB
end

step "exec"
./waterpump exec "$name" -- sh -c 'command -v curl'
# The daemon reports some non-zero exits as operation failures; waterpump must
# still hand back the command's own code (see "Exec has no websocket").
set +e
./waterpump exec "$name" -- sh -c 'exit 127'
rc=$?
set -e
[ "$rc" -eq 127 ] || { echo "::error::exec exit 127 came back as $rc"; exit 1; }
end

step "snapshot, restore"
./waterpump snapshot "$name" s1
./waterpump snapshots "$name" | grep -q '^s1 '
./waterpump restore "$name" s1
end

step "serve and the API"
./waterpump serve --port "$port" >"$WATERPUMP_DATA_DIR/serve.log" 2>&1 &
serve_pid=$!
i=0
until curl -sf "http://127.0.0.1:$port/api/status" >/dev/null; do
    i=$((i + 1))
    [ "$i" -lt 30 ] || { cat "$WATERPUMP_DATA_DIR/serve.log"; exit 1; }
    sleep 1
done
curl -sf "http://127.0.0.1:$port/api/containers" \
    | python3 -c 'import json,sys; names=[c["name"] for c in json.load(sys.stdin)["data"]]; assert sys.argv[1] in names, names' "$name"
end

step "stop, delete"
./waterpump stop "$name"
./waterpump delete -y "$name"
if ./waterpump list --json | python3 -c 'import json,sys; sys.exit(0 if any(c["name"]==sys.argv[1] for c in json.load(sys.stdin)) else 1)' "$name"; then
    echo "::error::$name still exists after delete"
    exit 1
fi
end

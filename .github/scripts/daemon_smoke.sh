#!/bin/sh
# CI check against a live daemon: the part of waterpump pr.yml's other jobs
# cannot reach, run by daemon.yml on the self-hosted `waterpump-daemon` runner,
# once per Python version in its matrix. Everything goes through ./waterpump,
# so the CLI, ContainerService and lxd.py are exercised the way a user drives
# them, and `serve` is checked over real HTTP.
#
# The runner is long-lived, so every name carries the run id (and the Python,
# since daemon.yml runs this once per version in one run) and the trap
# removes what this run made even when a step fails -- a leftover instance
# would otherwise sit on a 1 GB VM until someone noticed.
set -eu

flavor=${WATERPUMP_FLAVOR:-incus}
py=$(python3 -c 'import sys; print("py%d%d" % sys.version_info[:2])')
run=${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-$py
name=ci-$run
template=ci-t-$run
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
    ./waterpump template-destroy -y "$template" >/dev/null 2>&1 || true
    ./waterpump template-delete -y "$template" >/dev/null 2>&1 || true
    rm -rf "$WATERPUMP_DATA_DIR"
}
trap cleanup EXIT

step() { echo "::group::$*"; }
end() { echo "::endgroup::"; }

# Every listing is taken in an assignment of its own, never at the head of a
# pipeline: there only the last command's status counts, so a listing that
# failed would be judged on whatever it printed.

# The names of the instances tagged with a template, one per line, sorted.
tagged() {
    listing=$(./waterpump list --json)
    printf '%s' "$listing" | python3 -c '
import json, sys
for c in sorted(json.load(sys.stdin), key=lambda c: c["name"]):
    if c["template"] == sys.argv[1]:
        print(c["name"], c["status"])' "$1"
}

# Fail unless `waterpump <listing> --json` has nothing called <name>. An
# assertion, not an exit code read as "absent", so a listing that is not
# valid JSON fails too rather than passing for an empty one.
absent() {
    listing=$(./waterpump "$1" --json)
    printf '%s' "$listing" | python3 -c '
import json, sys
names = [r["name"] for r in json.load(sys.stdin)]
if sys.argv[1] in names:
    sys.exit("::error::%s still listed after delete" % sys.argv[1])' "$2"
}

step "status ($flavor, $(python3 --version))"
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

step "template save"
# The app check goes in as a file and comes back as the script itself: it is
# read from the template every round, never copied onto the instance.
printf '#!/bin/sh\ncommand -v curl >/dev/null\n' >"$WATERPUMP_DATA_DIR/check.sh"
./waterpump template-save "$template" -i images:alpine/3.22 -b base -c 1 -m 256MiB \
    --prefix "$template" --app-check "$WATERPUMP_DATA_DIR/check.sh"
./waterpump templates --json | python3 -c '
import json, sys
t = {t["name"]: t for t in json.load(sys.stdin)}[sys.argv[1]]
assert t["image"] == "images:alpine/3.22", t
assert t["bootstrap"]["modules"] == ["base"], t
assert "command -v curl" in t["app_check"]["script"], t' "$template"
end

# --no-serve: the run happens in this command. Nothing is serving yet anyway,
# but this keeps the step about the CLI path; the launch through serve is
# checked below.
step "template launch"
./waterpump launch --no-serve -n 2 "$template"
got=$(tagged "$template")
want=$(printf '%s-1 Running\n%s-2 Running' "$template" "$template")
[ "$got" = "$want" ] || { echo "::error::after launch: $got"; exit 1; }
end

step "template exec"
./waterpump template-exec --json "$template" -- hostname | python3 -c '
import json, sys
r = json.load(sys.stdin)
assert r["ok"], r
out = sorted(i["exec"]["stdout"].strip() for i in r["instances"])
assert out == [sys.argv[1] + "-1", sys.argv[1] + "-2"], out' "$template"
# A command failing in the instances is the run failing, not a quiet success.
if ./waterpump template-exec "$template" -- sh -c 'exit 3'; then
    echo "::error::template-exec of a failing command exited 0"
    exit 1
fi
end

step "template destroy"
./waterpump template-destroy -y "$template"
got=$(tagged "$template")
[ -z "$got" ] || { echo "::error::left after destroy: $got"; exit 1; }
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

# With serve up, `launch` hands the run to it rather than running it in this
# command, and the run is then serve's to report -- the path the UI follows.
step "template launch through serve"
# Into a file, not through tee, which would hide launch's own exit status.
rc=0
./waterpump launch -n 1 "$template" >"$WATERPUMP_DATA_DIR/launch.log" 2>&1 || rc=$?
cat "$WATERPUMP_DATA_DIR/launch.log"
[ "$rc" -eq 0 ] || exit "$rc"
grep -q 'running on serve here' "$WATERPUMP_DATA_DIR/launch.log" \
    || { echo "::error::launch did not go through serve"; exit 1; }
curl -sf "http://127.0.0.1:$port/api/template-runs" | python3 -c '
import json, sys
runs = [r for r in json.load(sys.stdin)["data"] if r["template"] == sys.argv[1]]
assert len(runs) == 1, runs
r = runs[0]
assert r["action"] == "launch" and r["finished_at"] and r["result"]["ok"], r
assert [i["name"] for i in r["result"]["instances"]] == [sys.argv[1] + "-1"], r' "$template"
[ "$(tagged "$template")" = "$template-1 Running" ] || { echo "::error::not running after a serve launch"; exit 1; }
./waterpump template-destroy -y "$template"
./waterpump template-delete -y "$template"
absent templates "$template"
end

step "stop, delete"
./waterpump stop "$name"
./waterpump delete -y "$name"
absent list "$name"
end

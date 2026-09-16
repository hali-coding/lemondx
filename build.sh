#!/bin/sh
# Build the web UI into web/dist, which is not in git -- a clone has to run
# this once before `./lemondx serve` has anything to serve. A release archive
# already contains it; see docs/development.md.
#
#   ./build.sh            # install deps if needed, then tsc -b && vite build
#   ./build.sh --clean    # discard web/dist and node_modules first
#
# Everything it needs is a Node toolchain. The Python side needs nothing built,
# so a failure here leaves a working CLI and API behind -- only the UI is missing.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$root"

clean=0
for arg in "$@"; do
    case "$arg" in
        --clean) clean=1 ;;
        -h|--help)
            sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            echo "$0: unknown option '$arg' (try --help)" >&2
            exit 2 ;;
    esac
done

die() {
    echo "build.sh: $1" >&2
    exit 1
}

# -- check the toolchain before touching anything -------------------------

command -v node >/dev/null 2>&1 || die "node is not installed.
lemondx itself does not need Node -- only building the UI does. Install it
from your distro, from https://nodejs.org, or with a version manager (nvm,
fnm, mise), then run this again. Or download a release, which ships web/dist
already built."

command -v npm >/dev/null 2>&1 || die "npm is not installed.
It normally comes with Node; some distros split it into a separate package
(Debian/Ubuntu: 'apt install npm')."

# Vite 8 requires ^20.19.0 || >=22.12.0. Check it here rather than letting the
# build fail halfway through with a stack trace from inside a dependency.
version=$(node --version | sed 's/^v//')
major=${version%%.*}
rest=${version#*.}
minor=${rest%%.*}
case "$major" in
    ''|*[!0-9]*) die "could not parse the Node version ('$version')." ;;
esac
ok=0
if [ "$major" -ge 23 ]; then
    ok=1
elif [ "$major" -eq 22 ] && [ "$minor" -ge 12 ]; then
    ok=1
elif [ "$major" -eq 20 ] && [ "$minor" -ge 19 ]; then
    ok=1
fi
[ "$ok" -eq 1 ] || die "Node $version is too old for this build (Vite needs
20.19+ or 22.12+; CI builds on 24). Upgrade Node, or download a release, which
ships web/dist already built."

[ -f web/package.json ] || die "web/package.json is missing -- run this from a
full checkout."

echo "Node $(node --version), npm $(npm --version)"

# -- build ---------------------------------------------------------------

if [ "$clean" -eq 1 ]; then
    echo "Removing web/dist and web/node_modules"
    rm -rf web/dist web/node_modules
fi

# `npm ci` is the reproducible install, but it deletes node_modules every time,
# so only pay for it when there is nothing there or the lockfile has moved on.
if [ ! -d web/node_modules ]; then
    echo "Installing dependencies (npm ci)"
    npm --prefix web ci
elif [ web/package-lock.json -nt web/node_modules ]; then
    echo "Lockfile is newer than node_modules; reinstalling (npm ci)"
    npm --prefix web ci
else
    echo "Dependencies already installed (--clean to start over)"
fi

echo "Building (tsc -b && vite build)"
npm --prefix web run build

[ -f web/dist/index.html ] || die "the build finished but web/dist/index.html
is missing -- check the output above."

echo
echo "Built web/dist:"
ls -l web/dist
echo
echo "Now run: ./lemondx serve --open"

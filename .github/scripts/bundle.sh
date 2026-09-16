#!/bin/sh
# Assemble the release archives: a tree that runs with nothing but Python 3.9+,
# which is what a clone gave you back when web/dist was committed.
#
#   .github/scripts/bundle.sh <version> [outdir]
#
# Run it by hand to check what a release will contain -- the release workflow
# calls exactly this after `npm run build`, so there is no second definition of
# "all components" to drift out of step.
set -eu

version=${1:-}
outdir=${2:-dist}
if [ -z "$version" ]; then
    echo "usage: $0 <version> [outdir]" >&2
    exit 2
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$root"

# The UI is no longer in git, so a bundle without it would silently ship a
# server that serves 404s. Refuse instead.
if [ ! -f web/dist/index.html ]; then
    echo "error: web/dist/index.html is missing -- run ./build.sh first" >&2
    exit 1
fi

name="lemondx-$version"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
stage="$work/$name"
mkdir -p "$stage/web"

# Everything the runtime reaches for: the launcher, the package, the built UI,
# the bootstrap modules, the systemd templates, and the docs that error
# messages point at ("see docs/security.md"). Not web/src, not .github --
# those are for changing lemondx, not running it.
for item in lemondx pyproject.toml README.md CHANGELOG.md src modules systemd docs; do
    [ -e "$item" ] || continue
    cp -R "$item" "$stage/"
done
cp -R web/dist "$stage/web/dist"

# Screenshots are Git LFS pointers unless the checkout fetched them, and they
# only exist to render on the forge. Shipping stub files would be worse than
# shipping nothing.
rm -f "$stage"/docs/*.png
find "$stage" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$stage" -name '*.py[co]' -delete

mkdir -p "$outdir"
outdir=$(CDPATH= cd -- "$outdir" && pwd)
rm -f "$outdir/$name.tar.gz" "$outdir/$name.zip"

# Reproducible-ish: sorted entries, no owner names, no mtimes from the runner.
(cd "$work" && tar --sort=name --owner=0 --group=0 --numeric-owner \
    --mtime="@${SOURCE_DATE_EPOCH:-0}" -czf "$outdir/$name.tar.gz" "$name")
(cd "$work" && zip -q -r -X "$outdir/$name.zip" "$name")

(cd "$outdir" && sha256sum "$name.tar.gz" "$name.zip" > SHA256SUMS)

echo "Wrote:"
(cd "$outdir" && ls -l "$name.tar.gz" "$name.zip" SHA256SUMS)

"""Read image catalogs from simplestreams servers (the LXD/Incus image remotes).

Both `images:` and `ubuntu:` publish a simplestreams index that lists every
image they offer. Reading it lets lemondx show a real, browsable catalog
instead of a hand-written shortlist -- and because the stream carries the same
combined SHA256 that LXD uses as an image fingerprint, we can say exactly which
entries are already downloaded.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

INDEX_PATH = "streams/v1/index.json"
DOWNLOAD_DATATYPE = "image-downloads"
CACHE_TTL = 900          # 15 minutes; these catalogs change daily at most
USER_AGENT = "lemondx/0.1"

# ftype of the metadata item that carries the combined fingerprints.
METADATA_FTYPE = "lxd.tar.xz"
# Keys holding the fingerprint of the finished image, per instance type.
CONTAINER_HASH = "combined_squashfs_sha256"
VM_HASHES = ("combined_disk-kvm-img_sha256", "combined_disk1-img_sha256")

_cache = {}
_lock = threading.Lock()


class CatalogError(Exception):
    pass


def _get_json(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise CatalogError("Cannot reach %s: %s" % (url, getattr(exc, "reason", exc)))
    except ValueError as exc:
        raise CatalogError("Malformed catalog at %s: %s" % (url, exc))


def _join(base, path):
    return "%s/%s" % (base.rstrip("/"), path.lstrip("/"))


def fetch_catalog(remote, base_url, timeout=30, refresh=False):
    """Every image a remote publishes, newest serial per product.

    Results are cached in memory; these catalogs are regenerated daily at most.
    """
    now = time.time()
    with _lock:
        cached = _cache.get(remote)
        if cached and not refresh and now - cached[0] < CACHE_TTL:
            return cached[1]

    index = _get_json(_join(base_url, INDEX_PATH), timeout)
    streams = (index.get("index") or {}).values()
    download = next(
        (s for s in streams if s.get("datatype") == DOWNLOAD_DATATYPE), None)
    if not download or not download.get("path"):
        raise CatalogError("%s publishes no downloadable images." % remote)

    catalog = _get_json(_join(base_url, download["path"]), timeout)
    entries = [
        entry for entry in (
            _parse_product(remote, product) for product in
            (catalog.get("products") or {}).values()
        ) if entry
    ]
    # Sort the way people read these: by OS, then by version number rather
    # than codename -- "debian/11" before "debian/12" before "debian/14",
    # not bookworm, bullseye, forky.
    entries.sort(key=lambda e: (e["os"].lower(), _natural_key(e["alias"])))

    with _lock:
        _cache[remote] = (now, entries)
    return entries


def _parse_product(remote, product):
    versions = product.get("versions") or {}
    if not versions:
        return None
    serial = max(versions)
    items = (versions[serial].get("items") or {})

    metadata = next(
        (i for i in items.values() if i.get("ftype") == METADATA_FTYPE), None)
    if not metadata:
        return None

    container_fp = metadata.get(CONTAINER_HASH)
    vm_fp = next((metadata[k] for k in VM_HASHES if metadata.get(k)), None)
    if not container_fp and not vm_fp:
        return None

    alias = _best_alias(product.get("aliases", ""))
    if not alias:
        return None

    rootfs = next((i for i in items.values() if i.get("ftype") == "squashfs"), None)
    disk = next((i for i in items.values() if i.get("ftype") == "disk-kvm.img"), None)

    os_name = product.get("os") or ""
    title = product.get("release_title") or product.get("release") or ""

    return {
        "alias": alias,
        "full_alias": "%s:%s" % (remote, alias),
        "label": _label(os_name, alias, title, product.get("variant") or ""),
        "remote": remote,
        "os": os_name,
        "release": product.get("release") or "",
        "release_title": title,
        "variant": product.get("variant") or "default",
        "arch": product.get("arch") or "",
        "serial": serial,
        "size": (rootfs or {}).get("size") or 0,
        "vm_size": (disk or {}).get("size") or 0,
        "container_fingerprint": container_fp,
        "vm_fingerprint": vm_fp,
        "supported": product.get("supported"),
        "eol": product.get("support_eol"),
    }


def _best_alias(aliases):
    """Pick the alias a person would type.

    `ubuntu:` offers e.g. "24.04,n,noble" and `images:` offers
    "alpine/3.21/default,alpine/3.21" -- so drop one-character shorthands and
    take the shortest of what is left.
    """
    candidates = [a.strip() for a in (aliases or "").split(",") if a.strip()]
    usable = [a for a in candidates if len(a) > 1] or candidates
    return min(usable, key=len) if usable else ""


def _label(os_name, alias, title, variant=""):
    """A name a person would recognise.

    Remotes disagree about what `release_title` holds: a version for Alpine
    ("3.21"), a codename for Debian ("bookworm"), a marketing string for Ubuntu
    ("24.04 LTS"). Where the alias carries a version the title does not, show
    both -- "Debian 12 (bookworm)" beats either half on its own.
    """
    # `ubuntu` publishes its os as lower case; everything else is title-cased.
    if os_name and os_name.islower():
        os_name = os_name.capitalize()

    segment = alias.split("/")[1] if "/" in alias else ""
    # The second segment is a version for debian/12, but a variant for
    # archlinux/cloud -- only the former belongs in the name.
    if segment and segment.lower() == variant.lower():
        segment = ""
    if segment and title and segment.lower() != title.lower():
        return "%s %s (%s)" % (os_name, segment, title)
    return ("%s %s" % (os_name, title or segment)).strip()


def _natural_key(text):
    """Split digits from text so 9 sorts before 11, not after it."""
    parts, digits = [], ""
    for char in str(text):
        if char.isdigit():
            digits += char
        else:
            if digits:
                parts.append((0, int(digits), ""))
                digits = ""
            parts.append((1, 0, char))
    if digits:
        parts.append((0, int(digits), ""))
    return parts


def clear_cache():
    with _lock:
        _cache.clear()

#!/usr/bin/env python3
"""
Upload recent unuploaded drives from a Comma 3X (or similar openpilot device).

Run this on the device (via SSH) while it is connected to Wi-Fi.
Requires the openpilot Python environment (PYTHONPATH=/data/openpilot or run from the openpilot venv).

Usage:
    python3 upload-new-drives.py [--days 7] [--dry-run] [--include-dcamera] [--verbose]
"""

import argparse
import contextlib
import logging
import os
import re
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

try:
    from openpilot.common.api import Api
    from openpilot.common.hardware.hw import Paths
    from openpilot.common.params import Params
    from openpilot.common.utils import get_upload_stream
    from openpilot.system.loggerd.xattr_cache import setxattr
except ImportError as e:
    print("Error: Could not import openpilot modules.")
    print(f"Import error: {e}")
    print()
    print("This script must be run on the Comma device where openpilot is installed.")
    print("Current PYTHONPATH:", os.environ.get("PYTHONPATH", "(not set)"))
    print()
    print("Typical fix:")
    print("  PYTHONPATH=/data/openpilot python3 upload-new-drives.py")
    print()
    traceback.print_exc()
    sys.exit(1)

import requests

UPLOAD_ATTR_NAME = "user.upload"
UPLOAD_ATTR_VALUE = b"1"

HTTP_OK = 200
HTTP_CREATED = 201
HTTP_PRECONDITION_FAILED = 412
HTTP_NOT_FOUND = 404
SUCCESS_CODES = {HTTP_OK, HTTP_CREATED}

SEGMENT_PARTS = 2
PROBE_TIMEOUT = 15
# /v1/route/.../files is rate-limited to 5/min
ROUTE_FILES_INTERVAL = 12.5  # seconds between calls to stay under limit
PROGRESS_EVERY = 50

LOG_DIR = Path.home() / ".local" / "state" / "upload-new-drives"
LOG_FILE = LOG_DIR / "debug.log"

# Files we care about for a "full" upload (qlogs/qcameras are usually already uploaded)
DEFAULT_FILES = (
    "rlog",
    "rlog.zst",
    "fcamera.hevc",
    "ecamera.hevc",
)

DCAMERA = "dcamera.hevc"

# Map filename -> key in the /files response
FILE_TO_RESPONSE_KEY = {
    "fcamera.hevc": "cameras",
    "ecamera.hevc": "ecameras",
    "dcamera.hevc": "dcameras",
    "rlog": "logs",
    "rlog.zst": "logs",
    "rlog.bz2": "logs",
}

log = logging.getLogger("upload-new-drives")

# Regex to pull segment number out of a storage URL
_SEG_RE = re.compile(r"/(\d+)/[^/]+\.(?:hevc|zst|bz2|ts)(?:\?|$)")


def _setup_logging(verbose: bool) -> None:
    """Always log verbosely to a file; optionally also to stderr."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()

    file_fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(file_fmt)
    log.addHandler(fh)

    if verbose:
        console_fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S")
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(console_fmt)
        log.addHandler(ch)

    log.debug("logging started (file=%s, verbose=%s)", LOG_FILE, verbose)


def _is_recent(path: Path, cutoff: datetime) -> bool:
    """True if the path's mtime is newer than the cutoff."""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return False
    return mtime >= cutoff


def _parse_segment_dir(name: str) -> tuple[str, int] | None:
    """Return (route_id, segment_number) if the directory name looks like a segment."""
    parts = name.rsplit("--", 1)
    if len(parts) == SEGMENT_PARTS and parts[1].isdigit():
        return parts[0], int(parts[1])
    return None


def _files_in_segment(
    seg_dir: Path, interesting: list[str]
) -> list[tuple[str, str, str, int]]:
    """Return list of (rel_key, abs_path, route_id, seg_num)."""
    parsed = _parse_segment_dir(seg_dir.name)
    if not parsed:
        return []
    route_id, seg_num = parsed
    found = []
    for fname in interesting:
        fpath = seg_dir / fname
        if fpath.is_file():
            found.append((f"{seg_dir.name}/{fname}", str(fpath), route_id, seg_num))
    return found


def _collect_local(
    root: Path, interesting: list[str], cutoff: datetime
) -> dict[str, list[tuple[str, str, int]]]:
    """Return dict of route_id -> list of (rel_key, abs_path, seg_num)."""
    if not root.is_dir():
        log.warning("Log root not found: %s", root)
        return {}

    by_route: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for seg_dir in sorted(root.iterdir(), reverse=True):
        if not seg_dir.is_dir() or not _is_recent(seg_dir, cutoff):
            continue
        for key, path, route_id, seg_num in _files_in_segment(seg_dir, interesting):
            by_route[route_id].append((key, path, seg_num))
    total = sum(len(v) for v in by_route.values())
    log.debug(
        "collected %d local candidate file(s) in %d route(s)", total, len(by_route)
    )
    return by_route


def _extract_segment(url: str) -> int | None:
    """Pull the segment number out of a signed storage URL."""
    m = _SEG_RE.search(unquote(url))
    if m:
        return int(m.group(1))
    return None


def _uploaded_from_response(data: dict[str, Any]) -> dict[str, set[int]]:
    """From a /files response, return mapping of response_key -> set of segment numbers present."""
    result: dict[str, set[int]] = {}
    for key in ("cameras", "ecameras", "dcameras", "logs", "qlogs", "qcameras"):
        segs = set()
        for url in data.get(key) or []:
            seg = _extract_segment(url)
            if seg is not None:
                segs.add(seg)
        result[key] = segs
    return result


def _get_route_files(
    api: Api, dongle_id: str, route_id: str
) -> dict[str, set[int]] | None:
    """Fetch already-uploaded files for one route. Returns None on hard error."""
    route_name = f"{dongle_id}|{route_id}"
    t0 = time.monotonic()
    try:
        resp = api.get(
            f"v1/route/{route_name}/files",
            timeout=PROBE_TIMEOUT,
            access_token=api.get_token(),
        )
    except requests.RequestException as e:
        elapsed = time.monotonic() - t0
        log.debug("route files error %s (%.1fs): %s", route_id, elapsed, e)
        return None

    elapsed = time.monotonic() - t0
    if resp.status_code == HTTP_NOT_FOUND:
        log.debug("route %s not on server yet (%.1fs)", route_id, elapsed)
        return {}  # nothing uploaded
    if resp.status_code != HTTP_OK:
        log.warning(
            "route files %s -> HTTP %s (%.1fs)", route_id, resp.status_code, elapsed
        )
        return None

    try:
        data = resp.json()
    except (ValueError, requests.JSONDecodeError) as e:
        log.warning("route files %s bad JSON: %s", route_id, e)
        return None

    uploaded = _uploaded_from_response(data)
    n = sum(len(s) for s in uploaded.values())
    log.debug("route %s: %d uploaded file(s) (%.1fs)", route_id, n, elapsed)
    return uploaded


def _filter_by_route_files(
    api: Api,
    dongle_id: str,
    by_route: dict[str, list[tuple[str, str, int]]],
) -> list[tuple[str, str]]:
    """Keep only files that are not already present on the server, using one call per route."""
    if not by_route:
        return []

    missing: list[tuple[str, str]] = []
    routes = list(by_route.keys())
    log.info("checking %d route(s) via /files (rate-limited to 5/min)", len(routes))

    for i, route_id in enumerate(routes):
        if i > 0:
            time.sleep(ROUTE_FILES_INTERVAL)

        uploaded = _get_route_files(api, dongle_id, route_id)
        candidates = by_route[route_id]

        if uploaded is None:
            # Could not check; fall back to treating all as missing (safer than skipping)
            log.warning(
                "could not check route %s, will attempt upload of its %d file(s)",
                route_id,
                len(candidates),
            )
            missing.extend((k, p) for k, p, _ in candidates)
            continue

        for key, path, seg_num in candidates:
            fname = key.rsplit("/", 1)[-1]
            resp_key = FILE_TO_RESPONSE_KEY.get(fname)
            if resp_key is None or seg_num not in uploaded.get(resp_key, set()):
                missing.append((key, path))

    log.info(
        "route check finished: %d missing out of %d",
        len(missing),
        sum(len(v) for v in by_route.values()),
    )
    return missing


def find_unuploaded(
    api: Api,
    dongle_id: str,
    root: Path,
    max_days: int,
    include_dcamera: bool,
) -> list[tuple[str, str]]:
    """Return list of (relative_key, absolute_path) that the server does not yet have."""
    cutoff = datetime.now() - timedelta(days=max_days)
    interesting = list(DEFAULT_FILES)
    if include_dcamera:
        interesting.append(DCAMERA)

    by_route = _collect_local(root, interesting, cutoff)
    if not by_route:
        return []

    return _filter_by_route_files(api, dongle_id, by_route)


def _mark_uploaded(fn: str) -> None:
    with contextlib.suppress(OSError):
        setxattr(fn, UPLOAD_ATTR_NAME, UPLOAD_ATTR_VALUE)


def _handle_412(fn: str) -> bool:
    log.info("skipped 412: %s", fn)
    _mark_uploaded(fn)
    return True


def _put_file(url: str, headers: dict, fn: str, key: str) -> bool:
    stream, size = get_upload_stream(
        fn, key.endswith(".zst") and not fn.endswith(".zst")
    )
    try:
        resp = requests.put(url, data=stream, headers=headers, timeout=60)
    finally:
        stream.close()

    if resp.status_code in SUCCESS_CODES:
        _mark_uploaded(fn)
        log.info("uploaded %s (%.1f MB)", key, size / 1e6)
        return True

    log.warning("upload failed %s: HTTP %s", key, resp.status_code)
    return False


def upload_file(api: Api, dongle_id: str, key: str, fn: str, dry_run: bool) -> bool:
    """Request upload URL and PUT the file. Returns True on success."""
    if dry_run:
        log.info("[dry-run] would upload %s", key)
        return True

    log.info("uploading %s", key)
    try:
        url_resp = api.get(
            f"v1.4/{dongle_id}/upload_url/",
            timeout=15,
            path=key,
            access_token=api.get_token(),
        )
    except requests.RequestException:
        log.exception("error getting upload URL for %s", key)
        return False

    if url_resp.status_code == HTTP_PRECONDITION_FAILED:
        return _handle_412(fn)

    if url_resp.status_code != HTTP_OK:
        log.warning("failed to get URL for %s: HTTP %s", key, url_resp.status_code)
        return False

    data = url_resp.json()
    return _put_file(data["url"], data.get("headers", {}), fn, key)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload recent unuploaded openpilot drives"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="How many recent days to scan (default: 14)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="List files but do not upload"
    )
    parser.add_argument(
        "--include-dcamera",
        action="store_true",
        help="Also upload dcamera.hevc if present",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Also log progress to stderr"
    )
    return parser.parse_args()


def _get_dongle_id() -> str:
    params = Params()
    raw = params.get("DongleId")
    if raw is None:
        log.error("Device not registered (no DongleId)")
        sys.exit(1)
    # Newer openpilot returns str; older versions may return bytes
    dongle_id = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    if not dongle_id or dongle_id == "UnregisteredDevice":
        log.error("Device not registered (no DongleId)")
        sys.exit(1)
    return dongle_id


def _print_files(files: list[tuple[str, str]]) -> None:
    log.info("found %d file(s) to upload", len(files))
    for key, _ in files:
        log.info("  %s", key)


def _do_uploads(
    api: Api, dongle_id: str, files: list[tuple[str, str]], dry_run: bool
) -> int:
    success = 0
    for key, fn in files:
        if upload_file(api, dongle_id, key, fn, dry_run):
            success += 1
        if not dry_run:
            time.sleep(0.5)
    return success


def main() -> None:
    args = _parse_args()
    _setup_logging(args.verbose)

    dongle_id = _get_dongle_id()
    root = Path(Paths.log_root())

    log.info("Dongle ID: %s", dongle_id)
    log.info("Log root:  %s", root)
    log.info("Scanning last %d days...", args.days)

    api = Api(dongle_id)
    files = find_unuploaded(api, dongle_id, root, args.days, args.include_dcamera)
    if not files:
        log.info("No unuploaded files found.")
        return

    _print_files(files)

    success = _do_uploads(api, dongle_id, files, args.dry_run)

    if args.dry_run:
        log.info("Dry run - nothing uploaded (%d/%d files).", success, len(files))
    else:
        log.info("Done. %d/%d uploaded successfully.", success, len(files))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Interrupted.")
        sys.exit(130)

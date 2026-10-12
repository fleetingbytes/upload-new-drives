#!/usr/bin/env python3
"""
Upload recent unuploaded drives from a Comma 3X (or similar openpilot device).

Run this on the device (via SSH) while it is connected to Wi-Fi.
Requires the openpilot Python environment (PYTHONPATH=/data/openpilot or run from the openpilot venv).

Usage:
    python3 upload-new-drives.py [--days 7] [--dry-run] [--include-dcamera]
"""

import argparse
import asyncio
import contextlib
import os
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

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

# Files we care about for a "full" upload (qlogs/qcameras are usually already uploaded)
DEFAULT_FILES = (
    "rlog",
    "rlog.zst",
    "fcamera.hevc",
    "ecamera.hevc",
)

DCAMERA = "dcamera.hevc"

# Map local filename to the key used in the /files response
FILE_TO_SERVER_KEY = {
    "rlog": "logs",
    "rlog.zst": "logs",
    "fcamera.hevc": "cameras",
    "ecamera.hevc": "ecameras",
    "dcamera.hevc": "dcameras",
}

RouteFiles = dict[str, list[tuple[str, str, int]]]


def _is_recent(path: Path, cutoff: datetime) -> bool:
    """True if the path's mtime is newer than the cutoff."""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return False
    return mtime >= cutoff


def _parse_segment_dir(name: str) -> tuple[str, int] | None:
    """Return (route_part, segment_num) or None if the name is not a segment dir."""
    parts = name.rsplit("--", 1)
    if len(parts) != SEGMENT_PARTS or not parts[1].isdigit():
        return None
    return parts[0], int(parts[1])


def _add_files_from_segment(
    by_route: RouteFiles,
    seg_dir: Path,
    interesting: list[str],
    route_part: str,
    segment: int,
) -> None:
    for fname in interesting:
        fpath = seg_dir / fname
        if fpath.is_file():
            key = f"{seg_dir.name}/{fname}"
            by_route[route_part].append((key, str(fpath), segment))


def _collect_local(root: Path, interesting: list[str], cutoff: datetime) -> RouteFiles:
    """Return {route_part: [(key, path, segment), ...]}."""
    by_route: RouteFiles = defaultdict(list)
    if not root.is_dir():
        print(f"Log root not found: {root}")
        return by_route

    for seg_dir in sorted(root.iterdir(), reverse=True):
        if not seg_dir.is_dir() or not _is_recent(seg_dir, cutoff):
            continue
        parsed = _parse_segment_dir(seg_dir.name)
        if parsed is None:
            continue
        route_part, segment = parsed
        _add_files_from_segment(by_route, seg_dir, interesting, route_part, segment)
    return by_route


def _server_has_file(files_resp: dict, fname: str, segment: int) -> bool:
    """True if the server response indicates this segment/file is already uploaded."""
    server_key = FILE_TO_SERVER_KEY.get(fname)
    if not server_key:
        return False
    urls = files_resp.get(server_key, [])
    # Arrays are sorted by segment number; index matches segment when present
    return isinstance(urls, list) and segment < len(urls) and bool(urls[segment])


async def _query_route_files(api: Api, dongle_id: str, route_part: str) -> dict | None:
    """Fetch the /files endpoint for one route. Returns the JSON or None on error."""
    route_name = f"{dongle_id}|{route_part}"
    endpoint = f"v1/route/{route_name}/files"
    try:
        resp = await asyncio.to_thread(
            api.get,
            endpoint,
            timeout=20,
            access_token=api.get_token(),
        )
    except Exception as e:
        print(f"  warning: could not query {route_name}: {e}")
        return None

    if resp.status_code == HTTP_NOT_FOUND:
        return {}
    if resp.status_code != HTTP_OK:
        print(f"  warning: {endpoint} returned HTTP {resp.status_code}")
        return None
    try:
        return resp.json()
    except Exception as e:
        print(f"  warning: bad JSON from {endpoint}: {e}")
        return None


async def _probe_upload_url(api: Api, dongle_id: str, key: str) -> bool:
    """Return True if the server already has this file (412)."""
    try:
        resp = await asyncio.to_thread(
            api.get,
            f"v1.4/{dongle_id}/upload_url/",
            timeout=15,
            path=key,
            access_token=api.get_token(),
        )
    except Exception:
        return False
    return resp.status_code == HTTP_PRECONDITION_FAILED


async def _probe_missing(
    api: Api,
    dongle_id: str,
    items: list[tuple[str, str, int]],
) -> list[tuple[str, str]]:
    """Probe each file with the device-authenticated upload_url endpoint."""
    sem = asyncio.Semaphore(8)

    async def check(item: tuple[str, str, int]) -> tuple[str, str] | None:
        key, path, _ = item
        async with sem:
            already = await _probe_upload_url(api, dongle_id, key)
            return None if already else (key, path)

    results = await asyncio.gather(*(check(it) for it in items))
    return [r for r in results if r is not None]


def _missing_for_route(
    files_resp: dict,
    items: list[tuple[str, str, int]],
) -> list[tuple[str, str]]:
    missing = []
    for key, path, segment in items:
        fname = key.rsplit("/", 1)[-1]
        if not _server_has_file(files_resp, fname, segment):
            missing.append((key, path))
    return missing


def _classify_results(
    by_route: RouteFiles,
    results: list[tuple[str, dict | None]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str, int]]]:
    """Split query results into already-known missing files and items that need probing."""
    missing: list[tuple[str, str]] = []
    need_probe: list[tuple[str, str, int]] = []
    for route_part, files_resp in results:
        items = by_route[route_part]
        if files_resp is None:
            print(f"  falling back to per-file probe for route {route_part}")
            need_probe.extend(items)
        else:
            missing.extend(_missing_for_route(files_resp, items))
    return missing, need_probe


async def _filter_by_server(
    api: Api,
    dongle_id: str,
    by_route: RouteFiles,
) -> list[tuple[str, str]]:
    """Query each route; fall back to per-file upload_url probes when needed."""
    if not by_route:
        return []

    sem = asyncio.Semaphore(3)

    async def bounded_query(route_part: str) -> tuple[str, dict | None]:
        async with sem:
            result = await _query_route_files(api, dongle_id, route_part)
            await asyncio.sleep(0.2)
            return route_part, result

    tasks = [bounded_query(rp) for rp in by_route]
    results = await asyncio.gather(*tasks)

    missing, need_probe = _classify_results(by_route, results)

    if need_probe:
        print(f"  probing {len(need_probe)} file(s) via upload_url...")
        missing.extend(await _probe_missing(api, dongle_id, need_probe))

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

    print(f"Querying server for {len(by_route)} route(s)...")
    return asyncio.run(_filter_by_server(api, dongle_id, by_route))


def _mark_uploaded(fn: str) -> None:
    with contextlib.suppress(OSError):
        setxattr(fn, UPLOAD_ATTR_NAME, UPLOAD_ATTR_VALUE)


def _handle_412(fn: str) -> bool:
    print("    skipped (412 - already exists or ignored)")
    _mark_uploaded(fn)
    return True


def _put_file(url: str, headers: dict, fn: str, key: str) -> bool:
    stream, size = get_upload_stream(fn, key.endswith(".zst") and not fn.endswith(".zst"))
    try:
        resp = requests.put(url, data=stream, headers=headers, timeout=60)
    finally:
        stream.close()

    if resp.status_code in SUCCESS_CODES:
        _mark_uploaded(fn)
        print(f"    success ({size / 1e6:.1f} MB)")
        return True

    print(f"    upload failed: HTTP {resp.status_code}")
    return False


def upload_file(api: Api, dongle_id: str, key: str, fn: str, dry_run: bool) -> bool:
    """Request upload URL and PUT the file. Returns True on success."""
    if dry_run:
        print(f"  [dry-run] would upload {key}")
        return True

    print(f"  Uploading {key} ...")
    try:
        url_resp = api.get(
            f"v1.4/{dongle_id}/upload_url/",
            timeout=15,
            path=key,
            access_token=api.get_token(),
        )
    except Exception as e:
        print(f"    error: {e}")
        return False

    if url_resp.status_code == HTTP_PRECONDITION_FAILED:
        return _handle_412(fn)

    if url_resp.status_code != HTTP_OK:
        print(f"    failed to get URL: HTTP {url_resp.status_code}")
        return False

    data = url_resp.json()
    return _put_file(data["url"], data.get("headers", {}), fn, key)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload recent unuploaded openpilot drives")
    parser.add_argument(
        "--days", type=int, default=14, help="How many recent days to scan (default: 14)"
    )
    parser.add_argument("--dry-run", action="store_true", help="List files but do not upload")
    parser.add_argument(
        "--include-dcamera", action="store_true", help="Also upload dcamera.hevc if present"
    )
    return parser.parse_args()


def _get_dongle_id() -> str:
    params = Params()
    raw = params.get("DongleId")
    if raw is None:
        print("Error: Device not registered (no DongleId)")
        sys.exit(1)
    # Newer openpilot returns str; older versions may return bytes
    dongle_id = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    if not dongle_id or dongle_id == "UnregisteredDevice":
        print("Error: Device not registered (no DongleId)")
        sys.exit(1)
    return dongle_id


def _print_files(files: list[tuple[str, str]]) -> None:
    print(f"Found {len(files)} file(s) to upload:")
    for key, _ in files:
        print(f"  {key}")


def _do_uploads(api: Api, dongle_id: str, files: list[tuple[str, str]], dry_run: bool) -> int:
    success = 0
    for key, fn in files:
        if upload_file(api, dongle_id, key, fn, dry_run):
            success += 1
        if not dry_run:
            time.sleep(0.5)
    return success


def main() -> None:
    args = _parse_args()
    dongle_id = _get_dongle_id()
    root = Path(Paths.log_root())

    print(f"Dongle ID: {dongle_id}")
    print(f"Log root:  {root}")
    print(f"Scanning last {args.days} days...")

    api = Api(dongle_id)
    files = find_unuploaded(api, dongle_id, root, args.days, args.include_dcamera)
    if not files:
        print("No unuploaded files found.")
        return

    _print_files(files)
    print()

    success = _do_uploads(api, dongle_id, files, args.dry_run)

    if args.dry_run:
        print(f"\nDry run - nothing uploaded ({success}/{len(files)} files).")
    else:
        print(f"\nDone. {success}/{len(files)} uploaded successfully.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Upload recent unuploaded drives from a Comma 3X (or similar openpilot device).

Run this on the device (via SSH) while it is connected to Wi-Fi.
Requires the openpilot Python environment (PYTHONPATH=/data/openpilot or run from the openpilot venv).

Usage:
    python3 upload-new-drives.py [--days 7] [--dry-run] [--include-dcamera]
"""

import argparse
import contextlib
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

try:
    from openpilot.common.api import Api
    from openpilot.common.hardware.hw import Paths
    from openpilot.common.params import Params
    from openpilot.common.utils import get_upload_stream
    from openpilot.system.loggerd.xattr_cache import getxattr, setxattr
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
SUCCESS_CODES = {HTTP_OK, HTTP_CREATED}

# Files we care about for a "full" upload (qlogs/qcameras are usually already uploaded)
DEFAULT_FILES = (
    "rlog",
    "rlog.zst",
    "fcamera.hevc",
    "ecamera.hevc",
)

DCAMERA = "dcamera.hevc"


def is_uploaded(path: str) -> bool:
    try:
        return getxattr(path, UPLOAD_ATTR_NAME) == UPLOAD_ATTR_VALUE
    except OSError:
        return False


def _is_recent(path: Path, cutoff: datetime) -> bool:
    """True if the path's mtime is newer than the cutoff."""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return False
    return mtime >= cutoff


def _collect_from_segment(seg_dir: Path, interesting: list[str]) -> list[tuple[str, str]]:
    """Look for interesting files directly inside a segment directory."""
    found = []
    # Relative key used by the API: route/segment/filename
    # Segment dir name is already "route--segment"
    parts = seg_dir.name.rsplit("--", 1)
    if len(parts) != 2:
        return found
    route, segment = parts
    for fname in interesting:
        fpath = seg_dir / fname
        if fpath.is_file() and not is_uploaded(str(fpath)):
            rel = f"{route}/{segment}/{fname}"
            found.append((rel, str(fpath)))
    return found


def find_unuploaded(root: Path, max_days: int, include_dcamera: bool) -> list[tuple[str, str]]:
    """Return list of (relative_key, absolute_path) for files that need uploading."""
    if not root.is_dir():
        print(f"Log root not found: {root}")
        return []

    cutoff = datetime.now() - timedelta(days=max_days)
    interesting = list(DEFAULT_FILES)
    if include_dcamera:
        interesting.append(DCAMERA)

    results: list[tuple[str, str]] = []
    # realdata layout is flat: each child is a segment directory named route--segment
    for seg_dir in sorted(root.iterdir(), reverse=True):
        if not seg_dir.is_dir():
            continue
        if not _is_recent(seg_dir, cutoff):
            continue
        results.extend(_collect_from_segment(seg_dir, interesting))

    return results


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


def main() -> None:
    args = _parse_args()
    dongle_id = _get_dongle_id()
    root = Path(Paths.log_root())

    print(f"Dongle ID: {dongle_id}")
    print(f"Log root:  {root}")
    print(f"Scanning last {args.days} days...")

    files = find_unuploaded(root, args.days, args.include_dcamera)
    if not files:
        print("No unuploaded files found.")
        return

    _print_files(files)
    if args.dry_run:
        print("\nDry run - nothing uploaded.")
        return

    api = Api(dongle_id)
    success = 0
    for key, fn in files:
        if upload_file(api, dongle_id, key, fn, args.dry_run):
            success += 1
        time.sleep(0.5)

    print(f"\nDone. {success}/{len(files)} uploaded successfully.")


if __name__ == "__main__":
    main()

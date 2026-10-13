#!/usr/bin/env python3
"""
Upload recent unuploaded drives from a Comma 3X (or similar openpilot device).

Run this on the device (via SSH) while it is connected to Wi-Fi.
Requires the openpilot Python environment (PYTHONPATH=/data/openpilot or run from the openpilot venv).

Usage:
    python3 upload-new-drives.py [--days 7] [--dry-run] [--include-dcamera] [--verbose]
"""

import argparse
import asyncio
import contextlib
import logging
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

SEGMENT_PARTS = 2
PROBE_TIMEOUT = 20
PROBE_CONCURRENCY = 2
PROBE_RETRIES = 3
PROBE_BACKOFF = 2.0  # seconds, exponential
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

log = logging.getLogger("upload-new-drives")


def _setup_logging(verbose: bool) -> None:
    """Always log verbosely to a file; optionally also to stderr."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()

    file_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
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


def _parse_segment_dir(name: str) -> bool:
    """True if the directory name looks like a segment (route--N)."""
    parts = name.rsplit("--", 1)
    return len(parts) == SEGMENT_PARTS and parts[1].isdigit()


def _has_upload_xattr(path: str) -> bool:
    """True if the file is already marked as uploaded via xattr."""
    try:
        return getxattr(path, UPLOAD_ATTR_NAME) == UPLOAD_ATTR_VALUE
    except OSError:
        return False


def _files_in_segment(seg_dir: Path, interesting: list[str]) -> list[tuple[str, str]]:
    found = []
    for fname in interesting:
        fpath = seg_dir / fname
        if fpath.is_file():
            found.append((f"{seg_dir.name}/{fname}", str(fpath)))
    return found


def _collect_local(root: Path, interesting: list[str], cutoff: datetime) -> list[tuple[str, str]]:
    """Return list of (relative_key, absolute_path) for recent interesting files that are not locally marked uploaded."""
    if not root.is_dir():
        log.warning("Log root not found: %s", root)
        return []

    found: list[tuple[str, str]] = []
    skipped_xattr = 0
    for seg_dir in sorted(root.iterdir(), reverse=True):
        if not seg_dir.is_dir() or not _is_recent(seg_dir, cutoff):
            continue
        if not _parse_segment_dir(seg_dir.name):
            continue
        for key, path in _files_in_segment(seg_dir, interesting):
            if _has_upload_xattr(path):
                skipped_xattr += 1
                continue
            found.append((key, path))
    log.debug(
        "collected %d local candidate file(s) (%d already marked via xattr)",
        len(found),
        skipped_xattr,
    )
    return found


async def _probe_one(api: Api, dongle_id: str, key: str) -> bool:
    """Return True if the server already has this file (HTTP 412).

    Retries on timeout / 503 with exponential backoff.
    """
    delay = PROBE_BACKOFF
    for attempt in range(PROBE_RETRIES):
        t0 = time.monotonic()
        try:
            resp = await asyncio.to_thread(
                api.get,
                f"v1.4/{dongle_id}/upload_url/",
                timeout=PROBE_TIMEOUT,
                path=key,
                access_token=api.get_token(),
            )
        except requests.RequestException as e:
            elapsed = time.monotonic() - t0
            log.debug("probe error %s attempt %d (%.1fs): %s", key, attempt + 1, elapsed, e)
            if attempt + 1 < PROBE_RETRIES:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            return False  # give up, treat as missing

        elapsed = time.monotonic() - t0
        if resp.status_code == HTTP_PRECONDITION_FAILED:
            log.debug("probe %s -> 412 (already uploaded, %.1fs)", key, elapsed)
            return True
        if resp.status_code in (HTTP_OK, 503, 429):
            # 200 = needs upload; 503/429 = temporary, retry
            if resp.status_code == HTTP_OK:
                log.debug("probe %s -> %s (needs upload, %.1fs)", key, resp.status_code, elapsed)
                return False
            log.debug("probe %s -> %s attempt %d (%.1fs), retrying", key, resp.status_code, attempt + 1, elapsed)
            if attempt + 1 < PROBE_RETRIES:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            return False  # treat as missing after retries
        # other status: log and treat as missing
        log.debug("probe %s -> %s (%.1fs)", key, resp.status_code, elapsed)
        return False
    return False


async def _check_one(
    sem: asyncio.Semaphore,
    api: Api,
    dongle_id: str,
    item: tuple[str, str],
    progress: dict[str, int],
) -> tuple[str, str] | None:
    key, path = item
    async with sem:
        already = await _probe_one(api, dongle_id, key)
        if already:
            _mark_uploaded(path)
        progress["done"] += 1
        done = progress["done"]
        total = progress["total"]
        if done % PROGRESS_EVERY == 0 or done == total:
            log.info("progress: %d/%d probed", done, total)
        return None if already else (key, path)


async def _filter_present(
    api: Api,
    dongle_id: str,
    candidates: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Keep only files the server does not already have.

    Uses the device-auth upload_url endpoint (412 = already present).
    """
    if not candidates:
        return []

    log.info(
        "probing %d file(s) with upload_url (concurrency=%d, retries=%d)",
        len(candidates),
        PROBE_CONCURRENCY,
        PROBE_RETRIES,
    )
    sem = asyncio.Semaphore(PROBE_CONCURRENCY)
    progress = {"done": 0, "total": len(candidates)}

    tasks = [_check_one(sem, api, dongle_id, it, progress) for it in candidates]
    results = await asyncio.gather(*tasks)
    missing = [r for r in results if r is not None]
    log.info("probe finished: %d missing out of %d", len(missing), len(candidates))
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

    candidates = _collect_local(root, interesting, cutoff)
    if not candidates:
        return []

    return asyncio.run(_filter_present(api, dongle_id, candidates))


def _mark_uploaded(fn: str) -> None:
    with contextlib.suppress(OSError):
        setxattr(fn, UPLOAD_ATTR_NAME, UPLOAD_ATTR_VALUE)


def _handle_412(fn: str) -> bool:
    log.info("skipped 412: %s", fn)
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
    parser = argparse.ArgumentParser(description="Upload recent unuploaded openpilot drives")
    parser.add_argument(
        "--days", type=int, default=14, help="How many recent days to scan (default: 14)"
    )
    parser.add_argument("--dry-run", action="store_true", help="List files but do not upload")
    parser.add_argument(
        "--include-dcamera", action="store_true", help="Also upload dcamera.hevc if present"
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

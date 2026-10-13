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
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote

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
PROBE_TIMEOUT = 30
BATCH_SIZE = 40
BATCH_INTERVAL = 3.0  # seconds between batch calls
SINGLE_INTERVAL = 1.0  # seconds between single probes
PROBE_RETRIES = 4
PROBE_BACKOFF = 5.0  # base seconds, exponential

LOG_DIR = Path.home() / ".local" / "state" / "upload-new-drives"
LOG_FILE = LOG_DIR / "debug.log"

DEFAULT_FILES = (
    "rlog",
    "rlog.zst",
    "fcamera.hevc",
    "ecamera.hevc",
)

DCAMERA = "dcamera.hevc"

log = logging.getLogger("upload-new-drives")


def _setup_logging(verbose: bool) -> None:
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
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return False
    return mtime >= cutoff


def _parse_segment_dir(name: str) -> bool:
    parts = name.rsplit("--", 1)
    return len(parts) == SEGMENT_PARTS and parts[1].isdigit()


def _has_upload_xattr(path: str) -> bool:
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


def _path_from_url(url: str) -> str | None:
    """Best-effort extract of the original relative path from a signed URL."""
    try:
        decoded = unquote(url)
        # typical: .../dongle/route--N/filename?...
        parts = decoded.split("/")
        for i, p in enumerate(parts):
            if "--" in p and i + 1 < len(parts):
                return f"{p}/{parts[i + 1].split('?')[0]}"
    except (ValueError, IndexError, TypeError):
        pass
    return None


def _batch_probe(
    api: Api, dongle_id: str, items: list[tuple[str, str]]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]] | None:
    """Try one batch call. Returns (missing, already) or None on failure."""
    paths = [k for k, _ in items]
    url = f"https://api.commadotai.com/v1/{dongle_id}/upload_urls/"
    headers = {
        "Authorization": "JWT " + api.get_token(),
        "Content-Type": "application/json",
        "User-Agent": "upload-new-drives",
    }
    body = {"paths": paths, "expiry_days": 1}

    delay = PROBE_BACKOFF
    for attempt in range(PROBE_RETRIES):
        t0 = time.monotonic()
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=PROBE_TIMEOUT)
        except requests.RequestException as e:
            elapsed = time.monotonic() - t0
            log.debug("batch error attempt %d (%.1fs): %s", attempt + 1, elapsed, e)
            if attempt + 1 < PROBE_RETRIES:
                time.sleep(delay)
                delay *= 2
                continue
            return None

        elapsed = time.monotonic() - t0
        if resp.status_code != HTTP_OK:
            log.debug("batch -> HTTP %s attempt %d (%.1fs)", resp.status_code, attempt + 1, elapsed)
            if resp.status_code in (503, 429) and attempt + 1 < PROBE_RETRIES:
                time.sleep(delay)
                delay *= 2
                continue
            return None

        try:
            data = resp.json()
        except (ValueError, requests.JSONDecodeError) as e:
            log.debug("batch bad JSON: %s", e)
            return None

        if not isinstance(data, list):
            log.debug("batch unexpected response type")
            return None

        returned_paths = set()
        for item in data:
            if isinstance(item, dict) and "url" in item:
                p = _path_from_url(item["url"])
                if p:
                    returned_paths.add(p)

        missing = []
        already = []
        for key, path in items:
            if key in returned_paths:
                missing.append((key, path))
            else:
                already.append((key, path))

        log.debug(
            "batch of %d: %d need upload, %d already present (%.1fs)",
            len(items),
            len(missing),
            len(already),
            elapsed,
        )
        return missing, already

    return None


def _single_probe(api: Api, dongle_id: str, key: str) -> bool:
    """Return True if already uploaded (412)."""
    delay = PROBE_BACKOFF
    for attempt in range(PROBE_RETRIES):
        t0 = time.monotonic()
        try:
            resp = api.get(
                f"v1.4/{dongle_id}/upload_url/",
                timeout=PROBE_TIMEOUT,
                path=key,
                access_token=api.get_token(),
            )
        except requests.RequestException as e:
            elapsed = time.monotonic() - t0
            log.debug("single probe error %s attempt %d (%.1fs): %s", key, attempt + 1, elapsed, e)
            if attempt + 1 < PROBE_RETRIES:
                time.sleep(delay)
                delay *= 2
                continue
            return False

        elapsed = time.monotonic() - t0
        if resp.status_code == HTTP_PRECONDITION_FAILED:
            log.debug("single probe %s -> 412 (%.1fs)", key, elapsed)
            return True
        if resp.status_code == HTTP_OK:
            log.debug("single probe %s -> 200 (%.1fs)", key, elapsed)
            return False
        if resp.status_code in (503, 429) and attempt + 1 < PROBE_RETRIES:
            log.debug(
                "single probe %s -> %s attempt %d (%.1fs), retrying",
                key,
                resp.status_code,
                attempt + 1,
                elapsed,
            )
            time.sleep(delay)
            delay *= 2
            continue
        log.debug("single probe %s -> %s (%.1fs)", key, resp.status_code, elapsed)
        return False
    return False


def _filter_present(api: Api, dongle_id: str, candidates: list[tuple[str, str]]) -> list[tuple[str, str]]:
    if not candidates:
        return []

    log.info("probing %d file(s) via batch upload_urls (batch_size=%d)", len(candidates), BATCH_SIZE)
    missing: list[tuple[str, str]] = []
    already_count = 0

    for i in range(0, len(candidates), BATCH_SIZE):
        chunk = candidates[i : i + BATCH_SIZE]
        if i > 0:
            time.sleep(BATCH_INTERVAL)

        result = _batch_probe(api, dongle_id, chunk)
        if result is None:
            log.warning("batch failed for %d files, falling back to single probes", len(chunk))
            for key, path in chunk:
                time.sleep(SINGLE_INTERVAL)
                if _single_probe(api, dongle_id, key):
                    _mark_uploaded(path)
                    already_count += 1
                else:
                    missing.append((key, path))
        else:
            chunk_missing, chunk_already = result
            missing.extend(chunk_missing)
            for _, path in chunk_already:
                _mark_uploaded(path)
                already_count += 1

        log.info(
            "progress: %d/%d checked (%d already, %d missing so far)",
            min(i + BATCH_SIZE, len(candidates)),
            len(candidates),
            already_count,
            len(missing),
        )

    log.info("probe finished: %d missing out of %d", len(missing), len(candidates))
    return missing


def find_unuploaded(
    api: Api,
    dongle_id: str,
    root: Path,
    max_days: int,
    include_dcamera: bool,
) -> list[tuple[str, str]]:
    cutoff = datetime.now() - timedelta(days=max_days)
    interesting = list(DEFAULT_FILES)
    if include_dcamera:
        interesting.append(DCAMERA)

    candidates = _collect_local(root, interesting, cutoff)
    if not candidates:
        return []

    return _filter_present(api, dongle_id, candidates)


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
    parser.add_argument("--days", type=int, default=14, help="How many recent days to scan (default: 14)")
    parser.add_argument("--dry-run", action="store_true", help="List files but do not upload")
    parser.add_argument("--include-dcamera", action="store_true", help="Also upload dcamera.hevc if present")
    parser.add_argument("-v", "--verbose", action="store_true", help="Also log progress to stderr")
    return parser.parse_args()


def _get_dongle_id() -> str:
    params = Params()
    raw = params.get("DongleId")
    if raw is None:
        log.error("Device not registered (no DongleId)")
        sys.exit(1)
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

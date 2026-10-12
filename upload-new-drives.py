#!/usr/bin/env python3
"""
Upload recent unuploaded drives from a Comma 3X (or similar openpilot device).

Run this on the device (via SSH) while it is connected to Wi-Fi.
Requires the openpilot Python environment (PYTHONPATH=/data/openpilot or run from the openpilot venv).

Usage:
  python3 upload-new-drives.py [--days 7] [--dry-run] [--include-dcamera]
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
  from openpilot.common.api import Api
  from openpilot.common.params import Params
  from openpilot.common.utils import get_upload_stream
  from openpilot.system.loggerd.xattr_cache import getxattr, setxattr
  from openpilot.system.hardware.hw import Paths
except ImportError:
  print("Error: Could not import openpilot modules.")
  print("Run this script on the Comma device with PYTHONPATH=/data/openpilot")
  print("  e.g. PYTHONPATH=/data/openpilot python3 upload-new-drives.py")
  sys.exit(1)

UPLOAD_ATTR_NAME = "user.upload"
UPLOAD_ATTR_VALUE = b"1"

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


def find_unuploaded(root: Path, max_days: int, include_dcamera: bool) -> list[tuple[str, str]]:
  """Return list of (relative_key, absolute_path) for files that need uploading."""
  cutoff = datetime.now() - timedelta(days=max_days)
  interesting = list(DEFAULT_FILES)
  if include_dcamera:
    interesting.append(DCAMERA)

  results = []
  if not root.is_dir():
    print(f"Log root not found: {root}")
    return results

  for route_dir in sorted(root.iterdir(), reverse=True):
    if not route_dir.is_dir() or not route_dir.name[0].isdigit():
      continue

    # Rough date check from route name (YYYY-MM-DD--...)
    try:
      route_date = datetime.strptime(route_dir.name[:10], "%Y-%m-%d")
      if route_date < cutoff:
        continue
    except ValueError:
      pass  # keep scanning if name doesn't match

    for seg_dir in sorted(route_dir.iterdir()):
      if not seg_dir.is_dir():
        continue
      for fname in interesting:
        fpath = seg_dir / fname
        if fpath.is_file() and not is_uploaded(str(fpath)):
          rel = f"{route_dir.name}/{seg_dir.name}/{fname}"
          results.append((rel, str(fpath)))

  return results


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
    if url_resp.status_code == 412:
      print(f"    skipped (412 - already exists or ignored)")
      # Still mark as uploaded so we don't retry forever
      try:
        setxattr(fn, UPLOAD_ATTR_NAME, UPLOAD_ATTR_VALUE)
      except OSError:
        pass
      return True

    if url_resp.status_code != 200:
      print(f"    failed to get URL: HTTP {url_resp.status_code}")
      return False

    data = url_resp.json()
    url = data["url"]
    headers = data.get("headers", {})

    stream, size = get_upload_stream(fn, key.endswith(".zst") and not fn.endswith(".zst"))
    try:
      resp = requests.put(url, data=stream, headers=headers, timeout=60)
    finally:
      stream.close()

    if resp.status_code in (200, 201):
      try:
        setxattr(fn, UPLOAD_ATTR_NAME, UPLOAD_ATTR_VALUE)
      except OSError as e:
        print(f"    uploaded but failed to set xattr: {e}")
      print(f"    success ({size / 1e6:.1f} MB)")
      return True
    else:
      print(f"    upload failed: HTTP {resp.status_code}")
      return False

  except Exception as e:
    print(f"    error: {e}")
    return False


def main():
  parser = argparse.ArgumentParser(description="Upload recent unuploaded openpilot drives")
  parser.add_argument("--days", type=int, default=14, help="How many recent days to scan (default: 14)")
  parser.add_argument("--dry-run", action="store_true", help="List files but do not upload")
  parser.add_argument("--include-dcamera", action="store_true", help="Also upload dcamera.hevc if present")
  args = parser.parse_args()

  params = Params()
  dongle_id = params.get("DongleId", encoding="utf-8")
  if not dongle_id or dongle_id == "UnregisteredDevice":
    print("Error: Device not registered (no DongleId)")
    sys.exit(1)

  root = Path(Paths.log_root())
  print(f"Dongle ID: {dongle_id}")
  print(f"Log root:  {root}")
  print(f"Scanning last {args.days} days...")

  files = find_unuploaded(root, args.days, args.include_dcamera)
  if not files:
    print("No unuploaded files found.")
    return

  print(f"Found {len(files)} file(s) to upload:")
  for key, _ in files:
    print(f"  {key}")

  if args.dry_run:
    print("\nDry run - nothing uploaded.")
    return

  api = Api(dongle_id)
  success = 0
  for key, fn in files:
    if upload_file(api, dongle_id, key, fn, args.dry_run):
      success += 1
    time.sleep(0.5)  # be nice to the API

  print(f"\nDone. {success}/{len(files)} uploaded successfully.")


if __name__ == "__main__":
  import requests  # imported late so the script can show the import error first
  main()

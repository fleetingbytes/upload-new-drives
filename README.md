# upload-new-drives

Upload recent driving sessions from a Comma 3X (or other openpilot device) that have not yet been fully uploaded to comma Connect.

## Why this exists

The background uploader on the device only automatically uploads small files (qlog, qcamera). Full logs (`rlog`) and high-resolution camera videos require an explicit request via the Connect web UI. This script automates that request for recent drives.

## Requirements

- Run **on the Comma device** (via SSH) while it is connected to Wi-Fi.
- The openpilot Python environment must be available:

```bash
export PYTHONPATH=/data/openpilot
python3 upload-new-drives.py
```

(or activate the openpilot virtualenv if you use one).

## Usage

```bash
# Upload everything from the last 14 days (default)
PYTHONPATH=/data/openpilot python3 upload-new-drives.py

# Only last 7 days
PYTHONPATH=/data/openpilot python3 upload-new-drives.py --days 7

# See what would be uploaded without actually uploading
PYTHONPATH=/data/openpilot python3 upload-new-drives.py --dry-run

# Also include the driver-facing camera (if you have it enabled)
PYTHONPATH=/data/openpilot python3 upload-new-drives.py --include-dcamera
```

## How it works

1. Scans `/data/media/0/realdata/` for recent segments.
2. Skips any file that already has the `user.upload` extended attribute set (local fast path).
3. For the remaining files, sends them in small batches to `POST /v1/{dongle}/upload_urls/` (device-auth compatible). Paths that come back with a signed URL need uploading; the others are already present and get the xattr marked.
4. If a batch fails, it falls back to sequential single `GET /v1.4/.../upload_url/` probes (412 = already present) with delays and exponential backoff.
5. Missing files are uploaded via a signed URL and PUT. On success the `user.upload` attribute is set.

The batch approach reduces the number of API calls by ~40× compared with per-file probes, which helps stay under the server's rate limits.

## Notes

- Large files (especially camera videos) can take a long time. Leave the device on Wi-Fi and charging.
- You can safely re-run it; already-uploaded files (local xattr or server confirmation) are skipped.
- The first run on a large backlog may still take a minute or two for the probe phase; subsequent runs are much faster.

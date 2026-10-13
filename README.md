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
3. For the remaining files, probes the server with `GET /v1.4/{dongle}/upload_url/?path=...` (the same endpoint the official uploader uses). HTTP 412 means the file is already present; the script marks the xattr and skips it.
4. Probes run with low concurrency (2) and automatic retries + exponential backoff on timeouts and 503s so they stay polite to the server.
5. Missing files are uploaded via a signed URL and PUT. On success the `user.upload` attribute is set.

Note: the Connect web UI’s route `/files` endpoint requires a user JWT, not the device JWT, so it cannot be used from the device itself. The upload_url 412 check is the device-compatible equivalent.

## Notes

- Large files (especially camera videos) can take a long time. Leave the device on Wi-Fi and charging.
- You can safely re-run it; already-uploaded files (local xattr or server 412) are skipped.
- The first run on a large backlog may take a few minutes for the probe phase; subsequent runs are much faster thanks to the xattr.

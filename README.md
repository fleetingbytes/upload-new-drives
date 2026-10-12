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

1. Scans `/data/media/0/realdata/` for recent routes.
2. Checks the `user.upload` extended attribute on `rlog`, `fcamera.hevc`, `ecamera.hevc` (and optionally `dcamera.hevc`).
3. For files missing the attribute, requests a signed upload URL via the device's own API credentials and PUTs the file.
4. On success, sets the `user.upload` attribute so the file is not retried.

## Notes

- Large files (especially camera videos) can take a long time. Leave the device on Wi-Fi and charging.
- The script is intentionally simple and sequential. If you have dozens of segments it may take a while.
- You can safely re-run it; already-uploaded files are skipped.

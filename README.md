# Archived

Legacy project for automatically uploading recent drive logs from a Comma 3X device. New project is upcoming.

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
3. Probes the remaining files one-by-one with `GET /v1.4/{dongle}/upload_url/` (device-auth compatible). A 412 means already present (xattr is set); a 200 means it needs uploading.
4. Probes are spaced 2.5 s apart. If the server returns too many consecutive 503s the script stops early so it does not interfere with normal web UI use. Unchecked files stay unmarked and will be tried on the next run.
5. Missing files are uploaded via a signed URL and PUT. On success the `user.upload` attribute is set.

The approach is deliberately slow and polite so it stays under the server's rate limits and does not disrupt the Connect web UI.

## Notes

- Large files (especially camera videos) can take a long time. Leave the device on Wi-Fi and charging.
- You can safely re-run it; already-confirmed files are skipped via the local xattr.
- If the probe phase stops early because of 503s, wait a few minutes and run it again — progress is saved.

#!/usr/bin/env bash
# Nightly backup: a consistent SQLite snapshot + config/policies/prompts (scripts/backup.py),
# kept 7 nights on the VM and copied to gs://$NOESIS_GCS_BUCKET/backups/ when that is set.
# Secrets (.env) are never included. The VM's service account needs write access to the bucket.
set -euo pipefail
cd /opt/noesis
DEST="${NOESIS_DATA_DIR:-/var/lib/noesis}/backups"
archive="$(/opt/noesis/.venv/bin/python scripts/backup.py --dest "$DEST" --keep 7)"
echo "local backup: $archive"
if [[ -n "${NOESIS_GCS_BUCKET:-}" ]]; then
  if command -v gcloud >/dev/null 2>&1; then
    gcloud storage cp --quiet "$archive" "gs://${NOESIS_GCS_BUCKET#gs://}/backups/"
  else
    gsutil -q cp "$archive" "gs://${NOESIS_GCS_BUCKET#gs://}/backups/"
  fi
  echo "copied to gs://${NOESIS_GCS_BUCKET#gs://}/backups/$(basename "$archive")"
else
  echo "NOESIS_GCS_BUCKET not set: kept on the VM only"
fi

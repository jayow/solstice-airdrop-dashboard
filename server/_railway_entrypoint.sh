#!/bin/bash
# Railway container entrypoint: prep the volume-mounted slim DB, run the
# daily refresh, publish the artifact to a GH release, checkpoint WAL.
#
# Env vars (set in the Railway dashboard):
#   HELIUS_ENDPOINTS  comma-separated RPC URLs (used by src/.../rpc_helper.py)
#   HELIUS_URL        single fallback RPC URL (optional)
#   GITHUB_TOKEN      PAT with read access to `slim-cache` release and
#                     write access to `daily-results-*` releases
#   GH_REPO           owner/name for the gh release calls (e.g. jay/solstice)
#
# Does NOT auto-push to git. Daily totals get uploaded to a GH release;
# the user reviews and merges/pushes on their own clock.
set -euo pipefail

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

# ── Fix 3 (Exponent learnings): wait for the volume to actually mount ─
# Railway occasionally starts the container before the volume FS is ready.
# Poll a touch-probe rather than assuming `-d` is enough.
log "waiting for /mnt/data volume mount"
until [ -d /mnt/data ] && touch /mnt/data/.mount_probe 2>/dev/null; do
  echo "waiting for volume"
  sleep 1
done
rm -f /mnt/data/.mount_probe
log "volume mounted"

# ── DB placement ─────────────────────────────────────────────────────
# The app reads data/solstice.db relative to /app (hardcoded in
# src/flares_estimator/db.py). We keep the actual DB on the volume and
# symlink it into /app/data/ so no code changes are required.
export SOLSTICE_DB="${SOLSTICE_DB:-/mnt/data/solstice.slim.db}"
mkdir -p /app/data
ln -sfn "$SOLSTICE_DB" /app/data/solstice.db

# ── Seed the DB from the slim-cache release if missing ───────────────
if [ ! -f "$SOLSTICE_DB" ]; then
  log "no DB at $SOLSTICE_DB — downloading from GH release slim-cache"
  if [ -z "${GITHUB_TOKEN:-}" ] || [ -z "${GH_REPO:-}" ]; then
    echo "FATAL: GITHUB_TOKEN and GH_REPO must be set to seed the DB." >&2
    exit 1
  fi
  export GH_TOKEN="$GITHUB_TOKEN"
  if ! gh release download slim-cache \
        --repo "$GH_REPO" \
        --pattern 'solstice.slim.db.gz' \
        --output - \
        | gunzip > "$SOLSTICE_DB"; then
    echo "FATAL: failed to download/unpack slim-cache release." >&2
    rm -f "$SOLSTICE_DB"
    exit 1
  fi
  log "seeded DB ($(stat -c %s "$SOLSTICE_DB" 2>/dev/null || stat -f %z "$SOLSTICE_DB") bytes)"
fi

# ── Integrity check; delete + bail on corruption so next run re-seeds ─
log "running integrity_check on $SOLSTICE_DB"
if ! python3 -c "
import sqlite3, sys
c = sqlite3.connect('$SOLSTICE_DB')
r = c.execute('PRAGMA integrity_check').fetchone()[0]
print('integrity:', r)
sys.exit(0 if r == 'ok' else 1)
"; then
  echo "FATAL: integrity_check failed — deleting DB so next run reseeds." >&2
  rm -f "$SOLSTICE_DB"
  exit 1
fi

# ── Run the refresh ──────────────────────────────────────────────────
cd /app
log "starting server/refresh.sh"
bash server/refresh.sh
log "refresh.sh done"

# ── Publish daily_totals.json to a dated GH release ──────────────────
# Tiny file; safe to upload every day. Larger artifacts (data.json,
# solstice.slim.db.gz) are NOT uploaded here — leave that to the user
# via tools/build_slim_db.sh + manual release.
TODAY=$(date -u +%F)
TAG="daily-results-$TODAY"
ARTIFACT="/app/server/daily_totals.json"
if [ -f "$ARTIFACT" ] && [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GH_REPO:-}" ]; then
  export GH_TOKEN="$GITHUB_TOKEN"
  log "publishing $ARTIFACT to release $TAG"
  if ! gh release view "$TAG" --repo "$GH_REPO" >/dev/null 2>&1; then
    gh release create "$TAG" --repo "$GH_REPO" \
      --title "Daily totals $TODAY" \
      --notes "Automated daily refresh artifact." \
      "$ARTIFACT" || log "WARN: release create failed"
  else
    gh release upload "$TAG" --repo "$GH_REPO" --clobber "$ARTIFACT" \
      || log "WARN: release upload failed"
  fi
else
  log "skipping release upload (artifact missing or GH env not set)"
fi

# ── Fix 5 (Exponent learnings): truncate WAL so next start is fast ───
log "WAL checkpoint(TRUNCATE)"
python3 -c "
import sqlite3
sqlite3.connect('$SOLSTICE_DB').execute('PRAGMA wal_checkpoint(TRUNCATE)')
"

log "entrypoint complete"

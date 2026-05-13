#!/bin/bash
# One-shot: gzip the local SQLite DB and upload as a GitHub Release asset
# (release tag `db-cache`). The daily-refresh workflow downloads from there
# on first run / cache miss, skipping the slow from-scratch rebuild.
#
# Usage:
#   bash tools/upload_db_to_release.sh
#
# Requirements: `gh` CLI authenticated (run `gh auth login` first if needed).
set -e
cd "$(dirname "$0")/.."

DB=data/solstice.db
TAG=db-cache

if [ ! -f "$DB" ]; then
  echo "ERROR: $DB not found"
  exit 1
fi

echo "Compressing $DB (this may take a minute)..."
gzip -k -f "$DB"
SIZE=$(du -h "$DB.gz" | cut -f1)
echo "  → $DB.gz ($SIZE)"

# Create the release if it doesn't exist, then upload (replacing existing asset)
if gh release view "$TAG" --json tagName 2>/dev/null >/dev/null; then
  echo "Release '$TAG' exists — replacing asset..."
  gh release upload "$TAG" "$DB.gz" --clobber
else
  echo "Creating release '$TAG'..."
  gh release create "$TAG" "$DB.gz" \
    --title "DB cache for CI refresh" \
    --notes "Compressed SQLite DB snapshot used as CI cache seed. Replaced periodically." \
    --prerelease
fi

echo
echo "Done. Next CI run will download this asset on cache miss."
echo "To trigger a refresh now:"
echo "  gh workflow run 'Daily S2 refresh'"

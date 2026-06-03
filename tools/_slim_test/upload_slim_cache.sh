#!/bin/bash
# Upload the slim DB to a GitHub release so Railway cold-starts can fetch it
# without depending on the Mac being online.
#
# The release tag (default `slim-cache`) holds two assets:
#   - solstice.slim.db.gz                (the gzipped slim DB)
#   - solstice.slim.db.gz.manifest.json  (sha256 + size + uploaded_at + git commit)
#
# Railway's entrypoint downloads both, verifies sha256 against the manifest,
# then gunzips into place.
#
# Usage:
#   bash tools/_slim_test/upload_slim_cache.sh
#   bash tools/_slim_test/upload_slim_cache.sh --src data/solstice.slim.db.gz
#   bash tools/_slim_test/upload_slim_cache.sh --release slim-cache --dry-run
set -euo pipefail
cd "$(dirname "$0")/../.."

SRC="data/solstice.slim.db.gz"
RELEASE="slim-cache"
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --src)     SRC="$2"; shift 2 ;;
    --release) RELEASE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

ASSET_NAME="solstice.slim.db.gz"
MANIFEST_NAME="solstice.slim.db.gz.manifest.json"

run() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "  [dry-run] $*"
  else
    eval "$@"
  fi
}

# --- 1. Preflight: gh CLI installed + authenticated ---
if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: gh CLI not installed. brew install gh" >&2
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "ERROR: gh not authenticated. Run: gh auth login" >&2
  exit 1
fi

# --- 2. Ensure source exists (build if missing) ---
if [ ! -f "$SRC" ]; then
  echo "Source $SRC not found — running tools/build_slim_db.sh ..."
  if ! bash tools/build_slim_db.sh; then
    echo "ERROR: tools/build_slim_db.sh failed; cannot upload" >&2
    exit 1
  fi
  if [ ! -f "$SRC" ]; then
    echo "ERROR: build_slim_db.sh ran but $SRC still missing" >&2
    exit 1
  fi
fi

# --- 3. Compute sha256 + size (mac/linux cross-compat) ---
SHA256=$(shasum -a 256 "$SRC" | awk '{print $1}')
if stat -f %z "$SRC" >/dev/null 2>&1; then
  SIZE=$(stat -f %z "$SRC")      # mac
else
  SIZE=$(stat -c %s "$SRC")      # linux
fi
UPLOADED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
GIT_COMMIT=$(git rev-parse HEAD)

echo "Source:       $SRC"
echo "Release tag:  $RELEASE"
echo "SHA256:       $SHA256"
echo "Size (bytes): $SIZE"
echo "Uploaded at:  $UPLOADED_AT"
echo "Git commit:   $GIT_COMMIT"
echo

# --- 4. Write manifest next to the source ---
MANIFEST_PATH="${SRC}.manifest.json"
cat > "$MANIFEST_PATH" <<EOF
{
  "filename": "$ASSET_NAME",
  "sha256": "$SHA256",
  "size_bytes": $SIZE,
  "uploaded_at_utc": "$UPLOADED_AT",
  "git_commit": "$GIT_COMMIT"
}
EOF
echo "Wrote manifest: $MANIFEST_PATH"

# --- 5. Create release if missing, else replace existing assets ---
if gh release view "$RELEASE" >/dev/null 2>&1; then
  echo "Release '$RELEASE' exists — replacing assets"
  # delete-asset is a no-op-with-error if the asset doesn't exist; tolerate that
  run "gh release delete-asset '$RELEASE' '$ASSET_NAME' -y 2>/dev/null || true"
  run "gh release delete-asset '$RELEASE' '$MANIFEST_NAME' -y 2>/dev/null || true"
else
  echo "Release '$RELEASE' does not exist — creating"
  run "gh release create '$RELEASE' --notes 'slim DB cache for Railway cold-start seed'"
fi

# --- 6. Upload both assets ---
run "gh release upload '$RELEASE' '$SRC' '$MANIFEST_PATH'"

# --- 7. Print the public download URL ---
REPO_SLUG=$(gh repo view --json nameWithOwner -q .nameWithOwner)
DOWNLOAD_URL="https://github.com/${REPO_SLUG}/releases/download/${RELEASE}/${ASSET_NAME}"
MANIFEST_URL="https://github.com/${REPO_SLUG}/releases/download/${RELEASE}/${MANIFEST_NAME}"
echo
echo "Download URL:  $DOWNLOAD_URL"
echo "Manifest URL:  $MANIFEST_URL"

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "(dry-run: no network calls were made)"
fi

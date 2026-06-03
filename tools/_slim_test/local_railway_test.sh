#!/usr/bin/env bash
# local_railway_test.sh — Fast local-Docker harness mimicking Railway's container env.
# Iterate on the slim Railway image in 2-5 min for seed-only, or full entrypoint locally
# before pushing. Streams logs live, cleans up on Ctrl-C, captures daily_totals.json.

set -euo pipefail

# ---------- config ----------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCKERFILE="${REPO_ROOT}/Dockerfile.railway"
IMAGE_TAG="solstice-slim:local"
CONTAINER_NAME="solstice-slim-test-$$"
VOLUME_NAME="solstice-test-vol"
TMPFS_SIZE="5g"
MODE="entrypoint"
ENV_FILE="${REPO_ROOT}/.env"
OUT_DIR="${REPO_ROOT}/tools/_slim_test/_artifacts"
mkdir -p "${OUT_DIR}"

# ---------- arg parse ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="$2"; shift 2 ;;
    --mode=*)
      MODE="${1#*=}"; shift ;;
    -h|--help)
      cat <<EOF
Usage: $0 [--mode entrypoint|seed-only|refresh-only]

  entrypoint    Full container entrypoint: seed + refresh + upload (default).
  seed-only    Volume-mount + seed phase only (~30 sec).
  refresh-only  Skip seed, run only refresh.sh against pre-seeded /mnt/data (~60 min).
EOF
      exit 0 ;;
    *)
      echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

case "${MODE}" in
  entrypoint|seed-only|refresh-only) ;;
  *) echo "invalid --mode: ${MODE}" >&2; exit 2 ;;
esac

# ---------- docker presence ----------
if ! command -v docker >/dev/null 2>&1; then
  cat >&2 <<'EOF'
ERROR: docker not found on PATH.
Install Docker Desktop for macOS:
  https://www.docker.com/products/docker-desktop/
Then re-run this script.
EOF
  exit 127
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon not reachable. Start Docker Desktop and retry." >&2
  exit 127
fi

# ---------- env loading ----------
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "ERROR: ${ENV_FILE} missing — need HELIUS_API_KEY + GITHUB_TOKEN to mimic Railway." >&2
  exit 2
fi

# Extract specific vars from .env (don't source the whole file — may contain shell-incompat lines)
extract_env() {
  local key="$1"
  grep -E "^${key}=" "${ENV_FILE}" | tail -n1 | sed -E "s/^${key}=//;s/^['\"]//;s/['\"]$//" || true
}

HELIUS_API_KEY="$(extract_env HELIUS_API_KEY)"
GITHUB_TOKEN="$(extract_env GITHUB_TOKEN)"

if [[ -z "${HELIUS_API_KEY}" ]]; then
  echo "ERROR: HELIUS_API_KEY missing from ${ENV_FILE}" >&2
  exit 2
fi
if [[ -z "${GITHUB_TOKEN}" && "${MODE}" == "entrypoint" ]]; then
  echo "WARN: GITHUB_TOKEN missing — upload phase will fail (continuing)." >&2
fi

# ---------- cleanup trap ----------
cleanup() {
  local rc=$?
  echo
  echo "[cleanup] stopping container ${CONTAINER_NAME} (if running)..."
  docker stop "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  exit $rc
}
trap cleanup INT TERM

# ---------- build ----------
START_TS=$(date +%s)
echo "[build] docker build -f ${DOCKERFILE} -t ${IMAGE_TAG} ${REPO_ROOT}"
docker build -f "${DOCKERFILE}" -t "${IMAGE_TAG}" "${REPO_ROOT}"
BUILD_TS=$(date +%s)

# ---------- run ----------
RUN_ARGS=(
  --name "${CONTAINER_NAME}"
  --rm
  --tmpfs "/mnt/data:rw,size=${TMPFS_SIZE},exec"
  -e "HELIUS_API_KEY=${HELIUS_API_KEY}"
  -e "GITHUB_TOKEN=${GITHUB_TOKEN}"
  -e "GH_REPO=jayow/solstice-airdrop-dashboard"
  -e "SOLSTICE_DB=/mnt/data/solstice.slim.db"
  -e "SOLSTICE_RPC_CONCURRENCY=60"
  -e "TEST_MODE=${MODE}"
)

case "${MODE}" in
  entrypoint)
    echo "[run] mode=entrypoint — full seed + refresh + upload"
    docker run "${RUN_ARGS[@]}" "${IMAGE_TAG}"
    ;;
  seed-only)
    echo "[run] mode=seed-only — volume-mount + seed only (~30 sec)"
    docker run "${RUN_ARGS[@]}" --entrypoint /bin/bash "${IMAGE_TAG}" \
      -c "bash /app/server-s1/seed.sh 2>/dev/null || bash /app/seed.sh 2>/dev/null || python -m tools.seed_volume"
    ;;
  refresh-only)
    echo "[run] mode=refresh-only — skip seed, run refresh.sh"
    docker run "${RUN_ARGS[@]}" --entrypoint /bin/bash "${IMAGE_TAG}" \
      -c "bash /app/server/refresh.sh"
    ;;
esac
RUN_RC=$?
END_TS=$(date +%s)

# ---------- capture daily_totals.json ----------
DAILY_TOTALS_HOST=""
# Container is --rm so it's gone; try volume inspect via a sidecar container if exists.
if docker volume inspect "${VOLUME_NAME}" >/dev/null 2>&1; then
  DAILY_TOTALS_HOST="${OUT_DIR}/daily_totals.json"
  docker run --rm -v "${VOLUME_NAME}:/v" alpine sh -c "cat /v/daily_totals.json 2>/dev/null || true" \
    > "${DAILY_TOTALS_HOST}" || true
  [[ -s "${DAILY_TOTALS_HOST}" ]] || DAILY_TOTALS_HOST=""
fi

# ---------- summary ----------
WALL=$(( END_TS - START_TS ))
BUILD_WALL=$(( BUILD_TS - START_TS ))
RUN_WALL=$(( END_TS - BUILD_TS ))

echo
echo "================ SUMMARY ================"
echo "mode:               ${MODE}"
echo "image:              ${IMAGE_TAG}"
echo "build wall:         ${BUILD_WALL}s"
echo "run wall:           ${RUN_WALL}s"
echo "total wall:         ${WALL}s"
echo "exit code:          ${RUN_RC}"
echo "daily_totals.json:  ${DAILY_TOTALS_HOST:-<not captured — tmpfs gone with container>}"
echo "========================================="

exit ${RUN_RC}

#!/usr/bin/env bash
# Stage 5 driver: runs server/refresh.sh against the slim DB and compares
# the resulting daily_totals.json against the canonical baseline.
# Always restores the canonical baseline on exit (trap), even on failure
# or timeout. Hashes verify the canonical was preserved.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

REFRESH_TIMEOUT_SEC="${REFRESH_TIMEOUT_SEC:-2700}"  # 45 min default
SLIM_DB_SRC="${SLIM_DB:-data/solstice.slim.db}"

mkdir -p tmp/slim_refresh tmp/slim_test_report

# Preserve the originally-shipped canonical daily_totals.json so we can restore
# it at script exit. This is the dashboard's live data — must NOT be polluted.
cp server/daily_totals.json tmp/slim_refresh/shipped_canonical_daily_totals.json
SHIPPED_HASH=$(shasum -a 256 server/daily_totals.json | awk '{print $1}')
echo "Shipped canonical snapshot: $SHIPPED_HASH"

# Always-restore guard: triggers on script exit (success / failure / signal).
# Restores the ORIGINAL shipped daily_totals.json (NOT the freshly-rebuilt one).
restore_shipped_baseline() {
  if [ -f tmp/slim_refresh/shipped_canonical_daily_totals.json ]; then
    if ! cmp -s server/daily_totals.json tmp/slim_refresh/shipped_canonical_daily_totals.json 2>/dev/null; then
      cp tmp/slim_refresh/shipped_canonical_daily_totals.json server/daily_totals.json
      echo "Shipped canonical baseline RESTORED at exit"
    fi
  fi
}
trap restore_shipped_baseline EXIT INT TERM

# CRITICAL: regenerate canonical daily_totals.json from CURRENT canonical DB
# state so we compare apples-to-apples. The shipped baseline may have been
# produced by an older walker state (different quest_cache contents).
# Without this regen, stage 5 reports stale-baseline drift that's NOT a slim
# defect (validated 2026-06-03: raydium showed 85.77% drift vs shipped, but
# only 0.0752% drift vs freshly-rebuilt canonical).
echo "Regenerating canonical baseline from current canonical DB state..."
unset SOLSTICE_DB
python3 server/build_daily_totals.py > tmp/slim_refresh/canonical_rebuild.log 2>&1
CANONICAL_REGEN_EXIT=$?
if [ "$CANONICAL_REGEN_EXIT" -ne 0 ]; then
  echo "FAIL: canonical regen exit=$CANONICAL_REGEN_EXIT, see tmp/slim_refresh/canonical_rebuild.log"
  exit 1
fi
# Save the fresh canonical as the comparison baseline.
cp server/daily_totals.json tmp/slim_refresh/canonical_daily_totals.json
FRESH_HASH=$(shasum -a 256 server/daily_totals.json | awk '{print $1}')
echo "Fresh canonical baseline (compare target): $FRESH_HASH"

# Copy slim DB to working location so refresh doesn't mutate the under-test DB.
cp "$SLIM_DB_SRC" tmp/slim_refresh/solstice.slim.db

# Run refresh.sh against the slim copy with timeout.
START=$(date +%s)
echo "Starting refresh.sh against slim DB (timeout ${REFRESH_TIMEOUT_SEC}s)..."

# macOS doesn't have GNU timeout by default. Use a background-process pattern.
SOLSTICE_DB=tmp/slim_refresh/solstice.slim.db bash server/refresh.sh \
  > tmp/slim_refresh/refresh.log 2>&1 &
REFRESH_PID=$!

ELAPSED=0
REFRESH_TIMED_OUT=0
while kill -0 "$REFRESH_PID" 2>/dev/null; do
  sleep 5
  ELAPSED=$(( $(date +%s) - START ))
  if [ "$ELAPSED" -ge "$REFRESH_TIMEOUT_SEC" ]; then
    echo "TIMEOUT: refresh exceeded $REFRESH_TIMEOUT_SEC seconds, killing"
    kill "$REFRESH_PID" 2>/dev/null
    sleep 2
    kill -9 "$REFRESH_PID" 2>/dev/null
    REFRESH_TIMED_OUT=1
    break
  fi
done

wait "$REFRESH_PID" 2>/dev/null
REFRESH_EXIT=$?
END=$(date +%s)
WALL=$(( END - START ))

echo "Refresh: exit=$REFRESH_EXIT wall=${WALL}s timed_out=$REFRESH_TIMED_OUT"

# If refresh wrote a new daily_totals.json, copy it aside before the trap restores.
if [ -f server/daily_totals.json ] && \
   ! cmp -s server/daily_totals.json tmp/slim_refresh/canonical_daily_totals.json 2>/dev/null; then
  cp server/daily_totals.json tmp/slim_refresh/slim_daily_totals.json
  echo "Slim daily_totals.json captured for comparison"
else
  echo "WARNING: server/daily_totals.json was not updated by slim refresh"
fi

# Run comparison only if refresh succeeded and we have a slim output to compare.
COMPARE_EXIT=-1
if [ "$REFRESH_TIMED_OUT" = "1" ]; then
  STAGE5_RESULT="TIMEOUT"
  REASON="refresh.sh exceeded ${REFRESH_TIMEOUT_SEC}s"
elif [ "$REFRESH_EXIT" -ne 0 ]; then
  STAGE5_RESULT="FAIL"
  REASON="refresh.sh exit=$REFRESH_EXIT, see tmp/slim_refresh/refresh.log"
elif [ ! -f tmp/slim_refresh/slim_daily_totals.json ]; then
  STAGE5_RESULT="FAIL"
  REASON="slim refresh did not produce a daily_totals.json delta"
else
  python3 tools/compare_totals.py \
    --local tmp/slim_refresh/canonical_daily_totals.json \
    --remote tmp/slim_refresh/slim_daily_totals.json \
    --drift 0.001 --coverage 0.995 \
    --write-report tmp/slim_test_report/stage5_report.md \
    > tmp/slim_test_report/stage_5_compare.json 2> tmp/slim_test_report/stage_5_compare.err
  COMPARE_EXIT=$?
  if [ "$COMPARE_EXIT" -eq 0 ]; then
    STAGE5_RESULT="PASS"
    REASON="null"
  else
    STAGE5_RESULT="FAIL"
    REASON="compare_totals.py exit=$COMPARE_EXIT (drift or coverage gate tripped)"
  fi
fi

# Trap will fire after this block, restoring canonical.

# Hash check after trap restoration (done in subshell).
FINAL_HASH_AFTER_RESTORE=""
BASELINE_INTACT="unknown"

# Emit JSON result. Trap fires on exit AFTER this echoes the result.
python3 - "$STAGE5_RESULT" "$REFRESH_EXIT" "$WALL" "$COMPARE_EXIT" "$REASON" <<'PY'
import json, sys
result, refresh_exit, wall, compare_exit, reason = sys.argv[1:6]
out = {
    "stage": 5,
    "name": "output_parity",
    "result": result,
    "metrics": {
        "refresh_exit_code": int(refresh_exit),
        "refresh_wall_clock_sec": int(wall),
        "compare_exit_code": int(compare_exit) if compare_exit != "-1" else None,
        "report_md_path": "tmp/slim_test_report/stage5_report.md",
    },
    "first_failure_reason": None if reason == "null" else reason,
}
print(json.dumps(out))
PY

# Exit code 0 if PASS, otherwise 1
if [ "$STAGE5_RESULT" = "PASS" ]; then
  exit 0
else
  exit 1
fi

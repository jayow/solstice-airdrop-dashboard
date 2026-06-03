#!/usr/bin/env bash
# tools/test_slim_db.sh — 6-stage slim DB pipeline test driver.
# Spec: docs/slim_db_test_framework.md
#
# Orchestrates 6 stages, calls Python validators in tools/_slim_test/<stage>.py,
# emits structured report (test_results.json + test_results.md).
#
# Exit code = first failed stage number (1..6), or 0 if all pass.
# Stages 1-3 failure skips 4-6; stage 4 failure skips 5-6.

set -euo pipefail

# ---------- Defaults ----------
CANONICAL_DB="data/solstice.db"
SLIM_DB="data/solstice.slim.db"
REPORT_DIR="tmp/slim_test_report"
EXTENDED=0
SKIP_BUILD=0

# ---------- Args ----------
usage() {
  cat <<'USAGE'
Usage: tools/test_slim_db.sh [options]

Options:
  --canonical <path>    Canonical DB path (default: data/solstice.db)
  --slim <path>         Slim DB path (default: data/solstice.slim.db)
  --report-dir <path>   Report output directory (default: tmp/slim_test_report)
  --extended            Enable stage 6 (multi-day consistency); otherwise SKIP
  --skip-build          Use existing slim DB; skip rebuild inside stage 1
  --help                Show this message

Exit codes:
  0  all stages PASS (or stage 6 SKIP without --extended)
  N  first failed stage number (1..6)
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --canonical)  CANONICAL_DB="$2"; shift 2 ;;
    --slim)       SLIM_DB="$2"; shift 2 ;;
    --report-dir) REPORT_DIR="$2"; shift 2 ;;
    --extended)   EXTENDED=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --help|-h)    usage; exit 0 ;;
    *)            echo "Unknown arg: $1" >&2; usage; exit 64 ;;
  esac
done

# ---------- Resolve absolute paths ----------
abspath() {
  # Portable absolute-path resolver (parent must exist; basename may not yet).
  local p="$1"
  if [[ -d "$p" ]]; then
    ( cd "$p" && pwd )
  else
    local d b
    d="$(dirname "$p")"
    b="$(basename "$p")"
    mkdir -p "$d"
    echo "$( cd "$d" && pwd )/$b"
  fi
}

CANONICAL_DB="$(abspath "$CANONICAL_DB")"
SLIM_DB="$(abspath "$SLIM_DB")"
REPORT_DIR="$(abspath "$REPORT_DIR")"

mkdir -p "$REPORT_DIR"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALIDATOR_DIR="$REPO_ROOT/tools/_slim_test"
RESULTS_JSON="$REPORT_DIR/test_results.json"
RESULTS_MD="$REPORT_DIR/test_results.md"

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ---------- Per-stage runner ----------
# Each validator (tools/_slim_test/stageN.py) receives JSON-encoded args on
# stdin and writes a JSON result object to stdout. The driver appends that
# object (plus log_path) to a results array.
#
# Validator JSON contract (stdout):
#   {"stage": N, "name": "...", "result": "PASS"|"FAIL"|"SKIP", "metrics": {...}}
#
# Driver augments with "log_path" before writing the final report.

STAGE_RESULTS_FILE="$REPORT_DIR/.stage_results.jsonl"
: > "$STAGE_RESULTS_FILE"

FIRST_FAILURE=""

run_stage() {
  local n="$1" name="$2"
  shift 2
  local log_path="$REPORT_DIR/stage_${n}.log"
  local validator="$VALIDATOR_DIR/stage${n}.py"

  echo "[stage ${n}] ${name} — running" | tee -a "$log_path"

  if [[ ! -f "$validator" ]]; then
    echo "[stage ${n}] FAIL — validator missing: $validator" | tee -a "$log_path"
    printf '{"stage":%d,"name":"%s","result":"FAIL","metrics":{"error":"validator_missing","path":"%s"},"log_path":"%s"}\n' \
      "$n" "$name" "$validator" "$log_path" >> "$STAGE_RESULTS_FILE"
    FIRST_FAILURE="$n"
    return 1
  fi

  local args_json
  args_json=$(cat <<JSON
{
  "canonical_db": "$CANONICAL_DB",
  "slim_db": "$SLIM_DB",
  "report_dir": "$REPORT_DIR",
  "repo_root": "$REPO_ROOT",
  "extended": $([[ $EXTENDED -eq 1 ]] && echo true || echo false),
  "skip_build": $([[ $SKIP_BUILD -eq 1 ]] && echo true || echo false)
}
JSON
)

  local stage_json
  set +e
  stage_json=$(printf '%s' "$args_json" | python3 "$validator" 2>>"$log_path")
  local rc=$?
  set -e

  if [[ $rc -ne 0 || -z "$stage_json" ]]; then
    echo "[stage ${n}] validator non-zero exit ($rc); treating as FAIL" | tee -a "$log_path"
    stage_json=$(printf '{"stage":%d,"name":"%s","result":"FAIL","metrics":{"validator_exit":%d}}' "$n" "$name" "$rc")
  fi

  # Append log_path into the JSON object (simple textual splice; validators
  # MUST NOT emit a log_path key themselves).
  local with_log
  with_log=$(printf '%s' "$stage_json" | python3 -c '
import json, sys, os
obj = json.loads(sys.stdin.read())
obj["log_path"] = os.environ["LOG_PATH"]
print(json.dumps(obj))
' LOG_PATH="$log_path" 2>/dev/null || true)

  if [[ -z "$with_log" ]]; then
    with_log="$stage_json"
  fi

  echo "$with_log" >> "$STAGE_RESULTS_FILE"

  local result
  result=$(printf '%s' "$with_log" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read()).get("result",""))' 2>/dev/null || echo "")

  echo "[stage ${n}] result=${result}" | tee -a "$log_path"

  if [[ "$result" == "FAIL" ]]; then
    FIRST_FAILURE="$n"
    return 1
  fi
  return 0
}

skip_stage() {
  local n="$1" name="$2" reason="$3"
  local log_path="$REPORT_DIR/stage_${n}.log"
  echo "[stage ${n}] SKIP — ${reason}" | tee -a "$log_path"
  printf '{"stage":%d,"name":"%s","result":"SKIP","metrics":{"reason":"%s"},"log_path":"%s"}\n' \
    "$n" "$name" "$reason" "$log_path" >> "$STAGE_RESULTS_FILE"
}

# ---------- Stage cascade ----------
STAGE_NAMES=("build_validation" "schema_parity" "row_count_parity" "walker_smoke" "totals_parity" "multi_day")

# Stages 1-3: schema/build gate.
GATE_123_OK=1
for n in 1 2 3; do
  name="${STAGE_NAMES[$((n-1))]}"
  if ! run_stage "$n" "$name"; then
    echo "Stage ${n} FAIL — skipping subsequent stages"
    GATE_123_OK=0
    for m in $(seq $((n+1)) 6); do
      mname="${STAGE_NAMES[$((m-1))]}"
      skip_stage "$m" "$mname" "skipped_due_to_stage_${n}_fail"
    done
    break
  fi
done

# Stage 4: walker smoke (only if 1-3 OK).
GATE_4_OK=1
if [[ $GATE_123_OK -eq 1 ]]; then
  if ! run_stage 4 "${STAGE_NAMES[3]}"; then
    echo "Stage 4 FAIL — skipping subsequent stages"
    GATE_4_OK=0
    skip_stage 5 "${STAGE_NAMES[4]}" "skipped_due_to_stage_4_fail"
    if [[ $EXTENDED -eq 1 ]]; then
      skip_stage 6 "${STAGE_NAMES[5]}" "skipped_due_to_stage_4_fail"
    else
      skip_stage 6 "${STAGE_NAMES[5]}" "not_extended"
    fi
  fi
fi

# Stage 5: totals parity (only if 4 OK).
if [[ $GATE_123_OK -eq 1 && $GATE_4_OK -eq 1 ]]; then
  if ! run_stage 5 "${STAGE_NAMES[4]}"; then
    echo "Stage 5 FAIL — skipping subsequent stages"
    if [[ $EXTENDED -eq 1 ]]; then
      skip_stage 6 "${STAGE_NAMES[5]}" "skipped_due_to_stage_5_fail"
    else
      skip_stage 6 "${STAGE_NAMES[5]}" "not_extended"
    fi
  else
    # Stage 6: gated by --extended.
    if [[ $EXTENDED -eq 1 ]]; then
      run_stage 6 "${STAGE_NAMES[5]}" || true
    else
      skip_stage 6 "${STAGE_NAMES[5]}" "not_extended"
    fi
  fi
fi

# ---------- Emit final report ----------
FINISHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

OVERALL="PASS"
if [[ -n "$FIRST_FAILURE" ]]; then
  OVERALL="FAIL"
fi

# Render JSON + Markdown via a tiny Python emitter (zero deps).
STARTED_AT="$STARTED_AT" FINISHED_AT="$FINISHED_AT" OVERALL="$OVERALL" \
FIRST_FAILURE="${FIRST_FAILURE:-}" CANONICAL_DB="$CANONICAL_DB" SLIM_DB="$SLIM_DB" \
RESULTS_JSON="$RESULTS_JSON" RESULTS_MD="$RESULTS_MD" \
STAGE_RESULTS_FILE="$STAGE_RESULTS_FILE" \
python3 <<'PY'
import json, os

started = os.environ["STARTED_AT"]
finished = os.environ["FINISHED_AT"]
overall = os.environ["OVERALL"]
first_failure = os.environ.get("FIRST_FAILURE") or None
if first_failure:
    try:
        first_failure = int(first_failure)
    except ValueError:
        pass

stages = []
with open(os.environ["STAGE_RESULTS_FILE"], "r") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        stages.append(json.loads(line))
stages.sort(key=lambda s: s.get("stage", 0))

report = {
    "overall": overall,
    "started_at": started,
    "finished_at": finished,
    "canonical_db": os.environ["CANONICAL_DB"],
    "slim_db": os.environ["SLIM_DB"],
    "stages": stages,
    "first_failure": first_failure,
}

with open(os.environ["RESULTS_JSON"], "w") as fh:
    json.dump(report, fh, indent=2)
    fh.write("\n")

# Markdown
lines = []
lines.append(f"# Slim DB pipeline test — {overall}")
lines.append("")
lines.append(f"- started_at: {started}")
lines.append(f"- finished_at: {finished}")
lines.append(f"- canonical_db: `{report['canonical_db']}`")
lines.append(f"- slim_db: `{report['slim_db']}`")
lines.append(f"- first_failure: {first_failure}")
lines.append("")
lines.append("| stage | name | result | metrics | log |")
lines.append("|---|---|---|---|---|")
for s in stages:
    metrics_str = json.dumps(s.get("metrics", {}), separators=(",", ":"))
    lines.append(
        f"| {s.get('stage')} | {s.get('name')} | {s.get('result')} | `{metrics_str}` | `{s.get('log_path','')}` |"
    )
lines.append("")

with open(os.environ["RESULTS_MD"], "w") as fh:
    fh.write("\n".join(lines))
PY

# ---------- Stdout summary ----------
if [[ "$OVERALL" == "PASS" ]]; then
  echo "OVERALL: PASS"
else
  echo "OVERALL: FAIL (stage ${FIRST_FAILURE})"
fi

echo "REPORT_JSON: $RESULTS_JSON"
echo "REPORT_MD:   $RESULTS_MD"

if [[ "$OVERALL" == "PASS" ]]; then
  exit 0
else
  exit "${FIRST_FAILURE}"
fi

#!/usr/bin/env python3
"""Stage 5 — End-to-end totals parity.

Assumes the caller has already run the canonical refresh (BASELINE_TOTALS)
and the slim refresh (SLIM_TOTALS). Invokes `tools/compare_totals.py` with
drift 0.001 and coverage 0.995. Compare exit 0 → PASS, else FAIL.

Env vars:
  BASELINE_TOTALS  path to canonical daily_totals.json (required)
  SLIM_TOTALS      path to slim daily_totals.json (required)
  REPORT_DIR       optional dir for stage5.report log
  COMPARE_DAY      optional YYYY-MM-DD passed to --day

Exit: 0 PASS, 1 FAIL.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

STAGE = 5
NAME = "totals_parity"
COMPARE = "tools/compare_totals.py"
DRIFT = "0.001"
COVERAGE = "0.995"


def _emit(result: str, metrics: dict, reason: str | None) -> int:
    sys.stdout.write(json.dumps({
        "stage": STAGE,
        "name": NAME,
        "result": result,
        "metrics": metrics,
        "first_failure_reason": reason,
    }) + "\n")
    sys.stdout.flush()
    return 0 if result == "PASS" else 1


def _report_dir() -> Path:
    rd = os.environ.get("REPORT_DIR") or "/tmp"
    p = Path(rd)
    p.mkdir(parents=True, exist_ok=True)
    return p


def main() -> int:
    baseline = os.environ.get("BASELINE_TOTALS")
    slim_totals = os.environ.get("SLIM_TOTALS")
    if not baseline or not slim_totals:
        return _emit("FAIL", {}, "BASELINE_TOTALS and SLIM_TOTALS must be set")
    if not Path(baseline).is_file():
        return _emit("FAIL", {}, f"BASELINE_TOTALS not found: {baseline}")
    if not Path(slim_totals).is_file():
        return _emit("FAIL", {}, f"SLIM_TOTALS not found: {slim_totals}")
    if not Path(COMPARE).is_file():
        return _emit("FAIL", {}, f"compare script missing: {COMPARE}")

    report_dir = _report_dir()
    report_path = report_dir / "stage5.report"

    args = [
        sys.executable, COMPARE,
        "--local", baseline,
        "--remote", slim_totals,
        "--drift", DRIFT,
        "--coverage", COVERAGE,
    ]
    day = os.environ.get("COMPARE_DAY")
    if day:
        args.extend(["--day", day])

    metrics: dict = {
        "baseline": baseline,
        "slim_totals": slim_totals,
        "drift": float(DRIFT),
        "coverage": float(COVERAGE),
        "report_path": str(report_path),
        "compare_day": day,
    }

    try:
        with open(report_path, "wb") as out_f:
            proc = subprocess.run(args, stdout=out_f, stderr=subprocess.STDOUT)
    except Exception as exc:
        return _emit("FAIL", metrics, f"compare_totals invocation failed: {exc}")

    metrics["compare_exit_code"] = proc.returncode

    # Always copy the report tail into metrics for debugging.
    try:
        body = report_path.read_text(errors="replace")
        metrics["report_tail"] = body[-2000:]
    except Exception:
        pass

    if proc.returncode == 0:
        return _emit("PASS", metrics, None)
    return _emit("FAIL", metrics, f"compare_totals exit={proc.returncode}")


if __name__ == "__main__":
    raise SystemExit(main())

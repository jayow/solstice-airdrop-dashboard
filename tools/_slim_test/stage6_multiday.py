#!/usr/bin/env python3
"""Stage 6 — Multi-day consistency (gated by --extended).

Without --extended, emits SKIP and exits 0.

With --extended, repeats Stage 5 comparison for N=3 days using historical
baselines + slim totals at:
  backups/daily_totals.<DAY>.json   (canonical)
  $RUN_DIR/daily_totals.slim.<DAY>.json (slim)

Stage 6 PASS iff all 3 days PASS (== 9/9 substages in the original spec; here
we model the 3 day-level checks, since stages 1 + 3 are date-invariant for
the current SLIM_DB and don't need re-running per day).

Env vars:
  BASELINE_DIR     dir holding backups/daily_totals.<DAY>.json (default: backups)
  SLIM_TOTALS_DIR  dir holding daily_totals.slim.<DAY>.json (default: REPORT_DIR)
  DAYS             optional comma list of YYYY-MM-DD; defaults to today-1..3

Exit: 0 PASS or SKIP, 1 FAIL.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

STAGE = 6
NAME = "multi_day"
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
    return 0 if result in ("PASS", "SKIP") else 1


def _report_dir() -> Path:
    rd = os.environ.get("REPORT_DIR") or "/tmp"
    p = Path(rd)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _default_days() -> list[str]:
    today = dt.date.today()
    return [(today - dt.timedelta(days=i)).isoformat() for i in (1, 2, 3)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extended", action="store_true", help="enable multi-day run")
    args, _ = ap.parse_known_args()

    if not args.extended:
        return _emit("SKIP", {"reason": "--extended not set"}, None)

    if not Path(COMPARE).is_file():
        return _emit("FAIL", {}, f"compare script missing: {COMPARE}")

    baseline_dir = Path(os.environ.get("BASELINE_DIR") or "backups")
    slim_dir = Path(os.environ.get("SLIM_TOTALS_DIR") or _report_dir())
    days_env = os.environ.get("DAYS")
    days = [d.strip() for d in days_env.split(",")] if days_env else _default_days()
    report_dir = _report_dir()

    per_day: list[dict] = []
    first_failure: dict | None = None

    for day in days:
        baseline = baseline_dir / f"daily_totals.{day}.json"
        slim_totals = slim_dir / f"daily_totals.slim.{day}.json"
        report_path = report_dir / f"stage6.{day}.report"
        rec: dict = {"day": day, "baseline": str(baseline), "slim": str(slim_totals)}
        if not baseline.is_file():
            rec.update({"result": "FAIL", "reason": f"missing baseline {baseline}"})
            per_day.append(rec)
            if first_failure is None:
                first_failure = rec
            continue
        if not slim_totals.is_file():
            rec.update({"result": "FAIL", "reason": f"missing slim totals {slim_totals}"})
            per_day.append(rec)
            if first_failure is None:
                first_failure = rec
            continue

        cmd = [
            sys.executable, COMPARE,
            "--local", str(baseline),
            "--remote", str(slim_totals),
            "--day", day,
            "--drift", DRIFT,
            "--coverage", COVERAGE,
        ]
        try:
            with open(report_path, "wb") as out_f:
                proc = subprocess.run(cmd, stdout=out_f, stderr=subprocess.STDOUT)
        except Exception as exc:
            rec.update({"result": "FAIL", "reason": f"invocation error: {exc}",
                        "report": str(report_path)})
            per_day.append(rec)
            if first_failure is None:
                first_failure = rec
            continue

        rec["compare_exit_code"] = proc.returncode
        rec["report"] = str(report_path)
        if proc.returncode == 0:
            rec["result"] = "PASS"
        else:
            rec["result"] = "FAIL"
            rec["reason"] = f"compare exit={proc.returncode}"
            if first_failure is None:
                first_failure = rec
        per_day.append(rec)

    passed = sum(1 for r in per_day if r.get("result") == "PASS")
    metrics = {
        "days_total": len(days),
        "days_passed": passed,
        "per_day": per_day,
    }
    if first_failure is None and passed == len(days):
        return _emit("PASS", metrics, None)

    reason = (f"day {first_failure['day']}: {first_failure.get('reason', 'fail')}"
              if first_failure else f"{passed}/{len(days)} days passed")
    return _emit("FAIL", metrics, reason)


if __name__ == "__main__":
    raise SystemExit(main())

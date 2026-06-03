#!/usr/bin/env python3
"""Stage 1 — Slim DB build validation.

Checks (ALL must hold):
  - SLIM_DB file exists.
  - `PRAGMA integrity_check` == 'ok'.
  - size in [100 MB, 5 GB].

Reads env vars:
  SLIM_DB        path to slim DB (required)
  CANONICAL_DB   path to canonical DB (read but unused here; kept for symmetry)
  REPORT_DIR     optional dir for log files (created if absent)

Stdout: a single JSON line:
  {"stage": 1, "name": "build_validation", "result": "PASS|FAIL",
   "metrics": {...}, "first_failure_reason": null|"..."}

Exit: 0 PASS, 1 FAIL.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

STAGE = 1
NAME = "build_validation"
SIZE_FLOOR = 104857600     # 100 MB
SIZE_CEIL = 5368709120     # 5 GB


def _emit(result: str, metrics: dict, reason: str | None) -> int:
    line = {
        "stage": STAGE,
        "name": NAME,
        "result": result,
        "metrics": metrics,
        "first_failure_reason": reason,
    }
    sys.stdout.write(json.dumps(line) + "\n")
    sys.stdout.flush()
    return 0 if result == "PASS" else 1


def main() -> int:
    slim = os.environ.get("SLIM_DB")
    if not slim:
        return _emit("FAIL", {}, "SLIM_DB env var not set")

    slim_path = Path(slim)
    metrics: dict = {"slim_db": slim}
    violations: list[dict] = []

    # exists
    if not slim_path.is_file():
        violations.append({"condition": "exists", "observed_value": False, "threshold": True})
        metrics["violations"] = violations
        return _emit("FAIL", metrics, f"SLIM_DB not found at {slim}")

    # size
    size = slim_path.stat().st_size
    metrics["size_bytes"] = size
    if size < SIZE_FLOOR:
        violations.append({"condition": "size_floor", "observed_value": size, "threshold": SIZE_FLOOR})
    if size > SIZE_CEIL:
        violations.append({"condition": "size_ceiling", "observed_value": size, "threshold": SIZE_CEIL})

    # integrity
    integrity_value = None
    try:
        conn = sqlite3.connect(f"file:{slim}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA integrity_check;").fetchone()
            integrity_value = row[0] if row else None
        finally:
            conn.close()
    except Exception as exc:
        violations.append({"condition": "integrity", "observed_value": f"error: {exc}", "threshold": "ok"})
    metrics["integrity"] = integrity_value
    if integrity_value != "ok":
        violations.append({"condition": "integrity", "observed_value": integrity_value, "threshold": "ok"})

    if violations:
        metrics["violations"] = violations
        first = violations[0]
        reason = f"{first['condition']}: observed={first['observed_value']} threshold={first['threshold']}"
        return _emit("FAIL", metrics, reason)

    return _emit("PASS", metrics, None)


if __name__ == "__main__":
    raise SystemExit(main())

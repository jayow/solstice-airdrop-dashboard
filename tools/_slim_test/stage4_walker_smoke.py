#!/usr/bin/env python3
"""Stage 4 — Walker smoke (walk_s2_yt) against a copy of slim DB.

Copies SLIM_DB to /tmp/slim_smoke.db (so we never mutate the artifact under
test), runs `python3 tools/walk_s2_yt.py` with SOLSTICE_DB pointing at the
copy. Asserts exit code 0 and no error-pattern in stdout/stderr.

Timeout: 600s; exceeding → FAIL with reason `timeout`.

Exit: 0 PASS, 1 FAIL.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

STAGE = 4
NAME = "walker_smoke"
WALKER = "tools/walk_s2_yt.py"
SMOKE_COPY = "/tmp/slim_smoke.db"
TIMEOUT_SEC = 600
ERROR_RE = re.compile(r"(ERROR|RuntimeError|sqlite3\.OperationalError|no such table: wallet_txs)")


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
    slim = os.environ.get("SLIM_DB")
    if not slim:
        return _emit("FAIL", {}, "SLIM_DB env var not set")
    if not Path(slim).is_file():
        return _emit("FAIL", {}, f"SLIM_DB not found at {slim}")
    if not Path(WALKER).is_file():
        return _emit("FAIL", {}, f"walker not found at {WALKER}")

    # Copy slim -> /tmp/slim_smoke.db so the walker never mutates the artifact.
    try:
        shutil.copyfile(slim, SMOKE_COPY)
    except Exception as exc:
        return _emit("FAIL", {}, f"copy slim->smoke failed: {exc}")

    report_dir = _report_dir()
    stdout_path = report_dir / "stage4.stdout"
    stderr_path = report_dir / "stage4.stderr"

    env = os.environ.copy()
    env["SOLSTICE_DB"] = SMOKE_COPY

    metrics: dict = {
        "walker": WALKER,
        "smoke_copy": SMOKE_COPY,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "timeout_sec": TIMEOUT_SEC,
    }

    try:
        with open(stdout_path, "wb") as out_f, open(stderr_path, "wb") as err_f:
            proc = subprocess.run(
                [sys.executable, WALKER],
                stdout=out_f,
                stderr=err_f,
                env=env,
                timeout=TIMEOUT_SEC,
            )
    except subprocess.TimeoutExpired:
        metrics["exit_code"] = None
        metrics["timed_out"] = True
        return _emit("FAIL", metrics, f"walker exceeded {TIMEOUT_SEC}s timeout")
    except Exception as exc:
        return _emit("FAIL", metrics, f"walker invocation failed: {exc}")

    metrics["exit_code"] = proc.returncode

    # Scan stdout + stderr for error patterns.
    matched_lines: list[str] = []
    for p in (stdout_path, stderr_path):
        try:
            with open(p, "r", errors="replace") as f:
                for line in f:
                    if ERROR_RE.search(line):
                        matched_lines.append(line.rstrip("\n"))
        except FileNotFoundError:
            pass
    metrics["error_matches"] = len(matched_lines)
    metrics["matched_lines"] = matched_lines[:20]  # cap

    if proc.returncode != 0:
        return _emit("FAIL", metrics, f"walker exit_code={proc.returncode}")
    if matched_lines:
        return _emit("FAIL", metrics, f"{len(matched_lines)} error-pattern match(es) in logs")
    return _emit("PASS", metrics, None)


if __name__ == "__main__":
    raise SystemExit(main())

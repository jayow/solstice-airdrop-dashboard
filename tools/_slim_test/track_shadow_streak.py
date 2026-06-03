#!/usr/bin/env python3
"""Track the shadow-streak: consecutive days the slim-test parity check passes.

State is persisted as a state.json asset on a GitHub release tagged
`shadow-streak`. The GitHub Action invokes this script once per day after
running the per-day parity comparison.

State.json schema:
{
  "current_streak_days": int,
  "history": [
    {"date": "YYYY-MM-DD", "result": "PASS"|"FAIL",
     "max_drift": float, "min_coverage": float}
  ]
}
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import date as _date


MILESTONES = (3, 5, 7)
HISTORY_LIMIT = 30
STATE_ASSET = "state.json"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def release_exists(tag: str) -> bool:
    r = _run(["gh", "release", "view", tag, "--json", "tagName"])
    return r.returncode == 0


def create_release(tag: str) -> None:
    _run(
        [
            "gh",
            "release",
            "create",
            tag,
            "--title",
            "Slim-test shadow streak",
            "--notes",
            "Rolling state for the slim-test shadow comparison streak. "
            "Updated daily by the slim-test GitHub Action.",
        ]
    )


def download_state(tag: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, STATE_ASSET)
        r = _run(
            [
                "gh",
                "release",
                "download",
                tag,
                "--pattern",
                STATE_ASSET,
                "--output",
                out_path,
                "--clobber",
            ]
        )
        if r.returncode != 0:
            return {"current_streak_days": 0, "history": []}
        try:
            with open(out_path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {"current_streak_days": 0, "history": []}
    if not isinstance(data, dict):
        return {"current_streak_days": 0, "history": []}
    data.setdefault("current_streak_days", 0)
    data.setdefault("history", [])
    if not isinstance(data["history"], list):
        data["history"] = []
    try:
        data["current_streak_days"] = int(data["current_streak_days"])
    except (TypeError, ValueError):
        data["current_streak_days"] = 0
    return data


def upload_state(tag: str, state: dict) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, STATE_ASSET)
        with open(path, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        # Delete existing asset (ignore failure: asset may not exist yet).
        _run(["gh", "release", "delete-asset", tag, STATE_ASSET, "--yes"])
        r = _run(["gh", "release", "upload", tag, path, "--clobber"])
        return r.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", required=True, choices=["PASS", "FAIL"])
    ap.add_argument("--max-drift", required=True, type=float)
    ap.add_argument("--min-coverage", required=True, type=float)
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--release-tag", default="shadow-streak")
    args = ap.parse_args()

    today = args.date or _date.today().isoformat()
    tag = args.release_tag

    state = {"current_streak_days": 0, "history": []}
    network_ok = True
    try:
        if release_exists(tag):
            state = download_state(tag)
        else:
            create_release(tag)
    except Exception as e:  # noqa: BLE001
        print(f"warning: failed to load shadow-streak state: {e}", file=sys.stderr)
        network_ok = False

    # Append today's entry (replace if same date already present).
    state["history"] = [h for h in state["history"] if h.get("date") != today]
    state["history"].append(
        {
            "date": today,
            "result": args.result,
            "max_drift": args.max_drift,
            "min_coverage": args.min_coverage,
        }
    )

    # Update streak.
    if args.result == "PASS":
        state["current_streak_days"] = int(state.get("current_streak_days", 0)) + 1
    else:
        state["current_streak_days"] = 0

    # Trim history.
    state["history"] = state["history"][-HISTORY_LIMIT:]

    streak = int(state["current_streak_days"])
    milestone_value = next((m for m in MILESTONES if m == streak), None)
    milestone_reached = milestone_value is not None

    # Re-upload (best effort).
    if network_ok:
        try:
            upload_state(tag, state)
        except Exception as e:  # noqa: BLE001
            print(f"warning: failed to upload shadow-streak state: {e}", file=sys.stderr)

    print(
        f"shadow-streak: result={args.result} streak={streak}d "
        f"max_drift={args.max_drift} min_coverage={args.min_coverage} "
        f"history={len(state['history'])}",
        file=sys.stderr,
    )
    print(
        json.dumps(
            {
                "current_streak_days": streak,
                "milestone_reached": milestone_reached,
                "milestone_value": milestone_value,
                "history_length": len(state["history"]),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

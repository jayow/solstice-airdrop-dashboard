"""Solstice snapshot timestamp utility.

Solstice publishes one snapshot per day at 00:00 UTC. Every transform that
writes to wallet_quests should integrate THROUGH the last completed 00:00 UTC,
not through wall-clock now — otherwise the dashboard shows live-accruing
values that don't match Solstice's published numbers.

Use `last_snapshot_ts()` everywhere a transform takes a `now_ts`.
"""
import time


def last_snapshot_ts(now: int | None = None) -> int:
    """Return the most recent 00:00 UTC unix timestamp at or before `now`.

    Example: if called at 2026-05-13 14:30 UTC, returns 2026-05-13 00:00 UTC.
    Matches Solstice's once-daily-at-00:00-UTC publication cadence so
    wallet_quests stays in lockstep with Solstice's leaderboard."""
    if now is None: now = int(time.time())
    return (now // 86400) * 86400

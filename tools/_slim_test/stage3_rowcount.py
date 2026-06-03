#!/usr/bin/env python3
"""Stage 3 — Row count parity for HOT tables.

For each HOT table: `slim_count == canonical_count` (exact).
For `wallet_txs`: `slim_count == 0`.

Exit: 0 PASS, 1 FAIL.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

STAGE = 3
NAME = "row_count_parity"

HOT_TABLES = [
    "wallets",
    "walker_events",
    "quest_cache",
    "slx_allocations",
    "wallet_atas",
    "flares_snapshots",
    "walker_coverage",
    "market_state_history",
    "wallet_quests",
    "slx_balance_snapshots",
    "rpc_cache_responses",
]


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


def _connect_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _count(conn: sqlite3.Connection, table: str) -> int | None:
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else None
    except sqlite3.OperationalError:
        return None


def main() -> int:
    canonical = os.environ.get("CANONICAL_DB")
    slim = os.environ.get("SLIM_DB")
    if not canonical or not slim:
        return _emit("FAIL", {}, "CANONICAL_DB and SLIM_DB must be set")

    try:
        c_conn = _connect_ro(canonical)
        s_conn = _connect_ro(slim)
    except Exception as exc:
        return _emit("FAIL", {}, f"open error: {exc}")

    mismatches: list[dict] = []
    hot_ok = 0
    per_table: list[dict] = []

    for tbl in HOT_TABLES:
        c = _count(c_conn, tbl)
        s = _count(s_conn, tbl)
        delta = None if (c is None or s is None) else s - c
        per_table.append({"table": tbl, "canonical": c, "slim": s, "delta": delta})
        if c is None or s is None or delta != 0:
            mismatches.append({"table": tbl, "canonical": c, "slim": s, "delta": delta})
        else:
            hot_ok += 1

    # wallet_txs must be 0 on slim (canonical may have any rowcount)
    wtx_c = _count(c_conn, "wallet_txs")
    wtx_s = _count(s_conn, "wallet_txs")
    wallet_txs_ok = (wtx_s == 0)
    if not wallet_txs_ok:
        mismatches.append({
            "table": "wallet_txs",
            "canonical": wtx_c,
            "slim": wtx_s,
            "delta": None if wtx_s is None else wtx_s,
        })

    c_conn.close()
    s_conn.close()

    metrics = {
        "hot_tables_ok": hot_ok,
        "hot_tables_total": len(HOT_TABLES),
        "wallet_txs_rows": wtx_s,
        "wallet_txs_canonical": wtx_c,
        "per_table": per_table,
        "mismatches": mismatches,
    }

    if not mismatches:
        return _emit("PASS", metrics, None)

    first = mismatches[0]
    if first["table"] == "wallet_txs":
        reason = f"wallet_txs slim={first['slim']} (expected 0)"
    else:
        reason = (
            f"{first['table']} delta={first['delta']} "
            f"(canonical={first['canonical']}, slim={first['slim']})"
        )
    return _emit("FAIL", metrics, reason)


if __name__ == "__main__":
    raise SystemExit(main())

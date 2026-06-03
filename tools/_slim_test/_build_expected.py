#!/usr/bin/env python3
"""Build tools/_slim_test/expected.json from canonical DB (read-only)."""
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "solstice.db"
OUT = ROOT / "tools" / "_slim_test" / "expected.json"

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

# Stage 1 size band (bytes)
SIZE_MIN_BYTES = 104857600           # 100 MB
SIZE_MAX_BYTES = 5368709120          # 5 GB

# Stage 5/6 drift thresholds
MAX_DRIFT = 0.001                    # 0.1%
MIN_COVERAGE = 0.995                 # 99.5%

# wallet_txs row count assertion for slim DB
WALLET_TXS_SLIM_ROWS = 0


def open_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def main() -> int:
    if not DB.exists():
        print(f"ERROR: canonical DB missing at {DB}", file=sys.stderr)
        return 1

    con = open_ro(DB)
    cur = con.cursor()

    # 1) Discover all existing tables in canonical
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name;"
    )
    all_tables = [r[0] for r in cur.fetchall()]

    missing = [t for t in HOT_TABLES if t not in all_tables]
    if missing:
        print(f"ERROR: HOT tables missing from canonical DB: {missing}", file=sys.stderr)
        return 2

    hot_tables = {}
    for t in HOT_TABLES:
        # columns
        cur.execute(f"PRAGMA table_info({t});")
        cols = [
            {
                "cid": row[0],
                "name": row[1],
                "type": row[2],
                "notnull": int(row[3]),
                "dflt": row[4],
                "pk": int(row[5]),
            }
            for row in cur.fetchall()
        ]

        # indexes (exclude auto-indexes created for PK/UNIQUE constraints —
        # those are sqlite_autoindex_* and not user-defined)
        cur.execute(f"PRAGMA index_list({t});")
        idx_rows = cur.fetchall()
        # PRAGMA index_list columns: seq, name, unique, origin, partial
        indexes = []
        for r in idx_rows:
            iname = r[1]
            origin = r[3] if len(r) > 3 else None
            indexes.append(
                {
                    "name": iname,
                    "unique": int(r[2]),
                    "origin": origin,  # 'c' user-created, 'u' UNIQUE, 'pk' PK
                }
            )

        # row count assertion
        row_count_assertion = "equal_to_canonical"

        hot_tables[t] = {
            "columns": cols,
            "indexes": indexes,
            "row_count_assertion": row_count_assertion,
        }

    # 2) wallet_txs: list its auto-indexes (these get dropped with the table)
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='wallet_txs' ORDER BY name;"
    )
    wallet_txs_indexes = [r[0] for r in cur.fetchall()]

    dropped_tables = ["wallet_txs"]
    dropped_indexes = wallet_txs_indexes

    # 3) Capture wallet_txs assertion explicitly (separate, since it's not HOT)
    wallet_txs_assertion = {
        "table": "wallet_txs",
        "row_count_assertion": "exactly:0",
        "expected_rows": WALLET_TXS_SLIM_ROWS,
    }

    con.close()

    expected = {
        "version": 1,
        "generated_from": str(DB.relative_to(ROOT)),
        "canonical_db": str(DB.relative_to(ROOT)),
        "slim_db": "data/solstice.slim.db",

        # Stage 1
        "size_min_bytes": SIZE_MIN_BYTES,
        "size_max_bytes": SIZE_MAX_BYTES,
        "size_min_mb": SIZE_MIN_BYTES // (1024 * 1024),
        "size_max_mb": SIZE_MAX_BYTES // (1024 * 1024),
        "integrity_check_expected": "ok",

        # Stage 2 + 3
        "hot_tables": hot_tables,

        # Explicitly dropped
        "dropped_tables": dropped_tables,
        "dropped_indexes": dropped_indexes,

        # wallet_txs slim assertion
        "wallet_txs": wallet_txs_assertion,

        # Stage 5 / 6
        "max_drift": MAX_DRIFT,
        "min_coverage": MIN_COVERAGE,

        # Stage 6 config
        "multi_day_n": 3,
        "multi_day_substages_required": 9,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as f:
        json.dump(expected, f, indent=2, sort_keys=False)

    print(f"wrote {OUT}")
    print(f"hot_tables: {len(hot_tables)}")
    total_idx = sum(len(v["indexes"]) for v in hot_tables.values())
    print(f"total_indexes_across_hot: {total_idx}")
    print(f"dropped_tables: {dropped_tables}")
    print(f"dropped_indexes_count: {len(dropped_indexes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

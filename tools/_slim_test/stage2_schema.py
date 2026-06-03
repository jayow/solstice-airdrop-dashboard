#!/usr/bin/env python3
"""Stage 2 — Schema parity.

Compares `sqlite_master` rows for tables (excluding `wallet_txs` and
`sqlite_%`) between CANONICAL_DB and SLIM_DB. Then for every shared table,
diffs `PRAGMA table_info(<t>)` tuples exactly.

PASS: schema-row diff length == 0 AND every shared table has identical
column tuples. FAIL: emit missing/extra/mismatch detail.

Exit: 0 PASS, 1 FAIL.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

STAGE = 2
NAME = "schema_parity"

SCHEMA_SQL = (
    "SELECT name, sql FROM sqlite_master "
    "WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'wallet_txs' "
    "ORDER BY name;"
)


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


def _schema_rows(conn: sqlite3.Connection) -> list[tuple]:
    return list(conn.execute(SCHEMA_SQL).fetchall())


def _table_info(conn: sqlite3.Connection, table: str) -> list[tuple]:
    rows = conn.execute(f"PRAGMA table_info({table});").fetchall()
    # (cid, name, type, notnull, dflt_value, pk)
    return [tuple(r) for r in rows]


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

    try:
        c_rows = _schema_rows(c_conn)
        s_rows = _schema_rows(s_conn)
    except Exception as exc:
        return _emit("FAIL", {}, f"schema query error: {exc}")

    c_names = {r[0] for r in c_rows}
    s_names = {r[0] for r in s_rows}
    missing = sorted(c_names - s_names)   # in canonical, not slim
    extra = sorted(s_names - c_names)     # in slim, not canonical
    shared = sorted(c_names & s_names)

    # Build a unified-diff-equivalent line count: any row mismatch counts.
    schema_diff_lines = 0
    c_map = {r[0]: r[1] for r in c_rows}
    s_map = {r[0]: r[1] for r in s_rows}
    for name in sorted(c_names | s_names):
        if c_map.get(name) != s_map.get(name):
            schema_diff_lines += 1

    column_mismatches: list[dict] = []
    for tbl in shared:
        c_info = _table_info(c_conn, tbl)
        s_info = _table_info(s_conn, tbl)
        if c_info == s_info:
            continue
        c_cols = {row[1]: row for row in c_info}
        s_cols = {row[1]: row for row in s_info}
        for col in sorted(set(c_cols) | set(s_cols)):
            if c_cols.get(col) != s_cols.get(col):
                column_mismatches.append({
                    "table": tbl,
                    "column": col,
                    "canonical": c_cols.get(col),
                    "slim": s_cols.get(col),
                })

    c_conn.close()
    s_conn.close()

    metrics = {
        "shared_tables": len(shared),
        "missing_tables": missing,
        "extra_tables": extra,
        "schema_diff_lines": schema_diff_lines,
        "column_mismatches": column_mismatches,
        "column_mismatch_count": len(column_mismatches),
    }

    if schema_diff_lines == 0 and not column_mismatches and not missing and not extra:
        return _emit("PASS", metrics, None)

    if missing:
        reason = f"missing tables in slim: {missing}"
    elif extra:
        reason = f"extra tables in slim: {extra}"
    elif schema_diff_lines:
        reason = f"{schema_diff_lines} sqlite_master row(s) differ"
    else:
        first = column_mismatches[0]
        reason = f"column mismatch {first['table']}.{first['column']}"
    return _emit("FAIL", metrics, reason)


if __name__ == "__main__":
    raise SystemExit(main())

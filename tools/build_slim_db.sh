#!/bin/bash
# Build a slim copy of data/solstice.db by DROPPING ONLY the `wallet_txs`
# table (and its indexes). Everything else is preserved byte-identical row
# content (schema + rows are reissued in the destination via CREATE + INSERT).
#
# wallet_txs is the bulk of the DB (~30GB of raw Solana tx blobs); the rest
# of the schema is <1GB and is what the server/refresh pipeline needs.
#
# We do NOT use `VACUUM INTO` + `DROP TABLE` because that approach needs
# ~64GB of free disk during the build. Instead we ATTACH a fresh empty
# destination and stream rows table-by-table, skipping wallet_txs entirely.
#
# Usage:
#   bash tools/build_slim_db.sh           # data/solstice.db -> data/solstice.slim.db
#   SRC=foo.db DST=bar.db bash tools/build_slim_db.sh
#   bash tools/build_slim_db.sh -v        # verbose: print each table as it's copied
#
# Output:
#   $DST       — slim SQLite DB
#   $DST.gz    — gzip'd for upload-friendliness
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="${SRC:-data/solstice.db}"
DST="${DST:-data/solstice.slim.db}"
VERBOSE=0

for arg in "$@"; do
  case "$arg" in
    -v|--verbose) VERBOSE=1 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [ ! -f "$SRC" ]; then
  echo "ERROR: source DB not found: $SRC" >&2
  exit 1
fi

# Wipe any stale destination + its sidecar files + old gzip
rm -f "$DST" "$DST-journal" "$DST-wal" "$DST-shm" "$DST.gz"

SRC_SIZE=$(du -h "$SRC" | cut -f1)
echo "Source: $SRC ($SRC_SIZE)"
echo "Dest:   $DST"
echo "Excluding: wallet_txs (+ its indexes)"
echo

export SRC DST VERBOSE
python3 <<'PY'
import os
import sqlite3
import sys
import time

SRC = os.environ["SRC"]
DST = os.environ["DST"]
VERBOSE = os.environ.get("VERBOSE") == "1"

EXCLUDE_TABLE = "wallet_txs"

# Open source read-only via URI so we can't accidentally mutate it.
src_uri = f"file:{SRC}?mode=ro"
src = sqlite3.connect(src_uri, uri=True)
src.row_factory = sqlite3.Row

# Open empty destination. PRAGMAs for speed during bulk copy.
dst = sqlite3.connect(DST)
dst.executescript(
    "PRAGMA journal_mode = OFF;"
    "PRAGMA synchronous = OFF;"
    "PRAGMA temp_store = MEMORY;"
    "PRAGMA locking_mode = EXCLUSIVE;"
)

# --- 1. Copy all tables EXCEPT wallet_txs ---
tables = src.execute(
    "SELECT name, sql FROM sqlite_master "
    "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
    "ORDER BY name"
).fetchall()

row_counts = {}
total_rows = 0
t0 = time.time()
for tname, tsql in tables:
    if tname == EXCLUDE_TABLE:
        if VERBOSE:
            print(f"  [skip] {tname}")
        continue

    if VERBOSE:
        print(f"  [table] {tname} ...", end="", flush=True)

    # Recreate schema verbatim
    dst.execute(tsql)

    # Stream rows
    cols = [r[1] for r in src.execute(f'PRAGMA table_info("{tname}")').fetchall()]
    placeholders = ",".join(["?"] * len(cols))
    col_list = ",".join(f'"{c}"' for c in cols)
    insert_sql = f'INSERT INTO "{tname}" ({col_list}) VALUES ({placeholders})'

    cur = src.execute(f'SELECT {col_list} FROM "{tname}"')
    BATCH = 5000
    n = 0
    dst.execute("BEGIN")
    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            break
        dst.executemany(insert_sql, rows)
        n += len(rows)
    dst.execute("COMMIT")

    row_counts[tname] = n
    total_rows += n
    if VERBOSE:
        print(f" {n:,} rows")

# --- 2. Copy all indexes EXCEPT those on wallet_txs ---
indexes = src.execute(
    "SELECT name, tbl_name, sql FROM sqlite_master "
    "WHERE type='index' AND sql IS NOT NULL "
    "  AND tbl_name != ? "
    "  AND name NOT LIKE 'sqlite_%' "
    "ORDER BY name",
    (EXCLUDE_TABLE,),
).fetchall()
for iname, itbl, isql in indexes:
    if VERBOSE:
        print(f"  [index] {iname} (on {itbl})")
    dst.execute(isql)

# --- 3. Copy views and triggers (skip any referencing wallet_txs) ---
for typ in ("view", "trigger"):
    objs = src.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master "
        f"WHERE type='{typ}' AND sql IS NOT NULL "
        "  AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    for oname, otbl, osql in objs:
        if otbl == EXCLUDE_TABLE:
            if VERBOSE:
                print(f"  [skip {typ}] {oname} (on {EXCLUDE_TABLE})")
            continue
        if VERBOSE:
            print(f"  [{typ}] {oname}")
        dst.execute(osql)

elapsed = time.time() - t0
print(f"\nCopied {len(row_counts)} tables, {total_rows:,} rows in {elapsed:.1f}s")

# --- 4. Restore safe PRAGMAs + VACUUM ---
dst.executescript(
    "PRAGMA journal_mode = DELETE;"
    "PRAGMA synchronous = NORMAL;"
)
print("VACUUM ...", flush=True)
t1 = time.time()
dst.execute("VACUUM")
print(f"VACUUM done in {time.time() - t1:.1f}s")

# --- 5. Integrity check ---
print("PRAGMA integrity_check ...", flush=True)
ic = dst.execute("PRAGMA integrity_check").fetchall()
ic_result = ic[0][0] if ic else "(no result)"
print(f"integrity_check: {ic_result}")

# --- 6. Per-table row counts (sorted desc) ---
print("\nRow counts (slim DB):")
for tname, n in sorted(row_counts.items(), key=lambda kv: -kv[1]):
    print(f"  {tname:40s} {n:>15,}")

dst.close()
src.close()

# Write summary for the shell wrapper to pick up.
src_bytes = os.path.getsize(SRC)
dst_bytes = os.path.getsize(DST)
print(f"\nSource size: {src_bytes / 1e9:.2f} GB")
print(f"Slim size:   {dst_bytes / 1e9:.3f} GB ({100 * dst_bytes / src_bytes:.2f}% of source)")

if ic_result != "ok":
    print(f"FATAL: integrity_check failed: {ic_result}", file=sys.stderr)
    sys.exit(1)
PY

echo
echo "Gzipping $DST -> $DST.gz ..."
gzip -k -f "$DST"
GZ_SIZE=$(du -h "$DST.gz" | cut -f1)
echo "  -> $DST.gz ($GZ_SIZE)"

echo
echo "Done."

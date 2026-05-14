"""
Persistent RPC response cache — SQLite-backed.

Why it exists: don't waste Helius credits re-fetching data that hasn't changed
AND don't silently zero out valid wallet_quests rows when RPC errors. Cache
hits are free; misses store the response so the next call is free too.

Storage: data/solstice.db → `rpc_cache_responses` table.
Schema:
  method        TEXT
  params_hash   TEXT     (24-char sha256 prefix of method+params, sort-stable)
  params_json   TEXT     (for forensics; never used by lookup)
  response_json TEXT     (the raw RPC 'result' field, JSON-encoded)
  status        TEXT     ('ok'|'empty'|'error')
  fetched_at    INTEGER  (epoch seconds; index for age filtering)
  PRIMARY KEY (method, params_hash)

Previously this lived as 62k+ JSON files under data/rpc_cache/. Migrated to
SQLite 2026-05-14 so the cache participates in the same ELT pipeline as the
rest of the data (single .db backup, SQL-queryable, no inode pressure).

Lookup: `get(method, params, max_age_hours=None)` returns the cached entry
(method/params/result/status/fetched_at) or None.

The `_disk_*` functions below provide one-time migration from the old file
cache — see `migrate_files_to_sqlite()`.
"""
import os, json, hashlib, sqlite3, time
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(ROOT, "data", "solstice.db")
LEGACY_CACHE_DIR = os.path.join(ROOT, "data", "rpc_cache")   # for migration only

_conn_lock = Lock()
_initialized = False


def _conn() -> sqlite3.Connection:
    global _initialized
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    # WAL gives concurrent reads while a writer is active — important since
    # walkers run multiple threads against the same cache table.
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    if not _initialized:
        c.execute("""
            CREATE TABLE IF NOT EXISTS rpc_cache_responses (
                method        TEXT NOT NULL,
                params_hash   TEXT NOT NULL,
                params_json   TEXT,
                response_json TEXT,
                status        TEXT DEFAULT 'ok',
                fetched_at    INTEGER NOT NULL,
                PRIMARY KEY (method, params_hash)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_rpc_cache_fetched ON rpc_cache_responses(method, fetched_at)")
        _initialized = True
    return c


def _key(method: str, params: list) -> str:
    """Stable hash of (method, params). Matches the old file-cache hash so the
    migration is a 1:1 mapping by name."""
    payload = json.dumps([method, params], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def get(method: str, params: list, max_age_hours: Optional[float] = None) -> Optional[dict]:
    """Return cached entry if present (and fresh, if max_age_hours given). Else None."""
    h = _key(method, params)
    with _conn_lock:
        c = _conn()
        row = c.execute(
            "SELECT params_json, response_json, status, fetched_at FROM rpc_cache_responses WHERE method=? AND params_hash=?",
            (method, h),
        ).fetchone()
        c.close()
    if not row: return None
    if max_age_hours is not None:
        age_h = (time.time() - row["fetched_at"]) / 3600
        if age_h > max_age_hours: return None
    try:
        result = json.loads(row["response_json"])
    except Exception:
        return None
    return {
        "method": method,
        "params": params,
        "result": result,
        "status": row["status"],
        "fetched_at": datetime.fromtimestamp(row["fetched_at"], timezone.utc).isoformat(),
    }


def put(method: str, params: list, result: Any, status: str = "ok") -> None:
    """Upsert an RPC response."""
    h = _key(method, params)
    with _conn_lock:
        c = _conn()
        c.execute(
            "INSERT OR REPLACE INTO rpc_cache_responses(method, params_hash, params_json, response_json, status, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (method, h, json.dumps(params, default=str), json.dumps(result, default=str), status, int(time.time())),
        )
        c.close()


def cached_rpc(method: str, params: list, *, force_refresh: bool = False,
               max_age_hours: Optional[float] = 24) -> dict:
    """RPC call that consults SQLite cache first. Same return shape as live rpc()."""
    if not force_refresh:
        entry = get(method, params, max_age_hours=max_age_hours)
        if entry and entry.get("status") == "ok":
            return {"jsonrpc": "2.0", "id": 1, "result": entry["result"]}
    from rpc_helper import rpc as live_rpc
    j = live_rpc(method, params)
    if j and "result" in j and "error" not in j:
        put(method, params, j["result"], status="ok")
    return j


def stats() -> dict:
    """Cache size per method, from SQLite."""
    with _conn_lock:
        c = _conn()
        rows = c.execute(
            "SELECT method, COUNT(*) AS n, SUM(LENGTH(response_json)) AS bytes "
            "FROM rpc_cache_responses GROUP BY method ORDER BY n DESC"
        ).fetchall()
        c.close()
    return {r["method"]: {"entries": r["n"], "size_kb": round((r["bytes"] or 0) / 1024, 1)} for r in rows}


# ── one-time migration helpers ──────────────────────────────────────────────

def migrate_files_to_sqlite(batch_size: int = 5000, verbose: bool = True) -> dict:
    """Read every .json file under data/rpc_cache/ and bulk-insert into SQLite.

    Idempotent: existing keys are not overwritten (INSERT OR IGNORE), so if a
    file represents an older cached response than what's already in SQLite,
    the SQLite copy wins. Safe to re-run.
    """
    if not os.path.isdir(LEGACY_CACHE_DIR):
        if verbose: print("no legacy cache dir; nothing to migrate")
        return {"files_seen": 0, "rows_inserted": 0}

    files_seen = rows_inserted = errors = 0
    batch = []
    with _conn_lock:
        c = _conn()
    try:
        for method in sorted(os.listdir(LEGACY_CACHE_DIR)):
            d = os.path.join(LEGACY_CACHE_DIR, method)
            if not os.path.isdir(d): continue
            files = [f for f in os.listdir(d) if f.endswith(".json")]
            for fn in files:
                files_seen += 1
                fp = os.path.join(d, fn)
                try:
                    with open(fp) as fh: entry = json.load(fh)
                except Exception:
                    errors += 1; continue
                # File name = hash; recompute from (method, params) for safety
                params = entry.get("params")
                if params is None: errors += 1; continue
                h = _key(method, params)
                # Parse the ISO timestamp back to epoch
                try:
                    ts_iso = entry.get("fetched_at") or ""
                    ts = int(datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp())
                except Exception:
                    ts = int(time.time())
                batch.append((method, h,
                              json.dumps(params, default=str),
                              json.dumps(entry.get("result"), default=str),
                              entry.get("status") or "ok",
                              ts))
                if len(batch) >= batch_size:
                    with _conn_lock:
                        c.executemany("INSERT OR IGNORE INTO rpc_cache_responses VALUES (?, ?, ?, ?, ?, ?)", batch)
                    rows_inserted += len(batch)
                    if verbose: print(f"  migrated {rows_inserted:,} rows… ({errors} errors)", flush=True)
                    batch = []
        if batch:
            with _conn_lock:
                c.executemany("INSERT OR IGNORE INTO rpc_cache_responses VALUES (?, ?, ?, ?, ?, ?)", batch)
            rows_inserted += len(batch)
    finally:
        c.close()
    if verbose:
        print(f"migration done: {files_seen:,} files seen, {rows_inserted:,} rows inserted, {errors} errors")
    return {"files_seen": files_seen, "rows_inserted": rows_inserted, "errors": errors}


if __name__ == "__main__":
    print(json.dumps(stats(), indent=2))

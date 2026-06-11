"""Position watermark cache — short-circuits per-position sig walks when the
on-chain account data hasn't changed since the last refresh.

Hashes the position PDA's account data (already in memory from
getProgramAccounts) and compares to the stored hash from the previous run.
If equal, no on-chain state change → no new events possible → skip the
expensive getSignaturesForAddress + getTransaction loop for this position.

Threading: the helpers operate on the connection passed in. The walkers
pre-compute `unchanged_set` in the main thread before fanning out to the
ThreadPoolExecutor, then update_many() in the main thread after the pool
joins. This avoids SQLite cross-thread errors.

Usage:
    import position_watermark as pw
    pw.ensure_table(con)
    # In main thread, before threading:
    pos_hashes = [(p['pubkey'], p['data_hash']) for p in positions]
    unchanged = pw.unchanged_set(con, pos_hashes)
    # In walk() workers:
    if pos_pubkey in unchanged:
        # skip sig walk; reuse cached events
        ...
    else:
        # walk events, then queue (pos_pubkey, data_hash) for update
        to_update.append((pos_pubkey, data_hash))
    # After pool joins, in main thread:
    pw.update_many(con, to_update)
"""
import hashlib
import sqlite3
import time


SCHEMA = """
CREATE TABLE IF NOT EXISTS position_watermark (
    pos_pubkey  TEXT PRIMARY KEY,
    data_hash   TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
)
"""


def ensure_table(con: sqlite3.Connection) -> None:
    con.execute(SCHEMA)


def hash_data(account_data: bytes) -> str:
    """16-char hex digest. Short to keep the table tiny; collisions are
    astronomically rare for a per-position deduplication signal."""
    return hashlib.sha256(account_data).hexdigest()[:16]


def unchanged_set(con: sqlite3.Connection, pos_hashes: list) -> set:
    """Given [(pos_pubkey, current_hash), ...], return the set of pos_pubkeys
    whose stored data_hash equals the current_hash (i.e. on-chain state has
    not changed since the last walk).

    Single bulk query — pre-compute in main thread before fanning to a pool."""
    if not pos_hashes: return set()
    pubkey_to_hash = {p: h for p, h in pos_hashes}
    pubkeys = list(pubkey_to_hash.keys())
    unchanged = set()
    # Chunk to keep SQL "IN (?, ?, ...)" under ~999 (SQLite host parameter limit)
    CHUNK = 900
    for i in range(0, len(pubkeys), CHUNK):
        chunk = pubkeys[i:i+CHUNK]
        placeholders = ','.join('?' * len(chunk))
        rows = con.execute(
            f"SELECT pos_pubkey, data_hash FROM position_watermark WHERE pos_pubkey IN ({placeholders})",
            chunk
        ).fetchall()
        for pubkey, stored_hash in rows:
            if pubkey_to_hash.get(pubkey) == stored_hash:
                unchanged.add(pubkey)
    return unchanged


def update_many(con: sqlite3.Connection, items: list) -> None:
    """Bulk upsert; items is a list of (pos_pubkey, data_hash) tuples."""
    if not items: return
    ts = int(time.time())
    con.executemany(
        "INSERT OR REPLACE INTO position_watermark(pos_pubkey, data_hash, updated_at) VALUES (?,?,?)",
        [(p, h, ts) for p, h in items]
    )


def stats(con: sqlite3.Connection) -> dict:
    row = con.execute("SELECT COUNT(*), MIN(updated_at), MAX(updated_at) FROM position_watermark").fetchone()
    return {'rows': row[0] or 0, 'oldest_ts': row[1], 'newest_ts': row[2]}

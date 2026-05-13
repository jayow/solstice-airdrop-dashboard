"""SQLite data layer for Solstice S2 flares.

Single file lives at data/solstice.db. WAL mode for concurrent reads.

Tables:
  - wallets            : per-wallet metadata (cohort, classification, S1 status, last activity)
  - wallet_quests      : current per-wallet-per-quest flare values (what build_data.py reads)
  - quest_cache        : raw extract results per wallet × quest module (replaces data/quest_cache/)
  - walker_outputs     : pool-state walker results per wallet × quest
  - flares_snapshots   : append-only daily snapshots (inflation tracking)
  - wallet_atas        : cached ATA discovery per wallet × mint (avoids re-querying getTokenAccountsByOwner)

All writes happen inside transactions. Connections use WAL mode.
"""
import os, sqlite3, json, time, threading
from contextlib import contextmanager
from typing import Iterable

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(ROOT, 'data', 'solstice.db')

# One thread-local connection per thread (sqlite3 connections aren't multi-thread by default)
_tls = threading.local()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=30000')
    return conn


def conn() -> sqlite3.Connection:
    """Thread-local connection. Reuses an existing handle if one is open."""
    c = getattr(_tls, 'conn', None)
    if c is None:
        c = _connect()
        _tls.conn = c
    return c


@contextmanager
def txn():
    """Atomic transaction context — auto-commits on success, rolls back on exception."""
    c = conn()
    c.execute('BEGIN')
    try:
        yield c
        c.execute('COMMIT')
    except Exception:
        c.execute('ROLLBACK')
        raise


SCHEMA = """
-- =====================================================================
-- wallets : per-wallet metadata
-- =====================================================================
CREATE TABLE IF NOT EXISTS wallets (
    wallet           TEXT PRIMARY KEY,
    first_seen_ts    INTEGER,                 -- earliest on-chain activity ts we've recorded
    last_active_ts   INTEGER,                 -- latest on-chain activity ts (drives incremental refresh)
    classification   TEXT,                    -- 'real_user' | 'passive_user' | 'institution' | 'unclassified'
    cohort           TEXT,
    is_s1            INTEGER DEFAULT 0,       -- 1 if in S1 registration set
    in_partner_footprint INTEGER DEFAULT 0,
    in_exponent      INTEGER DEFAULT 0,
    n_protocols      INTEGER DEFAULT 0,
    notes            TEXT
);
CREATE INDEX IF NOT EXISTS idx_wallets_active ON wallets(last_active_ts);
CREATE INDEX IF NOT EXISTS idx_wallets_s1     ON wallets(is_s1);
CREATE INDEX IF NOT EXISTS idx_wallets_class  ON wallets(classification);

-- =====================================================================
-- wallet_quests : current per-wallet-per-quest flare values
-- =====================================================================
CREATE TABLE IF NOT EXISTS wallet_quests (
    wallet      TEXT,
    quest       TEXT,                          -- e.g. 'S2_EXPONENT_YIELD_USX_JUN26'
    flares      REAL    NOT NULL DEFAULT 0,
    source      TEXT,                          -- 'cache' | 'walker' | 'audit_fallback'
    updated_at  INTEGER,
    PRIMARY KEY (wallet, quest)
);
CREATE INDEX IF NOT EXISTS idx_wallet_quests_quest_flares
    ON wallet_quests(quest, flares DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_quests_wallet_total
    ON wallet_quests(wallet, flares DESC);

-- =====================================================================
-- quest_cache : raw extract results per (wallet, quest_key) — replaces data/quest_cache/
-- =====================================================================
CREATE TABLE IF NOT EXISTS quest_cache (
    wallet           TEXT,
    quest_key        TEXT,                     -- module's cache_key() — e.g. 'S2_HOLD_USX', 'S2_KAMINO'
    raw_json         TEXT NOT NULL,            -- JSON-serialized extract output
    watermark_slot   INTEGER DEFAULT 0,
    watermark_ts     INTEGER DEFAULT 0,
    extracted_at     INTEGER,
    schema_version   INTEGER DEFAULT 1,
    PRIMARY KEY (wallet, quest_key)
);
CREATE INDEX IF NOT EXISTS idx_quest_cache_age
    ON quest_cache(quest_key, watermark_ts);

-- =====================================================================
-- walker_outputs : authoritative per (walker, wallet, quest) flares
-- =====================================================================
CREATE TABLE IF NOT EXISTS walker_outputs (
    walker      TEXT,                          -- 'walk_s2_lp' | 'walk_s2_kamino' | ...
    wallet      TEXT,
    quest       TEXT,
    flares      REAL NOT NULL DEFAULT 0,
    refreshed_at INTEGER,
    PRIMARY KEY (walker, wallet, quest)
);
CREATE INDEX IF NOT EXISTS idx_walker_outputs_quest
    ON walker_outputs(quest, flares DESC);
CREATE INDEX IF NOT EXISTS idx_walker_outputs_wallet
    ON walker_outputs(wallet);

-- =====================================================================
-- flares_snapshots : append-only daily totals
-- =====================================================================
CREATE TABLE IF NOT EXISTS flares_snapshots (
    ts                INTEGER,                 -- unix ts of snapshot
    date_utc          TEXT,                    -- YYYY-MM-DD
    source            TEXT,                    -- 'our_framework' | 'solstice_dashboard'
    universe_size     INTEGER,
    grand_total       REAL,
    quest_totals_json TEXT,                    -- JSON {quest: flares}
    PRIMARY KEY (ts, source)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_source_date
    ON flares_snapshots(source, date_utc);

-- =====================================================================
-- wallet_atas : cached ATA discovery (wallet → mint → ata addresses)
-- =====================================================================
CREATE TABLE IF NOT EXISTS wallet_atas (
    wallet      TEXT,
    mint        TEXT,
    ata         TEXT,
    token_program TEXT,                        -- legacy SPL vs Token-2022
    discovered_at INTEGER,
    PRIMARY KEY (wallet, mint, ata)
);
CREATE INDEX IF NOT EXISTS idx_atas_wallet ON wallet_atas(wallet);
CREATE INDEX IF NOT EXISTS idx_atas_mint   ON wallet_atas(mint);

-- =====================================================================
-- schema metadata
-- =====================================================================
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def init():
    """Create tables and indexes if they don't exist."""
    c = conn()
    for stmt in SCHEMA.split(';'):
        s = stmt.strip()
        if s: c.execute(s)
    c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')")
    c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('initialized_at', strftime('%s','now'))")


def health() -> dict:
    """Return basic counts per table for quick sanity-check."""
    init()
    c = conn()
    tables = ['wallets', 'wallet_quests', 'quest_cache', 'walker_outputs',
              'flares_snapshots', 'wallet_atas']
    return {t: c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tables}


# =====================================================================
# Convenience accessors (used by build_data.py and refactored modules)
# =====================================================================

def upsert_wallet_quest(wallet: str, quest: str, flares: float, source: str = None):
    conn().execute(
        'INSERT OR REPLACE INTO wallet_quests(wallet, quest, flares, source, updated_at) '
        'VALUES (?, ?, ?, ?, strftime("%s","now"))',
        (wallet, quest, flares, source)
    )


def get_wallet_flares(wallet: str) -> dict:
    """Return {quest: flares} for a wallet."""
    rows = conn().execute(
        'SELECT quest, flares FROM wallet_quests WHERE wallet=? AND flares > 0',
        (wallet,)
    ).fetchall()
    return {r['quest']: r['flares'] for r in rows}


def all_wallet_totals() -> Iterable[dict]:
    """Yield {wallet, quest, flares} ordered by wallet."""
    for r in conn().execute('SELECT wallet, quest, flares FROM wallet_quests WHERE flares > 0 ORDER BY wallet'):
        yield dict(r)


def quest_totals() -> dict:
    rows = conn().execute(
        'SELECT quest, SUM(flares) AS total FROM wallet_quests GROUP BY quest'
    ).fetchall()
    return {r['quest']: r['total'] for r in rows}


def get_cache(wallet: str, quest_key: str) -> dict:
    row = conn().execute(
        'SELECT raw_json, watermark_slot, watermark_ts, extracted_at, schema_version '
        'FROM quest_cache WHERE wallet=? AND quest_key=?',
        (wallet, quest_key)
    ).fetchone()
    if not row: return None
    return {
        'raw': json.loads(row['raw_json']),
        'watermark_slot': row['watermark_slot'],
        'watermark_ts': row['watermark_ts'],
        'extracted_at': row['extracted_at'],
        'schema_version': row['schema_version'],
    }


def put_cache(wallet: str, quest_key: str, raw: dict,
              watermark_slot: int = 0, watermark_ts: int = 0):
    conn().execute(
        'INSERT OR REPLACE INTO quest_cache '
        '(wallet, quest_key, raw_json, watermark_slot, watermark_ts, extracted_at, schema_version) '
        'VALUES (?, ?, ?, ?, ?, strftime("%s","now"), 1)',
        (wallet, quest_key, json.dumps(raw, separators=(',', ':')),
         int(watermark_slot or 0), int(watermark_ts or 0))
    )


def upsert_walker_output(walker: str, wallet: str, quest: str, flares: float):
    conn().execute(
        'INSERT OR REPLACE INTO walker_outputs(walker, wallet, quest, flares, refreshed_at) '
        'VALUES (?, ?, ?, ?, strftime("%s","now"))',
        (walker, wallet, quest, flares)
    )


def walker_outputs_by_quest(quest: str) -> dict:
    """{wallet: flares} for a quest from walker_outputs."""
    rows = conn().execute(
        'SELECT wallet, SUM(flares) AS total FROM walker_outputs WHERE quest=? GROUP BY wallet',
        (quest,)
    ).fetchall()
    return {r['wallet']: r['total'] for r in rows}


def wallets_active_since(cutoff_ts: int) -> list:
    rows = conn().execute(
        'SELECT wallet FROM wallets WHERE last_active_ts >= ?', (cutoff_ts,)
    ).fetchall()
    return [r['wallet'] for r in rows]


if __name__ == '__main__':
    init()
    print('schema initialized at', DB_PATH)
    print('health:', health())

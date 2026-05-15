"""DB helpers for the pool-state walkers.

Replaces the legacy pattern of reading quest_results.jsonl to find "wallets to walk".
Walkers now self-enumerate from on-chain (their primary source) AND write their
output to walker_outputs table.

Conventions:
  - Each walker has a stable `walker_name` (e.g. 'walk_s2_kamino').
  - `upsert(walker, wallet, quest, flares)` — atomic per row.
  - `prune(walker)` — wipe all rows owned by this walker (use before a fresh run
                      to remove wallets that no longer have a position).
  - `wallets_for(quest)` — query other walkers' outputs (useful for cross-checks).
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db


def prune(walker_name: str):
    """Delete all rows for this walker — call at the start of a clean re-run."""
    db.init()
    with db.txn() as c:
        c.execute('DELETE FROM walker_outputs WHERE walker=?', (walker_name,))


def upsert(walker_name: str, wallet: str, quest: str, flares: float):
    db.conn().execute(
        'INSERT OR REPLACE INTO walker_outputs(walker, wallet, quest, flares, refreshed_at) '
        'VALUES (?, ?, ?, ?, strftime("%s","now"))',
        (walker_name, wallet, quest, float(flares or 0))
    )


def upsert_many(walker_name: str, rows):
    """rows = iterable of (wallet, quest, flares)."""
    db.init()
    now = int(time.time())
    with db.txn() as c:
        for wallet, quest, flares in rows:
            c.execute(
                'INSERT OR REPLACE INTO walker_outputs(walker, wallet, quest, flares, refreshed_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (walker_name, wallet, quest, float(flares or 0), now)
            )
            # Make sure wallet exists in metadata table
            c.execute("INSERT OR IGNORE INTO wallets(wallet, classification) VALUES (?, 'unclassified')", (wallet,))


def sync_to_wallet_quests(walker_name: str, quests: list):
    """After a walker rewrites its outputs, mirror them into wallet_quests.

    For each quest in `quests`:
      - Set wallet_quests.flares = walker_outputs.flares for wallets in this walker
      - Set wallet_quests.flares = 0 for wallets NOT in this walker (clears stale)

    This makes the walker the authoritative source for these quests.
    """
    db.init()
    with db.txn() as c:
        for q in quests:
            # Zero stale rows first
            c.execute("""
                UPDATE wallet_quests SET flares = 0, source = 'walker_zeroed', updated_at = strftime('%s','now')
                WHERE quest = ? AND wallet NOT IN (
                    SELECT wallet FROM walker_outputs WHERE walker = ? AND quest = ?
                )
            """, (q, walker_name, q))
            # Upsert fresh values. Also INSERT OR IGNORE into wallets metadata
            # so downstream build_data.py's INNER JOIN doesn't drop wallets
            # whose only on-chain footprint is via a walker (ghost wallets).
            for r in c.execute(
                'SELECT wallet, flares FROM walker_outputs WHERE walker=? AND quest=?',
                (walker_name, q)
            ).fetchall():
                c.execute("INSERT OR IGNORE INTO wallets(wallet, classification) VALUES (?, 'unclassified')",
                          (r['wallet'],))
                c.execute(
                    'INSERT OR REPLACE INTO wallet_quests(wallet, quest, flares, source, updated_at) '
                    'VALUES (?, ?, ?, ?, strftime("%s","now"))',
                    (r['wallet'], q, r['flares'], f'walker:{walker_name}')
                )


def wallets_with_quest_above(quest: str, threshold: float = 0) -> list:
    db.init()
    return [r['wallet'] for r in db.conn().execute(
        'SELECT wallet FROM wallet_quests WHERE quest=? AND flares > ?',
        (quest, threshold)
    )]


def write_coverage(walker: str, quest: str, pool_tvl_usd: float,
                   tracked_tvl_usd: float, n_positions: int = 0):
    """Persist this walker's TVL coverage for a quest.

    Called by each walker at end-of-run. Audit reads (tracked / pool) to detect
    "we never enumerated this account" bugs — the only failure mode that the
    output-layer audits CANNOT catch."""
    db.init()
    db.conn().execute(
        'INSERT OR REPLACE INTO walker_coverage'
        '(walker, quest, pool_tvl_usd, tracked_tvl_usd, n_positions, refreshed_at) '
        'VALUES (?, ?, ?, ?, ?, strftime("%s","now"))',
        (walker, quest, float(pool_tvl_usd or 0), float(tracked_tvl_usd or 0),
         int(n_positions or 0))
    )

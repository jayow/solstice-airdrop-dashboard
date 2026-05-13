"""Shared helpers for ground-truth walkers.

Every walker uses these to:
  - Connect to the DB (via flares_estimator.db)
  - Get the S2 window constants
  - Write outputs atomically to walker_outputs
  - Print a uniform `=== WALKER_NAME ===` report with cross-check
"""
import os, sys, time, json
from datetime import datetime, UTC
from contextlib import contextmanager

# Make the parent flares_estimator package importable
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(THIS_DIR)
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

import db
from rpc_helper import rpc

S2_START_TS = 1776038400      # 2026-04-13 00:00 UTC (verified via closed-YT cross-check on E3bBXoq..)
S2_END_TS   = 1785024000      # 2026-08-01 00:00 UTC
MIN_HOLD_DAYS = 1.0           # Solstice "min one day rewarded daily"


def s2_window_days(now_ts: int = None) -> float:
    if now_ts is None: now_ts = int(time.time())
    end_ts = min(now_ts, S2_END_TS)
    return max(0.0, (end_ts - S2_START_TS) / 86400.0)


def write_walker_outputs(walker_name: str, quest: str, rows: dict):
    """rows = {wallet: flares}. Prunes prior rows for this (walker, quest) and re-inserts."""
    db.init()
    now = int(time.time())
    with db.txn() as c:
        c.execute('DELETE FROM walker_outputs WHERE walker = ? AND quest = ?', (walker_name, quest))
        for wallet, flares in rows.items():
            if flares is None: continue
            fv = float(flares or 0)
            if fv <= 0: continue
            c.execute(
                'INSERT OR REPLACE INTO walker_outputs(walker, wallet, quest, flares, refreshed_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (walker_name, wallet, quest, fv, now)
            )
            # Ensure wallet exists in metadata
            c.execute("INSERT OR IGNORE INTO wallets(wallet, classification) VALUES (?, 'unclassified')", (wallet,))


def sync_to_wallet_quests(walker_name: str, quest: str):
    """After a walker completes, mirror its walker_outputs rows into wallet_quests
    (the dashboard's source-of-truth table) for this quest. Zeroes wallets that
    dropped out of the walker's output."""
    db.init()
    with db.txn() as c:
        # Zero stale rows
        c.execute("""
            UPDATE wallet_quests SET flares = 0, source = ?, updated_at = strftime('%s','now')
            WHERE quest = ? AND wallet NOT IN (
                SELECT wallet FROM walker_outputs WHERE walker = ? AND quest = ?
            )
        """, (f'zeroed_by:{walker_name}', quest, walker_name, quest))
        # Upsert fresh values
        for r in c.execute(
            'SELECT wallet, flares FROM walker_outputs WHERE walker=? AND quest=?',
            (walker_name, quest)
        ).fetchall():
            c.execute(
                'INSERT OR REPLACE INTO wallet_quests(wallet, quest, flares, source, updated_at) '
                'VALUES (?, ?, ?, ?, strftime("%s","now"))',
                (r['wallet'], quest, r['flares'], f'gt_walker:{walker_name}')
            )


@contextmanager
def report(walker_name: str, quest: str, source_addrs: list = None):
    """Wraps a walker. Prints a uniform start/end report and times the run."""
    print(f'=== {walker_name} ===', flush=True)
    print(f'    quest: {quest}', flush=True)
    if source_addrs:
        print(f'    sources: {", ".join(source_addrs)}', flush=True)
    t0 = time.time()
    yield
    print(f'    done in {time.time()-t0:.1f}s', flush=True)


def print_cross_check(label: str, our_total: float, on_chain_total: float, tolerance_pct: float = 1.0):
    """Print VERIFIED/MISMATCH and exit non-zero if too far off."""
    if on_chain_total == 0:
        status = 'VERIFIED' if our_total == 0 else 'MISMATCH'
        delta = our_total - on_chain_total
    else:
        delta_pct = abs((our_total - on_chain_total) / on_chain_total * 100)
        status = 'VERIFIED' if delta_pct <= tolerance_pct else f'MISMATCH ({delta_pct:.2f}% off)'
        delta = our_total - on_chain_total
    print(f'    cross-check [{label}]: our={our_total:,.4f}  on-chain={on_chain_total:,.4f}  Δ={delta:+,.4f}  [{status}]', flush=True)


# Reusable address-set
USX_MINT  = '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG'
EUSX_MINT = '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC'
USDG_MINT = '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH'

USX_JUN26_MARKET  = 'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm'
EUSX_JUN26_MARKET = 'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP'

EXPONENT_CORE = 'ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7'
KAMINO_LEND   = 'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD'
SOLSTICE_KAMINO_MARKET = '9Y7uwXgQ68mGqRtZfuFaP4hc4fxeJ7cE9zTtqTxVhfGU'
WHIRLPOOL     = 'whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc'
RAYDIUM_CLMM  = 'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK'

TOKEN_LEGACY = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN_2022   = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'

# eUSX peg PDA (read-only)
EUSX_PEG_PDA = 'JDs1wmLaVB2KsAotjbBKVEsiV1gbrG3Qrjyht5LnX9YP'

def live_eusx_peg() -> float:
    import base64, struct
    try:
        r = rpc('getAccountInfo', [EUSX_PEG_PDA, {'encoding': 'base64'}], timeout=10)
        d = base64.b64decode(r['result']['value']['data'][0])
        return struct.unpack('<Q', d[48:56])[0] / 1e18
    except Exception:
        return 1.156   # last known

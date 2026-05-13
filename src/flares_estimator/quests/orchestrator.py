"""
ELT orchestrator. For a given wallet:
  1. EXTRACT — run each quest module's extract(), persist raw to data/quest_cache/.
  2. LOAD — handled by extract() via base.save_quest_cache (atomic, watermarked).
  3. TRANSFORM — read cache, compute flares per quest. Pure, no RPC.

Modules registered in QUEST_MODULES below. Each owns one or more quest_codes.
Adding a new quest = drop a module in quests/ + add to this list.
"""
import os, sys, time, json
from typing import Dict, List, Type
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from .base import QuestExtractor, load_quest_cache
from .hold_usx import HoldUSXExtractor
from .hold_eusx import HoldEUSXExtractor
from .exponent_yt import ExponentYTExtractor
from .exponent_lp import ExponentLPExtractor
from .partner_state import KaminoExtractor, OrcaExtractor, RaydiumExtractor, LoopscaleExtractor

QUEST_MODULES: List[Type[QuestExtractor]] = [
    HoldUSXExtractor,
    HoldEUSXExtractor,
    ExponentYTExtractor,
    ExponentLPExtractor,
    KaminoExtractor,
    OrcaExtractor,
    RaydiumExtractor,
    LoopscaleExtractor,
]

# S2 referral bonus is SIWS-gated — we cannot derive it on-chain. Flag it
# explicitly here so the dashboard reports "0 (not extractable)" rather than
# silently omitting it.
NON_EXTRACTABLE_QUESTS = {"S2_REFERRAL_BONUS"}

# Empty — every Solstice S2 quest with on-chain proof is now wired up.
GATED_OFF_QUESTS: set = set()


def run_wallet(wallet: str, now_ts: int = None, force_refresh: bool = False) -> Dict[str, float]:
    """Run every quest module for this wallet. Returns {quest_code: flares}.
    First call extracts (RPC-bound). Subsequent calls hit cache (fast)."""
    if now_ts is None: now_ts = int(time.time())
    flares: Dict[str, float] = {}
    for cls in QUEST_MODULES:
        try:
            res = cls().run(wallet, now_ts, force_refresh=force_refresh)
            for q, f in (res or {}).items():
                flares[q] = flares.get(q, 0.0) + float(f or 0)
        except Exception as e:
            # Don't let one quest's failure kill the run; log & continue.
            print(f"  WARN {wallet} {cls.__name__}: {e}", file=sys.stderr)
    for q in NON_EXTRACTABLE_QUESTS | GATED_OFF_QUESTS:
        flares.setdefault(q, 0.0)
    return flares


def transform_wallet_from_cache(wallet: str, now_ts: int = None) -> Dict[str, float]:
    """Pure transform: read each cached quest entry, compute flares. NO RPC.
    Returns empty dict when cache is missing — caller handles fallback."""
    if now_ts is None: now_ts = int(time.time())
    flares: Dict[str, float] = {}
    for cls in QUEST_MODULES:
        m = cls()
        cached = load_quest_cache(m.cache_key(), wallet)
        if not cached: continue
        try:
            res = m.transform(cached["raw"], now_ts)
            for q, f in (res or {}).items():
                flares[q] = flares.get(q, 0.0) + float(f or 0)
        except Exception:
            continue
    for q in NON_EXTRACTABLE_QUESTS | GATED_OFF_QUESTS:
        flares.setdefault(q, 0.0)
    return flares


def run_bulk(wallets: List[str], workers: int = 8, force_refresh: bool = False,
              progress_every: int = 100) -> Dict[str, Dict[str, float]]:
    """Run extract+transform for many wallets in parallel."""
    out = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(run_wallet, w, force_refresh=force_refresh): w for w in wallets}
        for i, fut in enumerate(as_completed(futs), 1):
            w = futs[fut]
            try:
                out[w] = fut.result()
            except Exception as e:
                out[w] = {"_error": str(e)}
            if i % progress_every == 0 or i == len(wallets):
                rate = i / max(1, time.time() - t0)
                eta = (len(wallets) - i) / max(1, rate)
                print(f"  {i:>5}/{len(wallets):,}  rate={rate:.1f}/s  eta={eta/60:.1f}min", flush=True)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("wallet", nargs="?", help="Single wallet for ad-hoc test")
    ap.add_argument("--bulk", help="path to wallets file (one per line)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--force-refresh", action="store_true")
    args = ap.parse_args()

    if args.bulk:
        wallets = [w.strip() for w in open(args.bulk) if w.strip()]
        results = run_bulk(wallets, workers=args.workers, force_refresh=args.force_refresh)
        # Upsert per-wallet flares into the DB (wallet_quests). This is per-wallet
        # atomic — never overwrites OTHER wallets. The old jsonl write is preserved
        # as an append-friendly fallback so legacy scripts still see fresh data.
        try:
            from flares_estimator import db as _db  # use package import to avoid path issues
        except Exception:
            import sys, os as _os
            sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
            import db as _db
        _db.init()
        with _db.txn() as conn:
            for w, q in results.items():
                # Ensure wallet exists
                conn.execute(
                    "INSERT OR IGNORE INTO wallets(wallet, classification) VALUES (?, 'unclassified')",
                    (w,)
                )
                for quest, flares in (q or {}).items():
                    fv = float(flares or 0)
                    conn.execute(
                        "INSERT OR REPLACE INTO wallet_quests(wallet, quest, flares, source, updated_at) "
                        "VALUES (?, ?, ?, 'orchestrator', strftime('%s','now'))",
                        (w, quest, fv)
                    )
                # Update last_active_ts to now (signals this wallet was just refreshed)
                conn.execute(
                    "UPDATE wallets SET last_active_ts = strftime('%s','now') WHERE wallet=?",
                    (w,)
                )
        print(f"Upserted {len(results)} wallets into DB.wallet_quests")
    elif args.wallet:
        f = run_wallet(args.wallet, force_refresh=args.force_refresh)
        print(f"\n{args.wallet}:")
        total = 0
        for q, v in sorted(f.items(), key=lambda x: -x[1]):
            if v > 0:
                print(f"  {q:38s} {v:>14,.2f}")
                total += v
        print(f"  TOTAL: {total:,.2f}")
    else:
        ap.print_help()

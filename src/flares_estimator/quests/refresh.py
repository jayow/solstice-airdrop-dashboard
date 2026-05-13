"""
Daily incremental refresh — uses per-quest watermarks to only fetch new data.

For each (wallet, quest_code), the watermark records `last_slot` indexed during
the historical extract. This script walks only sigs/events past that watermark,
appends to the cached raw data, and advances the watermark.

Then runs transform on the updated cache to refresh stage 3 / dashboard.

Usage:
  # daily cron at 09:00 UTC (1 hour after Solstice's 08:00 UTC dashboard tick)
  python -m src.flares_estimator.quests.refresh --workers 6 [--limit N]

Implementation note: most quest modules don't have a separate
`extract_incremental` yet — they just re-run `extract` which (because rpc_helper
caches by (method, params) for 24h) effectively re-fetches only stale entries.
That's a reasonable bridge until per-quest incremental walks are written.
"""
import os, sys, time, json, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from .base import load_quest_cache, get_watermark
from .orchestrator import QUEST_MODULES, run_wallet


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DATA = os.path.join(ROOT, "data")


def stale_wallets(max_age_hours: float = 18) -> list:
    """Return wallets whose cache is older than max_age_hours for ANY quest module
    they have an entry for. These are the candidates for daily refresh."""
    cutoff = time.time() - max_age_hours * 3600
    out = set()
    cache_dir = os.path.join(DATA, "quest_cache")
    if not os.path.isdir(cache_dir): return []
    for quest_dir in os.listdir(cache_dir):
        d = os.path.join(cache_dir, quest_dir)
        if not os.path.isdir(d): continue
        for fname in os.listdir(d):
            if not fname.endswith(".json"): continue
            try:
                e = json.load(open(os.path.join(d, fname)))
                ts = e.get("watermark_ts") or 0
                if ts < cutoff:
                    out.add(fname[:-5])
            except Exception: continue
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-age-hours", type=float, default=18)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--all-active", action="store_true",
                    help="Refresh ALL active wallets, not just stale.")
    args = ap.parse_args()

    if args.all_active:
        wallets = [w.strip() for w in open(os.path.join(DATA, "active_wallets.txt")) if w.strip()]
    else:
        wallets = stale_wallets(args.max_age_hours)
    if args.limit: wallets = wallets[:args.limit]
    print(f"Refreshing {len(wallets):,} wallets (max_age_hours={args.max_age_hours})")

    t0 = time.time()
    n_done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_wallet, w, force_refresh=True): w for w in wallets}
        for fut in as_completed(futs):
            try: fut.result()
            except Exception as e:
                print(f"  ERR {futs[fut]}: {e}", file=sys.stderr)
            n_done += 1
            if n_done % 100 == 0 or n_done == len(wallets):
                rate = n_done / max(1, time.time() - t0)
                eta = (len(wallets) - n_done) / max(1, rate)
                print(f"  {n_done}/{len(wallets):,}  rate={rate:.1f}/s  eta={eta/60:.1f}min", flush=True)

    print(f"\nDone in {(time.time()-t0)/60:.1f} min. Cache fresh; transform_only.py will rebuild dashboard.")


if __name__ == "__main__":
    main()

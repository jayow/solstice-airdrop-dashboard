"""Force-refresh wallets whose HOLD/YT caches look empty.

Background: the orchestrator stores cache results regardless of content.
If a previous run hit a transient RPC failure or missed a position, the
empty result gets cached and subsequent runs short-circuit on `cache exists
→ skip extract` — the wallet stays stuck at zero forever.

This pass scans for `quest_cache` rows whose `raw_json` is suspiciously small
(empty atas list for HOLD, empty positions_by_market for YT), then forces
a re-extract for that specific (wallet, quest) pair. Most empty caches are
legitimately zero — but the few that aren't get recovered.

Run after the daily orchestrator pass:
  python3 src/flares_estimator/retry_empty_caches.py [--limit N] [--workers N]
"""
import os, sys, time, json, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as fdb
from quests import hold_usx, hold_eusx, exponent_yt


# Cache-size thresholds: below these, treat the cache as "suspiciously empty"
# and worth a retry. Tuned by inspecting actual empty vs populated caches.
EMPTY_THRESHOLDS = {
    'S2_HOLD_USX':     100,   # populated has many timeline points + atas list; empty is ~85 bytes
    'S2_HOLD_EUSX':    100,   # same
    'S2_EXPONENT_YT':  200,   # empty `{"positions_by_market":{}, "_watermark":...}` is ~66 bytes
}

EXTRACTORS = {
    'S2_HOLD_USX':    hold_usx.HoldUSXExtractor,
    'S2_HOLD_EUSX':   hold_eusx.HoldEUSXExtractor,
    'S2_EXPONENT_YT': exponent_yt.ExponentYTExtractor,
}


def find_candidates(cache_key: str, max_size: int, limit: int | None = None) -> list[str]:
    con = fdb.conn()
    sql = "SELECT wallet FROM quest_cache WHERE quest_key=? AND length(raw_json) < ?"
    params = [cache_key, max_size]
    if limit:
        sql += " LIMIT ?"; params.append(limit)
    return [r['wallet'] for r in con.execute(sql, params)]


def retry_one(wallet: str, cache_key: str) -> dict:
    """Force-refresh one (wallet, cache_key). Returns transform output or None."""
    ext_cls = EXTRACTORS[cache_key]
    ext = ext_cls()
    try:
        return ext.run(wallet, int(time.time()), force_refresh=True)
    except Exception as e:
        return {'_error': str(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=None, help='Cap per-quest retry count (debug)')
    ap.add_argument('--workers', type=int, default=4, help='Concurrent retries (RPC-heavy)')
    ap.add_argument('--only', type=str, default=None, help='Only this quest_key (e.g. S2_HOLD_USX)')
    args = ap.parse_args()

    fdb.init()
    con = fdb.conn()

    keys = list(EXTRACTORS) if not args.only else [args.only]
    total_recovered = 0
    total_recovered_flares = 0.0

    for key in keys:
        candidates = find_candidates(key, EMPTY_THRESHOLDS[key], args.limit)
        print(f'\n=== {key}: {len(candidates):,} suspiciously-empty caches ===', flush=True)
        if not candidates:
            continue
        t0 = time.time()
        recovered = 0
        recovered_flares = 0.0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(retry_one, w, key): w for w in candidates}
            for i, fut in enumerate(as_completed(futs), 1):
                w = futs[fut]
                out = fut.result() or {}
                if out and not out.get('_error'):
                    for q, v in out.items():
                        v = float(v or 0)
                        if v > 0:
                            prev = con.execute(
                                'SELECT flares FROM wallet_quests WHERE wallet=? AND quest=?',
                                (w, q)
                            ).fetchone()
                            prev_v = float((prev['flares'] if prev else 0) or 0)
                            if v - prev_v > 0.5:
                                fdb.upsert_wallet_quest(w, q, v, source='retry_empty_cache')
                                recovered_flares += (v - prev_v)
                                recovered += 1
                if i % 100 == 0 or i == len(candidates):
                    rate = i / max(1, time.time() - t0)
                    eta = (len(candidates) - i) / max(1, rate)
                    print(f'  {i:>5}/{len(candidates):,}  recovered={recovered}  +{recovered_flares:,.0f}f  rate={rate:.1f}/s  eta={eta/60:.1f}min', flush=True)
        con.commit()
        total_recovered += recovered
        total_recovered_flares += recovered_flares
        print(f'  {key} done in {time.time()-t0:.0f}s: recovered={recovered}  +{recovered_flares:,.0f} flares')

    print(f'\nGRAND TOTAL: {total_recovered} wallet-quest rows recovered, +{total_recovered_flares:,.0f} flares')


if __name__ == '__main__':
    main()

"""Sample-test that each walker's per-account cursor reaches the chain head.

For each walker, pick N random tracked accounts from quest_cache, fetch the
NEWEST sig on-chain via getSignaturesForAddress(limit=1), compare its ts to
the latest sig ts we have cached for that account. Lag > threshold = our
cursor is behind reality.

Writes one row per sample to walker_freshness; audit.py T4 reads recent rows
and flags walkers with stale cursors.

This is the ONLY check that catches "cursor logic stopped working" — every
other audit is downstream of the cache, so if the cursor's broken the audit
just sees a frozen-but-internally-consistent cache and reports green.

Usage:
    python3 tools/walker_cursor_freshness.py [--n 5] [--walker NAME]
"""
import os, sys, json, sqlite3, time, random, argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src', 'flares_estimator'))
DB = os.path.join(ROOT, 'data', 'solstice.db')

import db as _db
from rpc_helper import rpc

# Configs per walker: how to find sampleable accounts in quest_cache.
#
# Per-position walkers (Orca/Raydium/Kamino/Loopscale) sample per-position
# pubkeys; the comparison is meaningful because each position is exclusively
# touched by its owner.
#
# Vault-driven walkers (LP/YT) walk a SHARED vault PDA that sees txs from
# every user AND many non-event txs (refreshes, fees, etc). Their "latest
# cached event ts" lags chain head by design — most txs don't generate
# events. Freshness on these is a false positive. Skip them here; their
# correctness is covered by walker_saturation + walker_coverage instead.
WALKER_CFGS = {
    'walk_s2_orca':     {'quest_key': 'S2_ORCA',     'event_pubkey_field': 'pos_pubkey'},
    'walk_s2_raydium':  {'quest_key': 'S2_RAYDIUM',  'event_pubkey_field': 'pos_pubkey'},
    'walk_s2_kamino':   {'quest_key': 'S2_KAMINO',   'event_pubkey_field': 'pos_pubkey'},
    'walk_s2_loopscale':{'quest_key': 'S2_LOOPSCALE','event_pubkey_field': 'pos_pubkey'},
}


def latest_chain_sig_ts(pubkey: str) -> int:
    """Fetch newest sig from chain for `pubkey` — bypass cache so we see truth."""
    try:
        r = rpc('getSignaturesForAddress', [pubkey, {'limit': 1}], timeout=20, force_refresh=True)
    except Exception:
        return 0
    res = r.get('result') or []
    if not res: return 0
    return int(res[0].get('blockTime') or 0)


def latest_cached_ts_for_pubkey(con, walker: str, pubkey: str) -> int:
    """For a position-PDA walker, find the latest event ts our cache has
    for any wallet × this pubkey."""
    cfg = WALKER_CFGS[walker]
    quest_key = cfg.get('quest_key')
    if not quest_key: return 0
    rows = con.execute("SELECT raw_json FROM quest_cache WHERE quest_key=?",
                       (quest_key,)).fetchall()
    field = cfg.get('event_pubkey_field') or cfg.get('event_match_field')
    best = 0
    for r in rows:
        try:
            evs = (json.loads(r['raw_json']).get('events') or [])
        except Exception: continue
        for e in evs:
            if e.get(field) == pubkey:
                ts = int(e.get('ts') or 0)
                if ts > best: best = ts
    return best


def sample_position_pubkeys(con, walker: str, n: int) -> list:
    """Pick N random position pubkeys from this walker's quest_cache."""
    cfg = WALKER_CFGS[walker]
    quest_key = cfg['quest_key']
    field = cfg['event_pubkey_field']
    seen = set()
    rows = con.execute(f"SELECT raw_json FROM quest_cache WHERE quest_key=?", (quest_key,)).fetchall()
    for r in rows:
        try:
            evs = (json.loads(r['raw_json']).get('events') or [])
        except Exception: continue
        for e in evs:
            pk = e.get(field)
            if pk: seen.add(pk)
    pool = list(seen)
    random.shuffle(pool)
    return pool[:n]


def check_walker(con, walker: str, n: int) -> list:
    """Sample N pubkeys, compare cache ts vs chain ts. Return list of finding tuples."""
    cfg = WALKER_CFGS[walker]
    if cfg.get('fixed_pubkeys'):
        sample = cfg['fixed_pubkeys']
    else:
        sample = sample_position_pubkeys(con, walker, n)
    findings = []
    for pk in sample:
        chain_ts = latest_chain_sig_ts(pk)
        our_ts = latest_cached_ts_for_pubkey(con, walker, pk)
        lag = max(0, chain_ts - our_ts)
        findings.append((walker, pk, our_ts, chain_ts, lag))
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=5)
    ap.add_argument('--walker', default=None, help='specific walker; default = all')
    args = ap.parse_args()

    _db.init()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    walkers = [args.walker] if args.walker else list(WALKER_CFGS.keys())

    now = int(time.time())
    all_findings = []
    for walker in walkers:
        print(f'== {walker} ==', flush=True)
        try:
            findings = check_walker(con, walker, args.n)
        except Exception as e:
            print(f'  ERROR: {e}', flush=True); continue
        for f in findings:
            walker_, pk, our_ts, chain_ts, lag = f
            print(f'  {pk[:12]}…  our={our_ts}  chain={chain_ts}  lag={lag}s', flush=True)
            con.execute(
                'INSERT INTO walker_freshness '
                '(ts, walker, pubkey, our_latest_ts, chain_latest_ts, lag_seconds) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (now, walker_, pk, our_ts, chain_ts, lag)
            )
        all_findings.extend(findings)
    con.commit()

    # Summary
    if all_findings:
        worst = max(all_findings, key=lambda x: x[4])
        print(f'\nMax lag: {worst[4]}s  ({worst[0]} {worst[1][:12]}…)', flush=True)


if __name__ == '__main__':
    main()

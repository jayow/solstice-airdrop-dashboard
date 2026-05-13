"""Walk S2 LP holders across both Exponent markets (USX-Jun26, eUSX-Jun26).

For each LP-vault sig in [S2_START_TS, now]:
  - Parse tx to get user wallet + LP delta + underlying delta
  - Append to per-wallet timeline

Then integrate LP_balance × per_LP_USD × multiplier over time for each wallet.

Formula (verified at 100-101% on user's S1 data):
  per_LP_USD(t) = |underlying_delta_at_tx| / |LP_delta_at_tx| × peg
  daily_flares = LP_balance × per_LP_USD × mult

Output: data/s2_lp_flares.json  {wallet: {USX: flares, eUSX: flares}}
"""
import os, sys, json, time, base64, base58
from datetime import datetime, UTC
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc
import walker_db

# Per-day eUSX peg lookup. USX market uses constant peg=1.0; eUSX market uses
# peg_at(ts) so we integrate against the historical peg at each segment.
from quests.eusx_peg import peg_at as eusx_peg_at

S2_START_TS = 1776038400
S2_END_TS   = 1785024000   # only used to cap if walking beyond now

# Live eUSX peg
EUSX_PEG_PDA = 'JDs1wmLaVB2KsAotjbBKVEsiV1gbrG3Qrjyht5LnX9YP'
import struct
def get_eusx_peg():
    r = rpc('getAccountInfo', [EUSX_PEG_PDA, {'encoding':'base64'}])
    d = base64.b64decode(r['result']['value']['data'][0])
    return struct.unpack('<Q', d[48:56])[0] / 1e18

MARKETS = {
    'USX-Jun26': {
        'market':       'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm',
        'lp_vault':     'CRPy147RiyosYzEzU4NLVbhZjhy1GGAHtXyvbfFHK736',
        'lp_mint':      'BR2JKV9gPoJfX8A8DkFmo2yNQKCeGipg33oYaZ4EmjbW',
        'underlying':   '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG',
        'mult':         20,
        'peg':          1.0,
        'quest':        'S2_EXPONENT_LP_USX_JUN26',
    },
    'eUSX-Jun26': {
        'market':       'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP',
        'lp_vault':     '3wMTeNVRXsYaMuVMpX6uAXVdX2zi5puk7SbsU5YXts82',
        'lp_mint':      '4GT6g1iKx2TyYCkwt1tERkReQjSUuVE7uh14M5W8v2nn',
        'underlying':   '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC',
        'mult':         10,
        'peg':          None,   # populated from chain
        'quest':        'S2_EXPONENT_LP_EUSX_JUN26',
    },
}


def fetch_all_sigs(addr: str, until_ts: int) -> list:
    """Pull sigs newest→oldest until reaching until_ts."""
    sigs = []
    before = None
    while True:
        params = [addr, {'limit': 1000}]
        if before: params[1]['before'] = before
        r = rpc('getSignaturesForAddress', params)
        batch = r.get('result', []) or []
        if not batch: break
        # Stop early if we pass the cutoff
        keep = [s for s in batch if (s.get('blockTime') or 0) >= until_ts]
        sigs.extend(keep)
        if len(keep) < len(batch): break
        if len(batch) < 1000: break
        before = batch[-1]['signature']
    return sigs


def parse_tx_lp_event(tx: dict, cfg: dict) -> dict:
    """From a tx, extract user_wallet, lp_delta (user's claim change), underlying_delta (user-side eUSX/USX)."""
    meta = tx['meta']
    if meta.get('err'): return None
    pre = {(t['accountIndex'], t['mint']): t for t in meta.get('preTokenBalances', [])}
    post = {(t['accountIndex'], t['mint']): t for t in meta.get('postTokenBalances', [])}
    lp_delta = 0.0
    user_underlying_delta = 0.0
    user_wallet = None
    for k in set(pre)|set(post):
        idx, mint = k
        pb = pre.get(k); pob = post.get(k)
        owner = (pob or pb).get('owner')
        pre_ui = (pb or {}).get('uiTokenAmount',{}).get('uiAmount') or 0
        post_ui = (pob or {}).get('uiTokenAmount',{}).get('uiAmount') or 0
        d = (post_ui or 0) - (pre_ui or 0)
        if d == 0: continue
        if mint == cfg['lp_mint'] and owner == cfg['market']:
            lp_delta = d
        if mint == cfg['underlying']:
            # User's ATA — owner is a real wallet, not the SY-auth
            # SY-auth owns underlying vault (Δ +X when user deposits)
            # User's ATA has Δ -X (eUSX leaves user)
            # We want the user's wallet.
            # Heuristic: the wallet whose underlying balance MIRRORS the LP delta direction
            # (deposit: user_underlying_Δ < 0 AND lp_delta > 0)
            if owner and owner != cfg['market']:
                # Check if this owner looks like a user (not protocol address)
                # Skip the SY-auth (which owns the pool vault)
                # We'll detect SY-auth by it having a HUGE balance
                if abs(post_ui) < 1_000_000:   # ordinary user ATA
                    if user_underlying_delta == 0 or abs(d) > abs(user_underlying_delta):
                        user_underlying_delta = d
                        user_wallet = owner
    if lp_delta == 0 or user_wallet is None:
        return None
    return {
        'user': user_wallet,
        'lp_delta': lp_delta,
        'underlying_delta': user_underlying_delta,   # negative on deposit, positive on withdraw
        'per_lp_underlying': abs(user_underlying_delta) / abs(lp_delta) if lp_delta else None,
    }


def main():
    eusx_peg = get_eusx_peg()
    MARKETS['eUSX-Jun26']['peg'] = eusx_peg
    print(f'eUSX live peg: ${eusx_peg:.6f}', flush=True)

    now_ts = int(time.time())
    print(f'S2 window: {datetime.fromtimestamp(S2_START_TS, UTC).strftime("%Y-%m-%d")} → now ({(now_ts-S2_START_TS)/86400:.1f} days)\n', flush=True)

    all_results = defaultdict(lambda: defaultdict(float))   # wallet → {quest: flares}
    all_events_by_wallet = defaultdict(list)                  # wallet → all events (both markets)
    all_snapshots = defaultdict(lambda: defaultdict(float))   # wallet → quest → final LP balance × rate

    for mname, cfg in MARKETS.items():
        print(f'=== {mname} (mult {cfg["mult"]}, peg ${cfg["peg"]:.4f}) ===', flush=True)
        sigs = fetch_all_sigs(cfg['lp_vault'], S2_START_TS)
        print(f'  {len(sigs):,} LP-vault sigs in S2 window', flush=True)

        # Fetch txs in parallel
        def fetch(s):
            try:
                r = rpc('getTransaction', [s['signature'], {'encoding':'jsonParsed','maxSupportedTransactionVersion':0}])
                return s, r.get('result')
            except: return s, None

        # per-wallet event timeline
        events_by_wallet = defaultdict(list)
        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = [ex.submit(fetch, s) for s in sigs]
            done = 0
            for fut in as_completed(futs):
                s, tx = fut.result()
                done += 1
                if done % 200 == 0: print(f'    {done}/{len(sigs)}', flush=True)
                if not tx: continue
                t = tx.get('blockTime') or 0
                if t < S2_START_TS: continue
                parsed = parse_tx_lp_event(tx, cfg)
                if not parsed: continue
                events_by_wallet[parsed['user']].append({
                    't': t, 'lp_delta': parsed['lp_delta'],
                    'rate': parsed['per_lp_underlying'],
                    'sig': s['signature'],
                    'underlying_delta': parsed['underlying_delta'],
                    'market': cfg['market'],
                    'market_label': mname,
                    'underlying_mint': cfg['underlying'],
                })
        print(f'  unique LP-active wallets in S2: {len(events_by_wallet):,}', flush=True)

        # For each wallet, integrate LP × per_LP_USD × peg(t) × mult.
        # For USX market peg is a constant 1.0. For eUSX market we evaluate
        # peg_at(midpoint) per segment so the historical appreciation curve is
        # respected (this lifts LP eUSX from Tier 2 to Tier 1).
        is_eusx_market = (mname == 'eUSX-Jun26')
        def _peg(t0, t1):
            if not is_eusx_market: return 1.0
            try: return eusx_peg_at((t0 + t1) // 2)
            except Exception: return cfg['peg'] or 1.0
        for wallet, evs in events_by_wallet.items():
            evs.sort(key=lambda x: x['t'])
            # TODO: subtract pre-S2 LP balance if any.
            lp_balance = 0.0
            usd_days = 0.0
            last_rate = None
            for i in range(len(evs)):
                e = evs[i]
                lp_balance += e['lp_delta']
                if e['rate']: last_rate = e['rate']
                if i + 1 < len(evs):
                    t0, t1 = e['t'], evs[i+1]['t']
                    dt = (t1 - t0) / 86400
                    if dt > 0 and lp_balance > 0 and last_rate:
                        usd_days += lp_balance * last_rate * _peg(t0, t1) * dt
            # Tail: from last event to now
            if lp_balance > 0 and last_rate:
                t0 = evs[-1]['t']
                dt = (now_ts - t0) / 86400
                if dt > 0:
                    usd_days += lp_balance * last_rate * _peg(t0, now_ts) * dt
            flares = usd_days * cfg['mult']
            if flares > 0:
                all_results[wallet][cfg['quest']] = flares
            # Aggregate per-wallet events + final LP value across both markets
            all_events_by_wallet[wallet].extend(evs)
            if lp_balance > 0 and last_rate:
                # Snapshot uses live peg (current value) for the "value now" display.
                all_snapshots[wallet][cfg['quest']] = lp_balance * last_rate * (cfg['peg'] or 1.0)

        # Quick stats for this market
        market_total = sum(r.get(cfg['quest'], 0) for r in all_results.values())
        top = sorted([(w, r.get(cfg['quest'], 0)) for w, r in all_results.items()], key=lambda x: -x[1])[:5]
        print(f'  total {cfg["quest"]} flares: {market_total:,.2f}')
        print(f'  top 5:')
        for w, f in top:
            if f > 0: print(f'    {w}  {f:,.2f}')
        print(flush=True)

    # Save
    out = {w: dict(r) for w, r in all_results.items()}
    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 's2_lp_flares.json')
    with open(out_path, 'w') as f: json.dump(out, f, indent=2)
    print(f'\nSaved {len(out)} wallets to {out_path}')

    # DB: walker_outputs + sync to wallet_quests
    WALKER_QUESTS_DB = ['S2_EXPONENT_LP_USX_JUN26', 'S2_EXPONENT_LP_EUSX_JUN26']
    walker_db.prune('walk_s2_lp')
    rows_db = []
    for w_, pq_ in out.items():
        for q_, v_ in pq_.items():
            if v_ > 0: rows_db.append((w_, q_, v_))
    walker_db.upsert_many('walk_s2_lp', rows_db)
    walker_db.sync_to_wallet_quests('walk_s2_lp', WALKER_QUESTS_DB)
    print(f'DB: walker_outputs={len(rows_db)} rows; synced to wallet_quests')

    # Per-wallet snapshot + event timeline → quest_cache (S2_EXPONENT_LP)
    # Format mirrors Orca/Raydium/Kamino/Loopscale so the drawer renders the
    # per-position cards consistently.
    import db
    db.init()
    snap_count = 0
    total_events = 0
    for wallet, evs in all_events_by_wallet.items():
        if not evs and not all_snapshots.get(wallet): continue
        # Transform LP-walker events into the drawer's event format
        drawer_events = []
        for e in evs:
            lp_d = e.get('lp_delta', 0)
            ix = 'increaseliquidity' if lp_d > 0 else 'decreaseliquidity'
            underlying_amt = abs(e.get('underlying_delta') or 0)
            drawer_events.append({
                'ts': e.get('t'),
                'ix': ix,
                'pos_pubkey': e.get('market'),                    # group by market
                'sig': e.get('sig'),
                'market_label': e.get('market_label'),
                'side': 'lp',
                'deltas': [{'mint': e.get('underlying_mint'), 'amt': underlying_amt}] if underlying_amt > 0 else [],
                'lp_delta': lp_d,                                 # preserved so we can recompute from cache
                'rate': e.get('rate'),                            # per_lp_underlying at this event
            })
        drawer_events.sort(key=lambda e: e.get('ts') or 0)
        snap_positions = {
            'usx_jun26_lp_usd': round(all_snapshots[wallet].get('S2_EXPONENT_LP_USX_JUN26', 0), 2),
            'eusx_jun26_lp_usd': round(all_snapshots[wallet].get('S2_EXPONENT_LP_EUSX_JUN26', 0), 2),
        }
        snap = {
            'positions': snap_positions,
            'events': drawer_events,
            '_watermark': {'slot': 0, 'ts': now_ts},
        }
        db.put_cache(wallet, 'S2_EXPONENT_LP', snap, watermark_ts=now_ts)
        snap_count += 1
        total_events += len(drawer_events)
    print(f'Per-wallet snapshots written: {snap_count}  ({total_events} total events)')


if __name__ == '__main__':
    main()

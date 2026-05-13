"""Walk S2 Kamino lending users — SELF-ENUMERATING.

Discovers the wallet set on-chain via getProgramAccounts on the Kamino Lending
program filtered by lendingMarket = Solstice market. No dependency on
quest_results.jsonl or any prior file.

For each obligation owner:
  1. Fetch current obligation state via Kamino REST (deposits + borrows USD)
  2. Walk obligation account sig history during S2 → first S2 tx ts
  3. days_held = (now - first_S2_ts) / 86400, clamped ≥ 1.0 (Solstice min-rule)
  4. flares = position_USD × mult × days_held per quest

Writes:
  - DB.walker_outputs (walker='walk_s2_kamino') + DB.wallet_quests via sync
  - data/s2_kamino_flares.json (legacy compat for older readers)
"""
import os, sys, json, time, base64, base58, struct
from datetime import datetime, UTC
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc
import walker_db
import db as _db
from incremental_events import extract_events_incremental

S2_START_TS = 1776038400
MIN_HOLD_DAYS = 1.0

SOLSTICE_MARKET = '9Y7uwXgQ68mGqRtZfuFaP4hc4fxeJ7cE9zTtqTxVhfGU'
USX_MINT  = '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG'
EUSX_MINT = '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC'
USDG_MINT = '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH'
SF_DENOM = 2**60

# Quest config: (api_field, quest_code, multiplier)
LEND_QUESTS = {
    USX_MINT:  ('S2_KAMINO_LEND_USX',  5),
    EUSX_MINT: ('S2_KAMINO_LEND_EUSX', 1),
    USDG_MINT: ('S2_KAMINO_LEND_USDG', 5),
}
BORROW_QUESTS = {
    USX_MINT:  ('S2_KAMINO_BORROW_USX',  1),
    USDG_MINT: ('S2_KAMINO_BORROW_USDG', 1),
}


def _kget(path: str):
    return requests.get(f'https://api.kamino.finance{path}', timeout=20).json()


def _reserve_to_mint():
    r = _kget(f'/kamino-market/{SOLSTICE_MARKET}/reserves/metrics')
    return {x['reserve']: x['liquidityTokenMint'] for x in r if x.get('reserve')}


def get_obligations(wallet: str) -> list:
    """Returns list of {address, deposits[{reserve, marketValueUSD}], borrows[...]}"""
    r = _kget(f'/kamino-market/{SOLSTICE_MARKET}/users/{wallet}/obligations')
    return r or []


def main():
    now_ts = int(time.time())
    print(f'S2 window: {datetime.fromtimestamp(S2_START_TS, UTC).strftime("%Y-%m-%d")} → now ({(now_ts-S2_START_TS)/86400:.1f} days)\n', flush=True)

    # Self-enumerate Kamino obligation owners on Solstice market.
    # Kamino Lend program: `KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD`
    # Obligation layout: lendingMarket at offset 32, owner at offset 64.
    KLEND = 'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD'
    print(f'Enumerating Kamino obligations on Solstice market via getProgramAccounts...', flush=True)
    r = rpc('getProgramAccounts', [KLEND, {
        'encoding': 'base64',
        'dataSlice': {'offset': 64, 'length': 32},   # owner pubkey only
        'filters': [{'memcmp': {'offset': 32, 'bytes': SOLSTICE_MARKET}}]
    }], timeout=60)
    owners = set()
    for a in (r.get('result') or []):
        try:
            d = base64.b64decode(a['account']['data'][0])
            owners.add(base58.b58encode(d[:32]).decode())
        except Exception: continue
    wallets = sorted(owners)
    print(f'{len(wallets):,} unique Kamino obligation owners on Solstice market\n', flush=True)

    reserve2mint = _reserve_to_mint()
    print(f'Loaded {len(reserve2mint)} reserves\n', flush=True)

    # For each wallet, get obligations + walk each obligation account sig history
    results = defaultdict(lambda: defaultdict(float))
    snapshots = defaultdict(lambda: defaultdict(float))   # wallet → pos key → USD
    events_by_wallet = defaultdict(list)                  # wallet → list of events

    # Preload existing per-(wallet, obligation) events for incremental walks
    _db.init()
    existing_by_user_pos = defaultdict(list)
    for r in _db.conn().execute("SELECT wallet, raw_json FROM quest_cache WHERE quest_key='S2_KAMINO'"):
        try:
            for e in (json.loads(r['raw_json']).get('events') or []):
                pp = e.get('pos_pubkey')
                if pp: existing_by_user_pos[(r['wallet'], pp)].append(e)
        except Exception: pass
    print(f'Preloaded existing events for {len(existing_by_user_pos)} (wallet, obligation) pairs', flush=True)
    KAMINO_IXS = {
        'depositreserveliquidityandobligationcollateral': 'DEPOSIT',
        'depositreserveliquidity': 'DEPOSIT',
        'depositobligationcollateral': 'DEPOSIT COLLATERAL',
        'withdrawreserveliquidity': 'WITHDRAW',
        'withdrawobligationcollateralandredeemreservecollateral': 'WITHDRAW',
        'withdrawobligationcollateral': 'WITHDRAW COLLATERAL',
        'borrowobligationliquidity': 'BORROW',
        'repayobligationliquidity': 'REPAY',
        'liquidateobligationandredeemreservecollateral': 'LIQUIDATED',
    }
    SIDE_MINT_TO_POSKEY = {
        ('lend', USX_MINT):  'kamino_supply_usx',
        ('lend', EUSX_MINT): 'kamino_supply_eusx',
        ('lend', USDG_MINT): 'kamino_supply_usdg',
        ('borrow', USX_MINT):  'kamino_borrow_usx',
        ('borrow', USDG_MINT): 'kamino_borrow_usdg',
    }
    RELEVANT_MINTS = {USX_MINT, EUSX_MINT, USDG_MINT, 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'}

    def process_wallet(wallet: str):
        try:
            obls = get_obligations(wallet)
        except Exception: return wallet, None, None, None, 'api-fail'
        per_quest = defaultdict(float)
        wallet_snap = defaultdict(float)
        wallet_events = []
        for obl in obls:
            obl_addr = obl.get('obligationAddress')
            state = obl.get('state', {})
            if not obl_addr: continue
            deps = {}
            for d in state.get('deposits', []):
                mint = reserve2mint.get(d.get('depositReserve'))
                if mint is None: continue
                usd = int(d.get('marketValueSf','0')) / SF_DENOM
                if usd > 0: deps[('lend', mint)] = deps.get(('lend',mint),0) + usd
            for b in state.get('borrows', []):
                mint = reserve2mint.get(b.get('borrowReserve'))
                if mint is None: continue
                usd = int(b.get('marketValueSf','0')) / SF_DENOM
                if usd > 0: deps[('borrow', mint)] = deps.get(('borrow',mint),0) + usd
            # Save current-position snapshot regardless of S2 history
            for (side, mint), usd in deps.items():
                k = SIDE_MINT_TO_POSKEY.get((side, mint))
                if k: wallet_snap[k] += usd
            if not deps: continue

            # Incremental walk: only fetch sigs newer than the last cached one
            existing = existing_by_user_pos.get((wallet, obl_addr), [])

            def _classify(tx, s):
                ix_name = None
                for ln in (tx['meta'].get('logMessages') or []):
                    if 'Program log: Instruction:' in ln:
                        nm = ln.split('Instruction:',1)[1].strip().split()[0].lower()
                        if nm in KAMINO_IXS:
                            ix_name = nm; break
                if not ix_name: return None
                pre = tx['meta'].get('preTokenBalances') or []
                post = tx['meta'].get('postTokenBalances') or []
                pre_by_idx = {b['accountIndex']: b for b in pre}
                deltas = {}
                for b in post:
                    if b.get('mint') not in RELEVANT_MINTS: continue
                    p = pre_by_idx.get(b['accountIndex'], {})
                    bef = float(((p.get('uiTokenAmount') or {}).get('uiAmount')) or 0)
                    aft = float(((b.get('uiTokenAmount') or {}).get('uiAmount')) or 0)
                    d = aft - bef
                    if d > 0: deltas[b['mint']] = deltas.get(b['mint'], 0) + d
                return {'ix': ix_name, 'deltas': [{'mint': m, 'amt': a} for m, a in deltas.items()]}

            new_evs = extract_events_incremental(obl_addr, existing, _classify)
            wallet_events.extend(existing)
            wallet_events.extend(new_evs)

            # Flare math: time-weight from earliest event ts (incl existing)
            all_event_ts = [e.get('ts') for e in (existing + new_evs) if e.get('ts')]
            in_s2 = [t for t in all_event_ts if t >= S2_START_TS]
            if not in_s2:
                days = (now_ts - S2_START_TS) / 86400
            else:
                first_ts = max(min(in_s2), S2_START_TS)
                days = (now_ts - first_ts) / 86400
            if days < MIN_HOLD_DAYS:
                continue
            for (side, mint), usd in deps.items():
                if side == 'lend' and mint in LEND_QUESTS:
                    q, mult = LEND_QUESTS[mint]
                    per_quest[q] += usd * mult * days
                elif side == 'borrow' and mint in BORROW_QUESTS:
                    q, mult = BORROW_QUESTS[mint]
                    per_quest[q] += usd * mult * days
        return wallet, dict(per_quest), dict(wallet_snap), wallet_events, 'ok'

    print('Walking obligations…', flush=True)
    n_done = 0; n_ok = 0; n_api_fail = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(process_wallet, w) for w in wallets]
        for fut in as_completed(futs):
            wallet, pq, snap, evts, status = fut.result()
            n_done += 1
            if status == 'api-fail': n_api_fail += 1
            elif pq is not None:
                if pq:
                    n_ok += 1
                    for q, v in pq.items(): results[wallet][q] = v
                if snap:
                    for k, v in snap.items(): snapshots[wallet][k] += v
                if evts: events_by_wallet[wallet].extend(evts)
            if n_done % 50 == 0:
                print(f'  {n_done}/{len(wallets)}  ok={n_ok}  api-fail={n_api_fail}', flush=True)

    print(f'\nDone. {n_ok:,} wallets with computed S2 Kamino flares, {n_api_fail} API failures', flush=True)

    # Totals
    totals = defaultdict(float)
    for wallet, pq in results.items():
        for q, v in pq.items(): totals[q] += v
    print('\nPer-quest S2 totals (history-walked):')
    for q, v in sorted(totals.items()):
        print(f'  {q:<32s} {v:>20,.0f}')
    print(f'  {"TOTAL":<32s} {sum(totals.values()):>20,.0f}')

    out = {w: dict(pq) for w, pq in results.items() if pq}
    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 's2_kamino_flares.json')
    with open(out_path, 'w') as f: json.dump(out, f, indent=2)
    print(f'\nSaved {len(out)} wallets to {out_path}')

    # Write to DB: walker_outputs + sync to wallet_quests
    WALKER_QUESTS = ['S2_KAMINO_LEND_USX','S2_KAMINO_LEND_EUSX','S2_KAMINO_LEND_USDG',
                     'S2_KAMINO_BORROW_USX','S2_KAMINO_BORROW_USDG']
    walker_db.prune('walk_s2_kamino')
    rows = []
    for w, pq in out.items():
        for q, v in pq.items():
            if v > 0: rows.append((w, q, v))
    walker_db.upsert_many('walk_s2_kamino', rows)
    walker_db.sync_to_wallet_quests('walk_s2_kamino', WALKER_QUESTS)
    print(f'DB: walker_outputs={len(rows)} rows; synced to wallet_quests')

    # Per-wallet snapshot + event timeline → quest_cache (S2_KAMINO)
    import db
    db.init()
    snap_count = 0
    all_owners = set(snapshots.keys()) | set(events_by_wallet.keys())
    for owner in all_owners:
        snap_map = snapshots.get(owner, {})
        evts = events_by_wallet.get(owner, [])
        if all(v <= 0 for v in snap_map.values()) and not evts: continue
        evts.sort(key=lambda e: e.get('ts') or 0)
        snap = {
            'positions': {
                'kamino_supply_usx':   round(snap_map.get('kamino_supply_usx', 0), 2),
                'kamino_supply_eusx':  round(snap_map.get('kamino_supply_eusx', 0), 2),
                'kamino_supply_usdg':  round(snap_map.get('kamino_supply_usdg', 0), 2),
                'kamino_borrow_usx':   round(snap_map.get('kamino_borrow_usx', 0), 2),
                'kamino_borrow_usdg':  round(snap_map.get('kamino_borrow_usdg', 0), 2),
                'kamino_kvault_usx_usdg': 0.0,  # filled by walk_s2_kamino_strategy
            },
            'events': evts,
            '_watermark': {'slot': 0, 'ts': now_ts},
        }
        db.put_cache(owner, 'S2_KAMINO', snap, watermark_ts=now_ts)
        snap_count += 1
    print(f'Per-wallet snapshots written: {snap_count}  ({sum(len(v) for v in events_by_wallet.values())} total events)')


if __name__ == '__main__':
    main()

"""Walk S2 Raydium CLMM LP holders across 2 Solstice S2 pools.

Same approach as walk_s2_orca.py.
"""
import os, sys, json, time, base64, base58, struct, math, requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc
import walker_db
import db as _db
from incremental_events import extract_events_incremental

S2_START_TS = 1776038400
MIN_HOLD_DAYS = 1.0
RAYDIUM_CLMM = 'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK'
POSITION_DISC = '466f967ee60f1975'

POOLS = {
    'S2_RAYDIUM_USX_USDC': {'addr': 'EWivkwNtcxuPsU6RyD7Pfvs7u9Yv8nQ79tJ7xgGyPrp6', 'mult': 9,
                             'price_a': 1.0, 'price_b': 1.0, 'dec_a': 6, 'dec_b': 6},
    'S2_RAYDIUM_EUSX_USX': {'addr': 'BkvKpstxgeEJYzvFnWWuAbTDcrFMJBty3kXxUfGG9D7n', 'mult': 4,
                             'price_a': 1.156, 'price_b': 1.0, 'dec_a': 6, 'dec_b': 6},
}


def get_pool_tick(pool_id: str) -> int:
    """Get current tick from Raydium API."""
    try:
        r = requests.get(f'https://api-v3.raydium.io/pools/info/ids?ids={pool_id}', timeout=15).json()
        data = r.get('data') or []
        if data and data[0]:
            return int(data[0].get('tickCurrent') or 0)
    except Exception: pass
    return 0


def find_nft_owner(mint: str) -> str:
    import time as _t
    for attempt in range(3):
        try:
            r = rpc('getTokenLargestAccounts', [mint], timeout=12)
            top = r.get('result', {}).get('value', []) if isinstance(r, dict) else []
            for h in top:
                if float(h.get('uiAmount') or 0) >= 1:
                    addr = h['address']
                    r2 = rpc('getAccountInfo', [addr, {'encoding':'jsonParsed'}], timeout=12)
                    v = r2.get('result',{}).get('value') if isinstance(r2, dict) else None
                    if v:
                        info = (v.get('data',{}).get('parsed',{}) or {}).get('info',{}) or {}
                        owner = info.get('owner')
                        if owner: return owner
        except Exception: pass
        _t.sleep(0.5 * (2 ** attempt))
    return None


def liquidity_to_usd(L, tick_lower, tick_upper, current_tick, price_a, price_b, dec_a, dec_b):
    if L == 0: return 0.0
    def t2p(t): return math.pow(1.0001, t/2)
    sl, su, sp = t2p(tick_lower), t2p(tick_upper), t2p(current_tick)
    if current_tick < tick_lower:
        a, b = L*(su-sl)/(sl*su), 0
    elif current_tick >= tick_upper:
        a, b = 0, L*(su-sl)
    else:
        a, b = L*(su-sp)/(sp*su), L*(sp-sl)
    return (a/10**dec_a)*price_a + (b/10**dec_b)*price_b


def main():
    now_ts = int(time.time())
    print(f'S2 window: {(now_ts-S2_START_TS)/86400:.1f} days\n', flush=True)
    all_results = defaultdict(lambda: defaultdict(float))
    all_positions = defaultdict(lambda: defaultdict(float))
    position_events_by_owner = defaultdict(list)

    # Preload cached per-(owner, position) events for incremental walking.
    _db.init()
    existing_by_owner_pos = defaultdict(list)
    for r in _db.conn().execute("SELECT wallet, raw_json FROM quest_cache WHERE quest_key='S2_RAYDIUM'"):
        try:
            for e in (json.loads(r['raw_json']).get('events') or []):
                pp = e.get('pos_pubkey')
                if pp: existing_by_owner_pos[(r['wallet'], pp)].append(e)
        except Exception: pass
    print(f'Preloaded existing events for {len(existing_by_owner_pos)} (wallet, position) pairs', flush=True)
    QUEST_TO_POSKEY = {
        'S2_RAYDIUM_USX_USDC': 'raydium_usx_usdc',
        'S2_RAYDIUM_EUSX_USX': 'raydium_eusx_usx',
    }
    WHIRL_IXS = {
        'increaseliquidity','increaseliquidityv2',
        'decreaseliquidity','decreaseliquidityv2',
        'openposition','openpositionv2','openpositionwithtokenextensions',
        'openpositionwithtoken22nft',
        'closeposition','closepositionwithtokenextensions','closepositionwithtoken22nft',
        'collectfees','collectfeesv2','collectreward','collectrewardv2',
        'swap','swapv2','swapbaseinput','swapbaseoutput',
    }
    INCREASE_IXS = {'increaseliquidity','increaseliquidityv2',
                    'openposition','openpositionv2',
                    'openpositionwithtokenextensions','openpositionwithtoken22nft'}
    DECREASE_IXS = {'decreaseliquidity','decreaseliquidityv2',
                    'closeposition','closepositionwithtokenextensions','closepositionwithtoken22nft'}
    MINT_USD = {
        '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG': 1.0,   # USX
        '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC': 1.156, # eUSX
        '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH': 1.0,   # USDG
        'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v': 1.0,   # USDC
    }
    RELEVANT_MINTS = set(MINT_USD.keys())

    for quest, cfg in POOLS.items():
        pool_addr = cfg['addr']; mult = cfg['mult']
        print(f'=== {quest} ({pool_addr[:8]}…) mult {mult}× ===', flush=True)
        current_tick = get_pool_tick(pool_addr)
        print(f'  current_tick={current_tick}', flush=True)

        # Find Raydium positions by pool_id
        pool_bytes = base58.b58encode(base58.b58decode(pool_addr)).decode()
        # In Raydium PersonalPositionState, pool_id is at offset 41
        r = rpc('getProgramAccounts', [RAYDIUM_CLMM, {
            'encoding': 'base64',
            'filters': [
                {'dataSize': 281},
                {'memcmp': {'offset': 41, 'bytes': pool_bytes}}
            ]
        }], timeout=120)
        accs = r.get('result', []) or []
        print(f'  {len(accs)} positions', flush=True)

        positions = []
        for a in accs:
            d = base64.b64decode(a['account']['data'][0])
            if d[:8].hex() != POSITION_DISC: continue
            mint = base58.b58encode(d[9:41]).decode()
            tick_lower = int.from_bytes(d[73:77], 'little', signed=True)
            tick_upper = int.from_bytes(d[77:81], 'little', signed=True)
            L = int.from_bytes(d[81:97], 'little')
            if L == 0: continue
            positions.append({'pubkey': a['pubkey'], 'mint': mint, 'L': L, 'tl': tick_lower, 'tu': tick_upper})

        def process(p):
            owner = find_nft_owner(p['mint'])
            if not owner: return None
            usd = liquidity_to_usd(p['L'], p['tl'], p['tu'], current_tick,
                                    cfg['price_a'], cfg['price_b'], cfg['dec_a'], cfg['dec_b'])
            return (owner, p['pubkey'], p['mint'], usd) if usd > 0 else None

        position_owners = []
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = [ex.submit(process, p) for p in positions]
            done = 0
            for fut in as_completed(futs):
                done += 1
                if done % 50 == 0: print(f'    {done}/{len(positions)} owners', flush=True)
                res = fut.result()
                if res:
                    position_owners.append(res)
                    owner, pos_pubkey, mint, usd = res
                    all_positions[owner][QUEST_TO_POSKEY[quest]] += usd
        print(f'  {len(position_owners)} positions with USD value', flush=True)

        def _classify(tx, s):
            ix_name = None
            for ln in (tx['meta'].get('logMessages') or []):
                if 'Program log: Instruction:' in ln:
                    nm = ln.split('Instruction:',1)[1].strip().split()[0].lower()
                    if nm in WHIRL_IXS:
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

        def _event_usd_change(ev):
            ix = (ev.get('ix') or '').lower()
            if ix in INCREASE_IXS: sign = +1
            elif ix in DECREASE_IXS: sign = -1
            else: return 0.0
            s = 0.0
            for d in (ev.get('deltas') or []):
                amt = float(d.get('amt') or 0)
                s += MINT_USD.get(d.get('mint'), 0.0) * amt
            return sign * s

        def _integrate_position(events, current_usd):
            evs = sorted([e for e in events if e.get('ts')], key=lambda e: e['ts'])
            if not evs:
                days = (now_ts - S2_START_TS) / 86400
                return current_usd * days if days >= MIN_HOLD_DAYS else 0.0
            usd_running = 0.0
            timeline = []
            for e in evs:
                usd_running = max(0.0, usd_running + _event_usd_change(e))
                timeline.append((e['ts'], usd_running))
            last_usd = timeline[-1][1] if timeline else 0.0
            scale = (current_usd / last_usd) if (last_usd > 0 and current_usd > 0) else 1.0
            carry_in = 0.0
            for t, u in timeline:
                if t < S2_START_TS: carry_in = u
                else: break
            usd_days = 0.0
            prev_t = S2_START_TS
            prev_u = carry_in * scale
            for t, u in timeline:
                if t < S2_START_TS: continue
                dt = (t - prev_t) / 86400
                if dt > 0 and prev_u > 0:
                    usd_days += prev_u * dt
                prev_t = t
                prev_u = u * scale
            if prev_u > 0 and prev_t < now_ts:
                dt = (now_ts - prev_t) / 86400
                if dt > 0:
                    usd_days += prev_u * dt
            return usd_days

        def walk(args):
            owner, pos_pubkey, mint, usd = args
            existing = existing_by_owner_pos.get((owner, pos_pubkey), [])
            new_evs = extract_events_incremental(pos_pubkey, existing, _classify)
            for e in new_evs: e.setdefault('mint_position', mint)
            position_events_by_owner[owner].extend(existing)
            position_events_by_owner[owner].extend(new_evs)
            all_evs = existing + new_evs
            usd_days = _integrate_position(all_evs, usd)
            if usd_days <= 0: return None
            return owner, usd_days * mult

        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = [ex.submit(walk, po) for po in position_owners]
            done = 0
            for fut in as_completed(futs):
                done += 1
                if done % 50 == 0: print(f'    walked {done}/{len(position_owners)}', flush=True)
                res = fut.result()
                if res:
                    owner, flares = res
                    all_results[owner][quest] += flares

        q_total = sum(r.get(quest, 0) for r in all_results.values())
        print(f'  {quest} total: {q_total:,.0f}\n', flush=True)

    out = {w: dict(r) for w, r in all_results.items() if any(v > 0 for v in r.values())}
    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 's2_raydium_flares.json')
    with open(out_path, 'w') as f: json.dump(out, f, indent=2)
    totals = defaultdict(float)
    for pq in out.values():
        for q, v in pq.items(): totals[q] += v
    print(f'\nSaved {len(out)} wallets to {out_path}')

    # DB: walker_outputs + sync to wallet_quests
    WALKER_QUESTS_DB = ['S2_RAYDIUM_USX_USDC', 'S2_RAYDIUM_EUSX_USX']
    walker_db.prune('walk_s2_raydium')
    rows_db = []
    for w_, pq_ in out.items():
        for q_, v_ in pq_.items():
            if v_ > 0: rows_db.append((w_, q_, v_))
    walker_db.upsert_many('walk_s2_raydium', rows_db)
    walker_db.sync_to_wallet_quests('walk_s2_raydium', WALKER_QUESTS_DB)
    print(f'DB: walker_outputs={len(rows_db)} rows; synced to wallet_quests')

    # Per-wallet snapshot + event timeline → quest_cache (S2_RAYDIUM)
    import db
    db.init()
    snap_count = 0
    all_owners = set(all_positions.keys()) | set(position_events_by_owner.keys())
    for owner in all_owners:
        pos_map = all_positions.get(owner, {})
        events  = position_events_by_owner.get(owner, [])
        if all(v <= 0 for v in pos_map.values()) and not events: continue
        events.sort(key=lambda e: e.get('ts') or 0)
        snap = {
            'positions': {
                'raydium_usx_usdc': round(pos_map.get('raydium_usx_usdc', 0), 2),
                'raydium_eusx_usx': round(pos_map.get('raydium_eusx_usx', 0), 2),
            },
            'events': events,
            '_watermark': {'slot': 0, 'ts': now_ts},
        }
        db.put_cache(owner, 'S2_RAYDIUM', snap, watermark_ts=now_ts)
        snap_count += 1
    print(f'Per-wallet snapshots written: {snap_count}  ({sum(len(v) for v in position_events_by_owner.values())} total events)')

    for q, v in sorted(totals.items()): print(f'  {q:<32s} {v:>16,.0f}')


if __name__ == '__main__':
    main()

"""Walk S2 Orca whirlpool LP holders across the 3 Solstice S2 pools.

For each pool:
  1. Enumerate all Position accounts on the whirlpool program filtered by whirlpool
  2. For each position: decode liquidity, tick range, current owner
  3. For each position owner: compute current USD value via CLMM math
  4. Walk owner's position-NFT sig history during S2 to time-weight
  5. Integrate USD × mult × dt

Three S2 pools:
  USX/USDC  9× — 2e3WeM4WwdEqwTtRnWN3gJSbhNg1P6Aj2y7kEdfrYbix
  eUSX/USX  4× — AUr5EVRwGDsKB2EeS1V63ncjHXDNRDLVfBP47qNvPoVf
  USDG/USX  9× — J6h5bf3iohBXtsRNRFAqFc5FeBCh3yAjxXGuiE1sTc5Q

Output: data/s2_orca_flares.json
"""
import os, sys, json, time, base64, base58, struct, math, requests
from datetime import datetime, UTC
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc
import walker_db
import db as _db
from incremental_events import extract_events_incremental

S2_START_TS = 1776038400
MIN_HOLD_DAYS = 1.0
WHIRL_PROG = 'whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc'
POSITION_DISC = 'aabc8fe47a40f7d0'   # Whirlpool Position account discriminator

POOLS = {
    'S2_ORCA_USX_USDC': {'addr': '2e3WeM4WwdEqwTtRnWN3gJSbhNg1P6Aj2y7kEdfrYbix', 'mult': 9,
                          'price_a': 1.0, 'price_b': 1.0, 'dec_a': 6, 'dec_b': 6},   # USX, USDC
    'S2_ORCA_EUSX_USX': {'addr': 'AUr5EVRwGDsKB2EeS1V63ncjHXDNRDLVfBP47qNvPoVf', 'mult': 4,
                          'price_a': 1.156, 'price_b': 1.0, 'dec_a': 6, 'dec_b': 6}, # eUSX, USX
    'S2_ORCA_USX_USDG': {'addr': 'J6h5bf3iohBXtsRNRFAqFc5FeBCh3yAjxXGuiE1sTc5Q', 'mult': 9,
                          'price_a': 1.0, 'price_b': 1.0, 'dec_a': 6, 'dec_b': 6},   # USDG, USX
}


def get_pool_data(pool_addr: str) -> dict:
    """Get pool's current state from Orca API."""
    r = requests.get(f'https://api.orca.so/v2/solana/pools/{pool_addr}', timeout=15).json()
    return r.get('data', {}) or {}


def find_nft_owner(mint: str) -> str:
    """Find current owner of an NFT mint. Retries up to 3 times on transient
    RPC failure (rate limits / timeouts) so the walker degrades gracefully
    under load rather than silently dropping positions."""
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
            # Empty result — retry
        except Exception: pass
        _t.sleep(0.5 * (2 ** attempt))
    return None


def liquidity_to_usd(L: int, tick_lower: int, tick_upper: int, current_tick: int,
                     price_a_usd: float, price_b_usd: float, dec_a: int, dec_b: int) -> float:
    """Compute USD value of a CLMM position with virtual liquidity L."""
    if L == 0: return 0.0
    def tick_to_sqrt_price(t):
        return math.pow(1.0001, t / 2)
    sqrt_p_lower = tick_to_sqrt_price(tick_lower)
    sqrt_p_upper = tick_to_sqrt_price(tick_upper)
    sqrt_p = tick_to_sqrt_price(current_tick)
    if current_tick < tick_lower:
        amt_a = L * (sqrt_p_upper - sqrt_p_lower) / (sqrt_p_lower * sqrt_p_upper)
        amt_b = 0
    elif current_tick >= tick_upper:
        amt_a = 0
        amt_b = L * (sqrt_p_upper - sqrt_p_lower)
    else:
        amt_a = L * (sqrt_p_upper - sqrt_p) / (sqrt_p * sqrt_p_upper)
        amt_b = L * (sqrt_p - sqrt_p_lower)
    a_ui = amt_a / 10**dec_a
    b_ui = amt_b / 10**dec_b
    return a_ui * price_a_usd + b_ui * price_b_usd


def main():
    now_ts = int(time.time())
    print(f'S2 window: {(now_ts-S2_START_TS)/86400:.1f} days\n', flush=True)

    all_results = defaultdict(lambda: defaultdict(float))
    all_positions = defaultdict(lambda: defaultdict(float))  # owner → {pool_key: current_usd}
    position_events_by_owner = defaultdict(list)  # owner → list of event dicts (across all pools)
    skipped_quests = set()  # quests whose pool returned empty positions but has TVL — don't sync (would zero existing data)

    # Preload cached per-(owner, position) events so incremental walk knows
    # what's already processed (only fetches signatures newer than the last
    # cached one for each position).
    _db.init()
    existing_by_owner_pos = defaultdict(list)
    cached_owner_by_pos = {}  # pos_pubkey -> wallet; fallback when find_nft_owner flakes
    for r in _db.conn().execute("SELECT wallet, raw_json FROM quest_cache WHERE quest_key='S2_ORCA'"):
        try:
            for e in (json.loads(r['raw_json']).get('events') or []):
                pp = e.get('pos_pubkey')
                if pp:
                    existing_by_owner_pos[(r['wallet'], pp)].append(e)
                    cached_owner_by_pos[pp] = r['wallet']
        except Exception: pass
    print(f'Preloaded existing events for {len(existing_by_owner_pos)} (wallet, position) pairs', flush=True)
    print(f'Cached owner→position map: {len(cached_owner_by_pos)} positions (fallback for find_nft_owner flake)', flush=True)

    QUEST_TO_POSKEY = {
        'S2_ORCA_USX_USDC': 'orca_usx_usdc',
        'S2_ORCA_EUSX_USX': 'orca_eusx_usx',
        'S2_ORCA_USX_USDG': 'orca_usx_usdg',
    }

    for quest, cfg in POOLS.items():
        pool_addr = cfg['addr']; mult = cfg['mult']
        print(f'=== {quest} ({pool_addr[:8]}…) mult {mult}× ===', flush=True)

        # Get pool state for current tick (prices hardcoded — all stablecoins)
        pool = get_pool_data(pool_addr)
        if not pool:
            print(f'  pool data unavailable\n'); continue
        current_tick = int(pool.get('tickCurrentIndex', 0))
        price_a = cfg['price_a']; price_b = cfg['price_b']
        dec_a = cfg['dec_a']; dec_b = cfg['dec_b']
        tvl = pool.get('tvlUsdc', '0')
        print(f'  pool TVL=${float(tvl):,.2f}  priceA=${price_a:.4f}  priceB=${price_b:.4f}  tick={current_tick}', flush=True)

        # Find all positions in this pool via getProgramAccounts. Always retry
        # on empty for our hardcoded S2 pools (they're known-active). RPC
        # occasionally returns [] for active pools under load; the dataset is
        # too small to safely auto-detect "actually empty" vs "RPC flake," so
        # we just retry and skip-sync if all retries fail.
        pool_bytes = base58.b58encode(base58.b58decode(pool_addr)).decode()
        accs = []
        import time as _t
        for attempt in range(4):
            r = rpc('getProgramAccounts', [WHIRL_PROG, {
                'encoding': 'base64',
                'filters': [
                    {'dataSize': 216},
                    {'memcmp': {'offset': 8, 'bytes': pool_bytes}}
                ]
            }], timeout=120, force_refresh=(attempt > 0))
            accs = r.get('result', []) or []
            if accs: break
            print(f'  retry {attempt+1}: 0 positions — retry in {2*(attempt+1)}s', flush=True)
            _t.sleep(2 * (attempt + 1))
        print(f'  {len(accs)} positions in pool', flush=True)
        if len(accs) == 0:
            print(f'  WARN: skipping {quest} sync — RPC empty after 4 retries', flush=True)
            skipped_quests.add(quest)
            continue

        # Decode positions
        positions = []
        for a in accs:
            d = base64.b64decode(a['account']['data'][0])
            if d[:8].hex() != POSITION_DISC: continue
            mint = base58.b58encode(d[40:72]).decode()
            L = int.from_bytes(d[72:88], 'little')
            tick_lower = int.from_bytes(d[88:92], 'little', signed=True)
            tick_upper = int.from_bytes(d[92:96], 'little', signed=True)
            if L == 0: continue
            positions.append({'pubkey': a['pubkey'], 'mint': mint, 'L': L,
                              'tick_lower': tick_lower, 'tick_upper': tick_upper})

        # For each position, find NFT owner and compute USD value. If
        # find_nft_owner returns None (RPC flake — getTokenLargestAccounts can
        # silently fail under load), fall back to the cached owner from
        # quest_cache. Position-NFT ownership is invariant once opened, so
        # the cached owner is authoritative for any position we've seen before.
        def process(p):
            owner = find_nft_owner(p['mint'])
            fallback = False
            if not owner:
                owner = cached_owner_by_pos.get(p['pubkey'])
                fallback = bool(owner)
            if not owner: return None
            usd = liquidity_to_usd(p['L'], p['tick_lower'], p['tick_upper'],
                                    current_tick, price_a, price_b, dec_a, dec_b)
            return (owner, p['pubkey'], p['mint'], usd, fallback)

        n_processed = 0
        n_fallback = 0
        position_owners = []
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = [ex.submit(process, p) for p in positions]
            for fut in as_completed(futs):
                n_processed += 1
                res = fut.result()
                if res:
                    owner, pos_pubkey, mint, usd, fallback = res
                    if fallback: n_fallback += 1
                    if usd > 0:
                        position_owners.append((owner, pos_pubkey, mint, usd))
                        all_positions[owner][QUEST_TO_POSKEY[quest]] += usd
                if n_processed % 50 == 0: print(f'    {n_processed}/{len(positions)} owners found', flush=True)
        print(f'  {len(position_owners)} positions with active USD value (fallback used: {n_fallback})', flush=True)

        ORCA_IXS = {
            'increaseliquidity','increaseliquidityv2',
            'decreaseliquidity','decreaseliquidityv2',
            'openposition','openpositionwithmetadata','openpositionwithtokenextensions',
            'closeposition','closepositionwithtokenextensions',
            'collectfees','collectfeesv2','collectreward','collectrewardv2',
            'swap','swapv2','twohopswap','twohopswapv2',
            'updatefeesandrewards','collectprotocolfees',
        }
        # Instructions that materially change a position's deposited liquidity
        # (in USD terms). Used by the timeline walker to compute usd(t) — all
        # other ix names (collectFees, swap, updateFees) leave L untouched.
        INCREASE_IXS = {'increaseliquidity','increaseliquidityv2',
                        'openposition','openpositionwithmetadata',
                        'openpositionwithtokenextensions'}
        DECREASE_IXS = {'decreaseliquidity','decreaseliquidityv2',
                        'closeposition','closepositionwithtokenextensions'}
        MINT_USD = {
            '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG': 1.0,  # USX
            '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC': 1.156, # eUSX
            '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH': 1.0,  # USDG
            'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v': 1.0,  # USDC
        }
        RELEVANT_MINTS = set(MINT_USD.keys())
        def _classify(tx, s):
            ix_name = None
            for ln in (tx['meta'].get('logMessages') or []):
                if 'Program log: Instruction:' in ln:
                    nm = ln.split('Instruction:',1)[1].strip().split()[0].lower()
                    if nm in ORCA_IXS:
                        ix_name = nm; break
            if not ix_name: return None
            pre  = tx['meta'].get('preTokenBalances') or []
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
            """USD value of liquidity added (+) or removed (-) at this event.
            Returns 0 for ix that don't affect L (collectFees, swap, etc.)."""
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
            """Walk events forward to build usd(t), then integrate over the S2
            window. Replaces the prior `current_usd × days × mult` shortcut,
            which over/under-counts whenever liquidity changes during S2."""
            evs = sorted([e for e in events if e.get('ts')], key=lambda e: e['ts'])
            if not evs:
                # No event history — fall back: assume constant current_usd over S2.
                # (Position predates S2 and has been held untouched, OR cache is empty.)
                days = (now_ts - S2_START_TS) / 86400
                return current_usd * days if days >= MIN_HOLD_DAYS else 0.0
            # Forward-accumulate usd(t) starting from 0.
            usd_running = 0.0
            timeline = []
            for e in evs:
                usd_running = max(0.0, usd_running + _event_usd_change(e))
                timeline.append((e['ts'], usd_running))
            # Sanity: if accumulator drifted from current_usd (missing events, off-tick
            # USD math), scale the post-S2 portion so the LAST point matches current_usd.
            # This keeps the integration anchored to what we can actually verify on-chain.
            last_usd = timeline[-1][1] if timeline else 0.0
            scale = (current_usd / last_usd) if (last_usd > 0 and current_usd > 0) else 1.0
            # Compute carry-in: usd value just before S2_START.
            carry_in = 0.0
            for t, u in timeline:
                if t < S2_START_TS: carry_in = u
                else: break
            # Integrate piecewise from S2_START.
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
            for e in new_evs:
                e.setdefault('mint_position', mint)
                e['quest'] = quest   # tag so downstream can attribute cost basis per quest
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

    # Save
    out = {w: dict(r) for w, r in all_results.items() if any(v > 0 for v in r.values())}
    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 's2_orca_flares.json')
    with open(out_path, 'w') as f: json.dump(out, f, indent=2)

    totals = defaultdict(float)
    for w, pq in out.items():
        for q, v in pq.items(): totals[q] += v
    print(f'\nSaved {len(out)} wallets to {out_path}')

    # DB: walker_outputs + sync to wallet_quests
    # Skip syncing any quest whose pool returned empty positions due to RPC
    # flakiness — preserves existing wallet_quests data instead of zeroing.
    WALKER_QUESTS_DB = [q for q in ['S2_ORCA_USX_USDC', 'S2_ORCA_EUSX_USX', 'S2_ORCA_USX_USDG'] if q not in skipped_quests]
    if skipped_quests:
        print(f'NOTE: skipped sync for {skipped_quests} (RPC empty for active pool)', flush=True)
    walker_db.prune('walk_s2_orca')
    rows_db = []
    for w_, pq_ in out.items():
        for q_, v_ in pq_.items():
            if v_ > 0: rows_db.append((w_, q_, v_))
    walker_db.upsert_many('walk_s2_orca', rows_db)
    walker_db.sync_to_wallet_quests('walk_s2_orca', WALKER_QUESTS_DB)
    print(f'DB: walker_outputs={len(rows_db)} rows; synced to wallet_quests')

    # Per-wallet position snapshot + event timeline → quest_cache (S2_ORCA)
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
                'orca_usx_usdc': round(pos_map.get('orca_usx_usdc', 0), 2),
                'orca_eusx_usx': round(pos_map.get('orca_eusx_usx', 0), 2),
                'orca_usx_usdg': round(pos_map.get('orca_usx_usdg', 0), 2),
            },
            'events': events,
            '_watermark': {'slot': 0, 'ts': now_ts},
        }
        db.put_cache(owner, 'S2_ORCA', snap, watermark_ts=now_ts)
        snap_count += 1
    print(f'Per-wallet snapshots written: {snap_count}  ({sum(len(v) for v in position_events_by_owner.values())} total events)')
    print('\nPer-quest totals:')
    for q, v in sorted(totals.items()): print(f'  {q:<32s} {v:>16,.0f}')


if __name__ == '__main__':
    main()

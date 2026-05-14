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
import os, sys, json, time, base64, base58, struct
from datetime import datetime, UTC
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc
import walker_db

# Exponent program ID — emits the Wrapper events via emit_cpi
EXPONENT_PROG = 'ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7'

# Anchor event discriminators for each Wrapper LP event. Computed as
# sha256("event:<EventName>")[:8]; verified against exponent-core source.
# Each event's emit_cpi inner-ix data layout is:
#   bytes[0:8]   anchor IX disc (CPI sentinel, ignored)
#   bytes[8:16]  event disc (matches one of the values below)
#   bytes[16:48] user pubkey
#   bytes[48:80] market pubkey
#   ... event-specific u64 fields ...
#   bytes[-8:]   lp_price (f64) — canonical lp_price_in_asset() at tx time
# Each entry: (lp_amount_offset_from_pubkeys, sign_for_lp_delta).
# lp_amount_offset is the 0-indexed position of the lp_in/lp_out u64 among the
# trailing u64 fields (after the two pubkeys, before the f64 lp_price).
_LP_EVENT_TYPES = {
    bytes.fromhex('d12ae34dbbd811b1'): ('provide',         1, +1),  # WrapperProvideLiquidity: base_in, lp_out, yt_out, lp_price
    bytes.fromhex('3c79a45ddc0d8ec5'): ('provide_base',    3, +1),  # WrapperProvideLiquidityBase: base_in, pt_out, sy_in, lp_out, lp_price
    bytes.fromhex('57a396a2ba93eac8'): ('provide_classic', 2, +1),  # WrapperProvideLiquidityClassic: base_in, pt_in, lp_out, lp_price
    bytes.fromhex('3420b4f124dd48a7'): ('withdraw',         1, -1), # WrapperWithdrawLiquidity: base_out, lp_in, lp_price
    bytes.fromhex('129ad42724179e7c'): ('withdraw_classic', 1, -1), # WrapperWithdrawLiquidityClassic: base_out, lp_in, pt_out, lp_price
}


def _decode_lp_event(data: bytes):
    """Decode an Exponent Wrapper LP event from emit_cpi inner-ix data.
    Returns (event_type, user_pk, market_pk, lp_amount, lp_price) or None.

    Uses the canonical lp_price_in_asset() value emitted by the program, which
    is what Exponent's dashboard displays. This bypasses token-delta heuristics
    entirely and matches the protocol's authoritative LP valuation."""
    if len(data) < 16 + 32 + 32 + 8 + 8: return None
    disc = data[8:16]
    if disc not in _LP_EVENT_TYPES: return None
    event_type, lp_field_idx, sign = _LP_EVENT_TYPES[disc]
    user = base58.b58encode(data[16:48]).decode()
    market = base58.b58encode(data[48:80]).decode()
    # u64 fields between pubkeys (offset 80) and lp_price (last 8 bytes)
    body = data[80:-8]
    n_u64 = len(body) // 8
    if lp_field_idx >= n_u64: return None
    p = lp_field_idx * 8
    lp_raw = int.from_bytes(body[p:p+8], 'little')
    lp_price = struct.unpack('<d', data[-8:])[0]
    return event_type, user, market, lp_raw, lp_price, sign


def _extract_inner_ix_data(tx: dict) -> list:
    """Return [(programId, data_bytes), ...] for every inner instruction in the tx.
    Handles both jsonParsed (base58 data) and base64 encoding formats."""
    out = []
    meta = tx.get('meta') or {}
    inner = meta.get('innerInstructions') or []
    for group in inner:
        for ix in (group.get('instructions') or []):
            pid = ix.get('programId') or ix.get('program')
            d = ix.get('data')
            if not pid or not d: continue
            try:
                # jsonParsed default encoding is base58 for unknown programs
                raw = base58.b58decode(d)
            except Exception:
                try: raw = base64.b64decode(d)
                except Exception: continue
            out.append((pid, raw))
    return out

S2_START_TS = 1776038400
S2_END_TS   = 1785024000   # only used to cap if walking beyond now

# Live eUSX/USD price — pulled from Solstice's protocol API (canonical source).
# We previously read offset 48 of an on-chain PDA which returned ~1.156, but
# that turns out to be a different vault ratio, NOT the eUSX→USD price. The
# correct number (~1.032 as of May 2026) lives in Solstice's /api/protocol
# `eusxPrice` and Exponent's market `syExchangeRate`.
import struct
def get_eusx_peg():
    import urllib3 as _u
    _u.disable_warnings()
    try:
        r = requests.get('https://app.solstice.finance/api/protocol', timeout=8, verify=False).json()
        p = float(r.get('eusxPrice') or 0)
        if 0.9 < p < 2.0: return p
    except Exception: pass
    return 1.0319   # fallback

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


def fetch_all_sigs(addr: str, until_ts: int = 0) -> list:
    """Pull sigs newest→oldest. If until_ts > 0, stop once a sig is older than it;
    if until_ts == 0, walk the full history (needed for pre-S2 carry-in).

    CRITICAL: an empty result mid-pagination is NOT a reliable end-of-history
    marker — RPC nodes (especially from CI runners) sometimes return [] under
    load even when more sigs exist. Retry 4× before treating empty as terminal.
    """
    import time as _t
    sigs = []
    before = None
    while True:
        batch = []
        for attempt in range(4):
            params = [addr, {'limit': 1000}]
            if before: params[1]['before'] = before
            r = rpc('getSignaturesForAddress', params, force_refresh=(attempt > 0))
            batch = r.get('result', []) or []
            if batch: break
            _t.sleep(0.5 * (attempt + 1))
        if not batch: break
        if until_ts > 0:
            keep = [s for s in batch if (s.get('blockTime') or 0) >= until_ts]
            sigs.extend(keep)
            if len(keep) < len(batch): break
        else:
            sigs.extend(batch)
        if len(batch) < 1000: break
        before = batch[-1]['signature']
    return sigs


def _tx_signer(tx: dict) -> str:
    """Return the fee-payer / primary signer of the tx. For every Exponent
    Wrapper* instruction this is the end user — Exponent doesn't have a relayer
    or meta-tx flow. Authoritative attribution; no heuristics needed."""
    msg = (tx.get('transaction') or {}).get('message') or {}
    keys = msg.get('accountKeys') or []
    # jsonParsed format: list of {pubkey, signer, writable, source}
    for k in keys:
        if isinstance(k, dict) and k.get('signer'):
            return k.get('pubkey')
        if isinstance(k, str):
            # Older raw format: signers are the first N entries per
            # header.numRequiredSignatures. Fee payer is index 0.
            return k
    return None


def parse_tx_lp_event(tx: dict, cfg: dict) -> dict:
    """Parse an Exponent LP tx using the program's emitted Wrapper event.

    Returns the canonical (user, lp_delta, lp_price, underlying_delta) where
    lp_price is the exact value Exponent's dashboard displays — computed by
    `market.financials.lp_price_in_asset()` and emitted via emit_cpi! at the
    moment of the tx. This bypasses heuristics entirely:

      * No <1M USD balance filter (worked but skipped whales)
      * No PT/underlying delta accounting (works but loses to slippage)
      * No eUSX peg multiplication (mismatched Exponent's accounting)

    The dashboard shows `amount_lp × lp_price` (in market asset units). For
    flare math, integrate `lp_balance × lp_price(t) × mult × dt` — no peg.
    """
    meta = tx.get('meta') or {}
    if meta.get('err'): return None

    market_pk = cfg['market']

    # Outer signers of the tx — used to detect CPI-routed LP activity. When
    # another protocol (e.g. Loopscale) deposits into Exponent on a user's
    # behalf, the event's `user_address` is the protocol's PDA (PDA-style
    # signed via Anchor seeds), but the outer tx is signed by the actual
    # user. The real user gets rewarded via THEIR primary quest
    # (S2_LOOPSCALE_SUPPLY etc.) — crediting them here with S2_EXPONENT_LP
    # would double-count. The PDA itself should be excluded from leaderboards
    # since it doesn't represent a real participant.
    msg = (tx.get('transaction') or {}).get('message') or {}
    keys = msg.get('accountKeys') or []
    outer_signers = {k.get('pubkey') for k in keys if isinstance(k, dict) and k.get('signer')}

    # Scan inner instructions for an Exponent Wrapper LP event matching this market.
    for prog, data in _extract_inner_ix_data(tx):
        if prog != EXPONENT_PROG: continue
        decoded = _decode_lp_event(data)
        if not decoded: continue
        event_type, user, evt_market, lp_raw, lp_price, sign = decoded
        if evt_market != market_pk: continue
        # CPI-routed depositor → skip. event's user_address must be in the
        # outer tx's signers list for a direct, user-initiated LP action.
        if user not in outer_signers:
            return None
        # Convert lp_raw to UI amount.
        lp_decimals = _get_lp_decimals(cfg['lp_mint'])
        lp_amount_ui = lp_raw / (10 ** lp_decimals)
        lp_delta = sign * lp_amount_ui  # + on supply, - on withdraw

        # Also extract signer-side underlying/PT deltas for snapshot/UI context.
        signer = user  # event user_address == outer signer (verified above)
        pre = {(t['accountIndex'], t['mint']): t for t in (meta.get('preTokenBalances') or [])}
        post = {(t['accountIndex'], t['mint']): t for t in (meta.get('postTokenBalances') or [])}
        pt_mint = _get_pt_mint(market_pk)
        underlying_delta = 0.0
        pt_delta = 0.0
        for k in set(pre) | set(post):
            _, mint = k
            pb = pre.get(k); pob = post.get(k)
            owner = (pob or pb).get('owner')
            if owner != signer: continue
            pre_ui = float((pb or {}).get('uiTokenAmount', {}).get('uiAmount') or 0)
            post_ui = float((pob or {}).get('uiTokenAmount', {}).get('uiAmount') or 0)
            d = post_ui - pre_ui
            if abs(d) < 1e-12: continue
            if mint == cfg['underlying']: underlying_delta += d
            elif pt_mint and mint == pt_mint: pt_delta += d

        return {
            'user': user,
            'event_type': event_type,
            'lp_delta': lp_delta,
            'lp_price': lp_price,           # canonical — what Exponent dashboard shows
            'underlying_delta': underlying_delta,  # informational only
            'pt_delta': pt_delta,                  # informational only
            'per_lp_underlying': lp_price,         # kept for back-compat with integration loop
        }
    return None


# Cache of LP mint → decimals. LP mints are usually 6-decimal but we read it
# once per mint to be safe; market_two.rs doesn't hardcode this.
_lp_decimals_cache: dict = {}

def _get_lp_decimals(lp_mint: str) -> int:
    if lp_mint in _lp_decimals_cache: return _lp_decimals_cache[lp_mint]
    r = rpc('getAccountInfo', [lp_mint, {'encoding': 'jsonParsed'}])
    v = r.get('result', {}).get('value')
    decs = 6
    if v:
        info = (v.get('data', {}) or {}).get('parsed', {}).get('info', {}) or {}
        decs = int(info.get('decimals') or 6)
    _lp_decimals_cache[lp_mint] = decs
    return decs


# Cache of market PDA → its offset-40 mint (PT). Avoids re-fetching the market
# account per tx (it's static for the lifetime of the market).
_pt_mint_cache: dict = {}

def _get_pt_mint(market_pk: str):
    if market_pk in _pt_mint_cache: return _pt_mint_cache[market_pk]
    r = rpc('getAccountInfo', [market_pk, {'encoding': 'base64'}])
    v = r.get('result', {}).get('value')
    if not v:
        _pt_mint_cache[market_pk] = None; return None
    d = base64.b64decode(v['data'][0])
    if len(d) < 72:
        _pt_mint_cache[market_pk] = None; return None
    mint = base58.b58encode(d[40:72]).decode()
    _pt_mint_cache[market_pk] = mint
    return mint


def main():
    eusx_peg = get_eusx_peg()
    MARKETS['eUSX-Jun26']['peg'] = eusx_peg
    print(f'eUSX live peg: ${eusx_peg:.6f}', flush=True)

    now_ts = int(time.time())
    print(f'S2 window: {datetime.fromtimestamp(S2_START_TS, UTC).strftime("%Y-%m-%d")} → now ({(now_ts-S2_START_TS)/86400:.1f} days)\n', flush=True)

    all_results = defaultdict(lambda: defaultdict(float))   # wallet → {quest: flares}
    all_events_by_wallet = defaultdict(list)                  # wallet → all events (both markets)
    all_snapshots = defaultdict(lambda: defaultdict(float))   # wallet → quest → final LP balance × rate
    all_cost_basis = defaultdict(dict)                        # wallet → quest → {usd_paid, usd_recovered, usd_basis, n_supplies, n_withdraws}
    skipped_quests = set()  # quests whose vault returned 0 sigs (RPC flake) — don't sync (would zero existing data)

    for mname, cfg in MARKETS.items():
        print(f'=== {mname} (mult {cfg["mult"]}, peg ${cfg["peg"]:.4f}) ===', flush=True)
        # Walk the FULL LP-vault history. fetch_all_sigs has 4-retry-on-empty
        # protection but persistent RPC failure can still return 0. Don't let
        # that wipe existing wallet_quests data — track skipped quests and
        # exclude from sync.
        sigs = fetch_all_sigs(cfg['lp_vault'], 0)
        in_s2 = sum(1 for s in sigs if (s.get('blockTime') or 0) >= S2_START_TS)
        print(f'  {len(sigs):,} LP-vault sigs total ({in_s2:,} in S2 window)', flush=True)
        if len(sigs) == 0:
            # The LP vaults have thousands of sigs in their lifetime — 0 is
            # always an RPC failure, never legitimate.
            print(f'  WARN: skipping {cfg["quest"]} sync — RPC empty for known-active LP vault', flush=True)
            skipped_quests.add(cfg['quest'])
            continue

        # Fetch txs in parallel
        def fetch(s):
            try:
                r = rpc('getTransaction', [s['signature'], {'encoding':'jsonParsed','maxSupportedTransactionVersion':0}])
                return s, r.get('result')
            except: return s, None

        # per-wallet event timeline (all events, both pre-S2 and S2)
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
        print(f'  unique LP-active wallets (all-time): {len(events_by_wallet):,}', flush=True)

        # Integrate LP × lp_price(t) × mult — no peg multiplication. Exponent's
        # lp_price_in_asset() is already denominated in the market's asset units
        # which Exponent treats 1:1 with USX (i.e. USD-equivalent). Applying the
        # eUSX peg here over-counts eUSX LP by ~14%; the user gets the eUSX
        # yield via the SY exchange rate which is baked into lp_price already.
        peg = cfg.get('peg') or 1.0
        for wallet, all_evs in events_by_wallet.items():
            all_evs.sort(key=lambda x: x['t'])

            # Cost basis: sum of USD invested minus USD recovered across all LP events
            # (pre-S2 + S2). LP price reflects current value of one LP token, so
            # USD per event = |lp_delta| × lp_price × peg. No time-decay needed
            # because lp_price itself evolves over time (unlike YT which decays).
            usd_paid = 0.0
            usd_recovered = 0.0
            n_supplies = 0
            n_withdraws = 0
            for e in all_evs:
                rate = e.get('rate') or 0
                if not rate: continue
                usd = abs(e['lp_delta']) * rate * peg
                if e['lp_delta'] > 0:
                    usd_paid += usd
                    n_supplies += 1
                else:
                    usd_recovered += usd
                    n_withdraws += 1
            if n_supplies + n_withdraws > 0:
                all_cost_basis[wallet][cfg['quest']] = {
                    'usd_basis':     max(0.0, usd_paid - usd_recovered),
                    'usd_paid':      usd_paid,
                    'usd_recovered': usd_recovered,
                    'n_supplies':    n_supplies,
                    'n_withdraws':   n_withdraws,
                }

            # Pre-S2 carry-in: net LP delta + lp_price snapshot at the boundary.
            pre_evs = [e for e in all_evs if e['t'] < S2_START_TS]
            evs = [e for e in all_evs if e['t'] >= S2_START_TS]
            carry_in = sum(e['lp_delta'] for e in pre_evs)
            if carry_in < 0: carry_in = 0.0  # data gap; clamp non-negative
            carry_price = None
            for e in reversed(pre_evs):
                if e.get('rate'):
                    carry_price = e['rate']; break

            lp_balance = carry_in
            last_lp_price = carry_price
            usd_days = 0.0
            prev_t = S2_START_TS
            for i in range(len(evs)):
                e = evs[i]
                t1 = e['t']
                dt = (t1 - prev_t) / 86400
                if dt > 0 and lp_balance > 0 and last_lp_price:
                    usd_days += lp_balance * last_lp_price * dt
                lp_balance += e['lp_delta']
                if e.get('rate'): last_lp_price = e['rate']
                prev_t = t1
            # Tail: last event (or S2_START) to now
            if lp_balance > 0 and last_lp_price and prev_t < now_ts:
                dt = (now_ts - prev_t) / 86400
                if dt > 0:
                    usd_days += lp_balance * last_lp_price * dt
            flares = usd_days * cfg['mult']
            if flares > 0:
                all_results[wallet][cfg['quest']] = flares
            # Store ALL events (pre-S2 + S2) in cache so build_daily_totals.py
            # can reconstruct the correct lp_balance(t) carry-in at S2_START.
            # The chart code clips pre-S2 segments via S2_START_TS in
            # integrate_balance_segments — but it needs the events to know the
            # pre-S2 balance. Without these, wallets holding LP from before S2
            # contribute 0 to the chart.
            all_events_by_wallet[wallet].extend(all_evs)
            if lp_balance > 0 and last_lp_price:
                all_snapshots[wallet][cfg['quest']] = lp_balance * last_lp_price

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
    WALKER_QUESTS_DB = [q for q in ['S2_EXPONENT_LP_USX_JUN26', 'S2_EXPONENT_LP_EUSX_JUN26'] if q not in skipped_quests]
    if skipped_quests:
        print(f'NOTE: skipped sync for {skipped_quests} (RPC empty for active vault)', flush=True)
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
            'cost_basis_by_quest': all_cost_basis.get(wallet, {}),
            '_watermark': {'slot': 0, 'ts': now_ts},
        }
        db.put_cache(wallet, 'S2_EXPONENT_LP', snap, watermark_ts=now_ts)
        snap_count += 1
        total_events += len(drawer_events)
    print(f'Per-wallet snapshots written: {snap_count}  ({total_events} total events)')


if __name__ == '__main__':
    main()

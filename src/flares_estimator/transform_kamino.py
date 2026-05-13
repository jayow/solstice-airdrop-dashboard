"""Recompute Kamino flares from cached events with proper time integration.

The walker (walk_s2_kamino.py) writes flares as `current_usd × mult × total_days`,
which is correct only when the position size was constant since the first event.
For wallets that scaled up/down during S2 the formula systematically over- or
under-counts.

This module replaces the approximation with a proper piecewise integral of
`balance(t) × usd_per_token(mint, t) × mult × dt` walked from cached events:

  1. Group events per (obligation, side, mint).
  2. Reconstruct carry-in: balance at S2_START_TS such that walking the events
     forward arrives at the current on-chain position size.
  3. Walk events chronologically, emit (t0, t1, balance_tokens) segments.
  4. Integrate each segment using:
       - peg_USD for USX/USDG = 1.0 (constant)
       - peg_USD for eUSX     = eusx_peg.peg_at(t_mid)
     Multiply by the quest's flare multiplier.
  5. Sum into the quest result, write to wallet_quests.

Skips wallets whose `positions` snapshot is empty (nothing to integrate).
Reads only from quest_cache — never hits RPC, so it's fast (~1–2 min for the
entire Kamino user base) and idempotent.
"""
import os, sys, json, time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as fdb
from quests.eusx_peg import peg_at as eusx_peg_at
from snapshot_ts import last_snapshot_ts

S2_START_TS = 1776038400
S2_END_TS   = 1785024000

USX_MINT  = '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG'
EUSX_MINT = '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC'
USDG_MINT = '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH'

LEND_QUESTS = {
    USX_MINT:  ('S2_KAMINO_LEND_USX',  5),
    EUSX_MINT: ('S2_KAMINO_LEND_EUSX', 1),
    USDG_MINT: ('S2_KAMINO_LEND_USDG', 5),
}
BORROW_QUESTS = {
    USX_MINT:  ('S2_KAMINO_BORROW_USX',  1),
    USDG_MINT: ('S2_KAMINO_BORROW_USDG', 1),
}

SIDE_MINT_TO_POSKEY = {
    ('lend',   USX_MINT):  'kamino_supply_usx',
    ('lend',   EUSX_MINT): 'kamino_supply_eusx',
    ('lend',   USDG_MINT): 'kamino_supply_usdg',
    ('borrow', USX_MINT):  'kamino_borrow_usx',
    ('borrow', USDG_MINT): 'kamino_borrow_usdg',
}

# Map ix name → (side, signed_direction). Includes V2 variants since Kamino
# emits V2-suffixed instruction names for newer flows.
IX_MAP = {
    'depositreserveliquidityandobligationcollateral':         ('lend',   +1),
    'depositreserveliquidityandobligationcollateralv2':       ('lend',   +1),
    'depositreserveliquidity':                                ('lend',   +1),
    'depositreserveliquidityv2':                              ('lend',   +1),
    'depositobligationcollateral':                            ('lend',   +1),
    'depositobligationcollateralv2':                          ('lend',   +1),
    'withdrawreserveliquidity':                               ('lend',   -1),
    'withdrawreserveliquidityv2':                             ('lend',   -1),
    'withdrawobligationcollateral':                           ('lend',   -1),
    'withdrawobligationcollateralv2':                         ('lend',   -1),
    'withdrawobligationcollateralandredeemreservecollateral': ('lend',   -1),
    'withdrawobligationcollateralandredeemreservecollateralv2':('lend',  -1),
    'borrowobligationliquidity':                              ('borrow', +1),
    'borrowobligationliquidityv2':                            ('borrow', +1),
    'repayobligationliquidity':                               ('borrow', -1),
    'repayobligationliquidityv2':                             ('borrow', -1),
    'liquidateobligationandredeemreservecollateral':          ('lend',   -1),
    'liquidateobligationandredeemreservecollateralv2':        ('lend',   -1),
}

# Map a quest-cache mint to a (current) USD-per-token value. Only used to
# convert the positions[] snapshot (USD) back into token units for carry-in.
def _current_usd_per_token(mint: str) -> float:
    if mint == EUSX_MINT:
        try: return eusx_peg_at(int(time.time()))
        except Exception: return 1.156
    return 1.0


def _peg_at(mint: str, ts: int) -> float:
    if mint == EUSX_MINT:
        try: return eusx_peg_at(ts)
        except Exception: return 1.156
    return 1.0


POSKEY_TO_SIDE_MINT = {v: k for k, v in SIDE_MINT_TO_POSKEY.items()}


def transform_wallet(positions: dict, events: list, now_ts: int) -> dict:
    """Return {quest_code: flares} from a wallet's Kamino cache entry.

    For each (side, mint) covered by a quest:
      - If events exist for that bucket: walk them, integrate piecewise.
      - If no events but positions show a balance: assume the wallet held
        that balance for the full S2 window (pre-S2 deposit, never touched).
    """
    end_ts = min(now_ts, S2_END_TS)
    # Bucket events by (side, mint). We collapse obligation_address — multiple
    # obligations on the same (side, mint) are equivalent from a flare-math
    # perspective (positions snapshot is already summed across obligations).
    by_key = defaultdict(list)
    for e in events or []:
        ix = (e.get('ix') or '').lower()
        if ix not in IX_MAP: continue
        side, sign = IX_MAP[ix]
        ts = e.get('ts')
        if ts is None: continue
        for d in (e.get('deltas') or []):
            mint = d.get('mint')
            amt = float(d.get('amt') or 0)
            if amt <= 0 or not mint: continue
            by_key[(side, mint)].append((ts, sign * amt))

    flares = defaultdict(float)
    all_buckets = set(by_key.keys()) | set(POSKEY_TO_SIDE_MINT[k] for k in (positions or {})
                                            if k in POSKEY_TO_SIDE_MINT and (positions.get(k) or 0) > 0)
    for (side, mint) in all_buckets:
        # Resolve quest + multiplier
        if side == 'lend' and mint in LEND_QUESTS:
            qcode, mult = LEND_QUESTS[mint]
        elif side == 'borrow' and mint in BORROW_QUESTS:
            qcode, mult = BORROW_QUESTS[mint]
        else:
            continue

        evs = sorted(by_key.get((side, mint), []), key=lambda x: x[0])
        # Net flow during S2 (token units)
        net_s2 = sum(d for t, d in evs if S2_START_TS <= t <= end_ts)
        # Current position in tokens (snapshot positions[] is USD)
        pos_key = SIDE_MINT_TO_POSKEY.get((side, mint))
        cur_usd = float((positions or {}).get(pos_key, 0) or 0)
        cur_tokens = cur_usd / _current_usd_per_token(mint) if cur_usd > 0 else 0
        # carry-in: balance the wallet held entering S2, derived so events
        # forward integrate to current position. Clamped to 0.
        carry_in = max(0.0, cur_tokens - net_s2)

        # Walk events, emit segments
        bal = carry_in
        prev_t = S2_START_TS
        for ts, delta in evs:
            if ts < S2_START_TS:
                bal = max(0.0, bal + delta); continue
            seg_end = min(ts, end_ts)
            if seg_end > prev_t and bal > 0:
                flares[qcode] += bal * _peg(mint, prev_t, seg_end) * mult * (seg_end - prev_t) / 86400.0
            bal = max(0.0, bal + delta)
            prev_t = ts
            if prev_t >= end_ts: break
        if prev_t < end_ts and bal > 0:
            flares[qcode] += bal * _peg(mint, prev_t, end_ts) * mult * (end_ts - prev_t) / 86400.0

    return dict(flares)


def _peg(mint: str, t0: int, t1: int) -> float:
    """Average peg over [t0, t1] — uses midpoint for smooth pegs."""
    return _peg_at(mint, (t0 + t1) // 2)


def run_all():
    """Recompute and upsert wallet_quests for every wallet with a S2_KAMINO cache."""
    fdb.init()
    con = fdb.conn()
    rows = con.execute(
        "SELECT wallet, raw_json FROM quest_cache WHERE quest_key='S2_KAMINO'"
    ).fetchall()
    now_ts = last_snapshot_ts()
    all_quests = ['S2_KAMINO_LEND_USX', 'S2_KAMINO_LEND_EUSX', 'S2_KAMINO_LEND_USDG',
                  'S2_KAMINO_BORROW_USX', 'S2_KAMINO_BORROW_USDG']
    n_updated = 0
    old_sum = defaultdict(float); new_sum = defaultdict(float)
    for r in rows:
        try: raw = json.loads(r['raw_json'])
        except Exception: continue
        positions = raw.get('positions') or {}
        events = raw.get('events') or []
        if not events and all((positions.get(k) or 0) <= 0 for k in positions): continue
        out = transform_wallet(positions, events, now_ts)
        # Compare and upsert each quest. Missing keys default to 0 so a quest
        # that no longer applies gets cleared.
        for q in all_quests:
            prev = con.execute(
                'SELECT flares FROM wallet_quests WHERE wallet=? AND quest=?',
                (r['wallet'], q)
            ).fetchone()
            prev_v = float((prev['flares'] if prev else 0) or 0)
            new_v = float(out.get(q, 0) or 0)
            old_sum[q] += prev_v
            new_sum[q] += new_v
            fdb.upsert_wallet_quest(r['wallet'], q, new_v, source='kamino_event_integrated')
        n_updated += 1
    con.commit()
    print(f'Recomputed Kamino flares for {n_updated:,} wallets.')
    print(f'{"Quest":<32} {"OLD":>18}  {"NEW":>18}  {"delta":>18}')
    for q in all_quests:
        d = new_sum[q] - old_sum[q]
        print(f'{q:<32} {old_sum[q]:>18,.0f}  {new_sum[q]:>18,.0f}  {d:>+18,.0f}')


if __name__ == '__main__':
    run_all()

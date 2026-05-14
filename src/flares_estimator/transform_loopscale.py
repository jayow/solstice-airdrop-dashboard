"""Recompute Loopscale borrow flares from cached events.

The walker (walk_s2_loopscale.py) writes BORROW flares as `principal × days`
per loan-history entry from the API. This treats each loan's principal as
constant, missing intra-loan partial repayments — `repayprincipal` events
shrink the principal partway through and the walker doesn't credit the
reduction.

Supply side already uses proper sig-walked balance timelines in the walker
(see walk_supply) — left alone here.

This transform reads only from quest_cache (no RPC) and recomputes
S2_LOOPSCALE_BORROW_USX using piecewise integration of principal_usx(t).
"""
import os, sys, json, time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as fdb
from snapshot_ts import last_snapshot_ts

S2_START_TS = 1776038400
S2_END_TS   = 1785024000

USX_MINT = '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG'

QUEST_CODE = 'S2_LOOPSCALE_BORROW_USX'
MULT = 1

# Signed direction for each ix on the borrow side
IX_SIGN = {
    'createloan':      +1,
    'borrowprincipal': +1,
    'lockloan':         0,   # state change, no principal delta
    'repayprincipal':  -1,
    'closeloan':       -1,
    'withdrawprincipal': -1,
    # Collateral events don't change principal; ignore for borrow integration
    'depositcollateral':  0,
    'withdrawcollateral': 0,
    'stake':              0,
    'unstake':            0,
}


def compute_cost_basis(events: list) -> dict:
    """USD basis on the BORROW side: borrowed - repaid (net outstanding).

    USX is $1-pegged so we sum USX-mint deltas directly. Skips collateral
    ixs (those affect collateral, not principal).

    Returns: {'S2_LOOPSCALE_BORROW_USX': {kind: 'borrow', net_outstanding,
    usd_borrowed, usd_repaid, n_borrows, n_repays}}.
    """
    borrowed = repaid = 0.0
    n_b = n_r = 0
    for e in events or []:
        if e.get('side') != 'borrow': continue
        ix = (e.get('ix') or '').lower()
        sign = IX_SIGN.get(ix)
        if not sign: continue
        # USX-mint deltas only — borrow quest is USX-only
        usx_delta = 0.0
        for d in (e.get('deltas') or []):
            if d.get('mint') != USX_MINT: continue
            usx_delta += float(d.get('amt') or 0)
        if usx_delta <= 0: continue
        if sign > 0:
            borrowed += usx_delta
            n_b += 1
        else:
            repaid += usx_delta
            n_r += 1
    out = {}
    if n_b + n_r > 0:
        out['S2_LOOPSCALE_BORROW_USX'] = {
            'kind': 'borrow',
            'net_outstanding': borrowed - repaid,
            'usd_borrowed':    borrowed,
            'usd_repaid':      repaid,
            'n_borrows':       n_b,
            'n_repays':        n_r,
        }
    # SUPPLY side: lp_balance_change events with signed lp_delta and per-event share_value
    supplied = withdrawn = 0.0
    n_s = n_w = 0
    for e in events or []:
        if e.get('side') != 'supply': continue
        lp_delta = float(e.get('lp_delta') or 0)
        sv = float(e.get('share_value') or 0)
        if sv <= 0: continue
        usd = lp_delta * sv
        if lp_delta > 0:
            supplied += usd
            n_s += 1
        elif lp_delta < 0:
            withdrawn += -usd
            n_w += 1
    if n_s + n_w > 0:
        out['S2_LOOPSCALE_SUPPLY_USX_ONE'] = {
            'kind':          'lend',
            'usd_basis':     max(0.0, supplied - withdrawn),
            'usd_paid':      supplied,
            'usd_recovered': withdrawn,
            'n_supplies':    n_s,
            'n_withdraws':   n_w,
        }
    return out


def transform_wallet(positions: dict, events: list, now_ts: int) -> float:
    end_ts = min(now_ts, S2_END_TS)
    by_loan = defaultdict(list)
    for e in events or []:
        if e.get('side') != 'borrow': continue
        ix = (e.get('ix') or '').lower()
        sign = IX_SIGN.get(ix)
        if sign is None or sign == 0: continue
        ts = e.get('ts')
        if ts is None: continue
        # USX-only borrow quest; only credit USX-mint deltas
        usx_delta = 0.0
        for d in (e.get('deltas') or []):
            if d.get('mint') != USX_MINT: continue
            usx_delta += float(d.get('amt') or 0)
        if usx_delta <= 0: continue
        loan = e.get('pos_pubkey') or 'unknown'
        by_loan[loan].append((ts, sign * usx_delta))

    flares = 0.0
    # Each loan's principal timeline is independent — sum flares across loans.
    for loan, evs in by_loan.items():
        evs.sort(key=lambda x: x[0])
        # Walk forward. Carry-in = 0 (a borrow position can only exist after
        # `createloan`, which is in events; pre-S2 loans that bled into S2 will
        # have their `createloan` event before S2 and we back-extend below).
        bal = 0.0
        # Pre-S2 events update the carry-in
        in_s2 = []
        for ts, d in evs:
            if ts < S2_START_TS:
                bal = max(0.0, bal + d)
            else:
                in_s2.append((ts, d))
        prev_t = S2_START_TS
        for ts, delta in in_s2:
            seg_end = min(ts, end_ts)
            if seg_end > prev_t and bal > 0:
                flares += bal * MULT * (seg_end - prev_t) / 86400.0
            bal = max(0.0, bal + delta)
            prev_t = ts
            if prev_t >= end_ts: break
        if prev_t < end_ts and bal > 0:
            flares += bal * MULT * (end_ts - prev_t) / 86400.0

    # Fallback: if positions show current borrow USX but no events captured,
    # assume full S2 window at that principal. (Edge case for wallets whose
    # walker cache didn't capture all events.)
    if not by_loan:
        cur_usd = float((positions or {}).get('loopscale_borrow_usx', 0) or 0)
        if cur_usd > 0:
            days = (end_ts - S2_START_TS) / 86400.0
            flares = cur_usd * MULT * days
    return flares


def run_all():
    fdb.init()
    con = fdb.conn()
    rows = con.execute(
        "SELECT wallet, raw_json FROM quest_cache WHERE quest_key='S2_LOOPSCALE'"
    ).fetchall()
    now_ts = last_snapshot_ts()
    n_updated = 0
    old_sum = 0.0; new_sum = 0.0
    for r in rows:
        try: raw = json.loads(r['raw_json'])
        except Exception: continue
        positions = raw.get('positions') or {}
        events = raw.get('events') or []
        v = transform_wallet(positions, events, now_ts)
        prev = con.execute(
            'SELECT flares FROM wallet_quests WHERE wallet=? AND quest=?',
            (r['wallet'], QUEST_CODE)
        ).fetchone()
        prev_v = float((prev['flares'] if prev else 0) or 0)
        old_sum += prev_v
        new_sum += v
        if v != prev_v:
            fdb.upsert_wallet_quest(r['wallet'], QUEST_CODE, v, source='loopscale_event_integrated')
            n_updated += 1
    con.commit()
    print(f'Recomputed Loopscale BORROW for {len(rows):,} cached wallets ({n_updated} changed).')
    print(f'{QUEST_CODE}:  OLD={old_sum:,.0f}  NEW={new_sum:,.0f}  delta={new_sum-old_sum:+,.0f}')


if __name__ == '__main__':
    run_all()

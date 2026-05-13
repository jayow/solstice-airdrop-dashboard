"""Reconstruct cumulative S2 flare totals at end-of-day 00:00 UTC boundaries.

Matches Solstice's snapshot cadence (once daily at 00:00 UTC). We stop at the
LAST COMPLETED midnight — today is in-progress until the next 00:00 UTC, so
it's omitted from the chart.

For each (wallet, quest), we integrate from the on-chain timeline / event list
in `quest_cache`. No global linear approximation:

  HOLD USX / eUSX:   piecewise integral of balance(t) timeline
  Exponent YT:        piecewise integral of yt(t) per-position timeline
  Exponent LP:        piecewise integral of lp_balance(t) × rate × peg
                       (lp_delta + rate captured per event)
  Kamino / Loopscale / Orca / Raydium:
                       piecewise integral of position_usd(t) reconstructed
                       from event deltas (sign inferred from ix name)

Carry-in: if current USD > 0 but no open event captured, we assume the
position was opened at S2_START with the current USD value (pre-S2 hold).

Output: server/daily_totals.json
"""
import os, sys, json, sqlite3, time
from datetime import datetime, timezone
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'solstice.db')
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'daily_totals.json')

S2_START_TS = 1776038400          # 2026-04-13 00:00 UTC
EUSX_PEG    = 1.156               # live peg captured from chain; close enough for chart
HOLD_MULT   = {'USX': 10, 'eUSX': 2}
YT_MULT     = {
    'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm': 30,
    'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP': 15,
}
LP_QUEST_TO_MULT = {
    'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm': 20,    # USX-Jun26 LP
    'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP': 10,    # eUSX-Jun26 LP
}
LP_PEG = {
    'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm': 1.0,
    'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP': EUSX_PEG,
}

# Per-event sign convention: + = position USD grew, - = shrank, 0 = no change
ORCA_RAY_SIGN = {
    'openposition': +1, 'openpositionwithmetadata': +1,
    'openpositionwithtokenextensions': +1, 'openpositionwithtoken22nft': +1,
    'openpositionv2': +1,
    'increaseliquidity': +1, 'increaseliquidityv2': +1,
    'decreaseliquidity': -1, 'decreaseliquidityv2': -1,
    'closeposition': -1, 'closepositionwithtokenextensions': -1,
    'closepositionwithtoken22nft': -1,
    'collectfees': 0, 'collectfeesv2': 0,
    'collectreward': 0, 'collectrewardv2': 0,
    'swap': 0, 'swapv2': 0, 'twohopswap': 0, 'twohopswapv2': 0,
    'updatefeesandrewards': 0,
}
KAMINO_SIGN = {
    'depositreserveliquidity': +1,
    'depositreserveliquidityandobligationcollateral': +1,
    'depositobligationcollateral': +1,
    'withdrawreserveliquidity': -1,
    'withdrawobligationcollateral': -1,
    'withdrawobligationcollateralandredeemreservecollateral': -1,
    'borrowobligationliquidity': +1,
    'repayobligationliquidity': -1,
    'liquidateobligationandredeemreservecollateral': -1,
}
LOOP_SIGN = {
    'create_loan': +1, 'createloan': +1,
    'borrow_principal': +1, 'borrowprincipal': +1,
    'deposit_principal': +1, 'depositprincipal': +1,
    'deposit_collateral': +1, 'depositcollateral': +1,
    'stake': 0,
    'close_loan': -1, 'closeloan': -1,
    'lock_loan': 0, 'lockloan': 0,
    'repay_principal': -1, 'repayprincipal': -1,
    'withdraw_principal': -1, 'withdrawprincipal': -1,
    'withdraw_collateral': -1, 'withdrawcollateral': -1,
    'unstake': 0,
}


def integrate_balance_segments(segments, day_ends, mult, integrate_end):
    """Given segments = [(t_start, t_end, value), ...] (chronological), and
    a list of day-end timestamps, return (per_day, total_through_integrate_end).

    `value` is the USD-equivalent. Daily flare contribution from a segment is
    `value × mult × dt_days`. Each segment is clipped to S2 window. The
    `total_through_integrate_end` is the integral from S2_START to
    `integrate_end` (which is typically `now`, not the last completed
    midnight) — used as the normalization denominator so per-day values stay
    consistent with the wallet's *current* flares total."""
    per_day = [0.0] * len(day_ends)
    total = 0.0
    for t0, t1, v in segments:
        if v <= 0: continue
        if t0 < S2_START_TS: t0 = S2_START_TS
        t1c = min(t1, integrate_end)
        if t1c <= t0: continue
        total += v * mult * (t1c - t0) / 86400.0
        for i, day_end in enumerate(day_ends):
            day_start = day_end - 86400
            overlap_start = max(t0, day_start)
            overlap_end = min(t1c, day_end)
            if overlap_end <= overlap_start: continue
            per_day[i] += v * mult * (overlap_end - overlap_start) / 86400.0
    return per_day, total


def timeline_to_segments(timeline, end_ts):
    """[(ts, bal), ...] → [(t_start, t_end, bal_during), ...]"""
    segs = []
    if not timeline: return segs
    # Back-extend: if first point > S2_START, assume held first balance from S2_START
    first_t, first_b = timeline[0]
    if first_t > S2_START_TS and first_b > 0:
        segs.append((S2_START_TS, first_t, first_b))
    for i in range(len(timeline) - 1):
        t0, b0 = timeline[i]; t1, _ = timeline[i+1]
        if t1 > end_ts: t1 = end_ts
        if t1 > t0 and b0 > 0:
            segs.append((t0, t1, b0))
    # Tail: last point extends to end_ts at last balance
    last_t, last_b = timeline[-1]
    if last_t < end_ts and last_b > 0:
        segs.append((last_t, end_ts, last_b))
    return segs


def events_to_segments(events, current_usd, sign_map, peg, end_ts):
    """Reconstruct a position's USD-over-time from its event list + current USD.

    Algorithm (working forward in time):
      1. Sum signed event amounts to get net flow during S2 from event log.
      2. carry_in_usd = current_usd - net_flow_since_pos_open
         (clamped to 0)
      3. Start at S2_START with carry_in_usd.
      4. Apply each event's delta_usd in chronological order; emit segments.

    For events with sign=0 (swaps, fee collections) we ignore them — they
    don't change the position's USD-equivalent.
    """
    segs = []
    if not events:
        # No events → assume held current_usd since S2_START
        if current_usd > 0:
            segs.append((S2_START_TS, end_ts, current_usd))
        return segs

    # Filter to events with a defined sign
    typed = []
    for e in sorted(events, key=lambda e: e.get('ts') or 0):
        ix = (e.get('ix') or '').lower()
        s = sign_map.get(ix)
        if s is None: continue
        if s == 0:
            typed.append((e.get('ts'), 0, 0)); continue
        delta_usd = sum((d.get('amt') or 0) for d in (e.get('deltas') or [])) * peg
        typed.append((e.get('ts'), s, s * delta_usd))

    # Sum net flow from events; carry-in = current_usd - net_flow
    net = sum(d for _, _, d in typed)
    carry_in = max(0.0, current_usd - net)

    # Walk forward, emit segments between events
    bal = carry_in
    prev_t = S2_START_TS
    for ts, sign, d in typed:
        if ts is None: continue
        seg_end = min(ts, end_ts)
        if seg_end > prev_t and bal > 0:
            segs.append((prev_t, seg_end, bal))
        bal = max(0.0, bal + d)
        prev_t = ts
        if prev_t >= end_ts: break
    # Tail
    if prev_t < end_ts and bal > 0:
        segs.append((prev_t, end_ts, bal))
    return segs


def main():
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    now = int(time.time())

    # Exclude every wallet the dashboard hides. build_data.py filters records
    # by `classification in ('pda', 'pda_or_uninit')` (line 71) — match that
    # exactly so daily-chart totals reconcile with the headline number.
    # `pda_protocol` is kept here too because the dashboard hides it by
    # default via the Hide-PDAs toggle.
    pda_set = set()
    for r in con.execute("SELECT wallet FROM wallets WHERE classification IN ('pda','pda_or_uninit','pda_protocol')"):
        pda_set.add(r['wallet'])
    p_pdas = os.path.join(ROOT, 'data', 'protocol_pdas.json')
    if os.path.exists(p_pdas):
        manual = json.load(open(p_pdas)).get('addresses') or {}
        for a in manual.keys(): pda_set.add(a)
    print(f'Excluding {len(pda_set):,} PDA / uninit wallets (matches dashboard filter).')

    # Day-end boundaries at 00:00 UTC. Each "day D" bucket covers
    # [D 00:00, D+1 00:00). The chart shows days where end-of-bucket has
    # already passed.
    last_complete_midnight = (now // 86400) * 86400   # most recent 00:00 UTC ≤ now
    if last_complete_midnight <= S2_START_TS:
        print('S2 has not started a full day yet.'); return
    n_days = int((last_complete_midnight - S2_START_TS) // 86400)
    day_ends = [S2_START_TS + (i + 1) * 86400 for i in range(n_days)]
    end_ts = day_ends[-1]
    # Segments are built out to NOW (so a wallet's full current-flares total is
    # represented). The day_ends array stops at last completed midnight; the
    # contribution beyond that goes into `total` only, used for normalization.
    integrate_end = now

    print(f'Reconstructing {n_days} complete S2 days through {datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")} UTC (yesterday 00:00 UTC).')

    day_totals = [0.0] * n_days
    # Per-partner daily inflation. Keys match `PROTOCOLS` in build_data.py so the
    # frontend can render a stacked-bar view with the same color scheme as the
    # leaderboard's protocol columns.
    partner_day_totals = defaultdict(lambda: [0.0] * n_days)
    def _add(partner_key, per_day):
        for i, v in enumerate(per_day):
            day_totals[i] += v
            partner_day_totals[partner_key][i] += v

    # === HOLD ===
    # Per-wallet scaling: walk each wallet's full quest, normalize emitted to
    # match wallet_quests.flares, then only the day_ends portion contributes
    # to the chart.
    HOLD_QUEST = {'USX': 'S2_HOLD_USX_DAILY', 'eUSX': 'S2_HOLD_EUSX_DAILY'}
    # HOLD USX → "solstice" partner; HOLD eUSX → "yield_vault" (matches quest_map.py)
    HOLD_PARTNER = {'USX': 'solstice', 'eUSX': 'yield_vault'}
    for ek, lbl in [('S2_HOLD_USX','USX'), ('S2_HOLD_EUSX','eUSX')]:
        mult = HOLD_MULT[lbl]
        qcode = HOLD_QUEST[lbl]
        partner = HOLD_PARTNER[lbl]
        for r in con.execute("SELECT wallet, raw_json FROM quest_cache WHERE quest_key=?", (ek,)):
            if r['wallet'] in pda_set: continue
            raw = json.loads(r['raw_json'])
            tl = raw.get('timeline') or []
            if not tl: continue
            segs = timeline_to_segments(tl, integrate_end)
            per_day, emitted = integrate_balance_segments(segs, day_ends, mult, integrate_end)
            tr = con.execute('SELECT flares FROM wallet_quests WHERE wallet=? AND quest=?', (r['wallet'], qcode)).fetchone()
            target = tr[0] if tr else 0
            if emitted > 0 and target > 0:
                k = target / emitted
                per_day = [v * k for v in per_day]
            elif target <= 0:
                # wallet_quests has 0 for this (wallet, quest) → wallet's cache may
                # have closed events the dashboard hides. Don't bloat the chart.
                per_day = [0.0] * n_days
            _add(partner, per_day)

    # === Exponent YT ===
    YT_QUEST = {
        'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm': 'S2_EXPONENT_YIELD_USX_JUN26',
        'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP': 'S2_EXPONENT_YIELD_EUSX_JUN26',
    }
    for r in con.execute("SELECT wallet, raw_json FROM quest_cache WHERE quest_key='S2_EXPONENT_YT'"):
        if r['wallet'] in pda_set: continue
        raw = json.loads(r['raw_json'])
        # Walk all the wallet's YT positions, group per-market, normalize per-market
        per_market_per_day = defaultdict(lambda: [0.0]*n_days)
        per_market_emit = defaultdict(float)
        for mkt, positions in (raw.get('positions_by_market') or {}).items():
            mult = YT_MULT.get(mkt)
            if not mult: continue
            for p in positions:
                if p.get('method') == 'current_state_fallback' and not p.get('is_emitting'):
                    continue
                tl = p.get('timeline') or []
                if not tl: continue
                segs = timeline_to_segments(tl, integrate_end)
                pd, em = integrate_balance_segments(segs, day_ends, mult, integrate_end)
                for i, v in enumerate(pd): per_market_per_day[mkt][i] += v
                per_market_emit[mkt] += em
        for mkt, per_day in per_market_per_day.items():
            qcode = YT_QUEST.get(mkt)
            if not qcode: continue
            emitted = per_market_emit[mkt]
            tr = con.execute('SELECT flares FROM wallet_quests WHERE wallet=? AND quest=?', (r['wallet'], qcode)).fetchone()
            target = tr[0] if tr else 0
            if emitted > 0 and target > 0:
                k = target / emitted
                per_day = [v * k for v in per_day]
            elif target <= 0:
                # wallet_quests has 0 for this (wallet, quest) → wallet's cache may
                # have closed events the dashboard hides. Don't bloat the chart.
                per_day = [0.0] * n_days
            _add('exponent', per_day)

    # === Exponent LP: events have lp_delta + rate → reconstruct lp_balance(t) ===
    LP_QUEST_CODE = {
        'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm': 'S2_EXPONENT_LP_USX_JUN26',
        'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP': 'S2_EXPONENT_LP_EUSX_JUN26',
    }
    for r in con.execute("SELECT wallet, raw_json FROM quest_cache WHERE quest_key='S2_EXPONENT_LP'"):
        if r['wallet'] in pda_set: continue
        raw = json.loads(r['raw_json'])
        evs = raw.get('events') or []
        if not evs: continue
        by_market = defaultdict(list)
        for e in evs:
            by_market[e.get('pos_pubkey')].append(e)
        for mkt, list_ in by_market.items():
            mult = LP_QUEST_TO_MULT.get(mkt)
            peg = LP_PEG.get(mkt, 1.0)
            if not mult: continue
            list_.sort(key=lambda e: e.get('ts') or 0)
            segs = []
            bal = 0.0
            prev_t = S2_START_TS
            last_rate = None
            for e in list_:
                ld = e.get('lp_delta')
                if ld is None:
                    ix = (e.get('ix') or '').lower()
                    ld = (1 if 'increase' in ix or 'open' in ix else -1) * sum((d.get('amt') or 0) for d in (e.get('deltas') or []))
                rate = e.get('rate') or last_rate or 1.0
                ts = e.get('ts')
                if ts is None: continue
                if ts > prev_t and bal > 0 and last_rate:
                    segs.append((prev_t, min(ts, integrate_end), bal * last_rate * peg))
                bal = max(0.0, bal + ld)
                prev_t = ts
                last_rate = rate
                if prev_t >= integrate_end: break
            if prev_t < integrate_end and bal > 0 and last_rate:
                segs.append((prev_t, integrate_end, bal * last_rate * peg))
            per_day, emitted = integrate_balance_segments(segs, day_ends, mult, integrate_end)
            qcode = LP_QUEST_CODE.get(mkt)
            if qcode:
                tr = con.execute('SELECT flares FROM wallet_quests WHERE wallet=? AND quest=?', (r['wallet'], qcode)).fetchone()
                target = tr[0] if tr else 0
                if emitted > 0 and target > 0:
                    per_day = [v * (target/emitted) for v in per_day]
                elif target <= 0:
                    per_day = [0.0] * n_days
            _add('exponent', per_day)

    # === Kamino / Loopscale / Orca / Raydium: reconstruct USD over time ===
    PROTOCOL_SPECS = [
        ('S2_ORCA',     'whirlpool', ORCA_RAY_SIGN, {
            'orca_usx_usdc': ('S2_ORCA_USX_USDC', 9),
            'orca_eusx_usx': ('S2_ORCA_EUSX_USX', 4),
            'orca_usx_usdg': ('S2_ORCA_USX_USDG', 9),
        }),
        ('S2_RAYDIUM',  'raydium', ORCA_RAY_SIGN, {
            'raydium_usx_usdc': ('S2_RAYDIUM_USX_USDC', 9),
            'raydium_eusx_usx': ('S2_RAYDIUM_EUSX_USX', 4),
        }),
        ('S2_KAMINO',   'kamino', KAMINO_SIGN, {
            'kamino_supply_usx':       ('S2_KAMINO_LEND_USX',  5),
            'kamino_supply_eusx':      ('S2_KAMINO_LEND_EUSX', 1),
            'kamino_supply_usdg':      ('S2_KAMINO_LEND_USDG', 5),
            'kamino_borrow_usx':       ('S2_KAMINO_BORROW_USX', 1),
            'kamino_borrow_usdg':      ('S2_KAMINO_BORROW_USDG', 1),
            'kamino_kvault_usx_usdg':  ('S2_KAMINO_KVAULT_USDG_USX', 10),
        }),
        ('S2_LOOPSCALE', 'loopscale', LOOP_SIGN, {
            'loopscale_supply_usx':    ('S2_LOOPSCALE_SUPPLY_USX_ONE', 5),
            'loopscale_borrow_usx':    ('S2_LOOPSCALE_BORROW_USX', 1),
        }),
    ]
    # For each cache, integrate per-position USD-over-time, summed across the
    # wallet's positions, weighted by each quest's multiplier.
    for cache_key, partner, sign_map, pos_to_quest_mult in PROTOCOL_SPECS:
        for r in con.execute("SELECT wallet, raw_json FROM quest_cache WHERE quest_key=?", (cache_key,)):
            if r['wallet'] in pda_set: continue
            try: raw = json.loads(r['raw_json'])
            except Exception: continue
            positions = raw.get('positions') or {}
            events = raw.get('events') or []
            # Group events by position pubkey
            events_by_pos = defaultdict(list)
            for e in events: events_by_pos[e.get('pos_pubkey')].append(e)
            # Distribute current_usd across positions: if multiple positions, allocate by event-sum
            # For simplicity, treat each `pos` separately if its key is in positions dict
            for pos_key, (quest_code, mult) in pos_to_quest_mult.items():
                current_usd = positions.get(pos_key) or 0
                # Try to find events relevant to this position-key. For Orca/Raydium
                # the position pubkey is the actual on-chain account, distinct from
                # the position-key label. We don't have a clean mapping → fallback to
                # "all events for this protocol", which is fine because each
                # protocol_key sums correctly.
                # For Kamino/Loopscale, similar story.
                if current_usd <= 0 and not events: continue
                segs = events_to_segments(events, current_usd, sign_map, peg=1.0, end_ts=integrate_end)
                per_day, emitted = integrate_balance_segments(segs, day_ends, mult, integrate_end)
                tr = con.execute('SELECT flares FROM wallet_quests WHERE wallet=? AND quest=?', (r['wallet'], quest_code)).fetchone()
                target = tr[0] if tr else 0
                if emitted > 0 and target > 0:
                    k = target / emitted
                    per_day = [v * k for v in per_day]
                elif target > 0 and not segs:
                    per_day = [target / n_days] * n_days
                elif target <= 0:
                    # wallet_quests zeroed this (CLMM in-range gating, Kamino
                    # event integration with closed positions, etc.) — keep chart
                    # in lock-step with the dashboard.
                    per_day = [0.0] * n_days
                _add(partner, per_day)

    # Build output: day_totals[i] = inflation on day i; cumulative is the running sum.
    # Also emit per-partner cumulative + inflation so the frontend can render a
    # stacked / filtered view.
    partners = sorted(partner_day_totals.keys())
    partner_cum = {p: 0.0 for p in partners}
    out_days = []
    cum = 0.0
    for i, end in enumerate(day_ends):
        cum += day_totals[i]
        row = {'date': datetime.fromtimestamp(end - 1, tz=timezone.utc).strftime('%Y-%m-%d'),
               'cumulative': round(cum, 0),
               'inflation': round(day_totals[i], 0),
               'by_partner': {}}
        for p in partners:
            partner_cum[p] += partner_day_totals[p][i]
            row['by_partner'][p] = {
                'cumulative': round(partner_cum[p], 0),
                'inflation': round(partner_day_totals[p][i], 0),
            }
        out_days.append(row)
    final_cum = cum
    partner_grand_totals = {p: round(partner_cum[p], 0) for p in partners}

    grand_db = con.execute("SELECT SUM(flares) FROM wallet_quests WHERE flares > 0").fetchone()[0] or 0
    pda_excl = con.execute(
        "SELECT SUM(wq.flares) FROM wallet_quests wq JOIN wallets w ON wq.wallet=w.wallet "
        "WHERE w.classification='pda_protocol'"
    ).fetchone()[0] or 0
    grand_non_pda = grand_db - pda_excl

    payload = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        's2_start_ts': S2_START_TS,
        'last_complete_midnight_ts': end_ts,
        'partners': partners,
        'partner_totals': partner_grand_totals,
        'days': out_days,
        'sources': {
            'reconstructed_through_last_midnight': round(final_cum, 0),
            'wallet_quests_grand_total_now': round(grand_db, 0),
            'wallet_quests_grand_total_non_pda': round(grand_non_pda, 0),
            'pda_excluded_count': len(pda_set),
            'pda_excluded_flares': round(pda_excl, 0),
            'note': 'Bars represent flares as of each day\'s 00:00 UTC. Today is in-progress and is omitted until tomorrow\'s 00:00 UTC. Protocol-PDA wallets are excluded to match Solstice\'s leaderboard.',
        },
    }
    with open(OUT, 'w') as f: json.dump(payload, f, indent=2)
    print(f'\nReconstructed cum @ last midnight ({datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m-%d 00:00")} UTC): {final_cum:,.0f}')
    print(f'  wallet_quests grand total NOW (all):      {grand_db:,.0f}')
    print(f'  wallet_quests grand total NOW (non-PDA):  {grand_non_pda:,.0f}')
    # Pull the most recent solstice_dashboard snapshot so this line stays
    # current as new daily Solstice numbers are recorded.
    sol = con.execute("SELECT date_utc, grand_total FROM flares_snapshots "
                       "WHERE source='solstice_dashboard' ORDER BY ts DESC LIMIT 1").fetchone()
    if sol:
        print(f'  Solstice {sol["date_utc"]} 00:00 UTC:           {sol["grand_total"]:>15,.0f}  (reference)')
    print(f'Wrote {OUT}')


if __name__ == '__main__':
    main()

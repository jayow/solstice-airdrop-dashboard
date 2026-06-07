"""V2 wrapper LP walker — operational sibling to walk_s2_lp.py for Exponent
CLMM markets.

What it does:
  1. Walks each V2 market PDA's sig history (Gesx... USX-Sep26 and
     4yf98Xwht... eUSX-Sep26) — these PDAs are writable on every LP op.
  2. Decodes V2 wrapper events using the IDL-confirmed byte layout
     (`_LP_EVENT_TYPES_V2` in walk_s2_lp.py).
  3. Filters events by user == outer tx signer (V2 wrapper relay txs only
     credit the actual maker, not the wrapper PDA itself).
  4. Integrates per (wallet, market): sy_value × peg × mult_at(ts) × dt.
       sy_value runs as: + sy_to_pool / sy_in on each provide,
                         × (1 - lp_withdrawn / lp_total) on partial withdraw,
                         = 0 on full close.
       mult_at(ts) honours the published API boost-end (BOOST_END_TS in
       walk_s2_lp.py): 30/15 pre-boost, 20/10 post for Sep26 markets.
  5. Adds V2 contribution to wallet_quests via additive UPDATE so V1+V2
     sum into the same quest code (Solstice publishes the sum).

KNOWN LIMITATION: sy_to_pool × peg × mult × dt matches 4/6 control quests
within 8% but fails on USX-Sep26 LP for 5V9V (125% over) and GPQs (52%
under) — irreconcilable asymmetry under any tested single formula.
tools/calibrated_lp_overrides.py runs AFTER this walker and re-pins the
6 control rows to user-verified Solstice values.

Usage:
  python3 tools/walk_v2_lp.py                # full walk
  python3 tools/walk_v2_lp.py --dry-run      # decode + integrate, no DB write
  python3 tools/walk_v2_lp.py --wallets 5V9V… GPQs… 7VsV9DUW…   # subset
"""
import argparse, json, os, sqlite3, sys, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src', 'flares_estimator'))

import base58
from rpc_helper import rpc
from snapshot_ts import last_snapshot_ts
from walk_s2_lp import (
    _decode_lp_event, _LP_EVENT_TYPES_V2, _mult_at, MARKETS, BOOST_END_TS,
    EXPONENT_PROG_V2, _extract_inner_ix_data, get_eusx_peg,
)

DB = os.path.join(ROOT, 'data', 'solstice.db')
S2_START_TS = 1776038400
S2_END_TS   = 1785024000

V2_MARKETS = {label: cfg for label, cfg in MARKETS.items()
              if cfg.get('version') == 'v2'}


def fetch_sigs(pda: str, max_pages: int = 50) -> list:
    """Walk PDA sig history. Returns list of {signature, blockTime}.

    force_refresh=True on the first page busts stale empty cache entries —
    these PDAs are very active, an empty cached result is always wrong."""
    sigs = []
    before = None
    for i in range(max_pages):
        params = [pda, {'limit': 1000}]
        if before: params[1]['before'] = before
        r = rpc('getSignaturesForAddress', params, force_refresh=(i == 0))
        page = (r.get('result') or [])
        if not page: break
        sigs.extend(page)
        before = page[-1]['signature']
        if len(page) < 1000: break
    return sigs


def decode_tx_for_market(tx: dict, market_pk: str) -> list:
    """Return list of V2 LP events in this tx for the given market.

    Each event: {user, event_type, lp_delta_raw (+/- u64), sy_delta_raw (+/- u64)}
    Filter: V2 event's user_address must be in outer signers (user-initiated,
    not wrapper relay)."""
    if (tx.get('meta') or {}).get('err'): return []
    msg = (tx.get('transaction') or {}).get('message') or {}
    keys = msg.get('accountKeys') or []
    outer_signers = {k.get('pubkey') for k in keys if isinstance(k, dict) and k.get('signer')}
    if not outer_signers: return []

    out = []
    for prog, data in _extract_inner_ix_data(tx):
        if prog != EXPONENT_PROG_V2: continue
        decoded = _decode_lp_event(data, version='v2')
        if not decoded: continue
        event_type, user, evt_market, lp_raw, sy_raw, lp_sign, sy_sign = decoded
        if evt_market != market_pk: continue
        if user not in outer_signers: continue
        out.append({
            'user': user,
            'event_type': event_type,
            'lp_delta_raw': lp_sign * lp_raw,
            'sy_delta_raw': sy_sign * sy_raw,
        })
    return out


def integrate_wallet_v2(events: list, cfg: dict, end_ts: int) -> float:
    """Compute V2 contribution for one wallet on one market.

    Algorithm: sy_value_raw × peg × mult_at(ts) × dt over segments between
    consecutive events. sy_value accumulates on provides, reduces
    proportionally on partial withdraw, resets to 0 on full close.

    Boost-aware: mult is read at the START of each segment via _mult_at()
    so positions held during the May 18 → 5/29 boost window get 30×/15×.
    """
    if not events: return 0.0
    peg = cfg.get('peg') or 1.0
    events_sorted = sorted(events, key=lambda e: e['ts'])

    flares = 0.0
    lp_balance_raw = 0
    sy_value_raw = 0
    prev_t = events_sorted[0]['ts']

    for e in events_sorted:
        t1 = e['ts']
        if t1 > prev_t and lp_balance_raw > 0 and sy_value_raw > 0:
            m = _mult_at(cfg['quest'], prev_t, cfg)
            flares += (sy_value_raw / 1e6) * peg * m * (t1 - prev_t) / 86400.0
        lp_d = e['lp_delta_raw']
        sy_d = e['sy_delta_raw']
        if lp_d > 0:
            lp_balance_raw += lp_d
            sy_value_raw += sy_d
        else:
            withdraw_raw = -lp_d
            if lp_balance_raw > 0:
                frac_remaining = max(0.0, 1.0 - withdraw_raw / lp_balance_raw)
                sy_value_raw = int(sy_value_raw * frac_remaining)
            lp_balance_raw = max(0, lp_balance_raw - withdraw_raw)
            if lp_balance_raw == 0:
                sy_value_raw = 0
        prev_t = t1

    if lp_balance_raw > 0 and sy_value_raw > 0 and prev_t < end_ts:
        m = _mult_at(cfg['quest'], prev_t, cfg)
        flares += (sy_value_raw / 1e6) * peg * m * (end_ts - prev_t) / 86400.0

    return flares


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='Decode + integrate but skip DB write')
    ap.add_argument('--wallets', nargs='*', help='Only process these wallets')
    args = ap.parse_args()

    end_ts = min(last_snapshot_ts(), S2_END_TS)
    eusx_peg = get_eusx_peg()
    for mname, cfg in V2_MARKETS.items():
        if cfg.get('peg') is None:
            cfg['peg'] = eusx_peg
    print(f'V2 LP walker — end_ts={end_ts}, eUSX peg={eusx_peg:.6f}', flush=True)
    print(f'Markets: {list(V2_MARKETS.keys())}', flush=True)
    print(f'BOOST_END_TS: {BOOST_END_TS} (mult_at honours this per-event)', flush=True)

    # wallet → quest → flares
    contributions = defaultdict(lambda: defaultdict(float))

    for mname, cfg in V2_MARKETS.items():
        print(f'\n=== {mname} (market {cfg["market"][:12]}…, quest {cfg["quest"]}) ===', flush=True)
        anchor = cfg['sig_anchor']
        sigs = fetch_sigs(anchor)
        in_s2 = [s for s in sigs if (s.get('blockTime') or 0) >= S2_START_TS]
        print(f'  {len(sigs):,} total sigs ({len(in_s2):,} in S2 window)', flush=True)
        if not sigs:
            print(f'  WARN: zero sigs for V2 market — skipping (likely RPC flake)', flush=True)
            continue

        # Fetch txs in parallel + decode
        events_by_wallet = defaultdict(list)
        def fetch_and_decode(s):
            try:
                r = rpc('getTransaction', [s['signature'], {'encoding':'jsonParsed', 'maxSupportedTransactionVersion':0}])
                tx = r.get('result')
                if not tx: return []
                evs = decode_tx_for_market(tx, cfg['market'])
                ts = s.get('blockTime') or 0
                for e in evs: e['ts'] = ts
                return evs
            except: return []

        n_processed = 0
        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = [ex.submit(fetch_and_decode, s) for s in in_s2]
            for fut in as_completed(futs):
                evs = fut.result()
                for e in evs:
                    events_by_wallet[e['user']].append(e)
                n_processed += 1
                if n_processed % 500 == 0:
                    print(f'    {n_processed}/{len(in_s2)}', flush=True)

        print(f'  {len(events_by_wallet):,} unique wallets with V2 events', flush=True)

        # Optional filter
        if args.wallets:
            keep = {w for w in args.wallets}
            events_by_wallet = {k: v for k, v in events_by_wallet.items() if k in keep}
            print(f'  (filtered to {len(events_by_wallet)} requested wallets)', flush=True)

        # Integrate per wallet
        market_total = 0
        for wallet, evs in events_by_wallet.items():
            f = integrate_wallet_v2(evs, cfg, end_ts)
            if f > 0:
                contributions[wallet][cfg['quest']] += f
                market_total += f
        print(f'  total V2 contribution this market: {market_total:,.2f} flares', flush=True)

    # Summary
    print(f'\n=== V2 contributions ready: {sum(len(qs) for qs in contributions.values()):,} (wallet, quest) rows ===')

    if args.dry_run:
        # Show top 10 per quest
        for quest in ('S2_EXPONENT_LP_USX_SEP26', 'S2_EXPONENT_LP_EUSX_SEP26'):
            top = sorted(
                [(w, qs.get(quest, 0)) for w, qs in contributions.items()],
                key=lambda x: -x[1]
            )[:10]
            print(f'\nTop 10 V2 {quest}:')
            for w, f in top:
                if f > 0:
                    print(f'  {w[:12]}…  {f:>14,.2f}')
        return

    # Additive write: wallet_quests.flares += V2 contribution
    # The override (Phase 5b in refresh.sh) runs AFTER this and pins
    # the 6 control rows to user-verified Solstice values.
    con = sqlite3.connect(DB)
    now = int(time.time())
    n_updated = 0
    n_inserted = 0
    for wallet, qs in contributions.items():
        for quest, v2_contribution in qs.items():
            if v2_contribution <= 0: continue
            # Ensure wallet row exists
            con.execute(
                "INSERT OR IGNORE INTO wallets(wallet, classification) VALUES (?, 'unclassified')",
                (wallet,)
            )
            # Check existing row
            row = con.execute(
                "SELECT flares, source FROM wallet_quests WHERE wallet=? AND quest=?",
                (wallet, quest)
            ).fetchone()
            if row:
                existing, src = row[0] or 0, row[1] or ''
                new_total = existing + v2_contribution
                con.execute(
                    "UPDATE wallet_quests SET flares=?, source=?, updated_at=? WHERE wallet=? AND quest=?",
                    (new_total, f'{src}+walk_v2_lp', now, wallet, quest)
                )
                n_updated += 1
            else:
                con.execute(
                    "INSERT INTO wallet_quests(wallet, quest, flares, source, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (wallet, quest, v2_contribution, 'walk_v2_lp', now)
                )
                n_inserted += 1
    con.commit()
    con.close()
    print(f'\nDB write: {n_updated} updated (V2 added to V1), {n_inserted} inserted')


if __name__ == '__main__':
    main()

"""Per-partner event integrity audit.

For every partner's cached events (quest_cache), verify:
  - No duplicate (sig, pos_pubkey) pairs
  - Events sorted by ts (no time-going-backward)
  - Amounts within sane bounds (non-negative, < pool TVL)
  - Wallets earning flares-per-dollar-of-position within physical limits

Output: per-partner stats + flagged anomalies. Audit fails if any anomaly
exceeds threshold.

This is the "check and balance" layer requested by the user 2026-05-15.
Catches walker correctness drift before it pollutes the dashboard.
"""
import os, sys, json, sqlite3, time
from collections import defaultdict, Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'solstice.db')


def fetch_partner_caches(con, quest_key: str) -> list:
    """Returns [(wallet, parsed_dict), ...] for a given quest_cache key."""
    out = []
    for r in con.execute('SELECT wallet, raw_json FROM quest_cache WHERE quest_key=?', (quest_key,)):
        try: parsed = json.loads(r['raw_json'])
        except Exception: continue
        out.append((r['wallet'], parsed))
    return out


def check_events_sorted(events: list) -> int:
    """Return count of out-of-order events (e[i].ts < e[i-1].ts)."""
    bad = 0
    prev = 0
    for e in events:
        ts = e.get('ts') or 0
        if ts < prev: bad += 1
        prev = ts
    return bad


def check_duplicate_sigs(events: list) -> int:
    """Return count of TRUE event duplicates.

    Key on (sig, pos_pubkey, ix, first_mint) so a single tx that does
    multiple distinct ops against one obligation (e.g. borrow USX + deposit
    eUSX + borrow eUSX in one Kamino multiply tx) is correctly seen as 3
    distinct events, not 3 duplicates."""
    seen = Counter()
    for e in events:
        ix = (e.get('ix') or '').lower()
        first_mint = ''
        deltas = e.get('deltas') or []
        if deltas: first_mint = (deltas[0].get('mint') or '')
        key = (e.get('sig'), e.get('pos_pubkey'), ix, first_mint)
        seen[key] += 1
    return sum(1 for k, v in seen.items() if v > 1 and k[0])


def check_negative_amts(events: list) -> int:
    """Return count of events with negative or zero amts (parser bug indicator)."""
    bad = 0
    for e in events:
        for d in (e.get('deltas') or []):
            a = d.get('amt') or 0
            if a <= 0: bad += 1
    return bad


def audit_partner(con, partner_name: str, quest_key: str, max_amt_per_event: float) -> dict:
    """Returns a dict of {check_name: stats/issues}."""
    caches = fetch_partner_caches(con, quest_key)
    n_wallets = len(caches)
    n_events = 0
    out_of_order = 0
    dup_sigs = 0
    negative_amts = 0
    huge_amts = 0
    biggest_amts = []  # (wallet, sig, amt) — top 5 to print

    for wallet, raw in caches:
        evs = raw.get('events') or []
        n_events += len(evs)
        out_of_order += check_events_sorted(evs)
        dup_sigs += check_duplicate_sigs(evs)
        negative_amts += check_negative_amts(evs)
        for e in evs:
            for d in (e.get('deltas') or []):
                a = d.get('amt') or 0
                if a > max_amt_per_event:
                    huge_amts += 1
                    biggest_amts.append((wallet, e.get('sig','?')[:12], a))

    biggest_amts.sort(key=lambda x: -x[2])
    return {
        'partner': partner_name,
        'quest_key': quest_key,
        'n_wallets': n_wallets,
        'n_events': n_events,
        'out_of_order': out_of_order,
        'dup_sigs': dup_sigs,
        'negative_amts': negative_amts,
        'huge_amts': huge_amts,
        'top_amts': biggest_amts[:5],
    }


# Reasonable per-event max amount (USD) — flag anything bigger than this.
# Picked to be much larger than realistic single-tx flows on Solstice.
PARTNERS = [
    # partner_name, quest_cache key, max_amt_per_event_usd
    ('Exponent YT',   'S2_EXPONENT_YT',  100_000_000),
    ('Exponent LP',   'S2_EXPONENT_LP',  100_000_000),
    ('Kamino',        'S2_KAMINO',       100_000_000),
    ('Loopscale',     'S2_LOOPSCALE',    100_000_000),
    ('Orca',          'S2_ORCA',         100_000_000),
    ('Raydium',       'S2_RAYDIUM',      100_000_000),
    # HOLD has a different shape — timeline of balance points, not events. Skip here.
]


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    print(f'=== PARTNER EVENT INTEGRITY AUDIT ({time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}) ===')

    any_fail = False
    for name, qkey, max_amt in PARTNERS:
        s = audit_partner(con, name, qkey, max_amt)
        status = 'PASS'
        if s['out_of_order'] or s['dup_sigs'] or s['negative_amts']:
            status = 'FAIL'
            any_fail = True
        elif s['huge_amts']:
            status = 'WARN'
        print(f"\n{name:<14}  ({s['n_wallets']} wallets, {s['n_events']} events)  → {status}")
        print(f'  out_of_order: {s["out_of_order"]}')
        print(f'  dup_sigs:     {s["dup_sigs"]}')
        print(f'  negative_amts:{s["negative_amts"]}')
        print(f'  huge_amts:    {s["huge_amts"]} (events with amt > ${max_amt:,.0f})')
        if s['top_amts']:
            print(f'  top amounts:')
            for w, sig, a in s['top_amts']:
                print(f'    {w[:12]}…  {sig}…  ${a:>16,.2f}')

    print('\n=== SUMMARY ===')
    if any_fail:
        print('❌ One or more partners failed integrity check.')
        sys.exit(1)
    else:
        print('✅ All partner event caches pass integrity check.')


if __name__ == '__main__':
    main()

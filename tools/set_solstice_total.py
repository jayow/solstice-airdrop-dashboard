"""Record today's Solstice published grand-flares total into flares_snapshots.

Solstice exposes the grand-flares total via
`app.solstice.finance/api/rewards/global/analytics` → flare.totalFlare. This
tool fetches that value and writes it into flares_snapshots so refresh.sh and
audit.py can read a single source of truth.

Usage:
    python3 tools/set_solstice_total.py                # auto-fetch from API
    python3 tools/set_solstice_total.py <total>        # override manually
    python3 tools/set_solstice_total.py --date 2026-05-18 <total>

Auto-fetch is the default; pass an explicit `total` only when the API is
unreachable or you want to override.
"""
import os, sys, sqlite3, argparse, json
import datetime as dt
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'solstice.db')
API_URL = 'https://app.solstice.finance/api/rewards/global/analytics'


def fetch_from_api(timeout: int = 10) -> tuple[float, int]:
    """Returns (flare.totalFlare, users.totalUsers) from Solstice's analytics."""
    r = requests.get(API_URL, timeout=timeout,
                     headers={'User-Agent': 'solstice-flares-dashboard/1.0'})
    r.raise_for_status()
    j = r.json()
    return float(j['flare']['totalFlare']), int(j.get('users', {}).get('totalUsers', 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('total', type=float, nargs='?', default=None,
                    help="Solstice's grand_flares number (omit to auto-fetch from API)")
    ap.add_argument('--date', default=None, help='YYYY-MM-DD (UTC), defaults to today')
    args = ap.parse_args()

    total_users = 0
    if args.total is None:
        try:
            args.total, total_users = fetch_from_api()
            print(f'  Auto-fetched from {API_URL}')
        except Exception as e:
            print(f'  ERROR fetching from API: {e}', file=sys.stderr)
            print(f'  Pass total manually: python3 tools/set_solstice_total.py <total>', file=sys.stderr)
            sys.exit(1)

    # Convention: one canonical row per (date_utc, source='solstice_dashboard'),
    # ts = midnight UTC of date_utc. The partial UNIQUE INDEX
    # uniq_snapshots_solstice_date(date_utc) WHERE source='solstice_dashboard'
    # enforces this structurally; ON CONFLICT below is the upsert path.
    date_utc = args.date or dt.datetime.now(dt.UTC).strftime('%Y-%m-%d')
    midnight_ts = int(dt.datetime.strptime(date_utc, '%Y-%m-%d').replace(tzinfo=dt.UTC).timestamp())

    con = sqlite3.connect(DB)
    # Monotonicity guard: Solstice's published total only grows; if a stale
    # rerun supplies a smaller value, preserve the existing truth.
    con.execute(
        'INSERT INTO flares_snapshots '
        '(ts, date_utc, source, universe_size, grand_total, quest_totals_json) '
        'VALUES (?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(date_utc) WHERE source=\'solstice_dashboard\' DO UPDATE SET '
        '  ts = excluded.ts, '
        '  universe_size = excluded.universe_size, '
        '  grand_total = excluded.grand_total, '
        '  quest_totals_json = excluded.quest_totals_json '
        'WHERE excluded.grand_total >= flares_snapshots.grand_total',
        (midnight_ts, date_utc, 'solstice_dashboard', total_users, args.total, '{}'))
    con.commit()
    stored = con.execute(
        "SELECT grand_total, universe_size FROM flares_snapshots "
        "WHERE date_utc=? AND source='solstice_dashboard'",
        (date_utc,)).fetchone()
    con.close()
    if stored and abs(stored[0] - args.total) > 0.01:
        print(f'NOTE: incoming grand_total ({args.total:,.2f}) <= stored ({stored[0]:,.2f}) — '
              f'monotonicity guard preserved stored value for {date_utc}')
    else:
        print(f'Recorded Solstice grand_total = {args.total:,.2f}  totalUsers = {total_users:,} for {date_utc} (ts={midnight_ts})')


if __name__ == '__main__':
    main()

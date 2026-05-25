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

    date_utc = args.date or dt.datetime.now(dt.UTC).strftime('%Y-%m-%d')
    midnight_ts = int(dt.datetime.strptime(date_utc, '%Y-%m-%d').replace(tzinfo=dt.UTC).timestamp())

    con = sqlite3.connect(DB)
    con.execute(
        'INSERT OR REPLACE INTO flares_snapshots '
        '(ts, date_utc, source, universe_size, grand_total, quest_totals_json) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (midnight_ts, date_utc, 'solstice_dashboard', total_users, args.total, '{}'))
    con.commit()
    con.close()
    print(f'Recorded Solstice grand_total = {args.total:,.2f}  totalUsers = {total_users:,} for {date_utc} (ts={midnight_ts})')


if __name__ == '__main__':
    main()

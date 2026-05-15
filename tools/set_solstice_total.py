"""Record today's Solstice published grand-flares total into flares_snapshots.

Solstice doesn't expose grand_flares via API — it's a number we observe from
their public dashboard and feed in manually each day. This tool puts it in
the DB so refresh.sh + audit.py can read a single source of truth.

Usage:
    python3 tools/set_solstice_total.py <total> [--date YYYY-MM-DD]

If --date is omitted, uses today UTC. Re-running for the same date updates
the value (idempotent).
"""
import os, sys, sqlite3, time, argparse, datetime as dt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'solstice.db')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('total', type=float, help="Solstice's grand_flares number")
    ap.add_argument('--date', default=None, help='YYYY-MM-DD (UTC), defaults to today')
    args = ap.parse_args()

    date_utc = args.date or dt.datetime.now(dt.UTC).strftime('%Y-%m-%d')
    midnight_ts = int(dt.datetime.strptime(date_utc, '%Y-%m-%d').replace(tzinfo=dt.UTC).timestamp())

    con = sqlite3.connect(DB)
    con.execute(
        'INSERT OR REPLACE INTO flares_snapshots '
        '(ts, date_utc, source, universe_size, grand_total, quest_totals_json) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (midnight_ts, date_utc, 'solstice_dashboard', 0, args.total, '{}'))
    con.commit()
    con.close()
    print(f'Recorded Solstice grand_total = {args.total:,.0f} for {date_utc} (ts={midnight_ts})')


if __name__ == '__main__':
    main()

"""Post-walk CONFIDENCE GATE — the daily "do not push if this trips" check.

Runs after the walkers + transforms, before the dashboard is trusted. It catches
the recurring failure classes that have shipped bad numbers before, so a daily
refresh can repeat without silently publishing garbage.

Checks (all read from data/solstice.db — ELT-SQLite-first):

  1. PER-QUEST OUTLIER (the CLMM / LP / Kamino blow-up signature).
     The decode bugs produce 1e9-1e13 flares for 1-3 random wallets while the
     rest of that quest sits at normal values. A LEGIT whale, by contrast, lives
     in a smooth distribution (e.g. HOLD: 18B, 7.7B, 5.6B … ~2x gaps). So we
     baseline each quest against its RANK-5 wallet (below any 1-3-wallet blow-up
     cluster, above the smooth tail) and flag any wallet > OUTLIER_RATIO x that
     baseline. This flags blow-ups WITHOUT false-flagging legitimate large holders.

  2. DAY-OVER-DAY SWING per quest. Compares each quest's total to the last
     recorded day and flags a >SWING_UP x explosion (e.g. HOLD_USX_DAILY 20M->150B)
     or a collapse below SWING_DOWN (a walker that silently emptied a quest), and
     quests that VANISHED entirely.

Exit code 0 = clean (safe to push). Non-zero = >=1 FLAG (DO NOT PUSH).
Records today's per-quest totals into quest_total_history for tomorrow's compare.

Usage:
    python3 tools/confidence_gate.py            # gate (exit 1 on flags)
    python3 tools/confidence_gate.py --record-only   # just snapshot, never fail
"""
import os, sys, sqlite3, argparse
import datetime as dt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'solstice.db')

OUTLIER_RATIO = 50.0     # a wallet > 50x the quest's rank-5 baseline = suspected blow-up
OUTLIER_BASELINE_RANK = 5  # 0-indexed rank-4; tolerates a 1-4 wallet blow-up cluster
SWING_UP = 3.0           # quest total grew > 3x day-over-day
SWING_DOWN = 0.5         # quest total fell below 50% day-over-day
MIN_QUEST_TOTAL = 1e6    # ignore tiny quests for swing (noise floor)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--record-only', action='store_true',
                    help='snapshot today and exit 0 without failing on flags')
    ap.add_argument('--allow', nargs='+', metavar=('WALLET', 'QUEST'),
                    help='mark (WALLET QUEST ["note"]) as reviewed-legit so it stops flagging; QUEST="*" for all')
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.execute('PRAGMA busy_timeout=30000')
    con.execute(
        'CREATE TABLE IF NOT EXISTS quest_total_history ('
        '  date_utc TEXT, quest TEXT, total REAL, n_wallets INTEGER, ts INTEGER, '
        '  PRIMARY KEY (date_utc, quest))')
    # Reviewed-legit (wallet, quest) pairs — confirmed real (e.g. a genuine whale
    # that isn't a PDA) so the gate stops re-flagging them daily. quest='*' allows
    # the wallet across every quest. Classified PDAs are already auto-excluded.
    con.execute(
        'CREATE TABLE IF NOT EXISTS confidence_gate_allow ('
        '  wallet TEXT, quest TEXT, note TEXT, ts INTEGER, PRIMARY KEY (wallet, quest))')
    # Make the per-quest top-N lookups instant on the 511k-row table.
    con.execute('CREATE INDEX IF NOT EXISTS idx_wq_quest_flares ON wallet_quests(quest, flares)')
    con.commit()

    if args.allow:
        w = args.allow[0]
        q = args.allow[1] if len(args.allow) > 1 else '*'
        note = ' '.join(args.allow[2:]) if len(args.allow) > 2 else ''
        import time as _t
        con.execute('INSERT OR REPLACE INTO confidence_gate_allow (wallet, quest, note, ts) '
                    'VALUES (?,?,?,?)', (w, q, note, int(_t.time())))
        con.commit()
        print(f'allowlisted {w} for quest {q}  ({note or "no note"})')
        sys.exit(0)

    allow = set()       # (wallet, quest) pairs
    allow_all = set()   # wallets allowed across every quest
    for w, q in con.execute('SELECT wallet, quest FROM confidence_gate_allow'):
        (allow_all.add(w) if q == '*' else allow.add((w, q)))

    today = dt.datetime.now(dt.UTC).strftime('%Y-%m-%d')
    now_ts = int(dt.datetime.now(dt.UTC).timestamp())
    flags = []

    # The gate judges what actually SHIPS — the real-user leaderboard — so it
    # excludes wallets already classified PDA/institution (those are handled by
    # the separate Layer-2 classification, never shown as real users). An
    # unclassified large outlier still flags: it's either a decode bug to fix or
    # an institution to classify — both require review before push.
    PDA_FILTER = "COALESCE(w.classification,'') NOT IN ('pda_protocol','institution')"

    # Today's per-quest totals (real users only).
    totals = {q: (t, n) for q, t, n in con.execute(
        'SELECT wq.quest, SUM(wq.flares), COUNT(*) FROM wallet_quests wq '
        'LEFT JOIN wallets w ON w.wallet = wq.wallet '
        f'WHERE wq.flares > 0 AND {PDA_FILTER} GROUP BY wq.quest')}

    # --- Check 1: per-quest outlier (rank-5 baseline) ---
    print('=== Check 1: per-quest outlier (top wallet vs rank-5 baseline) ===')
    for q in sorted(totals):
        top = con.execute(
            'SELECT wq.wallet, wq.flares FROM wallet_quests wq '
            'LEFT JOIN wallets w ON w.wallet = wq.wallet '
            f'WHERE wq.quest=? AND wq.flares>0 AND {PDA_FILTER} '
            'ORDER BY wq.flares DESC LIMIT 20', (q,)).fetchall()
        if len(top) < OUTLIER_BASELINE_RANK + 1:
            continue  # too few wallets to judge an outlier cluster
        baseline = top[OUTLIER_BASELINE_RANK][1]
        if baseline <= 0:
            continue
        for w, f in top:
            if (w, q) in allow or w in allow_all:
                continue   # reviewed-legit — don't re-flag
            if f > OUTLIER_RATIO * baseline:
                flags.append(f'OUTLIER  {q}: {w[:10]}… = {f:,.0f} is {f/baseline:.0f}x the rank-{OUTLIER_BASELINE_RANK+1} '
                             f'baseline ({baseline:,.0f}) — suspected decode/blow-up')

    # --- Check 2: day-over-day swing ---
    print('=== Check 2: day-over-day quest swing ===')
    prior_date = con.execute(
        'SELECT MAX(date_utc) FROM quest_total_history WHERE date_utc < ?', (today,)).fetchone()[0]
    if prior_date:
        prior = {q: t for q, t in con.execute(
            'SELECT quest, total FROM quest_total_history WHERE date_utc=?', (prior_date,))}
        for q, (t, n) in totals.items():
            pt = prior.get(q)
            if not pt or pt < MIN_QUEST_TOTAL or t < MIN_QUEST_TOTAL:
                continue
            if t > SWING_UP * pt:
                flags.append(f'SWING-UP  {q}: {pt:,.0f} -> {t:,.0f} ({t/pt:.1f}x) vs {prior_date}')
            elif t < SWING_DOWN * pt:
                flags.append(f'SWING-DOWN {q}: {pt:,.0f} -> {t:,.0f} ({t/pt:.2f}x) vs {prior_date}')
        for q, pt in prior.items():
            if pt >= MIN_QUEST_TOTAL and q not in totals:
                flags.append(f'VANISHED  {q}: was {pt:,.0f} on {prior_date}, now absent')
        print(f'  compared {len(totals)} quests vs {prior_date}')
    else:
        print('  (no prior history — recording first baseline, no swing check)')

    # Record today's snapshot for tomorrow.
    con.executemany(
        'INSERT OR REPLACE INTO quest_total_history (date_utc, quest, total, n_wallets, ts) '
        'VALUES (?,?,?,?,?)',
        [(today, q, t, n, now_ts) for q, (t, n) in totals.items()])
    con.commit()
    con.close()

    # Verdict.
    print()
    if flags:
        print(f'❌ CONFIDENCE GATE: {len(flags)} FLAG(S) — DO NOT PUSH')
        for fl in flags:
            print(f'   • {fl}')
        if args.record_only:
            print('   (--record-only: snapshot saved, exiting 0 anyway)')
            sys.exit(0)
        sys.exit(1)
    print(f'✓ CONFIDENCE GATE CLEAN — {len(totals)} quests, no outliers or anomalous swings')
    sys.exit(0)


if __name__ == '__main__':
    main()

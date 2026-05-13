"""DB-backed dashboard builder. Reads from data/solstice.db, writes server/data.json.

Replaces the old build_data.py file-based path. Source of truth is now the DB.

Output schema (data.json) matches the existing dashboard payload — no frontend
changes required.
"""
import os, sys, json, csv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
OUT  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.json')

sys.path.insert(0, os.path.join(ROOT, 'src', 'flares_estimator'))
import db
from quest_map import QUESTS

QUEST_PROTO = {q['code']: q['protocol'] for q in QUESTS}
QUEST_CODES = [q['code'] for q in QUESTS if not q.get('disabled')]
PROTOCOLS = ['solstice', 'yield_vault', 'exponent', 'kamino', 'whirlpool', 'raydium', 'loopscale']


def main():
    db.init()
    c = db.conn()

    # 1. Wallet metadata
    print('Loading wallet metadata from DB...')
    wallets_meta = {r['wallet']: dict(r) for r in c.execute('SELECT * FROM wallets')}
    print(f'  {len(wallets_meta):,} wallets')

    # 2. wallet_quests (current flares) — pivot into per-wallet dicts
    print('Loading per-quest flares from DB...')
    quest_data = {}  # wallet -> {quest: flares}
    for r in c.execute('SELECT wallet, quest, flares FROM wallet_quests WHERE flares > 0'):
        quest_data.setdefault(r['wallet'], {})[r['quest']] = r['flares']
    print(f'  {len(quest_data):,} wallets with positive flares')

    # 3. Optional: legacy flares_stage3_filtered.csv for current_tvl_usd / tier (minor metadata, not on DB yet)
    legacy_tvl_tier = {}
    p = os.path.join(DATA, 'flares_stage3_filtered.csv')
    if os.path.exists(p):
        with open(p) as fh:
            for r in csv.DictReader(fh):
                legacy_tvl_tier[r['wallet']] = {
                    'current_tvl_usd': round(float(r.get('current_tvl_usd') or 0), 2),
                    'tier': r.get('tier') or '',
                }

    # Load manual protocol-PDA override list and mirror into DB classification
    manual_pda_labels = {}
    p_pdas = os.path.join(DATA, 'protocol_pdas.json')
    if os.path.exists(p_pdas):
        d = json.load(open(p_pdas))
        manual_pda_labels = d.get('addresses') or {}
        for addr in manual_pda_labels:
            if wallets_meta.get(addr, {}).get('classification') != 'pda_protocol':
                c.execute('INSERT INTO wallets(wallet, classification) VALUES (?, ?) '
                          'ON CONFLICT(wallet) DO UPDATE SET classification=excluded.classification',
                          (addr, 'pda_protocol'))
                wallets_meta.setdefault(addr, {})['classification'] = 'pda_protocol'

    # 4. Build records (uninit PDAs are excluded — see filter_pdas_db.py).
    # Protocol PDAs (auto- or manually-labelled) are kept but tagged so the
    # frontend can show a warning chip + banner.
    records = []
    n_pda_skipped = 0
    for wallet, flares in quest_data.items():
        m_meta = wallets_meta.get(wallet) or {}
        cls = (m_meta.get('classification') or '').lower()
        if cls in ('pda', 'pda_or_uninit'):
            n_pda_skipped += 1
            continue
        # Fill missing quests with 0 for stable schema
        per_quest_cols = {q: round(flares.get(q, 0.0), 2) for q in QUEST_CODES}
        # Protocol aggregates
        proto_cols = {p: 0.0 for p in PROTOCOLS}
        for q, v in per_quest_cols.items():
            p = QUEST_PROTO.get(q)
            if p and p in proto_cols: proto_cols[p] += v
        proto_cols = {p: round(v, 2) for p, v in proto_cols.items()}
        total = round(sum(proto_cols.values()), 2)
        if total <= 0: continue

        m = wallets_meta.get(wallet) or {}
        tvl_tier = legacy_tvl_tier.get(wallet, {})
        records.append({
            'wallet': wallet,
            'total': total,
            **proto_cols,
            'by_quest': per_quest_cols,
            'current_tvl_usd': tvl_tier.get('current_tvl_usd', 0),
            'tier': tvl_tier.get('tier', ''),
            'is_s1': bool(m.get('is_s1')),
            'cohort': m.get('cohort') or '',
            'class': m.get('classification') or 'unclassified',
            'reason': '',
            'in_partner_footprint': bool(m.get('in_partner_footprint')),
            'in_exponent': bool(m.get('in_exponent')),
            'n_protocols_with_flares': m.get('n_protocols') or 0,
            'is_protocol_pda': (cls == 'pda_protocol') or (wallet in manual_pda_labels),
            'pda_source': 'manual' if wallet in manual_pda_labels else ('auto' if cls == 'pda_protocol' else None),
            'pda_label':  manual_pda_labels.get(wallet, {}).get('label') if wallet in manual_pda_labels else None,
        })

    # 5. Sort + ranks
    records.sort(key=lambda x: -x['total'])
    for i, r in enumerate(records): r['rank_all'] = i + 1
    s1_only = [r for r in records if r['is_s1']]
    for i, r in enumerate(s1_only): r['rank_s1'] = i + 1
    real_only = [r for r in records if r.get('class') in ('real_user', 'passive_user', 'institution')]
    real_only.sort(key=lambda x: -x['total'])
    for i, r in enumerate(real_only): r['rank_real'] = i + 1

    # 6. Per-quest + per-partner totals. Two views:
    #   `quest_totals`: sum across ALL records (incl. tagged PDAs that the
    #                   frontend toggle can show)
    #   `partner_totals_real`: sum across real-user records only (excludes PDAs)
    qt = {q: round(sum(r['by_quest'].get(q, 0) for r in records), 2) for q in QUEST_CODES}
    grand_total = round(sum(qt.values()), 2)
    partner_totals = {p: round(sum(r.get(p, 0) for r in records), 2) for p in PROTOCOLS}
    partner_totals_real = {p: round(sum(r.get(p, 0) for r in records if not r.get('is_protocol_pda')), 2) for p in PROTOCOLS}

    # 7. Snapshot: append a row to flares_snapshots (DB) with inflation delta if prior exists
    s1_count = sum(1 for r in records if r.get('is_s1'))
    payload = {
        'generated_at_utc': __import__('datetime').datetime.now(__import__('datetime').UTC).isoformat(),
        'quest_codes': QUEST_CODES,
        'quest_totals': qt,
        'partners': PROTOCOLS,
        'partner_totals': partner_totals,            # all records (incl tagged PDAs)
        'partner_totals_real': partner_totals_real,  # real users only — matches dashboard headline
        'totals': {
            'all_earners': len(records),
            's1_registered_earners': len(s1_only),
            's1_registered_total': sum(1 for w in wallets_meta.values() if w.get('is_s1')),
            'new_in_s2_earners': sum(1 for r in records if not r['is_s1']),
            'classified': sum(1 for r in records if r.get('class') and r['class'] != 'unclassified'),
            'real_users': sum(1 for r in records if r.get('class') == 'real_user'),
            'passive_users': sum(1 for r in records if r.get('class') == 'passive_user'),
            'real_or_passive': len(real_only),
            'institutions': sum(1 for r in records if r.get('class') == 'institution'),
        },
        'records': records,
    }

    with open(OUT, 'w') as fh: json.dump(payload, fh)
    print(f'Wrote {OUT}: {len(records):,} records, {os.path.getsize(OUT)/1024/1024:.1f}MB  (PDA-filtered: {n_pda_skipped})')

    print('\nPer-quest totals:')
    for q in QUEST_CODES:
        if qt[q] > 0: print(f'  {q:<38s} {qt[q]:>20,.0f}')
    print(f'  {"GRAND TOTAL":<38s} {grand_total:>20,.0f}')

    # Capture today's eUSX peg snapshot — accumulates a per-day peg history
    # so HOLD_EUSX transforms use interpolated peg(t) instead of a constant.
    try:
        import sys as _sys
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in _sys.path: _sys.path.insert(0, _root)
        from src.flares_estimator.quests.eusx_peg import record_snapshot, invalidate_cache
        peg = record_snapshot()
        invalidate_cache()
        print(f'eUSX peg snapshot recorded: {peg:.10f}')
    except Exception as e:
        print(f'WARN eUSX peg snapshot failed: {e}')

    # Log snapshot to DB (our_framework source)
    now = __import__('datetime').datetime.now(__import__('datetime').UTC)
    prev = c.execute(
        "SELECT * FROM flares_snapshots WHERE source='our_framework' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    c.execute(
        'INSERT OR REPLACE INTO flares_snapshots(ts, date_utc, source, universe_size, grand_total, quest_totals_json) '
        'VALUES (?,?,?,?,?,?)',
        (int(now.timestamp()), now.strftime('%Y-%m-%d'), 'our_framework',
         len(records), grand_total, json.dumps(qt))
    )
    if prev:
        delta = grand_total - prev['grand_total']
        hrs = (int(now.timestamp()) - prev['ts']) / 3600
        print(f'\nSnapshot logged to DB. Inflation: {delta:+,.0f} flares over {hrs:.2f}h')
    else:
        print(f'\nFirst snapshot logged to DB.')


if __name__ == '__main__':
    main()

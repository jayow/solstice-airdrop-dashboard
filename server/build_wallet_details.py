"""Emit per-wallet breakdown JSON files into server/wallets/<addr>.json.

Each file contains:
  - meta: classification, cohort, etc. from `wallets` table
  - totals: total flares + by-quest
  - sources: which walker produced each quest value
  - evidence: decoded positions/timelines per cached quest (HOLD timelines,
    YT positions, LP/Kamino/Loopscale/Orca/Raydium snapshot positions)

Reads only from data/solstice.db — no RPC. Frontend fetches the file on
wallet-click and renders.
"""
import os, sys, json, sqlite3, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB   = os.path.join(ROOT, 'data', 'solstice.db')
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wallets')

QUESTS_ORDER = [
    'S2_HOLD_USX_DAILY', 'S2_HOLD_USX_1MO', 'S2_HOLD_USX_3MO',
    'S2_HOLD_EUSX_DAILY', 'S2_HOLD_EUSX_1MO', 'S2_HOLD_EUSX_3MO',
    'S2_EXPONENT_YIELD_USX_JUN26', 'S2_EXPONENT_YIELD_EUSX_JUN26',
    'S2_EXPONENT_LP_USX_JUN26', 'S2_EXPONENT_LP_EUSX_JUN26',
    'S2_KAMINO_LEND_USX', 'S2_KAMINO_LEND_EUSX', 'S2_KAMINO_LEND_USDG',
    'S2_KAMINO_BORROW_USX', 'S2_KAMINO_BORROW_USDG', 'S2_KAMINO_KVAULT_USDG_USX',
    'S2_LOOPSCALE_SUPPLY_USX_ONE', 'S2_LOOPSCALE_BORROW_USX',
    'S2_ORCA_USX_USDC', 'S2_ORCA_EUSX_USX', 'S2_ORCA_USX_USDG',
    'S2_RAYDIUM_USX_USDC', 'S2_RAYDIUM_EUSX_USX',
    'S2_REFERRAL_BONUS',
]


def decode_evidence(qk: str, raw: dict) -> dict:
    """Convert a quest_cache.raw_json into a frontend-friendly evidence block."""
    if qk in ('S2_HOLD_USX', 'S2_HOLD_EUSX'):
        return {
            'type': 'hold',
            'atas': raw.get('atas', []),
            'timeline': raw.get('timeline', []),
        }
    if qk == 'S2_EXPONENT_YT':
        out = {'type': 'yt', 'by_market': []}
        cost_basis = raw.get('cost_basis_by_market') or {}
        for mkt, positions in (raw.get('positions_by_market') or {}).items():
            poss = []
            for p in (positions if isinstance(positions, list) else positions.get('positions', [])):
                poss.append({
                    'pubkey': p.get('pubkey'),
                    'yt': p.get('current_yt', 0) or 0,
                    'method': p.get('method'),
                    'emit': bool(p.get('is_emitting')),
                    'timeline': p.get('timeline') or [],
                })
            entry = {'market': mkt, 'positions': poss}
            cb = cost_basis.get(mkt)
            if cb: entry['cost_basis'] = cb   # {usd_basis, usd_paid, usd_paid_decayed_at_s2, usd_recovered, n_buys, n_sells}
            out['by_market'].append(entry)
        return out
    if qk == 'S2_EXPONENT_LP':
        out = {'type': 'lp', 'positions': raw.get('positions', [])}
        if raw.get('events'): out['events'] = raw['events']
        if raw.get('cost_basis_by_quest'): out['cost_basis_by_quest'] = raw['cost_basis_by_quest']
        return out
    if qk == 'S2_KAMINO':
        # Old shape: positions dict; new shape: obligations list
        p = raw.get('positions') or {}
        return {'type': 'kamino', 'positions': p, 'obligations': raw.get('obligations', [])}
    if qk in ('S2_LOOPSCALE', 'S2_ORCA', 'S2_RAYDIUM'):
        out = {'type': qk.split('_')[1].lower(), 'positions': raw.get('positions', {})}
        if raw.get('events'): out['events'] = raw['events']
        return out
    return {'type': 'unknown', 'raw_keys': list(raw.keys())}


# Per-quest multipliers (matches quest_map.py and the transform code).
# Used to compute the wallet's CURRENT daily flare emission rate from its
# present-day positions — for the projection calculator in the drawer.
QUEST_MULT = {
    'S2_HOLD_USX_DAILY': 10,
    'S2_HOLD_EUSX_DAILY': 2,
    'S2_EXPONENT_YIELD_USX_JUN26': 30,
    'S2_EXPONENT_YIELD_EUSX_JUN26': 15,
    'S2_EXPONENT_LP_USX_JUN26': 20,
    'S2_EXPONENT_LP_EUSX_JUN26': 10,
    'S2_KAMINO_LEND_USX': 5,    'S2_KAMINO_LEND_EUSX': 1,    'S2_KAMINO_LEND_USDG': 5,
    'S2_KAMINO_BORROW_USX': 1,  'S2_KAMINO_BORROW_USDG': 1,
    'S2_KAMINO_KVAULT_USDG_USX': 10,
    'S2_LOOPSCALE_SUPPLY_USX_ONE': 5,  'S2_LOOPSCALE_BORROW_USX': 1,
    'S2_ORCA_USX_USDC': 9,   'S2_ORCA_EUSX_USX': 4,   'S2_ORCA_USX_USDG': 9,
    'S2_RAYDIUM_USX_USDC': 9,  'S2_RAYDIUM_EUSX_USX': 4,
}

EUSX_PEG = 1.156  # close enough for daily-rate calc; exact peg interpolation lives in eusx_peg.py


def compute_daily_emission(evidence: dict) -> dict:
    """Best-effort estimate of the wallet's CURRENT flare emission rate per quest
    (flares per day at present-day position sizes). Used to extrapolate forward
    in the projection calculator. Returns {quest_code: flares_per_day}."""
    rates = {}

    # HOLD: balance at end of timeline × mult × peg
    for ek, qcode, peg in [('S2_HOLD_USX', 'S2_HOLD_USX_DAILY', 1.0),
                            ('S2_HOLD_EUSX', 'S2_HOLD_EUSX_DAILY', EUSX_PEG)]:
        ev = evidence.get(ek) or {}
        tl = ev.get('timeline') or []
        if not tl: continue
        bal = tl[-1][1] if tl else 0
        if bal > 0:
            rates[qcode] = bal * peg * QUEST_MULT[qcode]

    # YT: sum yt × mult for currently-emitting positions per market
    yt = evidence.get('S2_EXPONENT_YT') or {}
    for mkt in (yt.get('by_market') or []):
        market_pk = mkt.get('market')
        mult = QUEST_MULT.get('S2_EXPONENT_YIELD_USX_JUN26' if market_pk.startswith('Bxbi')
                              else 'S2_EXPONENT_YIELD_EUSX_JUN26', 0)
        for p in mkt.get('positions') or []:
            # Trust on-chain: any non-zero YT balance earns flares.
            yt_amt = p.get('yt') or 0
            if yt_amt > 0:
                q = 'S2_EXPONENT_YIELD_USX_JUN26' if market_pk.startswith('Bxbi') else 'S2_EXPONENT_YIELD_EUSX_JUN26'
                rates[q] = rates.get(q, 0) + yt_amt * mult

    # LP: snapshot lp_value × mult (positions list has lp_value_usd per market).
    # Some legacy cache entries store LP positions as strings or partial dicts —
    # defensively skip anything that doesn't look like our expected shape.
    lp = evidence.get('S2_EXPONENT_LP') or {}
    for p in (lp.get('positions') or []):
        if not isinstance(p, dict): continue
        v_usd = p.get('lp_value_usd') or 0
        if v_usd <= 0: continue
        m_pk = p.get('market', '')
        q = 'S2_EXPONENT_LP_USX_JUN26' if m_pk.startswith('Bxbi') else 'S2_EXPONENT_LP_EUSX_JUN26'
        rates[q] = rates.get(q, 0) + v_usd * QUEST_MULT[q]

    # Kamino / Loopscale / Orca / Raydium: positions dict has USD per position-key
    pos_to_quest = {
        'kamino_supply_usx':   'S2_KAMINO_LEND_USX',
        'kamino_supply_eusx':  'S2_KAMINO_LEND_EUSX',
        'kamino_supply_usdg':  'S2_KAMINO_LEND_USDG',
        'kamino_borrow_usx':   'S2_KAMINO_BORROW_USX',
        'kamino_borrow_usdg':  'S2_KAMINO_BORROW_USDG',
        'kamino_kvault_usx_usdg': 'S2_KAMINO_KVAULT_USDG_USX',
        'loopscale_supply_usx': 'S2_LOOPSCALE_SUPPLY_USX_ONE',
        'loopscale_borrow_usx': 'S2_LOOPSCALE_BORROW_USX',
        'orca_usx_usdc': 'S2_ORCA_USX_USDC',
        'orca_eusx_usx': 'S2_ORCA_EUSX_USX',
        'orca_usx_usdg': 'S2_ORCA_USX_USDG',
        'raydium_usx_usdc': 'S2_RAYDIUM_USX_USDC',
        'raydium_eusx_usx': 'S2_RAYDIUM_EUSX_USX',
    }
    for ek in ('S2_KAMINO', 'S2_LOOPSCALE', 'S2_ORCA', 'S2_RAYDIUM'):
        ev = evidence.get(ek) or {}
        positions = ev.get('positions') or {}
        for pk, usd in positions.items():
            if not isinstance(usd, (int, float)) or usd <= 0: continue
            q = pos_to_quest.get(pk)
            if not q: continue
            rates[q] = rates.get(q, 0) + usd * QUEST_MULT[q]

    return rates


def _safe_daily_emission(evidence: dict, wallet: str) -> dict:
    try:
        return compute_daily_emission(evidence)
    except Exception as e:
        # Defensive — one weird cache shape shouldn't kill the whole batch.
        print(f'  WARN daily_emission failed for {wallet[:10]}: {e}', flush=True)
        return {}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    # Manual protocol-PDA labels (for known vault addresses that auto-detection misses)
    manual_pda_labels = {}
    p_pdas = os.path.join(ROOT, 'data', 'protocol_pdas.json')
    if os.path.exists(p_pdas):
        manual_pda_labels = (json.load(open(p_pdas)).get('addresses') or {})

    # All wallets that have any signal: wallet_quests OR quest_cache OR wallets
    print('Collecting wallet set...')
    all_wallets = set()
    for r in con.execute('SELECT DISTINCT wallet FROM wallet_quests'): all_wallets.add(r['wallet'])
    for r in con.execute('SELECT DISTINCT wallet FROM quest_cache'):   all_wallets.add(r['wallet'])
    for r in con.execute('SELECT DISTINCT wallet FROM wallets'):       all_wallets.add(r['wallet'])
    print(f'  {len(all_wallets):,} unique wallets')

    # Preload all metadata in one shot to avoid N+1
    print('Preloading metadata...')
    meta_by_w = {r['wallet']: dict(r) for r in con.execute('SELECT * FROM wallets')}
    quests_by_w = {}
    for r in con.execute('SELECT wallet, quest, flares, source, updated_at FROM wallet_quests'):
        quests_by_w.setdefault(r['wallet'], []).append(dict(r))
    cache_by_w = {}
    for r in con.execute('SELECT wallet, quest_key, raw_json, extracted_at FROM quest_cache'):
        cache_by_w.setdefault(r['wallet'], []).append(dict(r))

    print(f'  wallet_quests: {sum(len(v) for v in quests_by_w.values()):,} rows')
    print(f'  quest_cache:   {sum(len(v) for v in cache_by_w.values()):,} rows')

    print(f'\nWriting per-wallet JSON to {OUT_DIR}/...')
    t0 = time.time()
    written = 0
    for w in all_wallets:
        meta = meta_by_w.get(w, {})
        # Quest breakdown — fill in zero for quests not present
        present = {q['quest']: q for q in quests_by_w.get(w, [])}
        quest_rows = []
        total = 0.0
        for qcode in QUESTS_ORDER:
            row = present.get(qcode)
            if row:
                quest_rows.append({
                    'quest': qcode,
                    'flares': row['flares'],
                    'source': row['source'],
                    'updated_at': row['updated_at'],
                })
                total += row['flares'] or 0
            else:
                quest_rows.append({'quest': qcode, 'flares': 0, 'source': None, 'updated_at': None})

        # Evidence
        evidence = {}
        activity_events = []
        for c in cache_by_w.get(w, []):
            try:
                if c['quest_key'] == 'WALLET_ACTIVITY':
                    raw = json.loads(c['raw_json'])
                    activity_events = raw.get('events') or []
                    continue
                evidence[c['quest_key']] = {
                    'extracted_at': c['extracted_at'],
                    **decode_evidence(c['quest_key'], json.loads(c['raw_json'])),
                }
            except Exception as e:
                evidence[c['quest_key']] = {'error': str(e)}

        manual = manual_pda_labels.get(w)
        is_pda = (meta.get('classification') == 'pda_protocol') or (manual is not None)
        payload = {
            'wallet': w,
            'meta': {
                'classification': meta.get('classification'),
                'cohort': meta.get('cohort'),
                'is_s1': bool(meta.get('is_s1') or 0),
                'n_protocols': meta.get('n_protocols'),
                'first_seen_ts': meta.get('first_seen_ts'),
                'last_active_ts': meta.get('last_active_ts'),
                'is_protocol_pda': is_pda,
                'pda_source': 'manual' if manual else ('auto' if meta.get('classification') == 'pda_protocol' else None),
                'pda_label':  manual.get('label') if manual else None,
                'pda_protocol_hint': manual.get('protocol') if manual else None,
            },
            'total_flares': total,
            'by_quest': quest_rows,
            'evidence': evidence,
            'activity': activity_events,
            # Daily emission rate per quest at CURRENT position sizes — used
            # by the drawer's projection calculator to extrapolate forward.
            'daily_emission_by_quest': _safe_daily_emission(evidence, w),
        }
        with open(os.path.join(OUT_DIR, f'{w}.json'), 'w') as f:
            json.dump(payload, f, separators=(',', ':'))
        written += 1
        if written % 2000 == 0: print(f'  {written:,}/{len(all_wallets):,}  ({time.time()-t0:.1f}s)')

    print(f'\nDone. {written:,} files in {time.time()-t0:.1f}s')


if __name__ == '__main__':
    main()

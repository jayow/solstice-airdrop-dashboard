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
    'S2_EXPONENT_YIELD_USX_SEP26', 'S2_EXPONENT_YIELD_EUSX_SEP26',
    'S2_EXPONENT_LP_USX_JUN26', 'S2_EXPONENT_LP_EUSX_JUN26',
    'S2_EXPONENT_LP_USX_SEP26', 'S2_EXPONENT_LP_EUSX_SEP26',
    'S2_KAMINO_LEND_USX', 'S2_KAMINO_LEND_EUSX', 'S2_KAMINO_LEND_USDG',
    'S2_KAMINO_BORROW_USX', 'S2_KAMINO_BORROW_USDG', 'S2_KAMINO_KVAULT_USDG_USX',
    'S2_LOOPSCALE_SUPPLY_USX_ONE', 'S2_LOOPSCALE_SUPPLY_USX_RWA', 'S2_LOOPSCALE_BORROW_USX',
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
        out = {'type': 'kamino', 'positions': p, 'obligations': raw.get('obligations', [])}
        if raw.get('cost_basis_by_quest'): out['cost_basis_by_quest'] = raw['cost_basis_by_quest']
        return out
    if qk in ('S2_LOOPSCALE', 'S2_ORCA', 'S2_RAYDIUM'):
        out = {'type': qk.split('_')[1].lower(), 'positions': raw.get('positions', {})}
        if raw.get('events'): out['events'] = raw['events']
        if raw.get('cost_basis_by_quest'): out['cost_basis_by_quest'] = raw['cost_basis_by_quest']
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
    'S2_EXPONENT_YIELD_USX_SEP26': 45,
    'S2_EXPONENT_YIELD_EUSX_SEP26': 22.5,
    'S2_EXPONENT_LP_USX_JUN26': 20,
    'S2_EXPONENT_LP_EUSX_JUN26': 10,
    'S2_EXPONENT_LP_USX_SEP26': 30,
    'S2_EXPONENT_LP_EUSX_SEP26': 15,
    'S2_KAMINO_LEND_USX': 5,    'S2_KAMINO_LEND_EUSX': 1,    'S2_KAMINO_LEND_USDG': 5,
    'S2_KAMINO_BORROW_USX': 1,  'S2_KAMINO_BORROW_USDG': 1,
    'S2_KAMINO_KVAULT_USDG_USX': 10,
    'S2_LOOPSCALE_SUPPLY_USX_ONE': 5,  'S2_LOOPSCALE_SUPPLY_USX_RWA': 5,  'S2_LOOPSCALE_BORROW_USX': 1,
    'S2_ORCA_USX_USDC': 9,   'S2_ORCA_EUSX_USX': 4,   'S2_ORCA_USX_USDG': 9,
    'S2_RAYDIUM_USX_USDC': 9,  'S2_RAYDIUM_EUSX_USX': 4,
}

EUSX_PEG = 1.0319  # eUSX/USD per Solstice eusxPrice & Exponent syExchangeRate (May 2026).
                   # We previously read 1.156 from an on-chain PDA field that's actually a
                   # different vault ratio, not the USD peg. Per-second history lives in
                   # eusx_peg.peg_at() (rebuilt from Solstice API).


def compute_daily_emission(evidence: dict) -> dict:
    """Best-effort estimate of the wallet's CURRENT flare emission rate per quest
    (flares per day at present-day position sizes). Used to extrapolate forward
    in the projection calculator. Returns {quest_code: flares_per_day}."""
    rates = {}

    # HOLD: balance at end of timeline × mult × peg.
    # Also emit bonus-tier daily rates if the wallet has qualified (held
    # ≥$100 continuously for the threshold period). Bonus tiers (1MO=30d, 3MO=90d)
    # use the same balance × peg base, with their respective multipliers.
    HOLD_BONUSES = {
        'S2_HOLD_USX':  [('S2_HOLD_USX_DAILY', 10, 0), ('S2_HOLD_USX_1MO', 6, 30), ('S2_HOLD_USX_3MO', 15, 90)],
        'S2_HOLD_EUSX': [('S2_HOLD_EUSX_DAILY', 2, 0), ('S2_HOLD_EUSX_1MO', 4, 30), ('S2_HOLD_EUSX_3MO', 10, 90)],
    }
    for ek, tiers in HOLD_BONUSES.items():
        ev = evidence.get(ek) or {}
        tl = ev.get('timeline') or []
        if not tl: continue
        peg = EUSX_PEG if ek == 'S2_HOLD_EUSX' else 1.0
        bal = tl[-1][1] if tl else 0
        if bal <= 0: continue
        now_ts = int(__import__('time').time())
        # Walk backward through timeline to find longest continuous run of bal ≥ $100
        MIN_BAL = 100.0
        run_start = None
        for ts, b in tl:
            if (b * peg) >= MIN_BAL:
                if run_start is None: run_start = ts
            else:
                run_start = None
        # Current run duration (sec). bal at end is already > 0, so if run_start set
        # the wallet is currently in a qualifying run.
        run_secs = (now_ts - run_start) if run_start else 0
        for qcode, mult, qual_days in tiers:
            if qual_days == 0 or run_secs >= qual_days * 86400:
                rates[qcode] = bal * peg * mult

    # YT: sum yt × mult for currently-emitting positions per market.
    # Market PDA → (YIELD quest code, mult) — covers V1 (Jun26) + V2 (Sep26).
    YT_MARKET_TO_QUEST = {
        'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm': 'S2_EXPONENT_YIELD_USX_JUN26',
        'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP': 'S2_EXPONENT_YIELD_EUSX_JUN26',
        '2pZuAPFRJLbT57qJ1ebs8B2ExWwHywyaHUC6Y515BaMm': 'S2_EXPONENT_YIELD_USX_SEP26',
        'EsVGeJ99ADQGwGWLiBEg93xBtmuMjyC4P5zG9bpVMJWf': 'S2_EXPONENT_YIELD_EUSX_SEP26',
    }
    yt = evidence.get('S2_EXPONENT_YT') or {}
    for mkt in (yt.get('by_market') or []):
        market_pk = mkt.get('market')
        q = YT_MARKET_TO_QUEST.get(market_pk)
        if not q: continue
        mult = QUEST_MULT.get(q, 0)
        for p in mkt.get('positions') or []:
            # Trust on-chain: any non-zero YT balance earns flares.
            yt_amt = p.get('yt') or 0
            if yt_amt > 0:
                rates[q] = rates.get(q, 0) + yt_amt * mult

    # LP: snapshot lp_value × mult. The walker writes positions as a flat dict
    # with keys usx_jun26_lp_usd / eusx_jun26_lp_usd (see walk_s2_lp.py:487).
    # Legacy entries used a list-of-dicts shape — handle both for safety.
    LP_MARKET_TO_QUEST = {
        'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm': 'S2_EXPONENT_LP_USX_JUN26',
        'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP': 'S2_EXPONENT_LP_EUSX_JUN26',
        '2pZuAPFRJLbT57qJ1ebs8B2ExWwHywyaHUC6Y515BaMm': 'S2_EXPONENT_LP_USX_SEP26',
        'EsVGeJ99ADQGwGWLiBEg93xBtmuMjyC4P5zG9bpVMJWf': 'S2_EXPONENT_LP_EUSX_SEP26',
    }
    lp = evidence.get('S2_EXPONENT_LP') or {}
    positions = lp.get('positions')
    if isinstance(positions, dict):
        for pos_key, qcode in [
            ('usx_jun26_lp_usd',  'S2_EXPONENT_LP_USX_JUN26'),
            ('eusx_jun26_lp_usd', 'S2_EXPONENT_LP_EUSX_JUN26'),
            ('usx_sep26_lp_usd',  'S2_EXPONENT_LP_USX_SEP26'),
            ('eusx_sep26_lp_usd', 'S2_EXPONENT_LP_EUSX_SEP26'),
        ]:
            v = positions.get(pos_key) or 0
            if v > 0:
                rates[qcode] = rates.get(qcode, 0) + v * QUEST_MULT[qcode]
    elif isinstance(positions, list):
        for p in positions:
            if not isinstance(p, dict): continue
            v_usd = p.get('lp_value_usd') or 0
            if v_usd <= 0: continue
            m_pk = p.get('market', '')
            q = LP_MARKET_TO_QUEST.get(m_pk)
            if not q: continue
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


def compute_tvl_by_quest(evidence: dict) -> dict:
    """Derive current per-quest TVL (USD) for the dashboard.

    For HOLD / LP / Kamino / Loopscale / Orca / Raydium quests: daily emission
    is already TVL_usd × multiplier (the walker writes USD-denominated
    positions), so TVL_usd = rate / mult holds.

    For Exponent YT quests this inversion is WRONG — the walker tracks YT
    token count (not USD) and computes daily emission as yt × mult. Each YT
    represents a fractional claim on remaining yield, not $1 of underlying,
    so yt count != USD. Per Solstice's docs: "Exponent YT TVL is tracked at
    the amount you originally deposited, not the current market value." We
    use cost_basis.usd_basis (per-market) — the same amount-deposited figure
    the YT walker computes for cost-basis math.

    Bonus tiers (_1MO, _3MO) are excluded — one-shot qualification bonuses,
    not daily TVL-proportional.

    Returns {quest_code: usd_amount}.
    """
    YT_MARKET_TO_QUEST = {
        'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm': 'S2_EXPONENT_YIELD_USX_JUN26',
        'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP': 'S2_EXPONENT_YIELD_EUSX_JUN26',
        '2pZuAPFRJLbT57qJ1ebs8B2ExWwHywyaHUC6Y515BaMm': 'S2_EXPONENT_YIELD_USX_SEP26',
        'EsVGeJ99ADQGwGWLiBEg93xBtmuMjyC4P5zG9bpVMJWf': 'S2_EXPONENT_YIELD_EUSX_SEP26',
    }
    rates = compute_daily_emission(evidence)
    tvl = {}
    for qcode, rate in rates.items():
        # Skip bonus tiers — they're not daily TVL-proportional.
        if qcode.endswith('_1MO') or qcode.endswith('_3MO'):
            continue
        # Skip YT — handled separately below using cost basis (the correct
        # amount-deposited measure).
        if qcode.startswith('S2_EXPONENT_YIELD_'):
            continue
        mult = QUEST_MULT.get(qcode)
        if not mult or mult <= 0: continue
        tvl[qcode] = rate / mult

    # YT TVL = cost_basis.usd_basis (amount originally deposited, decay-adjusted
    # for pre-S2 buys). Only credit quests where the wallet currently holds YT.
    #
    # Exception: S2_EXPONENT_YIELD_USX_JUN26 uses usd_paid (original funding
    # amount) instead of usd_basis (s1_contribution + s2_contribution). The
    # JUN26 market is a pre-S2 vintage; the usd_basis adjustment underreports
    # the user's actual capital committed. User requested 2026-06-08.
    yt = evidence.get('S2_EXPONENT_YT') or {}
    cb_by_market = (yt.get('cost_basis_by_market') or {})
    QUESTS_USE_USD_PAID = {'S2_EXPONENT_YIELD_USX_JUN26'}
    for mb in (yt.get('by_market') or []):
        mkt = mb.get('market')
        qcode = YT_MARKET_TO_QUEST.get(mkt)
        if not qcode: continue
        any_emitting = any((p.get('yt') or 0) > 0 for p in (mb.get('positions') or []))
        if not any_emitting: continue
        cb = cb_by_market.get(mkt) or mb.get('cost_basis') or {}
        if qcode in QUESTS_USE_USD_PAID:
            value = float(cb.get('usd_paid') or 0)
        else:
            value = float(cb.get('usd_basis') or 0)
        if value > 0:
            tvl[qcode] = tvl.get(qcode, 0) + value
    return tvl


def _safe_tvl_by_quest(evidence: dict, wallet: str) -> dict:
    try:
        return compute_tvl_by_quest(evidence)
    except Exception as e:
        print(f'  WARN tvl_by_quest failed for {wallet[:10]}: {e}', flush=True)
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

    # On-chain audit set: wallets that don't exist on-chain are walker artifacts —
    # we suppress them from per-wallet JSON, aggregates, and data.json records.
    # See tools/audit_onchain_classification.py for how this table is populated.
    nonexistent_set = set()
    onchain_audit = {}  # wallet -> (kind, n_sigs_seen, n_token_accts)
    try:
        for r in con.execute("SELECT wallet, kind, n_sigs_seen, n_token_accts FROM wallet_onchain_audit"):
            if r['kind'] == 'nonexistent':
                nonexistent_set.add(r['wallet'])
            onchain_audit[r['wallet']] = (r['kind'], r['n_sigs_seen'] or 0, r['n_token_accts'] or 0)
        print(f'On-chain audit: {len(onchain_audit):,} wallets, {len(nonexistent_set):,} nonexistent (will be suppressed)')
    except sqlite3.OperationalError:
        print('On-chain audit table not present — skipping nonexistent-wallet suppression')

    # Walker-artifact suppression: wallets whose positions are walker bugs (e.g.
    # phantom owners, undetected closed positions). Treated like nonexistent —
    # suppressed from per-wallet JSON, aggregates, and data.json records.
    walker_artifact_set = set()
    p_art = os.path.join(ROOT, 'data', 'walker_artifacts.json')
    if os.path.exists(p_art):
        try:
            for entry in (json.load(open(p_art)).get('wallets') or []):
                addr = entry.get('address') if isinstance(entry, dict) else entry
                if addr: walker_artifact_set.add(addr)
            print(f'Walker artifacts: {len(walker_artifact_set):,} wallets (will be suppressed)')
        except Exception as e:
            print(f'WARN: failed to load walker_artifacts.json: {e}')
    # Fold artifacts into the same suppression set used downstream.
    nonexistent_set |= walker_artifact_set

    # Exclusion reasons (descriptive category + human reason per wallet) from
    # the wallet_exclusion_reasons table. Surfaced in per-wallet payload.meta
    # so the drawer can show why a wallet is filtered from the retail view.
    exclusion_reasons = {}
    try:
        for r in con.execute('SELECT wallet, category, reason FROM wallet_exclusion_reasons'):
            exclusion_reasons[r['wallet']] = (r['category'], r['reason'])
        print(f'Exclusion reasons: {len(exclusion_reasons):,} wallets')
    except sqlite3.OperationalError:
        print('wallet_exclusion_reasons table missing — skipping exclusion context')

    # CEX hot wallets: confirmed exchange custody fronts. Excluded from system
    # aggregates and tagged as PDAs in data.json (is_protocol_pda=true,
    # pda_label='(CEX hot wallet)').
    cex_hot_set = set()
    cex_hot_labels = {}   # wallet -> label string for the data.json record
    p_cex = os.path.join(ROOT, 'data', 'cex_hot_wallets.json')
    if os.path.exists(p_cex):
        try:
            for entry in (json.load(open(p_cex)).get('wallets') or []):
                addr = entry.get('address') if isinstance(entry, dict) else entry
                if not addr: continue
                cex_hot_set.add(addr)
                cex_hot_labels[addr] = entry.get('exchange') or entry.get('reason') or '(CEX hot wallet)'
            print(f'CEX hot wallets: {len(cex_hot_set):,} wallets (excluded from real-user aggregate)')
        except Exception as e:
            print(f'WARN: failed to load cex_hot_wallets.json: {e}')

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
    system_daily_by_quest = {}   # aggregated from real-user wallets only
    system_tvl_by_quest = {}     # aggregated TVL (USD) from real-user wallets only
    total_tvl_by_wallet = {}     # wallet -> total_tvl_usd (used to inject into data.json)
    SERVER = os.path.dirname(os.path.abspath(__file__))
    n_nonexistent_skipped = 0
    for w in all_wallets:
        # Skip wallets that don't exist on-chain — they're walker artifacts.
        # Don't write per-wallet JSON, don't aggregate into system_tvl_by_quest,
        # and don't inject into total_tvl_by_wallet (so data.json records are
        # filtered downstream).
        if w in nonexistent_set:
            n_nonexistent_skipped += 1
            continue
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

        # If wallet has zero S2 YT flares (never participated in S2 YT), zero
        # out the YT cost basis. Otherwise an old pre-S2 round-trip can leak a
        # phantom cost basis even though the wallet has no S2 stake.
        yt_flares = sum((r.get('flares') or 0) for r in quest_rows
                        if r.get('quest','').startswith('S2_EXPONENT_YIELD'))
        if yt_flares <= 0:
            for mb in (evidence.get('S2_EXPONENT_YT') or {}).get('by_market', []):
                cb = mb.get('cost_basis')
                if cb:
                    cb['usd_basis'] = 0.0

        manual = manual_pda_labels.get(w)
        is_cex = w in cex_hot_set
        is_pda = (meta.get('classification') == 'pda_protocol') or (manual is not None) or is_cex
        tvl_by_quest = _safe_tvl_by_quest(evidence, w)
        total_tvl_usd = sum(v for v in tvl_by_quest.values() if v and v > 0)
        # Resolve per-wallet PDA label / source.
        if is_cex:
            pda_source = 'cex_hot_wallet'
            pda_label  = '(CEX hot wallet)'
            pda_proto  = cex_hot_labels.get(w)
        elif manual:
            pda_source = 'manual'
            pda_label  = manual.get('label')
            pda_proto  = manual.get('protocol')
        elif meta.get('classification') == 'pda_protocol':
            pda_source = 'auto'
            pda_label  = None
            pda_proto  = None
        else:
            pda_source = None
            pda_label  = None
            pda_proto  = None
        excl = exclusion_reasons.get(w)
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
                'pda_source': pda_source,
                'pda_label':  pda_label,
                'pda_protocol_hint': pda_proto,
                'exclusion_category': excl[0] if excl else None,
                'exclusion_reason':   excl[1] if excl else None,
            },
            'total_flares': total,
            'by_quest': quest_rows,
            'evidence': evidence,
            'activity': activity_events,
            # Daily emission rate per quest at CURRENT position sizes — used
            # by the drawer's projection calculator to extrapolate forward.
            'daily_emission_by_quest': _safe_daily_emission(evidence, w),
            # Current per-quest TVL (USD) — daily-emitting quests only.
            # Powers the Flares ↔ TVL toggle in the dashboard.
            'tvl_by_quest': tvl_by_quest,
            'total_tvl_usd': total_tvl_usd,
        }
        with open(os.path.join(OUT_DIR, f'{w}.json'), 'w') as f:
            json.dump(payload, f, separators=(',', ':'))
        written += 1
        if written % 2000 == 0: print(f'  {written:,}/{len(all_wallets):,}  ({time.time()-t0:.1f}s)')
        # Aggregate system-wide daily emission per quest (used by the SLX
        # calculator to project the system total at end-date with YT-cap math).
        if not is_pda:
            for q, v in payload['daily_emission_by_quest'].items():
                system_daily_by_quest[q] = system_daily_by_quest.get(q, 0) + (v or 0)
            for q, v in tvl_by_quest.items():
                system_tvl_by_quest[q] = system_tvl_by_quest.get(q, 0) + (v or 0)
        # Stash total_tvl_usd for every wallet (PDAs included) so the
        # data.json injection can populate r.tvl on the table records.
        if total_tvl_usd > 0:
            total_tvl_by_wallet[w] = total_tvl_usd

    # Inject system_daily_emission_by_quest + system_tvl_by_quest into data.json
    # so the calculator can compute YT-decay-aware system projections AND the
    # dashboard's Flares↔TVL toggle can sum r.tvl across visible rows without
    # scanning 28k files.
    data_json_path = os.path.join(SERVER, 'data.json')
    try:
        with open(data_json_path) as f: data = json.load(f)
        data['system_daily_emission_by_quest'] = {q: round(v, 4) for q, v in system_daily_by_quest.items() if v > 0}
        data['system_tvl_by_quest'] = {q: round(v, 4) for q, v in system_tvl_by_quest.items() if v > 0}
        # Filter out nonexistent wallets from records (walker artifacts) AND
        # attach per-record tvl so the table can render the TVL column without
        # fetching every per-wallet detail JSON.
        orig_recs = data.get('records', [])
        kept_recs = []
        n_rec_dropped = 0
        n_rec_cex_flagged = 0
        for rec in orig_recs:
            w_ = rec.get('wallet')
            if w_ in nonexistent_set:
                n_rec_dropped += 1
                continue
            rec['tvl'] = round(total_tvl_by_wallet.get(w_, 0), 4)
            if w_ in cex_hot_set:
                # Tag CEX hot wallets so the dashboard treats them like PDAs.
                rec['is_protocol_pda'] = True
                rec['pda_label'] = '(CEX hot wallet)'
                n_rec_cex_flagged += 1
            # Ensure exclusion fields are populated even for records that
            # build_data.py emitted before the reasons table existed.
            if not rec.get('exclusion_category'):
                excl_r = exclusion_reasons.get(w_)
                if excl_r:
                    rec['exclusion_category'] = excl_r[0]
                    rec['exclusion_reason']   = excl_r[1]
            kept_recs.append(rec)
        data['records'] = kept_recs
        with open(data_json_path, 'w') as f: json.dump(data, f, separators=(',', ':'))
        total_daily = sum(system_daily_by_quest.values())
        total_tvl   = sum(system_tvl_by_quest.values())
        print(f'\nInjected system_daily_emission_by_quest into data.json: {total_daily:,.0f} flares/day across {sum(1 for v in system_daily_by_quest.values() if v > 0)} quests')
        print(f'Injected system_tvl_by_quest into data.json: ${total_tvl:,.0f} TVL across {sum(1 for v in system_tvl_by_quest.values() if v > 0)} quests')
        print(f'Dropped {n_rec_dropped:,} nonexistent-wallet records from data.json (kept {len(kept_recs):,}/{len(orig_recs):,})')
        if n_rec_cex_flagged:
            print(f'Flagged {n_rec_cex_flagged:,} CEX hot wallet records (is_protocol_pda=true)')
    except Exception as e:
        print(f'WARN: failed to inject system daily into data.json: {e}')

    # TASK 3: Flag institutional-EOA candidates (informational, doesn't reclassify).
    # Real EOAs (exists, n_sigs_seen<30, n_token_accts<10) with TVL>$1M look like
    # institutional fronting wallets — low organic activity + concentrated capital.
    try:
        candidates = []
        for w, total_tvl_usd in total_tvl_by_wallet.items():
            if total_tvl_usd <= 1_000_000:
                continue
            audit = onchain_audit.get(w)
            if not audit:
                continue
            kind, n_sigs, n_tok = audit
            if kind != 'eoa': continue
            if n_sigs >= 30 or n_tok >= 10: continue
            meta = meta_by_w.get(w, {})
            candidates.append({
                'wallet': w,
                'classification': meta.get('classification'),
                'n_sigs_seen': n_sigs,
                'n_token_accts': n_tok,
                'total_tvl_usd': round(total_tvl_usd, 2),
                'flag': 'institutional_eoa_candidate',
            })
        candidates.sort(key=lambda x: -x['total_tvl_usd'])
        out_path = os.path.join(ROOT, 'data', 'institutional_candidates.json')
        with open(out_path, 'w') as f:
            json.dump({
                'generated_at': int(time.time()),
                'criteria': 'kind=eoa AND n_sigs_seen<30 AND n_token_accts<10 AND total_tvl_usd>1_000_000',
                'note': 'Informational only. These are real EOAs (not PDAs) but exhibit low-activity, high-TVL patterns typical of institutional fronting wallets.',
                'count': len(candidates),
                'wallets': candidates,
            }, f, indent=2)
        print(f'Wrote {len(candidates):,} institutional_eoa_candidate flags to {out_path}')
    except Exception as e:
        print(f'WARN: failed to write institutional_candidates.json: {e}')

    print(f'\nDone. {written:,} files in {time.time()-t0:.1f}s (skipped {n_nonexistent_skipped:,} nonexistent wallets)')


if __name__ == '__main__':
    main()

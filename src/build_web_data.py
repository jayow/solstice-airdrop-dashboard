#!/usr/bin/env python3
"""Produce the static JSON payload that powers the web/ UI.

Outputs:
  web/public/data.json                — all wallets + totals (for the main table)
  web/public/events/{addr}.json       — per-wallet trade events (for the detail page)
"""
import os, json, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEE_PAYERS = os.path.join(ROOT, 'data/fee_payers.json')
TRADES     = os.path.join(ROOT, 'data/exponent_trades.jsonl')
COHORTS    = os.path.join(ROOT, 'data/solstice_registration/accounts.jsonl')
PARTNERS   = os.path.join(ROOT, 'data/partner_footprint.json')
HOLDINGS   = os.path.join(ROOT, 'data/wallet_holdings.json')
PRESALE    = os.path.join(ROOT, 'data/slx_presale_buyers.json')
OUT_MAIN   = os.path.join(ROOT, 'web/public/data.json')
OUT_EV_DIR = os.path.join(ROOT, 'web/public/events')

SOL_PRICE_USD = 175
DUST_MIN_SOL = 0.001
DUST_MIN_STABLES = 0.01

MARKETS = ['USX-09FEB26', 'eUSX-11MAR26', 'USX-01JUN26', 'eUSX-01JUN26']  # chronological by maturity

# Hardcoded in the Solstice app bundle.
COHORT_ALLOC = {
    '1': {'shareOfSlxPct': 3.16, 'users': 11},
    '2': {'shareOfSlxPct': 1.38, 'users': 49},
    '3': {'shareOfSlxPct': 1.37, 'users': 195},
    '4': {'shareOfSlxPct': 1.38, 'users': 646},
    '5': {'shareOfSlxPct': 1.38, 'users': 3571},
    '6': {'shareOfSlxPct': 0.49, 'users': 1423199},
}


def load_cohort_map(path):
    m = {}
    if not os.path.exists(path):
        return m
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if '_error' in d:
                continue
            a = d.get('walletAddress')
            c = d.get('cohort')
            if not a:
                continue
            m[a] = {
                'cohort': str(c) if c not in (None, '', '0') else None,
                'claimTx': d.get('txSignature') or '',
            }
    return m

def group_for(market):
    if market.startswith('USX-'): return 'USX'
    if market.startswith('eUSX-'): return 'eUSX'
    return None

def main():
    os.makedirs(OUT_EV_DIR, exist_ok=True)

    payers = json.load(open(FEE_PAYERS))
    payers = [p for p in payers
              if p['totalSOL'] >= DUST_MIN_SOL
              or p['totalUSDC'] >= DUST_MIN_STABLES
              or p['totalUSDT'] >= DUST_MIN_STABLES]

    cohort_map = load_cohort_map(COHORTS)
    partner_map = json.load(open(PARTNERS)) if os.path.exists(PARTNERS) else {}
    holdings_map = json.load(open(HOLDINGS)) if os.path.exists(HOLDINGS) else {}
    presale_raw = json.load(open(PRESALE)) if os.path.exists(PRESALE) else {'buyers': []}
    # Include ALL depositors — fully-refunded wallets are kept so we can show their journey
    presale_map = {b['sender']: b for b in presale_raw.get('buyers', []) if (b.get('deposited') or 0) > 0.01}

    def partner_slim(addr):
        """Returns the partner footprint for a wallet, stripped to what the UI needs.
           4 entries: kamino/orca/raydium (DeFi) + holdings (direct in-wallet)."""
        f = partner_map.get(addr)
        out = {}
        for p in ('kamino', 'orca', 'raydium'):
            v = (f or {}).get(p, {}) if f else {}
            if not v.get('any'):
                out[p] = None; continue
            slim = {
                'pre':  bool(v.get('pre')),
                'txs':  int(v.get('txs') or 0),
                'firstTs': int(v.get('firstTs') or 0),
                'lastTs':  int(v.get('lastTs') or 0),
            }
            if p == 'kamino':
                slim['supplyUsd'] = float(v.get('supplyUsd') or 0)
                slim['borrowUsd'] = float(v.get('borrowUsd') or 0)
            out[p] = slim

        h = holdings_map.get(addr)
        if not h or not (h.get('usx', {}).get('held') or h.get('eusx', {}).get('held')):
            out['holdings'] = None
        else:
            usx = h.get('usx', {})
            eusx = h.get('eusx', {})
            # Pre-flag: we don't know historical pre/post-snapshot for holdings; but ATA
            # presence implies *ever held*, which is what matters for Season 1. Treat as pre.
            out['holdings'] = {
                'pre': True,
                'usxCurr':  float(usx.get('currBal') or 0),
                'eusxCurr': float(eusx.get('currBal') or 0),
                'usxHeld':  bool(usx.get('held')),
                'eusxHeld': bool(eusx.get('held')),
            }
        return out

    # Init wallet rows
    by_addr = {}
    for p in payers:
        fee_usd = round(p['totalUSDC'] + p['totalUSDT'] + p['totalSOL'] * SOL_PRICE_USD, 2)
        cinfo = cohort_map.get(p['sender'], {})
        cohort = cinfo.get('cohort')
        alloc = COHORT_ALLOC.get(cohort, {}) if cohort else {}
        row = {
            'addr': p['sender'],
            'fee': fee_usd,
            'feeTxs': p['txCount'],
            'first': p['firstTime'],
            'last':  p['lastTime'],
            'cohort': cohort,
            'cohortShareOfSlxPct': alloc.get('shareOfSlxPct'),
            'cohortUsers': alloc.get('users'),
            'perUserSharePct': round(alloc['shareOfSlxPct']/alloc['users']*100, 6) if alloc else None,
            'claimed': bool(cinfo.get('claimTx')),
            'claimTx': cinfo.get('claimTx') or None,
            'partners': partner_slim(p['sender']),
            'presale': (lambda _b: {
                'deposited': round(float(_b.get('deposited') or 0), 2),
                'refunded':  round(float(_b.get('refunded') or 0), 2),
                'net':       round(float(_b.get('totalUsdc') or 0), 2),
                'status':    ('refunded' if (_b.get('refunded') or 0) > 0.01
                                  and (_b.get('totalUsdc') or 0) < 0.01 else
                              'partial'  if (_b.get('refunded') or 0) > 0.01 else
                              'kept'),
                'txCount': int(_b.get('txCount') or 0),
                'firstTs': int(_b.get('firstTs') or 0),
                'lastTs':  int(_b.get('lastTs') or 0),
            } if _b else None)(presale_map.get(p['sender'])),
            'm': {m: {'buy': 0.0, 'sell': 0.0} for m in MARKETS},
            'lp': {'USX': {'add': 0.0, 'remove': 0.0}, 'eUSX': {'add': 0.0, 'remove': 0.0}},
            'claim': {'USX': 0.0, 'eUSX': 0.0},
            'totalYtBuys': 0.0, 'totalYtSells': 0.0,
            'totalLpAdds': 0.0, 'totalLpRemoves': 0.0,
            'totalClaims': 0.0,
            'ytNet': 0.0, 'totalSpent': 0.0,
            'expTxs': 0,
        }
        by_addr[p['sender']] = row

    events_by_addr = {a: [] for a in by_addr}

    for l in open(TRADES):
        l = l.strip()
        if not l: continue
        try: r = json.loads(l)
        except: continue
        if not r.get('market'): continue
        addr = r.get('signer')
        if addr not in by_addr: continue  # ignore non-fee-payers (sub-wallets etc.)
        events_by_addr[addr].append(r)
        row = by_addr[addr]
        row['expTxs'] += 1
        usd = abs(r.get('usdNet', 0))
        g = group_for(r['market'])
        act = r.get('action', 'other')
        if act == 'buyYt':
            row['m'][r['market']]['buy'] += usd
            row['totalYtBuys'] += usd
        elif act == 'sellYt':
            row['m'][r['market']]['sell'] += usd
            row['totalYtSells'] += usd
        elif act == 'addLiq':
            row['lp'][g]['add'] += usd
            row['totalLpAdds'] += usd
        elif act == 'removeLiq':
            row['lp'][g]['remove'] += usd
            row['totalLpRemoves'] += usd
        elif act == 'claimYield':
            row['claim'][g] += usd
            row['totalClaims'] += usd

    # Round + derive
    for row in by_addr.values():
        for m in MARKETS:
            row['m'][m]['buy']  = round(row['m'][m]['buy'],  2)
            row['m'][m]['sell'] = round(row['m'][m]['sell'], 2)
        for g in row['lp']:
            row['lp'][g]['add']    = round(row['lp'][g]['add'],    2)
            row['lp'][g]['remove'] = round(row['lp'][g]['remove'], 2)
        for g in row['claim']:
            row['claim'][g] = round(row['claim'][g], 2)
        row['totalYtBuys']    = round(row['totalYtBuys'],    2)
        row['totalYtSells']   = round(row['totalYtSells'],   2)
        row['totalLpAdds']    = round(row['totalLpAdds'],    2)
        row['totalLpRemoves'] = round(row['totalLpRemoves'], 2)
        row['totalClaims']    = round(row['totalClaims'],    2)
        row['ytNet']          = round(row['totalYtBuys'] - row['totalYtSells'], 2)
        row['totalSpent']     = round(row['totalYtBuys'] + row['totalLpAdds'], 2)

    wallets = sorted(by_addr.values(), key=lambda r: (-r['ytNet'], -r['totalSpent']))

    yt_active = sum(1 for r in wallets if r['totalYtBuys'] + r['totalYtSells'] + r['totalLpAdds'] > 0)

    cohort_breakdown = {c: {'feePayers': 0, 'claimed': 0, 'orphan': 0, **COHORT_ALLOC[c]} for c in COHORT_ALLOC}
    registered = claimed_total = orphan_fee = no_cohort = 0
    presale_totals = {
        # Our fee-payer subset
        'buyers':      sum(1 for w in wallets if w.get('presale')),
        'buyersKept':  sum(1 for w in wallets if (w.get('presale') or {}).get('status') == 'kept'),
        'buyersRefunded': sum(1 for w in wallets if (w.get('presale') or {}).get('status') == 'refunded'),
        'totalDeposited': round(sum(float((w.get('presale') or {}).get('deposited') or 0) for w in wallets), 2),
        'totalRefunded':  round(sum(float((w.get('presale') or {}).get('refunded') or 0) for w in wallets), 2),
        'totalUsdc':      round(sum(float((w.get('presale') or {}).get('net') or 0) for w in wallets), 2),
        # Global presale
        'globalBuyers':   int(presale_raw.get('uniqueBuyers') or 0),
        'globalDepositors': int(presale_raw.get('allDepositors') or 0),
        'globalUsdc':     round(float(presale_raw.get('netUsdc') or 0), 2),
        'grossDeposits':  round(float(presale_raw.get('grossDeposits') or 0), 2),
        'grossRefunds':   round(float(presale_raw.get('grossRefunds') or 0), 2),
    }

    partner_totals = {'kamino': 0, 'orca': 0, 'raydium': 0, 'holdings': 0, 'any': 0}
    for w in wallets:
        pre_any = False
        for p in ('kamino', 'orca', 'raydium', 'holdings'):
            part = w['partners'].get(p)
            if part and part.get('pre'):
                partner_totals[p] += 1
                pre_any = True
        if pre_any:
            partner_totals['any'] += 1
    for w in wallets:
        if w['cohort']:
            registered += 1
            cohort_breakdown[w['cohort']]['feePayers'] += 1
            if w['claimed']:
                claimed_total += 1
                cohort_breakdown[w['cohort']]['claimed'] += 1
            else:
                orphan_fee += 1
                cohort_breakdown[w['cohort']]['orphan'] += 1
        else:
            no_cohort += 1

    dataset = {
        'generatedAt': datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z',
        'totals': {
            'wallets':   len(wallets),
            'feesUsd':   round(sum(w['fee'] for w in wallets), 2),
            'ytActive':  yt_active,
            'ytBuys':    round(sum(w['totalYtBuys']  for w in wallets), 2),
            'ytSells':   round(sum(w['totalYtSells'] for w in wallets), 2),
            'lpAdds':    round(sum(w['totalLpAdds']  for w in wallets), 2),
            'lpRemoves': round(sum(w['totalLpRemoves'] for w in wallets), 2),
            'claims':    round(sum(w['totalClaims']   for w in wallets), 2),
            'registered': registered,
            'claimedSlx': claimed_total,
            'orphanFee':  orphan_fee,
            'noCohort':   no_cohort,
            'cohorts':   cohort_breakdown,
            'partners':  partner_totals,
            'presale':   presale_totals,
        },
        'wallets': wallets,
    }
    with open(OUT_MAIN, 'w') as f:
        json.dump(dataset, f, separators=(',', ':'))
    print(f'wrote {OUT_MAIN}: {len(wallets)} wallets')

    # Per-wallet events (only those with at least one event to keep output small)
    written = 0
    for addr, evs in events_by_addr.items():
        if not evs: continue
        # Keep the event fields the UI reads; drop heavy ones
        small = [{
            'sig': e['sig'], 'blockTime': e.get('blockTime'),
            'market': e['market'], 'signer': e['signer'],
            'action': e.get('action', 'other'),
            'instr':  e.get('instr'),
            'ytDelta':         e.get('ytDelta', 0),
            'underlyingDelta': e.get('underlyingDelta', 0),
            'syDelta':         e.get('syDelta', 0),
            'usdNet':          e.get('usdNet', 0),
            'eusxRate':        e.get('eusxRate'),
        } for e in sorted(evs, key=lambda x: x.get('blockTime', 0))]
        with open(os.path.join(OUT_EV_DIR, f'{addr}.json'), 'w') as f:
            json.dump(small, f, separators=(',', ':'))
        written += 1
    print(f'wrote per-wallet event files: {written}')

if __name__ == '__main__':
    main()

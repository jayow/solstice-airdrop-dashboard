"""Aggregate per-wallet Kamino/Orca/Raydium footprint for the Solstice dashboard.

Output: data/partner_footprint.json
  { "<wallet>": {
      "kamino":  { "any": bool, "pre": bool, "txs": int, "supplyUsd": float, "borrowUsd": float, "firstTs": int, "lastTs": int },
      "orca":    { "any": bool, "pre": bool, "txs": int, "firstTs": int, "lastTs": int },
      "raydium": { "any": bool, "pre": bool, "txs": int, "firstTs": int, "lastTs": int },
    }, ... }

"pre" = any activity before Season 1 snapshot = before 2026-04-13T05:00 UTC.
"""
import os, json, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SNAPSHOT_TS = 1776243600  # 2026-04-13T05:00:00Z — Season 2 start / Season 1 snapshot cutoff

POOLS = {
    'orca': [
        'orca_usx_usdc',
        'orca_eusx_usx',
        'orca_usx_usdg',
    ],
    'raydium': [
        'raydium_usx_usdc',
        'raydium_eusx_usx',
    ],
}


def init_entry():
    return {
        'kamino':  {'any': False, 'pre': False, 'txs': 0, 'supplyUsd': 0.0, 'borrowUsd': 0.0, 'firstTs': 0, 'lastTs': 0},
        'orca':    {'any': False, 'pre': False, 'txs': 0, 'firstTs': 0, 'lastTs': 0},
        'raydium': {'any': False, 'pre': False, 'txs': 0, 'firstTs': 0, 'lastTs': 0},
    }


def touch(entry, partner, ts):
    p = entry[partner]
    p['any'] = True
    if ts and ts < SNAPSHOT_TS:
        p['pre'] = True
    if ts:
        if not p['firstTs'] or ts < p['firstTs']:
            p['firstTs'] = ts
        if ts > p['lastTs']:
            p['lastTs'] = ts


def main():
    out = {}
    def get(addr):
        if addr not in out:
            out[addr] = init_entry()
        return out[addr]

    # --- Kamino: parsed events include action + usdNet + signer ---
    print('Aggregating Kamino events...', flush=True)
    for line in open(os.path.join(ROOT, 'data/kamino_events.jsonl')):
        try:
            r = json.loads(line)
        except Exception:
            continue
        signer = r.get('signer')
        if not signer:
            continue
        ts = r.get('blockTime')
        e = get(signer)
        touch(e, 'kamino', ts)
        k = e['kamino']
        k['txs'] += 1
        usd = abs(r.get('usdNet') or 0)
        act = r.get('action')
        if act == 'supply':
            k['supplyUsd'] += usd
        elif act == 'borrow':
            k['borrowUsd'] += usd
    # --- Orca & Raydium: sigs + signers (joined by sig) ---
    for partner, pool_keys in POOLS.items():
        for pk in pool_keys:
            sigs_path = os.path.join(ROOT, f'data/partner_pools/{pk}.sigs.json')
            signers_path = os.path.join(ROOT, f'data/partner_pools/{pk}.signers.jsonl')
            if not (os.path.exists(sigs_path) and os.path.exists(signers_path)):
                continue

            # sig -> blockTime
            ts_by_sig = {}
            for s in json.load(open(sigs_path)):
                ts_by_sig[s['signature']] = s.get('blockTime') or 0

            # signer per sig
            print(f'Aggregating {pk} ({partner})...', flush=True)
            for line in open(signers_path):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                signer = r.get('signer')
                sig = r.get('sig')
                if not signer:
                    continue
                ts = ts_by_sig.get(sig, 0)
                e = get(signer)
                touch(e, partner, ts)
                e[partner]['txs'] += 1

    # Round USD
    for addr, e in out.items():
        e['kamino']['supplyUsd'] = round(e['kamino']['supplyUsd'], 2)
        e['kamino']['borrowUsd'] = round(e['kamino']['borrowUsd'], 2)

    out_path = os.path.join(ROOT, 'data/partner_footprint.json')
    with open(out_path, 'w') as f:
        json.dump(out, f, separators=(',', ':'))
    print(f'\nWrote {out_path}')
    print(f'  wallets with any partner footprint: {len(out):,}')
    # quick stats
    for p in ('kamino', 'orca', 'raydium'):
        any_c = sum(1 for e in out.values() if e[p]['any'])
        pre_c = sum(1 for e in out.values() if e[p]['pre'])
        print(f'  {p:<8s}: any={any_c:,}  pre-snapshot={pre_c:,}')


if __name__ == '__main__':
    main()

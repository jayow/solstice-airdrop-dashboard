"""Find wallets that interacted with Solstice/partner programs since a given
cutoff timestamp. Returns the SMALL set of wallets whose flare data may have
changed and therefore need a fresh extract.

Output: prints one wallet per line to stdout.

Source programs scanned for sigs > cutoff_ts:
  - Exponent (YT/LP changes)
  - Kamino Lending on Solstice market (deposits/borrows)
  - Whirlpool S2 pools (Orca LP changes)
  - Raydium CLMM S2 pools (Raydium LP changes)
  - Loopscale program

For each tx since cutoff, parse the signer(s) — those are the user wallets.
"""
import os, sys, json, time, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc

# Watch-list of accounts whose sig history bounds "S2-relevant activity"
WATCH_ADDRESSES = [
    # Exponent USX/eUSX market accounts (every YT/LP op touches one of these)
    'BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm',   # USX-Jun26 market
    'rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP',    # eUSX-Jun26 market
    # Kamino Solstice lending market
    '9Y7uwXgQ68mGqRtZfuFaP4hc4fxeJ7cE9zTtqTxVhfGU',
    # Solstice reserves
    'H2pmnDSjfxeQ8zUeyUohokegYbXZgkjH4kgmoQVybyAX',   # USX reserve
    'ARQFJTiUJEuxoiA9VtAcnoAUHYvbTmhKytz7D6nfnfEb',   # eUSX reserve
    '34Bb1oLf9F7H4CAGefC56HFBsuJQ1tSJafmZnYkFCd83',   # USDG reserve
    # Orca whirlpools (S2-incentivized)
    '2e3WeM4WwdEqwTtRnWN3gJSbhNg1P6Aj2y7kEdfrYbix',   # USX/USDC
    'AUr5EVRwGDsKB2EeS1V63ncjHXDNRDLVfBP47qNvPoVf',   # eUSX/USX
    'J6h5bf3iohBXtsRNRFAqFc5FeBCh3yAjxXGuiE1sTc5Q',   # USDG/USX
    # Raydium CLMM S2 pools
    'EWivkwNtcxuPsU6RyD7Pfvs7u9Yv8nQ79tJ7xgGyPrp6',   # USX/USDC
    'BkvKpstxgeEJYzvFnWWuAbTDcrFMJBty3kXxUfGG9D7n',   # eUSX/USX
    # Loopscale USX ONE vault
    '3s3vAaYpwkyjrgzpBRwgSDxpwHPD1jic25mb1VDzM8Rk',
    # Kamino USDG/USX Strategy
    '45bdcbekD687TU49RFux1a4csf3TN3cM3J1UaFcFhWt2',
]


def fetch_sigs_since(addr: str, cutoff_ts: int) -> list:
    """Returns all sigs on `addr` with blockTime >= cutoff_ts."""
    sigs = []
    before = None
    while True:
        params = [addr, {'limit': 1000}]
        if before: params[1]['before'] = before
        r = rpc('getSignaturesForAddress', params, timeout=30)
        batch = r.get('result', []) or []
        if not batch: break
        keep = [s for s in batch if (s.get('blockTime') or 0) >= cutoff_ts]
        sigs.extend(keep)
        if len(keep) < len(batch): break
        if len(batch) < 1000: break
        before = batch[-1]['signature']
    return sigs


def fetch_signers(sig: str) -> list:
    """Return all signer pubkeys from a tx."""
    try:
        r = rpc('getTransaction', [sig, {'encoding':'jsonParsed','maxSupportedTransactionVersion':0}], timeout=15)
        tx = r.get('result')
        if not tx: return []
        msg = tx['transaction']['message']
        signers = []
        for k in msg.get('accountKeys', []):
            if isinstance(k, dict) and k.get('signer'):
                signers.append(k['pubkey'])
        return signers
    except Exception: return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since-hours', type=float, default=24.0)
    ap.add_argument('--since-ts', type=int, help='Override cutoff timestamp (epoch)')
    args = ap.parse_args()

    now_ts = int(time.time())
    cutoff = args.since_ts or int(now_ts - args.since_hours * 3600)
    print(f'# scanning since {cutoff} ({(now_ts-cutoff)/3600:.1f}h ago) — {len(WATCH_ADDRESSES)} watch addresses', file=sys.stderr, flush=True)

    # Step 1: collect all sigs since cutoff across watch addresses
    all_sigs = set()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_sigs_since, a, cutoff): a for a in WATCH_ADDRESSES}
        for fut in as_completed(futs):
            addr = futs[fut]
            sigs = fut.result()
            new = sum(1 for s in sigs if s['signature'] not in all_sigs)
            for s in sigs: all_sigs.add(s['signature'])
            print(f'#   {addr[:10]}.. : {len(sigs)} sigs (+{new} new)', file=sys.stderr, flush=True)
    print(f'# total unique sigs since cutoff: {len(all_sigs)}', file=sys.stderr, flush=True)

    # Step 2: fetch each tx and extract signers
    wallets = set()
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(fetch_signers, s) for s in all_sigs]
        n = 0
        for fut in as_completed(futs):
            n += 1
            for w in fut.result(): wallets.add(w)
            if n % 200 == 0:
                print(f'#   {n}/{len(all_sigs)} txs scanned, wallets so far: {len(wallets)}', file=sys.stderr, flush=True)

    print(f'# {len(wallets)} active wallets in window', file=sys.stderr, flush=True)
    for w in sorted(wallets):
        print(w, flush=True)


if __name__ == '__main__':
    main()

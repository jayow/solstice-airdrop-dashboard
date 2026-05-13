"""For each mystery wallet, list every tx they personally signed and categorize by action.

We care about WHAT THEY SIGN FOR — i.e., what actions they're authorizing. This is
different from "what txs appear on their account" (which includes inbound transfers
they didn't sign).

For each tx where the wallet is a signer (usually fee payer):
  1. List all top-level programs invoked
  2. If Squads V4 is called, mark the category based on what the proposal executes
  3. Summary: count of action types, and print each tx's sig + programs
"""
import os, json, time, datetime as dt
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = None
for line in open(os.path.join(ROOT, '.env')):
    if line.startswith('HELIUS_API_KEY'):
        URL = line.split('=', 1)[1].strip().strip('"').strip("'")
        break

WALLETS = [
    ('FA1qn', 'FA1qnqZWMptKtmyQ4DQvufH5tiYentEyvStPehsJfCTk'),
    ('EY7yP', 'EY7yPT1nJr7AWiXApcnuMyPqba9NM5MgyzFZKscPXzxE'),
    # Also the multisig vaults themselves (if they sign as fee payer, which Squads vaults usually don't)
    ('FA1qn-vault', '8VSon1QGWuEXWgL9hSrZC7rH3hx8S1YA8Vvr6JJ9eA4T'),  # guess - verify below
    ('EY7yP-vault', 'J9iWieFzRKZFyU2sXVMqfQqpJVZUP6Tg5K8rJ7fDNx77'),  # guess - verify below
]

# Known programs — friendly names
PROGRAMS = {
    '11111111111111111111111111111111': 'System (SOL xfer)',
    'SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf': 'Squads V4',
    'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA': 'SPL Token',
    'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb': 'Token-2022',
    'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL': 'Associated Token',
    'ComputeBudget111111111111111111111111111111': 'ComputeBudget',
    'DuX1wcoQrJ6XypxLNq3GRrmHFAAMgCqAKbzboabyCtzB': '**SOLSTICE fee**',
    'CHtfHPSiFoATLzciMtNe2QVKckXtP8ASWucu8Ad69cyK': '**SOLSTICE presale**',
    'T1pyyaTNZsKv2WcRAB8oVnk93mLJw2XzjtVYqCsaHqt': 'Tiplink',
    'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4': 'Jupiter v6',
    'whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc': 'Orca Whirlpool',
    'srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX': 'Serum',
    '675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8': 'Raydium AMM',
    'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK': 'Raydium CLMM',
    'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD': 'Kamino Lend',
    'MEisE1HzehtrDpAAT8PnLHjpSSkRYakotTuJRPjTpo8': 'Drift',
    'PERPHjGBqRHArX4DySjwM6UJHiR3sWAatqfdBS2qQJu': 'Drift Perps',
    'M2mx93ekt1fmXSVkTrUL9xVFHkmME8HTUi5Cyc5aF7K': 'MEV bot program',
    'L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95': 'Loopscale',
    'MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr': 'Squads Memo',
    'moontUzsdepotRGe5xsfip7vLPTJnVuafqdUWexVnPM': 'Moonshot depot',
    'FUTARELBfJfQ8RDGhg1wdhddq1odMAJUePHFuBYfUxKq': 'Futarchy (MetaDAO)',
    'FUTKPrt66uGGCTpk6f9tmRX2325cWgXzGCwvWhyyzjea': 'Futarchy (MetaDAO)',
    'bankt33JL42cBaaCDcxKsU51LrePe3HrC5vEhHCp1tK': 'Marinade bank',
    'stakedcgmJBcZzShwtNh31PCYKWRQH4AnWCmaEyDJnG3': 'Jito stake',
    'jforXcLCv1Tua8vz6SPpRqH2aASzxdgVZuUP83pd2oc': 'Jupiter DCA',
    'JUpRJvL7C6wPs5vVbBsvLPUJLTLn4Z5ZJKKfYzqNq7h': 'Jupiter Swap v4',
    'GhQ68iFYNB1uUgdUbUH3mqn4cGwZPWS9Hku1NX8Vpd2z': '**Kraken CEX**',
    'AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2': '**Binance CEX**',
    '5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9': '**Binance hot**',
    '2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm': '**Coinbase**',
    '9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM': '**Coinbase**',
}

session = requests.Session()


def rpc(method, params, retries=6):
    body = {'jsonrpc':'2.0','id':1,'method':method,'params':params}
    for i in range(retries):
        try:
            r = session.post(URL, json=body, timeout=25)
            if r.status_code in (429, 503):
                time.sleep(min(8, 0.5*(2**i))); continue
            j = r.json()
            if 'error' in j: time.sleep(0.5); continue
            return j.get('result')
        except requests.RequestException:
            time.sleep(0.5*(i+1))
    return None


def all_sigs(addr, cap=5000):
    out = []; before = None
    while len(out) < cap:
        params = [addr, {'limit': 1000}]
        if before: params[1]['before'] = before
        b = rpc('getSignaturesForAddress', params)
        if not b: break
        out.extend(b); before = b[-1]['signature']
        if len(b) < 1000: break
    return out


def fetch_tx(sig):
    return rpc('getTransaction', [sig, {
        'encoding':'jsonParsed','maxSupportedTransactionVersion':0,'commitment':'confirmed'}])


def prog_name(pid):
    return PROGRAMS.get(pid, pid[:16]+'..')


def categorize_squads_inner(tx):
    """Look at all programs invoked INSIDE a Squads V4 tx (inner instructions of vault_transaction_execute).
    Returns the non-Squads programs hit — those are the real actions being authorized."""
    meta = tx.get('meta') or {}
    inner_programs = set()
    for inner in (meta.get('innerInstructions') or []):
        for ix in inner.get('instructions', []):
            pid = ix.get('programId') or ix.get('program')
            if pid and pid != 'SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf':
                inner_programs.add(pid)
    return inner_programs


def analyze(label, addr):
    print(f'\n==== {label} ({addr}) ====', flush=True)
    sigs = all_sigs(addr)
    print(f'  {len(sigs)} total signatures', flush=True)

    # Fetch all txs
    records = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_tx, s['signature']): s for s in sigs}
        for f in as_completed(futs):
            s = futs[f]; tx = f.result()
            if tx: records.append((s, tx))

    # For each tx, check if wallet is a SIGNER (usually fee payer = accountKeys[0] with signer=true)
    signed_txs = []
    not_signed = 0
    for s, tx in records:
        meta = tx.get('meta') or {}
        if meta.get('err'): continue
        msg = tx['transaction']['message']
        keys = msg.get('accountKeys', [])
        # Check if addr is a signer
        is_signer = False
        is_fee_payer = False
        for i, k in enumerate(keys):
            if isinstance(k, dict):
                if k.get('pubkey') == addr and k.get('signer'):
                    is_signer = True
                    if i == 0: is_fee_payer = True
                    break
        if not is_signer:
            not_signed += 1; continue
        signed_txs.append((s, tx, is_fee_payer))

    print(f'  {len(signed_txs)} txs where {label} is SIGNER  ({not_signed} where only passive)', flush=True)

    # For each signed tx, categorize
    program_counter = Counter()
    squads_inner_counter = Counter()
    action_examples = defaultdict(list)
    solstice_txs = []

    for s, tx, fee_payer in signed_txs:
        msg = tx['transaction']['message']
        top_progs = set()
        for ix in msg.get('instructions', []):
            pid = ix.get('programId') or ix.get('program')
            if pid: top_progs.add(pid)

        # Is Squads V4 involved?
        has_squads = 'SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf' in top_progs

        # Inner programs (what the Squads proposal actually executes, or composed actions)
        inner_progs = categorize_squads_inner(tx)

        # All programs = top + inner
        all_progs = top_progs | inner_progs

        for p in all_progs:
            program_counter[p] += 1
            # example sig per program
            if len(action_examples[p]) < 2:
                action_examples[p].append(s['signature'])

        for p in inner_progs:
            squads_inner_counter[p] += 1

        # Flag any Solstice-related
        if 'DuX1wcoQrJ6XypxLNq3GRrmHFAAMgCqAKbzboabyCtzB' in all_progs or \
           'CHtfHPSiFoATLzciMtNe2QVKckXtP8ASWucu8Ad69cyK' in all_progs:
            solstice_txs.append(s['signature'])

        # Also scan account keys for Solstice addresses
        keys_list = [(k.get('pubkey') if isinstance(k, dict) else k) for k in msg.get('accountKeys', [])]
        for solstice_addr in ('DuX1wcoQrJ6XypxLNq3GRrmHFAAMgCqAKbzboabyCtzB',
                              'CHtfHPSiFoATLzciMtNe2QVKckXtP8ASWucu8Ad69cyK'):
            if solstice_addr in keys_list and s['signature'] not in solstice_txs:
                solstice_txs.append(s['signature'])

    # Print summary
    print(f'\n  PROGRAMS authorized by {label}:')
    for pid, n in program_counter.most_common():
        name = prog_name(pid)
        ex = action_examples[pid][0] if action_examples[pid] else ''
        print(f'    {n:>4}x  {name:<30}  {pid[:32]}..  ex: {ex[:16]}..')

    if squads_inner_counter:
        print(f'\n  Non-Squads programs embedded in Squads proposals:')
        for pid, n in squads_inner_counter.most_common():
            print(f'    {n:>4}x  {prog_name(pid)}  ({pid})')

    if solstice_txs:
        print(f'\n  SOLSTICE-related txs ({len(solstice_txs)}):')
        for sig in solstice_txs[:20]:
            print(f'    https://solscan.io/tx/{sig}')

    return {
        'addr': addr,
        'total_sigs': len(sigs),
        'signed_txs': len(signed_txs),
        'programs': dict(program_counter),
        'squads_inner_programs': dict(squads_inner_counter),
        'solstice_txs': solstice_txs,
        'examples': {p: v[:5] for p, v in action_examples.items()},
    }


def main():
    # First, derive the actual vault addresses from Squads V4 PDA.
    # Squads V4 vault derivation: seed = [b"multisig", multisig_key, b"vault", vault_index_u8].
    # We don't know multisig_key for certain; but prior investigation already knows the funded multisig
    # addresses. For now let's just run on FA1qn and EY7yP themselves. If they sign the Squads proposal,
    # we'll see it here. The linked multisig vaults (that hold funds) we can add via cluster_trace.json.

    result = {}
    for label, addr in WALLETS[:2]:  # just FA1qn + EY7yP themselves for now
        result[label] = analyze(label, addr)

    out_path = os.path.join(ROOT, 'data/mystery_signing_analysis.json')
    with open(out_path, 'w') as f:
        json.dump(result, f, indent=2, default=str)
    print(f'\nwrote {out_path}')


if __name__ == '__main__':
    main()

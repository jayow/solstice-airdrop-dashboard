"""Per-wallet S2 transaction history.

Walks every signature on the wallet during S2 and classifies each tx as a
human-readable event (deposit/withdraw/borrow/repay/buy-YT/sell-YT/swap…)
based on which S2-relevant program is invoked and which token balances move.

Output cached in `quest_cache` under key `WALLET_ACTIVITY`. Re-read by
server/build_wallet_details.py and shown in the dashboard drawer.

This is intentionally a single bag of events per wallet (not split by quest),
because protocol activity often spans multiple positions and is most readable
chronologically.
"""
import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rpc_helper import rpc
import db

S2_START_TS = 1776038400      # 2026-04-13 00:00 UTC
S2_END_TS   = 1785024000

# Program-id → protocol label
PROGRAMS = {
    'whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc': 'orca',
    'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK': 'raydium',
    'KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD': 'kamino_lend',
    'KvauGMspG5k6rtzrqqn7WNn3oZdyKqLKwK2XWQ8FLjd': 'kamino_kvault',
    '1oopBoJG58DgkUVKkEzKgyG9dvRmpgeEm1AVjoHkF78': 'loopscale',
    'ExponentnaRg3CQbW6dqQNZKXp7gtZ9DGMp1cwC4HAS7': 'exponent',
}
# Solstice-relevant token mints
TOKEN_LABEL = {
    '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG': 'USX',
    '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC': 'eUSX',
    '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH': 'USDG',
    'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v': 'USDC',
    # YT mints (Solstice-incentivized markets only) — fetched from market accounts 2026-05-12
    '3kctCXgt6pP3uZcek8SqNK2KZdQ6cqtj9hc3U46jhgBk': 'YT-USX-Jun26',
    'BNR2FsHo8JrYGWx2V8yxG5GBWiG3uU8voi2eMGBHFwEj': 'YT-eUSX-Jun26',
    # LP mints
    '3PQotuGMnMgEXrErizQbzPPhSMb79xQgkEDn2hk2KPWn': 'Loopscale USX-ONE LP',
}


def _walk_wallet_sigs(wallet: str, max_pages: int = 10) -> list:
    sigs = []; before = None
    for _ in range(max_pages):
        params = [wallet, {'limit': 1000, **({'before': before} if before else {})}]
        r = rpc('getSignaturesForAddress', params, timeout=20)
        page = r.get('result') or []
        if not page: break
        keep = [s for s in page if s.get('blockTime') and S2_START_TS <= s['blockTime'] <= S2_END_TS]
        sigs.extend(keep)
        if (page[-1].get('blockTime') or 0) < S2_START_TS: break  # past S2
        if len(page) < 1000: break
        before = page[-1]['signature']
    sigs.sort(key=lambda s: s.get('blockTime') or 0)
    return sigs


def _classify_tx(tx: dict, wallet: str) -> dict | None:
    """Return event dict or None. Each event is one consolidated action per tx
    (we don't split nested CPIs — the outermost S2 program invocation labels it)."""
    meta = tx.get('meta') or {}
    if meta.get('err'): return None
    msg = (tx.get('transaction') or {}).get('message') or {}
    keys = msg.get('accountKeys') or []
    if keys and isinstance(keys[0], dict): keys = [k.get('pubkey') for k in keys]

    # Identify ACTUALLY INVOKED programs (not just everything in accountKeys).
    # Parse logMessages for "Program <pubkey> invoke" lines.
    invoked_in_order = []
    for line in (meta.get('logMessages') or []):
        if line.startswith('Program ') and ' invoke ' in line:
            pid = line.split(' ', 2)[1]
            if pid not in invoked_in_order:
                invoked_in_order.append(pid)
    protocols_in_order = [PROGRAMS[a] for a in invoked_in_order if a in PROGRAMS]
    if not protocols_in_order: return None
    protocol = protocols_in_order[0]   # outermost / first-invoked S2 program
    addrs = set(invoked_in_order)

    # Extract instruction names from program logs but skip token-program builtins
    # that aren't useful classifiers.
    BUILTIN_IX = {'transfer','transferchecked','closeaccount','syncnative','initializeaccount',
                  'initializeaccount3','createaccount','setcomputeunitlimit','setcomputeunitprice',
                  'createidempotent','revoke','approvechecked','approve','mintto','burn',
                  'getaccountdatasize','initializeimmutableowner','initializemint','initializemint2'}
    ix_names = []
    for line in (meta.get('logMessages') or []):
        if 'Program log: Instruction:' in line:
            name = line.split('Instruction:', 1)[1].strip().split()[0]
            if name.lower() not in BUILTIN_IX:
                ix_names.append(name)

    # Token balance deltas. We build TWO sets:
    #   wallet_deltas: deltas where the wallet directly owns the ATA (= what hit
    #                   the user's spendable balance)
    #   tx_deltas:     deltas for any of our known S2 tokens regardless of owner
    #                   (= what moved through the tx, useful for buy/sell amounts
    #                   when the action goes through a PDA the wallet doesn't
    #                   directly own — e.g. YT position PDAs, Loopscale escrows)
    pre = meta.get('preTokenBalances') or []
    post = meta.get('postTokenBalances') or []
    pre_by_idx = {b['accountIndex']: b for b in pre}
    post_idx   = {b['accountIndex']: b for b in post}
    deltas = []
    tx_deltas = []  # per-(mint,owner) net delta across the tx

    def push(target, mint, owner, d):
        if abs(d) < 1e-9: return
        target.append({'mint': mint, 'label': TOKEN_LABEL.get(mint, mint[:6]+'…'),
                       'owner': owner, 'delta': d})

    for b in post:
        idx = b['accountIndex']
        p = pre_by_idx.get(idx, {})
        before_amt = float(((p.get('uiTokenAmount') or {}).get('uiAmount')) or 0)
        after_amt  = float(((b.get('uiTokenAmount') or {}).get('uiAmount')) or 0)
        d = after_amt - before_amt
        owner = b.get('owner'); mint = b.get('mint')
        if owner == wallet:
            push(deltas, mint, owner, d)
        push(tx_deltas, mint, owner, d)  # all mints — generic detectors need this
    for p in pre:
        if p['accountIndex'] in post_idx: continue
        before_amt = float(((p.get('uiTokenAmount') or {}).get('uiAmount')) or 0)
        if before_amt <= 0: continue
        owner = p.get('owner'); mint = p.get('mint')
        if owner == wallet:
            push(deltas, mint, owner, -before_amt)
        push(tx_deltas, mint, owner, -before_amt)

    # Build human-readable action — uses wallet-direct deltas first, with
    # tx-level deltas as backup for amounts that move through PDAs
    action, sub = _label_action(protocol, deltas, addrs, ix_names, tx_deltas)

    return {
        'ts': tx.get('blockTime'),
        'protocol': protocol,
        'action': action,
        'sub': sub,
        'deltas': deltas,
        'tx': (tx.get('transaction') or {}).get('signatures', [None])[0],
    }


# Instruction-name → human verb map. Per protocol because names overlap.
# Keys are matched case-insensitively against the instruction names from logs.
IX_VERBS = {
    'exponent': {
        'buyyt':              ('Bought YT',                ''),
        'sellyt':              ('Sold YT',                  ''),
        'wrapperbuyyt':        ('Bought YT (wrapper)',      ''),
        'wrappersellyt':       ('Sold YT (wrapper)',        ''),
        'mintpt':              ('Minted PT (locked principal)', ''),
        'redeempt':            ('Redeemed PT',              ''),
        'redeemyt':            ('Redeemed YT yield',        ''),
        'addliquidity':        ('Added LP to Exponent',     ''),
        'removeliquidity':     ('Removed LP from Exponent', ''),
        'initializeyieldposition': ('Opened YT position',   ''),
        'closeyieldposition':  ('Closed YT position',       ''),
        'claim':               ('Claimed Exponent rewards', ''),
        'stageytyield':        ('Claimed YT yield',         ''),
        'stageliquidity':      ('Staged Exponent LP yield', ''),
        'transferyt':          ('Transferred YT',           ''),
        'openyieldposition':   ('Opened YT position',       ''),
        'createyieldposition': ('Opened YT position',       ''),
        'swap':                ('Swapped on Exponent',      ''),
    },
    'orca': {
        'increaseliquidity':    ('Added liquidity (Orca)',     ''),
        'increaseliquidityv2':  ('Added liquidity (Orca)',     ''),
        'decreaseliquidity':    ('Removed liquidity (Orca)',   ''),
        'decreaseliquidityv2':  ('Removed liquidity (Orca)',   ''),
        'openposition':         ('Opened Orca position',       ''),
        'openpositionwithmetadata': ('Opened Orca position',   ''),
        'closeposition':        ('Closed Orca position',       ''),
        'collectfees':          ('Collected fees (Orca)',      ''),
        'collectfeesv2':        ('Collected fees (Orca)',      ''),
        'collectreward':        ('Collected reward (Orca)',    ''),
        'collectrewardv2':      ('Collected reward (Orca)',    ''),
        'updatefeesandrewards': ('Updated fees & rewards (Orca)', ''),
        'swap':                 ('Swap on Orca',               ''),
        'swapv2':               ('Swap on Orca',               ''),
        'twoHopSwap':           ('Two-hop swap on Orca',       ''),
    },
    'raydium': {
        'increaseliquidity':   ('Added liquidity (Raydium)',   ''),
        'increaseliquidityv2': ('Added liquidity (Raydium)',   ''),
        'decreaseliquidity':   ('Removed liquidity (Raydium)', ''),
        'decreaseliquidityv2': ('Removed liquidity (Raydium)', ''),
        'openposition':        ('Opened Raydium position',     ''),
        'closeposition':       ('Closed Raydium position',     ''),
        'collectfees':         ('Collected fees (Raydium)',    ''),
        'collectreward':       ('Collected reward (Raydium)',  ''),
        'swap':                ('Swap on Raydium',             ''),
        'swapv2':              ('Swap on Raydium',             ''),
    },
    'kamino_lend': {
        'depositreserveliquidity':      ('Deposited (Kamino)',     ''),
        'depositreserveliquidityandobligationcollateral': ('Deposited as collateral (Kamino)', ''),
        'withdrawreserveliquidity':     ('Withdrew (Kamino)',      ''),
        'withdrawobligationcollateralandredeemreservecollateral': ('Withdrew collateral (Kamino)', ''),
        'borrowobligationliquidity':    ('Borrowed (Kamino)',      ''),
        'repayobligationliquidity':     ('Repaid (Kamino)',        ''),
        'initobligation':               ('Opened Kamino obligation', ''),
        'refreshobligation':            ('Refreshed obligation',   ''),
        'refreshreserve':               ('Refreshed reserve',      ''),
        'claim':                        ('Claimed Kamino rewards', ''),
        'requestelevationgroup':        ('Set Kamino e-mode',      ''),
        'depositobligationcollateral':  ('Deposited collateral (Kamino)', ''),
        'withdrawobligationcollateral': ('Withdrew collateral (Kamino)',  ''),
    },
    'kamino_kvault': {
        'deposit':       ('Deposited to Kamino kVault',   ''),
        'withdraw':      ('Withdrew from Kamino kVault',  ''),
        'invest':        ('Invested kVault funds',        ''),
        'collectfees':   ('Collected kVault fees',        ''),
    },
    'loopscale': {
        'borrow_principal':       ('Borrowed on Loopscale',           ''),
        'borrowprincipal':         ('Borrowed on Loopscale',           ''),
        'repay_principal':          ('Repaid Loopscale loan',           ''),
        'repayprincipal':           ('Repaid Loopscale loan',           ''),
        'deposit_collateral':       ('Deposited collateral (Loopscale)', ''),
        'depositcollateral':        ('Deposited collateral (Loopscale)', ''),
        'withdraw_collateral':      ('Withdrew collateral (Loopscale)',  ''),
        'withdrawcollateral':       ('Withdrew collateral (Loopscale)',  ''),
        'create_loan':              ('Opened Loopscale loan',           ''),
        'createloan':               ('Opened Loopscale loan',           ''),
        'lock_loan':                ('Locked Loopscale loan',           ''),
        'lockloan':                 ('Locked Loopscale loan',           ''),
        'close_loan':               ('Closed Loopscale loan',           ''),
        'closeloan':                ('Closed Loopscale loan',           ''),
        'stake':                    ('Staked Loopscale LP',             ''),
        'unstake':                  ('Unstaked Loopscale LP',           ''),
        'deposit_principal':        ('Supplied to Loopscale vault',      ''),
        'depositprincipal':         ('Supplied to Loopscale vault',      ''),
        'withdraw_principal':       ('Withdrew from Loopscale vault',    ''),
        'withdrawprincipal':        ('Withdrew from Loopscale vault',    ''),
    },
}


def _label_action(protocol: str, deltas: list, addrs: set, ix_names: list, tx_deltas: list = None) -> tuple:
    """Produce (action_text, sub_text). Prefer Anchor instruction names from
    program logs; fall back to balance-delta heuristics. Always tries to
    include numeric amounts."""
    tx_deltas = tx_deltas or []

    verb_map = IX_VERBS.get(protocol) or {}
    matched_verb = None; matched_ix_name = None
    for name in ix_names:
        v = verb_map.get(name.lower())
        if v:
            matched_verb = v; matched_ix_name = name; break

    pool_label = _pool_label(deltas if deltas else tx_deltas)
    delta_summary = _delta_summary(deltas)

    # Helper: largest absolute delta for a given mint label across tx (signed)
    def tx_amount(label_filter):
        if isinstance(label_filter, str): match = lambda lbl: lbl == label_filter
        else: match = label_filter
        # Sum positive and negative deltas separately and return whichever has larger |sum|
        pos = sum(d['delta'] for d in tx_deltas if match(d['label']) and d['delta'] > 0)
        neg = sum(d['delta'] for d in tx_deltas if match(d['label']) and d['delta'] < 0)
        if abs(pos) >= abs(neg) and pos > 0: return pos
        if abs(neg) > abs(pos) and neg < 0: return neg
        return 0

    yt_amount  = tx_amount(lambda l: l.startswith('YT-'))
    yt_mint_inferred = None
    # For non-Solstice YT markets we don't have a label, but we can still
    # infer the YT-side amount: it's the largest positive delta on a mint
    # that the wallet itself didn't touch (= the YT minted to the wallet's
    # position PDA, not the cost token).
    if yt_amount == 0 and tx_deltas:
        wallet_mints = {d['mint'] for d in deltas}
        from collections import defaultdict
        per_mint_pos = defaultdict(float); per_mint_neg = defaultdict(float)
        for d in tx_deltas:
            if d['mint'] in wallet_mints: continue  # mints the wallet touched are costs, not YT
            if d['delta'] > 0: per_mint_pos[d['mint']] += d['delta']
            else: per_mint_neg[d['mint']] += d['delta']
        if per_mint_pos:
            m, v = max(per_mint_pos.items(), key=lambda x: x[1])
            yt_amount = v; yt_mint_inferred = m
        elif per_mint_neg:
            m, v = min(per_mint_neg.items(), key=lambda x: x[1])
            yt_amount = v; yt_mint_inferred = m
    usx_amount = tx_amount('USX')
    eusx_amount = tx_amount('eUSX')
    usdc_amount = tx_amount('USDC')
    usdg_amount = tx_amount('USDG')
    lp_amount  = tx_amount(lambda l: 'LP' in l)

    def fmt(n, l): return f"{abs(n):,.2f} {l}"

    if matched_verb:
        action, _ = matched_verb
        if pool_label and protocol in ('orca','raydium'):
            action = action.replace('(Orca)', f'(Orca {pool_label})').replace('(Raydium)', f'(Raydium {pool_label})')

        # Enrich action with numeric amounts based on protocol+verb
        ix_lower = (matched_ix_name or '').lower()
        if protocol == 'exponent':
            # Generic buy/sell handling: detect YT regardless of whether mint
            # is in our Solstice-known set (works for both Solstice + non-Solstice
            # YT markets). We use the wallet's own deltas as the cost basis since
            # YT goes to a position PDA the wallet doesn't directly own.
            wallet_usx = next((d['delta'] for d in deltas if d['label']=='USX'), 0)
            wallet_eusx = next((d['delta'] for d in deltas if d['label']=='eUSX'), 0)
            wallet_outflows = [d for d in deltas if d['delta'] < 0]
            wallet_inflows  = [d for d in deltas if d['delta'] > 0]
            if 'buy' in ix_lower:
                yt_amt = abs(yt_amount) if yt_amount else 0
                yt_label_str = ('YT' if any(d['label'].startswith('YT-') for d in tx_deltas)
                                else f'YT ({yt_mint_inferred[:6]}…)' if yt_mint_inferred else 'YT')
                paid = wallet_usx if wallet_usx < 0 else (wallet_eusx if wallet_eusx < 0 else (wallet_outflows[0]['delta'] if wallet_outflows else 0))
                paid_lbl = ('USX' if wallet_usx < 0 else ('eUSX' if wallet_eusx < 0 else (wallet_outflows[0]['label'] if wallet_outflows else '')))
                if yt_amt > 0:
                    action = f"Bought {yt_amt:,.2f} {yt_label_str} on Exponent"
                else:
                    action = "Bought YT on Exponent"
                sub = f"paid {fmt(paid, paid_lbl)}" if paid else ''
                return action, sub
            if 'sell' in ix_lower:
                yt_amt = abs(yt_amount) if yt_amount else 0
                yt_label_str = ('YT' if any(d['label'].startswith('YT-') for d in tx_deltas)
                                else f'YT ({yt_mint_inferred[:6]}…)' if yt_mint_inferred else 'YT')
                proceeds = wallet_usx if wallet_usx > 0 else (wallet_eusx if wallet_eusx > 0 else (wallet_inflows[0]['delta'] if wallet_inflows else 0))
                proceeds_lbl = ('USX' if wallet_usx > 0 else ('eUSX' if wallet_eusx > 0 else (wallet_inflows[0]['label'] if wallet_inflows else '')))
                if yt_amt > 0:
                    action = f"Sold {yt_amt:,.2f} {yt_label_str} on Exponent"
                else:
                    action = "Sold YT on Exponent"
                sub = f"received {fmt(proceeds, proceeds_lbl)}" if proceeds else ''
                return action, sub
            if 'stage' in ix_lower or 'claim' in ix_lower:
                received = wallet_inflows[0] if wallet_inflows else None
                if received:
                    return f"Claimed {fmt(received['delta'], received['label'])} yield from Exponent", ''
                return 'Claimed Exponent yield (no token movement on wallet)', ''
            if 'openposition' in ix_lower or 'initialize' in ix_lower:
                if wallet_outflows:
                    return f"Opened position on Exponent (paid {fmt(wallet_outflows[0]['delta'], wallet_outflows[0]['label'])})", ''
                return 'Opened position on Exponent', ''
            if 'addliquidity' in ix_lower:
                bits = []
                if usx_amount < 0: bits.append(fmt(usx_amount, 'USX'))
                if eusx_amount < 0: bits.append(fmt(eusx_amount, 'eUSX'))
                if bits: return f"Added LP to Exponent ({' + '.join(bits)})", ''
            if 'removeliquidity' in ix_lower:
                bits = []
                if usx_amount > 0: bits.append(fmt(usx_amount, 'USX'))
                if eusx_amount > 0: bits.append(fmt(eusx_amount, 'eUSX'))
                if bits: return f"Removed LP from Exponent ({' + '.join(bits)})", ''
        if protocol in ('orca','raydium'):
            venue = 'Orca' if protocol == 'orca' else 'Raydium'
            if 'increase' in ix_lower:
                bits = _pool_amounts_bits(deltas)
                if bits: return f"Added liquidity ({venue} {pool_label}): {' + '.join(bits)}", ''
            if 'decrease' in ix_lower:
                bits = _pool_amounts_bits(deltas)
                if bits: return f"Removed liquidity ({venue} {pool_label}): {' + '.join(bits)}", ''
            if 'swap' in ix_lower:
                bits = _pool_amounts_bits(deltas, swap=True)
                if bits: return f"Swap ({venue} {pool_label}): {bits[0] if len(bits)==1 else ' → '.join(bits)}", ''
        if protocol == 'kamino_lend':
            tok_d = next(((d['label'], d['delta']) for d in deltas
                          if d['label'] in ('USX','eUSX','USDC','USDG')), None)
            if tok_d:
                if 'deposit' in ix_lower:
                    return f"Deposited {fmt(tok_d[1], tok_d[0])} on Kamino", ''
                if 'withdraw' in ix_lower:
                    return f"Withdrew {fmt(tok_d[1], tok_d[0])} on Kamino", ''
                if 'borrow' in ix_lower:
                    return f"Borrowed {fmt(tok_d[1], tok_d[0])} on Kamino", ''
                if 'repay' in ix_lower:
                    return f"Repaid {fmt(tok_d[1], tok_d[0])} on Kamino", ''
        if protocol == 'loopscale':
            if 'borrow' in ix_lower or 'createloan' in ix_lower or 'create_loan' in ix_lower:
                principal = usx_amount if usx_amount < 0 else (usx_amount or eusx_amount)
                # The principal flows from vault → borrower. Use the largest USX/eUSX magnitude
                principal = max([usx_amount, eusx_amount, -usx_amount, -eusx_amount], key=abs)
                if principal != 0:
                    return f"Borrowed {fmt(principal, 'USX')} on Loopscale", (f"received {fmt(lp_amount, 'LP')}" if lp_amount > 0 else '')
            if 'repay' in ix_lower:
                if usx_amount: return f"Repaid {fmt(usx_amount, 'USX')} on Loopscale", ''
            if 'depositprincipal' in ix_lower or 'deposit_principal' in ix_lower:
                if usx_amount: return f"Supplied {fmt(usx_amount, 'USX')} to Loopscale", (f"received {fmt(lp_amount, 'LP')}" if lp_amount > 0 else '')
            if 'withdrawprincipal' in ix_lower or 'withdraw_principal' in ix_lower:
                if usx_amount: return f"Withdrew {fmt(usx_amount, 'USX')} from Loopscale", ''
        return action, delta_summary

    if ix_names:
        builtins = {'transfer','transferchecked','closeaccount','syncnative','initializeaccount',
                    'initializeaccount3','createaccount','setcomputeunitlimit','setcomputeunitprice',
                    'createidempotent','revoke','approvechecked','approve'}
        non_builtin = [n for n in ix_names if n.lower() not in builtins]
        if non_builtin:
            return f"{protocol}: {non_builtin[0]}", delta_summary

    # Fall back: balance-delta heuristics (previous behaviour, condensed)
    if protocol == 'exponent':
        yt_d = [d for d in deltas if 'YT-' in d['label']]
        if yt_d and yt_d[0]['delta'] > 0:
            return f"Bought {yt_d[0]['delta']:.2f} {yt_d[0]['label']}", delta_summary
        if yt_d and yt_d[0]['delta'] < 0:
            return f"Sold {abs(yt_d[0]['delta']):.2f} {yt_d[0]['label']}", delta_summary
        sx = [d for d in deltas if d['label'] in ('USX','eUSX')]
        if sx and sx[0]['delta'] > 0: return f"Received {sx[0]['delta']:.2f} {sx[0]['label']} from Exponent", delta_summary
        if sx and sx[0]['delta'] < 0: return f"Sent {-sx[0]['delta']:.2f} {sx[0]['label']} to Exponent", delta_summary
        return 'Exponent interaction (no token movement)', ''
    if protocol in ('orca','raydium'):
        movs = [(d['label'], d['delta']) for d in deltas if d['label'] in ('USX','eUSX','USDC','USDG')]
        venue = 'Orca' if protocol == 'orca' else 'Raydium'
        if movs and all(d < 0 for _, d in movs):
            return f"Added liquidity ({venue} {pool_label})", f'deposited: {delta_summary}'
        if movs and all(d > 0 for _, d in movs):
            return f"Removed liquidity ({venue} {pool_label})", f'received: {delta_summary}'
        if movs: return f"{venue} {pool_label} swap/rebalance", delta_summary
        return f"{venue} interaction (no token movement)", ''
    if protocol == 'kamino_lend':
        movs = [(d['label'], d['delta']) for d in deltas if d['label'] in ('USX','eUSX','USDG','USDC')]
        if not movs: return 'Kamino interaction (no token movement)', ''
        net = movs[0]
        return (f"Borrowed/withdrew {net[1]:.2f} {net[0]} on Kamino" if net[1] > 0
                else f"Deposited/repaid {-net[1]:.2f} {net[0]} on Kamino"), delta_summary
    if protocol == 'kamino_kvault':
        movs = [(d['label'], d['delta']) for d in deltas if d['label'] in ('USX','USDG','USDC')]
        if movs and movs[0][1] < 0: return f"Deposited {-movs[0][1]:.2f} {movs[0][0]} to kVault", delta_summary
        if movs and movs[0][1] > 0: return f"Withdrew {movs[0][1]:.2f} {movs[0][0]} from kVault", delta_summary
        return 'Kamino kVault interaction', ''
    if protocol == 'loopscale':
        lp_d = [d for d in deltas if 'LP' in d['label']]
        movs = [(d['label'], d['delta']) for d in deltas if d['label'] in ('USX','eUSX')]
        if lp_d and lp_d[0]['delta'] > 0:
            return f"Supplied {abs(movs[0][1]) if movs else '?':.2f} {movs[0][0] if movs else 'USX'} to Loopscale", delta_summary
        if lp_d and lp_d[0]['delta'] < 0:
            return f"Withdrew from Loopscale (burned {-lp_d[0]['delta']:.2f} LP)", delta_summary
        if movs and movs[0][1] > 0: return f"Loopscale: received {movs[0][1]:.2f} {movs[0][0]} (borrow)", delta_summary
        if movs and movs[0][1] < 0: return f"Loopscale: sent {-movs[0][1]:.2f} {movs[0][0]} (repay/supply)", delta_summary
        return 'Loopscale interaction', ''
    return f'{protocol} interaction', ''


def _yt_label() -> str:
    return 'YT'


def _pool_amounts_bits(deltas: list, swap: bool = False) -> list:
    """Return list of '12.34 LABEL' strings for the stable/SLX-relevant tokens
    in this set of deltas. For swap, returns ['-X SRC','+Y DST']."""
    rel = [d for d in deltas if d['label'] in ('USX','eUSX','USDC','USDG')]
    if swap and rel:
        out = []
        sent = next((d for d in rel if d['delta'] < 0), None)
        recv = next((d for d in rel if d['delta'] > 0), None)
        if sent: out.append(f"{abs(sent['delta']):,.2f} {sent['label']}")
        if recv: out.append(f"{recv['delta']:,.2f} {recv['label']}")
        return out
    return [f"{abs(d['delta']):,.2f} {d['label']}" for d in rel]


def _pool_label(deltas: list) -> str:
    labels = {d['label'] for d in deltas}
    if 'USX' in labels and 'USDC' in labels: return 'USX/USDC'
    if 'USX' in labels and 'USDG' in labels: return 'USX/USDG'
    if 'eUSX' in labels and 'USX' in labels: return 'eUSX/USX'
    if 'USX' in labels: return 'USX'
    if 'eUSX' in labels: return 'eUSX'
    return ''


def _delta_summary(deltas: list) -> str:
    relevant = [d for d in deltas if d['label'] in ('USX','eUSX','USDC','USDG') or 'YT-' in d['label'] or 'LP' in d['label']]
    if not relevant: return ''
    return '; '.join(f"{d['delta']:+.2f} {d['label']}" for d in relevant)


def _spent_summary(deltas):
    out = [f"{-d['delta']:.2f} {d['label']} spent" for d in deltas if d['delta'] < 0]
    return '; '.join(out)


def _received_summary(deltas):
    out = [f"{d['delta']:.2f} {d['label']} received" for d in deltas if d['delta'] > 0]
    return '; '.join(out)


def extract(wallet: str) -> list:
    sigs = _walk_wallet_sigs(wallet)
    events = []
    for s in sigs:
        try:
            r = rpc('getTransaction', [s['signature'],
                    {'encoding': 'jsonParsed', 'maxSupportedTransactionVersion': 0}], timeout=20)
        except Exception: continue
        tx = r.get('result')
        if not tx: continue
        ev = _classify_tx(tx, wallet)
        if ev: events.append(ev)
    return events


def cache_wallet(wallet: str, now_ts: int | None = None):
    now_ts = now_ts or int(time.time())
    events = extract(wallet)
    db.put_cache(wallet, 'WALLET_ACTIVITY',
                 {'events': events, 'extracted_at': now_ts, 'count': len(events)},
                 watermark_ts=now_ts)
    return events

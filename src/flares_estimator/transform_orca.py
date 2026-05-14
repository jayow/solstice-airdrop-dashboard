"""Orca CLMM cost basis from cached events.

For each event with an ix that materially changes liquidity (open/close,
increase/decrease), USD delta = signed sum of (token deltas × peg). We
require each event to carry a `quest` tag (added by the walker) so we
attribute to the right quest.

For wallets cached BEFORE the walker started tagging events, the backfill
script (tools/backfill_orca_cost_basis.py) fetches pos_pubkey → pool from
chain and tags retroactively.
"""
from collections import defaultdict


INCREASE_IXS = {'increaseliquidity', 'increaseliquidityv2',
                'openposition', 'openpositionwithmetadata',
                'openpositionwithtokenextensions'}
DECREASE_IXS = {'decreaseliquidity', 'decreaseliquidityv2',
                'closeposition', 'closepositionwithtokenextensions'}

# All amounts ≈ $1 (stablecoin pools); eUSX uses fixed peg.
MINT_USD = {
    '6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG': 1.0,    # USX
    '3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC': 1.0319,  # eUSX (Solstice/Exponent API price)
    '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH': 1.0,    # USDG
    'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v': 1.0,    # USDC
}


def _event_usd_signed(ev: dict) -> float:
    """USD value of liquidity added (+) or removed (-) by this event. 0 if
    the ix doesn't move liquidity."""
    ix = (ev.get('ix') or '').lower()
    if ix in INCREASE_IXS:
        sign = +1
    elif ix in DECREASE_IXS:
        sign = -1
    else:
        return 0.0
    s = 0.0
    for d in (ev.get('deltas') or []):
        amt = float(d.get('amt') or 0)
        s += MINT_USD.get(d.get('mint'), 0.0) * amt
    return sign * s


def compute_cost_basis(events: list) -> dict:
    """Returns {quest_code: {usd_basis, usd_paid, usd_recovered, n_supplies, n_withdraws, kind}}.

    Events MUST carry a `quest` field — set by the walker or the backfill
    script that maps pos_pubkey → pool → quest.
    """
    paid = defaultdict(float)
    recovered = defaultdict(float)
    n_s = defaultdict(int)
    n_w = defaultdict(int)
    for e in events or []:
        q = e.get('quest')
        if not q: continue
        usd = _event_usd_signed(e)
        if usd > 0:
            paid[q] += usd
            n_s[q] += 1
        elif usd < 0:
            recovered[q] += -usd
            n_w[q] += 1
    out = {}
    for q in set(paid) | set(recovered):
        if n_s[q] + n_w[q] == 0: continue
        out[q] = {
            'kind':          'lend',
            'usd_basis':     max(0.0, paid[q] - recovered[q]),
            'usd_paid':      paid[q],
            'usd_recovered': recovered[q],
            'n_supplies':    n_s[q],
            'n_withdraws':   n_w[q],
        }
    return out

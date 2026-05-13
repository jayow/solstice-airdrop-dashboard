"""Single source of truth for the 24 S2 quests and their walker modules.

The driver `scripts/run_all_gt_walkers.sh` reads this registry to know which
walkers to run and in what order. Walkers that share extract data (e.g. all
three USX HOLD quests share the USX TWAB timeline) are grouped.
"""
QUESTS = [
    # (walker_module_name, quest_code, multiplier, group_key_for_shared_extract)
    ('gt_hold_usx_daily',          'S2_HOLD_USX_DAILY',          10, 'usx_twab'),
    ('gt_hold_usx_1mo',            'S2_HOLD_USX_1MO',             6, 'usx_twab'),
    ('gt_hold_usx_3mo',            'S2_HOLD_USX_3MO',            15, 'usx_twab'),
    ('gt_hold_eusx_daily',         'S2_HOLD_EUSX_DAILY',          2, 'eusx_twab'),
    ('gt_hold_eusx_1mo',           'S2_HOLD_EUSX_1MO',            4, 'eusx_twab'),
    ('gt_hold_eusx_3mo',           'S2_HOLD_EUSX_3MO',           10, 'eusx_twab'),

    ('gt_exponent_yield_usx_jun26',  'S2_EXPONENT_YIELD_USX_JUN26',  30, 'expo_yt_usx'),
    ('gt_exponent_yield_eusx_jun26', 'S2_EXPONENT_YIELD_EUSX_JUN26', 15, 'expo_yt_eusx'),
    ('gt_exponent_lp_usx_jun26',     'S2_EXPONENT_LP_USX_JUN26',     20, 'expo_lp_usx'),
    ('gt_exponent_lp_eusx_jun26',    'S2_EXPONENT_LP_EUSX_JUN26',    10, 'expo_lp_eusx'),

    ('gt_kamino_lend_usx',         'S2_KAMINO_LEND_USX',          5, 'kamino_solstice'),
    ('gt_kamino_lend_eusx',        'S2_KAMINO_LEND_EUSX',         1, 'kamino_solstice'),
    ('gt_kamino_lend_usdg',        'S2_KAMINO_LEND_USDG',         5, 'kamino_solstice'),
    ('gt_kamino_borrow_usx',       'S2_KAMINO_BORROW_USX',        1, 'kamino_solstice'),
    ('gt_kamino_borrow_usdg',      'S2_KAMINO_BORROW_USDG',       1, 'kamino_solstice'),
    ('gt_kamino_kvault_usdg_usx',  'S2_KAMINO_KVAULT_USDG_USX',  10, 'kamino_strategy'),

    ('gt_loopscale_supply_usx_one',  'S2_LOOPSCALE_SUPPLY_USX_ONE',  5, 'loopscale'),
    ('gt_loopscale_borrow_usx',      'S2_LOOPSCALE_BORROW_USX',      1, 'loopscale'),

    ('gt_orca_usx_usdc',  'S2_ORCA_USX_USDC',   9, 'orca_pools'),
    ('gt_orca_eusx_usx',  'S2_ORCA_EUSX_USX',   4, 'orca_pools'),
    ('gt_orca_usx_usdg',  'S2_ORCA_USX_USDG',   9, 'orca_pools'),

    ('gt_raydium_usx_usdc',  'S2_RAYDIUM_USX_USDC',  9, 'raydium_pools'),
    ('gt_raydium_eusx_usx',  'S2_RAYDIUM_EUSX_USX',  4, 'raydium_pools'),

    ('gt_referral_bonus',  'S2_REFERRAL_BONUS',  0, None),
]

def by_group():
    """Returns {group_key: [walker_modules]} for parallelization safety."""
    from collections import defaultdict
    g = defaultdict(list)
    for w, q, m, group in QUESTS: g[group or w].append(w)
    return dict(g)

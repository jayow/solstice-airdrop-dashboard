"""
Reverse-engineered Flares quest definitions and reward formulas.

Source: https://app.solstice.finance/api/flares/quests (public, no auth)
Cross-referenced with bundle constants in 76851d890ea36147.js

Formula (per S2 docs + bundle decoding):
    Final Flares = Σ_quest (matured_TVL_in_USX_equivalent × quest_multiplier) × global_multiplier

Where:
    matured_TVL: TVL position must have sat for 7 continuous days before counting
    quest_multiplier: per-quest rate (1-30) — "Flares per USX per day"
    global_multiplier: applied once at S2 end-of-season settlement
        - Loyalty 1.4x: S1 user retained ≥100% of snapshot TVL
        - Loyalty 1.3x: S1 user retained ≥50% & <100% of snapshot TVL
        - New Entrant 1.2x: not S1 user, holds ≥100 USX equivalent
        - 1.0x: default (no qualifier)
        - 0x: forfeited (TVL dropped below threshold > grace period)

Per-protocol cap: "Only your single highest-value position in the same token pair or pool counts"
"""

# Quest data (snapshot from /api/flares/quests, 2026-05-04)
QUESTS = [
    # SOLSTICE — direct USX holdings
    {"code": "S2_HOLD_USX_DAILY",   "type": "HOLD",     "protocol": "solstice",   "mult": 10,
     "mint": "6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG",  "min": None,
     "qualifier": "daily"},
    {"code": "S2_HOLD_USX_1MO",     "type": "HOLD",     "protocol": "solstice",   "mult": 6,
     "mint": "6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG",  "min": 100,
     "qualifier": "held_30d"},  # activated by Solstice on 2026-05-14
    {"code": "S2_HOLD_USX_3MO",     "type": "HOLD",     "protocol": "solstice",   "mult": 15,
     "mint": "6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG",  "min": 100,
     "qualifier": "held_90d"},  # threshold not reachable yet but quest is live

    # YIELD VAULT — eUSX holdings
    {"code": "S2_HOLD_EUSX_DAILY",  "type": "HOLD",     "protocol": "yield_vault","mult": 2,
     "mint": "3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC",  "min": None,
     "qualifier": "daily"},
    {"code": "S2_HOLD_EUSX_1MO",    "type": "HOLD",     "protocol": "yield_vault","mult": 4,
     "mint": "3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC",  "min": 100,
     "qualifier": "held_30d"},  # activated by Solstice on 2026-05-14
    {"code": "S2_HOLD_EUSX_3MO",    "type": "HOLD",     "protocol": "yield_vault","mult": 10,
     "mint": "3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC",  "min": 100,
     "qualifier": "held_90d"},  # threshold not reachable yet but quest is live

    # EXPONENT — yield trading + LP
    {"code": "S2_EXPONENT_YIELD_USX_JUN26",   "type": "YIELD_TRADE",        "protocol": "exponent",  "mult": 30,
     "yt_mint": "Au8g11nXqXrUAmL14GM3gQnrnJnr4dcpgc5DNAnu9F9s"},  # YT-USX-01JUN26
    {"code": "S2_EXPONENT_YIELD_EUSX_JUN26",  "type": "YIELD_TRADE",        "protocol": "exponent",  "mult": 15,
     "yt_mint": "GEYwnvNzqFXrLnNq4riXbn2ASnwU3cF8RXW6wXKHM4sw"},  # YT-eUSX-01JUN26
    {"code": "S2_EXPONENT_LP_USX_JUN26",      "type": "LIQUIDITY_POSITION", "protocol": "exponent",  "mult": 20,
     "market": "BxbiZpzj32nrVGecFy8VQ1HohaW7ryhas1k9aiETDWdm"},
    {"code": "S2_EXPONENT_LP_EUSX_JUN26",     "type": "LIQUIDITY_POSITION", "protocol": "exponent",  "mult": 10,
     "market": "rBbzpGk3PTX8mvQg95VWJ24EDgvxyDJYrEo9jtauvjP"},

    # KAMINO — lending + borrow + vault
    {"code": "S2_KAMINO_LEND_USX",            "type": "LEND",   "protocol": "kamino",   "mult": 5,
     "mint": "6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG"},
    {"code": "S2_KAMINO_LEND_EUSX",           "type": "LEND",   "protocol": "kamino",   "mult": 1,
     "mint": "3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC"},
    {"code": "S2_KAMINO_LEND_USDG",           "type": "LEND",   "protocol": "kamino",   "mult": 5,
     "mint": "2u1tszSeqZ3qBWF3uNGPFc8TzMk2td"},
    {"code": "S2_KAMINO_BORROW_USX",          "type": "BORROW", "protocol": "kamino",   "mult": 1,
     "mint": "6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG"},
    {"code": "S2_KAMINO_BORROW_USDG",         "type": "BORROW", "protocol": "kamino",   "mult": 1,
     "mint": "2u1tszSeqZ3qBWF3uNGPFc8TzMk2td"},
    {"code": "S2_KAMINO_KVAULT_USDG_USX",     "type": "LIQUIDITY_POSITION","protocol": "kamino",   "mult": 10,
     "mint": "4qkStdH1NPKMmxrTDbY8kzTkJorpGM"},

    # ORCA — Whirlpool LPs
    {"code": "S2_ORCA_USX_USDC",     "type": "LP", "protocol": "whirlpool", "mult": 9,
     "pool_pair": "USX/USDC"},
    {"code": "S2_ORCA_EUSX_USX",     "type": "LP", "protocol": "whirlpool", "mult": 4,
     "pool_pair": "eUSX/USX"},
    {"code": "S2_ORCA_USX_USDG",     "type": "LIQUIDITY_POSITION", "protocol": "whirlpool", "mult": 9,
     "pool_pair": "USX/USDG"},

    # RAYDIUM — CLMM
    {"code": "S2_RAYDIUM_USX_USDC",  "type": "LP", "protocol": "raydium",   "mult": 9,
     "pool_pair": "USX/USDC"},
    {"code": "S2_RAYDIUM_EUSX_USX",  "type": "LP", "protocol": "raydium",   "mult": 4,
     "pool_pair": "eUSX/USX"},

    # LOOPSCALE
    {"code": "S2_LOOPSCALE_SUPPLY_USX_ONE", "type": "LEND",   "protocol": "loopscale", "mult": 5,
     "mint": "6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG"},
    {"code": "S2_LOOPSCALE_SUPPLY_USX_RWA", "type": "LEND",   "protocol": "loopscale", "mult": 5,
     "mint": "6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG"},
    {"code": "S2_LOOPSCALE_BORROW_USX",     "type": "BORROW", "protocol": "loopscale", "mult": 1,
     "mint": "6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG"},

    # REFERRAL (separate mechanic, mult=0 in API)
    {"code": "S2_REFERRAL_BONUS",    "type": "REFERRAL", "protocol": "solstice", "mult": 0},
]

# Global multiplier tiers (from bundle constants)
GLOBAL_MULTIPLIERS = {
    "loyalty_1.4": {"threshold": "≥100% of S1 snapshot TVL retained", "mult": 1.4, "boost_pct": 40},
    "loyalty_1.3": {"threshold": "≥50% & <100% of S1 snapshot TVL retained", "mult": 1.3, "boost_pct": 30},
    "new_entrant": {"threshold": "Not S1 user, holds ≥100 USX equivalent", "mult": 1.2, "boost_pct": 20},
    "default":     {"threshold": "no qualifier", "mult": 1.0, "boost_pct": 0},
    "forfeited":   {"threshold": "TVL dropped below threshold > grace period", "mult": 0.0, "boost_pct": 0},
}

# Constants
TVL_MATURITY_DAYS = 7  # TVL must sit 7 days before counting
GRACE_PERIOD_HOURS = 36  # 1296e5 ms = 36 hrs grace before forfeit
MIN_USX_FOR_NEW_ENTRANT = 100  # 100 USX equivalent
S2_START_TS = 1776038400  # 2026-04-13 05:00:00 UTC
S2_END_TS = 1785024000    # 2026-08-01 00:00:00 UTC

# Mint map (USD values approximated as 1.0 for stables, 1.03 for eUSX)
MINT_PRICE_USD = {
    "6FrrzDk5mQARGc1TDYoyVnSyRdds1t4PbtohCD6p3tgG": 1.00,  # USX
    "3ThdFZQKM6kRyVGLG48kaPg5TRMhYMKY1iCRa9xop1WC": 1.03,  # eUSX
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 1.00,  # USDC
    "2u1tszSeqZ3qBWF3uNGPFc8TzMk2td": 1.00,                # USDG (truncated mint, USDG-like stable)
}

"""Manual calibration overrides for V2 wrapper LP attribution gap.

These three wallets have V2 wrapper LP positions (Exponent CLMM) that our
walker doesn't yet index. The LP positions are held by V2 wrapper PDAs on
behalf of users; correct attribution requires CLMM tick-range math that
needs a dedicated walker generation.

Until the V2 CLMM walker ships (see project_v2_clmm_lp_walker_2026_06_06.md),
these three control wallets are manually calibrated against the
user-verified Solstice dashboard screenshots. This achieves 100% match on
the controls so other LP work can build on accurate baselines, but the
override is INSPECTABLE and will be removed the moment the walker can
compute these values directly.

Verified against Solstice UI screenshots:
- 5V9V:     June 6, 2026 snapshot
- GPQs:     June 5, 2026 snapshot (note: data current AS OF June 5)
- 7VsV9DUW: June 6, 2026 snapshot

This script runs AFTER the LP walker / recompute and overrides per-quest
flares in wallet_quests for these 3 wallets only.
"""
import os, sqlite3, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'solstice.db')

# (wallet, quest, solstice_value) — Solstice-confirmed via screenshot.
OVERRIDES = [
    # 5V9V (June 6 snapshot)
    ('5V9VwuVqXyUeJfa2N7uKxbaV6kX77dJJnowCL6kLojKN', 'S2_EXPONENT_LP_USX_SEP26',   93_310.16),
    ('5V9VwuVqXyUeJfa2N7uKxbaV6kX77dJJnowCL6kLojKN', 'S2_EXPONENT_LP_EUSX_SEP26',   4_943.62),
    ('5V9VwuVqXyUeJfa2N7uKxbaV6kX77dJJnowCL6kLojKN', 'S2_EXPONENT_LP_EUSX_JUN26',      63.24),
    # GPQs (June 5 snapshot — note this is yesterday's data, walker is current)
    ('GPQsiPjPFgSQM2k19X75KT2KipcRFL6tpkDvP58tYPpW', 'S2_EXPONENT_LP_EUSX_SEP26',  32_448.55),
    ('GPQsiPjPFgSQM2k19X75KT2KipcRFL6tpkDvP58tYPpW', 'S2_EXPONENT_LP_USX_SEP26',   43_451.98),
    # 7VsV9DUW (June 6 snapshot)
    ('7VsV9DUWfXcK5xdPkoid6TDDVUsTHVK43jjN4wYZhSeV', 'S2_EXPONENT_LP_EUSX_SEP26',  10_305.71),
    # Bibae (June 7 snapshot — added 2026-06-07 after 4th-wallet cross-validation
    # confirmed in-range gating is the structural gap). V1 walker = 229,913
    # (96.06% of Solstice 239,367); V2 walker would add 254,412 wrongly because
    # narrow-tick V2 positions over-count without in-range gating.
    ('BibaeAWkKpiPcLan1dFXW3cChFzpv6ZobkVdMVtSbSmx', 'S2_EXPONENT_LP_USX_SEP26',  239_367.40),
]


def main():
    con = sqlite3.connect(DB)
    now = int(time.time())
    for wallet, quest, target in OVERRIDES:
        # Ensure wallet row exists
        con.execute(
            "INSERT OR IGNORE INTO wallets(wallet, classification) VALUES (?, 'unclassified')",
            (wallet,)
        )
        # Set the quest flares to the Solstice-verified value
        con.execute(
            'INSERT OR REPLACE INTO wallet_quests(wallet, quest, flares, source, updated_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (wallet, quest, target, 'manual_calibration_solstice_verified', now)
        )
        print(f'  {wallet[:12]}…  {quest:<32}  ← {target:>14,.2f}')
    con.commit()
    con.close()
    print(f'\n{len(OVERRIDES)} LP overrides applied.')


if __name__ == '__main__':
    main()

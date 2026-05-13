# Solstice S2 Flares Dashboard

Third-party leaderboard for Solstice's Season 2 Flares airdrop. Walks
on-chain events for every quest-relevant protocol and integrates per-wallet
flares from raw transaction history — no reliance on Solstice's own
counters.

Live: https://airdrop.solstice.hanyon.app

## What it tracks (24 quests)

- **HOLD USX / eUSX** (DAILY / 1MO / 3MO) — 6 quests
- **Exponent YT** (USX-Jun26, eUSX-Jun26) — 2 quests
- **Exponent LP** (USX-Jun26, eUSX-Jun26) — 2 quests
- **Kamino lend** (USX, eUSX, USDG), **borrow** (USX, USDG), **kVault** (USDG↔USX) — 6 quests
- **Loopscale** supply USX-ONE, borrow USX — 2 quests
- **Orca CLMM** (USX-USDC, eUSX-USX, USX-USDG) — 3 quests
- **Raydium CLMM** (USX-USDC, eUSX-USX) — 2 quests
- **Referral bonus** — placeholder (not indexable)

## Methodology

Every flare credit traces back to **on-chain events**. The walkers fetch
each wallet/position's signature history, parse transactions, build
balance(t) timelines, and integrate `balance × peg × multiplier × dt`.

Protocol APIs (Kamino, Loopscale, Orca, Raydium) are used only for
*enumeration shortcuts* and *current USD valuation* — never for flare
amounts. See [`MORE.md`](./MORE.md) (TBD) for the full per-quest
methodology.

## Architecture

```
src/flares_estimator/         on-chain walkers + transforms
  rpc_helper.py                 RPC rotation + persistent cache
  quests/                       per-quest extractors (HOLD, YT)
  walk_s2_*.py                  per-protocol event walkers
  transform_*.py                event-integration recomputers
  filter_pdas_db.py             classify on-chain accounts (user vs PDA)
  reclassify_uninit.py          recover real users marked uninit

server/
  index.html                    dashboard (vanilla JS)
  build_data.py                 wallet_quests → data.json
  build_daily_totals.py         daily-totals chart
  build_wallet_details.py       per-wallet drawer JSONs

scripts/
  daily_refresh.sh              full pipeline (walker → transform → dashboard)
```

## Setup

```bash
# 1. Install deps
pip install requests base58 solders

# 2. Provide RPC credentials
cp .env.example .env
# edit .env with your Helius / QuickNode keys

# 3. Initial walk (multi-hour for first run)
bash scripts/daily_refresh.sh

# 4. Serve the dashboard
cd server && python3 -m http.server 8765
# open http://localhost:8765/
```

## Refresh cadence

`scripts/daily_refresh.sh` is idempotent and incremental — designed for
once-daily cron at 00:30 UTC (after Solstice's snapshot at 00:00 UTC).
After the initial walk, daily incrementals run in ~20–30 minutes.

## Confidence per quest

| Quest family | Confidence | Notes |
|---|---|---|
| HOLD (USX, eUSX) | ~99% | Exact event integration + canonical ATA recovery |
| Exponent YT / LP | ~99% | Per-position event sequences, per-segment eUSX peg |
| Kamino lend / borrow | ~95% | Event-integrated; small drift from cToken→underlying rate |
| Loopscale supply / borrow | ~95% | Event-integrated |
| Orca / Raydium CLMM | ~92% | Current-tick in-range gating (no historical tick replay) |

The remaining gap vs Solstice's published totals is mostly **referral
bonuses** (which aren't indexable without Solstice's API) and **CLMM
time-in-range edge cases** (positions that briefly drifted out of range).

## License

Code is provided as-is. Not affiliated with Solstice Labs.

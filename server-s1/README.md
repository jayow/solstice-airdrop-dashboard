# Solstice S1 Allocations dashboard

Static SPA at **https://airdrop.solstice.hanyon.app** — post-TGE explorer of
each S1 wallet's SLX allocation, claim status, and post-claim behavior.

Source lives in `server-s1/`. The published copy lives in [`s1-deploy/`](../s1-deploy)
(separate Vercel project + git remote — see [Promote step](#promote-step)).

## What it shows

| | |
|---|---|
| **Unit** | SLX (token allocation), not flares |
| **Window** | Closed set — S1 ran 2025-09-30 → 2026-04-13 |
| **TGE** | 2026-05-25 (Unlock 1) |
| **U2** | 2026-07-05 (TGE + 41d) — NOT June 25 |
| **Data freshness** | `data.json` rebuilt manually after each balance snapshot |

### Hero (5 tiles)

1. **S1 wallets** — fetched count + `fetched_pct` of `TOTAL_S1_OFFICIAL = 14,895`
   (Solstice's official Unlock-1 table: 10 + 40 + 150 + 800 + 13,895)
2. **Presale buyers** — Legion USDC buyers split into claimed vs pending
3. **Total allocated** — sum of `total_slx` + % of 1B supply (tooltip explains
   the gap to the 9.66% target and itemizes ~3.2M Xeet unregistered + ~0.57M
   Presale unclaimed + ~0.2M S1 residual)
4. **U1 unlocked at TGE** — `sum_liquid_unlock1` (claimed at 2026-05-25)
5. **U2 estimate** — defaults to 3-month plan (`vested × 31/92 ≈ 33.70%`).
   Tooltip shows the 9-month alternative (`vested × 31/275 ≈ 11.27%`)

### Behavior donut (4 slices)

`post-claim wallet behavior`, refreshed every 15 min from `slx_balance_snapshots`:

| slice | colour | meaning |
|---|---|---|
| **Sold** | pink | balance dropped vs allocation |
| **Held** | green | balance ≈ allocation, not staked |
| **Staked** | amber | balance in GLAM stSLX (`balance_stslx > 0`) |
| **Bought-more** | indigo | balance > allocation |

Center: total post-claim wallet count. Per-slice tooltip exposes count,
`balance_now`, `liquid_total`, retention %.

### Wallet table (17 columns)

| col | source |
|---|---|
| rank | sort position |
| wallet | + Solscan link, copy button, tag pills, cohort pill |
| Total alloc | `total_slx` |
| Liquid (U1) | `liquid_slx` (TGE-liquid portion of total) |
| U2 est. (3m) | `vested × 31/92` (tooltip shows 9m alt) |
| Vested | `vested_slx` |
| S1 base | `amt_standard` (~8.5% of 1B target) |
| Exponent | `amt_exponent` (Exponent V2 partner farming bonus) |
| Flares bonus | `amt_flares_bonus` (flat 721.28 SLX per real-user wallet) |
| Presale | `amt_presale` (Legion presale, Dec 2025, ~0.29% target) |
| Xeet | `amt_xeet` (Tonso × Xeet partnership, top-100 creators, ~0.87% target) |
| Others | `amt_rivalz` aggregate (with hover-detail) |
| Held / Staked / Bought / Sold | latest `slx_balance_snapshots` deltas |
| Claimed | `claim_at` (earliest `claim_at` across batches; ✓ pill with date or muted "unclaimed") |

### Tag pills

| tag | colour | meaning |
|---|---|---|
| **S1** | amber | wallet is in Solstice's S1 list (`is_s1=1`) |
| **PRESALE** | indigo | paid USDC AND received Legion SLX (in `slx_legion_distributions`) |
| **PRESALE_PENDING** | faded italic indigo | paid USDC, no Legion SLX received yet |
| **XEET** | pink | has `amt_xeet > 0` |
| **RIVALZ** | cyan | has `amt_rivalz > 0` |
| **EXP** | rose | has `amt_exponent > 0` |

### Filters

- Search (wallet pubkey or prefix)
- Claim status (claimed / unclaimed)
- Min-allocation (1 / 100 / 1k / 10k / 100k / 1M SLX) — default ≥1 SLX excludes dust
- Tags multi-select
- Cohorts multi-select (C1–C6 + `(none)`)

## Data flow

```
data/solstice.db
  ├─ slx_allocations             (per-batch amounts: total/liquid/vested + 6 buckets, schedule_code, claim_at)
  ├─ wallets                     (cohort, classification, is_s1)
  ├─ slx_balance_snapshots       (latest balance_slx + balance_stslx + delta_pct + category)
  ├─ legion_buyers               (net_usdc per wallet)
  └─ slx_legion_distributions    (on-chain proof of Legion SLX claim)
        ↓
tools/build_s1_data.py           (single rollup query + per-wallet tag derivation)
        ↓
server-s1/data.json              (~5.7 MB, 15,299 wallet rows)
        ↓
server-s1/index.html             (static SPA, one fetch on load)
```

Upstream of the DB: on-chain Solana state + Clique acs-v4 allocations API
(deployment `019e588b-67b6-742f-af98-104cbb6a425c`, app `85da4f0c`).

## Rebuild

```bash
# 1. Start the 15-min balance snapshot daemon (runs forever; idempotent INSERTs)
#    Not in cron — run in a nohup session so it survives logout:
nohup python3 tools/slx_holding_tracker.py > /tmp/slx_tracker.log 2>&1 & disown

# 2. Rebuild dashboard JSON
python3 tools/build_s1_data.py
```

Writes `server-s1/data.json`. Console prints `fetched_pct` + `sum_liquid_unlock1`.

## Promote step

`server-s1/` is the working source. `s1-deploy/` is the published copy and a
separate Vercel project (`s1-solstice-airdrop` / `prj_qxfSh7BDG44hnCno6m5B1B57TA8D`)
with its own git remote (`jayow/s1_solstice_airdrop_claim`).

```bash
# Copy source + data into the deploy directory
cp server-s1/index.html s1-deploy/
cp server-s1/data.json  s1-deploy/

# Deploy
cd s1-deploy && vercel --prod --yes --archive=tgz
```

`s1-deploy/vercel.json` sets `cleanUrls: true` + 60 s cache on `.json`.

## U2 math reference (canonical)

Per code comment in `tools/build_s1_data.py:105-106`:

```
3m plan: vested × 31/92  ≈ 33.70%   ← default surfaced in table
9m plan: vested × 31/275 ≈ 11.27%   ← tooltip alt
```

U2 = **2026-07-05** (TGE + 41 days). Do not mistake for 2026-06-25; that error
keeps recurring — see `feedback_u2_july5_not_june25` in memory.

## Related concepts

- TVL/TWA framework: see [`../docs/twa_framework.md`](../docs/twa_framework.md)
  for the S1 TWA computation that drives `amt_flares_bonus` eligibility
- SLX vesting + circulation: see [`../docs/slx_vesting_and_circulation.md`](../docs/slx_vesting_and_circulation.md)
  for the on-chain Claim instruction layout + U1/U2 timeline
- S2 dashboard: see [`../server/README.md`](../server/README.md)

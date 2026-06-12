# S2 SLX Calculator

Live at **https://s2.solstice.hanyon.app/s2-calculator.html**

Forward SLX projection — given your current flares and the system total,
estimates your share of the S2 SLX bucket. No vesting / redistribution applied
(see [Caveats](#caveats)).

## Formula

```
slx = (boostedUser / boostedSys) × SLX_TOTAL_SUPPLY × airdrop_pct
```

where:

- `boostedUser = rawUser × yourMult` (your flares × your loyalty multiplier)
- `boostedSys  = rawSys  × avgMult`  (system flares × avg system loyalty)
- `SLX_TOTAL_SUPPLY = 1_000_000_000`  (constant)
- `airdrop_pct` = 0.03 default (3% = 30M SLX bucket); selector also offers 0.0325 / 0.035 as scenario models

`rawUser` and `rawSys` are clamped to `≥0` before any math (`renderResults` line ~1229).

## Inputs

Three tabs in the form panel:

### 1. Load wallet
- Paste a wallet address → fetches `wallets/<addr>.json` from the S2 dashboard
- Pre-fills `current_flares` + projects YT decay + non-YT daily rate to end date
- Defaults end date to **2026-08-01** (S2_END_TS)

### 2. Simulate new deposit
Top-of-tab selects (synced with Load wallet tab via `_syncFromSimTab` / `_syncFromMainTab`):
- **Your loyalty** — 1.4× Full / 1.3× Reduced / 1.2× Minimum / 1.0× None
- **Avg system loyalty** — 1.4× / 1.3× (default) / 1.2× / 1.1× / 1.0×
- **SLX bucket** — 3.00% / 3.25% / 3.50%

Below: simulate a hypothetical YT deposit (deposit amount + YT market → projected total flares).

Bottom readout (derived):
- **Sys total** — `rawSys × avgMult`
- **Your share** — `boostedUser / boostedSys` as %
- **Projected SLX** — formula above using the simulated total

### 3. Manual entry
Direct input of `Your total flares` + `System total flares` + SLX bucket. No
guardrail on the manual sys-total — paste Solstice's published grand total
(NOT the dashboard's reconstructed real-user total — they can differ by ~2×).

## Auto-projection (system total)

`computeSystemTotalAtEnd(daysAhead)` (line ~1138):

1. Prefer Solstice's official `solstice_series` (from `daily_totals.json`)
2. Compute 7-day average daily delta, clamping outliers (>2.5B/day dropped)
3. Project: `sysNow + avgDaily × daysAhead`
4. Fallback to framework-based per-quest emission if `solstice_series` absent

User can override via the System total input — `SYS_TOTAL_USER_EDITED` flag
suppresses re-projection until they clear the field.

## FDV scenarios + breakeven

Below the SLX projection, a table with 7 FDV scenarios (50M / 100M / 150M /
200M / 300M / 400M / 500M) + a live "NOW" row pulled from SLX market price:

```
price = sc.fdvM × 1e6 / SLX_TOTAL_SUPPLY
usd   = slx × price
roi   = usd / basis           ← basis = YT cost basis (loaded wallet OR YT helper deposit USD)
```

Breakeven FDV (shown in hero under SLX):

```
breakevenPrice = basis / slx
breakevenFdvM  = breakevenPrice × SLX_TOTAL_SUPPLY / 1e6
```

## Caveats

The calculator shows **gross nominal allocation**. It does NOT:

- Apply vesting haircuts (9-month is the DEFAULT path per docs/slx_vesting_and_circulation.md)
- Add the flat redistribution bonus (~936-1,617 SLX per registered wallet from
  unclaimed pool — number depends on which source: code-derived 13.9M ÷ 14,851
  = 936, memory note says 1,617)
- Apply the Cohort-6 retroactive boost (memory says 23×, separate note says 14× — inconsistent)
- Distinguish "your share of the S2 pool" vs "what lands liquid at TGE"

These are intentional omissions per user request — vesting is communicated in
posts rather than UI copy.

## Code map

| concern | file | line |
|---|---|---|
| Formula | `server/s2-calculator.html` | ~1229 (`renderResults`) |
| Constants | `server/s2-calculator.html` | ~854-855 (`SLX_TOTAL_SUPPLY`, `S2_AIRDROP_PCT`) |
| YT projection | `server/s2-calculator.html` | `projectYtFlares()` |
| System total projection | `server/s2-calculator.html` | `computeSystemTotalAtEnd()` ~1138 |
| Wallet load + YT decay | `server/s2-calculator.html` | `calcLoadWallet()` + `refreshUserProjection()` |
| Tab sync | `server/s2-calculator.html` | `_syncFromSimTab` + `_syncFromMainTab` ~1051 |
| FDV scenarios + breakeven | `server/s2-calculator.html` | inside `renderResults` after share calc |

## Reference numbers

Per `project_slx_tokenomics_breakdown` (memory anchor):

- S2 allocation: 3% of 1B = **30M SLX**
- At 130B projected system flares: **0.000231 SLX per flare**
- At $0.25/SLX: $0.0000577 per flare

Sanity checks (all pass):
- 1M / 100B / 3% → 300 SLX
- 10M / 130B / 3% → 2,307.69 SLX
- 6,952 / 63.9B / 3% → 3.26 SLX

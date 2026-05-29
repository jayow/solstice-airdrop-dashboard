# TVL framework — S1 TWA vs S2 current snapshot

**Critical**: S1 and S2 use DIFFERENT metrics. Do not mix the code between them.

- **S1** = TWA (time-weighted average) over a closed window. Historical, frozen.
- **S2** = CURRENT TVL snapshot from S2 start onward. **No S2 TWA exists** — Solstice does not publish one and we do not compute one.

---

## Two metrics — and only two

| Metric | What it answers | File | Status |
|---|---|---|---|
| **S1 TWA TVL** | "What was your time-weighted average TVL during Season 1?" | `tools/transform_twa.py` | ✓ ±0.2% accuracy (3 cal wallets) |
| **S2 current TVL** | "What's your TVL right now?" (live snapshot from S2 start onward) | `server/build_wallet_details.py:compute_tvl_by_quest` | ✓ Live in dashboard |

There is **NO** S2 TWA. Do not create `transform_twa_s2.py`. S2 scoring is driven by
cumulative flares earned across the season (a separate pipeline). TVL on the S2
dashboard is always a **current snapshot**, never time-weighted.

---

## S1 TWA TVL — frozen, ±0.2% accuracy

### Window
- Start: `2025-09-30 00:00 UTC` (S1_START_TS = 1759190400)
- End: `2026-04-13 00:00 UTC` (S1_END_TS = 1776038400, exclusive)
- D_total = 195 days, L_season = 2026-04-12 (last counted day)

### Formula
```
TWA = S_w / n_w
where:
  S_w = Σ_{d ∈ [f_w, L_w]} T_w(d)              # sum of daily TVL
  n_w = count of days in [f_w, L_w] with T_w(d) > 0
  f_w = first day with positive cutoff balance
  L_w = LAST day with positive cutoff balance
  T_w(d) = cutoff at end of day d (= (d+1) 00:00 UTC),
           or last positive intra-day value if cutoff is 0
```

### Day boundary (from PDF spec)
`c(d) = (d+1) 00:00 UTC` — state at midnight UTC of the NEXT day = end of day d.

### Sources included — matches Solstice's official S1 TWA column spec

Per Solstice's official S1 TWA breakdown table (6 columns):

| Solstice column | What it counts | Our decoder | Status |
|---|---|---|---|
| `kamino_usx` | Kamino reserve/lending position value at cutoff | `decode_kamino` (USX/eUSX/USDG reserves) | ✓ |
| `exponent_usx` | Capital still invested in Exponent at cutoff (LP + YT combined, cost basis) | `decode_lp` + `decode_yt` | ✓ |
| `raydium_usx` | CLMM LP position value (token0 + token1) converted to USX | `decode_clmm` (Raydium) | ✓ |
| `whirlpool_usx` | Orca Whirlpool CLMM same as Raydium | `decode_clmm` (Orca) | ✓ |
| `usx_holding` | Raw USX SPL balance in wallet at slot | `extract_balance_events` | ✓ |
| `eusx_holding` | eUSX SPL balance × eusxRate at slot | `extract_balance_events` + `peg_at()` | ✓ |

T_w(d) = sum of all 6 columns at cutoff c(d) = (d+1) 00:00 UTC.

### Sources NOT included (deliberate — Loopscale wasn't a Solstice partner during S1)
- ❌ **Loopscale** — not yet integrated when S1 ended (became a partner in S2)
- ❌ Kamino Strategy / KVault — not active during S1

### LP-specific rules
1. **Cost basis tracking**: deposits add `base_amount`; withdraws reduce proportionally.
2. **SY-only valuation**: `user_sy_value = lp_balance × (sy_balance / lp_supply) × peg`. PT does NOT count (per Solstice docs).
3. **Legacy market exclusion**: any Exponent market whose `expiration_ts` is within (S1_START, S1_END) is EXCLUDED entirely. Solstice only credited markets active at season end. Auto-detected via `get_sy_per_lp()` which reads `expiration_ts` from on-chain.
4. **Historical sy/lp at deposit time**: looked up from `market_state_history` table (populated by `tools/index_market_state.py`). Calibrated by factor **0.948** to account for SY-reconstruction over-count in the indexer.

### eUSX peg
- Real on-chain snapshots from `eusx_peg.peg_at()` (table `eusx_peg_snapshots`).
- Back-extrapolated at 6% APY for timestamps before earliest snapshot.

### Validated against
| Wallet | Solstice TWA | Our output | Match |
|---|---:|---:|---:|
| `uen3EiFgQB…` | $24,134.76 | $24,117.95 | **99.93%** |
| `5V9VwuVqXy…` (Jay) | $3,621.22 | $3,615.07 | **99.83%** |
| `7m8sSFp1gg…` | $5,462.79 | $5,469.22 | **100.12%** |

---

## S2 current TVL — dashboard snapshot

### What it is
**Current** USD-equivalent of each wallet's positions across all flare-earning
quests, computed live from the most recent walker outputs.

There is no time-weighted version. The TVL column on the S2 dashboard is always a
live snapshot.

### ⚠️ Key asymmetry vs Solstice's "Current TVL"

| | Solstice's "Current TVL" | Our S2 TVL |
|---|---|---|
| Pre-S2 deposit still held today | **counted in full** (whole notional position) | **counted only for the S2 portion** |
| Position opened during S2 | counted in full | counted in full |
| Window basis | walks current on-chain balance, no time gating | walks events from `S2_START_TS` onward; pre-S2 deposits don't appear |

Solstice's "Current TVL" is purely "what's your wallet worth right now in flare-eligible positions" — they don't care when you deposited.

Our number deliberately attributes only S2-era exposure because every other S2
metric in our pipeline (flares, emissions, multiplier inversion) is bounded to
the S2 window. Reading them together stays internally consistent.

**Consequence**: for wallets with large pre-S2 positions still parked, our S2 TVL
will read LOWER than Solstice's Current TVL. That gap is expected and is NOT a
walker bug — do not "fix" it by walking pre-S2 history.

### File
`server/build_wallet_details.py:compute_tvl_by_quest()`

### Formula (per quest)
```
tvl_by_quest[q] = daily_emission_rate_of_q / multiplier_of_q
```
Inverts the flare formula `flares_per_day = TVL × multiplier`.

### Special case: Exponent YT
For YT quests, inversion would yield YT-count, not USD. Instead:
```
tvl_by_quest[YT_quest] = cost_basis.usd_basis
```
Per Solstice docs: *"Exponent YT TVL is tracked at the amount you originally deposited."*

### Sources included (S2 spec — extends S1's 6 columns with Loopscale + KVault)
All 29 official S2 quest codes via `walker_outputs` table. Includes **Loopscale**
and **Kamino Strategy / KVault** (both became Solstice partners in S2 — neither
was integrated during S1, which is why S1's column spec doesn't list them).

### What this is NOT
- ❌ NOT a time-weighted average
- ❌ NOT season-cumulative
- ❌ NOT a SLX allocation predictor

---

## Naming conventions to keep them separate

- `S1_*` constants → only in `transform_twa.py` (frozen)
- S2 dashboard logic → `server/build_wallet_details.py` (snapshot only)
- Never introduce an `S2_TWA_*` constant or a `transform_twa_s2.py` file. There is no S2 TWA.

## Quick recipe: which file do I edit?

| Goal | File |
|---|---|
| Improve S1 TWA accuracy | `tools/transform_twa.py` |
| Improve dashboard TVL column (S2 current snapshot) | `server/build_wallet_details.py` + `server/index.html` |
| Add a new S2 walker | `src/flares_estimator/walk_s2_*.py` |
| Reindex Exponent market state (S1 only) | `tools/index_market_state.py <market_pk>` |
| Refresh eUSX peg | `python -c "from src.flares_estimator.quests.eusx_peg import record_snapshot; record_snapshot()"` |

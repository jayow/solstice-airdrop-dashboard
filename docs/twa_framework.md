# TWA framework — S1 vs S2 vs current snapshot TVL

**Critical**: these are three SEPARATE metrics with different formulas, windows, and walker coverage.
Do not mix the code between them. Each metric has its own module.

---

## Three metrics

| Metric | What it answers | File | Status |
|---|---|---|---|
| **S1 TWA TVL** | "What was your time-weighted average TVL during Season 1?" | `tools/transform_twa.py` | ✓ ±0.2% accuracy (3 cal wallets) |
| **S2 TVL (snapshot)** | "What's your TVL right now?" | `server/build_wallet_details.py:compute_tvl_by_quest` | ✓ Live in dashboard |
| **S2 TWA TVL** | "What's your time-weighted average TVL during Season 2?" | **NOT BUILT YET** | Pending |

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

### Sources included (walkers covered)
| Source | Decoder in transform_twa.py | Notes |
|---|---|---|
| HOLD USX / eUSX / weUSX | `extract_balance_events` | Wallet ATA balance via Helius tokenBalanceChanges |
| Exponent LP | `decode_lp` | All 5 LP event types (provide/withdraw/classic/base) |
| Exponent YT | `decode_yt` | BuyYt + SellYt + carry-in for pre-S1 buys |
| Kamino lending | `decode_kamino` | USX/eUSX/USDG reserves on Solstice market |
| Orca CLMM | `decode_clmm` | increase/decrease_liquidity events |
| Raydium CLMM | `decode_clmm` | increase/decrease_liquidity events |

### Sources NOT included (deliberately, for S1)
- ❌ Loopscale supply / borrow (legacy: minimal S1 exposure on cal wallets)
- ❌ Kamino Strategy / KVault (legacy: not active during S1)

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

## S2 TVL (snapshot) — current dashboard

### What it is
**Current** USD-equivalent of each wallet's positions across all flare-earning quests. NOT a TWA.

### File
`server/build_wallet_details.py:compute_tvl_by_quest()`

### Formula (per quest)
```
tvl_by_quest[q] = daily_emission_rate_of_q / multiplier_of_q
```
Inverts the flare formula `flares_per_day = TVL × multiplier`.

### Special case: Exponent YT
For YT quests, we don't use the inversion (it would produce YT-count instead of USD). Instead:
```
tvl_by_quest[YT_quest] = cost_basis.usd_basis
```
Per Solstice docs: *"Exponent YT TVL is tracked at the amount you originally deposited."*

### Sources included
All 29 official S2 quest codes via `walker_outputs` table. **Includes Loopscale + KVault** (unlike S1).

### What this is NOT
- ❌ NOT a time-weighted average
- ❌ NOT season-cumulative
- ❌ NOT a SLX allocation predictor

---

## S2 TWA TVL — NOT BUILT YET (future work)

### What it would answer
"What's your time-weighted average TVL during Season 2 (Apr 13 → Aug 1)?"

This metric would be needed for:
- S2 loyalty multiplier reverse-engineering (if Solstice publishes per-wallet TWAs)
- Predicting end-of-S2 SLX allocation per wallet

### Window (S2 official)
- Start: `2026-04-13 00:00 UTC` (S2_START_TS = 1776038400)
- End: `2026-08-01 00:00 UTC` (S2_END_TS ≈ 1785024000, exclusive)
- D_total ≈ 110 days

### Formula
Same as S1 in shape (`TWA = S_w / n_w`), but with S2 window.

### Sources required (must ALL be added)
| Source | Status | What's missing |
|---|---|---|
| HOLD USX / eUSX | reuse from S1 | nothing |
| Exponent LP | reuse from S1 | active markets are Jun26 + Sep26 (different sy/lp ratios) |
| Exponent YT | reuse from S1 | same |
| Kamino lending | reuse from S1 | nothing |
| Orca CLMM | reuse from S1 | historical pool tick needed (currently uses snapshot) |
| Raydium CLMM | reuse from S1 | same |
| **Kamino Strategy / KVault** | **NEW** | needs `decode_kamino_strategy()` |
| **Loopscale supply / borrow** | **NEW** | needs `decode_loopscale()` + `decode_loopscale_events()` |

### S2-specific rule differences vs S1
- **Legacy markets**: definition shifts. For S2, "legacy" = matured during S2 window (Apr 13 → Aug 1). Currently that's USX-Feb26, eUSX-Mar26 (already expired pre-S2, so not relevant) AND any Jun26 markets (mature 2026-06-01, mid-S2). **Need to verify if Solstice excludes Jun26 from S2 TWA after its maturity.**
- **eUSX peg**: snapshots are richer post-S1 (we sample more frequently). Use `peg_at()` directly.
- **SY exchange rate**: by S2, the rate has grown noticeably from S1 start. Calibration factor `0.948` may not transfer cleanly.
- **YT cost basis carry-in**: pre-S2 YT buys decay per the YT cost-basis algorithm (already in code, but timing is S2-relative not S1-relative).

### What would need to be done
1. New file: `tools/transform_twa_s2.py` (mirror of `transform_twa.py` with S2 constants and S2-specific rules).
2. Add decoders: `decode_kamino_strategy()`, `decode_loopscale()`.
3. Re-index legacy + active markets through end of S2 window for historical sy/lp.
4. Batch run across all S2-active wallets (~7,000 in current `data.json` records).
5. Calibrate against 3+ wallets where Solstice publishes S2 TWA (currently not public — would need to ask Solstice or wait for post-TGE disclosure).
6. Parallelize: estimated ~30s per wallet → ~60 hours serial → need ThreadPool with checkpointing.

### Why not just adapt `transform_twa.py`?
Bad idea — would mix the S1 and S2 rule sets. The constants differ, the LP rules differ, the YT cost-basis carry-in window differs. Forking keeps the S1 implementation frozen and verified.

---

## Naming conventions to keep them separate

- `S1_*` constants → only in `transform_twa.py`
- `S2_*` constants → only in `transform_twa_s2.py` (future) and `server/build_wallet_details.py`
- Never share a function that uses both. Decoders (`decode_lp`, etc.) are window-agnostic and can be shared; integrators MUST be per-season.

## Quick recipe: which file do I edit?

| Goal | File |
|---|---|
| Improve S1 TWA accuracy | `tools/transform_twa.py` |
| Improve dashboard TVL column | `server/build_wallet_details.py` + `server/index.html` |
| Build S2 TWA | NEW: `tools/transform_twa_s2.py` |
| Add a new walker | `src/flares_estimator/walk_s2_*.py` + decoder in BOTH transforms |
| Reindex Exponent market state | `tools/index_market_state.py <market_pk>` |
| Refresh eUSX peg | `python -c "from src.flares_estimator.quests.eusx_peg import record_snapshot; record_snapshot()"` |

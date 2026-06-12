# Repo orientation

This is the **Solstice S2 Flares dashboard** — an independent third-party
leaderboard for Solstice Finance's Season 2 airdrop. It walks on-chain Solana
events for every quest-relevant protocol and reproduces the flare credit
formula that Solstice uses internally.

## What ships

| dashboard | URL | source dir |
|---|---|---|
| S2 Flares leaderboard | https://s2.solstice.hanyon.app | `server/` |
| S2 SLX Calculator | https://s2.solstice.hanyon.app/s2-calculator.html | `server/s2-calculator.html` |
| SLX Liquidity Depth | https://s2.solstice.hanyon.app/slx-depth.html | `server/slx-depth.html` |
| S1 Allocations | https://airdrop.solstice.hanyon.app | `server-s1/` → `s1-deploy/` |

Full alias map: [`DEPLOYMENTS.md`](DEPLOYMENTS.md).

## Architecture

```
┌──────────────────────┐
│ Solana RPC (Helius)  │  ← walkers fetch sigs + decode tx data
└──────────┬───────────┘
           │
┌──────────▼───────────────────────────────────────────────┐
│ src/flares_estimator/                                    │
│   ├─ walk_s2_<protocol>.py     (per-quest walkers)       │
│   ├─ gt_walkers/gt_hold_*.py   (HOLD walker family)      │
│   ├─ transform_*.py            (post-walk aggregations)  │
│   └─ db.py                     (SQLite singleton)        │
└──────────┬───────────────────────────────────────────────┘
           │
┌──────────▼───────────────────────────────────────────────┐
│ data/solstice.db    ~41 GB                               │
│   ├─ wallet_quests         (per-wallet per-quest flares) │
│   ├─ quest_cache           (per-wallet timeline cache)   │
│   ├─ flares_snapshots      (daily totals: Solstice + us) │
│   ├─ wallets               (classification, is_s1)       │
│   ├─ slx_allocations       (S1 SLX bucket detail)        │
│   └─ slx_balance_snapshots (post-claim balance behavior) │
└──────────┬───────────────────────────────────────────────┘
           │
┌──────────▼───────────────────────────────────────────────┐
│ server/build_*.py + server-s1/build_*.py                 │
│        → server/{data,daily_totals}.json                 │
│        → server/wallets/<addr>.json × ~28k               │
│        → server-s1/data.json                             │
└──────────┬───────────────────────────────────────────────┘
           │
┌──────────▼───────────────────────────────────────────────┐
│ Vercel — static SPAs + 2 serverless functions            │
└──────────────────────────────────────────────────────────┘
```

## Daily refresh

```bash
bash server/refresh.sh              # full S2 pipeline (~10-15 min)
```

Phases described in [`server/README.md`](server/README.md). The HOLD walker
family is NOT in `refresh.sh` — run separately, serially:

```bash
for w in gt_hold_usx_daily gt_hold_eusx_daily \
         gt_hold_usx_1mo gt_hold_eusx_1mo \
         gt_hold_usx_3mo gt_hold_eusx_3mo; do
  cd src && python3 -u -m flares_estimator.gt_walkers.$w && cd ..
done
```

After USX_DAILY writes today's schema-2 cache, the other 5 reuse it and finish
in ~30s each.

## Per-quest indexing

The walkers reproduce each partner's flare formula. Indexing methodology lives
in memory (read these first when touching any walker):

- `reference_indexing_principles` — universal rules (no heuristics, on-chain
  truth, midnight snapshot cutoff)
- `reference_indexing_hold` — HOLD USX/eUSX (the gold-standard pattern)
- `reference_indexing_exponent_yt` — YT walker (CRITICAL: users don't hold YT)
- `reference_indexing_exponent_lp` — LP walker
- `reference_indexing_kamino` — KLEND walker
- `reference_indexing_loopscale` — Loopscale walker
- `reference_indexing_clmm` — Orca + Raydium CLMM walkers

## Key invariants

These get re-discovered every few sessions — keep them top of mind:

1. **Snapshot cutoff**: walkers MUST use `last_snapshot_ts()` (midnight UTC),
   never `time.time()`. Intraday integration inflates totals vs Solstice.
2. **S1 is ground truth for real_user**: never auto-classify a wallet with
   `is_s1=1` as PDA, even if `getAccountInfo` returns null (SOL-less wallets
   are a common legitimate pattern).
3. **U2 = 2026-07-05** (TGE + 41 days). NOT June 25.
4. **Solstice publishes once daily at 00:00 UTC**. Compare day-over-day deltas
   only at that boundary.
5. **Flares, not dollars**. Never use `$` prefix on flare values.
6. **No S2 TWA exists** — S1 is frozen historical TWA; S2 uses current TVL.
   See `docs/twa_framework.md`.
7. **Walker concurrency** — `SOLSTICE_RPC_CONCURRENCY=100` in `.env` is the
   sweet spot. Higher → Helius archival reads degrade silently (no 429s, just
   slow tail). 6 walkers in parallel + 41 GB DB → SQLite contention slows
   things; run HOLD walkers SERIALLY when possible.

## Data sources of truth

| concern | source | freshness |
|---|---|---|
| Solstice baseline totals | `flares_snapshots` (source='solstice_dashboard') | daily via `tools/set_solstice_total.py` |
| Per-wallet flares | `wallet_quests` (written by walker `sync_to_wallet_quests`) | daily via refresh.sh |
| Wallet classification | `wallets.classification` (written by `filter_pdas_db.py`) | weekly or on-demand |
| Manual PDA list | `data/protocol_pdas.json` | manual edit |
| S1 allocations | `slx_allocations` (built from on-chain + Clique API) | manual |
| Post-claim balances | `slx_balance_snapshots` | 15-min refresh via `tools/slx_holding_tracker.py` (run in nohup; daemon loops every CYCLE_SEC=900) |

## Where things live

```
src/flares_estimator/          (walkers + RPC helpers + db connector)
tools/                         (one-shot scripts: build_s1_data, set_solstice_total, snapshots, etc)
server/                        (S2 dashboards + build scripts + Vercel deploy)
server-s1/                     (S1 dashboard source — promote to s1-deploy/)
s1-deploy/                     (S1 dashboard published copy + separate git remote)
docs/                          (methodology reference, not user-facing)
data/                          (solstice.db + protocol_pdas.json + manual data caches)
```

## Common gotchas

- **Zombie walker processes**: `pkill -9 -f "flares_estimator.gt_walkers"` if
  a previous run hung. Check elapsed time on `ps -ef` before launching a new
  pipeline.
- **41 GB SQLite DB**: `busy_timeout=30000` is set in `db._init_conn`. If you
  see SQLITE_BUSY errors it's a different connection without the PRAGMA.
- **Deploy from wrong dir**: only deploy `server/` for the main project. Root
  `.vercel/` + `flares-deploy/.vercel/` both point at the same project — easy
  to overwrite production from the wrong tree. See [`DEPLOYMENTS.md`](DEPLOYMENTS.md).
- **`requests` library not available** in the system Python — `tools/set_solstice_total.py`
  imports it but the system Python doesn't have it. Use the project's venv.
- **YT walkers**: users don't hold YT in their wallets — read `reference_indexing_exponent_yt`
  before touching anything related.

# Solstice S2 Flares dashboard

Live at **https://s2.solstice.hanyon.app** — the flagship Season 2 flares
leaderboard + per-wallet drawer + per-partner donut.

Source lives in `server/`. Deployed to Vercel project `solstice-flares`
(`prj_7BFlFPdWTxjteCARFwu3vWM5Of5k`). See [`../DEPLOYMENTS.md`](../DEPLOYMENTS.md)
for the full alias map.

## What it shows

Three pages on the same project:

| path | file | purpose |
|---|---|---|
| `/` | `index.html` | Main S2 dashboard — partner donut, cohort table, wallet drawer, daily totals chart |
| `/s2-calculator.html` | `s2-calculator.html` | Forward SLX projection calculator — see [`../docs/s2-calculator.md`](../docs/s2-calculator.md) |
| `/slx-depth.html` | `slx-depth.html` | CEX + DEX SLX market depth — see [`../docs/slx-depth.md`](../docs/slx-depth.md) |

## Build pipeline

```
data/solstice.db
  ├─ wallet_quests          (per-wallet per-quest flares — source of truth)
  ├─ wallets                (classification, is_s1, cohort)
  ├─ quest_cache            (per-wallet timeline cache from walkers)
  ├─ flares_snapshots       (daily Solstice + framework totals)
  └─ data/protocol_pdas.json  (manual PDA override list)
        ↓
bash server/rebuild.sh                  # runs all 3 below in dependency order
  ├─ python3 server/build_data.py          ⟶ server/data.json
  ├─ python3 server/build_daily_totals.py  ⟶ server/daily_totals.json
  └─ python3 server/build_wallet_details.py ⟶ server/wallets/<addr>.json × ~28k
        ↓
server/index.html  (loads data.json + daily_totals.json + lazy-loads wallets/<addr>.json on drawer open)
```

### Daily refresh

The full pipeline is orchestrated by `server/refresh.sh`. Run shortly after
00:00 UTC each day:

```bash
bash server/refresh.sh
```

Phases:

| phase | what | typical time |
|---|---|---|
| 0 | `tools/set_solstice_total.py` — fetch Solstice's official totalFlare into `flares_snapshots` | ~2s |
| 0.5 | `walk_xpbook.py` + `transform_xpbook.py` — XPBook orderbook indexer (cursor-incremental) | ~30s |
| 1 | 6 walkers in parallel: `walk_s2_lp/yt/kamino/loopscale/orca/raydium` | 3–7 min |
| 2 | `transform_kamino.py` (depends on phase-1 Kamino cache) | ~1 min |
| 3 | `transform_loopscale.py` + `resync_hold_quests.py` + `walk_v2_lp.py` | ~30s |
| 4 | `rebuild.sh` — `build_data.py` + `build_daily_totals.py` + `build_wallet_details.py` | ~15s |

Env vars:

- `REFRESH_MODE=ci` — sequential walkers (use on GitHub Actions; the runner
  can't sustain 100+ concurrent Helius connections)
- `REFRESH_MODE=parallel` (default) — parallel walkers, ~4× faster

### HOLD walkers (separate pipeline)

HOLD walkers (USX + eUSX × daily/1mo/3mo = 6 binaries) live in
`src/flares_estimator/gt_walkers/gt_hold_*`. They are NOT in `refresh.sh` —
they're run separately because their cold-walk migration is slow (~30 min for
USX_DAILY's full universe).

Run serially (the contention pattern at the 41 GB DB scale forces serial):

```bash
for w in gt_hold_usx_daily gt_hold_eusx_daily \
         gt_hold_usx_1mo gt_hold_eusx_1mo \
         gt_hold_usx_3mo gt_hold_eusx_3mo; do
  cd src && python3 -u -m flares_estimator.gt_walkers.$w
  cd ..
done
```

After USX_DAILY writes today's schema-2 cache, the other 5 fly through in ~30s
each because they hit the cache.

See `_shared_hold.py:build_twab_timeline` for the COLD / FAST / SLOW path
logic + the lazy-migration fast path for schema-1 → schema-2 caches.

### PDA classification

```bash
python3 src/flares_estimator/filter_pdas_db.py
```

Runs `getAccountInfo` on every wallet with positive flares. Marks System
Program-owned as `user`, program-owned as `pda`, account-doesn't-exist as
`unknown` (NOT `pda_or_uninit` — see commit `1e21c54cda`). Skips `is_s1=1`
wallets per `feedback_s1_is_ground_truth_for_user`.

Manual PDA overrides live in `data/protocol_pdas.json` — addresses there get
`pda_protocol` classification on the next `build_data.py` run.

## Serverless API functions

`server/api/` ships two Vercel serverless functions (Node 20.x):

- `api/cex-depth.js` — CORS proxy for CEX SLX/USDT order-book endpoints
  (mexc, gate, lbank, orangex, toobit, ourbit, weex, bingx). Used by
  `slx-depth.html`.
- `api/perp-oi.js` — CORS proxy for perp OI + funding endpoints (gate, mexc,
  bingx). Pinned region `hnd1` (Tokyo) for proximity to those exchanges.

Convention: negative funding = shorts pay longs = SHORT-crowded.

## Manual deploy

```bash
cd server
vercel --prod --yes --archive=tgz
```

Vercel uploads everything in `server/` minus `.vercelignore`. The `solstice-flares`
project is aliased to `s2.solstice.hanyon.app`.

> ⚠️ Note: `server/.vercel/project.json`, `flares-deploy/.vercel/project.json`,
> AND root `.vercel/project.json` all point to the SAME project. Only deploy
> from `server/` — the others are stale / risky. See [`../DEPLOYMENTS.md`](../DEPLOYMENTS.md).

## Related

- S1 dashboard: [`../server-s1/README.md`](../server-s1/README.md)
- Calculator: [`../docs/s2-calculator.md`](../docs/s2-calculator.md)
- Liquidity depth: [`../docs/slx-depth.md`](../docs/slx-depth.md)
- Deployment topology: [`../DEPLOYMENTS.md`](../DEPLOYMENTS.md)

# Deployments map

Every local directory → its Vercel project → its production alias.

## Current live deploys

| Local dir | Vercel project | Project ID | Live alias |
|---|---|---|---|
| `server/` | `solstice-flares` | `prj_7BFlFPdWTxjteCARFwu3vWM5Of5k` | **https://s2.solstice.hanyon.app** |
| `s1-deploy/` | `s1-solstice-airdrop` | `prj_qxfSh7BDG44hnCno6m5B1B57TA8D` | **https://airdrop.solstice.hanyon.app** |
| `web/` | `web` | `prj_GEu7KLlPNILqZ9lC17k4AiyzR3si` | (stale, pre-S2, last build April 24 2026) |

## Deploy commands

### Main S2 dashboard + calculator + slx-depth

```bash
cd server
vercel --prod --yes --archive=tgz
```

Builds + uploads `server/` (minus `.vercelignore`). Includes:
- `index.html` (S2 leaderboard)
- `s2-calculator.html`
- `slx-depth.html`
- `slx-depth-engine.js`
- `api/cex-depth.js` + `api/perp-oi.js` (Vercel serverless functions)
- `data.json` + `daily_totals.json` + `wallets/<addr>.json × ~28k`

### S1 dashboard

`server-s1/` is the source. `s1-deploy/` is the published copy on a separate
Vercel project + separate git remote (`jayow/s1_solstice_airdrop_claim`).

```bash
# Promote source to deploy
cp server-s1/index.html s1-deploy/
cp server-s1/data.json  s1-deploy/

# Deploy
cd s1-deploy
vercel --prod --yes --archive=tgz
```

## ⚠️ Stale `.vercel` configs

Three directories all hold a `.vercel/project.json` pointing to the SAME
`prj_7BFlFPdWTxjteCARFwu3vWM5Of5k`:

| dir | status | what to do |
|---|---|---|
| `.vercel/` (repo root) | stale link | **don't deploy from root** — only `server/` |
| `server/.vercel/` | canonical | use this |
| `flares-deploy/.vercel/` | legacy snapshot (~May 27) | **don't deploy** — superseded by `server/` |

If you accidentally `vercel --prod` from the wrong dir you'll overwrite
production with whatever's in that dir. Safer: always `cd server && vercel`.

## Cron / scheduled refresh

There is no cron config in this repo. The dashboard's data is refreshed by
running [`server/refresh.sh`](server/refresh.sh) locally and pushing data
changes through the normal deploy. See [`server/README.md`](server/README.md)
for the refresh phases.

If a GitHub Actions / cron job is added later, it should set `REFRESH_MODE=ci`
so walkers run sequentially (the parallel mode floods Helius and silently drops
data on CI runners).

## Vercel.json contracts

| file | what it does |
|---|---|
| `server/vercel.json` | Pins `api/perp-oi.js` to `hnd1` (Tokyo) region |
| `s1-deploy/vercel.json` | `cleanUrls: true` + 60s `Cache-Control` on `.json` |
| `flares-deploy/vercel.json` | (legacy) same shape as `s1-deploy/` |
| `web/vercel.json` | Next.js build (`buildCommand: npm run build`, `outputDirectory: out`) |

## Production URL guide

Quick reference for which URL serves what:

```
s2.solstice.hanyon.app/                  → server/index.html        (S2 leaderboard)
s2.solstice.hanyon.app/s2-calculator.html → server/s2-calculator.html (SLX projection)
s2.solstice.hanyon.app/slx-depth.html    → server/slx-depth.html    (CEX/DEX depth)
s2.solstice.hanyon.app/api/cex-depth     → server/api/cex-depth.js  (serverless)
s2.solstice.hanyon.app/api/perp-oi       → server/api/perp-oi.js    (serverless)

airdrop.solstice.hanyon.app/             → s1-deploy/index.html     (S1 allocations)
```

## Related docs

- [`server/README.md`](server/README.md) — S2 build pipeline + refresh
- [`server-s1/README.md`](server-s1/README.md) — S1 dashboard methodology
- [`docs/s2-calculator.md`](docs/s2-calculator.md) — calculator formula
- [`docs/slx-depth.md`](docs/slx-depth.md) — liquidity depth sources

# SLX Liquidity Depth

Live at **https://s2.solstice.hanyon.app/slx-depth.html**

Real-time SLX/USDT market depth across CEX + DEX venues, plus perp OI &
funding. Pure-black design-spec theme.

## What it shows

- **Order-book depth** — bid/ask volume within configurable price bands across
  every listed CEX
- **DEX liquidity** — Solana DEX pool depth (Orca, Raydium, etc.) for SLX/USDC
- **Perp OI + funding** — open interest and funding rate per venue. Negative
  funding = shorts pay longs = SHORT-crowded.

## Data sources

| venue | spot endpoint | perp endpoint | proxy |
|---|---|---|---|
| MEXC | `api.mexc.com/api/v3/depth?symbol=SLXUSDT` | `contract.mexc.com/api/v1/contract/...` | `api/cex-depth?venue=mexc` + `api/perp-oi?venue=mexc` |
| Gate | `api.gateio.ws/api/v4/spot/order_book` | `api.gateio.ws/api/v4/futures/usdt/...` | `api/cex-depth?venue=gate` + `api/perp-oi?venue=gate` |
| LBank | `api.lbkex.com/v2/depth.do` | — | `api/cex-depth?venue=lbank` |
| OrangeX | listed in `api/cex-depth.js` | — | `api/cex-depth?venue=orangex` |
| Toobit | listed in `api/cex-depth.js` | — | `api/cex-depth?venue=toobit` |
| OurBit | listed in `api/cex-depth.js` | — | `api/cex-depth?venue=ourbit` |
| WEEX | listed in `api/cex-depth.js` | — | `api/cex-depth?venue=weex` |
| BingX | listed (currently returns `symbol is not found`) | — | `api/cex-depth?venue=bingx` |

DEX pools come from public on-chain queries (no proxy needed).

## Why proxies

CEX endpoints either omit `Access-Control-Allow-Origin` or actively reject
preflights — the browser can't call them directly. Two Vercel serverless
functions handle this:

- **`server/api/cex-depth.js`** — Node 20.x runtime. Routes by `?venue=<key>`.
  Returns the upstream's raw order-book JSON. Default region (US).
- **`server/api/perp-oi.js`** — Node 20.x runtime. Routes by `?venue=<key>`.
  Returns a unified shape: `{venue, oi_usd, funding_rate, funding_cycle_hours, mark}`.
  Pinned to `hnd1` (Tokyo) region in `server/vercel.json` for proximity to those exchanges.

Both use 8s timeout (`TIMEOUT_MS = 8_000`) and AbortController to fail-fast on
slow upstreams.

## Engine

`server/slx-depth-engine.js` — fans out parallel `fetch()` calls to all proxies,
normalizes responses, aggregates into the visualizations on the page.

## Adding a new venue

1. **Spot depth**: add entry to `VENUES` in `server/api/cex-depth.js` with the
   upstream URL. The proxy auto-derives by `?venue=<key>` lookup.
2. **Perp OI**: add to `server/api/perp-oi.js` with the dual OI + funding endpoints.
3. **Visualization**: register the venue in `slx-depth-engine.js`'s fetcher list.
4. Test locally via `curl 'localhost:3000/api/cex-depth?venue=<key>'`.

## Verified upstreams (per 2026-05-31 audit)

mexc, gate, lbank, orangex, toobit, ourbit, weex — all returning valid depth.

BingX listed for forward-compatibility but returns `symbol is not found` until
they list SLX/USDT spot. Engine surfaces this as an upstream error rather than
crashing.

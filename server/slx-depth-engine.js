// SLX Depth Engine — live orderbook + on-chain pool reader for both BID (sell)
// and ASK (buy) sides.
//
// Vanilla browser module. No imports, no bundler. Exposes window.SLXDepthEngine.
//
// Naming convention (kept stable across the buy-side extension):
//   fetchXxxBids() — historical name, now returns BOTH `bids` and `asks` arrays.
//   We deliberately did NOT rename these to fetchXxxDepth, because callers
//   (orchestrator + dashboards) already key off the existing names. Lower
//   friction = less risk of breaking the sell path.
//
// Live venues (assembled 2026-05-31, all individually verified):
//   CEX (CORS-confirmed):  Bitget, BitMart, Kraken, Hotcoin, DigiFinex, KCEX
//   CEX (via Vercel proxy): MEXC, Gate, LBank, OrangeX, BingX, Toobit, Ourbit, WEEX
//   BSC on-chain:           PancakeSwapInfinity (CLPoolManager singleton),
//                           UniswapV3-BSC pool 0xc7EFB8...,
//                           UniswapV4 pool 0xfb58b9... (StateView wrapper)
//   Solana:                 Jupiter v6 quote API
//
// SLX decimals: 6 on BSC (verified on-chain), 6 on Solana.

(function () {
  "use strict";

  // ─── Constants ────────────────────────────────────────────────────────────
  const SLX_TOTAL_SUPPLY = 1_000_000_000;
  const BSC_RPC = "https://bsc-dataseed.bnbchain.org";

  // Master switch for the 6 CORS-blocked CEXes that go through our Vercel
  // proxy at /api/cex-depth. Flip to false to silence them without touching
  // the orchestrator wiring (useful if the proxy itself is down).
  const PROXIED_CEX_ENABLED = true;
  const PROXY_BASE = "/api/cex-depth";

  const SLX_DECIMALS = 6;
  const SLX_MINT_SOL = "SLXdx4BU9Hes3X3o3F5DauVcQyN1JbgWQGypcLNbqQu";   // Solana SLX mint (canonical)
  const USDC_MINT_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"; // Solana USDC

  // ─── In-memory caches ────────────────────────────────────────────────────
  const CEX_TTL_MS = 15_000;
  const BSC_TTL_MS = 30_000;
  const CEX_CACHE = new Map();
  const BSC_CACHE = new Map();

  async function cached(key, cache, ttlMs, fn) {
    const now = Date.now();
    const hit = cache.get(key);
    if (hit && now - hit.t < ttlMs) return hit.v;
    const v = await fn();
    cache.set(key, { t: now, v });
    return v;
  }

  // ═════════════════════════════════════════════════════════════════════════
  // CEX FETCHERS (live, CORS-verified)
  // ═════════════════════════════════════════════════════════════════════════

  // Fetches the live Bitget SLX/USDT bid + ask orderbook.
  async function fetchBitgetBids() {
    const venue = "Bitget";
    const url = "https://api.bitget.com/api/v2/spot/market/orderbook?symbol=SLXUSDT&limit=150";

    const res = await fetch(url, { headers: { accept: "application/json" } });
    if (!res.ok) {
      throw new Error(`${venue} orderbook HTTP ${res.status}`);
    }

    const json = await res.json();
    if (!json || json.code !== "00000" || !json.data || !Array.isArray(json.data.bids)) {
      throw new Error(`${venue} orderbook malformed response (code=${json && json.code})`);
    }

    const parseLevel = (lvl) => {
      let p, s;
      if (Array.isArray(lvl)) {
        p = lvl[0];
        s = lvl[1];
      } else if (lvl && typeof lvl === "object") {
        p = lvl.price ?? lvl.p ?? lvl[0];
        s = lvl.size ?? lvl.quantity ?? lvl.qty ?? lvl.amount ?? lvl.q ?? lvl[1];
      }
      const priceUsd = parseFloat(p);
      const sizeSlx = parseFloat(s);
      return [priceUsd, sizeSlx];
    };

    const bids = json.data.bids
      .map(parseLevel)
      .filter(([p, s]) => Number.isFinite(p) && Number.isFinite(s) && p > 0 && s > 0)
      .sort((a, b) => b[0] - a[0]);

    if (bids.length === 0) {
      throw new Error(`${venue} orderbook returned no usable bids`);
    }

    const asks = Array.isArray(json.data.asks)
      ? json.data.asks
          .map(parseLevel)
          .filter(([p, s]) => Number.isFinite(p) && Number.isFinite(s) && p > 0 && s > 0)
          .sort((a, b) => a[0] - b[0])
      : [];

    return {
      venue,
      type: "cex",
      bids,
      asks,
      midUsd: bids[0][0],
      fetchedAt: Date.now(),
    };
  }

  // Fetches SLX/USDT bid book from BitMart spot API.
  async function fetchBitMartBids() {
    const url = "https://api-cloud.bitmart.com/spot/quotation/v3/books?symbol=SLX_USDT&limit=50";
    const res = await fetch(url);
    if (!res.ok) {
      throw new Error(`BitMart HTTP ${res.status} ${res.statusText}`);
    }
    const json = await res.json();
    const rawBids = json && json.data && Array.isArray(json.data.bids) ? json.data.bids : null;
    if (!rawBids || rawBids.length === 0) {
      throw new Error("BitMart returned no bids for SLX_USDT");
    }

    const bids = [];
    for (const entry of rawBids) {
      let priceRaw, sizeRaw;
      if (Array.isArray(entry)) {
        priceRaw = entry[0];
        sizeRaw = entry[1];
      } else if (entry && typeof entry === "object") {
        priceRaw = entry.price ?? entry.p ?? entry[0];
        sizeRaw = entry.amount ?? entry.size ?? entry.quantity ?? entry.q ?? entry[1];
      } else {
        continue;
      }
      const priceUsd = parseFloat(priceRaw);
      const sizeSlx = parseFloat(sizeRaw);
      if (!isFinite(priceUsd) || !isFinite(sizeSlx) || priceUsd <= 0 || sizeSlx <= 0) continue;
      bids.push([priceUsd, sizeSlx]);
    }

    if (bids.length === 0) {
      throw new Error("BitMart bids parsed to empty array (unexpected format)");
    }
    bids.sort((a, b) => b[0] - a[0]);

    const rawAsks = json && json.data && Array.isArray(json.data.asks) ? json.data.asks : [];
    const asks = [];
    for (const entry of rawAsks) {
      let priceRaw, sizeRaw;
      if (Array.isArray(entry)) {
        priceRaw = entry[0];
        sizeRaw = entry[1];
      } else if (entry && typeof entry === "object") {
        priceRaw = entry.price ?? entry.p ?? entry[0];
        sizeRaw = entry.amount ?? entry.size ?? entry.quantity ?? entry.q ?? entry[1];
      } else {
        continue;
      }
      const priceUsd = parseFloat(priceRaw);
      const sizeSlx = parseFloat(sizeRaw);
      if (!isFinite(priceUsd) || !isFinite(sizeSlx) || priceUsd <= 0 || sizeSlx <= 0) continue;
      asks.push([priceUsd, sizeSlx]);
    }
    asks.sort((a, b) => a[0] - b[0]);

    return {
      venue: "BitMart",
      type: "cex",
      bids,
      asks,
      midUsd: bids[0][0],
      fetchedAt: Date.now(),
    };
  }

  // Kraken SLX/USD bid orderbook.
  async function fetchKrakenBids() {
    const url = "https://api.kraken.com/0/public/Depth?pair=SLXUSD&count=100";
    const res = await fetch(url);
    if (!res.ok) {
      throw new Error(`Kraken depth HTTP ${res.status}`);
    }
    const json = await res.json();
    if (json.error && json.error.length) {
      throw new Error(`Kraken depth API error: ${json.error.join(", ")}`);
    }
    const result = json && json.result;
    if (!result || typeof result !== "object") {
      throw new Error("Kraken depth: missing result object");
    }
    const book = Object.values(result)[0];
    if (!book || !Array.isArray(book.bids)) {
      throw new Error("Kraken depth: missing bids array");
    }
    const bids = [];
    for (const entry of book.bids) {
      let priceRaw, sizeRaw;
      if (Array.isArray(entry)) {
        priceRaw = entry[0];
        sizeRaw = entry[1];
      } else if (entry && typeof entry === "object") {
        priceRaw = entry.price ?? entry.p ?? entry[0];
        sizeRaw = entry.size ?? entry.volume ?? entry.qty ?? entry[1];
      } else {
        continue;
      }
      const priceUsd = parseFloat(priceRaw);
      const sizeSlx = parseFloat(sizeRaw);
      if (!Number.isFinite(priceUsd) || !Number.isFinite(sizeSlx)) continue;
      if (priceUsd <= 0 || sizeSlx <= 0) continue;
      bids.push([priceUsd, sizeSlx]);
    }
    if (!bids.length) {
      throw new Error("Kraken depth: empty bids after parse");
    }
    bids.sort((a, b) => b[0] - a[0]);

    const asks = [];
    if (Array.isArray(book.asks)) {
      for (const entry of book.asks) {
        let priceRaw, sizeRaw;
        if (Array.isArray(entry)) {
          priceRaw = entry[0];
          sizeRaw = entry[1];
        } else if (entry && typeof entry === "object") {
          priceRaw = entry.price ?? entry.p ?? entry[0];
          sizeRaw = entry.size ?? entry.volume ?? entry.qty ?? entry[1];
        } else {
          continue;
        }
        const priceUsd = parseFloat(priceRaw);
        const sizeSlx = parseFloat(sizeRaw);
        if (!Number.isFinite(priceUsd) || !Number.isFinite(sizeSlx)) continue;
        if (priceUsd <= 0 || sizeSlx <= 0) continue;
        asks.push([priceUsd, sizeSlx]);
      }
      asks.sort((a, b) => a[0] - b[0]);
    }

    return {
      venue: "Kraken",
      type: "cex",
      bids,
      asks,
      midUsd: bids[0][0],
      fetchedAt: Date.now(),
    };
  }

  // Fetches the live Hotcoin SLX/USDT bid orderbook.
  async function fetchHotcoinBids() {
    const url = "https://api.hotcoinfin.com/v1/depth?symbol=slx_usdt";
    const res = await fetch(url);
    if (!res.ok) {
      throw new Error(`Hotcoin depth HTTP ${res.status}: ${res.statusText}`);
    }
    const json = await res.json();
    if (json && json.code != null && json.code !== 200) {
      throw new Error(`Hotcoin depth API error code ${json.code}: ${json.msg || "unknown"}`);
    }

    const container = (json && json.data) || {};
    const rawBids =
      (container.depth && (container.depth.bids || container.depth.buys)) ||
      (container.tick && (container.tick.bids || container.tick.buys)) ||
      container.bids ||
      container.buys ||
      null;

    if (!Array.isArray(rawBids) || rawBids.length === 0) {
      throw new Error("Hotcoin depth returned no bids for slx_usdt");
    }

    const bids = [];
    for (const entry of rawBids) {
      let price, size;
      if (Array.isArray(entry)) {
        price = parseFloat(entry[0]);
        size = parseFloat(entry[1]);
      } else if (entry && typeof entry === "object") {
        price = parseFloat(entry.price ?? entry.p ?? entry[0]);
        size = parseFloat(entry.size ?? entry.amount ?? entry.quantity ?? entry.q ?? entry[1]);
      } else {
        continue;
      }
      if (Number.isFinite(price) && Number.isFinite(size) && price > 0 && size > 0) {
        bids.push([price, size]);
      }
    }

    if (bids.length === 0) {
      throw new Error("Hotcoin depth bids parsed to empty array");
    }
    bids.sort((a, b) => b[0] - a[0]);

    const rawAsks =
      (container.depth && (container.depth.asks || container.depth.sells)) ||
      (container.tick && (container.tick.asks || container.tick.sells)) ||
      container.asks ||
      container.sells ||
      [];
    const asks = [];
    if (Array.isArray(rawAsks)) {
      for (const entry of rawAsks) {
        let price, size;
        if (Array.isArray(entry)) {
          price = parseFloat(entry[0]);
          size = parseFloat(entry[1]);
        } else if (entry && typeof entry === "object") {
          price = parseFloat(entry.price ?? entry.p ?? entry[0]);
          size = parseFloat(entry.size ?? entry.amount ?? entry.quantity ?? entry.q ?? entry[1]);
        } else {
          continue;
        }
        if (Number.isFinite(price) && Number.isFinite(size) && price > 0 && size > 0) {
          asks.push([price, size]);
        }
      }
      asks.sort((a, b) => a[0] - b[0]);
    }

    return {
      venue: "Hotcoin",
      type: "cex",
      bids,
      asks,
      midUsd: bids[0][0],
      fetchedAt: Date.now(),
    };
  }

  // Fetches SLX/USDT bids from DigiFinex spot v3 REST.
  // CORS is fully open (Access-Control-Allow-Origin: *).
  // Response: { code:0, date:<unix>, bids:[[price,qty],...], asks:[...] }
  // bids are numeric pairs (not strings), pre-sorted descending by price.
  async function fetchDigiFinexBids() {
    const url = "https://openapi.digifinex.com/v3/order_book?symbol=slx_usdt&limit=100";
    const res = await fetch(url, { headers: { accept: "application/json" } });
    if (!res.ok) {
      throw new Error(`DigiFinex depth HTTP ${res.status}`);
    }
    const json = await res.json();
    if (json && typeof json.code === "number" && json.code !== 0) {
      throw new Error(`DigiFinex depth code=${json.code}`);
    }
    const rawBids = json && Array.isArray(json.bids) ? json.bids : null;
    if (!rawBids || rawBids.length === 0) {
      throw new Error("DigiFinex returned no bids for slx_usdt");
    }
    const bids = [];
    for (const entry of rawBids) {
      let priceRaw, sizeRaw;
      if (Array.isArray(entry)) {
        priceRaw = entry[0];
        sizeRaw = entry[1];
      } else if (entry && typeof entry === "object") {
        priceRaw = entry.price ?? entry.p ?? entry[0];
        sizeRaw = entry.size ?? entry.amount ?? entry.quantity ?? entry.q ?? entry[1];
      } else {
        continue;
      }
      const price = parseFloat(priceRaw);
      const size = parseFloat(sizeRaw);
      if (!Number.isFinite(price) || !Number.isFinite(size) || price <= 0 || size <= 0) continue;
      bids.push([price, size]);
    }
    if (bids.length === 0) {
      throw new Error("DigiFinex bids parsed to empty array");
    }
    bids.sort((a, b) => b[0] - a[0]);

    const rawAsks = json && Array.isArray(json.asks) ? json.asks : [];
    const asks = [];
    for (const entry of rawAsks) {
      let priceRaw, sizeRaw;
      if (Array.isArray(entry)) {
        priceRaw = entry[0];
        sizeRaw = entry[1];
      } else if (entry && typeof entry === "object") {
        priceRaw = entry.price ?? entry.p ?? entry[0];
        sizeRaw = entry.size ?? entry.amount ?? entry.quantity ?? entry.q ?? entry[1];
      } else {
        continue;
      }
      const price = parseFloat(priceRaw);
      const size = parseFloat(sizeRaw);
      if (!Number.isFinite(price) || !Number.isFinite(size) || price <= 0 || size <= 0) continue;
      asks.push([price, size]);
    }
    asks.sort((a, b) => a[0] - b[0]);

    return {
      venue: "DigiFinex",
      type: "cex",
      bids,
      asks,
      midUsd: bids[0][0],
      fetchedAt: Date.now(),
    };
  }

  // Fetches SLX/USDT bids from KCEX web-frontend depth endpoint.
  // CORS is open (Access-Control-Allow-Origin: *) on www.kcex.com (the api.kcex.com
  // host is CloudFront-WAF-blocked). Response shape is doubly-nested:
  //   { code:200, data:{ data:{ bids:[{p,q}], asks:[{p,q}] } } }
  // Bid entries are objects, not [price, qty] tuples — _parseLevelPair-style handling
  // is inlined here to keep the fetcher self-contained alongside the other direct ones.
  async function fetchKCEXBids() {
    const url = "https://www.kcex.com/api/platform/spot/market/depth?symbol=SLX_USDT&depth=20";
    const res = await fetch(url, { headers: { accept: "application/json" } });
    if (!res.ok) {
      throw new Error(`KCEX depth HTTP ${res.status}`);
    }
    const json = await res.json();
    if (json && typeof json.code === "number" && json.code !== 200 && json.code !== 0) {
      throw new Error(`KCEX depth code=${json.code} msg=${json.msg || "unknown"}`);
    }
    const rawBids =
      (json && json.data && json.data.data && Array.isArray(json.data.data.bids))
        ? json.data.data.bids
        : null;
    if (!rawBids || rawBids.length === 0) {
      throw new Error("KCEX returned no bids for SLX_USDT");
    }
    const bids = [];
    for (const entry of rawBids) {
      let priceRaw, sizeRaw;
      if (entry && typeof entry === "object" && !Array.isArray(entry)) {
        priceRaw = entry.p ?? entry.price ?? entry[0];
        sizeRaw = entry.q ?? entry.size ?? entry.quantity ?? entry.amount ?? entry[1];
      } else if (Array.isArray(entry)) {
        priceRaw = entry[0];
        sizeRaw = entry[1];
      } else {
        continue;
      }
      const price = parseFloat(priceRaw);
      const size = parseFloat(sizeRaw);
      if (!Number.isFinite(price) || !Number.isFinite(size) || price <= 0 || size <= 0) continue;
      bids.push([price, size]);
    }
    if (bids.length === 0) {
      throw new Error("KCEX bids parsed to empty array");
    }
    bids.sort((a, b) => b[0] - a[0]);

    const rawAsks =
      (json && json.data && json.data.data && Array.isArray(json.data.data.asks))
        ? json.data.data.asks
        : [];
    const asks = [];
    for (const entry of rawAsks) {
      let priceRaw, sizeRaw;
      if (entry && typeof entry === "object" && !Array.isArray(entry)) {
        priceRaw = entry.p ?? entry.price ?? entry[0];
        sizeRaw = entry.q ?? entry.size ?? entry.quantity ?? entry.amount ?? entry[1];
      } else if (Array.isArray(entry)) {
        priceRaw = entry[0];
        sizeRaw = entry[1];
      } else {
        continue;
      }
      const price = parseFloat(priceRaw);
      const size = parseFloat(sizeRaw);
      if (!Number.isFinite(price) || !Number.isFinite(size) || price <= 0 || size <= 0) continue;
      asks.push([price, size]);
    }
    asks.sort((a, b) => a[0] - b[0]);

    return {
      venue: "KCEX",
      type: "cex",
      bids,
      asks,
      midUsd: bids[0][0],
      fetchedAt: Date.now(),
    };
  }

  // ── KRW → USD conversion (cached per session) ────────────────────────────
  // Korean exchanges (Bithumb, Upbit) quote in KRW. Convert via Upbit's own
  // USDT-KRW ticker (no auth, CORS-friendly). Cached for the session — KRW/USD
  // moves slowly relative to crypto volatility so a single fetch is fine.
  let _krwUsdRateCache = { rate: null, ts: 0 };
  async function _getKrwToUsdRate() {
    const now = Date.now();
    if (_krwUsdRateCache.rate && (now - _krwUsdRateCache.ts) < 10 * 60_000) {
      return _krwUsdRateCache.rate;
    }
    try {
      // Upbit lists USDT as KRW-USDT (KRW is the quote in their naming).
      // trade_price is "how many KRW per 1 USDT" → invert for KRW→USD.
      const res = await fetch("https://api.upbit.com/v1/ticker?markets=KRW-USDT", {
        headers: { accept: "application/json" },
      });
      const j = await res.json();
      const trade_price = j && j[0] && parseFloat(j[0].trade_price);
      if (!Number.isFinite(trade_price) || trade_price < 500 || trade_price > 3000) {
        throw new Error("KRW-USDT out of range");
      }
      _krwUsdRateCache = { rate: 1 / trade_price, ts: now };
      return _krwUsdRateCache.rate;
    } catch (e) {
      return 1 / 1470;   // fallback to a recent typical rate (KRW-USDT ~1470 on 2026-06-01)
    }
  }

  // OKX intentionally NOT included for spot: as of 2026-06-01 OKX has only
  // SLX-USDT-SWAP (perp). No spot pair exists. Add a fetcher here only when
  // OKX lists SLX-USDT spot.

  // ── Bithumb spot SLX/KRW (Korean — newly listed 2026-06-01) ──────────────
  // Uses Bithumb's v1 (Upbit-style) endpoint since the legacy /public/orderbook
  // path returns "not a listed coin" for new listings.
  async function fetchBithumbBids() {
    const url = "https://api.bithumb.com/v1/orderbook?markets=KRW-SLX";
    const res = await fetch(url, { headers: { accept: "application/json" } });
    if (!res.ok) throw new Error(`Bithumb depth HTTP ${res.status}`);
    const json = await res.json();
    if (!Array.isArray(json) || !json[0]) {
      throw new Error("Bithumb malformed response");
    }
    const units = json[0].orderbook_units || [];
    const totalBidSize = parseFloat(json[0].total_bid_size || 0);
    const totalAskSize = parseFloat(json[0].total_ask_size || 0);
    if (units.length === 0 || (totalBidSize === 0 && totalAskSize === 0)) {
      throw new Error("Bithumb orderbook empty (listing not yet trading)");
    }
    const krwToUsd = await _getKrwToUsdRate();
    const bids = [];
    const asks = [];
    for (const u of units) {
      const bp = parseFloat(u.bid_price), bs = parseFloat(u.bid_size);
      const ap = parseFloat(u.ask_price), as = parseFloat(u.ask_size);
      if (Number.isFinite(bp) && Number.isFinite(bs) && bp > 0 && bs > 0) bids.push([bp * krwToUsd, bs]);
      if (Number.isFinite(ap) && Number.isFinite(as) && ap > 0 && as > 0) asks.push([ap * krwToUsd, as]);
    }
    bids.sort((a, b) => b[0] - a[0]);
    asks.sort((a, b) => a[0] - b[0]);
    if (bids.length === 0) throw new Error("Bithumb returned no bids");
    return { venue: "Bithumb", type: "cex", bids, asks, midUsd: bids[0][0], fetchedAt: Date.now() };
  }

  // ── Upbit spot SLX/KRW (Korean — triple-listed 2026-06-01) ───────────────
  async function fetchUpbitBids() {
    const url = "https://api.upbit.com/v1/orderbook?markets=KRW-SLX";
    const res = await fetch(url, { headers: { accept: "application/json" } });
    if (!res.ok) throw new Error(`Upbit depth HTTP ${res.status}`);
    const json = await res.json();
    if (!Array.isArray(json) || !json[0] || !Array.isArray(json[0].orderbook_units)) {
      throw new Error("Upbit malformed response");
    }
    const krwToUsd = await _getKrwToUsdRate();
    const bids = [];
    const asks = [];
    for (const u of json[0].orderbook_units) {
      const bp = parseFloat(u.bid_price), bs = parseFloat(u.bid_size);
      const ap = parseFloat(u.ask_price), as = parseFloat(u.ask_size);
      if (Number.isFinite(bp) && Number.isFinite(bs) && bp > 0 && bs > 0) {
        bids.push([bp * krwToUsd, bs]);
      }
      if (Number.isFinite(ap) && Number.isFinite(as) && ap > 0 && as > 0) {
        asks.push([ap * krwToUsd, as]);
      }
    }
    bids.sort((a, b) => b[0] - a[0]);
    asks.sort((a, b) => a[0] - b[0]);
    if (bids.length === 0) throw new Error("Upbit returned no bids");
    return { venue: "Upbit", type: "cex", bids, asks, midUsd: bids[0][0], fetchedAt: Date.now() };
  }

  // ═════════════════════════════════════════════════════════════════════════
  // PROXIED CEX FETCHERS (via /api/cex-depth — CORS-blocked upstreams)
  // ═════════════════════════════════════════════════════════════════════════
  // These call our Vercel serverless function which forwards to the real CEX
  // endpoint. Shape varies per upstream so each fetcher knows its own JSON
  // path to the bids array. Output contract is identical to the direct
  // fetchers above: { venue, type:"cex", bids:[[price,qty]...], midUsd, fetchedAt }.

  // Parses a [price, qty] level (array or object) into a numeric tuple.
  // Returns null if either field is missing / not finite / non-positive.
  function _parseLevelPair(lvl) {
    let p, s;
    if (Array.isArray(lvl)) {
      p = lvl[0];
      s = lvl[1];
    } else if (lvl && typeof lvl === "object") {
      p = lvl.price ?? lvl.p ?? lvl[0];
      s = lvl.size ?? lvl.quantity ?? lvl.qty ?? lvl.amount ?? lvl.q ?? lvl[1];
    } else {
      return null;
    }
    const price = parseFloat(p);
    const qty = parseFloat(s);
    if (!Number.isFinite(price) || !Number.isFinite(qty) || price <= 0 || qty <= 0) return null;
    return [price, qty];
  }

  // GETs the proxy endpoint and returns parsed JSON. Throws with a useful
  // message on transport / HTTP / proxy-reported upstream failure.
  async function _proxyFetch(venueKey) {
    const url = `${PROXY_BASE}?venue=${encodeURIComponent(venueKey)}`;
    const res = await fetch(url, { headers: { accept: "application/json" } });
    const text = await res.text();
    let json;
    try {
      json = JSON.parse(text);
    } catch (e) {
      throw new Error(`${venueKey} proxy: non-JSON response (HTTP ${res.status})`);
    }
    if (!res.ok) {
      const upstreamMsg = json && json.error ? json.error : `HTTP ${res.status}`;
      throw new Error(`${venueKey} proxy: ${upstreamMsg}`);
    }
    return json;
  }

  // Generic finisher: take a raw bids array, parse + sort + assemble the
  // venue record. Throws if no usable levels.
  function _finishCexBids(venue, rawBids) {
    if (!Array.isArray(rawBids) || rawBids.length === 0) {
      throw new Error(`${venue} returned no bids`);
    }
    const bids = rawBids.map(_parseLevelPair).filter(Boolean).sort((a, b) => b[0] - a[0]);
    if (bids.length === 0) {
      throw new Error(`${venue} bids parsed to empty array`);
    }
    return {
      venue,
      type: "cex",
      bids,
      midUsd: bids[0][0],
      fetchedAt: Date.now(),
    };
  }

  // Parallel to _finishCexBids: parse a raw asks array. Unlike bids we do NOT
  // throw if asks come back empty — that just means the proxy upstream didn't
  // include them, and the buy side should silently skip this venue.
  function _finishCexAsks(rawAsks) {
    if (!Array.isArray(rawAsks) || rawAsks.length === 0) return [];
    return rawAsks.map(_parseLevelPair).filter(Boolean).sort((a, b) => a[0] - b[0]);
  }

  // Convenience: take a fully-built bid record and a raw asks array, attach
  // parsed asks onto the record, return record. Used by the proxied fetchers.
  function _attachAsks(rec, rawAsks) {
    rec.asks = _finishCexAsks(rawAsks);
    return rec;
  }

  // MEXC — shape: { bids: [[price, qty]], asks: [...] }
  async function fetchMEXCBids() {
    const j = await _proxyFetch("mexc");
    return _attachAsks(_finishCexBids("MEXC", j && j.bids), j && j.asks);
  }

  // Gate.io — shape: { bids: [[price, qty]], asks: [...] }
  async function fetchGateBids() {
    const j = await _proxyFetch("gate");
    return _attachAsks(_finishCexBids("Gate", j && j.bids), j && j.asks);
  }

  // LBank — shape: { data: { bids: [[price, qty]], asks: [...] }, result: "true" }
  async function fetchLBankBids() {
    const j = await _proxyFetch("lbank");
    if (j && j.result && j.result !== "true" && j.result !== true) {
      throw new Error(`LBank result=${j.result} msg=${j.msg || ""}`);
    }
    return _attachAsks(
      _finishCexBids("LBank", j && j.data && j.data.bids),
      j && j.data && j.data.asks
    );
  }

  // OrangeX (Deribit-style JSON-RPC) — shape: { result: { bids: [[price, qty]], asks: [...] } }
  async function fetchOrangeXBids() {
    const j = await _proxyFetch("orangex");
    return _attachAsks(
      _finishCexBids("OrangeX", j && j.result && j.result.bids),
      j && j.result && j.result.asks
    );
  }

  // BingX — shape (when listed): { code:0, data: { bids: [[price, qty]], asks: [...] } }
  // Currently returns { code:100204, msg:"symbol is not found." } — fetcher
  // will surface that and the orchestrator drops it like any other failure.
  async function fetchBingXBids() {
    const j = await _proxyFetch("bingx");
    if (j && typeof j.code === "number" && j.code !== 0) {
      throw new Error(`BingX code=${j.code} msg=${j.msg || "unknown"}`);
    }
    const rawBids =
      (j && j.data && j.data.bids) ||
      (j && j.bids) ||
      null;
    const rawAsks =
      (j && j.data && j.data.asks) ||
      (j && j.asks) ||
      null;
    return _attachAsks(_finishCexBids("BingX", rawBids), rawAsks);
  }

  // Toobit — shape: { t: <ts>, b: [[price, qty]], a: [...] }
  async function fetchToobitBids() {
    const j = await _proxyFetch("toobit");
    return _attachAsks(
      _finishCexBids("Toobit", j && (j.b || j.bids)),
      j && (j.a || j.asks)
    );
  }

  // Ourbit — Binance-clone shape: { bids: [[price_str, qty_str]], asks: [...] }
  async function fetchOurbitBids() {
    const j = await _proxyFetch("ourbit");
    return _attachAsks(_finishCexBids("Ourbit", j && j.bids), j && j.asks);
  }

  // WEEX — Binance-style: { lastUpdateId, bids: [[price_str, qty_str]], asks: [...] }
  // Surfaces upstream error codes (e.g. -1142 for bad `limit`) as fetcher errors.
  async function fetchWEEXBids() {
    const j = await _proxyFetch("weex");
    if (j && typeof j.code === "number" && j.code !== 0 && j.code !== 200) {
      throw new Error(`WEEX code=${j.code} msg=${j.msg || "unknown"}`);
    }
    return _attachAsks(_finishCexBids("WEEX", j && j.bids), j && j.asks);
  }

  // ═════════════════════════════════════════════════════════════════════════
  // BSC ON-CHAIN READERS
  // ═════════════════════════════════════════════════════════════════════════

  async function _bscEthCall(to, data) {
    const res = await fetch(BSC_RPC, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        method: "eth_call",
        params: [{ to, data }, "latest"],
      }),
    });
    const j = await res.json();
    if (j.error) throw new Error(`eth_call failed: ${j.error.message || JSON.stringify(j.error)}`);
    if (!j.result || j.result === "0x") throw new Error("eth_call returned empty");
    return j.result;
  }

  // ── PancakeSwap Infinity (BSC) ───────────────────────────────────────────
  // Singleton CLPoolManager; per-pool state lives in mappings keyed by bytes32 PoolId.
  const PCS_INF_MANAGER = "0xa0FfB9c1CE1Fe56963B0321B32E7A0302114058b";
  const PCS_INF_POOL_ID = "1a96f5b1dc28fd3c9e3772c255e5541b8635c20db1c88f0f5156c5be94ad58cb";
  const PCS_INF_SEL_SLOT0     = "0xc815641c"; // getSlot0(bytes32)
  const PCS_INF_SEL_LIQUIDITY = "0xfa6793d5"; // getLiquidity(bytes32)
  const PCS_INF_SEL_TICK_INFO = "0x5aa208a4"; // getPoolTickInfo(bytes32,int24)
  const PCS_INF_SEL_BITMAP    = "0x7c352ef6"; // getPoolBitmapInfo(bytes32,int16)
  const PCS_INF_TICK_SPACING  = 10;            // confirmed via on-chain bitmap probe (lpFee≈dynamic)
  const PCS_INF_MIN_SQRT_RATIO = 4295128739n;  // Uniswap V3 constant; PCS CL uses same TickMath
  const PCS_INF_Q96 = 2n ** 96n;

  // Tick/bitmap data only changes when an LP adds/removes liquidity — rare vs swap frequency.
  // 60s TTL is safe and amortizes RPC cost across many quotes within the same dashboard tick.
  const PCS_INF_TICK_TTL_MS = 60_000;
  const PCS_INF_TICK_CACHE = new Map();

  async function _pcsInfTickCached(key, fn) {
    const now = Date.now();
    const hit = PCS_INF_TICK_CACHE.get(key);
    if (hit && now - hit.t < PCS_INF_TICK_TTL_MS) return hit.v;
    const v = await fn();
    PCS_INF_TICK_CACHE.set(key, { t: now, v });
    return v;
  }

  function _pcsInfEncInt24(n) {
    return n >= 0
      ? n.toString(16).padStart(64, "0")
      : ((1n << 256n) + BigInt(n)).toString(16).padStart(64, "0");
  }
  function _pcsInfEncInt16(n) {
    return n >= 0
      ? n.toString(16).padStart(64, "0")
      : ((1n << 256n) + BigInt(n)).toString(16).padStart(64, "0");
  }

  // ABI-padded int128: data is in the LOW 128 bits, sign-extended into the high half.
  function _pcsInfDecI128Slot(hex64) {
    const low = BigInt("0x" + hex64) & ((1n << 128n) - 1n);
    return low >= (1n << 127n) ? low - (1n << 128n) : low;
  }
  function _pcsInfDecU128Slot(hex64) {
    return BigInt("0x" + hex64) & ((1n << 128n) - 1n);
  }

  async function _pcsInfGetTickInfo(tick) {
    return _pcsInfTickCached("tick:" + tick, async () => {
      const raw = await _bscEthCall(PCS_INF_MANAGER, PCS_INF_SEL_TICK_INFO + PCS_INF_POOL_ID + _pcsInfEncInt24(tick));
      const h = raw.slice(2);
      return {
        liquidityGross: _pcsInfDecU128Slot(h.slice(0, 64)),
        liquidityNet:   _pcsInfDecI128Slot(h.slice(64, 128)),
      };
    });
  }
  async function _pcsInfGetBitmap(wordPos) {
    return _pcsInfTickCached("bm:" + wordPos, async () => {
      return BigInt(await _bscEthCall(PCS_INF_MANAGER, PCS_INF_SEL_BITMAP + PCS_INF_POOL_ID + _pcsInfEncInt16(wordPos)));
    });
  }

  // Largest initialized tick strictly below tickCurrent. Scans bitmap words downward
  // (Uniswap V3 style) up to scanLimitWords (each word covers 256 ticks × spacing).
  async function _pcsInfNextInitializedTickBelow(tickCurrent, spacing, scanLimitWords) {
    const compressed = Math.floor(tickCurrent / spacing);
    const scanCompressed = compressed - 1; // strictly below
    const wordPos = scanCompressed >> 8;
    const startBit = scanCompressed & 0xff;
    for (let wi = 0; wi < scanLimitWords; wi++) {
      const w = wordPos - wi;
      let bm;
      try { bm = await _pcsInfGetBitmap(w); } catch { return null; }
      if (bm !== 0n) {
        const limit = wi === 0 ? startBit : 255;
        for (let b = limit; b >= 0; b--) {
          if ((bm >> BigInt(b)) & 1n) return (w * 256 + b) * spacing;
        }
      }
    }
    return null;
  }

  // TickMath.getSqrtRatioAtTick — exact Uniswap V3 fixed-point implementation.
  function _pcsInfGetSqrtRatioAtTick(tick) {
    const absTick = BigInt(tick < 0 ? -tick : tick);
    if (absTick > 887272n) throw new Error("PCS_INF: tick out of bounds");
    let ratio = (absTick & 0x1n) !== 0n
      ? 0xfffcb933bd6fad37aa2d162d1a594001n
      : 0x100000000000000000000000000000000n;
    if ((absTick & 0x2n)     !== 0n) ratio = (ratio * 0xfff97272373d413259a46990580e213an) >> 128n;
    if ((absTick & 0x4n)     !== 0n) ratio = (ratio * 0xfff2e50f5f656932ef12357cf3c7fdccn) >> 128n;
    if ((absTick & 0x8n)     !== 0n) ratio = (ratio * 0xffe5caca7e10e4e61c3624eaa0941cd0n) >> 128n;
    if ((absTick & 0x10n)    !== 0n) ratio = (ratio * 0xffcb9843d60f6159c9db58835c926644n) >> 128n;
    if ((absTick & 0x20n)    !== 0n) ratio = (ratio * 0xff973b41fa98c081472e6896dfb254c0n) >> 128n;
    if ((absTick & 0x40n)    !== 0n) ratio = (ratio * 0xff2ea16466c96a3843ec78b326b52861n) >> 128n;
    if ((absTick & 0x80n)    !== 0n) ratio = (ratio * 0xfe5dee046a99a2a811c461f1969c3053n) >> 128n;
    if ((absTick & 0x100n)   !== 0n) ratio = (ratio * 0xfcbe86c7900a88aedcffc83b479aa3a4n) >> 128n;
    if ((absTick & 0x200n)   !== 0n) ratio = (ratio * 0xf987a7253ac413176f2b074cf7815e54n) >> 128n;
    if ((absTick & 0x400n)   !== 0n) ratio = (ratio * 0xf3392b0822b70005940c7a398e4b70f3n) >> 128n;
    if ((absTick & 0x800n)   !== 0n) ratio = (ratio * 0xe7159475a2c29b7443b29c7fa6e889d9n) >> 128n;
    if ((absTick & 0x1000n)  !== 0n) ratio = (ratio * 0xd097f3bdfd2022b8845ad8f792aa5825n) >> 128n;
    if ((absTick & 0x2000n)  !== 0n) ratio = (ratio * 0xa9f746462d870fdf8a65dc1f90e061e5n) >> 128n;
    if ((absTick & 0x4000n)  !== 0n) ratio = (ratio * 0x70d869a156d2a1b890bb3df62baf32f7n) >> 128n;
    if ((absTick & 0x8000n)  !== 0n) ratio = (ratio * 0x31be135f97d08fd981231505542fcfa6n) >> 128n;
    if ((absTick & 0x10000n) !== 0n) ratio = (ratio * 0x9aa508b5b7a84e1c677de54f3e99bc9n)  >> 128n;
    if ((absTick & 0x20000n) !== 0n) ratio = (ratio * 0x5d6af8dedb81196699c329225ee604n)   >> 128n;
    if ((absTick & 0x40000n) !== 0n) ratio = (ratio * 0x2216e584f5fa1ea926041bedfe98n)     >> 128n;
    if ((absTick & 0x80000n) !== 0n) ratio = (ratio * 0x48a170391f7dc42444e8fa2n)          >> 128n;
    if (tick > 0) ratio = (1n << 256n) / ratio;
    const rem = ratio & ((1n << 32n) - 1n);
    return (ratio >> 32n) + (rem === 0n ? 0n : 1n);
  }

  async function fetchPancakeSwapInfinityState() {
    const slot0Hex = await _bscEthCall(PCS_INF_MANAGER, PCS_INF_SEL_SLOT0 + PCS_INF_POOL_ID);
    const liqHex   = await _bscEthCall(PCS_INF_MANAGER, PCS_INF_SEL_LIQUIDITY + PCS_INF_POOL_ID);

    const s = slot0Hex.startsWith("0x") ? slot0Hex.slice(2) : slot0Hex;
    if (s.length < 256) throw new Error("getSlot0 returned " + s.length / 2 + " bytes, expected 128");

    const sqrtPriceX96 = BigInt("0x" + s.slice(0, 64));
    let tickRaw = BigInt("0x" + s.slice(64, 128));
    const SIGN_BIT_24 = 1n << 23n;
    const MASK_24 = (1n << 24n) - 1n;
    tickRaw = tickRaw & MASK_24;
    const tick = Number(tickRaw & SIGN_BIT_24 ? tickRaw - (1n << 24n) : tickRaw);
    const protocolFee = Number(BigInt("0x" + s.slice(128, 192)));
    const lpFee = Number(BigInt("0x" + s.slice(192, 256)));

    const lHex = liqHex.startsWith("0x") ? liqHex.slice(2) : liqHex;
    const liquidity = BigInt("0x" + lHex);

    const priceScaled = (sqrtPriceX96 * sqrtPriceX96 * (10n ** 18n)) / (PCS_INF_Q96 * PCS_INF_Q96 * (10n ** 12n));
    const midUsd = Number(priceScaled) / 1e18;

    return {
      venue: "PancakeSwapInfinity",
      type: "dex_bsc",
      state: {
        sqrtPriceX96: sqrtPriceX96.toString(),
        liquidity: liquidity.toString(),
        tick,
        protocolFee,
        lpFee,
        tickSpacing: PCS_INF_TICK_SPACING,
        token0: { address: "0x02bcc4c181b83a8c0a342bc003389cbecb4bc54d", symbol: "SLX", decimals: 6 },
        token1: { address: "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", symbol: "USDC", decimals: 18 },
        slxIsToken0: true,
      },
      midUsd,
      fetchedAt: Date.now(),
    };
  }

  // In-range constant-product slippage. Cheap (no RPC), accurate only while the trade
  // stays inside the current tick range.
  function _pcsInfInRangeSell(state, slxInRaw) {
    const sqrtP = BigInt(state.state.sqrtPriceX96);
    const L = BigInt(state.state.liquidity);
    if (L === 0n) throw new Error("zero in-range liquidity");
    const dec0 = state.state.token0.decimals;
    const dec1 = state.state.token1.decimals;

    const denom = L + (slxInRaw * sqrtP) / PCS_INF_Q96;
    const sqrtNew = (sqrtP * L) / denom;
    const outRaw = (L * (sqrtP - sqrtNew)) / PCS_INF_Q96;

    const usdOut = Number(outRaw) / Math.pow(10, dec1);
    const slxIn = Number(slxInRaw) / Math.pow(10, dec0);
    const avgPrice = slxIn > 0 ? usdOut / slxIn : 0;
    const spot = state.midUsd;
    const priceImpactPct = spot > 0 ? ((spot - avgPrice) / spot) * 100 : 0;
    return { usdOut, priceImpactPct, avgPrice, mode: "in-range", ticksCrossed: 0 };
  }

  // Tick-walking sell of token0 (SLX) for token1 (USDC). Crosses initialized ticks below
  // the current price, updating active L by subtracting liquidityNet at each crossing
  // (zeroForOne direction). Caps at maxIter ticks; returns partial fill + warning on overflow.
  async function PancakeSwapInfinityTickWalkSell(state, slxInRaw, opts) {
    if (typeof slxInRaw !== "bigint") slxInRaw = BigInt(slxInRaw);
    if (slxInRaw <= 0n) throw new Error("slxInRaw must be > 0");
    const maxIter   = (opts && opts.maxIter)   || 50;
    const scanWords = (opts && opts.scanWords) || 30;
    const spacing = state.state.tickSpacing || PCS_INF_TICK_SPACING;
    const dec0 = state.state.token0.decimals;
    const dec1 = state.state.token1.decimals;

    let sqrtP = BigInt(state.state.sqrtPriceX96);
    let tick  = state.state.tick;
    let L     = BigInt(state.state.liquidity);
    const sqrtP_initial = sqrtP;

    let remaining = slxInRaw;
    let totalOut = 0n;
    let ticksCrossed = 0;
    let warning = null;

    for (let iter = 0; iter < maxIter && remaining > 0n; iter++) {
      if (L === 0n) {
        // Dead zone — jump to the next initialized tick below and pick up liquidity.
        const tn = await _pcsInfNextInitializedTickBelow(tick, spacing, scanWords);
        if (tn === null) { warning = "L=0 in dead zone and no further init ticks"; break; }
        const info = await _pcsInfGetTickInfo(tn);
        L = L - info.liquidityNet;
        sqrtP = _pcsInfGetSqrtRatioAtTick(tn);
        tick = tn - 1;
        ticksCrossed++;
        continue;
      }
      const tickNext = await _pcsInfNextInitializedTickBelow(tick, spacing, scanWords);
      let sqrtPNext;
      let isBoundary = false;
      if (tickNext === null) {
        sqrtPNext = PCS_INF_MIN_SQRT_RATIO + 1n;
        warning = "no initialized tick within scan window; using MIN_SQRT_RATIO";
      } else {
        sqrtPNext = _pcsInfGetSqrtRatioAtTick(tickNext);
        isBoundary = true;
      }

      // Exact V3: amount0 = ceil( L * (sqrtP - sqrtPNext) * Q96 / (sqrtP * sqrtPNext) )
      const num = L * (sqrtP - sqrtPNext) * PCS_INF_Q96;
      const denomCross = sqrtP * sqrtPNext;
      const amount0ToNext = num / denomCross + (num % denomCross === 0n ? 0n : 1n);

      if (amount0ToNext > remaining) {
        // Trade ends inside this range. sqrtP_new = L * sqrtP * Q96 / (L*Q96 + remaining*sqrtP)
        const sqrtPnew = (L * sqrtP * PCS_INF_Q96) / (L * PCS_INF_Q96 + remaining * sqrtP);
        totalOut += (L * (sqrtP - sqrtPnew)) / PCS_INF_Q96;
        sqrtP = sqrtPnew;
        remaining = 0n;
        break;
      }
      // Consume full segment, cross the tick.
      totalOut += (L * (sqrtP - sqrtPNext)) / PCS_INF_Q96;
      remaining -= amount0ToNext;
      sqrtP = sqrtPNext;
      if (!isBoundary) {
        warning = warning || "reached MIN_SQRT_RATIO — partial fill";
        break;
      }
      const info = await _pcsInfGetTickInfo(tickNext);
      L = L - info.liquidityNet; // zeroForOne: L -= liquidityNet
      if (L < 0n) { warning = "negative L after crossing " + tickNext + " — partial fill"; L = 0n; break; }
      tick = tickNext - 1;
      ticksCrossed++;
    }

    if (remaining > 0n && !warning) {
      warning = "maxIter (" + maxIter + ") reached — partial fill";
    }

    const usdOut = Number(totalOut) / Math.pow(10, dec1);
    const filledHuman = Number(slxInRaw - remaining) / Math.pow(10, dec0);
    const avgPrice = filledHuman > 0 ? usdOut / filledHuman : 0;
    const sqrtPnumInit = Number(sqrtP_initial) / Number(PCS_INF_Q96);
    const spot = sqrtPnumInit * sqrtPnumInit * Math.pow(10, dec0 - dec1);
    const priceImpactPct = spot > 0 ? ((spot - avgPrice) / spot) * 100 : 0;

    return {
      usdOut, priceImpactPct, avgPrice, ticksCrossed,
      mode: "tick-walked",
      finalSqrtP: sqrtP.toString(),
      slxUnfilled: Number(remaining) / Math.pow(10, dec0),
      warning,
    };
  }

  // Synchronous entry — preserves the existing API surface used by _ammToBidCurve
  // and the orchestrator. Returns the in-range constant-product approximation.
  // For accurate quotes on large trades, callers should use the async variant below.
  function PancakeSwapInfinitySellSlippage(state, slxInRaw) {
    if (typeof slxInRaw !== "bigint") slxInRaw = BigInt(slxInRaw);
    if (slxInRaw <= 0n) throw new Error("slxInRaw must be > 0");
    return _pcsInfInRangeSell(state, slxInRaw);
  }

  // Adaptive quote: returns in-range math for small trades (cheap, no RPC) and tick-walks
  // larger ones. Threshold = trade would push price > 0.5% with in-range L alone (i.e. likely
  // crosses at least one tick boundary at spacing=10). Returned shape matches the sync version.
  async function PancakeSwapInfinitySellSlippageAdaptive(state, slxInRaw, opts) {
    if (typeof slxInRaw !== "bigint") slxInRaw = BigInt(slxInRaw);
    if (slxInRaw <= 0n) throw new Error("slxInRaw must be > 0");

    // Cheap pre-check: in-range sqrtP_new and compare to current. If |Δprice| < 0.5%, skip the walk.
    const sqrtP = BigInt(state.state.sqrtPriceX96);
    const L = BigInt(state.state.liquidity);
    if (L === 0n) {
      // Skip the in-range fast path — must walk to find liquidity.
      return PancakeSwapInfinityTickWalkSell(state, slxInRaw, opts);
    }
    const sqrtNew = (sqrtP * L) / (L + (slxInRaw * sqrtP) / PCS_INF_Q96);
    // (sqrtP/sqrtNew)^2 ≈ priceRatio. Move > 0.5% if sqrtP*1000 > sqrtNew*1003 roughly.
    // Use direct bigint compare to avoid floating-point drift.
    if (sqrtP * 1000n <= sqrtNew * 1003n) {
      return _pcsInfInRangeSell(state, slxInRaw);
    }
    return PancakeSwapInfinityTickWalkSell(state, slxInRaw, opts);
  }

  // ── PancakeSwap Infinity BUY side (oneForZero: USDC in, SLX out, price UP) ───
  //
  // V3 math for token1 input that raises price (oneForZero, exact-in token1):
  //   sqrtP_new = sqrtP_old + (usdtIn * Q96) / L          // amount1 → ΔsqrtP up
  //   slxOut_raw = L * (sqrtP_new - sqrtP_old) * Q96 / (sqrtP_new * sqrtP_old)
  //
  // Both formulas use token1's "raw" amount (i.e. scaled by 10^dec1). On
  // PancakeSwap Infinity the SLX/USDC pool uses USDC with 18 decimals here
  // (pool is bridged-USDC), so usdcInRaw = usdHuman * 1e18.
  function _pcsInfInRangeBuy(state, usdcInRaw) {
    const sqrtP = BigInt(state.state.sqrtPriceX96);
    const L = BigInt(state.state.liquidity);
    if (L === 0n) throw new Error("zero in-range liquidity");
    const dec0 = state.state.token0.decimals;
    const dec1 = state.state.token1.decimals;

    const sqrtNew = sqrtP + (usdcInRaw * PCS_INF_Q96) / L;
    // amount0 (SLX out) = L * (sqrtNew - sqrtP) * Q96 / (sqrtNew * sqrtP)
    const outRaw = (L * (sqrtNew - sqrtP) * PCS_INF_Q96) / (sqrtNew * sqrtP);

    const slxOut = Number(outRaw) / Math.pow(10, dec0);
    const usdIn = Number(usdcInRaw) / Math.pow(10, dec1);
    const avgPrice = slxOut > 0 ? usdIn / slxOut : 0;
    const spot = state.midUsd;
    // Buyer slippage: paying MORE per SLX than spot is bad → positive number.
    const priceImpactPct = spot > 0 ? ((avgPrice - spot) / spot) * 100 : 0;
    return { slxOut, priceImpactPct, avgPrice, mode: "in-range", ticksCrossed: 0 };
  }

  // Largest initialized tick strictly ABOVE tickCurrent. Mirror of NextInitializedTickBelow.
  async function _pcsInfNextInitializedTickAbove(tickCurrent, spacing, scanLimitWords) {
    const compressed = Math.floor(tickCurrent / spacing);
    const scanCompressed = compressed + 1; // strictly above
    const wordPos = scanCompressed >> 8;
    const startBit = scanCompressed & 0xff;
    for (let wi = 0; wi < scanLimitWords; wi++) {
      const w = wordPos + wi;
      let bm;
      try { bm = await _pcsInfGetBitmap(w); } catch { return null; }
      if (bm !== 0n) {
        const lo = wi === 0 ? startBit : 0;
        for (let b = lo; b <= 255; b++) {
          if ((bm >> BigInt(b)) & 1n) return (w * 256 + b) * spacing;
        }
      }
    }
    return null;
  }

  // Uniswap V3 MAX_SQRT_RATIO (mirror of MIN). Highest sqrtP the math permits.
  const PCS_INF_MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342n;

  // Tick-walking BUY of token1 (USDC) for token0 (SLX). Walks UP through initialized
  // ticks; at each crossing L is updated by +liquidityNet (oneForZero direction).
  async function PancakeSwapInfinityTickWalkBuy(state, usdcInRaw, opts) {
    if (typeof usdcInRaw !== "bigint") usdcInRaw = BigInt(usdcInRaw);
    if (usdcInRaw <= 0n) throw new Error("usdcInRaw must be > 0");
    const maxIter   = (opts && opts.maxIter)   || 50;
    const scanWords = (opts && opts.scanWords) || 30;
    const spacing = state.state.tickSpacing || PCS_INF_TICK_SPACING;
    const dec0 = state.state.token0.decimals;
    const dec1 = state.state.token1.decimals;

    let sqrtP = BigInt(state.state.sqrtPriceX96);
    let tick  = state.state.tick;
    let L     = BigInt(state.state.liquidity);
    const sqrtP_initial = sqrtP;

    let remaining = usdcInRaw;
    let totalOut = 0n; // SLX raw out
    let ticksCrossed = 0;
    let warning = null;

    for (let iter = 0; iter < maxIter && remaining > 0n; iter++) {
      if (L === 0n) {
        // Dead zone — jump up to the next initialized tick and pick up liquidity.
        const tn = await _pcsInfNextInitializedTickAbove(tick, spacing, scanWords);
        if (tn === null) { warning = "L=0 in dead zone and no further init ticks"; break; }
        const info = await _pcsInfGetTickInfo(tn);
        L = L + info.liquidityNet; // oneForZero: L += liquidityNet on upward crossing
        sqrtP = _pcsInfGetSqrtRatioAtTick(tn);
        tick = tn; // resume at the activated tick
        ticksCrossed++;
        continue;
      }
      const tickNext = await _pcsInfNextInitializedTickAbove(tick, spacing, scanWords);
      let sqrtPNext;
      let isBoundary = false;
      if (tickNext === null) {
        sqrtPNext = PCS_INF_MAX_SQRT_RATIO - 1n;
        warning = "no initialized tick within scan window; using MAX_SQRT_RATIO";
      } else {
        sqrtPNext = _pcsInfGetSqrtRatioAtTick(tickNext);
        isBoundary = true;
      }

      // Exact V3: amount1 to move sqrtP from sqrtP → sqrtPNext (up)
      //   amount1 = ceil( L * (sqrtPNext - sqrtP) / Q96 )
      const numA1 = L * (sqrtPNext - sqrtP);
      const amount1ToNext = numA1 / PCS_INF_Q96 + (numA1 % PCS_INF_Q96 === 0n ? 0n : 1n);

      if (amount1ToNext > remaining) {
        // Trade ends inside this range:
        //   sqrtP_new = sqrtP + remaining * Q96 / L
        const sqrtPnew = sqrtP + (remaining * PCS_INF_Q96) / L;
        // SLX out: L * (sqrtPnew - sqrtP) * Q96 / (sqrtPnew * sqrtP)
        totalOut += (L * (sqrtPnew - sqrtP) * PCS_INF_Q96) / (sqrtPnew * sqrtP);
        sqrtP = sqrtPnew;
        remaining = 0n;
        break;
      }
      // Consume the full segment up to sqrtPNext.
      totalOut += (L * (sqrtPNext - sqrtP) * PCS_INF_Q96) / (sqrtPNext * sqrtP);
      remaining -= amount1ToNext;
      sqrtP = sqrtPNext;
      if (!isBoundary) {
        warning = warning || "reached MAX_SQRT_RATIO — partial fill";
        break;
      }
      const info = await _pcsInfGetTickInfo(tickNext);
      L = L + info.liquidityNet; // oneForZero: L += liquidityNet
      if (L < 0n) { warning = "negative L after crossing " + tickNext + " — partial fill"; L = 0n; break; }
      tick = tickNext;
      ticksCrossed++;
    }

    if (remaining > 0n && !warning) {
      warning = "maxIter (" + maxIter + ") reached — partial fill";
    }

    const slxOut = Number(totalOut) / Math.pow(10, dec0);
    const filledHuman = Number(usdcInRaw - remaining) / Math.pow(10, dec1);
    const avgPrice = slxOut > 0 ? filledHuman / slxOut : 0;
    const sqrtPnumInit = Number(sqrtP_initial) / Number(PCS_INF_Q96);
    const spot = sqrtPnumInit * sqrtPnumInit * Math.pow(10, dec0 - dec1);
    const priceImpactPct = spot > 0 ? ((avgPrice - spot) / spot) * 100 : 0;

    return {
      slxOut, priceImpactPct, avgPrice, ticksCrossed,
      mode: "tick-walked",
      finalSqrtP: sqrtP.toString(),
      usdUnfilled: Number(remaining) / Math.pow(10, dec1),
      warning,
    };
  }

  // Adaptive BUY: in-range for small trades, tick walker for larger.
  // Threshold gate identical to the sell side: if in-range sqrtP move < ~0.3%
  // we skip the walker and use the cheap formula.
  async function PancakeSwapInfinityBuyAdaptive(state, usdcInRaw, opts) {
    if (typeof usdcInRaw !== "bigint") usdcInRaw = BigInt(usdcInRaw);
    if (usdcInRaw <= 0n) throw new Error("usdcInRaw must be > 0");

    const sqrtP = BigInt(state.state.sqrtPriceX96);
    const L = BigInt(state.state.liquidity);
    if (L === 0n) {
      return PancakeSwapInfinityTickWalkBuy(state, usdcInRaw, opts);
    }
    const sqrtNew = sqrtP + (usdcInRaw * PCS_INF_Q96) / L;
    // Above gate: priceRatio = (sqrtNew/sqrtP)^2. Move > ~0.3% if sqrtNew*1000 > sqrtP*1003.
    if (sqrtNew * 1000n <= sqrtP * 1003n) {
      return _pcsInfInRangeBuy(state, usdcInRaw);
    }
    return PancakeSwapInfinityTickWalkBuy(state, usdcInRaw, opts);
  }

  // ── UniswapV3 (BSC) ──────────────────────────────────────────────────────
  async function fetchUniswapV3State() {
    const POOL = "0xc7EFB8807071Fa15302dA049E4B2637C23cf9e8A";
    const DEC0 = 6;
    const DEC1 = 18;

    const slot0Hex = await _bscEthCall(POOL, "0x3850c7bd");
    if (!slot0Hex || slot0Hex === "0x") {
      throw new Error("UniswapV3 pool returned empty slot0 — wrong address or contract uninitialised");
    }
    const hex = slot0Hex.slice(2);
    const word = (i) => hex.slice(i * 64, (i + 1) * 64);
    const sqrtPriceX96 = BigInt("0x" + word(0));

    let tickRaw = BigInt("0x" + word(1));
    const SIGN24 = 1n << 23n;
    const MOD24 = 1n << 24n;
    if (tickRaw >= SIGN24) tickRaw -= MOD24;
    const tick = Number(tickRaw);

    const liqHex = await _bscEthCall(POOL, "0x1a686502");
    const liquidity = BigInt(liqHex);

    if (sqrtPriceX96 === 0n) throw new Error("UniswapV3 sqrtPriceX96 is zero — pool uninitialised");

    const Q96 = 2n ** 96n;
    const sqrtPNum = Number(sqrtPriceX96) / Number(Q96);
    const midUsd = sqrtPNum * sqrtPNum * Math.pow(10, DEC0 - DEC1);

    return {
      venue: "UniswapV3-BSC",
      type: "dex_bsc",
      state: {
        sqrtPriceX96: sqrtPriceX96.toString(),
        liquidity: liquidity.toString(),
        tick,
        token0Decimals: DEC0,
        token1Decimals: DEC1,
        poolAddress: POOL,
      },
      midUsd,
      fetchedAt: Date.now(),
    };
  }

  // BUY side for UniswapV3-BSC. Same V3 math, oneForZero direction:
  //   sqrtP_new = sqrtP + (usdtIn * Q96) / L
  //   slxOut_raw = L * (sqrtP_new - sqrtP) * Q96 / (sqrtP_new * sqrtP)
  // Returns { slxOut, priceImpactPct, avgPrice } — analogous to UniswapV3SellSlippage.
  function UniswapV3BuySlippage(stateObj, usdtInRaw) {
    const s = stateObj.state ? stateObj.state : stateObj;
    const sqrtP = BigInt(s.sqrtPriceX96);
    const L = BigInt(s.liquidity);
    const dIn = BigInt(usdtInRaw);
    if (L === 0n) throw new Error("UniswapV3 in-range liquidity is zero");
    if (dIn <= 0n) throw new Error("usdtInRaw must be > 0");

    const Q96 = 2n ** 96n;
    const DEC0 = s.token0Decimals ?? 6;
    const DEC1 = s.token1Decimals ?? 18;

    const sqrtP_new = sqrtP + (dIn * Q96) / L;
    const slxOutRaw = (L * (sqrtP_new - sqrtP) * Q96) / (sqrtP_new * sqrtP);

    const slxOut = Number(slxOutRaw) / Math.pow(10, DEC0);
    const usdInHuman = Number(dIn) / Math.pow(10, DEC1);
    const avgPrice = slxOut > 0 ? usdInHuman / slxOut : 0;

    const sqrtPnum = Number(sqrtP) / Number(Q96);
    const sqrtPnewNum = Number(sqrtP_new) / Number(Q96);
    const decAdj = Math.pow(10, DEC0 - DEC1);
    const midBefore = sqrtPnum * sqrtPnum * decAdj;
    const midAfter = sqrtPnewNum * sqrtPnewNum * decAdj;
    // Buyer slippage: price went UP, paying more per SLX = positive impact.
    const priceImpactPct = midBefore > 0 ? ((midAfter - midBefore) / midBefore) * 100 : 0;

    return { slxOut, priceImpactPct, avgPrice };
  }

  function UniswapV3SellSlippage(stateObj, slxInRaw) {
    // Accept either the full result {state:{...}, midUsd} or the inner state directly.
    const s = stateObj.state ? stateObj.state : stateObj;
    const sqrtP = BigInt(s.sqrtPriceX96);
    const L = BigInt(s.liquidity);
    const dIn = BigInt(slxInRaw);
    if (L === 0n) throw new Error("UniswapV3 in-range liquidity is zero");
    if (dIn <= 0n) throw new Error("slxInRaw must be > 0");

    const Q96 = 2n ** 96n;
    const DEC0 = s.token0Decimals ?? 6;
    const DEC1 = s.token1Decimals ?? 18;

    const sqrtP_new = (sqrtP * L) / (L + (dIn * sqrtP) / Q96);
    const usdtOutRaw = (L * (sqrtP - sqrtP_new)) / Q96;

    const usdOut = Number(usdtOutRaw) / Math.pow(10, DEC1);
    const slxInHuman = Number(dIn) / Math.pow(10, DEC0);
    const avgPrice = slxInHuman > 0 ? usdOut / slxInHuman : 0;

    const sqrtPnum = Number(sqrtP) / Number(Q96);
    const sqrtPnewNum = Number(sqrtP_new) / Number(Q96);
    const decAdj = Math.pow(10, DEC0 - DEC1);
    const midBefore = sqrtPnum * sqrtPnum * decAdj;
    const midAfter = sqrtPnewNum * sqrtPnewNum * decAdj;
    const priceImpactPct = midBefore > 0 ? ((midBefore - midAfter) / midBefore) * 100 : 0;

    return { usdOut, priceImpactPct, avgPrice };
  }

  // ── UniswapV4 (BSC, via StateView) ───────────────────────────────────────
  const V4_STATE_VIEW = "0xd13dd3d6e93f276fafc9db9e6bb47c1180aee0c4";
  const V4_POOL_ID = "0xfb58b98896d288b18bc123bb9be00a9d2e266ba2bbe1106276b83c048af9d8e5";
  const V4_SEL_GET_SLOT0 = "0xc815641c";
  const V4_SEL_GET_LIQUIDITY = "0xfa6793d5";
  const V4_SLX_DECIMALS = 6;
  const V4_USDT_DECIMALS = 18;
  const V4_Q96 = 1n << 96n;

  function _v4DecodeInt24(slotHex) {
    const v = BigInt(slotHex);
    const max = 1n << 23n;
    const mod = 1n << 24n;
    const masked = v & (mod - 1n);
    return masked >= max ? masked - mod : masked;
  }

  async function fetchUniswapV4State() {
    const stripped = V4_POOL_ID.slice(2);
    const slot0Raw = await _bscEthCall(V4_STATE_VIEW, V4_SEL_GET_SLOT0 + stripped);
    const liqRaw = await _bscEthCall(V4_STATE_VIEW, V4_SEL_GET_LIQUIDITY + stripped);

    const r = slot0Raw.slice(2);
    if (r.length < 256) throw new Error(`getSlot0 returned short: ${slot0Raw}`);
    const sqrtPriceX96 = BigInt("0x" + r.slice(0, 64));
    const tick = Number(_v4DecodeInt24("0x" + r.slice(64, 128)));
    const protocolFee = Number(BigInt("0x" + r.slice(128, 192)));
    const lpFee = Number(BigInt("0x" + r.slice(192, 256)));

    const liquidity = BigInt(liqRaw);

    const sqrtF = Number(sqrtPriceX96) / Number(V4_Q96);
    const midUsd = sqrtF * sqrtF * Math.pow(10, V4_SLX_DECIMALS - V4_USDT_DECIMALS);

    return {
      venue: "UniswapV4",
      type: "dex_bsc",
      state: {
        sqrtPriceX96: sqrtPriceX96.toString(),
        liquidity: liquidity.toString(),
        tick,
        protocolFee,
        lpFee,
        poolId: V4_POOL_ID,
        token0Decimals: V4_SLX_DECIMALS,
        token1Decimals: V4_USDT_DECIMALS,
      },
      midUsd,
      fetchedAt: Date.now(),
    };
  }

  // BUY side for UniswapV4 (BSC). Same V3 formula.
  function UniswapV4BuySlippage(stateObj, usdtInRaw) {
    const s = stateObj.state ? stateObj.state : stateObj;
    const sqrtP = BigInt(s.sqrtPriceX96);
    const L = BigInt(s.liquidity);
    if (L === 0n) {
      throw new Error("UniswapV4: in-range liquidity is 0; cannot quote buy");
    }
    if (typeof usdtInRaw !== "bigint") usdtInRaw = BigInt(usdtInRaw);
    if (usdtInRaw <= 0n) throw new Error("UniswapV4BuySlippage: usdtInRaw must be > 0");

    const sqrtPnew = sqrtP + (usdtInRaw * V4_Q96) / L;
    const slxOutRaw = (L * (sqrtPnew - sqrtP) * V4_Q96) / (sqrtPnew * sqrtP);

    const slxOut = Number(slxOutRaw) / Math.pow(10, V4_SLX_DECIMALS);
    const usdInHuman = Number(usdtInRaw) / Math.pow(10, V4_USDT_DECIMALS);
    const avgPrice = slxOut > 0 ? usdInHuman / slxOut : 0;

    const midSqrtF = Number(sqrtP) / Number(V4_Q96);
    const midPrice = midSqrtF * midSqrtF * Math.pow(10, V4_SLX_DECIMALS - V4_USDT_DECIMALS);
    const priceImpactPct = midPrice > 0 ? ((avgPrice - midPrice) / midPrice) * 100 : 0;

    return { slxOut, priceImpactPct, avgPrice };
  }

  function UniswapV4SellSlippage(stateObj, slxInRaw) {
    const s = stateObj.state ? stateObj.state : stateObj;
    const sqrtP = BigInt(s.sqrtPriceX96);
    const L = BigInt(s.liquidity);
    if (L === 0n) {
      throw new Error("UniswapV4: in-range liquidity is 0; cannot quote (fall back to volume heuristic)");
    }
    if (typeof slxInRaw !== "bigint") slxInRaw = BigInt(slxInRaw);
    if (slxInRaw <= 0n) throw new Error("UniswapV4SellSlippage: slxInRaw must be a positive BigInt");

    const sqrtPnew = (sqrtP * L) / (L + (slxInRaw * sqrtP) / V4_Q96);
    const usdtOutRaw = (L * (sqrtP - sqrtPnew)) / V4_Q96;

    const usdOut = Number(usdtOutRaw) / Math.pow(10, V4_USDT_DECIMALS);
    const slxInHuman = Number(slxInRaw) / Math.pow(10, V4_SLX_DECIMALS);
    const avgPrice = slxInHuman > 0 ? usdOut / slxInHuman : 0;

    const midSqrtF = Number(sqrtP) / Number(V4_Q96);
    const midPrice = midSqrtF * midSqrtF * Math.pow(10, V4_SLX_DECIMALS - V4_USDT_DECIMALS);
    const priceImpactPct = midPrice > 0 ? ((midPrice - avgPrice) / midPrice) * 100 : 0;

    return { usdOut, priceImpactPct, avgPrice };
  }

  // ═════════════════════════════════════════════════════════════════════════
  // SOLANA — Jupiter v6 quote
  // ═════════════════════════════════════════════════════════════════════════
  // GET https://quote-api.jup.ag/v6/quote
  //   ?inputMint=<SLX>&outputMint=<USDC>&amount=<lamports>&slippageBps=50
  async function fetchJupiterQuote(slxAmount) {
    if (!Number.isFinite(slxAmount) || slxAmount <= 0) {
      throw new Error("fetchJupiterQuote: slxAmount must be positive number");
    }
    const lamports = BigInt(Math.floor(slxAmount * Math.pow(10, SLX_DECIMALS)));
    const url =
      "https://quote-api.jup.ag/v6/quote" +
      `?inputMint=${SLX_MINT_SOL}` +
      `&outputMint=${USDC_MINT_SOL}` +
      `&amount=${lamports.toString()}` +
      "&slippageBps=50" +
      "&onlyDirectRoutes=false";

    const res = await fetch(url, { headers: { accept: "application/json" } });
    if (!res.ok) {
      throw new Error(`Jupiter quote HTTP ${res.status}`);
    }
    const j = await res.json();
    if (!j || !j.outAmount) {
      throw new Error("Jupiter quote: missing outAmount");
    }
    const outAmount = Number(j.outAmount) / 1e6; // USDC has 6 decimals
    const priceImpactPct = parseFloat(j.priceImpactPct ?? "0") * 100; // jup returns fraction
    const hops = Array.isArray(j.routePlan) ? j.routePlan.length : 0;

    return {
      venue: "Jupiter",
      type: "dex_sol",
      slxIn: slxAmount,
      outAmount,           // USDC out
      priceImpactPct,
      hops,
      avgPrice: slxAmount > 0 ? outAmount / slxAmount : 0,
      fetchedAt: Date.now(),
    };
  }

  // Jupiter v6 BUY quote: USDC in → SLX out. Mirror of fetchJupiterQuote with
  // input/output mints swapped. `usdcSpend` is the USD amount in human units
  // (e.g. 1000 = $1000).
  async function fetchJupiterBuyQuote(usdcSpend) {
    if (!Number.isFinite(usdcSpend) || usdcSpend <= 0) {
      throw new Error("fetchJupiterBuyQuote: usdcSpend must be positive number");
    }
    // USDC on Solana has 6 decimals.
    const usdcLamports = BigInt(Math.floor(usdcSpend * 1e6));
    const url =
      "https://quote-api.jup.ag/v6/quote" +
      `?inputMint=${USDC_MINT_SOL}` +
      `&outputMint=${SLX_MINT_SOL}` +
      `&amount=${usdcLamports.toString()}` +
      "&slippageBps=50" +
      "&onlyDirectRoutes=false";

    const res = await fetch(url, { headers: { accept: "application/json" } });
    if (!res.ok) {
      throw new Error(`Jupiter buy quote HTTP ${res.status}`);
    }
    const j = await res.json();
    if (!j || !j.outAmount) {
      throw new Error("Jupiter buy quote: missing outAmount");
    }
    const slxOut = Number(j.outAmount) / Math.pow(10, SLX_DECIMALS);
    // Jupiter returns priceImpactPct as a string fraction; convert to percent.
    // For a buy, a positive impact still means "worse than spot" → keep sign convention.
    const priceImpactPct = parseFloat(j.priceImpactPct ?? "0") * 100;
    const hops = Array.isArray(j.routePlan) ? j.routePlan.length : 0;

    return {
      venue: "Jupiter",
      type: "dex_sol",
      usdcIn: usdcSpend,
      slxOut,
      priceImpactPct,
      hops,
      avgPrice: slxOut > 0 ? usdcSpend / slxOut : 0,
      fetchedAt: Date.now(),
    };
  }

  // ═════════════════════════════════════════════════════════════════════════
  // BSC AMM → cumulative bid-curve adapter
  // ═════════════════════════════════════════════════════════════════════════
  // Build a stepwise bid-curve by sampling the AMM sell-slippage function at
  // geometrically increasing SLX-in sizes. Each step yields a (price, slx)
  // pair that the greedy allocator consumes identically to a CEX bid level.
  //
  // Sample sizes: 50, 250, 1k, 5k, 25k, 100k, 500k, 2M SLX.
  // Each step's "size" is the incremental SLX between this sample and the previous,
  // priced at the marginal (avg between previous and this) — this is an over-estimate
  // of the price at the END of the step but matches greedy semantics well enough.
  function _ammToBidCurve(venueName, state, slippageFn) {
    const SAMPLES = [50, 250, 1_000, 5_000, 25_000, 100_000, 500_000, 2_000_000];
    const dec = state.state.token0?.decimals ?? state.state.token0Decimals ?? 6;
    const bids = [];
    let prevSlxIn = 0;
    let prevUsdOut = 0;
    for (const sampleSlx of SAMPLES) {
      const raw = BigInt(Math.floor(sampleSlx * Math.pow(10, dec)));
      let q;
      try {
        q = slippageFn(state, raw);
      } catch (e) {
        break; // L=0 or other failure → stop sampling
      }
      const incSlx = sampleSlx - prevSlxIn;
      const incUsd = q.usdOut - prevUsdOut;
      if (incSlx <= 0 || incUsd <= 0) break;
      const marginalPrice = incUsd / incSlx;
      bids.push([marginalPrice, incSlx]);
      prevSlxIn = sampleSlx;
      prevUsdOut = q.usdOut;
    }
    bids.sort((a, b) => b[0] - a[0]);
    return {
      venue: venueName,
      type: "dex_bsc",
      bids,
      midUsd: state.midUsd,
      fetchedAt: Date.now(),
    };
  }

  // ───── BSC AMM → cumulative ASK-curve adapter ─────────────────────────────
  // Mirrors _ammToBidCurve, but samples on the USDC-IN axis and reads slxOut
  // off the buy-slippage function. Each step's marginal price is incUsd / incSlx,
  // ascending sort = best (cheapest) ask first.
  //
  // Sample sizes (USD in): 10, 50, 200, 1k, 5k, 25k, 100k, 500k.
  function _ammToAskCurve(venueName, state, buyFn) {
    const SAMPLES_USD = [10, 50, 200, 1_000, 5_000, 25_000, 100_000, 500_000];
    const dec1 = state.state.token1?.decimals ?? state.state.token1Decimals ?? 18;
    const asks = [];
    let prevUsdIn = 0;
    let prevSlxOut = 0;
    for (const sampleUsd of SAMPLES_USD) {
      const raw = BigInt(Math.floor(sampleUsd * Math.pow(10, dec1)));
      let q;
      try {
        q = buyFn(state, raw);
      } catch (e) {
        break;
      }
      const incUsd = sampleUsd - prevUsdIn;
      const incSlx = q.slxOut - prevSlxOut;
      if (incUsd <= 0 || incSlx <= 0) break;
      const marginalPrice = incUsd / incSlx;
      asks.push([marginalPrice, incSlx]);
      prevUsdIn = sampleUsd;
      prevSlxOut = q.slxOut;
    }
    asks.sort((a, b) => a[0] - b[0]);
    return {
      venue: venueName,
      type: "dex_bsc",
      asks,
      midUsd: state.midUsd,
      fetchedAt: Date.now(),
    };
  }

  async function _ammToAskCurveAsync(venueName, state, buyFnAsync) {
    const SAMPLES_USD = [10, 50, 200, 1_000, 5_000, 25_000, 100_000, 500_000];
    const dec1 = state.state.token1?.decimals ?? state.state.token1Decimals ?? 18;
    const asks = [];
    let prevUsdIn = 0;
    let prevSlxOut = 0;
    for (const sampleUsd of SAMPLES_USD) {
      const raw = BigInt(Math.floor(sampleUsd * Math.pow(10, dec1)));
      let q;
      try {
        q = await buyFnAsync(state, raw);
      } catch (e) {
        break;
      }
      const incUsd = sampleUsd - prevUsdIn;
      const incSlx = q.slxOut - prevSlxOut;
      if (incUsd <= 0 || incSlx <= 0) break;
      const marginalPrice = incUsd / incSlx;
      asks.push([marginalPrice, incSlx]);
      prevUsdIn = sampleUsd;
      prevSlxOut = q.slxOut;
    }
    asks.sort((a, b) => a[0] - b[0]);
    return {
      venue: venueName,
      type: "dex_bsc",
      asks,
      midUsd: state.midUsd,
      fetchedAt: Date.now(),
    };
  }

  // Async variant — samples are awaited so the curve can come from a tick-walking
  // slippage function. Identical bid-shape contract as _ammToBidCurve.
  async function _ammToBidCurveAsync(venueName, state, slippageFnAsync) {
    const SAMPLES = [50, 250, 1_000, 5_000, 25_000, 100_000, 500_000, 2_000_000];
    const dec = state.state.token0?.decimals ?? state.state.token0Decimals ?? 6;
    const bids = [];
    let prevSlxIn = 0;
    let prevUsdOut = 0;
    for (const sampleSlx of SAMPLES) {
      const raw = BigInt(Math.floor(sampleSlx * Math.pow(10, dec)));
      let q;
      try {
        q = await slippageFnAsync(state, raw);
      } catch (e) {
        break;
      }
      const incSlx = sampleSlx - prevSlxIn;
      const incUsd = q.usdOut - prevUsdOut;
      if (incSlx <= 0 || incUsd <= 0) break;
      const marginalPrice = incUsd / incSlx;
      bids.push([marginalPrice, incSlx]);
      prevSlxIn = sampleSlx;
      prevUsdOut = q.usdOut;
    }
    bids.sort((a, b) => b[0] - a[0]);
    return {
      venue: venueName,
      type: "dex_bsc",
      bids,
      midUsd: state.midUsd,
      fetchedAt: Date.now(),
    };
  }

  // ═════════════════════════════════════════════════════════════════════════
  // ORCHESTRATOR
  // ═════════════════════════════════════════════════════════════════════════
  async function fetchAllDepth() {
    const cexFns = [
      ["Upbit", fetchUpbitBids],        // NEW 2026-06-01 (Korean KRW-SLX, live + deep)
      ["Bithumb", fetchBithumbBids],    // NEW 2026-06-01 (Korean — listed, awaiting trading start)
      ["Bitget", fetchBitgetBids],
      ["BitMart", fetchBitMartBids],
      ["Kraken", fetchKrakenBids],
      ["Hotcoin", fetchHotcoinBids],
      ["DigiFinex", fetchDigiFinexBids],
      ["KCEX", fetchKCEXBids],
    ];
    if (PROXIED_CEX_ENABLED) {
      cexFns.push(
        ["MEXC", fetchMEXCBids],
        ["Gate", fetchGateBids],
        ["LBank", fetchLBankBids],
        ["OrangeX", fetchOrangeXBids],
        ["BingX", fetchBingXBids],
        ["Toobit", fetchToobitBids],
        ["Ourbit", fetchOurbitBids],
        ["WEEX", fetchWEEXBids],
      );
    }
    // Each entry: [name, fetchStateFn, sellFn, buyFn, isAsync?]
    const bscFns = [
      ["PancakeSwapInfinity", fetchPancakeSwapInfinityState,
        PancakeSwapInfinitySellSlippageAdaptive, PancakeSwapInfinityBuyAdaptive, true],
      ["UniswapV3-BSC", fetchUniswapV3State,
        UniswapV3SellSlippage, UniswapV3BuySlippage, false],
      ["UniswapV4", fetchUniswapV4State,
        UniswapV4SellSlippage, UniswapV4BuySlippage, false],
    ];

    const cexPromises = cexFns.map(([name, fn]) =>
      cached(name, CEX_CACHE, CEX_TTL_MS, fn).catch((e) => ({ __error: true, venue: name, error: e.message }))
    );
    // For each BSC pool, fetch state ONCE then build both the bid curve and
    // ask curve from it. This keeps the slipage math, RPC traffic, and tick
    // cache in sync between the two sides.
    const bscPromises = bscFns.map(([name, fn, sellFn, buyFn, isAsync]) =>
      cached(name, BSC_CACHE, BSC_TTL_MS, fn)
        .then(async (state) => {
          const bidRec = isAsync
            ? await _ammToBidCurveAsync(name, state, sellFn)
            : _ammToBidCurve(name, state, sellFn);
          let askRec;
          try {
            askRec = isAsync
              ? await _ammToAskCurveAsync(name, state, buyFn)
              : _ammToAskCurve(name, state, buyFn);
          } catch (_e) {
            askRec = { asks: [] };
          }
          bidRec.asks = askRec.asks || [];
          return bidRec;
        })
        .catch((e) => ({ __error: true, venue: name, error: e.message }))
    );

    const [cexResults, bscResults] = await Promise.all([
      Promise.allSettled(cexPromises),
      Promise.allSettled(bscPromises),
    ]);

    const flatten = (settled) =>
      settled
        .map((r) => (r.status === "fulfilled" ? r.value : { __error: true, error: String(r.reason) }))
        .filter((v) => v && !v.__error && Array.isArray(v.bids) && v.bids.length > 0);

    return {
      cex: flatten(cexResults),
      bsc: flatten(bscResults),
      solana: { quote: fetchJupiterQuote, quoteBuy: fetchJupiterBuyQuote },
      errors: [
        ...cexResults
          .map((r) => (r.status === "fulfilled" ? r.value : { __error: true, error: String(r.reason) }))
          .filter((v) => v && v.__error),
        ...bscResults
          .map((r) => (r.status === "fulfilled" ? r.value : { __error: true, error: String(r.reason) }))
          .filter((v) => v && v.__error),
      ],
    };
  }

  // ═════════════════════════════════════════════════════════════════════════
  // Fallback (estimated) bid curve — OLD quadratic heuristic
  // ═════════════════════════════════════════════════════════════════════════
  // Synthesize a stepwise bid curve from coarse venue stats. Used for venues
  // the caller flags as "needs_proxy" or that failed live fetch. Each level
  // tags source:"estimated" so the UI can downgrade confidence.
  //
  // Model:
  //   - Top of book = price * (1 - spread/2).
  //   - Curve depth in USD = vol24h * 0.02 (assume 2% of daily volume sits in book).
  //   - Slippage grows quadratically: at fillFrac f of book, price = top*(1 - f^2 * spread*5).
  function _estimatedCurveFromHeuristic(spec) {
    const price = Number(spec.price) > 0 ? Number(spec.price) : 0.2;
    const spread = Number(spec.spread) > 0 ? Number(spec.spread) : 0.005;
    const vol24h = Number(spec.vol24h) > 0 ? Number(spec.vol24h) : 0;
    const depthUsd = vol24h * 0.02;
    const topPrice = price * (1 - spread / 2);
    const STEPS = 12;
    const bids = [];
    let cumUsd = 0;
    for (let i = 1; i <= STEPS; i++) {
      const f = i / STEPS;
      const stepPrice = topPrice * (1 - (f * f) * spread * 5);
      if (stepPrice <= 0) break;
      const cumUsdHere = depthUsd * f;
      const stepUsd = cumUsdHere - cumUsd;
      cumUsd = cumUsdHere;
      const stepSlx = stepUsd / stepPrice;
      if (stepSlx > 0 && stepPrice > 0) bids.push([stepPrice, stepSlx]);
    }
    bids.sort((a, b) => b[0] - a[0]);
    return {
      venue: spec.venue,
      type: spec.type || "cex",
      bids,
      midUsd: topPrice,
      fetchedAt: Date.now(),
      __estimated: true,
    };
  }

  // ═════════════════════════════════════════════════════════════════════════
  // GREEDY ALLOCATOR
  // ═════════════════════════════════════════════════════════════════════════
  // priceSell(slxAmount, fallbackVenues)
  //   - fetches all live curves via fetchAllDepth()
  //   - adds estimated curves for any fallbackVenues entries (caller signals
  //     these are needs_proxy / unavailable)
  //   - merges all bid levels, sorts by price desc, consumes greedily
  //   - tracks per-venue fill totals; labels each fill source
  async function priceSell(slxAmount, fallbackVenues) {
    if (!Number.isFinite(slxAmount) || slxAmount <= 0) {
      throw new Error("priceSell: slxAmount must be positive number");
    }
    const depth = await fetchAllDepth();

    const curves = [];
    for (const c of depth.cex) curves.push({ ...c, source: "live" });
    for (const c of depth.bsc) curves.push({ ...c, source: "live" });

    if (Array.isArray(fallbackVenues)) {
      for (const spec of fallbackVenues) {
        if (!spec || !spec.venue) continue;
        const est = _estimatedCurveFromHeuristic(spec);
        if (est.bids.length > 0) curves.push({ ...est, source: "estimated" });
      }
    }

    // Flatten into [price, slx, venue, type, source] levels, sort by price desc.
    const levels = [];
    for (const c of curves) {
      for (const [p, s] of c.bids) {
        levels.push([p, s, c.venue, c.type, c.source]);
      }
    }
    levels.sort((a, b) => b[0] - a[0]);

    const perVenue = new Map(); // venue → {venue,type,slxFilled,usdReceived,marginalPrice,source}
    let remaining = slxAmount;
    let totalUsd = 0;
    let lastPrice = levels.length > 0 ? levels[0][0] : 0;

    for (const [price, sizeSlx, venue, type, source] of levels) {
      if (remaining <= 0) break;
      const take = Math.min(remaining, sizeSlx);
      const usd = take * price;
      remaining -= take;
      totalUsd += usd;
      lastPrice = price;
      const key = venue + "::" + source;
      const cur = perVenue.get(key) || {
        venue,
        type,
        slxFilled: 0,
        usdReceived: 0,
        marginalPrice: price,
        source,
      };
      cur.slxFilled += take;
      cur.usdReceived += usd;
      cur.marginalPrice = price; // last (worst) price hit at this venue
      perVenue.set(key, cur);
    }

    const filled = slxAmount - remaining;
    const avgPrice = filled > 0 ? totalUsd / filled : 0;
    // Slippage vs best (top-of-book) price across all curves.
    const bestPrice = levels.length > 0 ? levels[0][0] : 0;
    const slippagePct = bestPrice > 0 ? ((bestPrice - avgPrice) / bestPrice) * 100 : 0;

    return {
      usdReceived: totalUsd,
      avgPrice,
      slippagePct,
      slxFilled: filled,
      slxUnfilled: remaining,
      marginalPrice: lastPrice,
      perVenueFill: Array.from(perVenue.values()),
    };
  }

  // priceBuy(usdSpend, fallbackVenues)
  //   - fetches all live curves via fetchAllDepth() (provides asks per venue)
  //   - also fetches a Jupiter buy quote for the full usdSpend and treats it
  //     as a single ASK level (price = usdSpend/slxOut, size = slxOut)
  //   - adds estimated ask curves for any fallbackVenues entries
  //   - merges all ask levels, sorts by price asc (cheapest first), consumes
  //     greedily
  //   - tracks per-venue fill totals; labels each fill source
  //
  // Returns: { slxReceived, avgPrice, slippagePct, perVenueFill: [...] }
  //   slippagePct is vs best (cheapest) ask. Buyers paying MORE per SLX than
  //   best-of-book = positive slippage = bad.
  async function priceBuy(usdSpend, fallbackVenues) {
    if (!Number.isFinite(usdSpend) || usdSpend <= 0) {
      throw new Error("priceBuy: usdSpend must be positive number");
    }
    const depth = await fetchAllDepth();

    const curves = [];
    for (const c of depth.cex) {
      if (Array.isArray(c.asks) && c.asks.length > 0) {
        curves.push({ venue: c.venue, type: c.type, asks: c.asks, source: "live" });
      }
    }
    for (const c of depth.bsc) {
      if (Array.isArray(c.asks) && c.asks.length > 0) {
        curves.push({ venue: c.venue, type: c.type, asks: c.asks, source: "live" });
      }
    }

    // Solana: single-shot Jupiter buy quote for the full usdSpend.
    try {
      const jq = await fetchJupiterBuyQuote(usdSpend);
      if (jq && jq.slxOut > 0) {
        const px = usdSpend / jq.slxOut;
        curves.push({
          venue: "Jupiter",
          type: "dex_sol",
          asks: [[px, jq.slxOut]],
          source: "live",
        });
      }
    } catch (_e) {
      // Jupiter is best-effort; missing it doesn't block other venues.
    }

    if (Array.isArray(fallbackVenues)) {
      for (const spec of fallbackVenues) {
        if (!spec || !spec.venue) continue;
        // Reuse the bid-curve heuristic and flip its sign: top-of-book ask is
        // spot*(1+spread/2), depth grows quadratically the same way.
        const price = Number(spec.price) > 0 ? Number(spec.price) : 0.2;
        const spread = Number(spec.spread) > 0 ? Number(spec.spread) : 0.005;
        const vol24h = Number(spec.vol24h) > 0 ? Number(spec.vol24h) : 0;
        const depthUsd = vol24h * 0.02;
        const topPrice = price * (1 + spread / 2);
        const STEPS = 12;
        const asks = [];
        let cumUsd = 0;
        for (let i = 1; i <= STEPS; i++) {
          const f = i / STEPS;
          const stepPrice = topPrice * (1 + (f * f) * spread * 5);
          const cumUsdHere = depthUsd * f;
          const stepUsd = cumUsdHere - cumUsd;
          cumUsd = cumUsdHere;
          const stepSlx = stepUsd / stepPrice;
          if (stepSlx > 0 && stepPrice > 0) asks.push([stepPrice, stepSlx]);
        }
        asks.sort((a, b) => a[0] - b[0]);
        if (asks.length > 0) {
          curves.push({ venue: spec.venue, type: spec.type || "cex", asks, source: "estimated" });
        }
      }
    }

    // Flatten into [price, slx, venue, type, source] levels, sort by price ASC.
    const levels = [];
    for (const c of curves) {
      for (const [p, s] of c.asks) {
        levels.push([p, s, c.venue, c.type, c.source]);
      }
    }
    levels.sort((a, b) => a[0] - b[0]);

    const perVenue = new Map();
    let remainingUsd = usdSpend;
    let totalSlx = 0;
    let lastPrice = levels.length > 0 ? levels[0][0] : 0;

    for (const [price, sizeSlx, venue, type, source] of levels) {
      if (remainingUsd <= 0) break;
      const levelUsd = sizeSlx * price;
      const takeUsd = Math.min(remainingUsd, levelUsd);
      const takeSlx = price > 0 ? takeUsd / price : 0;
      remainingUsd -= takeUsd;
      totalSlx += takeSlx;
      lastPrice = price;
      const key = venue + "::" + source;
      const cur = perVenue.get(key) || {
        venue,
        type,
        usdSpent: 0,
        slxReceived: 0,
        marginalPrice: price,
        source,
      };
      cur.usdSpent += takeUsd;
      cur.slxReceived += takeSlx;
      cur.marginalPrice = price; // last (worst) price hit at this venue
      perVenue.set(key, cur);
    }

    const usdSpent = usdSpend - remainingUsd;
    const avgPrice = totalSlx > 0 ? usdSpent / totalSlx : 0;
    // Slippage vs best (cheapest) ask. Higher price for buyer = bad = positive.
    const bestPrice = levels.length > 0 ? levels[0][0] : 0;
    const slippagePct = bestPrice > 0 ? ((avgPrice - bestPrice) / bestPrice) * 100 : 0;

    return {
      slxReceived: totalSlx,
      avgPrice,
      slippagePct,
      usdSpent,
      usdUnfilled: remainingUsd,
      marginalPrice: lastPrice,
      perVenueFill: Array.from(perVenue.values()),
    };
  }

  // ═════════════════════════════════════════════════════════════════════════
  // PERPETUAL FUTURES OI + FUNDING — via Vercel proxy (CORS-blocked upstreams)
  // ═════════════════════════════════════════════════════════════════════════
  // All venue endpoints proxied through /api/perp-oi (uniform interface, no
  // CORS issues, no caching — refreshes on every call).
  //
  // Returns array of:
  //   { venue, oi_usd, funding_rate, funding_cycle_hours, mark, error? }
  //
  // Funding direction convention:
  //   negative funding = shorts pay longs = SHORT-crowded
  //   positive funding = longs pay shorts = LONG-crowded
  async function fetchAllPerpOI() {
    const VENUE_KEYS = [
      // Direct-API venues (live OI + funding from venue itself)
      "gate", "okx", "binance_futures", "bingx", "bitget", "bitmart", "mexc",
      // CoinGecko-sourced fallback venues (OI + volume reliable; funding unverified)
      "lbank", "ourbit", "hotcoin", "kcex", "weex", "orangex_perp",
    ];
    const VENUE_LABELS = {
      gate: "Gate", okx: "OKX", binance_futures: "Binance Futures",
      bingx: "BingX", bitget: "Bitget", bitmart: "BitMart", mexc: "MEXC",
      lbank: "LBank", ourbit: "Ourbit", hotcoin: "Hotcoin", kcex: "KCEX",
      weex: "WEEX", orangex_perp: "OrangeX Perp",
    };

    async function _one(key) {
      try {
        const res = await fetch(`/api/perp-oi?venue=${key}`, { cache: "no-store" });
        const j = await res.json();
        if (!res.ok || j.error) {
          return { venue: VENUE_LABELS[key], error: (j && j.error) || `HTTP ${res.status}` };
        }
        return j;
      } catch (e) {
        return { venue: VENUE_LABELS[key], error: String((e && e.message) || e).slice(0, 100) };
      }
    }

    return await Promise.all(VENUE_KEYS.map(_one));
  }

  // ═════════════════════════════════════════════════════════════════════════
  // PUBLIC API
  // ═════════════════════════════════════════════════════════════════════════
  if (typeof window !== "undefined") {
    window.SLXDepthEngine = {
      fetchAllDepth,
      fetchAllPerpOI,
      priceSell,
      priceBuy,
      fetchJupiterQuote,
      fetchJupiterBuyQuote,
      SLX_TOTAL_SUPPLY,
      // expose low-level helpers for debugging / dashboards
      _internals: {
        fetchBitgetBids,
        fetchBitMartBids,
        fetchKrakenBids,
        fetchHotcoinBids,
        fetchDigiFinexBids,
        fetchKCEXBids,
        fetchMEXCBids,
        fetchGateBids,
        fetchLBankBids,
        fetchOrangeXBids,
        fetchBingXBids,
        fetchToobitBids,
        fetchOurbitBids,
        fetchWEEXBids,
        fetchPancakeSwapInfinityState,
        fetchUniswapV3State,
        fetchUniswapV4State,
        PancakeSwapInfinitySellSlippage,
        PancakeSwapInfinitySellSlippageAdaptive,
        PancakeSwapInfinityTickWalkSell,
        PancakeSwapInfinityBuyAdaptive,
        PancakeSwapInfinityTickWalkBuy,
        UniswapV3SellSlippage,
        UniswapV3BuySlippage,
        UniswapV4SellSlippage,
        UniswapV4BuySlippage,
      },
    };
  }

  // Node compatibility for smoke-testing / CI. The browser export above is the
  // canonical surface; this is purely additive.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      fetchAllDepth,
      priceSell,
      priceBuy,
      fetchJupiterQuote,
      fetchJupiterBuyQuote,
      SLX_TOTAL_SUPPLY,
    };
  }
})();

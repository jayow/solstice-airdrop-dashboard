// SLX Depth Engine — live orderbook + on-chain pool reader for sell-side liquidity.
//
// Vanilla browser module. No imports, no bundler. Exposes window.SLXDepthEngine.
//
// Live venues (assembled 2026-05-31, all individually verified):
//   CEX (CORS-confirmed): Bitget, BitMart, Kraken, Hotcoin
//   BSC on-chain:         PancakeSwapInfinity (CLPoolManager singleton),
//                         UniswapV3-BSC pool 0xc7EFB8...,
//                         UniswapV4 pool 0xfb58b9... (StateView wrapper)
//   Solana:               Jupiter v6 quote API
//
// Dropped from live set:
//   Ourbit — endpoint healthy but CORS not confirmed (no explicit ACAO header).
//            Caller can still pass it in via fallbackVenues to get an estimated curve.
//
// SLX decimals: 6 on BSC (verified on-chain), 6 on Solana.

(function () {
  "use strict";

  // ─── Constants ────────────────────────────────────────────────────────────
  const SLX_TOTAL_SUPPLY = 1_000_000_000;
  const BSC_RPC = "https://bsc-dataseed.bnbchain.org";

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

  // Fetches the live Bitget SLX/USDT bid orderbook.
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

    return {
      venue,
      type: "cex",
      bids,
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

    return {
      venue: "BitMart",
      type: "cex",
      bids,
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
    return {
      venue: "Kraken",
      type: "cex",
      bids,
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

    return {
      venue: "Hotcoin",
      type: "cex",
      bids,
      midUsd: bids[0][0],
      fetchedAt: Date.now(),
    };
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
  async function fetchPancakeSwapInfinityState() {
    const MANAGER = "0xa0FfB9c1CE1Fe56963B0321B32E7A0302114058b";
    const POOL_ID = "1a96f5b1dc28fd3c9e3772c255e5541b8635c20db1c88f0f5156c5be94ad58cb";
    const SEL_GET_SLOT0 = "0xc815641c";
    const SEL_GET_LIQUIDITY = "0xfa6793d5";

    const slot0Hex = await _bscEthCall(MANAGER, SEL_GET_SLOT0 + POOL_ID);
    const liqHex = await _bscEthCall(MANAGER, SEL_GET_LIQUIDITY + POOL_ID);

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

    const Q96 = 2n ** 96n;
    const priceScaled = (sqrtPriceX96 * sqrtPriceX96 * (10n ** 18n)) / (Q96 * Q96 * (10n ** 12n));
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
        token0: { address: "0x02bcc4c181b83a8c0a342bc003389cbecb4bc54d", symbol: "SLX", decimals: 6 },
        token1: { address: "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", symbol: "USDC", decimals: 18 },
        slxIsToken0: true,
      },
      midUsd,
      fetchedAt: Date.now(),
    };
  }

  function PancakeSwapInfinitySellSlippage(state, slxInRaw) {
    if (typeof slxInRaw !== "bigint") slxInRaw = BigInt(slxInRaw);
    if (slxInRaw <= 0n) throw new Error("slxInRaw must be > 0");

    const Q96 = 2n ** 96n;
    const sqrtP = BigInt(state.state.sqrtPriceX96);
    const L = BigInt(state.state.liquidity);
    if (L === 0n) throw new Error("zero in-range liquidity");

    const denom = L + (slxInRaw * sqrtP) / Q96;
    const sqrtNew = (sqrtP * L) / denom;
    const outRaw = (L * (sqrtP - sqrtNew)) / Q96;

    const dec0 = state.state.token0.decimals;
    const dec1 = state.state.token1.decimals;
    const usdOut = Number(outRaw) / Math.pow(10, dec1);
    const slxIn = Number(slxInRaw) / Math.pow(10, dec0);
    const avgPrice = usdOut / slxIn;

    const spot = state.midUsd;
    const priceImpactPct = ((spot - avgPrice) / spot) * 100;

    return { usdOut, priceImpactPct, avgPrice };
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

  // ═════════════════════════════════════════════════════════════════════════
  // ORCHESTRATOR
  // ═════════════════════════════════════════════════════════════════════════
  async function fetchAllDepth() {
    const cexFns = [
      ["Bitget", fetchBitgetBids],
      ["BitMart", fetchBitMartBids],
      ["Kraken", fetchKrakenBids],
      ["Hotcoin", fetchHotcoinBids],
    ];
    const bscFns = [
      ["PancakeSwapInfinity", fetchPancakeSwapInfinityState, PancakeSwapInfinitySellSlippage],
      ["UniswapV3-BSC", fetchUniswapV3State, UniswapV3SellSlippage],
      ["UniswapV4", fetchUniswapV4State, UniswapV4SellSlippage],
    ];

    const cexPromises = cexFns.map(([name, fn]) =>
      cached(name, CEX_CACHE, CEX_TTL_MS, fn).catch((e) => ({ __error: true, venue: name, error: e.message }))
    );
    const bscPromises = bscFns.map(([name, fn, slipFn]) =>
      cached(name, BSC_CACHE, BSC_TTL_MS, fn)
        .then((state) => _ammToBidCurve(name, state, slipFn))
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
      solana: { quote: fetchJupiterQuote },
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

  // ═════════════════════════════════════════════════════════════════════════
  // PUBLIC API
  // ═════════════════════════════════════════════════════════════════════════
  if (typeof window !== "undefined") {
    window.SLXDepthEngine = {
      fetchAllDepth,
      priceSell,
      fetchJupiterQuote,
      SLX_TOTAL_SUPPLY,
      // expose low-level helpers for debugging / dashboards
      _internals: {
        fetchBitgetBids,
        fetchBitMartBids,
        fetchKrakenBids,
        fetchHotcoinBids,
        fetchPancakeSwapInfinityState,
        fetchUniswapV3State,
        fetchUniswapV4State,
        PancakeSwapInfinitySellSlippage,
        UniswapV3SellSlippage,
        UniswapV4SellSlippage,
      },
    };
  }
})();

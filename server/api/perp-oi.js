// Vercel serverless proxy for perpetual futures OI + funding endpoints.
//
// Routed by Vercel as:  GET /api/perp-oi?venue=<key>
//
// Why this exists: many CEX perp endpoints block CORS (Gate, MEXC, BingX),
// so the browser-side depth tool can't call them directly. This thin
// server-side passthrough fetches OI + funding in parallel and returns a
// unified shape: { venue, oi_usd, funding_rate, funding_cycle_hours, mark }.
//
// Convention: negative funding = shorts pay longs = SHORT-crowded.
//             positive funding = longs pay shorts = LONG-crowded.
//
// Runtime: Node 20.x.

export const config = {
  runtime: "nodejs",
};

const TIMEOUT_MS = 8_000;

async function fetchJson(url, headers = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      headers: { accept: "application/json", "user-agent": "solstice-perp-proxy/1.0", ...headers },
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

// Per-venue CoinGecko market identifier mapping for fallback fetching.
// Used when no direct venue API exists (LBank, Ourbit, Hotcoin, KCEX) or
// when only partial fields are exposed (WEEX has no funding, OrangeX has no OI).
const COINGECKO_MARKET_MAP = {
  lbank:   { label: "LBank",   cg_name: "LBank (Futures)",   cycle_hours: 8 },
  ourbit:  { label: "Ourbit",  cg_name: "Ourbit (Futures)",  cycle_hours: 8 },
  hotcoin: { label: "Hotcoin", cg_name: "Hotcoin (Futures)", cycle_hours: 8 },
  kcex:    { label: "KCEX",    cg_name: "KCEX (Futures)",    cycle_hours: 8 },
  weex:    { label: "WEEX",    cg_name: "WEEX (Futures)",    cycle_hours: 8 },
  orangex_perp: { label: "OrangeX Perp", cg_name: "OrangeX Futures", cycle_hours: 8 },
};

let _CG_DERIV_CACHE = { ts: 0, data: null };
async function _cgDerivatives() {
  const now = Date.now();
  if (_CG_DERIV_CACHE.data && (now - _CG_DERIV_CACHE.ts) < 60_000) return _CG_DERIV_CACHE.data;
  const j = await fetchJson("https://api.coingecko.com/api/v3/derivatives?include_tickers=all");
  const slx = (Array.isArray(j) ? j : []).filter(d => ((d.symbol || "") + "").toUpperCase().includes("SLX"));
  _CG_DERIV_CACHE = { ts: now, data: slx };
  return slx;
}

async function _fetchFromCG(key) {
  const meta = COINGECKO_MARKET_MAP[key];
  if (!meta) throw new Error(`unknown CG key ${key}`);
  const all = await _cgDerivatives();
  const row = all.find(d => (d.market || "").toLowerCase() === meta.cg_name.toLowerCase());
  if (!row) throw new Error(`${meta.label} not found in CoinGecko derivatives`);
  // CoinGecko funding_rate uses inconsistent scaling across venues (per-cycle
  // for some, annualized for others, unknown for the rest — verified 2026-06-01
  // when Gate showed -0.9144 in CG while actual was -0.02 per 4h).
  // We deliberately DROP the funding_rate field for CG-sourced venues to avoid
  // displaying misleading 100-200% values. OI + volume + price are reliable.
  return {
    venue: meta.label,
    oi_usd: row.open_interest != null ? parseFloat(row.open_interest) : null,
    funding_rate: null,                // intentionally omitted — see note above
    funding_cycle_hours: meta.cycle_hours,
    mark: row.price != null ? parseFloat(row.price) : null,
    volume_24h_usd: row.volume_24h != null ? parseFloat(row.volume_24h) : null,
    source: "coingecko",
  };
}

const VENUE_FETCHERS = {
  async gate() {
    const [c, oi] = await Promise.all([
      fetchJson("https://api.gateio.ws/api/v4/futures/usdt/contracts/SLX_USDT"),
      fetchJson("https://api.gateio.ws/api/v4/futures/usdt/contract_stats?contract=SLX_USDT&interval=5m&limit=1"),
    ]);
    const rate = parseFloat(c.funding_rate);
    const intervalSec = parseInt(c.funding_interval, 10) || 14400;
    const mark = parseFloat(c.mark_price) || 0;
    const oi_usd = parseFloat((oi[0] && oi[0].open_interest_usd) || 0);
    return { venue: "Gate", oi_usd, funding_rate: rate, funding_cycle_hours: intervalSec / 3600, mark };
  },

  async okx() {
    const [oiJ, fJ, tJ] = await Promise.all([
      fetchJson("https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=SLX-USDT-SWAP"),
      fetchJson("https://www.okx.com/api/v5/public/funding-rate?instId=SLX-USDT-SWAP"),
      fetchJson("https://www.okx.com/api/v5/market/ticker?instId=SLX-USDT-SWAP"),
    ]);
    const oi_usd = parseFloat((oiJ.data && oiJ.data[0] && oiJ.data[0].oiUsd) || 0);
    const rate = parseFloat((fJ.data && fJ.data[0] && fJ.data[0].fundingRate) || 0);
    const mark = parseFloat((tJ.data && tJ.data[0] && tJ.data[0].last) || 0);
    return { venue: "OKX", oi_usd, funding_rate: rate, funding_cycle_hours: 8, mark };
  },

  async binance_futures() {
    const [oiJ, fJ] = await Promise.all([
      fetchJson("https://fapi.binance.com/fapi/v1/openInterest?symbol=SLXUSDT"),
      fetchJson("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=SLXUSDT"),
    ]);
    const oi_slx = parseFloat(oiJ.openInterest || 0);
    const mark = parseFloat(fJ.markPrice || 0);
    const rate = parseFloat(fJ.lastFundingRate || 0);
    return { venue: "Binance Futures", oi_usd: oi_slx * mark, funding_rate: rate, funding_cycle_hours: 8, mark };
  },

  async bingx() {
    const j = await fetchJson("https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex?symbol=SLX-USDT");
    const d = Array.isArray(j.data) ? j.data[0] : j.data;
    const rate = parseFloat(d.lastFundingRate || 0);
    const mark = parseFloat(d.markPrice || 0);
    // BingX doesn't expose OI on public endpoints — return null.
    return { venue: "BingX", oi_usd: null, funding_rate: rate, funding_cycle_hours: 8, mark };
  },

  async bitget() {
    const [fJ, oiJ] = await Promise.all([
      fetchJson("https://api.bitget.com/api/v2/mix/market/current-fund-rate?symbol=SLXUSDT&productType=USDT-FUTURES"),
      fetchJson("https://api.bitget.com/api/v2/mix/market/open-interest?symbol=SLXUSDT&productType=USDT-FUTURES"),
    ]);
    const fd = Array.isArray(fJ.data) ? fJ.data[0] : fJ.data;
    const rate = parseFloat(fd.fundingRate || 0);
    const list = (oiJ.data && oiJ.data.openInterestList) || [];
    const od = list[0] || {};
    const amount = parseFloat(od.size || 0);
    const mark = parseFloat(od.priceUsd || 0);
    return { venue: "Bitget", oi_usd: amount * mark, funding_rate: rate, funding_cycle_hours: 8, mark };
  },

  async bitmart() {
    const j = await fetchJson("https://api-cloud-v2.bitmart.com/contract/public/details?symbol=SLXUSDT");
    const sym = ((j.data && j.data.symbols) || [])[0] || {};
    const rate = parseFloat(sym.funding_rate || 0);
    const mark = parseFloat(sym.last_price || 0);
    const oi_slx = parseFloat(sym.open_interest || 0);
    return { venue: "BitMart", oi_usd: oi_slx * mark, funding_rate: rate, funding_cycle_hours: 8, mark };
  },

  // Venues without direct public APIs — sourced from CoinGecko (OI + volume
  // reliable; funding rate marked unverified due to inconsistent scaling).
  async lbank()   { return await _fetchFromCG("lbank"); },
  async ourbit()  { return await _fetchFromCG("ourbit"); },
  async hotcoin() { return await _fetchFromCG("hotcoin"); },
  async kcex()    { return await _fetchFromCG("kcex"); },
  async weex()    { return await _fetchFromCG("weex"); },
  async orangex_perp() { return await _fetchFromCG("orangex_perp"); },

  async mexc() {
    // MEXC has no standalone /open_interest endpoint — OI lives on the
    // /ticker response as `holdVol`. Funding cycle is 4h on MEXC (not 8h —
    // per the contract's `collectCycle` field).
    const [fJ, tJ] = await Promise.all([
      fetchJson("https://contract.mexc.com/api/v1/contract/funding_rate/SLX_USDT"),
      fetchJson("https://contract.mexc.com/api/v1/contract/ticker?symbol=SLX_USDT"),
    ]);
    const rate = parseFloat((fJ.data && fJ.data.fundingRate) || 0);
    const cycleHours = parseInt((fJ.data && fJ.data.collectCycle) || 4, 10);
    const mark = parseFloat((tJ.data && tJ.data.lastPrice) || 0);
    const oi_slx = parseFloat((tJ.data && tJ.data.holdVol) || 0);
    return { venue: "MEXC", oi_usd: oi_slx * mark, funding_rate: rate, funding_cycle_hours: cycleHours, mark };
  },
};

export default async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");

  if (req.method === "OPTIONS") {
    res.status(204).end();
    return;
  }
  if (req.method !== "GET") {
    res.status(405).json({ error: "method not allowed" });
    return;
  }

  const venueKey = String(req.query.venue || "").toLowerCase().replace(/[\s-]/g, "_");
  const fetcher = VENUE_FETCHERS[venueKey];
  if (!fetcher) {
    res.status(400).json({
      error: `unknown venue '${venueKey}'`,
      supported: Object.keys(VENUE_FETCHERS),
    });
    return;
  }

  try {
    const data = await fetcher();
    // No cache — the depth tool refreshes on every page load.
    res.setHeader("Cache-Control", "no-store");
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    res.status(200).json(data);
  } catch (e) {
    const msg = e && e.name === "AbortError" ? `upstream timeout after ${TIMEOUT_MS}ms` : (e && e.message) || String(e);
    res.status(502).json({ error: msg, venue: venueKey });
  }
}

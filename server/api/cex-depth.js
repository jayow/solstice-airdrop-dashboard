// Vercel serverless proxy for CEX SLX/USDT depth endpoints that block CORS.
//
// Routed by Vercel as:  GET /api/cex-depth?venue=<key>
//
// Why this exists: the browser-side depth engine cannot call these CEX
// endpoints directly because the upstreams either omit
// Access-Control-Allow-Origin or actively reject preflights. Curl works fine,
// so a thin server-side passthrough is enough.
//
// Verified upstream (2026-05-31): mexc, gate, lbank, orangex, toobit.
// BingX is included for forward-compatibility but currently returns
// `symbol is not found` — fetcher will surface that as an upstream error.
//
// Runtime: Node 20.x (NOT edge — some CEXes block Vercel edge IPs and we
// want native Node fetch + AbortController).

export const config = {
  runtime: "nodejs",
};

const VENUES = {
  mexc: {
    url: "https://api.mexc.com/api/v3/depth?symbol=SLXUSDT&limit=100",
  },
  gate: {
    url: "https://api.gateio.ws/api/v4/spot/order_book?currency_pair=SLX_USDT&limit=100",
  },
  lbank: {
    url: "https://api.lbkex.com/v2/depth.do?symbol=slx_usdt&size=60",
  },
  orangex: {
    url: "https://api.orangex.com/api/v1/public/get_order_book?instrument_name=SLX-USDT&depth=20",
  },
  bingx: {
    // Symbol not yet listed (2026-05-31). Endpoint returns code=100204 until
    // BingX adds SLX. Fetcher treats non-200 / non-success codes as errors.
    url: "https://open-api.bingx.com/openApi/spot/v1/market/depth?symbol=SLX-USDT&limit=100",
  },
  toobit: {
    url: "https://api.toobit.com/quote/v1/depth?symbol=SOLSTICEUSDT&limit=100",
  },
};

const UPSTREAM_TIMEOUT_MS = 8_000;

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

  const venueKey = String(req.query.venue || "").toLowerCase();
  const spec = VENUES[venueKey];
  if (!spec) {
    res.status(400).json({
      error: `unknown venue '${venueKey}'`,
      supported: Object.keys(VENUES),
    });
    return;
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);

  try {
    const upstream = await fetch(spec.url, {
      headers: {
        accept: "application/json",
        // Some CEXes (notably BingX, MEXC) reject default fetch UA.
        "user-agent": "solstice-flares-proxy/1.0",
      },
      signal: controller.signal,
    });

    const body = await upstream.text();

    if (!upstream.ok) {
      res.status(502).json({
        error: `upstream HTTP ${upstream.status}`,
        venue: venueKey,
        upstreamBody: body.slice(0, 500),
      });
      return;
    }

    // Edge-cache the result for 15s, allow stale-while-revalidate for 30s.
    res.setHeader("Cache-Control", "s-maxage=15, stale-while-revalidate=30");
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    res.status(200).send(body);
  } catch (e) {
    const msg = e && e.name === "AbortError" ? `upstream timeout after ${UPSTREAM_TIMEOUT_MS}ms` : (e && e.message) || String(e);
    res.status(502).json({ error: msg, venue: venueKey });
  } finally {
    clearTimeout(timer);
  }
}

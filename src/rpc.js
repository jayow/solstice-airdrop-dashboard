import 'dotenv/config';

const RAW = (process.env.HELIUS_API_KEY || '').trim();
if (!RAW) {
  console.error('ERROR: HELIUS_API_KEY missing in .env');
  process.exit(1);
}
export const RPC_URL = RAW.startsWith('http')
  ? RAW
  : `https://mainnet.helius-rpc.com/?api-key=${RAW}`;
export const FEE_ADDRESS = process.env.FEE_ADDRESS || 'DuX1wcoQrJ6XypxLNq3GRrmHFAAMgCqAKbzboabyCtzB';

let requestId = 0;

function isRateLimit(res, errText) {
  if (res.status === 429 || res.status === 413) return true;
  // Helius sometimes returns HTTP 200 with a JSON-RPC error. Detect by
  // parsing — DO NOT regex the body because token/account names can contain
  // "rate" and falsely trigger (hours lost to this bug).
  if (res.status === 200 && errText) {
    try {
      const j = JSON.parse(errText);
      const err = j.error || (Array.isArray(j) && j[0]?.error);
      if (!err) return false;
      const code = err.code;
      const msg = err.message || '';
      if (code === -32429 || code === -32413) return true;
      if (/too many requests|max usage|rate.?limit/i.test(msg)) return true;
    } catch {}
  }
  return false;
}

function backoff(attempt) {
  return Math.min(4000, 300 * Math.pow(2, attempt)) + Math.random() * 200;
}

export async function rpc(method, params, { retries = 10, timeoutMs = 15000 } = {}) {
  const body = JSON.stringify({ jsonrpc: '2.0', id: ++requestId, method, params });
  let lastErr;
  for (let attempt = 0; attempt < retries; attempt++) {
    const controller = new AbortController();
    const t = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(RPC_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        signal: controller.signal,
      });
      const text = await res.text();
      if (isRateLimit(res, text)) {
        lastErr = new Error(`rate-limited HTTP ${res.status}`);
        await new Promise(r => setTimeout(r, backoff(attempt)));
        continue;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${text}`);
      const json = JSON.parse(text);
      if (json.error) {
        const msg = json.error.message || '';
        if (json.error.code === -32429 || json.error.code === -32413 || /too many|rate/i.test(msg)) {
          lastErr = new Error(`rate-limited RPC ${json.error.code}`);
          await new Promise(r => setTimeout(r, backoff(attempt)));
          continue;
        }
        throw new Error(`RPC error ${json.error.code}: ${msg}`);
      }
      return json.result;
    } catch (e) {
      lastErr = e;
      await new Promise(r => setTimeout(r, backoff(attempt)));
    } finally {
      clearTimeout(t);
    }
  }
  throw lastErr || new Error('rpc: retries exhausted');
}

export async function rpcBatch(calls, { retries = 20, timeoutMs = 30000 } = {}) {
  if (calls.length === 0) return [];
  const body = JSON.stringify(
    calls.map(c => ({ jsonrpc: '2.0', id: ++requestId, method: c.method, params: c.params }))
  );
  let lastErr;
  for (let attempt = 0; attempt < retries; attempt++) {
    const controller = new AbortController();
    const t = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(RPC_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        signal: controller.signal,
      });
      const text = await res.text();
      if (isRateLimit(res, text)) {
        lastErr = new Error(`rate-limited HTTP ${res.status}`);
        await new Promise(r => setTimeout(r, backoff(attempt)));
        continue;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${text}`);
      const json = JSON.parse(text);
      if (!Array.isArray(json)) {
        if (json.error && (json.error.code === -32429 || json.error.code === -32413 || /too many|rate/i.test(json.error.message || ''))) {
          lastErr = new Error(`rate-limited RPC ${json.error.code}`);
          await new Promise(r => setTimeout(r, backoff(attempt)));
          continue;
        }
        throw new Error(`Expected batch array, got: ${text.slice(0,200)}`);
      }
      const out = new Array(calls.length);
      let hadRateLimit = false;
      for (let i = 0; i < json.length; i++) {
        const r = json[i];
        if (r?.error) {
          const msg = r.error.message || '';
          if (r.error.code === -32429 || r.error.code === -32413 || /too many|rate/i.test(msg)) {
            hadRateLimit = true;
            break;
          }
          throw new Error(`Batch RPC error: ${msg}`);
        }
        out[i] = r?.result;
      }
      if (hadRateLimit) {
        lastErr = new Error('rate-limited inner batch');
        await new Promise(r => setTimeout(r, backoff(attempt)));
        continue;
      }
      return out;
    } catch (e) {
      lastErr = e;
      await new Promise(r => setTimeout(r, backoff(attempt)));
    } finally {
      clearTimeout(t);
    }
  }
  throw lastErr || new Error('rpc: retries exhausted');
}

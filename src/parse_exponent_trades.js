// Parse Exponent YT buy/sell events using Helius Enhanced Transactions API
// (batches of 100 signatures, ~35 TPS).
// For each tx we emit events of shape:
//   { sig, blockTime, market, signer, action, ytDelta, underlyingDelta, syDelta, usdNet }
import fs from 'node:fs';
import path from 'node:path';
import 'dotenv/config';
import { MARKETS, EXPONENT_PROGRAM } from './exponent_markets.js';

const SIGS_IN = path.resolve('data/exponent_sigs.json');
const TXS_OUT = path.resolve('data/exponent_trades.jsonl');
const BATCH = 100;
const CONCURRENCY = 3;

const RAW = (process.env.HELIUS_API_KEY || '').trim();
const KEY = RAW.startsWith('http') ? (RAW.match(/api-key=([^&]+)/) || [, ''])[1] : RAW;
if (!KEY) { console.error('HELIUS_API_KEY missing'); process.exit(1); }
const URL = `https://api.helius.xyz/v0/transactions?api-key=${KEY}&commitment=confirmed`;

function isRateLimit(res, text) { return res.status === 429 || res.status === 413 || /too many|rate/i.test(text || ''); }
function backoff(n) { return Math.min(4000, 300 * Math.pow(2, n)) + Math.random() * 200; }

async function fetchBatch(sigs, { retries = 10 } = {}) {
  let lastErr;
  for (let i = 0; i < retries; i++) {
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), 30000);
    try {
      const res = await fetch(URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ transactions: sigs }),
        signal: ctl.signal,
      });
      const text = await res.text();
      if (isRateLimit(res, text)) {
        lastErr = new Error(`rate-limited ${res.status}`);
        await new Promise(r => setTimeout(r, backoff(i)));
        continue;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`);
      return JSON.parse(text);
    } catch (e) {
      lastErr = e;
      await new Promise(r => setTimeout(r, backoff(i)));
    } finally {
      clearTimeout(to);
    }
  }
  throw lastErr || new Error('retries exhausted');
}

function classify(tx) {
  // Must be a successful Exponent tx (program in instructions)
  if (!tx || tx.transactionError) return [];
  const instrsTop = (tx.instructions || []).concat((tx.instructions || []).flatMap(i => i.innerInstructions || []));
  const touched = instrsTop.some(ix => ix.programId === EXPONENT_PROGRAM);
  if (!touched) return [];

  // Which YT mints are touched?
  const mintsInTx = new Set((tx.tokenTransfers || []).map(t => t.mint));
  const marketsHit = [];
  for (const [mkt, m] of Object.entries(MARKETS)) {
    if (mintsInTx.has(m.ytMint)) marketsHit.push(mkt);
  }
  if (marketsHit.length === 0) return [];

  const signer = tx.feePayer;
  const blockTime = tx.timestamp;

  // Sum signer-level token transfers: direction = +received, -sent (for signer as to/from)
  function signerDelta(mint) {
    let d = 0;
    for (const t of (tx.tokenTransfers || [])) {
      if (t.mint !== mint) continue;
      if (t.toUserAccount === signer) d += Number(t.tokenAmount || 0);
      if (t.fromUserAccount === signer) d -= Number(t.tokenAmount || 0);
    }
    return d;
  }

  // Find instruction type from description or actual instructions (Enhanced uses `description`).
  // We also look at the raw instruction names via description heuristics.
  const desc = (tx.description || '').toLowerCase();
  const nativeTransfers = tx.nativeTransfers || [];
  // Try raw instruction names from log via first-level Exponent instructions
  const exIx = (tx.instructions || []).filter(i => i.programId === EXPONENT_PROGRAM);
  const firstProgramLog = (tx.events?.logMessages || []);
  // Helius doesn't always return logs — fallback based on deltas

  const events = [];
  for (const mkt of marketsHit) {
    const m = MARKETS[mkt];
    const underlyingDelta = signerDelta(m.underlying);
    const syDelta = signerDelta(m.syMint);
    const ytDelta = signerDelta(m.ytMint);
    const usdNet = (underlyingDelta + syDelta) * m.underlyingUsdPrice;
    let action = 'other';
    if (ytDelta > 0.0001) action = 'buyYt';
    else if (ytDelta < -0.0001) action = 'sellYt';
    else if (underlyingDelta < -0.0001 || syDelta < -0.0001) action = 'buyYt'; // wrapper routes via non-signer YT accounts
    else if (underlyingDelta > 0.0001 || syDelta > 0.0001) action = 'sellYt';
    events.push({
      sig: tx.signature,
      blockTime,
      market: mkt,
      signer,
      action,
      ytDelta: +ytDelta.toFixed(6),
      underlyingDelta: +underlyingDelta.toFixed(6),
      syDelta: +syDelta.toFixed(6),
      usdNet: +usdNet.toFixed(4),
    });
  }
  return events;
}

async function worker(id, queue, out, state) {
  while (queue.length > 0) {
    const batch = queue.splice(0, BATCH);
    try {
      const txs = await fetchBatch(batch);
      for (let i = 0; i < batch.length; i++) {
        const sig = batch[i];
        const tx = txs[i];
        if (!tx) {
          out.write(JSON.stringify({ sig, error: 'no-tx' }) + '\n');
          continue;
        }
        const events = classify(tx);
        if (events.length === 0) {
          out.write(JSON.stringify({ sig, blockTime: tx.timestamp, events: [] }) + '\n');
        } else {
          for (const ev of events) out.write(JSON.stringify(ev) + '\n');
        }
      }
      state.processed += batch.length;
      process.stdout.write(`\rparsed ${state.processed}/${state.total}`);
    } catch (e) {
      for (const sig of batch) out.write(JSON.stringify({ sig, error: e?.message || String(e) }) + '\n');
      state.processed += batch.length;
      process.stdout.write(`\rparsed ${state.processed}/${state.total} (batch-fail: ${e.message})`);
    }
  }
}

async function main() {
  if (!fs.existsSync(SIGS_IN)) { console.error(`Missing ${SIGS_IN}`); process.exit(1); }
  const sigs = JSON.parse(fs.readFileSync(SIGS_IN, 'utf8'));
  console.log(`Loaded ${sigs.length} Exponent sigs`);

  const done = new Set();
  if (fs.existsSync(TXS_OUT)) {
    const lines = fs.readFileSync(TXS_OUT, 'utf8').split('\n').filter(Boolean);
    const kept = [];
    for (const l of lines) {
      try {
        const r = JSON.parse(l);
        if (r.error) continue;
        done.add(r.sig);
        kept.push(l);
      } catch {}
    }
    fs.writeFileSync(TXS_OUT, kept.length ? kept.join('\n') + '\n' : '');
    console.log(`Resuming: ${done.size} sigs already parsed`);
  }

  const queue = sigs.filter(s => !done.has(s.signature)).map(s => s.signature);
  console.log(`Fetching ${queue.length} txs in batches of ${BATCH} × ${CONCURRENCY} workers...`);

  const out = fs.createWriteStream(TXS_OUT, { flags: 'a' });
  const state = { processed: 0, total: queue.length };
  const workers = Array.from({ length: CONCURRENCY }, (_, i) => worker(i, queue, out, state));
  await Promise.all(workers);
  out.end();
  console.log(`\nDone.`);
}

main().catch(e => { console.error(e); process.exit(1); });

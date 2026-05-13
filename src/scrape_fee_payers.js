import fs from 'node:fs';
import path from 'node:path';
import pLimit from 'p-limit';
import { rpc, FEE_ADDRESS } from './rpc.js';

const SIGS_IN = path.resolve('data/signatures.json');
const TXS_OUT = path.resolve('data/fee_txs.jsonl');
const PAYERS_OUT = path.resolve('data/fee_payers.json');

const CONCURRENCY = 10;

const USDC_MINT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v';
const USDT_MINT = 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB';
const SOL_MINT = 'So11111111111111111111111111111111111111112';

function extractFeePayer(tx) {
  if (!tx || tx.meta?.err) return null;
  const meta = tx.meta;
  const msg = tx.transaction.message;
  const keys = msg.accountKeys.map(k => (typeof k === 'string' ? k : k.pubkey));

  const feeIdx = keys.findIndex(k => k === FEE_ADDRESS);
  if (feeIdx < 0) return null;

  const events = [];

  // SOL flow: net positive to fee address (excluding lamport fee paid by fee-payer of tx)
  const preSol = meta.preBalances[feeIdx] ?? 0;
  const postSol = meta.postBalances[feeIdx] ?? 0;
  const solDelta = postSol - preSol;
  if (solDelta > 0) {
    // Find the wallet whose SOL decreased most (net of fee). Exclude fee address.
    let bestSender = null;
    let bestDrop = 0;
    for (let i = 0; i < keys.length; i++) {
      if (i === feeIdx) continue;
      const d = (meta.preBalances[i] ?? 0) - (meta.postBalances[i] ?? 0);
      // adjust for tx fee if this account is the signer paying fee (index 0)
      const adjusted = i === 0 ? d - (meta.fee ?? 0) : d;
      if (adjusted > bestDrop) {
        bestDrop = adjusted;
        bestSender = keys[i];
      }
    }
    if (bestSender) {
      events.push({ sender: bestSender, mint: SOL_MINT, amount: solDelta / 1e9, decimals: 9 });
    }
  }

  // Token flow: find fee address's token balance increases
  const pre = meta.preTokenBalances || [];
  const post = meta.postTokenBalances || [];
  const preMap = new Map(pre.map(b => [`${b.accountIndex}:${b.mint}`, b]));
  const postMap = new Map(post.map(b => [`${b.accountIndex}:${b.mint}`, b]));

  // Index token balance deltas per (owner, mint)
  const deltas = new Map(); // key: owner|mint -> amountDelta
  const allKeys = new Set([...preMap.keys(), ...postMap.keys()]);
  for (const k of allKeys) {
    const p = preMap.get(k);
    const q = postMap.get(k);
    const owner = (q?.owner) || (p?.owner);
    const mint = (q?.mint) || (p?.mint);
    const dec = (q?.uiTokenAmount?.decimals ?? p?.uiTokenAmount?.decimals ?? 0);
    const preAmt = Number(p?.uiTokenAmount?.amount || 0);
    const postAmt = Number(q?.uiTokenAmount?.amount || 0);
    const delta = postAmt - preAmt;
    if (!owner || !mint || delta === 0) continue;
    const mapKey = `${owner}|${mint}`;
    const cur = deltas.get(mapKey) || { owner, mint, delta: 0, decimals: dec };
    cur.delta += delta;
    deltas.set(mapKey, cur);
  }

  // Find fee-address increases per mint
  for (const { owner, mint, delta, decimals } of deltas.values()) {
    if (owner !== FEE_ADDRESS) continue;
    if (delta <= 0) continue;
    // find matching decrease(s) of same mint from other owner
    let bestSender = null;
    let bestDrop = 0;
    for (const v of deltas.values()) {
      if (v.owner === FEE_ADDRESS) continue;
      if (v.mint !== mint) continue;
      if (v.delta < 0 && -v.delta > bestDrop) {
        bestDrop = -v.delta;
        bestSender = v.owner;
      }
    }
    if (!bestSender) {
      // fallback: fee-payer of tx (index 0)
      bestSender = keys[0];
    }
    events.push({ sender: bestSender, mint, amount: delta / Math.pow(10, decimals), decimals });
  }

  return events.length ? events : null;
}

async function main() {
  if (!fs.existsSync(SIGS_IN)) {
    console.error(`Missing ${SIGS_IN}. Run: npm run fee-payers -- --signatures first (node src/fetch_signatures.js)`);
    process.exit(1);
  }
  const allSigs = JSON.parse(fs.readFileSync(SIGS_IN, 'utf8')).filter(s => !s.err);

  // Resume: skip only successfully-parsed sigs; drop prior error rows so we retry them
  const done = new Set();
  if (fs.existsSync(TXS_OUT)) {
    const lines = fs.readFileSync(TXS_OUT, 'utf8').split('\n').filter(Boolean);
    const kept = [];
    for (const l of lines) {
      try {
        const row = JSON.parse(l);
        if (row.error) continue; // drop, will retry
        done.add(row.sig);
        kept.push(l);
      } catch {}
    }
    fs.writeFileSync(TXS_OUT, kept.length ? kept.join('\n') + '\n' : '');
    console.log(`Resuming: ${done.size} txs already parsed (retrying failures)`);
  }

  const toFetch = allSigs.filter(s => !done.has(s.signature)).map(s => s.signature);
  console.log(`Fetching ${toFetch.length} transactions...`);

  const out = fs.createWriteStream(TXS_OUT, { flags: 'a' });
  const limit = pLimit(CONCURRENCY);

  let processed = 0;
  let lastLog = Date.now();
  await Promise.all(toFetch.map(sig => limit(async () => {
    try {
      const tx = await rpc('getTransaction', [sig, { encoding: 'jsonParsed', maxSupportedTransactionVersion: 0, commitment: 'confirmed' }]);
      const events = extractFeePayer(tx);
      out.write(JSON.stringify({ sig, blockTime: tx?.blockTime, events }) + '\n');
    } catch (e) {
      const msg = e?.message || String(e);
      out.write(JSON.stringify({ sig, error: msg }) + '\n');
    }
    processed++;
    if (Date.now() - lastLog > 2000 || processed === toFetch.length) {
      process.stdout.write(`\rparsed ${processed}/${toFetch.length}`);
      lastLog = Date.now();
    }
  })));
  console.log();

  out.end();
  console.log(`\nAggregating payers...`);

  // Aggregate
  const agg = new Map(); // sender -> { count, totalUSDC, totalUSDT, totalSOL, mints: {mint: amount}, firstTime, lastTime }
  const lines = fs.readFileSync(TXS_OUT, 'utf8').split('\n').filter(Boolean);
  for (const l of lines) {
    let row; try { row = JSON.parse(l); } catch { continue; }
    if (!row.events) continue;
    for (const ev of row.events) {
      const k = ev.sender;
      let r = agg.get(k);
      if (!r) {
        r = { sender: k, txCount: 0, totalUSDC: 0, totalUSDT: 0, totalSOL: 0, otherMints: {}, firstTime: row.blockTime, lastTime: row.blockTime };
        agg.set(k, r);
      }
      r.txCount += 1;
      if (ev.mint === USDC_MINT) r.totalUSDC += ev.amount;
      else if (ev.mint === USDT_MINT) r.totalUSDT += ev.amount;
      else if (ev.mint === SOL_MINT) r.totalSOL += ev.amount;
      else r.otherMints[ev.mint] = (r.otherMints[ev.mint] || 0) + ev.amount;
      if (row.blockTime) {
        if (!r.firstTime || row.blockTime < r.firstTime) r.firstTime = row.blockTime;
        if (!r.lastTime || row.blockTime > r.lastTime) r.lastTime = row.blockTime;
      }
    }
  }

  const payers = [...agg.values()].sort((a, b) => b.txCount - a.txCount);
  fs.writeFileSync(PAYERS_OUT, JSON.stringify(payers, null, 2));
  console.log(`\nUnique fee payers: ${payers.length}`);
  console.log(`Top 5 by tx count:`);
  payers.slice(0, 5).forEach(p => {
    console.log(`  ${p.sender}  txs=${p.txCount}  USDC=${p.totalUSDC.toFixed(2)}  SOL=${p.totalSOL.toFixed(4)}`);
  });
}

main().catch(e => { console.error(e); process.exit(1); });

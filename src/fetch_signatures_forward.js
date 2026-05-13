// Fetch NEW signatures for FEE_ADDRESS (newer than what we already have).
// Walks from newest backward until it hits the newest known signature, then stops.
// Uses FORWARD_RPC env var if set, else falls back to the default rpc.js.
import fs from 'node:fs';
import path from 'node:path';
import { rpc as defaultRpc, FEE_ADDRESS } from './rpc.js';

const OUT = path.resolve('data/signatures.json');
const ALT = (process.env.FORWARD_RPC || '').trim();

async function altRpc(method, params, attempt = 0) {
  const body = JSON.stringify({ jsonrpc: '2.0', id: 1, method, params });
  try {
    const res = await fetch(ALT, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
    const text = await res.text();
    if (res.status === 429 || res.status === 503) throw new Error(`rate-limited HTTP ${res.status}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${text.slice(0,120)}`);
    const j = JSON.parse(text);
    if (j.error) throw new Error(`RPC error: ${j.error.message}`);
    return j.result;
  } catch (e) {
    if (attempt < 10) {
      const d = Math.min(8000, 500 * Math.pow(2, attempt)) + Math.random() * 200;
      await new Promise(r => setTimeout(r, d));
      return altRpc(method, params, attempt + 1);
    }
    throw e;
  }
}

const rpc = ALT ? altRpc : defaultRpc;

async function main() {
  if (ALT) console.log(`Using alt RPC: ${ALT}`);
  let existing = [];
  let knownNewest = null;
  if (fs.existsSync(OUT)) {
    existing = JSON.parse(fs.readFileSync(OUT, 'utf8'));
    if (existing.length) knownNewest = existing[0].signature;
  }
  const seen = new Set(existing.map(s => s.signature));
  console.log(`have ${existing.length} sigs; knownNewest=${knownNewest?.slice(0,12)}...`);

  const newRows = [];
  let before = null;
  let pageNum = 0;
  let reachedKnown = false;

  while (!reachedKnown) {
    const params = [FEE_ADDRESS, { limit: 1000 }];
    if (before) params[1].before = before;
    if (knownNewest) params[1].until = knownNewest;
    const page = await rpc('getSignaturesForAddress', params);
    pageNum++;
    if (!page || page.length === 0) break;
    for (const s of page) {
      if (s.signature === knownNewest) { reachedKnown = true; break; }
      if (!seen.has(s.signature)) { newRows.push(s); seen.add(s.signature); }
    }
    before = page[page.length - 1].signature;
    const oldest = new Date((page[page.length - 1].blockTime || 0) * 1000).toISOString();
    console.log(`page ${pageNum}: page=${page.length} new-total=${newRows.length} oldest-in-page=${oldest}`);
    if (page.length < 1000) break;
  }

  if (newRows.length === 0) {
    console.log('No new signatures.');
    return;
  }
  const merged = [...newRows, ...existing];
  fs.writeFileSync(OUT, JSON.stringify(merged));
  console.log(`Added ${newRows.length} new signatures (total ${merged.length}).`);
}

main().catch(e => { console.error(e); process.exit(1); });

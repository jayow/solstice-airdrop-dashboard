// Scrape all signatures that reference any Exponent-market address we care about.
// Writes incrementally to data/exponent_sigs.json so progress survives rate-limits.
// Re-run to resume from where it stopped.
import fs from 'node:fs';
import path from 'node:path';
import { rpc } from './rpc.js';
import { MARKETS } from './exponent_markets.js';

const OUT = path.resolve('data/exponent_sigs.json');
const CURSOR = path.resolve('data/exponent_sigs.cursor.json');

function loadExisting() {
  if (!fs.existsSync(OUT)) return { byKey: new Map() };
  try {
    const arr = JSON.parse(fs.readFileSync(OUT, 'utf8'));
    return { byKey: new Map(arr.map(s => [s.signature, s])) };
  } catch { return { byKey: new Map() }; }
}
function loadCursors() {
  if (!fs.existsSync(CURSOR)) return {};
  try { return JSON.parse(fs.readFileSync(CURSOR, 'utf8')); } catch { return {}; }
}
function save(byKey, cursors) {
  const arr = [...byKey.values()].sort((a,b)=>(b.blockTime||0)-(a.blockTime||0));
  fs.writeFileSync(OUT, JSON.stringify(arr));
  fs.writeFileSync(CURSOR, JSON.stringify(cursors, null, 2));
}

async function sigsFor(address, byKey, cursors) {
  let before = cursors[address]?.before || null;
  let done = cursors[address]?.done || false;
  if (done) { console.log(`  (already complete)`); return; }
  let pages = 0;
  while (true) {
    const params = [address, { limit: 1000 }];
    if (before) params[1].before = before;
    let page;
    try {
      page = await rpc('getSignaturesForAddress', params);
    } catch (e) {
      console.log(`\n  PAGING STOPPED (${e.message}). Will resume next run; cursor=${before || 'null'}`);
      cursors[address] = { before, done: false };
      save(byKey, cursors);
      return;
    }
    if (!page || page.length === 0) { done = true; break; }
    for (const s of page) {
      if (!s.err && !byKey.has(s.signature)) byKey.set(s.signature, { signature: s.signature, blockTime: s.blockTime });
    }
    before = page[page.length - 1].signature;
    pages++;
    process.stdout.write(`\r    page ${pages}: total ${byKey.size} sigs (this addr oldest=${new Date(page[page.length-1].blockTime*1000).toISOString()})       `);
    cursors[address] = { before, done: false };
    save(byKey, cursors);
    if (page.length < 1000) { done = true; break; }
  }
  process.stdout.write('\n');
  cursors[address] = { before, done };
  save(byKey, cursors);
}

async function main() {
  const { byKey } = loadExisting();
  const cursors = loadCursors();
  console.log(`Start: ${byKey.size} sigs already on disk`);

  for (const [key, m] of Object.entries(MARKETS)) {
    for (const addr of m.scrapeAddresses) {
      console.log(`[${key}] fetching sigs for ${addr}...`);
      await sigsFor(addr, byKey, cursors);
    }
  }
  save(byKey, cursors);
  console.log(`\nTotal unique Exponent sigs: ${byKey.size}`);
  const arr = [...byKey.values()];
  if (arr.length) {
    arr.sort((a,b)=>a.blockTime-b.blockTime);
    console.log(`Range: ${new Date(arr[0].blockTime*1000).toISOString()}  →  ${new Date(arr[arr.length-1].blockTime*1000).toISOString()}`);
  }
}

main().catch(e => { console.error(e); process.exit(1); });

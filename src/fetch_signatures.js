import fs from 'node:fs';
import path from 'node:path';
import { rpc, FEE_ADDRESS } from './rpc.js';

const OUT = path.resolve('data/signatures.json');

async function main() {
  let existing = [];
  let resumeBefore = null;
  if (fs.existsSync(OUT)) {
    existing = JSON.parse(fs.readFileSync(OUT, 'utf8'));
    if (existing.length) {
      resumeBefore = existing[existing.length - 1].signature;
      console.log(`Resuming from ${resumeBefore} (already have ${existing.length})`);
    }
  }

  const seen = new Set(existing.map(s => s.signature));
  let before = resumeBefore;
  let pageNum = 0;

  while (true) {
    const params = [FEE_ADDRESS, { limit: 1000 }];
    if (before) params[1].before = before;
    const page = await rpc('getSignaturesForAddress', params);
    pageNum++;
    if (!page || page.length === 0) {
      console.log(`Done: no more signatures.`);
      break;
    }
    let added = 0;
    for (const s of page) {
      if (!seen.has(s.signature)) {
        existing.push(s);
        seen.add(s.signature);
        added++;
      }
    }
    before = page[page.length - 1].signature;
    const oldestDate = new Date(page[page.length - 1].blockTime * 1000).toISOString();
    console.log(`page ${pageNum}: +${added} (total ${existing.length}) oldest=${oldestDate}`);
    fs.writeFileSync(OUT, JSON.stringify(existing));
    if (page.length < 1000) {
      console.log(`Done: last page had ${page.length} rows.`);
      break;
    }
  }

  console.log(`\nTotal signatures saved: ${existing.length}`);
  const successes = existing.filter(s => !s.err).length;
  console.log(`Successful: ${successes}, Failed: ${existing.length - successes}`);
}

main().catch(e => { console.error(e); process.exit(1); });

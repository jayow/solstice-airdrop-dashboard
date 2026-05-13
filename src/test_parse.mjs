import { rpc } from './rpc.js';
import fs from 'node:fs';
const sigs = JSON.parse(fs.readFileSync('/Users/jakeolaso/Downloads/Claude Projects/SolsticeAirdropUsers/data/exponent_sigs.json','utf8')).slice(0, 100);
const done = new Set(fs.readFileSync('/Users/jakeolaso/Downloads/Claude Projects/SolsticeAirdropUsers/data/exponent_trades.jsonl','utf8').split('\n').filter(Boolean).map(l => { try { return JSON.parse(l).sig; } catch { return null; } }).filter(Boolean));
const queue = sigs.map(s=>s.signature).filter(s => !done.has(s));
console.error('queue:', queue.length);
const out = fs.createWriteStream('/tmp/trades_debug.jsonl', { flags: 'w' });
let processed=0;
async function worker(id){
  while(queue.length>0){
    const sig=queue.shift();
    console.error(`[w${id}] start ${sig.slice(0,8)} q=${queue.length}`);
    const t0=Date.now();
    try {
      const tx=await rpc('getTransaction',[sig,{encoding:'jsonParsed',maxSupportedTransactionVersion:0}]);
      out.write(JSON.stringify({sig, ok:!!tx})+'\n');
      processed++;
      console.error(`[w${id}] done ${sig.slice(0,8)} ms=${Date.now()-t0}`);
    } catch(e) {
      console.error(`[w${id}] err ${sig.slice(0,8)} ${e.message}`);
    }
  }
}
const ws=Array.from({length:6},(_,i)=>worker(i));
await Promise.all(ws);
out.end();
console.error('done, processed=',processed);

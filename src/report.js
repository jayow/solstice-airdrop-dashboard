// Join fee payers with Exponent trade events and produce:
//  - data/report.csv       (one row per wallet)
//  - data/summary.txt      (ASCII summary + histogram)
import fs from 'node:fs';
import path from 'node:path';
import { MARKETS } from './exponent_markets.js';

const FEE_PAYERS = path.resolve('data/fee_payers.json');
const EXP_TRADES = path.resolve('data/exponent_trades.jsonl');
const CSV_OUT = path.resolve('data/report.csv');
const SUM_OUT = path.resolve('data/summary.txt');
const MD_OUT = path.resolve('data/report.md');

const USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v';
const USDT = 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB';
const SOL_PRICE_USD = 175; // rough estimate for SOL→USD on fee-payment mapping
// Wallets below these thresholds are "dust" (accidental transfers / bots) and
// excluded from the report.
const DUST_MIN_SOL = 0.001;
const DUST_MIN_STABLES = 0.01;

function loadFeePayers() {
  if (!fs.existsSync(FEE_PAYERS)) { console.error(`Missing ${FEE_PAYERS}`); process.exit(1); }
  return JSON.parse(fs.readFileSync(FEE_PAYERS, 'utf8'));
}

function loadExponentEvents() {
  if (!fs.existsSync(EXP_TRADES)) return [];
  const events = [];
  for (const l of fs.readFileSync(EXP_TRADES, 'utf8').split('\n')) {
    if (!l) continue;
    try {
      const r = JSON.parse(l);
      if (r.error || !r.signer) continue;
      events.push(r);
    } catch {}
  }
  return events;
}

function bar(n, max, width = 28) {
  if (max === 0) return '';
  const len = Math.max(1, Math.round((n / max) * width));
  return '█'.repeat(len);
}

function histogram(values, bins = 10, { logScale = false } = {}) {
  if (!values.length) return 'no data';
  let vs = values.slice();
  if (logScale) vs = vs.map(v => Math.log10(Math.max(v, 1)));
  const min = Math.min(...vs), max = Math.max(...vs);
  const bucketSize = (max - min) / bins || 1;
  const counts = new Array(bins).fill(0);
  for (const v of vs) {
    let i = Math.floor((v - min) / bucketSize);
    if (i >= bins) i = bins - 1;
    counts[i]++;
  }
  const cmax = Math.max(...counts);
  const lines = [];
  for (let i = 0; i < bins; i++) {
    const lo = min + i * bucketSize, hi = lo + bucketSize;
    const rangeLo = logScale ? Math.pow(10, lo) : lo;
    const rangeHi = logScale ? Math.pow(10, hi) : hi;
    lines.push(`  [${rangeLo.toFixed(2)} .. ${rangeHi.toFixed(2)}]  ${bar(counts[i], cmax)} ${counts[i]}`);
  }
  return lines.join('\n');
}

function main() {
  const rawPayers = loadFeePayers();
  const payers = rawPayers.filter(p =>
    p.totalSOL >= DUST_MIN_SOL || p.totalUSDC >= DUST_MIN_STABLES || p.totalUSDT >= DUST_MIN_STABLES
  );
  const dustCount = rawPayers.length - payers.length;
  const events = loadExponentEvents();

  // Aggregate per wallet
  const byWallet = new Map();
  for (const p of payers) {
    byWallet.set(p.sender, {
      wallet: p.sender,
      feeTxs: p.txCount,
      feeUSDC: +p.totalUSDC.toFixed(4),
      feeUSDT: +p.totalUSDT.toFixed(4),
      feeSOL: +p.totalSOL.toFixed(6),
      feeUSD: +(p.totalUSDC + p.totalUSDT + p.totalSOL * SOL_PRICE_USD).toFixed(2),
      firstFeeTime: p.firstTime,
      lastFeeTime: p.lastTime,
      // Per-market YT buys/sells
      ytBuysUSD_USX_09FEB26: 0, ytSellsUSD_USX_09FEB26: 0,
      ytBuysUSD_USX_01JUN26: 0, ytSellsUSD_USX_01JUN26: 0,
      ytBuysUSD_eUSX_11MAR26: 0, ytSellsUSD_eUSX_11MAR26: 0,
      ytBuysUSD_eUSX_01JUN26: 0, ytSellsUSD_eUSX_01JUN26: 0,
      // Aggregate YT buys/sells
      ytBuysUSD_USX: 0, ytSellsUSD_USX: 0, ytNetUSD_USX: 0,
      ytBuysUSD_eUSX: 0, ytSellsUSD_eUSX: 0, ytNetUSD_eUSX: 0,
      // LP activity (separate)
      lpAddUSD_USX: 0, lpRemoveUSD_USX: 0,
      lpAddUSD_eUSX: 0, lpRemoveUSD_eUSX: 0,
      expTxs: 0,
    });
  }
  const payerSet = new Set(byWallet.keys());

  // Unmatched events (signer not in fee payer list)
  let unmatched = 0;
  const unmatchedByWallet = new Map();
  // market -> ticker group
  const groupForMarket = m =>
    m.startsWith('USX-') ? 'USX'
    : m.startsWith('eUSX-') ? 'eUSX'
    : null;
  for (const ev of events) {
    const group = groupForMarket(ev.market);
    if (!group) continue;
    const row = byWallet.get(ev.signer);
    if (!row) {
      unmatched++;
      const u = unmatchedByWallet.get(ev.signer) || { wallet: ev.signer, txs: 0, usd: 0 };
      u.txs++; u.usd += Math.abs(ev.usdNet || 0);
      unmatchedByWallet.set(ev.signer, u);
      continue;
    }
    row.expTxs++;
    const usd = Math.abs(ev.usdNet || 0);
    const action = ev.action;  // now authoritative: from on-chain Instruction:
    if (action === 'buyYt') {
      row[`ytBuysUSD_${ev.market.replace('-','_')}`] = (row[`ytBuysUSD_${ev.market.replace('-','_')}`] || 0) + usd;
      row[`ytBuysUSD_${group}`] += usd;
    } else if (action === 'sellYt') {
      row[`ytSellsUSD_${ev.market.replace('-','_')}`] = (row[`ytSellsUSD_${ev.market.replace('-','_')}`] || 0) + usd;
      row[`ytSellsUSD_${group}`] += usd;
    } else if (action === 'addLiq') {
      row[`lpAddUSD_${group}`] += usd;
    } else if (action === 'removeLiq') {
      row[`lpRemoveUSD_${group}`] += usd;
    }
  }
  for (const row of byWallet.values()) {
    row.ytNetUSD_USX  = +(row.ytBuysUSD_USX  - row.ytSellsUSD_USX ).toFixed(2);
    row.ytNetUSD_eUSX = +(row.ytBuysUSD_eUSX - row.ytSellsUSD_eUSX).toFixed(2);
    for (const k of Object.keys(row)) {
      if (typeof row[k] === 'number' && /^yt/.test(k)) row[k] = +row[k].toFixed(2);
    }
  }

  const rows = [...byWallet.values()].sort((a, b) => (b.ytNetUSD_USX + b.ytNetUSD_eUSX) - (a.ytNetUSD_USX + a.ytNetUSD_eUSX));

  // CSV
  const cols = [
    'wallet','feeTxs','feeUSDC','feeUSDT','feeSOL','feeUSD','firstFeeTime','lastFeeTime',
    'expTxs',
    'ytBuysUSD_USX_09FEB26','ytSellsUSD_USX_09FEB26',
    'ytBuysUSD_eUSX_11MAR26','ytSellsUSD_eUSX_11MAR26',
    'ytBuysUSD_USX_01JUN26','ytSellsUSD_USX_01JUN26',
    'ytBuysUSD_eUSX_01JUN26','ytSellsUSD_eUSX_01JUN26',
    'ytBuysUSD_USX','ytSellsUSD_USX','ytNetUSD_USX',
    'ytBuysUSD_eUSX','ytSellsUSD_eUSX','ytNetUSD_eUSX',
    'lpAddUSD_USX','lpRemoveUSD_USX',
    'lpAddUSD_eUSX','lpRemoveUSD_eUSX',
  ];
  const csv = [cols.join(',')];
  for (const r of rows) {
    csv.push(cols.map(c => {
      const v = r[c];
      if (v == null) return '';
      if (typeof v === 'string' && /[,"]/.test(v)) return `"${v.replace(/"/g, '""')}"`;
      return v;
    }).join(','));
  }
  fs.writeFileSync(CSV_OUT, csv.join('\n'));

  // Summary
  const lines = [];
  const totalPayers = rows.length;
  const withYt = rows.filter(r => r.ytBuysUSD_USX + r.ytBuysUSD_eUSX + r.ytSellsUSD_USX + r.ytSellsUSD_eUSX > 0);
  const totalBuys = withYt.reduce((s, r) => s + r.ytBuysUSD_USX + r.ytBuysUSD_eUSX, 0);
  const totalSells = withYt.reduce((s, r) => s + r.ytSellsUSD_USX + r.ytSellsUSD_eUSX, 0);
  const totalFees = rows.reduce((s, r) => s + r.feeUSD, 0);
  const totalLpAdd = rows.reduce((s, r) => s + (r.lpAddUSD_USX||0) + (r.lpAddUSD_eUSX||0), 0);
  const totalLpRemove = rows.reduce((s, r) => s + (r.lpRemoveUSD_USX||0) + (r.lpRemoveUSD_eUSX||0), 0);

  lines.push('='.repeat(72));
  lines.push('SOLSTICE AIRDROP FEE-PAYERS → EXPONENT YT ACTIVITY');
  lines.push('='.repeat(72));
  lines.push(`Dust wallets excluded (<${DUST_MIN_SOL} SOL & no stables): ${dustCount}`);
  lines.push(`Fee-paying wallets (all-time):          ${totalPayers.toLocaleString()}`);
  lines.push(`Total fees paid (USDC+USDT+SOL):        $${totalFees.toLocaleString(undefined, { maximumFractionDigits: 2 })}`);
  lines.push(`Wallets with Exponent YT activity:      ${withYt.length.toLocaleString()}  (${(100*withYt.length/Math.max(totalPayers,1)).toFixed(1)}%)`);
  lines.push(`Total pure-YT buys (USD):               $${totalBuys.toLocaleString(undefined, { maximumFractionDigits: 0 })}`);
  lines.push(`Total pure-YT sells (USD):              $${totalSells.toLocaleString(undefined, { maximumFractionDigits: 0 })}`);
  lines.push(`Net pure-YT buying pressure (USD):      $${(totalBuys - totalSells).toLocaleString(undefined, { maximumFractionDigits: 0 })}`);
  lines.push(`Total LP deposits (USD):                $${totalLpAdd.toLocaleString(undefined, { maximumFractionDigits: 0 })}`);
  lines.push(`Total LP withdrawals (USD):             $${totalLpRemove.toLocaleString(undefined, { maximumFractionDigits: 0 })}`);
  lines.push(`Exponent trade events from non-payer signers (skipped): ${unmatched}`);
  lines.push('');
  lines.push('-- Top 20 wallets by net YT USD (buys − sells) --');
  lines.push('wallet                                         feeUSD   USX-net   eUSX-net   total-net  expTxs');
  for (const r of rows.slice(0, 20)) {
    const tot = r.ytNetUSD_USX + r.ytNetUSD_eUSX;
    lines.push(`${r.wallet.padEnd(44)}  ${String('$'+r.feeUSD.toFixed(0)).padStart(8)}  ${String('$'+r.ytNetUSD_USX.toFixed(0)).padStart(9)}  ${String('$'+r.ytNetUSD_eUSX.toFixed(0)).padStart(9)}  ${String('$'+tot.toFixed(0)).padStart(10)}  ${String(r.expTxs).padStart(5)}`);
  }
  lines.push('');
  lines.push('-- Distribution of YT-net USD across wallets (log-scaled) --');
  const netVals = rows.map(r => r.ytNetUSD_USX + r.ytNetUSD_eUSX).filter(v => v > 0);
  lines.push(histogram(netVals, 10, { logScale: true }));
  lines.push('');
  lines.push('-- Distribution of fees paid (USD) across wallets --');
  const feeVals = rows.map(r => r.feeUSD).filter(v => v > 0);
  lines.push(histogram(feeVals, 10));
  lines.push('');
  lines.push('-- Largest "non-fee-payer" wallets active on Exponent YT (may be sub-wallets) --');
  const uList = [...unmatchedByWallet.values()].sort((a,b)=>b.usd-a.usd).slice(0,10);
  for (const u of uList) lines.push(`  ${u.wallet}  txs=${u.txs}  |usd|=$${u.usd.toFixed(0)}`);
  lines.push('');
  fs.writeFileSync(SUM_OUT, lines.join('\n'));

  // Markdown report with visualized tables (for copy-paste)
  const md = [];
  md.push('# Solstice Airdrop Fee-Payers → Exponent YT Activity\n');
  md.push(`_Generated ${new Date().toISOString()}_\n`);
  md.push('## Headline');
  md.push(`- Fee-paying wallets (all-time, ex. dust): **${totalPayers.toLocaleString()}**`);
  md.push(`- Total fees paid (USD equiv): **$${totalFees.toLocaleString(undefined, { maximumFractionDigits: 0 })}**`);
  md.push(`- Wallets with any Exponent YT activity: **${withYt.length.toLocaleString()}** (${(100*withYt.length/Math.max(totalPayers,1)).toFixed(1)}%)`);
  md.push(`- Total YT buy volume (USD): **$${totalBuys.toLocaleString(undefined, { maximumFractionDigits: 0 })}**`);
  md.push(`- Total YT sell volume (USD): **$${totalSells.toLocaleString(undefined, { maximumFractionDigits: 0 })}**`);
  md.push(`- Net YT buy-pressure (USD): **$${(totalBuys-totalSells).toLocaleString(undefined, { maximumFractionDigits: 0 })}**\n`);

  md.push('## Top 30 fee-payer wallets by net YT USD spent\n');
  md.push('| # | wallet | fee $ | USX feb09 net | USX jun01 net | eUSX jun01 net | total net | exp-txs |');
  md.push('|---|---|---:|---:|---:|---:|---:|---:|');
  for (let i = 0; i < Math.min(30, rows.length); i++) {
    const r = rows[i];
    const usx_feb = (r.ytBuysUSD_USX_09FEB26||0) - (r.ytSellsUSD_USX_09FEB26||0);
    const usx_jun = (r.ytBuysUSD_USX_01JUN26||0) - (r.ytSellsUSD_USX_01JUN26||0);
    const eusx_jun = (r.ytBuysUSD_eUSX_01JUN26||0) - (r.ytSellsUSD_eUSX_01JUN26||0);
    const tot = usx_feb + usx_jun + eusx_jun;
    md.push(`| ${i+1} | \`${r.wallet}\` | $${r.feeUSD.toFixed(0)} | $${usx_feb.toFixed(0)} | $${usx_jun.toFixed(0)} | $${eusx_jun.toFixed(0)} | **$${tot.toFixed(0)}** | ${r.expTxs} |`);
  }
  md.push('');
  md.push('## Distribution of net YT USD (across wallets with any YT activity)');
  md.push('```');
  md.push(histogram(netVals, 10, { logScale: true }));
  md.push('```\n');
  md.push('## Distribution of fees paid (USD)');
  md.push('```');
  md.push(histogram(feeVals, 10));
  md.push('```\n');
  md.push('## Notes');
  md.push('- YT USD amount = signer net (underlying + SY) delta at 1 USX / 1.01 eUSX.');
  md.push('- Buys = WrapperBuyYt / BuyYt / (underlying paid, YT received).');
  md.push('- Sells = WrapperSellYt / SellYt / (underlying received, YT given).');
  md.push('- USX-09FEB26 matured; USX-01JUN26 & eUSX-01JUN26 currently live.');
  md.push('- `feeUSD` = USDC + USDT + SOL × $175 (SOL price approximation).');
  fs.writeFileSync(MD_OUT, md.join('\n'));

  console.log(lines.join('\n'));
  console.log(`\nCSV:      ${CSV_OUT}`);
  console.log(`Summary:  ${SUM_OUT}`);
  console.log(`Markdown: ${MD_OUT}`);
}

main();

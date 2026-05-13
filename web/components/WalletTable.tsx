'use client';
import { useMemo, useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import type { WalletRow, MarketKey, PartnerKey, PartnerFootprint } from '@/lib/types';

const PAGE_SIZE = 100;

const MARKETS: { key: MarketKey; label: string }[] = [
  { key: 'USX-09FEB26',  label: 'USX · 09 Feb 26' },
  { key: 'eUSX-11MAR26', label: 'eUSX · 11 Mar 26' },
  { key: 'USX-01JUN26',  label: 'USX · 01 Jun 26' },
  { key: 'eUSX-01JUN26', label: 'eUSX · 01 Jun 26' },
];

type SortKey =
  | 'wallet'
  | 'cohort'
  | 'partners'
  | 'presale'
  | 'ytNet' | 'totalYtBuys' | 'totalYtSells'
  | `m:${MarketKey}` | 'lpUsx' | 'lpEusx' | 'claim' | 'expTxs';

type CohortFilter = 'all' | 'any' | '1' | '2' | '3' | '4' | '5' | '6' | 'none';

const SLX_TOTAL_SUPPLY = 1_000_000_000;

export function WalletTable({ wallets }: { wallets: WalletRow[] }) {
  const router = useRouter();
  const [q, setQ] = useState('');
  const [sortKey, setSortKey] = useState<SortKey>('cohort');
  const [asc, setAsc] = useState(true);
  const [minUsd, setMinUsd] = useState(0);
  const [hideInactive, setHideInactive] = useState(false);
  const [cohortFilter, setCohortFilter] = useState<CohortFilter>('all');
  const [page, setPage] = useState(0);

  // Reset to first page whenever any filter/sort changes
  useEffect(() => { setPage(0); }, [q, hideInactive, minUsd, cohortFilter, sortKey, asc]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return wallets.filter(w => {
      if (needle && !w.addr.toLowerCase().includes(needle)) return false;
      if (hideInactive && w.totalYtBuys + w.totalYtSells === 0) return false;
      if (minUsd > 0 && w.totalYtBuys < minUsd) return false;
      if (cohortFilter === 'any' && !w.cohort) return false;
      if (cohortFilter === 'none' && w.cohort) return false;
      if (cohortFilter !== 'all' && cohortFilter !== 'any' && cohortFilter !== 'none'
          && w.cohort !== cohortFilter) return false;
      return true;
    });
  }, [wallets, q, hideInactive, minUsd, cohortFilter]);

  const sorted = useMemo(() => {
    const cohortRank = (w: WalletRow): number => w.cohort ? parseInt(w.cohort, 10) : 99;
    const getNum = (w: WalletRow): number => {
      switch (sortKey) {
        case 'cohort': return cohortRank(w);
        case 'partners': return partnerScore(w);
        case 'presale': return w.presale?.deposited || 0;
        case 'ytNet': return w.ytNet;
        case 'totalYtBuys': return w.totalYtBuys;
        case 'totalYtSells': return w.totalYtSells;
        case 'lpUsx': return w.lp.USX.add - w.lp.USX.remove;
        case 'lpEusx': return w.lp.eUSX.add - w.lp.eUSX.remove;
        case 'claim': return w.totalClaims || 0;
        case 'expTxs': return w.expTxs;
        default: {
          if (sortKey.startsWith('m:')) {
            const k = sortKey.slice(2) as MarketKey;
            return w.m[k].buy - w.m[k].sell;
          }
          return 0;
        }
      }
    };
    const arr = [...filtered];
    if (sortKey === 'wallet') {
      arr.sort((a, b) => a.addr.localeCompare(b.addr) * (asc ? 1 : -1));
    } else {
      arr.sort((a, b) => (getNum(a) - getNum(b)) * (asc ? 1 : -1));
    }
    return arr;
  }, [filtered, sortKey, asc]);

  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages - 1);
  const visible = sorted.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);
  const firstIdx = safePage * PAGE_SIZE;

  function onSort(k: SortKey) {
    if (sortKey === k) setAsc(v => !v);
    else { setSortKey(k); setAsc(false); }
  }
  function arrow(k: SortKey) {
    if (sortKey !== k) return null;
    return <span className="ml-1 text-ink-200">{asc ? '↑' : '↓'}</span>;
  }

  return (
    <section className="surface overflow-hidden">
      <div className="flex flex-wrap items-center gap-3 px-5 py-4 hair-b">
        <input
          value={q} onChange={e => setQ(e.target.value)}
          placeholder="Search wallet…"
          className="flex-1 min-w-[240px] bg-ink-850 border border-white/[0.06] focus:border-ink-500 focus:outline-none rounded-md px-3 py-2 text-[13px] placeholder-ink-500 font-mono"
        />
        <label className="flex items-center gap-2 text-[12px] text-ink-300">
          <input type="checkbox" checked={hideInactive} onChange={e => setHideInactive(e.target.checked)} className="accent-accent-500" />
          YT active
        </label>
        <label className="flex items-center gap-2 text-[12px] text-ink-300">
          min buys $
          <input type="number" min={0} value={minUsd} onChange={e => setMinUsd(Number(e.target.value) || 0)}
            className="w-20 bg-ink-850 border border-white/[0.06] rounded-md px-2 py-1 text-[13px] tabular-nums" />
        </label>
        <label className="flex items-center gap-2 text-[12px] text-ink-300">
          cohort
          <select
            value={cohortFilter}
            onChange={e => setCohortFilter(e.target.value as CohortFilter)}
            className="bg-ink-850 border border-white/[0.06] rounded-md px-2 py-1 text-[13px]"
          >
            <option value="all">all</option>
            <option value="any">any</option>
            <option value="1">1</option>
            <option value="2">2</option>
            <option value="3">3</option>
            <option value="4">4</option>
            <option value="5">5</option>
            <option value="6">6</option>
            <option value="none">none</option>
          </select>
        </label>
        <div className="ml-auto flex items-center gap-3 text-[12px] text-ink-400 tabular-nums">
          <span>
            <span className="text-ink-200">{sorted.length.toLocaleString()}</span>
            <span className="text-ink-500"> / {wallets.length.toLocaleString()}</span>
          </span>
          <Pager page={safePage} totalPages={totalPages} onPage={setPage} />
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-[13px]">
          <thead>
            <tr className="hair-b">
              <th className="cell text-ink-500 text-[11px] uppercase tracking-wider">#</th>
              <th className="th-sortable cell" onClick={() => onSort('wallet')}>Wallet{arrow('wallet')}</th>
              <th className="th-sortable cell text-center" onClick={() => onSort('cohort')}>Cohort{arrow('cohort')}</th>
              <th
                className="th-sortable cell text-center"
                onClick={() => onSort('partners')}
                title="Flares-earning footprint: H=Direct wallet holdings, K=Kamino, O=Orca, R=Raydium. Filled = activity before the Season 1 snapshot (2026-04-13 UTC). H = wallet has a USX or eUSX token account (held at some point, even if currently 0)."
              >Partners{arrow('partners')}</th>
              <th
                className="th-sortable cell text-right"
                onClick={() => onSort('presale')}
                title="USDC invested in the SLX presale (Legion sale, program CHtfHPSi…6cyK). Presale SLX is distributed directly by Legion — separate from the Flares-cohort airdrop. Presale participants also earned bonus Flares that feed into their cohort assignment."
              >Presale $ <span className="text-ink-500">ⓘ</span>{arrow('presale')}</th>
              {MARKETS.map(m => (
                <th key={m.key} className="th-sortable cell text-right" onClick={() => onSort(`m:${m.key}`)}>
                  {m.label}{arrow(`m:${m.key}`)}
                </th>
              ))}
              <th className="th-sortable cell text-right" onClick={() => onSort('totalYtBuys')}>YT buys{arrow('totalYtBuys')}</th>
              <th className="th-sortable cell text-right" onClick={() => onSort('totalYtSells')}>YT sells{arrow('totalYtSells')}</th>
              <th className="th-sortable cell text-right" onClick={() => onSort('ytNet')}>YT net{arrow('ytNet')}</th>
              <th className="th-sortable cell text-right" onClick={() => onSort('lpUsx')}>LP USX{arrow('lpUsx')}</th>
              <th className="th-sortable cell text-right" onClick={() => onSort('lpEusx')}>LP eUSX{arrow('lpEusx')}</th>
              <th className="th-sortable cell text-right" onClick={() => onSort('claim')}>Claimed{arrow('claim')}</th>
              <th className="th-sortable cell text-right" onClick={() => onSort('expTxs')}>Txs{arrow('expTxs')}</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((w, i) => (
              <tr
                key={w.addr}
                onClick={() => router.push(`/wallet/?addr=${w.addr}`)}
                className="cursor-pointer row-hover hair-b"
              >
                <td className="cell text-ink-500 tabular-nums">{firstIdx + i + 1}</td>
                <td className="cell">
                  <div className="flex items-center gap-2">
                    <span className="text-ink-100 font-mono text-[12px]">{short(w.addr)}</span>
                    <a
                      onClick={e => e.stopPropagation()}
                      className="text-ink-500 hover:text-ink-200 text-[11px]"
                      href={`https://solscan.io/account/${w.addr}`}
                      target="_blank" rel="noopener noreferrer"
                      title="View on Solscan"
                    >↗</a>
                  </div>
                </td>
                <td className="cell text-center" title={cohortTitle(w)}>{cohortBadge(w)}</td>
                <td className="cell text-center">{partnerBadges(w)}</td>
                <td className="cell text-right tabular-nums" title={presaleTitle(w)}>{presaleCell(w)}</td>
                {MARKETS.map(m => (
                  <td key={m.key} className="cell text-right tabular-nums"
                      title={`buys $${fmt(w.m[m.key].buy)}, sells $${fmt(w.m[m.key].sell)}`}>
                    {cellNet(w.m[m.key].buy - w.m[m.key].sell)}
                  </td>
                ))}
                <td className="cell text-right tabular-nums text-good">{w.totalYtBuys > 0 ? `$${fmt(w.totalYtBuys, 0)}` : em()}</td>
                <td className="cell text-right tabular-nums text-bad/80">{w.totalYtSells > 0 ? `$${fmt(w.totalYtSells, 0)}` : em()}</td>
                <td className="cell text-right tabular-nums text-ink-50 font-medium">{cellNet(w.ytNet)}</td>
                <td className="cell text-right tabular-nums" title={`add $${fmt(w.lp.USX.add)}, remove $${fmt(w.lp.USX.remove)}`}>
                  {cellNet(w.lp.USX.add - w.lp.USX.remove, 'lp')}
                </td>
                <td className="cell text-right tabular-nums" title={`add $${fmt(w.lp.eUSX.add)}, remove $${fmt(w.lp.eUSX.remove)}`}>
                  {cellNet(w.lp.eUSX.add - w.lp.eUSX.remove, 'lp')}
                </td>
                <td className="cell text-right tabular-nums text-accent-400" title={`USX $${fmt(w.claim?.USX || 0)}, eUSX $${fmt(w.claim?.eUSX || 0)}`}>
                  {(w.totalClaims || 0) > 0 ? `$${fmt(w.totalClaims || 0, 2)}` : em()}
                </td>
                <td className="cell text-right tabular-nums text-ink-400">{w.expTxs}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {sorted.length > PAGE_SIZE && (
        <div className="flex items-center justify-between gap-3 px-5 py-3 hair-t">
          <span className="text-[12px] text-ink-500 tabular-nums">
            {(firstIdx + 1).toLocaleString()}–{Math.min(firstIdx + PAGE_SIZE, sorted.length).toLocaleString()}
            <span className="text-ink-600"> of {sorted.length.toLocaleString()}</span>
          </span>
          <Pager page={safePage} totalPages={totalPages} onPage={setPage} />
        </div>
      )}
    </section>
  );
}

function Pager({ page, totalPages, onPage }: { page: number; totalPages: number; onPage: (p: number) => void }) {
  const btn = 'inline-flex items-center justify-center h-7 min-w-[28px] px-2 rounded-md border border-white/[0.06] bg-ink-900 text-ink-300 hover:text-ink-50 hover:border-white/[0.12] disabled:opacity-30 disabled:cursor-not-allowed transition text-[12px]';
  return (
    <div className="flex items-center gap-1.5">
      <button className={btn} onClick={() => onPage(0)} disabled={page === 0} aria-label="First">«</button>
      <button className={btn} onClick={() => onPage(page - 1)} disabled={page === 0} aria-label="Previous">‹</button>
      <span className="text-[12px] text-ink-300 tabular-nums px-1 min-w-[60px] text-center">
        {page + 1} <span className="text-ink-500">/ {totalPages}</span>
      </span>
      <button className={btn} onClick={() => onPage(page + 1)} disabled={page >= totalPages - 1} aria-label="Next">›</button>
      <button className={btn} onClick={() => onPage(totalPages - 1)} disabled={page >= totalPages - 1} aria-label="Last">»</button>
    </div>
  );
}

function short(a: string) { return a.slice(0, 4) + '…' + a.slice(-4); }
function fmt(n: number, d = 0) { return n.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }); }
function em() { return <span className="text-ink-600">—</span>; }

function cellNet(v: number, kind: 'yt' | 'lp' = 'yt') {
  if (v === 0) return em();
  const cls = v > 0 ? (kind === 'lp' ? 'text-accent-400' : 'text-good') : 'text-bad/80';
  return <span className={cls}>${fmt(Math.abs(v), 0)}</span>;
}

// Cohort tier — single accent at varying intensity, no rainbow
const COHORT_TONE: Record<string, string> = {
  '1': 'bg-accent-500/15 text-accent-300 border-accent-500/30',
  '2': 'bg-accent-500/10 text-accent-300/90 border-accent-500/25',
  '3': 'bg-accent-500/[0.06] text-accent-300/80 border-accent-500/15',
  '4': 'bg-ink-100/[0.04] text-ink-200 border-ink-100/10',
  '5': 'bg-ink-100/[0.03] text-ink-300 border-ink-100/[0.08]',
  '6': 'bg-ink-100/[0.02] text-ink-400 border-ink-100/[0.06]',
};

function cohortBadge(w: WalletRow) {
  if (!w.cohort) return <span className="text-ink-600 text-xs">—</span>;
  const tone = COHORT_TONE[w.cohort] || 'bg-ink-100/5 text-ink-300 border-ink-100/10';
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-md border px-1.5 py-0.5 text-[11px] font-medium tabular-nums ${tone}`}>
      {w.cohort}
      {w.claimed
        ? <span className="h-[5px] w-[5px] rounded-full bg-good" />
        : <span className="h-[5px] w-[5px] rounded-full bg-accent-500" title="Orphan fee" />}
    </span>
  );
}

function perUserSlx(w: WalletRow): number | null {
  if (!w.cohortShareOfSlxPct || !w.cohortUsers) return null;
  return (w.cohortShareOfSlxPct / 100) * SLX_TOTAL_SUPPLY / w.cohortUsers;
}

// Partner footprint helpers ----------------------------------------------
const PARTNER_LABELS: Record<PartnerKey, { letter: string; name: string }> = {
  holdings: { letter: 'H', name: 'Direct holdings' },
  kamino:   { letter: 'K', name: 'Kamino' },
  orca:     { letter: 'O', name: 'Orca' },
  raydium:  { letter: 'R', name: 'Raydium' },
};

const PARTNER_ORDER: PartnerKey[] = ['holdings', 'kamino', 'orca', 'raydium'];

function partnerScore(w: WalletRow): number {
  let s = 0;
  for (const k of PARTNER_ORDER) {
    const p = w.partners?.[k];
    if (!p) continue;
    s += p.pre ? 10 : 1;
  }
  return s;
}

function partnerCellTone(p: PartnerFootprint | null): string {
  if (!p) return 'bg-ink-100/[0.02] text-ink-600 border-ink-100/[0.06]';
  if (p.pre) return 'bg-accent-500/15 text-accent-300 border-accent-500/30';
  return 'bg-ink-100/[0.04] text-ink-300 border-ink-100/10';
}

function partnerTooltip(partner: PartnerKey, p: PartnerFootprint | null): string {
  const label = PARTNER_LABELS[partner].name;
  if (!p) return `${label}: no footprint`;
  if (partner === 'holdings') {
    const curr = (p.usxCurr || 0) + (p.eusxCurr || 0);
    const held = [p.usxHeld && 'USX', p.eusxHeld && 'eUSX'].filter(Boolean).join(' + ');
    const currDesc = curr > 0
      ? `holds now: ${fmt(p.usxCurr || 0, 0)} USX + ${fmt(p.eusxCurr || 0, 0)} eUSX`
      : 'currently 0 (withdrew)';
    return `Direct ${held} in-wallet\n${currDesc}`;
  }
  const first = p.firstTs ? new Date(p.firstTs * 1000).toISOString().slice(0, 10) : '?';
  const last  = p.lastTs  ? new Date(p.lastTs  * 1000).toISOString().slice(0, 10) : '?';
  const tag = p.pre ? 'PRE-SNAPSHOT ✓' : 'post-snapshot only';
  const extra =
    partner === 'kamino' && p.supplyUsd != null
      ? `\nsupply: $${fmt(p.supplyUsd, 0)}  borrow: $${fmt(p.borrowUsd || 0, 0)}`
      : '';
  return `${label} · ${tag}\n${p.txs || 0} txs · ${first} → ${last}${extra}`;
}

function partnerBadges(w: WalletRow) {
  return (
    <div className="inline-flex items-center gap-1">
      {PARTNER_ORDER.map(k => {
        const p = w.partners?.[k];
        return (
          <span
            key={k}
            title={partnerTooltip(k, p || null)}
            className={`inline-flex items-center justify-center h-[18px] w-[18px] rounded border text-[10px] font-semibold tabular-nums ${partnerCellTone(p || null)}`}
          >
            {PARTNER_LABELS[k].letter}
          </span>
        );
      })}
    </div>
  );
}

// Presale helpers --------------------------------------------------------
function presaleCell(w: WalletRow) {
  const p = w.presale;
  if (!p || !p.deposited) return em();
  // Fully-refunded: strike through
  if (p.status === 'refunded') {
    return (
      <span className="text-ink-500 line-through" title="Fully refunded">
        ${fmt(p.deposited, 0)}
      </span>
    );
  }
  // Partial refund: show net + "−ref" badge
  if (p.status === 'partial') {
    const cls =
      p.net >= 50000 ? 'text-accent-300 font-medium' :
      p.net >= 5000  ? 'text-accent-400' :
                       'text-ink-200';
    return (
      <span className="inline-flex items-baseline gap-1">
        <span className={cls}>${fmt(p.net, 0)}</span>
        <span className="text-[9px] text-ink-500" title={`deposited $${fmt(p.deposited, 0)}, refunded $${fmt(p.refunded, 0)}`}>
          (−${fmt(p.refunded, 0)})
        </span>
      </span>
    );
  }
  // Kept full amount
  const cls =
    p.deposited >= 50000 ? 'text-accent-300 font-medium' :
    p.deposited >= 5000  ? 'text-accent-400' :
                           'text-ink-200';
  return <span className={cls}>${fmt(p.deposited, 0)}</span>;
}

function presaleTitle(w: WalletRow) {
  const p = w.presale;
  if (!p || !p.deposited) return 'No SLX presale deposit from this wallet.';
  const first = p.firstTs ? new Date(p.firstTs * 1000).toISOString().slice(0, 10) : '?';
  const last  = p.lastTs  ? new Date(p.lastTs  * 1000).toISOString().slice(0, 10) : '?';
  const statusLabel =
    p.status === 'refunded' ? 'FULLY REFUNDED — received all $ back from vault' :
    p.status === 'partial'  ? `PARTIAL REFUND — $${fmt(p.refunded, 0)} returned, $${fmt(p.net, 0)} kept` :
                              'kept full deposit, no refund on-record';
  const early = p.firstTs && p.firstTs < 1766412000
    ? '\n⚡ Deposited BEFORE public presale opened (2025-12-22) — strategic/team round.'
    : '';
  return `SLX presale · deposited $${fmt(p.deposited, 2)} over ${p.txCount} tx (${first} → ${last})\n${statusLabel}${early}`;
}

function cohortTitle(w: WalletRow) {
  if (!w.cohort) {
    return 'No Solstice cohort assigned (Solstice API returned cohort=null).';
  }
  const slx = perUserSlx(w);
  const slxStr = slx != null ? `~${fmt(Math.round(slx))} SLX (est., non-linear)` : '—';
  const share = w.cohortShareOfSlxPct ? w.cohortShareOfSlxPct.toFixed(2) + '%' : '—';
  const users = w.cohortUsers ? w.cohortUsers.toLocaleString() : '—';
  if (w.claimed) {
    return `Cohort ${w.cohort} · ${share} of SLX supply · ${users} users · ${slxStr} per user · Claimed`;
  }
  return (
    `Cohort ${w.cohort} · ${share} of SLX supply · ${slxStr} per user.\n` +
    `No claim tx: fee paid on-chain, but the claim step never completed.`
  );
}

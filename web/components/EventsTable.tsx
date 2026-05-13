'use client';
import { useMemo, useState } from 'react';
import type { TradeEvent } from '@/lib/types';

type Action = TradeEvent['action'];
type Filter = Exclude<Action, 'other'>;

const ALL_FILTERS: Filter[] = ['buyYt', 'sellYt', 'addLiq', 'removeLiq', 'claimYield'];

const COLOR: Record<TradeEvent['action'], string> = {
  buyYt:      'text-good',
  sellYt:     'text-bad/80',
  addLiq:     'text-accent-400',
  removeLiq:  'text-accent-400/60',
  claimYield: 'text-accent-300',
  other:      'text-ink-500',
};

const LABEL: Record<Filter, string> = {
  buyYt:      'YT buys',
  sellYt:     'YT sells',
  addLiq:     'LP deposits',
  removeLiq:  'LP withdrawals',
  claimYield: 'Yield claims',
};

type SortKey = 'date' | 'market' | 'action' | 'instr' | 'usd' | 'underlying' | 'rate';

export function EventsTable({ events }: { events: TradeEvent[] }) {
  const [enabled, setEnabled] = useState<Set<Filter>>(new Set(ALL_FILTERS));
  const [sortKey, setSortKey] = useState<SortKey>('date');
  const [asc, setAsc] = useState(true);  // date ascending by default (oldest first)

  const counts = useMemo(() => {
    const c: Record<Filter, number> = { buyYt: 0, sellYt: 0, addLiq: 0, removeLiq: 0, claimYield: 0 };
    for (const e of events) {
      if (e.action in c) c[e.action as Filter]++;
    }
    return c;
  }, [events]);

  const filtered = useMemo(() => {
    const all = enabled.size === ALL_FILTERS.length;
    return all ? events : events.filter(e => enabled.has(e.action as Filter));
  }, [events, enabled]);

  const visible = useMemo(() => {
    const arr = [...filtered];
    const cmp = (a: TradeEvent, b: TradeEvent): number => {
      switch (sortKey) {
        case 'date':       return (a.blockTime || 0) - (b.blockTime || 0);
        case 'market':     return a.market.localeCompare(b.market);
        case 'action':     return a.action.localeCompare(b.action);
        case 'instr':      return (a.instr || '').localeCompare(b.instr || '');
        case 'usd':        return Math.abs(a.usdNet || 0) - Math.abs(b.usdNet || 0);
        case 'underlying': return (a.underlyingDelta || 0) - (b.underlyingDelta || 0);
        case 'rate':       return (a.eusxRate || 0) - (b.eusxRate || 0);
      }
    };
    arr.sort((a, b) => cmp(a, b) * (asc ? 1 : -1));
    return arr;
  }, [filtered, sortKey, asc]);

  function onSort(k: SortKey) {
    if (sortKey === k) setAsc(v => !v);
    else { setSortKey(k); setAsc(k === 'date'); }  // date default asc, others desc
  }
  function arrow(k: SortKey) {
    if (sortKey !== k) return null;
    return <span className="ml-1 text-white/70">{asc ? '↑' : '↓'}</span>;
  }

  function toggle(f: Filter) {
    setEnabled(prev => {
      const next = new Set(prev);
      if (next.has(f)) next.delete(f); else next.add(f);
      return next;
    });
  }

  const allOn = enabled.size === ALL_FILTERS.length;
  const noneOn = enabled.size === 0;

  return (
    <>
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <button
          onClick={() => setEnabled(new Set(allOn ? [] : ALL_FILTERS))}
          className={[
            'text-[12px] px-3 py-1.5 rounded-full border transition',
            allOn
              ? 'border-accent-500/40 bg-accent-500/10 text-ink-50'
              : noneOn
                ? 'border-white/[0.06] bg-ink-900 text-ink-500'
                : 'border-white/[0.06] bg-ink-900 text-ink-300 hover:text-ink-50',
          ].join(' ')}
          title={allOn ? 'Deselect all' : 'Select all'}
        >
          {allOn ? 'All' : noneOn ? 'None' : `${enabled.size} selected`}
          <span className="text-ink-500 tabular-nums ml-1.5">{events.length}</span>
        </button>
        {ALL_FILTERS.map(f => {
          const active = enabled.has(f);
          const n = counts[f];
          return (
            <button
              key={f}
              onClick={() => toggle(f)}
              disabled={n === 0}
              className={[
                'text-[12px] px-3 py-1.5 rounded-full border transition',
                active
                  ? 'border-accent-500/40 bg-accent-500/10 text-ink-50'
                  : 'border-white/[0.06] bg-ink-900 text-ink-400 hover:text-ink-100 hover:border-white/[0.12]',
                n === 0 ? 'opacity-40 cursor-not-allowed' : '',
              ].join(' ')}
            >
              {LABEL[f]} <span className="text-ink-500 tabular-nums ml-1.5">{n}</span>
            </button>
          );
        })}
      </div>

      <section className="surface overflow-x-auto">
        <table className="w-full text-[13px]">
          <thead>
            <tr className="hair-b">
              <th className="th-sortable cell text-left"  onClick={() => onSort('date')}>Date (UTC){arrow('date')}</th>
              <th className="th-sortable cell text-left"  onClick={() => onSort('market')}>Market{arrow('market')}</th>
              <th className="th-sortable cell text-left"  onClick={() => onSort('action')}>Action{arrow('action')}</th>
              <th className="th-sortable cell text-left"  onClick={() => onSort('instr')}>Instruction{arrow('instr')}</th>
              <th className="th-sortable cell text-right" onClick={() => onSort('usd')}>USD{arrow('usd')}</th>
              <th className="th-sortable cell text-right" onClick={() => onSort('underlying')}>Underlying Δ{arrow('underlying')}</th>
              <th className="th-sortable cell text-right" onClick={() => onSort('rate')}>eUSX rate{arrow('rate')}</th>
              <th className="cell text-left text-ink-400 text-[11px] uppercase tracking-wider">Tx</th>
            </tr>
          </thead>
          <tbody>
            {visible.length === 0 && (
              <tr><td className="cell text-ink-500" colSpan={8}>No events match.</td></tr>
            )}
            {visible.map(e => (
              <tr key={e.sig + e.market} className="hair-b row-hover">
                <td className="cell text-ink-300 font-mono text-[12px]">{fmtDate(e.blockTime)}</td>
                <td className="cell text-ink-100">{e.market}</td>
                <td className="cell">
                  <span className={COLOR[e.action] || 'text-ink-500'}>{e.action}</span>
                </td>
                <td className="cell text-ink-400 text-[12px]">{e.instr || '—'}</td>
                <td className="cell text-right tabular-nums text-ink-100">${fmt(Math.abs(e.usdNet || 0))}</td>
                <td className="cell text-right tabular-nums text-ink-300">{e.underlyingDelta.toFixed(4)}</td>
                <td className="cell text-right tabular-nums text-ink-500">{e.eusxRate ? e.eusxRate.toFixed(6) : '—'}</td>
                <td className="cell">
                  <a href={`https://solscan.io/tx/${e.sig}`} target="_blank" rel="noopener noreferrer"
                     className="text-ink-400 hover:text-ink-100 transition">↗</a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}

function fmt(n: number, d = 2) { return n.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }); }
function fmtDate(ts: number) { return new Date(ts * 1000).toISOString().replace('T', ' ').slice(0, 19); }

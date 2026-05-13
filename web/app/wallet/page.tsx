'use client';
import { Suspense, useEffect, useState } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import type { TradeEvent } from '@/lib/types';
import { EventsTable } from '@/components/EventsTable';

function WalletView() {
  const params = useSearchParams();
  const addr = params.get('addr') || '';
  const [events, setEvents] = useState<TradeEvent[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!addr) { setEvents([]); return; }
    setEvents(null); setErr(null);
    fetch(`/events/${addr}.json`)
      .then(async r => {
        if (r.status === 404) return [] as TradeEvent[];
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<TradeEvent[]>;
      })
      .then(setEvents)
      .catch(e => setErr(e.message || String(e)));
  }, [addr]);

  const totals = (events || []).reduce(
    (acc, e) => {
      const v = Math.abs(e.usdNet || 0);
      if (e.action === 'buyYt') acc.buys += v;
      else if (e.action === 'sellYt') acc.sells += v;
      else if (e.action === 'addLiq') acc.lpAdd += v;
      else if (e.action === 'removeLiq') acc.lpRemove += v;
      else if (e.action === 'claimYield') acc.claims += v;
      return acc;
    },
    { buys: 0, sells: 0, lpAdd: 0, lpRemove: 0, claims: 0 }
  );

  return (
    <main className="mx-auto max-w-[1100px] px-6 py-12">
      <Link href="/" className="text-ink-400 hover:text-ink-100 text-[13px] transition">← Back</Link>
      <h1 className="mt-6 display text-3xl">Wallet activity</h1>
      <p className="font-mono text-[12px] text-ink-400 break-all mt-2 flex items-center gap-3 flex-wrap">
        <span>{addr || '(no wallet specified)'}</span>
        {addr && (
          <a
            href={`https://solscan.io/account/${addr}`}
            target="_blank" rel="noopener noreferrer"
            className="text-ink-400 hover:text-ink-100 transition"
          >Solscan ↗</a>
        )}
      </p>

      <section className="mt-8 grid grid-cols-2 md:grid-cols-6 gap-px bg-white/[0.06] border border-white/[0.06] rounded-2xl overflow-hidden">
        <Stat label="YT buys" value={`$${Math.round(totals.buys).toLocaleString()}`} />
        <Stat label="YT sells" value={`$${Math.round(totals.sells).toLocaleString()}`} />
        <Stat label="YT net" value={`$${Math.round(totals.buys - totals.sells).toLocaleString()}`} />
        <Stat label="LP deposits" value={`$${Math.round(totals.lpAdd).toLocaleString()}`} />
        <Stat label="LP withdrawals" value={`$${Math.round(totals.lpRemove).toLocaleString()}`} />
        <Stat label="Yield claimed" value={`$${totals.claims.toFixed(2)}`} />
      </section>

      <div className="mt-10">
        {err ? (
          <div className="surface px-5 py-4 text-bad text-[13px]">Error loading events: {err}</div>
        ) : events === null ? (
          <div className="surface px-5 py-10 text-center text-ink-400 text-[13px]">Loading transactions…</div>
        ) : (
          <EventsTable events={events} />
        )}
      </div>
    </main>
  );
}

export default function WalletPage() {
  return (
    <Suspense fallback={<div className="mx-auto max-w-[1100px] px-6 py-12 text-ink-400">Loading…</div>}>
      <WalletView />
    </Suspense>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-ink-950 px-4 py-4">
      <div className="label">{label}</div>
      <div className="mt-1.5 display text-lg tabular-nums">{value}</div>
    </div>
  );
}

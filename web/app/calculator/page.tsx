import Link from 'next/link';
import { FlaresCalculator } from '@/components/FlaresCalculator';

export const dynamic = 'force-static';

export const metadata = {
  title: 'SLX Allocation Calculator — Solstice',
  description: 'Estimate your SLX airdrop allocation from Season 1 Flares.',
};

const TOTAL_FLARES_S1 = 410_000_000_000;
const SLX_TOTAL_SUPPLY = 1_000_000_000;
const S1_POOL = SLX_TOTAL_SUPPLY * 0.085;

const fmt = (n: number) => Math.round(n).toLocaleString('en-US');

export default function CalculatorPage() {
  return (
    <main className="mx-auto max-w-[1280px] px-6 py-6 sm:py-8">
      <header className="mb-6 flex items-center justify-between">
        <div className="flex items-center gap-4 text-ink-300">
          <Link href="/" className="inline-flex items-center gap-2 hover:text-ink-50 transition">
            <img src="/logos/solstice.svg" alt="" className="h-5 w-5" />
            <span className="text-sm font-medium tracking-tight2">Solstice · SLX allocation calculator</span>
          </Link>
          <span className="text-ink-500">·</span>
          <Link
            href="/"
            className="text-[11px] uppercase tracking-[0.14em] text-ink-500 hover:text-ink-200 transition"
          >
            ← Back to dashboard
          </Link>
        </div>
        <a
          href="https://hanyon.app"
          target="_blank"
          rel="noopener noreferrer"
          className="text-[11px] uppercase tracking-[0.14em] text-ink-500 hover:text-ink-200 transition"
        >
          Made by Hanyon Analytics
        </a>
      </header>

      {/* Context stats — compact single row */}
      <section className="grid grid-cols-3 gap-px bg-white/[0.06] border border-white/[0.06] rounded-xl overflow-hidden mb-6">
        <Stat label="Season 1 SLX pool" value={`${fmt(S1_POOL)} SLX`} sub="8.5% of total supply" />
        <Stat label="Total S1 Flares" value={fmt(TOTAL_FLARES_S1)} sub="across all users" />
        <Stat label="SLX per Flare" value={(S1_POOL / TOTAL_FLARES_S1).toFixed(6)} sub="proportional share" />
      </section>

      <FlaresCalculator />

      <footer className="text-center text-[10px] text-ink-500">
        Rough estimate. Actual distribution may include cohort multipliers, vesting schedules, and penalty hooks.
      </footer>
    </main>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="bg-ink-950 px-5 py-5">
      <div className="label">{label}</div>
      <div className="mt-2 display text-[22px] tabular-nums">{value}</div>
      {sub && <div className="mt-0.5 text-[11px] text-ink-400 tabular-nums">{sub}</div>}
    </div>
  );
}

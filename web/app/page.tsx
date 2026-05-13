import fs from 'node:fs';
import path from 'node:path';
import { WalletTable } from '@/components/WalletTable';
import type { Dataset } from '@/lib/types';

export const dynamic = 'force-static';

// Solstice UI-declared totals per cohort (hardcoded in the registration.solstice.finance JS bundle).
const TOTAL_ELIGIBLE = 11 + 49 + 195 + 646 + 3571 + 1423199; // 1,427,671

// Season 1 SLX pool: 9.16% of supply × 1B = 91.6M SLX.
const SLX_TOTAL_SUPPLY = 1_000_000_000;
const SEASON1_POOL = (0.0316 + 0.0138 + 0.0137 + 0.0138 + 0.0138 + 0.0049) * SLX_TOTAL_SUPPLY;

const fmt = (n: number) => Math.round(n).toLocaleString();
const fmtSlx = (n: number) => `${fmt(n)} SLX`;

async function loadData(): Promise<Dataset> {
  const p = path.join(process.cwd(), 'public', 'data.json');
  return JSON.parse(fs.readFileSync(p, 'utf8')) as Dataset;
}

export default async function HomePage() {
  const data = await loadData();
  const cohorts = Object.entries(data.totals.cohorts) as [string, typeof data.totals.cohorts['1']][];

  // Use the Solstice UI-hardcoded cohort size as the single source of truth for
  // the denominator. Backend sometimes shows slightly more wallets (e.g. Cohort 1
  // has 12 assigned vs UI's 11); those are treated as over-allocation anomalies
  // and excluded from the math so claim rate / per-user / projections stay
  // internally consistent with Solstice's public cohort table.
  const cohortMath = cohorts.map(([c, b]) => {
    const pool = (b.shareOfSlxPct / 100) * SLX_TOTAL_SUPPLY;
    const perUser = pool / b.users;
    const claimedCount = Math.min(b.claimed, b.users);
    const orphanCount = Math.min(b.orphan, Math.max(0, b.users - claimedCount));
    const unpaidCount = Math.max(0, b.users - b.feePayers);
    return {
      c, b, pool, perUser,
      claimedCount, orphanCount, unpaidCount,
      claimed: perUser * claimedCount,
      autoVested: perUser * orphanCount,
      forfeited: perUser * unpaidCount,
    };
  });

  const totalClaimedSlx = cohortMath.reduce((a, x) => a + x.claimed, 0);
  const totalVestedSlx  = cohortMath.reduce((a, x) => a + x.autoVested, 0);
  const totalForfeitSlx = cohortMath.reduce((a, x) => a + x.forfeited, 0);
  const regPct = (100 * data.totals.wallets) / TOTAL_ELIGIBLE;

  return (
    <main className="mx-auto max-w-[1200px] px-6 py-16 sm:py-20">
      {/* Masthead */}
      <header className="mb-10 flex items-center justify-between">
        <div className="flex items-center gap-4 text-ink-300">
          <a href="https://solstice.finance" target="_blank" rel="noopener noreferrer"
             className="inline-flex items-center gap-2 hover:text-ink-50 transition">
            <img src="/logos/solstice.svg" alt="" className="h-5 w-5" />
            <span className="text-sm font-medium tracking-tight2">Solstice</span>
          </a>
          <span className="text-ink-500">·</span>
          <a href="https://www.exponent.finance" target="_blank" rel="noopener noreferrer"
             className="inline-flex items-center hover:text-ink-50 transition">
            <img src="/logos/exponent-alt.svg" alt="Exponent" className="h-4" />
          </a>
        </div>
        <div className="flex items-center gap-4">
          <a href="/calculator"
             className="text-[11px] uppercase tracking-[0.14em] text-accent-300 hover:text-accent-200 transition">
            SLX allocation calculator →
          </a>
          <span className="text-ink-500 text-[11px]">·</span>
          <a href="https://hanyon.app" target="_blank" rel="noopener noreferrer"
             className="text-[11px] uppercase tracking-[0.14em] text-ink-500 hover:text-ink-200 transition">
            Made by Hanyon Analytics
          </a>
        </div>
      </header>

      {/* Top stats — five-tile grid */}
      <section className="grid grid-cols-2 md:grid-cols-5 gap-px bg-white/[0.06] border border-white/[0.06] rounded-2xl overflow-hidden mb-10">
        <Stat
          label="Wallets"
          value={fmt(data.totals.wallets)}
          sub={`${regPct.toFixed(2)}% of ${fmt(TOTAL_ELIGIBLE)} eligible`}
          hint="Unique wallets that sent SOL to the Solstice fee address (DuX1wcoQrJ6XypxLNq3GRrmHFAAMgCqAKbzboabyCtzB). Source = Solana RPC."
        />
        <Stat
          label="Registered"
          value={fmt(data.totals.claimedSlx)}
          sub={`${((100 * data.totals.claimedSlx) / data.totals.wallets).toFixed(1)}% of wallets`}
          accent
          hint="Wallets with a registration tx (txSignature present in Solstice's /api/account). They completed the full registration flow — fee sent + backend record finalized."
        />
        <Stat
          label="Unverified"
          value={fmt(data.totals.orphanFee + data.totals.noCohort)}
          sub={`${((100 * (data.totals.orphanFee + data.totals.noCohort)) / data.totals.wallets).toFixed(2)}% of wallets`}
          hint={`Wallets that paid the fee but we can't strictly confirm their registration against Solstice's backend. ${fmt(data.totals.orphanFee)} orphans (backend has cohort but empty txSignature — UI flow broke between fee transfer and backend write) + ${fmt(data.totals.noCohort)} unfetched (paid the fee too close to the 13:15 UTC deadline; the API shut down before we could query them).`}
        />
        <Stat
          label="SLX distributed"
          value={fmtSlx(totalClaimedSlx + totalVestedSlx)}
          sub={`of ${fmtSlx(SEASON1_POOL)}`}
          hint={`Snapshot projection: ${fmtSlx(totalClaimedSlx)} claimed + ${fmtSlx(totalVestedSlx)} auto-vested.`}
        />
        <Stat
          label="SLX unclaimed"
          value={fmtSlx(totalForfeitSlx)}
          sub={`~${fmt(totalForfeitSlx / (data.totals.wallets || 1))} SLX / wallet`}
          hint={`${fmtSlx(totalForfeitSlx)} comes from ${fmt(TOTAL_ELIGIBLE - data.totals.wallets)} eligible wallets that never paid the fee. If Solstice redistributes this pool equally across all ${fmt(data.totals.wallets)} wallets (registered + unverified), each gets an extra ~${fmt(totalForfeitSlx / (data.totals.wallets || 1))} SLX on top of their cohort allocation. Distribution mechanism isn't explicitly stated in docs — this estimate assumes equal redistribution.`}
        />
      </section>

      {/* Cohort tier list — visually hierarchical */}
      <section className="mb-14">
        <div className="flex items-baseline justify-between mb-4">
          <h2 className="display text-lg">Season 1 cohorts</h2>
          <span className="text-[11px] text-ink-500 tabular-nums">
            Pool {fmtSlx(SEASON1_POOL)} · {fmt(TOTAL_ELIGIBLE)} users across 6 tiers
          </span>
        </div>
        <div className="surface divide-y divide-white/[0.06] overflow-hidden">
          {cohortMath.map(({ c, b, pool, perUser, claimedCount }) => {
            const claimRate = b.users ? (100 * claimedCount) / b.users : 0;
            const slxPctOfTotal = (pool / SEASON1_POOL) * 100;  // tier's share within season 1 pool
            const tier = parseInt(c, 10);
            const intensity = tier <= 2 ? 'high' : tier <= 4 ? 'mid' : 'low';
            const accentCls =
              intensity === 'high' ? 'bg-accent-500/15 text-accent-200 border-accent-500/30' :
              intensity === 'mid'  ? 'bg-accent-500/8 text-accent-300/80 border-accent-500/20' :
                                      'bg-white/[0.03] text-ink-300 border-white/[0.08]';
            const perUserCls =
              intensity === 'high' ? 'text-accent-200' :
              intensity === 'mid'  ? 'text-accent-300/80' :
                                      'text-ink-200';
            return (
              <div key={c} className="px-5 py-4 hover:bg-white/[0.015] transition" title={tooltipCohort(c, b, perUser)}>
                <div className="flex items-center gap-5">
                  {/* Big tier numeral */}
                  <div className={`flex-none w-12 h-12 rounded-xl border flex items-center justify-center display text-xl tabular-nums ${accentCls}`}>
                    {c}
                  </div>

                  {/* Core info */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-baseline justify-between gap-4 flex-wrap">
                      <div className="flex items-baseline gap-2">
                        <span className="text-[13px] text-ink-100 font-medium">{fmt(b.users)} users</span>
                        <span className="text-[11px] text-ink-500">·</span>
                        <span className="text-[11px] text-ink-400 tabular-nums">{b.shareOfSlxPct}% of supply</span>
                        {b.orphan > 0 && (
                          <span className="ml-1 text-[10px] text-accent-400 tabular-nums" title={`${b.orphan} orphan`}>
                            ⚠ {b.orphan} orphan
                          </span>
                        )}
                      </div>
                      <div className="flex items-baseline gap-4 text-[11px] text-ink-400 tabular-nums">
                        <span>Pool <span className="text-ink-200">{fmt(pool)}</span> SLX</span>
                        <span>Est/user <span className={`${perUserCls} font-medium`}>{fmt(perUser)}</span> SLX</span>
                      </div>
                    </div>

                    {/* Claim progress bar */}
                    <div className="mt-2.5 flex items-center gap-3">
                      <div className="flex-1 h-1.5 rounded-full bg-white/[0.05] overflow-hidden">
                        <div
                          className={`h-full rounded-full ${intensity === 'high' ? 'bg-accent-400' : intensity === 'mid' ? 'bg-accent-500/70' : 'bg-ink-300'}`}
                          style={{ width: `${Math.min(100, claimRate)}%` }}
                        />
                      </div>
                      <span className="text-[11px] text-ink-300 tabular-nums min-w-[64px] text-right">
                        {claimRate.toFixed(claimRate < 10 ? 1 : 0)}% registered
                      </span>
                      <span className="text-[11px] text-ink-500 tabular-nums min-w-[110px] text-right">
                        {fmt(claimedCount)}/{fmt(b.users)}
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            );
          })}

          {/* Unfetched cohort — wallets that paid the fee too close to the API shutdown */}
          {data.totals.noCohort > 0 && (
            <div
              className="px-5 py-4 hover:bg-white/[0.015] transition bg-[repeating-linear-gradient(45deg,transparent_0_8px,rgba(255,255,255,0.015)_8px_16px)]"
              title={`${fmt(data.totals.noCohort)} wallets paid the 0.075 SOL registration fee but the /api/account endpoint shut down at the 2026-04-24 13:15 UTC deadline before we could fetch their cohort. These wallets likely ARE registered — we just can't verify or categorize them from our side. Solstice's backend may expose this data again via the claim site when TGE opens.`}
            >
              <div className="flex items-center gap-5">
                <div className="flex-none w-12 h-12 rounded-xl border border-white/[0.08] bg-white/[0.02] flex items-center justify-center display text-xl text-ink-500">
                  ?
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-baseline justify-between gap-4 flex-wrap">
                    <div className="flex items-baseline gap-2">
                      <span className="text-[13px] text-ink-100 font-medium">{fmt(data.totals.noCohort)} wallets</span>
                      <span className="text-[11px] text-ink-500">·</span>
                      <span className="text-[11px] text-ink-400">unfetched cohort</span>
                    </div>
                    <div className="flex items-baseline gap-4 text-[11px] text-ink-400 tabular-nums">
                      <span>Fee paid <span className="text-ink-200">✓</span></span>
                      <span>Cohort <span className="text-ink-500">unknown</span></span>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      </section>

      <WalletTable wallets={data.wallets} />

      <footer className="mt-12 text-center text-[11px] text-ink-500">
        Generated {new Date(data.generatedAt).toUTCString()} · Coverage 2025-10-24 → 2026-04-16 · eUSX repriced at on-chain rate · LP split out separately.
      </footer>
    </main>
  );
}

function Stat({ label, value, sub, hint, accent }: {
  label: string; value: string; sub?: string; hint?: string; accent?: boolean;
}) {
  return (
    <div className="bg-ink-950 px-5 py-5" title={hint}>
      <div className="label flex items-center gap-1">
        {label}
        {hint && <span className="text-ink-500 text-[10px] cursor-help">ⓘ</span>}
      </div>
      <div className={`mt-2 display text-[22px] tabular-nums ${accent ? 'text-accent-300' : ''}`}>
        {value}
      </div>
      {sub && <div className="mt-0.5 text-[11px] text-ink-400 tabular-nums">{sub}</div>}
    </div>
  );
}

function tooltipCohort(
  c: string,
  b: { shareOfSlxPct: number; users: number; feePayers: number; claimed: number; orphan: number },
  perUser: number
) {
  const pool = (b.shareOfSlxPct / 100) * SLX_TOTAL_SUPPLY;
  const regPct = (100 * b.feePayers) / b.users;
  const claimRate = b.users ? (100 * Math.min(b.claimed, b.users)) / b.users : 0;
  const stale = b.feePayers > b.users
    ? `\nNote: Solstice backend has ${b.feePayers} wallets assigned to this cohort (UI says ${b.users}) — the extra are treated as over-allocation and excluded from the math.`
    : '';
  return (
    `Cohort ${c} · ${b.shareOfSlxPct}% of total SLX supply\n` +
    `Pool: ${fmt(pool)} SLX · ${fmt(b.users)} users · ~${fmt(perUser)} SLX/user (estimate — Solstice's actual distribution is non-linear and Flares-weighted within cohort)\n` +
    `Fee-paid: ${fmt(b.feePayers)} (${regPct.toFixed(regPct < 1 ? 2 : 1)}% of cohort)\n` +
    `Registered: ${fmt(Math.min(b.claimed, b.users))} (${claimRate.toFixed(1)}% of cohort)` +
    (b.orphan ? `\nNo claim tx: ${b.orphan} (fee sent, claim never completed)` : '') +
    stale
  );
}

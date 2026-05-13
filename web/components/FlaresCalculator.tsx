'use client';

import { useMemo, useRef, useState } from 'react';

const TOTAL_FLARES_S1 = 410_000_000_000; // 410B
const SLX_TOTAL_SUPPLY = 1_000_000_000;
const S1_POOL_PCT = 0.085; // 8.5%
const S1_POOL = SLX_TOTAL_SUPPLY * S1_POOL_PCT; // 85M SLX

// Price ladder: each row's FDV = price × 1B supply. Stops at $1.00 → $1B FDV.
const PRICE_POINTS = [0.01, 0.05, 0.10, 0.15, 0.25, 0.50, 0.75, 1.00];

// Strip anything that isn't a digit (treats commas/dots/spaces/underscores
// all as thousand separators — correct for integer-only Flares input).
function parseFlares(v: string): number | null {
  if (!v) return null;
  const cleaned = v.replace(/[^\d]/g, '');
  if (!cleaned) return null;
  const n = Number(cleaned);
  if (!Number.isFinite(n) || n < 0) return null;
  return n;
}

// Format an integer-only string with comma thousand separators.
// Forces en-US locale so the separator is ALWAYS a comma — otherwise
// browsers in de-DE / nl-NL / fr-FR etc. use dots and the parser breaks.
function formatWithCommas(digits: string): string {
  if (!digits) return '';
  const clean = digits.replace(/[^\d]/g, '');
  if (!clean) return '';
  const trimmed = clean.replace(/^0+(?=\d)/, '');
  return Number(trimmed).toLocaleString('en-US');
}

// Count digits in string up to given index (inclusive-exclusive)
function countDigits(s: string, end: number): number {
  let n = 0;
  for (let i = 0; i < end && i < s.length; i++) {
    if (s[i] >= '0' && s[i] <= '9') n++;
  }
  return n;
}

// Given formatted string and a digit count, return the string index after that many digits
function indexAfterDigits(s: string, digits: number): number {
  let seen = 0;
  for (let i = 0; i < s.length; i++) {
    if (s[i] >= '0' && s[i] <= '9') {
      seen++;
      if (seen === digits) return i + 1;
    }
  }
  return s.length;
}

const fmtInt = (n: number) => Math.round(n).toLocaleString('en-US');
const fmtSlx = (n: number) =>
  n >= 1 ? n.toLocaleString('en-US', { maximumFractionDigits: 2 })
         : n.toLocaleString('en-US', { maximumFractionDigits: 4 });
const fmtUsd = (n: number) => {
  if (n >= 1000) return `$${Math.round(n).toLocaleString('en-US')}`;
  if (n >= 10) return `$${n.toFixed(2)}`;
  return `$${n.toFixed(4)}`;
};

const fmtFdv = (n: number) => {
  if (n >= 1_000_000_000) return `$${(n / 1_000_000_000).toFixed(n % 1_000_000_000 === 0 ? 0 : 2)}B`;
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(0)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}K`;
  return `$${n.toFixed(0)}`;
};

const fmtPrice = (n: number) => {
  if (n < 1) return `$${n.toFixed(2)}`;
  return `$${n.toFixed(2)}`;
};

export function FlaresCalculator() {
  const [raw, setRaw] = useState('');
  const [customPrice, setCustomPrice] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const flares = useMemo(() => parseFlares(raw), [raw]);

  function handleFlaresChange(e: React.ChangeEvent<HTMLInputElement>) {
    const el = e.target;
    const oldValue = raw;
    const newValue = el.value;
    const caret = el.selectionStart ?? newValue.length;

    // Count digits before caret in the new raw value
    const digitsBeforeCaret = countDigits(newValue, caret);

    const formatted = formatWithCommas(newValue);
    setRaw(formatted);

    // After React commits, put caret at position after same digit count
    requestAnimationFrame(() => {
      if (inputRef.current) {
        const pos = indexAfterDigits(formatted, digitsBeforeCaret);
        inputRef.current.setSelectionRange(pos, pos);
      }
    });
    void oldValue; // avoid lint
  }
  const customPriceN = useMemo(() => {
    const n = Number(customPrice);
    return Number.isFinite(n) && n > 0 ? n : null;
  }, [customPrice]);

  const slx = flares != null ? (flares / TOTAL_FLARES_S1) * S1_POOL : null;
  const sharePct = flares != null ? (flares / TOTAL_FLARES_S1) * 100 : null;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-5 gap-6 mb-10">
      {/* LEFT COLUMN — input + allocation (2/5 on lg) */}
      <div className="lg:col-span-2 surface p-5 flex flex-col">
        <label className="label block mb-2" htmlFor="flares-input">
          Your Season 1 Flares
        </label>
        <input
          id="flares-input"
          ref={inputRef}
          type="text"
          inputMode="numeric"
          autoComplete="off"
          placeholder="e.g. 12,500,000"
          value={raw}
          onChange={handleFlaresChange}
          className="w-full px-4 py-3 bg-ink-950 border border-white/[0.08] rounded-lg text-ink-50 display text-xl tabular-nums
                   placeholder:text-ink-500 focus:outline-none focus:border-accent-500/50 transition"
        />

        {/* SLX allocation hero */}
        <div className="mt-4 flex-1 bg-gradient-to-br from-accent-500/15 via-accent-500/5 to-transparent
                        border border-accent-500/30 rounded-2xl p-6 flex flex-col justify-center min-h-[150px]">
          <div className="label text-accent-300/80">Your SLX allocation</div>
          <div className="mt-2 display text-[40px] sm:text-[48px] leading-none tabular-nums text-accent-200">
            {slx != null ? fmtSlx(slx) : '—'}
          </div>
          <div className="mt-1 text-[12px] text-accent-300/70 tabular-nums">SLX</div>
        </div>

        {/* Share stats — compact two-column */}
        <div className="mt-3 grid grid-cols-2 gap-3">
          <div className="bg-ink-950 border border-white/[0.06] rounded-lg px-4 py-3">
            <div className="label text-[10px]">Share of S1 pool</div>
            <div className="mt-1 display text-[16px] tabular-nums">
              {sharePct != null ? `${sharePct.toFixed(sharePct < 0.01 ? 6 : 4)}%` : '—'}
            </div>
          </div>
          <div className="bg-ink-950 border border-white/[0.06] rounded-lg px-4 py-3">
            <div className="label text-[10px]">Share of total SLX</div>
            <div className="mt-1 display text-[16px] tabular-nums">
              {slx != null ? `${((slx / SLX_TOTAL_SUPPLY) * 100).toFixed(6)}%` : '—'}
            </div>
          </div>
        </div>

        <p className="mt-3 text-[10px] text-ink-500 leading-relaxed">
          Formula: <span className="text-ink-300 tabular-nums">flares / {fmtInt(TOTAL_FLARES_S1)} × {fmtInt(S1_POOL)} SLX</span>.
          Actual distribution may include cohort multipliers, vesting, or penalty hooks.
        </p>
      </div>

      {/* RIGHT COLUMN — moon math (3/5 on lg) */}
      <div className="lg:col-span-3">
        <div className="flex items-baseline justify-between mb-3">
          <h2 className="display text-lg">🌙 Moon math</h2>
          <span className="text-[11px] text-ink-500 tabular-nums">
            USD value at various SLX prices
          </span>
        </div>

        {slx == null ? (
          <div className="surface p-6 text-center text-ink-500 text-[13px]">
            Enter your Flares to see projected USD value across price scenarios.
          </div>
        ) : (
          <div className="surface overflow-hidden">
            <table className="w-full text-[13px] tabular-nums">
              <thead className="text-[10px] uppercase tracking-[0.12em] text-ink-500 border-b border-white/[0.06]">
                <tr>
                  <th className="text-left px-5 py-2.5 font-medium">SLX price</th>
                  <th className="text-right px-5 py-2.5 font-medium">FDV</th>
                  <th className="text-right px-5 py-2.5 font-medium">Your USD</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/[0.04]">
                {PRICE_POINTS.map((price) => {
                  const fdv = price * SLX_TOTAL_SUPPLY;
                  const usd = slx * price;
                  const isDream = price >= 0.5;
                  return (
                    <tr key={price} className="hover:bg-white/[0.02] transition">
                      <td className="px-5 py-2.5 text-ink-100">{fmtPrice(price)}</td>
                      <td className="px-5 py-2.5 text-right text-ink-300">{fmtFdv(fdv)}</td>
                      <td className={`px-5 py-2.5 text-right display text-[15px] ${isDream ? 'text-accent-300' : 'text-ink-100'}`}>
                        {fmtUsd(usd)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>

            {/* Custom price input */}
            <div className="border-t border-white/[0.06] px-5 py-3 flex flex-wrap items-center gap-3">
              <label className="text-[11px] text-ink-500" htmlFor="custom-price">
                Custom price:
              </label>
              <input
                id="custom-price"
                type="number"
                step="0.01"
                min="0"
                placeholder="1.75"
                value={customPrice}
                onChange={(e) => setCustomPrice(e.target.value)}
                className="w-28 px-3 py-1.5 bg-ink-950 border border-white/[0.08] rounded text-ink-50 text-sm tabular-nums
                         placeholder:text-ink-500 focus:outline-none focus:border-accent-500/50 transition"
              />
              <span className="text-[11px] text-ink-500">→</span>
              <span className="display text-[16px] tabular-nums text-accent-300">
                {customPriceN != null ? fmtUsd(slx * customPriceN) : '—'}
              </span>
              {customPriceN != null && (
                <span className="text-[11px] text-ink-500">
                  (FDV {fmtFdv(customPriceN * SLX_TOTAL_SUPPLY)})
                </span>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className="bg-ink-950 px-5 py-5">
      <div className="label">{label}</div>
      <div className={`mt-2 display text-[22px] tabular-nums ${accent ? 'text-accent-300' : ''}`}>
        {value}
      </div>
    </div>
  );
}

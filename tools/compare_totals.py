#!/usr/bin/env python3
"""Compare Mac canonical refresh output against Railway slim refresh output.

Reads two daily_totals.json-style files and verifies that per-quest (or per-partner)
flares agree within a tight drift bound for a chosen day. Also gates on
walker_coverage_* and S1 SLX-held row counts when those fields are present.

Exit code:
    0  PASS
    2  FAIL
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


# ---------- shape detection ----------------------------------------------------

def _load(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)


def _pick_day(doc: dict, day: str | None) -> tuple[str, dict]:
    """Return (date, day_record) for the requested day.

    If `day` is None, picks the last entry in `days`. Falls back to a synthetic
    record built from `partner_totals` if the file has no `days` array.
    """
    days = doc.get("days")
    if isinstance(days, list) and days:
        if day is None:
            rec = days[-1]
            return rec.get("date", "<last>"), rec
        for rec in days:
            if rec.get("date") == day:
                return day, rec
        raise SystemExit(f"day {day!r} not found in input (have {len(days)} days)")
    # fallback: treat partner_totals as a single "day"
    pt = doc.get("partner_totals") or {}
    synthetic = {
        "date": day or "<partner_totals>",
        "by_partner": {k: {"cumulative": v, "inflation": v} for k, v in pt.items()},
    }
    return synthetic["date"], synthetic


def _extract_quests(day_rec: dict) -> dict[str, float]:
    """Flatten a day record into {quest_name: flares}.

    Supports two shapes:
      1. by_partner = {partner: {cumulative, inflation}}  -> partner=cumulative
      2. quests = {QUEST_KEY: flares}                     -> passthrough
    """
    out: dict[str, float] = {}
    by_partner = day_rec.get("by_partner")
    if isinstance(by_partner, dict):
        for partner, blob in by_partner.items():
            if isinstance(blob, dict) and "cumulative" in blob:
                out[partner] = float(blob["cumulative"])
            elif isinstance(blob, (int, float)):
                out[partner] = float(blob)
    quests = day_rec.get("quests")
    if isinstance(quests, dict):
        for q, v in quests.items():
            if isinstance(v, (int, float)):
                out[q] = float(v)
            elif isinstance(v, dict) and "cumulative" in v:
                out[q] = float(v["cumulative"])
    return out


def _walker_coverage(doc: dict, day_rec: dict) -> dict[str, float]:
    """Collect walker_coverage_* fields from doc or day record."""
    found: dict[str, float] = {}
    for src in (doc, day_rec):
        if not isinstance(src, dict):
            continue
        for k, v in src.items():
            if k.startswith("walker_coverage") and isinstance(v, (int, float)):
                found[k] = float(v)
    return found


def _s1_count(doc: dict) -> int | None:
    """Return count of S1 wallets holding non-zero SLX, if present."""
    for key in ("s1_slx_held", "real_users_with_slx"):
        v = doc.get(key)
        if v is None:
            continue
        if isinstance(v, int):
            return v
        if isinstance(v, list):
            # count rows with non-zero SLX if rows are dicts
            n = 0
            for row in v:
                if isinstance(row, dict):
                    slx = row.get("slx") or row.get("slx_held") or row.get("balance") or 0
                    try:
                        if float(slx) > 0:
                            n += 1
                    except (TypeError, ValueError):
                        pass
                else:
                    n += 1
            return n
        if isinstance(v, dict):
            n = 0
            for slx in v.values():
                try:
                    if float(slx) > 0:
                        n += 1
                except (TypeError, ValueError):
                    pass
            return n
    return None


# ---------- comparison core ----------------------------------------------------

def _drift(local: float, remote: float) -> float:
    return abs(remote - local) / max(local, 1.0)


def compare(
    local_doc: dict,
    remote_doc: dict,
    day: str | None,
    drift_tol: float,
    coverage_floor: float,
) -> dict:
    l_date, l_rec = _pick_day(local_doc, day)
    r_date, r_rec = _pick_day(remote_doc, day)

    local_q = _extract_quests(l_rec)
    remote_q = _extract_quests(r_rec)

    all_keys = sorted(set(local_q) | set(remote_q))
    rows = []
    failures: list[str] = []
    for k in all_keys:
        lv = local_q.get(k, 0.0)
        rv = remote_q.get(k, 0.0)
        d = _drift(lv, rv)
        passed = d <= drift_tol
        if k not in local_q:
            passed = False
            failures.append(f"{k}: missing on local")
        elif k not in remote_q:
            passed = False
            failures.append(f"{k}: missing on remote")
        elif not passed:
            failures.append(f"{k}: drift {d*100:.4f}% exceeds {drift_tol*100:.4f}%")
        rows.append({"quest": k, "local": lv, "remote": rv, "drift": d, "pass": passed})

    # walker_coverage gate (remote side only)
    coverage = _walker_coverage(remote_doc, r_rec)
    coverage_rows = []
    for k, v in sorted(coverage.items()):
        ok = v >= coverage_floor
        coverage_rows.append({"key": k, "value": v, "pass": ok})
        if not ok:
            failures.append(f"{k}={v:.4f} < {coverage_floor}")

    # S1 SLX-held row-count gate
    l_s1 = _s1_count(local_doc)
    r_s1 = _s1_count(remote_doc)
    s1_check = None
    if l_s1 is not None or r_s1 is not None:
        ll = l_s1 or 0
        rr = r_s1 or 0
        ratio = (rr / ll) if ll > 0 else 1.0
        ok = ll == 0 or ratio >= 0.95
        s1_check = {"local": l_s1, "remote": r_s1, "ratio": ratio, "pass": ok}
        if not ok:
            failures.append(
                f"S1 SLX-held: remote {rr} is {ratio*100:.2f}% of local {ll} (<95%)"
            )

    return {
        "local_date": l_date,
        "remote_date": r_date,
        "rows": rows,
        "coverage_rows": coverage_rows,
        "s1_check": s1_check,
        "failures": failures,
        "drift_tol": drift_tol,
        "coverage_floor": coverage_floor,
        "pass": not failures,
    }


# ---------- rendering ----------------------------------------------------------

def _fmt_num(x: float) -> str:
    if x == int(x):
        return f"{int(x):,}"
    return f"{x:,.2f}"


def render_console(result: dict) -> str:
    lines: list[str] = []
    banner = "PASS" if result["pass"] else "FAIL"
    bar = "=" * 60
    lines.append(bar)
    lines.append(f"  COMPARE RESULT: {banner}")
    lines.append(f"  local day = {result['local_date']}   remote day = {result['remote_date']}")
    lines.append(f"  drift tol = {result['drift_tol']*100:.4f}%   coverage floor = {result['coverage_floor']}")
    lines.append(bar)

    rows = result["rows"]
    headers = ("quest", "local", "remote", "drift%", "status")
    widths = [
        max(len(headers[0]), *(len(r["quest"]) for r in rows)) if rows else len(headers[0]),
        max(len(headers[1]), *(len(_fmt_num(r["local"])) for r in rows)) if rows else len(headers[1]),
        max(len(headers[2]), *(len(_fmt_num(r["remote"])) for r in rows)) if rows else len(headers[2]),
        max(len(headers[3]), 10),
        max(len(headers[4]), 6),
    ]
    fmt = "  " + " | ".join("{:<" + str(w) + "}" for w in widths)
    lines.append(fmt.format(*headers))
    lines.append("  " + "-+-".join("-" * w for w in widths))
    for r in rows:
        lines.append(
            fmt.format(
                r["quest"],
                _fmt_num(r["local"]),
                _fmt_num(r["remote"]),
                f"{r['drift']*100:.4f}%",
                "PASS" if r["pass"] else "FAIL",
            )
        )

    if result["coverage_rows"]:
        lines.append("")
        lines.append("  walker_coverage gates (remote):")
        for c in result["coverage_rows"]:
            tag = "PASS" if c["pass"] else "FAIL"
            lines.append(f"    {c['key']}: {c['value']:.4f}  [{tag}]")

    if result["s1_check"] is not None:
        s = result["s1_check"]
        tag = "PASS" if s["pass"] else "FAIL"
        lines.append("")
        lines.append(
            f"  S1 SLX-held check: local={s['local']} remote={s['remote']} "
            f"ratio={s['ratio']*100:.2f}%  [{tag}]"
        )

    lines.append("")
    lines.append(bar)
    if result["pass"]:
        lines.append(f"  SUMMARY: PASS ({len(rows)} quests within tolerance)")
    else:
        lines.append(f"  SUMMARY: FAIL ({len(result['failures'])} failure(s))")
        for f in result["failures"]:
            lines.append(f"    - {f}")
    lines.append(bar)
    return "\n".join(lines)


def render_markdown(result: dict) -> str:
    banner = "PASS" if result["pass"] else "FAIL"
    out: list[str] = []
    out.append(f"# Compare Totals: {banner}")
    out.append("")
    out.append(f"- local day: `{result['local_date']}`")
    out.append(f"- remote day: `{result['remote_date']}`")
    out.append(f"- drift tolerance: `{result['drift_tol']*100:.4f}%`")
    out.append(f"- coverage floor: `{result['coverage_floor']}`")
    out.append("")
    out.append("## Per-quest comparison")
    out.append("")
    out.append("| quest | local | remote | drift% | status |")
    out.append("|---|---:|---:|---:|:---:|")
    for r in result["rows"]:
        out.append(
            f"| `{r['quest']}` | {_fmt_num(r['local'])} | {_fmt_num(r['remote'])} | "
            f"{r['drift']*100:.4f}% | {'PASS' if r['pass'] else 'FAIL'} |"
        )
    if result["coverage_rows"]:
        out.append("")
        out.append("## Walker coverage (remote)")
        out.append("")
        out.append("| key | value | status |")
        out.append("|---|---:|:---:|")
        for c in result["coverage_rows"]:
            out.append(f"| `{c['key']}` | {c['value']:.4f} | {'PASS' if c['pass'] else 'FAIL'} |")
    if result["s1_check"] is not None:
        s = result["s1_check"]
        out.append("")
        out.append("## S1 SLX-held")
        out.append("")
        out.append(f"- local: `{s['local']}`  remote: `{s['remote']}`  ratio: `{s['ratio']*100:.2f}%`  status: **{'PASS' if s['pass'] else 'FAIL'}**")
    out.append("")
    out.append("## Summary")
    out.append("")
    if result["pass"]:
        out.append(f"**PASS** — {len(result['rows'])} quests within tolerance.")
    else:
        out.append(f"**FAIL** — {len(result['failures'])} failure(s):")
        for f in result["failures"]:
            out.append(f"- {f}")
    return "\n".join(out) + "\n"


# ---------- CLI ----------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Compare Mac canonical vs Railway slim daily_totals.json with strict accuracy gates.",
    )
    p.add_argument("--local", required=True, help="path to canonical (Mac) daily_totals.json")
    p.add_argument("--remote", required=True, help="path to slim (Railway) daily_totals.json")
    p.add_argument("--day", default=None, help="YYYY-MM-DD to compare (default: last day in local file)")
    p.add_argument("--drift", type=float, default=0.001, help="max per-quest drift (default 0.001 = 0.1%%)")
    p.add_argument("--coverage", type=float, default=0.995, help="min walker_coverage_* floor (default 0.995)")
    p.add_argument("--verbose", action="store_true", help="print all top-level keys from both files")
    p.add_argument("--write-report", default=None, help="path to write Markdown report")
    args = p.parse_args(argv)

    local_doc = _load(args.local)
    remote_doc = _load(args.remote)

    if args.verbose:
        print(f"[verbose] local keys: {sorted(local_doc.keys())}")
        print(f"[verbose] remote keys: {sorted(remote_doc.keys())}")
        if isinstance(local_doc.get("days"), list):
            print(f"[verbose] local days: {len(local_doc['days'])} "
                  f"({local_doc['days'][0].get('date')}..{local_doc['days'][-1].get('date')})")
        if isinstance(remote_doc.get("days"), list):
            print(f"[verbose] remote days: {len(remote_doc['days'])} "
                  f"({remote_doc['days'][0].get('date')}..{remote_doc['days'][-1].get('date')})")

    result = compare(local_doc, remote_doc, args.day, args.drift, args.coverage)
    print(render_console(result))

    if args.write_report:
        Path(args.write_report).write_text(render_markdown(result))

    return 0 if result["pass"] else 2


if __name__ == "__main__":
    sys.exit(main())

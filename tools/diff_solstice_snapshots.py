"""Diff today's Solstice snapshot vs yesterday's, emit markdown report.

Output: data/solstice_snapshots/<YYYY-MM-DD>_diff.md

Diffs:
  - File-by-file unified diff of changed text
  - Flag added/removed lines containing notable terms (TVL, $, milestone, conclude,
    whichever, end date, season, vesting, burn, tokenomics percentages, etc.)
  - Highlight changes in the dashboard's hardcoded strings (e.g., "01.08.26")

Run: python3 tools/diff_solstice_snapshots.py [--days-back 1]

Exits 0 always; the markdown report is the deliverable.
"""
import os, sys, re, difflib, argparse, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS_DIR = ROOT / 'data' / 'solstice_snapshots'

# Terms that, if added or removed, deserve top-of-report visibility
HIGH_SIGNAL_TERMS = [
    r'\$\d{1,4}[Mm]',          # dollar TVL milestones
    r'\$\d{1,3}[Bb]',
    r'milestone', r'whichever', r'conclude', r'TVL reaches',
    r'\d{1,3}\s*[Mm](?:illion)?\s*(?:TVL|burn)',
    r'\bend\s*date\b', r'\bclose[ds]?\b', r'\bclaim\b', r'\bvest',
    r'\b(?:Season\s*[12]|S[12])\s*(?:will|ends?|starts?|conclude)',
    r'\d{1,2}\.\d{1,2}\.\d{2,4}',  # date strings like 01.08.26
    r'\b\d{1,2}\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)',
    r'\b(?:burn|burns|burned)\b',
    r'\b(?:cliff|vesting|unlock)\b',
    r'\b\d{1,3}(?:\.\d)?%\b',   # percentages
    r'\bdistribut',             # distribution / distributions
    r'\b(?:Foundation|Community|Airdrop|Team|Strategic|Public)\b',
    r'\bSLX\b',
    # API-specific keys that, when their value changes, signal config drift
    r'"(?:endTs|startTs|cycleStartDate|cycleEndDate|season1EndTs|season2EndTs)"',
    r'"multiplier"\s*:',
    r'"questCode"\s*:',
    r'"isActive"\s*:',
    r'"campaignSlxAllocation', # Base or per-bucket allocation
    r'"slxTotalSupply"',
    r'"slxBurned"', r'"slxBoughtBack"',
    r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}',  # ISO timestamps
]
HIGH_SIGNAL_RE = re.compile('|'.join(HIGH_SIGNAL_TERMS), re.IGNORECASE)

def list_snapshot_dirs():
    return sorted(d.name for d in SNAPSHOTS_DIR.glob('*') if d.is_dir() and re.match(r'\d{4}-\d{2}-\d{2}', d.name))

def find_previous(today: str, days_back: int = 1) -> str | None:
    dirs = list_snapshot_dirs()
    if today not in dirs:
        # Today's snapshot might not be saved as dir name if we're querying weird
        idx = len(dirs)
    else:
        idx = dirs.index(today)
    target = idx - days_back
    return dirs[target] if target >= 0 else None

def collect_files(snap_dir: Path) -> dict[str, str]:
    """Return {relative_path: content} for all snapshot files."""
    out = {}
    for f in snap_dir.rglob('*'):
        if f.is_file():
            rel = str(f.relative_to(snap_dir))
            try:
                out[rel] = f.read_text()
            except Exception:
                out[rel] = '__BINARY__'
    return out

def diff_files(prev_text: str, today_text: str, path: str) -> list[str]:
    """Unified diff lines."""
    prev_lines = prev_text.splitlines(keepends=False)
    today_lines = today_text.splitlines(keepends=False)
    diff = list(difflib.unified_diff(prev_lines, today_lines, fromfile=f'a/{path}', tofile=f'b/{path}', lineterm='', n=2))
    return diff

def categorize_change(diff_lines: list[str]) -> dict:
    """Inspect diff for high-signal terms."""
    added = [l[1:].strip() for l in diff_lines if l.startswith('+') and not l.startswith('+++')]
    removed = [l[1:].strip() for l in diff_lines if l.startswith('-') and not l.startswith('---')]
    high_added = [l for l in added if HIGH_SIGNAL_RE.search(l)]
    high_removed = [l for l in removed if HIGH_SIGNAL_RE.search(l)]
    return {
        'n_added_lines': len(added),
        'n_removed_lines': len(removed),
        'high_added': high_added,
        'high_removed': high_removed,
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--today', default=datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d'))
    ap.add_argument('--days-back', type=int, default=1)
    args = ap.parse_args()

    today_dir = SNAPSHOTS_DIR / args.today
    if not today_dir.exists():
        print(f"⚠️  No snapshot for {args.today}. Run tools/snapshot_solstice.py first.")
        sys.exit(0)

    prev = find_previous(args.today, args.days_back)
    if not prev:
        print(f"ℹ️  No prior snapshot to diff against (today={args.today}). Writing baseline report.")
        report = f"# Solstice snapshot baseline {args.today}\n\nFirst snapshot — no diff to compute. All files captured.\n\n"
        files = collect_files(today_dir)
        report += f"## Files captured ({len(files)})\n\n"
        for f in sorted(files.keys()):
            report += f"- `{f}` ({len(files[f]):,} chars)\n"
        out_path = SNAPSHOTS_DIR / f"{args.today}_diff.md"
        out_path.write_text(report)
        print(f"→ {out_path}")
        return

    prev_dir = SNAPSHOTS_DIR / prev
    prev_files = collect_files(prev_dir)
    today_files = collect_files(today_dir)

    all_paths = sorted(set(prev_files) | set(today_files))
    report_sections = []
    high_signal_summary = []
    n_changed = 0
    n_added_files = 0
    n_removed_files = 0
    n_unchanged = 0

    for path in all_paths:
        prev_text = prev_files.get(path, '')
        today_text = today_files.get(path, '')
        if path not in prev_files:
            n_added_files += 1
            report_sections.append((path, 'ADDED', f"New file: `{path}` ({len(today_text):,} chars)\n", None))
            # Check for high-signal terms in new content
            high_added = [l.strip() for l in today_text.splitlines() if l.strip() and HIGH_SIGNAL_RE.search(l)]
            if high_added:
                high_signal_summary.append((path, [], high_added[:20]))
            continue
        if path not in today_files:
            n_removed_files += 1
            report_sections.append((path, 'REMOVED', f"Removed file: `{path}`\n", None))
            high_removed = [l.strip() for l in prev_text.splitlines() if l.strip() and HIGH_SIGNAL_RE.search(l)]
            if high_removed:
                high_signal_summary.append((path, high_removed[:20], []))
            continue
        if prev_text == today_text:
            n_unchanged += 1
            continue
        n_changed += 1
        diff = diff_files(prev_text, today_text, path)
        cat = categorize_change(diff)
        # Limit diff body to first 200 lines per file
        diff_body = '\n'.join(diff[:200])
        if len(diff) > 200:
            diff_body += f"\n\n... ({len(diff)-200} more diff lines truncated)"
        report_sections.append((path, 'CHANGED', diff_body, cat))
        if cat['high_added'] or cat['high_removed']:
            high_signal_summary.append((path, cat['high_removed'][:20], cat['high_added'][:20]))

    # Build report
    out_lines = []
    out_lines.append(f"# Solstice daily diff — {args.today} vs {prev}")
    out_lines.append("")
    out_lines.append(f"**Files**: {len(all_paths)} tracked · {n_changed} changed · {n_added_files} added · {n_removed_files} removed · {n_unchanged} unchanged")
    out_lines.append("")

    if high_signal_summary:
        out_lines.append("## ⚠️  High-signal changes")
        out_lines.append("")
        out_lines.append("Lines containing TVL/milestone/burn/conclude/date/percentage/vest terms.")
        out_lines.append("")
        for path, removed, added in high_signal_summary:
            out_lines.append(f"### `{path}`")
            if removed:
                out_lines.append(f"**Removed ({len(removed)}):**")
                for l in removed[:15]:
                    out_lines.append(f"  - `{l[:240]}`")
            if added:
                out_lines.append(f"**Added ({len(added)}):**")
                for l in added[:15]:
                    out_lines.append(f"  + `{l[:240]}`")
            out_lines.append("")
    else:
        out_lines.append("## ✓ No high-signal changes")
        out_lines.append("")

    if n_changed or n_added_files or n_removed_files:
        out_lines.append("## Full diffs")
        out_lines.append("")
        for path, status, body, cat in report_sections:
            out_lines.append(f"### `{path}` — {status}")
            if cat:
                out_lines.append(f"(+{cat['n_added_lines']} / -{cat['n_removed_lines']} lines)")
            out_lines.append("")
            out_lines.append("```diff")
            out_lines.append(body)
            out_lines.append("```")
            out_lines.append("")
    out = '\n'.join(out_lines)
    out_path = SNAPSHOTS_DIR / f"{args.today}_diff.md"
    out_path.write_text(out)
    print(f"→ {out_path}  ({n_changed} changed, {n_added_files} added, {n_removed_files} removed)")
    if high_signal_summary:
        print(f"  ⚠️  {len(high_signal_summary)} files with high-signal changes")

if __name__ == '__main__':
    main()

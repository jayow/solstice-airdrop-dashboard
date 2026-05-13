"""Scrape Solstice registration (/api/account) for a set of wallet addresses.

Input:  data/solstice_registration/addresses.txt  (one address per line)
Output: data/solstice_registration/accounts.jsonl (one JSON record per line)
Resumable: skips addresses already present in the output file.
"""
import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

ROOT = os.path.join(os.path.dirname(__file__), "..")
IN_PATH  = os.path.join(ROOT, "data/solstice_registration/addresses.txt")
OUT_PATH = os.path.join(ROOT, "data/solstice_registration/accounts.jsonl")

URL = "https://registration.solstice.finance/api/account"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
CONCURRENCY = 4
TIMEOUT = 20
RETRIES = 8
MAX_RPS = 4.0  # global rate cap across all threads

session = requests.Session()
session.headers.update(HEADERS)
write_lock = threading.Lock()
progress_lock = threading.Lock()
rate_lock = threading.Lock()
_next_slot = [0.0]
counters = {"ok": 0, "err": 0, "reg": 0}


def rate_limit():
    with rate_lock:
        now = time.time()
        wait = _next_slot[0] - now
        if wait > 0:
            time.sleep(wait)
            now += wait
        _next_slot[0] = now + 1.0 / MAX_RPS


def fetch(address: str):
    last_err = "no attempt"
    for attempt in range(RETRIES):
        rate_limit()
        try:
            r = session.get(URL, params={"address": address}, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            last_err = f"HTTP {r.status_code}"
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else (2 ** attempt)
                time.sleep(min(wait, 60))
                continue
            if 500 <= r.status_code < 600:
                time.sleep(2 ** attempt)
                continue
            # other 4xx -> don't retry
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(2 ** attempt)
    return {"walletAddress": address, "_error": last_err}


def main():
    with open(IN_PATH) as f:
        addrs = [line.strip() for line in f if line.strip()]
    done = set()
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH) as f:
            for line in f:
                try:
                    d = json.loads(line)
                    a = d.get("walletAddress")
                    if a and "_error" not in d:
                        done.add(a)
                except Exception:
                    pass
    todo = [a for a in addrs if a not in done]
    print(f"Total addresses: {len(addrs)}  already done: {len(done)}  remaining: {len(todo)}", flush=True)
    if not todo:
        return

    t0 = time.time()
    out = open(OUT_PATH, "a")
    try:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            futs = {ex.submit(fetch, a): a for a in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                data = fut.result()
                with write_lock:
                    out.write(json.dumps(data, separators=(",", ":")) + "\n")
                    if i % 250 == 0:
                        out.flush()
                with progress_lock:
                    if "_error" in data:
                        counters["err"] += 1
                    else:
                        counters["ok"] += 1
                        if data.get("cohort") not in (None, "", "0"):
                            counters["reg"] += 1
                if i % 500 == 0 or i == len(todo):
                    dt = time.time() - t0
                    rate = i / dt if dt else 0
                    eta = (len(todo) - i) / rate if rate else 0
                    print(f"  {i:>6}/{len(todo)}  ok={counters['ok']} err={counters['err']} registered={counters['reg']}  "
                          f"{rate:.1f}/s  eta={eta:.0f}s", flush=True)
    finally:
        out.close()

    dt = time.time() - t0
    print(f"Done in {dt:.0f}s.  ok={counters['ok']} err={counters['err']} registered={counters['reg']}", flush=True)


if __name__ == "__main__":
    main()

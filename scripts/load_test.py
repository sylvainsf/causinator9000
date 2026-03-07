#!/usr/bin/env python3
"""
Causinator 9000 Stress Test Suite

Four tests that probe the real performance boundaries:

  1. Fan-out: 1 upstream mutation → diagnose 100 downstream pods
  2. Concurrent clients: N threads hammering /diagnosis in parallel
  3. Large active window: 10k mutations + 10k signals, then diagnose
  4. Sustained flood: continuous injection + continuous diagnosis

Usage:
  python3 scripts/load_test.py              # run all tests
  python3 scripts/load_test.py --test fan   # run one test

Prerequisites:
  pip install requests
  Engine running with 26k topology loaded
"""

import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

ENGINE = os.environ.get("C9K_ENGINE_URL", "http://localhost:8080")
SESSION = requests.Session()  # connection pooling

# ── ANSI ──────────────────────────────────────────────────────────────────
B = "\033[1m"; D = "\033[2m"; R = "\033[0m"
GR = "\033[32m"; RD = "\033[31m"; YL = "\033[33m"; CY = "\033[36m"
MG = "\033[35m"; WH = "\033[97m"; BG = "\033[44m"; BGG = "\033[42m"; BGR = "\033[41m"

def banner(t):
    w = max(len(t)+6, 66)
    print(f"\n{BG}{WH}{B}{'━'*w}\n   {t}{' '*(w-len(t)-3)}\n{'━'*w}{R}\n")

def head(t):  print(f"  {CY}{B}▸ {t}{R}")
def info(t):  print(f"    {D}{t}{R}")
def kv(k, v, c=WH): print(f"    {D}{k}:{R} {c}{B}{v}{R}")
def good(t):  print(f"    {GR}✓{R} {t}")
def bad(t):   print(f"    {RD}✗{R} {t}")
def warn(t):  print(f"    {YL}⚠{R} {t}")

def ms_str(ms):
    if ms < 10:    return f"{GR}{B}{ms:.2f} ms{R}"
    elif ms < 100: return f"{GR}{B}{ms:.1f} ms{R}"
    elif ms < 500: return f"{YL}{B}{ms:.0f} ms{R}"
    else:          return f"{RD}{B}{ms:.0f} ms{R}"

def clear():
    SESSION.post(f"{ENGINE}/clear")

def inject_mut(node, mtype, ts=None):
    body = {"node_id": node, "mutation_type": mtype}
    if ts: body["timestamp"] = ts
    return SESSION.post(f"{ENGINE}/mutations", json=body)

def inject_sig(node, stype, val=1.0, sev="critical"):
    return SESSION.post(f"{ENGINE}/signals", json=body_sig(node, stype, val, sev))

def body_sig(node, stype, val=1.0, sev="critical"):
    return {"node_id": node, "signal_type": stype, "value": val, "severity": sev}

def diag(node):
    t = time.perf_counter()
    r = SESSION.get(f"{ENGINE}/diagnosis", params={"target": node})
    return r.json(), (time.perf_counter() - t) * 1000

def health():
    return SESSION.get(f"{ENGINE}/health").json()

def lat_report(lats, label=""):
    lats.sort()
    n = len(lats)
    if n == 0:
        return
    p50 = lats[n // 2]
    p95 = lats[int(n * 0.95)]
    p99 = lats[int(n * 0.99)]
    avg = statistics.mean(lats)
    mx = max(lats)
    print()
    if label:
        head(f"Results: {label}")
    kv("count", f"{n} queries")
    kv("avg", ms_str(avg))
    kv("p50", ms_str(p50))
    kv("p95", ms_str(p95))
    kv("p99", ms_str(p99))
    kv("max", ms_str(mx))
    return p95

# ── Test 1: Fan-out ───────────────────────────────────────────────────────

def test_fanout():
    banner("Test 1 ─ Fan-out: 1 Upstream Mutation → 100 Downstream Diagnoses")
    info("A single SecretRotation on kv-eastus-01 (KeyVault).")
    info("Then diagnose 100 downstream pods that depend on it.")
    info("KeyVault → Pod is a direct dependency edge — the solver must")
    info("walk the ancestor chain and match the KV's CPT to the pod's signals.")
    print()

    clear()

    head("Inject: 1 upstream mutation on shared KeyVault")
    inject_mut("kv-eastus-01", "SecretRotation")
    good("SecretRotation on kv-eastus-01")

    # Inject 403 errors on 100 pods (first 2 pods per app, 50 apps)
    head("Inject: AccessDenied_403 on 100 downstream pods")
    for app in range(50):
        for pod in range(2):
            node = f"pod-eastus-app{app:03d}-{pod:02d}"
            inject_sig(node, "AccessDenied_403")
    good("100 AccessDenied_403 signals injected")

    head("Diagnose all 100 pods")
    lats = []
    root_causes = {}
    for app in range(50):
        for pod in range(2):
            node = f"pod-eastus-app{app:03d}-{pod:02d}"
            d, ms = diag(node)
            lats.append(ms)
            rc = d.get("root_cause", "none")
            root_causes[rc] = root_causes.get(rc, 0) + 1

    p95 = lat_report(lats, "Fan-out (100 diagnoses)")

    print()
    head("Root cause distribution")
    for rc, count in sorted(root_causes.items(), key=lambda x: -x[1])[:5]:
        kv(rc, f"{count} pods")

    # Verdict
    print()
    if p95 < 50:
        good(f"p95 = {p95:.1f} ms — fan-out handled efficiently")
    elif p95 < 100:
        warn(f"p95 = {p95:.1f} ms — acceptable but shows ancestor-walk cost")
    else:
        bad(f"p95 = {p95:.1f} ms — ancestor walk is expensive at scale")

    return p95

# ── Test 2: Concurrent Clients ───────────────────────────────────────────

def test_concurrent():
    banner("Test 2 ─ Concurrent Clients: N Threads × M Queries")
    info("Tests Mutex<SolverState> lock contention under parallel load.")
    info("8 threads, each making 50 sequential diagnosis queries.")
    print()

    clear()

    # Seed some evidence first
    head("Seeding evidence: 20 mutations + 20 signals")
    for i in range(20):
        region = ["eastus", "westeurope", "japaneast", "westus2"][i % 4]
        app = i % 100
        node = f"pod-{region}-app{app:03d}-01"
        inject_mut(node, "ImageUpdate")
        inject_sig(node, "CrashLoopBackOff")
    good("20 events seeded")

    thread_count = 8
    queries_per_thread = 50

    head(f"Running {thread_count} threads × {queries_per_thread} queries")

    all_lats = []
    thread_stats = []

    def worker(thread_id):
        """Each thread makes sequential diagnosis queries."""
        s = requests.Session()
        lats = []
        for i in range(queries_per_thread):
            region = ["eastus", "westeurope", "japaneast", "westus2"][i % 4]
            app = (thread_id * 7 + i) % 100
            node = f"pod-{region}-app{app:03d}-01"
            t = time.perf_counter()
            r = s.get(f"{ENGINE}/diagnosis", params={"target": node})
            lats.append((time.perf_counter() - t) * 1000)
        return lats

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=thread_count) as pool:
        futures = {pool.submit(worker, tid): tid for tid in range(thread_count)}
        for future in as_completed(futures):
            tid = futures[future]
            lats = future.result()
            all_lats.extend(lats)
            thread_stats.append((tid, statistics.mean(lats), max(lats)))
    total_s = time.perf_counter() - t0

    total_queries = thread_count * queries_per_thread
    qps = total_queries / total_s

    p95 = lat_report(all_lats, f"Concurrent ({thread_count} threads)")
    print()
    kv("total queries", f"{total_queries}")
    kv("wall time", f"{total_s:.2f} s")
    kv("throughput", f"{qps:.0f} queries/sec")

    print()
    head("Per-thread breakdown")
    for tid, avg, mx in sorted(thread_stats):
        info(f"  thread {tid}: avg={avg:.1f}ms  max={mx:.1f}ms")

    print()
    if qps > 500:
        good(f"{qps:.0f} qps — lock contention is negligible")
    elif qps > 100:
        warn(f"{qps:.0f} qps — some lock contention visible")
    else:
        bad(f"{qps:.0f} qps — heavy lock contention, consider RwLock or sharding")

    return p95

# ── Test 3: Large Active Window ──────────────────────────────────────────

def test_large_window():
    banner("Test 3 ─ Large Active Window: 10k Mutations + 10k Signals")
    info("Fills the solver's active window with 10,000 mutations and")
    info("10,000 signals, then measures diagnosis latency.")
    info("Tests: active-set scan cost + memory pressure.")
    print()

    clear()

    head("Injecting 10,000 mutations")
    t0 = time.perf_counter()
    for i in range(10000):
        region = ["eastus", "westeurope", "japaneast", "westus2", "centralus",
                  "northeurope", "southeastasia", "australiaeast", "japaneast", "eastus2"][i % 10]
        app = i % 100
        pod = i % 4
        node = f"pod-{region}-app{app:03d}-{pod:02d}"
        SESSION.post(f"{ENGINE}/mutations", json={"node_id": node, "mutation_type": "ImageUpdate"})
        if (i + 1) % 2000 == 0:
            info(f"  {i+1}/10000 mutations...")
    mut_s = time.perf_counter() - t0
    kv("mutation injection", f"{mut_s:.1f}s ({10000/mut_s:.0f}/s)")

    head("Injecting 10,000 signals")
    t0 = time.perf_counter()
    for i in range(10000):
        region = ["eastus", "westeurope", "japaneast", "westus2", "centralus",
                  "northeurope", "southeastasia", "australiaeast", "japaneast", "eastus2"][i % 10]
        app = i % 100
        pod = i % 4
        node = f"pod-{region}-app{app:03d}-{pod:02d}"
        SESSION.post(f"{ENGINE}/signals", json={"node_id": node, "signal_type": "CrashLoopBackOff", "value": 1.0})
        if (i + 1) % 2000 == 0:
            info(f"  {i+1}/10000 signals...")
    sig_s = time.perf_counter() - t0
    kv("signal injection", f"{sig_s:.1f}s ({10000/sig_s:.0f}/s)")

    h = health()
    kv("active window", f"{h['active_mutations']} mutations, {h['active_signals']} signals")

    head("Diagnosing 50 nodes with 20k active events")
    lats = []
    for i in range(50):
        region = ["eastus", "westeurope", "japaneast"][i % 3]
        app = i % 100
        node = f"pod-{region}-app{app:03d}-01"
        _, ms = diag(node)
        lats.append(ms)

    p95 = lat_report(lats, "Large window (20k active events)")

    print()
    if p95 < 50:
        good(f"p95 = {p95:.1f} ms — 20k active events handled efficiently")
    elif p95 < 100:
        warn(f"p95 = {p95:.1f} ms — active-set scanning starting to show")
    else:
        bad(f"p95 = {p95:.1f} ms — active-set needs indexing (HashMap by node_id)")

    return p95

# ── Test 4: Sustained Flood ──────────────────────────────────────────────

def test_flood():
    banner("Test 4 ─ Sustained Flood: Inject + Diagnose Simultaneously")
    info("One thread continuously injects events.")
    info("Another thread continuously queries diagnoses.")
    info("Measures whether injection degrades diagnosis or vice versa.")
    info("Duration: 10 seconds.")
    print()

    clear()

    # Seed some initial evidence
    for i in range(50):
        region = ["eastus", "westeurope"][i % 2]
        node = f"pod-{region}-app{i:03d}-01"
        inject_mut(node, "ImageUpdate")
        inject_sig(node, "CrashLoopBackOff")

    duration = 10.0
    inject_count = [0]
    diag_lats = []
    diag_count = [0]
    stop = [False]

    def injector():
        s = requests.Session()
        i = 0
        while not stop[0]:
            region = ["eastus", "westeurope", "japaneast", "westus2"][i % 4]
            app = i % 100
            pod = i % 4
            node = f"pod-{region}-app{app:03d}-{pod:02d}"
            s.post(f"{ENGINE}/mutations", json={"node_id": node, "mutation_type": "ConfigChange"})
            s.post(f"{ENGINE}/signals", json={"node_id": node, "signal_type": "error_rate", "value": 0.8})
            inject_count[0] += 2
            i += 1

    def diagnoser():
        s = requests.Session()
        i = 0
        while not stop[0]:
            region = ["eastus", "westeurope", "japaneast"][i % 3]
            app = i % 100
            node = f"pod-{region}-app{app:03d}-01"
            t = time.perf_counter()
            s.get(f"{ENGINE}/diagnosis", params={"target": node})
            diag_lats.append((time.perf_counter() - t) * 1000)
            diag_count[0] += 1
            i += 1

    head(f"Running inject + diagnose for {duration:.0f}s")
    t0 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_inj = pool.submit(injector)
        f_diag = pool.submit(diagnoser)
        time.sleep(duration)
        stop[0] = True
        f_inj.result()
        f_diag.result()

    elapsed = time.perf_counter() - t0

    print()
    kv("duration", f"{elapsed:.1f}s")
    kv("events injected", f"{inject_count[0]} ({inject_count[0]/elapsed:.0f}/s)")
    kv("diagnoses completed", f"{diag_count[0]} ({diag_count[0]/elapsed:.0f}/s)")

    p95 = lat_report(diag_lats, "Sustained flood (diagnosis under injection)")

    h = health()
    kv("final active window", f"{h['active_mutations']} muts, {h['active_signals']} sigs")

    print()
    if p95 < 50:
        good(f"p95 = {p95:.1f} ms — concurrent inject + diagnose works well")
    elif p95 < 100:
        warn(f"p95 = {p95:.1f} ms — some contention between inject and diagnose")
    else:
        bad(f"p95 = {p95:.1f} ms — significant contention under sustained load")

    return p95

# ── Summary ───────────────────────────────────────────────────────────────

def summary(results):
    banner("Stress Test Summary")

    h = health()
    kv("Graph", f"{h['nodes']:,} nodes, {h['edges']:,} edges")
    print()

    all_pass = True
    for name, p95, threshold in results:
        if p95 is None:
            info(f"  {name}: skipped")
            continue
        color = GR if p95 < threshold else YL if p95 < threshold * 2 else RD
        status = f"{BGG}{WH}{B} PASS {R}" if p95 < threshold else f"{BGR}{WH}{B} FAIL {R}"
        print(f"    {name}: {color}{B}p95 = {p95:.1f} ms{R}  (target < {threshold} ms) {status}")
        if p95 >= threshold:
            all_pass = False

    print()
    if all_pass:
        good("All stress tests passed")
    else:
        warn("Some tests exceeded targets — see details above")

# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Causinator 9000 Stress Test Suite")
    parser.add_argument("--test", choices=["fan", "concurrent", "window", "flood", "all"],
                        default="all", help="Which test to run")
    args = parser.parse_args()

    # Preflight
    h = health()
    if not h:
        print(f"Engine not responding at {ENGINE}")
        sys.exit(1)
    print(f"{GR}✓{R} Engine: {h['nodes']:,} nodes, {h['edges']:,} edges")

    results = []
    tests = {
        "fan": ("Fan-out (1→100)", test_fanout, 50),
        "concurrent": ("Concurrent (8 threads)", test_concurrent, 50),
        "window": ("Large window (20k events)", test_large_window, 100),
        "flood": ("Sustained flood (10s)", test_flood, 100),
    }

    if args.test == "all":
        for key in ["fan", "concurrent", "window", "flood"]:
            name, fn, threshold = tests[key]
            p95 = fn()
            results.append((name, p95, threshold))
        summary(results)
    else:
        name, fn, threshold = tests[args.test]
        p95 = fn()
        summary([(name, p95, threshold)])

if __name__ == "__main__":
    main()

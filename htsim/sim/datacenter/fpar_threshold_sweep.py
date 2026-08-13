#!/usr/bin/env python3
"""FPAR queue-size threshold (-threshold) sweep.

FPAR picks, at each hop, between a set of candidate next-hops by comparing
their queue occupancy against a threshold T (dragonfly_plus_switch.cpp's
fully_progressive_adaptive_route): below T a queue is "good enough, take
it"; above T, FPAR escalates to the next priority tier and keeps looking.
T defaults to half the queue capacity (DragonflyPlusTopology::get_t(),
dragonfly_plus_topology.cpp:56-58) and is already exposed via -threshold,
but has never been tuned in this investigation despite FPAR being one of
the two strategies compared throughout.

-threshold is in BYTES, same units as queue capacity (-q is in *packets*
and gets converted internally via memFromPkt). This script computes the
actual byte queue size itself (packets * mtu) so the swept fractions are
correct regardless of what -q/-mtu you run with.

Usage:
    python fpar_threshold_sweep.py            # full grid, plots + CSV
    python3 fpar_threshold_sweep.py --dry-run   # just print the job count
    python3 fpar_threshold_sweep.py --workers 8

Edit NODES/Q_PKTS/MTU/SEEDS/THRESHOLD_FRACTIONS/FLOWSIZES/loads below to
match your own setup -- these mirror what the rest of this investigation
used (1100 nodes, size=m, q=50 packets, 3 seeds).
"""
import argparse
import csv
import re
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import mean

DC = Path("/home/chiara/Desktop/tesi_project/uet-htsim/htsim/sim/datacenter")
OUT = Path(__file__).parent / "fpar_tuning_sweep"
OUT.mkdir(parents=True, exist_ok=True)
BINARY = DC / "htsim_uec_dfp_reps"
CM_DIR = DC / "reps_perm_and_incast" / "connection_matrices"

FCT_RE = re.compile(r"finished at ([\d.]+)")
SUM_RE = re.compile(r"New: (\d+) Rtx: (\d+) RTS: (\d+) Bounced: (\d+) ACKs: (\d+) NACKs: (\d+)")

NODES = 1100
Q_PKTS = 50
MTU = 4150  # main_uec_df.cpp default packet_size; pass -mtu to match if you override it
QUEUESIZE_BYTES = Q_PKTS * MTU
END = "100000"
SEEDS = [1, 2, 3]

TORNADO_LOADS = [0.25, 0.50, 0.75, 1.00]
PERM_LOADS = [0.60, 0.70, 0.80, 0.90]
FLOWSIZES = [100_000, 500_000, 2_000_000, 10_000_000]

# Fraction of queue capacity used as the FPAR threshold T. 0.5 is the
# built-in default (DragonflyPlusTopology::get_t()).
THRESHOLD_FRACTIONS = [0.1, 0.25, 0.5, 0.75, 0.9]


def cm_path(pattern, load, fs, seed):
    return CM_DIR / f"{pattern}_load{load:.2f}_n{NODES}_fs{fs}_seed{seed}.cm"


def run_one(tag, cmd):
    log = OUT / f"{tag}.log"
    if not log.exists() or "Done" not in log.read_text(errors="ignore"):
        try:
            with open(log, "w") as fh:
                subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=DC, timeout=1200)
        except subprocess.TimeoutExpired:
            print(tag, "TIMEOUT", flush=True)
            return None
    t = log.read_text(errors="ignore")
    v = sorted(float(x) for x in FCT_RE.findall(t))
    m = SUM_RE.search(t)
    if not v or not m:
        print(tag, "NODATA", flush=True)
        return None
    new, rtx, rts, bounced, acks, nacks = (int(x) for x in m.groups())
    print(tag, "ok", flush=True)
    return dict(mean=sum(v) / len(v), p99=v[int(len(v) * .99)] if len(v) >= 100 else v[-1],
                rtx_pct=100.0 * rtx / new if new else 0.0)


def build_jobs():
    jobs = []
    points = [("tornado", ld, fs) for ld in TORNADO_LOADS for fs in FLOWSIZES] + \
             [("permutation", ld, fs) for ld in PERM_LOADS for fs in FLOWSIZES]
    for pattern, load, fs in points:
        for frac in THRESHOLD_FRACTIONS:
            thresh_bytes = int(QUEUESIZE_BYTES * frac)
            for seed in SEEDS:
                tag = f"fpar_th{frac:.2f}_{pattern}_l{load:.2f}_fs{fs}_s{seed}"
                cmd = [str(BINARY), "-load_balancing_algo", "oblivious", "-size", "m", "-nodes", str(NODES),
                       "-strat", "fpar", "-q", str(Q_PKTS), "-end", END, "-seed", str(seed),
                       "-o", str(OUT / f"{tag}.dat"), "-threshold", str(thresh_bytes)]
                if pattern == "tornado":
                    cmd += ["-tornado", "-tornado_conns", str(max(1, round(NODES * load))), "-tornado_flowsize", str(fs)]
                else:
                    cmd += ["-tm", str(cm_path(pattern, load, fs, seed))]
                jobs.append((frac, pattern, load, fs, seed, tag, cmd))
    return jobs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    jobs = build_jobs()
    print(len(jobs), "runs", flush=True)
    if args.dry_run:
        return

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        rows = list(ex.map(lambda j: (j, run_one(j[5], j[6])), jobs))

    with open(OUT / "fpar_threshold_sweep.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["threshold_frac", "threshold_bytes", "pattern", "load", "fs", "seed", "mean", "p99", "rtx_pct"])
        for (frac, pattern, load, fs, seed, tag, cmd), r in rows:
            if r:
                w.writerow([frac, int(QUEUESIZE_BYTES * frac), pattern, load, fs, seed,
                            r["mean"], r["p99"], r["rtx_pct"]])

    grouped = defaultdict(list)
    for (frac, pattern, load, fs, seed, tag, cmd), r in rows:
        if r:
            grouped[(frac, pattern, load, fs)].append(r)

    points = sorted(set((p, l, f) for (fr, p, l, f) in grouped.keys()))
    baseline_frac = 0.5

    print(f"\n{'pattern':12} {'load':6} {'fs':>10}  " + "  ".join(f"frac={f:.2f}" for f in THRESHOLD_FRACTIONS))
    for pattern, load, fs in points:
        base = grouped.get((baseline_frac, pattern, load, fs))
        if not base:
            continue
        base_mean = mean(r["mean"] for r in base)
        line = f"{pattern:12} {load:<6.2f} {fs:>10}  "
        for frac in THRESHOLD_FRACTIONS:
            rs = grouped.get((frac, pattern, load, fs))
            if not rs:
                line += f"{'--':>10}  "
                continue
            m_ = mean(r["mean"] for r in rs)
            d = 100 * (m_ - base_mean) / base_mean
            line += f"{d:+9.2f}%  "
        print(line)

    print("\n=== aggregate mean FCT delta vs frac=0.50 (default), across all points ===")
    for frac in THRESHOLD_FRACTIONS:
        deltas = []
        for pattern, load, fs in points:
            base = grouped.get((baseline_frac, pattern, load, fs))
            rs = grouped.get((frac, pattern, load, fs))
            if base and rs:
                base_mean = mean(r["mean"] for r in base)
                deltas.append(100 * (mean(r["mean"] for r in rs) - base_mean) / base_mean)
        if deltas:
            print(f"frac={frac:.2f}  avg_mean_delta={mean(deltas):+.2f}%  n={len(deltas)}")

    make_plots(grouped, points)
    print("DONE")


def make_plots(grouped, points):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available, skipping plots")
        return

    patterns = sorted(set(p for p, l, f in points))
    for pattern in patterns:
        loads = sorted(set(l for p, l, f in points if p == pattern))
        flowsizes = sorted(set(f for p, l, f in points if p == pattern))
        fig, axes = plt.subplots(len(flowsizes), 2, figsize=(11, 3.2 * len(flowsizes)), squeeze=False)
        for row, fs in enumerate(flowsizes):
            for col, metric in enumerate(["mean", "p99"]):
                ax = axes[row][col]
                for load in loads:
                    xs, ys = [], []
                    for frac in THRESHOLD_FRACTIONS:
                        rs = grouped.get((frac, pattern, load, fs))
                        if rs:
                            xs.append(frac)
                            ys.append(mean(r[metric] for r in rs))
                    if xs:
                        ax.plot(xs, ys, marker="o", label=f"load={load:.2f}")
                ax.axvline(0.5, color="gray", linestyle="--", linewidth=0.8, label="default" if row == 0 and col == 0 else None)
                ax.set_title(f"{pattern} fs={fs} - FCT {metric}")
                ax.set_xlabel("threshold fraction of queue capacity")
                ax.set_ylabel(f"FCT {metric} (us)")
                if row == 0 and col == 0:
                    ax.legend(fontsize=7)
        fig.tight_layout()
        outpath = OUT / f"plot_{pattern}.png"
        fig.savefig(outpath, dpi=130)
        plt.close(fig)
        print(f"wrote {outpath}")


if __name__ == "__main__":
    main()

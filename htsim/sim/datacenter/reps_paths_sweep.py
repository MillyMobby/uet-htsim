#!/usr/bin/env python3
"""Sweep -paths (path_entropy_size, default 64 -- main_uec_df.cpp:95): the
size of REPS's entropy/path space, i.e. how many distinct EVs it spreads
packets over. Never touched anywhere in this investigation; every REPS run
so far silently used the default 64. With -reps_partition_entropy on, half
of this space goes to the minimal tier, half to the open tier, so a small
value could starve one tier's granularity; a large value increases
collision-avoidance headroom but must stay >= physical path diversity to
matter, and must be a power of 2 (UecMpReps::_no_of_paths comment).

Two independent strategy runs, same representative 8-point subset (all
loads, 2MB flows) used by the buffer_size/exit_freeze sweep, for direct
comparability:
    python3 reps_paths_sweep.py --strategy no_partition
    python3 reps_paths_sweep.py --strategy partition
    python reps_paths_sweep.py --strategy both      # default
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
OUT = Path(__file__).parent / "reps_tuning_sweep"
OUT.mkdir(parents=True, exist_ok=True)
BINARY = DC / "htsim_uec_dfp_reps"
CM_DIR = DC / "reps_perm_and_incast" / "connection_matrices"

FCT_RE = re.compile(r"finished at ([\d.]+)")
SUM_RE = re.compile(r"New: (\d+) Rtx: (\d+) RTS: (\d+) Bounced: (\d+) ACKs: (\d+) NACKs: (\d+)")

NODES = 1100
Q_PKTS = 50
END = "100000"
SEEDS = [1, 2, 3]

TORNADO_LOADS = [0.25, 0.50, 0.75, 1.00]
PERM_LOADS = [0.60, 0.70, 0.80, 0.90]
FLOWSIZE = 2_000_000

PATH_SIZES = [8, 16, 32, 64, 128, 256]  # 64 is the built-in default; all powers of 2

PARTITION_EXTRA = ["-reps_partition_entropy", "-reps_escalate_threshold", "0.2",
                    "-reps_warmup_explore_rtts", "1.0", "-reps_explore_prob", "40"]


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


def sweep(strategy, points, workers):
    prefix = "part_" if strategy == "partition" else ""
    strategy_extra = PARTITION_EXTRA if strategy == "partition" else []
    jobs = []
    for pattern, load, fs in points:
        for paths in PATH_SIZES:
            for seed in SEEDS:
                tag = f"{prefix}paths_{paths}_{pattern}_l{load:.2f}_fs{fs}_s{seed}"
                cmd = [str(BINARY), "-load_balancing_algo", "reps", "-size", "m", "-nodes", str(NODES),
                       "-strat", "reps_dfp", "-q", str(Q_PKTS), "-end", END, "-seed", str(seed),
                       "-o", str(OUT / f"{tag}.dat"), "-paths", str(paths)] + strategy_extra
                if pattern == "tornado":
                    cmd += ["-tornado", "-tornado_conns", str(max(1, round(NODES * load))), "-tornado_flowsize", str(fs)]
                else:
                    cmd += ["-tm", str(cm_path(pattern, load, fs, seed))]
                jobs.append((paths, pattern, load, fs, seed, tag, cmd))

    print(f"{prefix}paths: {len(jobs)} runs", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(lambda j: (j, run_one(j[5], j[6])), jobs))

    with open(OUT / f"{prefix}paths.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["paths", "pattern", "load", "fs", "seed", "mean", "p99", "rtx_pct"])
        for (paths, pattern, load, fs, seed, tag, cmd), r in rows:
            if r:
                w.writerow([paths, pattern, load, fs, seed, r["mean"], r["p99"], r["rtx_pct"]])

    grouped = defaultdict(list)
    for (paths, pattern, load, fs, seed, tag, cmd), r in rows:
        if r:
            grouped[(paths, pattern, load, fs)].append(r)

    baseline = 64
    print(f"\n{'pattern':12} {'load':6} {'fs':>10}  " + "  ".join(f"paths={p}" for p in PATH_SIZES))
    for pattern, load, fs in points:
        base = grouped.get((baseline, pattern, load, fs))
        if not base:
            continue
        base_mean = mean(r["mean"] for r in base)
        line = f"{pattern:12} {load:<6.2f} {fs:>10}  "
        for paths in PATH_SIZES:
            rs = grouped.get((paths, pattern, load, fs))
            if not rs:
                line += f"{'--':>9}  "
                continue
            d = 100 * (mean(r["mean"] for r in rs) - base_mean) / base_mean
            line += f"{d:+8.2f}%  "
        print(line)

    print(f"\n=== {prefix}paths: aggregate mean FCT delta vs paths=64 ===")
    for paths in PATH_SIZES:
        deltas = []
        for pattern, load, fs in points:
            base = grouped.get((baseline, pattern, load, fs))
            rs = grouped.get((paths, pattern, load, fs))
            if base and rs:
                base_mean = mean(r["mean"] for r in base)
                deltas.append(100 * (mean(r["mean"] for r in rs) - base_mean) / base_mean)
        if deltas:
            print(f"paths={paths}  avg_mean_delta={mean(deltas):+.2f}%  n={len(deltas)}")

    make_plot(prefix, grouped, points)


def make_plot(prefix, grouped, points):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available, skipping plot")
        return
    patterns = sorted(set(p for p, l, f in points))
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    for col, metric in enumerate(["mean", "p99"]):
        ax = axes[col]
        for pattern in patterns:
            loads = sorted(set(l for p, l, f in points if p == pattern))
            for load in loads:
                fs = [f for p, l, f in points if p == pattern and l == load][0]
                xs, ys = [], []
                for paths in PATH_SIZES:
                    rs = grouped.get((paths, pattern, load, fs))
                    if rs:
                        xs.append(paths)
                        ys.append(mean(r[metric] for r in rs))
                if xs:
                    ax.plot(xs, ys, marker="o", label=f"{pattern[:4]} l={load:.2f}", alpha=0.8)
        ax.axvline(64, color="gray", linestyle="--", linewidth=0.8)
        ax.set_xscale("log", base=2)
        ax.set_title(f"REPS {prefix}paths - FCT {metric}")
        ax.set_xlabel("entropy space size (-paths)")
        ax.set_ylabel(f"FCT {metric} (us)")
        if col == 0:
            ax.legend(fontsize=6, ncol=2)
    fig.tight_layout()
    outpath = OUT / f"plot_{prefix}paths.png"
    fig.savefig(outpath, dpi=130)
    plt.close(fig)
    print(f"wrote {outpath}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=["no_partition", "partition", "both"], default="both")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    points = [("tornado", ld, FLOWSIZE) for ld in TORNADO_LOADS] + \
             [("permutation", ld, FLOWSIZE) for ld in PERM_LOADS]

    total = len(PATH_SIZES) * len(points) * len(SEEDS)
    total *= 2 if args.strategy == "both" else 1
    print(total, "total runs", flush=True)
    if args.dry_run:
        return

    if args.strategy in ("no_partition", "both"):
        sweep("no_partition", points, args.workers)
    if args.strategy in ("partition", "both"):
        sweep("partition", points, args.workers)
    print("DONE")


if __name__ == "__main__":
    main()

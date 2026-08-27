#!/usr/bin/env python3
"""

WHAT THE PLOT SHOWS

For each forward hop count (path length, in switches), the %
where the delay-based congestion signal fired WITHOUT an ECN mark also being
present ("delay-only"). This is a natural quantity to plot for two reasons:



Usage:
    python thesis_hop_rtt_plot.py                       
"""
from __future__ import annotations

import argparse, math, os, statistics, subprocess, sys
from pathlib import Path
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

HOP_CLASSES = [1, 3, 4, 5, 7]
MIN_SAMPLES = 50   # a hop class with fewer samples than this is dropped
ARMS = [
    ("off", "Hop normalization OFF", "-disable_hop_rtt_normalization"),
    ("on",  "Hop normalization ON",  ""),
]
COLOURS = {"off": "#eb6834", "on": "#2a78d6"}
INK, INK_MUTED, GRID = "#0b0b0b", "#52514e", "#d9d8d4"


def ensure_cm(cmdir: Path, nodes: int, flowsize: int, seed: int) -> Path:
    f = cmdir / f"perm_n{nodes}_fs{flowsize}_s{seed}.cm"
    if not f.exists():
        subprocess.run(["python3", "connection_matrices/gen_permutation.py", str(f),
                        str(nodes), str(nodes), str(flowsize), "0.0", str(seed)],
                       check=True, capture_output=True)
    return f


def run_one(binary: str, nodes: int, end: int, extra: str, cm: Path, dat: Path, seed: int):
    cmd = [binary, "-tm", str(cm), "-nodes", str(nodes), "-strat", "reps_dfp",
           "-load_balancing_algo", "reps", "-end", str(end), "-seed", str(seed),
           "-debug_hops", "-o", str(dat)]
    if extra:
        cmd.append(extra)

    tot = {h: 0 for h in HOP_CLASSES}
    delay_only = {h: 0 for h in HOP_CLASSES}
    ecn_any = 0
    proc = subprocess.Popen(cmd, cwd=os.path.dirname(os.path.abspath(binary)) or ".",
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1 << 20)
    for line in proc.stdout:
        if not line.startswith("HOPDBG"):
            continue
        kv = dict(p.split("=", 1) for p in line.split()[1:] if "=" in p)
        try:
            h = int(kv.get("hops", -1))
        except ValueError:
            continue
        if h not in tot:
            continue
        tot[h] += 1
        if kv.get("ecn") == "1":
            ecn_any += 1
        if kv.get("delay") == "1" and kv.get("ecn") == "0":
            delay_only[h] += 1
    proc.wait()
    dat.unlink(missing_ok=True)

    total = sum(tot.values())
    return {
        "total": total,
        "ecn_rate": 100.0 * ecn_any / total if total else float("nan"),
        "by_hop": {h: (100.0 * delay_only[h] / tot[h] if tot[h] >= MIN_SAMPLES else None)
                  for h in HOP_CLASSES},
    }


def mean_ci95(xs):
    xs = [x for x in xs if x is not None and not math.isnan(x)]
    if len(xs) < 2:
        return (xs[0] if xs else float("nan")), 0.0
    return statistics.mean(xs), 1.96 * statistics.stdev(xs) / math.sqrt(len(xs))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--binary", default="./htsim_uec_dfp_fin")
    ap.add_argument("--nodes", type=int, default=1100)
    ap.add_argument("--flowsize", type=int, default=2_000_000)
    ap.add_argument("--end", type=int, default=200, help="sim end time, us")
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--outdir", default="results/thesis_hopnorm")
    a = ap.parse_args()

    if not os.access(a.binary, os.X_OK):
        print(f"error: {a.binary} not executable. Build it and run from sim/datacenter/.",
              file=sys.stderr)
        return 1

    outdir = Path(a.outdir); outdir.mkdir(parents=True, exist_ok=True)
    cmdir = outdir / "connection_matrices"; cmdir.mkdir(exist_ok=True)

    results = {arm: [] for arm, _, _ in ARMS}
    for seed in range(1, a.seeds + 1):
        cm = ensure_cm(cmdir, a.nodes, a.flowsize, seed)
        for arm, label, extra in ARMS:
            print(f"seed {seed}  {label:18s} ...", end=" ", flush=True)
            r = run_one(a.binary, a.nodes, a.end, extra, cm, outdir / "_s.dat", seed)
            results[arm].append(r)
            print(f"{r['total']} samples, ecn_rate={r['ecn_rate']:.1f}%")

    hops = [h for h in HOP_CLASSES
            if any(r["by_hop"][h] is not None for arm in results for r in results[arm])]

    print("\nDelay-only rate by path length (%; = extra detections beyond ECN):")
    print("off = -disable_hop_rtt_normalization")
    print("on  = default for -strat reps_dfp\n")
    print(f"{'hops':>5}  " + "  ".join(f"{label:>18s}" for _, label, _ in ARMS))
    table = {arm: {} for arm, _, _ in ARMS}
    for h in hops:
        row = [f"{h:>5}"]
        for arm, label, _ in ARMS:
            xs = [r["by_hop"][h] for r in results[arm]]
            m, c = mean_ci95(xs)
            table[arm][h] = (m, c)
            row.append(f"{m:8.2f} +- {c:4.2f}  ")
        print("  ".join(row))

    # ---- the paragraph, ready to quote or paraphrase ----
    off_vals = [table["off"][h][0] for h in hops]
    on_vals = [table["on"][h][0] for h in hops]
    off_spread = max(off_vals) - min(off_vals)
    on_spread = max(on_vals) - min(on_vals)
    on_mean = statistics.mean(on_vals)
    ecn_mean = statistics.mean(r["ecn_rate"] for arm in results for r in results[arm])


    plt.rcParams.update({
        "font.size": 10, "axes.labelsize": 10, "axes.titlesize": 11,
        "legend.fontsize": 9, "xtick.labelsize": 9, "ytick.labelsize": 9,
        "axes.edgecolor": GRID, "axes.labelcolor": INK, "text.color": INK,
        "xtick.color": INK_MUTED, "ytick.color": INK_MUTED,
        "figure.facecolor": "white", "axes.facecolor": "white",
        "axes.spines.top": False, "axes.spines.right": False, "pdf.fonttype": 42})
#PLOT
    fig, ax = plt.subplots(figsize=(5.2, 3.4))
    w = 0.34
    top = max(m + c for arm in table for m, c in table[arm].values())
    pad = top * 0.06
    for i, (arm, label, _) in enumerate(ARMS):
        xs = [x + (i - 0.5) * w for x in range(len(hops))]
        vals = [table[arm][h][0] for h in hops]
        errs = [table[arm][h][1] for h in hops]
        ax.bar(xs, vals, w, label=label, color=COLOURS[arm], zorder=3,
               linewidth=0.8, edgecolor="white")
        ax.errorbar(xs, vals, yerr=errs, fmt="none", ecolor=INK_MUTED,
                    elinewidth=0.9, capsize=3, zorder=4)
        for x, v, e in zip(xs, vals, errs):  
            ax.text(x, v + e + pad * 0.4, f"{v:.1f}", ha="center", va="bottom",
                    fontsize=8, color=INK_MUTED)
    ax.set_ylim(0, top + pad * 5)
    ax.set_xticks(range(len(hops)))
    ax.set_xticklabels([str(h) for h in hops])
    ax.set_xlabel("Path length (switches traversed)")
    ax.set_ylabel("Delay-only detection rate (%)")
    ax.set_title("",
                 loc="left", pad=8)
    ax.grid(axis="y", color=GRID, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="upper left")
    fig.tight_layout()
    for ext in ("pdf", "png"):
        fig.savefig(outdir / f"fig_thesis.{ext}", dpi=300, bbox_inches="tight")
    plt.close(fig)
    (outdir / "_s.dat").unlink(missing_ok=True)
    print(f"\nfigure written to {outdir}/fig_thesis.{{png}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

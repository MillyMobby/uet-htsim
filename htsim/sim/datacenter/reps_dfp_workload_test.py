#!/usr/bin/env python3
"""REPS/oblivious + hop-count-RTT-normalization sweep for UEC over Dragonfly+.

Generates any missing connection_matrices/load_sweep/load<frac>_n<N>_seed<S>.cm
files (full-bisection permutation traffic, reproducibly seeded), runs the
simulator across a matrix of configurations, then writes a CSV, a text report,
and PNG plots (mean FCT / p99 FCT / retransmit rate vs. load fraction).

RUNS below covers two questions, chosen based on prior investigation:

  1. "Does REPS beat oblivious, and does hop-count RTT normalization help REPS,
     under real sustained congestion?"
     -> 512 nodes, Dragonfly+ size=m, strat=fpar. This topology/scale showed
        real congestion (RTS events, high retransmit rates) in prior testing,
        unlike larger/less-oversubscribed configurations.

  2. "Does the hop-count-normalization mechanism specifically require adaptive
     (fpar) routing, i.e. does it stop mattering under minimal routing where
     path length never varies?"
     -> 1024 nodes, Dragonfly+ size=l, both strat=fpar and strat=minimal.
        Cheap to run (fast at this scale) and directly tests the mechanism's
        premise: minimal routing has a fixed hop count for every packet, so
        normalization should have ~no effect there if the mechanism is doing
        what it's supposed to.

Requires a build of htsim_uec_dfp that includes:
  - the dragonfly_plus_switch.cpp fix so DragonflyPlusSwitch only increments
    hop_count for UECDATA packets (not Ack/Nack/Rts/Pull returning to the
    source) -- otherwise expected_rtt()/update_base_rtt() see a corrupted
    hop_count and this whole comparison is measuring the bug, not the feature.
  - the -disable_hop_rtt_normalization / -force_enable_hop_rtt_normalization
    CLI flags in main_uec_df.cpp, used to force normalization off/on
    independent of the routing strategy's default.
If your local htsim_uec_dfp doesn't recognize those flags, rebuild from the
updated sources first.

Plotting requires matplotlib (pip install matplotlib). If it's missing, the
sweep still runs and writes the CSV/report; only plotting is skipped.
"""
import csv
import re
import subprocess
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------- CONFIG ---
BINARY = "./htsim_uec_dfp"
OUTDIR = Path("reps_dfp_results/results_plots")
CM_DIR = Path("connection_matrices/results_plots_sweep")
GENERATOR = Path("connection_matrices/gen_permutation_full_bisection.py")

END_TIME_US = 100000
Q = 50
RUN_TIMEOUT_S = 600

FLOWSIZE = 2_000_000     # bytes
EXTRA_START = 0.0

LOAD_FRACTIONS = [0.60, 0.65, 0.75, 0.85, 0.90]
SEEDS = list(range(1, 6))   # bump this up if your machine has time to spare;
                             # the generator is now properly seeded so higher
                             # seed counts give real (not illusory) averaging.

# label, nodes, size, strat, algo, hop_rtt_normalization
RUNS = [
    # -- (1) REPS vs oblivious, and REPS norm on/off, under real congestion --
    ("reps_norm",   512, "m", "fpar", "reps",      True),
    ("reps_nonorm", 512, "m", "fpar", "reps",      False),
    ("oblivious",   512, "m", "fpar", "oblivious", False),

    # -- (2) Does hop-norm matter only under fpar, not under minimal? --
    #("reps_fpar_norm",      1024, "l", "fpar",    "reps", True),
    #("reps_fpar_nonorm",    1024, "l", "fpar",    "reps", False),
    #("reps_minimal_norm",   1024, "l", "minimal", "reps", True),
    #("reps_minimal_nonorm", 1024, "l", "minimal", "reps", False),*/
]

FCT_RE = re.compile(r"finished at ([\d.]+)")
SUMMARY_RE = re.compile(r"New: (\d+) Rtx: (\d+) RTS: (\d+) Bounced: \d+ ACKs: \d+ NACKs: (\d+)")


# --------------------------------------------------------------- HELPERS ---
def ensure_cm_files(nodes_needed):
    CM_DIR.mkdir(parents=True, exist_ok=True)
    for nodes in nodes_needed:
        for frac in LOAD_FRACTIONS:
            conns = max(1, round(nodes * frac))
            for seed in SEEDS:
                fname = CM_DIR / f"load{frac:.2f}_n{nodes}_seed{seed}.cm"
                if fname.exists():
                    continue
                cmd = ["python3", str(GENERATOR), str(fname), str(nodes), str(conns),
                       str(FLOWSIZE), str(EXTRA_START), str(seed)]
                print(" ".join(cmd))
                subprocess.run(cmd, check=True)


def run_one(label, nodes, size, strat, algo, norm_on, frac, seed):
    cmfile = CM_DIR / f"load{frac:.2f}_n{nodes}_seed{seed}.cm"
    tag = f"{label}_n{nodes}_load{frac:.2f}_seed{seed}"
    logpath = OUTDIR / f"{tag}.log"
    datpath = OUTDIR / f"{tag}.dat"
    cmd = [
        str(BINARY), "-tm", str(cmfile), "-load_balancing_algo", algo,
        "-size", size, "-nodes", str(nodes), "-strat", strat,
        "-q", str(Q), "-end", str(END_TIME_US), "-seed", str(seed),
        "-o", str(datpath),
    ]
    cmd.append("-force_enable_hop_rtt_normalization" if norm_on else "-disable_hop_rtt_normalization")
    with open(logpath, "w") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=RUN_TIMEOUT_S)
    return logpath, result.returncode


def parse_log(logpath):
    text = logpath.read_text()
    if "Done" not in text:
        return None
    fcts = sorted(float(x) for x in FCT_RE.findall(text))
    m = SUMMARY_RE.search(text)
    if not m or not fcts:
        return None
    new, rtx, rts, nacks = (int(x) for x in m.groups())
    n = len(fcts)
    return {
        "n_flows": n,
        "fct_mean": sum(fcts) / n,
        "fct_p50": fcts[n // 2],
        "fct_p99": fcts[int(n * 0.99)] if n >= 100 else fcts[-1],
        "fct_max": fcts[-1],
        "new": new,
        "rtx": rtx,
        "rts": rts,
        "nacks": nacks,
    }


# ------------------------------------------------------------------ MAIN ---
def main():
    nodes_needed = sorted({r[1] for r in RUNS})
    ensure_cm_files(nodes_needed)
    OUTDIR.mkdir(parents=True, exist_ok=True)

    rows = []
    total = len(RUNS) * len(LOAD_FRACTIONS) * len(SEEDS)
    done = 0
    for label, nodes, size, strat, algo, norm_on in RUNS:
        for frac in LOAD_FRACTIONS:
            for seed in SEEDS:
                done += 1
                logpath, rc = run_one(label, nodes, size, strat, algo, norm_on, frac, seed)
                parsed = parse_log(logpath)
                print(f"[{done}/{total}] {logpath.stem}: {'OK' if parsed else 'FAILED'}")
                if parsed:
                    rows.append({
                        "config": label, "nodes": nodes, "size": size, "strat": strat,
                        "algo": algo, "norm": norm_on, "load_fraction": frac, "seed": seed,
                        **parsed,
                    })

    csv_path = OUTDIR / "summary.csv"
    with open(csv_path, "w", newline="") as f:
        fieldnames = ["config", "nodes", "size", "strat", "algo", "norm", "load_fraction", "seed",
                      "n_flows", "fct_mean", "fct_p50", "fct_p99", "fct_max", "new", "rtx", "rts", "nacks"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} rows to {csv_path}")

    print_report(rows)
    try:
        make_plots(rows)
    except ImportError:
        print("\nmatplotlib not installed (pip install matplotlib) -- skipped plots, "
              f"CSV/report are still in {OUTDIR}/")


def print_report(rows):
    groups = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = (r["nodes"], r["size"], r["strat"], r["load_fraction"])
        groups[key][r["config"]].append(r)

    print("\n" + "=" * 90)
    print("SUMMARY (mean across seeds)")
    print("=" * 90)
    for key in sorted(groups):
        nodes, size, strat, frac = key
        print(f"\n== nodes={nodes} size={size} strat={strat} load_fraction={frac:.2f} ==")
        for config, runs in groups[key].items():
            n = len(runs)
            fct_mean = sum(r["fct_mean"] for r in runs) / n
            fct_p99 = sum(r["fct_p99"] for r in runs) / n
            rtx_rate = 100 * sum(r["rtx"] for r in runs) / sum(r["new"] for r in runs)
            per_seed = [round(r["fct_mean"], 1) for r in sorted(runs, key=lambda x: x["seed"])]
            print(f"  {config:20s} fct_mean={fct_mean:9.2f}  fct_p99={fct_p99:9.2f}  "
                  f"rtx_rate={rtx_rate:5.2f}%  n_seeds={n:2d}  per_seed={per_seed}")


def make_plots(rows):
    import matplotlib.pyplot as plt

    # Fixed categorical color order (palette slots 1/2/3): don't reassign per chart.
    COLORS = {
        "reps_norm": "#2a78d6", "reps_nonorm": "#008300", "oblivious": "#e87ba4",
        "reps_fpar_norm": "#2a78d6", "reps_fpar_nonorm": "#008300",
        "reps_minimal_norm": "#eda100", "reps_minimal_nonorm": "#eb6834",
    }

    groups = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for r in rows:
        key = (r["nodes"], r["size"], r["strat"])
        groups[key][r["config"]][r["load_fraction"]].append(r["fct_mean"])

    for (nodes, size, strat), configs in groups.items():
        fig, ax = plt.subplots(figsize=(7.5, 5), dpi=150)
        for config, by_load in configs.items():
            loads = sorted(by_load)
            means = [sum(by_load[l]) / len(by_load[l]) for l in loads]
            ax.plot(loads, means, marker="o", markersize=5, linewidth=2,
                    color=COLORS.get(config, "#888888"), label=config)
        ax.set_xlabel("Load fraction")
        ax.set_ylabel("Mean FCT (µs)")
        ax.set_title(f"Mean FCT vs. load\n{nodes} nodes, Dragonfly+ size={size}, strat={strat}", fontsize=11)
        ax.grid(True, linewidth=0.5, alpha=0.4)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.legend(frameon=False, loc="upper left", fontsize=9)
        fig.tight_layout()
        out_path = OUTDIR / f"plot_n{nodes}_{size}_{strat}_fct_mean.png"
        fig.savefig(out_path)
        plt.close(fig)
        print(f"wrote {out_path}")


if __name__ == "__main__":
    main()

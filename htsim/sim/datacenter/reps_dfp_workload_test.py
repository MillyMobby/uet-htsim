#!/usr/bin/env python3
"""Sweep comparing REPS_DFP vs FPAR routing on Dragonfly+ with UEC.

REPS_DFP is the static routing strategy (-strat reps_dfp) that exposes both
minimal and non-minimal paths as a single ECMP set, selected by REPS entropy
(-load_balancing_algo reps). 

    python3 sweep_reps_dfp_vs_fpar.py            # generate CMs, run sweep, plot
    python3 sweep_reps_dfp_vs_fpar.py --dry-run  # just print the planned runs

Requires a build of htsim_uec_dfp 
"""
import argparse
import csv
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

# ============================================================================
# CONFIGURATION 
# ============================================================================

BINARY = "./htsim_uec_dfp"

# ---- Topology -------------------------------------------------------------
TOPO_SIZE = "m"            # 's' | 'm' | 'l'  
NODES = 1100                # number of hosts

# Optional explicit Dragonfly+ radix parameters. L
DF_S = 0                    # spine switches per group
DF_L = 0                    # leaf switches connected to one spine
DF_H = 0                    # global links per spine switch
DF_P = 0                    # hosts per leaf switch
NO_PARALLEL_LINK = 1         # only meaningful when TOPO_SIZE == 's'

# ---- Simulation parameters -------------------------------------------------
QUEUE_SIZE_PKTS = 50
MTU_BYTES = None            # None -> binary default; else passed via -mtu
END_TIME_US = 100000
HOP_LATENCY_US = None       # None -> binary default; else passed via -hop_latency
SWITCH_LATENCY_US = None    # None -> binary default; else passed via -switch_latency

# ---- Strategies to compare: (label, -strat value, -load_balancing_algo value) --
STRATEGIES = [
    # label,      -strat,     -load_balancing_algo
    ("fpar",      "fpar",     "oblivious"),
    ("reps_dfp",  "reps_dfp", "reps"),
]
# Fixed plot color per strategy label, in the order above. Do not resort by
# value -- the same label always gets the same color across every plot.
STRATEGY_COLORS = {
    "fpar": "#9300B8",
    "reps_dfp": "#00A2D3",
}

# ---- Traffic patterns to sweep ---------------------------------------------
# "file" patterns pre-generate a connection matrix via a Python script
# (relative to connection_matrices/) and pass it via -tm; extra_args are
# appended after the standard (filename, nodes, conns, flowsize,
# extrastarttime, seed) positional args.

# Each pattern carries its own `loads` list (fractions of NODES that
# actively send) instead of sharing one global list. "tornado" pairs every
# host with one in a maximally-distant Dragonfly+ group, guaranteeing every
# flow crosses a global link -- the actual worst case for load balancing on
# this topology
TRAFFIC_PATTERNS = {
    #"permutation": {"kind": "file", "script": "gen_permutation_full_bisection.py",
                    # "extra_args": [], "loads": [0.60, 0.70, 0.80, 0.90]},
    #"incast":      {"kind": "file", "script": "gen_incast.py", "extra_args": ["0"],
    #                 "loads": [0.60, 0.70, 0.80, 0.90]},  # prefer_remote=0
     "tornado":     {"kind": "native", "loads": [0.25, 0.50, 0.75, 1.00], "extra_start": 0.0},
}

SEEDS = [1, 2, 3, 4 ,5]


# This is the main axis worth varying for "tornado" (whose `loads` is fixed at [1.0]) 
FLOWSIZES = [    
    #20_000,      
    #100_000,     
    500_000,     
    2_000_000,   
    10_000_000, ]
EXTRA_START_US = 0.0

RUN_TIMEOUT_S = 800

OUTDIR = Path("sweep_reps_dfp_vs_fpar")


CM_DIR = OUTDIR / "connection_matrices"
RUN_DIR = OUTDIR / "runs"
PLOT_DIR = OUTDIR / "PLOTS"
GEN_ROOT = Path("connection_matrices")

FCT_RE = re.compile(r"finished at ([\d.]+)")
SUMMARY_RE = re.compile(
    r"New: (\d+) Rtx: (\d+) RTS: (\d+) Bounced: (\d+) ACKs: (\d+) "
    r"NACKs: (\d+) Pulls: (\d+) sleek_pkts: (\d+)"
)

METRICS = [
    # (csv_field, plot_title, y_label)
    ("fct_mean", "Mean FCT", "Mean FCT (us)"),
    ("fct_p99", "p99 FCT", "p99 FCT (us)"),
    ("fct_max", "Completion time (max FCT)", "Time (us)"),
    ("rtx_rate_pct", "Retransmit rate", "Rtx / New (%)"),
    ("rts_rate_pct", "RTS rate", "RTS / New (%)"),
    ("nack_rate_pct", "NACK rate", "NACKs / New (%)"),
    ("bounced_rate_pct", "Bounced rate", "Bounced / New (%)"),
]



def cm_path(pattern, nodes, frac, flowsize, seed):
    return CM_DIR / f"{pattern}_load{frac:.2f}_n{nodes}_fs{flowsize}_seed{seed}.cm"


def ensure_cm_file(pattern, nodes, frac, flowsize, seed):
    fname = cm_path(pattern, nodes, frac, flowsize, seed)
    if fname.exists():
        return fname
    spec = TRAFFIC_PATTERNS[pattern]
    generator = GEN_ROOT / spec["script"]
    conns = max(1, round(nodes * frac))
    cmd = ["python3", str(generator), str(fname), str(nodes), str(conns),
           str(flowsize), str(EXTRA_START_US), str(seed), *spec["extra_args"]]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    return fname


def ensure_all_cm_files():
    CM_DIR.mkdir(parents=True, exist_ok=True)
    for pattern, spec in TRAFFIC_PATTERNS.items():
        if spec["kind"] != "file":
            continue  # native patterns (tornado) are generated by the binary 
        for frac in spec["loads"]:
            for flowsize in FLOWSIZES:
                for seed in SEEDS:
                    ensure_cm_file(pattern, NODES, frac, flowsize, seed)


def build_cmd(pattern, frac, flowsize, seed, strat, lb_algo, datpath):
    spec = TRAFFIC_PATTERNS[pattern]
    cmd = [
        BINARY,
        "-load_balancing_algo", lb_algo,
        "-size", TOPO_SIZE,
        "-nodes", str(NODES),
        "-strat", strat,
        "-q", str(QUEUE_SIZE_PKTS),
        "-end", str(END_TIME_US),
        "-seed", str(seed),
        "-o", str(datpath),
    ]
    if spec["kind"] == "native":
        conns = max(1, round(NODES * frac))
        cmd += ["-tornado", "-tornado_conns", str(conns), "-tornado_flowsize", str(flowsize)]
    else:
        cmd += ["-tm", str(cm_path(pattern, NODES, frac, flowsize, seed))]
    if DF_S:
        cmd += ["-s", str(DF_S)]
    if DF_L:
        cmd += ["-l", str(DF_L)]
    if DF_H:
        cmd += ["-h", str(DF_H)]
    if DF_P:
        cmd += ["-p", str(DF_P)]
    if TOPO_SIZE == "s" and NO_PARALLEL_LINK != 1:
        cmd += ["-p_link", str(NO_PARALLEL_LINK)]
    if MTU_BYTES is not None:
        cmd += ["-mtu", str(MTU_BYTES)]
    if HOP_LATENCY_US is not None:
        cmd += ["-hop_latency", str(HOP_LATENCY_US)]
    if SWITCH_LATENCY_US is not None:
        cmd += ["-switch_latency", str(SWITCH_LATENCY_US)]
    return cmd


def run_one(pattern, frac, flowsize, seed, label, strat, lb_algo):
    tag = f"{pattern}_{label}_load{frac:.2f}_fs{flowsize}_seed{seed}"
    logpath = RUN_DIR / f"{tag}.log"
    datpath = RUN_DIR / f"{tag}.dat"
    cmd = build_cmd(pattern, frac, flowsize, seed, strat, lb_algo, datpath)
    with open(logpath, "w") as f:
        try:
            result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                                     timeout=RUN_TIMEOUT_S)
            return logpath, result.returncode
        except subprocess.TimeoutExpired:
            f.write(f"\n[sweep] TIMEOUT after {RUN_TIMEOUT_S}s\n")
            return logpath, -1


def parse_log(logpath):
    text = logpath.read_text()
    if "Done" not in text:
        return None
    fcts = sorted(float(x) for x in FCT_RE.findall(text))
    m = SUMMARY_RE.search(text)
    if not m or not fcts:
        return None
    new, rtx, rts, bounced, acks, nacks, pulls, sleek = (int(x) for x in m.groups())
    n = len(fcts)

    def pct(numer):
        return 100.0 * numer / new if new else 0.0

    return {
        "n_flows": n,
        "fct_mean": sum(fcts) / n,
        "fct_p50": fcts[n // 2],
        "fct_p99": fcts[int(n * 0.99)] if n >= 100 else fcts[-1],
        "fct_max": fcts[-1],
        "fct_min": fcts[0],
        "new": new, "rtx": rtx, "rts": rts, "bounced": bounced,
        "acks": acks, "nacks": nacks, "pulls": pulls, "sleek_pkts": sleek,
        "rtx_rate_pct": pct(rtx),
        "rts_rate_pct": pct(rts),
        "nack_rate_pct": pct(nacks),
        "bounced_rate_pct": pct(bounced),
    }


# ------------------------------------------------------------------ MAIN ---
def validate_config():
    
    if DF_P != 0 and (DF_S == 0 or DF_H == 0):
        print("ERROR: DF_P (and/or DF_L) is set explicitly but DF_S/DF_H are still "
              "0. The topology only auto-fills DF_S/DF_L/DF_H together when DF_P "
              "== 0 -- with DF_P set, DF_S=0/DF_H=0 pass straight through and "
              "produce a degenerate topology that crashes (SIGFPE) almost "
              "immediately.\n"
              "Fix: set DF_S and DF_H explicitly too (all four radix params "
              "together), or leave DF_P/DF_L/DF_S/DF_H all at 0 for full "
              "auto-derive.", file=sys.stderr)
        sys.exit(1)


def plan_runs():
    """Yield (pattern, frac, flowsize, seed, label, strat, lb_algo) for every run."""
    for pattern, spec in TRAFFIC_PATTERNS.items():
        for frac in spec["loads"]:
            for flowsize in FLOWSIZES:
                for seed in SEEDS:
                    for label, strat, lb_algo in STRATEGIES:
                        yield pattern, frac, flowsize, seed, label, strat, lb_algo


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                     help="print the planned runs without executing anything")
    args = ap.parse_args()

    validate_config()

    runs = list(plan_runs())
    if args.dry_run:
        print(f"{len(runs)} runs planned (topology={TOPO_SIZE}, nodes={NODES}):")
        for pattern, frac, flowsize, seed, label, strat, lb_algo in runs:
            print(f"  pattern={pattern:12s} load={frac:.2f} flowsize={flowsize:>10d} seed={seed} "
                  f"strat={label:10s} (-strat {strat} -load_balancing_algo {lb_algo})")
        return

    if not Path(BINARY).exists():
        print(f"ERROR: {BINARY} not found. Build htsim_uec_dfp first "
              f"(cmake --build build --target htsim_uec_dfp) and run this "
              f"script from sim/datacenter/.", file=sys.stderr)
        sys.exit(1)

    ensure_all_cm_files()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    PLOT_DIR.mkdir(parents=True, exist_ok=True)

    rows = []
    failures = []
    total = len(runs)
    for done, (pattern, frac, flowsize, seed, label, strat, lb_algo) in enumerate(runs, 1):
        logpath, rc = run_one(pattern, frac, flowsize, seed, label, strat, lb_algo)
        parsed = parse_log(logpath)
        status = "OK" if parsed else f"FAILED (rc={rc})"
        print(f"[{done}/{total}] {logpath.stem}: {status}")
        if parsed:
            rows.append({
                "pattern": pattern, "nodes": NODES, "size": TOPO_SIZE,
                "strat_label": label, "strat": strat,
                "load_balancing_algo": lb_algo,
                "load_fraction": frac, "flowsize": flowsize, "seed": seed, **parsed,
            })
        else:
            failures.append((logpath.stem, rc))

    OUTDIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUTDIR / "summary.csv"
    fieldnames = ["pattern", "nodes", "size", "strat_label", "strat",
                  "load_balancing_algo", "load_fraction", "flowsize", "seed",
                  "n_flows", "fct_mean", "fct_p50", "fct_p99", "fct_max", "fct_min",
                  "new", "rtx", "rts", "bounced", "acks", "nacks", "pulls", "sleek_pkts",
                  "rtx_rate_pct", "rts_rate_pct", "nack_rate_pct", "bounced_rate_pct"]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} rows to {csv_path}")

    if failures:
        print(f"\n{len(failures)} run(s) FAILED (crash/timeout) -- see logs in {RUN_DIR}/:")
        for stem, rc in failures:
            print(f"  {stem}  (returncode={rc})")

    print_report(rows)

    try:
        make_plots(rows)
    except ImportError:
        print("\nmatplotlib not installed (pip install matplotlib) -- skipped plots, "
              f"CSV/report are still in {OUTDIR}/")


def print_report(rows):
    groups = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = (r["pattern"], r["load_fraction"], r["flowsize"])
        groups[key][r["strat_label"]].append(r)

    strat_desc = ", ".join(f"{label}(-strat {strat} -load_balancing_algo {lb_algo})"
                           for label, strat, lb_algo in STRATEGIES)
    print("\n" + "=" * 100)
    print(f"SUMMARY (mean across seeds)  --  nodes={NODES} size={TOPO_SIZE}")
    print(f"strategies: {strat_desc}")
    print("=" * 100)
    for key in sorted(groups):
        pattern, frac, flowsize = key
        print(f"\n== pattern={pattern} load_fraction={frac:.2f} flowsize={flowsize} ==")
        by_label = {}
        for label, runs in groups[key].items():
            n = len(runs)
            agg = {
                "fct_mean": sum(r["fct_mean"] for r in runs) / n,
                "fct_p99": sum(r["fct_p99"] for r in runs) / n,
                "rtx_rate_pct": sum(r["rtx_rate_pct"] for r in runs) / n,
                "rts_rate_pct": sum(r["rts_rate_pct"] for r in runs) / n,
                "nack_rate_pct": sum(r["nack_rate_pct"] for r in runs) / n,
            }
            by_label[label] = agg
            per_seed = [round(r["fct_mean"], 1) for r in sorted(runs, key=lambda x: x["seed"])]
            print(f"  {label:10s} fct_mean={agg['fct_mean']:9.2f}  fct_p99={agg['fct_p99']:9.2f}  "
                  f"rtx={agg['rtx_rate_pct']:5.2f}%  rts={agg['rts_rate_pct']:5.2f}%  "
                  f"nack={agg['nack_rate_pct']:5.2f}%  n_seeds={n:2d}  per_seed={per_seed}")

        # Relative delta of reps_dfp vs fpar (or, generically, second vs first
        # configured strategy), when both are present for this key.
        labels = [label for label, _, _ in STRATEGIES]
        if len(labels) == 2 and labels[0] in by_label and labels[1] in by_label:
            base, other = by_label[labels[0]], by_label[labels[1]]
            def delta(field):
                if base[field] == 0:
                    return float("nan")
                return 100.0 * (other[field] - base[field]) / base[field]
            print(f"  delta ({labels[1]} vs {labels[0]}): "
                  f"fct_mean={delta('fct_mean'):+6.1f}%  fct_p99={delta('fct_p99'):+6.1f}%  "
                  f"rtx={delta('rtx_rate_pct'):+6.1f}%")


def make_plots(rows):
    import matplotlib.pyplot as plt

    strategy_labels = [label for label, _, _ in STRATEGIES]
    
    display_label = {label: f"{label} ({lb_algo})" for label, _, lb_algo in STRATEGIES}

    by_pattern = defaultdict(list)
    for r in rows:
        by_pattern[r["pattern"]].append(r)

    for pattern, prows in by_pattern.items():
        spec = TRAFFIC_PATTERNS[pattern]
        # Plot against whichever axis actually varies for this pattern.
        # load_fraction is the default axis; patterns with a fixed
        # (single-value) loads list -- e.g. tornado -- plot against
        # flowsize instead, since that's what's actually being swept, with
        # one plot file per distinct value of the other (non-plotted) axis.
        if len(spec["loads"]) > 1:
            axis_field, axis_label, outer_field = "load_fraction", "Load fraction", "flowsize"
        else:
            axis_field, axis_label, outer_field = "flowsize", "Flow size (bytes)", "load_fraction"

        outer_values = sorted(set(r[outer_field] for r in prows))
        for outer_val in outer_values:
            subset = [r for r in prows if r[outer_field] == outer_val]

            groups = defaultdict(lambda: defaultdict(list))
            for r in subset:
                groups[r["strat_label"]][r[axis_field]].append(r)

            ncols = 4
            nrows = -(-len(METRICS) // ncols)  # ceil
            fig, axes = plt.subplots(nrows, ncols, figsize=(5.2 * ncols, 4.2 * nrows), dpi=150)
            axes = axes.flatten()

            for ax, (field, title, ylabel) in zip(axes, METRICS):
                for label in strategy_labels:
                    by_x = groups.get(label)
                    if not by_x:
                        continue
                    xs = sorted(by_x)
                    means = [sum(r[field] for r in by_x[x]) / len(by_x[x]) for x in xs]
                    ax.plot(xs, means, marker="o", markersize=6, linewidth=2,
                            color=STRATEGY_COLORS.get(label, "#888888"), label=display_label[label])
                ax.set_xlabel(axis_label)
                ax.set_ylabel(ylabel)
                ax.set_title(title, fontsize=11)
                ax.grid(True, linewidth=0.5, alpha=0.3)
                ax.spines["top"].set_visible(False)
                ax.spines["right"].set_visible(False)

            # Hide any unused
            for ax in axes[len(METRICS):]:
                ax.set_visible(False)

            # Single shared legend for the whole figure (color mapping is
            # identical across every subplot, so one legend suffices).
            handles, labels_ = axes[0].get_legend_handles_labels()
            fig.legend(handles, labels_, loc="upper center", ncol=len(strategy_labels),
                       frameon=False, bbox_to_anchor=(0.5, 1.02), fontsize=11)
            outer_desc = (f"load={outer_val:.2f}" if outer_field == "load_fraction"
                          else f"flowsize={outer_val}B")
            fig.suptitle(f"REPS_DFP vs FPAR -- pattern={pattern}, nodes={NODES}, "
                         f"size={TOPO_SIZE}, {outer_desc}", fontsize=12, y=1.06)
            fig.tight_layout()
            outer_tag = (f"load{outer_val:.2f}" if outer_field == "load_fraction"
                        else f"fs{outer_val}")
            out_path = PLOT_DIR / f"plot_{pattern}_n{NODES}_{TOPO_SIZE}_{outer_tag}.png"
            fig.savefig(out_path, bbox_inches="tight")
            plt.close(fig)
            print(f"wrote {out_path}")


if __name__ == "__main__":
    main()

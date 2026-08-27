#!/usr/bin/env python3
"""Reproduces the full REPS entropy-partition investigation: fpar vs uniform
REPS (no partition) vs REPS with the tuned partition schedule, on the
corrected Dragonfly+ routing code.

  python sweep_reps_partition_final.py

Requires htsim_uec_dfp built with -dfp_low_share, -dfp_low_uniform,
-reps_escalate_threshold, -reps_warmup_explore_rtts, -reps_explore_prob and
-threshold. 
"""
import argparse
import csv
import re
import subprocess
import sys
import matplotlib.pyplot as plt
import numpy as np
from collections import defaultdict
from pathlib import Path
from statistics import mean

# CONFIGURATION

BINARY = "./htsim_uec_dfp_fin"

TOPO_SIZE = "m"
NODES = 1100

DF_S = DF_L = DF_H = DF_P = 0
NO_PARALLEL_LINK = 1

QUEUE_SIZE_PKTS = 50
END_TIME_US = 100000
MTU_BYTES = 4150  # main_uec_df.cpp's default packet_size; match -mtu if  override

# Partition parameters after calibration

EXPLORE_PROB = 70
ESCALATE_THRESHOLD = 0.2
WARMUP_RTTS = 0.5
FPAR_THRESHOLD_FRAC = 0.10
FPAR_THRESHOLD_BYTES = int(QUEUE_SIZE_PKTS * MTU_BYTES * FPAR_THRESHOLD_FRAC)
FPAR_EXTRA_FLAGS = ["-disable_hop_rtt_normalization"]


def partition_extra_flags(pattern, load, fs, seed):
    return ["-reps_partition_entropy",
            "-reps_escalate_threshold", str(ESCALATE_THRESHOLD),
            "-reps_warmup_explore_rtts", str(WARMUP_RTTS),
            "-reps_explore_prob", str(EXPLORE_PROB)]


def partition_tag_suffix():
    return f"_e{ESCALATE_THRESHOLD}_w{WARMUP_RTTS}_p{EXPLORE_PROB}"


STRATEGIES = [
    #("fpar",              "fpar",     "oblivious", lambda p, l, f, s: list(FPAR_EXTRA_FLAGS)),
    ("fpar",        "fpar",     "oblivious", lambda p, l, f, s: ["-threshold", str(FPAR_THRESHOLD_BYTES)] + FPAR_EXTRA_FLAGS),
    ("reps_uniform_draws", "reps_dfp", "reps",      lambda p, l, f, s: ["-dfp_low_uniform"]),
    ("reps_share", "reps_dfp", "reps",      lambda p, l, f, s: []),
    ("reps_partition",    "reps_dfp", "reps",      partition_extra_flags),
]
STRATEGY_COLORS = {
    #"fpar":              "#0072B2",
    "fpar":         "#56B4E9",
    "reps_uniform_draws": "#D55E00",
    "reps_share":   "#CC79A7",
    "reps_partition":    "#E69F00",
}
BASELINE_LABEL = "reps_share"  # deltas in the report are vs this

TRAFFIC_PATTERNS = {
    "tornado":     {"kind": "native", "loads": [0.25, 0.50, 0.75, 1.00]},
    "permutation": {"kind": "file", "script": "gen_permutation_full_bisection.py",
                     "extra_args": [], "loads": [0.60, 0.70, 0.80, 0.90]},
    "permutation_random": {"kind": "file", "script": "gen_permutation.py",
                            "extra_args": [], "loads": [0.60, 0.70, 0.80, 0.90]},
}

SEEDS = [1, 2, 3, 4, 5]
FLOWSIZES = [100_000, 250_000,500_000, 2_000_000,  5_000_000, 10_000_000, ]
EXTRA_START_US = 0.0
RUN_TIMEOUT_S = 1200

OUTDIR = Path("finalmaybe")

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
    ("duration_us", "Total run duration (makespan)", "Duration (us)"),
    ("fct_mean", "Mean FCT", "Mean FCT (us)"),
    ("fct_p99", "p99 FCT", "p99 FCT (us)"),
    ("fct_max", "Slowest flow's FCT", "Time (us)"),
    ("rtx_rate_pct", "Retransmit rate", "Rtx / New (%)"),
    ("nack_rate_pct", "NACK rate", "NACKs / New (%)"),
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
    subprocess.run(cmd, check=True)
    return fname


def ensure_all_cm_files():
    CM_DIR.mkdir(parents=True, exist_ok=True)
    for pattern, spec in TRAFFIC_PATTERNS.items():
        if spec["kind"] != "file":
            continue
        for frac in spec["loads"]:
            for flowsize in FLOWSIZES:
                for seed in SEEDS:
                    ensure_cm_file(pattern, NODES, frac, flowsize, seed)


def build_cmd(pattern, frac, flowsize, seed, strat, lb_algo, extra_flags, datpath):
    spec = TRAFFIC_PATTERNS[pattern]
    cmd = [BINARY, "-load_balancing_algo", lb_algo, "-size", TOPO_SIZE,
           "-nodes", str(NODES), "-strat", strat, "-q", str(QUEUE_SIZE_PKTS),
           "-end", str(END_TIME_US), "-seed", str(seed), "-o", str(datpath)]
    if spec["kind"] == "native":
        conns = max(1, round(NODES * frac))
        cmd += ["-tornado", "-tornado_conns", str(conns), "-tornado_flowsize", str(flowsize)]
    else:
        cmd += ["-tm", str(cm_path(pattern, NODES, frac, flowsize, seed))]
    if DF_S: cmd += ["-s", str(DF_S)]
    if DF_L: cmd += ["-l", str(DF_L)]
    if DF_H: cmd += ["-h", str(DF_H)]
    if DF_P: cmd += ["-p", str(DF_P)]
    if TOPO_SIZE == "s" and NO_PARALLEL_LINK != 1:
        cmd += ["-p_link", str(NO_PARALLEL_LINK)]
    cmd += extra_flags
    return cmd


def run_one(tag, cmd):
    logpath = RUN_DIR / f"{tag}.log"
    if logpath.exists() and "Done" in logpath.read_text(errors="ignore"):
        return logpath, 0
    with open(logpath, "w") as f:
        try:
            result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=RUN_TIMEOUT_S)
            return logpath, result.returncode
        except subprocess.TimeoutExpired:
            f.write(f"\n[sweep] TIMEOUT after {RUN_TIMEOUT_S}s\n")
            return logpath, -1


def parse_log(logpath):
    text = logpath.read_text(errors="ignore")
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
        "duration_us": fcts[-1],
        "fct_mean": sum(fcts) / n,
        "fct_p50": fcts[n // 2],
        "fct_p99": fcts[int(n * 0.99)] if n >= 100 else fcts[-1],
        "fct_max": fcts[-1], "fct_min": fcts[0],
        "new": new, "rtx": rtx, "rts": rts, "bounced": bounced,
        "acks": acks, "nacks": nacks, "pulls": pulls, "sleek_pkts": sleek,
        "rtx_rate_pct": pct(rtx), "rts_rate_pct": pct(rts),
        "nack_rate_pct": pct(nacks), "bounced_rate_pct": pct(bounced),
    }


def check_binary():
    if not Path(BINARY).exists():
        print(f"ERROR: {BINARY} not found. Build it first and run this script "
              f"from sim/datacenter/.", file=sys.stderr)
        sys.exit(1)


def plan_compare_runs():
    for pattern, spec in TRAFFIC_PATTERNS.items():
        for frac in spec["loads"]:
            for flowsize in FLOWSIZES:
                for seed in SEEDS:
                    for label, strat, lb_algo, extra_fn in STRATEGIES:
                        yield pattern, frac, flowsize, seed, label, strat, lb_algo, extra_fn


def run_compare(dry_run):
    runs = list(plan_compare_runs())
    if dry_run:
        print(f"{len(runs)} runs planned (compare mode)")
        for pattern, frac, fs, seed, label, strat, lb, extra_fn in runs[:20]:
            extra = extra_fn(pattern, frac, fs, seed)
            print(f"  {pattern:12s} load={frac:.2f} fs={fs:>10d} seed={seed} "
                  f"{label:20s} extra={' '.join(extra) if extra else '(none)'}")
        if len(runs) > 20:
            print(f"  ... and {len(runs)-20} more")
        return

    check_binary()
    ensure_all_cm_files()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    PLOT_DIR.mkdir(parents=True, exist_ok=True)

    rows, failures = [], []
    total = len(runs)
    for done, (pattern, frac, fs, seed, label, strat, lb, extra_fn) in enumerate(runs, 1):
        extra = extra_fn(pattern, frac, fs, seed)
        tag = f"{pattern}_{label}_load{frac:.2f}_fs{fs}_seed{seed}"
        if label == "reps_partition":
            tag += partition_tag_suffix()
        datpath = RUN_DIR / f"{tag}.dat"
        cmd = build_cmd(pattern, frac, fs, seed, strat, lb, extra, datpath)
        logpath, rc = run_one(tag, cmd)
        parsed = parse_log(logpath)
        status = "OK" if parsed else f"FAILED/NODATA (rc={rc})"
        print(f"[{done}/{total}] {tag}: {status}")
        if parsed:
            rows.append({"pattern": pattern, "nodes": NODES, "size": TOPO_SIZE,
                          "strat_label": label, "strat": strat, "load_balancing_algo": lb,
                          "extra_flags": " ".join(extra), "load_fraction": frac,
                          "flowsize": fs, "seed": seed, **parsed})
        else:
            failures.append((tag, rc))

    OUTDIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUTDIR / "summary.csv"
    fieldnames = ["pattern", "nodes", "size", "strat_label", "strat", "load_balancing_algo",
                  "extra_flags", "load_fraction", "flowsize", "seed", "n_flows", "duration_us",
                  "fct_mean", "fct_p50", "fct_p99", "fct_max", "fct_min", "new", "rtx", "rts",
                  "bounced", "acks", "nacks", "pulls", "sleek_pkts", "rtx_rate_pct",
                  "rts_rate_pct", "nack_rate_pct", "bounced_rate_pct"]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {len(rows)} rows to {csv_path}")
    if failures:
        print(f"\n{len(failures)} run(s) produced no data (crash/timeout/0 flows in window):")
        for tag, rc in failures:
            print(f"  {tag}  (returncode={rc})")

    print_compare_report(rows)
    try:
        make_compare_plots(rows)
    except ImportError:
        print("\nmatplotlib not installed (pip install matplotlib) -- skipped plots, "
              f"CSV/report are still in {OUTDIR}/")


def print_compare_report(rows):
    groups = defaultdict(lambda: defaultdict(list))
    for r in rows:
        groups[(r["pattern"], r["load_fraction"], r["flowsize"])][r["strat_label"]].append(r)

    labels = [l for l, *_ in STRATEGIES]
    other_labels = [l for l in labels if l != BASELINE_LABEL]

    print("\n" + "=" * 110)
    print(f"COMPARISON  --  nodes={NODES} size={TOPO_SIZE}  baseline={BASELINE_LABEL!r}")
    print("=" * 110)

    deltas = defaultdict(lambda: defaultdict(list))
    win = defaultdict(int); tie = defaultdict(int); loss = defaultdict(int)

    for key in sorted(groups):
        pattern, frac, fs = key
        by_label = {}
        for label, runs in groups[key].items():
            n = len(runs)
            by_label[label] = {
                "fct_mean": sum(r["fct_mean"] for r in runs) / n,
                "fct_p99": sum(r["fct_p99"] for r in runs) / n,
                "duration_us": sum(r["duration_us"] for r in runs) / n,
                "rtx_rate_pct": sum(r["rtx_rate_pct"] for r in runs) / n,
                "nack_rate_pct": sum(r["nack_rate_pct"] for r in runs) / n,
                "n_seeds": n,
            }
        print(f"\n== pattern={pattern} load={frac:.2f} flowsize={fs} ==")
        for label in labels:
            a = by_label.get(label)
            if not a:
                continue
            print(f"  {label:20s} mean={a['fct_mean']:9.2f}  p99={a['fct_p99']:9.2f}  "
                  f"duration={a['duration_us']:9.2f}  rtx={a['rtx_rate_pct']:6.2f}%  "
                  f"nack={a['nack_rate_pct']:6.2f}%  n_seeds={a['n_seeds']}")
        base = by_label.get(BASELINE_LABEL)
        if not base:
            continue
        for label in other_labels:
            a = by_label.get(label)
            if not a:
                continue
            dm = 100.0 * (a["fct_mean"] - base["fct_mean"]) / base["fct_mean"] if base["fct_mean"] else float("nan")
            dp = 100.0 * (a["fct_p99"] - base["fct_p99"]) / base["fct_p99"] if base["fct_p99"] else float("nan")
            deltas[label]["mean"].append(dm)
            deltas[label]["p99"].append(dp)
            deltas[label]["cell"].append(f"{pattern} load {frac:.2f} fs {fs}")
            deltas[label]["rtx_abs"].append(a["rtx_rate_pct"])
            deltas[label]["nack_abs"].append(a["nack_rate_pct"])
            if dm < -2 and dp < -2:
                win[label] += 1
            elif dm > 2 or dp > 2:
                loss[label] += 1
            else:
                tie[label] += 1
            print(f"  delta ({label} vs {BASELINE_LABEL}): fct_mean={dm:+6.1f}%  fct_p99={dp:+6.1f}%  "
                  f"(rtx={a['rtx_rate_pct']:5.2f}%  nack={a['nack_rate_pct']:5.2f}% -- absolute, "
                  f"not a delta -- vs baseline rtx={base['rtx_rate_pct']:5.2f}%  nack={base['nack_rate_pct']:5.2f}%)")

    print("\n" + "=" * 110)
    print(f"OVERALL (average delta vs {BASELINE_LABEL!r}, win = >2% better on both mean and p99, "
          f"loss = >2% worse on either)")
    print("=" * 110)
    for label in other_labels:
        if not deltas[label]["mean"]:
            continue
        n = len(deltas[label]["mean"])
        print(f"  {label:20s} mean={mean(deltas[label]['mean']):+6.1f}%  "
              f"p99={mean(deltas[label]['p99']):+6.1f}%   "
              f"avg_rtx={mean(deltas[label]['rtx_abs']):5.2f}%  "
              f"avg_nack={mean(deltas[label]['nack_abs']):5.2f}%   "
              f"wins={win[label]}  ties={tie[label]}  losses={loss[label]}  (of {n} points)")

    # An average hides a setting that wins big on one workload and loses big on
    # another, so print the extremes too: a candidate default has to be judged
    # on its WORST cell, not its mean.
    print("\n" + "=" * 110)
    print("BEST AND WORST SINGLE CELL (mean FCT delta vs baseline) -- a default must survive its worst cell")
    print("=" * 110)
    for label in other_labels:
        d = deltas[label]["mean"]
        if not d:
            continue
        cells = deltas[label]["cell"]
        wi, bi = d.index(max(d)), d.index(min(d))
        verdict = "no cell regresses" if max(d) <= 2 else f"regresses up to {max(d):+.1f}%"
        print(f"  {label:20s} best={min(d):+6.1f}% ({cells[bi]})   "
              f"worst={max(d):+6.1f}% ({cells[wi]})   -> {verdict}")


def make_compare_plots(rows):

    labels = [l for l, *_ in STRATEGIES]
    display_label = {l: f"{l} ({lb})" for l, _, lb, _ in STRATEGIES}

    by_pattern = defaultdict(list)
    for r in rows:
        by_pattern[r["pattern"]].append(r)

    for pattern, prows in by_pattern.items():
        spec = TRAFFIC_PATTERNS[pattern]
        if len(spec["loads"]) > 1:
            axis_field, axis_label, outer_field = "load_fraction", "Load fraction", "flowsize"
        else:
            axis_field, axis_label, outer_field = "flowsize", "Flow size (bytes)", "load_fraction"

        for outer_val in sorted(set(r[outer_field] for r in prows)):
            subset = [r for r in prows if r[outer_field] == outer_val]
            groups = defaultdict(lambda: defaultdict(list))
            for r in subset:
                groups[r["strat_label"]][r[axis_field]].append(r)

            ncols = 3
            nrows = -(-len(METRICS) // ncols)
            fig, axes = plt.subplots(nrows, ncols, figsize=(5.2 * ncols, 4.2 * nrows), dpi=150)
            axes = axes.flatten()
            for ax, (field, title, ylabel) in zip(axes, METRICS):
                for label in labels:
                    by_x = groups.get(label)
                    if not by_x:
                        continue
                    xs = sorted(by_x)
                    means = [sum(r[field] for r in by_x[x]) / len(by_x[x]) for x in xs]
                    ax.plot(xs, means, marker="o", markersize=6, linewidth=2,
                            color=STRATEGY_COLORS.get(label, "#888888"), label=display_label[label])
                ax.set_xlabel(axis_label); ax.set_ylabel(ylabel); ax.set_title(title, fontsize=11)
                ax.grid(True, linewidth=0.5, alpha=0.3)
                ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
            for ax in axes[len(METRICS):]:
                ax.set_visible(False)
            handles, labels_ = axes[0].get_legend_handles_labels()
            fig.legend(handles, labels_, loc="upper center", ncol=4, frameon=False,
                       bbox_to_anchor=(0.5, 1.08), fontsize=10)
            outer_desc = f"load={outer_val:.2f}" if outer_field == "load_fraction" else f"flowsize={outer_val}B"
            fig.suptitle(f"fpar (default+tuned) vs reps (no partition) vs reps (tuned partition) -- "
                         f"pattern={pattern}, nodes={NODES}, size={TOPO_SIZE}, {outer_desc}",
                         fontsize=12, y=1.13)
            fig.tight_layout()
            outer_tag = f"load{outer_val:.2f}" if outer_field == "load_fraction" else f"fs{outer_val}"
            out_path = PLOT_DIR / f"plot_{pattern}_n{NODES}_{TOPO_SIZE}_{outer_tag}.png"
            fig.savefig(out_path, bbox_inches="tight")
            plt.close(fig)
            print(f"wrote {out_path}")

        make_duration_barplot(pattern, prows, axis_field, axis_label, labels, display_label)


def make_duration_barplot(pattern, prows, axis_field, axis_label, labels, display_label):
    

    groups = defaultdict(lambda: defaultdict(list))
    for r in prows:
        groups[r["strat_label"]][r[axis_field]].append(r)
    xs = sorted(set(r[axis_field] for r in prows))
    if not xs:
        return
    x_pos = np.arange(len(xs))
    n_s = len(labels)
    bar_w = 0.8 / max(n_s, 1)
    fig, ax = plt.subplots(figsize=(1.8 * len(xs) + 2, 4.5), dpi=150)
    for i, label in enumerate(labels):
        by_x = groups.get(label)
        if not by_x:
            continue
        means = [sum(r["duration_us"] for r in by_x.get(x, [])) / len(by_x[x])
                 if by_x.get(x) else 0 for x in xs]
        offset = (i - (n_s - 1) / 2) * bar_w
        ax.bar(x_pos + offset, means, width=bar_w,
               color=STRATEGY_COLORS.get(label, "#888888"), label=display_label[label])
    ax.set_xticks(x_pos)
    ax.set_xticklabels([f"{x:.2f}" if axis_field == "load_fraction" else str(x) for x in xs])
    ax.set_xlabel(axis_label); ax.set_ylabel("Total run duration (us)")
    ax.set_title(f"Total run duration (makespan) -- pattern={pattern}, nodes={NODES}, size={TOPO_SIZE}",
                 fontsize=10)
    ax.grid(True, axis="y", linewidth=0.5, alpha=0.3)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    ax.legend(frameon=False)
    fig.tight_layout()
    out_path = PLOT_DIR / f"duration_{pattern}_n{NODES}_{TOPO_SIZE}.png"
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {out_path}")


def main():
    global WARMUP_RTTS, EXPLORE_PROB, ESCALATE_THRESHOLD, OUTDIR, CM_DIR, RUN_DIR, PLOT_DIR
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--warmup", type=float, help=f"override WARMUP_RTTS (default {WARMUP_RTTS})")
    ap.add_argument("--prob", type=int, help=f"override EXPLORE_PROB (default {EXPLORE_PROB})")
    ap.add_argument("--escalate", type=float, help=f"override ESCALATE_THRESHOLD (default {ESCALATE_THRESHOLD})")
    ap.add_argument("--outdir", help=f"override OUTDIR (default {OUTDIR})")
    args = ap.parse_args()

    if args.warmup is not None:
        WARMUP_RTTS = args.warmup
    if args.prob is not None:
        EXPLORE_PROB = args.prob
    if args.escalate is not None:
        ESCALATE_THRESHOLD = args.escalate
    if args.outdir:
        OUTDIR = Path(args.outdir)
        CM_DIR, RUN_DIR, PLOT_DIR = OUTDIR / "connection_matrices", OUTDIR / "runs", OUTDIR / "PLOTS"
    print(f"[settings] escalate={ESCALATE_THRESHOLD} warmup={WARMUP_RTTS} "
          f"explore_prob={EXPLORE_PROB} (flat)  outdir={OUTDIR}")

    run_compare(args.dry_run)


if __name__ == "__main__":
    main()
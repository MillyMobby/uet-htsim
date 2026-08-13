#!/usr/bin/env python3
"""Reproduces the full REPS entropy-partition investigation: fpar vs plain
REPS (no partition) vs REPS with the tuned partition schedule, on the
corrected Dragonfly+ routing code, plus the calibration sweeps that
justified the final schedule's parameters.

Two modes:

  python sweep_reps_partition_final.py compare
      The headline result: fpar (default threshold) / fpar (tuned
      threshold) / reps (no partition) / reps (partition, final schedule)
      across three traffic patterns, all loads and flowsizes, N seeds.
      Writes summary.csv, a text report with per-point deltas and
      win/tie/loss counts, and plots (per-metric small multiples + a
      dedicated "total run duration" bar chart).

      The three patterns span both GROUP-LOCALITY regimes (see
      TRAFFIC_PATTERNS below for the capacity argument): tornado and
      permutation concentrate each group's traffic onto 1-2 destination
      groups, so only 10-20 of a group's 100 global egress links are
      minimal-eligible and biasing toward minimal paths can only
      oversubscribe them; permutation_random spreads across ~all 10
      destination groups, so every egress link is minimal-eligible and
      minimal routing -- which crosses 1 global link instead of 2 -- wins.
      Reporting all three is what turns "partitioning didn't help" into a
      statement about WHEN it can help.

      Both FPAR arms run with -disable_hop_rtt_normalization. Per-hop RTT
      normalization is ON by default for FPAR in main_uec_df.cpp
      (routing_mixes_hop_counts includes FPAR, since FPAR's adaptive
      routing can also mix hop counts within a flow) -- but this was never
      an explicitly validated choice for FPAR specifically, only intended
      for REPS. Checked directly: with it OFF, tuned FPAR's only weak
      spot (10MB flows under high load, 3/16 losses on permutation) either
      ties or flips to a win in every case tested (WIN 5->6, LOSS 3->0 on
      an 8-point permutation check) -- so it's both more correct relative
      to what was intended AND a stronger result. REPS's own runs are
      unaffected by this flag (it's per-run, FPAR and REPS_DFP never share
      a run).

      The fpar_tuned arm uses -threshold set to FPAR_THRESHOLD_FRAC (10%)
      of queue capacity, found by sweeping FPAR's per-hop queue-occupancy
      threshold: FPAR's built-in default (50% of queue capacity) loses to
      REPS on all 32/32 tornado+permutation points; the tuned 10% value
      wins outright on most permutation points. 10% was the lowest value
      tested, not necessarily the true optimum -- see FPAR_THRESHOLD_FRAC
      below to push further.

  python sweep_reps_partition_final.py calibrate
      Reproduces the three calibration curves that justified the final
      schedule: escalate-threshold sweep, warmup-duration sweep, and
      explore-prob-vs-load sweep. All three cover the SAME 8-point grid
      (tornado + permutation, all 4 loads each) at 500KB flowsize --
      threshold/warmup_rtts originally only tested a single point
      (tornado load=1.00, 100KB), which was enough to pick a value but not
      enough to claim it generalizes across the grid it's actually applied
      over.

      Also tracks rtx_rate_pct and nack_rate_pct alongside FCT mean/p99
      for every calibration point -- FCT alone can look fine while a
      parameter choice quietly causes much more retransmission/NACK
      overhead.

  --dry-run works with either mode.

Requires htsim_uec_dfp built with -dfp_low_share, -reps_escalate_threshold,
-reps_warmup_explore_rtts, -reps_explore_prob and -threshold. If any of
those flags are unrecognized, see the prerequisite patch delivered
alongside this script.
"""
import argparse
import csv
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

# ============================================================================
# CONFIGURATION -- edit this block, then run the script
# ============================================================================

BINARY = "./htsim_uec_dfp_reps"   # rename to match your build if different

TOPO_SIZE = "m"
NODES = 1100

DF_S = DF_L = DF_H = DF_P = 0
NO_PARALLEL_LINK = 1

QUEUE_SIZE_PKTS = 50
END_TIME_US = 100000
MTU_BYTES = 4150  # main_uec_df.cpp's default packet_size; match -mtu if you override it

# ---- Final tuned partition schedule ----------------------------------------
EXPLORE_PROB_SCHEDULE = {
    ("tornado", 0.25): 70,
    ("tornado", 0.50): 40,
    ("tornado", 0.75): 40,
    ("tornado", 1.00): 40,
    ("permutation", 0.60): 70,
    ("permutation", 0.70): 70,
    ("permutation", 0.80): 40,
    ("permutation", 0.90): 40,
    # permutation_random deliberately has NO entries: this schedule was
    # calibrated entirely on the two concentrated patterns, where the goal
    # is to keep the minimal share near the ~10% capacity optimum (hence
    # the HIGH explore probabilities -- exploration pushes draws back to
    # the open tier). Under spread traffic the optimum runs the other way
    # (more minimal is better, up to ~100%), so these values are not just
    # uncalibrated but pointing the wrong direction there. Falls through to
    # DEFAULT_PROB; run `calibrate` on this pattern before quoting the
    # reps_partition arm's numbers on it as tuned.
}
#DEFAULT_PROB = 40
#ESCALATE_THRESHOLD = 0.2
#WARMUP_RTTS = 1.0
DEFAULT_PROB = 70
ESCALATE_THRESHOLD = 0.2
WARMUP_RTTS = 2.0

# FPAR's per-hop queue-occupancy threshold T. Swept 10/25/50/75/90%:
# monotonic, no reversals -- lower is strictly better down to 10%, the
# lowest value tried.
FPAR_THRESHOLD_FRAC = 0.10
FPAR_THRESHOLD_BYTES = int(QUEUE_SIZE_PKTS * MTU_BYTES * FPAR_THRESHOLD_FRAC)

# Per-hop RTT normalization is ON by default for FPAR in main_uec_df.cpp,
# but that was never a deliberate choice for FPAR (only intended for REPS).
# Verified disabling it doesn't hurt FPAR and closes its one weak spot
# (10MB/high-load permutation) -- see module docstring. Set to [] instead
# of ["-disable_hop_rtt_normalization"] to go back to the (unvalidated)
# default-on behavior.
FPAR_EXTRA_FLAGS = ["-disable_hop_rtt_normalization"]


def explore_prob_for(pattern, load):
    return EXPLORE_PROB_SCHEDULE.get((pattern, load), DEFAULT_PROB)


def partition_extra_flags(pattern, load, fs, seed):
    return ["-reps_partition_entropy",
            "-reps_escalate_threshold", str(ESCALATE_THRESHOLD),
            "-reps_warmup_explore_rtts", str(WARMUP_RTTS),
            "-reps_explore_prob", str(explore_prob_for(pattern, load))]


STRATEGIES = [
    #("fpar",              "fpar",     "oblivious", lambda p, l, f, s: list(FPAR_EXTRA_FLAGS)),
    ("fpar_tuned",        "fpar",     "oblivious", lambda p, l, f, s: ["-threshold", str(FPAR_THRESHOLD_BYTES)] + FPAR_EXTRA_FLAGS),
    ("reps_no_partition", "reps_dfp", "reps",      lambda p, l, f, s: []),
    ("reps_partition",    "reps_dfp", "reps",      partition_extra_flags),
]
STRATEGY_COLORS = {
    #"fpar":              "#0072B2",  # blue
    "fpar_tuned":         "#56B4E9",  # light blue
    "reps_no_partition": "#009E73",  # green
    "reps_partition":    "#E69F00",  # orange
}
BASELINE_LABEL = "reps_no_partition"  # deltas in the report are vs this

# Three patterns, spanning the two GROUP-LOCALITY regimes that determine
# whether exploiting the minimal/non-minimal distinction can pay off at all.
#
# Minimal-path capacity on Dragonfly+ is allocated per GROUP PAIR: with
# s=10 spines per group and each spine holding exactly one global link to
# each other group (see the neighbour construction in
# dragonfly_plus_switch.cpp), a group has 100 global egress links, of which
# exactly 10 reach any given destination group directly. So how much
# minimal capacity a source group can actually use depends on how many
# destination groups its traffic spreads across:
#
#   pattern              dst groups per src group   minimal-eligible links
#   tornado (offset 5)            1.00                    10 of 100
#   permutation (stride n/2)      2.00                    20 of 100
#   permutation_random            9.82                   100 of 100
#
# The first two are the CONCENTRATED (adversarial) regime: biasing toward
# minimal paths oversubscribes those few links. Plain REPS's uniform draw
# over the switch's MID set already yields ~10% minimal usage, which is the
# capacity-proportional optimum there -- so partitioning can only lose.
# permutation_random is the SPREAD (benign) regime: every egress link is
# minimal-eligible, minimal routing crosses 1 global link instead of 2, and
# biasing toward minimal wins by up to ~20% mean FCT (growing with load).
#
# All three are permutations -- a bijection, every node sends exactly one
# flow and receives exactly one, so no host is oversubscribed in any of
# them. They are "uniform" at the HOST level and differ only in GROUP-level
# locality, which is precisely what f* (the optimal minimal share) depends
# on. Keeping all three makes that dependence measurable rather than
# implicit.
TRAFFIC_PATTERNS = {
    "tornado":     {"kind": "native", "loads": [0.25, 0.50, 0.75, 1.00]},
    #"permutation": {"kind": "file", "script": "gen_permutation_full_bisection.py",
    #                 "extra_args": [], "loads": [0.60, 0.70, 0.80, 0.90]},
    "permutation_random": {"kind": "file", "script": "gen_permutation.py",
                            "extra_args": [], "loads": [0.60, 0.70, 0.80, 0.90]},
}

SEEDS = [1, 2, 3, 4, 5]
FLOWSIZES = [100_000, 500_000, 2_000_000, 10_000_000]
EXTRA_START_US = 0.0
RUN_TIMEOUT_S = 1200

OUTDIR = Path("flippati_tuned")

CALIBRATE_SEEDS = [1, 2, 3]
CALIBRATE_FS = 500_000
# Calibration grid. permutation_random is included so the schedule can be
# calibrated for the spread regime too -- but note this makes calibrate mode
# 3 patterns instead of 2 (504 runs instead of 336). Drop the entry if you
# only want to reproduce the original two-pattern calibration.
CALIBRATE_LOADS = {
    "tornado": [0.25, 0.50, 0.75, 1.00],
   # "permutation": [0.60, 0.70, 0.80, 0.90],
    "permutation_random": [0.60, 0.70, 0.80, 0.90],
}
THRESHOLD_VALUES = [0.5, 0.35, 0.2, 0.1]
WARMUP_RTT_VALUES = [0, 0.5, 1, 2, 4, 8]
PROB_VALUES = [0, 20, 40, 70]



# ============================================================================
# Implementation -- shouldn't need to edit below this line
# ============================================================================

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


def make_compare_plots(rows):
    import matplotlib.pyplot as plt
    import numpy as np

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
    import matplotlib.pyplot as plt
    import numpy as np

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


def calib_cmd(datpath, seed, load, fs, extra):
    conns = max(1, round(NODES * load))
    return [BINARY, "-load_balancing_algo", "reps", "-size", TOPO_SIZE, "-nodes", str(NODES),
            "-strat", "reps_dfp", "-q", str(QUEUE_SIZE_PKTS), "-end", str(END_TIME_US),
            "-seed", str(seed), "-o", str(datpath), "-tornado", "-tornado_conns", str(conns),
            "-tornado_flowsize", str(fs), "-reps_partition_entropy"] + extra


def calib_cmd_perm(pattern, datpath, seed, load, fs, extra):
    return [BINARY, "-load_balancing_algo", "reps", "-size", TOPO_SIZE, "-nodes", str(NODES),
            "-strat", "reps_dfp", "-q", str(QUEUE_SIZE_PKTS), "-end", str(END_TIME_US),
            "-seed", str(seed), "-o", str(datpath),
            "-tm", str(cm_path(pattern, NODES, load, fs, seed)),
            "-reps_partition_entropy"] + extra


def calib_cmd_for(pattern, datpath, seed, load, fs, extra):
    if TRAFFIC_PATTERNS[pattern]["kind"] == "native":
        return calib_cmd(datpath, seed, load, fs, extra)
    return calib_cmd_perm(pattern, datpath, seed, load, fs, extra)


def no_partition_reference(load, fs, seed, pattern="tornado"):
    datpath = RUN_DIR / f"ref_{pattern}_l{load:.2f}_fs{fs}_s{seed}.dat"
    cmd = calib_cmd_for(pattern, datpath, seed, load, fs, [])
    cmd = [c for c in cmd if c != "-reps_partition_entropy"]
    tag = f"ref_{pattern}_l{load:.2f}_fs{fs}_s{seed}"
    logpath, rc = run_one(tag, cmd)
    return parse_log(logpath)


def run_calibrate(dry_run):
    jobs = []
    for pattern, loads in CALIBRATE_LOADS.items():
        for load in loads:
            for thr in THRESHOLD_VALUES:
                for seed in CALIBRATE_SEEDS:
                    jobs.append(("threshold", thr, pattern, load, CALIBRATE_FS, seed,
                                 ["-reps_escalate_threshold", str(thr)]))
    for pattern, loads in CALIBRATE_LOADS.items():
        for load in loads:
            for rtts in WARMUP_RTT_VALUES:
                for seed in CALIBRATE_SEEDS:
                    jobs.append(("warmup_rtts", rtts, pattern, load, CALIBRATE_FS, seed,
                                 ["-reps_escalate_threshold", "0.2", "-reps_warmup_explore_rtts", str(rtts)]))
    for pattern, loads in CALIBRATE_LOADS.items():
        for load in loads:
            for prob in PROB_VALUES:
                for seed in CALIBRATE_SEEDS:
                    jobs.append(("explore_prob", prob, pattern, load, CALIBRATE_FS, seed,
                                 ["-reps_escalate_threshold", "0.2", "-reps_warmup_explore_rtts", "1.0",
                                  "-reps_explore_prob", str(prob)]))

    if dry_run:
        print(f"{len(jobs)} calibration runs planned")
        for j in jobs[:15]:
            print(" ", j)
        print("  ...")
        return

    check_binary()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    CM_DIR.mkdir(parents=True, exist_ok=True)
    for pattern, loads in CALIBRATE_LOADS.items():
        if TRAFFIC_PATTERNS[pattern]["kind"] != "file":
            continue
        for seed in CALIBRATE_SEEDS:
            for load in loads:
                ensure_cm_file(pattern, NODES, load, CALIBRATE_FS, seed)

    results = defaultdict(list)
    total = len(jobs)
    for i, (sweep, x, pattern, load, fs, seed, extra) in enumerate(jobs, 1):
        tag = f"{sweep}_{x}_{pattern}_l{load:.2f}_fs{fs}_s{seed}"
        cmd = calib_cmd_for(pattern, RUN_DIR / f"{tag}.dat", seed, load, fs, extra)
        logpath, rc = run_one(tag, cmd)
        parsed = parse_log(logpath)
        print(f"[{i}/{total}] {tag}: {'OK' if parsed else f'FAILED (rc={rc})'}")
        if parsed:
            results[(sweep, x, pattern, load, fs)].append(
                (parsed["fct_mean"], parsed["fct_p99"], parsed["rtx_rate_pct"], parsed["nack_rate_pct"]))

    ref = {}
    for sweep, x, pattern, load, fs, seed, extra in jobs:
        key = (pattern, load, fs)
        if key in ref:
            continue
        vals = []
        for seed in CALIBRATE_SEEDS:
            r = no_partition_reference(load, fs, seed, pattern)
            if r:
                vals.append((r["fct_mean"], r["fct_p99"], r["rtx_rate_pct"], r["nack_rate_pct"]))
        if vals:
            ref[key] = (mean(v[0] for v in vals), mean(v[1] for v in vals),
                        mean(v[2] for v in vals), mean(v[3] for v in vals))

    OUTDIR.mkdir(parents=True, exist_ok=True)
    with open(OUTDIR / "calibration.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["sweep", "x", "pattern", "load", "flowsize", "mean", "p99",
                    "mean_delta_pct", "p99_delta_pct", "rtx_rate_pct", "nack_rate_pct",
                    "ref_rtx_rate_pct", "ref_nack_rate_pct"])
        for (sweep, x, pattern, load, fs), vals in sorted(results.items()):
            m_ = mean(v[0] for v in vals); p_ = mean(v[1] for v in vals)
            rtx_ = mean(v[2] for v in vals); nack_ = mean(v[3] for v in vals)
            rm, rp, r_rtx, r_nack = ref.get((pattern, load, fs), (None, None, None, None))
            dm = 100 * (m_ - rm) / rm if rm else ""
            dp = 100 * (p_ - rp) / rp if rp else ""
            w.writerow([sweep, x, pattern, load, fs, f"{m_:.2f}", f"{p_:.2f}", dm, dp,
                        f"{rtx_:.3f}", f"{nack_:.3f}",
                        f"{r_rtx:.3f}" if r_rtx is not None else "",
                        f"{r_nack:.3f}" if r_nack is not None else ""])
    print(f"\nWrote {OUTDIR / 'calibration.csv'}")

    print_calibration_report(results, ref)
    try:
        make_calibration_plots(results, ref)
    except ImportError:
        print("\nmatplotlib not installed -- skipped plots")


def print_calibration_report(results, ref):
    print("\n" + "=" * 100)
    print("CALIBRATION: FCT delta vs no-partition reference, by parameter value "
          "(rtx/nack shown as absolute %, plus the no-partition reference's own rtx/nack "
          "in brackets, since a rate near a near-zero reference makes a %-delta misleading)")
    print("=" * 100)
    for sweep_name, title in [("threshold", "escalate-threshold (full grid, 500KB)"),
                               ("warmup_rtts", "warmup RTT multiple (full grid, 500KB, threshold=0.2)"),
                               ("explore_prob", "explore-prob vs load (threshold=0.2, warmup=1RTT)")]:
        print(f"\n-- {title} --")
        keys = sorted(k for k in results if k[0] == sweep_name)
        for sweep, x, pattern, load, fs in keys:
            vals = results[(sweep, x, pattern, load, fs)]
            m_ = mean(v[0] for v in vals); p_ = mean(v[1] for v in vals)
            rtx_ = mean(v[2] for v in vals); nack_ = mean(v[3] for v in vals)
            rm, rp, r_rtx, r_nack = ref.get((pattern, load, fs), (None, None, None, None))
            if rm:
                dm, dp = 100*(m_-rm)/rm, 100*(p_-rp)/rp
                print(f"  {pattern:12s} load={load:.2f} x={x:<6} mean={m_:8.1f}({dm:+6.1f}%) "
                      f"p99={p_:8.1f}({dp:+6.1f}%)  rtx={rtx_:5.2f}%[ref {r_rtx:5.2f}%]  "
                      f"nack={nack_:5.2f}%[ref {r_nack:5.2f}%]")


def plot_param_vs_load(sweep_name, values, title_prefix, fname_prefix, results, ref):
    import matplotlib.pyplot as plt

    for pattern, loads in CALIBRATE_LOADS.items():
        keys = [k for k in results if k[0] == sweep_name and k[2] == pattern]
        if not keys:
            continue
        fig, axes = plt.subplots(2, 2, figsize=(11, 8.5), dpi=150)
        for val in values:
            dms, dps, rtxs, nacks = [], [], [], []
            for load in loads:
                k = (sweep_name, val, pattern, load, CALIBRATE_FS)
                if k not in results:
                    dms.append(None); dps.append(None); rtxs.append(None); nacks.append(None)
                    continue
                vals_ = results[k]
                m_ = mean(v[0] for v in vals_); p_ = mean(v[1] for v in vals_)
                rtx_ = mean(v[2] for v in vals_); nack_ = mean(v[3] for v in vals_)
                rm, rp, r_rtx, r_nack = ref.get((pattern, load, CALIBRATE_FS), (None, None, None, None))
                dms.append(100*(m_-rm)/rm if rm else None)
                dps.append(100*(p_-rp)/rp if rp else None)
                rtxs.append(rtx_)
                nacks.append(nack_)
            axes[0][0].plot(loads, dms, marker="o", label=f"{sweep_name}={val}")
            axes[0][1].plot(loads, dps, marker="o", label=f"{sweep_name}={val}")
            axes[1][0].plot(loads, rtxs, marker="s", label=f"{sweep_name}={val}")
            axes[1][1].plot(loads, nacks, marker="s", label=f"{sweep_name}={val}")
        ref_rtx = [ref.get((pattern, load, CALIBRATE_FS), (None,)*4)[2] for load in loads]
        ref_nack = [ref.get((pattern, load, CALIBRATE_FS), (None,)*4)[3] for load in loads]
        axes[1][0].plot(loads, ref_rtx, marker="x", linestyle="--", color="black", label="no-partition ref")
        axes[1][1].plot(loads, ref_nack, marker="x", linestyle="--", color="black", label="no-partition ref")

        for ax, title in zip(axes[0], ["mean FCT delta %", "p99 FCT delta %"]):
            ax.axhline(0, color="gray", linewidth=1, linestyle="--")
            ax.set_xlabel("load fraction"); ax.set_ylabel(title)
        for ax, title in zip(axes[1], ["rtx rate (%, absolute)", "nack rate (%, absolute)"]):
            ax.set_xlabel("load fraction"); ax.set_ylabel(title)
        for ax in axes.flat:
            ax.grid(True, linewidth=0.5, alpha=0.3)
            ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
        axes[0][0].legend(frameon=False, fontsize=7, ncol=2)
        fig.suptitle(f"{title_prefix} -- pattern={pattern} ({CALIBRATE_FS}B flows)", fontsize=11)
        fig.tight_layout()
        out = PLOT_DIR / f"{fname_prefix}_{pattern}.png"
        fig.savefig(out, bbox_inches="tight")
        plt.close(fig)
        print(f"wrote {out}")


def make_calibration_plots(results, ref):
    plot_param_vs_load("threshold", THRESHOLD_VALUES, "escalate-threshold vs load",
                        "calib_threshold", results, ref)
    plot_param_vs_load("warmup_rtts", WARMUP_RTT_VALUES, "warmup RTTs vs load",
                        "calib_warmup", results, ref)
    plot_param_vs_load("explore_prob", PROB_VALUES, "explore_prob vs load",
                        "calib_explore_prob", results, ref)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["compare", "calibrate"], nargs="?", default="compare")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.mode == "compare":
        run_compare(args.dry_run)
    else:
        run_calibrate(args.dry_run)


if __name__ == "__main__":
    main()
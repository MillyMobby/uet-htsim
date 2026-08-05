#!/usr/bin/env python3
"""Isolates the effect of hop-count RTT normalization on reps_dfp+reps.

Compares exactly two configurations, nothing else, so the comparison isn't
sharing plot/legend space with unrelated axes:
    1) reps_dfp + reps, hop-count RTT normalization OFF (-disable_hop_rtt_normalization)
    2) reps_dfp + reps, hop-count RTT normalization ON  (the default for reps_dfp)

Why this needs its own scenario set instead of reusing the 4-config sweep's
tornado-at-full-load runs: hop-count normalization corrects for the RTT
difference between minimal and non-minimal path lengths. Under tornado at
high load, retransmit rates run 19-35%+ and RTT is dominated by queueing
delay from real congestion, which swamps the (much smaller, fixed) hop-count
offset the normalization is trying to correct for -- so hop-norm on/off come
out statistically indistinguishable there (measured: <1% difference in
fct_mean across the board, smaller than seed noise). To actually see the
mechanism's effect you want less queueing noise in the RTT signal:
  - LOWER load (less sustained congestion drowning the hop-count signal)
  - permutation traffic (not every path uniformly saturated, unlike tornado)
  - smaller flows (RTT-bound early in the flow, before steady-state
    congestion dynamics dominate FCT)
This script's TRAFFIC_PATTERNS/FLOWSIZES are shifted accordingly. If hop-norm
still shows no effect here, that's a real finding, not a methodology gap.

    python3 sweep_hopnorm_isolation.py            # generate CMs, run sweep, write tables
    python3 sweep_hopnorm_isolation.py --dry-run  # just print the planned runs

No plotting -- output is the CSV plus a markdown comparison table
(hopnorm_comparison.md) with exact values and a %diff column per metric per
pattern, since the effect size here is often under 1% and unreadable on a
line plot.
"""
import argparse
import csv
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

# CONFIGURATION 
BINARY = "./htsim_uec_dfp"

# ---- Topology -------------------------------------------------------------
TOPO_SIZE = "m"            # 's' | 'm' | 'l'
NODES = 3600                # exact multiple of p*l so
                             # tornado's group pairing is a clean bijection --
                             # see the p/l note under TRAFFIC_PATTERNS.

# Optional explicit Dragonfly+ radix parameters. 
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

# ---- Configurations to compare: (label, -strat, -load_balancing_algo, extra CLI flags) --
STRATEGIES = [
        # label,                            -strat,     -load_balancing_algo, extra flags

    ("reps_dfp_no_hopnorm",   "reps_dfp", "reps", ["-disable_hop_rtt_normalization",
                                                              "-reps_partition_entropy"]),
    ("reps_dfp_hopnorm",      "reps_dfp", "reps", ["-reps_partition_entropy"]),
]

STRATEGY_COLORS = {
    "reps_dfp_no_hopnorm": "#D55E00",  
    "reps_dfp_hopnorm":    "#009E73",  
}

BASELINE_LABEL = "reps_dfp_no_hopnorm"

# ---- Traffic patterns to sweep ---------------------------------------------
# Shifted toward lighter load and permutation (see module docstring): this is
# where hop-count RTT normalization's effect has room to show up above the
# queueing-delay noise floor, unlike tornado-at-saturation.

TRAFFIC_PATTERNS = {
    #"permutation": {"kind": "file", "script": "gen_permutation_full_bisection.py",
    #                 "extra_args": [], "loads": [0.20, 0.40, 0.60, 0.80]},
    "tornado":     {"kind": "native", "loads": [0.10, 0.25, 0.50, 0.80, 0.90]},
}

SEEDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

FLOWSIZES = [
    #20_000,
    100_000,
    500_000,
    2_000_000,
]
EXTRA_START_US = 0.0

RUN_TIMEOUT_S = 800

OUTDIR = Path("sweep_hopnorm_isolation3600nodes")


# Implementation 

CM_DIR = OUTDIR / "connection_matrices"
RUN_DIR = OUTDIR / "runs"
GEN_ROOT = Path("connection_matrices")

FCT_RE = re.compile(r"finished at ([\d.]+)")
SUMMARY_RE = re.compile(
    r"New: (\d+) Rtx: (\d+) RTS: (\d+) Bounced: (\d+) ACKs: (\d+) "
    r"NACKs: (\d+) Pulls: (\d+) sleek_pkts: (\d+)"
)


# --------------------------------------------------------------- HELPERS ---
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
            continue  # native patterns (tornado) are generated by the binary itself
        for frac in spec["loads"]:
            for flowsize in FLOWSIZES:
                for seed in SEEDS:
                    ensure_cm_file(pattern, NODES, frac, flowsize, seed)


def build_cmd(pattern, frac, flowsize, seed, strat, lb_algo, extra_flags, datpath):
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
    cmd += extra_flags
    return cmd


def run_one(pattern, frac, flowsize, seed, label, strat, lb_algo, extra_flags):
    tag = f"{pattern}_{label}_load{frac:.2f}_fs{flowsize}_seed{seed}"
    logpath = RUN_DIR / f"{tag}.log"
    datpath = RUN_DIR / f"{tag}.dat"
    cmd = build_cmd(pattern, frac, flowsize, seed, strat, lb_algo, extra_flags, datpath)
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
    # The topology only auto-derives ALL FOUR radix params (s/l/h/p)
    # together, gated on _p == 0 (dragonfly_plus_topology.cpp). Setting
    # DF_P/DF_L explicitly while leaving DF_S/DF_H at 0 skips that
    # auto-derive entirely, leaving _s=_h=0 -- a degenerate topology that
    # crashes (SIGFPE, mod-by-zero) on essentially any traffic, independent
    # of routing strategy or traffic pattern.
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
    labels = [label for label, _, _, _ in STRATEGIES]
    if BASELINE_LABEL not in labels:
        print(f"ERROR: BASELINE_LABEL={BASELINE_LABEL!r} is not one of the "
              f"configured STRATEGIES labels {labels}.", file=sys.stderr)
        sys.exit(1)


def plan_runs():
    """Yield (pattern, frac, flowsize, seed, label, strat, lb_algo, extra_flags) for every run."""
    for pattern, spec in TRAFFIC_PATTERNS.items():
        for frac in spec["loads"]:
            for flowsize in FLOWSIZES:
                for seed in SEEDS:
                    for label, strat, lb_algo, extra_flags in STRATEGIES:
                        yield pattern, frac, flowsize, seed, label, strat, lb_algo, extra_flags


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                     help="print the planned runs without executing anything")
    args = ap.parse_args()

    validate_config()

    runs = list(plan_runs())
    if args.dry_run:
        print(f"{len(runs)} runs planned (topology={TOPO_SIZE}, nodes={NODES}):")
        for pattern, frac, flowsize, seed, label, strat, lb_algo, extra_flags in runs:
            extra_desc = " ".join(extra_flags) if extra_flags else "(none)"
            print(f"  pattern={pattern:12s} load={frac:.2f} flowsize={flowsize:>10d} seed={seed} "
                  f"strat={label:24s} (-strat {strat} -load_balancing_algo {lb_algo} {extra_desc})")
        return

    if not Path(BINARY).exists():
        print(f"ERROR: {BINARY} not found. Build htsim_uec_dfp first "
              f"(cmake --build build --target htsim_uec_dfp) and run this "
              f"script from sim/datacenter/.", file=sys.stderr)
        sys.exit(1)

    ensure_all_cm_files()
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    rows = []
    failures = []
    total = len(runs)
    for done, (pattern, frac, flowsize, seed, label, strat, lb_algo, extra_flags) in enumerate(runs, 1):
        logpath, rc = run_one(pattern, frac, flowsize, seed, label, strat, lb_algo, extra_flags)
        parsed = parse_log(logpath)
        status = "OK" if parsed else f"FAILED (rc={rc})"
        print(f"[{done}/{total}] {logpath.stem}: {status}")
        if parsed:
            rows.append({
                "pattern": pattern, "nodes": NODES, "size": TOPO_SIZE,
                "strat_label": label, "strat": strat,
                "load_balancing_algo": lb_algo,
                "extra_flags": " ".join(extra_flags),
                "load_fraction": frac, "flowsize": flowsize, "seed": seed, **parsed,
            })
        else:
            failures.append((logpath.stem, rc))

    OUTDIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUTDIR / "summary.csv"
    fieldnames = ["pattern", "nodes", "size", "strat_label", "strat",
                  "load_balancing_algo", "extra_flags", "load_fraction", "flowsize", "seed",
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
    write_comparison_tables(rows)


# Metrics shown in the comparison tables, and how to format each value.
TABLE_METRICS = [
    # (csv_field, column header, value format, diff format)
    ("fct_mean", "fct_mean (us)", "{:.2f}", "{:+.2f}%"),
    ("fct_p99", "fct_p99 (us)", "{:.2f}", "{:+.2f}%"),
    ("rtx_rate_pct", "rtx (%)", "{:.2f}", "{:+.2f}%"),
]


def write_comparison_tables(rows):
    """Plain-text markdown tables of hop-norm ON vs OFF, one per metric per
    pattern -- exact values and a %diff column, since a near-1% difference is
    unreadable on a line plot but perfectly readable as a number."""
    labels = [label for label, _, _, _ in STRATEGIES]
    other_label = next(l for l in labels if l != BASELINE_LABEL)

    groups = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = (r["pattern"], r["load_fraction"], r["flowsize"])
        groups[key][r["strat_label"]].append(r)

    by_pattern = defaultdict(list)
    for key in groups:
        by_pattern[key[0]].append(key)

    lines = ["# Hop-count RTT normalization: OFF vs ON", ""]
    for field, header, val_fmt, diff_fmt in TABLE_METRICS:
        lines.append(f"## {header}")
        for pattern in sorted(by_pattern):
            lines.append(f"\n### pattern={pattern}\n")
            lines.append(f"| load | flowsize | {BASELINE_LABEL} | {other_label} | diff |")
            lines.append("|---|---|---|---|---|")
            for key in sorted(by_pattern[pattern]):
                _, frac, flowsize = key
                by_label = groups[key]
                if BASELINE_LABEL not in by_label or other_label not in by_label:
                    continue
                base_vals = [r[field] for r in by_label[BASELINE_LABEL]]
                other_vals = [r[field] for r in by_label[other_label]]
                base_mean = sum(base_vals) / len(base_vals)
                other_mean = sum(other_vals) / len(other_vals)
                diff = (float("nan") if base_mean == 0
                        else 100.0 * (other_mean - base_mean) / base_mean)
                lines.append(
                    f"| {frac:.2f} | {flowsize} | {val_fmt.format(base_mean)} | "
                    f"{val_fmt.format(other_mean)} | {diff_fmt.format(diff)} |")
        lines.append("")

    table_path = OUTDIR / "hopnorm_comparison.md"
    table_path.write_text("\n".join(lines))
    print(f"\nWrote comparison tables to {table_path}")
    print("\n" + "\n".join(lines))


def print_report(rows):
    groups = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = (r["pattern"], r["load_fraction"], r["flowsize"])
        groups[key][r["strat_label"]].append(r)

    strat_desc = ", ".join(
        f"{label}(-strat {strat} -load_balancing_algo {lb_algo}"
        f"{' ' + ' '.join(extra) if extra else ''})"
        for label, strat, lb_algo, extra in STRATEGIES)
    print("\n" + "=" * 110)
    print(f"SUMMARY (mean across seeds)  --  nodes={NODES} size={TOPO_SIZE}")
    print(f"configurations: {strat_desc}")
    print(f"deltas below are vs baseline={BASELINE_LABEL!r}")
    print("=" * 110)
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
            print(f"  {label:24s} fct_mean={agg['fct_mean']:9.2f}  fct_p99={agg['fct_p99']:9.2f}  "
                  f"rtx={agg['rtx_rate_pct']:5.2f}%  rts={agg['rts_rate_pct']:5.2f}%  "
                  f"nack={agg['nack_rate_pct']:5.2f}%  n_seeds={n:2d}  per_seed={per_seed}")

        # Delta of every non-baseline config vs BASELINE_LABEL, when both
        # are present for this key.
        if BASELINE_LABEL in by_label:
            base = by_label[BASELINE_LABEL]

            def delta(agg, field):
                if base[field] == 0:
                    return float("nan")
                return 100.0 * (agg[field] - base[field]) / base[field]

            for label, agg in by_label.items():
                if label == BASELINE_LABEL:
                    continue
                print(f"  delta ({label} vs {BASELINE_LABEL}): "
                      f"fct_mean={delta(agg,'fct_mean'):+6.1f}%  "
                      f"fct_p99={delta(agg,'fct_p99'):+6.1f}%  "
                      f"rtx={delta(agg,'rtx_rate_pct'):+6.1f}%")


if __name__ == "__main__":
    main()
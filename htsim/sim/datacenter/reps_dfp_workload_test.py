#!/usr/bin/env python3
""" REPS vs oblivious sweep.
Generates any missing connection_matrices/load_sweep/load<frac>_n<N>_seed<S>.cm files
automatically, then runs the simulator and prints a report grouped by load fraction.
"""
import csv
import re
import subprocess
from collections import defaultdict
from pathlib import Path

# CONFIG 
BINARY = "./htsim_uec_dfp"
OUTDIR = Path("reps_dfp_results/reps_512m")
CM_DIR = Path("connection_matrices/load_sweep")
GENERATOR = Path("connection_matrices/gen_permutation_full_bisection.py")

NODES = 512
SIZE = "m"
STRAT = "fpar"           # or "minimal" 
END_TIME_US = 100000
Q = 50
RUN_TIMEOUT_S = 600      # oblivious near saturation can be slow

FLOWSIZE = 2_000_000     # bytes
EXTRA_START = 0.0

LOAD_FRACTIONS = [0.60, 0.65, 0.75, 0.85, 0.90]
ALGOS = ["reps", "oblivious"]
SEEDS = list(range(1, 6)) 
EXTRA_FLAGS = []  #  ["-reps_buffer_size", "32" ...]

FCT_RE = re.compile(r"finished at ([\d.]+)")
SUMMARY_RE = re.compile(r"New: \d+ Rtx: (\d+) RTS: \d+ Bounced: \d+ ACKs: \d+ NACKs: (\d+)")


def ensure_cm_files():
    CM_DIR.mkdir(parents=True, exist_ok=True)
    for frac in LOAD_FRACTIONS:
        conns = max(1, round(NODES * frac))
        for seed in SEEDS:
            fname = CM_DIR / f"load{frac:.2f}_n{NODES}_seed{seed}.cm"
            if fname.exists():
                continue
            cmd = ["python3", str(GENERATOR), str(fname), str(NODES), str(conns),
                   str(FLOWSIZE), str(EXTRA_START), str(seed)]
            print(" ".join(cmd))
            subprocess.run(cmd, check=True)


def run_one(frac, algo, seed):
    cmfile = CM_DIR / f"load{frac:.2f}_n{NODES}_seed{seed}.cm"
    tag = f"load{frac:.2f}_{algo}_seed{seed}"
    logpath = OUTDIR / f"{tag}.log"
    datpath = OUTDIR / f"{tag}.dat"
    cmd = [
        BINARY,
        "-tm", str(cmfile),
        "-load_balancing_algo", algo,
        "-size", SIZE,
        "-nodes", str(NODES),
        "-strat", STRAT,
        "-q", str(Q),
        "-end", str(END_TIME_US),
        "-seed", str(seed),
        "-o", str(datpath),
        *EXTRA_FLAGS,
    ]
    with open(logpath, "w") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                                timeout=RUN_TIMEOUT_S)
    return logpath, result.returncode


def parse_log(logpath):
    text = logpath.read_text()
    if "Done" not in text:
        return None
    fcts = [float(x) for x in FCT_RE.findall(text)]
    m = SUMMARY_RE.search(text)
    rtx, nacks = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    if not fcts:
        return None
    return {
        "n_flows": len(fcts),
        "fct_mean": sum(fcts) / len(fcts),
        "fct_max": max(fcts),
        "fct_min": min(fcts),
        "rtx": rtx,
        "nacks": nacks,
    }


def main():
    ensure_cm_files()
    OUTDIR.mkdir(parents=True, exist_ok=True)
    rows = []
    total = len(LOAD_FRACTIONS) * len(ALGOS) * len(SEEDS)
    done = 0
    for frac in LOAD_FRACTIONS:
        for algo in ALGOS:
            for seed in SEEDS:
                done += 1
                tag = f"load{frac:.2f}_{algo}_seed{seed}"
                logpath = OUTDIR / f"{tag}.log"
                if logpath.exists():
                    parsed = parse_log(logpath)
                    if parsed:
                        print(f"[{done}/{total}] {tag}: SKIP (already done)")
                        rows.append({
                            "load_fraction": frac, "nodes": NODES, "size": SIZE,
                            "algo": algo, "strat": STRAT, "seed": seed,
                            **parsed,
                        })
                        continue
                logpath, rc = run_one(frac, algo, seed)
                parsed = parse_log(logpath)
                print(f"[{done}/{total}] {logpath.stem}: {'OK' if parsed else 'FAILED'}")
                if parsed:
                    rows.append({
                        "load_fraction": frac, "nodes": NODES, "size": SIZE,
                        "algo": algo, "strat": STRAT, "seed": seed,
                        **parsed,
                    })

    csv_path = OUTDIR / "summary.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "load_fraction", "nodes", "size", "algo", "strat", "seed",
            "n_flows", "fct_mean", "fct_max", "fct_min", "rtx", "nacks",
        ])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} rows to {csv_path}")

    print_report(rows)


def print_report(rows):
    groups = defaultdict(lambda: defaultdict(list))
    for r in rows:
        groups[r["load_fraction"]][r["algo"]].append(r)

    print("\n" + "=" * 78)
    print(f"SUMMARY (mean across seeds) -- nodes={NODES}, strat={STRAT}")
    print("=" * 78)
    for frac in sorted(groups):
        print(f"\n== load_fraction={frac:.2f} ==")
        for algo in ["reps", "reps_legacy", "oblivious"]:
            runs = groups[frac].get(algo)
            if not runs:
                continue
            n = len(runs)
            fct_mean = sum(r["fct_mean"] for r in runs) / n
            fct_max  = sum(r["fct_max"]  for r in runs) / n
            rtx_mean = sum(r["rtx"]      for r in runs) / n
            per_seed = [round(r["fct_mean"], 1)
                        for r in sorted(runs, key=lambda x: x["seed"])]
            print(f"  {algo:12s} fct_mean={fct_mean:9.2f}  fct_max={fct_max:9.2f}  "
                  f"rtx_mean={rtx_mean:9.1f}  n_seeds={n:2d}  per_seed={per_seed}")


if __name__ == "__main__":
    main()
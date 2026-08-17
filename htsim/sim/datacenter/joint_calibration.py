#!/usr/bin/env python3
"""Joint calibration of the REPS entropy-partition parameters, done properly.

WHY THIS EXISTS
The original values (escalate_threshold=0.2, warmup=1.0 RTT, explore_prob
40/70 schedule) were arrived at incrementally: each parameter was added to
patch the previous configuration's failure, and each was swept ONE AT A
TIME with the others held at whatever value they happened to have. Three
consequences:
  - Sequential sweeps cannot see interaction. Measured example: warmup's
    best value is 4.0 when explore_prob=0, but 0 when explore_prob=70.
    Neither sweep alone can tell you that.
  - explore_prob was calibrated at a single flowsize (500KB) and then
    applied at four. A later candidate chosen the same way (warmup=0,
    prob=70) looked best at 500KB and turned out +20.2% worst-case once
    100KB was included.
  - The resulting values are not the optimum of any single experiment, so
    the calibration's own per-parameter optima (0.1 / 4.0 / 70) disagree
    with what is shipped (0.2 / 1.0 / 40-70).

METHOD (fixed before any results are seen)

1. DIMENSIONALITY REDUCTION, JUSTIFIED BY MEASUREMENT.
   escalate_threshold is pinned at 0.2. Justification: across its full
   tested range (0.1-0.5) it moved mean FCT by ~3pp on spread traffic and
   was monotone-but-shallow on concentrated traffic -- i.e. measurably the
   flattest of the three. Pinning it is an evidence-based reduction, not a
   convenience. The remaining search is the 2-D grid where the parameters
   actually interact.

2. JOINT GRID over warmup_rtts x explore_prob (5 x 4 = 20 configurations).
   Every configuration is evaluated on every calibration cell; no
   parameter is ever held at another's "previously chosen" value.

3. CALIBRATION SET SPANS THE DEPLOYMENT SPACE.
   Patterns: tornado (concentrated, 1.0 dst groups per src group) and
   permutation_random (spread, 9.8). These bracket the group-locality
   range that determines the optimal minimal-path share, so the
   calibration cannot be fitted to one regime.
   Flowsizes: 100KB and 2MB -- one short (finishes near the first RTT,
   where warmup dominates) and one long (where recycling dominates).
   Loads: all four per pattern.

4. SELECTION CRITERION, PRE-REGISTERED.
       PRIMARY: minimise mean FCT delta vs plain REPS across all
                calibration cells, SUBJECT TO worst-case cell <= +5%.
                If no configuration satisfies the constraint, the
                constraint is reported as unmet and the minimum-worst-case
                configuration is selected instead (this fallback is fixed
                in advance too).
   Other metrics (p99, rtx, win/loss counts) are computed and reported for
   transparency and for the sensitivity discussion, but are NOT used to
   select. This is stated here so the criterion cannot be chosen after
   seeing which one flatters a preferred answer.

5. HELD-OUT VALIDATION on cells never used for calibration:
       - the entire third pattern (permutation, strided: 2.0 dst groups --
         an intermediate locality the calibration never saw), all flowsizes
       - the calibration patterns at the two UNUSED flowsizes (500KB, 10MB)
   The selected configuration is compared there against the incumbent
   (warmup=1.0, explore_prob=40) and against plain REPS. A configuration
   that wins in calibration but loses on held-out cells is overfitted, and
   this is the step that detects it.

6. SENSITIVITY REPORTED, not just the winner: the full 20-point grid is
   printed, so a flat optimum (choice is low-risk) can be distinguished
   from a sharp one (choice is fragile).

USAGE
    python3 joint_calibration.py calibrate      # phase 1, writes winner.txt
    python3 joint_calibration.py validate       # phase 2, reads winner.txt
    python3 joint_calibration.py calibrate --dry-run

Phase 2 refuses to run before phase 1 has written its winner, so the
selection is committed before the held-out data is ever touched.

Run from htsim/sim/datacenter. Resumable: completed runs are skipped.
"""
import argparse
import csv
import json
import re
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import mean

# ---------------------------------------------------------------- config ---
BINARY = Path("htsim_uec_dfp_reps").resolve()
OUTDIR = Path("joint_calibration")
CM_DIR = OUTDIR / "connection_matrices"
RUN_DIR = OUTDIR / "runs"
GEN_ROOT = Path("connection_matrices")
WINNER_FILE = OUTDIR / "winner.json"

NODES, Q_PKTS, END, MTU = 1100, 50, 100000, 4150
SEEDS = [1, 2, 3]
WORKERS_DEFAULT = 4
RUN_TIMEOUT_S = 1800

ESCALATE_FIXED = "0.2"                 # step 1: pinned, justified above
WARMUPS = [0, 0.5, 1, 2, 4]            # step 2: joint grid
PROBS = [0, 20, 40, 70]
INCUMBENT = (1, 40)                    # currently shipped, for comparison

PATTERNS = {
    "tornado":            {"kind": "native", "loads": [0.25, 0.50, 0.75, 1.00]},
    "permutation_random": {"kind": "file", "script": "gen_permutation.py",
                            "loads": [0.60, 0.70, 0.80, 0.90]},
    "permutation":        {"kind": "file", "script": "gen_permutation_full_bisection.py",
                            "loads": [0.60, 0.70, 0.80, 0.90]},
}
CALIB_PATTERNS = ["tornado", "permutation_random"]   # step 3
CALIB_FLOWSIZES = [100_000, 2_000_000]
HELDOUT_PATTERN = "permutation"                       # step 5
HELDOUT_FLOWSIZES = [500_000, 10_000_000]

WORST_CASE_LIMIT = 5.0                                # step 4

FCT_RE = re.compile(r"finished at ([\d.]+)")
SUM_RE = re.compile(r"New: (\d+) Rtx: (\d+)")


DRY = False          # set from --dry-run; suppresses .cm generation during counting


# --------------------------------------------------------------- helpers ---
def cm_path(pattern, load, fs, seed):
    p = CM_DIR / f"{pattern}_load{load:.2f}_n{NODES}_fs{fs}_seed{seed}.cm"
    if not DRY and not p.exists():
        CM_DIR.mkdir(parents=True, exist_ok=True)
        conns = max(1, round(NODES * load))
        subprocess.run(["python3", str(GEN_ROOT / PATTERNS[pattern]["script"]), str(p),
                        str(NODES), str(conns), str(fs), "0.0", str(seed)],
                       check=True, capture_output=True)
    return p


def build_cmd(pattern, load, fs, seed, extra, tag):
    cmd = [str(BINARY), "-load_balancing_algo", "reps", "-size", "m", "-nodes", str(NODES),
           "-strat", "reps_dfp", "-q", str(Q_PKTS), "-end", str(END), "-seed", str(seed),
           "-o", str(RUN_DIR / f"{tag}.dat")] + extra
    if PATTERNS[pattern]["kind"] == "native":
        cmd += ["-tornado", "-tornado_conns", str(max(1, round(NODES * load))),
                "-tornado_flowsize", str(fs)]
    else:
        cmd += ["-tm", str(cm_path(pattern, load, fs, seed))]
    return cmd


def cfg_flags(warmup, prob):
    if warmup is None:                       # plain REPS baseline
        return []
    return ["-reps_partition_entropy", "-reps_escalate_threshold", ESCALATE_FIXED,
            "-reps_warmup_explore_rtts", str(warmup), "-reps_explore_prob", str(prob)]


def run_one(tag, cmd):
    log = RUN_DIR / f"{tag}.log"
    if not log.exists() or "Done" not in log.read_text(errors="ignore"):
        try:
            with open(log, "w") as fh:
                subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, timeout=RUN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            print(f"  {tag}: TIMEOUT", flush=True)
            return None
        except Exception as e:                       # never kill the whole sweep for one run
            print(f"  {tag}: FAILED ({e})", flush=True)
            return None
    dat = RUN_DIR / f"{tag}.dat"
    if dat.exists():
        dat.unlink()
    t = log.read_text(errors="ignore")
    v = sorted(float(x) for x in FCT_RE.findall(t))
    m = SUM_RE.search(t)
    if not v or not m:
        print(f"  {tag}: NODATA", flush=True)
        return None
    new, rtx = int(m.group(1)), int(m.group(2))
    return dict(mean=sum(v) / len(v),
                p99=v[int(len(v) * .99)] if len(v) >= 100 else v[-1],
                rtx=100.0 * rtx / new if new else 0.0)


def execute(jobs, workers):
    print(f"{len(jobs)} runs", flush=True)
    done = [0]

    def work(j):
        key, tag, cmd = j
        r = run_one(tag, cmd)
        done[0] += 1
        if done[0] % 50 == 0:
            print(f"  ... {done[0]}/{len(jobs)}", flush=True)
        return (key, r)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(work, jobs))


def cells(patterns, flowsizes):
    for pat in patterns:
        for load in PATTERNS[pat]["loads"]:
            for fs in flowsizes:
                yield (pat, load, fs)


def deltas(results, cell_list, warmup, prob):
    """mean-FCT delta vs plain REPS per cell, for one configuration."""
    out = {}
    for (pat, load, fs) in cell_list:
        b = results.get(("base", pat, load, fs))
        v = results.get(((warmup, prob), pat, load, fs))
        if b and v:
            bm = mean(x["mean"] for x in b)
            out[(pat, load, fs)] = 100 * (mean(x["mean"] for x in v) - bm) / bm
    return out


# ------------------------------------------------------------- phase one ---
def phase_calibrate(dry_run, workers):
    cl = list(cells(CALIB_PATTERNS, CALIB_FLOWSIZES))
    jobs = []
    for (pat, load, fs) in cl:
        for seed in SEEDS:
            jobs.append((("base", pat, load, fs), f"base_{pat}_l{load:.2f}_fs{fs}_s{seed}",
                         build_cmd(pat, load, fs, seed, cfg_flags(None, None),
                                   f"base_{pat}_l{load:.2f}_fs{fs}_s{seed}")))
            for w in WARMUPS:
                for p in PROBS:
                    tag = f"w{w}_p{p}_{pat}_l{load:.2f}_fs{fs}_s{seed}"
                    jobs.append((((w, p), pat, load, fs), tag,
                                 build_cmd(pat, load, fs, seed, cfg_flags(w, p), tag)))
    if dry_run:
        print(f"CALIBRATE: {len(jobs)} runs over {len(cl)} cells x "
              f"{len(WARMUPS)*len(PROBS)} configs + baseline")
        return

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    CM_DIR.mkdir(parents=True, exist_ok=True)
    rows = execute(jobs, workers)

    res = defaultdict(list)
    for key, r in rows:
        if r:
            res[key].append(r)

    with open(OUTDIR / "calibration_grid.csv", "w", newline="") as fh:
        w_ = csv.writer(fh)
        w_.writerow(["warmup", "explore_prob", "pattern", "load", "flowsize",
                     "mean_delta_pct", "p99_delta_pct", "rtx_pct"])
        for w in WARMUPS:
            for p in PROBS:
                for (pat, load, fs) in cl:
                    b = res.get(("base", pat, load, fs)); v = res.get(((w, p), pat, load, fs))
                    if not b or not v:
                        continue
                    bm = mean(x["mean"] for x in b); bp = mean(x["p99"] for x in b)
                    w_.writerow([w, p, pat, load, fs,
                                 f"{100*(mean(x['mean'] for x in v)-bm)/bm:.3f}",
                                 f"{100*(mean(x['p99'] for x in v)-bp)/bp:.3f}",
                                 f"{mean(x['rtx'] for x in v):.3f}"])

    # ---- step 6: full sensitivity grid ----
    print("\n" + "=" * 78)
    print("SENSITIVITY: mean-FCT delta vs plain REPS over calibration cells")
    print("=" * 78)
    print(f"  {'warmup':>7} " + "".join(f"{f'p={p}':>12}" for p in PROBS))
    scores = {}
    for w in WARMUPS:
        line = f"  {w:>7} "
        for p in PROBS:
            d = deltas(res, cl, w, p)
            if d:
                scores[(w, p)] = (mean(d.values()), max(d.values()))
                line += f"{mean(d.values()):>11.2f}%"
            else:
                line += f"{'--':>12}"
        print(line)
    print(f"\n  worst-case cell per configuration (constraint: <= +{WORST_CASE_LIMIT}%)")
    print(f"  {'warmup':>7} " + "".join(f"{f'p={p}':>12}" for p in PROBS))
    for w in WARMUPS:
        print(f"  {w:>7} " + "".join(
            f"{scores[(w,p)][1]:>11.2f}%" if (w, p) in scores else f"{'--':>12}" for p in PROBS))

    # ---- step 4: pre-registered criterion, applied mechanically ----
    feasible = {k: v for k, v in scores.items() if v[1] <= WORST_CASE_LIMIT}
    if feasible:
        winner = min(feasible, key=lambda k: feasible[k][0])
        basis = f"min mean subject to worst <= +{WORST_CASE_LIMIT}%"
    else:
        winner = min(scores, key=lambda k: scores[k][1])
        basis = (f"CONSTRAINT UNMET by every configuration -- fallback applied: "
                 f"min worst-case")
    m_, wc = scores[winner]
    print("\n" + "=" * 78)
    print(f"SELECTED (criterion fixed before results): warmup={winner[0]} explore_prob={winner[1]}")
    print(f"  basis   : {basis}")
    print(f"  mean    : {m_:+.2f}%   worst-case: {wc:+.2f}%")
    print(f"  incumbent warmup={INCUMBENT[0]} explore_prob={INCUMBENT[1]}: "
          f"mean {scores.get(INCUMBENT,(float('nan'),))[0]:+.2f}%, "
          f"worst {scores.get(INCUMBENT,(0,float('nan')))[1]:+.2f}%")
    print("=" * 78)

    WINNER_FILE.write_text(json.dumps({"warmup": winner[0], "explore_prob": winner[1],
                                       "basis": basis, "calib_mean": m_, "calib_worst": wc}))
    print(f"\nwrote {WINNER_FILE} -- now run:  python3 {Path(__file__).name} validate")


# ------------------------------------------------------------- phase two ---
def phase_validate(dry_run, workers):
    if not WINNER_FILE.exists():
        if dry_run:      # counting only; no held-out data is read
            win = {"warmup": "W", "explore_prob": "P"}
            hl = list(cells([HELDOUT_PATTERN], CALIB_FLOWSIZES + HELDOUT_FLOWSIZES)) \
               + list(cells(CALIB_PATTERNS, HELDOUT_FLOWSIZES))
            print(f"VALIDATE: {len(hl)*len(SEEDS)*3} runs over {len(hl)} held-out cells "
                  "(baseline + selected + incumbent)")
            return
        print("ERROR: run the calibrate phase first -- the winner must be committed "
              "before held-out data is touched.", file=sys.stderr)
        sys.exit(1)
    win = json.loads(WINNER_FILE.read_text())
    W, P = win["warmup"], win["explore_prob"]

    hl = list(cells([HELDOUT_PATTERN], CALIB_FLOWSIZES + HELDOUT_FLOWSIZES)) \
       + list(cells(CALIB_PATTERNS, HELDOUT_FLOWSIZES))
    configs = {"selected": (W, P)}
    if (W, P) != INCUMBENT:
        configs["incumbent"] = INCUMBENT
    else:
        print("NOTE: the joint calibration reselected the currently shipped values "
              f"(warmup={W}, explore_prob={P}). Held-out validation still runs, "
              "but against plain REPS only.\n")

    jobs = []
    for (pat, load, fs) in hl:
        for seed in SEEDS:
            jobs.append((("base", pat, load, fs), f"base_{pat}_l{load:.2f}_fs{fs}_s{seed}",
                         build_cmd(pat, load, fs, seed, cfg_flags(None, None),
                                   f"base_{pat}_l{load:.2f}_fs{fs}_s{seed}")))
            for name, (w, p) in configs.items():
                tag = f"w{w}_p{p}_{pat}_l{load:.2f}_fs{fs}_s{seed}"
                jobs.append((((w, p), pat, load, fs), tag,
                             build_cmd(pat, load, fs, seed, cfg_flags(w, p), tag)))
    if dry_run:
        print(f"VALIDATE: {len(jobs)} runs over {len(hl)} held-out cells")
        return

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    rows = execute(jobs, workers)
    res = defaultdict(list)
    for key, r in rows:
        if r:
            res[key].append(r)

    print("\n" + "=" * 78)
    print(f"HELD-OUT VALIDATION  (selected warmup={W} prob={P} vs incumbent "
          f"warmup={INCUMBENT[0]} prob={INCUMBENT[1]})")
    print(f"held-out = pattern '{HELDOUT_PATTERN}' (all flowsizes) + calibration patterns "
          f"at {HELDOUT_FLOWSIZES}")
    print("=" * 78)
    out = {}
    for name, (w, p) in configs.items():
        d = deltas(res, hl, w, p)
        out[name] = d
        if d:
            print(f"  {name:10} mean={mean(d.values()):+.2f}%  worst={max(d.values()):+.2f}%  "
                  f"cells>+2%={sum(1 for x in d.values() if x>2)}  "
                  f"cells<-2%={sum(1 for x in d.values() if x<-2)}")
    a, b = out.get("selected", {}), out.get("incumbent", {})
    common = set(a) & set(b)
    if common:
        better = sum(1 for k in common if a[k] < b[k] - 1)
        worse = sum(1 for k in common if a[k] > b[k] + 1)
        print(f"\n  head-to-head on {len(common)} held-out cells: selected better at {better}, "
              f"worse at {worse}, within 1pp at {len(common)-better-worse}")
        if worse:
            print("  cells where the selected config is WORSE than the incumbent:")
            for k in sorted(common, key=lambda k: b[k]-a[k])[:10]:
                if a[k] > b[k] + 1:
                    print(f"    {k[0]:20} load={k[1]:.2f} fs={k[2]:>9}  "
                          f"incumbent={b[k]:+6.2f}%  selected={a[k]:+6.2f}%")
        print("\n  INTERPRETATION: if the selected config wins in calibration but not here, "
              "it is overfitted to the calibration cells and the incumbent should stand.")

    with open(OUTDIR / "validation.csv", "w", newline="") as fh:
        w_ = csv.writer(fh)
        w_.writerow(["config", "warmup", "explore_prob", "pattern", "load", "flowsize", "mean_delta_pct"])
        for name, (w, p) in configs.items():
            for k, v in out.get(name, {}).items():
                w_.writerow([name, w, p, k[0], k[1], k[2], f"{v:.3f}"])
    print(f"\nwrote {OUTDIR/'validation.csv'}")


def main():
    global DRY
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["calibrate", "validate"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=WORKERS_DEFAULT)
    a = ap.parse_args()
    DRY = a.dry_run
    if not a.dry_run and not BINARY.exists():
        print(f"ERROR: {BINARY} not found -- run from htsim/sim/datacenter", file=sys.stderr)
        sys.exit(1)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    (phase_calibrate if a.phase == "calibrate" else phase_validate)(a.dry_run, a.workers)


if __name__ == "__main__":
    main()
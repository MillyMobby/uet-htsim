#!/usr/bin/env python3
"""Per-hop-count RTT baselines ON vs OFF, across traffic patterns, loads,
flow sizes and the REPS entropy partition.

Structured after sweep_reps_partition_final.py -- same topology, patterns,
loads, flow sizes, seeds and report shape -- but the variable under test is
the hop-count RTT baseline (-disable_hop_rtt_normalization), not the routing
strategy. Four arms, a 2x2:

    partition_off / norm_on      reps_dfp + reps, baselines ON  (the default)
    partition_off / norm_off     reps_dfp + reps, baselines OFF
    partition_on  / norm_on      + -reps_partition_entropy
    partition_on  / norm_off     + -reps_partition_entropy

Deltas are always norm_on vs norm_off WITHIN a partition setting, so the
question the report answers is "what does the baseline buy, with and without
partitioning" rather than confounding the two.

Two things are measured, and they answer different questions:

  * The OUTCOME -- FCT mean/p99/max, makespan, retransmit and NACK rates.
    Same metric set as the partition sweep, so results are comparable.

  * The DIAGNOSTIC -- how the delay-based congestion signal distributes over
    forward path lengths (-debug_hops). A packet's expected RTT should depend
    on its own path length, so the rate at which the delay signal fires with
    no ECN corroboration ("delay-only", the likeliest false positives) should
    be roughly FLAT across hop counts. A sloped profile means the signal is
    reporting propagation as congestion. delay_only_spread (max-min across hop
    classes, in percentage points) is the headline number.

    The diagnostic is what makes this sweep more than an A/B: it says WHY a
    cell went the way it did. Disable it with --no-hop-diag if you only want
    the outcome.

Deviations from sweep_reps_partition_final.py, both forced by -debug_hops:

  * Runs are STREAM-PARSED; raw stdout is never written to disk. The heaviest
    cell (10MB flows, permutation load 0.90, 1100 nodes) emits 107MB of HOPDBG
    per run, and the full grid is ~1400 runs -- writing those logs would need
    ~100GB. Each run instead leaves a small {tag}.json.

  * Caching keys on that .json rather than grepping "Done" out of a log. The
    tag carries every parameter that affects the run, so changing one produces
    fresh runs instead of silently reusing old ones (the lesson behind
    partition_tag_suffix() in the partition sweep).

Timings measured on this grid: ~3s for a 500KB cell, ~70s for a 10MB
permutation cell at load 0.90. The full grid is ~1400 runs; use --jobs.

    python3 sweep_hop_rtt_normalization.py --dry-run
    python sweep_hop_rtt_normalization.py --jobs 8
    python3 sweep_hop_rtt_normalization.py --flowsizes 500000 --seeds 3   # quick pass
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import csv
import json
import math
import os
import random
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

# ---------------------------------------------------------------- CONFIGURATION

BINARY = "./htsim_uec_dfp_fin"

TOPO_SIZE = "m"
NODES = 1100
QUEUE_SIZE_PKTS = 50
END_TIME_US = 100000

TRAFFIC_PATTERNS = {
    # Group-locality matters here: tornado and permutation concentrate each
    # group's traffic onto 1-2 destination groups, so only a fraction of a
    # group's global egress links are minimal-eligible; permutation_random
    # spreads across ~all destination groups. The baseline's job -- judging a
    # packet against its own path length -- has more to do in the first regime,
    # where minimal and non-minimal paths coexist in volume.
    "tornado":            {"kind": "native", "loads": [0.25, 0.50, 0.75, 1.00]},
    "permutation":        {"kind": "file", "script": "gen_permutation_full_bisection.py",
                           "loads": [0.60, 0.70, 0.80, 0.90]},
    "permutation_random": {"kind": "file", "script": "gen_permutation.py",
                           "loads": [0.60, 0.70, 0.80, 0.90]},
}

SEEDS = [1, 2, 3, 4, 5]
FLOWSIZES = [100_000, 250_000, 500_000, 2_000_000, 5_000_000, 10_000_000]
EXTRA_START_US = 0.0
RUN_TIMEOUT_S = 1800

# (label, partition, hop-norm) -> extra flags
ARMS = [
    ("part_off_norm_on",  False, "on",  []),
    ("part_off_norm_off", False, "off", ["-disable_hop_rtt_normalization"]),
    ("part_on_norm_on",   True,  "on",  ["-reps_partition_entropy"]),
    ("part_on_norm_off",  True,  "off", ["-reps_partition_entropy",
                                         "-disable_hop_rtt_normalization"]),
]
# Within each partition setting, norm_off is the reference the delta is against.
PAIRS = [("part_off_norm_on", "part_off_norm_off", "partition OFF"),
         ("part_on_norm_on",  "part_on_norm_off",  "partition ON")]

HOP_CLASSES = [1, 3, 4, 5, 7]

METRICS = [
    ("duration_us",   "Total run duration (makespan)", "Duration (us)"),
    ("fct_mean",      "Mean FCT",                      "Mean FCT (us)"),
    ("fct_p99",       "p99 FCT",                       "p99 FCT (us)"),
    ("fct_max",       "Slowest flow's FCT",            "Time (us)"),
    ("rtx_rate_pct",  "Retransmit rate",               "Rtx / New (%)"),
    ("nack_rate_pct", "NACK rate",                     "NACKs / New (%)"),
    ("delay_only_spread", "Delay-signal spread across hop counts",
                          "max-min delay-only rate (pp)"),
]

# Composite encoding: hue carries the variable under test (hop norm), line style
# carries the partition setting. Keeps the palette at two validated categorical
# slots instead of seating four series. Light/print only -- thesis figures.
# Validated: CVD dE 24.7, normal-vision dE 33.6, both >= 3:1 on the surface.
C_ON, C_OFF = "#2a78d6", "#eb6834"
INK, INK_MUTED, GRID = "#0b0b0b", "#52514e", "#d9d8d4"
ARM_STYLE = {
    "part_off_norm_on":  (C_ON,  "-",  "Baselines ON, partition off"),
    "part_off_norm_off": (C_OFF, "-",  "Baselines OFF, partition off"),
    "part_on_norm_on":   (C_ON,  "--", "Baselines ON, partition on"),
    "part_on_norm_off":  (C_OFF, "--", "Baselines OFF, partition on"),
}

OUTDIR = Path("hopnorm_sweepin")
GEN_ROOT = Path("connection_matrices")


# -------------------------------------------------------------- workload set-up

def cm_path(cmdir: Path, pattern, nodes, frac, flowsize, seed) -> Path:
    return cmdir / f"{pattern}_load{frac:.2f}_n{nodes}_fs{flowsize}_seed{seed}.cm"


def ensure_cm_file(cmdir: Path, pattern, nodes, frac, flowsize, seed) -> Path:
    fname = cm_path(cmdir, pattern, nodes, frac, flowsize, seed)
    if fname.exists():
        return fname
    spec = TRAFFIC_PATTERNS[pattern]
    conns = max(1, round(nodes * frac))
    subprocess.run(["python3", str(GEN_ROOT / spec["script"]), str(fname), str(nodes),
                    str(conns), str(flowsize), str(EXTRA_START_US), str(seed)],
                   check=True, capture_output=True)
    return fname


def build_cmd(a, cmdir, pattern, frac, flowsize, seed, extra, datpath) -> list[str]:
    spec = TRAFFIC_PATTERNS[pattern]
    cmd = [a.binary, "-load_balancing_algo", "reps", "-size", a.size,
           "-nodes", str(a.nodes), "-strat", "reps_dfp", "-q", str(a.queue_pkts),
           "-end", str(a.end), "-seed", str(seed), "-o", str(datpath)]
    if spec["kind"] == "native":
        cmd += ["-tornado", "-tornado_conns", str(max(1, round(a.nodes * frac))),
                "-tornado_flowsize", str(flowsize)]
    else:
        cmd += ["-tm", str(cm_path(cmdir, pattern, a.nodes, frac, flowsize, seed))]
    if a.hop_diag:
        cmd += ["-debug_hops"]
    return cmd + extra


# ------------------------------------------------------------------ run + parse

def run_one(job: dict) -> dict:
    """Run one simulation, stream-parsing stdout so a 107MB HOPDBG dump never
    lands on disk. Returns the parsed metrics; caches them as {tag}.json."""
    cache = Path(job["rundir"]) / f"{job['tag']}.json"
    if cache.exists():
        try:
            r = json.loads(cache.read_text())
            if r.get("ok"):
                r["cached"] = True
                return r
        except (json.JSONDecodeError, OSError):
            pass

    fcts: list[float] = []
    counts = {k: 0 for k in ("new", "rtx", "rts", "bounced", "acks", "nacks",
                             "pulls", "sleek")}
    tot = {h: 0 for h in HOP_CLASSES}
    delay_only = {h: 0 for h in HOP_CLASSES}
    done = False

    try:
        proc = subprocess.Popen(job["cmd"], cwd=job["cwd"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1 << 20)
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.startswith("HOPDBG"):
                kv = dict(p.split("=", 1) for p in line.split()[1:] if "=" in p)
                try:
                    h = int(kv.get("hops", -1))
                except ValueError:
                    continue
                if h in tot:
                    tot[h] += 1
                    if kv.get("delay") == "1" and kv.get("ecn") == "0":
                        delay_only[h] += 1
            elif "finished at" in line:
                f = line.split()
                fcts.append(float(f[f.index("at") + 1]))
            elif line.startswith("New:"):
                f = line.split()
                for key, tok in (("new", "New:"), ("rtx", "Rtx:"), ("rts", "RTS:"),
                                 ("bounced", "Bounced:"), ("acks", "ACKs:"),
                                 ("nacks", "NACKs:"), ("pulls", "Pulls:"),
                                 ("sleek", "sleek_pkts:")):
                    if tok in f:
                        counts[key] = int(f[f.index(tok) + 1])
            elif line.startswith("Done") or line.startswith(".Done"):
                done = True
        rc = proc.wait(timeout=RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = -1
    except OSError as e:
        return {"ok": False, "tag": job["tag"], "error": str(e)}

    Path(job["datpath"]).unlink(missing_ok=True)

    if not fcts or rc != 0:
        out = {"ok": False, "tag": job["tag"], "returncode": rc,
               "n_flows": len(fcts), "done": done}
        cache.write_text(json.dumps(out))
        return out

    fcts.sort()
    n = len(fcts)
    new = counts["new"]
    pct = lambda x: 100.0 * x / new if new else 0.0
    out = {
        "ok": True, "tag": job["tag"], "n_flows": n,
        "duration_us": fcts[-1],
        "fct_mean": sum(fcts) / n,
        "fct_p50": fcts[n // 2],
        "fct_p99": fcts[int(n * 0.99)] if n >= 100 else fcts[-1],
        "fct_max": fcts[-1], "fct_min": fcts[0],
        **counts,
        "rtx_rate_pct": pct(counts["rtx"]), "rts_rate_pct": pct(counts["rts"]),
        "nack_rate_pct": pct(counts["nacks"]),
        "bounced_rate_pct": pct(counts["bounced"]),
    }
    for h in HOP_CLASSES:
        out[f"h{h}_n"] = tot[h]
        out[f"h{h}_delay_only_pct"] = (100.0 * delay_only[h] / tot[h]) if tot[h] else float("nan")
    seen = [out[f"h{h}_delay_only_pct"] for h in HOP_CLASSES if tot[h]]
    out["delay_only_spread"] = (max(seen) - min(seen)) if seen else float("nan")
    out["hop_samples"] = sum(tot.values())
    cache.write_text(json.dumps(out))
    return out


# ------------------------------------------------------------------- statistics

def wilcoxon_exact(diffs: list[float]) -> tuple[float, float]:
    d = [x for x in diffs if x != 0]
    n = len(d)
    if n == 0:
        return float("nan"), 1.0
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    w_plus = sum(r for r, x in zip(ranks, d) if x > 0)
    total = sum(ranks)
    w_obs = min(w_plus, total - w_plus)
    if n > 22:
        mu, sigma = total / 2, math.sqrt(sum(r * r for r in ranks) / 4)
        z = (w_obs - mu) / sigma if sigma else 0.0
        return w_obs, min(1.0, math.erfc(abs(z) / math.sqrt(2)))
    cnt = sum(1 for bits in range(1 << n)
              if min(s := sum(ranks[k] for k in range(n) if bits >> k & 1), total - s)
              <= w_obs + 1e-12)
    return w_obs, min(1.0, cnt / (1 << n))


def bootstrap_ci(xs: list[float], iters=10000, seed=0) -> tuple[float, float]:
    rng = random.Random(seed)
    ms = sorted(statistics.mean(rng.choices(xs, k=len(xs))) for _ in range(iters))
    return ms[int(0.025 * iters)], ms[int(0.975 * iters)]


# ----------------------------------------------------------------------- report

def cell_means(rows, keys):
    g = defaultdict(list)
    for r in rows:
        g[tuple(r[k] for k in keys)].append(r)
    return g


def print_report(rows, out):
    w = out.write
    by_cell = cell_means(rows, ["pattern", "load_fraction", "flowsize", "arm"])
    cells = sorted({k[:3] for k in by_cell})

    w("\n" + "=" * 118 + "\n")
    w(f"HOP-COUNT RTT BASELINES ON vs OFF   nodes={NODES} size={TOPO_SIZE}   "
      f"delta = norm_on relative to norm_off, within each partition setting\n")
    w("=" * 118 + "\n")

    deltas = defaultdict(lambda: defaultdict(list))
    win = defaultdict(int); tie = defaultdict(int); loss = defaultdict(int)

    for pattern, frac, fs in cells:
        w(f"\n== pattern={pattern} load={frac:.2f} flowsize={fs} ==\n")
        for arm, *_ in ARMS:
            rs = by_cell.get((pattern, frac, fs, arm))
            if not rs:
                continue
            w(f"  {arm:18s} mean={mean(r['fct_mean'] for r in rs):9.2f}  "
              f"p99={mean(r['fct_p99'] for r in rs):9.2f}  "
              f"dur={mean(r['duration_us'] for r in rs):9.2f}  "
              f"rtx={mean(r['rtx_rate_pct'] for r in rs):6.2f}%  "
              f"nack={mean(r['nack_rate_pct'] for r in rs):6.2f}%  "
              f"spread={mean(r['delay_only_spread'] for r in rs):5.2f}pp  "
              f"n={len(rs)}\n")
        for on_arm, off_arm, tag in PAIRS:
            a = by_cell.get((pattern, frac, fs, on_arm))
            b = by_cell.get((pattern, frac, fs, off_arm))
            if not a or not b:
                continue
            am, bm = mean(r["fct_mean"] for r in a), mean(r["fct_mean"] for r in b)
            ap, bp = mean(r["fct_p99"] for r in a), mean(r["fct_p99"] for r in b)
            dm = 100.0 * (am - bm) / bm if bm else float("nan")
            dp = 100.0 * (ap - bp) / bp if bp else float("nan")
            deltas[tag]["mean"].append(dm)
            deltas[tag]["p99"].append(dp)
            deltas[tag]["cell"].append(f"{pattern} load {frac:.2f} fs {fs}")
            deltas[tag]["spread_on"].append(mean(r["delay_only_spread"] for r in a))
            deltas[tag]["spread_off"].append(mean(r["delay_only_spread"] for r in b))
            if dm < -2 and dp < -2:
                win[tag] += 1
            elif dm > 2 or dp > 2:
                loss[tag] += 1
            else:
                tie[tag] += 1
            w(f"  delta baselines ON vs OFF [{tag}]: fct_mean={dm:+6.1f}%  fct_p99={dp:+6.1f}%\n")

    w("\n" + "=" * 118 + "\n")
    w("OVERALL (win = >2% better on BOTH mean and p99; loss = >2% worse on EITHER)\n")
    w("=" * 118 + "\n")
    for tag in (t for _, _, t in PAIRS):
        d = deltas[tag]
        if not d["mean"]:
            continue
        n = len(d["mean"])
        w(f"  {tag:16s} mean={mean(d['mean']):+6.1f}%  p99={mean(d['p99']):+6.1f}%   "
          f"spread ON={mean(d['spread_on']):5.2f}pp vs OFF={mean(d['spread_off']):5.2f}pp   "
          f"wins={win[tag]} ties={tie[tag]} losses={loss[tag]} (of {n})\n")

    # An average hides a setting that wins big on one workload and loses big on
    # another. A default has to survive its WORST cell, so print the extremes.
    w("\n" + "=" * 118 + "\n")
    w("BEST AND WORST SINGLE CELL (mean-FCT delta) -- a default must survive its worst cell\n")
    w("=" * 118 + "\n")
    for tag in (t for _, _, t in PAIRS):
        d = deltas[tag]["mean"]
        if not d:
            continue
        cells_ = deltas[tag]["cell"]
        bi, wi = d.index(min(d)), d.index(max(d))
        verdict = "no cell regresses" if max(d) <= 2 else f"regresses up to {max(d):+.1f}%"
        w(f"  {tag:16s} best={min(d):+6.1f}% ({cells_[bi]})   "
          f"worst={max(d):+6.1f}% ({cells_[wi]})   -> {verdict}\n")

    # Paired across every (cell, seed) -- the cells are the population here.
    w("\n" + "=" * 118 + "\n")
    w("PAIRED TEST over all (pattern, load, flowsize, seed) points, ON - OFF\n")
    w("=" * 118 + "\n")
    idx = {(r["pattern"], r["load_fraction"], r["flowsize"], r["seed"], r["arm"]): r
           for r in rows}
    for on_arm, off_arm, tag in PAIRS:
        for metric in ("fct_mean", "fct_p99", "rtx_rate_pct", "delay_only_spread"):
            ds = []
            for k, r in idx.items():
                if k[4] != on_arm:
                    continue
                o = idx.get((*k[:4], off_arm))
                if o and not (math.isnan(r[metric]) or math.isnan(o[metric])):
                    ds.append(r[metric] - o[metric])
            if len(ds) < 2:
                continue
            lo, hi = bootstrap_ci(ds)
            _, p = wilcoxon_exact(ds)
            w(f"  {tag:16s} {metric:20s} n={len(ds):4d}  mean diff={mean(ds):+9.3f} "
              f"[{lo:+.3f}, {hi:+.3f}]  p={p:.4g}\n")


# ------------------------------------------------------------------------ plots

def make_plots(rows, plotdir: Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False

    plt.rcParams.update({
        "font.size": 9, "axes.labelsize": 9, "axes.titlesize": 10, "legend.fontsize": 8,
        "xtick.labelsize": 8, "ytick.labelsize": 8, "axes.edgecolor": GRID,
        "axes.labelcolor": INK, "text.color": INK, "xtick.color": INK_MUTED,
        "ytick.color": INK_MUTED, "figure.facecolor": "white", "axes.facecolor": "white",
        "axes.spines.top": False, "axes.spines.right": False, "pdf.fonttype": 42,
    })
    plotdir.mkdir(parents=True, exist_ok=True)

    by_pattern = defaultdict(list)
    for r in rows:
        by_pattern[r["pattern"]].append(r)

    for pattern, prows in by_pattern.items():
        for fs in sorted({r["flowsize"] for r in prows}):
            sub = [r for r in prows if r["flowsize"] == fs]
            g = defaultdict(lambda: defaultdict(list))
            for r in sub:
                g[r["arm"]][r["load_fraction"]].append(r)
            ncols, nrows = 3, -(-len(METRICS) // 3)
            fig, axes = plt.subplots(nrows, ncols, figsize=(4.6 * ncols, 3.4 * nrows))
            axes = axes.flatten()
            for ax, (field, title, ylabel) in zip(axes, METRICS):
                for arm, *_ in ARMS:
                    by_x = g.get(arm)
                    if not by_x:
                        continue
                    colour, style, label = ARM_STYLE[arm]
                    xs = sorted(by_x)
                    ys = [mean(r[field] for r in by_x[x]) for x in xs]
                    if all(math.isnan(y) for y in ys):
                        continue
                    ax.plot(xs, ys, marker="o", markersize=5, linewidth=2.0,
                            color=colour, linestyle=style, label=label, zorder=3)
                ax.set_xlabel("Load fraction"); ax.set_ylabel(ylabel)
                ax.set_title(title, loc="left")
                ax.grid(color=GRID, linewidth=0.6, zorder=0); ax.set_axisbelow(True)
            for ax in axes[len(METRICS):]:
                ax.set_visible(False)
            h, l = axes[0].get_legend_handles_labels()
            fig.legend(h, l, loc="upper center", ncol=4, frameon=False,
                       bbox_to_anchor=(0.5, 1.04))
            fig.suptitle(f"Hop-count RTT baselines ON vs OFF -- {pattern}, "
                         f"{fs/1000:.0f}KB flows, {NODES} nodes (size {TOPO_SIZE})",
                         y=1.09, fontsize=11)
            fig.tight_layout()
            for ext in ("pdf", "png"):
                fig.savefig(plotdir / f"metrics_{pattern}_fs{fs}.{ext}",
                            dpi=200, bbox_inches="tight")
            plt.close(fig)
    return True


# -------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--binary", default=BINARY)
    ap.add_argument("--nodes", type=int, default=NODES)
    ap.add_argument("--size", default=TOPO_SIZE, choices=["s", "m", "l"])
    ap.add_argument("--queue-pkts", type=int, default=QUEUE_SIZE_PKTS)
    ap.add_argument("--end", type=int, default=END_TIME_US)
    ap.add_argument("--patterns", nargs="+", default=list(TRAFFIC_PATTERNS),
                    choices=list(TRAFFIC_PATTERNS))
    ap.add_argument("--loads", nargs="+", type=float, default=None,
                    help="override the per-pattern load list")
    ap.add_argument("--flowsizes", nargs="+", type=int, default=FLOWSIZES)
    ap.add_argument("--seeds", type=int, default=len(SEEDS))
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--no-hop-diag", dest="hop_diag", action="store_false",
                    help="skip -debug_hops (outcome metrics only, much less parsing)")
    ap.add_argument("--outdir", default=str(OUTDIR))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    outdir = Path(a.outdir)
    cmdir, rundir, plotdir = outdir / "connection_matrices", outdir / "runs", outdir / "PLOTS"
    seeds = list(range(1, a.seeds + 1))
    cwd = os.path.dirname(os.path.abspath(a.binary)) or "."

    plan = []
    for pattern in a.patterns:
        loads = a.loads if a.loads is not None else TRAFFIC_PATTERNS[pattern]["loads"]
        for frac in loads:
            for fs in a.flowsizes:
                for seed in seeds:
                    for arm, part, norm, extra in ARMS:
                        # Every parameter that changes the run is in the tag, so a
                        # settings change cannot silently reuse a cached result.
                        tag = (f"{pattern}_{arm}_load{frac:.2f}_fs{fs}_seed{seed}"
                               f"_n{a.nodes}{a.size}_q{a.queue_pkts}_e{a.end}"
                               f"{'_hd' if a.hop_diag else ''}")
                        plan.append(dict(pattern=pattern, load_fraction=frac, flowsize=fs,
                                         seed=seed, arm=arm, partition=part, hopnorm=norm,
                                         extra=extra, tag=tag))

    if a.dry_run:
        print(f"{len(plan)} runs planned "
              f"({len(a.patterns)} patterns x loads x {len(a.flowsizes)} flowsizes "
              f"x {len(seeds)} seeds x {len(ARMS)} arms)")
        for j in plan[:12]:
            print(f"  {j['pattern']:19s} load={j['load_fraction']:.2f} "
                  f"fs={j['flowsize']:>9d} seed={j['seed']} {j['arm']:18s} "
                  f"extra={' '.join(j['extra']) or '(none)'}")
        if len(plan) > 12:
            print(f"  ... and {len(plan)-12} more")
        print("\nMeasured timings on this grid: ~3s per 500KB cell, ~70s per 10MB "
              "permutation cell at load 0.90.")
        return 0

    if not os.access(a.binary, os.X_OK):
        print(f"ERROR: {a.binary} not executable. Build it and run from sim/datacenter/.",
              file=sys.stderr)
        return 1

    cmdir.mkdir(parents=True, exist_ok=True)
    rundir.mkdir(parents=True, exist_ok=True)
    need = {(j["pattern"], j["load_fraction"], j["flowsize"], j["seed"]) for j in plan
            if TRAFFIC_PATTERNS[j["pattern"]]["kind"] == "file"}
    for pattern, frac, fs, seed in sorted(need):
        ensure_cm_file(cmdir, pattern, a.nodes, frac, fs, seed)
    print(f"connection matrices ready ({len(need)})")

    for j in plan:
        j["cmd"] = build_cmd(a, cmdir, j["pattern"], j["load_fraction"], j["flowsize"],
                             j["seed"], j["extra"], rundir / f"{j['tag']}.dat")
        j["cwd"] = cwd
        j["rundir"] = str(rundir)
        j["datpath"] = str(rundir / f"{j['tag']}.dat")

    rows, failures = [], []
    total = len(plan)
    with futures.ProcessPoolExecutor(max_workers=a.jobs) as ex:
        fut = {ex.submit(run_one, j): j for j in plan}
        for i, f in enumerate(futures.as_completed(fut), 1):
            j = fut[f]
            r = f.result()
            if r.get("ok"):
                rows.append({k: v for k, v in j.items()
                             if k in ("pattern", "load_fraction", "flowsize", "seed",
                                      "arm", "partition", "hopnorm")}
                            | {k: v for k, v in r.items() if k not in ("tag", "ok", "cached")})
                mark = "cached" if r.get("cached") else "OK"
                print(f"[{i}/{total}] {j['tag']}: {mark}  "
                      f"mean={r['fct_mean']:.1f} spread={r['delay_only_spread']:.2f}pp")
            else:
                failures.append(j["tag"])
                print(f"[{i}/{total}] {j['tag']}: FAILED (rc={r.get('returncode')}, "
                      f"{r.get('n_flows', 0)} flows)")

    if not rows:
        print("no successful runs", file=sys.stderr)
        return 1

    outdir.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with (outdir / "summary.csv").open("w", newline="") as f:
        wtr = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        wtr.writeheader()
        wtr.writerows(rows)
    print(f"\nWrote {len(rows)} rows to {outdir/'summary.csv'}")
    if failures:
        print(f"{len(failures)} run(s) produced no data:")
        for t in failures[:20]:
            print("  " + t)

    print_report(rows, sys.stdout)
    with (outdir / "report.txt").open("w") as f:
        print_report(rows, f)
    if make_plots(rows, plotdir):
        print(f"\nplots in {plotdir}/")
    else:
        print("\nmatplotlib not installed -- plots skipped; CSV/report still written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
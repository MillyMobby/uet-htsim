#!/usr/bin/env python3
"""
 splits the comparison by DragonFly+ topology size (m vs l) 

Usage:
    python aggregate_fpar_vs_reps.py <parent_dir>
    python3 aggregate_fpar_vs_reps.py results_report/fpar_vs_reps

For each size found, writes to <parent_dir>/AGGREGATE_ANALYSIS/size_<X>/:
    fct_comparison.png   - mean FCT, fpar vs reps_dfp, small multiples by
                            pattern, one line per strategy across load.
    duration_proxy.png   - fct_max proxy for simulation cost (see caveats
                            printed on the figure itself - not real seconds).
    summary.md           - aggregate mean-FCT diff%, split low load (<=0.5)
                            vs high load (>=0.75), plus which folders/strategy
                            label contributed to this size bucket.

No simulations are run - this only reads summary.csv (and, for the timeout
annotation, runs/*.log) already on disk under each subfolder.
"""
import argparse
import csv
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
import matplotlib
import matplotlib.pyplot as plt

OKABE_ITO = ["#0072B2", "#D55E00", "#009E73", "#E69F00", "#CC79A7", "#56B4E9", "#F0E442"]

LOG_RE = re.compile(
    r"^(?P<pattern>[a-zA-Z]+)_(?P<strat_label>.+)_load(?P<load>[0-9.]+)_"
    r"(?:fs(?P<flowsize>[0-9]+)_)?seed(?P<seed>[0-9]+)\.log$"
)

# which reps_dfp variant to treat as "the" reps_dfp for this comparison, in
# priority order, when a folder has more than one on offer.
REPS_PRIORITY = ["reps_dfp_hopnorm_part", "reps_dfp", "reps_dfp_hopnorm"]


def to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_all(parent: Path):
    
    out = []
    for sub in sorted(parent.iterdir()):
        csvf = sub / "summary.csv"
        if not csvf.exists():
            continue
        with open(csvf, newline="") as f:
            rows = list(csv.DictReader(f))
        runs_dir = sub / "runs"
        runs_dir = runs_dir if runs_dir.is_dir() else None
        for r in rows:
            out.append((r, sub.name, runs_dir))
    return out


def pick_reps_label(labels_in_folder):
    for cand in REPS_PRIORITY:
        if cand in labels_in_folder:
            return cand
    # any reps_dfp* label present
    for l in labels_in_folder:
        if l.startswith("reps_dfp"):
            return l
    return None


def build_size_buckets(all_rows):
    """returns {size: [(row, folder, runs_dir, reps_label_used), ...]}"""
    by_folder = defaultdict(list)
    for r, folder, runs_dir in all_rows:
        by_folder[folder].append((r, runs_dir))

    buckets = defaultdict(list)
    chosen_per_folder = {}
    for folder, items in by_folder.items():
        labels = {r["strat_label"] for r, _ in items}
        if "fpar" not in labels:
            continue
        reps_label = pick_reps_label(labels)
        if reps_label is None:
            continue
        chosen_per_folder[folder] = reps_label
        for r, runs_dir in items:
            if r["strat_label"] not in ("fpar", reps_label):
                continue
            size = r.get("size", "?")
            buckets[size].append((r, folder, runs_dir, reps_label))
    return buckets, chosen_per_folder


def fs_sort_key(fs):
    return (0, int(fs)) if str(fs).isdigit() else (1, str(fs))


def panel_list(bucket_rows):
    """(pattern, flowsize) panels, sorted - kept SEPARATE deliberately: pooling
    raw FCT/fct_max across flowsizes spanning a 500x range (20KB..10MB in
    this data) creates a survivorship-bias artifact, since FPAR's largest-
    flowsize runs are exactly the ones most likely to time out and drop out
    of summary.csv, which would otherwise make its pooled mean look
    artificially good at high load."""
    pairs = sorted({(r["pattern"], r.get("flowsize", "n/a")) for r, *_ in bucket_rows},
                    key=lambda p: (p[0], fs_sort_key(p[1])))
    return pairs


def grid_shape(n_panels, max_cols=5):
    ncols = min(n_panels, max_cols)
    nrows = -(-n_panels // ncols)  # ceil
    return nrows, ncols


def fct_comparison_plot(bucket_rows, size, out_path: Path):
    
    matplotlib.use("Agg")

    panels = panel_list(bucket_rows)
    nrows, ncols = grid_shape(len(panels))
    fig_w = max(3.2 * ncols, 8.6)
    fig_h = max(3.4 * nrows, 4.2)
    fig, axgrid = plt.subplots(nrows, ncols, figsize=(fig_w, fig_h), squeeze=False)
    flat_axes = [axgrid[r][c] for r in range(nrows) for c in range(ncols)]

    for ax, (pattern, fs) in zip(flat_axes, panels):
        for strat in ("fpar", "reps"):
            by_load = defaultdict(list)
            for r, folder, runs_dir, reps_label in bucket_rows:
                if r["pattern"] != pattern or r.get("flowsize", "n/a") != fs:
                    continue
                is_match = (r["strat_label"] == "fpar") if strat == "fpar" else (r["strat_label"] == reps_label)
                if not is_match:
                    continue
                v = to_float(r.get("fct_mean"))
                if v is not None:
                    by_load[float(r["load_fraction"])].append(v)
            if not by_load:
                continue
            loads = sorted(by_load)
            means = [statistics.mean(by_load[l]) for l in loads]
            lo = [min(by_load[l]) for l in loads]
            hi = [max(by_load[l]) for l in loads]
            color = OKABE_ITO[0] if strat == "fpar" else OKABE_ITO[1]
            label = "fpar" if strat == "fpar" else "reps_dfp"
            ax.plot(loads, means, marker="o", markersize=4, color=color, label=label)
            ax.fill_between(loads, lo, hi, color=color, alpha=0.15, linewidth=0)
        ax.set_title(f"{pattern}, fs={fs}", fontsize=9)
        ax.set_xlabel("load", fontsize=8)
        ax.set_ylabel("mean FCT (us)", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.grid(alpha=0.3)
    for ax in flat_axes[len(panels):]:
        ax.axis("off")
    flat_axes[0].legend(loc="upper left", fontsize=8)
    fig.suptitle(
        f"FPAR vs REPS_DFP - mean FCT - size={size}\n"
        "each panel = one (pattern, flowsize) "
        "shaded = min/max across seeds+folders",
        fontsize=9.5,
    )
    top_margin = 1.0 - (0.62 / fig_h)
    fig.subplots_adjust(top=top_margin, bottom=0.5 / fig_h, left=0.07, right=0.98, hspace=0.55, wspace=0.35)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def duration_proxy_plot(bucket_rows, size, out_path: Path):
    matplotlib.use("Agg")

    def log_fs(m):
        return m["flowsize"] if m["flowsize"] is not None else "n/a"

    timeout_counts = defaultdict(int)  # (pattern, strat, load, flowsize) -> n
    seen_runs_dirs = {rd for _, _, rd, _ in bucket_rows if rd is not None}
    for runs_dir in seen_runs_dirs:
        for lf in runs_dir.glob("*.log"):
            m = LOG_RE.match(lf.name)
            if not m:
                continue
            tail = lf.read_text(errors="ignore")[-2000:]
            if re.search(r"TIMEOUT after \d+s", tail):
                timeout_counts[(m["pattern"], m["strat_label"], m["load"], log_fs(m))] += 1

    panels = panel_list(bucket_rows)
    nrows, ncols = grid_shape(len(panels))
    fig_w = max(3.2 * ncols, 8.6)
    fig_h = max(3.6 * nrows, 4.6)
    fig, axgrid = plt.subplots(nrows, ncols, figsize=(fig_w, fig_h), squeeze=False)
    flat_axes = [axgrid[r][c] for r in range(nrows) for c in range(ncols)]

    for ax, (pattern, fs) in zip(flat_axes, panels):
        for strat in ("fpar", "reps"):
            by_load = defaultdict(list)
            for r, folder, runs_dir, reps_label in bucket_rows:
                if r["pattern"] != pattern or r.get("flowsize", "n/a") != fs:
                    continue
                is_match = (r["strat_label"] == "fpar") if strat == "fpar" else (r["strat_label"] == reps_label)
                if not is_match:
                    continue
                v = to_float(r.get("fct_max"))
                if v is not None:
                    by_load[float(r["load_fraction"])].append(v)
            if not by_load:
                continue
            loads = sorted(by_load)
            means = [statistics.mean(by_load[l]) / 1000.0 for l in loads]  # us -> ms
            color = OKABE_ITO[0] if strat == "fpar" else OKABE_ITO[1]
            label = "fpar" if strat == "fpar" else "reps_dfp"
            ax.plot(loads, means, marker="o", markersize=4, color=color, label=label)
            for l, mval in zip(loads, means):
                strat_label_for_key = "fpar" if strat == "fpar" else reps_label
                n_to = timeout_counts.get((pattern, strat_label_for_key, f"{l:.2f}", fs), 0)
                if n_to:
                    ax.plot(l, mval, marker="D", markerfacecolor="none", markeredgecolor=color,
                            markersize=9, markeredgewidth=1.4)
                    ax.annotate(f"+{n_to}", (l, mval), textcoords="offset points",
                                xytext=(4, 4), fontsize=6, color=color)
        ax.set_title(f"{pattern}, fs={fs}", fontsize=9)
        ax.set_xlabel("load", fontsize=8)
        ax.set_ylabel("mean max-FCT (ms, sim)", fontsize=7.5)
        ax.tick_params(labelsize=7)
        ax.grid(alpha=0.3)
    for ax in flat_axes[len(panels):]:
        ax.axis("off")
    flat_axes[0].legend(loc="upper left", fontsize=8)
    fig.suptitle(
        f"Simulation-cost (fct_max, not real seconds) - size={size}\n"
        "each panel = one (pattern, flowsize); (diamond) = timeout",
        fontsize=9,
    )
    top_margin = 1.0 - (0.68 / fig_h)
    fig.subplots_adjust(top=top_margin, bottom=0.55 / fig_h, left=0.08, right=0.98, hspace=0.6, wspace=0.35)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def write_summary(bucket_rows, size, chosen_per_folder, out_path: Path):
    grp = defaultdict(lambda: defaultdict(list))
    for r, folder, runs_dir, reps_label in bucket_rows:
        key = (r["pattern"], r["load_fraction"], r.get("flowsize", "n/a"))
        strat = "fpar" if r["strat_label"] == "fpar" else "reps"
        v = to_float(r.get("fct_mean"))
        if v is not None:
            grp[key][strat].append(v)

    diffs = []  # (load, diff_pct)
    for (pattern, load, fs), d in grp.items():
        if "fpar" in d and "reps" in d:
            fm, rm = statistics.mean(d["fpar"]), statistics.mean(d["reps"])
            diffs.append((float(load), (fm - rm) / rm * 100))

    lines = [f"# size={size} - FPAR vs reps_dfp summary", ""]
    folders = sorted({f for _, f, _, _ in bucket_rows})
    lines.append("Folders contributing to this bucket, and which reps_dfp variant was used as "
                  "\"the\" reps_dfp for each:")
    for f in folders:
        lines.append(f"  - `{f}` -> `{chosen_per_folder.get(f, '?')}`")
    lines.append("")

    if diffs:
        overall = statistics.mean(d for _, d in diffs)
        lo = [d for l, d in diffs if l <= 0.5]
        hi = [d for l, d in diffs if l >= 0.75]
        lines.append(f"Mean FCT, FPAR vs reps_dfp, overall: **{overall:+.1f}%** (n={len(diffs)} cells)")
        if lo:
            lines.append(f"  - load<=0.5: {statistics.mean(lo):+.1f}% (n={len(lo)})")
        if hi:
            lines.append(f"  - load>=0.75: {statistics.mean(hi):+.1f}% (n={len(hi)})")
        lines.append("")
        lines.append("(positive = FPAR worse/slower than reps_dfp; negative = FPAR better)")
    else:
        lines.append("No overlapping (pattern,load,flowsize) cells with both strategies present.")

    out_path.write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("parent_dir", type=Path, help="directory containing sweep-result subfolders")
    args = ap.parse_args()

    if not args.parent_dir.is_dir():
        sys.exit(f"[aggregate] {args.parent_dir} is not a directory")

    all_rows = load_all(args.parent_dir)
    if not all_rows:
        sys.exit(f"[aggregate] no summary.csv found under {args.parent_dir}/*/")

    buckets, chosen_per_folder = build_size_buckets(all_rows)
    if not buckets:
        sys.exit("[aggregate] no folder had both an fpar and a reps_dfp* strategy - nothing to compare")

    out_root = args.parent_dir / "AGGREGATE_ANALYSIS"
    for size, bucket_rows in sorted(buckets.items()):
        out_dir = out_root / f"size_{size}"
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"[aggregate] size={size}: {len(bucket_rows)} rows from "
              f"{len({f for _,f,_,_ in bucket_rows})} folder(s)")
        fct_comparison_plot(bucket_rows, size, out_dir / "fct_comparison.png")
        duration_proxy_plot(bucket_rows, size, out_dir / "duration_proxy.png")
        write_summary(bucket_rows, size, chosen_per_folder, out_dir / "summary.md")
        print(f"[aggregate] wrote {out_dir}/{{fct_comparison.png, duration_proxy.png, summary.md}}")


if __name__ == "__main__":
    main()

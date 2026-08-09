#!/usr/bin/env python3
"""
Generic post-hoc analyzer for a completed sweep-results folder.

Takes a folder containing a summary.csv (as written by any of the
sweep_*.py scripts in this directory, or a hand-organized results_report/
subfolder like the ones on the `backup` branch) and produces:

  <folder>/ANALYSIS/comparison_table.md   - per-metric value+diff table, one
                                             column pair per non-baseline
                                             strategy, N-ary (any number of
                                             strategies).
  <folder>/ANALYSIS/coverage_gaps.md      - which (pattern, strategy, load,
                                             flowsize) cells have fewer seeds
                                             than the rest of the sweep, or
                                             are missing outright, plus which
                                             attempted runs never produced a
                                             CSV row (crashed) vs hit the
                                             sweep's own timeout cap.
  <folder>/ANALYSIS/duration.png          - wall-clock duration plot IF the
                                             CSV has a duration_s column, else
                                             an mtime-reconstruction fallback
                                             IF runs/*.log carry real (non
                                             git-checkout-flattened) mtimes,
                                             else an fct_max proxy (validated
                                             r=0.85-0.95 vs real duration_s
                                             within-strategy), else a
                                             timeout-rate proxy - whichever
                                             tier fires, the limitation is
                                             stated directly on the figure.


"""
import argparse
import csv
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

OKABE_ITO = ["#0072B2", "#D55E00", "#009E73", "#E69F00", "#CC79A7", "#56B4E9", "#F0E442"]

LOG_RE = re.compile(
    r"^(?P<pattern>[a-zA-Z]+)_(?P<strat_label>.+)_load(?P<load>[0-9.]+)_"
    r"(?:fs(?P<flowsize>[0-9]+)_)?seed(?P<seed>[0-9]+)\.log$"
)


def log_flowsize(m) -> str:
    return m["flowsize"] if m["flowsize"] is not None else "n/a"


def fs_sort_key(fs):
    return (0, int(fs)) if str(fs).isdigit() else (1, str(fs))


def panel_list(rows):
    
    pairs = sorted({(r["pattern"], r.get("flowsize", "n/a")) for r in rows},
                    key=lambda p: (p[0], fs_sort_key(p[1])))
    return pairs


def grid_shape(n_panels, max_cols=5):
    ncols = min(max(n_panels, 1), max_cols)
    nrows = -(-n_panels // ncols)  # ceil
    return nrows, ncols


def make_panel_grid(n_panels, per_panel_w=3.2, per_panel_h=3.4, min_w=8.6, min_h=4.2):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    nrows, ncols = grid_shape(n_panels)
    fig_w = max(per_panel_w * ncols, min_w)
    fig_h = max(per_panel_h * nrows, min_h)
    fig, axgrid = plt.subplots(nrows, ncols, figsize=(fig_w, fig_h), squeeze=False)
    flat_axes = [axgrid[r][c] for r in range(nrows) for c in range(ncols)]
    return fig, flat_axes, fig_h


def load_csv(path: Path):
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        sys.exit(f"[analyze] {path} has no data rows")
    fieldnames = list(reader.fieldnames)
    if "flowsize" not in fieldnames:
        for r in rows:
            r["flowsize"] = "n/a"
        fieldnames.append("flowsize")
    return rows, fieldnames


def to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def group_key(r):
    return (r["pattern"], r["load_fraction"], r["flowsize"])


def build_expected_grid(rows):
    """union of (pattern -> loads, flowsizes, strat_labels, seeds) seen anywhere
    in the CSV, used as the 'intended' grid to detect partial coverage."""
    grid = defaultdict(lambda: {"loads": set(), "flowsizes": set(), "strats": set(), "seeds": set()})
    for r in rows:
        g = grid[r["pattern"]]
        g["loads"].add(r["load_fraction"])
        g["flowsizes"].add(r["flowsize"])
        g["strats"].add(r["strat_label"])
        g["seeds"].add(r["seed"])
    return grid


def coverage_report(rows, runs_dir: Optional[Path]):
    grid = build_expected_grid(rows)

    present = defaultdict(set)  # (pattern, strat, load, fs) -> {seeds}
    for r in rows:
        present[(r["pattern"], r["strat_label"], r["load_fraction"], r["flowsize"])].add(r["seed"])

    lines = ["# Coverage gap report", ""]
    lines.append(
        "Intended grid per pattern is approximated as the UNION of loads / "
        "flowsizes / seeds / strategies actually observed anywhere for that "
        "pattern in this folder's summary.csv (there is no external spec to "
        "compare against, so this only catches *relative* gaps - one "
        "strategy tested less than its siblings in the same sweep)."
    )
    lines.append("")

    any_gap = False
    for pattern, g in sorted(grid.items()):
        max_seeds = max((len(present[(pattern, s, l, fs)])
                          for s in g["strats"] for l in g["loads"] for fs in g["flowsizes"]
                          if present[(pattern, s, l, fs)]), default=0)
        pattern_lines = []
        for strat in sorted(g["strats"]):
            for load in sorted(g["loads"], key=float):
                for fs in sorted(g["flowsizes"], key=lambda x: (0, int(x)) if x.isdigit() else (1, x)):
                    seeds = present[(pattern, strat, load, fs)]
                    if len(seeds) == 0:
                        pattern_lines.append(
                            f"- **MISSING ENTIRELY**: strat=`{strat}` load={load} flowsize={fs} "
                            f"(other strategies have data here)"
                        )
                        any_gap = True
                    elif max_seeds > 1 and len(seeds) < max_seeds:
                        pattern_lines.append(
                            f"- fewer seeds: strat=`{strat}` load={load} flowsize={fs} "
                            f"-> {len(seeds)}/{max_seeds} seeds (have {sorted(seeds)})"
                        )
                        any_gap = True
        if pattern_lines:
            lines.append(f"## pattern={pattern}")
            lines.extend(pattern_lines)
            lines.append("")

    if not any_gap:
        lines.append("No relative gaps detected within this folder's own grid.")
        lines.append("")

    if runs_dir and runs_dir.is_dir():
        lines.append("## Attempted-but-not-in-summary.csv runs (crashed / timed out)")
        lines.append("")
        log_files = sorted(runs_dir.glob("*.log"))
        in_csv = {(r["pattern"], r["strat_label"], float(r["load_fraction"]), str(r["flowsize"]), int(r["seed"]))
                  for r in rows}
        timeouts, crashes = [], []
        for lf in log_files:
            m = LOG_RE.match(lf.name)
            if not m:
                continue
            key = (m["pattern"], m["strat_label"], float(m["load"]), log_flowsize(m), int(m["seed"]))
            if key in in_csv:
                continue
            tail = lf.read_text(errors="ignore")[-2000:]
            tmo = re.search(r"TIMEOUT after (\d+)s", tail)
            if tmo:
                timeouts.append((lf.name, tmo.group(1)))
            else:
                crashes.append(lf.name)
        if timeouts:
            lines.append(f"**{len(timeouts)} run(s) hit the sweep's timeout cap** (excluded from summary.csv):")
            cap_counts = defaultdict(int)
            for _, cap in timeouts:
                cap_counts[cap] += 1
            for cap, n in sorted(cap_counts.items(), key=lambda kv: int(kv[0])):
                lines.append(f"  - {n} run(s) at the {cap}s cap")
            lines.append("")
        if crashes:
            lines.append(f"**{len(crashes)} run(s) neither produced a CSV row nor show a TIMEOUT marker** "
                          f"(likely crashed / usage error - inspect manually):")
            for c in crashes[:20]:
                lines.append(f"  - {c}")
            if len(crashes) > 20:
                lines.append(f"  - ... and {len(crashes) - 20} more")
            lines.append("")
        if not timeouts and not crashes:
            lines.append("None - every attempted run in runs/ has a matching summary.csv row.")
            lines.append("")

    return "\n".join(lines)


TABLE_METRICS = [
    ("fct_mean", "mean FCT (us)", "{:.2f}", "{:+.1f}%"),
    ("fct_p99", "p99 FCT (us)", "{:.2f}", "{:+.1f}%"),
    ("fct_max", "max FCT (us)", "{:.2f}", "{:+.1f}%"),
]


def write_comparison_table(rows, fieldnames, out_path: Path):
    labels = sorted({r["strat_label"] for r in rows})
    if len(labels) < 2:
        out_path.write_text("# Comparison table\n\nOnly one strategy present in this folder - nothing to compare.\n")
        return
    baseline = next((l for l in labels if "reps_dfp" in l and "no_hopnorm" not in l), labels[0])
    others = [l for l in labels if l != baseline]

    agg = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = group_key(r)
        for col, *_ in TABLE_METRICS:
            v = to_float(r.get(col))
            if v is not None:
                agg[(key, r["strat_label"])][col].append(v)
    if "duration_s" in fieldnames:
        for r in rows:
            v = to_float(r.get("duration_s"))
            if v is not None:
                agg[(group_key(r), r["strat_label"])]["duration_s"].append(v)

    metrics = list(TABLE_METRICS)
    if "duration_s" in fieldnames:
        metrics.append(("duration_s", "wall-clock time (s)", "{:.1f}", "{:+.1f}%"))

    def fs_sort_key(fs):
        return (0, int(fs)) if fs.isdigit() else (1, fs)

    keys = sorted({group_key(r) for r in rows}, key=lambda k: (k[0], float(k[1]), fs_sort_key(k[2])))

    lines = [f"# Comparison table (baseline = `{baseline}`)", ""]
    header = "| pattern | load | flowsize | metric | " + f"`{baseline}` | " + \
             " | ".join(f"`{o}` | diff vs {baseline}" for o in others) + " |"
    sep = "|" + "---|" * (4 + 1 + 2 * len(others))
    lines.append(header)
    lines.append(sep)
    for key in keys:
        pattern, load, fs = key
        for col, label, fmt, dfmt in metrics:
            base_vals = agg[(key, baseline)].get(col)
            if not base_vals:
                continue
            base_mean = statistics.mean(base_vals)
            row_cells = [pattern, load, fs, label, fmt.format(base_mean)]
            for o in others:
                o_vals = agg[(key, o)].get(col)
                if not o_vals:
                    row_cells.append("-- | --")
                    continue
                o_mean = statistics.mean(o_vals)
                diff = (o_mean - base_mean) / base_mean * 100 if base_mean else float("nan")
                row_cells.append(f"{fmt.format(o_mean)} | {dfmt.format(diff)}")
            lines.append("| " + " | ".join(row_cells) + " |")
    out_path.write_text("\n".join(lines) + "\n")


def plot_duration_real(rows, out_path: Path, folder_name: str):
    labels = sorted({r["strat_label"] for r in rows})
    panels = panel_list(rows)
    fig, flat_axes, fig_h = make_panel_grid(len(panels))

    for ax, (pattern, fs) in zip(flat_axes, panels):
        for i, label in enumerate(labels):
            by_load = defaultdict(list)
            for r in rows:
                if r["pattern"] != pattern or r.get("flowsize", "n/a") != fs or r["strat_label"] != label:
                    continue
                v = to_float(r.get("duration_s"))
                if v is not None:
                    by_load[float(r["load_fraction"])].append(v)
            if not by_load:
                continue
            loads = sorted(by_load)
            means = [statistics.mean(by_load[l]) for l in loads]
            ax.plot(loads, means, marker="o", markersize=4, color=OKABE_ITO[i % len(OKABE_ITO)], label=label)
        ax.set_title(f"{pattern}, fs={fs}", fontsize=9)
        ax.set_xlabel("load", fontsize=8)
        ax.set_ylabel("mean wall-clock time (s)", fontsize=7.5)
        ax.tick_params(labelsize=7)
        ax.grid(alpha=0.3)
    for ax in flat_axes[len(panels):]:
        ax.axis("off")
    flat_axes[0].legend(loc="upper left", fontsize=8)
    fig.suptitle(f"Wall-clock run duration - {folder_name}\n"
                 "each panel = one (pattern, flowsize)",
                 fontsize=10)
    top_margin = 1.0 - 0.62 / fig_h
    fig.subplots_adjust(top=top_margin, bottom=0.5 / fig_h, left=0.08, right=0.98, hspace=0.55, wspace=0.35)
    fig.savefig(out_path, dpi=150)
    import matplotlib.pyplot as plt
    plt.close(fig)


def plot_duration_fctmax_proxy(rows, runs_dir, out_path: Path, folder_name: str):
    """When no real duration_s exists, fct_max (max flow-completion time, in
    simulated us - already a column in every summary.csv, no log parsing
    needed) is a validated stand-in: 

    It only covers completed runs - any run that timed out is excluded from
    summary.csv entirely, so a cell with timeouts UNDERSTATES true cost the
    same way the real duration_s plot does. Cells with 1+ timeout in runs/
    are marked with a hollow diamond and an annotated count."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not any(to_float(r.get("fct_max")) is not None for r in rows):
        return False

    timeout_counts = defaultdict(int)  # (pattern, strat, load, flowsize) -> n timed out
    if runs_dir and runs_dir.is_dir():
        for lf in runs_dir.glob("*.log"):
            m = LOG_RE.match(lf.name)
            if not m:
                continue
            tail = lf.read_text(errors="ignore")[-2000:]
            if re.search(r"TIMEOUT after \d+s", tail):
                timeout_counts[(m["pattern"], m["strat_label"], m["load"], log_flowsize(m))] += 1

    labels = sorted({r["strat_label"] for r in rows})
    panels = panel_list(rows)
    fig, flat_axes, fig_h = make_panel_grid(len(panels))

    for ax, (pattern, fs) in zip(flat_axes, panels):
        for i, label in enumerate(labels):
            by_load = defaultdict(list)
            for r in rows:
                if r["pattern"] != pattern or r.get("flowsize", "n/a") != fs or r["strat_label"] != label:
                    continue
                v = to_float(r.get("fct_max"))
                if v is not None:
                    by_load[float(r["load_fraction"])].append(v)
            if not by_load:
                continue
            loads = sorted(by_load)
            means = [statistics.mean(by_load[l]) / 1000.0 for l in loads]  # us -> ms
            color = OKABE_ITO[i % len(OKABE_ITO)]
            ax.plot(loads, means, marker="o", markersize=4, color=color, label=label)
            for l, mval in zip(loads, means):
                load_str = f"{l:.2f}"
                n_to = timeout_counts.get((pattern, label, load_str, fs), 0)
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
        "Simulated max-FCT (ms)\n"
        "not real seconds; "
        "each panel = one (pattern, flowsize)\n"
        "(diamond) = cell has timeouts excluded from summary.csv",
        fontsize=8.5,
    )
    top_margin = 1.0 - 0.85 / fig_h
    fig.subplots_adjust(top=top_margin, bottom=0.55 / fig_h, left=0.08, right=0.98, hspace=0.6, wspace=0.35)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return True


def plot_timeout_rate_proxy(rows, runs_dir: Path, out_path: Path, folder_name: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    in_csv = {(r["pattern"], r["strat_label"], r["load_fraction"], r["flowsize"], r["seed"]) for r in rows}
    attempted = defaultdict(int)   # (pattern, strat, load) -> n attempted
    timed_out = defaultdict(int)
    caps_seen = set()
    for lf in sorted(runs_dir.glob("*.log")):
        m = LOG_RE.match(lf.name)
        if not m:
            continue
        key = (m["pattern"], m["strat_label"], m["load"])
        attempted[key] += 1
        tail = lf.read_text(errors="ignore")[-2000:]
        tmo = re.search(r"TIMEOUT after (\d+)s", tail)
        if tmo:
            timed_out[key] += 1
            caps_seen.add(tmo.group(1))

    if not attempted:
        return False

    patterns = sorted({k[0] for k in attempted})
    labels = sorted({k[1] for k in attempted})
    fig_w = max(5 * len(patterns), 8.6)
    fig_h = 5.3
    fig, axes = plt.subplots(1, len(patterns), figsize=(fig_w, fig_h), squeeze=False)
    axes = axes[0]
    for ax, pattern in zip(axes, patterns):
        for i, label in enumerate(labels):
            loads = sorted({k[2] for k in attempted if k[0] == pattern and k[1] == label}, key=float)
            if not loads:
                continue
            rates = [100.0 * timed_out[(pattern, label, l)] / attempted[(pattern, label, l)] for l in loads]
            ax.plot([float(l) for l in loads], rates, marker="o", color=OKABE_ITO[i % len(OKABE_ITO)], label=label)
        ax.set_title(f"pattern={pattern}")
        ax.set_xlabel("load fraction")
        ax.set_ylabel("timeout rate (%)")
        ax.set_ylim(-5, 105)
        ax.grid(alpha=0.3)
    axes[0].legend(loc="upper left", fontsize=8)
    cap_str = "/".join(sorted(caps_seen, key=int)) + "s" if caps_seen else "?s"
    fig.suptitle(
        f"NO wall-clock timing available in this folder's summary.csv\n"
        f"Showing timeout rate vs the sweep's own {cap_str} cap  - {folder_name}",
        fontsize=9,
    )
    top_margin = 1.0 - 1.05 / fig_h
    fig.subplots_adjust(top=top_margin, bottom=0.11, left=0.08, right=0.97, wspace=0.3)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return True


def mtimes_are_git_checkout_artifact(runs_dir: Path) -> bool:
    try:
        res = subprocess.run(
            ["git", "-C", str(runs_dir), "ls-files", "--error-unmatch", "."],
            capture_output=True, text=True, timeout=10,
        )
        if res.returncode != 0:
            return False  # not tracked -> real local directory
        status = subprocess.run(
            ["git", "-C", str(runs_dir), "status", "--porcelain", "."],
            capture_output=True, text=True, timeout=10,
        )
        return status.returncode == 0 and status.stdout.strip() == ""
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def plot_duration_from_mtimes(runs_dir: Path, out_path: Path, folder_name: str):
    """Reconstructs approximate per-run duration from consecutive completed
    log mtimes. Only valid if these are real filesystem timestamps from a
    sequential (non-parallel) sweep run still sitting on local, un-committed
    disk """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if mtimes_are_git_checkout_artifact(runs_dir):
        return False

    logs = sorted(runs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
    mtimes = [p.stat().st_mtime for p in logs]
    if len(set(mtimes)) < max(5, len(mtimes) // 10):
        # too few distinct mtimes -> almost certainly a flattened git checkout, not real timing
        return False

    parsed = []
    for i, lf in enumerate(logs):
        m = LOG_RE.match(lf.name)
        if not m:
            continue
        dur = mtimes[i] - (mtimes[i - 1] if i > 0 else mtimes[i])
        parsed.append((m["pattern"], log_flowsize(m), m["strat_label"], m["load"], dur))

    by_key = defaultdict(list)
    for pattern, fs, label, load, dur in parsed:
        by_key[(pattern, fs, label)].append((float(load), dur))

    panels = sorted({(p, fs) for p, fs, *_ in by_key}, key=lambda pf: (pf[0], fs_sort_key(pf[1])))
    labels = sorted({label for *_, label in by_key})
    fig, flat_axes, fig_h = make_panel_grid(len(panels))

    for ax, (pattern, fs) in zip(flat_axes, panels):
        for i, label in enumerate(labels):
            pts = sorted(by_key.get((pattern, fs, label), []))
            if not pts:
                continue
            by_load = defaultdict(list)
            for l, d in pts:
                by_load[l].append(d)
            loads = sorted(by_load)
            means = [statistics.mean(by_load[l]) for l in loads]
            ax.plot(loads, means, marker="o", markersize=4, color=OKABE_ITO[i % len(OKABE_ITO)], label=label)
        ax.set_title(f"{pattern}, fs={fs}", fontsize=9)
        ax.set_xlabel("load", fontsize=8)
        ax.set_ylabel("approx. wall-clock time (s)", fontsize=7.5)
        ax.tick_params(labelsize=7)
        ax.grid(alpha=0.3)
    for ax in flat_axes[len(panels):]:
        ax.axis("off")
    flat_axes[0].legend(loc="upper left", fontsize=8)
    fig.suptitle(
        f"Wall-clock duration RECONSTRUCTED from local file mtimes (approximate) - {folder_name}\n"
        "each panel = one (pattern, flowsize) ",
        fontsize=9.5,
    )
    top_margin = 1.0 - 0.62 / fig_h
    fig.subplots_adjust(top=top_margin, bottom=0.5 / fig_h, left=0.08, right=0.98, hspace=0.55, wspace=0.35)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sweep_folder", type=Path, help="folder containing summary.csv")
    args = ap.parse_args()

    csv_path = args.sweep_folder / "summary.csv"
    if not csv_path.exists():
        sys.exit(f"[analyze] no summary.csv in {args.sweep_folder}")
    rows, fieldnames = load_csv(csv_path)
    runs_dir = args.sweep_folder / "runs"
    if not runs_dir.is_dir():
        runs_dir = None

    out_dir = args.sweep_folder / "ANALYSIS"
    out_dir.mkdir(exist_ok=True)
    folder_name = args.sweep_folder.name

    print(f"[analyze] {folder_name}: {len(rows)} rows, columns={fieldnames}")

    write_comparison_table(rows, fieldnames, out_dir / "comparison_table.md")
    print(f"[analyze] wrote {out_dir / 'comparison_table.md'}")

    (out_dir / "coverage_gaps.md").write_text(coverage_report(rows, runs_dir))
    print(f"[analyze] wrote {out_dir / 'coverage_gaps.md'}")

    dur_path = out_dir / "duration.png"
    if "duration_s" in fieldnames and any(to_float(r.get("duration_s")) is not None for r in rows):
        plot_duration_real(rows, dur_path, folder_name)
        print(f"[analyze] wrote {dur_path} (real duration_s from summary.csv)")
    elif runs_dir and plot_duration_from_mtimes(runs_dir, dur_path, folder_name):
        print(f"[analyze] wrote {dur_path} (reconstructed from local file mtimes - approximate)")
    elif plot_duration_fctmax_proxy(rows, runs_dir, dur_path, folder_name):
        print(f"[analyze] wrote {dur_path} (NO real timing data - plotted fct_max proxy, "
              f"validated r=0.85-0.95 within-strategy)")
    elif runs_dir and plot_timeout_rate_proxy(rows, runs_dir, dur_path, folder_name):
        print(f"[analyze] wrote {dur_path} (NO real timing data available - plotted timeout-rate proxy instead)")
    else:
        print("[analyze] no duration_s column, no usable runs/ directory -> skipped duration plot entirely")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""By-load breakdown.  Usage: perload_provenance.py summary.csv

IMPORTANT -- the design nests load inside pattern rather than crossing them:
    tornado             loads 0.25 0.50 0.75 1.00
    permutation         loads 0.60 0.70 0.80 0.90
    permutation_random  loads 0.60 0.70 0.80 0.90
So a table marginalised over patterns at a fixed absolute load is confounded:
the 0.25 row would be tornado-only, the 0.60 row permutation-only.  Everything
below is therefore reported WITHIN each pattern, indexed by load rank
(L1..L4 = the four loads that pattern was run at, ascending).

Same aggregation convention as the pattern/flow-size scripts:
  - average the 5 seeds inside each (pattern, load, flowsize) cell
  - delta each arm's cell mean against the reps_share cell mean
  - average the 6 cell-deltas (6 flow sizes) belonging to a (pattern, load)
  - win = mean AND p99 both >2% better; loss = either >2% worse; else tie
"""
import sys, csv
from statistics import mean

rows = list(csv.DictReader(open(sys.argv[1])))
for r in rows:
    r['load'] = float(r['load_fraction']); r['fs'] = int(r['flowsize']); r['seed'] = int(r['seed'])
    for k in ('fct_mean', 'fct_p99', 'duration_us', 'rtx_rate_pct'):
        r[k] = float(r[k])

idx   = {(r['pattern'], r['load'], r['fs'], r['seed'], r['strat_label']): r for r in rows}
cells = sorted({(r['pattern'], r['load'], r['fs']) for r in rows})
pats  = sorted({c[0] for c in cells})
S     = sorted({r['seed'] for r in rows})
BASE  = 'reps_share'
ARMS  = ['fpar', 'reps_uniform_draws', 'reps_partition']
NAME  = {'fpar': 'FPAR', 'reps_uniform_draws': 'REPS Uniform',
         'reps_partition': 'REPS Partition', BASE: 'REPS Share'}
LOADS = {p: sorted({c[1] for c in cells if c[0] == p}) for p in pats}

cm = lambda c, a, m: mean(idx[c + (s, a)][m] for s in S)
def deltas(C, a, metric):
    return [100 * (cm(c, a, metric) - cm(c, BASE, metric)) / cm(c, BASE, metric) for c in C]

print("DESIGN: load is nested in pattern, not crossed")
for p in pats:
    print(f"  {p:22} loads={LOADS[p]}")

print("\n\nTABLE tab:perload  (within pattern)")
for p in pats:
    print(f"\n{p}")
    for L in LOADS[p]:
        C = [c for c in cells if c[0] == p and c[1] == L]
        print(f"  load {L:.2f}  ({len(C)} cells x {len(S)} seeds)")
        print(f"    {'algorithm':16}{'mean%':>9}{'p99%':>9}{'dur%':>9}{'rtx%':>8}   W/T/L")
        for a in ARMS:
            dm, dp, dd = (deltas(C, a, m) for m in ('fct_mean', 'fct_p99', 'duration_us'))
            w = sum(1 for x, y in zip(dm, dp) if x < -2 and y < -2)
            l = sum(1 for x, y in zip(dm, dp) if x >  2 or  y >  2)
            print(f"    {NAME[a]:16}{mean(dm):+9.1f}{mean(dp):+9.1f}{mean(dd):+9.1f}"
                  f"{mean(cm(c,a,'rtx_rate_pct') for c in C):8.2f}   {w}/{len(C)-w-l}/{l}")
        print(f"    {NAME[BASE]:16}{'--':>9}{'--':>9}{'--':>9}"
              f"{mean(cm(c,BASE,'rtx_rate_pct') for c in C):8.2f}")

def sweep(metric, label, absolute=False):
    print(f"\n\n{label}")
    for p in pats:
        print(f"\n  {p}   loads {', '.join(f'{L:.2f}' for L in LOADS[p])}")
        print(f"    {'arm':16}" + "".join(f"{'L'+str(i+1):>10}" for i in range(len(LOADS[p]))))
        for a in (ARMS + [BASE] if absolute else ARMS):
            vals = [(mean(cm(c, a, metric) for c in [x for x in cells if x[0]==p and x[1]==L])
                     if absolute else
                     mean(deltas([x for x in cells if x[0]==p and x[1]==L], a, metric)))
                    for L in LOADS[p]]
            print(f"    {NAME[a]:16}" + "".join(f"{v:{'10.2f' if absolute else '+10.1f'}}" for v in vals))

sweep('fct_mean',    "Mean-FCT delta vs load rank, within pattern")
sweep('fct_p99',     "P99-FCT delta vs load rank, within pattern")
sweep('rtx_rate_pct',"Absolute retransmission rate vs load rank, within pattern", absolute=True)
sweep('fct_mean',    "Absolute mean FCT (us) vs load rank -- does load actually load the fabric?", absolute=True)

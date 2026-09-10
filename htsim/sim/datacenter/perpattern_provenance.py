#!/usr/bin/env python3
"""Regenerates Table tab:perpattern. Usage: perpattern_provenance.py summary.csv

"""
import sys, csv
from statistics import mean

rows = list(csv.DictReader(open(sys.argv[1])))
for r in rows:
    r['load'] = float(r['load_fraction']); r['fs'] = int(r['flowsize']); r['seed'] = int(r['seed'])
    for k in ('fct_mean','fct_p99','duration_us','fct_p50','fct_max','rtx_rate_pct'):
        r[k] = float(r[k])
idx = {(r['pattern'], r['load'], r['fs'], r['seed'], r['strat_label']): r for r in rows}
cells = sorted({(r['pattern'], r['load'], r['fs']) for r in rows})
S = [1, 2, 3, 4, 5]
cm = lambda c, a, m: mean(idx[c + (s, a)][m] for s in S)      # cell mean over seeds
NAME = {'fpar':'FPAR', 'reps_uniform_draws':'REPS Uniform', 'reps_partition':'REPS Partition'}

for p in ['tornado', 'permutation', 'permutation_random']:
    C = [c for c in cells if c[0] == p]
    print(f"\n{p}  ({len(C)} cells x {len(S)} seeds)")
    print(f"  {'algorithm':16}{'mean%':>9}{'p99%':>9}{'dur%':>9}{'rtx%':>8}{'tail':>7}   W/T/L")
    for a in ['fpar', 'reps_uniform_draws', 'reps_partition']:
        dm = [100*(cm(c,a,'fct_mean')   -cm(c,'reps_share','fct_mean'))   /cm(c,'reps_share','fct_mean')    for c in C]
        dp = [100*(cm(c,a,'fct_p99')    -cm(c,'reps_share','fct_p99'))    /cm(c,'reps_share','fct_p99')     for c in C]
        dd = [100*(cm(c,a,'duration_us')-cm(c,'reps_share','duration_us'))/cm(c,'reps_share','duration_us') for c in C]
        w = sum(1 for x, y in zip(dm, dp) if x < -2 and y < -2)
        l = sum(1 for x, y in zip(dm, dp) if x >  2 or  y >  2)
        tail = mean(idx[c+(s,a)]['fct_max']/idx[c+(s,a)]['fct_p50'] for c in C for s in S)
        print(f"  {NAME[a]:16}{mean(dm):+9.1f}{mean(dp):+9.1f}{mean(dd):+9.1f}"
              f"{mean(cm(c,a,'rtx_rate_pct') for c in C):8.2f}{tail:7.2f}   {w}/{len(C)-w-l}/{l}")
    tb = mean(idx[c+(s,'reps_share')]['fct_max']/idx[c+(s,'reps_share')]['fct_p50'] for c in C for s in S)
    print(f"  {'REPS Share':16}{'--':>9}{'--':>9}{'--':>9}"
          f"{mean(cm(c,'reps_share','rtx_rate_pct') for c in C):8.2f}{tb:7.2f}")

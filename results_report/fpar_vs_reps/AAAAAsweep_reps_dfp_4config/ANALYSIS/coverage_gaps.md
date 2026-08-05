# Coverage gap report

Intended grid per pattern is approximated as the UNION of loads / flowsizes / seeds / strategies actually observed anywhere for that pattern in this folder's summary.csv (there is no external spec to compare against, so this only catches *relative* gaps - one strategy tested less than its siblings in the same sweep).

## pattern=tornado
- fewer seeds: strat=`fpar` load=0.75 flowsize=10000000 -> 4/5 seeds (have ['2', '3', '4', '5'])
- fewer seeds: strat=`fpar` load=1.0 flowsize=10000000 -> 4/5 seeds (have ['1', '2', '4', '5'])

## Attempted-but-not-in-summary.csv runs (crashed / timed out)

**2 run(s) hit the sweep's timeout cap** (excluded from summary.csv):
  - 2 run(s) at the 1000s cap
